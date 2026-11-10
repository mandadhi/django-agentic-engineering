"""Phase 5: the Django Engineering Pack runs through the harness as the registered
``django-engineering`` plugin, from the same canonical files the standalone plugin loads.

Covers plugin registration, agent discovery, skill and rule resolution, templates and shared
references, harness-side security (write modes, workspace boundaries, no escalation, plugin
hooks kept out of harness sessions) and execution through the existing runner and workflow
engine with the fake SDK adapter. Real SDK runs are in ``test_django_sdk_live.py`` (opt-in).

Tests use ``pack_registry`` / ``pack_runner`` (conftest): the shipped registries with entries
that are absent from this checkout pruned, so they run in any layout.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from agent_runtime.exceptions import PluginResolutionError
from agent_runtime.options_factory import AgentInstructions, build_agent_definition, load_instructions
from agent_runtime.options_factory import permission_settings, with_preloaded_skills
from agent_runtime.pack_resources import PLUGIN_ROOT_VAR, declared_resources, resolve_resources
from agent_runtime.plugin_loader import ResolvedPlugins, resolve_plugins, verify_loaded
from agent_runtime.security_hooks import ApprovalDecision, ApprovalRequest
from core import HARNESS_HOME
from core.config import PluginSpec, load_config
from core.errors import ReferenceValidationError
from core.frontmatter import read_markdown
from core.plugin_policy import PluginPolicyError, load_agent_policy
from core.registry import HarnessRegistry
from core.security import SecurityPolicy
from core.validation import _check_agent_policy
from plugin_packaging import build
from runtime.state import StageStatus

from conftest import write_yaml
from fakes import FakeSDKAdapter, agent_result, succeed
from test_django_agents import EXPECTED, EXPECTED_RULES, EXPECTED_TOOLS
from test_django_rules import RULES
from test_django_templates import AGENT_REFERENCES, AGENT_TEMPLATES

PLUGIN = "django-engineering"
AGENT_IDS = sorted(EXPECTED)
SKILL_IDS = sorted(p.parent.name for p in (HARNESS_HOME / "skills").glob("*/SKILL.md"))
RULE_IDS = sorted(RULES)
READ_ONLY = ("django-project-analyzer", "django-code-review-agent")
MODES = {"django-project-analyzer": "read-only", "django-code-review-agent": "read-only",
         "django-documentation-agent": "documentation-only", "django-security-agent": "confirm-writes",
         "django-architecture-agent": "workspace-write", "django-model-agent": "workspace-write",
         "django-api-agent": "workspace-write", "django-testing-agent": "workspace-write",
         "django-performance-agent": "workspace-write"}


def pack_plugin(registry: HarnessRegistry) -> PluginSpec:
    return next(p for p in registry.config.plugins if p.id == PLUGIN)


def first_pack_skill(data: dict) -> dict:
    """The first skill-registry entry of the pack plugin (layout-independent: in the full
    repository layout the Phase 2 repo-root skills come first)."""
    return next(s for s in data["skills"] if s.get("plugin") == PLUGIN)


def expect_issue(root: Path, code: str, text: str) -> None:
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(root)
    matches = [i for i in info.value.issues if i.code == code and text in i.message]
    assert matches, [(i.code, i.message) for i in info.value.issues]


# ---------------------------------------------------------------------- tool-call scenarios


def calls_then_succeed(*calls: tuple[str, dict]):
    """Run each tool call through the registered hooks, then return a valid AgentResult."""
    async def scenario(s):
        yield s.init()
        for name, tool_input in calls:
            await s.tool(name, tool_input, {"ok": True})
        yield s.result(structured=agent_result(s))
    return scenario


def decisions(session) -> list[tuple[str, str]]:
    out = []
    for _, decision in session.decisions:
        spec = decision.get("hookSpecificOutput", {})
        out.append((spec.get("permissionDecision") or "allow", spec.get("permissionDecisionReason", "")))
    return out


def run_calls(pack_runner, workspace: Path, agent_id: str, *calls: tuple[str, dict], approver=None):
    adapter = FakeSDKAdapter(calls_then_succeed(*calls))
    runner = pack_runner(adapter, **({"approver": approver} if approver else {}))
    execution = runner.start_agent(agent_id, "Do the task.", workspace=workspace)
    outcome = asyncio.run(runner.run_workflow(execution.execution_id))
    return outcome, adapter.calls[0], runner, execution


class RecordingApprover:
    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return request.decide(self.approve, "test", "approved in test" if self.approve else "denied in test")


# ====================================================================== plugin registration


def test_pack_plugin_registered_at_the_canonical_root(pack_registry: HarnessRegistry) -> None:
    plugin = pack_plugin(pack_registry)
    assert plugin.path == HARNESS_HOME.resolve() and plugin.base == "harness" and plugin.rel_path == "."
    manifest = json.loads(plugin.manifest.read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN and manifest["skills"] == ["./skills/", "./rules/"]
    assert plugin.read_roots == tuple((HARNESS_HOME / d).resolve() for d in ("templates", "references", "skills",
                                                                             "rules"))
    assert plugin.agent_policy_path == (HARNESS_HOME / "plugin_packaging" / "standalone-security.yaml").resolve()
    assert plugin.load_hooks is False


def test_plugin_root_does_not_depend_on_cwd_or_config_location(pack_root: Path, tmp_path: Path) -> None:
    previous = os.getcwd()
    try:
        os.chdir(tmp_path)                                          # unrelated working directory
        registry = HarnessRegistry.load(pack_root)                  # config copied outside the project
    finally:
        os.chdir(previous)
    assert pack_root.resolve() not in HARNESS_HOME.parents and pack_plugin(registry).path == HARNESS_HOME.resolve()
    assert registry.agents.get("django-model-agent").path == (HARNESS_HOME / "agents" / "django-model-agent.md").resolve()


def test_missing_plugin_directory_is_reported(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"][-1].update(path="no-such-pack"))
    expect_issue(pack_root, "missing-path", "directory not found: no-such-pack")


def test_plugin_must_stay_inside_its_base(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"][-1].update(path=".."))
    expect_issue(pack_root, "outside-repo", "outside the harness directory")


def test_invalid_and_mismatched_manifests(pack_root: Path, edit_yaml, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "broken" / ".claude-plugin").mkdir(parents=True)
    (repo / "broken" / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
    (repo / "other" / ".claude-plugin").mkdir(parents=True)
    (repo / "other" / ".claude-plugin" / "plugin.json").write_text('{"name": "something-else"}', encoding="utf-8")
    edit_yaml("config/framework-config.yaml", lambda d: d["paths"].update(repo_root=str(repo)))
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"].extend(
        [{"id": "broken", "path": "broken"}, {"id": "other", "path": "other"}]))
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(pack_root)
    found = {(i.code, i.message.split(":")[0]) for i in info.value.issues}
    assert ("invalid-plugin", "plugin 'broken'") in found and ("name-mismatch", "plugin 'other'") in found
    assert all("django-engineering" not in i.message for i in info.value.issues)   # the pack is unaffected


def test_invalid_plugin_configuration_is_rejected_by_schema(pack_root: Path, edit_yaml) -> None:
    from core.errors import ConfigError
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"][-1].update(base="cwd"))
    with pytest.raises(ConfigError, match="Invalid configuration"):
        HarnessRegistry.load(pack_root)


@pytest.mark.parametrize("read_path, code", [("..", "outside-plugin"), (".", "outside-plugin"),
                                             ("no-such-folder", "missing-path")])
def test_read_paths_must_be_existing_plugin_sub_folders(pack_root: Path, edit_yaml, read_path: str,
                                                        code: str) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"][-1].update(read_paths=[read_path]))
    expect_issue(pack_root, code, f"read path '{read_path}'")


def test_plugin_hooks_can_only_be_dropped_when_the_harness_enforces_a_policy(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["plugins"][-1].pop("agent_policy"))
    expect_issue(pack_root, "schema", "load_hooks false requires an agent_policy")


def test_agent_policy_must_cover_exactly_the_plugin_agents(pack_registry: HarnessRegistry, tmp_path: Path) -> None:
    source = yaml.safe_load((HARNESS_HOME / "plugin_packaging" / "standalone-security.yaml").read_text(encoding="utf-8"))
    del source["agents"]["django-security-agent"]
    source["agents"]["ghost-agent"] = "read-only"
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
    plugin = replace(pack_plugin(pack_registry), path=tmp_path, agent_policy="policy.yaml")
    config = replace(pack_registry.config, plugins=(plugin,))
    issues = {(i.code, i.message) for i in _check_agent_policy(config, plugin, "agent-registry", "plugins/0")}
    assert any(c == "missing-policy" and "django-security-agent" in m for c, m in issues)
    assert any(c == "unknown-agent" and "ghost-agent" in m for c, m in issues)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(kind="Other"), "expected kind 'PluginSecurityPolicy'"),
    (lambda d: d.update(plugin="someone-else"), "policy is for plugin 'someone-else'"),
    (lambda d: d["agents"].update({"django-model-agent": "god-mode"}), "unknown write modes"),
    (lambda d: d["read_only_commands"].append({"id": "bad", "regex": "("}), "invalid regex"),
])
def test_agent_policy_file_is_validated(pack_registry: HarnessRegistry, tmp_path: Path, mutate, message: str) -> None:
    doc = yaml.safe_load((HARNESS_HOME / "plugin_packaging" / "standalone-security.yaml").read_text(encoding="utf-8"))
    mutate(doc)
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(PluginPolicyError, match=message):
        load_agent_policy(replace(pack_plugin(pack_registry), path=tmp_path, agent_policy="policy.yaml"))


@pytest.mark.skipif(os.environ.get("DAE_TEST_PRUNE_UNAVAILABLE") == "1", reason="needs the unpruned shipped registry")
def test_existing_plugin_registrations_keep_their_behaviour(harness_root: Path) -> None:
    """The shipped plugins/agents/skills load exactly as before (config level: runs in any layout)."""
    config = load_config(harness_root)
    repo_root = config.framework.repo_root
    existing = [p for p in config.plugins if p.id != PLUGIN]
    assert [p.id for p in existing] == ["security", "database", "api", "pr-automation"]
    for plugin in existing:
        assert plugin.base == "repo_root" and plugin.anchor == repo_root
        assert plugin.path == (repo_root / plugin.rel_path).resolve()
        assert plugin.read_roots == (plugin.path,) and plugin.load_hooks and plugin.agent_policy is None
        assert plugin.preload_skills                         # Phase 5.1 (AGT-01): SKILL.md injected by the harness
    for agent in config.agents:
        if agent.plugin != PLUGIN:
            assert agent.source == "plugin" and agent.path == (repo_root / agent.rel_path).resolve()
            assert agent.plugin == agent.rel_path.split("/")[1]            # found by containing directory
    existing_plugins = ResolvedPlugins(tuple(existing), ("security:owasp-top10-review",))
    assert not existing_plugins.hooks_disabled                              # their hooks still load
    assert existing_plugins.definition_skills == ("security:owasp-top10-review",)   # still via AgentDefinition
    assert "disableAllHooks" not in permission_settings(config.security)


# ====================================================================== agent discovery


def test_all_nine_agents_discovered_from_the_plugin(pack_registry: HarnessRegistry) -> None:
    pack = [a for a in pack_registry.agents if a.plugin == PLUGIN]
    assert sorted(a.id for a in pack) == AGENT_IDS and len(pack) == len({a.id for a in pack}) == 9
    manifest = json.loads(pack_plugin(pack_registry).manifest.read_text(encoding="utf-8"))
    # Phase 6: the manifest also lists the plugin-only orchestrator, which the harness does not register
    # (core/orchestration.py plays that role there; see tests/test_orchestration.py).
    orchestrator = (HARNESS_HOME / "agents" / "django-orchestrator.md").resolve()
    listed = sorted((HARNESS_HOME / p).resolve() for p in manifest["agents"])
    assert orchestrator in listed and "django-orchestrator" not in pack_registry.agents
    assert [p for p in listed if p != orchestrator] == sorted(a.path for a in pack)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_frontmatter_and_metadata_preserved(pack_registry: HarnessRegistry, agent_id: str) -> None:
    agent = pack_registry.agents.get(agent_id)
    meta, body = read_markdown(HARNESS_HOME / "agents" / f"{agent_id}.md")
    assert agent.description == str(meta["description"]).strip() and agent.model == meta["model"]
    assert agent.tools == EXPECTED_TOOLS[agent_id] and agent.role == EXPECTED[agent_id][0]
    assert agent.sdlc_phases == (meta["sdlc_phase"],)
    assert load_instructions(agent).prompt == body                        # instructions untouched


def test_no_second_copy_of_any_agent(pack_registry: HarnessRegistry) -> None:
    folders = [d for d in HARNESS_HOME.iterdir() if d.is_dir() and d.name not in (".venv", "var", ".git", ".pytest_cache")]
    for agent_id in AGENT_IDS:
        copies = [p for folder in folders for p in folder.rglob(f"{agent_id}.md")]
        assert copies == [HARNESS_HOME / "agents" / f"{agent_id}.md"], copies


# ====================================================================== skills and rules


def test_all_skills_and_rules_resolve_to_canonical_files(pack_registry: HarnessRegistry) -> None:
    pack = {s.id: s for s in pack_registry.skills if s.plugin == PLUGIN}
    assert sorted(pack) == sorted(SKILL_IDS + RULE_IDS) and len(SKILL_IDS) == len(RULE_IDS) == 12
    for skill_id, skill in pack.items():
        folder = "rules" if skill_id in RULES else "skills"
        assert skill.path == (HARNESS_HOME / folder / skill_id).resolve() and skill.sdk_name == f"{PLUGIN}:{skill_id}"
        meta = read_markdown(skill.path / "SKILL.md")[0]
        assert skill.description == str(meta["description"]).strip()       # frontmatter read in place


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_receives_exactly_its_registered_skills_and_rules(pack_registry: HarnessRegistry,
                                                                agent_id: str) -> None:
    agent = pack_registry.agents.get(agent_id)
    resolved = resolve_plugins(pack_registry, agent)
    expected = tuple(f"{PLUGIN}:{s}" for s in EXPECTED[agent_id][1] + EXPECTED_RULES[agent_id])
    assert resolved.skill_names == expected                               # order: skills, then rules
    assert len(set(resolved.skill_names)) == len(expected)                # each loads once
    unrelated = {f"{PLUGIN}:{s}" for s in SKILL_IDS + RULE_IDS} - set(expected)
    assert unrelated and not unrelated & set(resolved.skill_names)        # nothing else is resolved
    # the harness preloads exactly these files, verbatim and once; AgentDefinition.skills does not repeat them
    assert [name for name, _ in resolved.preloaded] == list(expected)
    instructions = with_preloaded_skills(load_instructions(agent), resolved)
    for name, skill_md in resolved.preloaded:
        text = skill_md.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
        assert instructions.prompt.count(text) == 1 and instructions.prompt.count(f'<skill name="{name}"') == 1
    for name in unrelated:
        assert f'<skill name="{name}"' not in instructions.prompt
    definition = build_agent_definition(agent, instructions, SecurityPolicy(
        pack_registry.config.security, pack_registry.agents), resolved)
    assert definition.skills is None and definition.prompt == instructions.prompt


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_standalone_frontmatter_skills_equal_the_harness_mapping(pack_registry: HarnessRegistry,
                                                                 agent_id: str) -> None:
    """The generated frontmatter `skills:` line (standalone) and the registry (harness) agree exactly."""
    meta = read_markdown(HARNESS_HOME / "agents" / f"{agent_id}.md")[0]
    assert tuple(meta["skills"]) == resolve_plugins(pack_registry, pack_registry.agents.get(agent_id)).skill_names


@pytest.mark.parametrize("skill_id, folder", [("django-orm", "skills"), ("django-security-standards", "rules")])
def test_missing_skill_or_rule_fails_validation(pack_root: Path, edit_yaml, skill_id: str, folder: str) -> None:
    def move(doc: dict) -> None:
        next(s for s in doc["skills"] if s["id"] == skill_id)["path"] = f"{folder}/{skill_id}-missing"
    edit_yaml("config/skill-registry.yaml", move)
    expect_issue(pack_root, "missing-path", f"skill '{skill_id}': no SKILL.md in {folder}/{skill_id}-missing")


@pytest.mark.parametrize("skill_id", ["django-orm", "django-security-standards"])
def test_missing_skill_or_rule_fails_the_run_before_the_sdk(pack_registry: HarnessRegistry, skill_id: str,
                                                            tmp_path: Path) -> None:
    gone = replace(pack_registry.skills.get(skill_id), path=tmp_path / "gone")
    skills = type(pack_registry.skills)({s.id: (gone if s.id == skill_id else s) for s in pack_registry.skills})
    registry = replace(pack_registry, skills=skills)
    with pytest.raises(PluginResolutionError, match=f"skill '{skill_id}' has no SKILL.md"):
        resolve_plugins(registry, registry.agents.get("django-model-agent"))


def test_unknown_skill_in_mapping_is_reported(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0]["skills"].append("django-orn"))
    expect_issue(pack_root, "unknown-skill", "unknown skill 'django-orn' (did you mean: django-orm")


def test_skill_naming_an_unregistered_plugin_is_reported(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: d["skills"][0].update(plugin="django-engineerin"))
    expect_issue(pack_root, "unregistered-plugin", "unknown plugin 'django-engineerin'")


def test_plugin_entry_must_use_source_plugin_and_stay_inside(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: first_pack_skill(d).update(source="local"))
    expect_issue(pack_root, "schema", "but source is 'local'")


def test_plugin_entry_cannot_escape_the_plugin(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: first_pack_skill(d).update(path="../outside"))
    expect_issue(pack_root, "outside-plugin", "leaves plugin 'django-engineering'")


def test_duplicate_content_is_rejected(pack_root: Path, edit_yaml, pack_registry: HarnessRegistry) -> None:
    agent = pack_registry.agents.get("django-model-agent")
    with pytest.raises(PluginResolutionError, match="registered twice"):
        resolve_plugins(pack_registry, replace(agent, skills=agent.skills + (agent.skills[0],)))
    edit_yaml("config/skill-registry.yaml", lambda d: d["skills"].append(
        {"id": "django-orm-alias", "source": "plugin", "plugin": PLUGIN, "path": "skills/django-orm"}))
    expect_issue(pack_root, "duplicate-path", "already registered as 'django-orm'")


# ====================================================================== templates and references


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_template_and_reference_mappings_resolve(pack_registry: HarnessRegistry, agent_id: str) -> None:
    agent = pack_registry.agents.get(agent_id)
    resources = resolve_resources(pack_registry, agent, load_instructions(agent))
    assert tuple(r.id for r in resources.templates) == AGENT_TEMPLATES[agent_id]
    assert tuple(r.id for r in resources.references) == AGENT_REFERENCES[agent_id]
    for r in resources.templates:
        assert r.path == (HARNESS_HOME / "templates" / f"{r.id}.template.md").resolve()
    for r in resources.references:
        assert r.path == (HARNESS_HOME / "references" / f"{r.id}.md").resolve()


def test_resource_paths_do_not_depend_on_cwd(pack_registry: HarnessRegistry, tmp_path: Path) -> None:
    agent = pack_registry.agents.get("django-api-agent")
    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        resources = resolve_resources(pack_registry, agent, load_instructions(agent))
    finally:
        os.chdir(previous)
    assert all(r.path.is_absolute() and r.path.is_file() for r in resources.all)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_plugin_root_substituted_like_claude_code_does(pack_registry: HarnessRegistry, agent_id: str) -> None:
    agent = pack_registry.agents.get(agent_id)
    original = load_instructions(agent)
    expanded = resolve_resources(pack_registry, agent, original).expand(original)
    root = HARNESS_HOME.resolve().as_posix()
    assert PLUGIN_ROOT_VAR in original.prompt and PLUGIN_ROOT_VAR not in expanded.prompt
    assert expanded.prompt == original.prompt.replace(PLUGIN_ROOT_VAR, root)      # nothing else changes
    assert f"{root}/templates/<id>.template.md" in expanded.prompt


def instructions_with(body_lines: list[str]) -> AgentInstructions:
    return AgentInstructions("x", "x", "\n".join(["# Agent", "", "## Templates and references", "", *body_lines]))


def test_missing_template_is_a_clear_error_never_a_fabrication(pack_registry: HarnessRegistry) -> None:
    agent = pack_registry.agents.get("django-project-analyzer")
    bad = instructions_with(["- Templates (`${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`): `no-such-report`"])
    with pytest.raises(PluginResolutionError, match=r"template 'no-such-report' not found at "
                                                    r"templates/no-such-report\.template\.md"):
        resolve_resources(pack_registry, agent, bad)


def test_missing_reference_is_a_clear_error(pack_registry: HarnessRegistry) -> None:
    agent = pack_registry.agents.get("django-project-analyzer")
    bad = instructions_with(["- References (`${CLAUDE_PLUGIN_ROOT}/references/<id>.md`): `no-such-checklist`"])
    with pytest.raises(PluginResolutionError, match="reference 'no-such-checklist' not found at references/"):
        resolve_resources(pack_registry, agent, bad)


def test_resource_outside_readable_folders_is_rejected(pack_registry: HarnessRegistry) -> None:
    agent = pack_registry.agents.get("django-project-analyzer")
    bad = instructions_with(["- Templates (`${CLAUDE_PLUGIN_ROOT}/config/<id>.yaml`): `security-config`"])
    with pytest.raises(PluginResolutionError, match="outside the readable folders"):
        resolve_resources(pack_registry, agent, bad)


def test_plugin_root_needs_a_registered_plugin(pack_registry: HarnessRegistry) -> None:
    agent = replace(pack_registry.agents.get("django-project-analyzer"), plugin=None, source="local")
    with pytest.raises(PluginResolutionError, match="not part of a registered plugin"):
        resolve_resources(pack_registry, agent, load_instructions(agent))


def test_declared_resources_parse_only_the_section() -> None:
    body = "\n".join(["## Output", "```", "- Templates (`${CLAUDE_PLUGIN_ROOT}/x/<id>`): `fenced`", "```",
                      "- Templates (`${CLAUDE_PLUGIN_ROOT}/x/<id>`): `elsewhere`",
                      "## Templates and references",
                      "- Templates (`${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`): `a` · `b`",
                      "- References (`${CLAUDE_PLUGIN_ROOT}/references/<id>.md`): `c`", "## Next"])
    assert declared_resources(body) == {"template": [("a", "${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md"),
                                                     ("b", "${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md")],
                                        "reference": [("c", "${CLAUDE_PLUGIN_ROOT}/references/<id>.md")]}


# ====================================================================== SDK execution (fake adapter)


def test_django_agent_executes_through_the_existing_runtime(pack_runner, workspace: Path, store) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = pack_runner(adapter)
    execution = runner.start_agent("django-project-analyzer", "Profile the project.", workspace=workspace)
    outcome = asyncio.run(runner.run_workflow(execution.execution_id))
    [stage] = outcome.stages
    assert stage.stage_status is StageStatus.SUCCEEDED, stage.failure

    [session] = adapter.calls
    options = session.options
    root = HARNESS_HOME.resolve()
    # the plugin ships its own `django-project-analyzer`; under that name the CLI would run the file and
    # drop the harness definition, so the harness's definition runs as dae-<id> (see session_agent_name)
    assert options.extra_args == {"agent": "dae-django-project-analyzer"}
    assert list(options.agents) == ["dae-django-project-analyzer"]
    assert options.plugins == [{"type": "local", "path": str(root)}]              # the canonical pack, once
    assert options.skills == [f"{PLUGIN}:{s}" for s in EXPECTED["django-project-analyzer"][1]
                              + EXPECTED_RULES["django-project-analyzer"]]
    definition = options.agents["dae-django-project-analyzer"]
    assert definition.skills is None and definition.model == "sonnet"            # preloaded by the harness
    assert [definition.prompt.count(f'<skill name="{name}"') for name in options.skills] == [1] * len(options.skills)
    assert '<skill name="django-engineering:django-testing"' not in definition.prompt  # not registered for it
    assert "STR-M1" in definition.prompt and "MOD-M1" not in definition.prompt
    assert PLUGIN_ROOT_VAR not in definition.prompt
    assert f"{root.as_posix()}/references/<id>.md" in definition.prompt
    assert options.tools == ["Read", "Grep", "Glob", "Bash"] and options.permission_mode == "dontAsk"
    settings = json.loads(options.settings)
    assert settings["disableAllHooks"] is True and settings["permissions"]["deny"]
    assert options.setting_sources == [] and options.mcp_servers == {} and options.strict_mcp_config

    # templates and references reach the task as references, never inline
    assert "`django-engineering.template.project-analysis`: load from `file:" in session.prompt
    [run] = store.list_agent_runs(execution.execution_id)
    refs = {r["id"]: r for r in run.task["context"]["refs"]}
    assert set(refs) == {"django-engineering.template.project-analysis",
                         "django-engineering.reference.django-project-discovery",
                         "django-engineering.reference.django-version-compatibility"}
    assert all(r["uri"].startswith("file:") for r in refs.values()) and run.task["context"]["inline"] == []
    # persisted with the existing store: session, plugin verified from the init frame, result
    assert run.session_id == session.session_id and run.sdk_metadata["plugins"] == [PLUGIN]
    assert run.result["status"] == "succeeded"
    assert any(c.reason == "agent_session_started" for c in store.list_checkpoints(execution.execution_id))


def test_skills_are_verified_from_the_init_frame(pack_registry: HarnessRegistry) -> None:
    agent = pack_registry.agents.get("django-testing-agent")
    resolved = resolve_plugins(pack_registry, agent)
    init = {"plugins": [{"name": PLUGIN, "path": str(HARNESS_HOME)}], "skills": list(resolved.skill_names)}
    verify_loaded(init, resolved)
    from agent_runtime.exceptions import PluginLoadError
    with pytest.raises(PluginLoadError, match="django-engineering:django-testing-standards"):
        verify_loaded({**init, "skills": [s for s in resolved.skill_names if not s.endswith("testing-standards")]},
                      resolved)


def test_plugin_load_error_in_session_fails_the_stage(pack_runner, workspace: Path) -> None:
    async def scenario(s):
        yield s.init(plugin_errors=[{"plugin": PLUGIN, "type": "manifest", "message": "bad"}])
        yield s.result(structured=agent_result(s))
    runner = pack_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("django-documentation-agent", "Document.", workspace=workspace)
    [stage] = asyncio.run(runner.run_workflow(execution.execution_id)).stages
    assert stage.stage_status is StageStatus.FAILED and stage.failure.code == "plugin.load_failed"


def test_sequential_workflow_of_django_agents(pack_root: Path, workspace: Path, store, tmp_path: Path) -> None:
    from agent_runtime.runner import AgentRunner
    from core.context import ContextManager
    from runtime.execution import ExecutionManager

    write_yaml(pack_root / "workflows" / "django-review.workflow.yaml", {
        "schema_version": "1.0", "kind": "WorkflowDefinition", "id": "django-review", "version": "0.1.0",
        "name": "Profile then review", "status": "draft",
        "inputs": {"type": "object", "required": ["repository"], "properties": {"repository": {"type": "string"}}},
        "stages": [
            {"id": "profile", "agent_id": "django-project-analyzer", "inputs": ["repository"],
             "objective": "Profile the project.", "expected_outputs": [{"name": "profile", "type": "report"}]},
            {"id": "review", "agent_id": "django-code-review-agent", "depends_on": ["profile"],
             "objective": "Review the project.", "expected_outputs": [{"name": "review", "type": "report"}]}]})
    registry = HarnessRegistry.load(pack_root)                       # discovers the new workflow file
    adapter = FakeSDKAdapter(succeed())
    context = ContextManager(registry.config.framework.context, max_inline_bytes=registry.config.security.max_inline_bytes)
    runner = AgentRunner(ExecutionManager(store, registry), context, adapter, artifacts_root=tmp_path / "artifacts",
                         cancel_poll_seconds=0.02)
    execution = runner.start("django-review", {"repository": str(workspace)}, workspace=workspace)
    outcome = asyncio.run(runner.run_workflow(execution.execution_id))
    assert [(s.stage_id, s.stage_status) for s in outcome.stages] == [("profile", StageStatus.SUCCEEDED),
                                                                       ("review", StageStatus.SUCCEEDED)]
    assert [c.options.extra_args["agent"] for c in adapter.calls] == ["dae-django-project-analyzer",
                                                                     "dae-django-code-review-agent"]
    assert "`upstream.profile.profile`" in adapter.calls[1].prompt                   # sequential hand-over
    assert "django-engineering.template.code-review-report" in adapter.calls[1].prompt
    assert "django-engineering.template.project-analysis" not in adapter.calls[1].prompt   # only its own


# ====================================================================== security


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_tool_escalation(pack_registry: HarnessRegistry, agent_id: str) -> None:
    policy = SecurityPolicy(pack_registry.config.security, pack_registry.agents)
    effective = policy.effective_tools(agent_id)
    assert set(effective) <= set(EXPECTED_TOOLS[agent_id])
    assert {"WebFetch", "WebSearch"} <= policy.denied_tools(agent_id)
    assert load_agent_policy(pack_plugin(pack_registry)).for_agent(agent_id).mode == MODES[agent_id]


@pytest.mark.parametrize("agent_id", READ_ONLY)
def test_read_only_agents_stay_read_only(pack_runner, workspace: Path, agent_id: str) -> None:
    template = (HARNESS_HOME / "templates" / "project-analysis.template.md").as_posix()
    _, session, _, _ = run_calls(
        pack_runner, workspace, agent_id,
        ("Read", {"file_path": "app/settings.py"}),
        ("Read", {"file_path": template}),
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": "echo x > notes.txt"}),
        ("Bash", {"command": "git commit -am wip"}),
        ("Bash", {"command": "sed -i s/a/b/ app/settings.py"}),
        ("Bash", {"command": "python manage.py makemigrations"}),
        ("Write", {"file_path": "app/new.py", "content": "x"}),
        ("Edit", {"file_path": "app/settings.py", "old_string": "a", "new_string": "b"}))
    got = decisions(session)
    assert [d for d, _ in got[:3]] == ["allow", "allow", "allow"]
    assert all(d == "deny" for d, _ in got[3:]), got
    assert "read-only; shell redirection to 'notes.txt'" in got[3][1]
    assert "rule 'git-write'" in got[4][1] and "rule 'in-place-edit'" in got[5][1] and "rule 'django-write'" in got[6][1]
    assert "not permitted" in got[7][1] and "not permitted" in got[8][1]          # Write/Edit never declared


def test_documentation_agent_has_no_shell_and_writes_docs_only(pack_runner, workspace: Path) -> None:
    _, session, _, _ = run_calls(
        pack_runner, workspace, "django-documentation-agent",
        ("Bash", {"command": "ls"}),
        ("Write", {"file_path": "docs/architecture.md", "content": "# Architecture"}),
        ("Write", {"file_path": "README.md", "content": "# Shop"}),
        ("Write", {"file_path": "app/views.py", "content": "x = 1"}),
        ("Edit", {"file_path": "app/settings.py", "old_string": "DEBUG", "new_string": "X"}))
    got = decisions(session)
    assert got[0][0] == "deny" and "not permitted" in got[0][1]                    # Bash is not even declared
    assert [d for d, _ in got[1:3]] == ["allow", "allow"]
    assert got[3][0] == "deny" and "documentation only; 'app/views.py'" in got[3][1]
    assert got[4][0] == "deny"


def test_documentation_agent_may_write_its_own_output(pack_runner, workspace: Path) -> None:
    """The run's output directory is harness-owned, not the codebase: any mode may write its report there."""
    decided: list[dict] = []

    async def scenario(s):
        yield s.init()
        artifact_dir = s.prompt.split("Output directory (write produced files here): `", 1)[1].split("`", 1)[0]
        decided.append(await s.tool("Write", {"file_path": f"{artifact_dir}/report.py", "content": "# r"}))
        yield s.result(structured=agent_result(s))
    runner = pack_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("django-documentation-agent", "Document.", workspace=workspace)
    asyncio.run(runner.run_workflow(execution.execution_id))
    assert decided == [{}], decided                                     # no objection from the guard


