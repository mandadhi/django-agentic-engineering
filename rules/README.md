---
project: django-agentic-engineering
abstract: Convention for the Django engineering rules (Phase 3C) and how agents receive them.
sdlc_phase: Architecture/Planning
tech_stack: [Django]
domain: [Software Engineering, AI Agents]
---

# rules/

Mandatory engineering standards for the Django agents. Skills teach how to do something;
rules state what every result **must** satisfy. A rule refers to skills and never copies them.

## Format and registration

No new mechanism was needed. A rule uses the same package format as a skill, and loads the
same way. This repository already ships standards as skills (`pr-review-standards`,
`api-design-standards`).

1. `rules/<rule-id>/SKILL.md` has frontmatter (`name` = id, `description`, `project`,
   `abstract`, `sdlc_phase`, `tech_stack`, `domain`) and these sections: Purpose, Scope,
   Mandatory requirements, Recommendations, Anti-patterns, Security and maintainability,
   Verification, Related skills.
2. Register the rule in `config/skill-registry.yaml` with `source: plugin`,
   `plugin: django-engineering`, `path: rules/<rule-id>` and the tag `rule`.
   `registry.skills.by_tag("rule")` lists all rules.
3. Attach the rule to agents in their `skills:` lists in `config/agent-registry.yaml`. The
   harness injects them like skills (each registered `SKILL.md`, in registry order). Each agent's
   `## Mandatory rules` section lists the same ids.

Requirement ids are stable: `<PREFIX>-M<n>` is mandatory (MUST), `<PREFIX>-R<n>` is
recommended (SHOULD), and `SEC-A<n>` is security conduct that every agent follows. Agents cite
these ids in findings.

## Rules

| Rule | Prefix | Covers |
|---|---|---|
| [django-project-structure](django-project-structure/SKILL.md) | STR | settings selection, apps and AppConfig, labels, start-up code, cross-app references |
| [django-coding-standards](django-coding-standards/SKILL.md) | COD | tooling, where logic lives, exceptions, Django APIs, transactions, async safety |
| [django-model-standards](django-model-standards/SKILL.md) | MOD | DB-enforced invariants, relations, field types, sensitive fields, `save()` |
| [django-api-standards](django-api-standards/SKILL.md) | API | default-deny, scoped querysets, explicit fields, pagination, compatibility |
| [django-security-standards](django-security-standards/SKILL.md) | SEC | security floor for code and conduct rules for **every** agent |
| [django-database-standards](django-database-standards/SKILL.md) | DB | migrations, reversibility, zero-downtime changes, transactions, raw SQL |
| [django-testing-standards](django-testing-standards/SKILL.md) | TST | tests per change, test database only, isolation, determinism, access matrix |
| [django-performance-standards](django-performance-standards/SKILL.md) | PRF | N+1, bounded queries, bulk writes, offloading, cache keys, measurement |
| [django-configuration-standards](django-configuration-standards/SKILL.md) | CFG | environment-sourced settings, production hardening, parsing, fail-fast |
| [django-logging-standards](django-logging-standards/SKILL.md) | LOG | module loggers, no secrets in logs, log once, security events |
| [django-documentation-standards](django-documentation-standards/SKILL.md) | DOC | docs match code, no secrets, config reference, API docs, ADRs |
| [django-dependency-standards](django-dependency-standards/SKILL.md) | DEP | manifest and locks, supported versions, audits, trusted sources |

## Agent applicability

`django-security-standards` applies to all nine agents. Rules grant no tools. Read-only agents
stay read-only, and the documentation agent changes documentation only (SEC-A4, DOC-M8).

| Agent | Rules |
|---|---|
| django-project-analyzer | security, project-structure, configuration, dependency (assessed and reported, not fixed) |
| django-architecture-agent | security, project-structure, configuration, logging, documentation, dependency |
| django-model-agent | security, model, database, coding, testing |
| django-api-agent | security, api, coding, testing, performance, logging |
| django-security-agent | security, configuration, logging, dependency, api, coding, testing |
| django-testing-agent | security, testing, coding |
| django-performance-agent | security, performance, database, coding, testing |
| django-documentation-agent | security, documentation |
| django-code-review-agent | all 12, applied per changed file |

The [shared references](../references/README.md) and [output templates](../templates/README.md)
(Phase 3E) cite requirement ids from these rules and never restate them; tests check that every
cited id exists.

As with the Django skills, the Claude Code plugin (Phase 4) loads these rules in place from
`./rules/` (declared in `.claude-plugin/plugin.json`), with no copy or adapter, and each agent
preloads its registered rules. Since Phase 5 the harness injects the same registered rules into each
agent (see [docs/harness.md](../docs/harness.md#harness-integration-phase-5)).
