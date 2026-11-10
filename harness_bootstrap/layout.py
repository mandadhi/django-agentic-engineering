"""Repository-local layout of a harness installation and its manifest (``installation.json``).

    <project>/.django-engineering/
        installation.json          manifest (this module); the mode resolver reads it
        .gitignore                 ignores everything except itself and config/framework-config.yaml
        config/framework-config.yaml   repository configuration (generated once, then the user's)
        runtime/<version>-<digest>/    versioned, read-only snapshot of the harness distribution
        database/harness.db        SQLite execution store (created by the harness's own migrations)
        database/backups/          copies taken before an upgrade migrates the database
        artifacts/                 large agent outputs (referenced from the database)
        logs/                      setup logs (credentials in URLs are redacted)
        .venv/                     the harness's own virtual environment

The manifest holds metadata only: versions, relative paths, the Python version, state and history.
It never holds credentials, and every path in it is relative to the installation directory and
checked to stay inside it. The interpreter the adapter runs is always derived from the layout
(``.venv``), never read from the manifest.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional

ENCLAVE_NAME = ".django-engineering"
MANIFEST_NAME = "installation.json"
MANIFEST_KIND = "DjangoEngineeringHarnessInstallation"
MANIFEST_SCHEMA_VERSION = 1

VENV_DIR = ".venv"
RUNTIME_DIR = "runtime"
CONFIG_FILE = "config/framework-config.yaml"
DATABASE_FILE = "database/harness.db"
BACKUPS_DIR = "database/backups"
ARTIFACTS_DIR = "artifacts"
LOGS_DIR = "logs"
GITIGNORE = ".gitignore"
LOCK_FILE = ".setup.lock"

#: Entries setup itself creates. Anything else inside an existing .django-engineering/ is a conflict.
KNOWN_ENTRIES = frozenset({MANIFEST_NAME, GITIGNORE, LOCK_FILE, VENV_DIR, RUNTIME_DIR, "config", "database",
                           ARTIFACTS_DIR, LOGS_DIR})
#: What a team may commit (a shared configuration); everything else is machine-local.
COMMITTABLE = (GITIGNORE, CONFIG_FILE)

GITIGNORE_TEXT = """\
# Created by /django-engineering:setup-harness. Everything in this directory is machine-local
# (virtual environment, runtime snapshot, execution database, logs) except the repository
# configuration, which a team may choose to commit.
*
!.gitignore
!config/
config/*
!config/framework-config.yaml
"""

STATES = ("provisioning", "installed", "failed")
PROFILES = ("pack", "full")
DEFAULT_PROFILE = "pack"

#: Manifest keys that must never appear (defence in depth: the manifest is metadata only).
_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|credential|auth)", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_python_rel() -> str:
    return f"{VENV_DIR}/" + ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@dataclass(frozen=True)
class Layout:
    """Paths of one repository's installation. ``project`` is the project root."""

    project: Path

    @property
    def enclave(self) -> Path:
        return self.project / ENCLAVE_NAME

    @property
    def manifest(self) -> Path:
        return self.enclave / MANIFEST_NAME

    @property
    def venv(self) -> Path:
        return self.enclave / VENV_DIR

    @property
    def python(self) -> Path:
        return venv_python(self.venv)

    @property
    def config_file(self) -> Path:
        return self.enclave / CONFIG_FILE

    @property
    def database(self) -> Path:
        return self.enclave / DATABASE_FILE

    @property
    def backups(self) -> Path:
        return self.enclave / BACKUPS_DIR

    @property
    def artifacts(self) -> Path:
        return self.enclave / ARTIFACTS_DIR

    @property
    def logs(self) -> Path:
        return self.enclave / LOGS_DIR

    @property
    def runtime_root(self) -> Path:
        return self.enclave / RUNTIME_DIR

    @property
    def lock(self) -> Path:
        return self.enclave / LOCK_FILE

    def runtime(self, name: str) -> Path:
        return self.runtime_root / name

    def inside(self, rel: str) -> Path:
        """``rel`` (manifest path) under the installation directory; raises ValueError if it escapes."""
        problem = path_problem(rel)
        if problem:
            raise ValueError(problem)
        return self.enclave / rel


def path_problem(rel: Any) -> Optional[str]:
    """Why ``rel`` is not a safe manifest path (relative, no ``..``, no drive), or None."""
    if not isinstance(rel, str) or not rel.strip():
        return "path must be a non-empty string"
    if "\\" in rel or re.match(r"^[A-Za-z]:", rel) or rel.startswith("/"):
        return f"path '{rel}' must be relative and use forward slashes"
    if ".." in PurePosixPath(rel).parts:
        return f"path '{rel}' must not contain '..'"
    return None


# ---------------------------------------------------------------------- manifest


class ManifestError(Exception):
    """The manifest cannot be read or is not a manifest of this schema."""


def new_manifest(*, project: Path, plugin_version: str, profile: str) -> Dict[str, Any]:
    now = utcnow()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "status": "provisioning",
        "enabled": False,
        "disabled_by_user": False,
        "harness_version": None,
        "plugin_version": plugin_version,
        "profile": profile,
        "installed_at": None,
        "created_at": now,
        "updated_at": now,
        "project_root": str(project),
        "runtime": None,
        "python": None,
        "paths": {"venv": VENV_DIR, "python": venv_python_rel(), "config": CONFIG_FILE,
                  "database": DATABASE_FILE, "artifacts": ARTIFACTS_DIR, "logs": LOGS_DIR},
        "last_error": None,
        "history": [],
    }


def read_manifest(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(f"{path} is not a JSON object")
    if doc.get("kind") != MANIFEST_KIND:
        raise ManifestError(f"{path}: kind is {doc.get('kind')!r}, expected {MANIFEST_KIND!r}")
    if doc.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"{path}: unsupported schema_version {doc.get('schema_version')!r} "
                            f"(this plugin reads {MANIFEST_SCHEMA_VERSION})")
    return doc


def manifest_problems(doc: Mapping[str, Any]) -> List[str]:
    """Structural problems of a manifest that :func:`read_manifest` accepted."""
    problems: List[str] = []
    if doc.get("status") not in STATES:
        problems.append(f"status {doc.get('status')!r} is not one of {', '.join(STATES)}")
    for key in ("enabled", "disabled_by_user"):
        if not isinstance(doc.get(key), bool):
            problems.append(f"'{key}' must be true or false")
    if doc.get("profile") not in PROFILES:
        problems.append(f"profile {doc.get('profile')!r} is not one of {', '.join(PROFILES)}")
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        problems.append("'paths' must be an object")
    else:
        expected = {"venv": VENV_DIR, "python": venv_python_rel(), "config": CONFIG_FILE, "database": DATABASE_FILE,
                    "artifacts": ARTIFACTS_DIR, "logs": LOGS_DIR}
        for key, value in expected.items():
            problem = path_problem(paths.get(key))
            if problem:
                problems.append(f"paths.{key}: {problem}")
            elif paths.get(key) != value:
                # The adapter derives these from the layout; a manifest pointing elsewhere is not trusted.
                problems.append(f"paths.{key} is '{paths.get(key)}', expected '{value}'")
    runtime = doc.get("runtime")
    if doc.get("status") == "installed":
        if not isinstance(runtime, dict):
            problems.append("an installed manifest needs a 'runtime' object")
        else:
            problem = path_problem(runtime.get("path"))
            if problem:
                problems.append(f"runtime.path: {problem}")
            elif not str(runtime.get("path")).startswith(f"{RUNTIME_DIR}/"):
                problems.append(f"runtime.path must be under {RUNTIME_DIR}/")
            for key in ("version", "digest"):
                if not isinstance(runtime.get(key), str) or not runtime.get(key):
                    problems.append(f"runtime.{key} is missing")
        if not isinstance(doc.get("harness_version"), str):
            problems.append("harness_version is missing")
    problems += [f"key '{k}' looks like a credential; the manifest must not hold secrets"
                 for k in _walk_keys(doc) if _SECRET_KEY.search(k)]
    return problems


def _walk_keys(value: Any) -> List[str]:
    keys: List[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.append(str(key))
            keys += _walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            keys += _walk_keys(inner)
    return keys


def write_manifest(path: Path, doc: Mapping[str, Any]) -> None:
    """Atomic replace (temporary file in the same directory, then ``os.replace``)."""
    found = [k for k in _walk_keys(doc) if _SECRET_KEY.search(k)]
    if found:
        raise ManifestError(f"refusing to write credential-like keys to the manifest: {found}")
    doc = dict(doc)
    doc["updated_at"] = utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".installation.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(doc, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_history(doc: Dict[str, Any], event: str, **data: Any) -> None:
    entry = {"at": utcnow(), "event": event}
    entry.update({k: v for k, v in data.items() if v is not None})
    history = doc.setdefault("history", [])
    history.append(entry)
    del history[:-50]                      # bounded
