"""ExecutionManager: the state machine that tracks a workflow execution.

It creates executions from registered workflow definitions, moves stages and
agent runs through the transitions in :mod:`runtime.state`, validates every
AgentTask/AgentResult against the contracts, records artifact *references*,
writes checkpoints and an execution log, and computes a resume plan after a
failure, pause or crash. It never calls an agent — a runner
(:mod:`agent_runtime.runner`) does that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from core.context import ContextBundle, TokenEstimator
from core.contracts import schema_issues
from core.errors import ContractValidationError, HarnessError, InputValidationError, InvalidTransitionError, \
    ValidationIssue
from core.log import get_logger
from core.registry import HarnessRegistry
from core.validation import check_workflow
from core.workflows import WorkflowDefinition, render_inputs

#: Execution metadata key holding an ad-hoc (single-agent) workflow definition, so the
#: execution stays resumable by any process even though the workflow is not in workflows/.
ADHOC_DEFINITION_KEY = "dae.workflow_definition"

from .models import AgentRun, ArtifactRecord, Checkpoint, Execution, FailureInfo, LogEntry, StageState, utcnow
from .records import ExecutionSummary, summarize_execution
from .state import (
    AGENT_RUN_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    STAGE_SATISFIED,
    STAGE_TRANSITIONS,
    AgentRunStatus,
    ExecutionStatus,
    StageStatus,
    ensure_transition,
)
from .store import ExecutionStore

log = get_logger(__name__)

CONTRACT_VERSION = "1.0"

#: Error code of a stage or agent run that was in progress when its process stopped.
INTERRUPTED = "runtime.interrupted"

# AgentResult.status → AgentRun status
_RESULT_TO_RUN = {
    "succeeded": AgentRunStatus.SUCCEEDED,
    "partial": AgentRunStatus.SUCCEEDED,
    "failed": AgentRunStatus.FAILED,
    "blocked": AgentRunStatus.FAILED,
    "cancelled": AgentRunStatus.CANCELLED,
}


@dataclass(frozen=True)
class ResumePlan:
    execution_id: str
    completed: tuple[str, ...]            # succeeded or skipped
    runnable: tuple[str, ...]             # can start now
    pending: tuple[str, ...]              # waiting on dependencies
    awaiting_approval: tuple[str, ...]
    exhausted: tuple[str, ...]            # failed with no attempts left — needs intervention
    checkpoint: Checkpoint | None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class ExecutionManager:
    def __init__(self, store: ExecutionStore, registry: HarnessRegistry, *,
                 redact: Callable[[str], str] | None = None, clock: Callable[[], str] = utcnow,
                 id_factory: Callable[[str], str] = _new_id) -> None:
        self.store = store
        self.registry = registry
        self._redact = redact or (lambda text: text)
        self._now = clock
        self._new_id = id_factory
        self._checkpoint_each_stage = registry.config.workflow.defaults.checkpoint_policy == "after_each_stage"
        self._adhoc: dict[str, WorkflowDefinition] = {}

    # ------------------------------------------------------------------ helpers

    def workflow_for(self, execution: Execution) -> WorkflowDefinition:
        """The definition an execution runs: a registered workflow, or the ad-hoc one stored with it."""
        if execution.workflow_id in self.registry.workflows:
            return self.registry.workflows.get(execution.workflow_id)
        if execution.execution_id not in self._adhoc:
            document = execution.metadata.get(ADHOC_DEFINITION_KEY)
            if not document:
                return self.registry.workflows.get(execution.workflow_id)   # raises "unknown workflow"
            self._adhoc[execution.execution_id] = self._build_adhoc(document)
        return self._adhoc[execution.execution_id]

    _workflow = workflow_for

    def _build_adhoc(self, document: Mapping[str, Any]) -> WorkflowDefinition:
        self.registry.contracts.validate("workflow-definition", document, "ad-hoc workflow")
        workflow = WorkflowDefinition.from_dict(document, self.registry.config.workflow.defaults)
        issues = check_workflow(workflow, f"workflow:{workflow.id}", {a.id: a.enabled for a in self.registry.agents})
        if issues:
            raise ContractValidationError("Invalid ad-hoc workflow", issues)
        return workflow

    def validate_inputs(self, workflow: WorkflowDefinition, inputs: Mapping[str, Any]) -> None:
        """Inputs must satisfy the workflow's ``inputs`` JSON Schema (required, types, enums, ...) and
        include every input a stage requires (its ``inputs`` list and ``${inputs.x}`` references)."""
        source = f"workflow:{workflow.id}/inputs"
        issues: list[ValidationIssue] = []
        schema_required: set[str] = set()
        if workflow.inputs_schema is not None:
            issues += schema_issues(workflow.inputs_schema, dict(inputs), source)
            schema_required = set(workflow.inputs_schema.get("required") or ())
        for stage in workflow.stages:
            for name in stage.required_inputs():
                if name not in inputs and name not in schema_required:
                    issues.append(ValidationIssue(source, name, f"stage '{stage.id}' requires input '{name}'",
                                                  "missing-input"))
        if issues:
            raise InputValidationError(f"Invalid inputs for workflow '{workflow.id}'", issues)

    def _log(self, execution_id: str, event: str, message: str, *, level: str = "INFO",
             stage_id: str | None = None, run_id: str | None = None, **data: Any) -> None:
        message = self._redact(message)
        self.store.append_log(LogEntry(execution_id=execution_id, event=event, message=message, level=level,
                                       timestamp=self._now(), stage_id=stage_id, run_id=run_id, data=data))
        fields = {"execution_id": execution_id, "stage_id": stage_id, "run_id": run_id, **data}
        log.log(_LEVELS.get(level, 20), event, extra={k: v for k, v in fields.items() if v is not None})

    def _set_execution(self, execution: Execution, target: ExecutionStatus) -> None:
        ensure_transition(f"execution {execution.execution_id}", execution.status, target, EXECUTION_TRANSITIONS)
        execution.status = target
        execution.updated_at = self._now()
        if target is ExecutionStatus.RUNNING and execution.started_at is None:
            execution.started_at = execution.updated_at
        if target in (ExecutionStatus.SUCCEEDED, ExecutionStatus.CANCELLED):
            execution.completed_at = execution.updated_at

    def _set_stage(self, stage: StageState, target: StageStatus) -> None:
        ensure_transition(f"stage {stage.stage_id}", stage.status, target, STAGE_TRANSITIONS)
        stage.status = target
        if target is StageStatus.RUNNING:
            stage.started_at, stage.completed_at = self._now(), None
        elif target in (StageStatus.SUCCEEDED, StageStatus.FAILED, StageStatus.SKIPPED, StageStatus.CANCELLED):
            stage.completed_at = self._now()

    def _running(self, execution_id: str) -> Execution:
        execution = self.store.get_execution(execution_id)
        if execution.status is not ExecutionStatus.RUNNING:
            raise InvalidTransitionError(f"execution {execution_id} is '{execution.status}', not running")
        return execution

    def _snapshot(self, execution: Execution, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        stages = self.store.list_stages(execution.execution_id)
        return {
            "workflow_id": execution.workflow_id,
            "workflow_version": execution.workflow_version,
            "execution_status": execution.status.value,
            "stages": {s.stage_id: {"status": s.status.value, "attempts": s.attempts} for s in stages},
            "extra": dict(extra or {}),
        }

    def _finish_if_done(self, execution: Execution) -> None:
        stages = self.store.list_stages(execution.execution_id)
        if all(s.status in STAGE_SATISFIED for s in stages):
            self._set_execution(execution, ExecutionStatus.SUCCEEDED)
            self.store.update_execution(execution)
            self._log(execution.execution_id, "execution.succeeded", "All stages completed")

    # ------------------------------------------------------------------ executions

    def start_execution(self, workflow_id: str, inputs: Mapping[str, Any] | None = None,
                        metadata: Mapping[str, Any] | None = None, *,
                        definition: Mapping[str, Any] | None = None) -> Execution:
        """Start a registered workflow, or — with ``definition`` — an ad-hoc one (e.g. a single agent).

        Inputs are validated before anything is persisted (:class:`InputValidationError`).
        """
        metadata = dict(metadata or {})
        if definition is not None:
            workflow = self._build_adhoc(definition)
            if workflow.id in self.registry.workflows:
                raise HarnessError(f"ad-hoc workflow id '{workflow.id}' collides with a registered workflow")
            metadata[ADHOC_DEFINITION_KEY] = dict(definition)
        else:
            workflow = self.registry.workflows.get(workflow_id)
        inputs = dict(inputs or {})
        self.validate_inputs(workflow, inputs)
        now = self._now()
        execution = Execution(execution_id=self._new_id("exec"), workflow_id=workflow.id,
                              workflow_version=workflow.version, status=ExecutionStatus.PENDING, created_at=now,
                              updated_at=now, inputs=inputs, metadata=metadata)
        if definition is not None:
            self._adhoc[execution.execution_id] = workflow
        stages = [
            StageState(execution_id=execution.execution_id, stage_id=stage_id,
                       agent_id=workflow.stage(stage_id).agent_id, status=StageStatus.PENDING, position=position,
                       max_attempts=workflow.stage(stage_id).max_attempts)
            for position, stage_id in enumerate(workflow.topological_order())
        ]
        self._set_execution(execution, ExecutionStatus.RUNNING)
        with self.store.transaction():
            self.store.create_execution(execution, stages)
            self._log(execution.execution_id, "execution.started",
                      f"Started {workflow.id} v{workflow.version}", stages=len(stages))
            self.store.add_checkpoint(execution.execution_id, reason="start", state=self._snapshot(execution))
        return execution

    def pause(self, execution_id: str, reason: str = "") -> Execution:
        execution = self.store.get_execution(execution_id)
        self._set_execution(execution, ExecutionStatus.PAUSED)
        with self.store.transaction():
            self.store.update_execution(execution)
            self._log(execution_id, "execution.paused", reason or "Paused")
            self.store.add_checkpoint(execution_id, reason="pause", state=self._snapshot(execution))
        return execution

    def cancel(self, execution_id: str, reason: str = "") -> Execution:
        execution = self.store.get_execution(execution_id)
        self._set_execution(execution, ExecutionStatus.CANCELLED)
        with self.store.transaction():
            for stage in self.store.list_stages(execution_id):
                if StageStatus.CANCELLED in STAGE_TRANSITIONS[stage.status]:
                    self._set_stage(stage, StageStatus.CANCELLED)
                    self.store.update_stage(stage)
            self.store.update_execution(execution)
            self._log(execution_id, "execution.cancelled", reason or "Cancelled", level="WARNING")
        return execution

    def checkpoint(self, execution_id: str, *, reason: str = "manual", stage_id: str | None = None,
                   extra: Mapping[str, Any] | None = None) -> Checkpoint:
        execution = self.store.get_execution(execution_id)
        return self.store.add_checkpoint(execution_id, reason=reason, stage_id=stage_id,
                                         state=self._snapshot(execution, extra))

    # ------------------------------------------------------------------ stages

    def runnable_stages(self, execution_id: str) -> tuple[str, ...]:
        execution = self.store.get_execution(execution_id)
        stages = self.store.list_stages(execution_id)
        satisfied = [s.stage_id for s in stages if s.status in STAGE_SATISFIED]
        pending = [s.stage_id for s in stages if s.status is StageStatus.PENDING]
        return self._workflow(execution).runnable_stages(satisfied, pending)

    def start_stage(self, execution_id: str, stage_id: str) -> StageState:
        execution = self._running(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        if stage.status is StageStatus.PENDING and stage_id not in self.runnable_stages(execution_id):
            unmet = [d for d in self._workflow(execution).stage(stage_id).depends_on
                     if self.store.get_stage(execution_id, d).status not in STAGE_SATISFIED]
            raise InvalidTransitionError(f"stage {stage_id}: dependencies not satisfied: {', '.join(unmet)}")
        if stage.attempts >= stage.max_attempts:
            raise InvalidTransitionError(f"stage {stage_id}: no attempts left ({stage.attempts}/{stage.max_attempts})")
        self._set_stage(stage, StageStatus.RUNNING)
        stage.attempts += 1
        stage.error = None
        with self.store.transaction():
            self.store.update_stage(stage)
            self._log(execution_id, "stage.started", f"Stage {stage_id} attempt {stage.attempts}",
                      stage_id=stage_id, attempt=stage.attempts)
        return stage

    def complete_stage(self, execution_id: str, stage_id: str) -> StageState:
        """Mark a running stage done. Gated stages wait in ``awaiting_approval``."""
        execution = self._running(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        definition = self._workflow(execution).stage(stage_id)
        target = StageStatus.AWAITING_APPROVAL if definition.approval_required else StageStatus.SUCCEEDED
        self._set_stage(stage, target)
        with self.store.transaction():
            self.store.update_stage(stage)
            self._log(execution_id, f"stage.{target.value}", f"Stage {stage_id} {target.value}", stage_id=stage_id)
            if target is StageStatus.SUCCEEDED:
                if self._checkpoint_each_stage:
                    self.store.add_checkpoint(execution_id, reason="stage_completed", stage_id=stage_id,
                                              state=self._snapshot(execution))
                self._finish_if_done(execution)
        return stage

    def approve_stage(self, execution_id: str, stage_id: str, approver: str) -> StageState:
        execution = self._running(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        self._set_stage(stage, StageStatus.SUCCEEDED)
        stage.approved_by = approver
        with self.store.transaction():
            self.store.update_stage(stage)
            self._log(execution_id, "stage.approved", f"Stage {stage_id} approved", stage_id=stage_id,
                      approver=approver)
            if self._checkpoint_each_stage:
                self.store.add_checkpoint(execution_id, reason="stage_approved", stage_id=stage_id,
                                          state=self._snapshot(execution))
            self._finish_if_done(execution)
        return stage

    def retry_stage(self, execution_id: str, stage_id: str) -> StageState:
        """Put a failed, retryable stage with attempts left back to ``pending`` (the execution resumes running).

        This is the workflow engine's retry decision; the agent runner never retries on its own.
        """
        execution = self.store.get_execution(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        if stage.status is not StageStatus.FAILED:
            raise InvalidTransitionError(f"stage {stage_id} is '{stage.status}'; only failed stages can be retried")
        if stage.attempts >= stage.max_attempts:
            raise InvalidTransitionError(f"stage {stage_id}: no attempts left ({stage.attempts}/{stage.max_attempts})")
        if stage.error is not None and not stage.error.retryable:
            raise InvalidTransitionError(f"stage {stage_id}: error '{stage.error.code}' is not retryable")
        self._set_stage(stage, StageStatus.PENDING)
        with self.store.transaction():
            self.store.update_stage(stage)
            if execution.status is ExecutionStatus.FAILED:
                self._set_execution(execution, ExecutionStatus.RUNNING)
                execution.error = None
                self.store.update_execution(execution)
            self._log(execution_id, "stage.retry", f"Stage {stage_id} retry after attempt {stage.attempts}",
                      stage_id=stage_id, attempt=stage.attempts)
        return stage

    def reject_stage(self, execution_id: str, stage_id: str, reviewer: str, reason: str) -> StageState:
        return self.fail_stage(execution_id, stage_id,
                               FailureInfo("approval.rejected", reason, retryable=True, details={"reviewer": reviewer}))

    def skip_stage(self, execution_id: str, stage_id: str, reason: str) -> StageState:
        execution = self._running(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        self._set_stage(stage, StageStatus.SKIPPED)
        with self.store.transaction():
            self.store.update_stage(stage)
            self._log(execution_id, "stage.skipped", reason, stage_id=stage_id)
            self._finish_if_done(execution)
        return stage

    def fail_stage(self, execution_id: str, stage_id: str, error: FailureInfo) -> StageState:
        """Record a stage failure; the execution fails and can later be resumed."""
        execution = self.store.get_execution(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        safe_error = FailureInfo(error.code, self._redact(error.message), error.retryable, error.details)
        self._set_stage(stage, StageStatus.FAILED)
        stage.error = safe_error
        with self.store.transaction():
            self.store.update_stage(stage)
            if execution.status is ExecutionStatus.RUNNING:
                self._set_execution(execution, ExecutionStatus.FAILED)
                execution.error = FailureInfo(safe_error.code, f"stage {stage_id}: {safe_error.message}",
                                              safe_error.retryable, {"stage_id": stage_id})
                self.store.update_execution(execution)
            self._log(execution_id, "stage.failed", safe_error.message, level="ERROR", stage_id=stage_id,
                      code=safe_error.code, retryable=safe_error.retryable)
            self.store.add_checkpoint(execution_id, reason="failure", stage_id=stage_id,
                                      state=self._snapshot(execution))
        return stage

    # ------------------------------------------------------------------ agent runs

    def create_task(self, execution_id: str, stage_id: str, *, context: ContextBundle,
                    estimator: TokenEstimator, extensions: Mapping[str, Any] | None = None) -> AgentRun:
        """Build the AgentTask for a running stage, validate it and queue an agent run."""
        execution = self._running(execution_id)
        stage = self.store.get_stage(execution_id, stage_id)
        if stage.status is not StageStatus.RUNNING:
            raise InvalidTransitionError(f"stage {stage_id} is '{stage.status}', start it before creating a task")
        definition = self._workflow(execution).stage(stage_id)
        if context.agent_id != definition.agent_id:
            raise HarnessError(f"context was assembled for '{context.agent_id}', stage expects '{definition.agent_id}'")
        task_id = self._new_id("task")
        inputs = execution.inputs
        constraints = [dict(c.to_dict(), description=render_inputs(c.description, inputs))
                       for c in definition.constraints]
        task: dict[str, Any] = {
            "schema_version": CONTRACT_VERSION,
            "kind": "AgentTask",
            "task_id": task_id,
            "agent_id": definition.agent_id,
            "execution_id": execution_id,
            "workflow_id": execution.workflow_id,
            "stage_id": stage_id,
            "attempt": stage.attempts,
            "objective": render_inputs(definition.objective, inputs),
            "context": context.to_task_context(estimator),
            "constraints": constraints,
            "dependencies": [{"type": "stage", "ref": dep, "required": True} for dep in definition.depends_on],
            "expected_outputs": [o.to_dict() for o in definition.expected_outputs],
            "created_at": self._now(),
        }
        extensions = dict(extensions or {})
        # A stage sees only the workflow inputs it declares, never the whole input set.
        stage_inputs = {name: inputs[name] for name in definition.required_inputs() if name in inputs}
        if stage_inputs:
            extensions["dae.inputs"] = stage_inputs
        if extensions:
            task["extensions"] = extensions
        self.registry.contracts.validate("agent-task", task)
        run = AgentRun(run_id=self._new_id("run"), execution_id=execution_id, stage_id=stage_id,
                       agent_id=definition.agent_id, task_id=task_id, attempt=stage.attempts,
                       status=AgentRunStatus.QUEUED, task=task, created_at=self._now())
        with self.store.transaction():
            self.store.save_agent_run(run)
            self._log(execution_id, "agent_run.queued", f"Task {task_id} for {run.agent_id}", stage_id=stage_id,
                      run_id=run.run_id, tokens=context.budget.used, omitted=len(context.omitted))
        return run

    def start_agent_run(self, run_id: str) -> AgentRun:
        run = self.store.get_agent_run(run_id)
        run.status = ensure_transition(f"run {run_id}", run.status, AgentRunStatus.RUNNING, AGENT_RUN_TRANSITIONS)
        run.started_at = self._now()
        with self.store.transaction():
            self.store.save_agent_run(run)
            self._log(run.execution_id, "agent_run.started", f"Agent {run.agent_id} started",
                      stage_id=run.stage_id, run_id=run_id)
        return run

    def update_run_metadata(self, run_id: str, *, session_id: str | None = None, cost_usd: float | None = None,
                            duration_ms: int | None = None, num_turns: int | None = None,
                            input_tokens: int | None = None, output_tokens: int | None = None,
                            sdk_metadata: Mapping[str, Any] | None = None) -> AgentRun:
        """Attach Agent SDK execution metadata to a run (identifiers and numbers, never transcripts).

        Called as soon as the SDK reports its session id, so a crash mid-run leaves a resumable session.
        ``None`` means "not reported" and leaves the stored value untouched. A finished run is history:
        its metadata can no longer change (InvalidTransitionError).
        """
        run = self.store.get_agent_run(run_id)
        if run.status not in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
            raise InvalidTransitionError(f"run {run_id} is '{run.status}'; a finished run's record is immutable")
        new_session = session_id is not None and session_id != run.session_id
        if session_id is not None:
            run.session_id = session_id
        if cost_usd is not None:
            run.cost_usd = cost_usd
        if duration_ms is not None:
            run.duration_ms = duration_ms
        if num_turns is not None:
            run.num_turns = num_turns
        if input_tokens is not None:
            run.input_tokens = input_tokens
        if output_tokens is not None:
            run.output_tokens = output_tokens
        if sdk_metadata:
            run.sdk_metadata = {**run.sdk_metadata, **sdk_metadata}
        with self.store.transaction():
            self.store.save_agent_run(run)
            if new_session:
                self._log(run.execution_id, "agent_run.session", f"SDK session {session_id}", stage_id=run.stage_id,
                          run_id=run_id, session_id=session_id)
        return run

    def audit(self, execution_id: str, event: str, message: str, *, level: str = "INFO", stage_id: str | None = None,
              run_id: str | None = None, **data: Any) -> None:
        """Append a (redacted) audit entry to the execution log, e.g. a tool-permission decision."""
        self._log(execution_id, event, message, level=level, stage_id=stage_id, run_id=run_id, **data)

    def record_result(self, run_id: str, result: Mapping[str, Any]) -> AgentRun:
        """Validate an AgentResult, store it (references only) and register its artifacts."""
        run = self.store.get_agent_run(run_id)
        self.registry.contracts.validate("agent-result", result)
        if result["task_id"] != run.task_id or result["agent_id"] != run.agent_id:
            raise HarnessError(f"result for task {result['task_id']}/{result['agent_id']} does not match run "
                               f"{run_id} ({run.task_id}/{run.agent_id})")
        target = _RESULT_TO_RUN[result["status"]]
        run.status = ensure_transition(f"run {run_id}", run.status, target, AGENT_RUN_TRANSITIONS)
        run.result = dict(result)
        run.completed_at = self._now()
        errors = result.get("errors") or []
        if errors:
            first = errors[0]
            run.error = FailureInfo(first["code"], self._redact(first["message"]), first.get("retryable", False))
        with self.store.transaction():
            self.store.save_agent_run(run)
            refs = [(o["name"], o["artifact"]) for o in result.get("outputs", [])]
            refs += [(None, a) for a in result.get("artifacts", [])]
            for name, ref in refs:
                self.store.add_artifact(ArtifactRecord(
                    artifact_id=self._new_id("art"), execution_id=run.execution_id, uri=ref["uri"],
                    created_at=self._now(), stage_id=run.stage_id, run_id=run_id, name=name,
                    media_type=ref.get("media_type"), sha256=ref.get("sha256"), size_bytes=ref.get("size_bytes"),
                    description=ref.get("description")))
            self._log(run.execution_id, f"agent_run.{run.status.value}",
                      f"Agent {run.agent_id} returned {result['status']}",
                      level="INFO" if target is AgentRunStatus.SUCCEEDED else "ERROR",
                      stage_id=run.stage_id, run_id=run_id, artifacts=len(refs))
        return run

    # ------------------------------------------------------------------ observability (Phase 5.3)

    def execution_summary(self, execution_id: str) -> ExecutionSummary:
        """Overall workflow outcome with metrics aggregated over every agent run (all attempts)."""
        execution = self.store.get_execution(execution_id)
        records = self.store.list_run_records(execution_id=execution_id, newest_first=False, limit=10_000)
        return summarize_execution(execution, self.store.list_stages(execution_id), records)

    # ------------------------------------------------------------------ resume

    def resume(self, execution_id: str) -> ResumePlan:
        """Bring a failed, paused or crashed execution back to running.

        * stages left ``running`` by a crash are failed as ``runtime.interrupted`` and their
          attempt is refunded (retry limits count failures, not process deaths), and their
          in-progress runs are marked interrupted so the runner can resume the SDK session;
        * failed stages with attempts left go back to ``pending``;
        * failed stages without attempts left are reported as ``exhausted``.

        Assumes the process that was driving the execution has stopped.
        """
        execution = self.store.get_execution(execution_id)
        if execution.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.CANCELLED):
            raise InvalidTransitionError(f"execution {execution_id} is '{execution.status}' and cannot be resumed")
        with self.store.transaction():
            for stage in self.store.list_stages(execution_id):
                if stage.status is StageStatus.RUNNING:
                    self._set_stage(stage, StageStatus.FAILED)
                    stage.error = FailureInfo(INTERRUPTED, "stage was running when the execution stopped",
                                              retryable=True)
                    stage.attempts = max(stage.attempts - 1, 0)
                    self.store.update_stage(stage)
                    for run in self.store.list_agent_runs(execution_id, stage.stage_id):
                        if run.status in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
                            run.status = ensure_transition(f"run {run.run_id}", run.status,
                                                           AgentRunStatus.CANCELLED, AGENT_RUN_TRANSITIONS)
                            run.completed_at = self._now()
                            # Distinguishes a crash from a deliberate cancel; the runner resumes
                            # the SDK session of an interrupted run (agent_runtime.session_manager).
                            run.error = FailureInfo(INTERRUPTED, "run was in progress when the execution stopped",
                                                    retryable=True)
                            self.store.save_agent_run(run)
                if stage.status is StageStatus.FAILED and stage.attempts < stage.max_attempts:
                    self._set_stage(stage, StageStatus.PENDING)
                    self.store.update_stage(stage)
            if execution.status is not ExecutionStatus.RUNNING:
                self._set_execution(execution, ExecutionStatus.RUNNING)
                execution.error = None
                self.store.update_execution(execution)
            self._log(execution_id, "execution.resumed", "Execution resumed")
            checkpoint = self.store.add_checkpoint(execution_id, reason="resume", state=self._snapshot(execution))
        return self.plan(execution_id, checkpoint)

    def plan(self, execution_id: str, checkpoint: Checkpoint | None = None) -> ResumePlan:
        stages = self.store.list_stages(execution_id)
        runnable = self.runnable_stages(execution_id)

        def ids(predicate: Callable[[StageState], bool]) -> tuple[str, ...]:
            return tuple(s.stage_id for s in stages if predicate(s))

        return ResumePlan(
            execution_id=execution_id,
            completed=ids(lambda s: s.status in STAGE_SATISFIED),
            runnable=runnable,
            pending=ids(lambda s: s.status is StageStatus.PENDING and s.stage_id not in runnable),
            awaiting_approval=ids(lambda s: s.status is StageStatus.AWAITING_APPROVAL),
            exhausted=ids(lambda s: s.status is StageStatus.FAILED),
            checkpoint=checkpoint or self.store.latest_checkpoint(execution_id),
        )


_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
