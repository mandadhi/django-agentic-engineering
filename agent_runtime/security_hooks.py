"""PreToolUse / PostToolUse guard that enforces security-config on a live SDK session.

Native SDK mechanisms do most of the work (see :mod:`agent_runtime.options_factory`):
``tools`` / ``allowed_tools`` / ``disallowed_tools``, ``permission_mode="dontAsk"``,
``permissions.deny`` path rules and, where the platform has one, the CLI's OS sandbox for Bash
(:mod:`agent_runtime.sandbox`). This guard adds only what they cannot express:

PreToolUse (every tool, ``matcher=None``)
    * re-checks the tool against :class:`core.security.SecurityPolicy` (defence in depth);
    * rejects a call attributed to a different agent (``agent_type`` in the hook input);
    * file tools: denied-path globs; reads only inside the workspace / artifact dir / the agent's
      plugin read roots, writes only inside the workspace / artifact dir; nothing inside the
      trusted harness area (:mod:`agent_runtime.workspace`) except those explicit roots; Glob
      patterns cannot climb out with ``..`` or an absolute prefix. Every path is resolved
      (symlinks, junctions, ``..``) before it is compared, and a resolution error denies.
    * Bash: the command is parsed (:mod:`core.shell_syntax`), never pattern-matched alone. Every
      simple command, including those inside substitutions, ``sh -c``, ``eval``, wrappers and
      ``find -exec``, is checked: ``commands.deny`` rules, environment dumps and expansions of
      sensitive variables, every redirection form (``N>``, ``&>``, ``>|``, ...), path operands and
      the files a glob or brace pattern expands to (inside the workspace and artifact dir only),
      ``commands.require_approval`` rules, destructive programs (``rm``, ``find -delete``, ...).
      Anything the parser cannot analyse is denied. Where the platform has no OS sandbox, Bash is
      disabled or approval-gated (``shell_mode``); filtering is never presented as isolation.
    * the agent's write mode (security-config ``tools.agents[].write_mode`` and/or the plugin's
      ``agent_policy``, the stricter applies; see :mod:`core.plugin_policy`). Modes only remove
      capabilities. Each mode grants a set of capabilities (security-config ``write_modes``, SEC-06):
      every tool and every simple shell command is classified by its effect (``read``, ``write``,
      ``delete``, ``execute``, ``network``, ``unknown``; :func:`core.shell_syntax.classify`), and an
      effect the mode does not grant is denied (``otherwise: deny``) or needs the approver
      (``otherwise: approve``). Unrecognised tools and commands are ``unknown``: default deny.
    * approvals (:class:`ApprovalRequest`) show the complete operation, the working directory and the
      agent, and are bound to a digest of the exact call: a decision for anything else, a reused or
      expired decision, or a call that changed while waiting is refused.
PostToolUse
    * masks secret patterns in the tool output before the model sees it
      (``updatedToolOutput``, same shape as the original so the CLI accepts it).

:class:`HookTripwire` watches every session's message stream: a tool result for a call this guard
never saw aborts the run. It is a *detector*, not enforcement: the call it reports has already run.
Preventive enforcement is the SDK hook registration itself (``on_failure="block"``).

Every decision is written to the execution audit log, with the input summarised and redacted. Any
exception inside the guard denies the call (fail closed).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from claude_agent_sdk import (AssistantMessage, HookMatcher, ResultMessage, SystemMessage, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from core.config import CommandRule, ModeCapabilities, SecurityConfig, ShellPolicy
from core.log import get_logger
from core.plugin_policy import AgentWriteMode
from core.security import Redactor, SecurityPolicy
from core.shell_syntax import EXECUTE, NETWORK, READ, UNKNOWN, WRITE, Facts, analyze, expand_word

from .exceptions import SecurityViolationError
from .sandbox import APPROVAL, DISABLED, SANDBOXED, UNSANDBOXED_RULE_ID

log = get_logger(__name__)

#: Tools the CLI itself injects for harness features, not capabilities an agent uses on
#: the codebase. ``StructuredOutput`` carries the AgentResult when --json-schema is set.
HARNESS_TOOLS = frozenset({"StructuredOutput"})

READ_PATH_KEYS = {"Read": ("file_path",), "Grep": ("path",), "Glob": ("path",), "NotebookRead": ("notebook_path",),
                  "LS": ("path",)}
WRITE_PATH_KEYS = {"Write": ("file_path",), "Edit": ("file_path",), "MultiEdit": ("file_path",),
                   "NotebookEdit": ("notebook_path",)}
#: Effect class of each non-shell tool (SEC-06). A tool not listed is ``unknown``. Bash is classified
#: per command; HARNESS_TOOLS are the harness's own and exempt.
TOOL_EFFECTS = {**{tool: READ for tool in (*READ_PATH_KEYS, "TodoWrite")},
                **{tool: WRITE for tool in WRITE_PATH_KEYS},
                "WebFetch": NETWORK, "WebSearch": NETWORK, "Task": EXECUTE, "Agent": EXECUTE}
#: Used when a write mode has no capability entry: read only, everything else refused (fail closed).
_FALLBACK_CAPABILITIES = ModeCapabilities(frozenset({READ}), "deny")
_SUMMARY_CHARS = 300                 # routine audit summaries only; approvals always show everything
_AUDIT_OPERATION_CHARS = 32_000      # stored with approval events (the digest covers the full call)

#: Approval reasons raised by the guard itself (not regexes: never matched against text).
CONFIRM_WRITE = CommandRule("confirm-writes", "", "this agent changes the codebase only with explicit approval")
DESTRUCTIVE = CommandRule("destructive-command", "", "deletes or destroys files")
OPAQUE_CODE = CommandRule("opaque-code", "", "runs inline code the guard cannot analyse")
UNSANDBOXED = CommandRule(UNSANDBOXED_RULE_ID, "", "no OS sandbox on this platform: every shell command is "
                                                    "approved individually")

AuditSink = Callable[..., None]     # (event, message, *, level="INFO", **data)


# ---------------------------------------------------------------------- approval


def _visible(text: str) -> str:
    """Show control characters instead of letting them rewrite the terminal (ANSI, carriage return)."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in text)


