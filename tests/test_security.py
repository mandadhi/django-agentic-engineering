import io
import json
import logging

from core.log import configure_logging, get_logger
from core.registry import HarnessRegistry
from core.security import Redactor, SecurityPolicy


def test_tool_policy_only_removes_tools(registry: HarnessRegistry) -> None:
    policy = SecurityPolicy(registry.config.security, registry.agents)
    # threat-modeler declares Write in its frontmatter; the policy must not take it away
    assert policy.is_tool_allowed("threat-modeler", "Write")
    # but cannot grant a tool the agent file does not declare
    assert not policy.is_tool_allowed("threat-modeler", "Bash")
    # global deny applies to everyone
    assert not policy.is_tool_allowed("db-schema-designer", "WebFetch")
    # per-agent deny: sast engineer may write reports but not edit source
    assert policy.is_tool_allowed("security-sast-engineer", "Write")
    assert not policy.is_tool_allowed("security-sast-engineer", "Edit")
    assert policy.denied_tools("security-auditor") == {"WebFetch", "WebSearch", "Write", "Edit"}


def test_policy_respects_agent_disallowed_tools(registry: HarnessRegistry) -> None:
    from dataclasses import replace

    agent = replace(registry.agents.get("pr-reviewer"), tools=None, disallowed_tools=("Bash",))

    class Lookup:
        def get(self, agent_id):
            return agent

    policy = SecurityPolicy(registry.config.security, Lookup())
    assert policy.is_tool_allowed("pr-reviewer", "Edit")        # tools omitted = inherits all
    assert not policy.is_tool_allowed("pr-reviewer", "Bash")    # agent's own disallowedTools


def test_denied_paths(registry: HarnessRegistry) -> None:
    policy = SecurityPolicy(registry.config.security, registry.agents)
    assert policy.is_path_denied(".env")
    assert policy.is_path_denied("config\\certs\\server.pem")
    assert policy.is_path_denied("app/secrets/db.txt")
    assert not policy.is_path_denied("app/settings/base.py")


def test_redaction(registry: HarnessRegistry) -> None:
    redactor = Redactor(registry.config.security)
    text = "SECRET_KEY = 'django-insecure-abcdefghijklmnopqrstuvwxyz' and AKIAABCDEFGHIJKLMNOP"
    assert redactor.find(text) == ["aws-access-key-id", "django-secret-key"]
    cleaned = redactor.redact(text)
    assert "AKIA" not in cleaned and "django-insecure" not in cleaned


def test_structured_json_logging_redacts(registry: HarnessRegistry) -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", redact=Redactor(registry.config.security), stream=stream)
    try:
        get_logger("test").info("stage.failed", extra={"execution_id": "e1", "detail": "key AKIAABCDEFGHIJKLMNOP"})
        record = json.loads(stream.getvalue())
        assert record["event"] == "stage.failed"
        assert record["execution_id"] == "e1"
        assert record["logger"] == "dae.test"
        assert record["detail"] == "key [REDACTED]"
    finally:
        configure_logging("WARNING", "text", stream=io.StringIO())
        logging.getLogger("dae").propagate = True
