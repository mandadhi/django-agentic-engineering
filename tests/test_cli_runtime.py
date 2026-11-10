"""CLI commands run through the same registry / ExecutionManager / AgentRunner / security path."""

from __future__ import annotations

import json

import pytest

from core.cli import main

from fakes import FakeSDKAdapter, succeed


@pytest.fixture
def cli(harness_root, tmp_path, capsys):
    db = tmp_path / "cli.db"

    def _run(*argv: str, adapter=None) -> tuple[int, str, str]:
        args = ["--root", str(harness_root), *argv]
        if argv[0] in ("execution",) or argv[:2] == ("agent", "run"):
            args += ["--db", str(db)]
        code = main(args, adapter=adapter)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def test_agent_list_and_inspect(cli) -> None:
    code, out, _ = cli("agent", "list")
    assert code == 0 and "security-auditor" in out and "tools=Read,Grep,Glob,Bash" in out
    code, out, _ = cli("agent", "inspect", "security-sast-engineer", "--json")
    doc = json.loads(out)
    assert doc["effective_tools"] == ["Read", "Grep", "Glob", "Bash", "Write"]     # Edit removed by security-config
    assert "Edit" in doc["denied_tools"] and doc["model"] == "sonnet"
    assert doc["skills"] == ["security:security-scan-gates", "security:owasp-top10-review"]


def test_unknown_agent_exit_code(cli, workspace) -> None:
    code, _, err = cli("agent", "inspect", "security-auditr")
    assert code == 4 and "Did you mean: security-auditor" in err
    code, _, err = cli("agent", "run", "nope", "--objective", "x", "--workspace", str(workspace),
                       adapter=FakeSDKAdapter(succeed()))
    assert code == 4


def test_agent_run_and_status(cli, workspace) -> None:
    adapter = FakeSDKAdapter(succeed())
    code, out, _ = cli("agent", "run", "threat-modeler", "--objective", "Model threats.", "--workspace",
                       str(workspace), "--output", "threat-model:report", adapter=adapter)
    assert code == 0 and "succeeded" in out
    eid = out.split("started ")[1].split()[0]
    code, out, _ = cli("execution", "status", eid, "--json")
    doc = json.loads(out)
    assert doc["status"] == "succeeded" and doc["runs"][0]["session_id"] == adapter.calls[0].session_id
    assert doc["runs"][0]["outputs"] == ["threat-model"] and doc["total_cost_usd"] == pytest.approx(0.0123)


def test_execution_lifecycle_with_approval(cli, workspace) -> None:
    adapter = FakeSDKAdapter(succeed())
    code, _, err = cli("execution", "start", "security-review", "--workspace", str(workspace), adapter=adapter)
    assert code == 4 and "'repository' is a required property" in err

    code, out, _ = cli("execution", "start", "security-review", "--workspace", str(workspace),
                       "--input", f"repository={workspace.as_posix()}", adapter=adapter)
    assert code == 3 and "awaiting approval: audit" in out                    # waiting for a human
    eid = out.split("started ")[1].split()[0]
    assert cli("execution", "approve", eid, "audit", "--approver", "lead")[0] == 0
    code, out, _ = cli("execution", "status", eid)
    assert code == 0 and "succeeded" in out.splitlines()[0]
    code, out, _ = cli("execution", "list")
    assert eid in out


def test_execution_cancel_and_resume_refused(cli, workspace) -> None:
    code, out, _ = cli("execution", "start", "security-review", "--workspace", str(workspace),
                       "--input", "repository=x", "--no-run")
    eid = out.split("started ")[1].split()[0]
    assert cli("execution", "cancel", eid)[0] == 0
    code, _, err = cli("execution", "resume", eid, adapter=FakeSDKAdapter(succeed()))
    assert code == 1 and "cannot be resumed" in err
