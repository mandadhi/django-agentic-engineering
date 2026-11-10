---
project: django-agentic-engineering
abstract: Reference for the Django Agentic Harness: configuration, registry, Agent SDK execution, security model, persistence, CLI and tests.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, SQLite, JSON Schema, YAML, pytest, Claude Agent SDK, Claude Code]
domain: [Software Engineering, AI Agents]
---

# Django Agentic Harness reference

Detailed engineering reference for the optional Python harness. For an overview, installation and
the difference between the harness and the standalone plugin, start with the [project README](../README.md).
The standalone plugin is documented in [plugin.md](plugin.md).

Commands below use the Windows virtual-environment path `.venv/Scripts/python`; on Linux and macOS
use `.venv/bin/python`.

> **Layout and profiles.** The shipped registries reuse agents and skills from a parent repository's
> `plugins/security`, `plugins/database`, `plugins/api` and `plugins/pr-automation`
> (`paths.repo_root: ../..` in `config/framework-config.yaml`).
>
> Since Phase 6 these four are registered as **optional** integrations:
> - The default `full` profile still requires them. In a standalone checkout every `core.cli`
>   command fails on those entries, with an error that says how to recover.
> - `--profile pack` (or `DAE_HARNESS_PROFILE=pack`) excludes them explicitly and reports each
>   exclusion.
>
> See [Parent repository layout](#parent-repository-layout) and
> [orchestration.md](orchestration.md#harness-setup-and-profiles).

## Requirements

| Requirement | Notes |
|---|---|
| Python ≥ 3.11 | Verified with 3.13. |
| `claude-agent-sdk==0.2.177` | Pinned in `requirements-dev.txt`; `pyproject.toml` allows `>=0.2.177,<0.3`. The wheel **bundles the Claude Code CLI** (2.1.317 for this version), so no separate CLI install is needed. |
| Credentials | Either a Claude Code login on the machine (`claude auth status`; no variable needed), or `ANTHROPIC_API_KEY`. Since Phase 5.1 the CLI receives only the variables allow-listed in `config/security-config.yaml` (`environment`); every other variable reaches it empty. Bedrock and Vertex: add their variables (`CLAUDE_CODE_USE_BEDROCK`, `AWS_*`, ...) to `environment.allow` or `environment.credentials` explicitly. |
| Bash and the OS sandbox | Since Phase 5.1 Bash runs only inside the Claude Code OS sandbox: macOS, or Linux with `bwrap` (bubblewrap) and `socat` on `PATH`. On Windows, or Linux without them, Bash is removed from every session (`shell.without_sandbox: disable`, the default) or every Bash call needs human approval (`approve`). See [Shell execution and sandboxing](#shell-execution-and-sandboxing-phase-51). |
| `CLAUDE_CODE_GIT_BASH_PATH` (Windows only) | The CLI offers the **Bash** tool on Windows only when it finds Git Bash. If Git is installed somewhere non-standard, point this variable at `...\Git\bin\bash.exe`. Without it, agents that declare Bash run without it. The harness detects this and records it (`session.tools_unavailable` audit event, `unavailable_tools` on the run). |
| `DAE_SDK_INTEGRATION=1` | Opt-in for the real-SDK tests only. |

No credentials or secrets are stored in this repository or in the harness database.

## Layout

| Path | What it holds |
|---|---|
| `config/` | The six configuration files and `config/schemas/` (one JSON Schema each). |
| `contracts/` | JSON Schemas for `AgentTask`, `AgentResult`, `WorkflowDefinition`, `WorkflowStage`, `EvaluationResult` and (Phase 5.3) `RunRecord`, plus `common.schema.json` and `examples/`. |
| `core/` | Config loader, contract validator, reference validation, registry, workflow model, context manager, security policy, structured logging and the CLI. |
| `runtime/` | Status/transition tables, persisted records, the `ExecutionStore` protocol, the SQLite store and `ExecutionManager`, which is the workflow engine. **Phase 5.3:** `records.py` (RunRecord contract, execution summary). |
| `agent_runtime/` | **Phase 2.** SDK adapter, options factory, plugin loader, security hooks, prompt rendering, structured-output schema, result processor, session manager, runner and the `agent`/`execution` CLI commands. |
| `templates/` | `agent-task.prompt.md`: how an `AgentTask` is rendered into the stage prompt. **Phase 3E:** twelve Django output templates (`<id>.template.md`). Since Phase 5 harness runs offer an agent's templates as task-context references (`agent_runtime/pack_resources.py`). |
| `workflows/` | Workflow definitions (`*.workflow.yaml`), discovered automatically. |
| `agents/`, `skills/`, `hooks/`, `evaluations/` | Extension points. Each has a README with its convention. |
| `rules/` | **Phase 3C.** Twelve mandatory Django engineering rules, packaged and registered like skills. |
| `commands/` | **Phase 3D.** Claude Code developer commands, one per Django agent. Not used by the harness. |
| `references/` | **Phase 3E.** Seven shared Django references (checklists and lookup tables) used by several agents. Offered to harness runs as task-context references since Phase 5. |
| `.claude-plugin/` | **Phase 4.** `plugin.json`: the `django-engineering` Claude Code plugin manifest. |
| `hooks/` | **Phase 4** adds the plugin's guard: `hooks.json`, `django_pack_guard.py` and the generated `security-policy.json`. |
| `plugin_packaging/` | **Phase 4.** `python -m plugin_packaging build/check/stage` and the canonical standalone policy `standalone-security.yaml`. Since Phase 5 the harness reads that file's per-agent write modes (registry `agent_policy`) and enforces them with its own guard; it never runs the packaging code. |
| `tests/` | pytest suite. `tests/fakes.py` is the fake SDK adapter. |
| `var/` | Runtime output (SQLite DB, `artifacts/`). Gitignored. |

## Architecture

```mermaid
flowchart TB
    subgraph CFG[config/ + workflows/ + contracts/]
        FC[framework-config<br/>paths · storage · agent_runtime]
        AR[agent-registry<br/>plugins · agents]
        SR[skill-registry]
        SC[security-config<br/>tools · paths · commands · secrets]
        WD[workflows/*.workflow.yaml]
    end
    PLUG[(repo plugins/*<br/>agent .md · SKILL.md<br/>reused by path)]

    subgraph CORE[core/]
        REG[HarnessRegistry]
        CM[ContextManager]
        SEC[SecurityPolicy · Redactor]
    end

    subgraph RT[runtime/ — workflow engine]
        EM[ExecutionManager<br/>DAG · attempts · approvals<br/>inputs · checkpoints · resume]
        SQL[(SQLite v2<br/>executions · stages · agent_runs<br/>+session_id/cost · logs · artifact refs)]
    end

    subgraph ART[agent_runtime/ — thin SDK layer]
        RUN[AgentRunner<br/>one stage = one query]
        OF[options_factory<br/>Markdown → AgentDefinition]
        PL[plugin_loader]
        HK[security_hooks<br/>Pre/PostToolUse · approvals · audit]
        RP[result_processor]
        SM[session_manager]
        AD[sdk_adapter]
    end
    SDK[[Claude Agent SDK<br/>bundled Claude Code CLI]]

    CFG --> REG
    PLUG -.validated.-> REG
    REG --> EM & CM & RUN
    EM <--> SQL
    RUN --> EM
    RUN --> OF & PL & HK & RP & SM
    OF --> AD
    PL -. plugins .-> AD
    HK -. hooks .-> AD
    AD --> SDK
    SDK -. messages .-> RP
    RP -- AgentResult --> EM
    SDK -. loads .-> PLUG
```

**Load path** (unchanged from Phase 1, with plugins added). `HarnessRegistry.load(root)`
reads `framework-config.yaml`, which names the other five configs. Each config is checked
for `schema_version`, `kind`, its JSON Schema and semantics. Workflows are discovered and
contract-validated. Last, every cross-reference is checked, including the new plugin and
input references. All problems are raised together.

### Agent execution sequence

```mermaid
sequenceDiagram
    autonumber
    participant CLI as CLI / caller
    participant R as AgentRunner
    participant EM as ExecutionManager
    participant DB as SQLite store
    participant SDK as Agent SDK (query)
    participant CC as Claude Code CLI
    participant H as ToolGuard hooks

    CLI->>R: start(workflow, inputs, workspace)
    R->>EM: start_execution (inputs validated)
    EM->>DB: execution + stages + checkpoint(start)
    loop while ExecutionManager reports runnable stages
        R->>EM: start_stage (dependencies, attempts)
        R->>R: load agent Markdown · resolve plugins/skills · assemble scoped context
        R->>EM: create_task (AgentTask validated) → run queued
        R->>R: AgentDefinition + ClaudeAgentOptions (tools, deny rules, hooks, json-schema)
        R->>EM: start_agent_run
        R->>SDK: query(prompt, options)  [timeout + cancel watcher]
        SDK->>CC: spawn: --agent id, --plugin-dir, --tools, --settings ...
        CC-->>R: system/init (session_id, model, tools, plugins, skills)
        R->>DB: session_id + checkpoint(agent_session_started)
        R->>R: verify plugins/skills/agent/tools — abort on mismatch
        loop agent loop (inside the SDK)
            CC->>H: PreToolUse(tool, input)
            H->>DB: audit (allowed / denied / approval)
            H-->>CC: {} or deny
            CC->>H: PostToolUse(output)
            H-->>CC: updatedToolOutput (secrets masked)
        end
        CC-->>R: ResultMessage (structured_output, usage, cost)
        R->>R: AgentResult: normalize · materialize artifacts · validate contract
        R->>EM: record_result + run metadata (cost, turns, duration)
        alt succeeded / partial
            R->>EM: complete_stage (or awaiting_approval) + checkpoint
        else failed / timeout / invalid
            R->>EM: fail_stage + checkpoint; retry_stage if retryable and attempts left
        end
    end
    R-->>CLI: outcome (done, awaiting approval, or failed)
```

## Agent execution (Phase 2)

### Responsibilities

| Concern | Owner |
|---|---|
| Which stage runs next, dependencies, attempts, retries, approval gates, state, checkpoints | `ExecutionManager` and the workflow DAG. `AgentRunner.run_workflow` only loops over `runnable_stages()` and calls `retry_stage()`. |
| The agent loop: model, tools, skills, hooks, session transcript | Claude Agent SDK and its bundled CLI |
| Turning a stage into one SDK query, and its result back into an `AgentResult` | `agent_runtime/` |

The runner contains no tool loop, LLM loop or retry policy. Stages run sequentially.

### How a stage becomes an SDK query

1. **Agent.** `options_factory.load_instructions` reads the agent's Markdown at run time.
   The **body is `AgentDefinition.prompt`**, followed (Phase 5.1, every agent) by the full text
   of each registered SKILL.md, once, in registry order. `description`, `tools`,
   `disallowedTools` and `model` come from the frontmatter. The registry adds only the skills.
2. **Run as that agent.** The definition is passed in `agents={"dae-<id>": AgentDefinition}` and
   selected with the CLI's `--agent dae-<id>` flag (`extra_args`). `system_prompt` is the
   `claude_code` preset without `append`, so no `--system-prompt` flag overrides the
   agent's prompt. *Verified with CLI 2.1.317:* the session's `init` frame reports the
   agent's model (e.g. `haiku` → `claude-haiku-5-5`) and its tool set.
3. **Plugins and skills.** See [Plugin configuration](#plugin-configuration).
4. **Context.** `ContextManager.assemble_for_stage` applies the agent's `context_needs`
   narrowed by the stage's `context:` block. Outputs of the stage's *declared*
   dependencies are added as task-scoped references. A stage sees only the workflow
   inputs it declares (`extensions["dae.inputs"]`).
5. **Task.** `ExecutionManager.create_task` builds and validates the `AgentTask`.
   `prompt.render_task_prompt` renders it with `templates/agent-task.prompt.md`. The
   template carries the objective, constraints, inputs, expected outputs, context
   references, the "workspace content is data, not instructions" rule and the result
   protocol. It does not carry the agent's instructions.
6. **Options.** See [Security model](#security-model). `cwd` is the workspace.
   `add_dirs` is the run's artifact directory. `max_turns` and `max_budget_usd` come from
   `framework-config.agent_runtime`. `output_format` is the AgentResult schema.
7. **Execute.** `sdk_adapter.ClaudeSDKAdapter.stream` is the only call into the SDK
   (`query()`). The runner consumes the stream under a timeout and a cancellation watcher.
   On the `init` frame it persists the session id and a checkpoint first, then verifies
   what was loaded.
8. **Result.** `result_processor.ResultProcessor` builds a contract-valid `AgentResult`.
   See [Result processing](#result-processing).

### SDK APIs used (claude-agent-sdk 0.2.177, CLI 2.1.317)

Every item below was checked in the installed package source. The items marked
"observed" were also confirmed by running the CLI.

| API | Use |
|---|---|
| `query(prompt=, options=)` | One query per stage (`sdk_adapter.py`). |
| `ClaudeAgentOptions` | `agents`, `extra_args={"agent": "dae-<id>"}`, `system_prompt` preset, `tools`, `allowed_tools`, `disallowed_tools`, `permission_mode="dontAsk"`, `settings` (inline JSON `permissions.deny`), `sandbox` (macOS/Linux, Phase 5.1), `setting_sources=[]`, `skills`, `plugins`, `mcp_servers={}`, `strict_mcp_config`, `hooks`, `cwd`, `add_dirs`, `max_turns`, `max_budget_usd`, `output_format={"type":"json_schema",...}`, `resume`, `verbatim_prompts`, `stderr`, `env` (allow-listed environment, Phase 5.1). |
| `AgentDefinition` | `description`, `prompt`, `tools`, `disallowedTools`, `model`, `skills`. |
| `HookMatcher(matcher=None, hooks=[...], timeout=, on_failure="block")` | PreToolUse and PostToolUse. With `"block"`, a hook that raises, times out or returns an invalid value blocks the tool call. The SDK refuses to connect if the CLI cannot honor it. |
| Hook I/O | Input: `tool_name`, `tool_input`, `tool_response`, `agent_type`. Output: `hookSpecificOutput.permissionDecision="deny"` + `permissionDecisionReason`; PostToolUse `updatedToolOutput`. |
| Messages | `SystemMessage(subtype="init")`: `session_id`, `model`, `tools`, `agents`, `plugins`, `plugin_errors`, `skills`. `AssistantMessage`. `ResultMessage`: `structured_output`, `result`, `is_error`, `subtype`, `usage`, `total_cost_usd`, `duration_ms`, `num_turns`, `permission_denials`, `terminal_reason`, `api_error_status`. |
| Errors | `ResultError` (raised *after* an error `ResultMessage`, observed), `CLINotFoundError`, `CLIConnectionError`, `ProcessError`. |
| Sessions | `ClaudeAgentOptions.resume`, `get_session_info`, `get_session_messages`. |

Behaviour observed with the bundled CLI that the design depends on:

- A bad `--plugin-dir` does **not** fail the session. It only appears in `init.plugin_errors`.
- Plugin skills are named `<plugin>:<skill>`.
- With `--agent`, the session exposes only the tools in the agent definition. That
  includes the CLI's internal `StructuredOutput` tool, so the harness adds it when it
  requests structured output; without it, structured output silently never happens.
- The API rejects `allOf`/`anyOf`/`oneOf` at the top level of the structured-output
  schema. See [Result processing](#result-processing).
- Native `permissions.deny` rules refuse a call **before** PreToolUse runs. The refusal
  comes back in `ResultMessage.permission_denials`.

## Plugin configuration

`config/agent-registry.yaml` lists the plugin directories the SDK may load:

```yaml
plugins:
  - { id: security, path: plugins/security }      # id = .claude-plugin/plugin.json "name"
  - { id: database, path: plugins/database }
```

- Every agent or skill with `source: plugin` must live inside a registered plugin.
  `validate` reports `unregistered-plugin` otherwise.
- For a stage, only the plugins that hold the agent's registered skills are passed to
  `ClaudeAgentOptions.plugins`. Skills are passed by their SDK names to
  `AgentDefinition.skills` (preloaded into the agent) and to `skills` (the session's skill
  filter).
- **Before** the SDK starts, each plugin is checked: it is inside the repository, its
  directory and `plugin.json` exist, and the manifest `name` equals the id. A missing
  skill, a missing `SKILL.md` or a skill outside a plugin fails the stage with
  `plugin.unresolved`, and the SDK is never started.
- **After** the session starts, the `init` frame is checked. Any `plugin_errors`, a
  missing plugin, a missing skill, or the agent not being registered fails the run with
  `plugin.load_failed`.
- `setting_sources=[]` and `strict_mcp_config`: no user or project settings, no
  user-installed plugins, no MCP servers, and no `CLAUDE.md` from the workspace.
- Harness-local skills (`source: local`) cannot be loaded, because the SDK loads
  skills through plugins. See the known limitations.

Phase 5 adds optional keys. Existing entries leave them out and behave exactly as before.

| Key | On | Meaning |
|---|---|---|
| `base: harness` | plugin | `path` is relative to the harness installation (the folder holding `core/`), not `paths.repo_root`. It never depends on the working directory or on where `config/` was copied. The plugin must stay inside its base. |
| `read_paths` | plugin | Plugin sub-folders an agent may read at run time. The default is the whole plugin. |
| `agent_policy` | plugin | A `PluginSecurityPolicy` file (relative to the plugin) whose per-agent write modes the harness guard enforces. It must cover exactly the plugin's registered agents. |
| `load_hooks: false` | plugin | The plugin's own command hooks are not loaded in harness sessions (`disableAllHooks` in the flag settings). This applies only when every plugin of the session sets it, and it requires `agent_policy`. |
| `preload_skills: true` | plugin | The harness injects each registered `SKILL.md` into the agent itself and runs the definition as `dae-<id>`. |
| `plugin: <id>` | agent, skill | The entry belongs to that plugin (`source: plugin`), and `path` is relative to the plugin root. `validate` reports an unknown id, a wrong `source` or a path that leaves the plugin. |

## Harness integration (Phase 5)

The `django-engineering` plugin is registered once. Every Django agent, skill, rule, template and
reference resolves to the canonical pack file; nothing is copied.

```yaml
# config/agent-registry.yaml
plugins:
  - id: django-engineering
    path: .
    base: harness
    read_paths: [templates, references, skills, rules]
    agent_policy: plugin_packaging/standalone-security.yaml
    load_hooks: false
    preload_skills: true
agents:
  - { id: django-model-agent, source: plugin, plugin: django-engineering, path: agents/django-model-agent.md, ... }
# config/skill-registry.yaml
  - { id: django-orm, source: plugin, plugin: django-engineering, path: skills/django-orm, ... }
  - { id: django-model-standards, source: plugin, plugin: django-engineering, path: rules/django-model-standards, ... }
```

Each stage of a Django agent goes through the unchanged Phase 2 runner:

1. **Skills and rules.** The agent's registry `skills` list (skills, then rules) is the only
   mapping. The generated frontmatter `skills:` line used by the standalone plugin is built from
   it and is tested to be identical. Each `SKILL.md` is validated, then injected verbatim once, in
   registry order, with its base directory. Measured with CLI 2.1.317, a session run as the agent
   does not preload `AgentDefinition.skills` itself, so these names are not passed there (nothing
   is loaded twice). They stay in the session `skills` filter, and the `init` frame must list them.
2. **Session agent name.** The plugin ships agent files with the same names. Under such a name the
   CLI runs the plugin's file and drops the harness definition (measured). The harness definition
   therefore runs as `dae-<id>`, and the guard refuses calls attributed to any other agent. Since
   Phase 5.1 this holds for **every** agent, the reused Phase 2 agents included (AGT-02), and a run
   is refused before the SDK starts if a loaded plugin defines that `dae-<id>` name itself
   (`agent.definition_conflict`). Registries, tasks, results, logs and audit records keep the
   logical id; the run's `sdk_metadata.session_agent` records the session name.
3. **Templates and references.** They are read from the agent's own `## Templates and references`
   section, including the path pattern. Each named file is resolved in the plugin, and a missing or
   unreadable one fails closed with `plugin.unresolved`. `${CLAUDE_PLUGIN_ROOT}` is replaced with
   the plugin root (the CLI does not do this for programmatic agents). The files are offered as
   task-context references (`uri` only) and read on demand.
4. **Security.** The harness enforces the pack's write modes with its own PreToolUse guard. Each mode
   grants capabilities (see [Write modes and capabilities](#write-modes-and-capabilities-sec-05-sec-06)):
   - `read-only` (analyzer, code review): only operations classified `read`; no file writes, no shell
     redirection, and every other command (including unrecognised ones) is denied.
   - `documentation-only`: writes only to documentation paths, no shell.
   - `confirm-writes` (security agent): only `read` runs unprompted. Every other operation, including
     `sed -i`, redirections, test runs and unrecognised commands, goes to the harness approver. The
     default approver denies.
   - All modes: the run's output directory stays writable, and the global `security-config.yaml`
     rules, workspace boundaries, redaction and audit apply unchanged.

   The pack's standalone guard is not loaded (`load_hooks: false`). Its `ask` answers would be
   refused under `dontAsk` and would override the harness approver.

`python -m core.cli agent inspect <django agent>` shows the resolved skills, rules, templates,
references, read roots, write mode, preload size and plugin-hook handling.

## Security model

The effective tool policy only removes. The agent's `tools` frontmatter is the baseline;
its own `disallowedTools`, the global `tools.deny` and the per-agent denies are taken
away. Nothing is ever granted. The single exception is the CLI's internal
`StructuredOutput` tool, which returns the result and gives no access to the codebase.

| Threat | Enforcement (layer) |
|---|---|
| Tools the agent did not declare / denied tools | `AgentDefinition.tools` and session `tools` = effective tools. `disallowed_tools` = denied set. `allowed_tools` + `permission_mode="dontAsk"` deny anything not pre-approved, without prompting. PreToolUse re-checks every call (`SecurityPolicy.is_tool_allowed`). At `init`, if the session exposes a tool outside the policy, the run is aborted (`security.violation`). |
| Reading sensitive files | Native `permissions.deny`: `Read(glob)` and `Edit(glob)` for every `paths.deny` glob (verified live: `.env` refused). PreToolUse: denied-path globs on file tools, on every Bash token and on every file a Bash glob or brace pattern expands to. Reads are allowed only inside the workspace, the run's artifact directory and the agent's plugin read roots. Bash `commands.deny` blocks credential stores (`~/.ssh`, `.aws/credentials`, ...). |
| Harness tampering (Phase 5.1) | The workspace may not equal, contain or lie inside the harness, its configuration, database, artifacts, plugins, Python environment or the Claude config directory (canonical paths; refused before an execution exists). Every tool path is resolved (symlinks, junctions, `..`) before it is compared. See [Workspace isolation](#workspace-isolation-phase-51). |
| Secret exposure | The CLI and its commands receive only allow-listed environment variables (Phase 5.1). Commands that list the environment or expand a credential variable are denied. PostToolUse masks secret patterns in every tool output (`updatedToolOutput`, same shape; verified live on Read and Grep). The result summary and inline artifacts are redacted before storage. Logs, stored errors and approval prompts go through `Redactor`. |
| Writing outside the workspace | PreToolUse: Write, Edit, MultiEdit and NotebookEdit only inside the workspace or the artifact directory. Bash: every redirection form, path operand, glob and nested command is checked (Phase 5.1); the OS sandbox confines writes where the platform has one. Agent-reported output files outside those roots are rejected. |
| Destructive shell commands | `commands.require_approval` (recursive delete, history rewrite or commit, destructive SQL or `manage.py flush/migrate`, privilege or system commands) and, since Phase 5.1, every deletion the parser recognises (`rm`, `find -delete`, `find -exec rm`, `xargs rm`, ...) → the configured approver. The default `DenyApprover` denies; `--approve interactive` asks at the console. Every request and decision is audited. |
| Network operations | `WebFetch` and `WebSearch` are denied globally. Bash network commands (curl, wget, ssh, scp, git remotes, package installs, ...) require approval. Piping a download into a shell is denied outright. No MCP servers. |
| Secrets in execution logs | The audit stores the tool name, decision, rule ids and a **redacted, 300-character summary** of the input, never full outputs, and never secret values (pattern ids only). Approval events also store the complete redacted operation (up to 32,000 characters) and its approval id. |
| Prompt injection via project files | `setting_sources=[]`: the workspace's `CLAUDE.md`, `.claude/settings*`, hooks and agents are not loaded. `verbatim_prompts`: `@path` text in the prompt is not expanded into file reads. The prompt says workspace content is data. Tool restrictions limit what injected text could make the agent do. |
| Calls from other agents | PreToolUse denies a call whose `agent_type` is not the stage's agent. Subagent spawning is not in any agent's tools. |
| Read-only or confirm-writes agents changing code (SEC-05, SEC-06) | Write modes come from `security-config.yaml` (`tools.agents[].write_mode`) and the plugin's `agent_policy`; the stricter applies. Every tool and every simple shell command is classified by effect and checked against the mode's capabilities (`write_modes`), default deny. See [Write modes and capabilities](#write-modes-and-capabilities-sec-05-sec-06). |

**Fail closed.**
- Any exception in the guard denies the call.
- Matchers use `on_failure="block"`, so a hook timeout, crash or invalid output also blocks.
- The SDK refuses to connect to a CLI that cannot honor that.
- If the plugin, skill, agent or tool-set verification fails, the run is aborted before
  the model acts on its first turn.

### Hook behaviour

| Hook | Matcher | Returns |
|---|---|---|
| PreToolUse | every tool | `{}` (no opinion: native rules still apply), or `deny` with a reason. It never returns `allow`, so a hook can never widen native permissions. |
| PostToolUse | every tool | `{}`, or `updatedToolOutput` with secrets masked. If the output cannot be scanned, it returns `decision: "block"`. |

Audit events in the execution log:

- `tool.allowed`, `tool.denied` (with `source: permissions` for native refusals),
  `tool.approval_requested`, `tool.approved` (both with `approval_id`)
- `tool.output_redacted`, `result.redacted`
- `session.tools_unavailable`, `session.unavailable`
- `session.shell` (Bash isolation mode and why), `session.environment` (names of the forwarded
  variables, never values) — Phase 5.1

The hook timeout is `agent_runtime.hook_timeout_seconds` (600 s), which covers a human
approval. An approval decision arriving later than that is refused.

**Preventive enforcement vs. detection.** The SDK hook registration (`on_failure="block"`) is what
prevents a call: the CLI runs PreToolUse before every tool call and blocks the call if the hook
fails. `HookTripwire` is a **detector**, not enforcement. It watches every session (Phase 5.1; it
used to watch only sessions with plugin hooks disabled) and aborts the run with `security.violation`
when a tool result arrives for a call the guard never saw. That call has already run. An unseen
*error* result is accepted only when the call cannot have run: the session never offered the tool
(the CLI answers "No such tool available", observed live), or the result's `permission_denials`
lists it (native refusal). Every other one aborts the run (SEC-07).

### Workspace isolation (Phase 5.1)

The harness root is trusted infrastructure, the workspace is untrusted project data, and they never
overlap (`agent_runtime/workspace.py`):

- **Protected roots:** the harness installation, the configuration root and every config file, the
  contracts, the artifacts root and the SQLite database (also the one given with `--db`), every
  registered plugin (agents, skills, `.claude-plugin`, hooks, `agent_policy` files), the Python
  environment (`sys.prefix`), the installed SDK with its bundled CLI, and the Claude config directory
  (`CLAUDE_CONFIG_DIR` or `~/.claude`).
- **Validation:** the workspace is resolved strictly to its canonical path (symlinks, Windows
  junctions, `..` and 8.3 short names resolved); a resolution error refuses it. It must be a
  directory, not a filesystem root, and must neither equal, contain nor lie inside a protected root.
  It is checked on `start`, `agent run` and again before every stage and resume
  (`workspace.invalid`, CLI exit code 4). No string-prefix comparison is used.
- **Per call:** the guard resolves every path the same way. Reads stay inside the workspace, the
  run's artifact directory and the agent's plugin read roots; writes inside the first two; Bash
  operands inside the first two. Anything inside a protected root other than those explicit roots is
  refused even if a workspace somehow overlapped. Glob patterns may not climb out with `..` or an
  absolute prefix.

### Shell execution and sandboxing (Phase 5.1)

Bash commands are **parsed** (`core/shell_syntax.py`, standard library only, shared byte for byte
with the standalone guard), not pattern-matched. The parser handles quoting (including `$'...'`
escapes), every redirection form (`N>`, `N>>`, `&>`, `&>>`, `>|`, `N<>`, `>&file`; `2>&1`, `>&-` and
null devices are recognised as harmless), here-documents, lists, pipelines, subshells, command and
process substitutions, `sh -c`, `eval`, wrappers (`env`, `sudo`, `xargs`, `timeout`, ...) and
`find -exec`. Every simple command, nested ones included, is checked. Anything it cannot analyse
with certainty (unbalanced quotes, `case`, `eval $X`, an operand built from command output, a path
built from a variable) is denied.

Command analysis is policy, not isolation. The filesystem boundary for Bash is the Claude Code CLI's
OS sandbox (`agent_runtime/sandbox.py`), configured strictly: `failIfUnavailable`, no unsandboxed
fallback (`allowUnsandboxedCommands: false`), no excluded commands, no network unless
`shell.allowed_domains` lists it, and native `Read`/`Edit` deny rules for the harness configuration,
database, plugin manifests, hooks and policies, and home credential stores.

| Platform | Bash |
|---|---|
| macOS (`/usr/bin/sandbox-exec`) | Runs inside the Seatbelt sandbox. Writes only to the workspace and the run's output directory. |
| Linux with `bwrap` and `socat` on `PATH` | Runs inside the bubblewrap sandbox, as above. |
| Linux without them | Not run unrestricted: removed from the session (`without_sandbox: disable`) or every call approved (`approve`). |
| Windows | The CLI has no OS sandbox for Bash. Removed from the session (default) or every call approved (`approve`). Read, Grep, Glob, Write and Edit stay available and are confined by the guard. |

The configured sandbox has **not** been exercised on macOS or Linux in this work (it was done on
Windows); the Linux/macOS rows describe the configuration, verified offline only. With
`without_sandbox: approve`, inline code (`python -c`, ...) and every command need a human; with a
sandbox, inline code is left to the sandbox.

### Environment isolation (Phase 5.1)

The Agent SDK starts the CLI with `{**os.environ, **options.env}` and offers no way to drop an
inherited variable. So the harness passes an `env` that overrides **every** inherited variable
(`agent_runtime/environment.py`): `environment.allow` keeps its value, `environment.credentials`
(`ANTHROPIC_API_KEY`, ...) is passed only when set, and everything else reaches the CLI and its
commands as an empty string. Variable *names* stay visible; their values do not. When a credential
is passed, the CLI is also asked to strip it from its subprocesses
(`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, a CLI feature not verified here), and the guard denies any
command that lists the environment or expands a variable matching `shell.sensitive_variables`.
A Claude Code login needs no credential variable. Verified live (Phase 5.1): the CLI authenticates
and runs with the filtered environment.

### Secret protection (Phase 5.1)

Redaction is the second line of defence. `secrets.patterns` now also covers Anthropic, GitHub,
Slack, npm and PyPI tokens, credentials in URLs (database connection strings, proxies), AWS secret
and temporary keys, Google API keys, Azure storage keys, JWTs, `*_PASSWORD=`/`*_TOKEN=`-style
environment assignments, and whole private-key blocks (previously only the `BEGIN` line).

### Approval integrity (Phase 5.1)

- The approver receives the **complete** operation (`ApprovalRequest.operation`), redacted but never
  truncated: the full command (plus the agent's description of it), a Write's full content and size,
  an Edit's old and new text. `ConsoleApprover` prints it with the working directory, the logical
  agent and its session name, the execution, stage and tool call, and the rules; control characters
  are shown escaped so a command cannot rewrite the terminal.
- Each request carries a digest of the exact tool input, agent, working directory, execution, stage,
  tool call and a fresh nonce. An approver must answer through `request.decide(...)`. A decision
  without that binding, for another request or agent, after the hook timeout, already used, or for a
  call whose input changed while waiting is refused. Default-deny is unchanged.

### Write modes and capabilities (SEC-05, SEC-06)

**Who gets a mode.** `security-config.yaml` gives the reused Phase 2 reviewers (`security-auditor`,
`secrets-scanner`, `pr-reviewer`) `write_mode: read-only`; the Django pack's modes come from its
`agent_policy` (`plugin_packaging/standalone-security.yaml`). When both name a mode, the stricter one
applies (`read-only` > `documentation-only` > `confirm-writes` > `workspace-write`), so plugin metadata
can narrow the harness policy but never widen it. Agents without a mode keep the harness defaults.
`agent inspect` shows the mode and its source, and every session records both in `session.shell`.
Enforcement is the guard's; nothing depends on the agent's instructions or declared tools.

**What a mode may do** (`security-config.yaml` `write_modes`, default deny):

| Mode | Grants | Anything else |
|---|---|---|
| `read-only` | `read` | denied |
| `documentation-only` | `read`, `write` (documentation paths only); no shell | denied |
| `confirm-writes` | `read` | approval, bound to that exact call |
| `workspace-write` | everything (harness defaults: deletions, network and `require_approval` rules still ask) | — |

**Classification by effect** (`core/shell_syntax.classify`, shared with the standalone guard). Each
tool and each simple shell command is `read`, `write`, `delete`, `execute`, `network` or `unknown`.
This applies to commands nested in `$(...)`, backticks, `<(...)`, `sh -c`, `eval`, `find -exec`
and `xargs` as well. It is an allow-list. A command is `read` only when both its program and its
options are known to be read-only:
- `git status`/`diff`/`log` count as `read`, but `git -c ...`, `--output=`, `--ext-diff`, branch or tag creation and git aliases do not.
- `sort` counts as `read`, but `sort -o` does not.
- `sed` counts as `read`, but `sed -i` and scripts with `w`/`e` commands do not.
- `awk` counts as `read`, but `print >`, `system()`, pipes and `ENVIRON` do not.
- `uniq` and `xxd` count as `read` only with a single operand.
- `manage.py check`/`showmigrations`/`sqlmigrate` and `makemigrations --dry-run` count as `read`.
- `python -m bandit` counts as `read` without `-o`, as do `pip list`/`show`/`freeze`.

Wrappers carry their own effect: `sudo`, `nohup`, `setsid` and `strace` are `execute`. Setting a
variable that changes what runs (`PATH`, `LD_*`, `BASH_ENV`, `GIT_*`, `PYTHONPATH`, ...) is `unknown`;
only an allow-list of harmless names (`LANG`, `LC_*`, `TZ`, `PYTHONDONTWRITEBYTECODE`, ...) is free.
Programs nobody listed (`make`, `npm`, `pytest`, project scripts, `manage.py` custom commands, MCP
tools) are `unknown`. Output redirection is checked as before: `read-only` refuses it, and
`confirm-writes` asks for it outside the run's output directory. The previous `read_only_commands`
block-list stays as an extra check, with its rule ids kept in deny reasons and approval requests.

On macOS and Linux, a read-only agent's session also gets a native `Edit(<workspace>/**)` deny rule.
The OS sandbox then refuses workspace writes even from code the parser cannot see, such as project
code imported by `manage.py check`. This is configured, but it has not been exercised on those
platforms.

## Result processing

- **Structured output.** The AgentResult contract is bundled into one self-contained
  draft-07 schema (`output_schema.bundle_schema`). The bundling is deterministic: cross-file
  `$ref`s are inlined into `definitions`, and `$ref` siblings are wrapped in `allOf`. Tests
  check that it accepts and rejects exactly what the contract does. The SDK receives a
  **projection** of it (`structured_output_schema`) without top-level combinators, because
  the real API rejected them (`input_schema does not support oneOf, allOf, or anyOf at the
  top level`). The projection only ever widens. The full contract, including "failed or
  blocked ⇒ at least one error", is enforced on every result afterwards. The contract
  files are unchanged.
- **Fallback.** If there is no `structured_output` (for example, `structured_output: false`),
  the processor takes the JSON object from the final text: a ```json block, else the
  outermost object. Then the same validation applies. `extensions["dae.result_source"]`
  records which path was used.
- **Harness-owned fields** are set from the task and the SDK, never trusted from the model:
  `task_id`, `agent_id`, `execution_id`, `schema_version`, `kind`, `metrics`,
  `started_at` and `completed_at`.
- **Artifacts.** The agent writes files into the run's output directory, or returns content
  in `extensions["dae.output_content"]`, which lets read-only agents produce reports. The
  processor redacts inline content, writes it under
  `var/artifacts/<execution>/<stage>/<run>/`, verifies agent-written files, and computes
  `sha256` and `size_bytes`. Only references reach SQLite. A `succeeded` result must have
  every required expected output.
- **Errors** are normalized into stable codes with retryability:

  | Code | Retryable |
  |---|---|
  | `agent.timeout`, `runtime.cancelled` | yes |
  | `agent.max_turns`, `agent.budget_exceeded` | no |
  | `result.malformed`, `result.invalid`, `result.structured_output_failed` | yes |
  | `sdk.api_error` (429, 5xx, 529) | yes |
  | `sdk.invalid_request` (other 4xx) | no |
  | `sdk.authentication`, `sdk.cli_not_found` | no |
  | `sdk.connection`, `sdk.process_error` | yes |
  | `plugin.unresolved`, `plugin.load_failed`, `agent.invalid_definition`, `security.violation` | no |

  Messages are redacted and truncated. A rejected result is stored as a contract-valid
  `failed` result.

## Timeouts

Effective timeout = min(stage `timeout_seconds` (default 1800 from `workflow-config`),
`agent_runtime.max_agent_seconds` (3600), CLI `--timeout`). A watcher task enforces it
while the SDK stream is consumed, even during a long tool call with no messages. On
timeout:

1. the SDK query is cancelled and its subprocess closed;
2. the run records `failed` / `agent.timeout` (retryable);
3. the stage fails, with a checkpoint;
4. the workflow loop retries if attempts remain.

Nothing is left `running`. The same watcher polls the store, so `execution cancel` from
another terminal stops a live agent within about a second (`runtime.cancelled`). Ctrl-C
records the run as cancelled and the stage as failed (retryable) before exiting with
code 130.

## Workflow inputs

`start_execution` validates the inputs before anything is persisted, using the existing
JSON Schema machinery (`core.contracts.schema_issues`). It checks:

- the workflow's `inputs` schema: required inputs, types, `enum` and `const`, formats;
- every input a stage needs: its `inputs:` list and any `${inputs.name}` in its objective
  or constraints.

At load time, `validate` reports `invalid-inputs-schema` and `unknown-input` (a stage
referencing an undeclared input). `${inputs.x}` is substituted into the task, and a stage
receives only the inputs it declares.

## Session recovery

Migration **v2** adds these columns to `agent_runs`:

| Column | Holds |
|---|---|
| `session_id` | the SDK session id |
| `cost_usd`, `duration_ms`, `num_turns` | run totals from the `ResultMessage` |
| `sdk_metadata` | small JSON: model, CLI version, tools, plugins, usage, terminal reason, denial count, resume link |

The migration is additive (`ALTER TABLE ... ADD COLUMN`), so v1 databases upgrade in place
with their data. Only identifiers and numbers are stored; the conversation stays in the
SDK's own session transcript.

- The session id is persisted **as soon as `init` arrives**, before the agent does any
  work, together with an `agent_session_started` checkpoint.
- `execution resume` (`ExecutionManager.resume`) marks stages still `running` as
  interrupted. Their in-progress runs become `cancelled` with error `runtime.interrupted`,
  and the interrupted attempt is refunded: retry limits count failures, not crashes.
- The next attempt of that stage resumes the SDK session (`options.resume`) only if:
  - the latest run was interrupted, has a session id, and was not already resumed;
  - its transcript still exists for the same workspace.

  Otherwise it starts fresh and logs `session.unavailable`.
- Completed, failed, timed-out or deliberately cancelled runs are never resumed. A
  succeeded stage can never be started again, because the state machine forbids it.

**Assumption:** `resume` is run after the previous process has stopped, or once ownership
of the execution has otherwise been established. There is no cross-process locking.
Resuming needs the same machine and Claude config directory, because the transcript is
local to the CLI.

## Execution records (Phase 5.3)

`runtime.records.RunRecord` is the versioned execution contract
(`contracts/run-record.schema.json`, `schema_version` 1.0). It is a read-only view built
from the store, not a second copy of the data.

### Record granularity

| Level | Stored as | Key | Meaning |
|---|---|---|---|
| Workflow execution | `executions` | `execution_id` | Overall outcome. `ExecutionManager.execution_summary()` adds metrics aggregated over all runs. |
| Stage | `stages` | (`execution_id`, `stage_id`) | The *current* state of one workflow step and its attempt counter. It is overwritten as the stage moves on. |
| Agent-stage execution | `agent_runs` | `run_id` | One row per attempt. **A RunRecord is one row.** |

Why this split: a workflow runs several agents in sequence, and each stage can be attempted
more than once (retry, or resume after an interruption). So per-attempt history has to live
in its own row. That row already existed (`agent_runs`, v1/v2), so Phase 5.3 extends it
instead of adding a parallel table.

- Retries and resumed attempts always create a new `run_id` and `task_id`. Earlier rows are
  never rewritten.
- Once a run is finished, its record is immutable (`update_run_metadata` refuses).
- A resumed attempt carries `resumed_from_run`.
- `(stage, attempt)` is *not* unique: an interrupted attempt is refunded and reused. Use
  `run_id` as the key.

### Lifecycle

Status values are the existing `AgentRunStatus` values: `queued → running → succeeded | failed | cancelled`.
No new statuses are added. `error_category` tells non-success outcomes apart:

| Outcome | `status` | `error_code` | `error_category` | `failed_at` |
|---|---|---|---|---|
| success / partial result | `succeeded` | – | – | – |
| agent / SDK / result failure | `failed` | e.g. `agent.max_turns`, `sdk.api_error` | `agent`, `sdk`, `result`, ... | `completed_at` |
| timeout | `failed` | `agent.timeout` | `timeout` | `completed_at` |
| interrupted (process died; settled by `resume`) | `cancelled` | `runtime.interrupted` | `interrupted` | `completed_at` |
| deliberate cancel | `cancelled` | `runtime.cancelled` | `cancelled` | – |

The full set of categories is: `timeout`, `cancelled`, `interrupted`, `sdk`, `agent`,
`result`, `security`, `configuration`, `approval`, `internal`.

A run whose process dies stays `running` until `execution resume` settles it as
interrupted. It is never reported as succeeded. A persistence failure while recording the
result propagates as `StorageError`, so the run is not marked succeeded.

### Timing

- `duration_ms` is the harness wall clock from `started_at` to `completed_at`.
- `sdk_duration_ms` is the value the SDK reports in its `ResultMessage`.

### Metrics

`None` means "not reported". It is never replaced by 0, and a genuine 0 is stored as 0.

| Field | Source |
|---|---|
| `input_tokens`, `output_tokens` | `ResultMessage.usage`. Without a ResultMessage (timeout, cancel, crash): usage seen on assistant messages, de-duplicated by API message id, with `metrics_source: "partial"` (a lower bound). |
| `total_tokens` | `input_tokens + output_tokens`, only when both are known. Cache read/creation tokens stay in `sdk_metadata.usage`. |
| `cost_usd`, `num_turns`, `sdk_duration_ms` | `ResultMessage` only, including error results. They are unavailable after a timeout. |

`ExecutionSummary.metrics_complete` is false when a finished run reported no cost or
tokens. In that case the totals are lower bounds.

### Schema v3 (additive)

`agent_runs` gains three columns:

- `input_tokens INTEGER`, `CHECK (>= 0)`;
- `output_tokens INTEGER`, `CHECK (>= 0)`;
- `error_code TEXT`, a query column derived from `error` in the same write.

The migration also adds indexes `ix_agent_runs_created`, `ix_agent_runs_agent`,
`ix_agent_runs_error` and `ix_executions_created`.

Existing rows are back-filled from what v2 already stored. Only integer usage values are
copied, and error codes come from `error`. v1 and v2 are unchanged, and v1/v2 databases
upgrade in place.

### Retrieval

Retrieval goes through `ExecutionStore`:

- `get_run_record(run_id)`;
- `list_run_records(execution_id=, workflow_id=, stage_id=, agent_id=, status=, error_code=, limit=, newest_first=)`.

The workflow-level view is `ExecutionManager.execution_summary(execution_id)`.

These queries never load the AgentTask or the AgentResult document, only its status.
RunRecords hold no prompt, objective, inputs, agent output or conversation.

## CLI

```text
python -m core.cli [--profile full|pack] validate | list {agents,skills,workflows}
python -m core.cli orchestrate plan "<task>" [--category ID] [--with AGENT] [--without AGENT] [--json]
python -m core.cli orchestrate run  "<task>" --workspace DIR [--category ID] [--with/--without AGENT]
        [--timeout S] [--approve deny|interactive] [--db PATH]     # Phase 6, see orchestration.md
python -m core.cli agent list
python -m core.cli agent inspect <agent> [--json]        # effective tools, denies, skills, plugins, rules
python -m core.cli agent run <agent> --objective TEXT --workspace DIR
        [--output NAME:TYPE]... [--constraint TEXT]... [--timeout S] [--approve deny|interactive] [--db PATH]
python -m core.cli execution start <workflow> --workspace DIR [--input K=V]... [--inputs-file F.json]
        [--no-run] [--timeout S] [--approve deny|interactive] [--db PATH]
python -m core.cli execution status <id> [--json]        # stages, runs, session ids, cost
python -m core.cli execution list [--status S]
python -m core.cli execution resume <id> [--approve ...] # continue after failure, crash or approval
python -m core.cli execution cancel <id> [--reason R]
python -m core.cli execution approve <id> <stage> --approver NAME
python -m core.cli execution reject  <id> <stage> --reviewer NAME --reason R
```

`agent run` is a single-stage ad-hoc workflow. `orchestrate run` (Phase 6) is a multi-stage
ad-hoc workflow planned from `config/orchestration-policy.yaml`. Both definitions are stored with
the execution, so they are resumable like any other. Every command goes through the same
`AgentRunner`, `ExecutionManager` and security hooks.

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | Validation or execution failed |
| 2 | Usage error |
| 3 | Waiting for a human (approval, or paused) |
| 4 | Invalid inputs or arguments |
| 5 | Runtime error |
| 130 | Interrupted |

## Tests

```bash
.venv/Scripts/python -m pytest -q                       # see the counts below
DAE_SDK_INTEGRATION=1 .venv/Scripts/python -m pytest -m integration -s   # real SDK, small cost
DAE_PLUGIN_LIVE=1 .venv/Scripts/python -m pytest -m plugin_live -s       # real Claude Code plugin run, small cost
DAE_TEST_PRUNE_UNAVAILABLE=1 .venv/Scripts/python -m pytest -q          # outside the full repository layout
```

**Phase 5 runs** (Python 3.13, Windows, in a standalone checkout *without* the parent repository's
`plugins/`). Only `tests/test_django_harness_integration.py` and the opt-in live file are independent
of the layout; they use `pack_registry`, which prunes registry entries absent from the checkout.

| Run | Result |
|---|---|
| Baseline before Phase 5, same checkout | 634 passed, 26 failed, 373 errors, 5 skipped. Every failure and error comes from the absent `plugins/` directory. |
| After Phase 5, same checkout | 750 passed, 26 failed, 373 errors, 9 skipped. The 26 failures are the same tests by name. The 116 new offline tests pass, and the 4 new live tests skip. |
| After Phase 5, `DAE_TEST_PRUNE_UNAVAILABLE=1`, Django, packaging and registry files | 927 passed, 14 failed, 1 skipped. 13 failures assert the pruned Phase 2 agents, skills or workflow (for example 17 agents). The 14th is the known `QuerySets` failure. |
| `DAE_SDK_INTEGRATION=1 tests/test_django_sdk_live.py` (CLI 2.1.317, agents on haiku) | 4 passed, about $0.03 |

`DAE_TEST_PRUNE_UNAVAILABLE=1` prunes the same way inside `harness_root`, so the Django tests can run
in a standalone checkout. Tests that assert the reused Phase 2 plugins then fail by design.

Latest full run (Phase 4, Python 3.13, with the project at `projects/django-agentic-engineering`
inside the repository so that `plugins/` resolves): **1032 passed, 1 failed, 5 skipped**. Before
Phase 4 the same layout gave 846 passed, 1 failed, 2 skipped. The tests must run from that layout.
Outside it, the plugin paths in the registries do not exist and most tests error. On Windows, keep
the checkout path short: under a deep folder, some skill reference paths exceed 260 characters and
four duplication tests fail with `FileNotFoundError`. The one failure,
`test_registry.py::test_local_django_skills`, predates Phase 3E and Phase 4: it expects the word
"QuerySets" in the `django-orm` skill description, which says "QuerySet internals". The opt-in
`test_plugin_live.py` passed 3/3 with `DAE_PLUGIN_LIVE=1` (CLI 2.1.319). Its read-only shell test
skips, rather than fails, when the model declines to attempt the forbidden call.

The unit tests use `tests/fakes.py`. It yields the SDK's real message dataclasses, derives
the `init` frame from the options the way the CLI does, and invokes the hook callbacks
actually registered in the options.

| File | Covers |
|---|---|
| `test_agent_runtime.py` | execution, Markdown-as-source, plugin and skill failures, invalid tasks and results |
| `test_runtime_security.py` | tool policy, paths, commands, approvals, hook failure, redaction, audit |
| `test_write_mode_capabilities.py` | SEC-05/SEC-06: effect classification (nested, wrapped, environment), Phase 2 reviewer read-only enforcement, stricter-mode resolution, read-only and confirm-writes capability gates, unknown tools and commands, approval binding and reuse, standalone guard and build |
| `test_runtime_sessions.py` | transitions, checkpoints, timeouts, cancellation, session resume, v2 migration |
| `test_run_records.py` | **Phase 5.3.** RunRecord contract, lifecycle outcomes (timeout, interruption, cancel, retry), metrics incl. missing/partial, retrieval, v2 → v3 migration |
| `test_workflow_runtime.py` | sequencing, dependencies, context restrictions, inputs, retries, approval gate |
| `test_output_schema.py` | bundle equivalence and projection |
| `test_cli_runtime.py` | CLI commands |
| `test_django_agents.py` | Phase 3B Django agents: registration (in the pack plugin since Phase 5), skills, sections, pinned tool sets, no copied skill text, runs reach the SDK |
| `test_django_harness_integration.py` | Phase 5: plugin registration and validation, CWD-independent roots, agent discovery and metadata, skill and rule resolution and harness preload, standalone/harness skill parity, templates and references, write modes, workspace boundaries, no escalation, same-named plugin agents refused, tripwire, plugin hooks kept out, workflow and runtime execution, persistence, `agent inspect` |
| `test_django_sdk_live.py` | Opt-in (`DAE_SDK_INTEGRATION=1`), real SDK: the read-only analyzer, the documentation agent writing `docs/` with skill and template, only registered rules preloaded (measured by quoting), `disableAllHooks` keeping SDK hooks |
| `test_django_rules.py` | Phase 3C rules: discovery, requirement ids, agent applicability, no copied skill text |
| `test_django_commands.py` | Phase 3D developer commands: discovery, metadata, agent/skill/rule references, no permission escalation, read-only and documentation-only limits, approval gates |
| `test_django_templates.py` | Phase 3E templates and shared references: discovery, structure, required/optional sections, placeholders, links and anchors, rule ids, no version assumptions, no copied text, agent and command mappings, unchanged tools |
| `test_plugin_packaging.py` | Phase 4 plugin: manifest, structure, agent/skill/rule/command discovery, `${CLAUDE_PLUGIN_ROOT}` template and reference paths, naming collision, the guard's decisions per agent, fail-closed behaviour, no tool escalation, no duplication, generated-file drift, staging, registry compatibility; runs `claude plugin validate` when the CLI is on PATH |
| `test_plugin_live.py` | Opt-in (`DAE_PLUGIN_LIVE=1`): loads the plugin with the installed Claude Code CLI and checks discovery, template/reference reads and guard decisions from the real session |

The opt-in `test_sdk_integration.py` runs `secrets-scanner` (haiku) on a throw-away
workspace, bounded by `max_turns`, `max_budget_usd` and a timeout. It checks structured
output, plugin loading from the real `init` frame, hooks, redaction, and that a `.env`
read is refused. Mocked tests do not demonstrate real execution; this test does.

## Configuration reference (excerpts)

```yaml
# framework-config.yaml
agent_runtime:
  max_turns: 60
  max_budget_usd: 10.0
  max_agent_seconds: 3600
  hook_timeout_seconds: 600
  structured_output: true

# security-config.yaml (tools/paths/secrets as in Phase 1, plus:)
commands:
  deny:             [{ id: dump-environment, regex: '...' }, ...]
  require_approval: [{ id: recursive-delete, regex: '...' }, ...]

# workflow stage
- id: threat-model
  agent_id: threat-modeler
  inputs: [repository]          # must be declared in the workflow's inputs.properties
  objective: Threat-model ${inputs.repository}.
```

Agents are referenced by path; `id` must equal the file's frontmatter `name`. See
`config/*.yaml` for the full files and `config/schemas/` for their schemas. Contract
design rules are unchanged:

- core fields are closed (`additionalProperties: false`), so a typo is an error;
- `ArtifactRef` never carries content;
- ids follow `^[a-z0-9][a-z0-9._-]*$`.

## Parent repository layout

The harness was built inside a larger repository and its registries still expect that layout.
`paths.repo_root` is `../..` relative to the harness root, so the four reused plugins must sit
two levels up:

```text
<parent-repository>/
├── plugins/
│   ├── security/          threat-modeler, security-sast-engineer, secrets-scanner, security-auditor
│   ├── database/          db-schema-designer, db-migration-engineer
│   ├── api/               api-contract-designer
│   └── pr-automation/     pr-reviewer
└── projects/
    └── django-agentic-engineering/   this repository (harness root and Django Engineering Plugin root)
```

Those plugins are **not** part of this repository. The Django Engineering Pack itself resolves from
the harness installation (`base: harness`) and does not depend on this layout.

Registry validation loads every entry, so under the default `full` profile no `core.cli` command
runs without the parent plugins. Since Phase 6 they are registered `optional: true` with a
`description`:

- `--profile pack` excludes them explicitly, together with their agents, skills, the workflows
  that use them, and the security-config overrides and evaluation targets that name them.
  `validate` lists every exclusion.
- The security rules for the remaining agents are unchanged.

The standalone Claude Code plugin is unaffected.

- The registries reuse reviewed agents and skills from `plugins/security`,
  `plugins/database`, `plugins/api` and `plugins/pr-automation`, by path. Nothing is copied.
- Projects Explorer scans `projects/*/agents/*.md`, every `SKILL.md`, and
  `projects/*/hooks/*.json`. This folder lists 10 agents (`agents/*.md`: nine specialists and,
  since Phase 6, the plugin's orchestrator) and 24 skill
  packages: 12 skills and the 12 rules, which the Explorer shows as skills because they share
  the `SKILL.md` format. Its READMEs are skipped because their names are ALL-CAPS. The Explorer
  does not list commands, templates or shared references, because it does not scan those folders.
  Since Phase 4 it lists two "hooks": `hooks/hooks.json` (the plugin's hook configuration) and
  `hooks/security-policy.json` (the guard's generated policy).
- The plugin follows the parent repository's plugin conventions (`.claude-plugin/plugin.json` with
  `name`, `version`, `description`, `author`, `keywords`; `agents/` and `skills/` folders;
  `${CLAUDE_PLUGIN_ROOT}` for paths). It adds `commands/`, `rules/` and `hooks/`. When this project
  lives inside a repository that has its own `.claude-plugin/marketplace.json`, that marketplace can
  list the plugin with a relative `source` pointing at this directory.

## Repository-local harness (Phase 5.6)

Users do not clone this repository to use the harness. In their own project they run
`/django-engineering:setup-harness` in Claude Code; the plugin's bootstrap
(`harness_bootstrap/`, standard library only) installs this harness *into that repository* and the
plugin's `/orchestrate` then runs through it. The harness itself is unchanged: the same
`core.cli`, `ExecutionManager`, `AgentRunner`, ToolGuard, approvals, checkpoints, SQLite store and
RunRecords. Only two harness changes were needed, both described below: an optional
`paths.enclave` and the workspace rule that goes with it.

### Components

| Module | Role |
|---|---|
| `harness_bootstrap/django_harness.py` | Entry point the plugin commands call: `python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" <subcommand>` |
| `harness_bootstrap/project.py` | Project-root detection: Git work-tree root (`.git` directory or file), Django markers, existing installations; refuses home, filesystem roots, the Claude config directory and the plugin itself; asks when a monorepo makes it ambiguous |
| `harness_bootstrap/mode.py` | The single execution-mode resolver (`standalone`, `harness_enabled`, `harness_disabled`, `harness_invalid`). Reads files only; never imports or starts the harness |
| `harness_bootstrap/distribution.py` | The harness distribution shipped in the plugin, its SHA-256 digest, versioned runtime snapshots and their verification |
| `harness_bootstrap/environment.py` | Read-only checks: Python 3.11+, venv/ensurepip, pip, disk, write permission, Windows path length, credentials (names only), OS sandbox |
| `harness_bootstrap/provision.py` | Plan, consent binding (`plan_id`), idempotent setup, repair, upgrade, validation, enable, disable |
| `harness_bootstrap/adapter.py` | Runtime adapter: plan, run, list, status, resume, approve, reject, cancel, result, each a `python -m core.cli` call of the repository's runtime |
| `harness_bootstrap/probe.py` | Runs inside the repository's virtual environment: writes the repository configuration, opens the database through `runtime.store.open_store` (migrations), readiness, results |

No MCP server is used. Every operation is a bounded command the plugin command runs through
Claude Code's Bash tool (with Claude Code's own permission prompt), and a long-running execution
is a background command. An MCP server would add a persistent process without adding a capability.

### Layout and configuration

```text
<project>/.django-engineering/
  installation.json                  manifest (schema 1): versions, relative paths, profile, state, history
  .gitignore                         everything ignored except itself and config/framework-config.yaml
  config/framework-config.yaml       derived once from the snapshot's canonical framework-config.yaml
  runtime/<version>-<digest8>/       snapshot: core/ runtime/ agent_runtime/ config/ contracts/ workflows/
                                     agents/ commands/ skills/ rules/ templates/ references/ hooks/
                                     harness_bootstrap/ + plugin.json, standalone-security.yaml,
                                     requirements-harness.txt, RUNTIME.json (SHA-256 of every file)
  database/harness.db  database/backups/
  artifacts/  logs/  .venv/
```

The adapter runs `<.venv python> -X utf8 -E -s -m core.cli --root runtime/<snapshot>
--framework-file .django-engineering/config/framework-config.yaml --profile <profile> ...` with the
snapshot as working directory, so the code, registries, policies and agents come from the
snapshot, never from the plugin cache or `PYTHONPATH`. The repository configuration's relative
paths resolve against the snapshot: `configs:` loads the canonical registries, security
configuration and orchestration policy; `../../database/harness.db`, `../../artifacts` and
`paths.enclave: ../..` point into `.django-engineering/`. The default profile is `pack`; `full`
keeps its current behaviour and validation, and its optional parent-repository plugins are never
copied (provide them under `.django-engineering/plugins/` if you need them).

### Execution modes

| Mode | When | `/orchestrate` |
|---|---|---|
| `standalone` | no `.django-engineering/installation.json` at or above the directory (up to the Git root) | standalone plugin, unchanged |
| `harness_enabled` | `status: installed`, `enabled: true`, files present, project not moved, runtime version and digest equal to the plugin's distribution | the harness |
| `harness_disabled` | `enabled: false` and `disabled_by_user: true` | standalone plugin; history kept |
| `harness_invalid` | anything else: `setup-interrupted`, `setup-failed`, `manifest-unreadable`, `manifest-invalid`, `missing-file`, `missing-runtime`, `relocated`, `version-mismatch`, `runtime-drift` | stops with the problem and its remedy; never falls back |

### Setup, repair and upgrade

`plan` changes nothing and returns a `plan_id`, a digest of the project, its state, the
distribution, the interpreter, the profile and every action. `setup --approve-plan <id>`
recomputes the plan and refuses (exit 12) if the id differs. Then:

1. lock (`.setup.lock`, exclusive), manifest `provisioning` with `enabled: false`;
2. directories and `.gitignore` (only when absent);
3. runtime snapshot: copied into a temporary sibling, verified, renamed; an existing snapshot is
   verified and reused, never overwritten;
4. virtual environment: created with `python -I -m venv` when absent, otherwise reused; never deleted;
5. pinned dependencies: installed with `pip install -r requirements-harness.txt` from the snapshot,
   only if not already satisfied, with `PIP_REQUIRE_VIRTUALENV=1` (output logged, URL credentials redacted);
6. configuration: created only when absent;
7. database: opened through the harness's own migrations; once a database existed it must still
   exist (`--new-database` is the explicit way to start an empty history); an upgrade first copies
   it to `database/backups/` with SQLite's backup API;
8. validation: `core.cli validate` and the readiness probe (pinned versions, SDK import, registry,
   orchestration policy, `paths.enclave`, database schema and integrity);
9. manifest `installed`, enabled unless the user had disabled it.

A failed step records `status: failed` and `last_error` (an upgrade keeps the previous runtime),
an interruption leaves `provisioning`; the next setup resumes. Setup on a valid installation only
validates. `--upgrade` is required whenever the plugin's distribution differs; a downgrade is refused.

### Security in harness mode

* **SEC-04 with an enclave.** `paths.enclave` is a protected root. `validate_workspace(...,
  enclave=...)` accepts a workspace that contains protected roots only when it is exactly the
  enclave's parent and every contained root lies inside the enclave; a workspace inside the
  enclave, above the project, or containing any other protected root is still refused, and without
  `paths.enclave` nothing changes. The ToolGuard already denies every path inside a protected root
  except the run's output directory (writes) and the pack's read roots (reads).
* **OS sandbox.** For sandboxed sessions (macOS, Linux) the runner adds native deny rules: `Read`
  and `Edit` for the enclave's manifest, configuration, database and logs, and `Edit` for the runtime
  snapshot and the virtual environment. `artifacts/` stays writable (it holds the run's output
  directory), so a sandboxed command could change another run's artifacts; their SHA-256 is in the
  database.
* **Standalone guard.** `plugin_packaging/standalone-security.yaml` `protected_paths` adds
  `**/.django-engineering` and `**/.django-engineering/**` to the guard's denied paths, so pack
  agents in standalone mode (for example with the harness disabled) can neither read nor change the
  installation.
* **Approvals from Claude Code.** `run` and `resume` always pass `--approve deny`: a Bash call has no
  terminal for the console approver. Tool calls that need approval are denied and recorded
  (`result` lists them); stage approval gates wait (exit 3) for `execution approve <id> <stage>
  --by <name>`, which the command runs only after the user approved in the conversation.
* **Interpreter and paths.** The adapter only ever runs the layout's `.venv` interpreter with
  `-E -s`; the manifest's paths must equal the layout's and stay inside `.django-engineering/`.
  Nothing from the manifest or configuration is executed as a command. Interpreter probes run with
  `-I`, so nothing in the project directory can shadow a module.

### Tests

`tests/test_harness_bootstrap.py` (56 tests) covers default standalone behaviour, detection, the
resolver, setup (clean, repeated, plan binding, missing Python, missing venv/pip, pip failure with
redaction, database failure, permission denial, interruption and resume, conflicts, adopted team
configuration, missing database), upgrades (configuration, history and backups preserved, disabled
state kept, no downgrade), enable/disable, the adapter, and harness execution from a
repository-local installation: the existing engine and store, ToolGuard protection of the enclave,
recorded approval denials, checkpoint and resume, sandbox deny rules, status and doctor. Setup
tests simulate the virtual environment and pip; everything else runs for real.

## Known limitations

- **Repository-local harness (Phase 5.6).** Approvals of individual tool calls are not available
  from Claude Code (always denied and recorded; stage gates work). Upgrading reinstalls the pinned
  packages in the existing virtual environment, so a failed upgrade can leave packages of the newer
  version next to the previous runtime. A project moved to another path is reported as `relocated`
  (the virtual environment holds absolute paths). The plugin's own interpreter must be called
  `python`, `python3` or `py`. Verified end to end on Windows (Python 3.13); Linux and macOS are
  covered by the offline tests only.

- **Pre-existing content columns (unchanged by Phase 5.3).** These v1 columns still store
  documents:
  - `executions.inputs` holds the workflow inputs;
  - `agent_runs.task` holds the AgentTask, including the rendered objective, which resume needs;
  - `agent_runs.result` holds the AgentResult summary and references.

  All three are size-capped and secret-redacted. RunRecords never expose them.
- **Partial metrics are a lower bound.** After a timeout or crash, tokens come only from the
  assistant messages seen before the stream stopped. Cost and turns stay unavailable.
- **Stage-preparation failures have no RunRecord.** Examples are an unresolved plugin or an
  invalid task. The run never started, so the failure is on the stage row and in the execution log.

- **Harness-local skills still cannot be loaded.** The SDK loads skills through plugins, so a
  `source: local` skill fails with `plugin.unresolved`. Since Phase 5 no shipped skill is local:
  the Django skills and rules belong to the registered `django-engineering` plugin.
- **Phase 2 plugin agents and the CLI (fixed in Phase 5.1).** Two behaviours of CLI 2.1.317 were
  measured in Phase 5: (1) a session run *as* an agent (`--agent`) does not preload
  `AgentDefinition.skills`; (2) when a loaded plugin ships an agent file with the same name as the
  harness's programmatic agent, the CLI runs the plugin's file and drops the harness definition.
  Since Phase 5.1 every plugin entry is `preload_skills: true` and every agent runs as `dae-<id>`,
  so the eight reused Phase 2 agents now receive their registered SKILL.md files in full (AGT-01)
  and always run the harness definition (AGT-02; verified live with `secrets-scanner`: the session
  ran `dae-secrets-scanner` and returned `StructuredOutput`, which only the harness definition adds).
  No Phase 2 plugin file was changed.
- **Harness preloading costs context.** Every agent's registered skills and rules are injected in
  full: about 46 KB for the analyzer up to 141 KB for the code-review agent.
- **`load_hooks: false` relies on verified CLI behaviour.** `disableAllHooks` in the session's flag
  settings stops plugin command hooks and keeps SDK callback hooks (CLI 2.1.317; pinned by
  `tests/test_django_sdk_live.py`). If a CLI ever also stopped SDK hooks, the HookTripwire (every
  session) aborts the run at the first tool result that bypassed the guard. It is a detector: that
  call has already executed.
- **Running outside the full repository layout.** The shipped registries reuse `plugins/` from the
  parent repository. Without it, `validate` and every CLI command fail on those entries, as before
  Phase 5. The Django pack itself resolves from the harness installation (`base: harness`).
- **Shell analysis is static (Phase 5.1).** Commands are parsed, and anything that cannot be
  analysed is denied, but static analysis cannot know runtime values: a plain `$var` operand
  (`cat $f` in a loop), a script file the agent wrote and then runs, `python script.py`, or
  `sed`/`awk` programs that write files are not bounded by the guard. That is why Bash runs only
  inside the OS sandbox, and is removed or approval-gated where there is none.
- **Write-mode classification is static too (SEC-05, SEC-06).** Read-only and confirm-writes use a
  default-deny allow-list of read operations, so an unrecognised command is refused or approval-gated,
  never silently allowed. Some operations classified `read` still run code or touch state the parser
  cannot see:
  - `manage.py check`/`showmigrations`/`sqlmigrate` import the project's own code.
  - `git status` refreshes `.git/index`.
  - Repository configuration in the workspace (`core.fsmonitor`, `diff.external`, textconv filters)
    can make `git status`/`diff` run programs.
  - `bandit` and `pip` read their own configuration.

  On macOS and Linux the OS sandbox, with a native workspace write-deny rule for read-only agents,
  is the boundary for those; that rule is configured but has not been exercised there. On Windows,
  Bash is disabled by default, and with `without_sandbox: approve` every command needs a human anyway.
  `workspace-write` is unchanged: unknown commands run there, inside the sandbox.
- **The OS sandbox is configured, not exercised here.** The macOS and Linux sandbox configuration
  is tested offline only; this work ran on Windows, where Bash is disabled by default.
- **Environment variable names stay visible.** The SDK cannot remove an inherited variable, so
  unlisted variables reach the CLI with empty values rather than not at all.
- **Symlink tests need a privilege on Windows.** Creating a symlink needs Developer Mode or
  `SeCreateSymbolicLinkPrivilege`; without it those tests skip, and the junction variants (no
  privilege needed) cover the same resolution logic.
- **Approval is per call and synchronous.** Interactive approval needs a terminal; there
  is no queue, web UI or remote approver.
- **Redaction is pattern-based.** Only the configured `secrets.patterns` are masked. If the
  CLI rejected an `updatedToolOutput` shape, the original output would reach the model;
  this was verified to work for Read and Grep, not for every tool.
- **Bash on Windows** is disabled by default since Phase 5.1 (no OS sandbox). With
  `shell.without_sandbox: approve` it runs with per-call human approval, and needs
  `CLAUDE_CODE_GIT_BASH_PATH` when Git Bash is not found.
- **Undeclared `tools`.** An agent with no `tools` frontmatter gets no auto-approved
  tools under `dontAsk`. Declare tools for every agent.
- **No cross-process coordination.** One runner per execution. `resume` assumes the
  previous process is gone. The cancel watcher polls the store about once a second.
- **Session resume is local.** It needs the CLI transcript on the same machine and config
  dir. `ClaudeAgentOptions.session_store` could mirror it remotely.
- **Stages run sequentially**, even independent ones such as `secrets-scan` and `sast`.
- **Context.** Token estimates are approximate (characters ÷ 4). There is no summarizer
  implementation. References are loaded by the agent with Read, so their files must be
  readable inside the permitted roots.
- **Developer commands (Phase 3D)** run through the `django-engineering` plugin, namespaced
  (`/django-engineering:django-security`); a command stops if its agent is missing. Outside the
  harness, the harness hooks and approver do not apply. Commands rely on their approval gates,
  the agents' own `tools`, the plugin guard and Claude Code's permission prompts. Projects
  Explorer does not list commands.
- **Templates and shared references (Phase 3E).** Since Phase 5, harness runs read them at the
  plugin root the harness substitutes for `${CLAUDE_PLUGIN_ROOT}`. They are offered as task-context
  references and read on demand. A template defines structure only: nothing checks an agent's
  actual reply against it, which is left to evaluation.
- **Evaluation** criteria are validated, but no scorer runs yet.
- **SQLite only**; the `ExecutionStore` protocol is ready for PostgreSQL.

## Possible next steps

Not implemented. Recorded from earlier planning; the Django agents and skills it once listed
first are done.

1. **Harness-local skills.** Ship the harness itself as a plugin so `source: local` skills load.
2. **Evaluation runner.** Rule scorers (contract compliance, expected outputs,
   no-secrets-in-artifacts) and an LLM-judge scorer writing `EvaluationResult`s per run.
3. **Stronger Bash isolation.** Enable the SDK sandbox on Linux and macOS CI runners, or
   run stages in a container or worktree. Then relax the command regexes into an
   allowlist.
4. **Parallel stages and an approval service.** Run independent runnable stages
   concurrently with per-stage leases, and add an approval queue (CLI/web) instead of a
   blocking console prompt.
5. **Shared state.** A PostgreSQL `ExecutionStore`, and a `SessionStore` mirror so
   executions can resume on another host.
6. **Promote `security-review` to `active`** after a full live run on a real Django
   codebase, and add the feature-delivery and modernization workflows.
