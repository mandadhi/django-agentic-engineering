"""Status enums and the allowed-transition tables for executions, stages and agent runs.

Every status change in the runtime goes through :func:`ensure_transition`; the
tables below are the single source of truth for the lifecycle.

Execution:  pending → running → (paused ⇄ running) → succeeded | failed | cancelled
            failed → running            (resume)
Stage:      pending → running → succeeded | failed | awaiting_approval
            awaiting_approval → succeeded | failed
            failed → pending            (retry on resume)
            pending → skipped | cancelled
Agent run:  queued → running → succeeded | failed | cancelled
"""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping, TypeVar

from core.errors import InvalidTransitionError


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


E, S, A = ExecutionStatus, StageStatus, AgentRunStatus

EXECUTION_TRANSITIONS: Mapping[ExecutionStatus, frozenset[ExecutionStatus]] = {
    E.PENDING: frozenset({E.RUNNING, E.CANCELLED}),
    E.RUNNING: frozenset({E.PAUSED, E.SUCCEEDED, E.FAILED, E.CANCELLED}),
    E.PAUSED: frozenset({E.RUNNING, E.CANCELLED}),
    E.FAILED: frozenset({E.RUNNING, E.CANCELLED}),
    E.SUCCEEDED: frozenset(),
    E.CANCELLED: frozenset(),
}

STAGE_TRANSITIONS: Mapping[StageStatus, frozenset[StageStatus]] = {
    S.PENDING: frozenset({S.RUNNING, S.SKIPPED, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.AWAITING_APPROVAL, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED}),
    S.FAILED: frozenset({S.PENDING}),
    S.SUCCEEDED: frozenset(),
    S.SKIPPED: frozenset(),
    S.CANCELLED: frozenset(),
}

AGENT_RUN_TRANSITIONS: Mapping[AgentRunStatus, frozenset[AgentRunStatus]] = {
    A.QUEUED: frozenset({A.RUNNING, A.CANCELLED}),
    A.RUNNING: frozenset({A.SUCCEEDED, A.FAILED, A.CANCELLED}),
    A.SUCCEEDED: frozenset(),
    A.FAILED: frozenset(),
    A.CANCELLED: frozenset(),
}

#: Stage states that let dependants start.
STAGE_SATISFIED = frozenset({S.SUCCEEDED, S.SKIPPED})
STAGE_TERMINAL = frozenset({S.SUCCEEDED, S.SKIPPED, S.CANCELLED})
EXECUTION_TERMINAL = frozenset({E.SUCCEEDED, E.CANCELLED})

StatusT = TypeVar("StatusT", ExecutionStatus, StageStatus, AgentRunStatus)


def ensure_transition(entity: str, current: StatusT, target: StatusT,
                      table: Mapping[StatusT, frozenset[StatusT]]) -> StatusT:
    """Return ``target`` if ``current → target`` is allowed, else raise InvalidTransitionError."""
    if target not in table[current]:
        allowed = ", ".join(sorted(s.value for s in table[current])) or "none (terminal)"
        raise InvalidTransitionError(f"{entity}: cannot move from '{current}' to '{target}' (allowed: {allowed})")
    return target
