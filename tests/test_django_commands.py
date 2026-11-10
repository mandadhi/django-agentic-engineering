"""Phase 3D: Django developer commands (Claude Code command files under ``commands/``).

Each command is a thin entry point to exactly one Django agent. These tests cover discovery,
metadata, agent references, skill and rule references, descriptions, the absence of any
permission grant, read-only and documentation-only restrictions, the authorization gates on
code changes, non-duplication of agent, skill and rule text, independence from the Python
harness, and compatibility with the existing agents, skills, rules and commands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import HARNESS_HOME
from core.frontmatter import read_markdown
from core.registry import HarnessRegistry

COMMANDS_DIR = HARNESS_HOME / "commands"
AGENTS_DIR = HARNESS_HOME / "agents"
REPO_ROOT = HARNESS_HOME.parents[1]

# command -> agent. This mapping is the Phase 3D specification.
EXPECTED: dict[str, str] = {
    "django-analyze": "django-project-analyzer",
    "django-design": "django-architecture-agent",
    "django-model": "django-model-agent",
    "django-api": "django-api-agent",
    "django-security": "django-security-agent",
    "django-test": "django-testing-agent",
    "django-optimize": "django-performance-agent",
    "django-document": "django-documentation-agent",
    "django-review": "django-code-review-agent",
}
COMMAND_IDS = sorted(EXPECTED)

REQUIRED_SECTIONS = ("Purpose", "Arguments", "Agent", "Skills and rules", "Authorization", "Steps",
                     "Expected output", "Completion criteria")
SDLC_PHASES = {"Requirements", "Architecture/Planning", "Design", "Development", "Testing", "Security",
               "Deployment/Maintenance"}
ALLOWED_KEYS = {"name", "description", "argument-hint", "project", "abstract", "sdlc_phase", "tech_stack", "domain"}
# Frontmatter that would grant tools, change the model or permissions, or dispatch execution.
FORBIDDEN_KEYS = {"allowed-tools", "allowed_tools", "allowedTools", "tools", "disallowedTools", "disallowed-tools",
                  "model", "agent", "context", "hooks", "permissionMode", "permission-mode", "mcpServers",
                  "disable-model-invocation"}
READ_ONLY = {"django-analyze", "django-review"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
GATED_BY_APPLY = {"django-model", "django-api", "django-test", "django-optimize"}
MAX_BODY_CHARS = 6000
# Claude Code built-in slash commands a pack command must never shadow.
BUILTIN_COMMANDS = {"review", "security-review", "init", "help", "clear", "compact", "config", "agents", "model",
                    "memory", "permissions", "doctor", "cost", "status", "login", "logout", "resume", "mcp", "hooks"}


def body_of(command_id: str) -> str:
    return read_markdown(COMMANDS_DIR / f"{command_id}.md")[1]


def meta_of(command_id: str) -> dict:
    return read_markdown(COMMANDS_DIR / f"{command_id}.md")[0]


def sections(body: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def listed(section: str, label: str) -> tuple[str, ...]:
    line = next(line for line in section.splitlines() if line.strip().startswith(f"- {label}:"))
    return tuple(re.findall(r"`([a-z0-9-]+)`", line))


def split_skills_and_rules(registry: HarnessRegistry, agent_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rules = {s.id for s in registry.skills.by_tag("rule")}
    skills = registry.agents.get(agent_id).skills
    return tuple(s for s in skills if s not in rules), tuple(s for s in skills if s in rules)


# ---------------------------------------------------------------------- discovery


#: Phase 6: /orchestrate briefs the orchestrator, not a specialist (see tests/test_orchestration.py).
ORCHESTRATE = "orchestrate"
#: Phase 5.6: opt-in harness commands; they run the bootstrap script, not an agent
#: (see tests/test_harness_bootstrap.py).
HARNESS_COMMANDS = {"setup-harness", "harness-status", "enable-harness", "disable-harness", "doctor"}


def test_all_nine_commands_discovered() -> None:
    files = {p.stem for p in COMMANDS_DIR.glob("*.md") if not p.stem.isupper()}     # skip README.md
    assert files == set(EXPECTED) | {ORCHESTRATE} | HARNESS_COMMANDS
    assert {p.name for p in COMMANDS_DIR.iterdir()} == {f"{c}.md" for c in EXPECTED} | {f"{ORCHESTRATE}.md",
                                                                                         "README.md"} | {
        f"{c}.md" for c in HARNESS_COMMANDS}


def test_readme_lists_every_command_with_its_agent() -> None:
    readme = (COMMANDS_DIR / "README.md").read_text(encoding="utf-8")
    for command_id, agent_id in EXPECTED.items():
        assert re.search(rf"\[/{command_id}\]\({command_id}\.md\) \| `{agent_id}`", readme), command_id


# ---------------------------------------------------------------------- metadata


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_frontmatter(command_id: str) -> None:
    meta = meta_of(command_id)
    assert meta["name"] == command_id
    assert meta["project"] == "django-agentic-engineering"
    assert meta["sdlc_phase"] in SDLC_PHASES
    for key in ("description", "argument-hint", "abstract", "tech_stack", "domain"):
        assert meta.get(key), f"{command_id}: frontmatter '{key}' is missing"
    assert set(meta) <= ALLOWED_KEYS, f"{command_id}: unexpected keys {set(meta) - ALLOWED_KEYS}"


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_metadata_matches_the_agent(command_id: str) -> None:
    agent_meta = read_markdown(AGENTS_DIR / f"{EXPECTED[command_id]}.md")[0]
    meta = meta_of(command_id)
    for key in ("sdlc_phase", "tech_stack", "domain"):
        assert meta[key] == agent_meta[key], f"{command_id}: {key} differs from its agent"


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_description_states_purpose_and_agent(command_id: str) -> None:
    description = " ".join(str(meta_of(command_id)["description"]).split())
    assert 80 <= len(description) <= 400, f"{command_id}: description length {len(description)}"
    assert EXPECTED[command_id] in description
    assert "Django" in description


def test_descriptions_and_hints_are_distinct() -> None:
    descriptions = [str(meta_of(c)["description"]) for c in COMMAND_IDS]
    hints = [str(meta_of(c)["argument-hint"]) for c in COMMAND_IDS]
    assert len(set(descriptions)) == len(descriptions) and len(set(hints)) == len(hints)


# ---------------------------------------------------------------------- structure and arguments


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_required_sections_present_and_concise(command_id: str) -> None:
    body = body_of(command_id)
    found = sections(body)
    assert tuple(s for s in found if s in REQUIRED_SECTIONS) == REQUIRED_SECTIONS, f"{command_id}: sections"
    assert all(found[s].strip() for s in REQUIRED_SECTIONS)
    assert body.startswith(f"# /{command_id}\n")
    assert len(body) <= MAX_BODY_CHARS, f"{command_id}: {len(body)} chars; detail belongs in the agent"
    assert "- [ ]" in found["Completion criteria"]


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_accepts_user_input_and_file_context(command_id: str) -> None:
    arguments = sections(body_of(command_id))["Arguments"]
    assert "$ARGUMENTS" in arguments
    assert "@file" in arguments                                   # file/context arguments are passed on
    for flag in re.findall(r"--[a-z-]+", str(meta_of(command_id)["argument-hint"])):
        assert f"`{flag}" in arguments, f"{command_id}: {flag} in argument-hint but not documented"


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_brief_mirrors_the_agent_task_fields(command_id: str) -> None:
    steps = sections(body_of(command_id))["Steps"]
    for field in ("**Objective:**", "**Constraints:**", "**Inputs:**", "**Expected outputs:**"):
        assert field in steps, f"{command_id}: brief lacks {field}"


# ---------------------------------------------------------------------- agent references


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_agent_section_names_exactly_one_registered_agent(registry: HarnessRegistry, command_id: str) -> None:
    agent_section = sections(body_of(command_id))["Agent"]
    delegated = re.findall(r"Delegate to the `([a-z0-9-]+)` subagent", agent_section)
    assert delegated == [EXPECTED[command_id]]
    agent = registry.agents.get(EXPECTED[command_id])
    assert agent.plugin == "django-engineering" and agent.enabled and agent.path.is_file()


def test_every_django_agent_has_exactly_one_command(registry: HarnessRegistry) -> None:
    local_agents = {a.id for a in registry.agents if a.plugin == "django-engineering"}
    assert sorted(EXPECTED.values()) == sorted(local_agents)


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_mentioned_agents_and_commands_exist(registry: HarnessRegistry, command_id: str) -> None:
    body = body_of(command_id)
    agents = set(re.findall(r"`(django-[a-z-]+-agent|django-project-analyzer)`", body))
    assert agents <= set(registry.agents.ids()), agents - set(registry.agents.ids())
    commands = set(re.findall(r"`/(django-[a-z-]+)`", body))
    assert commands <= set(EXPECTED), commands - set(EXPECTED)


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_no_fallback_to_the_main_session(command_id: str) -> None:
    agent_section = " ".join(sections(body_of(command_id))["Agent"].split())
    assert "The main session does not" in agent_section or "Do not do the analysis in the main session" in agent_section


# ---------------------------------------------------------------------- skill and rule references


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_skills_and_rules_match_the_agent_registration(registry: HarnessRegistry, command_id: str) -> None:
    section = sections(body_of(command_id))["Skills and rules"]
    skills, rules = split_skills_and_rules(registry, EXPECTED[command_id])
    assert listed(section, "Skills") == skills
    assert listed(section, "Rules") == rules
    assert "django-security-standards" in rules


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_skill_and_rule_references_resolve(registry: HarnessRegistry, command_id: str) -> None:
    section = sections(body_of(command_id))["Skills and rules"]
    rule_ids = {s.id for s in registry.skills.by_tag("rule")}
    for skill_id in listed(section, "Skills"):
        assert skill_id not in rule_ids and (HARNESS_HOME / "skills" / skill_id / "SKILL.md").is_file()
    for rule_id in listed(section, "Rules"):
        assert rule_id in rule_ids and (HARNESS_HOME / "rules" / rule_id / "SKILL.md").is_file()


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_cited_requirement_ids_exist(command_id: str) -> None:
    defined = {req for path in (HARNESS_HOME / "rules").glob("*/SKILL.md")
               for req in re.findall(r"\*\*([A-Z]+-[MAR]\d+)\*\*", path.read_text(encoding="utf-8"))}
    cited = set(re.findall(r"\b(?:SEC|DOC|STR|COD|MOD|API|DB|TST|PRF|CFG|LOG|DEP)-[MAR]\d+\b", body_of(command_id)))
    assert cited <= defined, cited - defined


# ---------------------------------------------------------------------- no permission escalation


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_frontmatter_grants_nothing(command_id: str) -> None:
    meta = meta_of(command_id)
    assert not set(meta) & FORBIDDEN_KEYS, f"{command_id}: grants or overrides {set(meta) & FORBIDDEN_KEYS}"


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_body_grants_no_extra_tools(registry: HarnessRegistry, command_id: str) -> None:
    body = body_of(command_id)
    assert not re.search(r"\b(WebFetch|WebSearch|MultiEdit|NotebookEdit)\b", body)
    assert not re.search(r"(?i)\b(allowed-tools|bypassPermissions|dangerously|--no-verify)\b", body)
    # A command never names a tool its agent does not declare.
    tools = set(registry.agents.get(EXPECTED[command_id]).tools)
    named = set(re.findall(r"`(Read|Write|Edit|Grep|Glob|Bash)`", body))
    assert named <= tools, f"{command_id}: names tools outside its agent: {named - tools}"


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_destructive_operations_are_never_automatic(command_id: str) -> None:
    authorization = " ".join(sections(body_of(command_id))["Authorization"].split())
    if command_id in READ_ONLY:
        assert re.search(r"Read-only", authorization)
    else:
        assert re.search(r"(?i)\bnever\b", authorization), f"{command_id}: no 'never' boundary"
    assert not re.search(r"(?i)\brun(s)? `?(python )?manage\.py migrate", body_of(command_id))


@pytest.mark.parametrize("command_id", sorted(GATED_BY_APPLY))
def test_code_changes_require_explicit_authorization(command_id: str) -> None:
    authorization = " ".join(sections(body_of(command_id))["Authorization"].split())
    assert "`--apply`" in authorization
    assert re.search(r"must not (create or )?change files", authorization), command_id
    assert re.search(r"ask the user to approve", authorization), command_id
    criteria = sections(body_of(command_id))["Completion criteria"]
    assert re.search(r"only after (the user's )?approval or `--apply`", " ".join(criteria.split())), command_id


def test_security_fixes_only_on_explicit_request() -> None:
    meta, body = meta_of("django-security"), body_of("django-security")
    authorization = " ".join(sections(body)["Authorization"].split())
    assert "--fix" in str(meta["argument-hint"])
    assert "Without `--fix`:** review mode" in authorization
    assert "must not change the workspace" in authorization
    assert "ask the user to approve that list" in authorization
    assert "`--apply`" not in body                                # no other route to changes


def test_design_scaffolds_only_on_request_and_approval() -> None:
    authorization = " ".join(sections(body_of("django-design"))["Authorization"].split())
    assert "must not change the workspace" in authorization
    assert "`--scaffold`" in authorization and "ask the user to approve" in authorization


# ---------------------------------------------------------------------- read-only restrictions


@pytest.mark.parametrize("command_id", sorted(READ_ONLY))
def test_read_only_commands_stay_read_only(registry: HarnessRegistry, command_id: str) -> None:
    meta, body = meta_of(command_id), body_of(command_id)
    assert not set(registry.agents.get(EXPECTED[command_id]).tools) & WRITE_TOOLS
    for flag in ("--apply", "--fix", "--scaffold", "--out"):
        assert flag not in body and flag not in str(meta["argument-hint"]), f"{command_id} offers {flag}"
    authorization = " ".join(sections(body)["Authorization"].split())
    assert authorization.startswith("Read-only")
    assert "workspace is unchanged" in sections(body)["Completion criteria"]


def test_review_allows_only_read_only_git() -> None:
    body = body_of("django-review")
    git_commands = re.findall(r"`git ([a-z-]+)", body)
    assert git_commands and set(git_commands) <= {"status", "diff", "log", "show"}
    assert "has no fix mode" in body


def test_document_is_restricted_to_documentation(registry: HarnessRegistry) -> None:
    body = body_of("django-document")
    authorization = " ".join(sections(body)["Authorization"].split())
    assert "Bash" not in registry.agents.get("django-documentation-agent").tools
    assert authorization.startswith("- **Documentation changes only.**")
    assert "never changes application code" in authorization
    assert "boundary violation" in authorization and "Do not revert anything automatically" in authorization
    assert "--apply" not in body and "--fix" not in body
    assert "Only documentation files changed." in sections(body)["Completion criteria"]


# ---------------------------------------------------------------------- linked, not copied


def _long_lines(paths) -> set[str]:
    return {line.strip() for path in paths for line in path.read_text(encoding="utf-8").splitlines()
            if len(line.strip()) >= 60}


def test_commands_do_not_duplicate_agent_skill_or_rule_text() -> None:
    sources = [*AGENTS_DIR.glob("*.md"), *(HARNESS_HOME / "skills").rglob("*.md"),
               *(HARNESS_HOME / "rules").rglob("*.md")]
    existing = _long_lines(sources)
    copied = {c: [line.strip() for line in body_of(c).splitlines() if line.strip() in existing] for c in COMMAND_IDS}
    assert not any(copied.values()), {k: v for k, v in copied.items() if v}


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_no_secrets_in_commands(registry: HarnessRegistry, command_id: str) -> None:
    text = (COMMANDS_DIR / f"{command_id}.md").read_text(encoding="utf-8")
    for pattern in registry.config.security.secret_patterns:
        assert not re.search(pattern.regex, text), f"{command_id} matches secret pattern {pattern.id}"


# ---------------------------------------------------------------------- independence and compatibility


@pytest.mark.parametrize("command_id", COMMAND_IDS)
def test_independent_of_the_python_harness(command_id: str) -> None:
    body = body_of(command_id)
    assert not re.search(r"core\.cli|agent_runtime|\bruntime\.|execution (start|resume)|\bagent (run|inspect)\b"
                         r"|\.workflow\.yaml|AgentRunner", body)


def test_commands_are_not_wired_into_the_harness(registry: HarnessRegistry) -> None:
    harness_text = "\n".join(p.read_text(encoding="utf-8") for folder in ("config", "core", "runtime", "agent_runtime",
                                                                          "workflows")
                             for p in (HARNESS_HOME / folder).rglob("*") if p.suffix in {".py", ".yaml", ".json"})
    # security-config's Bash rule keys are the only existing "commands/..." strings.
    assert "commands/" not in harness_text.replace("commands/deny", "").replace("commands/require_approval", "")
    assert not set(EXPECTED) & (set(registry.agents.ids()) | set(registry.workflows.ids()))


# Phase 4 resolved the only overlap: Claude Code exposes plugin skills as slash commands, and a
# command shadows a same-named skill (verified with CLI 2.1.319), so the `django-security` skill
# was renamed `django-security-engineering`. Commands and skills now share no name.
def test_names_do_not_collide(registry: HarnessRegistry) -> None:
    ids = set(EXPECTED)
    assert all(c.startswith("django-") for c in ids)
    assert not ids & set(registry.agents.ids())
    assert not ids & set(registry.skills.ids())
    assert not ids & set(registry.workflows.ids())
    assert not ids & BUILTIN_COMMANDS


def test_compatible_with_existing_repository_commands() -> None:
    """Command names stay unique across the repository's plugins and projects."""
    existing = {p.stem for pattern in ("plugins/*/commands/*.md", "projects/*/commands/*.md", ".claude/commands/*.md")
                for p in REPO_ROOT.glob(pattern) if p.parent.resolve() != COMMANDS_DIR.resolve()}
    assert not set(EXPECTED) & existing


def test_existing_agents_skills_and_rules_unchanged(registry: HarnessRegistry) -> None:
    assert len(registry.agents) == 17 and len(registry.skills) == 33
    assert len(registry.skills.by_tag("rule")) == 12
    assert {p.stem for p in AGENTS_DIR.glob("*.md") if not p.stem.isupper()} == set(EXPECTED.values()) | {
        "django-orchestrator"}
