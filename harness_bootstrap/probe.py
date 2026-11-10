"""Helpers that run *inside* the repository's harness virtual environment, from its runtime snapshot.

    <venv python> -X utf8 -E -s -m harness_bootstrap.probe <command> --root <runtime> --framework-file <config> ...

Run with the snapshot directory as the working directory, so ``core``, ``runtime`` and
``agent_runtime`` are imported from the versioned snapshot, never from the plugin cache. Every
command reuses the existing harness: configuration is loaded and validated by ``core.config``, the
database is opened through ``runtime.store.open_store`` (whose versioned migrations create or
upgrade it in place), executions are read through the ``ExecutionStore`` API and redacted with the
harness's own ``Redactor``. Nothing here routes, executes, retries or checkpoints anything.

Commands print one JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

RESULT_CHARS = 20000                 # per output artifact returned to Claude Code


def _emit(doc: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(doc, indent=2, default=str))
    return code


def _fail(message: str, code: int = 1, **extra: Any) -> int:
    return _emit({"ok": False, "error": message, **extra}, code)


def cmd_write_config(args: argparse.Namespace) -> int:
    """The repository's framework-config.yaml, derived from the snapshot's canonical one. Never overwrites."""
    import yaml

    out = Path(args.out)
    if out.exists():
        return _emit({"ok": True, "written": False, "path": str(out), "note": "existing configuration kept"})
    canonical = Path(args.root) / "config" / "framework-config.yaml"
    doc = yaml.safe_load(canonical.read_text(encoding="utf-8"))
    # Relative paths resolve against the runtime snapshot (--root): .django-engineering/runtime/<name>/.
    doc["paths"]["repo_root"] = "../.."                 # optional `full`-profile plugins would live in plugins/ here
    doc["paths"]["artifacts_dir"] = "../../artifacts"
    doc["paths"]["enclave"] = "../.."
    doc["storage"]["sqlite"]["path"] = "../../database/harness.db"
    header = (
        "# Repository configuration of the Django Agentic Harness, created once by\n"
        "# /django-engineering:setup-harness from the canonical config/framework-config.yaml of the runtime\n"
        "# snapshot. It is yours: setup and upgrades never overwrite it. A team may commit it.\n"
        "#\n"
        "# Relative paths resolve against the runtime snapshot (.django-engineering/runtime/<version>/), so\n"
        "# `configs:` loads the canonical registries, security configuration and orchestration policy that\n"
        "# ship with the plugin, and `../..` is this .django-engineering/ directory.\n"
        "# paths.enclave marks .django-engineering/ as the harness's own protected area inside the project:\n"
        "# agents may work on the project, never inside it (agent_runtime/workspace.py).\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "x", encoding="utf-8", newline="\n") as handle:      # "x": never overwrite
        handle.write(header + yaml.safe_dump(doc, sort_keys=False))
    return _emit({"ok": True, "written": True, "path": str(out)})


def _config(args: argparse.Namespace):
    from core.config import load_config
    return load_config(args.root, args.framework_file, profile=args.profile)


def cmd_init_db(args: argparse.Namespace) -> int:
    from core.errors import HarnessError
    from runtime.sqlite_store import SCHEMA_VERSION
    from runtime.store import open_store

    try:
        config = _config(args)
    except HarnessError as exc:
        return _fail(f"configuration invalid: {exc}", 2)
    path = config.framework.storage.sqlite_path
    existed = bool(path and path.is_file())
    if args.must_exist and not existed:
        return _fail(f"the execution database {path} is missing; setup never recreates it silently. Restore it "
                     "from database/backups/ or run setup with --new-database to start an empty history.", 3,
                     path=str(path))
    store = open_store(config.framework.storage)          # applies the harness's own versioned migrations
    try:
        version = store.schema_version
    finally:
        store.close()
    return _emit({"ok": version == SCHEMA_VERSION, "path": str(path), "created": not existed,
                  "schema_version": version, "expected_schema_version": SCHEMA_VERSION})


def _pinned(root: Path) -> list[tuple[str, str]]:
    pins = []
    for line in (root / "requirements-harness.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and "==" in line:
            name, version = line.split("==", 1)
            pins.append((name.strip(), version.strip()))
    return pins


def cmd_readiness(args: argparse.Namespace) -> int:
    """Dependencies, configuration, registry, orchestration policy and database, without running an agent."""
    import importlib.metadata as metadata
    import sqlite3

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, remediation: str = "") -> None:
        checks.append({"name": name, "status": "ok" if ok else "fail", "detail": detail,
                       **({"remediation": remediation} if remediation and not ok else {})})

    root = Path(args.root)
    missing = []
    for name, version in _pinned(root):
        try:
            found = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(f"{name} (not installed)")
            continue
        if found != version:
            missing.append(f"{name} {found} (pinned {version})")
    add("dependencies", not missing, "pinned harness dependencies installed" if not missing else
        "missing or different: " + ", ".join(missing), "run /django-engineering:setup-harness to install the pinned versions")
    try:
        import claude_agent_sdk  # noqa: F401
        import agent_runtime.runner  # noqa: F401
        add("agent-runtime", True, f"claude-agent-sdk {metadata.version('claude-agent-sdk')} and the harness runtime import")
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        add("agent-runtime", False, f"{type(exc).__name__}: {exc}", "run /django-engineering:setup-harness")
    try:
        from core.registry import HarnessRegistry
        registry = HarnessRegistry.load(args.root, args.framework_file, profile=args.profile)
        policy = registry.config.orchestration
        add("configuration", True, f"{len(registry.agents)} agents, {len(registry.skills)} skills, "
            f"{len(registry.workflows)} workflows (profile {registry.config.profile})")
        add("orchestration-policy", policy is not None,
            f"policy {policy.version}: {len(policy.categories)} categories" if policy else "no orchestration policy")
        enclave = registry.config.framework.enclave
        add("enclave", enclave is not None, f"protected installation area {enclave}" if enclave else
            "paths.enclave is not set: the harness would refuse the project as a workspace",
            "restore paths.enclave: ../.. in .django-engineering/config/framework-config.yaml")
        db = registry.config.framework.storage.sqlite_path
        if db is None or not db.is_file():
            add("database", False, f"execution database missing: {db}", "run /django-engineering:setup-harness")
        else:
            from runtime.sqlite_store import SCHEMA_VERSION
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
            add("database", version == SCHEMA_VERSION and integrity == "ok",
                f"{db.name}: schema {version} (expected {SCHEMA_VERSION}), integrity {integrity}",
                "run /django-engineering:setup-harness --upgrade to migrate, or restore from database/backups/")
    except Exception as exc:  # noqa: BLE001
        add("configuration", False, f"{type(exc).__name__}: {exc}",
            "fix .django-engineering/config/framework-config.yaml, or run /django-engineering:doctor")
    ok = all(c["status"] == "ok" for c in checks)
    return _emit({"ok": ok, "checks": checks}, 0 if ok else 1)


def _artifact_text(uri: str, artifacts_dir: Path, redactor) -> dict[str, Any]:
    parsed = urlparse(uri)
    path = Path(unquote(parsed.path.lstrip("/") if len(parsed.path) > 2 and parsed.path[2] == ":" else parsed.path)) \
        if parsed.scheme == "file" else artifacts_dir / uri
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return {"uri": uri, "available": False}
    base = artifacts_dir.resolve()
    if resolved != base and base not in resolved.parents:
        return {"uri": uri, "available": False, "note": "outside the artifacts directory; not read"}
    text = resolved.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > RESULT_CHARS
    return {"uri": uri, "available": True, "truncated": truncated, "content": redactor.redact(text[:RESULT_CHARS])}


def cmd_results(args: argparse.Namespace) -> int:
    """Stage results of one execution: status, summaries, output artifacts and guard decisions (redacted)."""
    from core.errors import HarnessError, NotFoundError
    from core.security import Redactor
    from runtime.store import open_store

    try:
        config = _config(args)
    except HarnessError as exc:
        return _fail(f"configuration invalid: {exc}", 2)
    redactor = Redactor(config.security)
    store = open_store(config.framework.storage)
    try:
        try:
            execution = store.get_execution(args.execution_id)
        except NotFoundError as exc:
            return _fail(str(exc), 4)
        stages = []
        for stage in store.list_stages(args.execution_id):
            runs = store.list_agent_runs(args.execution_id, stage.stage_id)
            last = runs[-1] if runs else None
            result = (last.result or {}) if last else {}
            stages.append({
                "stage_id": stage.stage_id, "agent_id": stage.agent_id, "status": stage.status.value,
                "attempts": stage.attempts, "error": stage.error.to_dict() if stage.error else None,
                "result_status": result.get("status"),
                "summary": redactor.redact(result.get("summary", "")) if result.get("summary") else None,
                "outputs": [{"name": o.get("name"), **_artifact_text(o.get("artifact", {}).get("uri", ""),
                                                                     config.framework.artifacts_dir, redactor)}
                            for o in result.get("outputs", [])],
                "errors": result.get("errors", []),
                "cost_usd": last.cost_usd if last else None,
            })
        guard = [{"event": e.event, "stage_id": e.stage_id, "message": redactor.redact(e.message)}
                 for e in store.list_logs(args.execution_id)
                 if e.event in ("tool.denied", "tool.approval_requested", "tool.approved")]
        return _emit({"ok": True, "execution_id": execution.execution_id, "workflow_id": execution.workflow_id,
                      "status": execution.status.value, "metadata": execution.metadata,
                      "error": execution.error.to_dict() if execution.error else None,
                      "stages": stages, "guard_decisions": guard[-50:]})
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness_bootstrap.probe")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write-config")
    write.add_argument("--root", required=True)
    write.add_argument("--out", required=True)
    for name in ("init-db", "readiness", "results"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--root", required=True)
        cmd.add_argument("--framework-file", required=True)
        cmd.add_argument("--profile", required=True)
        if name == "init-db":
            cmd.add_argument("--must-exist", action="store_true")
        if name == "results":
            cmd.add_argument("execution_id")
    args = parser.parse_args(argv)
    handler = {"write-config": cmd_write_config, "init-db": cmd_init_db, "readiness": cmd_readiness,
               "results": cmd_results}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
