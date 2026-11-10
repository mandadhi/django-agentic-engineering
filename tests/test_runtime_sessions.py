"""Runtime behaviour: state transitions, failures, timeouts, cancellation, checkpoints, sessions, resume."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent_runtime.runner import AgentRunner
from agent_runtime.session_manager import find_resumable_run
from core.context import ContextManager
from runtime.execution import ExecutionManager
from runtime.sqlite_store import MIGRATIONS, SCHEMA_VERSION, SQLiteExecutionStore
from runtime.state import AgentRunStatus, ExecutionStatus, StageStatus

from fakes import FakeSDKAdapter, FakeSession, agent_result, error_result, hang, succeed


def run(coro):
    return asyncio.run(coro)


def adhoc(runner, workspace, agent_id="secrets-scanner"):
    return runner.start_agent(agent_id, "Scan for secrets.", workspace=workspace).execution_id


def test_successful_transitions_and_checkpoints(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    eid = adhoc(runner, workspace)
    run(runner.run_workflow(eid))
    events = [e.event for e in manager.store.list_logs(eid) if not e.event.startswith("tool.")]
    assert events == ["execution.started", "stage.started", "agent_run.queued",
                      "session.shell", "session.environment",                 # Phase 5.1: isolation audited
                      "agent_run.started", "agent_run.session", "agent_run.succeeded", "stage.succeeded",
                      "execution.succeeded"]
    reasons = [c.reason for c in manager.store.list_checkpoints(eid)]
    assert reasons == ["start", "agent_session_started", "stage_completed"]
    session_cp = manager.store.list_checkpoints(eid)[1]
    assert session_cp.state["extra"]["session_id"] == manager.store.list_agent_runs(eid)[0].session_id


def test_failed_agent_execution(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(error_result("error_max_turns")))
    eid = adhoc(runner, workspace)
    outcome = run(runner.run_workflow(eid))
    [stage] = outcome.stages
    assert stage.stage_status is StageStatus.FAILED and stage.failure.code == "agent.max_turns"
    [agent_run] = manager.store.list_agent_runs(eid)
    assert agent_run.status is AgentRunStatus.FAILED and agent_run.session_id is not None
    assert manager.store.get_execution(eid).status is ExecutionStatus.FAILED
    assert manager.store.latest_checkpoint(eid).reason == "failure"


def test_api_error_is_retryable(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(error_result("success", api_error_status=529, terminal_reason="api_error")))
    eid = adhoc(runner, workspace)
    run(runner.run_workflow(eid))
    [agent_run] = manager.store.list_agent_runs(eid)    # ad-hoc stage has max_attempts 1: no retry
    assert agent_run.error.code == "sdk.api_error" and agent_run.error.retryable


def test_rejected_request_is_not_retryable(make_runner, workspace, manager) -> None:
    """Observed with the real API: a 400 arrives with terminal_reason 'api_error' — still not retryable."""
    runner = make_runner(FakeSDKAdapter(error_result("success", api_error_status=400, terminal_reason="api_error")))
    eid = adhoc(runner, workspace)
    run(runner.run_workflow(eid))
    [agent_run] = manager.store.list_agent_runs(eid)
    assert agent_run.error.code == "sdk.invalid_request" and not agent_run.error.retryable


def test_timeout_cancels_agent_and_records_failure(make_runner, workspace, manager) -> None:
    adapter = FakeSDKAdapter(hang(30))
    runner = make_runner(adapter, timeout_override=0.3)
    eid = adhoc(runner, workspace)
    outcome = run(asyncio.wait_for(runner.run_workflow(eid), timeout=10))
    [stage] = outcome.stages
    assert stage.failure.code == "agent.timeout" and stage.failure.retryable
    [agent_run] = manager.store.list_agent_runs(eid)
    assert agent_run.status is AgentRunStatus.FAILED          # not left running
    assert agent_run.session_id is not None                   # persisted at init, before the timeout
    assert manager.store.get_stage(eid, "run").status is StageStatus.FAILED
    assert manager.store.get_execution(eid).status is ExecutionStatus.FAILED
    assert manager.store.latest_checkpoint(eid).reason == "failure"


def test_timeout_uses_the_lowest_limit(make_runner, registry) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    stage = registry.workflows.get("security-review").stage("audit")         # 1800 s default
    assert runner.effective_timeout(stage) == 1800
    assert make_runner(FakeSDKAdapter(succeed()), timeout_override=5).effective_timeout(stage) == 5


def test_execution_cancel_stops_running_agent(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(hang(30)))
    eid = adhoc(runner, workspace)

    async def scenario():
        task = asyncio.ensure_future(runner.run_workflow(eid))
        while not manager.store.list_agent_runs(eid) or not manager.store.list_agent_runs(eid)[0].session_id:
            await asyncio.sleep(0.01)
        manager.cancel(eid, "operator stop")              # what `execution cancel` does from another process
        return await asyncio.wait_for(task, timeout=10)

    outcome = run(scenario())
    [agent_run] = manager.store.list_agent_runs(eid)
    assert agent_run.status is AgentRunStatus.CANCELLED and agent_run.error.code == "runtime.cancelled"
    assert outcome.execution.status is ExecutionStatus.CANCELLED
    assert manager.store.get_stage(eid, "run").status is StageStatus.CANCELLED


def test_in_process_cancellation_is_recorded(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(hang(30)))
    eid = adhoc(runner, workspace)

    async def scenario():
        task = asyncio.ensure_future(runner.run_stage(eid, "run"))
        while not manager.store.list_agent_runs(eid) or not manager.store.list_agent_runs(eid)[0].session_id:
            await asyncio.sleep(0.01)
        task.cancel()                                     # e.g. Ctrl-C
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    [agent_run] = manager.store.list_agent_runs(eid)
    assert agent_run.status is AgentRunStatus.CANCELLED
    stage = manager.store.get_stage(eid, "run")
    assert stage.status is StageStatus.FAILED and stage.error.code == "runtime.cancelled" and stage.error.retryable


# ---------------------------------------------------------------------- sessions & resume


def _crash_mid_stage(manager: ExecutionManager, runner: AgentRunner, eid: str, stage_id: str,
                     session_id: str) -> str:
    """Leave the store exactly as a process killed after the SDK init frame leaves it."""
    execution = manager.store.get_execution(eid)
    definition = manager.workflow_for(execution).stage(stage_id)
    agent = manager.registry.agents.get(definition.agent_id)
    manager.start_stage(eid, stage_id)
    bundle = runner.context.assemble_for_stage(agent, definition, f"{eid}:{stage_id}")
    agent_run = manager.create_task(eid, stage_id, context=bundle, estimator=runner.context.estimator)
    manager.start_agent_run(agent_run.run_id)
    manager.update_run_metadata(agent_run.run_id, session_id=session_id)
    return agent_run.run_id


def test_session_persisted_and_resumed_after_interruption(tmp_path, registry, context_manager, workspace) -> None:
    db = tmp_path / "state.db"
    first_store = SQLiteExecutionStore(db)
    first = ExecutionManager(first_store, registry)
    runner = AgentRunner(first, context_manager, FakeSDKAdapter(succeed()), artifacts_root=tmp_path / "a")
    eid = adhoc(runner, workspace, agent_id="security-auditor")          # ad-hoc stage: max_attempts 1
    crashed = _crash_mid_stage(first, runner, eid, "run", "11111111-2222-3333-4444-555555555555")
    first_store.close()                                                    # the process is gone

    store = SQLiteExecutionStore(db)
    manager = ExecutionManager(store, registry)
    adapter = FakeSDKAdapter(succeed(), sessions={"11111111-2222-3333-4444-555555555555"})
    resumed_runner = AgentRunner(manager, ContextManager(registry.config.framework.context, max_inline_bytes=4096),
                                 adapter, artifacts_root=tmp_path / "a")
    outcome = run(resumed_runner.resume_workflow(eid))

    assert outcome.execution.status is ExecutionStatus.SUCCEEDED
    [session] = adapter.calls
    assert session.options.resume == "11111111-2222-3333-4444-555555555555"
    assert "was interrupted" in session.prompt
    old, new = store.list_agent_runs(eid)
    assert old.run_id == crashed and old.status is AgentRunStatus.CANCELLED
    assert old.error.code == "runtime.interrupted"
    assert new.sdk_metadata["resumed_from_run"] == crashed and new.status is AgentRunStatus.SUCCEEDED
    assert store.get_stage(eid, "run").attempts == 1          # the interruption did not use up the only attempt
    assert find_resumable_run(store, eid, "run") is None      # completed: never resumed again
    store.close()


def test_missing_session_transcript_starts_fresh(tmp_path, registry, context_manager, workspace) -> None:
    store = SQLiteExecutionStore(tmp_path / "s.db")
    manager = ExecutionManager(store, registry)
    adapter = FakeSDKAdapter(succeed(), sessions=set())      # transcript is gone
    runner = AgentRunner(manager, context_manager, adapter, artifacts_root=tmp_path / "a")
    eid = adhoc(runner, workspace)
    _crash_mid_stage(manager, runner, eid, "run", "dead-session")
    run(runner.resume_workflow(eid))
    assert adapter.calls[0].options.resume is None
    assert any(e.event == "session.unavailable" for e in store.list_logs(eid))
    store.close()


def test_deliberate_cancel_or_failure_is_not_resumed(make_runner, workspace, manager) -> None:
    runner = make_runner(FakeSDKAdapter(error_result("error_max_turns")))
    eid = adhoc(runner, workspace)
    run(runner.run_workflow(eid))
    assert manager.store.list_agent_runs(eid)[0].session_id
    assert find_resumable_run(manager.store, eid, "run") is None


def test_completed_stage_is_never_run_twice(make_runner, workspace, manager) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    eid = runner.start("security-review", {"repository": str(workspace)}, workspace=workspace).execution_id
    run(runner.run_stage(eid, "threat-model"))
    from core.errors import InvalidTransitionError
    with pytest.raises(InvalidTransitionError):
        run(runner.run_stage(eid, "threat-model"))
    assert len(adapter.calls) == 1


# ---------------------------------------------------------------------- migration


def test_v2_migration_preserves_v1_data(tmp_path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for statement in filter(str.strip, MIGRATIONS[0][1].split(";")):
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-11-01T00:00:00Z')")
    conn.execute("INSERT INTO executions VALUES ('e1','wf','1.0.0','running','{}','{}',NULL,'t','t',NULL,NULL)")
    conn.execute("INSERT INTO stages VALUES ('e1','a','agent-a','running',0,1,2,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO agent_runs VALUES ('r1','e1','a','agent-a','task-1',1,'running','{\"x\":1}',NULL,NULL,"
                 "'t',NULL,NULL)")
    conn.commit()
    conn.close()

    store = SQLiteExecutionStore(path)
    assert store.schema_version == SCHEMA_VERSION      # v1 → latest (v3 since Phase 5.3) in one open
    old = store.get_agent_run("r1")
    assert old.task == {"x": 1} and old.session_id is None and old.sdk_metadata == {}
    old.session_id, old.cost_usd = "s-1", 0.5
    store.save_agent_run(old)
    assert store.get_agent_run("r1").session_id == "s-1"
    store.close()
    reopened = SQLiteExecutionStore(path)          # idempotent
    assert reopened.get_agent_run("r1").cost_usd == 0.5
    reopened.close()
