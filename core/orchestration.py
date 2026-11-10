"""Canonical orchestration contract: load, validate, classify a task and plan its route.

``config/orchestration-policy.yaml`` says how the Django specialists are selected and coordinated;
the agents' own Markdown says what each of them does. Two adapters consume the policy:

* the standalone plugin, through the plugin build (generated section of
  ``agents/django-orchestrator.md`` and the orchestrator's delegation allowlist in the guard policy);
* the harness, through :func:`plan_route` and :func:`workflow_document`: a route becomes one ad-hoc
  ``WorkflowDefinition`` that the existing :class:`runtime.execution.ExecutionManager` and
  ``AgentRunner`` execute like any other workflow (stages, retries, approvals, ToolGuard, RunRecords).
  There is no second workflow engine.

This module is deterministic and has no model calls: the harness classifies by the policy's
signals and refuses to guess (``ambiguous`` / ``unclassified``). Standard library + PyYAML only, so
the plugin build can use it without the Agent SDK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import HarnessError, ValidationIssue, suggest

POLICY_KIND = "OrchestrationPolicy"
#: Logical identity of the orchestrator in both modes (the harness records it on every orchestrated execution).
EXTENSION_KEY = "dae.orchestration"
WORKFLOW_PREFIX = "orchestrate."


class OrchestrationError(HarnessError):
    """A task cannot be routed safely. ``code``: empty-task, unclassified, ambiguous, unknown-category,
    invalid-selection, missing-implementation, too-many-steps. ``candidates`` lists categories to choose from."""

    def __init__(self, code: str, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = tuple(candidates)


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Step:
    agent: str
    purpose: str
    optional: bool = False
    when: str = ""
    signals: tuple[str, ...] = ()
    after: tuple[str, ...] | None = None        # None: the previous included step


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    signals: tuple[str, ...]
    route: tuple[Step, ...]
    requires_any: tuple[str, ...] = ()
    supplementary_skills: tuple[str, ...] = ()

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(step.agent for step in self.route)


@dataclass(frozen=True)
class Constraint:
    id: str
    description: str


@dataclass(frozen=True)
class OrchestrationPolicy:
    version: str
    orchestrator_id: str
    orchestrator_path: str
    command: str
    orchestrator_skills: tuple[str, ...]
    max_delegations: int
    max_attempts_per_step: int
    max_clarifying_questions: int
    specialists: tuple[str, ...]
    categories: tuple[Category, ...]
    step_constraints: tuple[Constraint, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    clarify_when: tuple[str, ...] = ()
    failure_policy: tuple[str, ...] = ()
    security_boundaries: tuple[str, ...] = ()
    source: Path | None = None

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any], source: Path | None = None) -> "OrchestrationPolicy":
        """Build from a document that passed ``orchestration-policy.schema.json``."""
        orch, limits = doc["orchestrator"], doc["limits"]
        return cls(
            version=str(doc["version"]),
            orchestrator_id=orch["id"],
            orchestrator_path=orch["path"],
            command=orch["command"],
            orchestrator_skills=tuple(orch.get("skills", ())),
            max_delegations=int(limits["max_delegations"]),
            max_attempts_per_step=int(limits["max_attempts_per_step"]),
            max_clarifying_questions=int(limits.get("max_clarifying_questions", 3)),
            specialists=tuple(doc["specialists"]),
            categories=tuple(
                Category(
                    id=c["id"], title=c["title"], signals=tuple(c["signals"]),
                    route=tuple(Step(agent=s["agent"], purpose=s["purpose"], optional=bool(s.get("optional", False)),
                                     when=s.get("when", ""), signals=tuple(s.get("signals", ())),
                                     after=tuple(s["after"]) if "after" in s else None)
                                for s in c["route"]),
                    requires_any=tuple(c.get("requires_any", ())),
                    supplementary_skills=tuple(c.get("supplementary_skills", ())),
                )
                for c in doc["categories"]
            ),
            step_constraints=tuple(Constraint(c["id"], c["description"]) for c in doc.get("step_constraints", ())),
            completion_criteria=tuple(doc.get("completion_criteria", ())),
            clarify_when=tuple(doc.get("clarify_when", ())),
            failure_policy=tuple(doc.get("failure_policy", ())),
            security_boundaries=tuple(doc.get("security_boundaries", ())),
            source=source,
        )

    def category(self, category_id: str) -> Category:
        for category in self.categories:
            if category.id == category_id:
                return category
        hint = suggest(category_id, [c.id for c in self.categories])
        raise OrchestrationError("unknown-category", f"unknown category '{category_id}'"
                                 + (f" (did you mean: {', '.join(hint)}?)" if hint else ""),
                                 [c.id for c in self.categories])


# --------------------------------------------------------------------------- validation


def policy_issues(policy: OrchestrationPolicy, label: str, *, agent_ids: Iterable[str],
                  specialist_ids: Iterable[str], skill_ids: Iterable[str],
                  orchestrator_file: Path | None) -> list[ValidationIssue]:
    """Every broken reference in the policy.

    ``agent_ids``: every agent the harness registers (the orchestrator id must not collide with one).
    ``specialist_ids``: the pack's registered specialists; ``specialists`` must list exactly these.
    ``orchestrator_file``: the resolved orchestrator agent file (None: not checked).
    """
    agents, specialists, skills = set(agent_ids), set(specialist_ids), set(skill_ids)
    issues: list[ValidationIssue] = []

    def issue(where: str, message: str, code: str) -> None:
        issues.append(ValidationIssue(label, where, message, code))

    def unknown(kind: str, value: str, known: set[str]) -> str:
        hint = suggest(value, known)
        return f"unknown {kind} '{value}'" + (f" (did you mean: {', '.join(hint)}?)" if hint else "")

    orch = policy.orchestrator_id
    if orch in agents:
        issue("orchestrator/id", f"orchestrator id '{orch}' collides with a registered harness agent", "duplicate-id")
    if orch in policy.specialists:
        issue("specialists", f"the orchestrator '{orch}' must not be one of its own specialists (recursion)", "recursion")
    if orchestrator_file is not None:
        issues += _orchestrator_file_issues(orch, orchestrator_file, policy.orchestrator_path, label)
    for index, skill_id in enumerate(policy.orchestrator_skills):
        if skill_id not in skills:
            issue(f"orchestrator/skills/{index}", unknown("skill", skill_id, skills), "unknown-skill")

    for index, agent_id in enumerate(policy.specialists):
        if agent_id not in specialists:
            issue(f"specialists/{index}", unknown("specialist agent", agent_id, specialists), "unknown-agent")
    for agent_id in sorted(specialists - set(policy.specialists)):
        issue("specialists", f"registered pack agent '{agent_id}' is missing from specialists", "missing-agent")

    seen: set[str] = set()
    for c_index, category in enumerate(policy.categories):
        where = f"categories/{c_index}"
        if category.id in seen:
            issue(f"{where}/id", f"duplicate category id '{category.id}'", "duplicate-id")
        seen.add(category.id)
        earlier: list[str] = []
        for s_index, step in enumerate(category.route):
            at = f"{where}/route/{s_index}"
            if step.agent not in policy.specialists:
                code = "recursion" if step.agent == orch else "unknown-agent"
                issue(f"{at}/agent", f"category '{category.id}': "
                      + unknown("specialist", step.agent, set(policy.specialists)), code)
            if step.agent in earlier:
                issue(f"{at}/agent", f"category '{category.id}': agent '{step.agent}' appears twice in the route",
                      "duplicate-id")
            for a_index, ref in enumerate(step.after or ()):
                if ref == step.agent:
                    issue(f"{at}/after/{a_index}", f"category '{category.id}': step '{ref}' runs after itself", "cycle")
                elif ref not in earlier:
                    issue(f"{at}/after/{a_index}", f"category '{category.id}': step '{step.agent}' runs after "
                          f"'{ref}', which is not an earlier step of this route", "invalid-sequence")
            earlier.append(step.agent)
        if all(step.optional for step in category.route):
            issue(f"{where}/route", f"category '{category.id}': every step is optional", "invalid-sequence")
        for r_index, agent_id in enumerate(category.requires_any):
            if agent_id not in category.agents:
                issue(f"{where}/requires_any/{r_index}", f"category '{category.id}': requires '{agent_id}', "
                      "which is not a step of its route", "invalid-sequence")
        if len(category.route) > policy.max_delegations:
            issue(f"{where}/route", f"category '{category.id}': {len(category.route)} steps exceed "
                  f"limits.max_delegations ({policy.max_delegations})", "too-many-steps")
        for k_index, skill_id in enumerate(category.supplementary_skills):
            if skill_id not in skills:
                issue(f"{where}/supplementary_skills/{k_index}", unknown("skill", skill_id, skills), "unknown-skill")

    constraint_ids = [c.id for c in policy.step_constraints]
    for index, cid in enumerate(constraint_ids):
        if constraint_ids.count(cid) > 1:
            issue(f"step_constraints/{index}/id", f"duplicate constraint id '{cid}'", "duplicate-id")
    return issues


def _orchestrator_file_issues(orch: str, path: Path, rel: str, label: str) -> list[ValidationIssue]:
    from .frontmatter import FrontmatterError, read_frontmatter

    if not path.is_file():
        return [ValidationIssue(label, "orchestrator/path", f"orchestrator agent file not found: {rel}", "missing-path")]
    try:
        meta = read_frontmatter(path)
    except (FrontmatterError, OSError) as exc:
        return [ValidationIssue(label, "orchestrator/path", f"{rel}: {exc}", "invalid-frontmatter")]
    if meta.get("name") != orch:
        return [ValidationIssue(label, "orchestrator/id", f"{rel}: frontmatter name is '{meta.get('name')}', "
                                f"expected '{orch}'", "name-mismatch")]
    return []


# --------------------------------------------------------------------------- classification


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def signal_hits(text: str, signals: Iterable[str]) -> tuple[str, ...]:
    """Signals (lower-case word prefixes) that occur in ``text``."""
    norm = _normalise(text)
    return tuple(s for s in signals if re.search(r"(?<![a-z0-9])" + re.escape(s), norm))


@dataclass(frozen=True)
class Classification:
    scores: Mapping[str, int]                  # category id -> number of signals found (only > 0)
    best: tuple[str, ...]                      # categories with the top score

    @property
    def category(self) -> str | None:
        return self.best[0] if len(self.best) == 1 else None


def classify(policy: OrchestrationPolicy, task: str) -> Classification:
    scores = {c.id: len(signal_hits(task, c.signals)) for c in policy.categories}
    scores = {cid: score for cid, score in scores.items() if score}
    top = max(scores.values(), default=0)
    best = tuple(cid for cid, score in scores.items() if score == top and top)
    if len(best) > 1:
        # Superset rule: when exactly one tied category's route contains every required step of the
        # others (e.g. feature-delivery vs api-development), the task asks for the broader work.
        def required(cid: str) -> set[str]:
            return {s.agent for s in policy.category(cid).route if not s.optional}

        covering = [cid for cid in best
                    if all(required(other) <= set(policy.category(cid).agents) for other in best if other != cid)]
        if len(covering) == 1:
            best = (covering[0],)
    return Classification(scores, best)


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class PlannedStep:
    agent: str
    purpose: str
    depends_on: tuple[str, ...]
    optional: bool
    reason: str                                # why the step is in the route


@dataclass(frozen=True)
class RoutePlan:
    category: str
    title: str
    steps: tuple[PlannedStep, ...]
    selection: str                             # classified | explicit
    scores: Mapping[str, int] = field(default_factory=dict)
    supplementary_skills: tuple[str, ...] = ()
    policy_version: str = ""

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(step.agent for step in self.steps)


def plan_route(policy: OrchestrationPolicy, task: str, *, category: str | None = None,
               include: Sequence[str] = (), exclude: Sequence[str] = ()) -> RoutePlan:
    """Select the category and the smallest route for ``task``. Raises :class:`OrchestrationError`
    instead of guessing when the task is empty, unclassified or ambiguous."""
    if not task or not task.strip():
        raise OrchestrationError("empty-task", "the task is empty; describe the Django work to do")
    if category is not None:
        chosen, selection, scores = policy.category(category), "explicit", dict(classify(policy, task).scores)
    else:
        result = classify(policy, task)
        scores = dict(result.scores)
        if not result.best:
            raise OrchestrationError("unclassified", "the task matches no orchestration category; choose one with "
                                     "--category", [c.id for c in policy.categories])
        if result.category is None:
            raise OrchestrationError("ambiguous", "the task fits several categories equally "
                                     f"({', '.join(result.best)}); choose one with --category", result.best)
        chosen, selection = policy.category(result.category), "classified"

    include, exclude = set(include), set(exclude)
    optional = {s.agent for s in chosen.route if s.optional}
    for agent_id in sorted((include | exclude) - optional):
        kind = "a required" if agent_id in chosen.agents else "not a"
        raise OrchestrationError("invalid-selection", f"'{agent_id}' is {kind} step of category '{chosen.id}'; "
                                 f"only its optional steps can be selected ({', '.join(sorted(optional)) or 'none'})")
    if include & exclude:
        raise OrchestrationError("invalid-selection", f"{', '.join(sorted(include & exclude))} both included and excluded")

    steps: list[PlannedStep] = []
    included: list[str] = []
    for index, step in enumerate(chosen.route):
        if step.optional:
            hits = signal_hits(task, step.signals)
            if step.agent in exclude or not (hits or step.agent in include):
                continue
            reason = "selected with --with" if step.agent in include else f"task mentions: {', '.join(hits)}"
        else:
            reason = "required step of the route"
        if step.after is None:
            depends = (included[-1],) if included else ()
        else:
            depends = tuple(dict.fromkeys(_nearest_included(chosen, ref, included) for ref in step.after))
            depends = tuple(d for d in depends if d)
        steps.append(PlannedStep(step.agent, step.purpose, depends, step.optional, reason))
        included.append(step.agent)

    if chosen.requires_any and not set(chosen.requires_any) & set(included):
        raise OrchestrationError("missing-implementation", f"category '{chosen.id}' needs at least one of "
                                 f"{', '.join(chosen.requires_any)}; the task does not say which. Name it with --with")
    if len(steps) > policy.max_delegations:
        raise OrchestrationError("too-many-steps", f"{len(steps)} steps exceed limits.max_delegations "
                                 f"({policy.max_delegations})")
    return RoutePlan(chosen.id, chosen.title, tuple(steps), selection, scores, chosen.supplementary_skills,
                     policy.version)


def _nearest_included(category: Category, ref: str, included: list[str]) -> str | None:
    """``ref`` if it was included, else the closest included step before it in the route."""
    agents = list(category.agents)
    for agent_id in reversed(agents[: agents.index(ref) + 1]):
        if agent_id in included:
            return agent_id
    return None


# --------------------------------------------------------------------------- harness adapter


def workflow_document(policy: OrchestrationPolicy, plan: RoutePlan, task: str, *,
                      timeout_seconds: int | None = None) -> dict[str, Any]:
    """The route as an ad-hoc WorkflowDefinition for ``ExecutionManager.start_execution(definition=...)``."""
    safe_task = task.strip().replace("${", "$ {")          # never an ${inputs.x} reference
    total = len(plan.steps)
    constraints = [{"id": c.id, "description": c.description} for c in policy.step_constraints]
    if plan.supplementary_skills:
        constraints.append({"id": "supplementary-skills", "severity": "should", "description":
                            "Read these pack skills before you start: "
                            + ", ".join(f"skills/{s}/SKILL.md" for s in plan.supplementary_skills)})
    stages = []
    for position, step in enumerate(plan.steps, start=1):
        objective = (f"{safe_task}\n\nYou are step {position} of {total} in the orchestrated "
                     f"'{plan.category}' route. Your part: {step.purpose}")
        if step.depends_on:
            objective += " Build on the results of the earlier steps provided in your context."
        stage: dict[str, Any] = {
            "id": step.agent, "name": f"{position}. {step.agent}", "agent_id": step.agent, "objective": objective,
            "depends_on": list(step.depends_on), "constraints": list(constraints),
            "max_attempts": policy.max_attempts_per_step,
            "expected_outputs": [{"name": "report", "type": "report", "description": step.purpose}],
            "extensions": {EXTENSION_KEY: {"position": position, "optional": step.optional, "reason": step.reason}},
        }
        if timeout_seconds:
            stage["timeout_seconds"] = timeout_seconds
        stages.append(stage)
    return {
        "schema_version": "1.0", "kind": "WorkflowDefinition", "id": f"{WORKFLOW_PREFIX}{plan.category}",
        "version": policy.version, "name": f"Orchestrated: {plan.title}", "status": "draft",
        "description": f"Route planned by {policy.orchestrator_id} (policy {policy.version}) for: {safe_task[:200]}",
        "stages": stages,
        "extensions": {EXTENSION_KEY: {"orchestrator": policy.orchestrator_id, "policy_version": policy.version,
                                       "category": plan.category, "selection": plan.selection,
                                       "scores": dict(plan.scores)}},
    }


def plan_summary(plan: RoutePlan) -> dict[str, Any]:
    return {"category": plan.category, "title": plan.title, "selection": plan.selection, "scores": dict(plan.scores),
            "policy_version": plan.policy_version, "supplementary_skills": list(plan.supplementary_skills),
            "steps": [{"agent": s.agent, "purpose": s.purpose, "depends_on": list(s.depends_on),
                       "optional": s.optional, "reason": s.reason} for s in plan.steps]}
