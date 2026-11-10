"""Phase 3E: Django output templates (``templates/*.template.md``) and shared references
(``references/*.md``).

Covers discovery, template structure, required and optional sections, placeholder consistency,
reference discovery and links, non-duplication of skill, rule, agent and command text, the
agent-to-template and command-to-template mappings, compatibility with the existing agents,
commands, skills, rules and runtime prompt template, and the absence of any permission change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_runtime import prompt
from core import HARNESS_HOME
from core.frontmatter import read_markdown
from core.registry import HarnessRegistry

from test_django_agents import EXPECTED_TOOLS, READ_ONLY, WRITE_TOOLS
from test_django_commands import EXPECTED as COMMAND_AGENT

TEMPLATES_DIR = HARNESS_HOME / "templates"
REFERENCES_DIR = HARNESS_HOME / "references"
AGENTS_DIR = HARNESS_HOME / "agents"
COMMANDS_DIR = HARNESS_HOME / "commands"
SKILLS_DIR = HARNESS_HOME / "skills"
RULES_DIR = HARNESS_HOME / "rules"

# agent -> templates (primary template first). This mapping is the Phase 3E specification.
AGENT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "django-project-analyzer": ("project-analysis",),
    "django-architecture-agent": ("architecture-design", "architecture-decision-record", "implementation-plan"),
    "django-model-agent": ("django-model-specification", "migration-plan"),
    "django-api-agent": ("api-specification",),
    "django-security-agent": ("security-assessment",),
    "django-testing-agent": ("test-plan",),
    "django-performance-agent": ("performance-assessment",),
    "django-documentation-agent": ("technical-documentation",),
    "django-code-review-agent": ("code-review-report",),
}
# agent -> shared references.
AGENT_REFERENCES: dict[str, tuple[str, ...]] = {
    "django-project-analyzer": ("django-project-discovery", "django-version-compatibility"),
    "django-architecture-agent": ("django-project-discovery", "django-version-compatibility"),
    "django-model-agent": ("django-migration-safety", "django-version-compatibility"),
    "django-api-agent": ("django-api-design-checklist", "django-security-checklist", "django-testing-strategy"),
    "django-security-agent": ("django-security-checklist", "django-version-compatibility"),
    "django-testing-agent": ("django-testing-strategy",),
    "django-performance-agent": ("django-performance-checklist", "django-migration-safety"),
    "django-documentation-agent": ("django-project-discovery",),
    "django-code-review-agent": ("django-security-checklist", "django-api-design-checklist",
                                 "django-performance-checklist", "django-migration-safety", "django-testing-strategy"),
}
# command -> templates named in its Expected output section.
COMMAND_TEMPLATES: dict[str, tuple[str, ...]] = {
    "django-analyze": ("project-analysis",),
    "django-design": ("architecture-design", "architecture-decision-record", "implementation-plan"),
    "django-model": ("django-model-specification", "migration-plan"),
    "django-api": ("api-specification",),
    "django-security": ("security-assessment",),
    "django-test": ("test-plan",),
    "django-optimize": ("performance-assessment",),
    "django-document": ("technical-documentation",),
    "django-review": ("code-review-report",),
}
EXPECTED_TEMPLATES = frozenset({
    "project-analysis", "architecture-design", "architecture-decision-record", "django-model-specification",
    "api-specification", "security-assessment", "test-plan", "performance-assessment", "technical-documentation",
    "code-review-report", "implementation-plan", "migration-plan",
})
EXPECTED_REFERENCES = frozenset({
    "django-version-compatibility", "django-project-discovery", "django-security-checklist",
    "django-testing-strategy", "django-migration-safety", "django-api-design-checklist",
    "django-performance-checklist",
})
TEMPLATE_IDS = sorted(EXPECTED_TEMPLATES)
REFERENCE_IDS = sorted(EXPECTED_REFERENCES)
AGENT_IDS = sorted(AGENT_TEMPLATES)
COMMAND_IDS = sorted(COMMAND_TEMPLATES)

TEMPLATE_SECTIONS = ("Purpose", "When to use", "Placeholders", "Sections", "Filling rules", "Output structure",
                     "Completion checklist")
REFERENCE_FIRST_SECTIONS = ("Purpose", "Scope", "How to use")
REFERENCE_LAST_SECTION = "Related skills and rules"
HEADER_PLACEHOLDERS = {"subject", "project_name", "scope", "prepared_by", "date", "document_status", "sources"}
HEADER_ROWS = ("| Project | {{project_name}} |", "| Scope | {{scope}} |", "| Prepared by | {{prepared_by}} |",
               "| Date | {{date}} |", "| Status | {{document_status}} |", "| Sources | {{sources}} |")
SDLC_PHASES = {"Requirements", "Architecture/Planning", "Design", "Development", "Testing", "Security",
               "Deployment/Maintenance"}
# Frontmatter that would make a template or reference grant tools, pick a model or change permissions.
FORBIDDEN_KEYS = {"tools", "allowed-tools", "allowed_tools", "allowedTools", "disallowedTools", "disallowed-tools",
                  "model", "agent", "context", "hooks", "permissionMode", "permission-mode", "mcpServers"}
REQ_ID = r"\b(?:SEC|DOC|STR|COD|MOD|API|DB|TST|PRF|CFG|LOG|DEP)-[MAR]\d+\b"
# Version-specific claims the pack must not hard-code (they are looked up at the time of use).
VERSION_CLAIM = r"(?i)\b(?:django|drf|django rest framework|python|postgres(?:ql)?|psycopg|celery)\s+v?\d+(?:\.\d+)?\b"


# ---------------------------------------------------------------------- parsing helpers


def template_file(template_id: str) -> Path:
    return TEMPLATES_DIR / f"{template_id}.template.md"


def reference_file(reference_id: str) -> Path:
    return REFERENCES_DIR / f"{reference_id}.md"


def _fence_open(line: str) -> int:
    match = re.match(r"^\s*(`{3,})", line)
    return len(match.group(1)) if match else 0


def _iter_unfenced(lines: list[str]):
    """Yield (line, inside_fence) with nested fences of different lengths handled."""
    fence = 0
    for line in lines:
        ticks = _fence_open(line)
        if fence == 0 and ticks:
            fence = ticks
            yield line, True
        elif fence and ticks >= fence and not line.strip().strip("`"):
            fence = 0
            yield line, True
        else:
            yield line, fence != 0


def sections(body: str) -> dict[str, str]:
    """`## ` sections outside code fences, in order."""
    found: dict[str, list[str]] = {}
    current = None
    for line, fenced in _iter_unfenced(body.splitlines()):
        if not fenced and line.startswith("## "):
            current = line[3:].strip()
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return {k: "\n".join(v) for k, v in found.items()}


def skeleton(template_id: str) -> str:
    """The ````markdown skeleton inside the Output structure section."""
    lines = sections(read_markdown(template_file(template_id))[1])["Output structure"].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("````markdown"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "````")
    return "\n".join(lines[start + 1:end])


def skeleton_headings(text: str, level: str = "## ") -> list[str]:
    return [line[len(level):].strip() for line, fenced in _iter_unfenced(text.splitlines())
            if not fenced and line.startswith(level)]


def table_rows(text: str) -> list[list[str]]:
    """Data rows of the first Markdown table in ``text`` (header and separator skipped)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    block = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        block.append(line)
    rows = [line for line in block[1:] if not re.match(r"^\|[-| ]+\|$", line)]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def section_rows(template_id: str) -> list[tuple[str, str]]:
    table = sections(read_markdown(template_file(template_id))[1])["Sections"]
    return [(row[0], row[1]) for row in table_rows(table)]


