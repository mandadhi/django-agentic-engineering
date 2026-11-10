"""The only module that calls the Claude Agent SDK.

:class:`SDKAdapter` is the seam the runner depends on: one ``stream()`` call is one SDK
``query()`` — one workflow stage. The SDK runs the agent loop, tools, skills, hooks
and session persistence; the adapter adds nothing to that loop. Tests substitute a
fake adapter that yields real SDK message dataclasses and drives the hooks found in
the options, so no API call is needed.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from claude_agent_sdk import ClaudeAgentOptions, get_session_info, get_session_messages, query


class SDKAdapter(Protocol):
    def stream(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        """Run one query and yield its messages (SystemMessage, AssistantMessage, ResultMessage, ...)."""

    def session_exists(self, session_id: str, cwd: str) -> bool:
        """True when the SDK can resume ``session_id`` for working directory ``cwd``."""


class ClaudeSDKAdapter:
    def stream(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        return query(prompt=prompt, options=options)

    def session_exists(self, session_id: str, cwd: str) -> bool:
        try:
            if get_session_info(session_id, directory=cwd) is not None:
                return True
            return bool(get_session_messages(session_id, directory=cwd, limit=1))
        except Exception:  # noqa: BLE001 - unreadable transcript = cannot resume; start fresh
            return False