@dataclass(frozen=True)
class ApprovalRequest:
    agent_id: str
    tool_name: str
    summary: str                 # redacted one-line description (kept for compatibility; see ``operation``)
    rules: tuple[str, ...]       # ids of the rules that require approval
    reason: str
    execution_id: str | None = None
    stage_id: str | None = None
    session_agent: str = ""      # the name the SDK session runs the agent under (dae-<id>)
    cwd: str = ""                # the workspace the command runs in
    operation: str = ""          # the COMPLETE operation, redacted, never truncated
    tool_use_id: str | None = None
    digest: str = ""             # binds a decision to this exact call and context
    expires_at: float = 0.0      # time.time() after which a decision is refused

    def decide(self, approved: bool, approver: str, reason: str = "") -> "ApprovalDecision":
        """The decision for *this* request (bound to its digest). Approvers must answer through it."""
        return ApprovalDecision(approved, approver, reason, binding=self.digest)

    def render(self) -> str:
        """Everything a human needs to review, in full."""
        lines = [f"[approval required] {self.tool_name} by agent '{self.agent_id}'"
                 + (f" (session agent '{self.session_agent}')" if self.session_agent else ""),
                 f"  working directory: {self.cwd or '-'}",
                 f"  execution: {self.execution_id or '-'}  stage: {self.stage_id or '-'}  "
                 f"call: {self.tool_use_id or '-'}",
                 f"  rules: {', '.join(self.rules)} - {self.reason}",
                 f"  approval id: {self.digest[:16]}",
                 "  ---- operation (complete) ----"]
        lines += ["  " + line for line in _visible(self.operation or self.summary).split("\n")]
        lines.append("  ---- end of operation ----")
        return "\n".join(lines)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    approver: str
    reason: str = ""
    binding: str | None = None   # the request digest this decision answers (see ApprovalRequest.decide)


class Approver(Protocol):
    def __call__(self, request: ApprovalRequest) -> Awaitable[ApprovalDecision]: ...


class DenyApprover:
    """Default: no human is available, so high-risk operations are denied."""

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        return request.decide(False, "policy:non-interactive",
                              "approval required and no interactive approver is configured")


