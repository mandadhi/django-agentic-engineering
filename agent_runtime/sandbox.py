"""Where Bash may run, per platform (SEC-02). Command filtering is never treated as isolation.

The filesystem and network boundary for Bash is the Claude Code CLI's OS sandbox
(``ClaudeAgentOptions.sandbox``), which exists on macOS (Seatbelt, ``/usr/bin/sandbox-exec``) and
on Linux (bubblewrap, with ``socat`` for its network proxy). The CLI confines a sandboxed command's
writes to the working directory and the additional directories (here: the workspace and the run's
output directory); reads are limited by ``Read(...)`` deny rules, network by the allowed domains.

The harness configures it strictly:

* ``failIfUnavailable``: if the sandbox cannot start, the CLI refuses the command instead of
  running it unsandboxed (verified to exist in CLI 2.1.317: ``sandbox_required_unavailable``);
* ``allowUnsandboxedCommands: false`` and no ``excludedCommands``: no escape hatch;
* ``autoAllowBashIfSandboxed: false``: the harness's own permissions and PreToolUse guard still
  decide every call.

Where no sandbox is available (Windows; Linux without bubblewrap or socat) Bash is not run
unrestricted. security-config ``shell.without_sandbox`` decides: ``disable`` (default) removes Bash
from the session, ``approve`` sends every Bash call to the approver (the default approver denies).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable

from core.config import ShellPolicy

SANDBOXED = "sandboxed"     # Bash runs inside the OS sandbox
DISABLED = "disabled"       # Bash is removed from the session
APPROVAL = "approval"       # Bash runs unsandboxed, every call approved by a human

#: PreToolUse approval reason when Bash runs without a sandbox (``without_sandbox: approve``).
UNSANDBOXED_RULE_ID = "unsandboxed-shell"


@dataclass(frozen=True)
class ShellIsolation:
    mode: str
    reason: str
    sandbox: dict[str, Any] | None = None       # ClaudeAgentOptions.sandbox when mode == SANDBOXED

    def to_metadata(self) -> dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason}


def sandbox_support(platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which,
                    exists: Callable[[str], bool] = os.path.exists) -> tuple[bool, str]:
    """(available, explanation) for the CLI's OS sandbox on this machine."""
    if platform == "darwin":
        if exists("/usr/bin/sandbox-exec"):
            return True, "macOS Seatbelt sandbox"
        return False, "macOS without /usr/bin/sandbox-exec"
    if platform.startswith("linux"):
        missing = [tool for tool in ("bwrap", "socat") if not which(tool)]
        if missing:
            return False, f"Linux sandbox needs {' and '.join(missing)} on PATH (bubblewrap, socat)"
        return True, "Linux bubblewrap sandbox"
    return False, f"the Claude Code CLI has no OS sandbox for Bash on {platform}"


def shell_isolation(policy: ShellPolicy, *, platform: str = sys.platform,
                    which: Callable[[str], str | None] = shutil.which,
                    exists: Callable[[str], bool] = os.path.exists) -> ShellIsolation:
    available, why = sandbox_support(platform, which, exists)
    if available:
        return ShellIsolation(SANDBOXED, why, {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "network": {"allowedDomains": list(policy.allowed_domains)},
        })
    if policy.without_sandbox == "approve":
        return ShellIsolation(APPROVAL, f"{why}; every Bash call needs human approval (shell.without_sandbox)")
    return ShellIsolation(DISABLED, f"{why}; Bash is disabled (shell.without_sandbox: disable)")
