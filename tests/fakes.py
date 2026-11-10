"""A fake Agent SDK adapter: no subprocess, no API call.

It yields the SDK's real message dataclasses and, for tool calls, invokes the hook
callbacks actually registered in ``ClaudeAgentOptions.hooks`` with the hook input shape
declared by the installed SDK (``PreToolUseHookInput`` / ``PostToolUseHookInput``).
The ``init`` frame is derived from the options the way the CLI reports them
(observed with CLI 2.1.317): plugin names from each plugin.json, skills namespaced
``<plugin>:<skill>``, the session's tool list.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock
from claude_agent_sdk import ResultError

Scenario = Callable[["FakeSession"], AsyncIterator[Any]]


def plugin_name(path: str) -> str:
    return json.loads((Path(path) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]


@dataclass
class FakeSession:
    prompt: str
    options: ClaudeAgentOptions
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    decisions: list[tuple[str, dict]] = field(default_factory=list)

    def init(self, **overrides: Any) -> SystemMessage:
        o = self.options
        data = {
            "type": "system", "subtype": "init", "session_id": self.session_id, "cwd": str(o.cwd),
            "model": "claude-test", "permissionMode": o.permission_mode, "claude_code_version": "fake",
            "tools": list(o.tools) if isinstance(o.tools, list) else ["Read", "Write", "Bash"],
            "agents": ["general-purpose", *(o.agents or {})],
            "plugins": [{"name": plugin_name(p["path"]), "path": p["path"], "source": "x@inline"} for p in o.plugins],
            "skills": list(o.skills or []),
        }
        data.update(overrides)
        return SystemMessage(subtype="init", data=data)

    def text(self, text: str) -> AssistantMessage:
        return AssistantMessage(content=[TextBlock(text=text)], model="claude-test", session_id=self.session_id)

    def result(self, *, structured: Any = None, text: str | None = None, is_error: bool = False,
               subtype: str = "success", cost: float = 0.0123, **extra: Any) -> ResultMessage:
        return ResultMessage(subtype=subtype, duration_ms=1500, duration_api_ms=1200, is_error=is_error, num_turns=3,
                             session_id=self.session_id, total_cost_usd=cost,
                             usage={"input_tokens": 1200, "output_tokens": 300}, result=text,
                             structured_output=structured, **extra)

    async def tool(self, name: str, tool_input: dict, response: Any = None, *,
                   agent_type: str | None = None) -> dict:
        """Run the registered PreToolUse hooks (and PostToolUse when allowed). Returns the decision output."""
        tool_use_id = f"toolu_{uuid.uuid4().hex[:8]}"
        base = {"session_id": self.session_id, "transcript_path": "t.jsonl", "cwd": str(self.options.cwd),
                "tool_name": name, "tool_input": tool_input, "tool_use_id": tool_use_id}
        if agent_type is not None:
            base["agent_type"] = agent_type
        decision: dict = {}
        for matcher in (self.options.hooks or {}).get("PreToolUse", []):
            for hook in matcher.hooks:
                decision = await hook({**base, "hook_event_name": "PreToolUse"}, tool_use_id, {"signal": None})
        self.decisions.append((name, decision))
        denied = decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        if not denied and response is not None:
            for matcher in (self.options.hooks or {}).get("PostToolUse", []):
                for hook in matcher.hooks:
                    post = await hook({**base, "hook_event_name": "PostToolUse", "tool_response": response},
                                      tool_use_id, {"signal": None})
                    decision = {**decision, "post": post}
        return decision


class FakeSDKAdapter:
    def __init__(self, *scenarios: Scenario, sessions: set[str] | None = None) -> None:
        self._scenarios = list(scenarios)
        self.sessions_on_disk = sessions if sessions is not None else set()
        self.calls: list[FakeSession] = []

    def stream(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        scenario = self._scenarios.pop(0) if len(self._scenarios) > 1 else self._scenarios[0]
        session = FakeSession(prompt, options, session_id=options.resume or str(uuid.uuid4()))
        self.calls.append(session)
        return scenario(session)

    def session_exists(self, session_id: str, cwd: str) -> bool:
        return session_id in self.sessions_on_disk


# ---------------------------------------------------------------------- AgentResult builders


def agent_result(session: FakeSession, *, status: str = "succeeded", outputs: dict[str, str] | None = None,
                 **extra: Any) -> dict:
    """An AgentResult the way an agent would return it: identity + inline output content."""
    task_id = _between(session.prompt, "Task id: `", "`")
    agent_id = _between(session.prompt, "- Agent: `", "`")
    names = outputs if outputs is not None else {n: f"# {n}\n\nFindings." for n in expected_output_names(session)}
    result = {"schema_version": "1.0", "kind": "AgentResult", "task_id": task_id, "agent_id": agent_id,
              "status": status, "summary": "done",
              "outputs": [{"name": n, "artifact": {"uri": f"inline:{n}"}} for n in names],
              "extensions": {"dae.output_content": names}}
    result.update(extra)
    return result


def expected_output_names(session: FakeSession) -> list[str]:
    block = session.prompt.split("### Expected outputs", 1)[1].split("###", 1)[0]
    return [line.split("`")[1] for line in block.splitlines() if line.startswith("- `")]


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


# ---------------------------------------------------------------------- canned scenarios


def succeed(**result_kwargs: Any) -> Scenario:
    async def scenario(s: FakeSession):
        yield s.init()
        yield s.text("Working on it.")
        yield s.result(structured=agent_result(s, **result_kwargs))
    return scenario


def tool_then_succeed(name: str, tool_input: dict, response: Any = None) -> Scenario:
    async def scenario(s: FakeSession):
        yield s.init()
        await s.tool(name, tool_input, response)
        yield s.result(structured=agent_result(s))
    return scenario


def error_result(subtype: str = "error_during_execution", **kwargs: Any) -> Scenario:
    async def scenario(s: FakeSession):
        yield s.init()
        yield s.result(is_error=True, subtype=subtype, **kwargs)
        raise ResultError("Claude Code returned an error result", {"subtype": subtype, "session_id": s.session_id,
                                                                    **{k: v for k, v in kwargs.items()
                                                                       if k in ("api_error_status", "terminal_reason")}},
                          exit_code=1)
    return scenario


def hang(seconds: float = 30.0, *, after_init: bool = True) -> Scenario:
    async def scenario(s: FakeSession):
        if after_init:
            yield s.init()
        await asyncio.sleep(seconds)
        yield s.result(structured=agent_result(s))
    return scenario


def crash_after_init() -> Scenario:
    """The process dies mid-run: the stream raises after the session started."""
    async def scenario(s: FakeSession):
        yield s.init()
        raise KeyboardInterrupt  # stands in for the process being killed
    return scenario
