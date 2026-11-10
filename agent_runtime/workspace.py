"""The trusted harness root and the agent workspace are separate (SEC-04).

The harness installation, its configuration, its execution database, the registered plugins
(including their security definitions), the Python environment and the Claude Code config
directory are *trusted infrastructure*. An agent's workspace is untrusted project data. If the
two overlapped, an agent could weaken ``config/security-config.yaml``, widen a plugin agent's
``tools``, or rewrite ``var/harness.db`` for later runs: a persistent privilege escalation.

So a workspace is accepted only when its canonical path (symlinks, junctions, ``..`` and 8.3
short names resolved, strictly) neither equals, contains nor lies inside any protected root.
A path-prefix comparison of the raw strings is never used. Resolution errors refuse the
workspace (fail closed).

The per-call checks live in :class:`agent_runtime.security_hooks.ToolGuard`: every tool path is
resolved the same way and must stay inside the workspace (or the run's own output directory,
or a plugin read root for reads), and nothing inside a protected root is reachable except
those explicit exceptions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from core import HARNESS_HOME
from core.config import HarnessConfig
from core.errors import HarnessError


class WorkspaceError(HarnessError):
    """The workspace is not a usable, untrusted project directory. Callers fail closed."""

    code = "workspace.invalid"


def canonical(path: str | Path) -> Path:
    """Absolute, fully resolved path of an existing directory or file (strict: raises on failure)."""
    try:
        return Path(os.path.expanduser(str(path))).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:        # missing, permission, symlink loop, NUL byte
        raise WorkspaceError(f"cannot resolve the canonical path of {path!s}: {exc}") from exc


def _canonical_or_lexical(path: Path) -> Path:
    """A protected root that does not exist yet (e.g. var/ before the first run) is still protected."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(path))


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def protected_roots(config: HarnessConfig, *, extra: Iterable[Path | None] = ()) -> tuple[Path, ...]:
    """Every directory or file that belongs to the trusted harness, never to an agent workspace."""
    framework = config.framework
    candidates: list[Path | None] = [
        HARNESS_HOME,                         # the harness code and its shipped pack
        framework.root,                       # the configuration in use (may be a copy of HARNESS_HOME/config)
        *config.sources.values(),             # every configuration file
        framework.contracts_dir,
        framework.artifacts_dir,
        framework.storage.sqlite_path,
        *(plugin.path for plugin in config.plugins),   # plugin agents, skills and security definitions
        Path(sys.prefix),                     # the Python environment the harness runs in
        claude_config_dir(),                  # CLI credentials, settings and transcripts
        *extra,
    ]
    try:
        import claude_agent_sdk
        candidates.append(Path(claude_agent_sdk.__file__).parent)   # the SDK and its bundled CLI
    except ImportError:                                            # pragma: no cover - the SDK is a dependency
        pass
    roots: dict[str, Path] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        path = _canonical_or_lexical(Path(candidate))
        roots.setdefault(os.path.normcase(str(path)), path)
    return tuple(roots.values())


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_workspace(value: str | Path, protected: Iterable[Path]) -> Path:
    """The canonical workspace directory, or :class:`WorkspaceError` if it overlaps the harness."""
    path = canonical(value)
    if not path.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {path}")
    if path.parent == path:
        raise WorkspaceError(f"a filesystem root cannot be a workspace: {path}")
    for root in protected:
        if _within(path, root):
            raise WorkspaceError(f"workspace {path} lies inside the trusted harness area {root}; "
                                 "point the agent at the project directory instead")
        if _within(root, path):
            raise WorkspaceError(f"workspace {path} contains the trusted harness area {root} "
                                 "(harness, configuration, database, plugins or credentials); "
                                 "point the agent at the project directory instead")
    return path
