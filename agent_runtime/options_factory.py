"""Turn a registered agent into Claude Agent SDK options for one stage query.

The agent's Markdown is the single source of truth: its body becomes
``AgentDefinition.prompt``; ``description``, ``tools``, ``disallowedTools`` and
``model`` come from its frontmatter (already parsed into :class:`AgentSpec`); the
registry contributes only the skills (as SDK names). The session runs *as* that
agent: the definition is passed in ``agents`` and selected with the CLI's
``--agent`` flag (through ``extra_args``). Verified with CLI 2.1.317: the session's
``init`` frame then reports the agent's model and tool set.

Security is applied at both the agent and the session level, so the session can never
be wider than the agent:

* ``tools`` = effective tools (declared minus denied) — what exists at all;
* ``allowed_tools`` = the same list — auto-approved; ``permission_mode="dontAsk"``
  denies everything else instead of prompting;
* ``disallowed_tools`` = :meth:`SecurityPolicy.denied_tools`;
* ``settings.permissions.deny`` = ``Read(...)`` / ``Edit(...)`` rules for every
  denied path glob (native, enforced by the CLI before any hook);
* ``setting_sources=[]`` — no user/project settings, CLAUDE.md or settings hooks
  from the (untrusted) workspace; ``strict_mcp_config`` and no MCP servers;
* ``settings.disableAllHooks`` when every loaded plugin is registered ``load_hooks: false``:
  that plugin's own command hooks would otherwise run in the session too (verified), and
  their ``ask`` answers are refused under ``dontAsk``, overriding the harness approver;
  the harness enforces the plugin's ``agent_policy`` itself instead;
* ``verbatim_prompts`` — ``@path`` text in the prompt is never expanded into a file read;
* PreToolUse / PostToolUse hooks from :class:`agent_runtime.security_hooks.ToolGuard`;
* ``env`` — the operator's environment never reaches the CLI unfiltered (Phase 5.1, SEC-03,
  :mod:`agent_runtime.environment`);
* Bash (Phase 5.1, SEC-02, :mod:`agent_runtime.sandbox`): runs inside the CLI's OS sandbox where the
  platform has one (``sandbox``, no unsandboxed fallback); elsewhere it is removed from the session
  or approval-gated. With the sandbox, ``Read(...)`` deny rules also cover credential stores and the
  harness's own configuration and database, since the sandbox enforces read restrictions from them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher

from core.config import AgentRuntimeSettings, AgentSpec, SecurityConfig
from core.frontmatter import FrontmatterError, read_markdown
from core.security import SecurityPolicy

from .environment import session_environment
from .exceptions import AgentDefinitionError
from .plugin_loader import ResolvedPlugins
from .sandbox import DISABLED, ShellIsolation

CLIENT_APP = "django-agentic-engineering/0.2.0"


@dataclass(frozen=True)
class AgentInstructions:
    """What the agent file says about itself, read at run time (the file is the source of truth)."""

    name: str
    description: str
    prompt: str            # Markdown body


def load_instructions(agent: AgentSpec) -> AgentInstructions:
    try:
        meta, body = read_markdown(agent.path)
    except (OSError, FrontmatterError) as exc:
        raise AgentDefinitionError(f"agent '{agent.id}': cannot read {agent.rel_path}: {exc}") from exc
    if meta.get("name") != agent.id:
        raise AgentDefinitionError(f"agent '{agent.id}': frontmatter name is '{meta.get('name')}' in {agent.rel_path}")
    if not str(meta.get("description") or "").strip():
        raise AgentDefinitionError(f"agent '{agent.id}': frontmatter has no description")
    if not body:
        raise AgentDefinitionError(f"agent '{agent.id}': {agent.rel_path} has no instructions after the frontmatter")
    return AgentInstructions(name=agent.id, description=str(meta["description"]).strip(), prompt=body)


PRELOAD_HEADING = "# Preloaded skills and rules"


def with_preloaded_skills(instructions: AgentInstructions, plugins: ResolvedPlugins) -> AgentInstructions:
    """Append the full SKILL.md of every skill the harness preloads (plugin ``preload_skills``).

    Claude Code preloads a subagent's ``skills`` when it spawns the subagent. A session that runs
    *as* the agent (``--agent``, how the harness runs every stage) does not: with CLI 2.1.317 an
    ``AgentDefinition.skills`` entry adds no tokens to the context (measured). So the harness puts
    the registered files in itself: verbatim (frontmatter included), once each, in registry order,
    and only the agent's own. Each block names its base directory, so relative links inside a
    skill resolve to files the agent may read.
    """
    if not plugins.preloaded:
        return instructions
    if PRELOAD_HEADING in instructions.prompt:
        raise AgentDefinitionError(f"agent '{instructions.name}': skills are already preloaded; refusing to inject "
                                   "them twice")
    blocks = [f"\n\n---\n\n{PRELOAD_HEADING}\n\nThe skills and rules registered for you are loaded below in full, "
              "from the canonical plugin files. They are part of your instructions; rules are mandatory. Paths "
              "inside a skill are relative to its base directory."]
    for name, skill_md in plugins.preloaded:
        try:
            text = skill_md.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentDefinitionError(f"agent '{instructions.name}': cannot read skill {name}: {exc}") from exc
        if not text:
            raise AgentDefinitionError(f"agent '{instructions.name}': skill {name} is empty ({skill_md})")
        blocks.append(f'<skill name="{name}" base="{skill_md.parent.as_posix()}">\n{text}\n</skill>')
    return AgentInstructions(name=instructions.name, description=instructions.description,
                             prompt=instructions.prompt + "\n\n".join(blocks))


#: Credential stores in the home directory. Native ``Read`` deny rules for them are added for sandboxed
#: sessions, where they are what keeps a sandboxed command from reading them (the guard denies the paths
#: too, on every platform).
HOME_CREDENTIAL_STORES = ("~/.ssh/**", "~/.aws/**", "~/.azure/**", "~/.kube/**", "~/.config/gcloud/**",
                          "~/.docker/config.json", "~/.netrc", "~/.git-credentials", "~/.npmrc", "~/.pypirc",
                          "~/.pgpass", "~/.claude/.credentials.json", "~/.claude.json")


def _absolute_rule(path: Path) -> str:
    """Permission-rule form of an absolute path (``//`` = from the filesystem root; POSIX only)."""
    return "/" + path.as_posix()


def permission_settings(security: SecurityConfig, *, disable_plugin_hooks: bool = False,
                        protected_read_deny: Iterable[Path] = (), write_deny: Iterable[Path] = ()) -> dict[str, Any]:
    """Native Claude Code permission rules for the denied path globs.

    ``disable_plugin_hooks`` adds ``disableAllHooks``: command hooks shipped by the loaded plugins
    do not run. The harness's own hooks are SDK callbacks and still run (verified with CLI
    2.1.317; :class:`agent_runtime.security_hooks.HookTripwire` detects it if they ever stop).

    ``protected_read_deny`` (sandboxed sessions only): absolute paths of harness internals that the
    sandbox must keep unreadable, plus the home credential stores.

    ``write_deny`` (sandboxed sessions only, SEC-05): directories nothing in the session may change,
    the workspace of a read-only agent. The sandbox applies ``Edit(...)`` deny rules to every
    command's writes, including code the guard cannot analyse (a test run, ``manage.py check``
    importing the project).
    """
    deny = [f"{tool}({glob})" for glob in security.denied_paths for tool in ("Read", "Edit")]
    protected = list(protected_read_deny)
    if protected:
        for path in protected:
            # a directory: everything below it; a file: it and its siblings (harness.db-wal, -shm, -journal)
            pattern = _absolute_rule(path) + ("/**" if path.is_dir() or not path.suffix else "*")
            deny += [f"Read({pattern})", f"Edit({pattern})"]
        deny += [f"{tool}({glob})" for glob in HOME_CREDENTIAL_STORES for tool in ("Read", "Edit")]
    deny += [f"Edit({_absolute_rule(path)}/**)" for path in write_deny]
    settings: dict[str, Any] = {"permissions": {"deny": deny}}
    if disable_plugin_hooks:
        settings["disableAllHooks"] = True
    return settings


#: The CLI's internal tool that returns ``--json-schema`` structured output. Observed with
#: CLI 2.1.317: a session run with ``--agent`` only exposes tools in the agent definition's
#: ``tools``, so without this entry structured output silently never happens. It is not a
#: capability on the codebase; the PreToolUse guard allows it explicitly (HARNESS_TOOLS).
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def session_tools(agent: AgentSpec, policy: SecurityPolicy,
                  isolation: ShellIsolation | None = None) -> tuple[list[str] | None, list[str]]:
    """(effective tools or None, denied tools) for this session: the policy minus Bash without a sandbox."""
    effective = policy.effective_tools(agent.id)
    denied = set(policy.denied_tools(agent.id))
    if isolation is not None and isolation.mode == DISABLED:
        denied.add("Bash")
        effective = tuple(t for t in effective if t != "Bash") if effective is not None else None
    return (list(effective) if effective is not None else None), sorted(denied)


def build_agent_definition(agent: AgentSpec, instructions: AgentInstructions, policy: SecurityPolicy,
                           plugins: ResolvedPlugins, *, structured_output: bool = False,
                           isolation: ShellIsolation | None = None) -> AgentDefinition:
    tools, denied = session_tools(agent, policy, isolation)
    if tools is not None and structured_output:
        tools.append(STRUCTURED_OUTPUT_TOOL)
    return AgentDefinition(
        description=instructions.description,
        prompt=instructions.prompt,
        tools=tools,
        disallowedTools=denied or None,
        model=agent.model,
        skills=list(plugins.definition_skills) or None,     # harness-preloaded skills are not listed twice
    )


@dataclass(frozen=True)
class RunPaths:
    workspace: Path        # session cwd: the project the agent works on
    artifact_dir: Path     # where this run's outputs are written


def harness_environment(security: SecurityConfig, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``ClaudeAgentOptions.env``: the allow-listed environment plus the harness's own client name."""
    return session_environment(security.environment, fixed={"CLAUDE_AGENT_SDK_CLIENT_APP": CLIENT_APP},
                               source=source)


def build_options(*, agent: AgentSpec, instructions: AgentInstructions, policy: SecurityPolicy,
                  security: SecurityConfig, plugins: ResolvedPlugins, paths: RunPaths,
                  runtime: AgentRuntimeSettings, hooks: Mapping[str, list[HookMatcher]],
                  output_schema: Mapping[str, Any] | None, resume_session: str | None = None,
                  stderr: Callable[[str], None] | None = None, isolation: ShellIsolation | None = None,
                  env: Mapping[str, str] | None = None,
                  protected_read_deny: Iterable[Path] = (), write_deny: Iterable[Path] = ()) -> ClaudeAgentOptions:
    definition = build_agent_definition(agent, instructions, policy, plugins,
                                        structured_output=output_schema is not None, isolation=isolation)
    effective, denied = session_tools(agent, policy, isolation)
    session_agent = plugins.session_agent_name(agent.id)
    sandbox = isolation.sandbox if isolation is not None else None
    sandboxed_posix = bool(sandbox) and os.name != "nt"
    settings = permission_settings(security, disable_plugin_hooks=plugins.hooks_disabled,
                                   protected_read_deny=protected_read_deny if sandboxed_posix else (),
                                   write_deny=write_deny if sandboxed_posix else ())
    return ClaudeAgentOptions(
        agents={session_agent: definition},
        extra_args={"agent": session_agent},
        # Preset without `append` sends no --system-prompt flag, so the --agent definition's
        # prompt is what the session runs with (system_prompt=None would send an empty one).
        system_prompt={"type": "preset", "preset": "claude_code"},
        tools=list(effective) if effective is not None else {"type": "preset", "preset": "claude_code"},
        allowed_tools=list(effective) if effective is not None else [],
        disallowed_tools=denied,
        permission_mode="dontAsk",
        settings=json.dumps(settings),
        sandbox=sandbox,                 # type: ignore[arg-type]  (SandboxSettings; failIfUnavailable is a CLI key)
        setting_sources=[],
        skills=list(plugins.skill_names),
        plugins=plugins.sdk_config(),   # type: ignore[arg-type]  (list of SdkPluginConfig dicts)
        mcp_servers={},
        strict_mcp_config=True,
        hooks=dict(hooks),               # type: ignore[arg-type]
        cwd=str(paths.workspace),
        add_dirs=[str(paths.artifact_dir)],
        max_turns=runtime.max_turns,
        max_budget_usd=runtime.max_budget_usd,
        output_format={"type": "json_schema", "schema": dict(output_schema)} if output_schema else None,
        resume=resume_session,
        verbatim_prompts=True,
        stderr=stderr,
        env=dict(env) if env is not None else harness_environment(security),
    )
