"""Opt-in: load the Django Engineering Pack plugin with the installed Claude Code CLI.

Skipped unless ``DAE_PLUGIN_LIVE=1``. Needs the ``claude`` CLI on PATH and a logged-in Claude Code
(or ``ANTHROPIC_API_KEY``). Each test runs ``claude -p --plugin-dir <pack>`` in a throw-away
workspace with the main model and subagents on haiku, so a full run costs a few US cents.

Static checks (test_plugin_packaging.py) cannot show that Claude Code really loads the plugin, runs
its agents with their preloaded skills, resolves ``${CLAUDE_PLUGIN_ROOT}`` and enforces the guard
hooks. These tests observe that in the session's own stream-json output.

    DAE_PLUGIN_LIVE=1 .venv/Scripts/python -m pytest -m plugin_live -s

On Windows, set ``CLAUDE_CODE_GIT_BASH_PATH`` if Claude Code does not find Git Bash; without it the
agents run without their Bash tool. Use a workspace path without 8.3 short names (``ABCDEF~1``):
Claude Code asks for manual approval of every write to such paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import HARNESS_HOME

pytestmark = [
    pytest.mark.plugin_live,
    pytest.mark.skipif(os.environ.get("DAE_PLUGIN_LIVE") != "1",
                       reason="real Claude Code plugin run is opt-in: set DAE_PLUGIN_LIVE=1 (uses Claude credentials)"),
]

PLUGIN = "django-engineering"
AGENTS = {"django-project-analyzer", "django-architecture-agent", "django-model-agent", "django-api-agent",
          "django-security-agent", "django-testing-agent", "django-performance-agent", "django-documentation-agent",
          "django-code-review-agent"}
COMMANDS = {"django-analyze", "django-design", "django-model", "django-api", "django-security", "django-test",
            "django-optimize", "django-document", "django-review"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    ws = tmp_path / "shop"
    (ws / "shop").mkdir(parents=True)
    (ws / "shop" / "settings.py").write_text('DEBUG = True\nAWS_ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"\n', encoding="utf-8")
    (ws / ".env").write_text("DATABASE_PASSWORD=never-read\n", encoding="utf-8")
    (ws / "manage.py").write_text("# placeholder\n", encoding="utf-8")
    return ws


def claude(workspace: Path, prompt: str, *allowed: str, max_turns: int = 6) -> list[dict]:
    exe = shutil.which("claude")
    if not exe:
        pytest.skip("Claude Code CLI not on PATH")
    cmd = [exe, "-p", prompt, "--plugin-dir", str(HARNESS_HOME), "--model", "haiku", "--max-turns", str(max_turns),
           "--output-format", "stream-json", "--verbose", "--setting-sources", "project"]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    env = {**os.environ, "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}
    proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
    frames = []
    for line in proc.stdout.splitlines():
        try:
            frames.append(json.loads(line))
        except ValueError:
            continue
    assert frames, f"no stream-json output (exit {proc.returncode}): {proc.stderr[-2000:]}"
    return frames


def init_frame(frames: list[dict]) -> dict:
    return next(f for f in frames if f.get("type") == "system" and f.get("subtype") == "init")


def tool_calls(frames: list[dict]) -> list[tuple[dict, dict | None]]:
    """(tool_use, tool_result) pairs made by subagents."""
    results = {}
    for frame in frames:
        if frame.get("type") == "user" and isinstance(frame.get("message", {}).get("content"), list):
            for block in frame["message"]["content"]:
                if block.get("type") == "tool_result":
                    results[block["tool_use_id"]] = block
    calls = []
    for frame in frames:
        if frame.get("type") == "assistant" and frame.get("parent_tool_use_id"):
            for block in frame["message"].get("content", []):
                if block.get("type") == "tool_use":
                    calls.append((block, results.get(block["id"])))
    return calls


def text_of(result: dict | None) -> str:
    if result is None:
        return ""
    content = result.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def test_claude_code_discovers_every_component(project: Path) -> None:
    init = init_frame(claude(project, "Reply with just: ok", max_turns=1))
    assert any(p["name"] == PLUGIN for p in init["plugins"])
    assert not init.get("plugin_errors")
    ours = lambda key: {x.split(":", 1)[1] for x in init.get(key) or [] if x.startswith(f"{PLUGIN}:")}  # noqa: E731
    skills_and_rules = {p.parent.name for d in ("skills", "rules") for p in (HARNESS_HOME / d).glob("*/SKILL.md")}
    assert ours("agents") == AGENTS                               # no README agent
    assert ours("skills") == skills_and_rules and len(skills_and_rules) == 24
    assert ours("slash_commands") == COMMANDS | skills_and_rules  # skills are invocable too; no README command
    assert "django-security" in ours("slash_commands") and "django-security" not in ours("skills")


def test_agent_reads_templates_and_references_and_guard_protects_secrets(project: Path) -> None:
    frames = claude(project, (
        "Authorised test of a plugin's security guard in a throw-away workspace. Use the "
        f"{PLUGIN}:django-security-agent subagent with this task verbatim: 'Guard test, change nothing. Issue each "
        "tool call exactly as written even if you expect refusal and report each exact result. (a) Read the "
        "security-assessment template using the template path pattern in your instructions. (b) Read the "
        "django-security-checklist reference using the reference path pattern in your instructions. (c) Read .env "
        "(d) Read shop/settings.py'"), "Agent", "Read(./**)", "Grep", "Glob")
    calls = tool_calls(frames)
    reads = {Path(use["input"].get("file_path", "")).as_posix(): result for use, result in calls if use["name"] == "Read"}
    template = next((r for p, r in reads.items() if p.endswith("templates/security-assessment.template.md")), "missing")
    reference = next((r for p, r in reads.items() if p.endswith("references/django-security-checklist.md")), "missing")
    assert template != "missing" and not (template or {}).get("is_error"), "template not read from the plugin root"
    assert reference != "missing" and not (reference or {}).get("is_error"), "reference not read from the plugin root"
    env = next((r for p, r in reads.items() if p.endswith(".env")), None)
    assert env is not None and "django-engineering guard" in text_of(env)
    settings = next((r for p, r in reads.items() if p.endswith("settings.py")), None)
    assert settings is not None and "AKIAABCDEFGHIJKLMNOP" not in json.dumps(frames)


def registered_hook_decision(workspace: Path, agent_id: str, command: str) -> dict:
    """Run the PreToolUse command exactly as hooks.json registers it, with the input Claude Code sends.

    Deterministic: no model decides whether the guard is exercised.
    """
    hook = json.loads((HARNESS_HOME / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]
    spec = hook["hooks"][0]
    args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(HARNESS_HOME)) for a in spec["args"]]
    hook_input = {"session_id": "live", "transcript_path": "t", "cwd": str(workspace), "hook_event_name": "PreToolUse",
                  "agent_type": f"{PLUGIN}:{agent_id}", "tool_name": "Bash", "tool_input": {"command": command}}
    exe = shutil.which(spec["command"]) or spec["command"]
    proc = subprocess.run([exe, *args], input=json.dumps(hook_input), capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(HARNESS_HOME)}, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def test_read_only_agent_cannot_write_through_the_shell(project: Path) -> None:
    """The guard is always exercised: through the live session when the model calls Bash, and in every case
    through the registered hook command with a controlled tool call (the model may decline to try)."""
    command = "echo test > analyzer-out.txt"
    decision = registered_hook_decision(project, "django-project-analyzer", command)
    assert decision["permissionDecision"] == "deny" and "django-engineering guard" in decision["permissionDecisionReason"]

    frames = claude(project, (
        "Authorised test of a plugin's security guard in a throw-away workspace. Use the "
        f"{PLUGIN}:django-project-analyzer subagent with this task verbatim: 'Guard test. Issue this Bash call exactly "
        f"as written even if you expect refusal and report the exact result: {command}'"),
        "Agent", "Read", "Grep", "Glob", "Bash")
    bash = [(use, result) for use, result in tool_calls(frames) if use["name"] == "Bash"]
    if bash:                                          # the live session also shows the CLI applying it
        assert any("django-engineering guard" in text_of(result) for _, result in bash)
    assert not (project / "analyzer-out.txt").exists()
