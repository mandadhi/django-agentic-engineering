"""Turn an SDK message stream (or failure) into a validated AgentResult.

:class:`SessionCollector` watches the stream: the ``system``/``init`` frame (session
id, model, plugins, skills), assistant text, and the final ``ResultMessage`` (result
text, ``structured_output``, usage, cost, duration). An error result arrives as a
``ResultMessage`` with ``is_error`` and is then raised as ``ResultError`` (observed
with CLI 2.1.317), so both are kept.

:class:`ResultProcessor` builds the AgentResult:

1. the candidate is ``structured_output`` when the SDK produced it, else the JSON
   object in the final text (fallback, also used when structured output is off);
2. harness-owned fields are set from the task and the SDK, never trusted from the
   model: ``schema_version``, ``kind``, ``task_id``, ``agent_id``, ``execution_id``,
   ``metrics``, ``started_at``/``completed_at``;
3. outputs are made real: inline content (``extensions["dae.output_content"]``) is
   redacted and written under the run's artifact directory; files the agent wrote are
   checked to exist inside the permitted roots; ``sha256``/``size_bytes`` are computed.
   The content never reaches the database — only the reference does;
4. the result is validated against the AgentResult contract, and a ``succeeded``
   result must have every required expected output.

Anything that fails becomes a contract-valid ``failed`` result with a normalized error
code (see :func:`normalize_failure`), so the run is always recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from claude_agent_sdk import (AssistantMessage, CLIConnectionError, CLINotFoundError, ProcessError, ResultMessage,
                              SystemMessage, TextBlock)
from claude_agent_sdk import ResultError

from core.contracts import ContractValidator
from core.errors import ContractValidationError
from core.security import Redactor
from runtime.models import FailureInfo, utcnow
from runtime.records import usage_tokens

from .exceptions import AgentRuntimeError

OUTPUT_CONTENT_KEY = "dae.output_content"
INLINE_PREFIX = "inline:"
MAX_SUMMARY_CHARS = 4000
MAX_MESSAGE_CHARS = 1000
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_EXTENSIONS = {"report": (".md", "text/markdown"), "json": (".json", "application/json"),
               "patch": (".diff", "text/x-diff"), "text": (".txt", "text/plain"), "file": (".txt", "text/plain")}


# ---------------------------------------------------------------------- collection


@dataclass
class SessionCollector:
    init: dict[str, Any] | None = None
    session_id: str | None = None
    model: str | None = None
    last_text: str = ""
    result: ResultMessage | None = None
    assistant_error: str | None = None
    started_at: str = field(default_factory=utcnow)
    messages: int = 0
    # Usage reported on assistant messages, per API message id (the SDK repeats one message's usage
    # on each of its content blocks). Only used when no ResultMessage arrives (timeout, cancel, crash).
    partial_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    def observe(self, message: Any) -> bool:
        """Record one SDK message. Returns True when it was the session's ``init`` frame."""
        self.messages += 1
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self.init = dict(message.data)
            self.session_id = self.init.get("session_id") or self.session_id
            self.model = self.init.get("model") or self.model
            return True
        if isinstance(message, AssistantMessage):
            texts = [b.text for b in message.content if isinstance(b, TextBlock) and b.text.strip()]
            if texts and message.parent_tool_use_id is None:
                self.last_text = "\n".join(texts)
            if message.error:
                self.assistant_error = message.error
            self.session_id = message.session_id or self.session_id
            usage = getattr(message, "usage", None)
            message_id = getattr(message, "message_id", None)
            if isinstance(usage, dict) and message_id:
                tokens = {k: v for k, v in zip(("input_tokens", "output_tokens"), usage_tokens(usage))
                          if v is not None}
                if tokens:
                    self.partial_usage[message_id] = tokens
        elif isinstance(message, ResultMessage):
            self.result = message
            self.session_id = message.session_id or self.session_id
        return False

    def sdk_metadata(self) -> dict[str, Any]:
        """Small, secret-free summary stored with the run (no conversation content)."""
        data: dict[str, Any] = {}
        if self.init:
            data.update({k: self.init[k] for k in ("model", "claude_code_version", "permissionMode", "tools")
                         if self.init.get(k)})
            data["plugins"] = sorted(p.get("name", "?") for p in self.init.get("plugins") or [] if p.get("path") != "builtin")
        if self.result is not None:
            r = self.result
            data.update({"subtype": r.subtype, "is_error": r.is_error, "num_turns": r.num_turns,
                         "duration_api_ms": r.duration_api_ms, "stop_reason": r.stop_reason,
                         "terminal_reason": r.terminal_reason,
                         "permission_denials": len(r.permission_denials or [])})
            if r.usage:
                data["usage"] = {k: v for k, v in r.usage.items() if isinstance(v, (int, float))}
            if r.model_usage:
                data["model_usage"] = {m: {k: u.get(k) for k in ("inputTokens", "outputTokens", "costUSD")}
                                       for m, u in r.model_usage.items()}
        return {k: v for k, v in data.items() if v is not None}

    def metrics(self) -> dict[str, Any]:
        """Run metrics for the RunRecord. A value the SDK did not report is None, never 0.

        ``metrics_source`` is ``"result"`` when the final ResultMessage arrived (it is authoritative,
        also for error results), ``"partial"`` when only assistant-message usage was seen before the
        stream stopped (token lower bound; no cost or turns), else None.
        """
        empty = {"input_tokens": None, "output_tokens": None, "cost_usd": None, "duration_ms": None,
                 "num_turns": None, "metrics_source": None}
        r = self.result
        if r is not None:
            input_tokens, output_tokens = usage_tokens(r.usage)
            cost = r.total_cost_usd if isinstance(r.total_cost_usd, (int, float)) \
                and not isinstance(r.total_cost_usd, bool) else None
            return {**empty, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
                    "duration_ms": r.duration_ms if isinstance(r.duration_ms, int) else None,
                    "num_turns": r.num_turns if isinstance(r.num_turns, int) else None, "metrics_source": "result"}
        if self.partial_usage:
            def total(key: str) -> int | None:
                values = [u[key] for u in self.partial_usage.values() if key in u]
                return sum(values) if values else None
            return {**empty, "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
                    "metrics_source": "partial"}
        return empty


