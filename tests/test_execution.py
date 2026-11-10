"""Agent-run recording, failure handling and resume."""

import pytest

from core.context import ContextItem, ContextManager, ContextScope
from core.errors import ContractValidationError, HarnessError, InvalidTransitionError
from core.security import Redactor
from runtime.execution import ExecutionManager
from runtime.models import FailureInfo
from runtime.sqlite_store import SQLiteExecutionStore
from runtime.state import AgentRunStatus, ExecutionStatus, StageStatus

SHA = "a" * 64


def run_stage_task(manager: ExecutionManager, ctx: ContextManager, eid: str, stage_id: str):
    stage = manager.registry.workflows.get("security-review").stage(stage_id)
    agent = manager.registry.agents.get(stage.agent_id)
    bundle = ctx.assemble_for_stage(agent, stage, f"{eid}-{stage_id}")
    return manager.create_task(eid, stage_id, context=bundle, estimator=ctx.estimator)


def result_for(run, status="succeeded", **extra) -> dict:
    result = {"schema_version": "1.0", "kind": "AgentResult", "task_id": run.task_id, "agent_id": run.agent_id,
              "status": status,
              "outputs": [{"name": "threat-model",
                           "artifact": {"uri": "file://var/artifacts/tm.md", "sha256": SHA, "size_bytes": 1200}}]}
    result.update(extra)
    return result


def test_task_creation_follows_contract(manager: ExecutionManager, context_manager: ContextManager) -> None:
    context_manager.add(ContextItem("settings", ContextScope.PROJECT, uri="repo://settings.py",
                                    tags={"security"}, token_estimate=800))
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    run = run_stage_task(manager, context_manager, eid, "threat-model")
    assert run.status is AgentRunStatus.QUEUED
    assert run.task["objective"].startswith("Produce a STRIDE threat model")
    assert run.task["context"]["refs"][0]["id"] == "settings"
    assert run.task["expected_outputs"] == [{"name": "threat-model", "type": "report", "required": True,
                                             "description": "STRIDE table plus data-flow diagram"}]
    assert manager.store.get_agent_run(run.run_id).task == run.task


def test_task_requires_running_stage(manager: ExecutionManager, context_manager: ContextManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    with pytest.raises(InvalidTransitionError, match="start it before creating a task"):
        run_stage_task(manager, context_manager, eid, "threat-model")


def test_record_result_stores_artifact_refs(manager: ExecutionManager, context_manager: ContextManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    run = run_stage_task(manager, context_manager, eid, "threat-model")
    manager.start_agent_run(run.run_id)
    stored = manager.record_result(run.run_id, result_for(run))
    assert stored.status is AgentRunStatus.SUCCEEDED
    [artifact] = manager.store.list_artifacts(eid)
    assert (artifact.name, artifact.uri, artifact.sha256) == ("threat-model", "file://var/artifacts/tm.md", SHA)
    events = [e.event for e in manager.store.list_logs(eid)]
    assert events == ["execution.started", "stage.started", "agent_run.queued", "agent_run.started",
                      "agent_run.succeeded"]


def test_invalid_or_mismatched_result_rejected(manager: ExecutionManager, context_manager: ContextManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    run = run_stage_task(manager, context_manager, eid, "threat-model")
    manager.start_agent_run(run.run_id)
    with pytest.raises(ContractValidationError):
        manager.record_result(run.run_id, result_for(run, status="failed"))      # failed without errors
    with pytest.raises(HarnessError, match="does not match run"):
        manager.record_result(run.run_id, result_for(run, task_id="task-other"))
    assert manager.store.get_agent_run(run.run_id).status is AgentRunStatus.RUNNING


def test_failure_then_resume_retries_stage(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.fail_stage(eid, "threat-model", FailureInfo("agent.error", "model overloaded", retryable=True))
    execution = manager.store.get_execution(eid)
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error.details == {"stage_id": "threat-model"}

    plan = manager.resume(eid)
    assert manager.store.get_execution(eid).status is ExecutionStatus.RUNNING
    assert plan.runnable == ("threat-model",) and plan.exhausted == ()
    assert plan.checkpoint.reason == "resume"
    stage = manager.start_stage(eid, "threat-model")
    assert stage.attempts == 2


def test_exhausted_stage_reported(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    for stage in ("threat-model", "secrets-scan", "sast"):
        manager.start_stage(eid, stage)
        manager.complete_stage(eid, stage)
    manager.start_stage(eid, "audit")                       # max_attempts: 1
    manager.fail_stage(eid, "audit", FailureInfo("agent.error", "bad output"))
    plan = manager.resume(eid)
    assert plan.exhausted == ("audit",)
    assert plan.completed == ("threat-model", "secrets-scan", "sast")
    with pytest.raises(InvalidTransitionError):
        manager.start_stage(eid, "audit")


def test_resume_after_crash(tmp_path, registry, context_manager: ContextManager) -> None:
    path = tmp_path / "crash.db"
    store = SQLiteExecutionStore(path)
    first = ExecutionManager(store, registry)
    eid = first.start_execution("security-review", {"repository": "acme"}).execution_id
    first.start_stage(eid, "threat-model")
    first.complete_stage(eid, "threat-model")
    first.start_stage(eid, "sast")
    run = run_stage_task(first, context_manager, eid, "sast")
    first.start_agent_run(run.run_id)
    store.close()                                            # process dies mid-stage

    store = SQLiteExecutionStore(path)
    second = ExecutionManager(store, registry)
    plan = second.resume(eid)
    assert plan.completed == ("threat-model",)
    assert plan.runnable == ("secrets-scan", "sast")
    assert plan.pending == ("audit",)
    assert store.get_agent_run(run.run_id).status is AgentRunStatus.CANCELLED
    assert store.get_stage(eid, "sast").status is StageStatus.PENDING
    store.close()


def test_resume_paused_and_reject_terminal(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.pause(eid)
    assert manager.resume(eid).runnable == ("threat-model",)
    manager.cancel(eid)
    with pytest.raises(InvalidTransitionError, match="cannot be resumed"):
        manager.resume(eid)


def test_approval_rejection_fails_stage(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    for stage in ("threat-model", "secrets-scan", "sast", "audit"):
        manager.start_stage(eid, stage)
        manager.complete_stage(eid, stage)
    stage = manager.reject_stage(eid, "audit", reviewer="lead", reason="missing evidence")
    assert stage.status is StageStatus.FAILED and stage.error.code == "approval.rejected"


def test_failure_messages_are_redacted(store, registry) -> None:
    manager = ExecutionManager(store, registry, redact=Redactor(registry.config.security))
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.fail_stage(eid, "threat-model", FailureInfo("tool.error", "leaked AKIAABCDEFGHIJKLMNOP in output"))
    assert "AKIA" not in store.get_stage(eid, "threat-model").error.message
    assert all("AKIA" not in entry.message for entry in store.list_logs(eid))
