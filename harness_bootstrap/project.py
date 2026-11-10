"""Which project directory is the target repository? (filesystem only; no subprocess, no Git binary)

Resolution, from the start directory (``--project`` or the current directory):

1. A start directory inside a ``.django-engineering/`` installation is moved to the project that owns it.
2. ``find_installation``: the nearest ancestor (stopping at the Git work-tree root) that holds
   ``.django-engineering/installation.json``. Activation is per repository: an installation in one
   repository is never found from another, and installations in the home directory or a
   filesystem root are ignored.
3. For setup (``detect_project``): an explicit ``--project`` wins; else an existing installation;
   else the Git work-tree root (a ``.git`` directory, or a ``.git`` file for worktrees and
   submodules). When the nearest ``manage.py`` lies below the Git root (a monorepo), both are
   candidates and the user must choose. Without Git, a directory holding a project marker
   (``manage.py``, ``pyproject.toml``, ...) is used with medium confidence; with no marker at all the
   user must confirm the directory explicitly.

Some directories are never a target: a filesystem root, the home directory, the Claude Code
configuration directory and the plugin installation itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import PLUGIN_ROOT
from .layout import ENCLAVE_NAME, MANIFEST_NAME

PROJECT_MARKERS = ("manage.py", "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt", "Pipfile")


def canonical(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path)))).resolve()


def _home() -> Path:
    return canonical(Path.home())


def claude_config_dir() -> Path:
    return canonical(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def owner_of_enclave(path: Path) -> Path:
    """``path`` itself, or the project that owns the ``.django-engineering`` directory containing it."""
    for candidate in (path, *path.parents):
        if candidate.name == ENCLAVE_NAME:
            return candidate.parent
    return path


def find_git_root(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _ignored_location(path: Path) -> bool:
    return path.parent == path or path == _home()


def find_installation(start: Path) -> Optional[Path]:
    """Project root of the nearest installation at or above ``start`` (inside one Git work tree)."""
    start = owner_of_enclave(canonical(start))
    git = find_git_root(start)
    for candidate in (start, *start.parents):
        if not _ignored_location(candidate) and (candidate / ENCLAVE_NAME / MANIFEST_NAME).is_file():
            return candidate
        if git is not None and candidate == git:
            break
    return None


def refusal(root: Path, plugin_root: Path = PLUGIN_ROOT) -> Optional[str]:
    """Why ``root`` can never hold a harness installation, or None."""
    plugin_root = canonical(plugin_root)
    if root.parent == root:
        return f"{root} is a filesystem root"
    if root == _home():
        return f"{root} is your home directory, not a project"
    if _within(root, claude_config_dir()):
        return f"{root} is inside the Claude Code configuration directory"
    if _within(root, plugin_root):
        return f"{root} is the Django Engineering plugin installation itself, not your project"
    if root.name == ENCLAVE_NAME:
        return f"{root} is a harness installation directory, not a project"
    return None


@dataclass
class Detection:
    start: Path
    root: Optional[Path]
    confidence: str                    # explicit | high | medium | ambiguous | low | refused
    reason: str
    git_root: Optional[Path] = None
    installation: Optional[Path] = None
    candidates: List[Path] = field(default_factory=list)
    markers: List[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        """Safe to act on without asking the user which directory is meant."""
        return self.root is not None and self.confidence in ("explicit", "high", "medium")

    def to_dict(self) -> dict:
        return {"start": str(self.start), "root": str(self.root) if self.root else None,
                "confidence": self.confidence, "confirmed": self.confirmed, "reason": self.reason,
                "git_root": str(self.git_root) if self.git_root else None,
                "is_git_repository": self.git_root is not None,
                "installation": str(self.installation) if self.installation else None,
                "candidates": [str(c) for c in self.candidates], "markers": self.markers}


def _markers(path: Path) -> List[str]:
    return [m for m in PROJECT_MARKERS if (path / m).is_file()]


def _nearest_manage_py(start: Path, stop: Optional[Path]) -> Optional[Path]:
    for candidate in (start, *start.parents):
        if (candidate / "manage.py").is_file():
            return candidate
        if stop is not None and candidate == stop:
            break
    return None


def detect_project(start: Optional[Path] = None, *, explicit: Optional[Path] = None,
                   plugin_root: Path = PLUGIN_ROOT) -> Detection:
    if explicit is not None:
        root = owner_of_enclave(canonical(explicit))
        if not root.is_dir():
            return Detection(canonical(explicit), None, "refused", f"{root} is not a directory")
        why = refusal(root, plugin_root)
        if why:
            return Detection(root, None, "refused", why)
        return Detection(root, root, "explicit", "directory given with --project", find_git_root(root),
                         root if (root / ENCLAVE_NAME / MANIFEST_NAME).is_file() else None, markers=_markers(root))

    start = owner_of_enclave(canonical(start or Path.cwd()))
    git = find_git_root(start)
    installed = find_installation(start)
    if installed is not None:
        why = refusal(installed, plugin_root)
        if why:
            return Detection(start, None, "refused", why, git)
        return Detection(start, installed, "high", "existing harness installation", git, installed,
                         markers=_markers(installed))
    if git is not None:
        why = refusal(git, plugin_root)
        if why:
            return Detection(start, None, "refused", why, git)
        manage = _nearest_manage_py(start, git)
        if manage is not None and manage != git:
            return Detection(start, None, "ambiguous", f"manage.py is in {manage}, below the Git root {git}; "
                             "choose which directory is the project", git, candidates=[git, manage],
                             markers=_markers(manage))
        return Detection(start, git, "high", "Git work-tree root", git, markers=_markers(git))
    why = refusal(start, plugin_root)
    if why:
        return Detection(start, None, "refused", why)
    markers = _markers(start)
    if markers:
        return Detection(start, start, "medium", f"not a Git repository; using the directory holding "
                         f"{', '.join(markers)}", markers=markers)
    manage = _nearest_manage_py(start, None)
    if manage is not None and refusal(manage, plugin_root) is None:
        return Detection(start, manage, "medium", f"not a Git repository; using {manage}, which holds manage.py",
                         markers=_markers(manage))
    return Detection(start, None, "low", "no Git repository and no project marker (manage.py, pyproject.toml, ...) "
                     "found; confirm the project directory with --project", candidates=[start])
