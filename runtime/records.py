"""RunRecord: the versioned, read-only execution contract (Phase 5.3).

Granularity (see README "Execution records"):

* **workflow execution** — one ``executions`` row (``execution_id``); its outcome is the
  execution status. :class:`ExecutionSummary` aggregates its runs.
* **stage** — one ``stages`` row per (execution, stage): the *current* state of that
  workflow step (status, attempts counter). It is overwritten as the stage moves on.
* **agent-stage execution** — one ``agent_runs`` row per attempt (``run_id``). Every
  attempt, retry and resumed attempt gets its own row, so history is never overwritten.
  A :class:`RunRecord` is the contract view of exactly one such row.

A RunRecord is *derived* from the stored row (plus the execution's workflow id), never
stored a second time. It carries execution metadata only: no task objective, prompt,
inputs, agent result content or conversation.

Status vocabulary is :class:`runtime.state.AgentRunStatus`. No extra statuses are added;
the cause of a non-success is the ``error_category``:

=================  =========================  ==============================
outcome            status                     error_code / error_category
=================  =========================  ==============================
timeout            failed                     ``agent.timeout`` / timeout
interrupted        cancelled                  ``runtime.interrupted`` / interrupted
deliberate cancel  cancelled                  ``runtime.cancelled`` / cancelled
=================  =========================  ==============================

Metrics distinguish *unavailable* (``None``) from zero. ``metrics_source`` says where they
came from: ``"result"`` (the SDK's final ResultMessage), ``"partial"`` (usage observed on
assistant messages before the stream stopped — a lower bound, no cost or turns), or
``None`` (nothing reported).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .models import AgentRun, Execution, FailureInfo, StageState
from .state import AgentRunStatus, ExecutionStatus

#: RunRecord contract version (contracts/run-record.schema.json). Major bump = breaking change.
RUN_RECORD_VERSION = "1.0"

#: sdk_metadata keys written by the runner that the record lifts into typed fields.
SESSION_AGENT_KEY = "session_agent"
RESUMED_FROM_KEY = "resumed_from_run"          # same value as agent_runtime.session_manager.RESUMED_FROM
METRICS_SOURCE_KEY = "metrics_source"

#: Error categories. Small and closed: evaluation code groups by these, codes stay detailed.
CATEGORIES = ("timeout", "cancelled", "interrupted", "sdk", "agent", "result", "security", "configuration",
              "approval", "internal")

_EXACT = {"agent.timeout": "timeout", "runtime.cancelled": "cancelled", "runtime.interrupted": "interrupted",
          "runtime.prepare_failed": "configuration", "agent.invalid_definition": "configuration",
          "agent.definition_conflict": "configuration"}
_PREFIX = {"sdk": "sdk", "agent": "agent", "result": "result", "security": "security", "plugin": "configuration",
           "task": "configuration", "approval": "approval"}


def error_category(code: str | None) -> str | None:
    """Stable category of an error code (``None`` when there is no error)."""
    if not code:
        return None
    if code in _EXACT:
        return _EXACT[code]
    return _PREFIX.get(code.split(".", 1)[0], "internal")


def _ms_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    ms = int(delta.total_seconds() * 1000)
    return ms if ms >= 0 else None          # clock skew: unavailable, never negative


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_tokens(usage: Mapping[str, Any] | None) -> tuple[int | None, int | None]:
    """(input_tokens, output_tokens) from an SDK usage dict; a missing/non-integer value stays None."""
    usage = usage or {}
    return _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))


def total_tokens(input_tokens: int | None, output_tokens: int | None) -> int | None:
    """Uncached input + output tokens, only when both are known (cache tokens stay in sdk_metadata.usage)."""
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


@dataclass(frozen=True)
class RunRecord:
    """One agent-stage execution attempt. See the module docstring for semantics."""

    # identity and correlation
    run_id: str
    execution_id: str
    workflow_id: str
    workflow_version: str
    stage_id: str
    agent_id: str
    task_id: str
    attempt: int
    session_agent: str | None
    session_id: str | None
    resumed_from_run: str | None
    # lifecycle
    status: AgentRunStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None            # harness wall clock, started_at → completed_at
    sdk_duration_ms: int | None        # ResultMessage.duration_ms
    # metrics (None = unavailable, never a stand-in zero)
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    num_turns: int | None
    metrics_source: str | None
    # outcome / failure
    result_status: str | None          # AgentResult.status (distinguishes 'partial' from 'succeeded')
    error_code: str | None
    error_category: str | None
    error_retryable: bool | None
    failed_at: str | None
    sdk_metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = RUN_RECORD_VERSION
    kind: str = "RunRecord"

    @property
    def terminal(self) -> bool:
        return self.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready document conforming to contracts/run-record.schema.json (None values omitted)."""
        data = asdict(self)
        data["status"] = self.status.value
        data["sdk_metadata"] = dict(self.sdk_metadata)
        return {k: v for k, v in data.items() if v is not None}


