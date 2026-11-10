# Changelog

This file records notable changes. No release has been published on GitHub yet.

Component versions are independent:
- Django Engineering Plugin: `.claude-plugin/plugin.json` `version`.
- Harness Python package: `pyproject.toml` `version`.
- Framework configuration: `config/framework-config.yaml` `framework.version`.

## [Unreleased]

### Phase 5.6: opt-in repository-local Agentic Harness

Plugin `0.5.0` → `0.6.0`. Harness package and framework configuration unchanged. Not released or tagged.

#### Added
- `/django-engineering:setup-harness`: detects the project, checks the environment, shows a plan
  and, only after the user approves that exact plan (`plan_id`), installs the existing harness into
  `<project>/.django-engineering/` (versioned runtime snapshot, own virtual environment with the
  pinned requirements, repository configuration, SQLite database through the existing migrations,
  manifest), validates it and enables it for that repository. Idempotent, resumable, explicit
  upgrades (`--upgrade`) with database backup; never overwrites, deletes or installs globally.
- `/django-engineering:harness-status`, `enable-harness`, `disable-harness`, `doctor`.
- `harness_bootstrap/`: project detection, the single execution-mode resolver (`standalone`,
  `harness_enabled`, `harness_disabled`, `harness_invalid`), distribution snapshots, environment
  checks, provisioning, and a runtime adapter that delegates every operation to `python -m core.cli`
  of the repository's runtime. Standard library only (except the in-environment probe).
- framework-config `paths.enclave` (optional, schema updated): marks a repository-local
  installation as a protected root.
- `tests/test_harness_bootstrap.py` (56 tests).

#### Changed
- `/orchestrate` resolves the execution mode first. Standalone and disabled repositories behave as
  before; an enabled harness runs the same route through the harness; an invalid installation is
  reported and never bypassed.
- `agent_runtime/workspace.py`: a workspace may contain protected roots only when it is exactly the
  parent of `paths.enclave` and every contained root lies inside it. Without `paths.enclave`
  nothing changes.
- `agent_runtime/runner.py`: sandboxed sessions get native Read/Edit deny rules for the enclave's
  manifest, configuration, database and logs, and Edit deny rules for its runtime and environment.
- `plugin_packaging/standalone-security.yaml`: `protected_paths` (`.django-engineering`), appended
  to the standalone guard's denied paths. `hooks/security-policy.json` regenerated.
- `plugin_packaging stage` also exports `harness_bootstrap/`; staged bundles still carry no harness.
- `plugin.json`: 15 commands; `EXPECTED_COUNTS["commands"]` 15.
- `.gitignore`: repository-local harness state.

#### Not changed
- Specialist agents, skills, rules, templates, references, the orchestration policy, the security
  configuration, ExecutionManager, AgentRunner, ToolGuard, approvals, checkpoints, the database
  schema and the RunRecord contract.

### Phase 6: dual orchestration and simpler harness setup

Plugin `0.4.0` → `0.5.0`; harness package `0.2.0` → `0.3.0`. Not released or tagged.

#### Added
- `agents/django-orchestrator.md`: a user-facing orchestrator that routes a request to the smallest
  set of the nine specialists.
  - It is read-only. The plugin guard lets it delegate only to those nine specialists, never to
    itself.
  - If it cannot spawn subagents where it runs, it returns a ROUTING PLAN.
- `commands/orchestrate.md`: `/django-engineering:orchestrate`, the deterministic entry point.
- `config/orchestration-policy.yaml` and its schema: the canonical routing contract, shared by
  both modes.
  - It defines task categories, routes, optional steps, sequencing, limits, step constraints,
    completion criteria, clarification rules, the failure policy and security boundaries.
  - The plugin consumes it through generated, drift-checked content; the harness consumes it
    through `core/orchestration.py`.
- `core/orchestration.py`: policy model, reference validation, deterministic classification and
  route planning.
  - `core.cli orchestrate plan|run` runs a route as one workflow execution of the existing
    `ExecutionManager`/`AgentRunner`.
  - New `AgentRunner.start_definition`.
- Harness profiles: `--profile full|pack` and `DAE_HARNESS_PROFILE`.
  - The four parent-repository plugins are now registered `optional: true` with a description.
  - `pack` excludes them explicitly and reports each exclusion. `full` (the default) is unchanged.
- `scripts/harness.py setup|check|run`: a standard-library guided setup and launcher.
  - It creates `.venv` only if absent, installs the pinned `requirements-harness.txt` after
    confirmation, validates, and never falls back to the plugin.
- `requirements-harness.txt` (pinned runtime dependencies), `docs/orchestration.md` and
  `tests/test_orchestration.py`.

#### Changed
- `plugin_packaging build/check` also generates and drift-checks:
  - the orchestrator's routing section and `skills:` line;
  - its guard entry, which includes a `delegates` allowlist.

  Expected counts are now 10 agents and 10 commands.
- `hooks/django_pack_guard.py`: delegation check for agents with a `delegates` list.
- `core.cli validate` reports the profile, the orchestration policy and the exclusions. A
  missing-plugin error now says whether the plugin is mandatory or optional and how to recover.
- `requirements-dev.txt` now includes `requirements-harness.txt` plus `pytest`.
- Tests that enumerate `agents/` and `commands/` now list the orchestrator and `/orchestrate`
  explicitly. The opt-in live test expects them too.

No database schema, migration or RunRecord change.

### Added
- `.claude-plugin/marketplace.json`, a marketplace named `django-agentic-engineering` that lists
  the `django-engineering` plugin at the repository root. The plugin can now be installed from
  GitHub with `/plugin marketplace add <owner>/<repo>` and
  `/plugin install django-engineering@django-agentic-engineering`.
- `CONTRIBUTING.md`, `SECURITY.md` and this changelog.
- `docs/plugin.md` and `docs/harness.md`, which hold the detailed references that were previously
  in the README.

### Changed
- Rewrote the README for first-time GitHub users: installation, the two consumption modes,
  requirements per mode, and the security comparison.
- Corrected component READMEs that still described harness integration as not implemented and
  showed obsolete `source: local` registration examples.
- `tests/test_plugin_packaging.py` now allows `marketplace.json` next to `plugin.json`, checks the
  marketplace entry, and validates both manifests with `claude plugin validate`.

## Phase 5 (commit `7cb93c2`, local tag `phase5-release`)

The first commit of this repository. It contains:

- **Harness foundation (Phase 1):** schema-validated configuration, JSON Schema contracts, the
  agent/skill/workflow registry, scoped context, and the SQLite execution runtime with
  checkpoints and resume.
- **Agent execution (Phase 2):** one Claude Agent SDK query per workflow stage, security hooks,
  approvals, audit, timeouts, workflow inputs, `AgentResult` processing and session resume.
- **Django Engineering Pack (Phase 3):** 12 skills, 9 agents, 12 rules, 9 commands, 12 output
  templates and 7 shared references.
- **Django Engineering Plugin (Phase 4):** the pack as the standalone `django-engineering` Claude
  Code plugin (version 0.4.0) with a fail-closed security guard.
- **Harness integration (Phase 5):** the same plugin registered in the harness; security hardening
  (Phase 5.1); versioned `RunRecord` execution records (Phase 5.3).