def test_security_agent_changes_code_only_with_approval(pack_runner, workspace: Path) -> None:
    calls = (("Read", {"file_path": "app/settings.py"}),
             ("Edit", {"file_path": "app/settings.py", "old_string": "DEBUG = False", "new_string": "DEBUG = 0"}),
             ("Bash", {"command": "sed -i s/False/0/ app/settings.py"}),
             ("Bash", {"command": "grep -rn SECRET app"}))
    # default approver (non-interactive): every change is refused
    _, session, runner, execution = run_calls(pack_runner, workspace, "django-security-agent", *calls)
    got = decisions(session)
    assert got[0][0] == "allow" and got[3][0] == "allow"
    assert got[1][0] == "deny" and "requires human approval (confirm-writes)" in got[1][1]
    assert got[2][0] == "deny" and "file-change-command" not in got[2][1] and "in-place-edit" in got[2][1]
    events = [e.event for e in runner.manager.store.list_logs(execution.execution_id)]
    assert events.count("tool.approval_requested") == 2                         # audited
    # an explicit human approval lets the same change through
    approver = RecordingApprover(True)
    _, session, _, _ = run_calls(pack_runner, workspace, "django-security-agent", *calls, approver=approver)
    assert [d for d, _ in decisions(session)] == ["allow"] * 4
    assert [r.rules for r in approver.requests] == [("confirm-writes",), ("in-place-edit",)]