def declared_placeholders(template_id: str) -> list[str]:
    table = sections(read_markdown(template_file(template_id))[1])["Placeholders"]
    return [re.fullmatch(r"`\{\{([^}]+)\}\}`", row[0]).group(1) for row in table_rows(table)]


def listed_ids(section: str, label: str) -> tuple[str, ...]:
    line = next(line for line in section.splitlines() if line.strip().startswith(f"- {label}"))
    return tuple(re.findall(r"`([a-z0-9-]+)`", line.split(":", 1)[1]))


def agent_body(agent_id: str) -> str:
    return read_markdown(AGENTS_DIR / f"{agent_id}.md")[1]


def command_body(command_id: str) -> str:
    return read_markdown(COMMANDS_DIR / f"{command_id}.md")[1]


def agent_inline_output(agent_id: str) -> tuple[str | None, list[str]]:
    """The H1 title prefix and the `##` headings of the skeleton in the agent's Output section."""
    output = sections(agent_body(agent_id))["Output"]
    headings = [re.match(r"^## (.+?)(?:\s{2,}.*)?$", line).group(1).strip()
                for line in output.splitlines() if line.startswith("## ")]
    title = next((line[2:].split(" — ")[0].strip() for line in output.splitlines() if line.startswith("# ")), None)
    if not headings:                                         # prose form: "has these sections: A · B (note) · C."
        listing = re.search(r"has these sections:(.+?)\.\s*$", " ".join(output.split()), flags=re.S)
        headings = [re.sub(r"\(.*?\)", "", part).strip() for part in listing.group(1).split("·")]
    return title, headings


