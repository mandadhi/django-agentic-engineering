---
project: django-agentic-engineering
abstract: How the Django Orchestrator routes requests to the specialist agents in the standalone plugin and in the harness, from one canonical orchestration contract, and how the harness is set up and run with explicit profiles.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, YAML, Claude Code, Claude Agent SDK]
domain: [Software Engineering, AI Agents]
---

# Orchestration reference (Phase 6)

You don't have to pick one of the nine Django specialists yourself. Hand the request to the
**Django Orchestrator**. It classifies the request, selects the smallest route of specialists,
runs them in order and reports honestly what was done and verified.

There are two orchestrators, one for each execution mode. Both follow the same **canonical
orchestration contract**, `config/orchestration-policy.yaml`.

| | Standalone plugin | Harness |
|---|---|---|
| Orchestrator | `agents/django-orchestrator.md`, a Claude Code agent (model judgement) | `core/orchestration.py`, a deterministic planner (no model call) |
| Entry points | `/django-engineering:orchestrate <request>`; `claude --agent django-engineering:django-orchestrator`; or ask for the orchestrator by name | `python -m core.cli orchestrate plan\|run "<task>"` |
| How it reads the contract | Generated, drift-checked routing section in the agent file, plus a delegation allowlist in the guard policy | Loaded from `framework-config` `configs.orchestration` and validated with the registry |
| Classification | The agent judges the request, using the policy's signals as hints | Signal scoring. Empty, unclassified or ambiguous tasks are refused (`--category` resolves them) |
| Execution | Claude Code delegation (`Agent` tool), one specialist per step | One workflow execution of the existing `ExecutionManager`/`AgentRunner`: one stage per step |
| Bounds | Delegation allowlist enforced by the guard. Delegation count and retries are instructions only | `max_delegations`, `max_attempts_per_step` and stage dependencies enforced by code |
| Security | Plugin guard (`hooks/django_pack_guard.py`) | Harness ToolGuard, approvals, workspace isolation |
| Persistence | None | Executions, stages, agent runs, RunRecords (SQLite) |

## The canonical contract

`config/orchestration-policy.yaml` (schema `config/schemas/orchestration-policy.schema.json`)
defines:

- `orchestrator`: logical identity `django-orchestrator`, its agent file, its command and the
  skills it preloads (`django-security-standards`).
- `limits`: `max_delegations` (6), `max_attempts_per_step` (2, so one retry) and
  `max_clarifying_questions` (3).
- `specialists`: the only delegation targets. Validation requires exactly the registered pack agents.
- `categories`: task classification (`signals`) and the `route` for each category. Steps run in
  order. `after` may name earlier steps only. `optional` steps carry a `when` condition (plugin)
  and `signals` (harness). `requires_any` asks for at least one implementation step.
  `supplementary_skills` names pack skills that no agent preloads.
- `step_constraints`, `completion_criteria`, `clarify_when`, `failure_policy` and
  `security_boundaries`.

The contract does **not** describe what a specialist does: that stays in `agents/<id>.md`. Each
specialist's skills and rules stay in `config/agent-registry.yaml`.

| Category | Route (*optional* steps only when needed) |
|---|---|
| `project-analysis` | project-analyzer |
| `architecture-design` | project-analyzer → architecture-agent |
| `data-model` | *architecture-agent* → model-agent |
| `api-development` | *model-agent* → api-agent |
| `security-assessment` | security-agent → *api-agent* |
| `testing` | testing-agent |
| `performance` | performance-agent → *model-agent* |
| `documentation` | documentation-agent |
| `code-review` | code-review-agent |
| `feature-delivery` | project-analyzer → *architecture-agent* → *model-agent* → *api-agent* → testing-agent → code-review-agent (at least one of model/api) |

### Validation and drift

`python -m core.cli validate` and `python -m plugin_packaging check` both reject:

- unknown or missing specialists;
- the orchestrator appearing as a specialist or a route step (recursion);
- `after` references to the step itself or to a later or missing step;
- duplicate category, constraint or canonical ids;
- an orchestrator id that collides with a registered harness agent;
- routes longer than `max_delegations`;
- unknown skills.

`plugin_packaging check` also fails when any of these drifts from the policy:

- the generated section of `agents/django-orchestrator.md`;
- the orchestrator's `skills:` line;
- the delegation allowlist in `hooks/security-policy.json`.

## Standalone plugin

```text
/django-engineering:orchestrate Add an invoices API with its model and tests
/django-engineering:orchestrate --plan-only Why is the orders list slow?
claude --agent django-engineering:django-orchestrator      # the orchestrator as the main agent
```

**How the command runs.** `/django-engineering:orchestrate` is the deterministic entry point. It
briefs the `django-orchestrator` subagent. According to the Claude Code documentation, a subagent
may spawn subagents only when `Agent` is in its tools and the depth limit allows it. The orchestrator declares
`Agent`, but neither the plugin nor the user's settings guarantee nested delegation.

When the orchestrator cannot delegate, it returns a **ROUTING PLAN** instead. The command then
tells the main session to run the plan with the `Agent` tool, within the same limits.

**Main-agent mode.** With `claude --agent django-engineering:django-orchestrator`, the
orchestrator is the main agent and delegates directly. This was verified live with Claude Code
2.1.322.

