"""Command line: validate the harness configuration, inspect the registry, run agents.

    python -m core.cli validate [--root PATH]
    python -m core.cli list {agents,skills,workflows} [--root PATH]
    python -m core.cli agent {list,inspect,run} ...
    python -m core.cli execution {start,status,list,resume,cancel,approve,reject} ...

Exit codes: 0 ok, 1 validation or execution failed, 2 usage error, 3 waiting for a
human (approval / paused), 4 invalid inputs or arguments, 5 unexpected runtime error,
130 interrupted. See agent_runtime/cli.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import HARNESS_HOME
from .config import DEFAULT_FRAMEWORK_FILE
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
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="load and validate every config and workflow")
    listing = sub.add_parser("list", help="list registered items")
    listing.add_argument("what", choices=["agents", "skills", "workflows"])
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
        registry = HarnessRegistry.load(args.root, args.framework_file)
    except HarnessError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    if args.command in ("agent", "execution"):
        if runtime_cli is None:   # pragma: no cover - parse would already have failed
            print(f"ERROR: claude-agent-sdk is not installed: {sdk_error}", file=sys.stderr)
            return 5
        configure_logging(args.log_level, "text", redact=Redactor(registry.config.security))
        return runtime_cli.run_command(args, registry, adapter=adapter)

    if args.command == "validate":
        print(f"OK: {len(registry.agents)} agents, {len(registry.skills)} skills, "
              f"{len(registry.workflows)} workflows; all references resolve.")
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


if __name__ == "__main__":
    sys.exit(main())