def slug(heading: str) -> str:
    heading = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", heading)


def markdown_links(text: str) -> list[str]:
    return re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text)


def pack_files() -> list[Path]:
    return [*(template_file(t) for t in TEMPLATE_IDS), *(reference_file(r) for r in REFERENCE_IDS)]


def _long_lines(paths) -> set[str]:
    return {line.strip() for path in paths for line in path.read_text(encoding="utf-8").splitlines()
            if len(line.strip()) >= 60}


# ---------------------------------------------------------------------- template discovery


def test_all_twelve_templates_discovered() -> None:
    found = {p.name[: -len(".template.md")] for p in TEMPLATES_DIR.glob("*.template.md")}
    assert found == EXPECTED_TEMPLATES
    assert {p.name for p in TEMPLATES_DIR.iterdir()} == ({f"{t}.template.md" for t in EXPECTED_TEMPLATES}
                                                         | {"README.md", "agent-task.prompt.md"})


def test_runtime_prompt_template_is_separate_and_unchanged_in_role() -> None:
    assert prompt.TEMPLATE == TEMPLATES_DIR / "agent-task.prompt.md" and prompt.TEMPLATE.is_file()
    text = prompt.TEMPLATE.read_text(encoding="utf-8")
    assert "{{" not in text                                  # output-template placeholders never leak into it
    for template_id in TEMPLATE_IDS:
        assert "${" not in template_file(template_id).read_text(encoding="utf-8")   # no string.Template syntax


def test_templates_readme_lists_every_template_with_its_producers() -> None:
    readme = (TEMPLATES_DIR / "README.md").read_text(encoding="utf-8")
    assert "## Filling conventions" in readme
    for template_id in TEMPLATE_IDS:
        row = next(line for line in readme.splitlines()
                   if line.startswith(f"| [{template_id}]({template_id}.template.md) |"))
        meta = read_markdown(template_file(template_id))[0]
        for agent_id in meta["agents"]:
            assert agent_id in row, (template_id, agent_id)
        for command_id in meta["commands"]:
            assert f"/{command_id}" in row, (template_id, command_id)


