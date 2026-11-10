"""Phase 5.3: RunRecord execution contract, lifecycle, metrics and persistence (schema v3).

Layout-independent: runner tests use ``pack_runner`` (registries pruned to what this checkout
contains) and the Django Engineering Pack's read-only agents, with the fake SDK adapter — no
API call is made.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from agent_runtime.result_processor import SessionCollector
from agent_runtime.runner import WORKSPACE_KEY
from core import HARNESS_HOME
from core.contracts import ContractValidator
from core.errors import InvalidTransitionError, NotFoundError, StorageError
from runtime.models import AgentRun, Execution, FailureInfo, StageState
from runtime.records import RUN_RECORD_VERSION, build_run_record, error_category, total_tokens
from runtime.sqlite_store import MIGRATIONS, SCHEMA_VERSION, SQLiteExecutionStore
from runtime.state import AgentRunStatus, ExecutionStatus, StageStatus

from fakes import FakeSDKAdapter, FakeSession, agent_result, error_result, hang, succeed

ANALYZER = "django-project-analyzer"
REVIEWER = "django-code-review-agent"
CONTENT_KEYS = {"task", "result", "objective", "inputs", "prompt", "context", "outputs", "summary", "messages"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def contracts() -> ContractValidator:
    return ContractValidator(HARNESS_HOME / "contracts")


def start(runner, workspace: Path, stages: list[dict], wid: str = "adhoc.phase53") -> str:
    """Start an ad-hoc workflow (several stages / attempts) through the normal execution path."""
    for stage in stages:
        stage.setdefault("objective", f"Do {stage['id']}.")
        stage.setdefault("expected_outputs", [{"name": "report", "type": "report"}])
    doc = {"schema_version": "1.0", "kind": "WorkflowDefinition", "id": wid, "version": "0.0.1",
           "name": "Phase 5.3 test workflow", "status": "draft", "stages": stages}
    return runner.manager.start_execution(wid, {}, {WORKSPACE_KEY: str(runner._workspace(workspace))},
                                          definition=doc).execution_id


def single(runner, workspace: Path, **stage) -> str:
    return start(runner, workspace, [{"id": "run", "agent_id": ANALYZER, "max_attempts": 1, **stage}])


def assistant(s: FakeSession, message_id: str, input_tokens: int, output_tokens: int) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text="working")], model="claude-test", session_id=s.session_id,
                            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
                            message_id=message_id)


def usage_then_hang():
    async def scenario(s: FakeSession):
        yield s.init()
        yield assistant(s, "msg_1", 100, 10)
        yield assistant(s, "msg_1", 100, 25)          # same API message, later block: replaces, not adds
        yield assistant(s, "msg_2", 400, 5)
        await asyncio.sleep(30)
        yield s.result(structured=agent_result(s))
    return scenario


# ---------------------------------------------------------------------- contract units


@pytest.mark.parametrize("code, category", [
    ("agent.timeout", "timeout"), ("runtime.cancelled", "cancelled"), ("runtime.interrupted", "interrupted"),
    ("sdk.api_error", "sdk"), ("agent.max_turns", "agent"), ("result.invalid", "result"),
    ("security.violation", "security"), ("plugin.unresolved", "configuration"), ("task.invalid", "configuration"),
    ("runtime.prepare_failed", "configuration"), ("approval.rejected", "approval"), ("runtime.error", "internal"),
    ("custom-code", "internal"), (None, None),
])
def test_error_categories(code, category) -> None:
    assert error_category(code) == category


def _run(**overrides) -> AgentRun:
    base = dict(run_id="run-1", execution_id="exec-1", stage_id="run", agent_id=ANALYZER, task_id="task-1",
                attempt=1, status=AgentRunStatus.RUNNING, task={"objective": "SECRET OBJECTIVE"},
                created_at="2026-11-10T10:00:00.000+00:00", started_at="2026-11-10T10:00:01.000+00:00")
    base.update(overrides)
    return AgentRun(**base)


def test_missing_metrics_stay_unavailable_and_zero_stays_zero() -> None:
    missing = build_run_record(_run(), workflow_id="wf", workflow_version="1")
    assert (missing.input_tokens, missing.output_tokens, missing.total_tokens, missing.cost_usd,
            missing.num_turns, missing.metrics_source, missing.duration_ms) == (None,) * 7
    zero = build_run_record(_run(input_tokens=0, output_tokens=0, cost_usd=0.0, num_turns=0,
                                 sdk_metadata={"metrics_source": "result"}), workflow_id="wf", workflow_version="1")
    assert (zero.input_tokens, zero.output_tokens, zero.total_tokens, zero.cost_usd, zero.num_turns) == (0, 0, 0, 0.0, 0)
    assert total_tokens(5, None) is None and total_tokens(None, 5) is None and total_tokens(2, 3) == 5


def test_record_is_versioned_metadata_only_and_schema_valid(contracts: ContractValidator) -> None:
    record = build_run_record(
        _run(status=AgentRunStatus.FAILED, completed_at="2026-11-10T10:00:03.500+00:00",
             error=FailureInfo("agent.timeout", "timed out", True), input_tokens=10, output_tokens=2,
             result={"status": "failed", "summary": "SECRET SUMMARY"},
             sdk_metadata={"session_agent": "dae-x", "model": "m"}),
        workflow_id="wf", workflow_version="1")
    doc = record.to_dict()
    contracts.validate("run-record", doc)
    assert doc["schema_version"] == RUN_RECORD_VERSION and doc["kind"] == "RunRecord"
    assert doc["duration_ms"] == 2500 and doc["failed_at"] == doc["completed_at"]
    assert doc["error_category"] == "timeout" and doc["result_status"] == "failed" and doc["total_tokens"] == 12
    assert not CONTENT_KEYS & set(doc)
    assert "SECRET" not in json.dumps(doc)


def test_collector_metrics_result_partial_and_none() -> None:
    nothing = SessionCollector()
    assert set(nothing.metrics().values()) == {None}

    partial = SessionCollector()
    for mid, i, o in (("a", 100, 10), ("a", 100, 25), ("b", 400, 5)):
        partial.observe(AssistantMessage(content=[], model="m", usage={"input_tokens": i, "output_tokens": o},
                                         message_id=mid))
    partial.observe(AssistantMessage(content=[], model="m", usage={"input_tokens": "n/a"}, message_id="c"))
    assert partial.metrics() == {"input_tokens": 500, "output_tokens": 30, "cost_usd": None, "duration_ms": None,
                                 "num_turns": None, "metrics_source": "partial"}

    result = SessionCollector()
    result.observe(AssistantMessage(content=[], model="m", usage={"input_tokens": 9, "output_tokens": 9},
                                    message_id="x"))
    result.observe(ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=0,
                                 session_id="s", total_cost_usd=None, usage={"input_tokens": 0}))
    assert result.metrics() == {"input_tokens": 0, "output_tokens": None, "cost_usd": None, "duration_ms": 10,
                                "num_turns": 0, "metrics_source": "result"}      # the result is authoritative


# ---------------------------------------------------------------------- runner integration


def test_successful_run_record(pack_runner, workspace, contracts) -> None:
    runner = pack_runner(FakeSDKAdapter(succeed()))
    eid = single(runner, workspace)
    run(runner.run_workflow(eid))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    contracts.validate("run-record", record.to_dict())
    assert record.status is AgentRunStatus.SUCCEEDED and record.result_status == "succeeded"
    assert (record.workflow_id, record.stage_id, record.agent_id, record.attempt) == ("adhoc.phase53", "run",
                                                                                     ANALYZER, 1)
    assert record.session_agent == f"dae-{ANALYZER}" and record.session_id
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (1200, 300, 1500)
    assert (record.cost_usd, record.num_turns, record.sdk_duration_ms) == (0.0123, 3, 1500)
    assert record.metrics_source == "result" and record.duration_ms is not None and record.duration_ms >= 0
    assert record.created_at <= record.started_at <= record.completed_at
    assert (record.error_code, record.error_category, record.failed_at) == (None, None, None)
    summary = runner.manager.execution_summary(eid)
    assert summary.status is ExecutionStatus.SUCCEEDED and summary.completed_at and summary.failed_at is None
    assert (summary.runs, summary.total_tokens, summary.cost_usd, summary.metrics_complete) == (1, 1500, 0.0123, True)


def test_agent_failure_keeps_metrics_of_the_error_result(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(error_result("error_max_turns")))
    eid = single(runner, workspace)
    run(runner.run_workflow(eid))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    assert record.status is AgentRunStatus.FAILED and record.result_status == "failed"
    assert (record.error_code, record.error_category, record.error_retryable) == ("agent.max_turns", "agent", False)
    assert record.failed_at == record.completed_at is not None
    assert (record.input_tokens, record.cost_usd, record.metrics_source) == (1200, 0.0123, "result")
    summary = runner.manager.execution_summary(eid)
    assert summary.status is ExecutionStatus.FAILED and summary.completed_at is None      # resumable
    assert summary.failed_at is not None and summary.error_category == "agent"


def test_timeout_is_distinguishable_and_keeps_partial_metrics(pack_runner, workspace, contracts) -> None:
    runner = pack_runner(FakeSDKAdapter(usage_then_hang()), timeout_override=0.3)
    eid = single(runner, workspace)
    run(asyncio.wait_for(runner.run_workflow(eid), timeout=10))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    contracts.validate("run-record", record.to_dict())
    assert record.status is AgentRunStatus.FAILED
    assert (record.error_code, record.error_category, record.error_retryable) == ("agent.timeout", "timeout", True)
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (500, 30, 530)
    assert (record.cost_usd, record.num_turns, record.sdk_duration_ms) == (None, None, None)   # not reported
    assert record.metrics_source == "partial" and record.duration_ms >= 300
    assert runner.manager.execution_summary(eid).metrics_complete is False


def test_timeout_without_usage_records_no_invented_zeros(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(hang(30)), timeout_override=0.2)
    eid = single(runner, workspace)
    run(asyncio.wait_for(runner.run_workflow(eid), timeout=10))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    assert record.error_category == "timeout"
    assert (record.input_tokens, record.output_tokens, record.total_tokens, record.cost_usd,
            record.metrics_source) == (None,) * 5
    with sqlite3.connect(runner.manager.store.path) as conn:      # NULL in the database, not 0
        assert conn.execute("SELECT input_tokens, output_tokens, cost_usd FROM agent_runs").fetchone() == (None,) * 3
    summary = runner.manager.execution_summary(eid)
    assert (summary.total_tokens, summary.cost_usd, summary.metrics_complete) == (None, None, False)


def test_deliberate_cancel_is_not_a_failure(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(hang(30)))
    eid = single(runner, workspace)
    store = runner.manager.store

    async def scenario():
        task = asyncio.ensure_future(runner.run_workflow(eid))
        while not store.list_agent_runs(eid) or not store.list_agent_runs(eid)[0].session_id:
            await asyncio.sleep(0.01)
        runner.manager.cancel(eid, "operator stop")
        return await asyncio.wait_for(task, timeout=10)

    run(scenario())
    [record] = store.list_run_records(execution_id=eid)
    assert record.status is AgentRunStatus.CANCELLED and record.error_category == "cancelled"
    assert record.failed_at is None and record.completed_at is not None
    assert runner.manager.execution_summary(eid).status is ExecutionStatus.CANCELLED


def test_retry_adds_a_record_and_keeps_history(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(
        error_result("success", api_error_status=529, terminal_reason="api_error"), succeed()))
    eid = single(runner, workspace, max_attempts=2)
    outcome = run(runner.run_workflow(eid))
    assert outcome.execution.status is ExecutionStatus.SUCCEEDED
    first, second = runner.manager.store.list_run_records(execution_id=eid, stage_id="run", newest_first=False)
    assert first.run_id != second.run_id and first.task_id != second.task_id
    assert (first.attempt, first.status, first.error_code) == (1, AgentRunStatus.FAILED, "sdk.api_error")
    assert first.cost_usd == 0.0123 and first.failed_at is not None          # history kept after the retry
    assert (second.attempt, second.status, second.error_code) == (2, AgentRunStatus.SUCCEEDED, None)
    summary = runner.manager.execution_summary(eid)
    assert summary.runs_by_status == {"failed": 1, "succeeded": 1} and summary.cost_usd == 0.0246
    assert summary.error_code is None                                       # execution recovered


def test_interrupted_run_and_resume(pack_runner, workspace) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    adapter = FakeSDKAdapter(succeed(), sessions={session_id})
    runner = pack_runner(adapter)
    manager, store = runner.manager, runner.manager.store
    eid = single(runner, workspace)
    # leave the store as a process killed after the SDK init frame leaves it
    execution = store.get_execution(eid)
    definition = manager.workflow_for(execution).stage("run")
    manager.start_stage(eid, "run")
    bundle = runner.context.assemble_for_stage(manager.registry.agents.get(ANALYZER), definition, f"{eid}:run")
    crashed = manager.create_task(eid, "run", context=bundle, estimator=runner.context.estimator)
    manager.start_agent_run(crashed.run_id)
    manager.update_run_metadata(crashed.run_id, session_id=session_id, sdk_metadata={"session_agent": "dae-x"})

    assert store.get_run_record(crashed.run_id).status is AgentRunStatus.RUNNING     # never reported successful
    assert manager.execution_summary(eid).status is ExecutionStatus.RUNNING
    outcome = run(runner.resume_workflow(eid))
    assert outcome.execution.status is ExecutionStatus.SUCCEEDED

    old, new = store.list_run_records(execution_id=eid, newest_first=False)
    assert old.run_id == crashed.run_id
    assert (old.status, old.error_code, old.error_category) == (AgentRunStatus.CANCELLED, "runtime.interrupted",
                                                                "interrupted")
    assert old.failed_at == old.completed_at is not None and old.session_id == session_id
    assert new.resumed_from_run == old.run_id and new.status is AgentRunStatus.SUCCEEDED
    assert new.attempt == old.attempt == 1               # refunded attempt: run_id, not attempt, is unique


def test_multi_stage_workflow_failure_and_correlation(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(succeed(), error_result("error_max_budget_usd")))
    eid = start(runner, workspace, [{"id": "analyze", "agent_id": ANALYZER},
                                    {"id": "review", "agent_id": REVIEWER, "depends_on": ["analyze"]}])
    run(runner.run_workflow(eid))
    store = runner.manager.store
    records = store.list_run_records(workflow_id="adhoc.phase53", newest_first=False)
    assert [(r.stage_id, r.agent_id, r.status.value) for r in records] == [
        ("analyze", ANALYZER, "succeeded"), ("review", REVIEWER, "failed")]
    assert {r.execution_id for r in records} == {eid}
    [review] = store.list_run_records(execution_id=eid, stage_id="review")
    assert review.error_code == "agent.budget_exceeded" and review.session_agent == f"dae-{REVIEWER}"
    summary = runner.manager.execution_summary(eid)
    assert summary.status is ExecutionStatus.FAILED and summary.error_code == "agent.budget_exceeded"
    assert summary.stages == {"analyze": "succeeded", "review": "failed"} and summary.total_tokens == 3000


def test_finished_run_record_is_immutable(pack_runner, workspace) -> None:
    runner = pack_runner(FakeSDKAdapter(succeed()))
    eid = single(runner, workspace)
    run(runner.run_workflow(eid))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    with pytest.raises(InvalidTransitionError, match="immutable"):
        runner.manager.update_run_metadata(record.run_id, cost_usd=99.0, input_tokens=1)
    with pytest.raises(InvalidTransitionError):
        runner.manager.start_agent_run(record.run_id)
    assert runner.manager.store.get_run_record(record.run_id) == record


def test_persistence_failure_never_reports_success(pack_runner, workspace, monkeypatch) -> None:
    runner = pack_runner(FakeSDKAdapter(succeed()))
    eid = single(runner, workspace)

    def broken(*args, **kwargs):
        raise StorageError("disk I/O error")

    monkeypatch.setattr(runner.manager, "record_result", broken)
    with pytest.raises(StorageError):
        run(runner.run_workflow(eid))
    [record] = runner.manager.store.list_run_records(execution_id=eid)
    assert record.status is AgentRunStatus.RUNNING and record.result_status is None
    assert runner.manager.store.get_execution(eid).status is ExecutionStatus.RUNNING
    monkeypatch.undo()
    runner.manager.resume(eid)                                    # the next process settles it as interrupted
    assert runner.manager.store.get_run_record(record.run_id).error_category == "interrupted"


# ---------------------------------------------------------------------- store: retrieval & identity


def _seed(store: SQLiteExecutionStore) -> None:
    for eid, wid in (("e1", "wf-a"), ("e2", "wf-b")):
        store.create_execution(Execution(eid, wid, "1.0.0", ExecutionStatus.RUNNING, "t0", "t0"),
                               [StageState(eid, "s1", "agent-a", StageStatus.RUNNING, 0)])
    rows = [("r1", "e1", "agent-a", AgentRunStatus.SUCCEEDED, None, "2026-11-10T10:00:01+00:00"),
            ("r2", "e1", "agent-a", AgentRunStatus.FAILED, FailureInfo("agent.timeout", "t", True),
             "2026-11-10T10:00:02+00:00"),
            ("r3", "e2", "agent-a", AgentRunStatus.FAILED, FailureInfo("sdk.api_error", "x", True),
             "2026-11-10T10:00:03+00:00")]
    for run_id, eid, agent, status, error, created in rows:
        store.save_agent_run(AgentRun(run_id, eid, "s1", agent, f"task-{run_id}", 1, status, {"objective": "o"},
                                      created, error=error))


def test_retrieval_api(store: SQLiteExecutionStore) -> None:
    _seed(store)
    assert store.get_run_record("r2").error_code == "agent.timeout"
    with pytest.raises(NotFoundError):
        store.get_run_record("nope")
    assert [r.run_id for r in store.list_run_records()] == ["r3", "r2", "r1"]                  # recent first
    assert [r.run_id for r in store.list_run_records(limit=1)] == ["r3"]
    assert [r.run_id for r in store.list_run_records(execution_id="e1", newest_first=False)] == ["r1", "r2"]
    assert [r.run_id for r in store.list_run_records(workflow_id="wf-b")] == ["r3"]
    assert [r.run_id for r in store.list_run_records(status=AgentRunStatus.FAILED)] == ["r3", "r2"]
    assert [r.run_id for r in store.list_run_records(error_code="agent.timeout")] == ["r2"]
    assert [r.run_id for r in store.list_run_records(agent_id="agent-a", stage_id="s1", execution_id="e2")] == ["r3"]


def test_run_and_task_identifiers_are_unique(store: SQLiteExecutionStore) -> None:
    _seed(store)
    with pytest.raises(StorageError):          # another run reusing a task id
        store.save_agent_run(AgentRun("r9", "e1", "s1", "agent-a", "task-r1", 1, AgentRunStatus.QUEUED, {}, "t"))
    with pytest.raises(StorageError):          # a run for a stage that does not exist
        store.save_agent_run(AgentRun("r8", "e1", "zz", "agent-a", "task-r8", 1, AgentRunStatus.QUEUED, {}, "t"))


def test_negative_token_counts_are_rejected(store: SQLiteExecutionStore) -> None:
    _seed(store)
    with pytest.raises(StorageError):
        store.save_agent_run(AgentRun("r7", "e1", "s1", "agent-a", "task-r7", 1, AgentRunStatus.QUEUED, {}, "t",
                                      input_tokens=-1))


# ---------------------------------------------------------------------- migration v2 → v3


def _v2_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, ddl in MIGRATIONS[:2]:
        for statement in filter(str.strip, ddl.split(";")):
            conn.execute(statement)
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, f"2026-11-0{version}T00:00:00Z"))
    conn.execute("INSERT INTO executions VALUES ('e1','wf','1.0.0','failed','{}','{}',NULL,"
                 "'2026-11-02T10:00:00+00:00','2026-11-02T10:00:09+00:00','2026-11-02T10:00:00+00:00',NULL)")
    conn.execute("INSERT INTO stages VALUES ('e1','a','agent-a','failed',0,1,1,NULL,NULL,NULL,NULL)")
    insert = ("INSERT INTO agent_runs (run_id, execution_id, stage_id, agent_id, task_id, attempt, status, task, "
              "result, error, created_at, started_at, completed_at, session_id, cost_usd, duration_ms, num_turns, "
              "sdk_metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    conn.execute(insert, ("r-ok", "e1", "a", "agent-a", "t1", 1, "succeeded", '{"objective":"o"}',
                          '{"status":"partial"}', None, "2026-11-02T10:00:00+00:00", "2026-11-02T10:00:01+00:00",
                          "2026-11-02T10:00:04+00:00", "s-1", 0.25, 2900, 4,
                          json.dumps({"usage": {"input_tokens": 700, "output_tokens": 0}, "session_agent": "dae-a"})))
    conn.execute(insert, ("r-bad", "e1", "a", "agent-a", "t2", 1, "failed", '{"objective":"o"}', None,
                          json.dumps({"code": "agent.timeout", "message": "slow", "retryable": True}),
                          "2026-11-02T10:00:05+00:00", "2026-11-02T10:00:05+00:00", "2026-11-02T10:00:09+00:00",
                          None, None, None, None, json.dumps({"usage": {"input_tokens": "n/a"}})))
    conn.commit()
    conn.close()


def test_v2_database_upgrades_to_v3(tmp_path: Path, contracts: ContractValidator) -> None:
    path = tmp_path / "v2.db"
    _v2_database(path)
    store = SQLiteExecutionStore(path)
    assert store.schema_version == SCHEMA_VERSION == 3
    with sqlite3.connect(path) as conn:          # history kept: v1/v2 not re-applied
        assert conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()[:2] == [
            (1, "2026-11-01T00:00:00Z"), (2, "2026-11-02T00:00:00Z")]

    ok = store.get_run_record("r-ok")             # back-filled from v2 metadata; zero stays zero
    assert (ok.input_tokens, ok.output_tokens, ok.total_tokens) == (700, 0, 700)
    assert (ok.cost_usd, ok.sdk_duration_ms, ok.duration_ms, ok.num_turns) == (0.25, 2900, 3000, 4)
    assert ok.metrics_source == "result" and ok.result_status == "partial" and ok.session_agent == "dae-a"
    bad = store.get_run_record("r-bad")           # non-integer usage is not coerced
    assert (bad.input_tokens, bad.output_tokens, bad.cost_usd, bad.metrics_source) == (None, None, None, None)
    assert (bad.error_code, bad.error_category, bad.failed_at) == ("agent.timeout", "timeout",
                                                                   "2026-11-02T10:00:09+00:00")
    assert [r.run_id for r in store.list_run_records(error_code="agent.timeout")] == ["r-bad"]
    for record in (ok, bad):
        contracts.validate("run-record", record.to_dict())

    legacy = store.get_agent_run("r-ok")          # the Phase 1/2 API reads the same row unchanged
    assert legacy.task == {"objective": "o"} and legacy.result == {"status": "partial"} and legacy.cost_usd == 0.25
    legacy.status = AgentRunStatus.SUCCEEDED
    store.save_agent_run(legacy)
    store.close()
    reopened = SQLiteExecutionStore(path)         # idempotent
    assert reopened.schema_version == 3 and reopened.get_run_record("r-ok").input_tokens == 700
    reopened.close()


def test_v3_adds_only_metadata_columns(tmp_path: Path) -> None:
    store = SQLiteExecutionStore(tmp_path / "h.db")
    with sqlite3.connect(store.path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")]
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(agent_runs)")}
    store.close()
    assert columns[-3:] == ["input_tokens", "output_tokens", "error_code"]
    assert {"ix_agent_runs_created", "ix_agent_runs_agent", "ix_agent_runs_error"} <= indexes
