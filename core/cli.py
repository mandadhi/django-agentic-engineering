"""Command line: validate the harness configuration, inspect the registry, run agents.

    python -m core.cli [--profile full|pack] validate [--root PATH]
    python -m core.cli list {agents,skills,workflows} [--root PATH]
    python -m core.cli orchestrate plan "<task>" [--category ID] [--with AGENT] [--without AGENT] [--json]
    python -m core.cli orchestrate run "<task>" --workspace PATH [--category ID] [--approve deny|interactive]
    python -m core.cli agent {list,inspect,run} ...
    python -m core.cli execution {start,status,list,resume,cancel,approve,reject} ...

Profiles: ``full`` (default) requires every registered plugin; ``pack`` explicitly excludes the optional
parent-repository integrations and reports what it excluded (also: DAE_HARNESS_PROFILE=pack).

Exit codes: 0 ok, 1 validation or execution failed, 2 usage error, 3 waiting for a
human (approval / paused), 4 invalid inputs or arguments, 5 unexpected runtime error,
130 interrupted. See agent_runtime/cli.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import HARNESS_HOME
from .config import DEFAULT_FRAMEWORK_FILE, PROFILES
from .errors import HarnessError
from .log import configure_logging
from .registry import HarnessRegistry
from .security import Redactor


def main(argv: list[str] | None = None, *, adapter=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=HARNESS_HOME, help="harness root (default: this project)")
    parser.add_argument("--framework-file", default=DEFAULT_FRAMEWORK_FILE)
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--profile", choices=PROFILES, default=None,
                        help="full (default): every registered plugin is required; pack: exclude the optional "
                             "parent-repository integrations explicitly (env: DAE_HARNESS_PROFILE)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="load and validate every config and workflow")
    listing = sub.add_parser("list", help="list registered items")
    listing.add_argument("what", choices=["agents", "skills", "workflows"])
    orchestrate = sub.add_parser("orchestrate", help="route a task through the canonical orchestration policy")
    orch_actions = orchestrate.add_subparsers(dest="action", required=True)
    for name, help_text in (("plan", "show the route the policy selects for a task (runs nothing)"),
                            ("run", "plan the route and run it as one workflow execution (needs claude-agent-sdk)")):
        cmd = orch_actions.add_parser(name, help=help_text)
        cmd.add_argument("task", help="what to do, in plain language")
        cmd.add_argument("--category", help="skip classification and use this policy category")
        cmd.add_argument("--with", dest="include", action="append", default=[], metavar="AGENT",
                         help="include this optional step of the route")
        cmd.add_argument("--without", dest="exclude", action="append", default=[], metavar="AGENT",
                         help="leave out this optional step of the route")
        if name == "plan":
            cmd.add_argument("--json", action="store_true")
        else:
            cmd.add_argument("--workspace", required=True, type=Path, help="project directory the agents work on")
            cmd.add_argument("--timeout", type=int, help="seconds per stage; can only lower the configured limits")
            cmd.add_argument("--approve", choices=["deny", "interactive"], default="deny",
                             help="how high-risk tool calls are approved: deny (default, unattended) or ask")
            cmd.add_argument("--db", type=Path, help="SQLite database (default: framework-config storage.sqlite.path)")
    runtime_cli = None
    try:
        from agent_runtime import cli as runtime_cli   # needs claude-agent-sdk
    except ImportError as exc:                          # validate/list keep working without the SDK
        sdk_error = exc
    else:
        runtime_cli.add_parsers(sub)
    args = parser.parse_args(argv)

    configure_logging(args.log_level, "text")
    try:
        registry = HarnessRegistry.load(args.root, args.framework_file, profile=args.profile)
    except HarnessError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    if args.command == "orchestrate" and args.action == "plan":
        return orchestrate_plan(registry, args, sys.stdout)
    if args.command in ("agent", "execution", "orchestrate"):
        if runtime_cli is None:   # pragma: no cover - parse would already have failed
            print(f"ERROR: claude-agent-sdk is not installed: {sdk_error}", file=sys.stderr)
            return 5
        configure_logging(args.log_level, "text", redact=Redactor(registry.config.security))
        return runtime_cli.run_command(args, registry, adapter=adapter)

    if args.command == "validate":
        print(f"OK: {len(registry.agents)} agents, {len(registry.skills)} skills, "
              f"{len(registry.workflows)} workflows; all references resolve (profile: {registry.config.profile}).")
        if registry.config.orchestration is not None:
            policy = registry.config.orchestration
            print(f"orchestration policy {policy.version}: {len(policy.categories)} categories, "
                  f"{len(policy.specialists)} specialists, max {policy.max_delegations} delegations.")
        for plugin in registry.config.excluded_plugins:
            print(f"excluded optional plugin '{plugin.id}' ({plugin.rel_path})"
                  + (f": {plugin.description}" if plugin.description else ""))
        if registry.config.excluded:
            print("excluded by profile: " + ", ".join(registry.config.excluded))
    elif args.what == "agents":
        for agent in registry.agents:
            print(f"{agent.id:<24} {agent.source:<7} {agent.rel_path}  skills={','.join(agent.skills)}")
    elif args.what == "skills":
        for skill in registry.skills:
            print(f"{skill.id:<26} {skill.source:<7} {skill.rel_path}")
    else:
        for wf in registry.workflows:
            print(f"{wf.id:<20} v{wf.version} [{wf.status}] stages: {' -> '.join(wf.topological_order())}")
    return 0


def orchestrate_plan(registry: HarnessRegistry, args: argparse.Namespace, out) -> int:
    """``orchestrate plan``: the route, each step's registered skills and write mode. Runs nothing."""
    from .orchestration import OrchestrationError, plan_route, plan_summary
    from .plugin_policy import resolve_write_mode

    policy = registry.config.orchestration
    if policy is None:
        print("ERROR: no orchestration policy configured (framework-config configs.orchestration)", file=sys.stderr)
        return 4
    try:
        plan = plan_route(policy, args.task, category=args.category, include=args.include, exclude=args.exclude)
    except OrchestrationError as exc:
        print(f"CANNOT ROUTE [{exc.code}]: {exc}", file=sys.stderr)
        if exc.candidates:
            print("choose with --category: " + ", ".join(exc.candidates), file=sys.stderr)
        return 4
    doc = plan_summary(plan)
    for step in doc["steps"]:
        agent = registry.agents.get(step["agent"])     # resolves through the registry
        resolved = resolve_write_mode(registry.config, agent)
        step["write_mode"] = resolved.mode if resolved else None
        step["skills"] = [s.sdk_name for s in registry.skills_for(agent.id)]
    if args.json:
        print(json.dumps(doc, indent=2), file=out)
        return 0
    print(f"category: {doc['category']} ({doc['title']}); selection: {doc['selection']}; "
          f"policy {doc['policy_version']}", file=out)
    for number, step in enumerate(doc["steps"], start=1):
        after = ", ".join(step["depends_on"]) or "-"
        print(f"  {number}. {step['agent']:<28} after: {after:<28} write mode: {step['write_mode']}", file=out)
        print(f"     {step['purpose']} [{step['reason']}]", file=out)
    if doc["supplementary_skills"]:
        print("supplementary skills: " + ", ".join(doc["supplementary_skills"]), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
