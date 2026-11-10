"""``agent`` and ``execution`` commands of ``python -m core.cli`` (Phase 2).

Every command goes through the same objects as library use — HarnessRegistry,
ExecutionManager, AgentRunner, the security hooks — so the CLI cannot bypass the
permission layer. Exit codes:

    0  ok / execution succeeded
    1  configuration invalid, or the execution failed / was cancelled
    2  usage error (argparse)
    3  waiting for a human (a stage is awaiting approval, or the execution is paused)
    4  invalid workflow inputs or arguments (unknown agent, bad workspace, ...)
    5  unexpected runtime error
  130  interrupted (Ctrl-C); the execution can be resumed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

from core.context import ContextManager
from core.errors import HarnessError, InputValidationError, InvalidTransitionError, NotFoundError, \
    RegistryLookupError
from core.registry import HarnessRegistry
from core.security import Redactor, SecurityPolicy
from runtime.execution import ExecutionManager
from runtime.state import ExecutionStatus
from runtime.store import open_store

EXIT_OK, EXIT_FAILED, EXIT_WAITING, EXIT_INPUT, EXIT_RUNTIME, EXIT_INTERRUPTED = 0, 1, 3, 4, 5, 130


# ---------------------------------------------------------------------- parser


def _approve_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approve", choices=["deny", "interactive"], default="deny",
                        help="how high-risk tool calls are approved: deny (default, unattended) or ask at the console")


def _db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, help="SQLite database (default: framework-config storage.sqlite.path)")


def add_parsers(sub: argparse._SubParsersAction) -> None:
    agent = sub.add_parser("agent", help="inspect and run registered agents").add_subparsers(dest="action",
                                                                                           required=True)
    agent.add_parser("list", help="registered agents with model and effective tools")
    inspect = agent.add_parser("inspect", help="everything the harness will pass to the SDK for one agent")
    inspect.add_argument("agent_id")
    inspect.add_argument("--json", action="store_true")
    run = agent.add_parser("run", help="run one agent as an ad-hoc single-stage execution")
    run.add_argument("agent_id")
    run.add_argument("--objective", required=True)
    run.add_argument("--workspace", required=True, type=Path, help="project directory the agent works on")
    run.add_argument("--output", action="append", default=[], metavar="NAME:TYPE",
                     help="expected output (type: report|json|patch|text|file); default report:report")
    run.add_argument("--constraint", action="append", default=[], metavar="TEXT")
    run.add_argument("--timeout", type=int, help="seconds; can only lower the configured limits")
    _approve_arg(run)
    _db_arg(run)

    execution = sub.add_parser("execution", help="start, inspect, resume and control workflow executions")
    actions = execution.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="start a workflow and run it until done, gated or failed")
    start.add_argument("workflow_id")
    start.add_argument("--workspace", required=True, type=Path)
    start.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                       help="workflow input; VALUE is parsed as JSON when possible")
    start.add_argument("--inputs-file", type=Path, help="JSON object of workflow inputs")
    start.add_argument("--no-run", action="store_true", help="only create the execution")
    start.add_argument("--timeout", type=int)
    _approve_arg(start)
    _db_arg(start)
    for name, help_text in (("status", "stages, agent runs, sessions and cost"),):
        cmd = actions.add_parser(name, help=help_text)
        cmd.add_argument("execution_id")
        cmd.add_argument("--json", action="store_true")
        _db_arg(cmd)
    listing = actions.add_parser("list", help="recent executions")
    listing.add_argument("--status", choices=[s.value for s in ExecutionStatus])
    listing.add_argument("--limit", type=int, default=20)
    _db_arg(listing)
    resume = actions.add_parser("resume", help="resume a failed, paused, interrupted or approved execution "
                                               "(the previous process must have stopped)")
    resume.add_argument("execution_id")
    resume.add_argument("--timeout", type=int)
    _approve_arg(resume)
    _db_arg(resume)
    cancel = actions.add_parser("cancel", help="cancel an execution (a live runner stops its agent)")
    cancel.add_argument("execution_id")
    cancel.add_argument("--reason", default="cancelled from CLI")
    _db_arg(cancel)
    approve = actions.add_parser("approve", help="approve a stage awaiting approval")
    approve.add_argument("execution_id")
    approve.add_argument("stage_id")
    approve.add_argument("--approver", required=True)
    _db_arg(approve)
    reject = actions.add_parser("reject", help="reject a stage awaiting approval")
    reject.add_argument("execution_id")
    reject.add_argument("stage_id")
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)
    _db_arg(reject)


# ---------------------------------------------------------------------- helpers


def _manager(registry: HarnessRegistry, db: Path | None) -> ExecutionManager:
    settings = registry.config.framework.storage
    if db is not None:
        from runtime.sqlite_store import SQLiteExecutionStore
        store = SQLiteExecutionStore(db, max_json_bytes=settings.max_json_bytes)
    else:
        store = open_store(settings)
    return ExecutionManager(store, registry, redact=Redactor(registry.config.security))


def _runner(registry: HarnessRegistry, manager: ExecutionManager, args: argparse.Namespace, adapter=None):
    from .runner import AgentRunner
    from .security_hooks import ConsoleApprover, DenyApprover

    approver = ConsoleApprover() if getattr(args, "approve", "deny") == "interactive" else DenyApprover()
    context = ContextManager(registry.config.framework.context,
                             max_inline_bytes=registry.config.security.max_inline_bytes)
    return AgentRunner(manager, context, adapter, approver=approver, timeout_override=getattr(args, "timeout", None))


def _parse_inputs(args: argparse.Namespace) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    if args.inputs_file:
        data = json.loads(args.inputs_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HarnessError("--inputs-file must contain a JSON object")
        inputs.update(data)
    for item in args.input:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise HarnessError(f"--input expects KEY=VALUE, got '{item}'")
        try:
            inputs[key] = json.loads(value)
        except json.JSONDecodeError:
            inputs[key] = value
    return inputs


def _outputs(specs: list[str]) -> list[dict[str, Any]] | None:
    outputs = []
    for spec in specs:
        name, _, kind = spec.partition(":")
        outputs.append({"name": name, "type": kind or "report"})
    return outputs or None


def _print_outcome(outcome, out) -> int:
    for stage in outcome.stages:
        cost = f" ${stage.cost_usd:.4f}" if stage.cost_usd is not None else ""
        line = f"  {stage.stage_id:<20} {stage.stage_status.value:<18} run={stage.run_id or '-'}{cost}"
        if stage.failure:
            line += f"  [{stage.failure.code}] {stage.failure.message}"
        print(line, file=out)
    execution, plan = outcome.execution, outcome.plan
    print(f"execution {execution.execution_id}: {execution.status.value}", file=out)
    if plan.awaiting_approval:
        print(f"awaiting approval: {', '.join(plan.awaiting_approval)} — approve with "
              f"`execution approve {execution.execution_id} <stage> --approver NAME`, then `execution resume`.",
              file=out)
        return EXIT_WAITING
    if execution.status is ExecutionStatus.SUCCEEDED:
        return EXIT_OK
    if execution.status is ExecutionStatus.PAUSED:
        return EXIT_WAITING
    return EXIT_FAILED


def _status(manager: ExecutionManager, execution_id: str, as_json: bool, out) -> int:
    store = manager.store
    execution = store.get_execution(execution_id)
    stages = store.list_stages(execution_id)
    runs = store.list_agent_runs(execution_id)
    if as_json:
        doc = {"execution_id": execution_id, "workflow_id": execution.workflow_id, "status": execution.status.value,
               "error": execution.error.to_dict() if execution.error else None,
               "stages": [{"stage_id": s.stage_id, "agent_id": s.agent_id, "status": s.status.value,
                           "attempts": s.attempts, "max_attempts": s.max_attempts,
                           "error": s.error.to_dict() if s.error else None, "approved_by": s.approved_by}
                          for s in stages],
               "runs": [{"run_id": r.run_id, "stage_id": r.stage_id, "attempt": r.attempt, "status": r.status.value,
                         "session_id": r.session_id, "cost_usd": r.cost_usd, "duration_ms": r.duration_ms,
                         "num_turns": r.num_turns, "error": r.error.to_dict() if r.error else None,
                         "outputs": [o["name"] for o in (r.result or {}).get("outputs", [])]} for r in runs],
               "total_cost_usd": round(sum(r.cost_usd or 0 for r in runs), 6)}
        print(json.dumps(doc, indent=2), file=out)
        return EXIT_OK
    print(f"{execution_id}  {execution.workflow_id} v{execution.workflow_version}  {execution.status.value}", file=out)
    if execution.error:
        print(f"  error: [{execution.error.code}] {execution.error.message}", file=out)
    for s in stages:
        err = f"  [{s.error.code}] {s.error.message}" if s.error else ""
        print(f"  stage {s.stage_id:<20} {s.status.value:<18} attempts {s.attempts}/{s.max_attempts}{err}", file=out)
    for r in runs:
        cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "-"
        print(f"  run   {r.run_id}  {r.stage_id:<14} #{r.attempt} {r.status.value:<10} session={r.session_id or '-'} "
              f"cost={cost}", file=out)
    total = sum(r.cost_usd or 0 for r in runs)
    print(f"  total cost: ${total:.4f}", file=out)
    return EXIT_OK


def _inspect(registry: HarnessRegistry, agent_id: str, as_json: bool, out) -> int:
    from core.plugin_policy import resolve_write_mode

    from .options_factory import load_instructions, permission_settings
    from .pack_resources import resolve_resources
    from .plugin_loader import resolve_plugins

    agent = registry.agents.get(agent_id)
    policy = SecurityPolicy(registry.config.security, registry.agents)
    instructions = load_instructions(agent)
    plugins = resolve_plugins(registry, agent)
    resources = resolve_resources(registry, agent, instructions)
    resolved = resolve_write_mode(registry.config, agent)
    write_mode = resolved.mode if resolved else None
    effective = policy.effective_tools(agent_id)
    rules = {s.id for s in registry.skills.by_tag("rule")}
    doc = {
        "id": agent.id, "session_agent": plugins.session_agent_name(agent.id),
        "file": agent.rel_path, "source": agent.source, "plugin": agent.plugin, "role": agent.role,
        "description": agent.description, "model": agent.model,
        "declared_tools": list(agent.tools) if agent.tools is not None else None,
        "effective_tools": list(effective) if effective is not None else None,
        "denied_tools": sorted(policy.denied_tools(agent_id)),
        "write_mode": write_mode,
        "write_mode_source": resolved.source if resolved else None,
        "skills": list(plugins.skill_names),
        "rules": [s for s in agent.skills if s in rules],
        "plugins": [p.path.as_posix() for p in plugins.plugins],
        "plugin_hooks": "disabled (harness enforces agent_policy)" if plugins.hooks_disabled else "loaded",
        "preloaded_by_harness": {"skills": [name for name, _ in plugins.preloaded],
                                 "bytes": sum(path.stat().st_size for _, path in plugins.preloaded)},
        "templates": {r.id: r.path.as_posix() for r in resources.templates},
        "references": {r.id: r.path.as_posix() for r in resources.references},
        "read_roots": [p.as_posix() for p in dict.fromkeys([*plugins.read_roots, *resources.read_roots])],
        "instructions": {"lines": instructions.prompt.count("\n") + 1, "chars": len(instructions.prompt)},
        "permission_deny_rules": permission_settings(registry.config.security)["permissions"]["deny"],
        "command_rules": {"deny": [r.id for r in registry.config.security.denied_commands],
                          "require_approval": [r.id for r in registry.config.security.approval_commands]},
        "context_needs": {"scopes": list(agent.context_needs.scopes), "tags": list(agent.context_needs.tags),
                          "token_budget": agent.context_needs.token_budget},
    }
    if as_json:
        print(json.dumps(doc, indent=2), file=out)
    else:
        for key, value in doc.items():
            print(f"{key:<22} {value if not isinstance(value, (list, dict)) else json.dumps(value)}", file=out)
    return EXIT_OK


# ---------------------------------------------------------------------- dispatch


def run_command(args: argparse.Namespace, registry: HarnessRegistry, *, out=None, adapter=None) -> int:
    out = out or sys.stdout
    err = sys.stderr
    try:
        if args.command == "agent" and args.action == "list":
            policy = SecurityPolicy(registry.config.security, registry.agents)
            for agent in registry.agents:
                tools = policy.effective_tools(agent.id)
                print(f"{agent.id:<24} {agent.model or '-':<7} tools={','.join(tools) if tools is not None else '*'} "
                      f"skills={','.join(s.sdk_name for s in registry.skills_for(agent.id))}", file=out)
            return EXIT_OK
        if args.command == "agent" and args.action == "inspect":
            return _inspect(registry, args.agent_id, args.json, out)

        manager = _manager(registry, getattr(args, "db", None))
        try:
            return _dispatch_execution(args, registry, manager, out, adapter)
        finally:
            manager.store.close()
    except (InputValidationError,) as exc:
        print(f"INVALID INPUTS: {exc}", file=err)
        return EXIT_INPUT
    except (RegistryLookupError, NotFoundError) as exc:
        print(f"ERROR: {exc}", file=err)
        return EXIT_INPUT
    except InvalidTransitionError as exc:
        print(f"ERROR: {exc}", file=err)
        return EXIT_FAILED
    except HarnessError as exc:
        print(f"ERROR: {exc}", file=err)
        return EXIT_INPUT
    except KeyboardInterrupt:
        print("interrupted — resume with `execution resume <id>`", file=err)
        return EXIT_INTERRUPTED


def _orchestrate_run(args: argparse.Namespace, registry: HarnessRegistry, manager: ExecutionManager, out,
                     adapter, run_async: Callable[[Any], Any]) -> int:
    """Plan the route from the canonical policy and run it through the existing workflow engine."""
    from core.orchestration import EXTENSION_KEY, OrchestrationError, plan_route, workflow_document

    policy = registry.config.orchestration
    if policy is None:
        raise HarnessError("no orchestration policy configured (framework-config configs.orchestration)")
    try:
        plan = plan_route(policy, args.task, category=args.category, include=args.include, exclude=args.exclude)
    except OrchestrationError as exc:
        hint = f"; choose with --category: {', '.join(exc.candidates)}" if exc.candidates else ""
        print(f"CANNOT ROUTE [{exc.code}]: {exc}{hint}", file=sys.stderr)
        return EXIT_INPUT
    runner = _runner(registry, manager, args, adapter)
    definition = workflow_document(policy, plan, args.task)
    execution = runner.start_definition(definition, workspace=args.workspace, metadata={
        EXTENSION_KEY: {"orchestrator": policy.orchestrator_id, "policy_version": policy.version,
                        "category": plan.category, "agents": list(plan.agents), "selection": plan.selection}})
    print(f"started {execution.execution_id} ({execution.workflow_id}): {' -> '.join(plan.agents)}", file=out)
    code = _print_outcome(run_async(runner.run_workflow(execution.execution_id)), out)
    not_run = [s.stage_id for s in manager.store.list_stages(execution.execution_id)
               if s.status.value in ("pending", "ready")]
    if not_run and code != EXIT_WAITING:
        print(f"not run: {', '.join(not_run)} (an earlier step did not succeed)", file=out)
    return code


def _dispatch_execution(args: argparse.Namespace, registry: HarnessRegistry, manager: ExecutionManager, out,
                        adapter) -> int:
    run_async: Callable[[Any], Any] = asyncio.run
    if args.command == "orchestrate" and args.action == "run":
        return _orchestrate_run(args, registry, manager, out, adapter, run_async)
    if args.command == "agent" and args.action == "run":
        runner = _runner(registry, manager, args, adapter)
        execution = runner.start_agent(args.agent_id, args.objective, workspace=args.workspace,
                                       expected_outputs=_outputs(args.output), constraints=args.constraint)
        print(f"started {execution.execution_id} ({execution.workflow_id})", file=out)
        return _print_outcome(run_async(runner.run_workflow(execution.execution_id)), out)

    action = args.action
    if action == "start":
        runner = _runner(registry, manager, args, adapter)
        execution = runner.start(args.workflow_id, _parse_inputs(args), workspace=args.workspace)
        print(f"started {execution.execution_id} ({execution.workflow_id} v{execution.workflow_version})", file=out)
        if args.no_run:
            return EXIT_OK
        return _print_outcome(run_async(runner.run_workflow(execution.execution_id)), out)
    if action == "status":
        return _status(manager, args.execution_id, args.json, out)
    if action == "list":
        for e in manager.store.list_executions(status=args.status, limit=args.limit):
            print(f"{e.execution_id}  {e.workflow_id:<28} {e.status.value:<10} {e.created_at}", file=out)
        return EXIT_OK
    if action == "resume":
        runner = _runner(registry, manager, args, adapter)
        return _print_outcome(run_async(runner.resume_workflow(args.execution_id)), out)
    if action == "cancel":
        execution = manager.cancel(args.execution_id, args.reason)
        print(f"{execution.execution_id}: {execution.status.value}", file=out)
        return EXIT_OK
    if action == "approve":
        stage = manager.approve_stage(args.execution_id, args.stage_id, args.approver)
        print(f"stage {stage.stage_id}: {stage.status.value} (approved by {args.approver})", file=out)
        return EXIT_OK
    if action == "reject":
        stage = manager.reject_stage(args.execution_id, args.stage_id, args.reviewer, args.reason)
        print(f"stage {stage.stage_id}: {stage.status.value} (rejected by {args.reviewer})", file=out)
        return EXIT_OK
    raise HarnessError(f"unknown command {args.command} {action}")   # pragma: no cover
