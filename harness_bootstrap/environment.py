"""Read-only environment checks for setup, status and doctor. Nothing here installs or writes anything.

Each check returns a :class:`Check` with ``ok``, ``warn`` or ``fail`` and, when it is not ``ok``, the
action that fixes it. Credentials are reported by *name* only, never by value.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MIN_PYTHON = (3, 11)
#: claude-agent-sdk bundles the Claude Code CLI (~325 MB installed); the rest is small.
REQUIRED_FREE_BYTES = 600 * 1024 * 1024
RECOMMENDED_FREE_BYTES = 1024 * 1024 * 1024
#: Longest file path inside the harness virtual environment, relative to .venv/ (measured with the
#: pinned requirements on Windows: Lib/site-packages/jsonschema/tests/typing/__pycache__/... = 115).
VENV_DEEPEST = 115
#: Windows MAX_PATH (260 including the terminating NUL) when long paths are not enabled.
WINDOWS_MAX_PATH = 259
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

_PROBE = ("import json, sys; print(json.dumps({'version': list(sys.version_info[:3]), 'executable': sys.executable, "
          "'prefix': sys.prefix, 'base_prefix': getattr(sys, 'base_prefix', sys.prefix)}))")


@dataclass
class Check:
    name: str
    status: str                 # ok | warn | fail
    detail: str
    remediation: str = ""

    def to_dict(self) -> Dict[str, str]:
        doc = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.remediation:
            doc["remediation"] = self.remediation
        return doc


class Runner:
    """Subprocess seam (tests substitute it). Never uses a shell; arguments are a list."""

    def run(self, argv: Sequence[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
            capture: bool = True, timeout: Optional[float] = 120) -> subprocess.CompletedProcess:
        return subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=env,
                              capture_output=capture, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, stdin=subprocess.DEVNULL)


def python_info(executable: Sequence[str], runner: Runner) -> Optional[Dict]:
    try:
        result = runner.run([*executable, "-I", "-c", _PROBE], timeout=30)   # -I: nothing from the cwd
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    info["version"] = tuple(info["version"])
    return info


def _candidates(explicit: Optional[str]) -> List[Sequence[str]]:
    if explicit:
        return [[explicit]]
    found: List[Sequence[str]] = []
    if sys.version_info[:2] >= MIN_PYTHON:
        found.append([sys.executable])
    if os.name == "nt" and shutil.which("py"):
        found += [["py", f"-3.{minor}"] for minor in (13, 12, 11)]
    for name in ("python3.13", "python3.12", "python3.11", "python3", "python"):
        path = shutil.which(name)
        if path:
            found.append([path])
    return found


def find_python(explicit: Optional[str], runner: Runner) -> Tuple[Optional[Dict], Check]:
    """The interpreter that will create the harness environment (3.11+), and the check result."""
    seen: List[str] = []
    for candidate in _candidates(explicit):
        info = python_info(candidate, runner)
        if info is None:
            continue
        version = ".".join(map(str, info["version"]))
        if tuple(info["version"][:2]) >= MIN_PYTHON:
            return info, Check("python", "ok", f"Python {version} at {info['executable']}")
        seen.append(f"{version} ({info['executable']})")
    need = ".".join(map(str, MIN_PYTHON))
    if explicit:
        detail = (f"{explicit} is Python {seen[0]}" if seen else f"{explicit} does not run")
        return None, Check("python", "fail", detail + f"; the harness needs Python {need}+",
                           f"pass an interpreter of Python {need} or newer with --python")
    return None, Check("python", "fail", f"no Python {need}+ found" + (f" (found {', '.join(seen)})" if seen else ""),
                       f"install Python {need} or newer (python.org, your package manager, or `winget install "
                       f"Python.Python.3.13` on Windows), then run setup again; or name one with --python")


def check_venv_support(python: Sequence[str], runner: Runner) -> List[Check]:
    checks: List[Check] = []
    result = runner.run([*python, "-I", "-c", "import venv, ensurepip"], timeout=30)
    if result.returncode != 0:
        checks.append(Check("venv", "fail", "this Python cannot create virtual environments with pip "
                            "(the venv or ensurepip module is missing)",
                            "install your distribution's venv package (for example `sudo apt install python3-venv`) "
                            "or use a python.org build"))
    else:
        checks.append(Check("venv", "ok", "venv and ensurepip available"))
    pip = runner.run([*python, "-I", "-m", "pip", "--version"], timeout=60)
    if pip.returncode == 0:
        checks.append(Check("pip", "ok", pip.stdout.strip().split(" from ")[0]))
    elif result.returncode == 0:
        checks.append(Check("pip", "ok", "pip is not installed for this interpreter; the new virtual environment "
                            "gets its own pip from ensurepip"))
    else:
        checks.append(Check("pip", "fail", "pip is unavailable and cannot be bootstrapped",
                            "install pip and the venv module for this interpreter, or choose another with --python"))
    return checks


def check_disk(path: Path, needs_environment: bool) -> Check:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(str(probe)).free
    except OSError as exc:
        return Check("disk", "warn", f"free space could not be measured: {exc}")
    gib = free / (1024 ** 3)
    if not needs_environment:
        return Check("disk", "ok", f"{gib:.1f} GiB free")
    if free < REQUIRED_FREE_BYTES:
        return Check("disk", "fail", f"only {gib:.2f} GiB free; the harness environment needs about 0.6 GiB",
                     "free disk space on this volume and run setup again")
    if free < RECOMMENDED_FREE_BYTES:
        return Check("disk", "warn", f"{gib:.2f} GiB free; the harness environment uses about 0.6 GiB")
    return Check("disk", "ok", f"{gib:.1f} GiB free")


def _windows_long_paths() -> Optional[bool]:
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except OSError:
        return None


def check_paths(project: Path, deepest_relative: Optional[int] = None, *, windows: Optional[bool] = None,
                long_paths: Optional[bool] = None) -> List[Check]:
    """Write permission and, on Windows, whether the deepest file setup creates fits MAX_PATH.

    ``deepest_relative``: the longest path below ``.django-engineering/`` (distribution.deepest_path).
    """
    checks: List[Check] = []
    target = project / ".django-engineering"
    writable_dir = target if target.exists() else project
    if not os.access(str(writable_dir), os.W_OK):
        checks.append(Check("permissions", "fail", f"{writable_dir} is not writable by this user",
                            "run Claude Code as a user who can write to the project, or fix the directory permissions"))
    else:
        checks.append(Check("permissions", "ok", f"{writable_dir} is writable"))
    windows = os.name == "nt" if windows is None else windows
    if windows:
        deepest = len(str(target)) + 1 + (deepest_relative or (len(".venv/") + VENV_DEEPEST))
        enabled = _windows_long_paths() if long_paths is None else long_paths
        if enabled:
            checks.append(Check("path-length", "ok", f"Windows long paths enabled (deepest path {deepest} characters)"))
        elif deepest > WINDOWS_MAX_PATH:
            checks.append(Check("path-length", "fail", f"the deepest file setup creates would be {deepest} characters "
                                f"long; Windows limits paths to {WINDOWS_MAX_PATH} unless long paths are enabled",
                                f"enable Windows long paths (LongPathsEnabled), or use a project whose path is at least "
                                f"{deepest - WINDOWS_MAX_PATH} characters shorter"))
        elif deepest > WINDOWS_MAX_PATH - 15:
            checks.append(Check("path-length", "warn", f"the deepest file setup creates is {deepest} characters, close "
                                f"to the Windows limit of {WINDOWS_MAX_PATH}", "consider enabling Windows long paths"))
        else:
            checks.append(Check("path-length", "ok", f"deepest path {deepest} characters (limit {WINDOWS_MAX_PATH})"))
    return checks


def check_credentials() -> Check:
    """Names only. A Claude Code login (credentials file or OS keychain) needs no variable."""
    present = [name for name in CREDENTIAL_VARIABLES if os.environ.get(name)]
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    login_file = (config_dir / ".credentials.json").is_file()
    if present:
        return Check("credentials", "ok", "credential variable set: " + ", ".join(present) + " (value not shown)")
    if login_file:
        return Check("credentials", "ok", f"Claude Code login found in {config_dir}")
    return Check("credentials", "warn", "no credential variable and no login file found (a login kept in the OS "
                 "keychain cannot be checked from here); agent execution needs one, setup and validation do not",
                 "run `claude auth status` in a terminal; log in with `claude` or set ANTHROPIC_API_KEY")


def check_shell_sandbox() -> Check:
    """Informational: where harness agents may run Bash (agent_runtime.sandbox decides at run time)."""
    if sys.platform == "darwin":
        ok = Path("/usr/bin/sandbox-exec").exists()
        return Check("shell-sandbox", "ok" if ok else "warn",
                     "macOS sandbox available: harness agents run Bash inside it" if ok else
                     "sandbox-exec not found: harness agents get no Bash (security-config shell.without_sandbox)")
    if sys.platform.startswith("linux"):
        ok = bool(shutil.which("bwrap") and shutil.which("socat"))
        return Check("shell-sandbox", "ok" if ok else "warn",
                     "bubblewrap and socat available: harness agents run Bash in the OS sandbox" if ok else
                     "bubblewrap or socat missing: harness agents get no Bash (security-config shell.without_sandbox)",
                     "" if ok else "install bubblewrap and socat to let harness agents run tests and commands")
    return Check("shell-sandbox", "warn", "no OS sandbox on this platform: harness agents get no Bash tool "
                 "(security-config shell.without_sandbox: disable); file tools still work",
                 "use the standalone plugin for work that needs shell commands, or run the harness on macOS/Linux")
