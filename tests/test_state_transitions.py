import pytest

from core.errors import InvalidTransitionError
from runtime.state import (
    AGENT_RUN_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    STAGE_TRANSITIONS,
    AgentRunStatus,
    ExecutionStatus,
    StageStatus,
    ensure_transition,
)


def test_tables_cover_every_status() -> None:
    assert set(EXECUTION_TRANSITIONS) == set(ExecutionStatus)
    assert set(STAGE_TRANSITIONS) == set(StageStatus)
    assert set(AGENT_RUN_TRANSITIONS) == set(AgentRunStatus)


@pytest.mark.parametrize("current,target", [
    (ExecutionStatus.PENDING, ExecutionStatus.RUNNING),
    (ExecutionStatus.RUNNING, ExecutionStatus.PAUSED),
    (ExecutionStatus.PAUSED, ExecutionStatus.RUNNING),
    (ExecutionStatus.FAILED, ExecutionStatus.RUNNING),
])
def test_allowed_execution_transitions(current, target) -> None:
    assert ensure_transition("execution", current, target, EXECUTION_TRANSITIONS) is target


@pytest.mark.parametrize("current,target", [
    (ExecutionStatus.SUCCEEDED, ExecutionStatus.RUNNING),
    (ExecutionStatus.CANCELLED, ExecutionStatus.RUNNING),
    (ExecutionStatus.PENDING, ExecutionStatus.SUCCEEDED),
])
def test_rejected_execution_transitions(current, target) -> None:
    with pytest.raises(InvalidTransitionError, match=f"cannot move from '{current}' to '{target}'"):
        ensure_transition("execution", current, target, EXECUTION_TRANSITIONS)


def test_terminal_message() -> None:
    with pytest.raises(InvalidTransitionError, match=r"allowed: none \(terminal\)"):
        ensure_transition("stage", StageStatus.SUCCEEDED, StageStatus.RUNNING, STAGE_TRANSITIONS)


def test_stage_lifecycle_in_manager(manager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    # dependencies are enforced
    with pytest.raises(InvalidTransitionError, match="dependencies not satisfied: threat-model"):
        manager.start_stage(eid, "sast")
    # cannot complete a stage that is not running
    with pytest.raises(InvalidTransitionError):
        manager.complete_stage(eid, "threat-model")

    assert manager.runnable_stages(eid) == ("threat-model",)
    manager.start_stage(eid, "threat-model")
    manager.complete_stage(eid, "threat-model")
    assert manager.runnable_stages(eid) == ("secrets-scan", "sast")

    for stage in ("secrets-scan", "sast"):
        manager.start_stage(eid, stage)
        manager.complete_stage(eid, stage)
    manager.start_stage(eid, "audit")
    audit = manager.complete_stage(eid, "audit")
    assert audit.status is StageStatus.AWAITING_APPROVAL          # approval gate
    assert manager.store.get_execution(eid).status is ExecutionStatus.RUNNING

    manager.approve_stage(eid, "audit", approver="lead@techwave.com")
    execution = manager.store.get_execution(eid)
    assert execution.status is ExecutionStatus.SUCCEEDED and execution.completed_at
    assert manager.store.get_stage(eid, "audit").approved_by == "lead@techwave.com"
    with pytest.raises(InvalidTransitionError):
        manager.pause(eid)


def test_skip_satisfies_dependants(manager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.complete_stage(eid, "threat-model")
    manager.skip_stage(eid, "secrets-scan", "scanner runs in CI already")
    manager.start_stage(eid, "sast")
    manager.complete_stage(eid, "sast")
    assert manager.runnable_stages(eid) == ("audit",)


def test_paused_execution_blocks_stage_work(manager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.pause(eid, "maintenance window")
    with pytest.raises(InvalidTransitionError, match="not running"):
        manager.start_stage(eid, "threat-model")


def test_cancel_cancels_open_stages(manager) -> None:
    eid = manager.start_execution("security-review", {"repository": "acme"}).execution_id
    manager.start_stage(eid, "threat-model")
    manager.cancel(eid, "superseded")
    assert {s.status for s in manager.store.list_stages(eid)} == {StageStatus.CANCELLED}
