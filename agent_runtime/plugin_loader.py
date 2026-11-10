"""Resolve, validate and verify the plugins an agent's registered skills come from.

Only plugins listed in agent-registry ``plugins`` are ever passed to the SDK, and only
those that hold a skill registered for the agent being run. Every path is checked
before the SDK starts (:func:`resolve_plugins`), and the SDK session's own ``init``
report is checked after it starts (:func:`verify_loaded`): the CLI does **not** fail
on a bad ``--plugin-dir`` — it only lists it in ``init.plugin_errors`` and carries
on (verified with CLI 2.1.317) — so the harness aborts the run itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.config import AgentSpec, PluginSpec
from core.errors import RegistryLookupError
from core.frontmatter import FrontmatterError, read_markdown
from core.registry import HarnessRegistry

from .exceptions import AgentDefinitionConflictError, PluginLoadError, PluginResolutionError

#: Prefix of the name every session runs the harness's AgentDefinition under (see session_agent_name).
SESSION_AGENT_PREFIX = "dae-"


def session_agent_name(agent_id: str) -> str:
    """Name the session runs the harness's AgentDefinition under (``agents`` key and ``--agent``).

    When a loaded plugin ships an agent file with the same name, the CLI runs that file and drops
    the programmatic definition (measured with CLI 2.1.317 for the Django pack, and confirmed live
    for the reused Phase 2 agents: AGT-02). So every agent runs as ``dae-<id>``, the one name no
    registered plugin may define (:func:`check_agent_collisions`). The logical id ``agent_id`` is
    what registries, logs, audit records, results and workflows keep using.
    """
    return f"{SESSION_AGENT_PREFIX}{agent_id}"


@dataclass(frozen=True)
class ResolvedPlugins:
    plugins: tuple[PluginSpec, ...]       # in registry order, no duplicates
    skill_names: tuple[str, ...]          # names the SDK knows the skills by ("<plugin>:<skill>")
    #: (SDK name, SKILL.md) of the skills the harness preloads itself (plugin ``preload_skills``).
    preloaded: tuple[tuple[str, Path], ...] = ()

    @property
    def definition_skills(self) -> tuple[str, ...]:
        """``AgentDefinition.skills``: every skill the harness does not preload itself (never both)."""
        harness = {name for name, _ in self.preloaded}
        return tuple(name for name in self.skill_names if name not in harness)

    def session_agent_name(self, agent_id: str) -> str:
        """See the module-level :func:`session_agent_name` (the same for every agent since Phase 5.1)."""
        return session_agent_name(agent_id)

    def sdk_config(self) -> list[dict[str, str]]:
        """Value for ``ClaudeAgentOptions.plugins`` (SdkPluginConfig: local plugins only)."""
        return [{"type": "local", "path": str(plugin.path)} for plugin in self.plugins]

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Plugin directories an agent may read (each plugin's ``read_paths``, else the plugin root)."""
        return tuple(root for plugin in self.plugins for root in plugin.read_roots)

    @property
    def hooks_disabled(self) -> bool:
        """True when every loaded plugin is registered ``load_hooks: false``.

        The session then runs with ``disableAllHooks`` in its flag settings. Verified with CLI
        2.1.317: that stops command hooks from ``--plugin-dir`` plugins, while the harness's own
        SDK callback hooks (ToolGuard) still run. A session that loads any plugin whose hooks are
        allowed keeps all plugin hooks (the stricter outcome).
        """
        return bool(self.plugins) and all(not plugin.load_hooks for plugin in self.plugins)


