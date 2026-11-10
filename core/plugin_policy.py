"""Per-agent write modes a plugin declares for its own agents (agent-registry ``agent_policy``).

A plugin may ship a ``PluginSecurityPolicy`` file that gives each of its agents a write mode;
the registry entry names it (for the Django Engineering Pack it is the same canonical file the
pack's standalone guard policy is generated from). The harness reads only what
restricts an agent and enforces it with its **own** PreToolUse guard
(:class:`agent_runtime.security_hooks.ToolGuard`); it never runs or trusts the plugin's hook.

Modes only take capabilities away; the agent's ``tools`` frontmatter, ``security-config.yaml``
and the workspace boundaries still apply on top:

* ``read-only``          no file writes; no shell redirection to a file; ``read_only_commands`` denied
* ``documentation-only`` file writes in the workspace only to ``documentation_paths``; no shell
* ``confirm-writes``     every change to the workspace (file write, state-changing shell command)
                         needs the harness approver; the default approver denies
* ``workspace-write``    the harness defaults (writes inside the workspace and artifact directory)

What each mode may do is defined by the harness, not by the plugin: security-config ``write_modes``
(capabilities ``read``/``write``/``delete``/``execute``/``network``/``unknown``, default deny, SEC-06).

security-config can also give any agent a write mode (``tools.agents[].write_mode``, SEC-05): the
reused Phase 2 reviewers have no plugin policy. :func:`resolve_write_mode` combines both sources;
the stricter mode wins, so plugin metadata can narrow the harness policy but never widen it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .config import AgentSpec, CommandRule, HarnessConfig, PluginSpec
from .errors import HarnessError

POLICY_KIND = "PluginSecurityPolicy"
WRITE_MODES = frozenset({"read-only", "documentation-only", "confirm-writes", "workspace-write"})
#: Strictest first. documentation-only ranks above confirm-writes: it never runs a shell and never
#: changes code, while confirm-writes may do both with approval.
MODE_ORDER = ("read-only", "documentation-only", "confirm-writes", "workspace-write")


def stricter(a: str, b: str) -> str:
    """The more restrictive of two write modes (an unknown mode counts as read-only: fail closed)."""
    rank = {mode: i for i, mode in enumerate(MODE_ORDER)}
    return min((a, b), key=lambda mode: rank.get(mode, -1)) if a in rank and b in rank else "read-only"


class PluginPolicyError(HarnessError):
    """A plugin's agent policy is missing, unreadable or invalid. Callers fail closed."""


@dataclass(frozen=True)
class AgentWriteMode:
    """What a plugin's policy says about one agent (enforced by ToolGuard)."""

    agent_id: str
    mode: str
    documentation_paths: tuple[str, ...] = ()
    read_only_commands: tuple[CommandRule, ...] = ()
    source: str = "plugin"            # plugin | security-config | security-config+plugin


@dataclass(frozen=True)
class PluginAgentPolicy:
    plugin: str
    modes: Mapping[str, str]
    documentation_paths: tuple[str, ...]
    read_only_commands: tuple[CommandRule, ...]

    def for_agent(self, agent_id: str) -> AgentWriteMode:
        mode = self.modes.get(agent_id)
        if mode is None:
            raise PluginPolicyError(f"plugin '{self.plugin}' policy has no write mode for agent '{agent_id}'")
        return AgentWriteMode(agent_id, mode, self.documentation_paths, self.read_only_commands)


def _rules(items: Any, label: str) -> tuple[CommandRule, ...]:
    if not isinstance(items, list):
        raise PluginPolicyError(f"{label}: read_only_commands must be a list")
    rules = []
    for index, rule in enumerate(items):
        if not isinstance(rule, Mapping) or not rule.get("id") or not rule.get("regex"):
            raise PluginPolicyError(f"{label}: read_only_commands[{index}] needs an id and a regex")
        try:
            re.compile(rule["regex"])
        except re.error as exc:
            raise PluginPolicyError(f"{label}: read_only_commands[{index}] ({rule['id']}): invalid regex: {exc}") from exc
        rules.append(CommandRule(str(rule["id"]), str(rule["regex"]), str(rule.get("description", ""))))
    return tuple(rules)


def load_agent_policy(plugin: PluginSpec) -> PluginAgentPolicy:
    """Parse and check ``plugin.agent_policy``. Raises :class:`PluginPolicyError` on any problem."""
    path: Path | None = plugin.agent_policy_path
    if path is None:
        raise PluginPolicyError(f"plugin '{plugin.id}' declares no agent_policy")
    label = f"plugin '{plugin.id}' agent_policy {plugin.agent_policy}"
    if path != plugin.path and plugin.path not in path.parents:
        raise PluginPolicyError(f"{label}: outside the plugin directory")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginPolicyError(f"{label}: file not found") from None
    except (OSError, yaml.YAMLError) as exc:
        raise PluginPolicyError(f"{label}: unreadable: {exc}") from exc
    if not isinstance(doc, Mapping) or doc.get("kind") != POLICY_KIND:
        raise PluginPolicyError(f"{label}: expected kind '{POLICY_KIND}'")
    if str(doc.get("schema_version")) != "1.0":
        raise PluginPolicyError(f"{label}: unsupported schema_version '{doc.get('schema_version')}'")
    if doc.get("plugin") != plugin.id:
        raise PluginPolicyError(f"{label}: policy is for plugin '{doc.get('plugin')}'")
    modes = doc.get("agents")
    if not isinstance(modes, Mapping) or not modes:
        raise PluginPolicyError(f"{label}: no agents")
    unknown = sorted(f"{agent}={mode}" for agent, mode in modes.items() if mode not in WRITE_MODES)
    if unknown:
        raise PluginPolicyError(f"{label}: unknown write modes {unknown} (known: {sorted(WRITE_MODES)})")
    docs = doc.get("documentation_paths") or []
    if not isinstance(docs, list) or not all(isinstance(p, str) and p for p in docs):
        raise PluginPolicyError(f"{label}: documentation_paths must be a list of globs")
    return PluginAgentPolicy(plugin=plugin.id, modes={str(k): str(v) for k, v in modes.items()},
                             documentation_paths=tuple(docs),
                             read_only_commands=_rules(doc.get("read_only_commands") or [], label))


def resolve_write_mode(config: HarnessConfig, agent: AgentSpec,
                       loader: Callable[[PluginSpec], PluginAgentPolicy] | None = None) -> AgentWriteMode | None:
    """The write mode the harness enforces on ``agent``, or None (harness defaults).

    Sources: security-config ``tools.agents[].write_mode`` (harness policy) and the agent's plugin
    ``agent_policy``. With both, the stricter applies. Raises :class:`PluginPolicyError` when the
    plugin's policy is unusable or names no mode for the agent (callers fail closed).
    """
    harness = config.security.write_mode_of(agent.id)
    if harness is not None and harness not in WRITE_MODES:
        raise PluginPolicyError(f"security-config gives agent '{agent.id}' unknown write mode '{harness}'")
    plugin = next((p for p in config.plugins if p.id == agent.plugin), None)
    load = loader or load_agent_policy
    declared = load(plugin).for_agent(agent.id) if plugin is not None and plugin.agent_policy else None
    if harness is None:
        return declared
    if declared is None:
        return AgentWriteMode(agent.id, harness, source="security-config")
    return replace(declared, mode=stricter(harness, declared.mode), source="security-config+plugin")
