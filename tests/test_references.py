"""Missing / broken references must fail loading with precise, aggregated issues."""

from pathlib import Path

import pytest

from core.errors import ReferenceValidationError
from core.registry import HarnessRegistry

WORKFLOW = "workflows/security-review.workflow.yaml"


def load_issues(root: Path):
    with pytest.raises(ReferenceValidationError) as info:
        HarnessRegistry.load(root)
    return info.value.issues


def test_stage_references_missing_agent(harness_root: Path, edit_yaml) -> None:
    edit_yaml(WORKFLOW, lambda d: d["stages"][3].__setitem__("agent_id", "security-auditr"))
    [issue] = load_issues(harness_root)
    assert issue.code == "unknown-agent"
    assert issue.source == WORKFLOW
    assert issue.pointer == "stages/3/agent_id"
    assert "did you mean: security-auditor" in issue.message


def test_agent_removed_from_registry(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml",
              lambda d: d.__setitem__("agents", [a for a in d["agents"] if a["id"] != "secrets-scanner"]))
    issues = load_issues(harness_root)
    # referenced by the workflow, the security overrides and an evaluation target
    assert {(i.source, i.code) for i in issues} == {
        (WORKFLOW, "unknown-agent"),
        ("config/security-config.yaml", "unknown-agent"),
        ("config/evaluation-config.yaml", "unknown-agent"),
    }


def test_agent_references_missing_skill(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0]["skills"].append("django-orm-review"))
    [issue] = load_issues(harness_root)
    assert issue.code == "unknown-skill"
    assert issue.pointer == "agents/0/skills/1"


def test_agent_file_missing(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml",
              lambda d: d["agents"][0].__setitem__("path", "plugins/security/agents/nope.md"))
    [issue] = load_issues(harness_root)
    assert issue.code == "missing-path" and "nope.md" in issue.message


def test_skill_directory_without_skill_md(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: d["skills"][0].__setitem__("path", "plugins/security"))
    [issue] = load_issues(harness_root)
    assert issue.code == "missing-path" and "SKILL.md" in issue.message


def test_disabled_agent_cannot_be_scheduled(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0].__setitem__("enabled", False))
    [issue] = load_issues(harness_root)
    assert issue.code == "disabled-agent"


def test_unknown_stage_dependency(harness_root: Path, edit_yaml) -> None:
    edit_yaml(WORKFLOW, lambda d: d["stages"][3].__setitem__("depends_on", ["sast", "secret-scan"]))
    [issue] = load_issues(harness_root)
    assert issue.code == "unknown-stage" and "did you mean: secrets-scan?" in issue.message


def test_dependency_cycle(harness_root: Path, edit_yaml) -> None:
    edit_yaml(WORKFLOW, lambda d: d["stages"][0].__setitem__("depends_on", ["audit"]))
    [issue] = load_issues(harness_root)
    assert issue.code == "cycle"
    assert "audit" in issue.message and "threat-model" in issue.message


def test_duplicate_stage_ids(harness_root: Path, edit_yaml) -> None:
    edit_yaml(WORKFLOW, lambda d: d["stages"][2].__setitem__("id", "secrets-scan"))
    codes = {i.code for i in load_issues(harness_root)}
    assert "duplicate-id" in codes


def test_evaluation_references(harness_root: Path, edit_yaml) -> None:
    def mutate(d):
        d["targets"][0]["id"] = "security-reveiw"
        d["targets"][1]["id"] = "security-review/report"
        d["targets"][2]["criteria"] = ["contract-compliance", "speed"]
    edit_yaml("config/evaluation-config.yaml", mutate)
    codes = sorted(i.code for i in load_issues(harness_root))
    assert codes == ["unknown-criterion", "unknown-stage", "unknown-workflow"]


def test_invalid_workflow_contract(harness_root: Path, edit_yaml) -> None:
    edit_yaml(WORKFLOW, lambda d: d["stages"][0].pop("expected_outputs"))
    issues = load_issues(harness_root)
    assert [(i.source, i.code, i.pointer) for i in issues if i.source == WORKFLOW] == [(WORKFLOW, "schema", "stages/0")]
    # the rejected workflow is not registered, so references to it are reported too
    assert {i.code for i in issues if i.source != WORKFLOW} == {"unknown-workflow"}


def test_duplicate_workflow_id(harness_root: Path) -> None:
    source = harness_root / WORKFLOW
    (harness_root / "workflows" / "copy.workflow.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    [issue] = load_issues(harness_root)
    assert issue.code == "duplicate-id"


def test_registry_id_must_match_frontmatter_name(harness_root: Path, edit_yaml) -> None:
    # pr-reviewer's file declares `name: pr-reviewer`; registering it under another id breaks dispatch
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][7].__setitem__("id", "code-reviewer"))
    # SEC-05: security-config gives pr-reviewer a write mode; keep that entry pointing at the renamed id so the
    # name mismatch stays the only issue (a stale entry is reported as unknown-agent on its own).
    edit_yaml("config/security-config.yaml", lambda d: [o.__setitem__("agent_id", "code-reviewer")
                                                       for o in d["tools"]["agents"] if o["agent_id"] == "pr-reviewer"])
    [issue] = load_issues(harness_root)
    assert issue.code == "name-mismatch" and issue.pointer == "agents/7/id"
    assert "frontmatter name is 'pr-reviewer'" in issue.message


def test_agent_file_without_frontmatter(harness_root: Path, edit_yaml, tmp_path: Path) -> None:
    bare = tmp_path / "bare-agent.md"
    bare.write_text("# Just prose, no frontmatter" + chr(10), encoding="utf-8")
    # A temp file is not inside a registered plugin, so register it as a local agent (Phase 2
    # reports `unregistered-plugin` for source: plugin files outside every plugin).
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0].update(path=str(bare), source="local"))
    [issue] = load_issues(harness_root)
    assert issue.code == "invalid-frontmatter" and "no YAML frontmatter" in issue.message


def test_skill_name_mismatch(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml",
              lambda d: d["skills"][0].__setitem__("path", "plugins/security/skills/security-scan-gates"))
    # Since Phase 5 the same directory registered twice is also reported (duplicate-path):
    # security-scan-gates is registered on its own entry, so both issues are expected.
    issues = load_issues(harness_root)
    assert sorted((i.code, i.pointer) for i in issues) == [
        ("duplicate-path", "skills/2/path"), ("name-mismatch", "skills/0/id")]
    assert all(i.source == "config/skill-registry.yaml" for i in issues)
    [issue] = [i for i in issues if i.code == "name-mismatch"]
    assert "frontmatter name is 'security-scan-gates'" in issue.message
