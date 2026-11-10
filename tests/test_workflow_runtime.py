"""Workflow orchestration with real agents (fake SDK): order, dependencies, context, inputs, retries, approval."""

from __future__ import annotations

import asyncio

import pytest

from core.context import ContextItem, ContextScope
from core.errors import InputValidationError, InvalidTransitionError, ReferenceValidationError
from core.registry import HarnessRegistry
from runtime.state import ExecutionStatus, StageStatus

from fakes import FakeSDKAdapter, error_result, succeed

STAGE_AGENTS = ["threat-modeler", "secrets-scanner", "security-sast-engineer", "security-auditor"]


def run(coro):
    return asyncio.run(coro)


def start(runner, workspace, **inputs):
    return runner.start("security-review", {"repository": str(workspace), **inputs}, workspace=workspace).execution_id


def test_sequential_stages_follow_the_dag_and_stop_at_the_gate(make_runner, workspace, manager) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    outcome = run(runner.run_workflow(eid))
    # each stage runs its agent's harness definition under the session name dae-<id> (Phase 5.1, AGT-02)
    assert [list(s.options.agents)[0] for s in adapter.calls] == [f"dae-{a}" for a in STAGE_AGENTS]
    assert [s.stage_id for s in outcome.stages] == ["threat-model", "secrets-scan", "sast", "audit"]
    assert outcome.awaiting_approval and outcome.plan.awaiting_approval == ("audit",)
    assert outcome.execution.status is ExecutionStatus.RUNNING


def test_stage_dependency_enforcement(make_runner, workspace) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    with pytest.raises(InvalidTransitionError, match="dependencies not satisfied: secrets-scan, sast"):
        run(runner.run_stage(eid, "audit"))
    assert adapter.calls == []


def test_stage_context_restrictions(make_runner, workspace, context_manager, manager) -> None:
    context_manager.add(ContextItem("arch", ContextScope.PROJECT, uri="repo://docs/arch.md", tags={"architecture"}))
    context_manager.add(ContextItem("billing", ContextScope.PROJECT, uri="repo://billing.md", tags={"billing"}))
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace, branch="main")
    run(runner.run_workflow(eid))
    tasks = {r.stage_id: r.task for r in manager.store.list_agent_runs(eid)}

    threat_refs = {r["id"] for r in tasks["threat-model"]["context"]["refs"]}
    assert threat_refs == {"arch"}                                 # stage tags [architecture, security]
    # a stage sees only the inputs it declares (repository), never the whole input set (branch)
    assert tasks["threat-model"]["extensions"]["dae.inputs"] == {"repository": str(workspace)}
    # upstream outputs come only from *declared* dependencies, by reference
    audit_refs = {r["id"] for r in tasks["audit"]["context"]["refs"]}
    assert audit_refs == {"upstream.secrets-scan.secrets-findings", "upstream.sast.sast-findings"}
    assert all(r["uri"].startswith("file:") for r in tasks["audit"]["context"]["refs"])
    # secrets-scanner's registered scopes are project+task: workflow-scope items never reach it
    assert all(r["scope"] in ("project", "task") for r in tasks["secrets-scan"]["context"]["refs"])
    assert "`repository`" in adapter.calls[0].prompt and "branch" not in adapter.calls[0].prompt


# ---------------------------------------------------------------------- inputs


