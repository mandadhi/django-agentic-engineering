"""Phase 3C: Django engineering rules.

Rules are mandatory standards packaged like skills (``rules/<id>/SKILL.md``), registered in
the skill registry with the ``rule`` tag, and attached to agents through the existing
``skills`` lists. These tests cover discovery, metadata, structure, enforceability,
agent applicability, non-duplication of skill content, and compatibility with the
existing agent and skill registries.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_runtime.plugin_loader import resolve_plugins
from core import HARNESS_HOME
from core.frontmatter import read_markdown
from core.registry import HarnessRegistry

RULES_DIR = HARNESS_HOME / "rules"
SKILLS_DIR = HARNESS_HOME / "skills"

# rule id -> requirement-id prefix
RULES: dict[str, str] = {
    "django-project-structure": "STR",
    "django-coding-standards": "COD",
    "django-model-standards": "MOD",
    "django-api-standards": "API",
    "django-security-standards": "SEC",
    "django-database-standards": "DB",
    "django-testing-standards": "TST",
    "django-performance-standards": "PRF",
    "django-configuration-standards": "CFG",
    "django-logging-standards": "LOG",
    "django-documentation-standards": "DOC",
    "django-dependency-standards": "DEP",
}
RULE_IDS = sorted(RULES)
SECURITY_RULE = "django-security-standards"
REQUIRED_SECTIONS = ("Purpose", "Scope", "Mandatory requirements", "Recommendations", "Anti-patterns",
                     "Security and maintainability", "Verification", "Related skills")
SDLC_PHASES = {"Requirements", "Architecture/Planning", "Design", "Development", "Testing", "Security",
               "Deployment/Maintenance"}
MAX_BODY_CHARS = 7000
ORIGINAL_SKILLS = {
    "owasp-top10-review", "secret-detection-patterns", "security-scan-gates", "db-schema-conventions",
    "db-migration-playbook", "api-design-standards", "api-versioning-policy", "pr-review-standards",
    "pr-diff-hygiene", "django-core", "django-architecture", "django-orm", "django-rest-framework",
    "postgresql-engineering", "django-security-engineering", "django-testing", "django-performance", "django-frontend",
    "django-integrations", "django-data-processing", "django-report-generation",
}


def rule_file(rule_id: str) -> Path:
    return RULES_DIR / rule_id / "SKILL.md"


def body_of(rule_id: str) -> str:
    return read_markdown(rule_file(rule_id))[1]


def sections(body: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    found: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip().split(" — ")[0]          # "Mandatory requirements — A. Code"
        found[heading] = found.get(heading, "") + parts[i + 1]
    return found


def django_agents(registry: HarnessRegistry):
    return [a for a in registry.agents if a.plugin == "django-engineering"]


# ---------------------------------------------------------------------- discovery and registration


def test_rules_discovered_by_tag(registry: HarnessRegistry) -> None:
    rules = registry.skills.by_tag("rule")
    assert {r.id for r in rules} == set(RULES)
    for rule in rules:
        assert rule.source == "plugin" and rule.enabled and rule.plugin == "django-engineering"
        assert rule.rel_path == f"rules/{rule.id}" and rule.path == rule_file(rule.id).parent.resolve()
        # only these tags, so existing topic queries (by_tag("api"), ...) are unaffected
        assert rule.tags == ("django", "rule")
        assert rule.description == str(read_markdown(rule_file(rule.id))[0]["description"]).strip()


def test_every_rule_directory_is_registered(registry: HarnessRegistry) -> None:
    on_disk = {p.parent.name for p in RULES_DIR.glob("*/SKILL.md")}
    assert on_disk == set(RULES)
    assert {p.name for p in RULES_DIR.iterdir()} == set(RULES) | {"README.md"}


def test_rules_are_not_mixed_into_skills_dir() -> None:
    assert not set(RULES) & {p.name for p in SKILLS_DIR.iterdir()}


# ---------------------------------------------------------------------- metadata and structure


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_rule_frontmatter(rule_id: str) -> None:
    meta, _ = read_markdown(rule_file(rule_id))
    assert meta["name"] == rule_id
    assert meta["project"] == "django-agentic-engineering"
    assert meta["sdlc_phase"] in SDLC_PHASES
    for key in ("description", "abstract", "tech_stack", "domain"):
        assert meta.get(key), f"{rule_id}: frontmatter '{key}' is missing"
    assert "Django" in meta["tech_stack"] or "Django" in str(meta["description"])
    assert "model" not in meta and "tools" not in meta          # a rule is not an agent and grants nothing


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_rule_sections_and_size(rule_id: str) -> None:
    body = body_of(rule_id)
    found = sections(body)
    missing = [s for s in REQUIRED_SECTIONS if s not in found]
    assert not missing, f"{rule_id}: missing sections {missing}"
    assert all(found[s].strip() for s in REQUIRED_SECTIONS)
    assert len(body) <= MAX_BODY_CHARS, f"{rule_id}: {len(body)} chars; keep rules concise"
    assert "**MUST / MUST NOT** are mandatory" in body and "**SHOULD** is a recommendation" in body


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_requirements_are_identified_and_separated(rule_id: str) -> None:
    prefix = RULES[rule_id]
    found = sections(body_of(rule_id))
    mandatory = re.findall(rf"\*\*({prefix}-[MA]\d+)\*\*", found["Mandatory requirements"])
    recommended = re.findall(rf"\*\*({prefix}-R\d+)\*\*", found["Recommendations"])
    assert len(mandatory) >= 5 and recommended
    assert len(set(mandatory)) == len(mandatory) and len(set(recommended)) == len(recommended)
    # no recommendation hidden among mandatory items, and vice versa
    assert not re.search(rf"\*\*{prefix}-R\d+\*\*", found["Mandatory requirements"])
    assert not re.search(rf"\*\*{prefix}-[MA]\d+\*\*", found["Recommendations"])


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_anti_patterns_cite_existing_requirements(rule_id: str) -> None:
    body = body_of(rule_id)
    defined = set(re.findall(r"\*\*([A-Z]+-[MAR]\d+)\*\*", body))
    table = sections(body)["Anti-patterns"]
    rows = [line for line in table.splitlines() if line.startswith("|") and not line.startswith("|---")][1:]
    assert len(rows) >= 4
    for row in rows:
        cited = re.findall(r"\b[A-Z]+-[MAR]\d+\b", row.rsplit("|", 2)[-2])
        assert cited and set(cited) <= defined, f"{rule_id}: anti-pattern cites unknown id: {row}"


def test_requirement_ids_unique_across_rules() -> None:
    seen: dict[str, str] = {}
    for rule_id in RULE_IDS:
        for req in re.findall(r"\*\*([A-Z]+-[MAR]\d+)\*\*", body_of(rule_id)):
            assert req not in seen, f"{req} defined in {seen.get(req)} and {rule_id}"
            seen[req] = rule_id


def test_cross_rule_references_resolve() -> None:
    for rule_id in RULE_IDS:
        for other in re.findall(r"`(django-[a-z-]+-standards|django-project-structure)`", body_of(rule_id)):
            assert other in RULES, f"{rule_id} references unknown rule {other}"


# ---------------------------------------------------------------------- skills are referenced, not copied


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_related_skills_exist_and_links_resolve(registry: HarnessRegistry, rule_id: str) -> None:
    related = sections(body_of(rule_id))["Related skills"]
    links = re.findall(r"\[([a-z0-9-]+)\]\(([^)]+)\)", related)
    assert links
    rule_ids = {r.id for r in registry.skills.by_tag("rule")}
    for skill_id, target in links:
        assert skill_id in registry.skills and skill_id not in rule_ids
        assert (rule_file(rule_id).parent / target).resolve() == (SKILLS_DIR / skill_id / "SKILL.md").resolve()


def test_rules_do_not_duplicate_skill_content() -> None:
    skill_lines = {line.strip() for path in SKILLS_DIR.rglob("*.md")
                   for line in path.read_text(encoding="utf-8").splitlines() if len(line.strip()) >= 60}
    copied = {r: [line.strip() for line in body_of(r).splitlines() if line.strip() in skill_lines] for r in RULE_IDS}
    assert not any(copied.values()), {k: v for k, v in copied.items() if v}


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_no_secrets_in_rules(registry: HarnessRegistry, rule_id: str) -> None:
    text = rule_file(rule_id).read_text(encoding="utf-8")
    for pattern in registry.config.security.secret_patterns:
        assert not re.search(pattern.regex, text), f"{rule_id} matches secret pattern {pattern.id}"


# ---------------------------------------------------------------------- agent applicability


def test_security_rule_applies_to_every_django_agent(registry: HarnessRegistry) -> None:
    agents = django_agents(registry)
    assert len(agents) == 9
    for agent in agents:
        assert SECURITY_RULE in agent.skills, agent.id
        assert SECURITY_RULE in [s.id for s in registry.skills_for(agent.id)]


def test_security_rule_covers_agent_conduct() -> None:
    conduct = re.findall(r"\*\*(SEC-A\d+)\*\*", body_of(SECURITY_RULE))
    assert conduct == ["SEC-A1", "SEC-A2", "SEC-A3", "SEC-A4"]
    assert "read-only agents do not modify files" in body_of(SECURITY_RULE)


def test_every_rule_is_used_and_code_review_gets_all(registry: HarnessRegistry) -> None:
    used = {s for a in django_agents(registry) for s in a.skills if s in RULES}
    assert used == set(RULES)
    review = registry.agents.get("django-code-review-agent")
    assert set(RULES) <= set(review.skills)


def test_documentation_agent_is_bound_to_documentation_rules(registry: HarnessRegistry) -> None:
    agent = registry.agents.get("django-documentation-agent")
    assert [s for s in agent.skills if s in RULES] == [SECURITY_RULE, "django-documentation-standards"]
    assert "DOC-M8" in body_of("django-documentation-standards")


def test_rules_attach_only_to_django_agents(registry: HarnessRegistry) -> None:
    for agent in registry.agents:
        if agent.plugin != "django-engineering":
            assert not set(agent.skills) & set(RULES), agent.id


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_rules_resolve_through_existing_skill_loading(registry: HarnessRegistry, rule_id: str) -> None:
    """Rules use the skill pipeline unchanged; since Phase 5 they load through the pack plugin like skills."""
    agent = next(a for a in django_agents(registry) if rule_id in a.skills)
    assert f"django-engineering:{rule_id}" in resolve_plugins(registry, agent).skill_names


# ---------------------------------------------------------------------- registry compatibility


def test_existing_skill_registry_entries_unchanged(registry: HarnessRegistry) -> None:
    assert set(registry.skills.ids()) == ORIGINAL_SKILLS | set(RULES)
    assert len(registry.skills) == 33
    for skill_id in ORIGINAL_SKILLS:
        assert "rule" not in registry.skills.get(skill_id).tags
    assert {s.id for s in registry.skills.by_tag("django") if "rule" not in s.tags} == {
        s for s in ORIGINAL_SKILLS if s.startswith("django-") or s == "postgresql-engineering"}


def test_plugin_agents_unchanged(registry: HarnessRegistry) -> None:
    expected = {
        "threat-modeler": ("owasp-top10-review",),
        "security-sast-engineer": ("security-scan-gates", "owasp-top10-review"),
        "secrets-scanner": ("secret-detection-patterns",),
        "security-auditor": ("owasp-top10-review", "security-scan-gates"),
        "db-schema-designer": ("db-schema-conventions",),
        "db-migration-engineer": ("db-migration-playbook", "db-schema-conventions"),
        "api-contract-designer": ("api-design-standards", "api-versioning-policy"),
        "pr-reviewer": ("pr-review-standards", "pr-diff-hygiene"),
    }
    assert {a.id: a.skills for a in registry.agents
            if a.source == "plugin" and a.plugin != "django-engineering"} == expected
