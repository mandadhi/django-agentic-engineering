"""Opt-in: run a real registered agent through the real Claude Agent SDK.

Skipped unless ``DAE_SDK_INTEGRATION=1``. Needs Claude credentials the bundled CLI can
use (``ANTHROPIC_API_KEY``, or a logged-in Claude Code). It runs ``secrets-scanner``
(model ``haiku`` per its frontmatter) on a tiny throw-away workspace, bounded by
``max_turns``, ``max_budget_usd`` and a timeout, and checks the real session end to end:
plugin/skill loading verified from the ``init`` frame, hooks invoked, secrets masked,
AgentResult validated and stored with its session id and cost.

    DAE_SDK_INTEGRATION=1 .venv/Scripts/python -m pytest -m integration -s
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from agent_runtime.runner import AgentRunner
from runtime.execution import ExecutionManager
from runtime.state import AgentRunStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("DAE_SDK_INTEGRATION") != "1",
                       reason="real SDK run is opt-in: set DAE_SDK_INTEGRATION=1 (uses Claude credentials)"),
]

FAKE_KEY = "AKIA" + "Q" * 16      # matches the aws-access-key-id pattern; not a real key


def test_real_agent_execution(tmp_path, registry, store, context_manager) -> None:
    workspace = tmp_path / "shop"
    (workspace / "shop").mkdir(parents=True)
    (workspace / "shop" / "settings.py").write_text(
        "DEBUG = False\nAWS_ACCESS_KEY_ID = '" + FAKE_KEY + "'\nALLOWED_HOSTS = ['shop.example']\n", encoding="utf-8")
    (workspace / ".env").write_text("DATABASE_PASSWORD=do-not-read\n", encoding="utf-8")

    runtime = replace(registry.config.framework.agent_runtime, max_turns=15, max_budget_usd=0.5)
    registry = replace(registry, config=replace(registry.config,
                                                framework=replace(registry.config.framework, agent_runtime=runtime)))
    manager = ExecutionManager(store, registry)
    runner = AgentRunner(manager, context_manager, artifacts_root=tmp_path / "artifacts", timeout_override=300)
    execution = runner.start_agent(
        "secrets-scanner", "Scan this small Django project for hard-coded credentials and report each finding "
        "with file and line. Keep it brief.", workspace=workspace,
        expected_outputs=[{"name": "secrets-findings", "type": "json"}])
    outcome = asyncio.run(runner.run_workflow(execution.execution_id))

    [agent_run] = store.list_agent_runs(execution.execution_id)
    logs = store.list_logs(execution.execution_id)
    tool_events = [(e.event, e.data.get("tool"), e.data.get("summary")) for e in logs if e.event.startswith("tool.")]
    print("\nstage:", [(s.stage_id, s.stage_status.value, s.failure) for s in outcome.stages])
    print("run:", agent_run.status, agent_run.session_id, agent_run.cost_usd, agent_run.num_turns)
    print("sdk_metadata:", json.dumps(agent_run.sdk_metadata, indent=1))
    print("tool events:", *tool_events, sep="\n  ")
    print("result:", json.dumps(agent_run.result, indent=1)[:3000])

    assert agent_run.session_id, "the SDK session id was not captured"
    assert agent_run.sdk_metadata.get("plugins") == ["security"]       # verified from the real init frame
    assert agent_run.status is AgentRunStatus.SUCCEEDED, agent_run.error
    registry.contracts.validate("agent-result", agent_run.result)
    assert "StructuredOutput" in agent_run.sdk_metadata["tools"]
    assert agent_run.result["extensions"]["dae.result_source"] == "structured_output"
    assert FAKE_KEY not in json.dumps(agent_run.result)
    assert all(FAKE_KEY not in e.message and FAKE_KEY not in json.dumps(e.data) for e in logs)
    for artifact in store.list_artifacts(execution.execution_id):
        path = Path(url2pathname(urlparse(artifact.uri).path))
        assert FAKE_KEY not in path.read_text(encoding="utf-8")
    assert tool_events, "the PreToolUse hook never ran"


def test_real_denied_path_is_enforced(tmp_path, registry, store, context_manager) -> None:
    """A read of a denied path is refused by the live CLI (native deny rule and/or the hook)."""
    workspace = tmp_path / "shop"
    workspace.mkdir()
    marker = "zebra-" + "7" * 12
    (workspace / ".env").write_text(f"DATABASE_PASSWORD={marker}\n", encoding="utf-8")
    (workspace / "README.md").write_text("Shop project.\n", encoding="utf-8")

    runtime = replace(registry.config.framework.agent_runtime, max_turns=8, max_budget_usd=0.25)
    registry = replace(registry, config=replace(registry.config,
                                                framework=replace(registry.config.framework, agent_runtime=runtime)))
    manager = ExecutionManager(store, registry)
    runner = AgentRunner(manager, context_manager, artifacts_root=tmp_path / "artifacts", timeout_override=240)
    execution = runner.start_agent(
        "secrets-scanner", "Use the Read tool on the file .env in the workspace root and report whether it could "
        "be read. If it cannot be read, say so and stop.", workspace=workspace,
        expected_outputs=[{"name": "secrets-findings", "type": "json"}])
    asyncio.run(runner.run_workflow(execution.execution_id))

    [agent_run] = store.list_agent_runs(execution.execution_id)
    logs = store.list_logs(execution.execution_id)
    events = [(e.event, e.data.get("tool"), e.message) for e in logs if e.event.startswith("tool.")]
    print("\nrun:", agent_run.status, agent_run.session_id, agent_run.cost_usd,
          "permission_denials:", agent_run.sdk_metadata.get("permission_denials"))
    print("tool events:", *events, sep="\n  ")
    print("summary:", (agent_run.result or {}).get("summary"))
    assert agent_run.session_id
    denied_by_hook = any(e == "tool.denied" and t == "Read" for e, t, _ in events)
    denied_natively = (agent_run.sdk_metadata.get("permission_denials") or 0) > 0
    assert denied_by_hook or denied_natively, "the .env read was not refused"
    assert marker not in json.dumps(agent_run.result)
    for artifact in store.list_artifacts(execution.execution_id):
        assert marker not in Path(url2pathname(urlparse(artifact.uri).path)).read_text(encoding="utf-8")
