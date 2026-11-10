"""Harness-level guard rails driven by security-config.yaml.

* :class:`Redactor` masks secret patterns in text (logs, persisted error messages).
  At run time the PostToolUse hook applies it to tool output (``updatedToolOutput``).
* :class:`SecurityPolicy` answers per-agent tool questions and checks denied paths.
  The tool policy only removes tools: an agent's ``tools`` frontmatter is the
  baseline (``None`` means the agent inherits every tool), and the agent's own
  ``disallowedTools``, the global ``tools.deny`` and per-agent denies are taken
  away from it. :meth:`SecurityPolicy.denied_tools` is the value the Agent SDK
  expects in ``disallowed_tools`` / ``AgentDefinition.disallowedTools``, and
  :meth:`SecurityPolicy.is_tool_allowed` is the check a PreToolUse hook runs
  using the hook input's ``agent_type``.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Protocol

from .config import AgentSpec, SecurityConfig


class _AgentLookup(Protocol):
    def get(self, agent_id: str) -> AgentSpec: ...


class Redactor:
    def __init__(self, config: SecurityConfig) -> None:
        self._enabled = config.redaction_enabled
        self._replacement = config.redaction_replacement
        self._patterns = [(p.id, re.compile(p.regex)) for p in config.secret_patterns]

    def find(self, text: str) -> list[str]:
        """Ids of the secret patterns present in ``text``."""
        return [pattern_id for pattern_id, regex in self._patterns if regex.search(text)]

    def redact(self, text: str) -> str:
        if not self._enabled:
            return text
        for _, regex in self._patterns:
            text = regex.sub(self._replacement, text)
        return text

    __call__ = redact


class SecurityPolicy:
    def __init__(self, config: SecurityConfig, agents: _AgentLookup) -> None:
        self._config = config
        self._agents = agents
        self._overrides = {o.agent_id: o for o in config.tool_overrides}

    def denied_tools(self, agent_id: str) -> frozenset[str]:
        """Everything the agent must not use: its own disallowedTools + global + per-agent denies."""
        agent = self._agents.get(agent_id)
        denied = set(agent.disallowed_tools) | set(self._config.denied_tools)
        override = self._overrides.get(agent_id)
        if override:
            denied |= set(override.deny)
        return frozenset(denied)

    def effective_tools(self, agent_id: str) -> tuple[str, ...] | None:
        """The agent's declared tools minus everything denied. ``None`` = not declared (inherits all
        tools; :meth:`denied_tools` still applies). Never contains a tool the agent did not declare."""
        agent = self._agents.get(agent_id)
        if agent.tools is None:
            return None
        denied = self.denied_tools(agent_id)
        return tuple(tool for tool in agent.tools if tool not in denied)

    def is_tool_allowed(self, agent_id: str, tool: str) -> bool:
        agent = self._agents.get(agent_id)
        if agent.tools is not None and tool not in agent.tools:
            return False
        return tool not in self.denied_tools(agent_id)

    def is_path_denied(self, path: str) -> bool:
        """True when ``path`` (any separator) matches a deny glob. ``**/`` also matches at the root."""
        posix = PurePosixPath(path.replace("\\", "/")).as_posix()
        for pattern in self._config.denied_paths:
            if fnmatch(posix, pattern):
                return True
            if pattern.startswith("**/") and fnmatch(posix, pattern[3:]):
                return True
        return False
