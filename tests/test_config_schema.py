from pathlib import Path

import pytest

from core.config import load_config
from core.errors import ConfigError, ConfigVersionError


def codes(exc: ConfigError) -> list[str]:
    return [issue.code for issue in exc.issues]


def test_wrong_type_is_rejected_with_pointer(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/workflow-config.yaml", lambda d: d["defaults"].__setitem__("max_attempts", "two"))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    issue = info.value.issues[0]
    assert issue.source == "config/workflow-config.yaml"
    assert issue.pointer == "defaults/max_attempts"
    assert "'two' is not of type 'integer'" in issue.message


def test_unknown_key_is_rejected(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0].__setitem__("skils", []))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert "'skils' was unexpected" in str(info.value)


def test_missing_required_field(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: d["skills"][1].pop("path"))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert info.value.issues[0].pointer == "skills/1"
    assert "'path' is a required property" in info.value.issues[0].message


def test_invalid_identifier_pattern(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"][0].__setitem__("id", "Threat Modeler"))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert info.value.issues[0].pointer == "agents/0/id"


def test_unsupported_schema_version(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/evaluation-config.yaml", lambda d: d.__setitem__("schema_version", "2.0"))
    with pytest.raises(ConfigVersionError) as info:
        load_config(harness_root)
    assert codes(info.value) == ["version"]
    assert "schema_version '2.0' is not supported (supported: 1.0)" in str(info.value)


def test_wrong_kind(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/security-config.yaml", lambda d: d.__setitem__("kind", "AgentRegistry"))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert "kind" in codes(info.value)


def test_duplicate_ids(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/skill-registry.yaml", lambda d: d["skills"].append(dict(d["skills"][0])))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert codes(info.value) == ["duplicate-id", "duplicate-id"]


def test_invalid_secret_regex(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/security-config.yaml",
              lambda d: d["secrets"]["patterns"].append({"id": "broken", "regex": "(unclosed"}))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert codes(info.value) == ["invalid-regex"]


def test_unsupported_storage_backend(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/framework-config.yaml", lambda d: d["storage"].__setitem__("backend", "postgres"))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    assert info.value.issues[0].pointer == "storage/backend"


def test_all_problems_reported_together(harness_root: Path, edit_yaml) -> None:
    edit_yaml("config/workflow-config.yaml", lambda d: d["defaults"].__setitem__("max_attempts", 0))
    edit_yaml("config/evaluation-config.yaml", lambda d: d["criteria"][0].__setitem__("threshold", 1.5))
    with pytest.raises(ConfigError) as info:
        load_config(harness_root)
    sources = {issue.source for issue in info.value.issues}
    assert sources == {"config/workflow-config.yaml", "config/evaluation-config.yaml"}