@pytest.mark.parametrize("agent_id", ["django-model-agent", "django-api-agent"])
def test_model_and_api_agents_follow_existing_approval_rules(pack_runner, workspace: Path, agent_id: str) -> None:
    _, session, _, _ = run_calls(
        pack_runner, workspace, agent_id,
        ("Write", {"file_path": "app/models.py", "content": "x"}),
        ("Bash", {"command": "python manage.py makemigrations shop"}),
        ("Bash", {"command": "python manage.py migrate"}),
        ("Bash", {"command": "pip install requests"}),
        ("Write", {"file_path": str(workspace.parent / "elsewhere.py"), "content": "x"}))
    got = decisions(session)
    assert [d for d, _ in got[:2]] == ["allow", "allow"]
    assert got[2][0] == "deny" and "database-destructive" in got[2][1]
    assert got[3][0] == "deny" and "network-access" in got[3][1]
    assert got[4][0] == "deny" and "outside the permitted workspace" in got[4][1]


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_workspace_and_sensitive_file_boundaries(pack_runner, workspace: Path, agent_id: str) -> None:
    _, session, _, _ = run_calls(
        pack_runner, workspace, agent_id,
        ("Read", {"file_path": ".env"}),
        ("Read", {"file_path": str(HARNESS_HOME / "config" / "security-config.yaml")}),   # plugin, not readable
        ("Read", {"file_path": str(HARNESS_HOME / "var" / "harness.db")}),
        ("Read", {"file_path": str(HARNESS_HOME / "skills" / "django-orm" / "SKILL.md")}),
        ("Read", {"file_path": str(HARNESS_HOME / "references" / "django-security-checklist.md")}))
    got = decisions(session)
    assert got[0] == ("deny", "path matches a denied path pattern")
    assert got[1][0] == got[2][0] == "deny" and "read outside the permitted workspace" in got[1][1]
    assert got[3][0] == got[4][0] == "allow"


