"""SEC-05 and SEC-06: harness-enforced write modes with capability-based, default-deny authorization.

SEC-05: the reused Phase 2 "read-only" reviewers (security-auditor, secrets-scanner, pr-reviewer) had no
harness write mode; they could change the workspace through Bash. security-config now gives them
``write_mode: read-only``, combined with a plugin's ``agent_policy`` so that the stricter mode applies.

SEC-06: read-only and confirm-writes Bash enforcement was a block-list of state-changing commands.
Every tool and every simple shell command is now classified by its effect (``core.shell_syntax.classify``)
and checked against the capabilities its write mode grants (security-config ``write_modes``). Anything
unrecognised is ``unknown``, so read-only denies it and confirm-writes asks for it.

Every test is deterministic. It calls the real guards (harness ``ToolGuard``, standalone
``django_pack_guard``) directly, or drives the real runner with the fake SDK adapter. No model chooses
a command.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from agent_runtime.options_factory import permission_settings
from agent_runtime.sandbox import SANDBOXED
from agent_runtime.security_hooks import ApprovalDecision, ApprovalRequest, ToolGuard
from agent_runtime.workspace import protected_roots
from core import HARNESS_HOME
from core.config import DEFAULT_WRITE_MODES
from core.plugin_policy import AgentWriteMode, resolve_write_mode, stricter
from core.registry import HarnessRegistry
from core.security import Redactor, SecurityPolicy
from core.shell_syntax import analyze
from plugin_packaging import build as packaging

from conftest import read_yaml, write_yaml
from fakes import FakeSDKAdapter, FakeSession, agent_result

REVIEWERS = ("security-auditor", "secrets-scanner", "pr-reviewer")
READ_ONLY_PACK, CONFIRM, WRITER = "django-project-analyzer", "django-security-agent", "django-model-agent"


def run(coro):
    return asyncio.run(coro)


def verdict(output: dict) -> str:
    return output.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def reason(output: dict) -> str:
    return output.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


class Approver:
    def __init__(self, approve: bool = True) -> None:
        self.approve, self.requests = approve, []

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return request.decide(self.approve, "lead@example.test", "ok")


@pytest.fixture
def ws(workspace: Path) -> Path:
    (workspace / "app" / "models.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "README.md").write_text("# demo\n", encoding="utf-8")
    return workspace


def guard_for(registry: HarnessRegistry, ws: Path, tmp_path: Path, agent_id: str, *, approver=None,
              write_mode: AgentWriteMode | None | str = "resolve") -> ToolGuard:
    """The guard exactly as the runner builds it (write mode resolved from security-config + plugin)."""
    mode = resolve_write_mode(registry.config, registry.agents.get(agent_id)) if write_mode == "resolve" else write_mode
    return ToolGuard(agent_id=agent_id, policy=SecurityPolicy(registry.config.security, registry.agents),
                     security=registry.config.security, redactor=Redactor(registry.config.security),
                     workspace=ws, artifact_dir=tmp_path / "out", write_mode=mode, approver=approver,
                     protected_roots=protected_roots(registry.config), shell_mode=SANDBOXED)


def call(guard: ToolGuard, tool: str, tool_input: dict, tool_use_id: str = "toolu_1") -> dict:
    return run(guard.pre_tool_use({"tool_name": tool, "tool_input": tool_input}, tool_use_id, {}))


def bash(guard: ToolGuard, command: str) -> dict:
    return call(guard, "Bash", {"command": command})


def reviewers(registry: HarnessRegistry) -> list[str]:
    present = [a for a in REVIEWERS if a in {spec.id for spec in registry.agents}]
    if not present:
        pytest.skip("Phase 2 reviewers are not available outside the full repository layout")
    return present


# ====================================================================== effect classification (shared parser)


@pytest.mark.parametrize("command, expected", [
    # reads: program and options known to change nothing
    ("git status", {"read"}), ("git diff main...HEAD", {"read"}), ("git log -5 --oneline", {"read"}),
    ("git branch -a", {"read"}), ("git tag", {"read"}), ("git config --get user.name", {"read"}),
    ("cat app/models.py | head -5", {"read"}), ("grep -rn csrf_exempt app 2>/dev/null", {"read"}),
    ("find . -name '*.py'", {"read"}), ("sed -n 1,5p app/models.py", {"read"}), ("awk '{print $1}' f", {"read"}),
    ("sort -k2 f", {"read"}), ("uniq f", {"read"}), ("python manage.py check", {"read"}),
    ("python manage.py makemigrations --check --dry-run", {"read"}), ("python -m bandit -r app -f json", {"read"}),
    ("pip list", {"read"}), ("LANG=C ls", {"read"}), ("timeout 5 git status", {"read"}),
    ("command -v rm", {"read"}),
    # writes by other means than redirection or the named file tools
    ("cp a b", {"write"}), ("sort -o f f", {"write"}), ("uniq in out", {"write"}), ("sed -i s/a/b/ f", {"write"}),
    ("sed 's/a/b/w out' f", {"write"}), ("awk '{print > \"out\"}' f", {"write"}), ("git diff --output=x", {"write"}),
    ("git branch topic", {"write"}), ("git stash", {"write"}), ("git config user.name x", {"write"}),
    ("python manage.py makemigrations", {"write"}), ("tee out < f", {"write"}), ("touch f", {"write"}),
    ("find . -fprint list", {"read", "write"}),
    # deletion
    ("rm f", {"delete"}), ("rmdir d", {"delete"}), ("unlink f", {"delete"}), ("truncate -s0 f", {"delete"}),
    ("find . -delete", {"read", "delete"}), ("git rm f", {"delete"}), ("git clean -fd", {"delete"}),
    # execution of code the guard cannot see
    ("python script.py", {"execute"}), ("python -m pytest", {"execute"}), ("python manage.py test", {"execute"}),
    ("python -c 'print(1)'", {"execute"}), ("bash script.sh", {"execute"}), ("source env.sh", {"execute"}),
    ("sed 1e f", {"execute"}), ("awk 'BEGIN{system(\"id\")}'", {"execute"}), ("rg --pre cat x", {"execute"}),
    ("git -c alias.x='!rm f' x", {"execute"}), ("git -p log", {"execute"}),
    # network
    ("curl https://example.com", {"network"}), ("pip install requests", {"network", "write"}),
    ("git push origin main", {"network", "write"}),
    # unknown: default deny
    ("make", {"unknown"}), ("npm test", {"unknown"}), ("pytest", {"unknown"}), ("ruff check --fix .", {"unknown"}),
    ("git st", {"unknown"}), ("alias ls=rm", {"unknown"}), ("python manage.py custom_command", {"unknown"}),
    ("x=1", {"unknown"}), ("export PATH=.", {"unknown"}),
])
def test_commands_are_classified_by_effect(command: str, expected: set) -> None:
    facts = analyze(command)
    assert not facts.problems, facts.problems
    assert facts.capabilities() == expected, (command, facts.effects)


@pytest.mark.parametrize("command, hidden", [
    ("bash -c 'rm app/x.py'", "delete"), ("sh -c \"sh -c 'touch x'\"", "write"),
    ("echo $(touch x)", "write"), ("echo `cp a b`", "write"), ("eval 'mv a b'", "write"),
    ("cat <(touch x)", "write"), ("find . -exec touch {} +", "write"), ("ls | xargs rm", "delete"),
    ("timeout 5 bash -c 'git commit -m x'", "write"), ("git status && bash -c 'make'", "unknown"),
    ("find . -name x | xargs uniq a", "unknown"),
])
def test_nested_commands_inherit_classification(command: str, hidden: str) -> None:
    """Commands inside substitutions, -c strings, eval, find -exec and xargs are classified like any other."""
    assert hidden in analyze(command).capabilities()


@pytest.mark.parametrize("command, expected", [
    ("sudo cat f", "execute"), ("nohup ls", "execute"), ("setsid ls", "execute"), ("strace ls", "execute"),
    ("env PATH=/tmp ls", "unknown"), ("PATH=. git status", "unknown"), ("BASH_ENV=x bash -c ls", "unknown"),
    ("env -S 'rm x' ls", "unknown"), ("LD_PRELOAD=x.so ls", "unknown"), ("GIT_PAGER=sh git log", "unknown"),
])
def test_wrappers_and_environment_cannot_expand_permissions(command: str, expected: str) -> None:
    assert expected in analyze(command).capabilities()


# ====================================================================== SEC-05: Phase 2 reviewers


def test_reviewers_get_a_harness_write_mode(registry: HarnessRegistry) -> None:
    for agent_id in reviewers(registry):
        assert registry.config.security.write_mode_of(agent_id) == "read-only"
        mode = resolve_write_mode(registry.config, registry.agents.get(agent_id))
        assert mode is not None and mode.mode == "read-only" and mode.source == "security-config"
    # agents that write keep the harness defaults
    assert resolve_write_mode(registry.config, registry.agents.get("security-sast-engineer")) is None


@pytest.mark.parametrize("command", [
    "rm app/models.py", "sed -i s/x/y/ app/models.py", "mv app/models.py app/m.py", "cp app/models.py app/c.py",
    "touch app/new.py", "echo x > app/x.py", "echo x 2> app/x.py", "cat app/models.py | tee app/c.py",
    "python -c \"open('app/models.py','w')\"", "pytest", "python manage.py test", "git checkout -- .",
    "git commit -am x", "find app -delete", "bash -c 'rm app/models.py'", "sort -o app/models.py app/models.py",
    "git -c alias.x='!rm app/models.py' x", "make", "curl https://example.com",
])
def test_reviewers_cannot_change_the_workspace_through_bash(registry, ws, tmp_path, command) -> None:
    for agent_id in reviewers(registry):
        approver = Approver(approve=True)                      # a human ready to approve changes nothing
        out = bash(guard_for(registry, ws, tmp_path, agent_id, approver=approver), command)
        assert verdict(out) == "deny" and "read-only" in reason(out), (agent_id, command, out)
        assert approver.requests == []


@pytest.mark.parametrize("command", [
    "git status", "git diff main...HEAD", "git log -5 --oneline", "git show HEAD:app/models.py",
    "grep -rn SECRET_KEY app", "ls -la app", "cat app/models.py", "find app -name '*.py'", "wc -l app/models.py",
    "python -m bandit -r app -f json 2>&1", "python manage.py check",
])
def test_reviewers_keep_their_read_operations(registry, ws, tmp_path, command) -> None:
    for agent_id in reviewers(registry):
        assert verdict(bash(guard_for(registry, ws, tmp_path, agent_id), command)) == "allow", (agent_id, command)


@pytest.mark.parametrize("tool, tool_input", [
    ("Write", {"file_path": "app/x.py", "content": "x"}),
    ("Edit", {"file_path": "app/models.py", "old_string": "x", "new_string": "y"}),
    ("NotebookEdit", {"notebook_path": "n.ipynb"}), ("MultiEdit", {"file_path": "app/models.py"}),
])
def test_reviewers_cannot_use_file_write_tools(registry, ws, tmp_path, tool, tool_input) -> None:
    for agent_id in reviewers(registry):
        assert verdict(call(guard_for(registry, ws, tmp_path, agent_id), tool, tool_input)) == "deny"


def test_read_only_holds_even_if_the_agent_declared_write_tools(registry, ws, tmp_path) -> None:
    """Enforcement does not depend on the agent file: a reviewer whose upstream definition later adds
    Write, Edit or an unknown tool is still refused by its write mode."""
    from dataclasses import replace

    from core.registry import AgentRegistry
    agent_id = reviewers(registry)[0]
    widened = replace(registry.agents.get(agent_id), tools=("Read", "Grep", "Glob", "Bash", "Write", "Edit",
                                                             "NotebookEdit", "mcp__fs__write_file"),
                      disallowed_tools=())
    agents = AgentRegistry({a.id: (widened if a.id == agent_id else a) for a in registry.agents})
    security = registry.config.security
    from dataclasses import replace as dc_replace
    security = dc_replace(security, tool_overrides=tuple(dc_replace(o, deny=()) for o in security.tool_overrides))
    guard = ToolGuard(agent_id=agent_id, policy=SecurityPolicy(security, agents), security=security,
                      redactor=Redactor(security), workspace=ws, artifact_dir=tmp_path / "out",
                      write_mode=AgentWriteMode(agent_id, "read-only", source="security-config"),
                      protected_roots=protected_roots(registry.config), shell_mode=SANDBOXED)
    assert verdict(call(guard, "Write", {"file_path": "app/x.py", "content": "x"})) == "deny"
    assert verdict(call(guard, "Edit", {"file_path": "app/models.py"})) == "deny"
    out = call(guard, "mcp__fs__write_file", {"path": "app/x.py"})
    assert verdict(out) == "deny" and "'unknown'" in reason(out)
    assert verdict(call(guard, "Read", {"file_path": "app/models.py"})) == "allow"


def test_reviewers_cannot_touch_harness_configuration(registry, ws, tmp_path) -> None:
    config = (registry.config.framework.root / "config" / "security-config.yaml").as_posix()
    for agent_id in reviewers(registry):
        guard = guard_for(registry, ws, tmp_path, agent_id)
        assert verdict(bash(guard, f"sed -i s/deny/allow/ {config}")) == "deny"
        assert verdict(bash(guard, f"cp app/models.py {config}")) == "deny"


def test_reviewer_session_is_read_only_end_to_end(make_runner, workspace, manager, registry) -> None:
    """Through the runner: the resolved mode reaches the guard, every write is refused, reads run."""
    agent_id = reviewers(registry)[0]
    decisions: list[dict] = []

    async def scenario(s: FakeSession):
        yield s.init()
        for command in ("git status", "sed -i s/False/True/ app/settings.py", "pytest", "rm app/settings.py"):
            decisions.append(await s.tool("Bash", {"command": command}))
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario), approver=Approver(approve=True))
    execution = runner.start_agent(agent_id, "Audit.", workspace=workspace)
    run(runner.run_workflow(execution.execution_id))
    assert [verdict(d) for d in decisions] == ["allow", "deny", "deny", "deny"]
    [shell] = [e for e in manager.store.list_logs(execution.execution_id) if e.event == "session.shell"]
    assert shell.data["write_mode"] == "read-only" and shell.data["write_mode_source"] == "security-config"
    assert (workspace / "app" / "settings.py").read_text(encoding="utf-8") == "DEBUG = False\n"


# ====================================================================== plugin metadata never widens harness policy


def test_stricter_mode_wins() -> None:
    assert stricter("read-only", "workspace-write") == "read-only"
    assert stricter("workspace-write", "confirm-writes") == "confirm-writes"
    assert stricter("confirm-writes", "documentation-only") == "documentation-only"
    assert stricter("workspace-write", "bogus") == "read-only"                     # unknown: fail closed


def test_harness_mode_narrows_a_plugin_mode(edit_yaml, pack_root) -> None:
    edit_yaml("config/security-config.yaml", lambda d: d["tools"]["agents"].append(
        {"agent_id": WRITER, "write_mode": "read-only"}))
    registry = HarnessRegistry.load(pack_root)
    mode = resolve_write_mode(registry.config, registry.agents.get(WRITER))     # plugin says workspace-write
    assert mode.mode == "read-only" and mode.source == "security-config+plugin"


def test_plugin_metadata_cannot_widen_the_harness_mode(edit_yaml, pack_root) -> None:
    # The harness asks for workspace-write, the plugin's policy says read-only: read-only applies.
    edit_yaml("config/security-config.yaml", lambda d: d["tools"]["agents"].append(
        {"agent_id": READ_ONLY_PACK, "write_mode": "workspace-write"}))
    registry = HarnessRegistry.load(pack_root)
    assert resolve_write_mode(registry.config, registry.agents.get(READ_ONLY_PACK)).mode == "read-only"
    # ... and a plugin file that tries to widen a harness read-only agent changes nothing either.
    edit_yaml("config/security-config.yaml", lambda d: d["tools"]["agents"].append(
        {"agent_id": CONFIRM, "write_mode": "read-only"}))
    registry = HarnessRegistry.load(pack_root)
    assert resolve_write_mode(registry.config, registry.agents.get(CONFIRM)).mode == "read-only"


def test_write_mode_values_are_schema_checked(edit_yaml, pack_root) -> None:
    from core.errors import ConfigError
    edit_yaml("config/security-config.yaml", lambda d: d["tools"]["agents"].append(
        {"agent_id": WRITER, "write_mode": "anything-goes"}))
    with pytest.raises(ConfigError):
        HarnessRegistry.load(pack_root)


def test_capability_table_values_are_schema_checked(edit_yaml, pack_root) -> None:
    from core.errors import ConfigError
    edit_yaml("config/security-config.yaml",
              lambda d: d["write_modes"]["read-only"].update(allow=["read", "everything"]))
    with pytest.raises(ConfigError):
        HarnessRegistry.load(pack_root)


def test_shipped_capability_table_matches_the_built_in_defaults(registry: HarnessRegistry) -> None:
    assert dict(registry.config.security.write_modes) == dict(DEFAULT_WRITE_MODES)
    assert registry.config.security.write_modes["read-only"].allow == {"read"}
    assert registry.config.security.write_modes["confirm-writes"].otherwise == "approve"


def test_read_only_workspace_is_write_denied_for_the_os_sandbox(registry, tmp_path) -> None:
    settings = permission_settings(registry.config.security, write_deny=[tmp_path / "ws"])
    assert f"Edit(/{(tmp_path / 'ws').as_posix()}/**)" in settings["permissions"]["deny"]


# ====================================================================== SEC-06: read-only (pack agent)


@pytest.mark.parametrize("command", [
    "python manage.py test", "pytest", "make", "npm test", "ruff check --fix .", "sort -o app/models.py app/models.py",
    "uniq app/models.py app/out", "git -c alias.x='!rm app/models.py' x", "git st", "git branch topic",
    "git stash", "git diff --output=app/d.txt", "sed 's/x/y/w app/out' app/models.py",
    "awk '{print > \"app/out\"}' app/models.py", "bash -c 'python script.py'", "./manage.py migrate",
    "unlink app/models.py", "env PATH=. ls", "x=1", "alias ls=rm",
])
def test_read_only_pack_agent_refuses_every_non_read_effect(pack_registry, ws, tmp_path, command) -> None:
    out = bash(guard_for(pack_registry, ws, tmp_path, READ_ONLY_PACK), command)
    assert verdict(out) == "deny" and "read-only" in reason(out), (command, out)


def test_read_only_names_the_effect_it_refused(pack_registry, ws, tmp_path) -> None:
    out = bash(guard_for(pack_registry, ws, tmp_path, READ_ONLY_PACK), "make docs")
    assert "'make docs' is classified as 'unknown'" in reason(out)
    out = bash(guard_for(pack_registry, ws, tmp_path, READ_ONLY_PACK), "python script.py")
    assert "classified as 'execute'" in reason(out)


def test_read_only_refuses_unknown_tools(pack_registry, ws, tmp_path) -> None:
    from dataclasses import replace

    from core.registry import AgentRegistry
    agent = replace(pack_registry.agents.get(READ_ONLY_PACK), tools=("Read", "Bash", "mcp__db__query", "Task"))
    agents = AgentRegistry({a.id: (agent if a.id == agent.id else a) for a in pack_registry.agents})
    security = pack_registry.config.security
    guard = ToolGuard(agent_id=agent.id, policy=SecurityPolicy(security, agents), security=security,
                      redactor=Redactor(security), workspace=ws, artifact_dir=tmp_path / "out",
                      write_mode=resolve_write_mode(pack_registry.config, agent),
                      protected_roots=protected_roots(pack_registry.config), shell_mode=SANDBOXED)
    assert verdict(call(guard, "mcp__db__query", {"sql": "delete from x"})) == "deny"
    assert verdict(call(guard, "Task", {"prompt": "x"})) == "deny"


# ====================================================================== SEC-06: confirm-writes


@pytest.mark.parametrize("command", [
    "pytest", "python manage.py test", "make", "sort -o app/models.py app/models.py", "cp app/models.py app/c.py",
    "git -c core.pager=cat log", "git st", "uniq app/models.py app/o", "awk '{print > \"app/o\"}' app/models.py",
])
def test_confirm_writes_needs_approval_outside_its_capabilities(pack_registry, ws, tmp_path, command) -> None:
    approver = Approver(approve=False)
    out = bash(guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=approver), command)
    assert verdict(out) == "deny" and "requires human approval" in reason(out), (command, out)
    [request] = approver.requests
    # the capability gate ('confirm-writes') or an existing read_only_commands rule ('file-change-command')
    assert request.rules and request.operation.startswith(command)


@pytest.mark.parametrize("command", ["git status", "grep -rn SECRET app", "cat app/models.py", "python manage.py check"])
def test_confirm_writes_reads_without_approval(pack_registry, ws, tmp_path, command) -> None:
    approver = Approver(approve=False)
    assert verdict(bash(guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=approver), command)) == "allow"
    assert approver.requests == []


def test_confirm_writes_approval_states_the_classification(pack_registry, ws, tmp_path) -> None:
    approver = Approver(approve=False)
    bash(guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=approver), "make migrations")
    assert "'make migrations' is classified as 'unknown'" in approver.requests[0].reason


def test_confirm_writes_approved_call_runs_once(pack_registry, ws, tmp_path) -> None:
    approver = Approver(approve=True)
    guard = guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=approver)
    assert verdict(bash(guard, "pytest")) == "allow"
    assert verdict(call(guard, "Bash", {"command": "pytest"}, "toolu_2")) == "allow"
    assert len(approver.requests) == 2                         # every call is approved on its own
    assert approver.requests[0].digest != approver.requests[1].digest


def test_confirm_writes_approval_cannot_be_reused_for_another_command(pack_registry, ws, tmp_path) -> None:
    class Replaying:
        first = None

        async def __call__(self, request):
            if self.first is None:
                self.first = request.decide(True, "lead", "ok")
            return self.first

    guard = guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=Replaying())
    assert verdict(bash(guard, "pytest")) == "allow"                                  # approved: pytest
    out = bash(guard, "pytest; make deploy")                                          # not what was approved
    assert verdict(out) == "deny" and "does not match" in reason(out)
    out = call(guard, "Bash", {"command": "make deploy"}, "toolu_9")
    assert verdict(out) == "deny" and "does not match" in reason(out)


def test_confirm_writes_command_modified_after_approval_is_refused(pack_registry, ws, tmp_path) -> None:
    tool_input = {"command": "pytest"}

    class Mutating:
        async def __call__(self, request):
            tool_input["command"] = "pytest && bash -c 'rm -rf app'"
            return request.decide(True, "lead", "ok")

    guard = guard_for(pack_registry, ws, tmp_path, CONFIRM, approver=Mutating())
    out = run(guard.pre_tool_use({"tool_name": "Bash", "tool_input": tool_input}, "toolu_m", {}))
    assert verdict(out) == "deny" and "changed after approval" in reason(out)


def test_confirm_writes_unknown_tool_needs_approval(pack_registry, ws, tmp_path) -> None:
    from dataclasses import replace

    from core.registry import AgentRegistry
    agent = replace(pack_registry.agents.get(CONFIRM), tools=("Read", "Bash", "mcp__db__query"))
    agents = AgentRegistry({a.id: (agent if a.id == agent.id else a) for a in pack_registry.agents})
    security = pack_registry.config.security
    approver = Approver(approve=False)
    guard = ToolGuard(agent_id=agent.id, policy=SecurityPolicy(security, agents), security=security,
                      redactor=Redactor(security), workspace=ws, artifact_dir=tmp_path / "out",
                      write_mode=resolve_write_mode(pack_registry.config, agent), approver=approver,
                      protected_roots=protected_roots(pack_registry.config), shell_mode=SANDBOXED)
    assert verdict(call(guard, "mcp__db__query", {"sql": "x"})) == "deny"
    assert [r.rules for r in approver.requests] == [("confirm-writes",)]


def test_workspace_write_mode_is_unchanged(pack_registry, ws, tmp_path) -> None:
    guard = guard_for(pack_registry, ws, tmp_path, WRITER)
    for command in ("pytest", "make", "cp app/models.py app/c.py", "python manage.py makemigrations"):
        assert verdict(bash(guard, command)) == "allow", command


def test_mode_without_capability_entry_fails_closed(pack_registry, ws, tmp_path) -> None:
    from dataclasses import replace
    security = replace(pack_registry.config.security, write_modes={})
    guard = ToolGuard(agent_id=WRITER, policy=SecurityPolicy(security, pack_registry.agents), security=security,
                      redactor=Redactor(security), workspace=ws, artifact_dir=tmp_path / "out",
                      write_mode=AgentWriteMode(WRITER, "workspace-write"), shell_mode=SANDBOXED)
    assert verdict(bash(guard, "make")) == "deny" and verdict(bash(guard, "git status")) == "allow"


# ====================================================================== standalone plugin (its own guard)


def _standalone():
    path = HARNESS_HOME / "hooks" / "django_pack_guard.py"
    spec = importlib.util.spec_from_file_location("django_pack_guard_sec06", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STANDALONE = _standalone()
POLICY = json.loads((HARNESS_HOME / "hooks" / "security-policy.json").read_text(encoding="utf-8"))


def standalone(agent_id: str, tool: str, tool_input: dict, cwd: Path, policy: dict | None = None) -> str:
    result = STANDALONE.handle("pre", {"agent_type": f"django-engineering:{agent_id}", "tool_name": tool,
                                       "cwd": str(cwd), "tool_input": tool_input}, policy or POLICY, HARNESS_HOME)
    return "none" if result is None else result["hookSpecificOutput"]["permissionDecision"]


def test_standalone_policy_carries_the_capability_table() -> None:
    security = read_yaml(HARNESS_HOME / "config" / "security-config.yaml")
    assert set(POLICY["write_modes"]) == set(security["write_modes"])
    for mode, spec in security["write_modes"].items():
        assert POLICY["write_modes"][mode]["allow"] == spec["allow"]
        assert POLICY["write_modes"][mode]["otherwise"] == spec["otherwise"]


@pytest.mark.parametrize("agent_id", ["django-project-analyzer", "django-code-review-agent"])
@pytest.mark.parametrize("command", ["pytest", "make", "npm test", "sort -o a.py a.py", "uniq a b",
                                     "git -c alias.x='!rm a' x", "git st", "bash -c 'python s.py'", "unlink a.py",
                                     "find . -exec touch {} +", "env PATH=. ls", "python manage.py test"])
def test_standalone_read_only_refuses_non_read_effects(ws, agent_id, command) -> None:
    assert standalone(agent_id, "Bash", {"command": command}, ws) == "deny", command


@pytest.mark.parametrize("command", ["pytest", "make", "sort -o a.py a.py", "python manage.py test", "git st"])
def test_standalone_confirm_writes_asks(ws, command) -> None:
    assert standalone(CONFIRM, "Bash", {"command": command}, ws) == "ask", command


@pytest.mark.parametrize("command", ["git status", "grep -rn x app", "python manage.py check"])
def test_standalone_reads_stay_unprompted(ws, command) -> None:
    for agent_id in ("django-project-analyzer", CONFIRM):
        assert standalone(agent_id, "Bash", {"command": command}, ws) == "none", (agent_id, command)


def test_standalone_unknown_tool_follows_the_mode(ws) -> None:
    policy = json.loads(json.dumps(POLICY))
    for agent_id in ("django-project-analyzer", CONFIRM):
        policy["agents"][agent_id]["tools"].append("mcp__db__query")
    assert standalone("django-project-analyzer", "mcp__db__query", {"sql": "x"}, ws, policy) == "deny"
    assert standalone(CONFIRM, "mcp__db__query", {"sql": "x"}, ws, policy) == "ask"


def test_standalone_policy_without_capabilities_fails_closed(tmp_path) -> None:
    broken = {k: v for k, v in POLICY.items() if k != "write_modes"}
    path = tmp_path / "security-policy.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(STANDALONE.GuardError, match="write_modes"):
        STANDALONE.load_policy(path)


def test_standalone_build_applies_the_stricter_harness_mode(tmp_path) -> None:
    root = tmp_path / "pack"
    for folder in ("config", "plugin_packaging", "agents", "skills", "rules"):
        shutil.copytree(HARNESS_HOME / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
    security_path = root / "config" / "security-config.yaml"
    security = read_yaml(security_path)
    security["tools"]["agents"].append({"agent_id": WRITER, "write_mode": "read-only"})
    security["tools"]["agents"].append({"agent_id": "django-project-analyzer", "write_mode": "workspace-write"})
    write_yaml(security_path, security)
    built = packaging.build_policy(root)
    assert built["agents"][WRITER]["mode"] == "read-only"                  # narrowed by the harness
    assert built["agents"]["django-project-analyzer"]["mode"] == "read-only"   # never widened
