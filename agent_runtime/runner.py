"""AgentRunner: executes workflow stages through the Agent SDK, one query per stage.

Division of labour:

* the **workflow engine** (:class:`runtime.execution.ExecutionManager` over the
  workflow DAG) decides what may run: dependencies, attempts, approval gates, retries,
  state transitions, checkpoints;
* the **Agent SDK** runs the agent: model loop, tools, skills, hooks, sessions;
* this runner connects them for one stage: resolve the agent from its Markdown,
  resolve plugins/skills, assemble scoped context, build the AgentTask, build SDK
  options with the security layer, run one ``query()`` under a timeout, process the
  result, and hand it back to the ExecutionManager.

:meth:`AgentRunner.run_workflow` is a thin loop over
``ExecutionManager.runnable_stages`` — it never decides order itself, and it retries a
stage only through :meth:`ExecutionManager.retry_stage` (which enforces
``max_attempts`` and retryability). Stages run sequentially.
"""

from __future__ import annotations

import asyncio
import functools
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core import HARNESS_HOME
from core.config import AgentSpec
from core.context import ContextItem, ContextManager, ContextScope
from core.errors import ContractValidationError, HarnessError
from core.log import get_logger
from core.plugin_policy import AgentWriteMode, PluginPolicyError, load_agent_policy, resolve_write_mode
from core.security import Redactor, SecurityPolicy
from core.workflows import StageDefinition
from runtime.execution import ExecutionManager, ResumePlan
from runtime.models import AgentRun, Execution, FailureInfo
from runtime.state import ExecutionStatus, StageStatus

from .exceptions import AgentDefinitionError, AgentRuntimeError, AgentTimeoutError, PluginResolutionError, \
    RunCancelledError, SecurityViolationError
from .environment import forwarded_names
from .options_factory import RunPaths, build_options, harness_environment, load_instructions, with_preloaded_skills
from .output_schema import schema_fingerprint, structured_output_schema
from .pack_resources import resolve_resources
from .plugin_loader import ResolvedPlugins, check_agent_collisions, resolve_plugins, verify_loaded
from .prompt import render_task_prompt
from .result_processor import ProcessedResult, ResultProcessor, SessionCollector
from .sdk_adapter import ClaudeSDKAdapter, SDKAdapter
from .security_hooks import HARNESS_TOOLS, Approver, DenyApprover, HookTripwire, ToolGuard
from .sandbox import SANDBOXED, ShellIsolation, shell_isolation as detect_shell_isolation
from .session_manager import RESUMED_FROM, find_resumable_run
from .workspace import claude_config_dir, protected_roots, validate_workspace

log = get_logger(__name__)

#: Execution metadata key for the workspace (the project directory agents work on).
WORKSPACE_KEY = "dae.workspace"


@dataclass(frozen=True)
class StageOutcome:
    execution_id: str
    stage_id: str
    run_id: str | None
    stage_status: StageStatus
    result: Mapping[str, Any] | None
    failure: FailureInfo | None
    session_id: str | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class WorkflowOutcome:
    execution: Execution
    plan: ResumePlan
    stages: tuple[StageOutcome, ...]

    @property
    def awaiting_approval(self) -> bool:
        return bool(self.plan.awaiting_approval)


def task_context_key(execution_id: str, stage_id: str) -> str:
    """``task_id`` to give task-scoped ContextItems meant for one stage of one execution."""
    return f"{execution_id}:{stage_id}"