def test_missing_workflow_input(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    with pytest.raises(InputValidationError) as info:
        runner.start("security-review", {}, workspace=workspace)
    assert [(i.code, i.message) for i in info.value.issues] == [("schema", "'repository' is a required property")]
    assert manager.store.list_executions() == []                  # nothing persisted


@pytest.mark.parametrize("inputs, fragment", [
    ({"repository": 42}, "42 is not of type 'string'"),
    ({"repository": "x", "branch": ["main"]}, "is not of type 'string'"),
])
def test_invalid_workflow_input_type(make_runner, workspace, inputs, fragment) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    with pytest.raises(InputValidationError, match=fragment):
        runner.start("security-review", inputs, workspace=workspace)


def test_allowed_values_and_stage_specific_requirements(harness_root, edit_yaml, store, workspace) -> None:
    from runtime.execution import ExecutionManager

    def mutate(doc):
        doc["inputs"]["properties"]["depth"] = {"enum": ["quick", "full"]}
        doc["stages"][3]["inputs"] = ["repository", "branch"]               # audit also needs the branch
        doc["stages"][0]["objective"] = "Threat-model ${inputs.repository} at depth ${inputs.depth}."

    edit_yaml("workflows/security-review.workflow.yaml", mutate)
    manager = ExecutionManager(store, HarnessRegistry.load(harness_root))
    with pytest.raises(InputValidationError) as info:
        manager.start_execution("security-review", {"repository": "r", "depth": "deep"})
    assert {(i.code, i.pointer) for i in info.value.issues} == {("schema", "depth"), ("missing-input", "branch")}
    execution = manager.start_execution("security-review", {"repository": "r", "depth": "full", "branch": "main"})
    assert execution.inputs["branch"] == "main"


def test_input_reference_rendered_into_task(harness_root, edit_yaml, store, context_manager) -> None:
    from runtime.execution import ExecutionManager

    edit_yaml("workflows/security-review.workflow.yaml", lambda d: d["stages"][0].update(
        objective="Threat-model the project at ${inputs.repository}."))
    registry = HarnessRegistry.load(harness_root)
    manager = ExecutionManager(store, registry)
    eid = manager.start_execution("security-review", {"repository": "/src/shop"}).execution_id
    manager.start_stage(eid, "threat-model")
    stage = registry.workflows.get("security-review").stage("threat-model")
    bundle = context_manager.assemble_for_stage(registry.agents.get("threat-modeler"), stage, "t")
    task = manager.create_task(eid, "threat-model", context=bundle, estimator=context_manager.estimator).task
    assert task["objective"] == "Threat-model the project at /src/shop."


@pytest.mark.parametrize("mutate", [
    lambda d: d["stages"][0].update(inputs=["repo"]),
    lambda d: d["stages"][0].update(objective="Scan ${inputs.nope}."),
])
def test_invalid_input_references_fail_validation(harness_root, edit_yaml, mutate) -> None:
    edit_yaml("workflows/security-review.workflow.yaml", mutate)
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(harness_root)
    [issue] = info.value.issues
    assert issue.code == "unknown-input" and issue.pointer == "stages/0/inputs"


def test_invalid_inputs_schema_fails_validation(harness_root, edit_yaml) -> None:
    edit_yaml("workflows/security-review.workflow.yaml", lambda d: d["inputs"].update(type="nonsense"))
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(harness_root)
    assert "invalid-inputs-schema" in {i.code for i in info.value.issues}


# ---------------------------------------------------------------------- retries & approval


def test_retry_limits(make_runner, workspace, manager) -> None:
    adapter = FakeSDKAdapter(error_result("success", api_error_status=529, terminal_reason="api_error"))
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    outcome = run(runner.run_workflow(eid))
    assert len(adapter.calls) == 2                                    # workflow-config default max_attempts: 2
    assert [s.stage_id for s in outcome.stages] == ["threat-model", "threat-model"]
    assert outcome.plan.exhausted == ("threat-model",)
    assert outcome.execution.status is ExecutionStatus.FAILED
    assert [e.event for e in manager.store.list_logs(eid)].count("stage.retry") == 1


def test_retry_then_success(make_runner, workspace) -> None:
    adapter = FakeSDKAdapter(error_result("success", api_error_status=529, terminal_reason="api_error"), succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    outcome = run(runner.run_workflow(eid))
    assert [s.stage_status for s in outcome.stages[:2]] == [StageStatus.FAILED, StageStatus.SUCCEEDED]
    assert outcome.plan.awaiting_approval == ("audit",)


def test_non_retryable_failure_is_not_retried(make_runner, workspace) -> None:
    adapter = FakeSDKAdapter(error_result("error_max_budget_usd"))
    runner = make_runner(adapter)
    outcome = run(runner.run_workflow(start(runner, workspace)))
    assert len(adapter.calls) == 1 and outcome.stages[0].failure.code == "agent.budget_exceeded"


def test_human_approval_gate(make_runner, workspace, manager) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    run(runner.run_workflow(eid))
    assert manager.store.get_stage(eid, "audit").status is StageStatus.AWAITING_APPROVAL
    manager.approve_stage(eid, "audit", approver="security-lead")
    assert manager.store.get_execution(eid).status is ExecutionStatus.SUCCEEDED
    outcome = run(runner.run_workflow(eid))                       # nothing left: no further agent call
    assert outcome.stages == () and len(adapter.calls) == 4


def test_rejected_gate_fails_and_reruns_only_that_stage(make_runner, workspace, manager, edit_yaml) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = start(runner, workspace)
    run(runner.run_workflow(eid))
    manager.reject_stage(eid, "audit", reviewer="security-lead", reason="missing evidence")
    assert manager.store.get_execution(eid).status is ExecutionStatus.FAILED
    plan = manager.plan(eid)
    assert plan.exhausted == ("audit",)                           # audit: max_attempts 1
