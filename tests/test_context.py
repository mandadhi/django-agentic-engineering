from typing import Sequence

import pytest

from core.context import ContextError, ContextItem, ContextManager, ContextScope
from core.errors import PayloadTooLargeError
from core.registry import HarnessRegistry


def item(id_: str, scope: str, *, tokens: int | None = 100, **kw) -> ContextItem:
    kw.setdefault("uri", f"repo://{id_}")
    return ContextItem(id=id_, scope=ContextScope(scope), token_estimate=tokens, **kw)


@pytest.fixture
def populated(context_manager: ContextManager) -> ContextManager:
    context_manager.extend([
        item("settings", "project", tags={"code", "security"}, priority=90),
        item("adr-index", "project", tags={"architecture"}),
        item("frontend-notes", "project", tags={"ui"}),
        item("threat-model", "workflow", tags={"security"}),
        item("audit-focus", "task", uri=None, content="Focus on DRF permission classes.", tokens=None,
             task_id="t1"),
        item("other-task", "task", task_id="t2"),
        item("auditor-memory", "agent", agent_id="security-auditor"),
        item("scanner-memory", "agent", agent_id="secrets-scanner"),
    ])
    return context_manager


def ids(items) -> list[str]:
    return [i.id for i in items]


def test_selects_only_relevant_context(populated: ContextManager, registry: HarnessRegistry) -> None:
    bundle = populated.assemble(registry.agents.get("security-auditor"), "t1")
    # task > agent > workflow > project; untagged-for-agent project items and other tasks/agents excluded
    assert ids(bundle.items) == ["audit-focus", "auditor-memory", "threat-model", "settings"]
    assert bundle.omitted == ()


def test_agent_scopes_limit_selection(populated: ContextManager, registry: HarnessRegistry) -> None:
    # secrets-scanner needs only project + task scopes with tag "code"
    bundle = populated.assemble(registry.agents.get("secrets-scanner"), "t1")
    assert ids(bundle.items) == ["audit-focus", "settings"]


def test_stage_can_narrow_but_not_widen(populated: ContextManager, registry: HarnessRegistry) -> None:
    scanner = registry.agents.get("secrets-scanner")
    bundle = populated.assemble(scanner, "t1", scopes=["task", "workflow"])
    assert ids(bundle.items) == ["audit-focus"]   # workflow is not in the agent's needs


def test_budget_enforced_and_omissions_reported(populated: ContextManager, registry: HarnessRegistry) -> None:
    agent = registry.agents.get("security-auditor")
    # 4000 reserved by framework-config -> 4200 - 4000 = 200 tokens available.
    # audit-focus (~8, inline) + auditor-memory (100) fit; each 100-token shared item would overflow.
    bundle = populated.assemble(agent, "t1", token_budget=4200)
    assert bundle.budget.used <= 200
    assert ids(bundle.items) == ["audit-focus", "auditor-memory"]
    assert ids(bundle.omitted) == ["threat-model", "settings"]


class FakeSummarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def summarize(self, items: Sequence[ContextItem], target_tokens: int) -> str:
        self.calls.append(([i.id for i in items], target_tokens))
        return "Summary: " + ", ".join(i.id for i in items)


def test_overflow_goes_to_summarizer(registry: HarnessRegistry) -> None:
    summarizer = FakeSummarizer()
    manager = ContextManager(registry.config.framework.context, max_inline_bytes=4096, summarizer=summarizer)
    manager.extend([item("a", "project", tokens=150, tags={"code"}), item("b", "project", tokens=150, tags={"code"})])
    bundle = manager.assemble(registry.agents.get("pr-reviewer"), "t1", token_budget=4200)
    assert ids(bundle.items) == ["a"]
    assert ids(bundle.summarized) == ["b"] and bundle.omitted == ()
    assert summarizer.calls == [(["b"], 50)]
    assert bundle.summary == "Summary: b"


def test_task_context_block_matches_contract(populated: ContextManager, registry: HarnessRegistry) -> None:
    bundle = populated.assemble(registry.agents.get("security-auditor"), "t1")
    block = bundle.to_task_context(populated.estimator)
    assert block["inline"][0]["id"] == "audit-focus"
    assert {r["id"] for r in block["refs"]} == {"auditor-memory", "threat-model", "settings"}
    assert block["token_budget"]["used"] == bundle.budget.used


def test_large_inline_content_rejected(context_manager: ContextManager) -> None:
    with pytest.raises(PayloadTooLargeError, match="add it by uri"):
        context_manager.add(ContextItem("big", ContextScope.PROJECT, content="x" * 5000))


def test_item_validation() -> None:
    with pytest.raises(ValueError, match="exactly one of content or uri"):
        ContextItem("x", ContextScope.PROJECT)
    with pytest.raises(ValueError, match="need agent_id"):
        ContextItem("x", ContextScope.AGENT, uri="repo://x")
    with pytest.raises(ValueError, match="need task_id"):
        ContextItem("x", ContextScope.TASK, uri="repo://x")


def test_assemble_for_stage_applies_stage_context(populated: ContextManager, registry: HarnessRegistry) -> None:
    workflow = registry.workflows.get("security-review")
    threat = workflow.stage("threat-model")                      # context.tags: [architecture, security]
    bundle = populated.assemble_for_stage(registry.agents.get("threat-modeler"), threat, "t1")
    assert ids(bundle.items) == ["audit-focus", "threat-model", "settings", "adr-index"]
    with pytest.raises(ContextError, match="runs 'threat-modeler'"):
        populated.assemble_for_stage(registry.agents.get("pr-reviewer"), threat, "t1")
