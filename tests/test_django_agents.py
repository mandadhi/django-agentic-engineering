"""Phase 3B: the nine Django engineering agents (harness-local Markdown agents).

Checks registration, metadata, skill wiring, required sections, tool boundaries and that
agents link to skills instead of repeating them. Runtime behaviour is checked with the
fake SDK adapter: until the harness ships as a plugin, a Django agent's local skills cannot
be loaded, and a run must fail closed before the SDK starts.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent_runtime.options_factory import build_agent_definition, load_instructions
from agent_runtime.plugin_loader import ResolvedPlugins, resolve_plugins
from core import HARNESS_HOME
from core.frontmatter import read_markdown
from core.registry import HarnessRegistry
from core.security import SecurityPolicy
from runtime.state import StageStatus

from fakes import FakeSDKAdapter, succeed

AGENTS_DIR = HARNESS_HOME / "agents"
SKILLS_DIR = HARNESS_HOME / "skills"

# Agent id -> (role, skills). The skill lists are the Phase 3B specification.
EXPECTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "django-project-analyzer": ("analyst", ("django-core", "django-architecture", "django-orm")),
    "django-architecture-agent": ("architect", ("django-core", "django-architecture", "django-security-engineering")),
    "django-model-agent": ("engineer", ("django-orm", "postgresql-engineering", "django-architecture")),
    "django-api-agent": ("engineer", ("django-rest-framework", "django-security-engineering", "django-orm")),
    "django-security-agent": ("reviewer", ("django-security-engineering", "django-rest-framework", "django-integrations")),
    "django-testing-agent": ("tester", ("django-testing", "django-core", "django-rest-framework")),
    "django-performance-agent": ("engineer", ("django-performance", "django-orm", "postgresql-engineering")),
    "django-documentation-agent": ("writer", ("django-core", "django-architecture", "django-rest-framework")),
    "django-code-review-agent": ("reviewer", ("django-core", "django-architecture", "django-orm",
                                              "django-rest-framework", "django-security-engineering", "django-testing",
                                              "django-performance")),
}
AGENT_IDS = sorted(EXPECTED)

# Phase 3C: rules each agent must follow (appended after its skills in agent-registry.yaml).
SEC = "django-security-standards"
EXPECTED_RULES: dict[str, tuple[str, ...]] = {
    "django-project-analyzer": (SEC, "django-project-structure", "django-configuration-standards",
                                "django-dependency-standards"),
    "django-architecture-agent": (SEC, "django-project-structure", "django-configuration-standards",
                                  "django-logging-standards", "django-documentation-standards",
                                  "django-dependency-standards"),
    "django-model-agent": (SEC, "django-model-standards", "django-database-standards", "django-coding-standards",
                           "django-testing-standards"),
    "django-api-agent": (SEC, "django-api-standards", "django-coding-standards", "django-testing-standards",
                         "django-performance-standards", "django-logging-standards"),
    "django-security-agent": (SEC, "django-configuration-standards", "django-logging-standards",
                              "django-dependency-standards", "django-api-standards", "django-coding-standards",
                              "django-testing-standards"),
    "django-testing-agent": (SEC, "django-testing-standards", "django-coding-standards"),
    "django-performance-agent": (SEC, "django-performance-standards", "django-database-standards",
                                 "django-coding-standards", "django-testing-standards"),
    "django-documentation-agent": (SEC, "django-documentation-standards"),
    "django-code-review-agent": (SEC, "django-project-structure", "django-coding-standards", "django-model-standards",
                                 "django-api-standards", "django-database-standards", "django-testing-standards",
                                 "django-performance-standards", "django-configuration-standards",
                                 "django-logging-standards", "django-documentation-standards",
                                 "django-dependency-standards"),
}

# Tool sets are pinned: adding rules (or anything else) must never widen an agent's tools.
RW = ("Read", "Write", "Edit", "Grep", "Glob", "Bash")
EXPECTED_TOOLS: dict[str, tuple[str, ...]] = {
    "django-project-analyzer": ("Read", "Grep", "Glob", "Bash"),
    "django-architecture-agent": RW,
    "django-model-agent": RW,
    "django-api-agent": RW,
    "django-security-agent": RW,
    "django-testing-agent": RW,
    "django-performance-agent": RW,
    "django-documentation-agent": ("Read", "Write", "Edit", "Grep", "Glob"),
    "django-code-review-agent": ("Read", "Grep", "Glob", "Bash"),
}

REQUIRED_SECTIONS = ("Purpose", "Responsibilities", "Scope and boundaries", "Required skills", "Mandatory rules",
                     "Tools", "Inputs", "Execution", "Output", "Error handling", "Security", "Completion criteria")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_ONLY = {"django-project-analyzer", "django-code-review-agent"}
NEVER_DECLARED = {"WebFetch", "WebSearch", "Task", "Agent"}   # no web access, no subagent spawning
SDLC_PHASES = {"Requirements", "Architecture/Planning", "Design", "Development", "Testing", "Security",
               "Deployment/Maintenance"}
MAX_BODY_CHARS = 9000                                          # agents stay concise; knowledge lives in skills


def body_of(agent_id: str) -> str:
    return read_markdown(AGENTS_DIR / f"{agent_id}.md")[1]


def sections(body: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


# ---------------------------------------------------------------------- registration


def test_all_nine_agents_registered_in_the_pack_plugin(registry: HarnessRegistry) -> None:
    pack = {a.id for a in registry.agents if a.plugin == "django-engineering"}
    assert pack == set(EXPECTED)
    for agent_id in AGENT_IDS:
        agent = registry.agents.get(agent_id)
        assert agent.source == "plugin" and agent.rel_path == f"agents/{agent_id}.md"
        assert agent.path == (AGENTS_DIR / f"{agent_id}.md").resolve()      # the canonical file, not a copy
        assert agent.path.is_file() and agent.enabled


def test_every_agent_file_is_registered(registry: HarnessRegistry) -> None:
    files = {p.stem for p in AGENTS_DIR.glob("*.md") if not p.stem.isupper()}   # skip README.md
    assert files == set(EXPECTED)
    assert {registry.agents.get(i).path.name for i in files} == {f"{i}.md" for i in files}


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_roles_and_skills_match_the_specification(registry: HarnessRegistry, agent_id: str) -> None:
    role, skills = EXPECTED[agent_id]
    agent = registry.agents.get(agent_id)
    assert agent.role == role
    assert agent.skills == skills + EXPECTED_RULES[agent_id]          # skills first, then rules
    for skill in registry.skills_for(agent_id):          # every skill resolves to a real pack skill
        assert skill.plugin == "django-engineering" and (skill.path / "SKILL.md").is_file()
        assert skill.path.name == skill.id


def test_existing_plugin_agents_are_unchanged(registry: HarnessRegistry) -> None:
    plugin_agents = {a.id for a in registry.agents if a.source == "plugin" and a.plugin != "django-engineering"}
    assert plugin_agents == {"threat-modeler", "security-sast-engineer", "secrets-scanner", "security-auditor",
                             "db-schema-designer", "db-migration-engineer", "api-contract-designer", "pr-reviewer"}


# ---------------------------------------------------------------------- metadata and structure


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_frontmatter(registry: HarnessRegistry, agent_id: str) -> None:
    meta, _ = read_markdown(AGENTS_DIR / f"{agent_id}.md")
    assert meta["name"] == agent_id
    assert meta["project"] == "django-agentic-engineering"
    assert meta["model"] in {"opus", "sonnet", "haiku"}
    assert meta["sdlc_phase"] in SDLC_PHASES
    for key in ("description", "abstract", "tech_stack", "domain"):
        assert meta.get(key), f"{agent_id}: frontmatter '{key}' is missing"
    agent = registry.agents.get(agent_id)                 # what the registry read from the file
    assert agent.tools and agent.description == str(meta["description"]).strip()


def test_descriptions_are_distinct(registry: HarnessRegistry) -> None:
    descriptions = [registry.agents.get(i).description for i in AGENT_IDS]
    assert len(set(descriptions)) == len(descriptions)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_required_sections_present_and_concise(agent_id: str) -> None:
    body = body_of(agent_id)
    found = sections(body)
    missing = [s for s in REQUIRED_SECTIONS if s not in found]
    assert not missing, f"{agent_id}: missing sections {missing}"
    assert all(found[s].strip() for s in REQUIRED_SECTIONS)
    assert len(body) <= MAX_BODY_CHARS, f"{agent_id}: {len(body)} chars; move detail into a skill"
    assert "- [ ]" in found["Completion criteria"]
    assert "AgentResult" in found["Output"]


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_required_skills_section_matches_registry(registry: HarnessRegistry, agent_id: str) -> None:
    table = sections(body_of(agent_id))["Required skills"]
    listed = tuple(re.findall(r"^\| `([a-z0-9-]+)` \|", table, flags=re.M))
    rules = {s.id for s in registry.skills.by_tag("rule")}
    assert listed == tuple(s for s in registry.agents.get(agent_id).skills if s not in rules)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_mandatory_rules_section_matches_registry(registry: HarnessRegistry, agent_id: str) -> None:
    section = sections(body_of(agent_id))["Mandatory rules"]
    listed = tuple(re.findall(r"`(django-[a-z-]+)`", section))
    rules = {s.id for s in registry.skills.by_tag("rule")}
    assert listed == tuple(s for s in registry.agents.get(agent_id).skills if s in rules) == EXPECTED_RULES[agent_id]


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_handoffs_name_registered_agents(registry: HarnessRegistry, agent_id: str) -> None:
    mentioned = set(re.findall(r"`(django-[a-z-]+-agent|django-project-analyzer)`", body_of(agent_id)))
    assert mentioned <= set(registry.agents.ids()), mentioned - set(registry.agents.ids())


# ---------------------------------------------------------------------- tools and security


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_tool_boundaries(registry: HarnessRegistry, agent_id: str) -> None:
    assert registry.agents.get(agent_id).tools == EXPECTED_TOOLS[agent_id]     # no permission drift
    assert not registry.agents.get(agent_id).disallowed_tools
    tools = set(registry.agents.get(agent_id).tools)
    assert tools <= {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}
    assert not tools & NEVER_DECLARED
    if agent_id in READ_ONLY:
        assert not tools & WRITE_TOOLS, f"{agent_id} is read-only"
    effective = SecurityPolicy(registry.config.security, registry.agents).effective_tools(agent_id)
    assert effective is not None and not set(effective) & {"WebFetch", "WebSearch"}


def test_documentation_agent_has_no_shell(registry: HarnessRegistry) -> None:
    assert "Bash" not in registry.agents.get("django-documentation-agent").tools


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_secrets_in_agent_files(registry: HarnessRegistry, agent_id: str) -> None:
    text = (AGENTS_DIR / f"{agent_id}.md").read_text(encoding="utf-8")
    for pattern in registry.config.security.secret_patterns:
        assert not re.search(pattern.regex, text), f"{agent_id} matches secret pattern {pattern.id}"


# ---------------------------------------------------------------------- skills are linked, not copied


def _skill_lines() -> set[str]:
    lines: set[str] = set()
    for path in SKILLS_DIR.rglob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if len(line) >= 60:
                lines.add(line)
    return lines


def test_agents_do_not_duplicate_skill_content() -> None:
    skill_lines = _skill_lines()
    copied = {agent_id: [line.strip() for line in body_of(agent_id).splitlines() if line.strip() in skill_lines]
              for agent_id in AGENT_IDS}
    assert not any(copied.values()), {k: v for k, v in copied.items() if v}


# ---------------------------------------------------------------------- runtime (fake SDK)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_definition_built_from_markdown(registry: HarnessRegistry, agent_id: str) -> None:
    agent = registry.agents.get(agent_id)
    instructions = load_instructions(agent)
    policy = SecurityPolicy(registry.config.security, registry.agents)
    definition = build_agent_definition(agent, instructions, policy, ResolvedPlugins((), ()), structured_output=True)
    assert definition.prompt == body_of(agent_id)
    assert definition.model == agent.model
    assert definition.tools == [*policy.effective_tools(agent_id), "StructuredOutput"]


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_skills_resolve_through_the_pack_plugin(registry: HarnessRegistry, agent_id: str) -> None:
    """Phase 5: the skills load through the registered django-engineering plugin (was plugin.unresolved)."""
    resolved = resolve_plugins(registry, registry.agents.get(agent_id))
    assert [p.id for p in resolved.plugins] == ["django-engineering"]
    assert resolved.skill_names == tuple(f"django-engineering:{s}" for s in registry.agents.get(agent_id).skills)


def test_run_reaches_the_sdk(make_runner, workspace: Path) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    execution = runner.start_agent("django-project-analyzer", "Profile the project.", workspace=workspace)
    [stage] = asyncio.run(runner.run_workflow(execution.execution_id)).stages
    assert stage.stage_status is StageStatus.SUCCEEDED, stage.failure
    assert len(adapter.calls) == 1
