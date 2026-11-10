"""Cross-file reference validation.

Runs after every file has passed its own schema. Returns *all* issues so a single
run of ``python -m core.cli validate`` shows every broken link:

* agent → skill ids; agent files / skill directories exist on disk and their
  frontmatter ``name`` matches the registry id;
* workflow stage → agent id (and the agent is enabled), stage → stage dependencies,
  duplicate stage ids, dependency cycles;
* security overrides → agent ids;
* evaluation targets → agent / workflow / stage ids, and → criterion ids.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .config import HarnessConfig, PluginSpec
from .errors import ValidationIssue, suggest
from .frontmatter import FrontmatterError, read_frontmatter
from .plugin_policy import PluginPolicyError, load_agent_policy
from .workflows import WorkflowCycleError, WorkflowDefinition


def _unknown(kind: str, value: str, known: set[str]) -> str:
    hint = suggest(value, known)
    return f"unknown {kind} '{value}'" + (f" (did you mean: {', '.join(hint)}?)" if hint else "")


def _check_plugin_member(config: HarnessConfig, kind: str, item_id: str, source: str, plugin_id: str | None,
                         path: Path, rel_path: str, label: str, where: str) -> list[ValidationIssue]:
    """An entry that names its plugin (``plugin: <id>``): registered, source plugin, path inside it."""
    plugins = {p.id: p for p in config.plugins}
    if plugin_id is None or plugin_id not in plugins:
        if plugin_id is not None:
            return [ValidationIssue(label, f"{where}/plugin", f"{kind} '{item_id}': "
                                    f"{_unknown('plugin', plugin_id, set(plugins))} (register it in agent-registry "
                                    "`plugins`)", "unregistered-plugin")]
        return []
    issues = []
    if source != "plugin":
        issues.append(ValidationIssue(label, f"{where}/source", f"{kind} '{item_id}': names plugin '{plugin_id}' "
                                      f"but source is '{source}' (must be 'plugin')", "schema"))
    root = plugins[plugin_id].path
    if path != root and root not in path.parents:
        issues.append(ValidationIssue(label, f"{where}/path", f"{kind} '{item_id}': {rel_path} leaves plugin "
                                      f"'{plugin_id}'", "outside-plugin"))
    return issues


def _check_registries(config: HarnessConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    agents_label, skills_label = config.label("agent_registry"), config.label("skill_registry")
    skill_ids = {s.id for s in config.skills}

    for index, agent in enumerate(config.agents):
        # No-op for entries located by containing directory; checks entries that name `plugin:`.
        issues += _check_plugin_member(config, "agent", agent.id, agent.source, agent.plugin, agent.path,
                                       agent.rel_path, agents_label, f"agents/{index}")
        if agent.source == "plugin" and agent.plugin is None:
            issues.append(ValidationIssue(agents_label, f"agents/{index}/path",
                                          f"agent '{agent.id}': {agent.rel_path} is not inside a registered plugin "
                                          "(add its plugin to agent-registry `plugins`)", "unregistered-plugin"))
        if not agent.path.is_file():
            issues.append(ValidationIssue(agents_label, f"agents/{index}/path",
                                          f"agent '{agent.id}': file not found: {agent.rel_path}", "missing-path"))
        else:
            issues += _check_definition(agent.id, agent.path, agents_label, f"agents/{index}", "agent")
        for s_index, skill_id in enumerate(agent.skills):
            if skill_id not in skill_ids:
                issues.append(ValidationIssue(agents_label, f"agents/{index}/skills/{s_index}",
                                              f"agent '{agent.id}': {_unknown('skill', skill_id, skill_ids)}",
                                              "unknown-skill"))

    seen_paths: dict[Path, str] = {}
    for index, skill in enumerate(config.skills):
        issues += _check_plugin_member(config, "skill", skill.id, skill.source, skill.plugin, skill.path,
                                       skill.rel_path, skills_label, f"skills/{index}")
        if skill.path in seen_paths:
            issues.append(ValidationIssue(skills_label, f"skills/{index}/path",
                                          f"skill '{skill.id}': {skill.rel_path} is already registered as "
                                          f"'{seen_paths[skill.path]}' (the same content would load twice)",
                                          "duplicate-path"))
        seen_paths.setdefault(skill.path, skill.id)
        if skill.source == "plugin" and skill.plugin is None:
            issues.append(ValidationIssue(skills_label, f"skills/{index}/path",
                                          f"skill '{skill.id}': {skill.rel_path} is not inside a registered plugin "
                                          "(add its plugin to agent-registry `plugins`)", "unregistered-plugin"))
        if not (skill.path / "SKILL.md").is_file():
            issues.append(ValidationIssue(skills_label, f"skills/{index}/path",
                                          f"skill '{skill.id}': no SKILL.md in {skill.rel_path}", "missing-path"))
        else:
            issues += _check_definition(skill.id, skill.path / "SKILL.md", skills_label, f"skills/{index}", "skill")
    return issues


def _check_definition(item_id: str, path: Path, label: str, where: str, kind: str) -> list[ValidationIssue]:
    """The Markdown definition must have frontmatter whose ``name`` equals the registry id.

    Claude Code and the Agent SDK address agents/skills by that ``name`` (hooks report it
    as ``agent_type``), so a mismatch would break dispatch and per-agent policy.
    """
    try:
        meta = read_frontmatter(path)
    except (FrontmatterError, OSError) as exc:
        return [ValidationIssue(label, f"{where}/path", f"{kind} '{item_id}': {exc}", "invalid-frontmatter")]
    issues = []
    if meta.get("name") != item_id:
        issues.append(ValidationIssue(label, f"{where}/id",
                                      f"{kind} '{item_id}': frontmatter name is '{meta.get('name')}'; "
                                      "registry id must match it", "name-mismatch"))
    if not meta.get("description"):
        issues.append(ValidationIssue(label, f"{where}/path",
                                      f"{kind} '{item_id}': frontmatter has no description", "invalid-frontmatter"))
    return issues


def _check_plugins(config: HarnessConfig) -> list[ValidationIssue]:
    """Registered plugin directories exist inside their base (repo_root or the harness) and their
    manifest names match; read paths and agent policies are valid."""
    label = config.label("agent_registry")
    repo_root = config.framework.repo_root
    issues: list[ValidationIssue] = []
    for index, plugin in enumerate(config.plugins):
        where = f"plugins/{index}"
        anchor = plugin.anchor or repo_root
        if plugin.path != anchor and anchor not in plugin.path.parents:
            base = "paths.repo_root" if plugin.base == "repo_root" else "the harness directory"
            issues.append(ValidationIssue(label, f"{where}/path", f"plugin '{plugin.id}': {plugin.rel_path} is outside "
                                                                   f"{base}", "outside-repo"))
            continue
        if not plugin.path.is_dir():
            issues.append(ValidationIssue(label, f"{where}/path", f"plugin '{plugin.id}': directory not found: "
                                                                   f"{plugin.rel_path} (base: {plugin.base})",
                                          "missing-path"))
            continue
        if not plugin.manifest.is_file():
            issues.append(ValidationIssue(label, f"{where}/path", f"plugin '{plugin.id}': no .claude-plugin/plugin.json "
                                                                   f"in {plugin.rel_path}", "missing-path"))
            continue
        try:
            manifest = json.loads(plugin.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(ValidationIssue(label, f"{where}/path", f"plugin '{plugin.id}': unreadable plugin.json: {exc}",
                                          "invalid-plugin"))
            continue
        if not isinstance(manifest, dict) or manifest.get("name") != plugin.id:
            name = manifest.get("name") if isinstance(manifest, dict) else None
            issues.append(ValidationIssue(label, f"{where}/id", f"plugin '{plugin.id}': plugin.json name is '{name}'; "
                                                                 "registry id must match it", "name-mismatch"))
        for r_index, (rel, root) in enumerate(zip(plugin.read_paths, plugin.read_roots)):
            if root == plugin.path or plugin.path not in root.parents:
                issues.append(ValidationIssue(label, f"{where}/read_paths/{r_index}",
                                              f"plugin '{plugin.id}': read path '{rel}' must be a sub-folder of the "
                                              "plugin", "outside-plugin"))
            elif not root.is_dir():
                issues.append(ValidationIssue(label, f"{where}/read_paths/{r_index}",
                                              f"plugin '{plugin.id}': read path '{rel}' not found", "missing-path"))
        issues += _check_agent_policy(config, plugin, label, where)
    return issues


def _check_agent_policy(config: HarnessConfig, plugin: PluginSpec, label: str, where: str) -> list[ValidationIssue]:
    """The policy parses, and it covers exactly the plugin's registered agents (fail closed on gaps)."""
    if plugin.agent_policy is None:
        if not plugin.load_hooks:
            return [ValidationIssue(label, f"{where}/load_hooks", f"plugin '{plugin.id}': load_hooks false requires "
                                    "an agent_policy the harness enforces in place of the plugin's hooks", "schema")]
        return []
    try:
        policy = load_agent_policy(plugin)
    except PluginPolicyError as exc:
        return [ValidationIssue(label, f"{where}/agent_policy", str(exc), "invalid-plugin")]
    agents = {a.id for a in config.agents if a.plugin == plugin.id}
    issues = [ValidationIssue(label, f"{where}/agent_policy", f"plugin '{plugin.id}': agent '{agent_id}' has no "
                              f"write mode in {plugin.agent_policy}", "missing-policy")
              for agent_id in sorted(agents - set(policy.modes))]
    issues += [ValidationIssue(label, f"{where}/agent_policy", f"plugin '{plugin.id}': {plugin.agent_policy} names "
                               f"'{agent_id}', which is not a registered agent of this plugin", "unknown-agent")
               for agent_id in sorted(set(policy.modes) - agents)]
    return issues