def test_plugin_hooks_are_kept_out_and_the_tripwire_fails_closed(pack_runner, workspace: Path) -> None:
    async def scenario(s):
        yield s.init()
        yield AssistantMessage(content=[ToolUseBlock(id="toolu_unseen", name="Read",
                                                     input={"file_path": "app/settings.py"})], model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="toolu_unseen", content="DEBUG", is_error=False)])
        yield s.result(structured=agent_result(s))
    runner = pack_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("django-project-analyzer", "Profile.", workspace=workspace)
    [stage] = asyncio.run(runner.run_workflow(execution.execution_id)).stages
    assert stage.stage_status is StageStatus.FAILED and stage.failure.code == "security.violation"
    assert "without passing the harness PreToolUse guard" in stage.failure.message


def test_tripwire_ignores_calls_the_guard_saw_and_native_refusals(pack_runner, workspace: Path) -> None:
    async def scenario(s):
        yield s.init()
        await s.tool("Read", {"file_path": "app/settings.py"}, {"ok": True})
        seen = s.decisions and next(iter(s.options.hooks["PreToolUse"][0].hooks)).__self__.seen_tool_use_ids
        [tool_use_id] = list(seen)
        yield AssistantMessage(content=[ToolUseBlock(id=tool_use_id, name="Read", input={})], model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content="ok", is_error=False)])
        yield AssistantMessage(content=[ToolUseBlock(id="toolu_native", name="Read", input={})], model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="toolu_native", content="denied", is_error=True)])
        # The CLI reports every native refusal in the result (observed with CLI 2.1.317); since Phase 5.1
        # (SEC-07) an unseen error result is legitimate only when it is listed there.
        yield s.result(structured=agent_result(s), permission_denials=[
            {"tool_name": "Read", "tool_use_id": "toolu_native", "tool_input": {}}])
    runner = pack_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("django-project-analyzer", "Profile.", workspace=workspace)
    [stage] = asyncio.run(runner.run_workflow(execution.execution_id)).stages
    assert stage.stage_status is StageStatus.SUCCEEDED, stage.failure