def validate_plugin(plugin: PluginSpec, repo_root: Path) -> None:
    """The directory is inside its base (repo_root, or the harness) and is a Claude Code plugin named ``plugin.id``."""
    path = plugin.path.resolve()
    root = (plugin.anchor or repo_root).resolve()
    if path != root and root not in path.parents:
        base = "the repository root" if plugin.base == "repo_root" else "the harness directory"
        raise PluginResolutionError(f"plugin '{plugin.id}': {plugin.rel_path} is outside {base}")
    if not path.is_dir():
        raise PluginResolutionError(f"plugin '{plugin.id}': directory not found: {plugin.rel_path}")
    if not plugin.manifest.is_file():
        raise PluginResolutionError(f"plugin '{plugin.id}': no .claude-plugin/plugin.json in {plugin.rel_path}")
    try:
        manifest = json.loads(plugin.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginResolutionError(f"plugin '{plugin.id}': unreadable plugin.json: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != plugin.id:
        raise PluginResolutionError(f"plugin '{plugin.id}': plugin.json name does not match the registry id")


def resolve_plugins(registry: HarnessRegistry, agent: AgentSpec) -> ResolvedPlugins:
    """Plugins and SDK skill names for an agent's registered skills. Raises on anything missing."""
    by_id = {p.id: p for p in registry.config.plugins}
    plugins: dict[str, PluginSpec] = {}
    names: list[str] = []
    preloaded: list[tuple[str, Path]] = []
    if len(set(agent.skills)) != len(agent.skills):
        raise PluginResolutionError(f"agent '{agent.id}': a skill is registered twice; each loads once")
    for skill_id in agent.skills:
        try:
            skill = registry.skills.get(skill_id)
        except RegistryLookupError as exc:
            raise PluginResolutionError(f"agent '{agent.id}': {exc}") from exc
        if not skill.enabled:
            raise PluginResolutionError(f"agent '{agent.id}': skill '{skill.id}' is disabled")
        if not (skill.path / "SKILL.md").is_file():
            raise PluginResolutionError(f"agent '{agent.id}': skill '{skill.id}' has no SKILL.md at {skill.rel_path}")
        if skill.plugin is None:
            raise PluginResolutionError(
                f"agent '{agent.id}': skill '{skill.id}' (source: {skill.source}) is not inside a registered plugin; "
                "the SDK can only load registered plugin skills")
        plugin = by_id.get(skill.plugin)
        if plugin is None:   # pragma: no cover - config validation guarantees membership
            raise PluginResolutionError(f"skill '{skill.id}': plugin '{skill.plugin}' is not registered")
        if plugin.id not in plugins:
            validate_plugin(plugin, registry.config.framework.repo_root)
            plugins[plugin.id] = plugin
        names.append(skill.sdk_name)
        if plugin.preload_skills:
            preloaded.append((skill.sdk_name, (skill.path / "SKILL.md").resolve()))
    if len({path for _, path in preloaded}) != len(preloaded):
        raise PluginResolutionError(f"agent '{agent.id}': two registered skills share one SKILL.md")
    return ResolvedPlugins(plugins=tuple(plugins.values()), skill_names=tuple(names), preloaded=tuple(preloaded))


def plugin_agent_names(plugin: PluginSpec) -> set[str]:
    """Names under which the CLI registers the agent files ``plugin`` ships.

    The manifest's ``agents`` list when it declares one (it then replaces the folder scan), else
    every ``agents/*.md``. An agent file is known by its frontmatter ``name`` (else its file name),
    both bare and namespaced ``<plugin>:<name>``. Unreadable files fail closed.
    """
    try:
        manifest = json.loads(plugin.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginResolutionError(f"plugin '{plugin.id}': unreadable plugin.json: {exc}") from exc
    declared = manifest.get("agents") if isinstance(manifest, dict) else None
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, list):
        files = [(plugin.path / str(entry)).resolve() for entry in declared]
    else:
        files = sorted((plugin.path / "agents").glob("*.md"))
    names: set[str] = set()
    for path in files:
        for agent_file in (sorted(path.glob("*.md")) if path.is_dir() else [path]):
            try:
                meta, _ = read_markdown(agent_file)
            except FrontmatterError:
                meta = {}                      # conservatively assume it registers under its file name
            except OSError as exc:
                raise PluginResolutionError(
                    f"plugin '{plugin.id}': cannot read agent file {agent_file.name}: {exc}") from exc
            name = str(meta.get("name") or agent_file.stem)
            names |= {name, f"{plugin.id}:{name}"}
    return names


def check_agent_collisions(plugins: ResolvedPlugins, agent_id: str) -> str:
    """Fail before the SDK starts if a loaded plugin defines the harness's session agent name.

    The harness passes its definition as ``dae-<id>``. A plugin agent registered under that name
    would silently replace it (AGT-02), so the run is refused instead. Returns the session name.
    """
    session_agent = session_agent_name(agent_id)
    for plugin in plugins.plugins:
        if session_agent in plugin_agent_names(plugin):
            raise AgentDefinitionConflictError(
                f"agent '{agent_id}': plugin '{plugin.id}' ships an agent named '{session_agent}', which would "
                "replace the harness definition; refusing to run")
    return session_agent


def _same_path(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return False


def verify_loaded(init: Mapping[str, Any], expected: ResolvedPlugins) -> None:
    """Check the SDK ``init`` frame: no plugin errors, every expected plugin and skill present."""
    errors = init.get("plugin_errors") or []
    if errors:
        detail = "; ".join(f"{e.get('plugin', '?')}: {e.get('type', 'error')}: {e.get('message', '')}" for e in errors)
        raise PluginLoadError(f"the SDK reported plugin load errors: {detail}")
    loaded = init.get("plugins") or []
    for plugin in expected.plugins:
        if not any(p.get("name") == plugin.id and _same_path(str(p.get("path", "")), plugin.path) for p in loaded):
            raise PluginLoadError(f"plugin '{plugin.id}' ({plugin.rel_path}) was not loaded by the SDK session")
    available = set(init.get("skills") or ())
    missing = [name for name in expected.skill_names if name not in available]
    if missing:
        raise PluginLoadError(f"skills not available in the SDK session: {', '.join(missing)}")
