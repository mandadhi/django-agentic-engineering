---
name: django-dependency-standards
description: >-
  Mandatory Django engineering rule for dependencies: a single manifest, locked and pinned
  versions, supported Django and DRF releases, justified additions, vulnerability auditing,
  development-only tools kept out of production, and trusted package sources. Apply when adding,
  upgrading or reviewing Python dependencies of a Django project.
project: django-agentic-engineering
abstract: >-
  Enforceable dependency and supply-chain standards for Django projects, independent of the
  packaging tool in use.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, pip, Poetry, uv]
domain: [Software Engineering, Security]
---

# Rule: Django Dependency Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Keep every deployment reproducible, supported and free of known-vulnerable or untrusted code.

## Scope

Applies to Python dependency manifests, lock files, `INSTALLED_APPS` entries from third-party
packages, and the upgrade process. System packages and container base images follow the
project's platform rules.

## Mandatory requirements

- **DEP-M1** Dependencies are declared in the project's single manifest (`pyproject.toml` or
  `requirements*.in`/`.txt`). Deployable applications install from a lock file, or from exact
  pins with hashes.
- **DEP-M2** Production runs a Django release that still receives security updates, and a DRF
  and Python version compatible with it. End-of-life versions are reported as findings.
- **DEP-M3** A new dependency needs a justification: it is maintained, its licence is
  compatible, and Django, the standard library or an existing dependency does not already
  provide the capability. Agents propose dependencies; they do not install them without approval.
- **DEP-M4** Dependencies are audited for known vulnerabilities (for example `pip-audit`) in
  CI. A change MUST NOT introduce a version with a known vulnerability that applies to the project.
- **DEP-M5** Development and test tools (debug toolbar, profilers, factory libraries) are in a
  development dependency group and are never enabled in production `INSTALLED_APPS` or middleware.
- **DEP-M6** Packages come from the configured trusted index. No `--trusted-host`, and no
  unpinned installs from URLs or VCS. A VCS dependency is pinned to a commit hash.

## Recommendations

- **DEP-R1** Automate update proposals (Dependabot or Renovate) and upgrade one major version at
  a time, with the full test suite.
- **DEP-R2** Run tests with deprecation warnings visible (`python -Wd`) before upgrading Django.
- **DEP-R3** Check the Django ↔ DRF ↔ Python ↔ third-party app compatibility matrix before upgrading.
- **DEP-R4** Prefer fewer, well-maintained packages over many small ones.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `Django>=3.2` with no lock file | Locked or exact pinned versions | DEP-M1 |
| Production on an end-of-life Django release | Planned upgrade to a supported release | DEP-M2 |
| Adding a package for something `django.utils` already does | Use the built-in | DEP-M3 |
| `debug_toolbar` in the base `INSTALLED_APPS` | Development settings only | DEP-M5 |
| `pip install git+https://...@main` | Pin to a commit hash | DEP-M6 |

## Security and maintainability

Supply-chain compromise and unpatched framework versions are high-impact, low-effort attack
paths. Locked dependencies make incidents reproducible and rollbacks reliable.

## Verification

Inspect the manifest and lock file. Compare the installed Django version against the supported
releases. Run the project's audit command where it is configured, and grep settings for
development-only apps.

## Related skills

[django-security-engineering](../../skills/django-security-engineering/SKILL.md) (dependency and supply-chain security) ·
[django-core](../../skills/django-core/SKILL.md) (production configuration).