**Natural language.** A request such as *"use the Django orchestrator to …"* usually reaches the
orchestrator through its description. Claude Code chooses agents itself, so this is **not
guaranteed**. Use the command when you need predictable routing.

**Guard.** The plugin guard treats the orchestrator as `read-only`, so it never writes files and
its shell use is restricted. Its `Agent` calls are allowed only when `tool_input.subagent_type`
names one of the nine specialists (`django-engineering:<id>` or the bare id). Calls to itself,
`general-purpose`, other plugins' agents or an empty target are denied.

Specialists do not declare `Agent`, so they cannot start other agents. Each delegated specialist
remains under its own write mode. The number of delegations and retries is bounded by the
orchestrator's instructions and by Claude Code's depth limit, not by the guard.

## Harness

```bash
python -m core.cli --profile pack orchestrate plan "Add a DRF endpoint for invoices"
python -m core.cli --profile pack orchestrate plan "Document the API" --category documentation --json
python -m core.cli --profile pack orchestrate run "Write tests for the billing app" \
    --workspace ../my-django-app --approve interactive
```

**`plan`** prints the category and every step: its dependencies, write mode, registered skills
and why it was selected. It runs nothing and needs no SDK.

**`run`** turns the route into a `WorkflowDefinition` (id `orchestrate.<category>`, version = the
policy version). It runs through `ExecutionManager.start_execution(definition=...)` and
`AgentRunner`, the same path as `agent run`. Each step is a stage that:

- runs as `dae-<id>` with its registered skills, loaded once;
- carries the policy's step constraints and `max_attempts_per_step`;
- depends on the previous step and receives its output as upstream context.

Everything that applies to any workflow applies here: ToolGuard, write modes, approvals,
workspace isolation, retries, checkpoints, resume and RunRecords. Failed steps stay failed.
Dependent steps are reported as `not run`.

The ad-hoc definition is stored in the execution metadata (`dae.workflow_definition`) together
with `dae.orchestration` (category, agents, policy version), so `execution status` and
`execution resume` work as usual.

**Exit codes:** 4 when a task cannot be routed (`empty-task`, `unclassified`, `ambiguous`,
`invalid-selection`, `missing-implementation`, `too-many-steps`); otherwise those of
`execution start`.

## Harness setup and profiles

The guided launcher uses only the standard library. Running it with no Python packages installed
is safe:

```bash
python scripts/harness.py setup --profile pack       # asks before installing; --yes to accept
python scripts/harness.py check --profile pack
python scripts/harness.py run   --profile pack -- orchestrate plan "Review my branch before merge"
```

`setup` does the following:

1. Requires Python ≥ 3.11.
2. Creates `.venv` if it is absent. An existing environment is reused; a non-environment
   directory is refused. Nothing is deleted.
3. Lists the pinned packages in `requirements-harness.txt` and installs them into that
   environment only, after you confirm. It uses your configured pip index and executes no remote
   scripts. Transitive dependencies are resolved by pip.
4. Runs `check`: dependencies, then `core.cli validate` for the profile.

`run` never falls back to another interpreter or to the standalone plugin. It was verified from
an empty virtual environment on Windows with Python 3.13; Linux and macOS have not been tested.

| Profile | Selects | Use it when |
|---|---|---|
| `full` (default) | Every registered plugin is required, as before Phase 6 | The repository sits at `<parent>/projects/django-agentic-engineering` with the parent `plugins/` |
| `pack` | Excludes the plugins registered `optional: true` (`security`, `database`, `api`, `pr-automation`), plus their 8 agents and 9 skills, the `security-review` workflow, and the security-config overrides and evaluation targets that name them | Independent clone; Django Engineering Pack only |

Select a profile with `--profile` (on `core.cli` or the launcher) or `DAE_HARNESS_PROFILE`.
`validate` prints every exclusion.

The `django-engineering` plugin is mandatory in every profile. If it is missing, validation fails
with "mandatory for the harness". If an optional plugin is missing under `full`, the error names
what that plugin provides and how to exclude it explicitly.

The `pack` profile changes no security setting for the agents that remain: tool denies, path
denies, command rules, secret patterns, shell, environment and write modes are identical.

## Security boundaries

- The plugin guard and the harness ToolGuard are separate implementations and are not
  equivalent. The harness never loads the plugin's hooks (`load_hooks: false`).
- Neither orchestrator grants tools, paths or write modes. A specialist runs exactly as it does
  when it is called directly.
- Destructive, network and production-affecting operations still need the user's approval through
  the active mechanism: Claude Code's permission prompt or the harness approver (default: deny).

## Limitations

- **Standalone bounds.** Delegation count and retries are not enforced by code. The guard enforces
  only the targets.
- **Natural-language routing.** Claude Code may or may not pick the orchestrator from a plain
  request. The command is the deterministic entry point.
- **Model judgement.** Plugin routing depends on the model's judgement. In a live haiku run, the
  orchestrator's report also recommended a specialist that does not exist. Prefer a stronger
  model for orchestration (`model: inherit` uses the session model).
- **Harness classification.** Keyword-based: requests that fit several categories equally are
  refused instead of guessed. Pass `--category`, `--with` or `--without`.
- **Sequential harness routes.** Stages run one at a time. The policy has no parallel harness
  routes.