def build_run_record(run: AgentRun, *, workflow_id: str, workflow_version: str,
                     result_status: str | None = None) -> RunRecord:
    """Project a stored AgentRun into the RunRecord contract (pure; no I/O).

    ``result_status`` defaults to ``run.result["status"]``; a store may pass it directly so it
    never has to load the AgentResult document itself.
    """
    meta = dict(run.sdk_metadata or {})
    error: FailureInfo | None = run.error
    code = error.code if error else None
    category = error_category(code)
    if result_status is None and run.result:
        result_status = run.result.get("status")
    input_tokens, output_tokens = run.input_tokens, run.output_tokens
    failed = run.status is AgentRunStatus.FAILED or category == "interrupted"
    source = meta.get(METRICS_SOURCE_KEY)
    if source is None and any(v is not None for v in (run.cost_usd, run.num_turns, run.duration_ms,
                                                      input_tokens, output_tokens)):
        source = "result"                   # pre-v3 rows: metrics only ever came from the ResultMessage
    return RunRecord(
        run_id=run.run_id, execution_id=run.execution_id, workflow_id=workflow_id,
        workflow_version=workflow_version, stage_id=run.stage_id, agent_id=run.agent_id, task_id=run.task_id,
        attempt=run.attempt, session_agent=meta.get(SESSION_AGENT_KEY), session_id=run.session_id,
        resumed_from_run=meta.get(RESUMED_FROM_KEY), status=run.status, created_at=run.created_at,
        started_at=run.started_at, completed_at=run.completed_at,
        duration_ms=_ms_between(run.started_at, run.completed_at), sdk_duration_ms=run.duration_ms,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens(input_tokens, output_tokens), cost_usd=run.cost_usd, num_turns=run.num_turns,
        metrics_source=source, result_status=result_status, error_code=code, error_category=category,
        error_retryable=error.retryable if error else None,
        failed_at=run.completed_at if failed else None, sdk_metadata=meta,
    )


# ---------------------------------------------------------------------- workflow level


def _sum(values: Sequence[Any]) -> Any:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


@dataclass(frozen=True)
class ExecutionSummary:
    """Overall workflow outcome plus metrics aggregated over every run (all attempts).

    A metric total is ``None`` when no run reported it; ``metrics_complete`` is False when at
    least one finished run did not report cost or tokens, so a total is a lower bound.
    """

    execution_id: str
    workflow_id: str
    workflow_version: str
    status: ExecutionStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    failed_at: str | None
    duration_ms: int | None
    error_code: str | None
    error_category: str | None
    stages: Mapping[str, str]                # stage_id → stage status
    runs: int
    runs_by_status: Mapping[str, int]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    num_turns: int | None
    metrics_complete: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


def summarize_execution(execution: Execution, stages: Sequence[StageState],
                        records: Sequence[RunRecord]) -> ExecutionSummary:
    finished = [r for r in records if r.terminal and r.started_at is not None]
    complete = all(r.cost_usd is not None and r.total_tokens is not None for r in finished)
    by_status: dict[str, int] = {}
    for record in records:
        by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
    code = execution.error.code if execution.error else None
    failed_at = execution.updated_at if execution.status is ExecutionStatus.FAILED else None
    cost = _sum([r.cost_usd for r in records])
    return ExecutionSummary(
        execution_id=execution.execution_id, workflow_id=execution.workflow_id,
        workflow_version=execution.workflow_version, status=execution.status, created_at=execution.created_at,
        started_at=execution.started_at, completed_at=execution.completed_at, failed_at=failed_at,
        duration_ms=_ms_between(execution.started_at, execution.completed_at),
        error_code=code, error_category=error_category(code),
        stages={s.stage_id: s.status.value for s in stages}, runs=len(records), runs_by_status=by_status,
        input_tokens=_sum([r.input_tokens for r in records]), output_tokens=_sum([r.output_tokens for r in records]),
        total_tokens=_sum([r.total_tokens for r in records]),
        cost_usd=round(cost, 6) if cost is not None else None, num_turns=_sum([r.num_turns for r in records]),
        metrics_complete=complete,
    )
