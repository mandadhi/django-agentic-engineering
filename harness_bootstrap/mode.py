"""The single execution-mode resolver. Every harness-aware command asks this module, and only this one.

=================  ======================================================  ====================================
mode               repository state                                        what /orchestrate does
=================  ======================================================  ====================================
standalone         no ``.django-engineering/installation.json``            standalone plugin (unchanged)
harness_enabled    installed, validated, explicitly enabled, runtime       the existing Agentic Harness
                   matches this plugin's distribution
harness_disabled   installed, explicitly disabled by the user              standalone plugin; history kept
harness_invalid    anything else: interrupted or failed setup, unreadable  stops and reports; never falls back
                   manifest, missing files, moved project, runtime out
                   of date with the plugin
=================  ======================================================  ====================================

Resolution is per repository (:func:`project.find_installation`); there is no global flag. It reads
files only: it never imports the harness, starts the runtime or creates anything, so standalone use
stays free of the harness. The distribution digest comparison (``runtime-drift``) runs only for an
installation that is otherwise enabled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import PLUGIN_ROOT, distribution
from .layout import Layout, ManifestError, manifest_problems, read_manifest
from .project import canonical, find_git_root, find_installation, owner_of_enclave

STANDALONE = "standalone"
HARNESS_ENABLED = "harness_enabled"
HARNESS_DISABLED = "harness_disabled"
HARNESS_INVALID = "harness_invalid"
MODES = (STANDALONE, HARNESS_ENABLED, HARNESS_DISABLED, HARNESS_INVALID)


@dataclass
class Problem:
    code: str
    message: str
    remediation: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message, "remediation": self.remediation}


@dataclass
class Resolution:
    mode: str
    project: Path
    installation: Optional[Path] = None
    manifest: Optional[Dict[str, Any]] = None
    problems: List[Problem] = field(default_factory=list)
    plugin_version: Optional[str] = None

    @property
    def layout(self) -> Optional[Layout]:
        return Layout(self.installation) if self.installation else None

    @property
    def runtime_dir(self) -> Optional[Path]:
        if self.layout is None or not self.manifest or not isinstance(self.manifest.get("runtime"), dict):
            return None
        try:
            return self.layout.inside(self.manifest["runtime"]["path"])
        except (ValueError, KeyError, TypeError):
            return None

    @property
    def profile(self) -> Optional[str]:
        return (self.manifest or {}).get("profile")

    def to_dict(self) -> Dict[str, Any]:
        manifest = self.manifest or {}
        runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
        return {
            "mode": self.mode,
            "project_root": str(self.project),
            "installation": str(self.layout.enclave) if self.layout else None,
            "status": manifest.get("status"),
            "enabled": manifest.get("enabled"),
            "harness_version": manifest.get("harness_version"),
            "runtime": runtime.get("path"),
            "plugin_version": self.plugin_version,
            "profile": manifest.get("profile"),
            "problems": [p.to_dict() for p in self.problems],
            "routes_to": {STANDALONE: "standalone plugin", HARNESS_DISABLED: "standalone plugin",
                          HARNESS_ENABLED: "Agentic Harness", HARNESS_INVALID: "nothing (repair or disable)"}[self.mode],
        }


_REPAIR = "run /django-engineering:doctor, then /django-engineering:setup-harness to repair, " \
          "or /django-engineering:disable-harness to use the standalone plugin"


def resolve(start: Optional[Path] = None, *, plugin_root: Path = PLUGIN_ROOT, check_digest: bool = True) -> Resolution:
    start = owner_of_enclave(canonical(start or Path.cwd()))
    try:
        version: Optional[str] = distribution.plugin_version(plugin_root)
    except distribution.DistributionError:
        version = None
    project = find_installation(start)
    if project is None:
        return Resolution(STANDALONE, find_git_root(start) or start, plugin_version=version)
    layout = Layout(project)
    try:
        manifest = read_manifest(layout.manifest)
    except ManifestError as exc:
        return Resolution(HARNESS_INVALID, project, project, None,
                          [Problem("manifest-unreadable", str(exc), "repair or move installation.json aside, or run "
                                   "/django-engineering:disable-harness (it offers to reset an unreadable manifest)")],
                          version)
    result = Resolution(HARNESS_INVALID, project, project, manifest, [], version)
    if manifest.get("enabled") is False and manifest.get("disabled_by_user") is True:
        result.mode = HARNESS_DISABLED
        return result
    problems = result.problems
    structural = manifest_problems(manifest)
    problems += [Problem("manifest-invalid", p, _REPAIR) for p in structural]
    status = manifest.get("status")
    if status == "provisioning":
        problems.append(Problem("setup-interrupted", "harness setup did not finish",
                                "run /django-engineering:setup-harness again to resume it, or disable the harness"))
    elif status == "failed":
        problems.append(Problem("setup-failed", f"harness setup failed: {manifest.get('last_error') or 'unknown error'}",
                                "run /django-engineering:doctor and /django-engineering:setup-harness again"))
    elif manifest.get("enabled") is not True:
        problems.append(Problem("not-enabled", "the installation is neither enabled nor explicitly disabled",
                                "run /django-engineering:enable-harness or /django-engineering:disable-harness"))
    if problems:
        return result

    recorded = manifest.get("project_root")
    if recorded and os.path.normcase(str(canonical(Path(recorded)))) != os.path.normcase(str(project)):
        problems.append(Problem("relocated", f"the installation was created for {recorded} and now lies in {project}; "
                                "its virtual environment holds absolute paths", "disable it, move .django-engineering "
                                "aside and run /django-engineering:setup-harness in the new location"))
    runtime_dir = result.runtime_dir
    for label, path in (("virtual environment interpreter", layout.python), ("configuration", layout.config_file),
                        ("execution database", layout.database)):
        if not path.is_file():
            problems.append(Problem("missing-file", f"{label} not found: {path}", _REPAIR))
    if runtime_dir is None or not (runtime_dir / distribution.RECORD).is_file():
        problems.append(Problem("missing-runtime", f"runtime snapshot not found: {runtime_dir}", _REPAIR))
    if problems:
        return result

    runtime = manifest["runtime"]
    if version is not None and runtime.get("version") != version:
        problems.append(Problem("version-mismatch", f"the repository runs harness {runtime.get('version')}, this plugin "
                                f"is {version}; runtimes are never upgraded silently",
                                "run /django-engineering:setup-harness --upgrade (keeps the database and your "
                                "configuration), or /django-engineering:disable-harness"))
    elif check_digest and version is not None and distribution.availability(plugin_root) is None:
        current = distribution.describe(plugin_root)["digest"]
        if current != runtime.get("digest"):
            problems.append(Problem("runtime-drift", "this plugin's harness distribution differs from the repository's "
                                    f"runtime snapshot (same version {version}, different content), so the two modes "
                                    "would not load the same canonical agents and policy",
                                    "run /django-engineering:setup-harness --upgrade, or disable the harness"))
    if not problems:
        result.mode = HARNESS_ENABLED
    return result