def test_calls_from_the_plugins_own_same_named_agent_are_refused(pack_runner, workspace: Path) -> None:
    """Only the harness's definition (dae-<id>) may act; the plugin's file-defined agent of the same name may not."""
    async def scenario(s):
        yield s.init()
        await s.tool("Read", {"file_path": "app/settings.py"}, {"ok": True}, agent_type="dae-django-model-agent")
        await s.tool("Read", {"file_path": "app/settings.py"}, {"ok": True}, agent_type="django-model-agent")
        await s.tool("Read", {"file_path": "app/settings.py"}, {"ok": True},
                     agent_type="django-engineering:django-model-agent")
        yield s.result(structured=agent_result(s))
    adapter = FakeSDKAdapter(scenario)
    runner = pack_runner(adapter)
    execution = runner.start_agent("django-model-agent", "Model it.", workspace=workspace)
    asyncio.run(runner.run_workflow(execution.execution_id))
    got = decisions(adapter.calls[0])
    assert got[0][0] == "allow" and got[1][0] == got[2][0] == "deny"
    assert "this session runs 'dae-django-model-agent'" in got[1][1]


def test_session_name_is_dae_even_without_harness_preload(pack_registry: HarnessRegistry) -> None:
    """Phase 5.1 (AGT-02): one naming contract for every agent, whatever its plugin's preload setting."""
    plugin = replace(pack_plugin(pack_registry), preload_skills=False)
    registry = replace(pack_registry, config=replace(pack_registry.config, plugins=(plugin,)))
    resolved = resolve_plugins(registry, registry.agents.get("django-model-agent"))
    assert resolved.preloaded == () and resolved.session_agent_name("django-model-agent") == "dae-django-model-agent"
    assert resolved.definition_skills == resolved.skill_names                  # names only when not preloaded