def adhoc_definition(agent_id: str, objective: str, *, expected_outputs: Sequence[Mapping[str, Any]] | None = None,
                     constraints: Sequence[str] = (), timeout_seconds: int | None = None) -> dict[str, Any]:
    """A one-stage WorkflowDefinition for ``agent run`` (validated like any workflow)."""
    stage: dict[str, Any] = {
        "id": "run", "agent_id": agent_id, "objective": objective, "max_attempts": 1,
        "expected_outputs": list(expected_outputs or [{"name": "report", "type": "report"}]),
    }
    if constraints:
        stage["constraints"] = [{"id": f"c{i}", "description": text} for i, text in enumerate(constraints, start=1)]
    if timeout_seconds:
        stage["timeout_seconds"] = timeout_seconds
    return {"schema_version": "1.0", "kind": "WorkflowDefinition", "id": f"adhoc.{agent_id}", "version": "0.0.0",
            "name": f"Ad-hoc run of {agent_id}", "status": "draft", "stages": [stage]}


class AgentRunner:
    def __init__(self, manager: ExecutionManager, context: ContextManager, adapter: SDKAdapter | None = None, *,
                 approver: Approver | None = None, artifacts_root: Path | None = None,
                 timeout_override: float | None = None, cancel_poll_seconds: float = 1.0,
                 shell_isolation: ShellIsolation | None = None) -> None:
        self.manager = manager
        self.registry = manager.registry
        self.context = context
        self.adapter: SDKAdapter = adapter or ClaudeSDKAdapter()
        self.approver: Approver = approver or DenyApprover()
        config = self.registry.config
        self.runtime = config.framework.agent_runtime
        self.security = config.security
        self.policy = SecurityPolicy(config.security, self.registry.agents)
        self.redactor = Redactor(config.security)
        self.processor = ResultProcessor(self.registry.contracts, self.redactor,
                                         max_json_bytes=config.framework.storage.max_json_bytes)
        self.artifacts_root = artifacts_root or config.framework.artifacts_dir
        # Trusted infrastructure no workspace may equal, contain or lie inside (SEC-04).
        self.protected_roots = protected_roots(config, extra=(self.artifacts_root, getattr(manager.store, "path", None)))
        # None: detected for this platform at every stage (agent_runtime.sandbox). Tests pass one explicitly.
        self.shell_isolation = shell_isolation
        self.timeout_override = timeout_override
        self.cancel_poll_seconds = cancel_poll_seconds
        # A transport projection of the AgentResult contract; the processor enforces the full contract.
        self.output_schema = structured_output_schema(self.registry.contracts, "agent-result")[0] \
            if self.runtime.structured_output else None

    # ------------------------------------------------------------------ starting

    def start(self, workflow_id: str, inputs: Mapping[str, Any] | None, *, workspace: str | Path) -> Execution:
        """Start a registered workflow on ``workspace`` (inputs validated by the ExecutionManager)."""
        return self.manager.start_execution(workflow_id, inputs, {WORKSPACE_KEY: str(self._workspace(workspace))})

    def start_agent(self, agent_id: str, objective: str, *, workspace: str | Path,
                    expected_outputs: Sequence[Mapping[str, Any]] | None = None, constraints: Sequence[str] = (),
                    timeout_seconds: int | None = None) -> Execution:
        """Start an ad-hoc single-stage execution of one agent (same execution path as a workflow)."""
        self.registry.agents.get(agent_id)       # unknown agent → RegistryLookupError with suggestions
        definition = adhoc_definition(agent_id, objective, expected_outputs=expected_outputs,
                                      constraints=constraints, timeout_seconds=timeout_seconds)
        return self.manager.start_execution(definition["id"], {}, {WORKSPACE_KEY: str(self._workspace(workspace))},
                                            definition=definition)

    def workspace_of(self, execution: Execution) -> Path:
        value = execution.metadata.get(WORKSPACE_KEY)
        if not value:
            raise HarnessError(f"execution {execution.execution_id} has no workspace (metadata '{WORKSPACE_KEY}')")
        return self._workspace(value)                # re-validated on every stage and resume

    def _workspace(self, value: str | Path) -> Path:
        """Canonical workspace; refused when it overlaps the trusted harness (SEC-04, fail closed)."""
        return validate_workspace(value, self.protected_roots)

    # ------------------------------------------------------------------ workflow loop

    async def run_workflow(self, execution_id: str) -> WorkflowOutcome:
        """Run every stage the workflow engine says is runnable, until done, blocked or failed."""
        outcomes: list[StageOutcome] = []
        store = self.manager.store
        while store.get_execution(execution_id).status is ExecutionStatus.RUNNING:
            runnable = self.manager.runnable_stages(execution_id)
            if not runnable:
                break                       # finished, awaiting approval, or waiting on a failed dependency
            outcome = await self.run_stage(execution_id, runnable[0])
            outcomes.append(outcome)
            if outcome.stage_status is StageStatus.FAILED:
                stage = store.get_stage(execution_id, outcome.stage_id)
                if stage.error and stage.error.retryable and stage.attempts < stage.max_attempts:
                    self.manager.retry_stage(execution_id, outcome.stage_id)
                    continue
                break
        return WorkflowOutcome(store.get_execution(execution_id), self.manager.plan(execution_id), tuple(outcomes))

    async def resume_workflow(self, execution_id: str) -> WorkflowOutcome:
        """``ExecutionManager.resume`` (marks interrupted work) then continue; interrupted SDK sessions resume."""
        self.manager.resume(execution_id)
        return await self.run_workflow(execution_id)

    # ------------------------------------------------------------------ one stage

    def effective_timeout(self, stage: StageDefinition) -> float:
        limits = [stage.timeout_seconds, self.runtime.max_agent_seconds, self.timeout_override]
        return float(min(v for v in limits if v))

    async def run_stage(self, execution_id: str, stage_id: str) -> StageOutcome:
        store = self.manager.store
        execution = store.get_execution(execution_id)
        definition = self.manager.workflow_for(execution).stage(stage_id)
        agent = self.registry.agents.get(definition.agent_id)
        workspace = self.workspace_of(execution)
        resumable = find_resumable_run(store, execution_id, stage_id)
        self.manager.start_stage(execution_id, stage_id)          # dependencies + attempts enforced here

        try:
            instructions = load_instructions(agent)
            plugins = resolve_plugins(self.registry, agent)
            session_agent = check_agent_collisions(plugins, agent.id)         # dae-<id>; no plugin may define it
            resources = resolve_resources(self.registry, agent, instructions)   # templates and references
            instructions = resources.expand(instructions)                       # ${CLAUDE_PLUGIN_ROOT}
            instructions = with_preloaded_skills(instructions, plugins)        # registered SKILL.md, once each
            write_mode = self._write_mode(agent)
            key = task_context_key(execution_id, stage_id)
            self._add_upstream_context(execution_id, definition, key)
            for item in resources.context_items(key):                           # references only, never inline
                self.context.add(item, replace=True)
            bundle = self.context.assemble_for_stage(agent, definition, key)
            run = self.manager.create_task(execution_id, stage_id, context=bundle, estimator=self.context.estimator)
        except HarnessError as exc:
            code = exc.code if isinstance(exc, AgentRuntimeError) else (
                "task.invalid" if isinstance(exc, ContractValidationError) else "runtime.prepare_failed")
            failure = FailureInfo(code, self.redactor.redact(str(exc).splitlines()[0])[:1000],
                                  getattr(exc, "retryable", False))
            stage = self.manager.fail_stage(execution_id, stage_id, failure)
            return StageOutcome(execution_id, stage_id, None, stage.status, None, failure)

        artifact_dir = (self.artifacts_root / execution_id / stage_id / run.run_id).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        resume_session = self._resume_session(resumable, run, workspace)
        audit = functools.partial(self._audit, execution_id, stage_id, run.run_id)
        isolation = self.shell_isolation or detect_shell_isolation(self.security.shell)
        env = harness_environment(self.security)
        guard = ToolGuard(agent_id=agent.id, policy=self.policy, security=self.security, redactor=self.redactor,
                          workspace=workspace, artifact_dir=artifact_dir,
                          extra_read_roots=dict.fromkeys([*plugins.read_roots, *resources.read_roots]),
                          approver=self.approver, audit=audit, execution_id=execution_id, stage_id=stage_id,
                          write_mode=write_mode, session_agent=session_agent, protected_roots=self.protected_roots,
                          shell_mode=isolation.mode, approval_ttl_seconds=self.runtime.hook_timeout_seconds)
        # The logical id stays the agent's identity everywhere (registry, task, result, audit); the session
        # name is recorded next to it so a transcript can be traced back to the harness definition.
        self.manager.update_run_metadata(run.run_id, sdk_metadata={
            "session_agent": session_agent, "shell": isolation.to_metadata(),
            "environment": {"forwarded": forwarded_names(env)}})
        audit("session.shell", f"Bash: {isolation.mode} ({isolation.reason})",
              level="INFO" if isolation.mode == SANDBOXED else "WARNING", mode=isolation.mode,
              write_mode=write_mode.mode if write_mode else None,
              write_mode_source=write_mode.source if write_mode else None)
        audit("session.environment", "operator environment filtered to the allow-list",
              forwarded=forwarded_names(env), blanked=sum(1 for v in env.values() if not v))
        tripwire = HookTripwire(guard)          # every session: detects calls that bypassed the guard (SEC-07)
        options = build_options(agent=agent, instructions=instructions, policy=self.policy, security=self.security,
                                plugins=plugins, paths=RunPaths(workspace, artifact_dir), runtime=self.runtime,
                                hooks=guard.hook_matchers(self.runtime.hook_timeout_seconds),
                                output_schema=self.output_schema, resume_session=resume_session,
                                stderr=self._stderr, isolation=isolation, env=env,
                                protected_read_deny=self._protected_read_deny(),
                                # SEC-05: the OS sandbox also keeps a read-only agent's commands out of the workspace
                                write_deny=[workspace] if write_mode and write_mode.mode == "read-only" else ())
        prompt = render_task_prompt(run.task, workspace=workspace, artifact_dir=artifact_dir,
                                    resumed=resume_session is not None)
        self.manager.start_agent_run(run.run_id)
        collector = SessionCollector()
        timeout = self.effective_timeout(definition)
        try:
            await self._execute(prompt, options, collector, run, agent.id, plugins, timeout, tripwire)
        except asyncio.CancelledError:                     # Ctrl-C / task cancellation: record, then propagate
            self._finish(run, collector, workspace, artifact_dir, RunCancelledError("agent execution was cancelled"))
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run and the stage
            return self._finish(run, collector, workspace, artifact_dir, exc)
        return self._finish(run, collector, workspace, artifact_dir, None)

    # ------------------------------------------------------------------ helpers

    def _audit(self, execution_id: str, stage_id: str, run_id: str, event: str, message: str, *,
               level: str = "INFO", **data: Any) -> None:
        self.manager.audit(execution_id, event, message, level=level, stage_id=stage_id, run_id=run_id, **data)

    def _protected_read_deny(self) -> list[Path]:
        """Harness internals a sandboxed command must not read (native Read/Edit deny rules)."""
        config = self.registry.config
        paths: list[Path | None] = [config.framework.root / "config", HARNESS_HOME / "config",
                                    config.framework.storage.sqlite_path, getattr(self.manager.store, "path", None),
                                    claude_config_dir()]
        for plugin in config.plugins:      # plugin manifests, hooks and security definitions
            paths += [plugin.path / ".claude-plugin", plugin.path / "hooks", plugin.agent_policy_path]
        return list(dict.fromkeys(Path(p).resolve() for p in paths if p is not None))

    def _write_mode(self, agent: AgentSpec) -> AgentWriteMode | None:
        """The write mode the guard enforces: security-config ``write_mode`` and/or the plugin's
        ``agent_policy``, the stricter of the two (None: harness defaults)."""
        try:
            return resolve_write_mode(self.registry.config, agent, loader=load_agent_policy)
        except PluginPolicyError as exc:
            raise PluginResolutionError(f"agent '{agent.id}': {exc}") from exc

    def _stderr(self, line: str) -> None:
        log.debug("sdk.stderr", extra={"line": self.redactor.redact(line)[:500]})

    def _add_upstream_context(self, execution_id: str, definition: StageDefinition, key: str) -> None:
        """Outputs of the stage's *declared* dependencies become task context (references only)."""
        depends = set(definition.depends_on)
        for artifact in self.manager.store.list_artifacts(execution_id):
            if artifact.stage_id not in depends:
                continue
            self.context.add(ContextItem(
                id=f"upstream.{artifact.stage_id}.{artifact.name or artifact.artifact_id}", scope=ContextScope.TASK,
                uri=artifact.uri, task_id=key, priority=80,
                description=f"output of stage '{artifact.stage_id}'" + (f" ({artifact.media_type})"
                                                                        if artifact.media_type else ""),
                token_estimate=(artifact.size_bytes or 0) // 4), replace=True)

    def _resume_session(self, resumable: AgentRun | None, run: AgentRun, workspace: Path) -> str | None:
        if resumable is None or not resumable.session_id:
            return None
        if not self.adapter.session_exists(resumable.session_id, str(workspace)):
            self._audit(run.execution_id, run.stage_id, run.run_id, "session.unavailable",
                        f"session of interrupted run {resumable.run_id} cannot be resumed; starting a fresh session",
                        level="WARNING", session_id=resumable.session_id)
            return None
        self.manager.update_run_metadata(run.run_id, sdk_metadata={RESUMED_FROM: resumable.run_id,
                                                                   "resumed_session": resumable.session_id})
        return resumable.session_id

    async def _execute(self, prompt: str, options: Any, collector: SessionCollector, run: AgentRun, agent_id: str,
                       plugins: ResolvedPlugins, timeout: float, tripwire: HookTripwire | None = None) -> None:
        """Consume one SDK query, enforcing the timeout and watching for ``execution cancel``."""
        consumer = asyncio.ensure_future(self._drain(prompt, options, collector, run, agent_id, plugins, tripwire))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise AgentTimeoutError(timeout)
                done, _ = await asyncio.wait({consumer}, timeout=min(self.cancel_poll_seconds, remaining))
                if done:
                    consumer.result()        # re-raises the consumer's exception
                    return
                if self.manager.store.get_execution(run.execution_id).status is ExecutionStatus.CANCELLED:
                    raise RunCancelledError("the execution was cancelled while the agent was running")
        finally:
            if not consumer.done():
                consumer.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await consumer

    async def _drain(self, prompt: str, options: Any, collector: SessionCollector, run: AgentRun, agent_id: str,
                     plugins: ResolvedPlugins, tripwire: HookTripwire | None = None) -> None:
        stream = self.adapter.stream(prompt, options)
        try:
            async for message in stream:
                if tripwire is not None:
                    tripwire.observe(message)
                if collector.observe(message):
                    self._on_init(collector, run, agent_id, plugins, options)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()

    def _on_init(self, collector: SessionCollector, run: AgentRun, agent_id: str, plugins: ResolvedPlugins,
                 options: Any) -> None:
        """The session started: persist its id first (resumability), then verify what was loaded."""
        init = collector.init or {}
        self.manager.update_run_metadata(run.run_id, session_id=collector.session_id,
                                         sdk_metadata=collector.sdk_metadata())
        self.manager.checkpoint(run.execution_id, reason="agent_session_started", stage_id=run.stage_id,
                                extra={"run_id": run.run_id, "session_id": collector.session_id})
        verify_loaded(init, plugins)
        session_agent = plugins.session_agent_name(agent_id)
        if "agents" in init and session_agent not in (init.get("agents") or []):
            raise AgentDefinitionError(f"the SDK session did not register agent '{session_agent}'")
        if isinstance(options.tools, list) and "tools" in init:
            available = set(init.get("tools") or [])
            extra = sorted(available - set(options.tools) - HARNESS_TOOLS)
            if extra:
                raise SecurityViolationError(f"the SDK session exposes tools outside the agent's policy: "
                                             f"{', '.join(extra)}")
            # The reverse is not a security problem but must not go unnoticed: e.g. on Windows
            # the CLI only offers Bash when it finds Git Bash (CLAUDE_CODE_GIT_BASH_PATH).
            missing = [tool for tool in options.tools if tool not in available]
            if missing:
                self.manager.update_run_metadata(run.run_id, sdk_metadata={"unavailable_tools": missing})
                self._audit(run.execution_id, run.stage_id, run.run_id, "session.tools_unavailable",
                            f"declared tools not available in the SDK session: {', '.join(missing)}",
                            level="WARNING", tools=missing)

    def _finish(self, run: AgentRun, collector: SessionCollector, workspace: Path, artifact_dir: Path,
                exc: BaseException | None) -> StageOutcome:
        processed = self.processor.process(run.task, collector, workspace=workspace, artifact_dir=artifact_dir,
                                           exc=exc)
        result_msg = collector.result
        metadata = collector.sdk_metadata()
        if self.output_schema is not None:
            metadata["output_schema"] = schema_fingerprint(self.output_schema)
        # Whatever the SDK reported — also for failures, timeouts and cancellations (partial usage).
        metrics = collector.metrics()
        source = metrics.pop("metrics_source")
        if source is not None:
            metadata["metrics_source"] = source
        self.manager.update_run_metadata(run.run_id, session_id=collector.session_id, sdk_metadata=metadata,
                                         **metrics)
        # Native permission rules refuse a call *before* PreToolUse runs (observed with CLI 2.1.317),
        # so those refusals reach the audit log only through the result's permission_denials.
        for denial in (result_msg.permission_denials or []) if result_msg else []:
            tool = str(denial.get("tool_name", "?")) if isinstance(denial, Mapping) else "?"
            tool_input = denial.get("tool_input") if isinstance(denial, Mapping) else None
            summary = self.redactor.redact(json.dumps(tool_input, default=str))[:300] if tool_input else ""
            self._audit(run.execution_id, run.stage_id, run.run_id, "tool.denied",
                        f"{tool}: refused by native permission rules", level="WARNING", tool=tool,
                        summary=summary, source="permissions")
        if processed.redacted_patterns:
            self._audit(run.execution_id, run.stage_id, run.run_id, "result.redacted",
                        "secret patterns were masked in the agent's result", level="WARNING",
                        patterns=list(processed.redacted_patterns))
        try:
            self.manager.record_result(run.run_id, processed.result)
        except ContractValidationError as err:          # defensive: the processor already validated
            processed = self.processor.failure_result(
                run.task, FailureInfo("result.invalid", str(err).splitlines()[0][:500], True), collector)
            self.manager.record_result(run.run_id, processed.result)
        return self._transition(run, processed, collector)

    def _transition(self, run: AgentRun, processed: ProcessedResult, collector: SessionCollector) -> StageOutcome:
        eid, sid = run.execution_id, run.stage_id
        store = self.manager.store
        status = processed.result["status"]
        execution = store.get_execution(eid)
        stage = store.get_stage(eid, sid)
        failure = processed.failure
        if execution.status is ExecutionStatus.CANCELLED or stage.status is StageStatus.CANCELLED:
            pass                                          # `execution cancel` already settled the stage
        elif status in ("succeeded", "partial"):
            stage = self.manager.complete_stage(eid, sid)   # awaiting_approval when the stage is gated
        else:
            failure = failure or FailureInfo("agent.failed", f"agent returned status '{status}'", False)
            stage = self.manager.fail_stage(eid, sid, failure)
        cost = collector.result.total_cost_usd if collector.result else None
        return StageOutcome(eid, sid, run.run_id, stage.status, processed.result, failure, collector.session_id, cost)