# ---------------------------------------------------------------------- template structure


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_frontmatter(template_id: str) -> None:
    meta = read_markdown(template_file(template_id))[0]
    assert meta["name"] == template_id
    assert meta["kind"] == "output-template"
    assert meta["project"] == "django-agentic-engineering"
    assert meta["sdlc_phase"] in SDLC_PHASES
    for key in ("description", "abstract", "tech_stack", "domain", "agents", "commands"):
        assert meta.get(key), f"{template_id}: frontmatter '{key}' is missing"
    assert "Django" in str(meta["description"]) or "Django" in meta["tech_stack"]
    assert not set(meta) & FORBIDDEN_KEYS


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_sections_in_order(template_id: str) -> None:
    found = sections(read_markdown(template_file(template_id))[1])
    assert tuple(found) == TEMPLATE_SECTIONS, f"{template_id}: {tuple(found)}"
    assert all(found[s].strip() for s in TEMPLATE_SECTIONS)
    assert "- [ ]" in found["Completion checklist"]
    assert len(re.findall(r"^- \[ \]", found["Completion checklist"], flags=re.M)) >= 3
    assert re.search(r"\bNot (for|needed)\b", found["When to use"]), f"{template_id}: say when not to use it"
    assert "README.md#filling-conventions" in found["Filling rules"]


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_output_structure_has_title_and_standard_header(template_id: str) -> None:
    text = skeleton(template_id)
    assert text.startswith("# "), f"{template_id}: skeleton must start with an H1 title"
    for row in HEADER_ROWS:
        assert row in text, f"{template_id}: header row {row!r} missing"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_required_and_optional_sections_match_the_skeleton(template_id: str) -> None:
    rows = section_rows(template_id)
    assert [name for name, _ in rows] == skeleton_headings(skeleton(template_id))
    flags = [flag for _, flag in rows]
    assert set(flags) <= {"required", "optional"}
    assert flags.count("required") >= 3 and flags.count("optional") >= 1, template_id


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_skeleton_contains_no_fabricated_values(template_id: str) -> None:
    text = skeleton(template_id)
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b", text), "concrete date"
    assert not re.search(r"\b\d+\.\d+\b", text), "concrete version or measurement"
    assert not re.search(r"(?i)\b(lorem|example\.com|acme|foo|bar)\b", text)
    assert not re.search(r"https?://", text)


# ---------------------------------------------------------------------- placeholders


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_placeholders_are_declared_and_used(template_id: str) -> None:
    declared = declared_placeholders(template_id)
    used = set(re.findall(r"\{\{([^}]*)\}\}", skeleton(template_id)))
    assert len(declared) == len(set(declared)), f"{template_id}: duplicate placeholder rows"
    assert used == set(declared), f"{template_id}: undeclared {used - set(declared)}, unused {set(declared) - used}"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_placeholder_naming_and_standard_set(template_id: str) -> None:
    text = template_file(template_id).read_text(encoding="utf-8")
    for name in re.findall(r"\{\{([^}]*)\}\}", text):
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"{template_id}: bad placeholder {name!r}"
    assert HEADER_PLACEHOLDERS <= set(declared_placeholders(template_id))
    assert not re.search(r"\{[a-z_]+\}(?!\})", text.replace("{{", "").replace("}}", "")), "single-brace placeholder"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_shared_placeholders_mean_the_same_everywhere(template_id: str) -> None:
    table = sections(read_markdown(template_file(template_id))[1])["Placeholders"]
    rows = {row[0].strip("`{}"): row for row in table_rows(table)}
    assert rows["project_name"][1] == "Name of the Django project"
    assert "ISO 8601" in rows["date"][1]
    assert rows["prepared_by"][1].startswith("Agent or person")
    assert re.fullmatch(r"(`[a-z-]+`(, | or )?)+.*", rows["document_status"][1]), rows["document_status"][1]
    for name, row in rows.items():
        assert row[1] and row[2], f"{template_id}: {name} needs a meaning and a source"


# ---------------------------------------------------------------------- shared references


def test_all_seven_references_discovered() -> None:
    assert {p.stem for p in REFERENCES_DIR.glob("*.md") if not p.stem.isupper()} == EXPECTED_REFERENCES
    assert {p.name for p in REFERENCES_DIR.iterdir()} == {f"{r}.md" for r in EXPECTED_REFERENCES} | {"README.md"}


def test_references_readme_lists_every_reference() -> None:
    readme = (REFERENCES_DIR / "README.md").read_text(encoding="utf-8")
    for reference_id in REFERENCE_IDS:
        assert f"| [{reference_id}]({reference_id}.md) |" in readme, reference_id


@pytest.mark.parametrize("reference_id", REFERENCE_IDS)
def test_reference_frontmatter(reference_id: str) -> None:
    meta = read_markdown(reference_file(reference_id))[0]
    assert meta["name"] == reference_id and meta["kind"] == "shared-reference"
    assert meta["project"] == "django-agentic-engineering" and meta["sdlc_phase"] in SDLC_PHASES
    for key in ("description", "abstract", "tech_stack", "domain", "agents"):
        assert meta.get(key), f"{reference_id}: frontmatter '{key}' is missing"
    assert "Django" in meta["tech_stack"]
    assert not set(meta) & FORBIDDEN_KEYS


