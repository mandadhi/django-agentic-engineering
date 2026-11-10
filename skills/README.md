---
project: django-agentic-engineering
abstract: Convention for harness-local Django skills (added in Phase 2) and how they are registered.
sdlc_phase: Architecture/Planning
tech_stack: [Django]
domain: [Software Engineering, AI Agents]
---

# skills/

Harness-local skills go here, one directory per skill: `skills/<skill-id>/SKILL.md`.
The directory may also contain `references/` and `scripts/`. This matches the layout
that Claude Code and Projects Explorer expect.

Phase 4 ships these skills, unchanged and in place, in the `django-engineering` Claude Code
plugin (`.claude-plugin/plugin.json` lists `./skills/` and `./rules/`). There they are named
`django-engineering:<id>`, and each Django agent preloads its registered ones. In the harness, a
`source: local` skill still cannot be loaded: the Agent SDK loads skills only through registered
plugins, and harness integration is a later phase (see the project README, Known limitations).
`config/skill-registry.yaml` also reuses existing plugin skills, for example
`plugins/security/skills/owasp-top10-review`.

The security skill is `django-security-engineering` (renamed from `django-security` in Phase 4)
because `/django-security` is a developer command, and in a plugin a command shadows a skill
with the same name.

## Django Engineering Skills

| Skill | Covers |
|---|---|
| [django-core](django-core/SKILL.md) | project layout, settings, apps, middleware, request lifecycle, logging |
| [django-architecture](django-architecture/SKILL.md) | modular monolith, app boundaries, services/selectors, dependencies |
| [django-orm](django-orm/SKILL.md) | models, relationships, constraints, QuerySets, transactions, migrations |
| [django-rest-framework](django-rest-framework/SKILL.md) | serializers, views/ViewSets, permissions, auth, pagination, filtering |
| [postgresql-engineering](postgresql-engineering/SKILL.md) | indexes, query plans, locking, isolation, connections |
| [django-security-engineering](django-security-engineering/SKILL.md) | authn/authz, CSRF, XSS, SQL injection, secrets, secure settings |
| [django-testing](django-testing/SKILL.md) | pytest-django, factories, API tests, mocking, coverage, CI |
| [django-performance](django-performance/SKILL.md) | N+1, profiling, caching, pagination, offloading, async |
| [django-frontend](django-frontend/SKILL.md) | templates, static files, forms, HTMX |
| [django-integrations](django-integrations/SKILL.md) | HTTP clients, retries, timeouts, webhooks |
| [django-data-processing](django-data-processing/SKILL.md) | uploads, CSV/Excel import, bulk writes, Celery tasks |
| [django-report-generation](django-report-generation/SKILL.md) | reporting queries, CSV/Excel/PDF output, large datasets |

Each skill has one owner topic, and the others link to it instead of repeating it
(for example, Celery task design is in `django-data-processing`). Generic material is
reused from plugin skills: `db-query-tuning`, `db-schema-conventions`,
`db-migration-playbook`, `api-design-standards`, `api-versioning-policy`,
`api-resilience-patterns`, `owasp-top10-review`, `secret-detection-patterns`.

The pack's [shared references](../references/README.md) (Phase 3E) link into these skills and
their `references/` files for explanations, and never repeat them.

To add a skill:
1. Create `skills/<id>/SKILL.md`.
2. Register it with `source: local` and
   `path: projects/django-agentic-engineering/skills/<id>`.
3. Reference it from an agent's `skills:` list, then run `python -m core.cli validate`.
   Any unknown skill id, or a directory without `SKILL.md`, is reported.