# ---------------------------------------------------------------------- failures


def _short(text: str, redactor: Redactor | None) -> str:
    text = redactor.redact(text) if redactor else text
    text = text.strip() or "no message"
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS] + "…"


def normalize_failure(exc: BaseException | None, collector: SessionCollector,
                      redactor: Redactor | None = None) -> FailureInfo:
    """Map an SDK/harness failure (or an error ResultMessage) to a stable code + retryability."""
    result = collector.result
    if isinstance(exc, AgentRuntimeError):
        return FailureInfo(exc.code, _short(str(exc), redactor), exc.retryable)
    if collector.assistant_error == "authentication_failed":
        return FailureInfo("sdk.authentication", "the SDK could not authenticate (check credentials)", False)
    subtype = getattr(exc, "subtype", None) or (result.subtype if result is not None and result.is_error else None)
    status = getattr(exc, "api_error_status", None) or (result.api_error_status if result is not None else None)
    terminal = getattr(exc, "terminal_reason", None) or (result.terminal_reason if result is not None else None)
    text = (getattr(exc, "result", None) or (result.result if result is not None else None)
            or "; ".join(getattr(exc, "errors", None) or (result.errors if result is not None else None) or [])
            or (str(exc) if exc else "agent returned an error"))
    if subtype == "error_max_turns":
        return FailureInfo("agent.max_turns", _short(text, redactor), False)
    if subtype == "error_max_budget_usd":
        return FailureInfo("agent.budget_exceeded", _short(text, redactor), False)
    if subtype == "error_max_structured_output_retries":
        return FailureInfo("result.structured_output_failed", _short(text, redactor), True)
    if status in (401, 403):
        return FailureInfo("sdk.authentication", _short(text, redactor), False, {"status": status})
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429):
        # A rejected request (e.g. 400 invalid schema) fails the same way every time: never retry.
        return FailureInfo("sdk.invalid_request", _short(text, redactor), False, {"status": status})
    if status in (408, 409, 429, 500, 502, 503, 504, 529) or terminal == "api_error":
        return FailureInfo("sdk.api_error", _short(text, redactor), True, {"status": status} if status else {})
    if isinstance(exc, CLINotFoundError):
        return FailureInfo("sdk.cli_not_found", _short(str(exc), redactor), False)
    if isinstance(exc, ResultError) or (exc is None and result is not None and result.is_error):
        return FailureInfo("sdk.result_error", _short(text, redactor), False,
                           {"subtype": subtype} if subtype else {})
    if isinstance(exc, CLIConnectionError):
        return FailureInfo("sdk.connection", _short(str(exc), redactor), True)
    if isinstance(exc, ProcessError):
        return FailureInfo("sdk.process_error", _short(str(exc), redactor), True)
    if exc is not None:
        return FailureInfo("runtime.error", _short(f"{type(exc).__name__}: {exc}", redactor), False)
    return FailureInfo("result.missing", "the SDK stream ended without a result", True)


# ---------------------------------------------------------------------- processing