@pytest.mark.parametrize("reference_id", REFERENCE_IDS)
def test_reference_sections(reference_id: str) -> None:
    found = tuple(sections(read_markdown(reference_file(reference_id))[1]))
    assert found[:3] == REFERENCE_FIRST_SECTIONS and found[-1] == REFERENCE_LAST_SECTION, found
    assert len(found) >= 5, f"{reference_id}: needs topic sections between scope and related links"


@pytest.mark.parametrize("reference_id", REFERENCE_IDS)
def test_reference_links_to_existing_skills_and_rules(registry: HarnessRegistry, reference_id: str) -> None:
    related = sections(read_markdown(reference_file(reference_id))[1])[REFERENCE_LAST_SECTION]
    rule_ids = {s.id for s in registry.skills.by_tag("rule")}
    skills = set(re.findall(r"\(\.\./skills/([a-z0-9-]+)/", related))
    rules = set(re.findall(r"\(\.\./rules/([a-z0-9-]+)/SKILL\.md\)", related))
    assert skills and rules, f"{reference_id}: link at least one skill and one rule"
    assert skills <= set(registry.skills.ids()) - rule_ids
    assert rules <= rule_ids


@pytest.mark.parametrize("path", pack_files(), ids=lambda p: p.name)
def test_relative_links_and_anchors_resolve(path: Path) -> None:
    for target in markdown_links(path.read_text(encoding="utf-8")):
        if re.match(r"^[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        destination = (path.parent / file_part).resolve() if file_part else path
        assert destination.is_file(), f"{path.name}: broken link {target}"
        if anchor:
            headings = re.findall(r"^#{1,6} (.+)$", destination.read_text(encoding="utf-8"), flags=re.M)
            assert anchor in {slug(h) for h in headings}, f"{path.name}: missing anchor {target}"


@pytest.mark.parametrize("path", pack_files(), ids=lambda p: p.name)
def test_cited_requirement_ids_exist(path: Path) -> None:
    defined = {req for rule in RULES_DIR.glob("*/SKILL.md")
               for req in re.findall(r"\*\*([A-Z]+-[MAR]\d+)\*\*", rule.read_text(encoding="utf-8"))}
    cited = set(re.findall(REQ_ID, path.read_text(encoding="utf-8")))
    assert cited <= defined, cited - defined


@pytest.mark.parametrize("path", pack_files(), ids=lambda p: p.name)
def test_no_version_specific_assumptions(path: Path) -> None:
    body = read_markdown(path)[1]
    assert not re.search(VERSION_CLAIM, body), re.search(VERSION_CLAIM, body).group(0)
    assert not re.search(r"\b(19|20)\d{2}\b", body), "dates or years belong to a lookup, not the pack"


def test_every_reference_is_cited_by_its_rule_ids_not_rule_text() -> None:
    for reference_id in REFERENCE_IDS:
        assert re.search(REQ_ID, reference_file(reference_id).read_text(encoding="utf-8")), reference_id


# ---------------------------------------------------------------------- no duplicated knowledge


def test_templates_and_references_do_not_copy_skill_or_rule_text() -> None:
    existing = _long_lines([*SKILLS_DIR.rglob("*.md"), *RULES_DIR.rglob("*.md")])
    copied = {p.name: [line for line in read_markdown(p)[1].splitlines() if line.strip() in existing]
              for p in pack_files()}
    assert not any(copied.values()), {k: v for k, v in copied.items() if v}


def test_templates_and_references_do_not_copy_agent_or_command_text() -> None:
    existing = _long_lines([*AGENTS_DIR.glob("*.md"), *COMMANDS_DIR.glob("*.md")])
    copied = {p.name: [line for line in read_markdown(p)[1].splitlines() if line.strip() in existing]
              for p in pack_files()}
    assert not any(copied.values()), {k: v for k, v in copied.items() if v}


def test_agents_and_commands_do_not_embed_template_or_reference_content() -> None:
    pack = _long_lines(pack_files())
    for path in [*AGENTS_DIR.glob("django-*.md"), *COMMANDS_DIR.glob("django-*.md")]:
        copied = [line.strip() for line in read_markdown(path)[1].splitlines() if line.strip() in pack]
        assert not copied, (path.name, copied)
        assert "{{" not in path.read_text(encoding="utf-8"), f"{path.name} embeds template placeholders"


def test_references_do_not_copy_each_other() -> None:
    """Content sections are unique per reference; the Related links section is navigation and may overlap."""
    seen: dict[str, str] = {}
    for reference_id in REFERENCE_IDS:
        found = sections(read_markdown(reference_file(reference_id))[1])
        content = "\n".join(text for name, text in found.items() if name != REFERENCE_LAST_SECTION)
        for line in content.splitlines():
            line = line.strip()
            if len(line) >= 60 and not line.startswith("| Check | How to verify"):
                assert seen.setdefault(line, reference_id) == reference_id, (line, seen[line], reference_id)


@pytest.mark.parametrize("path", pack_files(), ids=lambda p: p.name)
def test_no_secrets(registry: HarnessRegistry, path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in registry.config.security.secret_patterns:
        assert not re.search(pattern.regex, text), f"{path.name} matches secret pattern {pattern.id}"


# ---------------------------------------------------------------------- agent mappings


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_section_lists_its_templates_and_references(agent_id: str) -> None:
    found = sections(agent_body(agent_id))
    names = list(found)
    assert names.index("Templates and references") == names.index("Output") + 1
    section = found["Templates and references"]
    assert listed_ids(section, "Templates") == AGENT_TEMPLATES[agent_id]
    assert listed_ids(section, "References") == AGENT_REFERENCES[agent_id]
    assert "add no tools" in " ".join(section.split())


def test_template_frontmatter_agrees_with_agent_sections() -> None:
    for template_id in TEMPLATE_IDS:
        producers = {a for a, templates in AGENT_TEMPLATES.items() if template_id in templates}
        assert producers, f"{template_id} is used by no agent"
        assert set(read_markdown(template_file(template_id))[0]["agents"]) == producers, template_id


def test_reference_frontmatter_agrees_with_agent_sections() -> None:
    for reference_id in REFERENCE_IDS:
        consumers = {a for a, refs in AGENT_REFERENCES.items() if reference_id in refs}
        assert consumers, f"{reference_id} is used by no agent"
        assert set(read_markdown(reference_file(reference_id))[0]["agents"]) == consumers, reference_id


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_primary_template_extends_the_agent_inline_output(agent_id: str) -> None:
    primary = AGENT_TEMPLATES[agent_id][0]
    title, inline = agent_inline_output(agent_id)
    template_headings = skeleton_headings(skeleton(primary))
    missing = [h for h in inline if h not in template_headings]
    assert not missing, f"{agent_id}: inline headings missing from {primary}: {missing}"
    order = [template_headings.index(h) for h in inline]
    assert order == sorted(order), f"{agent_id}: {primary} reorders the agent's sections"
    if title:
        titles = [h.split(" — ")[0].strip() for h in skeleton_headings(skeleton(primary), "# ")]
        assert title in titles, f"{agent_id}: report title {title!r} not in {primary}"


def test_agents_readme_table_matches_mapping() -> None:
    readme = (AGENTS_DIR / "README.md").read_text(encoding="utf-8")
    rows = {row[0]: row for row in table_rows(readme.split("## Templates and shared references (Phase 3E)")[1])}
    for agent_id in AGENT_IDS:
        assert tuple(x.strip() for x in rows[agent_id][1].split(",")) == AGENT_TEMPLATES[agent_id]
        assert tuple(x.strip() for x in rows[agent_id][2].split(",")) == AGENT_REFERENCES[agent_id]


# ---------------------------------------------------------------------- command mappings


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_command_names_its_templates(command_id: str) -> None:
    expected_output = sections(command_body(command_id))["Expected output"]
    named = tuple(t for t in re.findall(r"`([a-z0-9-]+)`", expected_output) if t in EXPECTED_TEMPLATES)
    assert named == COMMAND_TEMPLATES[command_id]
    assert "`templates/<id>.template.md`" in expected_output
    assert set(named) <= set(AGENT_TEMPLATES[COMMAND_AGENT[command_id]]), "command asks for a template its agent lacks"


def test_template_frontmatter_agrees_with_commands() -> None:
    for template_id in TEMPLATE_IDS:
        users = {c for c, templates in COMMAND_TEMPLATES.items() if template_id in templates}
        assert set(read_markdown(template_file(template_id))[0]["commands"]) == users, template_id


def test_commands_readme_table_matches_mapping() -> None:
    readme = (COMMANDS_DIR / "README.md").read_text(encoding="utf-8")
    rows = {row[0]: row[1] for row in table_rows(readme.split("## Output templates (Phase 3E)")[1])}
    for command_id in COMMAND_IDS:
        assert tuple(x.strip() for x in rows[f"/{command_id}"].split(",")) == COMMAND_TEMPLATES[command_id]


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_command_template_line_adds_no_delegation(command_id: str) -> None:
    body = command_body(command_id)
    assert len(re.findall(r"Delegate to the `[a-z0-9-]+` subagent", body)) == 1
    template_paragraph = sections(body)["Expected output"].strip().split("\n\n")[-1]
    assert template_paragraph.startswith("Template")
    assert not re.search(r"`django-[a-z-]+-agent`|subagent|`/django-|--[a-z]", template_paragraph)


# ---------------------------------------------------------------------- no permission changes


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_tools_unchanged(registry: HarnessRegistry, agent_id: str) -> None:
    meta = read_markdown(AGENTS_DIR / f"{agent_id}.md")[0]
    assert tuple(t.strip() for t in str(meta["tools"]).split(",")) == EXPECTED_TOOLS[agent_id]
    assert registry.agents.get(agent_id).tools == EXPECTED_TOOLS[agent_id]
    assert "disallowedTools" not in meta


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_new_agent_section_grants_nothing(agent_id: str) -> None:
    section = sections(agent_body(agent_id))["Templates and references"]
    assert not re.search(r"`(Read|Write|Edit|Grep|Glob|Bash|WebFetch|WebSearch|Task|Agent)`", section)
    assert not re.search(r"(?i)\b(may (write|edit|run)|allowed-tools|permission)\b", section)


def test_read_only_and_documentation_boundaries_preserved(registry: HarnessRegistry) -> None:
    for agent_id in READ_ONLY:
        assert not set(registry.agents.get(agent_id).tools) & WRITE_TOOLS
    documentation = registry.agents.get("django-documentation-agent")
    assert "Bash" not in documentation.tools
    assert "Write" in documentation.tools and set(documentation.tools) <= {"Read", "Write", "Edit", "Grep", "Glob"}


def test_templates_and_references_are_not_wired_into_the_harness(registry: HarnessRegistry) -> None:
    harness_text = "\n".join(p.read_text(encoding="utf-8") for folder in ("config", "core", "runtime", "agent_runtime",
                                                                          "workflows", "contracts")
                             for p in (HARNESS_HOME / folder).rglob("*") if p.suffix in {".py", ".yaml", ".json"})
    assert ".template.md" not in harness_text
    assert not re.search(r"references/django-", harness_text)
    ids = EXPECTED_TEMPLATES | EXPECTED_REFERENCES
    assert not ids & (set(registry.agents.ids()) | set(registry.skills.ids()) | set(registry.workflows.ids()))


# ---------------------------------------------------------------------- compatibility


def test_existing_components_unchanged(registry: HarnessRegistry) -> None:
    assert len(registry.agents) == 17 and len(registry.skills) == 33
    assert len(registry.skills.by_tag("rule")) == 12
    assert {a.id for a in registry.agents if a.plugin == "django-engineering"} == set(AGENT_TEMPLATES)
    assert sorted(COMMAND_AGENT) == COMMAND_IDS
    assert {p.parent.name for p in RULES_DIR.glob("*/SKILL.md")} == {s.id for s in registry.skills.by_tag("rule")}
    assert not (SKILLS_DIR / "templates").exists() and not (SKILLS_DIR / "references.md").exists()
