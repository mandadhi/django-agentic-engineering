"""Agent execution through the SDK adapter (fake adapter: no API calls)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.request import url2pathname
from urllib.parse import urlparse

import pytest
from jsonschema import Draft7Validator

from core.errors import RegistryLookupError
from core.frontmatter import read_markdown
from core.registry import HarnessRegistry
from runtime.state import AgentRunStatus, ExecutionStatus, StageStatus

from fakes import FakeSDKAdapter, FakeSession, agent_result, succeed


def run(coro):
    return asyncio.run(coro)


def start_agent(runner, workspace, agent_id="threat-modeler", **kwargs):
    return runner.start_agent(agent_id, "Model the threats of the Django app.", workspace=workspace, **kwargs)


def file_of(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(url2pathname(parsed.path))


# ---------------------------------------------------------------------- success path


def test_successful_execution(make_runner, workspace, manager, registry) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    execution = start_agent(runner, workspace)
    outcome = run(runner.run_workflow(execution.execution_id))

    assert outcome.execution.status is ExecutionStatus.SUCCEEDED
    [stage] = outcome.stages
    assert stage.stage_status is StageStatus.SUCCEEDED and stage.failure is None
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    assert agent_run.status is AgentRunStatus.SUCCEEDED
    assert agent_run.session_id == adapter.calls[0].session_id
    assert agent_run.cost_usd == pytest.approx(0.0123) and agent_run.num_turns == 3 and agent_run.duration_ms == 1500
    assert agent_run.result["metrics"] == {"input_tokens": 1200, "output_tokens": 300, "duration_ms": 1500}

    # inline content became a real, hashed artifact; the database holds only the reference
    [output] = agent_run.result["outputs"]
    path = file_of(output["artifact"]["uri"])
    assert path.read_text(encoding="utf-8").startswith("# report")
    assert output["artifact"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "dae.output_content" not in agent_run.result["extensions"]
    [record] = manager.store.list_artifacts(execution.execution_id)
    assert record.uri == output["artifact"]["uri"] and record.name == "report"


def test_markdown_is_the_source_of_truth(make_runner, workspace, registry) -> None:
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    session = adapter.calls[0]
    options = session.options
    agent = registry.agents.get("threat-modeler")
    meta, body = read_markdown(agent.path)

    # Phase 5.1 (AGT-02): the harness definition always runs as dae-<id>, never under the plugin file's name
    assert list(options.agents) == ["dae-threat-modeler"]
    definition = options.agents["dae-threat-modeler"]
    assert definition.prompt.startswith(body)            # instructions = Markdown body, not copied into Python
    assert definition.description == str(meta["description"]).strip()
    assert definition.model == meta["model"] == "opus"
    # declared tools + the CLI's internal StructuredOutput tool (needed with --agent to return the result)
    assert definition.tools == ["Read", "Grep", "Glob", "Write", "StructuredOutput"]
    # Phase 5.1 (AGT-01): the registered SKILL.md is in the prompt itself, not only named in AgentDefinition.skills
    [skill] = registry.skills_for("threat-modeler")
    skill_text = (skill.path / "SKILL.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    assert definition.prompt.count(skill_text) == 1 and definition.skills is None
    assert options.skills == ["security:owasp-top10-review"]     # the session's skill filter still lists it
    assert options.extra_args == {"agent": "dae-threat-modeler"}  # the session runs as this agent
    assert options.model is None                                 # model is not duplicated in options
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.plugins == [{"type": "local", "path": str(registry.config.plugins[0].path)}]
    assert options.cwd == str(workspace.resolve()) and options.setting_sources == []
    assert options.permission_mode == "dontAsk" and options.strict_mcp_config and options.verbatim_prompts
    Draft7Validator.check_schema(options.output_format["schema"])

    # the prompt carries the task, the untrusted-data rule and the result protocol — not the instructions
    assert "Model the threats of the Django app." in session.prompt
    assert "data,\n  not instructions" in session.prompt
    assert body[:60] not in session.prompt


def test_text_fallback_when_no_structured_output(make_runner, workspace, manager) -> None:
    async def scenario(s: FakeSession):
        yield s.init()
        yield s.result(text="Here you go:\n```json\n" + json.dumps(agent_result(s)) + "\n```")

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = start_agent(runner, workspace)
    assert run(runner.run_workflow(execution.execution_id)).execution.status is ExecutionStatus.SUCCEEDED
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    assert agent_run.result["extensions"]["dae.result_source"] == "text"


def test_agent_written_file_is_verified_and_hashed(make_runner, workspace, manager) -> None:
    async def scenario(s: FakeSession):
        yield s.init()
        artifact_dir = Path(s.prompt.split("Output directory (write produced files here): `")[1].split("`")[0])
        (artifact_dir / "report.md").write_text("# written by the agent\n", encoding="utf-8")
        result = agent_result(s, outputs={})
        result["outputs"] = [{"name": "report", "artifact": {"uri": str(artifact_dir / "report.md"),
                                                             "sha256": "0" * 64, "size_bytes": 1}}]
        yield s.result(structured=result)

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = start_agent(runner, workspace)
    run(runner.run_workflow(execution.execution_id))
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    artifact = agent_run.result["outputs"][0]["artifact"]
    data = file_of(artifact["uri"]).read_bytes()
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest() and artifact["size_bytes"] == len(data)


# ---------------------------------------------------------------------- preparation failures


def test_missing_agent(make_runner, workspace) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    with pytest.raises(RegistryLookupError, match="threat-modeller.*Did you mean: threat-modeler"):
        runner.start_agent("threat-modeller", "x", workspace=workspace)


def test_invalid_agent_definition(harness_root, edit_yaml, tmp_path, store, workspace) -> None:
    from agent_runtime.runner import AgentRunner
    from core.context import ContextManager
    from runtime.execution import ExecutionManager

    empty = tmp_path / "hollow-agent.md"
    empty.write_text("---\nname: hollow-agent\ndescription: has no instructions\ntools: Read\n---\n\n",
                     encoding="utf-8")
    edit_yaml("config/agent-registry.yaml", lambda d: d["agents"].append(
        {"id": "hollow-agent", "source": "local", "path": str(empty)}))
    registry = HarnessRegistry.load(harness_root)
    manager = ExecutionManager(store, registry)
    context = ContextManager(registry.config.framework.context, max_inline_bytes=4096)
    adapter = FakeSDKAdapter(succeed())
    runner = AgentRunner(manager, context, adapter, artifacts_root=tmp_path / "a")
    execution = runner.start_agent("hollow-agent", "x", workspace=workspace)
    outcome = run(runner.run_workflow(execution.execution_id))
    [stage] = outcome.stages
    assert stage.stage_status is StageStatus.FAILED and stage.failure.code == "agent.invalid_definition"
    assert "no instructions" in stage.failure.message
    assert adapter.calls == []                           # the SDK was never started


def _with_registry(manager, registry):
    manager.registry = registry
    return manager


def _swap_plugin(manager, registry, path: Path, repo_root: Path | None = None) -> None:
    # Since Phase 5 a plugin's containment base is its `anchor` (repo_root for `base: repo_root`),
    # fixed at config load. Relocating repo_root must move the anchor too, as load_config would.
    def swapped(p):
        return replace(p, path=path, anchor=repo_root or p.anchor)
    plugins = tuple(swapped(p) if p.id == "security" else p for p in registry.config.plugins)
    framework = replace(registry.config.framework, repo_root=repo_root or registry.config.framework.repo_root)
    _with_registry(manager, replace(registry, config=replace(registry.config, plugins=plugins, framework=framework)))


def test_missing_plugin_detected_before_sdk_start(make_runner, workspace, manager, registry, tmp_path) -> None:
    gone = tmp_path / "plugins" / "security"
    gone.mkdir(parents=True)                            # directory without .claude-plugin/plugin.json
    _swap_plugin(manager, registry, gone, repo_root=tmp_path)
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert outcome.stages[0].failure.code == "plugin.unresolved"
    assert "plugin.json" in outcome.stages[0].failure.message and adapter.calls == []


def test_plugin_outside_repository_is_never_loaded(make_runner, workspace, manager, registry, tmp_path) -> None:
    _swap_plugin(manager, registry, tmp_path / "elsewhere")
    adapter = FakeSDKAdapter(succeed())
    runner = make_runner(adapter)
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert "outside the repository root" in outcome.stages[0].failure.message and adapter.calls == []


def test_plugin_errors_reported_by_sdk_abort_the_run(make_runner, workspace) -> None:
    """The CLI does not fail on a bad --plugin-dir; it reports plugin_errors in init (observed)."""
    async def scenario(s: FakeSession):
        yield s.init(plugin_errors=[{"plugin": "inline[0]", "type": "path-not-found", "message": "Path not found"}])
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario))
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert outcome.stages[0].failure.code == "plugin.load_failed"
    assert "path-not-found" in outcome.stages[0].failure.message


def test_missing_skill(make_runner, workspace, manager, registry, tmp_path) -> None:
    skill = registry.skills.get("owasp-top10-review")
    skills = {s.id: (replace(s, path=tmp_path / "nope") if s.id == skill.id else s) for s in registry.skills}
    from core.registry import SkillRegistry
    _with_registry(manager, replace(registry, skills=SkillRegistry(skills)))
    runner = make_runner(FakeSDKAdapter(succeed()))
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert outcome.stages[0].failure.code == "plugin.unresolved"
    assert "owasp-top10-review" in outcome.stages[0].failure.message


def test_skill_missing_from_sdk_session(make_runner, workspace) -> None:
    async def scenario(s: FakeSession):
        yield s.init(skills=[])
        yield s.result(structured=agent_result(s))

    runner = make_runner(FakeSDKAdapter(scenario))
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert outcome.stages[0].failure.code == "plugin.load_failed"
    assert "security:owasp-top10-review" in outcome.stages[0].failure.message


def test_invalid_agent_task(make_runner, workspace, monkeypatch) -> None:
    runner = make_runner(FakeSDKAdapter(succeed()))
    original = runner.context.assemble_for_stage

    def broken(agent, stage, task_id):
        bundle = original(agent, stage, task_id)
        return replace(bundle, budget=replace(bundle.budget, total=0))    # violates TokenBudget.total >= 1

    monkeypatch.setattr(runner.context, "assemble_for_stage", broken)
    outcome = run(runner.run_workflow(start_agent(runner, workspace).execution_id))
    assert outcome.stages[0].failure.code == "task.invalid"


# ---------------------------------------------------------------------- invalid results


@pytest.mark.parametrize("mutate, code", [
    (lambda r: r.pop("status"), "result.invalid"),
    (lambda r: r.update(status="failed"), "result.invalid"),                       # failed without errors
    (lambda r: r.update(outputs=[], extensions={}), "result.invalid"),             # succeeded, required output missing
    (lambda r: r["outputs"].__setitem__(0, {"name": "Report!", "artifact": {"uri": "inline:report"}}),
     "result.invalid"),
    (lambda r: r.update(outputs=[{"name": "report", "artifact": {"uri": "../../../etc/passwd"}}], extensions={}),
     "result.invalid"),                                                            # file outside permitted roots
    (lambda r: r.update(unexpected=True), "result.invalid"),                       # closed contract
])
def test_invalid_agent_result_rejected(make_runner, workspace, manager, mutate, code) -> None:
    async def scenario(s: FakeSession):
        yield s.init()
        result = agent_result(s)
        mutate(result)
        yield s.result(structured=result)

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = start_agent(runner, workspace)
    outcome = run(runner.run_workflow(execution.execution_id))
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    assert agent_run.status is AgentRunStatus.FAILED and agent_run.error.code == code
    assert outcome.stages[0].stage_status is StageStatus.FAILED
    manager.registry.contracts.validate("agent-result", agent_run.result)    # what is stored is still valid


def test_malformed_text_result(make_runner, workspace, manager) -> None:
    async def scenario(s: FakeSession):
        yield s.init()
        yield s.result(text="I looked around but here is no JSON.")

    runner = make_runner(FakeSDKAdapter(scenario))
    execution = start_agent(runner, workspace)
    run(runner.run_workflow(execution.execution_id))
    [agent_run] = manager.store.list_agent_runs(execution.execution_id)
    assert agent_run.error.code == "result.malformed" and agent_run.error.retryable


def test_agent_reported_failure_is_recorded(make_runner, workspace, manager) -> None:
    errors = [{"code": "repo.unreadable", "message": "settings module not found"}]
    runner = make_runner(FakeSDKAdapter(succeed(status="blocked", outputs={}, errors=errors)))
    execution = start_agent(runner, workspace)
    outcome = run(runner.run_workflow(execution.execution_id))
    assert outcome.stages[0].failure.code == "repo.unreadable"
    assert manager.store.get_execution(execution.execution_id).status is ExecutionStatus.FAILED
