# Changelog

This file records notable changes. No release has been published on GitHub yet.

Component versions are independent:
- Django Engineering Plugin: `.claude-plugin/plugin.json` `version`.
- Harness Python package: `pyproject.toml` `version`.
- Framework configuration: `config/framework-config.yaml` `framework.version`.

## [Unreleased]

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
