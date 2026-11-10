# Django Agentic Engineering

![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)

Reusable Django engineering agents, skills, rules, slash commands, output templates and
references for [Claude Code](https://docs.claude.com/en/docs/claude-code), packaged as the
**Django Engineering Plugin**, plus an optional Python execution harness, the
**Django Agentic Harness**.

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Orchestrator](#the-django-orchestrator) · [Agents](#available-agents) · [Commands](#available-commands) · [Skills and rules](#skills-and-rules)
- [Requirements](#requirements)
- [Harness setup](#harness-setup)
- [Usage examples](#usage-examples)
- [Security model](#security-model)
- [Project structure](#project-structure)
- [Development and contribution](#development-and-contribution)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Overview

General-purpose coding agents know Django, but they don't share one standard for it. They don't
consistently check migration safety, queryset scoping in DRF views, N+1 queries or production
settings, and their reports come out in a different shape every time. This project puts that
standard into versioned, reviewable files:

- **The Django Engineering Pack** is the single source of Django engineering knowledge. It has
  nine specialist agents with one role each, a Django Orchestrator that routes requests to them, the skills they preload, mandatory rules with stable requirement
  ids (for example `SEC-M4`), report templates, and shared checklists.
- **The Django Engineering Plugin** is that pack as a standalone Claude Code plugin. You install
  it from this repository and use it in any Django project, with no Python setup beyond a `python`
  interpreter for its security hook.
- **The Django Agentic Harness** is an optional Python runtime. It runs the same canonical agents
  through the Claude Agent SDK inside versioned workflows. It adds its own tool guard, approvals,
  audit logging and persisted execution records.

The plugin and the harness are **two separate ways to use the same pack**. You can install the
plugin without ever installing or running the harness.

## Features

| | Count | Where |
|---|---|---|
| Django Orchestrator | 1 | [`agents/django-orchestrator.md`](agents/django-orchestrator.md), routing contract [`config/orchestration-policy.yaml`](config/orchestration-policy.yaml) |
| Django specialist agents | 9 | [`agents/`](agents/README.md) |
| Django engineering skills | 12 | [`skills/`](skills/README.md) |
| Django engineering rules | 12 | [`rules/`](rules/README.md) |
| Slash commands | 10 (9 specialist commands + `/orchestrate`) | [`commands/`](commands/README.md) |
| Output templates | 12 | [`templates/`](templates/README.md) |
| Shared references | 7 | [`references/`](references/README.md) |

- **Standalone Claude Code plugin** (`django-engineering`, version 0.5.0) with a fail-closed
  `PreToolUse`/`PostToolUse` security guard.
- **Orchestration in both modes** from one canonical contract: the plugin's
  `/django-engineering:orchestrate` command and the harness's `core.cli orchestrate plan|run`
  select the smallest set of specialists for a request. See [docs/orchestration.md](docs/orchestration.md).
- **Optional Django Agentic Harness**:
  - workflow execution and orchestration (dependencies, retries, approval gates, checkpoints, resume);
  - one Claude Agent SDK query per workflow stage;
  - a ToolGuard with per-agent write modes, workspace isolation and secret redaction;
  - execution metadata and `RunRecord` persistence in SQLite.

## Architecture

Both modes read the same canonical files: `agents/`, `skills/`, `rules/`, `commands/`,
`templates/` and `references/`. Both route requests by the same canonical orchestration contract,
`config/orchestration-policy.yaml`. Nothing is copied between them.

### Mode A: Standalone Claude Code plugin

```text
Developer
   |
   v
Claude Code
   |
   v
Django Engineering Plugin  (.claude-plugin/plugin.json, repository root)
   |
   +--> Orchestrator  agents/django-orchestrator.md   routes to the specialists (/orchestrate)
   +--> Agents        agents/*.md            9 specialist subagents, django-engineering:<id>
   +--> Skills        skills/*/SKILL.md      12, preloaded per agent
   +--> Rules         rules/*/SKILL.md       12, loaded as skills
   +--> Commands      commands/*.md          10, /django-engineering:<command>
   +--> Templates     templates/             read at ${CLAUDE_PLUGIN_ROOT}/templates/
   +--> References    references/            read at ${CLAUDE_PLUGIN_ROOT}/references/
   +--> Standalone security hooks  hooks/hooks.json -> hooks/django_pack_guard.py
```

Claude Code runs the agents itself. The plugin does not use the Python harness, its
database, its execution engine or its persistence layer.

### Mode B: Django Agentic Harness

```text
Developer
   |
   v
Django Agentic Harness  (python -m core.cli)
   |
   +--> Orchestration         core/orchestration.py (routes -> one workflow execution)
   +--> Workflow engine       runtime/execution.py (ExecutionManager)
   +--> Agent runtime         agent_runtime/ (one Agent SDK query per stage)
   +--> Context management    core/context.py
   +--> Tool guard            agent_runtime/security_hooks.py, core/security.py
   +--> Persistence           runtime/sqlite_store.py (SQLite, RunRecords)
   |
   v
Canonical Django Engineering Pack  (registered as plugin "django-engineering")
```

The harness registers the pack in `config/agent-registry.yaml` and enforces its own security
model. It does **not** load the plugin's standalone guard (`load_hooks: false`). The plugin, in
turn, provides none of the harness's workflow, approval or persistence features.

Detailed references:
- [docs/plugin.md](docs/plugin.md): packaging, standalone guard, verification and limitations.
- [docs/harness.md](docs/harness.md): execution, security model, persistence, CLI and tests.

## Installation

Installing the Django Engineering Plugin does not require the harness.

### Install from GitHub

Replace `<GITHUB_USERNAME>` and `<REPOSITORY_NAME>` with the GitHub account and repository that
host this project. Run the commands inside Claude Code:

**Step 1: Add the repository as a plugin marketplace**

```text
/plugin marketplace add <GITHUB_USERNAME>/<REPOSITORY_NAME>
```

**Step 2: Install the plugin**

```text
/plugin install django-engineering@django-agentic-engineering
```

`django-agentic-engineering` is the marketplace name declared in
[`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json). It stays the same even if
the repository is forked or renamed, so this step needs no placeholder.

Or run the same steps from a terminal:

```bash
claude plugin marketplace add <GITHUB_USERNAME>/<REPOSITORY_NAME>
claude plugin install django-engineering@django-agentic-engineering
```

Start a new Claude Code session (or run `/reload-plugins`) so the plugin loads.

### Verify the installation

```bash
claude plugin list          # django-engineering@django-agentic-engineering, enabled
```

In a session:

- `/agents` lists ten `django-engineering:` agents: the orchestrator and nine specialists.
- Typing `/django-engineering:` lists the ten commands, including `/django-engineering:orchestrate`.
  Skills and rules appear as `django-engineering:<skill>`.

`claude plugin details django-engineering@django-agentic-engineering` reports **Agents (0)**.
This is a known display quirk for agents listed file by file in `plugin.json`. The session itself
loads all ten.

### Update

```bash
claude plugin marketplace update django-agentic-engineering
claude plugin update django-engineering@django-agentic-engineering
```

Restart the session afterwards. Maintainers: bump `version` in `.claude-plugin/plugin.json` for
every release so that `plugin update` picks up the change.

### Disable or uninstall

```bash
claude plugin disable django-engineering@django-agentic-engineering     # keep it installed
claude plugin uninstall django-engineering@django-agentic-engineering
claude plugin marketplace remove django-agentic-engineering             # optional
```

### Local development (no installation)

From a clone of this repository, load the plugin for one session straight from the working copy:

```bash
claude --plugin-dir .
```

The plugin directory is the repository root. Use this while you edit agents, skills, rules or
commands. For normal use, install from GitHub as shown above. To build a clean, self-contained
plugin bundle without the Python sources, see `plugin_packaging stage` in
[docs/plugin.md](docs/plugin.md#install-and-use).

## The Django Orchestrator

You don't need to know which specialist to use. Give the request to the orchestrator:

```text
/django-engineering:orchestrate Add an invoices API with its model and tests
/django-engineering:orchestrate --plan-only Why is the orders list slow?
```

The orchestrator classifies the request and picks the smallest route from the policy. For
example, *"Add a DRF endpoint"* goes to the API agent alone, while *"implement X end to end with
a new model and an endpoint"* goes analyzer → model → API → testing → code review.

It then briefs each specialist, reconciles the results against your original request, and
reports what was done, what was verified and what is still open. It asks a clarifying question
only when an ambiguity would change the result or its safety.

- **Deterministic entry point:** `/django-engineering:orchestrate`.
- **Orchestrator as the main agent:** `claude --agent django-engineering:django-orchestrator`.
- **Natural-language requests:** a plain request like *"have the Django orchestrator review this
  branch"* usually reaches it too. Claude Code picks agents itself, so this is not guaranteed.

The orchestrator itself is read-only. The plugin guard lets it delegate **only** to the nine
specialists, never to itself, and each specialist keeps its own write mode and approval gates.
Details, limits and the harness equivalent: [docs/orchestration.md](docs/orchestration.md).

## Available agents

Agents are namespaced `django-engineering:<id>` in Claude Code. In the harness they keep their
plain id.

| Agent | Write mode (plugin guard) | Responsibility |
|---|---|---|
| `django-project-analyzer` | read-only | Use first on an unfamiliar codebase. Maps versions, settings, apps, models, URL/API surface, dependencies, background work, tests and architectural risks into a project profile. |
| `django-architecture-agent` | workspace-write | App boundaries, layering (views, services, selectors, models), dependency direction, transaction and integration boundaries. Produces an architecture design, ADRs and an implementation plan, and scaffolds on request. |
| `django-model-agent` | workspace-write | Models, relationships, managers and QuerySets, constraints, indexes, and safe migrations (including data migrations and zero-downtime sequencing). |
| `django-api-agent` | workspace-write | DRF serializers, views and ViewSets, routers, authentication and permissions, queryset scoping, pagination, filtering, throttling, errors and OpenAPI annotations, with API tests. |
| `django-security-agent` | confirm-writes | Security assessment of settings, auth and sessions, authorisation/IDOR, CSRF/CORS/CSP, injection, XSS, uploads, SSRF, secrets and dependencies. Remediates only when asked. |
| `django-testing-agent` | workspace-write | Designs, writes and runs unit, integration and DRF API tests (permission matrices, query counts) with the project's own runner. |
| `django-performance-agent` | workspace-write | N+1 queries, missing indexes, over-fetching, caching and background offloading, measured before and after. |
| `django-documentation-agent` | documentation-only | Code-derived architecture, app, API, setup, configuration and runbook documentation. Never changes application code. |
| `django-code-review-agent` | read-only | Pre-merge review of a diff, branch or files. Returns `file:line` findings by severity and an APPROVE / APPROVE-WITH-COMMENTS / CHANGES-REQUESTED verdict. |
| `django-orchestrator` | read-only (delegates to the nine above only) | Classifies a request, selects and sequences the relevant specialists, reconciles their results and reports. Plugin only; in the harness `core.cli orchestrate` plays this role. |

Skill and template mappings per agent: [agents/README.md](agents/README.md).

## Available commands

Commands are namespaced: `/django-engineering:django-analyze`, and so on. Each specialist command
briefs exactly one agent; `/orchestrate` briefs the orchestrator, which selects the specialists. Commands that change code propose first and write only after your approval (or an
explicit `--apply` / `--fix` / `--scaffold`).

| Command | Arguments | Agent | Purpose |
|---|---|---|---|
| `/django-analyze` | `[project path \| focus question] [--paths <glob,...>] [--exclude <glob,...>]` | `django-project-analyzer` | Profile an existing project without changing it. |
| `/django-design` | `<feature, refactoring or system> [--profile <file>] [--out <dir>] [--scaffold]` | `django-architecture-agent` | App boundaries, layering, ADRs and an ordered implementation plan. |
| `/django-model` | `<entities and changes> [--app <label>] [--design <file>] [--apply]` | `django-model-agent` | Models, constraints, indexes and migrations, with a migration-risk assessment. |
| `/django-api` | `<resources and operations> [--design <file>] [--version <api version>] [--apply]` | `django-api-agent` | DRF endpoints: proposes the contract first, then implements it with tests. |
| `/django-security` | `[scope] [--base <git ref>] [--fix [SEC-001,...]]` | `django-security-agent` | Security assessment. Review-only unless `--fix` is given and approved. |
| `/django-test` | `<what to test> [--base <git ref>] [--run-only] [--apply]` | `django-testing-agent` | Plan, write and run tests; changes test code only. |
| `/django-optimize` | `<slow endpoint, view, task or path> [--target <metric and goal>] [--apply]` | `django-performance-agent` | Baseline, propose, apply after approval, re-measure. |
| `/django-document` | `<documents and audience> [--paths <code paths>] [--out <docs dir>] [--dry-run]` | `django-documentation-agent` | Generate or update documentation from the code. |
| `/django-review` | `[--base <git ref>] [paths...] [--focus <areas>] [--threshold <severity>]` | `django-code-review-agent` | Read-only pre-merge review with a verdict. |
| `/orchestrate` | `<what you want done> [--plan-only] [--category <id>]` | `django-orchestrator` | Routes the request to the relevant specialists and reports. |

Details and approval gates: [commands/README.md](commands/README.md).

## Skills and rules

**Skills** explain how to do Django work well. **Rules** state what every result must satisfy,
as requirement ids: `<PREFIX>-M<n>` is mandatory, `<PREFIX>-R<n>` is recommended, and `SEC-A<n>`
is security conduct for all agents. Both use the `SKILL.md` package format.

- **Plugin:** `plugin.json` loads `./skills/` and `./rules/`. Each agent preloads exactly its
  registered skills and rules through its frontmatter `skills:` line, which is generated from
  `config/agent-registry.yaml`.
- **Harness:** the same registry list is the only mapping. The harness injects each registered
  `SKILL.md` into the agent's prompt.

| Skills (12) | Rules (12) |
|---|---|
| `django-core`, `django-architecture`, `django-orm`, `django-rest-framework`, `postgresql-engineering`, `django-security-engineering`, `django-testing`, `django-performance`, `django-frontend`, `django-integrations`, `django-data-processing`, `django-report-generation` | `django-coding-standards`, `django-project-structure`, `django-configuration-standards`, `django-dependency-standards`, `django-model-standards`, `django-database-standards`, `django-api-standards`, `django-security-standards`, `django-testing-standards`, `django-performance-standards`, `django-logging-standards`, `django-documentation-standards` |

The security skill is named `django-security-engineering` so that it does not collide with the
`/django-security` command. **Templates** ([`templates/`](templates/README.md)) fix the structure
of each report. **Shared references** ([`references/`](references/README.md)) are checklists that
cite rule ids.

## Requirements

### Standalone plugin

| Requirement | Why |
|---|---|
| Claude Code with plugin marketplace support and `onFailure` on command hooks | Verified with 2.1.319 and 2.1.322. |
| `python` 3.8+ on `PATH` (the command must be named `python`) | The security guard runs as `python hooks/django_pack_guard.py`. It uses only the standard library, so you install no packages. If `python` is missing, the guard fails closed and **every tool call is blocked** while the plugin is enabled. |
| Git Bash (Windows only, optional) | Claude Code offers Bash on Windows only when it finds Git Bash. If it doesn't, set `CLAUDE_CODE_GIT_BASH_PATH`; otherwise the agents run without Bash. |

The plugin does not need the harness, its Python dependencies, the Claude Agent SDK or a
database.

### Harness

| Requirement | Source |
|---|---|
| Python ≥ 3.11 (verified with 3.13) | `pyproject.toml` `requires-python` |
| `PyYAML>=6.0`, `jsonschema>=4.18`, `referencing>=0.30`, `claude-agent-sdk>=0.2.177,<0.3` | `pyproject.toml` |
| Pinned runtime versions: `PyYAML==6.0.3`, `jsonschema==4.26.0`, `referencing==0.37.0`, `claude-agent-sdk==0.2.177` (development adds `pytest==9.2.0`) | `requirements-harness.txt`, `requirements-dev.txt` |
| Claude Code CLI | Bundled in the `claude-agent-sdk` wheel (2.1.317 for 0.2.177); no separate install. |
| Credentials | A Claude Code login on the machine, or `ANTHROPIC_API_KEY`. The CLI receives only the environment variables allow-listed in `config/security-config.yaml`. |
| Parent repository plugins | `plugins/security`, `plugins/database`, `plugins/api`, `plugins/pr-automation`: **optional** integrations, not in this repository. Required by the default `full` profile; the `pack` profile excludes them explicitly. See [Harness setup](#harness-setup). |
| OS sandbox for Bash | macOS, or Linux with `bwrap` and `socat`. Without one (for example on Windows), Bash is removed from harness sessions by default. |

## Harness setup

The harness is optional. It needs Python ≥ 3.11 and its own dependencies; the plugin does not.
Nothing is installed when you install the plugin. Harness dependencies are installed only when
you run the setup below and confirm.

**Guided setup (recommended).** From a clone of this repository, with any Python ≥ 3.11:

```bash
python scripts/harness.py setup --profile pack     # shows the pinned packages, asks, installs into .venv
python scripts/harness.py check --profile pack     # dependencies + configuration validation
python scripts/harness.py run --profile pack -- orchestrate plan "Review my branch before merge"
```

The launcher uses only the standard library. Here is what it does:

- **Environment:** it creates `.venv` only when absent and reuses an existing one. It never
  deletes or overwrites an environment, configuration file or database (`var/harness.db`).
- **Packages:** it installs the pinned `requirements-harness.txt` from your pip index into that
  environment, and executes no downloaded scripts.
- **Errors:** it explains each missing dependency and never falls back to the standalone plugin.

It was verified from an empty environment on Windows with Python 3.13. Linux and macOS are not
yet tested; the manual steps below work everywhere.

**Profiles.** The harness registries also reuse eight agents and nine skills from four
**optional** parent-repository plugins (`plugins/security`, `plugins/database`, `plugins/api`,
`plugins/pr-automation`).

- **`full` (default):** the behaviour is unchanged. Those plugins are required, so in an
  independent clone validation fails. Each error names what the plugin provides and how to exclude
  it.
- **`pack`:** excludes them explicitly, with their agents, skills and the `security-review`
  workflow, and prints every exclusion. It runs the Django Engineering Pack, its orchestrator and
  any workflow built from the nine specialists.

`django-engineering` is mandatory in both profiles. Select a profile with `--profile` or
`DAE_HARNESS_PROFILE`. See [docs/orchestration.md](docs/orchestration.md#harness-setup-and-profiles).

**Manual setup** (equivalent):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-harness.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m core.cli --profile pack validate
.venv/Scripts/python -m core.cli --profile pack agent list
```

Configuration lives in `config/` (seven YAML files, each with a JSON Schema in `config/schemas/`).
Runtime output goes to `var/` (SQLite database `var/harness.db` and `var/artifacts/`), which is
gitignored.

Route and run a task through the canonical orchestration policy. The workspace must be outside the
harness directory:

```bash
.venv/Scripts/python -m core.cli --profile pack orchestrate plan "Add a DRF endpoint for invoices"
.venv/Scripts/python -m core.cli --profile pack orchestrate run "Add a DRF endpoint for invoices" \
    --workspace ../my-django-app --approve interactive
.venv/Scripts/python -m core.cli execution status <execution-id>
```

The shipped `security-review` workflow (status `draft`) uses the reused parent-repository security
agents, so it needs the `full` profile and the parent layout. The full CLI, exit codes and approval
options are in [docs/harness.md](docs/harness.md#cli).

## Usage examples

### In Claude Code (plugin)

Analyse a project (read-only):

```text
/django-engineering:django-analyze
/django-engineering:django-analyze --paths "apps/billing/**"
```

Design with the architecture agent:

```text
/django-engineering:django-design Split order fulfilment out of the orders app --out docs/architecture
```

You can also ask for an agent directly, for example: *"Use the
django-engineering:django-architecture-agent agent to propose app boundaries for invoicing."*

Review code before merge:

```text
/django-engineering:django-review --base main
/django-engineering:django-review apps/payments/ --focus security,migrations
```

Generate an implementation plan: `/django-design` returns an ordered implementation plan (template
`implementation-plan`) together with the design and ADRs:

```text
/django-engineering:django-design Add multi-tenant workspaces to the existing project
```

### Orchestrated (plugin)

```text
/django-engineering:orchestrate Profile this project and rank its architectural risks
/django-engineering:orchestrate Implement invoice export end to end with a new model and a DRF endpoint
```

### In the harness

These commands need credentials. With `--profile pack` they work in an independent clone; without
it, they need the parent-repository layout:

```bash
.venv/Scripts/python -m core.cli --profile pack orchestrate run "Review the changes on this branch" \
    --workspace ../my-django-app

.venv/Scripts/python -m core.cli agent run django-project-analyzer \
    --workspace ../my-django-app --objective "Profile this project and rank its architectural risks."

.venv/Scripts/python -m core.cli agent run django-code-review-agent \
    --workspace ../my-django-app --objective "Review the changes on this branch against main."

.venv/Scripts/python -m core.cli agent run django-architecture-agent \
    --workspace ../my-django-app --objective "Produce an implementation plan for multi-tenant workspaces." \
    --output implementation-plan:report
```

## Security model

The plugin and the harness have **separate** security implementations. They share
`config/security-config.yaml` and the shell parser (`core/shell_syntax.py`, copied byte for byte to
`hooks/shell_syntax.py`), and nothing else. They are not equivalent.

| Capability | Standalone plugin | Harness |
|---|---|---|
| Agent execution | Claude Code subagents | Claude Agent SDK runtime (one query per stage, run as `dae-<id>`) |
| Tool restrictions | Each agent's `tools` frontmatter, plus the plugin guard (`hooks/django_pack_guard.py`) | Effective tool set, native `permissions.deny`, PreToolUse ToolGuard |
| Write modes (read-only, documentation-only, confirm-writes) | Enforced by the plugin guard | Enforced by the harness guard (stricter of `security-config.yaml` and the pack policy) |
| Workspace boundaries | Guard: reads inside the project and the plugin, writes inside the project | Canonical-path workspace isolation; harness, config, database and plugins protected |
| Secret masking | PostToolUse masking (not for failed commands) | PostToolUse masking, plus redacted logs, results and artifacts |
| Approval handling | Claude Code's own permission prompt (`ask`); refused in `-p` mode | Harness approver (`--approve deny` default, or `interactive`), bound to the exact call |
| Audit log | Not provided | Every decision in the execution log |
| Execution persistence | Not provided | SQLite: executions, stages, agent runs, RunRecords |
| Orchestration of specialists | `django-orchestrator` (Claude Code delegation; the guard enforces its target allowlist; delegation count and retries are instructions only) | `core.cli orchestrate`: routes from the same policy, run by the workflow engine with enforced step limits |
| Workflow orchestration | Not provided | Workflow engine with dependencies, retries, approval gates and resume |

Platform limitations:

- **Static analysis, not a sandbox.** Both guards parse shell commands and deny anything they
  cannot analyse. They still cannot bound a script an agent writes and then runs, or a plain
  `$var` operand.
- **Harness Bash on Windows.** Claude Code has no OS sandbox on Windows, so the harness removes
  Bash from every session by default (`shell.without_sandbox: disable`), or asks approval for
  every call (`approve`). The macOS and Linux sandbox configuration has been tested offline only.
- **Plugin guard scope.** The guard acts only on the pack's ten agents (nine specialists and the
  orchestrator). Your main session and
  other plugins remain under Claude Code's own permissions. Use Claude Code's sandbox where your
  platform supports it for an OS-level boundary.
- **Plugin guard needs `python`.** Without `python` on `PATH`, all tool calls are blocked while
  the plugin is enabled (fail closed).

### Feature comparison

| Capability | Standalone plugin | Harness mode |
|---|---|---|
| Orchestrator | Yes, `django-orchestrator` (agent) | Yes, `core.cli orchestrate` (deterministic planner) |
| Specialist agents | Yes, 9 | Yes, the same 9 files |
| Skills and rules | Yes, preloaded per agent | Yes, injected per agent from the same registry |
| User-facing commands | Yes, 10 slash commands | CLI (`core.cli`); slash commands are not used |
| Explicit workflow engine | No | Yes, `ExecutionManager` |
| Controlled stage execution | Claude Code delegation behaviour | Harness execution manager, one SDK query per stage |
| Harness ToolGuard | No (the plugin has its own, separate guard) | Yes |
| Harness approvals | No (Claude Code's permission prompt) | Yes |
| RunRecord persistence | No | Yes |
| Checkpoints and recovery | No | Yes |
| Python environment | Only a `python` interpreter for the guard hook (no packages) | Required (≥ 3.11, pinned packages) |
| External harness plugins | Not required | `full` profile: the four parent plugins; `pack` profile: none |

Full details: [docs/plugin.md](docs/plugin.md#standalone-plugin-security),
[docs/harness.md](docs/harness.md#security-model) and [hooks/README.md](hooks/README.md).

## Project structure

```text
.
├── .claude-plugin/
│   ├── plugin.json           Django Engineering Plugin manifest (plugin root = repository root)
│   └── marketplace.json      GitHub marketplace "django-agentic-engineering"
├── agents/                   ┐
├── skills/                   │
├── rules/                    │ Canonical Django Engineering Pack
├── commands/                 │ (used by both the plugin and the harness)
├── templates/                │
├── references/               ┘
├── hooks/                    Plugin guard: hooks.json, django_pack_guard.py, generated policy
├── plugin_packaging/         build / check / stage, standalone-security.yaml
├── config/                   ┐
├── contracts/                │
├── core/                     │ Django Agentic Harness
├── runtime/                  │
├── agent_runtime/            │
├── workflows/                ┘
├── evaluations/              Evaluation convention (no scorers yet)
├── scripts/harness.py        Guided harness setup / check / launcher (standard library only)
├── docs/                     plugin.md, harness.md, orchestration.md
├── tests/                    pytest suite
├── pyproject.toml
├── requirements-harness.txt  Pinned harness runtime dependencies
└── requirements-dev.txt      requirements-harness.txt + pytest
```

## Development and contribution

```bash
git clone https://github.com/<GITHUB_USERNAME>/<REPOSITORY_NAME>.git
cd <REPOSITORY_NAME>
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt      # Linux/macOS: .venv/bin/python
```

| Task | Command |
|---|---|
| Validate the plugin manifest | `claude plugin validate .claude-plugin/plugin.json` |
| Validate the marketplace manifest | `claude plugin validate .claude-plugin/marketplace.json` |
| Check packaging drift (generated policy, `skills:` lines, guard parser copy) | `.venv/Scripts/python -m plugin_packaging check` |
| Regenerate derived files after editing a source | `.venv/Scripts/python -m plugin_packaging build` |
| Plugin and pack tests in a standalone clone | `DAE_TEST_PRUNE_UNAVAILABLE=1 .venv/Scripts/python -m pytest -q tests/test_plugin_packaging.py tests/test_django_harness_integration.py tests/test_orchestration.py` |
| Harness validation in a standalone clone | `.venv/Scripts/python -m core.cli --profile pack validate` |
| Full test suite (parent-repository layout) | `.venv/Scripts/python -m pytest -q` |
| Harness validation (parent-repository layout) | `.venv/Scripts/python -m core.cli validate` |

In a standalone clone, tests that load the full registry error or fail because the parent plugins
are missing. With `DAE_TEST_PRUNE_UNAVAILABLE=1`, the tests that assert those reused agents still
fail by design. See [docs/harness.md](docs/harness.md#tests). Live tests are opt-in
(`DAE_SDK_INTEGRATION=1`, `DAE_PLUGIN_LIVE=1`) and incur a small API cost.

**Single source of truth.** Agent, skill, rule, command, template and reference definitions exist
once, in their canonical folders. Never copy them into a plugin-specific or harness-specific
location. The agent-to-skill mapping lives only in `config/agent-registry.yaml`, and the routing
policy only in `config/orchestration-policy.yaml`. The derived content is drift-checked by
`plugin_packaging check`: `hooks/security-policy.json` (including the orchestrator's delegation
allowlist), each agent's `skills:` line, the generated routing section of
`agents/django-orchestrator.md` and `hooks/shell_syntax.py`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

| Problem | Fix |
|---|---|
| `marketplace add` fails | Use the `owner/repo` form (or a full `https://` git URL). The repository must be reachable with your git credentials, and `.claude-plugin/marketplace.json` must be on its default branch. Check the file locally with `claude plugin validate .claude-plugin/marketplace.json`. |
| `plugin install` cannot find the plugin | The suffix is the marketplace name, not the repository name: `django-engineering@django-agentic-engineering`. `claude plugin marketplace list` shows the configured names. Run `claude plugin marketplace update django-agentic-engineering` to refresh. |
| Plugin does not appear in `/agents` | Start a new session or run `/reload-plugins`. Check that `claude plugin list` shows it enabled. Agents appear as `django-engineering:<id>`. |
| A specific agent is not discovered | Agents are listed file by file in `plugin.json`. After editing them, run `python -m plugin_packaging check`. `claude plugin details` showing **Agents (0)** is expected; check `/agents` or the session `init` frame instead (see [docs/plugin.md](docs/plugin.md#install-and-use)). |
| Every tool call is blocked / "hook interpreter missing" | The guard could not start. Make sure a command named `python` (3.8+) is on `PATH`. On systems that ship only `python3`, provide `python`, for example with `python-is-python3`, a virtualenv or a symlink. The interpreter cannot be configured in the plugin. |
| Bash is unavailable to agents on Windows | Install Git for Windows, or set `CLAUDE_CODE_GIT_BASH_PATH` to `...\Git\bin\bash.exe`. |
| Harness: `directory not found: plugins/security` (`missing-path`) | An optional parent-repository plugin is missing. Provide the parent layout, or run with `--profile pack` to exclude the optional integrations explicitly; see [Harness setup](#harness-setup). The plugin is unaffected. |
| Harness: `CANNOT ROUTE [ambiguous]` / `[unclassified]` | The task fits several orchestration categories equally, or none. Re-run with `--category <id>` (the error lists the candidates). |
| `/orchestrate` returns a ROUTING PLAN | The orchestrator could not spawn subagents where it ran. The command then runs the plan from the main session; or start Claude Code with `claude --agent django-engineering:django-orchestrator`. |
| Harness: session shows agent `dae-<id>` instead of `<id>` | Expected. The harness runs its definition as `dae-<id>` so that the plugin's same-named agent file cannot replace it. Records, logs and results keep the logical id; `sdk_metadata.session_agent` holds the session name. |

## License

No license has been selected yet. `plugin.json` declares `"license": "UNLICENSED"` and the
repository contains no `LICENSE` file. The repository owners need to choose a license and add a
`LICENSE` file before inviting external use or contributions.