def test_disable_hooks_only_when_every_loaded_plugin_opts_out(pack_registry: HarnessRegistry) -> None:
    pack = pack_plugin(pack_registry)
    other = PluginSpec(id="security", path=Path("x"), rel_path="x")
    assert ResolvedPlugins((pack,), ()).hooks_disabled
    assert not ResolvedPlugins((pack, other), ()).hooks_disabled                  # mixed: keep all hooks
    assert not ResolvedPlugins((), ()).hooks_disabled


def test_missing_write_mode_fails_closed(pack_runner, workspace: Path, monkeypatch) -> None:
    import agent_runtime.runner as runner_module

    real = runner_module.load_agent_policy

    def without_api_agent(plugin):
        policy = real(plugin)
        return replace(policy, modes={k: v for k, v in policy.modes.items() if k != "django-api-agent"})
    monkeypatch.setattr(runner_module, "load_agent_policy", without_api_agent)
    adapter = FakeSDKAdapter(succeed())
    runner = pack_runner(adapter)
    execution = runner.start_agent("django-api-agent", "Build the API.", workspace=workspace)
    [stage] = asyncio.run(runner.run_workflow(execution.execution_id)).stages
    assert stage.stage_status is StageStatus.FAILED and stage.failure.code == "plugin.unresolved"
    assert "no write mode for agent 'django-api-agent'" in stage.failure.message and adapter.calls == []


# ====================================================================== standalone plugin unchanged


def test_standalone_packaging_is_unchanged_by_the_integration() -> None:
    report = build.run(build.PACK_ROOT, write=False)        # no drift: frontmatter skills + guard policy
    assert report.ok, report.problems
    assert sorted(build.pack_agents()) == AGENT_IDS
    assert json.loads((HARNESS_HOME / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]


def test_cli_inspect_reports_the_resolution(pack_root: Path, capsys) -> None:
    from core.cli import main
    assert main(["--root", str(pack_root), "agent", "inspect", "django-security-agent", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["plugin"] == PLUGIN and doc["write_mode"] == "confirm-writes"
    assert doc["plugin_hooks"].startswith("disabled")
    assert doc["rules"] == [s for s in EXPECTED_RULES["django-security-agent"]]
    assert list(doc["templates"]) == ["security-assessment"]
    assert list(doc["references"]) == list(AGENT_REFERENCES["django-security-agent"])
    assert doc["read_roots"] == [(HARNESS_HOME / d).resolve().as_posix()
                                 for d in ("templates", "references", "skills", "rules")]