def _check_inputs(workflow: WorkflowDefinition, label: str) -> list[ValidationIssue]:
    """The inputs schema is a valid JSON Schema and every stage input reference is declared."""
    issues: list[ValidationIssue] = []
    declared: set[str] = set()
    if workflow.inputs_schema is not None:
        try:
            Draft202012Validator.check_schema(workflow.inputs_schema)
        except SchemaError as exc:
            issues.append(ValidationIssue(label, "inputs", f"inputs is not a valid JSON Schema: {exc.message}",
                                          "invalid-inputs-schema"))
        declared = set((workflow.inputs_schema.get("properties") or {}).keys())
    for index, stage in enumerate(workflow.stages):
        for name in stage.required_inputs():
            if name not in declared:
                hint = suggest(name, declared)
                issues.append(ValidationIssue(
                    label, f"stages/{index}/inputs",
                    f"stage '{stage.id}': input '{name}' is not declared in the workflow inputs.properties"
                    + (f" (did you mean: {', '.join(hint)}?)" if hint else ""), "unknown-input"))
    return issues


def _check_workflow(workflow: WorkflowDefinition, label: str, agents: Mapping[str, bool]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = _check_inputs(workflow, label)
    stage_ids = set(workflow.stage_ids)
    agent_ids = set(agents)

    counts = Counter(workflow.stage_ids)
    for index, stage in enumerate(workflow.stages):
        where = f"stages/{index}"
        if counts[stage.id] > 1:
            issues.append(ValidationIssue(label, f"{where}/id", f"duplicate stage id '{stage.id}'", "duplicate-id"))
        if stage.agent_id not in agents:
            issues.append(ValidationIssue(label, f"{where}/agent_id",
                                          f"stage '{stage.id}': {_unknown('agent', stage.agent_id, agent_ids)}",
                                          "unknown-agent"))
        elif not agents[stage.agent_id]:
            issues.append(ValidationIssue(label, f"{where}/agent_id",
                                          f"stage '{stage.id}': agent '{stage.agent_id}' is disabled", "disabled-agent"))
        for d_index, dep in enumerate(stage.depends_on):
            if dep == stage.id:
                issues.append(ValidationIssue(label, f"{where}/depends_on/{d_index}",
                                              f"stage '{stage.id}' depends on itself", "cycle"))
            elif dep not in stage_ids:
                issues.append(ValidationIssue(label, f"{where}/depends_on/{d_index}",
                                              f"stage '{stage.id}': {_unknown('stage', dep, stage_ids)}",
                                              "unknown-stage"))
    if not any(i.code in {"cycle", "duplicate-id"} for i in issues):
        try:
            workflow.topological_order()
        except WorkflowCycleError as exc:
            issues.append(ValidationIssue(label, "stages", str(exc), "cycle"))
    return issues


#: Public entry point for checking one workflow (used for ad-hoc single-agent workflows).
check_workflow = _check_workflow


def _check_security(config: HarnessConfig, agent_ids: set[str]) -> list[ValidationIssue]:
    label = config.label("security")
    return [
        ValidationIssue(label, f"tools/agents/{index}/agent_id", _unknown("agent", o.agent_id, agent_ids), "unknown-agent")
        for index, o in enumerate(config.security.tool_overrides)
        if o.agent_id not in agent_ids
    ]


def _check_evaluation(config: HarnessConfig, agent_ids: set[str],
                      workflows: Mapping[str, WorkflowDefinition]) -> list[ValidationIssue]:
    label = config.label("evaluation")
    criteria = {c.id for c in config.evaluation.criteria}
    issues: list[ValidationIssue] = []
    for index, target in enumerate(config.evaluation.targets):
        where = f"targets/{index}"
        if target.type == "agent" and target.id not in agent_ids:
            issues.append(ValidationIssue(label, f"{where}/id", _unknown("agent", target.id, agent_ids), "unknown-agent"))
        elif target.type == "workflow" and target.id not in workflows:
            issues.append(ValidationIssue(label, f"{where}/id", _unknown("workflow", target.id, set(workflows)),
                                          "unknown-workflow"))
        elif target.type == "stage":
            workflow_id, _, stage_id = target.id.partition("/")
            if not stage_id:
                issues.append(ValidationIssue(label, f"{where}/id",
                                              f"stage target '{target.id}' must be '<workflow>/<stage>'", "schema"))
            elif workflow_id not in workflows:
                issues.append(ValidationIssue(label, f"{where}/id", _unknown("workflow", workflow_id, set(workflows)),
                                              "unknown-workflow"))
            elif stage_id not in workflows[workflow_id].stage_ids:
                known = set(workflows[workflow_id].stage_ids)
                issues.append(ValidationIssue(label, f"{where}/id", _unknown("stage", stage_id, known), "unknown-stage"))
        for c_index, criterion in enumerate(target.criteria):
            if criterion not in criteria:
                issues.append(ValidationIssue(label, f"{where}/criteria/{c_index}",
                                              _unknown("criterion", criterion, criteria), "unknown-criterion"))
    return issues


def validate_references(config: HarnessConfig, workflows: Mapping[str, WorkflowDefinition],
                        workflow_labels: Mapping[str, str] | None = None) -> list[ValidationIssue]:
    """Every broken cross-reference across configs and workflow definitions."""
    agents = {a.id: a.enabled for a in config.agents}
    agent_ids = set(agents)
    issues = _check_plugins(config) + _check_registries(config)
    for workflow_id, workflow in workflows.items():
        label = (workflow_labels or {}).get(workflow_id, f"workflow:{workflow_id}")
        issues += _check_workflow(workflow, label, agents)
    issues += _check_security(config, agent_ids)
    issues += _check_evaluation(config, agent_ids, workflows)
    return issues
