"""The environment the Claude Code CLI, and every command it runs, receives (SEC-03).

The Agent SDK starts the CLI with ``{**os.environ, **ClaudeAgentOptions.env}`` (claude-agent-sdk
0.2.177, ``SubprocessCLITransport.connect``): it always inherits the operator's environment, and
``options.env`` can only add or override. So the harness passes an ``env`` that overrides *every*
inherited variable: variables on the allow-list keep their value, credentials for the CLI's own
authentication are passed only when configured, and every other variable is set to the empty
string. The CLI and each Bash child it starts therefore see no value of an unlisted variable.

Limits (documented, tested): the *names* of unlisted variables remain visible with empty values,
because the SDK offers no way to remove an inherited variable. Values are what matters: cloud keys,
database URLs and tokens never reach the agent. The CLI's authentication credentials do reach the
CLI process; the CLI is asked to strip them from its subprocesses (``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB``,
a CLI feature not verified offline), and independently the guard denies any command that expands a
sensitive variable or dumps the environment (:mod:`agent_runtime.security_hooks`). A Claude Code
login (``~/.claude``, outside every workspace) needs no credential variable at all.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

from core.config import EnvironmentPolicy

#: Used when security-config has no ``environment`` section: what the CLI needs on every platform.
DEFAULT_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH",
)
DEFAULT_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

#: Variables the SDK itself sets or reads around the merge; the harness never blanks them.
SDK_MANAGED = frozenset({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION",
                         "CLAUDE_AGENT_SDK_CLIENT_APP", "CLAUDE_CODE_SDK_READS_SESSION_STATE",
                         "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "PWD", "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"})
#: Asks the CLI to remove its authentication credentials from the environment of Bash, hooks and MCP children.
SCRUB_FLAG = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"


def _key(name: str) -> str:
    return name.upper() if os.name == "nt" else name     # Windows variable names are case-insensitive


def session_environment(policy: EnvironmentPolicy | None, *, fixed: Mapping[str, str] | None = None,
                        source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``ClaudeAgentOptions.env``: allow-listed values, configured credentials, every other variable blank.

    ``fixed`` are harness settings that always win (client app name, entrypoint).
    """
    source = os.environ if source is None else source
    allow: Iterable[str] = policy.allow if policy is not None else DEFAULT_ALLOW
    credentials: Iterable[str] = policy.credentials if policy is not None else DEFAULT_CREDENTIALS
    allowed = {_key(n) for n in allow}
    secrets = {_key(n) for n in credentials}
    managed = {_key(n) for n in SDK_MANAGED}
    env: dict[str, str] = {}
    passed_credential = False
    for name, value in source.items():
        key = _key(name)
        if key in managed:
            continue
        if key in allowed:
            env[name] = value
        elif key in secrets and value:
            env[name] = value
            passed_credential = True
        else:
            env[name] = ""                                  # inherited by the SDK otherwise: override it
    if passed_credential:
        env[SCRUB_FLAG] = "1"
    env.update(fixed or {})
    return env


def forwarded_names(env: Mapping[str, str]) -> list[str]:
    """Names whose value reaches the CLI (for the audit log; never values)."""
    return sorted(name for name, value in env.items() if value)
