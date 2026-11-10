"""Guided setup and launcher for the Django Agentic Harness (standard library only).

    python scripts/harness.py setup  [--profile full|pack] [--venv PATH] [--yes]
    python scripts/harness.py check  [--profile full|pack] [--venv PATH]
    python scripts/harness.py run    [--profile full|pack] [--venv PATH] -- <core.cli arguments>

setup  Checks the Python version, creates the harness virtual environment if it does not exist
       (an existing one is reused, never replaced), shows the pinned dependencies of
       requirements-harness.txt and installs them into that environment after you confirm (or
       with --yes), then runs `check`. Nothing is installed globally, no remote script is
       downloaded or executed, and no configuration file or database is created or overwritten.
check  Verifies the environment and dependencies, then runs `python -m core.cli validate` for the
       selected profile and explains every missing mandatory or optional dependency.
run    Runs `python -m core.cli` from the harness environment with the selected profile. It never
       falls back to the standalone plugin or to another interpreter when something is missing.

Profiles: `full` (default) requires the parent-repository plugins; `pack` excludes those optional
integrations explicitly and runs the Django Engineering Pack alone. The standalone Claude Code
plugin needs none of this.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements-harness.txt"
DEFAULT_VENV = ROOT / ".venv"
MIN_PYTHON = (3, 11)
PROFILES = ("full", "pack")
IMPORTS = ("yaml", "jsonschema", "referencing", "claude_agent_sdk")


class SetupError(Exception):
    """A step failed; the message says how to recover."""


def say(message: str) -> None:
    print(message, flush=True)


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def check_python(executable_version: tuple[int, int] | None = None) -> None:
    version = executable_version or sys.version_info[:2]
    if version < MIN_PYTHON:
        raise SetupError(f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required for the harness; this is "
                         f"{'.'.join(map(str, version))}. Install a newer Python and run this script with it "
                         "(for example `py -3.13 scripts/harness.py setup` on Windows, "
                         "`python3.13 scripts/harness.py setup` elsewhere).")


def pinned_requirements() -> list[str]:
    if not REQUIREMENTS.is_file():
        raise SetupError(f"{REQUIREMENTS.name} is missing; restore it from the repository.")
    lines = [line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    reqs = [line for line in lines if line and not line.startswith("#")]
    unpinned = [r for r in reqs if "==" not in r]
    if unpinned:
        raise SetupError(f"{REQUIREMENTS.name} must pin exact versions; not pinned: {', '.join(unpinned)}")
    return reqs


def ensure_venv(venv_dir: Path) -> Path:
    python = venv_python(venv_dir)
    if python.is_file():
        say(f"reusing the existing virtual environment {venv_dir}")
        version = subprocess.run([str(python), "-c", "import sys; print('%d %d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, check=False)
        if version.returncode != 0:
            raise SetupError(f"{python} does not run. Remove or repair {venv_dir} yourself, or choose another "
                             "location with --venv; this script never deletes an environment.")
        check_python(tuple(int(x) for x in version.stdout.split()))
        return python
    if venv_dir.exists():
        raise SetupError(f"{venv_dir} exists but is not a virtual environment; choose another --venv. "
                         "This script never overwrites an existing directory.")
    say(f"creating a virtual environment at {venv_dir}")
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    if not python.is_file():
        raise SetupError(f"virtual environment creation did not produce {python}")
    return python


def missing_imports(python: Path) -> list[str]:
    missing = []
    for module in IMPORTS:
        probe = subprocess.run([str(python), "-c", f"import {module}"], capture_output=True, check=False)
        if probe.returncode != 0:
            missing.append(module)
    return missing


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SetupError("confirmation needed but no terminal is attached; re-run with --yes to accept")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def validate(python: Path, profile: str) -> int:
    say(f"validating the harness configuration (profile: {profile})")
    result = subprocess.run([str(python), "-m", "core.cli", "--profile", profile, "validate"], cwd=ROOT, check=False)
    if result.returncode != 0 and profile == "full":
        say("\nThe full profile needs the parent-repository plugins listed above. Either provide them, or "
            "run the Django Engineering Pack on its own with the explicit `pack` profile:\n"
            "  python scripts/harness.py check --profile pack")
    return result.returncode


def cmd_check(args: argparse.Namespace) -> int:
    check_python()
    python = venv_python(args.venv)
    if not python.is_file():
        raise SetupError(f"no harness environment at {args.venv}; run `python scripts/harness.py setup` first")
    missing = missing_imports(python)
    if missing:
        raise SetupError(f"missing harness dependencies in {args.venv}: {', '.join(missing)}; run "
                         "`python scripts/harness.py setup` to install the pinned versions")
    say(f"dependencies: ok ({', '.join(IMPORTS)})")
    database = ROOT / "var" / "harness.db"
    say(f"database: {'existing ' + str(database) + ' (reused, never overwritten)' if database.exists() else 'none yet (created on the first run)'}")
    say("credentials: a Claude Code login (`claude auth status`) or ANTHROPIC_API_KEY is needed to run agents; "
        "validation does not need them")
    return validate(python, args.profile)


def cmd_setup(args: argparse.Namespace) -> int:
    check_python()
    reqs = pinned_requirements()
    say("The harness needs these pinned packages, installed only into its own virtual environment:")
    for req in reqs:
        say(f"  {req}")
    say(f"environment: {args.venv}  (source: your configured pip index; nothing is installed globally)")
    if not confirm("Install them now?", args.yes):
        say("nothing installed")
        return 1
    python = ensure_venv(args.venv)
    result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                             str(REQUIREMENTS)], cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SetupError("pip could not install the pinned dependencies (see its output above). Check your network "
                         "or package index, then run setup again; the environment is kept as it is.")
    return cmd_check(args)


def cmd_run(args: argparse.Namespace) -> int:
    python = venv_python(args.venv)
    if not python.is_file():
        raise SetupError(f"no harness environment at {args.venv}; run `python scripts/harness.py setup` first. "
                         "The launcher never falls back to another interpreter or to the standalone plugin.")
    rest = args.cli_args[1:] if args.cli_args[:1] == ["--"] else args.cli_args
    if not rest:
        raise SetupError("nothing to run; pass core.cli arguments after --, for example: "
                         "python scripts/harness.py run --profile pack -- orchestrate plan \"Review my branch\"")
    return subprocess.run([str(python), "-m", "core.cli", "--profile", args.profile, *rest], cwd=ROOT,
                          check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/harness.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("setup", "check", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--profile", choices=PROFILES, default=os.environ.get("DAE_HARNESS_PROFILE") or "full")
        cmd.add_argument("--venv", type=Path, default=DEFAULT_VENV, help="harness virtual environment (default: .venv)")
        if name == "setup":
            cmd.add_argument("--yes", action="store_true", help="install without asking")
        if name == "run":
            cmd.add_argument("cli_args", nargs=argparse.REMAINDER, help="arguments for python -m core.cli")
    args = parser.parse_args(argv)
    args.venv = args.venv.resolve()
    try:
        return {"setup": cmd_setup, "check": cmd_check, "run": cmd_run}[args.action](args)
    except SetupError as exc:
        print(f"harness setup: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
