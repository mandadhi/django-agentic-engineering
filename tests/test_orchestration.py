"""Phase 6: the Django Orchestrator and the canonical orchestration contract.

Standalone plugin: the orchestrator agent and /orchestrate command are discoverable, the routing
section and delegation allowlist are generated from config/orchestration-policy.yaml (drift-checked),
the guard lets the orchestrator delegate only to the nine specialists (never to itself), and nothing
in the plugin depends on the Python harness.

Harness: the same policy is loaded and validated through the registry, routes are planned
deterministically (empty / unclassified / ambiguous tasks are refused, not guessed) and run as one
workflow execution through the existing ExecutionManager and AgentRunner, so ToolGuard, approvals,
workspace boundaries, retries and RunRecords apply unchanged. The ``pack`` profile excludes the
optional parent-repository integrations explicitly.

Offline: the SDK is faked (tests/fakes.py); no model is called.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from conftest import read_yaml, write_yaml
from core import HARNESS_HOME
from core.config import load_config
from core.errors import ConfigError, ReferenceValidationError
from core.frontmatter import read_markdown
from core.orchestration import (EXTENSION_KEY, OrchestrationError, OrchestrationPolicy, classify, plan_route,
                                policy_issues, workflow_document)
from core.registry import HarnessRegistry
from core.security import SecurityPolicy
from plugin_packaging import build as packaging
from runtime.state import ExecutionStatus, StageStatus
from test_django_agents import EXPECTED as SPECIALIST_SKILLS

ROOT = HARNESS_HOME
PLUGIN = "django-engineering"
ORCH = "django-orchestrator"
POLICY_FILE = ROOT / "config" / "orchestration-policy.yaml"
SPECIALISTS = sorted(SPECIALIST_SKILLS)


def policy() -> OrchestrationPolicy:
    return OrchestrationPolicy.from_dict(yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")))


def _load_guard():
    spec = importlib.util.spec_from_file_location("django_pack_guard_orch", ROOT / "hooks" / "django_pack_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def decide(agent_id: str, tool: str, tool_input: dict, cwd: Path) -> dict | None:
    hook_input = {"tool_name": tool, "cwd": str(cwd), "tool_input": tool_input, "agent_type": f"{PLUGIN}:{agent_id}"}
    gpolicy = json.loads((ROOT / "hooks" / "security-policy.json").read_text(encoding="utf-8"))
    return guard.handle("pre", hook_input, gpolicy, ROOT)


def verdict(result: dict | None) -> str:
    return "none" if result is None else result["hookSpecificOutput"]["permissionDecision"]


# ====================================================================== standalone plugin


def test_orchestrator_manifest_and_discovery() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert f"./agents/{ORCH}.md" in manifest["agents"]
    assert "./commands/orchestrate.md" in manifest["commands"]
    # every specialist is still listed exactly once; no duplicate under another name
    assert sorted(manifest["agents"]) == sorted([f"./agents/{a}.md" for a in SPECIALISTS] + [f"./agents/{ORCH}.md"])
    meta, body = read_markdown(ROOT / "agents" / f"{ORCH}.md")
    assert meta["name"] == ORCH and meta["description"].strip()
    assert meta["tools"].split(", ")[0] == "Agent" and "Write" not in meta["tools"] and "Edit" not in meta["tools"]
    assert meta["skills"] == [f"{PLUGIN}:django-security-standards"]      # generated from the policy
    assert isinstance(meta["maxTurns"], int)


def test_orchestration_command_delegates_to_the_orchestrator() -> None:
    meta, body = read_markdown(ROOT / "commands" / "orchestrate.md")
    assert meta["name"] == "orchestrate" and "django-orchestrator" in meta["description"]
    assert "Delegate to the `django-orchestrator` subagent" in body
    assert "Never invoke `django-orchestrator` again" in body                # no recursion via the command
    assert "ROUTING PLAN" in body                                          # nested delegation is not assumed
    assert policy().command == "orchestrate"


def test_structure_counts_include_the_orchestrator() -> None:
    assert packaging.structure_problems(ROOT) == []
    assert packaging.EXPECTED_COUNTS["agents"] == len(SPECIALISTS) + 1


def test_generated_routing_section_matches_the_policy() -> None:
    text = (ROOT / "agents" / f"{ORCH}.md").read_text(encoding="utf-8")
    pol = policy()
    section = packaging.render_orchestration(pol)
    assert section in text
    for category in pol.categories:
        assert f"#### `{category.id}`" in section
    assert f"At most **{pol.max_delegations}** specialist delegations" in section
    assert f"Never to `{PLUGIN}:{ORCH}`" in section
    # the policy routes; it never duplicates an agent's own instructions
    for agent_id in SPECIALISTS:
        own = read_markdown(ROOT / "agents" / f"{agent_id}.md")[1]
        responsibilities = own.split("## Responsibilities", 1)[1].split("##", 1)[0].strip()
        assert responsibilities[:80] not in text


def test_guard_policy_has_the_delegation_allowlist() -> None:
    gpolicy = json.loads((ROOT / "hooks" / "security-policy.json").read_text(encoding="utf-8"))
    entry = gpolicy["agents"][ORCH]
    assert entry["mode"] == "read-only"
    assert entry["delegates"] == [f"{PLUGIN}:{a}" for a in policy().specialists]
    assert sorted(a.split(":", 1)[1] for a in entry["delegates"]) == SPECIALISTS
    assert f"{PLUGIN}:{ORCH}" not in entry["delegates"]
    assert all("delegates" not in gpolicy["agents"][a] for a in SPECIALISTS)


@pytest.mark.parametrize("target", [f"{PLUGIN}:django-api-agent", "django-testing-agent",
                                    f"{PLUGIN}:django-code-review-agent"])
def test_orchestrator_may_delegate_to_a_specialist(tmp_path: Path, target: str) -> None:
    assert verdict(decide(ORCH, "Agent", {"subagent_type": target, "prompt": "x", "description": "x"},
                          tmp_path)) == "none"


@pytest.mark.parametrize("target, reason", [
    (f"{PLUGIN}:{ORCH}", "never delegates to itself"),
    (ORCH, "never delegates to itself"),
    ("general-purpose", "delegates only to the pack's specialists"),
    ("security:security-auditor", "delegates only to the pack's specialists"),
    (f"{PLUGIN}:django-unknown-agent", "delegates only to the pack's specialists"),
    ("", "must name a specialist"),
])
def test_orchestrator_delegation_outside_the_allowlist_is_denied(tmp_path: Path, target: str, reason: str) -> None:
    result = decide(ORCH, "Agent", {"subagent_type": target, "prompt": "x"}, tmp_path)
    assert verdict(result) == "deny" and reason in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_orchestrator_itself_stays_read_only(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert verdict(decide(ORCH, "Read", {"file_path": str(tmp_path / "app" / "x.py")}, tmp_path)) == "none"
    assert verdict(decide(ORCH, "Bash", {"command": "git status --porcelain"}, tmp_path)) == "none"
    for tool, tool_input in (("Write", {"file_path": str(tmp_path / "a.py"), "content": "x"}),
                             ("Edit", {"file_path": str(tmp_path / "a.py"), "old_string": "a", "new_string": "b"}),
                             ("Bash", {"command": "git commit -am wip"}),
                             ("Bash", {"command": "rm -rf app"}),
                             ("WebFetch", {"url": "https://example.com"})):
        assert verdict(decide(ORCH, tool, tool_input, tmp_path)) == "deny", tool_input


@pytest.mark.parametrize("agent_id", SPECIALISTS)
def test_specialists_cannot_delegate(tmp_path: Path, agent_id: str) -> None:
    """No specialist declares Agent, so nobody below the orchestrator can start agents (no recursion)."""
    result = decide(agent_id, "Agent", {"subagent_type": f"{PLUGIN}:{ORCH}", "prompt": "x"}, tmp_path)
    assert verdict(result) == "deny"
    assert "Agent" not in read_markdown(ROOT / "agents" / f"{agent_id}.md")[0]["tools"]


def test_standalone_plugin_does_not_depend_on_the_harness(tmp_path: Path) -> None:
    tree = ast.parse((ROOT / "hooks" / "django_pack_guard.py").read_text(encoding="utf-8"))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"core", "runtime", "agent_runtime", "plugin_packaging", "yaml", "jsonschema",
                           "claude_agent_sdk"}, imported
    for path in (ROOT / "agents" / f"{ORCH}.md", ROOT / "commands" / "orchestrate.md"):
        text = path.read_text(encoding="utf-8")
        assert "core.cli" not in text and "agent_runtime" not in text and "config/" not in text.split(
            "<!-- BEGIN GENERATED")[0].replace("config/orchestration-policy.yaml", "")
    report = packaging.stage(tmp_path / "bundle")
    assert report.ok, report.problems
    staged = {p.relative_to(tmp_path / "bundle").as_posix() for p in (tmp_path / "bundle").rglob("*") if p.is_file()}
    assert f"agents/{ORCH}.md" in staged and "commands/orchestrate.md" in staged
    assert not any(p.startswith(("core/", "runtime/", "agent_runtime/", "config/")) for p in staged)


def _pack_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "pack"
    for name in ("agents", "commands", "skills", "rules", "config", "hooks", "plugin_packaging", ".claude-plugin"):
        shutil.copytree(ROOT / name, copy / name)
    return copy


def test_drift_between_policy_and_generated_files_is_detected(tmp_path: Path) -> None:
    copy = _pack_copy(tmp_path)
    path = copy / "config" / "orchestration-policy.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("max_clarifying_questions: 3", "max_clarifying_questions: 2"),
                    encoding="utf-8")
    report = packaging.run(copy, write=False)
    assert any(f"agents/{ORCH}.md is out of date" in p for p in report.problems), report.problems
    assert packaging.run(copy, write=True).ok
    assert "At most **2** clarifying questions" in (copy / "agents" / f"{ORCH}.md").read_text(encoding="utf-8")
    assert packaging.run(copy, write=True).written == []                    # deterministic, idempotent


def test_hand_edits_to_the_generated_section_are_detected(tmp_path: Path) -> None:
    copy = _pack_copy(tmp_path)
    path = copy / "agents" / f"{ORCH}.md"
    path.write_text(path.read_text(encoding="utf-8").replace("At most **6**", "At most **60**"), encoding="utf-8")
    assert any(f"agents/{ORCH}.md is out of date" in p for p in packaging.run(copy, write=False).problems)


@pytest.mark.parametrize("old, new, message", [
    ("  - django-code-review-agent\n\n# Task", "  - django-code-review-agent\n  - django-ghost-agent\n\n# Task",
     "unknown specialist agent 'django-ghost-agent'"),
    ("      - agent: django-testing-agent\n        purpose: Plan",
     "      - agent: django-orchestrator\n        purpose: Plan", "recursion"),
    ("orchestrator:\n  id: django-orchestrator", "orchestrator:\n  id: django-model-agent", "collides"),
])
def test_packaging_rejects_invalid_policy_references(tmp_path: Path, old: str, new: str, message: str) -> None:
    copy = _pack_copy(tmp_path)
    path = copy / "config" / "orchestration-policy.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    report = packaging.run(copy, write=False)
    assert not report.ok and any(message in p for p in report.problems), report.problems


def test_packaging_requires_a_read_only_orchestrator(tmp_path: Path) -> None:
    copy = _pack_copy(tmp_path)
    path = copy / "plugin_packaging" / "standalone-security.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("  id: django-orchestrator\n  mode: read-only",
                                                             "  id: django-orchestrator\n  mode: workspace-write"),
                    encoding="utf-8")
    assert any("must be read-only" in p for p in packaging.run(copy, write=False).problems)


# ====================================================================== routing (shared contract)


@pytest.mark.parametrize("task, category, agents", [
    ("Help me understand this unfamiliar Django project", "project-analysis", ["django-project-analyzer"]),
    ("Design the app boundaries for a new app for invoicing", "architecture-design",
     ["django-project-analyzer", "django-architecture-agent"]),
    ("Add a unique constraint and a migration to Order", "data-model", ["django-model-agent"]),
    ("Add a DRF endpoint for invoices", "api-development", ["django-api-agent"]),
    ("Check our authentication and permission checks for IDOR", "security-assessment", ["django-security-agent"]),
    ("Write tests for the billing app", "testing", ["django-testing-agent"]),
    ("The orders list is slow, find the N+1", "performance", ["django-performance-agent"]),
    ("Write a runbook for the deployment", "documentation", ["django-documentation-agent"]),
    ("Review my branch before merge", "code-review", ["django-code-review-agent"]),
    ("Implement invoice export end to end with a new model and a DRF endpoint", "feature-delivery",
     ["django-project-analyzer", "django-model-agent", "django-api-agent", "django-testing-agent",
      "django-code-review-agent"]),
])
def test_representative_routes(task: str, category: str, agents: list[str]) -> None:
    plan = plan_route(policy(), task)
    assert plan.category == category and list(plan.agents) == agents and plan.selection == "classified"
    assert len(plan.steps) <= policy().max_delegations
    # sequential by default: each step runs after the previous included one
    for previous, step in zip(plan.steps, plan.steps[1:]):
        assert step.depends_on == (previous.agent,)


def test_smallest_route_skips_optional_steps_the_task_does_not_need() -> None:
    plan = plan_route(policy(), "Add a DRF endpoint for invoices")
    assert "django-model-agent" not in plan.agents
    plan = plan_route(policy(), "Add a DRF endpoint for invoices", include=["django-model-agent"])
    assert plan.agents == ("django-model-agent", "django-api-agent")
    plan = plan_route(policy(), "Add a DRF endpoint with a new field", exclude=["django-model-agent"])
    assert plan.agents == ("django-api-agent",)


@pytest.mark.parametrize("task, code", [("", "empty-task"), ("   ", "empty-task"), ("make it better", "unclassified"),
                                        ("Document the API", "ambiguous")])
def test_empty_unclassified_and_ambiguous_tasks_are_refused(task: str, code: str) -> None:
    with pytest.raises(OrchestrationError) as info:
        plan_route(policy(), task)
    assert info.value.code == code
    if code in ("unclassified", "ambiguous"):
        assert info.value.candidates                                  # the user is told what to choose from
    if code == "ambiguous":
        assert plan_route(policy(), task, category="documentation").agents == ("django-documentation-agent",)


@pytest.mark.parametrize("kwargs, code", [
    ({"category": "nope"}, "unknown-category"),
    ({"category": "testing", "include": ["django-api-agent"]}, "invalid-selection"),
    ({"category": "feature-delivery", "exclude": ["django-testing-agent"]}, "invalid-selection"),
    ({"category": "feature-delivery"}, "missing-implementation"),
])
def test_invalid_selections_are_refused(kwargs: dict, code: str) -> None:
    with pytest.raises(OrchestrationError) as info:
        plan_route(policy(), "do the thing", **kwargs)
    assert info.value.code == code


def test_superset_rule_prefers_the_broader_category_only_when_it_covers_the_others() -> None:
    pol = policy()
    assert classify(pol, "implement the export feature through a DRF endpoint").category == "feature-delivery"
    assert classify(pol, "Document the API").category is None


def test_delegation_bound_is_enforced() -> None:
    doc = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
    doc["limits"]["max_delegations"] = 3
    pol = OrchestrationPolicy.from_dict(doc)
    with pytest.raises(OrchestrationError) as info:
        plan_route(pol, "Implement invoice export end to end with a new model and a DRF endpoint")
    assert info.value.code == "too-many-steps"
    issues = policy_issues(pol, "p", agent_ids=SPECIALISTS, specialist_ids=SPECIALISTS, skill_ids=set(),
                           orchestrator_file=None)
    assert any(i.code == "too-many-steps" and "feature-delivery" in i.message for i in issues)


def test_workflow_document_is_a_valid_contract_and_carries_the_policy(pack_registry: HarnessRegistry) -> None:
    pol = pack_registry.config.orchestration
    plan = plan_route(pol, "Implement invoice export end to end with a new model and a DRF endpoint")
    doc = workflow_document(pol, plan, "Implement invoice export ${inputs.x} end to end")
    pack_registry.contracts.validate("workflow-definition", doc, "orchestrated")
    assert doc["id"] == "orchestrate.feature-delivery" and doc["version"] == pol.version
    assert [s["agent_id"] for s in doc["stages"]] == list(plan.agents)
    assert all(s["max_attempts"] == pol.max_attempts_per_step for s in doc["stages"])
    assert "${inputs" not in json.dumps(doc)                               # task text is never a template
    assert {c["id"] for c in doc["stages"][0]["constraints"]} >= {"stay-in-role", "honest-status", "no-secrets"}
    assert doc["extensions"][EXTENSION_KEY]["orchestrator"] == ORCH


# ====================================================================== harness: policy validation


def test_policy_is_loaded_through_the_framework_config(pack_registry: HarnessRegistry) -> None:
    pol = pack_registry.config.orchestration
    assert pol is not None and pol.orchestrator_id == ORCH
    assert set(pol.specialists) == {a.id for a in pack_registry.agents if a.plugin == PLUGIN} == set(SPECIALISTS)
    assert ORCH not in pack_registry.agents                                  # not a harness agent: no collision


def _expect(root: Path, code: str, text: str) -> None:
    with pytest.raises((ReferenceValidationError, ConfigError)) as info:
        HarnessRegistry.load(root)
    assert any(i.code == code and text in i.message for i in info.value.issues), \
        [(i.code, i.message) for i in info.value.issues]


@pytest.mark.parametrize("mutate, code, text", [
    (lambda d: d["specialists"].append("django-ghost-agent"), "unknown-agent", "django-ghost-agent"),
    (lambda d: d["specialists"].remove("django-testing-agent"), "missing-agent", "django-testing-agent"),
    (lambda d: d["categories"][0]["route"][0].update(agent="django-orchestrator"), "recursion", "django-orchestrator"),
    (lambda d: d["categories"][1]["route"][0].update(after=["django-architecture-agent"]), "invalid-sequence",
     "not an earlier step"),
    (lambda d: d["categories"].append(dict(d["categories"][0])), "duplicate-id", "duplicate category"),
    (lambda d: d["categories"][0].update(supplementary_skills=["django-ghost-skill"]), "unknown-skill",
     "django-ghost-skill"),
    (lambda d: d["orchestrator"].update(id="django-model-agent"), "duplicate-id", "collides"),
    (lambda d: d["orchestrator"].update(path="agents/missing.md"), "missing-path", "agents/missing.md"),
    (lambda d: d["limits"].update(max_delegations=2), "too-many-steps", "feature-delivery"),
    (lambda d: d.update(kind="Wrong"), "kind", "OrchestrationPolicy"),
])
def test_invalid_policy_fails_harness_validation(pack_root: Path, edit_yaml, mutate, code: str, text: str) -> None:
    edit_yaml("config/orchestration-policy.yaml", mutate)
    _expect(pack_root, code, text)


def test_policy_is_optional_for_older_configurations(pack_root: Path, edit_yaml) -> None:
    edit_yaml("config/framework-config.yaml", lambda d: d["configs"].pop("orchestration"))
    assert HarnessRegistry.load(pack_root).config.orchestration is None


# ====================================================================== harness: profiles and dependencies


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    """Like conftest's ``harness_root`` but never pruned (DAE_TEST_PRUNE_UNAVAILABLE does not apply): the
    profile tests need the shipped registries with their optional plugins still registered."""
    from conftest import REPO_ROOT

    root = tmp_path / "raw"
    for folder in ("config", "contracts", "workflows"):
        shutil.copytree(HARNESS_HOME / folder, root / folder)
    framework = root / "config" / "framework-config.yaml"
    data = read_yaml(framework)
    data["paths"]["repo_root"] = str(REPO_ROOT)
    write_yaml(framework, data)
    return root


def _parent_plugins_present() -> bool:
    cfg = read_yaml(ROOT / "config" / "agent-registry.yaml")
    return all((ROOT.parents[1] / p["path"]).is_dir() for p in cfg["plugins"] if p.get("optional"))


def test_optional_plugins_are_classified_and_the_pack_is_mandatory() -> None:
    plugins = {p["id"]: p for p in read_yaml(ROOT / "config" / "agent-registry.yaml")["plugins"]}
    assert {i for i, p in plugins.items() if p.get("optional")} == {"security", "database", "api", "pr-automation"}
    assert not plugins[PLUGIN].get("optional") and all(p.get("description") for p in plugins.values() if p.get("optional"))


def test_default_profile_still_requires_every_plugin(raw_root: Path) -> None:
    config = load_config(raw_root)
    assert config.profile == "full" and config.excluded == ()
    if _parent_plugins_present():
        HarnessRegistry.load(raw_root)
        return
    with pytest.raises(ReferenceValidationError) as info:            # standalone clone: actionable errors
        HarnessRegistry.load(raw_root)
    hints = [i.message for i in info.value.issues if i.code == "missing-path" and "optional integration" in i.message]
    assert len(hints) == 4 and all("--profile pack" in h for h in hints)


def test_pack_profile_excludes_optional_integrations_explicitly(raw_root: Path) -> None:
    registry = HarnessRegistry.load(raw_root, profile="pack")
    cfg = registry.config
    assert cfg.profile == "pack" and [p.id for p in cfg.plugins] == [PLUGIN]
    assert {p.id for p in cfg.excluded_plugins} == {"security", "database", "api", "pr-automation"}
    assert sorted(a.id for a in registry.agents) == SPECIALISTS
    assert "workflow:security-review" in cfg.excluded and registry.excluded_workflows == ("security-review",)
    assert {"agent:security-auditor", "skill:owasp-top10-review", "plugin:security"} <= set(cfg.excluded)
    assert all(t.id != "security-review" for t in cfg.evaluation.targets)


def test_pack_profile_keeps_every_security_control(raw_root: Path, pack_root: Path) -> None:
    pack = HarnessRegistry.load(raw_root, profile="pack")
    pruned = HarnessRegistry.load(pack_root)                           # the test-only pruned copy (Phase 5)
    a, b = pack.config.security, pruned.config.security
    assert (a.denied_tools, a.denied_paths, a.denied_commands, a.approval_commands, a.secret_patterns, a.shell,
            a.write_modes, a.environment) == (b.denied_tools, b.denied_paths, b.denied_commands,
                                              b.approval_commands, b.secret_patterns, b.shell, b.write_modes,
                                              b.environment)
    for agent_id in SPECIALISTS:
        assert SecurityPolicy(a, pack.agents).effective_tools(agent_id) == \
            SecurityPolicy(b, pruned.agents).effective_tools(agent_id)


def test_profile_from_environment_and_unknown_profile(raw_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("DAE_HARNESS_PROFILE", "pack")
    assert load_config(raw_root).profile == "pack"
    with pytest.raises(ConfigError, match="unknown profile 'tiny'"):
        load_config(raw_root, profile="tiny")


def test_missing_mandatory_plugin_is_actionable(raw_root: Path) -> None:
    path = raw_root / "config" / "agent-registry.yaml"
    data = read_yaml(path)
    next(p for p in data["plugins"] if p["id"] == PLUGIN).update(path="missing-pack")
    write_yaml(path, data)
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(raw_root, profile="pack")
    assert any(i.code == "missing-path" and "mandatory for the harness" in i.message for i in info.value.issues)


# ====================================================================== harness: execution


def _run(pack_runner, adapter, task: str, workspace: Path, **plan_kwargs):
    runner = pack_runner(adapter)
    pol = runner.registry.config.orchestration
    plan = plan_route(pol, task, **plan_kwargs)
    execution = runner.start_definition(workflow_document(pol, plan, task), workspace=workspace,
                                        metadata={EXTENSION_KEY: {"category": plan.category}})
    return runner, execution, asyncio.run(runner.run_workflow(execution.execution_id))


def test_route_runs_through_the_existing_workflow_engine(pack_runner, workspace: Path, store) -> None:
    from fakes import FakeSDKAdapter, succeed

    adapter = FakeSDKAdapter(succeed())
    runner, execution, outcome = _run(pack_runner, adapter, "Design the app boundaries for a new app for invoicing",
                                      workspace)
    assert outcome.execution.status is ExecutionStatus.SUCCEEDED
    assert [(s.stage_id, s.stage_status) for s in outcome.stages] == [
        ("django-project-analyzer", StageStatus.SUCCEEDED), ("django-architecture-agent", StageStatus.SUCCEEDED)]
    # registered agents, run under the harness identity, one SDK query per stage
    assert [c.options.extra_args["agent"] for c in adapter.calls] == ["dae-django-project-analyzer",
                                                                     "dae-django-architecture-agent"]
    assert "`upstream.django-project-analyzer.report`" in adapter.calls[1].prompt          # hand-over
    for call in adapter.calls:                                    # registered skills, each loaded once
        skills = call.options.skills
        assert len(skills) == len(set(skills)) and skills
        definition = next(iter(call.options.agents.values()))
        assert all(definition.prompt.count(f'<skill name="{s}"') == 1 for s in skills)
    # persisted with the existing store: metadata, ad-hoc definition, RunRecords
    stored = store.get_execution(execution.execution_id)
    assert stored.workflow_id == "orchestrate.architecture-design"
    assert stored.metadata[EXTENSION_KEY]["category"] == "architecture-design"
    records = store.list_run_records(execution_id=execution.execution_id, newest_first=False, limit=100)
    assert [r.stage_id for r in records] == ["django-project-analyzer", "django-architecture-agent"]
    summary = runner.manager.execution_summary(execution.execution_id)
    assert summary.to_dict()["status"] == "succeeded"


def test_failures_are_recorded_honestly_and_dependents_do_not_run(pack_runner, workspace: Path, store) -> None:
    from fakes import FakeSDKAdapter, error_result, succeed

    adapter = FakeSDKAdapter(succeed(), error_result(), error_result())
    _, execution, outcome = _run(pack_runner, adapter,
                                 "Implement invoice export end to end with a new model and a DRF endpoint", workspace)
    stages = {s.stage_id: s for s in store.list_stages(execution.execution_id)}
    assert stages["django-project-analyzer"].status is StageStatus.SUCCEEDED
    failed = stages["django-model-agent"]
    assert failed.status is StageStatus.FAILED and failed.error is not None
    assert failed.attempts <= failed.max_attempts == 2                             # bounded retries
    assert all(stages[a].status is StageStatus.PENDING for a in ("django-api-agent", "django-testing-agent",
                                                                 "django-code-review-agent"))
    assert outcome.execution.status is not ExecutionStatus.SUCCEEDED
    assert len(adapter.calls) == 1 + failed.attempts                               # nothing else was started


def test_tool_guard_approvals_and_workspace_boundaries_apply_to_routed_stages(pack_runner, workspace: Path,
                                                                             tmp_path: Path) -> None:
    from fakes import FakeSDKAdapter, agent_result

    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    async def scenario(s):
        yield s.init()
        await s.tool("Write", {"file_path": "app/new.py", "content": "x"})
        await s.tool("Read", {"file_path": str(outside)})
        await s.tool("Read", {"file_path": ".env"})
        yield s.result(structured=agent_result(s))

    adapter = FakeSDKAdapter(scenario)
    _run(pack_runner, adapter, "Review my branch before merge", workspace)              # read-only reviewer
    got = [d.get("hookSpecificOutput", {}).get("permissionDecision") for _, d in adapter.calls[0].decisions]
    assert got == ["deny", "deny", "deny"], adapter.calls[0].decisions

    async def remediation(s):
        yield s.init()
        await s.tool("Write", {"file_path": "app/settings.py", "content": "DEBUG = False"})
        yield s.result(structured=agent_result(s))

    adapter = FakeSDKAdapter(remediation)                                  # confirm-writes, default approver denies
    _run(pack_runner, adapter, "Check our auth and permission checks for IDOR", workspace)
    [(_, decision)] = adapter.calls[0].decisions
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (workspace / "app" / "settings.py").read_text(encoding="utf-8") == "DEBUG = False\n"


# ====================================================================== harness: CLI


def _cli(argv: list[str], **kwargs) -> tuple[int, str]:
    from core.cli import main

    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv, **kwargs)
    return code, out.getvalue()


def test_cli_validate_reports_the_profile_and_exclusions(raw_root: Path) -> None:
    code, out = _cli(["--root", str(raw_root), "--profile", "pack", "validate"])
    assert code == 0 and "(profile: pack)" in out and "excluded optional plugin 'security'" in out
    assert "orchestration policy" in out


def test_cli_orchestrate_plan(harness_root: Path, capsys) -> None:
    code, out = _cli(["--root", str(harness_root), "--profile", "pack", "orchestrate", "plan",
                      "Add a DRF endpoint for invoices", "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["category"] == "api-development"
    [step] = doc["steps"]
    assert step["agent"] == "django-api-agent" and step["write_mode"] == "workspace-write"
    assert f"{PLUGIN}:django-api-standards" in step["skills"]
    code, _ = _cli(["--root", str(harness_root), "--profile", "pack", "orchestrate", "plan", "Document the API"])
    assert code == 4 and "choose with --category: api-development, documentation" in capsys.readouterr().err


def test_cli_orchestrate_run_uses_the_runtime(harness_root: Path, workspace: Path, tmp_path: Path) -> None:
    from fakes import FakeSDKAdapter, succeed

    adapter = FakeSDKAdapter(succeed())
    code, out = _cli(["--root", str(harness_root), "--profile", "pack", "orchestrate", "run",
                      "Write tests for the billing app", "--workspace", str(workspace),
                      "--db", str(tmp_path / "db" / "h.db")], adapter=adapter)
    assert code == 0, out
    assert "orchestrate.testing" in out and "django-testing-agent" in out and "succeeded" in out
    assert [c.options.extra_args["agent"] for c in adapter.calls] == ["dae-django-testing-agent"]


def test_launcher_is_standard_library_only_and_never_installs_implicitly() -> None:
    source = (ROOT / "scripts" / "harness.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "argparse", "os", "subprocess", "sys", "venv", "pathlib"}, imported
    assert "rmtree" not in source and "unlink" not in source                 # never deletes environments or data
    reqs = [line for line in (ROOT / "requirements-harness.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    assert reqs and all("==" in r for r in reqs)                              # pinned


def test_launcher_refuses_without_confirmation_and_without_an_environment(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("harness_launcher", ROOT / "scripts" / "harness.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))                        # no terminal attached
    assert launcher.main(["setup", "--venv", str(tmp_path / "venv")]) == 2
    assert not (tmp_path / "venv").exists()                                  # nothing created
    assert launcher.main(["run", "--venv", str(tmp_path / "venv"), "--", "validate"]) == 2
    assert launcher.main(["check", "--venv", str(tmp_path / "venv")]) == 2
    (tmp_path / "notavenv").mkdir()
    with pytest.raises(launcher.SetupError, match="never overwrites"):
        launcher.ensure_venv(tmp_path / "notavenv")