class ConsoleApprover:
    """Asks a human at the terminal (``--approve interactive``). Anything but 'y'/'yes' denies.

    The complete operation is shown (no truncation), with the working directory, the agent and the
    execution context; control characters are made visible.
    """

    def __init__(self, approver_name: str = "console", stream=None, reader: Callable[[], str] | None = None) -> None:
        self._name = approver_name
        self._stream = stream or sys.stderr
        self._reader = reader or sys.stdin.readline

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self._stream.write("\n" + request.render() + "\nAllow this one call? [y/N] ")
        self._stream.flush()
        answer = await asyncio.to_thread(self._reader)
        approved = answer.strip().lower() in ("y", "yes")
        return request.decide(approved, self._name, "approved at console" if approved else "denied at console")


# ---------------------------------------------------------------------- helpers


def deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _glob_match(path: str, patterns: Iterable[str]) -> bool:
    """fnmatch with the harness's convention: a leading ``**/`` also matches at the root."""
    return any(fnmatch(path, p) or (p.startswith("**/") and fnmatch(path, p[3:])) for p in patterns)


def _tokens(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    tokens: list[str] = []
    for part in parts:
        part = part.strip("'\";,()")
        if not part:
            continue
        tokens.append(part)
        if "=" in part:                              # --env-file=.env, KEY=path
            tokens.append(part.split("=", 1)[1])
    return tokens


def redact_structure(value: Any, redactor: Redactor) -> tuple[Any, set[str]]:
    """Redact every string inside ``value`` keeping its shape. Returns (value, pattern ids found)."""
    if isinstance(value, str):
        found = redactor.find(value)
        return (redactor.redact(value), set(found)) if found else (value, set())
    if isinstance(value, list):
        items, found = [], set()
        for item in value:
            new, ids = redact_structure(item, redactor)
            items.append(new)
            found |= ids
        return items, found
    if isinstance(value, dict):
        out, found = {}, set()
        for key, item in value.items():
            new, ids = redact_structure(item, redactor)
            out[key] = new
            found |= ids
        return out, found
    return value, set()


# ---------------------------------------------------------------------- guard


class ToolGuard:
    def __init__(self, *, agent_id: str, policy: SecurityPolicy, security: SecurityConfig, redactor: Redactor,
                 workspace: Path, artifact_dir: Path, extra_read_roots: Iterable[Path] = (),
                 approver: Approver | None = None, audit: AuditSink | None = None,
                 execution_id: str | None = None, stage_id: str | None = None,
                 write_mode: AgentWriteMode | None = None, session_agent: str | None = None,
                 protected_roots: Iterable[Path] = (), shell_mode: str | None = None,
                 approval_ttl_seconds: float = 600.0) -> None:
        self.agent_id = agent_id
        self.session_agent = session_agent or agent_id     # the name hook inputs report as agent_type
        self._policy = policy
        self._redactor = redactor
        self._workspace = workspace.resolve()
        self._artifact_dir = artifact_dir.resolve()
        self._write_roots = (self._workspace, self._artifact_dir)
        self._extra_read_roots = tuple(p.resolve() for p in extra_read_roots)
        self._read_roots = (*self._write_roots, *self._extra_read_roots)
        self._protected = tuple(Path(p) for p in protected_roots)
        self._mode = write_mode
        self._caps: ModeCapabilities | None = None if write_mode is None else (
            security.write_modes.get(write_mode.mode) or _FALLBACK_CAPABILITIES)
        self._shell: ShellPolicy = security.shell
        self._shell_mode = shell_mode
        self._sensitive = re.compile(self._shell.sensitive_variables)
        self._read_only_rules = [(r, re.compile(r.regex)) for r in (write_mode.read_only_commands if write_mode else ())]
        self.seen_tool_use_ids: set[str] = set()       # every call this guard was asked about (HookTripwire)
        self._deny_rules = [(r, re.compile(r.regex)) for r in security.denied_commands]
        self._approval_rules = [(r, re.compile(r.regex)) for r in security.approval_commands]
        self._approver: Approver = approver or DenyApprover()
        self._approval_ttl = approval_ttl_seconds
        self._used_approvals: set[str] = set()
        self._audit_sink = audit
        self._execution_id = execution_id
        self._stage_id = stage_id
        self.decisions: list[tuple[str, str, str]] = []     # (tool, decision, reason) for this run

    # ------------------------------------------------------------ wiring

    def hook_matchers(self, timeout_seconds: float) -> dict[str, list[HookMatcher]]:
        """``ClaudeAgentOptions.hooks`` value. ``on_failure="block"``: a failing hook blocks the call."""
        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use], timeout=timeout_seconds,
                                       on_failure="block")],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use], timeout=timeout_seconds,
                                        on_failure="block")],
        }

    # ------------------------------------------------------------ audit

    def _summary(self, tool: str, tool_input: Mapping[str, Any]) -> str:
        if tool == "Bash":
            text = str(tool_input.get("command", ""))
        else:
            keys = ("file_path", "notebook_path", "path", "pattern", "glob", "url", "query", "skill")
            text = " ".join(f"{k}={tool_input[k]}" for k in keys if k in tool_input) or ",".join(sorted(tool_input))
        text = self._redactor.redact(text)
        return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS] + "…"

    def _operation(self, tool: str, tool_input: Mapping[str, Any]) -> str:
        """The complete operation a human approves (redacted, never truncated)."""
        if tool == "Bash":
            text = str(tool_input.get("command", ""))
            if tool_input.get("description"):
                text += f"\n# description given by the agent: {tool_input['description']}"
        elif tool == "Write":
            content = str(tool_input.get("content", ""))
            text = f"write {tool_input.get('file_path')} ({len(content.encode('utf-8'))} bytes):\n{content}"
        elif tool == "Edit":
            text = (f"edit {tool_input.get('file_path')}"
                    + (" (every occurrence)" if tool_input.get("replace_all") else "")
                    + f"\n--- replace:\n{tool_input.get('old_string', '')}\n+++ with:\n{tool_input.get('new_string', '')}")
        else:
            text = json.dumps(dict(tool_input), indent=2, sort_keys=True, default=str)
        return self._redactor.redact(text)

    def _audit(self, event: str, message: str, *, level: str = "INFO", **data: Any) -> None:
        if self._audit_sink is None:
            log.log(20 if level == "INFO" else 30, event, extra={"agent_id": self.agent_id, **data})
            return
        try:
            self._audit_sink(event, message, level=level, **data)
        except Exception:  # noqa: BLE001 - auditing must not turn an allow into a crash loop
            log.exception("audit.write_failed", extra={"event": event})

    def _decide(self, tool: str, decision: str, reason: str, summary: str, *, level: str = "INFO",
                **data: Any) -> None:
        self.decisions.append((tool, decision, reason))
        self._audit(f"tool.{decision}", f"{tool}: {reason}" if reason else tool, level=level, tool=tool,
                    agent_id=self.agent_id, summary=summary, **data)

    # ------------------------------------------------------------ PreToolUse

    async def pre_tool_use(self, hook_input: Mapping[str, Any], tool_use_id: str | None,
                           context: Any) -> dict[str, Any]:
        tool = str(hook_input.get("tool_name", "?"))
        if tool_use_id:
            self.seen_tool_use_ids.add(tool_use_id)
        try:
            tool_input = hook_input.get("tool_input") or {}
            if not isinstance(tool_input, Mapping):
                raise TypeError("tool_input is not an object")
            summary = self._summary(tool, tool_input)
            facts = analyze(str(tool_input.get("command", "")), destructive_programs=self._shell.destructive_programs) \
                if tool == "Bash" else None
            reason = self._denial(hook_input, tool, tool_input, facts) or self._mode_denial(tool, tool_input, facts)
            if reason:
                self._decide(tool, "denied", reason, summary, level="WARNING")
                return deny(reason)
            matched = self._mode_approvals(tool, tool_input, facts)
            if facts is not None:
                matched += self._shell_approvals(str(tool_input.get("command", "")), facts, matched)
            if matched:
                return await self._ask_approval(tool, tool_input, summary, matched, tool_use_id)
            self._decide(tool, "allowed", "", summary)
            return {}            # no opinion: native permission rules still apply
        except Exception as exc:  # noqa: BLE001 - fail closed
            self._decide(tool, "denied", f"security hook error ({type(exc).__name__}); denied", "", level="ERROR")
            return deny("security check could not be completed; the call is denied")

    def _denial(self, hook_input: Mapping[str, Any], tool: str, tool_input: Mapping[str, Any],
                facts: Facts | None) -> str | None:
        agent_type = hook_input.get("agent_type")
        if agent_type is not None and agent_type != self.session_agent:
            return f"call attributed to agent '{agent_type}', this session runs '{self.session_agent}'"
        if tool not in HARNESS_TOOLS and not self._policy.is_tool_allowed(self.agent_id, tool):
            return f"tool '{tool}' is not permitted for agent '{self.agent_id}'"
        for key in READ_PATH_KEYS.get(tool, ()):
            if tool_input.get(key):
                reason = self._check_path(str(tool_input[key]), write=False)
                if reason:
                    return reason
        if tool == "Grep" and tool_input.get("glob") and self._policy.is_path_denied(str(tool_input["glob"])):
            return "glob targets a denied path pattern"
        if tool == "Glob" and tool_input.get("pattern"):
            reason = self._check_glob_pattern(str(tool_input["pattern"]), tool_input.get("path"))
            if reason:
                return reason
        for key in WRITE_PATH_KEYS.get(tool, ()):
            if key not in tool_input:
                return f"{tool} call without {key}"
            reason = self._check_path(str(tool_input[key]), write=True)
            if reason:
                return reason
        if tool == "Bash":
            return self._check_command(str(tool_input.get("command", "")), facts)
        return None

    def _resolve(self, value: str, base: Path | None = None) -> Path:
        """Canonical path (symlinks, junctions, ``..`` resolved). Raises on failure: the caller denies."""
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            path = (base or self._workspace) / path
        return path.resolve()

    def _check_path(self, value: str, *, write: bool, shell: bool = False) -> str | None:
        resolved = self._resolve(value)
        if self._policy.is_path_denied(value) or self._policy.is_path_denied(resolved.as_posix()):
            return "path matches a denied path pattern"
        # Shell commands get the workspace and the artifact dir only: plugin guidance is read with Read.
        roots = self._write_roots if (write or shell) else self._read_roots
        if not _inside(resolved, roots):
            return f"{'write' if write else 'read'} outside the permitted workspace"
        if self._protected and _inside(resolved, self._protected):
            allowed = (self._artifact_dir,) if (write or shell) else (self._artifact_dir, *self._extra_read_roots)
            if not _inside(resolved, allowed):
                return "path is inside the trusted harness area (configuration, database, plugins)"
        return None

    def _check_glob_pattern(self, pattern: str, base: Any) -> str | None:
        """A Glob ``pattern`` may not climb out of its search root (``..`` or an absolute prefix)."""
        parts = re.split(r"[\\/]", pattern)
        literal: list[str] = []
        for part in parts:
            if any(c in part for c in "*?[{"):
                break
            literal.append(part)
        if ".." in parts[len(literal):]:
            return "glob pattern climbs out of the search directory after a wildcard"
        root = self._resolve(str(base)) if base else self._workspace
        prefix = "/".join(literal) if literal else "."
        target = Path(prefix) if Path(prefix).is_absolute() or re.match(r"^[A-Za-z]:", prefix) else root / prefix
        return self._check_path(str(target), write=False)

    def _check_command(self, command: str, facts: Facts | None) -> str | None:
        if self._shell_mode == DISABLED:
            return ("Bash is disabled in this session: no OS sandbox is available on this platform "
                    "(security-config shell.without_sandbox: disable)")
        facts = facts or analyze(command, destructive_programs=self._shell.destructive_programs)
        texts = [command, *(c.normalized for c in facts.commands)]
        for rule, regex in self._deny_rules:
            if any(regex.search(text) for text in texts):
                return f"command blocked by rule '{rule.id}'" + (f": {rule.description}" if rule.description else "")
        if facts.problems:
            return f"command cannot be verified: {facts.problems[0]}"
        if facts.env_dump:
            return "command blocked by rule 'dump-environment': it lists environment variables"
        sensitive = sorted({v for v in facts.variables if self._sensitive.search(v)})
        if sensitive:
            return f"command expands credential variable(s) {', '.join(sensitive)}; secrets are never read"
        for token in _tokens(command) + [w.text for c in facts.commands for w in c.argv]:
            if self._policy.is_path_denied(token):
                return "command references a denied path pattern"
        home = str(Path.home())
        for words, write in ((facts.path_words, False), (facts.reads, False), (facts.writes, True)):
            for word in words:
                paths, problem = expand_word(word, str(self._workspace), home)
                if problem:
                    return f"command cannot be verified: {problem}"
                for path in paths:
                    reason = self._check_path(path, write=write, shell=True)
                    if reason:
                        what = "shell redirection writes" if write else "command references a path"
                        return f"{what} '{path}': {reason}"
        return None

    def _shell_approvals(self, command: str, facts: Facts, already: list[CommandRule]) -> list[CommandRule]:
        texts = [command, *(c.normalized for c in facts.commands)]
        rules = [r for r, rx in self._approval_rules if any(rx.search(t) for t in texts)]
        if facts.destructive:
            rules.append(DESTRUCTIVE)
        if facts.opaque and self._shell_mode != SANDBOXED and not (self._mode and self._mode.mode == "confirm-writes"):
            rules.append(OPAQUE_CODE)               # unsandboxed: nothing contains what it does
        if self._shell_mode == APPROVAL:
            rules.append(UNSANDBOXED)
        known = {r.id for r in already}
        return [r for r in dict.fromkeys(rules) if r.id not in known]

    # ------------------------------------------------------------ plugin write mode

    def _outside(self, tool: str, facts: Facts | None) -> list[tuple[str, str]]:
        """Effects of this call that the write mode does not grant: [(capability, what)] (SEC-06)."""
        assert self._caps is not None
        if tool in HARNESS_TOOLS:
            return []
        if tool == "Bash":
            assert facts is not None
            effects = [*facts.effects, *((EXECUTE, opaque) for opaque in facts.opaque)]
            return [(cap, what) for cap, what in dict.fromkeys(effects) if cap not in self._caps.allow]
        effect = TOOL_EFFECTS.get(tool, UNKNOWN)
        return [] if effect in self._caps.allow else [(effect, tool)]

    def _capability_text(self, outside: list[tuple[str, str]]) -> str:
        cap, what = outside[0]
        allowed = ", ".join(sorted(self._caps.allow)) if self._caps and self._caps.allow else "nothing"
        return (f"'{what}' is classified as '{cap}', which write mode '{self._mode.mode}' does not grant "
                f"(it grants: {allowed}; unrecognised operations are 'unknown')")

    def _mode_denial(self, tool: str, tool_input: Mapping[str, Any], facts: Facts | None) -> str | None:
        """What the agent's write mode forbids outright (runs after every other check passed)."""
        mode = self._mode.mode if self._mode else None
        if mode is None:
            return None
        if tool == "Bash":
            facts = facts or analyze(str(tool_input.get("command", "")),
                                     destructive_programs=self._shell.destructive_programs)
        if tool in WRITE_PATH_KEYS:
            if mode == "read-only":
                return f"agent '{self.agent_id}' is read-only and never changes files"
            if mode == "documentation-only":
                for key in WRITE_PATH_KEYS[tool]:
                    resolved = self._resolve(str(tool_input[key]))
                    if _inside(resolved, (self._artifact_dir,)):
                        continue                    # the run's own output directory, not the codebase
                    relative = resolved.relative_to(self._workspace).as_posix()
                    if not _glob_match(relative, self._mode.documentation_paths):
                        return (f"agent '{self.agent_id}' writes documentation only; '{relative}' is not a "
                                "documentation path")
        if tool == "Bash":
            if mode == "documentation-only" or not self._caps.shell:
                return f"agent '{self.agent_id}' has no shell access"
            if mode == "read-only":
                command = str(tool_input.get("command", ""))
                if facts.writes:
                    return (f"agent '{self.agent_id}' is read-only; shell redirection to '{facts.writes[0].text}' "
                            "writes a file")
                texts = [command, *(c.normalized for c in facts.commands)]
                for rule, regex in self._read_only_rules:
                    if any(regex.search(text) for text in texts):
                        return (f"agent '{self.agent_id}' is read-only; command blocked by rule '{rule.id}'"
                                + (f": {rule.description}" if rule.description else ""))
                if facts.destructive:
                    return f"agent '{self.agent_id}' is read-only; '{facts.destructive[0]}' deletes files"
                if facts.opaque:
                    return f"agent '{self.agent_id}' is read-only; '{facts.opaque[0]}' runs code that cannot be analysed"
        if self._caps.otherwise == "deny" and tool not in WRITE_PATH_KEYS:   # file writes: checked above
            outside = self._outside(tool, facts)
            if outside:
                return f"agent '{self.agent_id}' is {mode}; " + self._capability_text(outside)
        return None

    def _mode_approvals(self, tool: str, tool_input: Mapping[str, Any], facts: Facts | None) -> list[CommandRule]:
        """``otherwise: approve`` (confirm-writes): every effect the mode does not grant needs the approver
        (the default denies). File writes to the run's own output directory are not codebase changes."""
        if not self._mode or self._caps.otherwise != "approve":
            return []
        if tool in WRITE_PATH_KEYS:
            targets = [self._resolve(str(tool_input[key])) for key in WRITE_PATH_KEYS[tool]]
            return [CONFIRM_WRITE] if any(not _inside(t, (self._artifact_dir,)) for t in targets) else []
        rules: list[CommandRule] = []
        if tool == "Bash":
            command = str(tool_input.get("command", ""))
            facts = facts or analyze(command, destructive_programs=self._shell.destructive_programs)
            texts = [command, *(c.normalized for c in facts.commands)]
            rules = [rule for rule, regex in self._read_only_rules if any(regex.search(t) for t in texts)]
            if any(not _inside(self._resolve(w.text), (self._artifact_dir,)) for w in facts.writes) or facts.opaque:
                rules.append(CONFIRM_WRITE)
        outside = self._outside(tool, facts)
        if outside and not rules:
            rules.append(CommandRule(CONFIRM_WRITE.id, "", f"{CONFIRM_WRITE.description}: "
                                     + self._capability_text(outside)))
        return rules

    # ------------------------------------------------------------ approval binding

    def _digest(self, tool: str, tool_input: Mapping[str, Any], tool_use_id: str | None, nonce: str) -> str:
        bound = {"agent_id": self.agent_id, "session_agent": self.session_agent, "tool": tool,
                 "tool_input": tool_input, "cwd": str(self._workspace), "artifact_dir": str(self._artifact_dir),
                 "execution_id": self._execution_id, "stage_id": self._stage_id, "tool_use_id": tool_use_id,
                 "nonce": nonce}
        return hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    async def _ask_approval(self, tool: str, tool_input: Mapping[str, Any], summary: str, rules: list[CommandRule],
                            tool_use_id: str | None) -> dict[str, Any]:
        snapshot = json.loads(json.dumps(dict(tool_input), default=str))   # what is approved, frozen now
        nonce = secrets.token_hex(16)
        digest = self._digest(tool, snapshot, tool_use_id, nonce)
        operation = self._operation(tool, snapshot)
        request = ApprovalRequest(agent_id=self.agent_id, tool_name=tool, summary=summary,
                                  rules=tuple(r.id for r in rules),
                                  reason="; ".join(r.description or r.id for r in rules),
                                  execution_id=self._execution_id, stage_id=self._stage_id,
                                  session_agent=self.session_agent, cwd=str(self._workspace), operation=operation,
                                  tool_use_id=tool_use_id, digest=digest,
                                  expires_at=time.time() + self._approval_ttl)
        self._audit("tool.approval_requested", f"{tool}: approval requested", tool=tool, agent_id=self.agent_id,
                    summary=summary, rules=list(request.rules), approval_id=digest, cwd=request.cwd,
                    operation=operation[:_AUDIT_OPERATION_CHARS], operation_chars=len(operation))
        decision = await self._approver(request)
        if decision.approved:
            problem = None
            current = json.loads(json.dumps(dict(tool_input), default=str))
            if decision.binding != digest:
                problem = "the approval does not match this exact operation and context"
            elif time.time() > request.expires_at:
                problem = "the approval expired"
            elif self._digest(tool, current, tool_use_id, nonce) != digest:
                problem = "the operation changed after approval was requested; a fresh approval is required"
            elif digest in self._used_approvals:
                problem = "the approval was already used"
            if problem is None:
                self._used_approvals.add(digest)
                self._decide(tool, "approved", decision.reason, summary, approver=decision.approver,
                             rules=list(request.rules), approval_id=digest)
                return {}
            reason = f"approval rejected: {problem}"
            self._decide(tool, "denied", reason, summary, level="WARNING", approver=decision.approver,
                         rules=list(request.rules), approval_id=digest)
            return deny(reason)
        reason = f"requires human approval ({', '.join(request.rules)}): {decision.reason}"
        self._decide(tool, "denied", reason, summary, level="WARNING", approver=decision.approver,
                     rules=list(request.rules), approval_id=digest)
        return deny(reason)

    # ------------------------------------------------------------ PostToolUse

    async def post_tool_use(self, hook_input: Mapping[str, Any], tool_use_id: str | None,
                            context: Any) -> dict[str, Any]:
        tool = str(hook_input.get("tool_name", "?"))
        try:
            redacted, found = redact_structure(hook_input.get("tool_response"), self._redactor)
        except Exception as exc:  # noqa: BLE001
            self._audit("tool.redaction_failed", f"{tool}: output could not be scanned ({type(exc).__name__})",
                        level="ERROR", tool=tool, agent_id=self.agent_id)
            return {"decision": "block", "reason": "tool output could not be scanned for secrets"}
        if not found:
            return {}
        self._audit("tool.output_redacted", f"{tool}: secret patterns masked before reaching the model",
                    level="WARNING", tool=tool, agent_id=self.agent_id, patterns=sorted(found))
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": redacted}}


