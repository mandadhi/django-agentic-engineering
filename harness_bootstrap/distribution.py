"""The harness distribution shipped with the plugin, and its versioned runtime snapshots.

The plugin root *is* the harness root: the canonical agents, skills, rules, templates,
references, orchestration policy and security configuration that the standalone plugin loads are
the same files the harness loads. Setup copies exactly that set (``DIST_DIRS`` + ``DIST_FILES``),
byte for byte, into ``.django-engineering/runtime/<version>-<digest>/``:

* versioned and reproducible: the directory name carries the plugin version and the SHA-256
  digest of every file, and ``RUNTIME.json`` records each file's hash;
* immutable: an existing snapshot is verified, never overwritten; a different distribution gets a
  different directory;
* independent of the plugin cache: Claude Code may replace or remove its cached plugin at any time,
  so the harness never runs from there;
* no parent-repository plugins and nothing else from outside the distribution is copied (the
  ``full`` profile's optional integrations stay out; the ``pack`` profile is the repository default).

A staged plugin bundle (``python -m plugin_packaging stage``) deliberately carries no harness
code; :func:`availability` then says so and setup stops with an explanation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DIST_DIRS = ("core", "runtime", "agent_runtime", "config", "contracts", "workflows", "agents", "commands", "skills",
             "rules", "templates", "references", "hooks", "harness_bootstrap")
DIST_FILES = (".claude-plugin/plugin.json", "plugin_packaging/standalone-security.yaml", "requirements-harness.txt")
#: Files whose absence means "this plugin installation carries no harness".
REQUIRED = ("core/cli.py", "runtime/sqlite_store.py", "agent_runtime/runner.py", "config/framework-config.yaml",
            "config/orchestration-policy.yaml", "requirements-harness.txt", "harness_bootstrap/probe.py")
RECORD = "RUNTIME.json"
_SKIP_PARTS = {"__pycache__", ".pytest_cache"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


class DistributionError(Exception):
    pass


def plugin_manifest(plugin_root: Path) -> Dict:
    try:
        return json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DistributionError(f"cannot read the plugin manifest: {exc}") from exc


def plugin_version(plugin_root: Path) -> str:
    version = plugin_manifest(plugin_root).get("version")
    if not isinstance(version, str) or not version:
        raise DistributionError("the plugin manifest has no version")
    return version


def availability(plugin_root: Path) -> Optional[str]:
    """None when the harness distribution is complete, else what is missing."""
    missing = [rel for rel in REQUIRED if not (plugin_root / rel).is_file()]
    if missing:
        return ("this plugin installation does not include the harness distribution (missing: "
                + ", ".join(missing) + "). Staged bundles ship the standalone plugin only; install the plugin "
                "from the repository marketplace to use the harness.")
    return None


def distribution_files(root: Path) -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    for name in DIST_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel_parts = path.relative_to(root).parts
            if path.is_file() and not (_SKIP_PARTS & set(rel_parts)) and path.suffix not in _SKIP_SUFFIXES:
                files.append((path.relative_to(root).as_posix(), path))
    for rel in DIST_FILES:
        if (root / rel).is_file():
            files.append((rel, root / rel))
    files.sort(key=lambda item: item[0])
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(entries: Iterable[Tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for rel, sha in sorted(entries):
        digest.update(f"{rel}\0{sha}\n".encode("utf-8"))
    return digest.hexdigest()


def describe(root: Path) -> Dict:
    """``{"version", "digest", "files": [{"path", "sha256"}]}`` of the distribution at ``root``."""
    entries = [(rel, _sha256(path)) for rel, path in distribution_files(root)]
    return {"version": plugin_version(root), "digest": tree_digest(entries),
            "files": [{"path": rel, "sha256": sha} for rel, sha in entries]}


def snapshot_name(version: str, digest: str) -> str:
    return f"{version}-{digest[:8]}"           # unique enough per installation; RUNTIME.json has the full digest


def deepest_path(described: Dict) -> int:
    """Length of the longest path setup creates below ``.django-engineering/`` for this distribution:
    a runtime snapshot file, or a file of the virtual environment (see environment.VENV_DEEPEST)."""
    from .environment import VENV_DEEPEST
    name = snapshot_name(described["version"], described["digest"])
    runtime = max((len(f"runtime/{name}/{entry['path']}") for entry in described["files"]), default=0)
    return max(runtime, len(".venv/") + VENV_DEEPEST)


def read_record(runtime_dir: Path) -> Dict:
    try:
        record = json.loads((runtime_dir / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DistributionError(f"{runtime_dir / RECORD} is unreadable: {exc}") from exc
    if not isinstance(record, dict) or not isinstance(record.get("files"), list):
        raise DistributionError(f"{runtime_dir / RECORD} is not a runtime record")
    return record


def verify_snapshot(runtime_dir: Path) -> List[str]:
    """Every difference between a snapshot and its RUNTIME.json (missing, changed, unexpected files)."""
    try:
        record = read_record(runtime_dir)
    except DistributionError as exc:
        return [str(exc)]
    problems: List[str] = []
    expected = {}
    for entry in record["files"]:
        rel, sha = entry.get("path"), entry.get("sha256")
        if not isinstance(rel, str) or ".." in Path(rel).parts or Path(rel).is_absolute():
            return [f"{RECORD} lists an unsafe path {rel!r}"]
        expected[rel] = sha
    for rel, sha in expected.items():
        path = runtime_dir / rel
        if not path.is_file():
            problems.append(f"missing {rel}")
        elif _sha256(path) != sha:
            problems.append(f"changed {rel}")
    actual = {rel for rel, _ in distribution_files(runtime_dir)}
    problems += [f"unexpected {rel}" for rel in sorted(actual - set(expected))]
    if tree_digest(expected.items()) != record.get("digest"):
        problems.append(f"{RECORD} digest does not match its file list")
    return problems


def copy_snapshot(plugin_root: Path, runtime_root: Path) -> Tuple[Path, Dict, bool]:
    """Provision ``runtime_root/<version>-<digest>`` from the plugin. Returns (dir, record, created).

    An existing snapshot is verified and reused, never overwritten. A new one is copied into a
    temporary sibling directory, verified, then renamed into place, so an interrupted copy never
    leaves a half-written snapshot under the final name.
    """
    description = describe(plugin_root)
    name = snapshot_name(description["version"], description["digest"])
    target = runtime_root / name
    if target.exists():
        problems = verify_snapshot(target)
        if problems:
            raise DistributionError(f"runtime snapshot {target} exists but does not verify ({'; '.join(problems[:5])}). "
                                    "It is never overwritten; move it aside yourself and run setup again.")
        return target, read_record(target), False
    runtime_root.mkdir(parents=True, exist_ok=True)
    partial = runtime_root / f".p{os.getpid()}"        # short: Windows MAX_PATH (deepest file ~80 characters)
    if partial.exists():
        shutil.rmtree(partial)              # our own leftover from this very process id
    try:
        for entry in description["files"]:
            source = plugin_root / entry["path"]
            dest = partial / entry["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        record = {"generated_by": "/django-engineering:setup-harness", "do_not_edit": True, **description}
        (partial / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        problems = verify_snapshot(partial)
        if problems:
            raise DistributionError("the copied runtime does not verify: " + "; ".join(problems[:5]))
        os.replace(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return target, record, True
