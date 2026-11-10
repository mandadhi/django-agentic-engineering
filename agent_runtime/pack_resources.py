"""Templates and shared references a plugin agent names in its own instructions.

The Django agents declare them in their Markdown, in a ``## Templates and references`` section:
one bullet per kind (``Templates`` / ``References``), the path pattern in backticks and
parentheses (``${CLAUDE_PLUGIN_ROOT}/<folder>/<id><suffix>``), then the ids, each in backticks.

That section is the only agent-to-resource mapping; this module reads it (ids *and* the path
pattern) and keeps no list of its own. For a run the harness then:

1. resolves every named file against the agent's registered plugin root and fails closed
   (``plugin.unresolved``) when one is missing or outside the plugin's readable folders, so a
   template is never silently replaced by something made up;
2. substitutes ``${CLAUDE_PLUGIN_ROOT}`` in the instructions with that root. Claude Code does this
   for plugin agents, but **not** for a programmatic ``AgentDefinition`` prompt (verified with
   CLI 2.1.317), which is how the harness runs agents;
3. offers the files as task context *references* (``uri`` only, never inline), so the agent
   loads one with Read only when the task needs it.

Nothing is copied: every path points into the canonical ``templates/`` and ``references/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.config import AgentSpec, PluginSpec
from core.context import ContextItem, ContextScope
from core.registry import HarnessRegistry

from .exceptions import PluginResolutionError
from .options_factory import AgentInstructions

PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
SECTION = "Templates and references"
#: Bullet label -> resource kind. The path pattern comes from the bullet itself.
KINDS = {"Templates": "template", "References": "reference"}

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_BULLET = re.compile(r"^-\s+(\w+)\s*\(`([^`]+)`\)\s*:\s*(.*)$")
_ID = re.compile(r"`([a-z0-9][a-z0-9-]*)`")


@dataclass(frozen=True)
class PackResource:
    kind: str           # template | reference
    id: str
    path: Path          # absolute, inside the plugin's readable folders

    def context_item(self, plugin_id: str, task_key: str) -> ContextItem:
        size = self.path.stat().st_size
        return ContextItem(id=f"{plugin_id}.{self.kind}.{self.id}", scope=ContextScope.TASK, uri=self.path.as_uri(),
                           task_id=task_key, priority=60, token_estimate=size // 4,
                           description=f"{self.kind} '{self.id}' from plugin '{plugin_id}': read it when the "
                                       "output needs it; never rebuild it from memory")


@dataclass(frozen=True)
class AgentResources:
    plugin: PluginSpec | None
    templates: tuple[PackResource, ...] = ()
    references: tuple[PackResource, ...] = ()

    @property
    def all(self) -> tuple[PackResource, ...]:
        return self.templates + self.references

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """The agent's own plugin folders it may read (so the named files are readable)."""
        return self.plugin.read_roots if self.plugin else ()

    def expand(self, instructions: AgentInstructions) -> AgentInstructions:
        """Instructions with ``${CLAUDE_PLUGIN_ROOT}`` replaced by the plugin root, as Claude Code does."""
        if self.plugin is None or PLUGIN_ROOT_VAR not in instructions.prompt:
            return instructions
        return AgentInstructions(name=instructions.name, description=instructions.description,
                                 prompt=instructions.prompt.replace(PLUGIN_ROOT_VAR, self.plugin.path.as_posix()))

    def context_items(self, task_key: str) -> list[ContextItem]:
        if self.plugin is None:
            return []
        return [resource.context_item(self.plugin.id, task_key) for resource in self.all]


def declared_resources(body: str) -> dict[str, list[tuple[str, str]]]:
    """``{kind: [(id, path pattern), ...]}`` from the body's ``## Templates and references`` section."""
    found: dict[str, list[tuple[str, str]]] = {}
    in_section = fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.match(line)
        if heading:
            in_section = heading.group(1) == SECTION
            continue
        bullet = _BULLET.match(line.strip()) if in_section else None
        if bullet and bullet.group(1) in KINDS:
            kind, pattern = KINDS[bullet.group(1)], bullet.group(2)
            found.setdefault(kind, []).extend((rid, pattern) for rid in _ID.findall(bullet.group(3)))
    return found


def _resolve_one(agent: AgentSpec, plugin: PluginSpec, kind: str, resource_id: str, pattern: str) -> PackResource:
    prefix = PLUGIN_ROOT_VAR + "/"
    if not pattern.startswith(prefix) or "<id>" not in pattern:
        raise PluginResolutionError(f"agent '{agent.id}': {kind} path pattern '{pattern}' must start with "
                                    f"{prefix} and contain <id>")
    relative = pattern[len(prefix):].replace("<id>", resource_id)
    path = (plugin.path / relative).resolve()
    if not any(path == root or root in path.parents for root in plugin.read_roots):
        raise PluginResolutionError(f"agent '{agent.id}': {kind} '{resource_id}' ({relative}) is outside the readable "
                                    f"folders of plugin '{plugin.id}'")
    if not path.is_file():
        raise PluginResolutionError(f"agent '{agent.id}': {kind} '{resource_id}' not found at {relative} in plugin "
                                    f"'{plugin.id}' ({plugin.path.as_posix()})")
    return PackResource(kind=kind, id=resource_id, path=path)


def resolve_resources(registry: HarnessRegistry, agent: AgentSpec, instructions: AgentInstructions) -> AgentResources:
    """Every template and reference the agent names, resolved in its plugin. Raises on anything missing."""
    declared = declared_resources(instructions.prompt)
    uses_root = PLUGIN_ROOT_VAR in instructions.prompt
    plugin = next((p for p in registry.config.plugins if p.id == agent.plugin), None) if agent.plugin else None
    if plugin is None:
        if declared or uses_root:
            raise PluginResolutionError(f"agent '{agent.id}' names {PLUGIN_ROOT_VAR} resources but is not part of a "
                                        "registered plugin")
        return AgentResources(plugin=None)
    resolved: dict[str, list[PackResource]] = {"template": [], "reference": []}
    seen: set[tuple[str, str]] = set()
    for kind, entries in declared.items():
        for resource_id, pattern in entries:
            if (kind, resource_id) in seen:          # named twice: offer it once
                continue
            seen.add((kind, resource_id))
            resolved[kind].append(_resolve_one(agent, plugin, kind, resource_id, pattern))
    return AgentResources(plugin=plugin, templates=tuple(resolved["template"]),
                          references=tuple(resolved["reference"]))
