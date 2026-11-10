"""Context management: give each agent only the context it needs, within a token budget.

Context is held as :class:`ContextItem` records in four scopes:

* ``project``  – long-lived facts about the codebase (settings, architecture notes);
* ``workflow`` – shared across the stages of one execution (upstream outputs);
* ``task``     – specific to one task (bound by ``task_id``);
* ``agent``    – private to one agent (bound by ``agent_id``).

:meth:`ContextManager.assemble` selects items by the agent's registered
``context_needs`` (optionally narrowed by the stage), orders them by specificity
and priority, fills the budget greedily, and hands overflow to an optional
:class:`Summarizer`. Whatever still does not fit is reported in ``omitted`` —
never silently dropped. Items can be passed by reference (``uri``) or inline
(``content``, size-capped), so large documents never travel inline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from .config import AgentSpec, ContextSettings
from .errors import HarnessError, PayloadTooLargeError
from .log import get_logger
from .workflows import StageDefinition

log = get_logger(__name__)


class ContextScope(StrEnum):
    PROJECT = "project"
    WORKFLOW = "workflow"
    TASK = "task"
    AGENT = "agent"


# Most specific first: task/agent context beats shared context when the budget is tight.
_SCOPE_RANK = {ContextScope.TASK: 0, ContextScope.AGENT: 1, ContextScope.WORKFLOW: 2, ContextScope.PROJECT: 3}


@dataclass(frozen=True)
class ContextItem:
    id: str
    scope: ContextScope
    content: str | None = None        # inline value (small)
    uri: str | None = None            # reference (preferred for anything large)
    description: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)
    priority: int = 50                # 0–100, higher survives budget pressure
    token_estimate: int | None = None
    agent_id: str | None = None       # required for AGENT scope
    task_id: str | None = None        # required for TASK scope

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", ContextScope(self.scope))
        object.__setattr__(self, "tags", frozenset(self.tags))
        if (self.content is None) == (self.uri is None):
            raise ValueError(f"context item '{self.id}': set exactly one of content or uri")
        if not 0 <= self.priority <= 100:
            raise ValueError(f"context item '{self.id}': priority must be 0–100")
        if self.scope is ContextScope.AGENT and not self.agent_id:
            raise ValueError(f"context item '{self.id}': agent-scoped items need agent_id")
        if self.scope is ContextScope.TASK and not self.task_id:
            raise ValueError(f"context item '{self.id}': task-scoped items need task_id")

    @property
    def is_reference(self) -> bool:
        return self.uri is not None


@runtime_checkable
class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


@dataclass(frozen=True)
class CharsPerTokenEstimator:
    """Cheap, model-agnostic estimate. Replace with a tokenizer-backed estimator when available."""

    chars_per_token: float = 4.0

    def estimate(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token) if text else 0


@runtime_checkable
class Summarizer(Protocol):
    """Condenses items that did not fit into one inline item of at most ``target_tokens``."""

    def summarize(self, items: Sequence[ContextItem], target_tokens: int) -> str: ...


@dataclass(frozen=True)
class TokenBudget:
    total: int
    reserved: int = 0
    used: int = 0

    @property
    def available(self) -> int:
        return max(self.total - self.reserved - self.used, 0)

    def to_dict(self) -> dict[str, int]:
        return {"total": self.total, "reserved": self.reserved, "used": self.used}


@dataclass(frozen=True)
class ContextBundle:
    agent_id: str
    task_id: str
    items: tuple[ContextItem, ...]
    omitted: tuple[ContextItem, ...]
    summary: str | None
    summarized: tuple[ContextItem, ...]
    budget: TokenBudget

    def to_task_context(self, estimator: TokenEstimator) -> dict[str, Any]:
        """The ``context`` block of an AgentTask (see contracts/agent-task.schema.json)."""
        refs, inline = [], []
        for item in self.items:
            if item.is_reference:
                ref: dict[str, Any] = {"id": item.id, "scope": item.scope.value, "uri": item.uri}
                if item.description:
                    ref["description"] = item.description
                if item.token_estimate is not None:
                    ref["token_estimate"] = item.token_estimate
                refs.append(ref)
            else:
                inline.append({"id": item.id, "scope": item.scope.value, "content": item.content,
                               "token_estimate": item.token_estimate or estimator.estimate(item.content or "")})
        block: dict[str, Any] = {"refs": refs, "inline": inline, "token_budget": self.budget.to_dict()}
        if self.summary:
            block["summary"] = self.summary
        return block


class ContextError(HarnessError):
    """Invalid context operation (duplicate id, unknown id)."""


class ContextManager:
    def __init__(self, settings: ContextSettings, *, max_inline_bytes: int,
                 estimator: TokenEstimator | None = None, summarizer: Summarizer | None = None) -> None:
        self._settings = settings
        self._max_inline_bytes = max_inline_bytes
        self.estimator: TokenEstimator = estimator or CharsPerTokenEstimator(settings.chars_per_token)
        self._summarizer = summarizer
        self._items: dict[str, ContextItem] = {}

    # ---------------------------------------------------------------- store

    def add(self, item: ContextItem, *, replace: bool = False) -> None:
        if item.id in self._items and not replace:
            raise ContextError(f"context item '{item.id}' already exists")
        if item.content is not None:
            size = len(item.content.encode("utf-8"))
            if size > self._max_inline_bytes:
                raise PayloadTooLargeError(
                    f"context item '{item.id}' is {size} bytes inline (limit {self._max_inline_bytes}); "
                    "store it as an artifact and add it by uri"
                )
        self._items[item.id] = item

    def extend(self, items: Iterable[ContextItem]) -> None:
        for item in items:
            self.add(item)

    def remove(self, item_id: str) -> None:
        if self._items.pop(item_id, None) is None:
            raise ContextError(f"unknown context item '{item_id}'")

    def items(self, scope: ContextScope | str | None = None) -> tuple[ContextItem, ...]:
        if scope is None:
            return tuple(self._items.values())
        scope = ContextScope(scope)
        return tuple(i for i in self._items.values() if i.scope is scope)

    def cost(self, item: ContextItem) -> int:
        """Tokens an item consumes: explicit estimate, else estimated from inline content.

        A reference without an estimate costs 0 — the agent decides whether to load it.
        """
        if item.token_estimate is not None:
            return item.token_estimate
        return self.estimator.estimate(item.content or "")

    # ---------------------------------------------------------------- assembly

    def _selected(self, agent: AgentSpec, task_id: str, scopes: Iterable[str], tags: Iterable[str]) -> list[ContextItem]:
        scopes, tags = {ContextScope(s) for s in scopes}, set(tags)
        chosen = []
        for item in self._items.values():
            if item.scope not in scopes:
                continue
            if item.scope is ContextScope.AGENT and item.agent_id != agent.id:
                continue
            if item.scope is ContextScope.TASK and item.task_id != task_id:
                continue
            # Tags filter shared context only; task/agent context is already targeted.
            if item.scope in (ContextScope.PROJECT, ContextScope.WORKFLOW) and tags and not (item.tags & tags):
                continue
            chosen.append(item)
        chosen.sort(key=lambda i: (_SCOPE_RANK[i.scope], -i.priority, i.id))
        return chosen

    def assemble_for_stage(self, agent: AgentSpec, stage: StageDefinition, task_id: str) -> ContextBundle:
        """Context for a workflow stage, applying the stage's ``context:`` block (scopes, tags, budget)."""
        if stage.agent_id != agent.id:
            raise ContextError(f"stage '{stage.id}' runs '{stage.agent_id}', not '{agent.id}'")
        return self.assemble(agent, task_id, scopes=stage.context_scopes, tags=stage.context_tags,
                             token_budget=stage.context_token_budget)

    def assemble(self, agent: AgentSpec, task_id: str, *, scopes: Sequence[str] | None = None,
                 tags: Sequence[str] | None = None, token_budget: int | None = None) -> ContextBundle:
        """Context for one agent task. ``scopes``/``tags`` narrow the agent's registered needs."""
        needs = agent.context_needs
        effective_scopes = [s for s in (scopes or needs.scopes) if s in needs.scopes]
        effective_tags = tags if tags is not None else needs.tags
        total = token_budget or needs.token_budget or self._settings.default_token_budget
        reserved = min(self._settings.reserve_tokens, total - 1)
        budget = TokenBudget(total=total, reserved=reserved)

        used = 0
        kept: list[ContextItem] = []
        overflow: list[ContextItem] = []
        for item in self._selected(agent, task_id, effective_scopes, effective_tags):
            cost = self.cost(item)
            if used + cost <= budget.available:
                kept.append(item)
                used += cost
            else:
                overflow.append(item)

        summary: str | None = None
        summarized: tuple[ContextItem, ...] = ()
        remaining = budget.available - used
        if overflow and self._summarizer and remaining > 0:
            candidate = self._summarizer.summarize(overflow, remaining)
            summary_cost = self.estimator.estimate(candidate)
            if candidate and summary_cost <= remaining:
                summary, summarized, used = candidate, tuple(overflow), used + summary_cost
                overflow = []

        bundle = ContextBundle(agent_id=agent.id, task_id=task_id, items=tuple(kept), omitted=tuple(overflow),
                               summary=summary, summarized=summarized,
                               budget=TokenBudget(total=total, reserved=reserved, used=used))
        log.debug("context.assembled", extra={"agent_id": agent.id, "task_id": task_id, "items": len(kept),
                                              "omitted": len(overflow), "summarized": len(summarized),
                                              "tokens_used": used, "token_budget": total})
        if overflow:
            log.warning("context.omitted", extra={"agent_id": agent.id, "task_id": task_id,
                                                  "omitted_ids": [i.id for i in overflow]})
        return bundle
