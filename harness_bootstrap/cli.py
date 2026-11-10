"""Command line of the opt-in harness integration. The plugin commands call it; users need not.

    django_harness.py mode     [--project DIR] [--json]
    django_harness.py detect   [--project DIR] [--json]
    django_harness.py plan     [--project DIR] [--profile pack|full] [--python EXE] [--upgrade] [--new-database] [--json]
    django_harness.py setup    --approve-plan ID  (same options as plan)
    django_harness.py status   [--project DIR] [--json]
    django_harness.py enable   [--project DIR]
    django_harness.py disable  [--project DIR] [--reset-unreadable-manifest]
    django_harness.py doctor   [--project DIR] [--python EXE] [--json]
    django_harness.py orchestrate plan|run (TASK | --task-stdin) [--category ID] [--with A] [--without A] [--json] [--timeout S]
    django_harness.py execution list|status|resume|approve|reject|cancel ...
    django_harness.py result EXECUTION_ID

Exit codes: 0 ok, 1 failed, 2 usage, 3 waiting for approval, 4 invalid input, 5 runtime error,
10 project needs confirmation, 11 blocked, 12 plan changed since approval, 13 harness invalid,
14 harness not enabled, 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import PLUGIN_ROOT, adapter, distribution
from .environment import (Check, Runner, check_credentials, check_disk, check_paths, check_shell_sandbox,
                          check_venv_support, find_python, python_info)
from .layout import Layout, ManifestError, manifest_problems, read_manifest
from .mode import HARNESS_ENABLED, HARNESS_INVALID, STANDALONE, resolve
from .project import detect_project, find_installation
from .provision import (Options, _lock_state, _foreign_entries, build_plan, deep_checks, disable, enable,
                        run_setup)

EXIT_CONFIRM, EXIT_BLOCKED = 10, 11


def _out_json(doc: Any) -> None:
    print(json.dumps(doc, indent=2, default=str))


def _print_checks(checks: List[Dict[str, Any]]) -> None:
    for item in checks:
        line = f"  [{item['status']:>4}] {item['name']}: {str(item['detail']).splitlines()[0] if item['detail'] else ''}"
        print(line)
        if item.get("remediation") and item["status"] != "ok":
            print(f"         -> {item['remediation']}")


def cmd_mode(args: argparse.Namespace) -> int:
    resolution = resolve(args.project)
    doc = resolution.to_dict()
    if args.json:
        _out_json(doc)
    else:
        print(f"{doc['mode']}  ({doc['project_root']}) -> {doc['routes_to']}")
        for problem in doc["problems"]:
            print(f"  [{problem['code']}] {problem['message']}")
            if problem["remediation"]:
                print(f"     -> {problem['remediation']}")
    return 13 if resolution.mode == HARNESS_INVALID else 0


def cmd_detect(args: argparse.Namespace) -> int:
    detection = detect_project(None, explicit=args.project)
    doc = detection.to_dict()
    if args.json:
        _out_json(doc)
    else:
        print(f"project: {doc['root'] or '(undetermined)'}  confidence: {doc['confidence']}  ({doc['reason']})")
        for candidate in doc["candidates"]:
            print(f"  candidate: {candidate}")
    return 0 if detection.confirmed else EXIT_CONFIRM


def _options(args: argparse.Namespace) -> Options:
    return Options(project=args.project, profile=args.profile, python=args.python, upgrade=args.upgrade,
                   new_database=args.new_database)


def cmd_plan(args: argparse.Namespace) -> int:
    plan = build_plan(_options(args))
    if args.json:
        _out_json(plan)
    else:
        project = plan["project"]
        print(f"Harness setup plan {plan['plan_id']}")
        print(f"  project:  {project['root'] or '(undetermined)'}  [{project['confidence']}: {project['reason']}]")
        if plan["distribution"]:
            print(f"  harness:  {plan['distribution']['version']} -> .django-engineering/{plan['distribution']['snapshot']}")
        print(f"  state:    {plan['state']}    profile: {plan['profile']}")
        if plan["python"]:
            print(f"  python:   {plan['python']}")
        print("checks:")
        _print_checks(plan["checks"])
        if plan["actions"]:
            print("actions (only after approval):")
            for action in plan["actions"]:
                print(f"  - {action}")
        if plan["warnings"]:
            print("warnings:")
            for warning in plan["warnings"]:
                print(f"  - {warning}")
        if plan["blocked"]:
            print("blocked:")
            for reason in plan["blocked"]:
                print(f"  - {reason}")
        print(f"next: {plan['next']}")
    if plan["next"] == "confirm-project":
        return EXIT_CONFIRM
    return EXIT_BLOCKED if plan["blocked"] else 0


def cmd_setup(args: argparse.Namespace) -> int:
    return run_setup(_options(args), args.approve_plan)


def _layout_or_none(project: Optional[Path]) -> Optional[Layout]:
    root = find_installation(project or Path.cwd())
    return Layout(root) if root else None


def _database_status(layout: Layout) -> Dict[str, Any]:
    path = layout.database
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            executions = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return {"status": "ok", "path": str(path), "schema_version": version, "executions": executions,
            "size_bytes": path.stat().st_size}


def cmd_status(args: argparse.Namespace) -> int:
    resolution = resolve(args.project)
    doc: Dict[str, Any] = {"repository": str(resolution.project), "execution_mode": resolution.mode,
                           "routes_to": resolution.to_dict()["routes_to"], "plugin_version": resolution.plugin_version}
    layout = resolution.layout
    if layout is None:
        doc["installation"] = {"status": "not installed",
                               "note": "the standalone plugin is in use; /django-engineering:setup-harness sets up "
                                       "the harness for this repository"}
    else:
        manifest = resolution.manifest or {}
        doc["installation"] = {"path": str(layout.enclave), "status": manifest.get("status", "unreadable"),
                               "enabled": manifest.get("enabled"), "disabled_by_user": manifest.get("disabled_by_user"),
                               "installed_at": manifest.get("installed_at"), "last_error": manifest.get("last_error")}
        doc["harness_version"] = manifest.get("harness_version")
        doc["runtime"] = (manifest.get("runtime") or {}).get("path")
        doc["profile"] = manifest.get("profile")
        info = python_info([str(layout.python)], Runner()) if layout.python.is_file() else None
        doc["python_environment"] = {"path": str(layout.venv),
                                     "status": "ok" if info else ("missing" if not layout.python.is_file() else "broken"),
                                     "version": ".".join(map(str, info["version"])) if info else None}
        doc["database"] = _database_status(layout)
        doc["configuration"] = {"path": str(layout.config_file), "status": "ok" if layout.config_file.is_file()
                                else "missing"}
    doc["runtime_ready"] = resolution.mode == HARNESS_ENABLED
    doc["problems"] = [p.to_dict() for p in resolution.problems]
    if args.json:
        _out_json(doc)
        return 0
    print(f"repository:      {doc['repository']}")
    print(f"execution mode:  {doc['execution_mode']}  (/orchestrate -> {doc['routes_to']})")
    inst = doc["installation"]
    if layout is None:
        print(f"installation:    {inst['status']} - {inst['note']}")
    else:
        print(f"installation:    {inst['status']}, {'enabled' if inst['enabled'] else 'disabled'}  ({inst['path']})")
        print(f"harness version: {doc['harness_version']}  (plugin {doc['plugin_version']}, runtime {doc['runtime']})")
        print(f"profile:         {doc['profile']}")
        env = doc["python_environment"]
        print(f"python env:      {env['status']}" + (f", Python {env['version']}" if env["version"] else ""))
        db = doc["database"]
        print(f"database:        {db['status']}" + (f", schema {db['schema_version']}, {db['executions']} executions"
                                                    if db["status"] == "ok" else ""))
        print(f"configuration:   {doc['configuration']['status']}")
    print(f"runtime ready:   {'yes' if doc['runtime_ready'] else 'no'}")
    for problem in doc["problems"]:
        print(f"  [{problem['code']}] {problem['message']}")
        if problem["remediation"]:
            print(f"     -> {problem['remediation']}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    layout = _layout_or_none(args.project)
    if layout is None:
        print("no harness installation found for this repository; run /django-engineering:setup-harness")
        return 11
    return enable(layout)


def cmd_disable(args: argparse.Namespace) -> int:
    layout = _layout_or_none(args.project)
    if layout is None:
        print("no harness installation found for this repository; it already uses the standalone plugin")
        return 0
    return disable(layout, reset_unreadable=args.reset_unreadable_manifest)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only diagnosis with an action for every problem. Never repairs anything."""
    runner = Runner()
    checks: List[Check] = []
    resolution = resolve(args.project)
    unavailable = distribution.availability(PLUGIN_ROOT)
    checks.append(Check("distribution", "fail" if unavailable else "ok", unavailable or
                        f"harness distribution {resolution.plugin_version} available in the plugin"))
    base, found = find_python(args.python, runner)
    checks.append(found if base or resolution.layout else Check(found.name, "fail", found.detail, found.remediation))
    if base is not None:
        checks += check_venv_support([base["executable"]], runner)
    layout = resolution.layout
    deepest = distribution.deepest_path(distribution.describe(PLUGIN_ROOT)) if not unavailable else None
    if layout is None:
        detection = detect_project(None, explicit=args.project)
        checks.append(Check("installation", "ok", f"none in {resolution.project}: standalone mode (setup is optional)"))
        if detection.root is not None:
            checks += check_paths(detection.root, deepest)
            checks.append(check_disk(detection.root, needs_environment=True))
        else:
            checks.append(Check("project", "warn", detection.reason, "pass --project DIR"))
    else:
        checks += check_paths(layout.project, deepest)
        checks.append(check_disk(layout.project, needs_environment=not layout.python.is_file()))
        try:
            manifest = read_manifest(layout.manifest)
        except ManifestError as exc:
            manifest = None
            checks.append(Check("manifest", "fail", str(exc), "fix it, or reset it with /django-engineering:disable-harness"))
        if manifest is not None:
            problems = manifest_problems(manifest)
            checks.append(Check("manifest", "fail" if problems else "ok", "; ".join(problems) or
                                f"status {manifest['status']}, {'enabled' if manifest['enabled'] else 'disabled'}, "
                                f"profile {manifest['profile']}", "run /django-engineering:setup-harness" if problems else ""))
            for problem in resolution.problems:
                checks.append(Check(f"mode:{problem.code}", "fail", problem.message, problem.remediation))
            if manifest.get("status") == "installed" and not problems:
                checks += deep_checks(layout, manifest, runner=runner, plugin_root=PLUGIN_ROOT)
            if manifest.get("profile") == "full":
                checks.append(Check("optional-plugins", "warn", "profile full requires the parent-repository plugins "
                                    "(security, database, api, pr-automation); they are optional integrations",
                                    "provide them under .django-engineering/plugins/, or use the pack profile"))
        db = _database_status(layout)
        if db["status"] != "ok":
            checks.append(Check("database", "fail", f"{db['status']}: {db['path']} {db.get('error', '')}".strip(),
                                "restore database/harness.db from database/backups/, or run setup with --new-database"))
        lock = _lock_state(layout)
        if lock:
            checks.append(Check("setup-lock", "warn", f"setup lock present (pid {lock['pid']}, {lock['age_seconds']} s old)",
                                "if no setup is running, delete .django-engineering/.setup.lock"))
        foreign = _foreign_entries(layout)
        if foreign:
            checks.append(Check("unexpected-files", "warn", "files setup did not create: " + ", ".join(foreign)))
        if not (layout.enclave / ".gitignore").is_file():
            checks.append(Check("gitignore", "warn", ".django-engineering/.gitignore is missing; the virtual environment "
                                "and database could be committed", "restore it with /django-engineering:setup-harness"))
    checks.append(check_credentials())
    checks.append(check_shell_sandbox())
    docs = [c.to_dict() for c in checks]
    failed = [c for c in docs if c["status"] == "fail"]
    if args.json:
        _out_json({"repository": str(resolution.project), "execution_mode": resolution.mode, "checks": docs,
                   "healthy": not failed})
    else:
        print(f"doctor: {resolution.project}  (mode {resolution.mode})")
        _print_checks(docs)
        print("healthy" if not failed else f"{len(failed)} problem(s); nothing was changed")
    return 1 if failed else 0


