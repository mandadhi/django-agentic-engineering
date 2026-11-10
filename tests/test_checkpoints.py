from runtime.execution import ExecutionManager
from runtime.sqlite_store import SQLiteExecutionStore
from runtime.models import FailureInfo


def test_store_sequences_checkpoints(store: SQLiteExecutionStore) -> None:
    from tests.test_sqlite_store import make_execution

    make_execution(store)
    first = store.add_checkpoint("e1", reason="manual", state={"n": 1})
    second = store.add_checkpoint("e1", reason="manual", state={"n": 2}, stage_id="a")
    assert (first.sequence, second.sequence) == (1, 2)
    latest = store.latest_checkpoint("e1")
    assert latest.checkpoint_id == second.checkpoint_id and latest.state == {"n": 2} and latest.stage_id == "a"


def test_execution_start_writes_checkpoint(manager: ExecutionManager) -> None:
    execution = manager.start_execution("security-review", {"repository": "acme"})
    [checkpoint] = manager.store.list_checkpoints(execution.execution_id)
    assert checkpoint.reason == "start"
    assert checkpoint.state["stages"]["threat-model"] == {"status": "pending", "attempts": 0}
    assert checkpoint.state["workflow_version"] == "0.1.0"


def test_checkpoint_after_each_stage(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.complete_stage(eid, "threat-model")
    latest = manager.store.latest_checkpoint(eid)
    assert latest.reason == "stage_completed" and latest.stage_id == "threat-model"
    assert latest.state["stages"]["threat-model"]["status"] == "succeeded"


def test_failure_and_manual_checkpoints(manager: ExecutionManager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.fail_stage(eid, "threat-model", FailureInfo("agent.error", "model overloaded", retryable=True))
    assert manager.store.latest_checkpoint(eid).reason == "failure"
    manual = manager.checkpoint(eid, reason="operator", extra={"note": "investigating"})
    assert manual.state["extra"] == {"note": "investigating"}
    assert [c.reason for c in manager.store.list_checkpoints(eid)] == ["start", "failure", "operator"]