# ---------------------------------------------------------------------- tripwire


@dataclass
class HookTripwire:
    """Detect a tool call that completed without passing the guard's PreToolUse (SEC-07).

    A *detector*, not enforcement: the call it reports has already executed. Preventive enforcement
    is the SDK hook registration (``on_failure="block"``), which the CLI applies before every call.
    The tripwire guards against that registration silently not applying (a CLI change, a session
    started with hooks disabled): it runs on every session and aborts the run with
    ``security.violation``.

    * A successful result for a call the guard never saw aborts at once.
    * An *error* result for an unseen call (a command that ran and exited non-zero is an error too)
      is explained only when the call could not have run: the session never offered that tool (the
      ``init`` frame's ``tools``; the CLI answers "No such tool available" before any hook, observed
      with CLI 2.1.317 when Bash is removed), or the CLI's native permission rules refused it, which
      the result lists in ``permission_denials``. Anything else aborts at the session's result.
    """

    guard: ToolGuard
    _names: dict[str, str] = field(default_factory=dict)
    _unseen_errors: dict[str, str] = field(default_factory=dict)
    _session_tools: frozenset[str] | None = None        # None until the init frame is seen: nothing is explained

    def observe(self, message: Any) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            tools = message.data.get("tools") if isinstance(message.data, Mapping) else None
            if isinstance(tools, list):
                self._session_tools = frozenset(str(t) for t in tools)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self._names[block.id] = block.name
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                name = self._names.get(block.tool_use_id)
                if block.tool_use_id in self.guard.seen_tool_use_ids or name in HARNESS_TOOLS:
                    continue
                if block.is_error:
                    if name is not None and self._session_tools is not None and name not in self._session_tools:
                        continue                         # a tool the session does not have cannot have run
                    self._unseen_errors[block.tool_use_id] = name or "unknown tool"
                    continue
                raise SecurityViolationError(f"tool call {block.tool_use_id} ({name or 'unknown tool'}) completed "
                                             "without passing the harness PreToolUse guard; run aborted")
        elif isinstance(message, ResultMessage):
            refused = {str(d.get("tool_use_id")) for d in (message.permission_denials or []) if isinstance(d, Mapping)}
            unexplained = {tid: name for tid, name in self._unseen_errors.items() if tid not in refused}
            if unexplained:
                tid, name = next(iter(unexplained.items()))
                raise SecurityViolationError(f"tool call {tid} ({name}) ran without passing the harness PreToolUse "
                                             "guard and was not refused by the native permission rules; run aborted")
