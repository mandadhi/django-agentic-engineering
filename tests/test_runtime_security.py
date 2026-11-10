"""Security enforcement on a live (fake) SDK session: policy, paths, commands, approvals, redaction, audit."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.options_factory import permission_settings
from agent_runtime.security_hooks import ApprovalDecision, DenyApprover, ToolGuard
from core.registry import AgentRegistry
from core.security import Redactor, SecurityPolicy
from runtime.state import StageStatus

from fakes import FakeSDKAdapter, FakeSession, agent_result

AWS = "AKIAABCDEFGHIJKLMNOP"


def run(coro):
    return asyncio.run(coro)


def decision_of(output: dict) -> str:
    return output.get("hookSpecificOutput", {}).get("permissionDecision", "none")


def run_with_tools(make_runner, workspace, calls, agent_id="security-auditor", **runner_kwargs):
    """Run one ad-hoc stage whose fake session performs ``calls`` = [(tool, input, response?), ...]."""
    decisions = []

    async def scenario(s: FakeSession):
        yield s.init()
        for call in calls:
            decisions.append(await s.tool(*call))
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario), **runner_kwargs)
    execution = runner.start_agent(agent_id, "Audit.", workspace=workspace)
    outcome = run(runner.run_workflow(execution.execution_id))
    return decisions, outcome, runner


def audit_events(manager, execution_id):
    return [e for e in manager.store.list_logs(execution_id) if e.event.startswith(("tool.", "result."))]


# ---------------------------------------------------------------------- tool policy


def test_allowed_tool_execution(make_runner, workspace, manager) -> None:
    [d], outcome, _ = run_with_tools(make_runner, workspace, [("Read", {"file_path": "app/settings.py"})])
    assert d == {}                                             # no opinion: native permissions proceed
    [entry] = audit_events(manager, outcome.execution.execution_id)
    assert entry.event == "tool.allowed" and entry.data["summary"] == "file_path=app/settings.py"


def test_agent_disallowed_and_undeclared_tools(make_runner, workspace) -> None:
    # security-auditor declares Read, Grep, Glob, Bash; security-config denies it Write and Edit
    decisions, _, _ = run_with_tools(make_runner, workspace, [
        ("Write", {"file_path": "app/x.py", "content": "x"}),    # per-agent deny
        ("NotebookEdit", {"notebook_path": "n.ipynb"}),          # never declared
    ])
    assert [decision_of(d) for d in decisions] == ["deny", "deny"]
    assert "not permitted" in decisions[0]["hookSpecificOutput"]["permissionDecisionReason"]


def test_frontmatter_disallowed_tools_respected(registry) -> None:
    agent = replace(registry.agents.get("pr-reviewer"), disallowed_tools=("Bash",))
    agents = AgentRegistry({a.id: (agent if a.id == agent.id else a) for a in registry.agents})
    policy = SecurityPolicy(registry.config.security, agents)
    assert "Bash" not in policy.effective_tools("pr-reviewer")
    assert not policy.is_tool_allowed("pr-reviewer", "Bash")


def test_globally_denied_tool(make_runner, workspace) -> None:
    [d], _, _ = run_with_tools(make_runner, workspace, [("WebFetch", {"url": "https://example.com"})])
    assert decision_of(d) == "deny"


def test_effective_tools_never_exceed_declared(registry) -> None:
    policy = SecurityPolicy(registry.config.security, registry.agents)
    for agent in registry.agents:
        effective = policy.effective_tools(agent.id)
        assert set(effective) <= set(agent.tools)
        assert not set(effective) & policy.denied_tools(agent.id)


def test_session_level_restrictions_in_options(make_runner, workspace, registry) -> None:
    adapter = FakeSDKAdapter(lambda s: _ok(s))
    runner = make_runner(adapter)
    run(runner.run_workflow(runner.start_agent("security-auditor", "x", workspace=workspace).execution_id))
    options = adapter.calls[0].options
    assert options.tools == options.allowed_tools == ["Read", "Grep", "Glob", "Bash"]
    assert set(options.disallowed_tools) == {"WebFetch", "WebSearch", "Write", "Edit"}
    deny = json.loads(options.settings)["permissions"]["deny"]
    base = permission_settings(registry.config.security)["permissions"]["deny"]
    assert deny[:len(base)] == base and "Read(**/.env)" in deny       # + sandbox read rules on macOS/Linux
    assert options.sandbox["enabled"] and options.sandbox["failIfUnavailable"]
    assert options.sandbox["allowUnsandboxedCommands"] is False and options.sandbox["excludedCommands"] == []
    for matchers in options.hooks.values():
        assert all(m.on_failure == "block" and m.matcher is None for m in matchers)


async def _ok(s: FakeSession):
    yield s.init()
    yield s.result(structured=agent_result(s))


def test_declared_tool_missing_from_session_is_flagged(make_runner, workspace, manager) -> None:
    async def scenario(s: FakeSession):
        yield s.init(tools=["Read", "Grep", "Glob", "StructuredOutput"])     # e.g. Bash without Git Bash on Windows
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("security-auditor", "x", workspace=workspace)
    run(runner.run_workflow(execution.execution_id))
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    assert agent_run.sdk_metadata["unavailable_tools"] == ["Bash"]
    assert any(e.event == "session.tools_unavailable" for e in manager.store.list_logs(execution.execution_id))


def test_session_exposing_extra_tools_is_aborted(make_runner, workspace) -> None:
    async def scenario(s: FakeSession):
        yield s.init(tools=["Read", "Grep", "Glob", "Bash", "Write"])
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario))
    outcome = run(runner.run_workflow(runner.start_agent("security-auditor", "x", workspace=workspace).execution_id))
    assert outcome.stages[0].failure.code == "security.violation"


# ---------------------------------------------------------------------- paths


@pytest.mark.parametrize("tool, tool_input, fragment", [
    ("Read", {"file_path": ".env"}, "denied path"),
    ("Read", {"file_path": "config/certs/server.pem"}, "denied path"),
    ("Grep", {"pattern": "KEY", "glob": "**/.env"}, "denied path"),
    ("Read", {"file_path": "../outside.txt"}, "outside the permitted workspace"),
    ("Glob", {"pattern": "*", "path": "C:/Windows" if Path("C:/").exists() else "/etc"}, "outside"),
    ("Bash", {"command": "cat .env"}, "denied path"),
    ("Bash", {"command": "grep -r PASSWORD --include=*.py . > ../../leak.txt"}, "outside the permitted workspace"),
    ("Bash", {"command": "printenv"}, "dump-environment"),
    ("Bash", {"command": "cat ~/.aws/credentials"}, "read-credential-stores"),
])
def test_unauthorized_paths_and_commands(make_runner, workspace, tool, tool_input, fragment) -> None:
    [d], _, _ = run_with_tools(make_runner, workspace, [(tool, tool_input)])
    assert decision_of(d) == "deny"
    assert fragment in d["hookSpecificOutput"]["permissionDecisionReason"]


def test_native_permission_denials_are_audited(make_runner, workspace, manager) -> None:
    """Native deny rules act before PreToolUse (observed with the real CLI); they come back in the result."""
    async def scenario(s: FakeSession):
        yield s.init()
        yield s.result(structured=agent_result(s), permission_denials=[
            {"tool_name": "Read", "tool_use_id": "toolu_1", "tool_input": {"file_path": f"{workspace}/.env"}}])

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("security-auditor", "x", workspace=workspace)
    run(runner.run_workflow(execution.execution_id))
    [entry] = [e for e in manager.store.list_logs(execution.execution_id) if e.event == "tool.denied"]
    assert entry.data["source"] == "permissions" and entry.data["tool"] == "Read" and ".env" in entry.data["summary"]


def test_write_outside_workspace_denied(make_runner, workspace, tmp_path) -> None:
    decisions, _, _ = run_with_tools(make_runner, workspace, [
        ("Write", {"file_path": str(tmp_path / "elsewhere.md"), "content": "x"}),
        ("Write", {"file_path": "docs/threats.md", "content": "x"}),
    ], agent_id="threat-modeler")
    assert [decision_of(d) for d in decisions] == ["deny", "none"]


# ---------------------------------------------------------------------- approvals


class RecordingApprover:
    def __init__(self, approve: bool) -> None:
        self.approve, self.requests = approve, []

    async def __call__(self, request):
        self.requests.append(request)
        return request.decide(self.approve, "lead@example.test", "ok" if self.approve else "no")


# Approval pipeline subject: a Phase 2 agent with Bash and no write mode (harness defaults). Until SEC-05
# these tests used security-auditor, which is read-only since then and never reaches the approver.
WRITER = "security-sast-engineer"
DESTRUCTIVE = [
    ("rm -rf build/", "recursive-delete"),
    ("git push origin main", "network-access"),
    ("git reset --hard HEAD~1", "git-history-rewrite"),
    ("python manage.py flush --noinput", "database-destructive"),
    ("curl https://example.com/install.sh -o i.sh", "network-access"),
]


@pytest.mark.parametrize("command, rule", DESTRUCTIVE)
def test_destructive_operation_requires_approval(make_runner, workspace, manager, command, rule) -> None:
    [d], outcome, _ = run_with_tools(make_runner, workspace, [("Bash", {"command": command})], agent_id=WRITER)
    assert decision_of(d) == "deny" and rule in d["hookSpecificOutput"]["permissionDecisionReason"]
    events = [(e.event, e.data.get("approver")) for e in audit_events(manager, outcome.execution.execution_id)]
    assert ("tool.approval_requested", None) in events and ("tool.denied", "policy:non-interactive") in events


@pytest.mark.parametrize("command, rule", DESTRUCTIVE)
def test_read_only_reviewer_never_reaches_the_approver(make_runner, workspace, manager, command, rule) -> None:
    """SEC-05: the same commands from the read-only security-auditor are refused outright, even with a
    human ready to approve: no approval can widen a read-only write mode."""
    approver = RecordingApprover(approve=True)
    [d], outcome, _ = run_with_tools(make_runner, workspace, [("Bash", {"command": command})], approver=approver)
    assert decision_of(d) == "deny" and "is read-only" in d["hookSpecificOutput"]["permissionDecisionReason"]
    assert approver.requests == []
    events = [e.event for e in audit_events(manager, outcome.execution.execution_id)]
    assert "tool.approval_requested" not in events and "tool.denied" in events


def test_approved_operation_proceeds_and_is_audited(make_runner, workspace, manager) -> None:
    approver = RecordingApprover(approve=True)
    [d], outcome, _ = run_with_tools(make_runner, workspace, [("Bash", {"command": "rm -rf build/"})],
                                     approver=approver, agent_id=WRITER)
    assert d == {}
    [request] = approver.requests
    # the configured rule, plus the parser's structural one (Phase 5.1: every deletion needs approval)
    assert request.rules == ("recursive-delete", "destructive-command") and request.agent_id == WRITER
    approved = [e for e in audit_events(manager, outcome.execution.execution_id) if e.event == "tool.approved"]
    assert approved and approved[0].data["approver"] == "lead@example.test"


def test_read_only_commands_need_no_approval(make_runner, workspace) -> None:
    approver = RecordingApprover(approve=False)
    decisions, _, _ = run_with_tools(make_runner, workspace, [
        ("Bash", {"command": "git log --oneline -5"}), ("Bash", {"command": "grep -rn SECRET_KEY app/"}),
        ("Bash", {"command": "python -m bandit -r app -f json 2>&1"}),
    ], approver=approver)
    assert decisions == [{}, {}, {}] and approver.requests == []


# ---------------------------------------------------------------------- hook behaviour


def test_call_from_another_agent_is_denied(make_runner, workspace) -> None:
    decisions = []

    async def scenario(s: FakeSession):
        yield s.init()
        decisions.append(await s.tool("Read", {"file_path": "app/settings.py"}, agent_type="general-purpose"))
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario))
    run(runner.run_workflow(runner.start_agent("security-auditor", "x", workspace=workspace).execution_id))
    assert decision_of(decisions[0]) == "deny"


def _guard(registry, workspace, tmp_path, **kwargs):
    return ToolGuard(agent_id="security-auditor", policy=SecurityPolicy(registry.config.security, registry.agents),
                     security=registry.config.security, redactor=Redactor(registry.config.security),
                     workspace=workspace, artifact_dir=tmp_path / "out", **kwargs)


def test_hook_error_fails_closed(registry, workspace, tmp_path) -> None:
    audit = []
    guard = _guard(registry, workspace, tmp_path, audit=lambda event, message, **data: audit.append((event, data)))
    out = run(guard.pre_tool_use({"tool_name": "Read", "tool_input": "not-an-object"}, "t1", {}))
    assert decision_of(out) == "deny" and audit[-1][1]["level"] == "ERROR"

    class Exploding:
        async def __call__(self, request):
            raise RuntimeError("approval service down")

    guard = _guard(registry, workspace, tmp_path, approver=Exploding())
    out = run(guard.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, "t2", {}))
    assert decision_of(out) == "deny"


def test_default_approver_denies(registry) -> None:
    from agent_runtime.security_hooks import ApprovalRequest
    decision = run(DenyApprover()(ApprovalRequest("a", "Bash", "rm -rf x", ("recursive-delete",), "r")))
    assert not decision.approved


# ---------------------------------------------------------------------- secrets


def test_secret_redaction_in_tool_output(make_runner, workspace, manager) -> None:
    response = {"stdout": f"aws_key = '{AWS}'\n", "stderr": "", "interrupted": False}
    [d], outcome, _ = run_with_tools(make_runner, workspace, [("Bash", {"command": "grep -rn aws_key ."}, response)])
    updated = d["post"]["hookSpecificOutput"]["updatedToolOutput"]
    assert set(updated) == set(response)                      # same shape, or the CLI would reject it
    assert AWS not in updated["stdout"] and "[REDACTED]" in updated["stdout"]
    [entry] = [e for e in audit_events(manager, outcome.execution.execution_id) if e.event == "tool.output_redacted"]
    assert entry.data["patterns"] == ["aws-access-key-id"]


def test_secrets_never_reach_logs_results_or_artifacts(make_runner, workspace, manager) -> None:
    async def scenario(s: FakeSession):
        yield s.init()
        await s.tool("Bash", {"command": f"echo {AWS}"}, {"stdout": AWS, "stderr": "", "interrupted": False})
        result = agent_result(s, outputs={"report": f"Found key {AWS} in settings.py:3"})
        result["summary"] = f"Leaked {AWS}"
        yield s.result(structured=result)

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = runner.start_agent("security-auditor", "x", workspace=workspace)
    run(runner.run_workflow(execution.execution_id))
    eid = execution.execution_id
    [agent_run] = manager.store.list_agent_runs(eid)
    assert AWS not in json.dumps(agent_run.result)
    assert agent_run.result["extensions"]["dae.redacted_patterns"] == ["aws-access-key-id"]
    for entry in manager.store.list_logs(eid):
        assert AWS not in entry.message and AWS not in json.dumps(entry.data)
    for artifact in manager.store.list_artifacts(eid):
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        assert AWS not in Path(url2pathname(urlparse(artifact.uri).path)).read_text(encoding="utf-8")