@dataclass(frozen=True)
class ProcessedResult:
    result: dict[str, Any]           # contract-valid AgentResult (references only)
    failure: FailureInfo | None      # set when the run did not succeed
    redacted_patterns: tuple[str, ...] = ()


def extract_json(text: str) -> Any:
    """The AgentResult object in a text reply: a fenced ```json block, else the outermost object."""
    for match in _JSON_BLOCK.finditer(text or ""):
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == r or r in path.parents for r in roots)


class ResultProcessor:
    def __init__(self, contracts: ContractValidator, redactor: Redactor, *, max_json_bytes: int = 65536) -> None:
        self._contracts = contracts
        self._redactor = redactor
        self._max_json_bytes = max_json_bytes

    # ------------------------------------------------------------ public

    def failure_result(self, task: Mapping[str, Any], failure: FailureInfo, collector: SessionCollector,
                       *, status: str = "failed") -> ProcessedResult:
        error: dict[str, Any] = {"code": failure.code, "message": failure.message, "retryable": failure.retryable}
        if failure.details:
            error["details"] = dict(failure.details)
        result = self._envelope(task, collector, {"status": status, "summary": failure.message,
                                                  "errors": [error]})
        self._contracts.validate("agent-result", result)
        return ProcessedResult(result, failure)

    def process(self, task: Mapping[str, Any], collector: SessionCollector, *, workspace: Path,
                artifact_dir: Path, exc: BaseException | None = None) -> ProcessedResult:
        if exc is not None or collector.result is None or collector.result.is_error:
            failure = normalize_failure(exc, collector, self._redactor)
            status = "cancelled" if failure.code == "runtime.cancelled" else "failed"
            return self.failure_result(task, failure, collector, status=status)
        candidate = collector.result.structured_output
        source = "structured_output"
        if not isinstance(candidate, dict):
            candidate = extract_json(collector.result.result or collector.last_text)
            source = "text"
        if not isinstance(candidate, dict):
            return self.failure_result(task, FailureInfo(
                "result.malformed", "the agent did not return an AgentResult JSON object", True), collector)
        try:
            result, patterns = self._normalize(task, candidate, collector, workspace, artifact_dir)
            result.setdefault("extensions", {})["dae.result_source"] = source
            self._contracts.validate("agent-result", result)
            self._check_expected_outputs(task, result)
        except (ContractValidationError, ValueError, OSError) as exc2:
            issues = getattr(exc2, "issues", ())
            details = {"issues": [_short(str(i), self._redactor) for i in issues[:10]]} if issues else {}
            return self.failure_result(task, FailureInfo(
                "result.invalid", _short(f"the agent's AgentResult was rejected: {str(exc2).splitlines()[0]}",
                                         self._redactor), True, details), collector)
        failure = None
        if result["status"] in ("failed", "blocked"):
            first = result["errors"][0]
            failure = FailureInfo(first["code"], first["message"], first.get("retryable", False))
        return ProcessedResult(result, failure, tuple(sorted(patterns)))

    # ------------------------------------------------------------ internals

    def _envelope(self, task: Mapping[str, Any], collector: SessionCollector,
                  body: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(body)
        result.update({"schema_version": "1.0", "kind": "AgentResult", "task_id": task["task_id"],
                       "agent_id": task["agent_id"], "started_at": collector.started_at, "completed_at": utcnow()})
        if task.get("execution_id"):
            result["execution_id"] = task["execution_id"]
        metrics: dict[str, int] = {}
        if collector.result is not None:
            usage = collector.result.usage or {}
            for key in ("input_tokens", "output_tokens"):
                if isinstance(usage.get(key), int):
                    metrics[key] = usage[key]
            if isinstance(collector.result.duration_ms, int):
                metrics["duration_ms"] = collector.result.duration_ms
        if metrics:
            result["metrics"] = metrics
        return result

    def _normalize(self, task: Mapping[str, Any], candidate: Mapping[str, Any], collector: SessionCollector,
                   workspace: Path, artifact_dir: Path) -> tuple[dict[str, Any], set[str]]:
        body = {k: v for k, v in candidate.items()
                if k not in ("schema_version", "kind", "task_id", "agent_id", "execution_id", "metrics",
                             "started_at", "completed_at")}
        result = self._envelope(task, collector, body)
        patterns: set[str] = set()
        extensions = dict(result.get("extensions") or {})
        content = extensions.pop(OUTPUT_CONTENT_KEY, None) or {}
        if not isinstance(content, Mapping):
            raise ValueError(f"extensions['{OUTPUT_CONTENT_KEY}'] must be an object of output name -> content")
        types = {o["name"]: o["type"] for o in task["expected_outputs"]}
        roots = (workspace.resolve(), artifact_dir.resolve())
        outputs = []
        for entry in result.get("outputs") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("artifact"), Mapping):
                outputs.append(entry)       # let contract validation report it
                continue
            name, artifact = entry.get("name"), dict(entry["artifact"])
            uri = str(artifact.get("uri", ""))
            if name in content or uri.startswith(INLINE_PREFIX):
                key = name if name in content else uri[len(INLINE_PREFIX):]
                if key not in content:
                    raise ValueError(f"output '{name}': no inline content for '{uri}'")
                artifact, found = self._write_inline(str(name), content[key], types.get(name, "text"), artifact_dir,
                                                     artifact)
                patterns |= found
            else:
                artifact = self._verify_file(str(name), artifact, workspace, roots)
            outputs.append({"name": name, "artifact": artifact})
        unused = set(content) - {o.get("name") for o in outputs if isinstance(o, Mapping)}
        for name in sorted(unused):        # content without an outputs entry: still keep it, by name
            artifact, found = self._write_inline(name, content[name], types.get(name, "text"), artifact_dir, {})
            patterns |= found
            outputs.append({"name": name, "artifact": artifact})
        if outputs:
            result["outputs"] = outputs
        summary = result.get("summary")
        if isinstance(summary, str):
            found = self._redactor.find(summary)
            patterns |= set(found)
            summary = self._redactor.redact(summary)
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS] + "…"
                extensions["dae.summary_truncated"] = True
            result["summary"] = summary
        if patterns:
            extensions["dae.redacted_patterns"] = sorted(patterns)
        result["extensions"] = extensions
        self._fit(result, artifact_dir)
        return result, patterns

    def _write_inline(self, name: str, content: Any, output_type: str, artifact_dir: Path,
                      artifact: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", name or ""):
            raise ValueError(f"invalid output name '{name}'")
        text = content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False)
        found = set(self._redactor.find(text))
        text = self._redactor.redact(text)
        suffix, media = _EXTENSIONS.get(output_type, (".txt", "text/plain"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"{name}{suffix}"
        path.write_text(text, encoding="utf-8")
        out = {"uri": path.resolve().as_uri(), "media_type": media, "sha256": _sha256(path),
               "size_bytes": path.stat().st_size}
        if artifact.get("description"):
            out["description"] = artifact["description"]
        return out, found

    def _verify_file(self, name: str, artifact: Mapping[str, Any], workspace: Path,
                     roots: tuple[Path, ...]) -> dict[str, Any]:
        uri = str(artifact.get("uri", ""))
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            path = Path(url2pathname(unquote(parsed.netloc + parsed.path) if parsed.netloc else unquote(parsed.path)))
        elif parsed.scheme and len(parsed.scheme) > 1:      # s3://, https:// ... — not verifiable locally
            raise ValueError(f"output '{name}': only local files are accepted, got '{parsed.scheme}://'")
        else:
            path = Path(uri)
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        if not _inside(path, roots):
            raise ValueError(f"output '{name}': {uri} is outside the workspace and the output directory")
        if not path.is_file():
            raise ValueError(f"output '{name}': file {uri} does not exist")
        out = dict(artifact)
        out.update({"uri": path.as_uri(), "sha256": _sha256(path), "size_bytes": path.stat().st_size})
        if "media_type" not in out:
            out["media_type"] = {".md": "text/markdown", ".json": "application/json", ".diff": "text/x-diff",
                                 ".patch": "text/x-diff"}.get(path.suffix.lower(), "text/plain")
        return out

    def _check_expected_outputs(self, task: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        if result["status"] != "succeeded":
            return
        produced = {o["name"] for o in result.get("outputs", [])}
        missing = [o["name"] for o in task["expected_outputs"] if o.get("required", True) and o["name"] not in produced]
        if missing:
            raise ValueError(f"status 'succeeded' but required outputs are missing: {', '.join(missing)}")

    def _fit(self, result: dict[str, Any], artifact_dir: Path) -> None:
        """Keep the stored result well under the metadata cap: large agent extensions go to a file."""
        if len(json.dumps(result, default=str).encode()) <= self._max_json_bytes // 2:
            return
        extra = {k: v for k, v in result.get("extensions", {}).items() if not k.startswith("dae.")}
        if extra:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = artifact_dir / "result-extensions.json"
            path.write_text(self._redactor.redact(json.dumps(extra, indent=2, default=str)), encoding="utf-8")
            kept = {k: v for k, v in result["extensions"].items() if k.startswith("dae.")}
            kept["dae.extensions_artifact"] = path.resolve().as_uri()
            result["extensions"] = kept
        refs = result.get("references")
        if isinstance(refs, list) and len(refs) > 200:
            result["references"] = refs[:200]
            result["extensions"]["dae.references_truncated"] = len(refs)
