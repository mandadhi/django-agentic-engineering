from pathlib import Path

import pytest

from core import HARNESS_HOME
from core.config import load_config
from core.errors import ConfigError
from tests.conftest import REPO_ROOT


def test_shipped_configuration_loads() -> None:
    config = load_config(HARNESS_HOME)
    assert config.framework.name == "django-agentic-engineering"
    assert config.framework.repo_root == REPO_ROOT.resolve()
    assert config.framework.storage.backend == "sqlite"
    assert config.framework.storage.sqlite_path == (HARNESS_HOME / "var" / "harness.db").resolve()
    assert {a.id for a in config.agents} >= {"security-auditor", "threat-modeler"}


def test_typed_values(harness_root: Path) -> None:
    config = load_config(harness_root)
    auditor = next(a for a in config.agents if a.id == "security-auditor")
    assert auditor.path.is_file()
    assert auditor.skills == ("owasp-top10-review", "security-scan-gates")
    assert auditor.context_needs.scopes == ("project", "workflow", "task", "agent")
    assert config.workflow.defaults.max_attempts == 2
    assert config.workflow.definitions_dir == (harness_root / "workflows").resolve()
    assert config.security.max_inline_bytes == 4096
    assert {c.id for c in config.evaluation.criteria} >= {"contract-compliance"}


def test_config_files_located_through_framework_config(harness_root: Path, edit_yaml) -> None:
    moved = harness_root / "config" / "registries"
    moved.mkdir()
    (harness_root / "config" / "agent-registry.yaml").rename(moved / "agents.yaml")
    edit_yaml("config/framework-config.yaml",
              lambda d: d["configs"].__setitem__("agent_registry", "config/registries/agents.yaml"))
    assert len(load_config(harness_root).agents) == 17


def test_alternate_framework_file(harness_root: Path) -> None:
    (harness_root / "config" / "framework-config.yaml").rename(harness_root / "config" / "staging.yaml")
    config = load_config(harness_root, "config/staging.yaml")
    assert config.sources["framework"].name == "staging.yaml"


def test_missing_config_file_is_reported(harness_root: Path) -> None:
    (harness_root / "config" / "security-config.yaml").unlink()
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert [i.code for i in info.value.issues] == ["missing-file"]
    assert "security-config.yaml" in str(info.value)


def test_invalid_yaml_reports_location(harness_root: Path) -> None:
    (harness_root / "config" / "workflow-config.yaml").write_text("kind: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    issue = info.value.issues[0]
    assert issue.code == "yaml" and issue.pointer.startswith("line ")
