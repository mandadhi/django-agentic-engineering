"""Registry: discovers agents, skills and workflows from configuration.

Nothing is hardcoded — agents and skills come from the registry files named in
framework-config, workflows from the directory/pattern in workflow-config.
:meth:`HarnessRegistry.load` is the single entry point and fails with every
problem found (schema, version, missing references) in one exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Generic, Iterator, Mapping, TypeVar

from .config import AgentSpec, HarnessConfig, SkillSpec, WorkflowSettings, load_config, load_yaml, relative_label
from .config import DEFAULT_FRAMEWORK_FILE
from .contracts import ContractValidator
from .errors import ConfigError, ReferenceValidationError, RegistryLookupError, ValidationIssue, suggest
from .frontmatter import as_list, read_frontmatter
from .log import get_logger
from .validation import validate_references
from .workflows import WorkflowDefinition

log = get_logger(__name__)

T = TypeVar("T")


class Catalog(Generic[T]):
    """Read-only id → item mapping with helpful lookup errors."""

    kind = "item"

    def __init__(self, items: Mapping[str, T]) -> None:
        self._items = dict(items)

    def get(self, item_id: str) -> T:
        try:
            return self._items[item_id]
        except KeyError:
            raise RegistryLookupError(self.kind, item_id, suggest(item_id, self._items)) from None

    def ids(self) -> tuple[str, ...]:
        return tuple(self._items)

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._items

    def __iter__(self) -> Iterator[T]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)


class AgentRegistry(Catalog[AgentSpec]):
    kind = "agent"

    def enabled(self) -> tuple[AgentSpec, ...]:
        return tuple(a for a in self if a.enabled)

    def by_phase(self, phase: str) -> tuple[AgentSpec, ...]:
        return tuple(a for a in self if phase in a.sdlc_phases)


class SkillRegistry(Catalog[SkillSpec]):
    kind = "skill"

    def by_tag(self, tag: str) -> tuple[SkillSpec, ...]:
        return tuple(s for s in self if tag in s.tags)


class WorkflowRegistry(Catalog[WorkflowDefinition]):
    kind = "workflow"


def discover_workflows(settings: WorkflowSettings, contracts: ContractValidator,
                       root: Path) -> tuple[dict[str, WorkflowDefinition], dict[str, str], list[ValidationIssue]]:
    """Load every workflow file matching the configured pattern; validate it against the contract."""
    workflows: dict[str, WorkflowDefinition] = {}
    labels: dict[str, str] = {}
    issues: list[ValidationIssue] = []
    directory = settings.definitions_dir
    if not directory.is_dir():
        return workflows, labels, [ValidationIssue("workflow-config", "discovery/definitions_dir",
                                                   f"directory not found: {directory}", "missing-path")]
    for path in sorted(directory.glob(settings.pattern)):
        label = relative_label(path, root)
        try:
            doc = load_yaml(path, label)
        except ConfigError as exc:
            issues.extend(exc.issues)
            continue
        found = contracts.issues("workflow-definition", doc, label)
        if found:
            issues.extend(found)
            continue
        workflow = WorkflowDefinition.from_dict(doc, settings.defaults, source=path)
        if workflow.id in workflows:
            issues.append(ValidationIssue(label, "id", f"workflow id '{workflow.id}' already defined in "
                                                        f"{labels[workflow.id]}", "duplicate-id"))
            continue
        workflows[workflow.id] = workflow
        labels[workflow.id] = label
    return workflows, labels, issues


def _with_agent_frontmatter(agent: AgentSpec) -> AgentSpec:
    """Fill the fields the agent's own Markdown declares (validated to exist beforehand)."""
    meta = read_frontmatter(agent.path)
    return replace(
        agent,
        description=str(meta["description"]).strip(),
        model=meta.get("model"),
        tools=as_list(meta.get("tools")),
        disallowed_tools=as_list(meta.get("disallowedTools")) or (),
        sdlc_phases=as_list(meta.get("sdlc_phase")) or (),
    )


def _with_skill_frontmatter(skill: SkillSpec) -> SkillSpec:
    meta = read_frontmatter(skill.path / "SKILL.md")
    return replace(skill, description=str(meta["description"]).strip())


def _exclude_workflows(config: HarnessConfig, workflows: dict[str, WorkflowDefinition]
                       ) -> tuple[dict[str, WorkflowDefinition], HarnessConfig, tuple[str, ...]]:
    """Under a profile that excluded agents, also exclude the workflows that use them (and their evaluation
    targets), and record them. In the ``full`` profile nothing is excluded and a missing agent stays an error."""
    gone = {e.split(":", 1)[1] for e in config.excluded if e.startswith("agent:")}
    if not gone:
        return workflows, config, ()
    dropped = tuple(sorted(wid for wid, wf in workflows.items() if any(s.agent_id in gone for s in wf.stages)))
    kept = {wid: wf for wid, wf in workflows.items() if wid not in dropped}
    targets = tuple(t for t in config.evaluation.targets
                    if t.type == "agent" or t.id.split("/", 1)[0] not in dropped)
    config = replace(config, evaluation=replace(config.evaluation, targets=targets),
                     excluded=config.excluded + tuple(f"workflow:{w}" for w in dropped))
    return kept, config, dropped


@dataclass(frozen=True)
class HarnessRegistry:
    config: HarnessConfig
    agents: AgentRegistry
    skills: SkillRegistry
    workflows: WorkflowRegistry
    contracts: ContractValidator
    #: Workflows the profile excluded because a stage uses an excluded agent (``pack`` profile).
    excluded_workflows: tuple[str, ...] = field(default=())

    @classmethod
    def load(cls, root: Path | str, framework_file: str = DEFAULT_FRAMEWORK_FILE, *,
             profile: str | None = None) -> "HarnessRegistry":
        config = load_config(root, framework_file, profile=profile)
        contracts = ContractValidator(config.framework.contracts_dir)
        workflows, labels, issues = discover_workflows(config.workflow, contracts, config.framework.root)
        workflows, config, excluded = _exclude_workflows(config, workflows)
        issues += validate_references(config, workflows, labels)
        if issues:
            raise ReferenceValidationError("Configuration references are invalid", issues)
        registry = cls(
            config=config,
            agents=AgentRegistry({a.id: _with_agent_frontmatter(a) for a in config.agents}),
            skills=SkillRegistry({s.id: _with_skill_frontmatter(s) for s in config.skills}),
            workflows=WorkflowRegistry(workflows),
            contracts=contracts,
            excluded_workflows=excluded,
        )
        log.info("registry.loaded", extra={"agents": len(registry.agents), "skills": len(registry.skills),
                                           "workflows": len(registry.workflows)})
        return registry

    def skills_for(self, agent_id: str) -> tuple[SkillSpec, ...]:
        return tuple(self.skills.get(skill_id) for skill_id in self.agents.get(agent_id).skills)
