"""Records persisted by an ExecutionStore. Metadata only — artifacts are references.

The read-only execution contract built from these rows is :class:`runtime.records.RunRecord`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .state import AgentRunStatus, ExecutionStatus, StageStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class FailureInfo:
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": dict(self.details)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "FailureInfo | None":
        if not data:
            return None
        return cls(data["code"], data["message"], data.get("retryable", False), data.get("details", {}))


@dataclass
class Execution:
    execution_id: str
    workflow_id: str
    workflow_version: str
    status: ExecutionStatus
    created_at: str
    updated_at: str
    inputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    error: FailureInfo | None = None


@dataclass
class StageState:
    execution_id: str
    stage_id: str
    agent_id: str
    status: StageStatus
    position: int                       # topological position, for stable ordering
    attempts: int = 0
    max_attempts: int = 1
    started_at: str | None = None
    completed_at: str | None = None
    error: FailureInfo | None = None
    approved_by: str | None = None


@dataclass
class AgentRun:
    run_id: str
    execution_id: str
    stage_id: str
    agent_id: str
    task_id: str
    attempt: int
    status: AgentRunStatus
    task: dict[str, Any]                # AgentTask document
    created_at: str
    result: dict[str, Any] | None = None  # AgentResult document (references only)
    started_at: str | None = None
    completed_at: str | None = None
    error: FailureInfo | None = None
    # --- Agent SDK execution metadata (schema v2). Identifiers and numbers only:
    # the conversation itself stays in the SDK's own session transcript.
    session_id: str | None = None         # SDK session, used to resume an interrupted run
    cost_usd: float | None = None         # ResultMessage.total_cost_usd
    duration_ms: int | None = None        # ResultMessage.duration_ms
    num_turns: int | None = None
    sdk_metadata: dict[str, Any] = field(default_factory=dict)  # model, terminal_reason, usage, ...
    # --- schema v3 (Phase 5.3): typed token metrics. None = not reported (never a stand-in zero).
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    execution_id: str
    sequence: int
    reason: str
    state: Mapping[str, Any]
    created_at: str
    stage_id: str | None = None


@dataclass(frozen=True)
class LogEntry:
    execution_id: str
    event: str
    message: str
    level: str = "INFO"
    timestamp: str = field(default_factory=utcnow)
    stage_id: str | None = None
    run_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    """Where an artifact lives — never the artifact itself."""

    artifact_id: str
    execution_id: str
    uri: str
    created_at: str
    stage_id: str | None = None
    run_id: str | None = None
    name: str | None = None
    media_type: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    description: str | None = None