def _task(args: argparse.Namespace) -> Optional[str]:
    if args.task_stdin:
        return sys.stdin.read().strip()
    return args.task


def cmd_orchestrate(args: argparse.Namespace) -> int:
    resolution = resolve(args.project)
    return adapter.execute(resolution, args.action, task=_task(args), category=args.category, include=args.include,
                           exclude=args.exclude, as_json=getattr(args, "json", False),
                           timeout=getattr(args, "timeout", None))


def cmd_execution(args: argparse.Namespace) -> int:
    resolution = resolve(args.project)
    return adapter.execute(resolution, args.action, execution_id=getattr(args, "execution_id", None),
                           stage_id=getattr(args, "stage_id", None), person=getattr(args, "by", None),
                           reason=getattr(args, "reason", None), status=getattr(args, "status", None),
                           limit=getattr(args, "limit", None), as_json=getattr(args, "json", False),
                           timeout=getattr(args, "timeout", None))


def cmd_result(args: argparse.Namespace) -> int:
    return adapter.execute(resolve(args.project), "result", execution_id=args.execution_id)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="django_harness.py", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = root.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--project", type=Path, help="project directory (default: detected from the current directory)")
        return cmd

    for name, help_text in (("mode", "execution mode of this repository"), ("detect", "which directory is the project")):
        command(name, help_text).add_argument("--json", action="store_true")
    for name, help_text in (("plan", "show the setup plan (changes nothing)"), ("setup", "perform an approved plan")):
        cmd = command(name, help_text)
        cmd.add_argument("--profile", choices=["pack", "full"])
        cmd.add_argument("--python", help="interpreter for the harness environment (3.11+)")
        cmd.add_argument("--upgrade", action="store_true", help="approve upgrading an installed runtime")
        cmd.add_argument("--new-database", action="store_true",
                         help="start an empty execution history when a previous database is missing")
        if name == "plan":
            cmd.add_argument("--json", action="store_true")
        else:
            cmd.add_argument("--approve-plan", required=True, metavar="PLAN_ID", help="the plan id the user approved")
    command("status", "installation and execution-mode status").add_argument("--json", action="store_true")
    command("enable", "validate and enable the harness for this repository")
    command("disable", "disable the harness for this repository (keeps everything)").add_argument(
        "--reset-unreadable-manifest", action="store_true")
    doctor = command("doctor", "diagnose the installation (read-only)")
    doctor.add_argument("--python")
    doctor.add_argument("--json", action="store_true")

    orch = command("orchestrate", "route a request through the harness")
    orch.add_argument("action", choices=["plan", "run"])
    orch.add_argument("task", nargs="?")
    orch.add_argument("--task-stdin", action="store_true", help="read the request from standard input")
    orch.add_argument("--category")
    orch.add_argument("--with", dest="include", action="append", default=[], metavar="AGENT")
    orch.add_argument("--without", dest="exclude", action="append", default=[], metavar="AGENT")
    orch.add_argument("--json", action="store_true")
    orch.add_argument("--timeout", type=int, help="seconds per stage (can only lower the configured limit)")

    execution = command("execution", "inspect and control harness executions")
    actions = execution.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int)
    status = actions.add_parser("status")
    status.add_argument("execution_id")
    status.add_argument("--json", action="store_true")
    resume = actions.add_parser("resume")
    resume.add_argument("execution_id")
    resume.add_argument("--timeout", type=int)
    cancel = actions.add_parser("cancel")
    cancel.add_argument("execution_id")
    cancel.add_argument("--reason")
    approve = actions.add_parser("approve")
    approve.add_argument("execution_id")
    approve.add_argument("stage_id")
    approve.add_argument("--by", required=True, help="name of the person who approved, as they gave it")
    reject = actions.add_parser("reject")
    reject.add_argument("execution_id")
    reject.add_argument("stage_id")
    reject.add_argument("--by", required=True)
    reject.add_argument("--reason", required=True)
    for cmd in (listing, status, resume, cancel, approve, reject):
        cmd.add_argument("--project", type=Path, default=argparse.SUPPRESS)

    command("result", "results of one harness execution").add_argument("execution_id")
    return root


HANDLERS = {"mode": cmd_mode, "detect": cmd_detect, "plan": cmd_plan, "setup": cmd_setup, "status": cmd_status,
            "enable": cmd_enable, "disable": cmd_disable, "doctor": cmd_doctor, "orchestrate": cmd_orchestrate,
            "execution": cmd_execution, "result": cmd_result}


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")      # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args(argv)
    if getattr(args, "project", None) is not None:
        args.project = args.project.expanduser()
    try:
        return HANDLERS[args.command](args)
    except adapter.AdapterError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


__all__ = ["main", "STANDALONE"]
