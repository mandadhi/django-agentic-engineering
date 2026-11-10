from pathlib import Path

import pytest

from core.errors import RegistryLookupError
from core.frontmatter import read_frontmatter
from core.registry import HarnessRegistry


def test_discovers_from_configuration(registry: HarnessRegistry) -> None:
    assert len(registry.agents) == 17          # 8 reused plugin agents + 9 Django pack agents
    assert len(registry.skills) == 33          # 9 reused plugin skills + 12 Django pack skills + 12 rules
    assert registry.workflows.ids() == ("security-review",)
    assert "pr-reviewer" in registry.agents


def test_local_django_skills(registry: HarnessRegistry) -> None:
    local = {s.id for s in registry.skills if s.plugin == "django-engineering"}
    assert local == {s.id for s in registry.skills.by_tag("django")}
    local -= {s.id for s in registry.skills.by_tag("rule")}      # Phase 3C rules are also pack + django
    assert len(local) == 12 and "django-orm" in local
    # description is read from each SKILL.md's frontmatter, not duplicated in the registry
    orm = registry.skills.get("django-orm")
    assert orm.description == str(read_frontmatter(orm.path / "SKILL.md")["description"]).strip()
    assert "QuerySet" in orm.description


def test_lookup_and_helpers(registry: HarnessRegistry) -> None:
    assert registry.agents.get("db-migration-engineer").source == "plugin"
    assert [s.id for s in registry.skills_for("pr-reviewer")] == ["pr-review-standards", "pr-diff-hygiene"]
    # sdlc_phase comes from each agent file's frontmatter
    assert {a.id for a in registry.agents.by_phase("Design")} == {"db-schema-designer", "api-contract-designer"}
    assert {s.id for s in registry.skills.by_tag("api")} == {"api-design-standards", "api-versioning-policy"}


def test_unknown_id_suggests_close_match(registry: HarnessRegistry) -> None:
    with pytest.raises(RegistryLookupError) as info:
        registry.agents.get("security-auditer")
    assert info.value.suggestions[0] == "security-auditor"     # best match first
    assert "Did you mean: security-auditor" in str(info.value)


def test_new_agent_appears_without_code_change(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"].append({
        "id": "db-query-tuner", "source": "plugin", "path": "plugins/database/agents/db-query-tuner.md",
        "skills": [],
    }))
    assert "db-query-tuner" in HarnessRegistry.load(harness_root).agents


def test_new_workflow_file_is_discovered(harness_root: Path) -> None:
    (harness_root / "workflows" / "schema-review.workflow.yaml").write_text("""
schema_version: "1.0"
kind: WorkflowDefinition
id: schema-review
version: 1.0.0
stages:
  - id: design
    agent_id: db-schema-designer
    objective: Review the Django models for normalisation issues.
    expected_outputs: [{name: review, type: report}]
""", encoding="utf-8")
    (harness_root / "workflows" / "notes.yaml").write_text("not: a workflow", encoding="utf-8")
    registry = HarnessRegistry.load(harness_root)
    assert set(registry.workflows.ids()) == {"security-review", "schema-review"}


def test_workflow_defaults_applied(registry: HarnessRegistry) -> None:
    workflow = registry.workflows.get("security-review")
    assert workflow.stage("sast").max_attempts == 2           # from workflow-config defaults
    assert workflow.stage("audit").max_attempts == 1          # stage override
    assert workflow.stage("audit").approval_required is True
    assert workflow.topological_order()[0] == "threat-model"
    assert workflow.topological_order()[-1] == "audit"
    assert workflow.runnable_stages({"threat-model"}, {"secrets-scan", "sast", "audit"}) == ("secrets-scan", "sast")


def test_agent_metadata_comes_from_markdown(registry: HarnessRegistry) -> None:
    auditor = registry.agents.get("security-auditor")
    assert auditor.model == "opus"
    assert auditor.tools == ("Read", "Grep", "Glob", "Bash")
    assert auditor.sdlc_phases == ("Security",)
    from core.frontmatter import read_frontmatter

    assert auditor.description == read_frontmatter(auditor.path)["description"].strip()
    skill = registry.skills.get("owasp-top10-review")
    assert skill.description == read_frontmatter(skill.path / "SKILL.md")["description"].strip()


def test_registry_rejects_duplicated_agent_metadata(harness_root: Path, edit_yaml) -> None:
    from core.errors import ConfigError

    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0].__setitem__("model", "haiku"))
    with pytest.raises(ConfigError, match="'model' was unexpected"):
        HarnessRegistry.load(harness_root)
