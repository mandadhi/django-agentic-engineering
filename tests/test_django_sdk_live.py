"""Opt-in (``DAE_SDK_INTEGRATION=1``): Django pack agents through the real Claude Agent SDK.

Needs Claude credentials the bundled CLI can use (``ANTHROPIC_API_KEY`` or a logged-in Claude
Code). Each run is bounded by ``max_turns``, ``max_budget_usd`` and a timeout. To keep the
cost small the agents run on ``haiku`` instead of their frontmatter model; everything else
(instructions, tools, skills, rules, templates, policy) is exactly what a normal run uses.

    DAE_SDK_INTEGRATION=1 .venv/Scripts/python -m pytest tests/test_django_sdk_live.py -s

What only a real session can show, and these tests check:

* the CLI loads the canonical pack (``init.plugins``) and exactly the agent's registered skills
  and rules (``init.skills``), with no plugin errors;
* the agent reads its template through the harness guard at the substituted plugin path;
* a read-only agent changes nothing; the documentation agent writes only documentation;
* with ``disableAllHooks`` (``load_hooks: false``) the plugin's command hooks do not run while the
  harness's SDK callback hooks still do: the behaviour the design relies on.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from agent_runtime.runner import AgentRunner
from agent_runtime.sdk_adapter import ClaudeSDKAdapter
from core.context import ContextManager
from runtime.execution import ExecutionManager
from runtime.state import AgentRunStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("DAE_SDK_INTEGRATION") != "1",
                       reason="real SDK run is opt-in: set DAE_SDK_INTEGRATION=1 (uses Claude credentials)"),
]

PLUGIN = "django-engineering"
FAKE_KEY = "AKIA" + "Z" * 16


class RecordingAdapter(ClaudeSDKAdapter):
    """The real adapter, keeping the init frame (the runner stores only a summary of it)."""

    def __init__(self) -> None:
        self.init: dict[str, Any] = {}

    def stream(self, prompt, options) -> AsyncIterator[Any]:
        async def gen():
            async for message in super(RecordingAdapter, self).stream(prompt, options):
                if getattr(message, "subtype", None) == "init" and hasattr(message, "data"):
                    self.init = dict(message.data)
                yield message
        return gen()


def django_project(root: Path) -> Path:
    (root / "shop").mkdir(parents=True)
    (root / "orders").mkdir()
    (root / "manage.py").write_text("import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shop.settings')\n",
                                    encoding="utf-8")
    (root / "shop" / "settings.py").write_text(
        "DEBUG = False\nINSTALLED_APPS = ['django.contrib.auth', 'orders']\n"
        f"AWS_ACCESS_KEY_ID = '{FAKE_KEY}'\n", encoding="utf-8")
    (root / "orders" / "models.py").write_text(
        "from django.db import models\n\nclass Order(models.Model):\n    total = models.DecimalField(max_digits=9, "
        "decimal_places=2)\n", encoding="utf-8")
    (root / "requirements.txt").write_text("Django>=4.2\n", encoding="utf-8")
    (root / ".env").write_text("DATABASE_PASSWORD=never-read-me\n", encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*"))
            if p.is_file()}


def live_run(pack_registry, store, tmp_path: Path, agent_id: str, objective: str, workspace: Path,
             expected_output: str):
    agent = replace(pack_registry.agents.get(agent_id), model="haiku")
    agents = type(pack_registry.agents)({a.id: (agent if a.id == agent_id else a) for a in pack_registry.agents})
    runtime = replace(pack_registry.config.framework.agent_runtime, max_turns=20, max_budget_usd=0.75)
    registry = replace(pack_registry, agents=agents, config=replace(
        pack_registry.config, framework=replace(pack_registry.config.framework, agent_runtime=runtime)))
    adapter = RecordingAdapter()
    context = ContextManager(registry.config.framework.context, max_inline_bytes=registry.config.security.max_inline_bytes)
    runner = AgentRunner(ExecutionManager(store, registry), context, adapter, artifacts_root=tmp_path / "artifacts",
                         timeout_override=420)
    execution = runner.start_agent(agent_id, objective, workspace=workspace,
                                   expected_outputs=[{"name": expected_output, "type": "report"}])
    outcome = asyncio.run(runner.run_workflow(execution.execution_id))
    [run] = store.list_agent_runs(execution.execution_id)
    events = [(e.event, e.data.get("tool"), e.data.get("summary"), e.message)
              for e in store.list_logs(execution.execution_id) if e.event.startswith(("tool.", "session."))]
    print(f"\n[{agent_id}] stage:", [(s.stage_id, s.stage_status.value, s.failure) for s in outcome.stages])
    print("run:", run.status, run.session_id, f"cost=${run.cost_usd}", "turns:", run.num_turns)
    print("init plugins:", [p.get("name") for p in adapter.init.get("plugins", []) if p.get("path") != "builtin"],
          "plugin_errors:", adapter.init.get("plugin_errors"))
    print("init skills:", adapter.init.get("skills"))
    print("init tools:", adapter.init.get("tools"), "| version:", adapter.init.get("claude_code_version"))
    print("tool events:", *events, sep="\n  ")
    print("summary:", (run.result or {}).get("summary"))
    return registry, run, adapter.init, events


def expected_skills(registry, agent_id: str) -> list[str]:
    return [f"{PLUGIN}:{s}" for s in registry.agents.get(agent_id).skills]


def test_live_read_only_analyzer(pack_registry, store, tmp_path: Path) -> None:
    workspace = django_project(tmp_path / "shop")
    before = snapshot(workspace)
    registry, run, init, events = live_run(
        pack_registry, store, tmp_path, "django-project-analyzer",
        "First read your project-analysis output template (its absolute path is in your instructions, under "
        "Templates and references) and follow its structure. Then profile this small Django project briefly. "
        "Keep the profile short.", workspace, "project-profile")

    assert run.session_id and run.status is AgentRunStatus.SUCCEEDED, run.error
    assert not init.get("plugin_errors")
    assert run.sdk_metadata["plugins"] == [PLUGIN]                         # the canonical pack, from init
    assert "dae-django-project-analyzer" in init.get("agents", [])         # the harness definition runs
    assert "haiku" in str(init.get("model", "")).lower()                   # ... so its model override applies
    assert set(expected_skills(registry, "django-project-analyzer")) <= set(init.get("skills") or [])
    assert set(init.get("tools") or []) <= {"Read", "Grep", "Glob", "Bash", "StructuredOutput"}
    template = "templates/project-analysis.template.md"
    assert any(e == "tool.allowed" and t == "Read" and template in (s or "").replace("\\", "/")
               for e, t, s, _ in events), "the template was not read through the harness guard"
    assert snapshot(workspace) == before                                   # read-only: nothing changed
    assert FAKE_KEY not in json.dumps(run.result) and "never-read-me" not in json.dumps(run.result)


def test_live_documentation_agent_uses_skill_and_template(pack_registry, store, tmp_path: Path) -> None:
    workspace = django_project(tmp_path / "shop")
    before = snapshot(workspace)
    registry, run, init, events = live_run(
        pack_registry, store, tmp_path, "django-documentation-agent",
        "Read your technical-documentation template (absolute path in your instructions), then write "
        "docs/overview.md: a short overview of this project's apps and models, following the template's structure. "
        "Do not change any other file.", workspace, "documentation-report")

    assert run.session_id and run.status is AgentRunStatus.SUCCEEDED, run.error
    assert not init.get("plugin_errors") and run.sdk_metadata["plugins"] == [PLUGIN]
    assert set(expected_skills(registry, "django-documentation-agent")) <= set(init.get("skills") or [])
    assert "Bash" not in (init.get("tools") or [])                          # no shell for this agent
    after = snapshot(workspace)
    changed = {k for k in after if before.get(k) != after[k]}
    assert "docs/overview.md" in changed, changed
    assert all(k.startswith("docs/") or k.endswith(".md") for k in changed), changed   # documentation only
    assert {k: v for k, v in after.items() if k in before} == before or changed <= {"docs/overview.md"}
    assert any(e == "tool.allowed" and t == "Read" and "templates/technical-documentation.template.md"
               in (s or "").replace("\\", "/") for e, t, s, _ in events)


def test_live_only_registered_rules_are_preloaded(pack_registry, store, tmp_path: Path) -> None:
    """``init.skills`` lists every discovered skill; only the registered ones are preloaded into the agent.

    The analyzer is registered with django-project-structure (STR-*) but not django-model-standards
    (MOD-*). Without tools it can quote STR-M1 from its preloaded rule, and not MOD-M1.
    """
    workspace = django_project(tmp_path / "shop")
    _, run, _, events = live_run(
        pack_registry, store, tmp_path, "django-project-analyzer",
        "Do not use any tools for this task and do not read any files. From the engineering rules already in your "
        "instructions or context, copy the first sentence of requirement STR-M1 exactly, and the first sentence of "
        "requirement MOD-M1 exactly. For a requirement whose text you were not given, write NOT-GIVEN instead of "
        "guessing. Put both answers in the summary as 'STR-M1: ...' and 'MOD-M1: ...'.", workspace, "answer")
    summary = (run.result or {}).get("summary", "")
    assert not [e for e in events if e[0].startswith("tool.") and e[1] not in ("StructuredOutput",)], events
    assert "one `manage.py` per deployable project" in summary.replace("\\`", "`") or \
        "one manage.py per deployable project" in summary, summary
    mod = summary.split("MOD-M1", 1)[1] if "MOD-M1" in summary else ""
    assert "NOT-GIVEN" in mod and "UniqueConstraint" not in mod, summary


def test_live_disable_all_hooks_keeps_sdk_hooks(tmp_path: Path, pack_registry) -> None:
    """Pins the CLI behaviour `load_hooks: false` relies on, with the harness's own settings builder."""
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher, ResultMessage, query

    from agent_runtime.options_factory import permission_settings

    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "hook-probe", "version": "0.0.1"}',
                                                          encoding="utf-8")
    (plugin / "hooks" / "mark.py").write_text(
        "import pathlib, sys\npathlib.Path(__file__).with_name('ran.txt').write_text('x')\n", encoding="utf-8")
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/mark.py"]}]}]}}),
        encoding="utf-8")
    target = tmp_path / "ws" / "hello.txt"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")

    async def run(disable: bool) -> list[str]:
        (plugin / "hooks" / "ran.txt").unlink(missing_ok=True)
        seen: list[str] = []

        async def pre(hook_input, tool_use_id, context):
            seen.append(hook_input.get("tool_name"))
            return {}
        settings = permission_settings(pack_registry.config.security, disable_plugin_hooks=disable)
        options = ClaudeAgentOptions(
            agents={"probe": AgentDefinition(description="probe", prompt="Do what the user asks.", tools=["Read"],
                                             model="haiku")},
            extra_args={"agent": "probe"}, system_prompt={"type": "preset", "preset": "claude_code"},
            tools=["Read"], allowed_tools=["Read"], permission_mode="dontAsk", setting_sources=[],
            settings=json.dumps(settings), plugins=[{"type": "local", "path": str(plugin)}], mcp_servers={},
            strict_mcp_config=True, hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre], on_failure="block")]},
            cwd=str(target.parent), max_turns=3, max_budget_usd=0.05)
        async for message in query(prompt="Read hello.txt with the Read tool and reply with its content.",
                                   options=options):
            if isinstance(message, ResultMessage):
                print(f"disable={disable}: cost ${message.total_cost_usd}")
        return seen

    seen_on = asyncio.run(run(disable=False))
    assert seen_on and (plugin / "hooks" / "ran.txt").exists()            # plugin hooks run by default
    seen_off = asyncio.run(run(disable=True))
    assert seen_off and not (plugin / "hooks" / "ran.txt").exists()       # suppressed; harness hook still ran
