---
name: django-optimize
description: >-
  Investigate Django application and ORM performance: N+1 queries, missing indexes,
  over-fetching, caching and slow request paths. Measures a baseline, proposes fixes, applies
  them after approval and measures again. Runs the django-performance-agent.
argument-hint: "<slow endpoint, view, task or path> [--target <metric and goal>] [--apply]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-performance-agent for measurement-driven optimisation:
  baseline, diagnosis, authorized targeted fixes, and before/after evidence.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, PostgreSQL, Redis]
domain: [Software Engineering, Performance]
---

# /django-optimize

## Purpose

Use when an endpoint, page or task is slow or expensive. No change is made or claimed without
a measurement before and after it.

## Arguments

`$ARGUMENTS`

- Free text (required): what is slow (an endpoint, view, task, queryset or path) and any
  numbers the team already has.
- `--target <metric and goal>`: for example `"p95 < 200ms"` or `"<= 5 queries per list page"`.
- `--apply`: the user authorizes applying the proposed optimisations without a second
  confirmation.
- `@file` mentions (traces, slow-query logs, profiles) are passed as context.

## Agent

Delegate to the `django-performance-agent` subagent. It owns measurement, diagnosis and the
optimisations; this command supplies the brief and the authorization gate. The main session
does not measure or change code itself.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-performance` · `django-orm` · `postgresql-engineering`
- Rules: `django-security-standards` · `django-performance-standards` · `django-database-standards` · `django-coding-standards` · `django-testing-standards`

## Authorization

1. **Baseline and diagnosis (always first).** The agent measures on the test database and
   ranks the bottlenecks. It must not change files.
2. **Approval.** Show the findings and the proposed changes, and ask the user to approve them,
   unless `--apply` was given.
3. **Optimise and re-measure.** Brief the agent with the approved changes. It applies them,
   adds regression tests and measures again.

Index or schema changes that need a migration are handed to `/django-model`, not applied here.
Never measure against production or shared databases or caches, and never run load tests
against shared environments.

## Steps

1. Parse the target paths, metric goal and `--apply`.
2. Brief the agent for the baseline:
   - **Objective:** baseline and diagnosis for the named paths.
   - **Constraints:** no file changes; test database only; the metric goal.
   - **Inputs:** paths and `@file` context.
   - **Expected outputs:** findings with measurements and proposed changes.
3. Apply the authorization gate above.
4. Brief the agent to apply the approved changes and re-measure.
5. Relay the performance report with before and after numbers side by side.

## Expected output

The performance report: targets and method, findings with evidence (queries, timings, plans),
changes with before and after values and their tests, recommendations for other commands,
verification commands, and limitations. Estimated gains are labelled as estimates.

Template: `performance-assessment`. Templates are in the pack at `templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`).
Name them in the brief's expected outputs so the agent's reply follows their structure.

## Completion criteria

- [ ] Each path in scope has a baseline, and each applied change has an after measurement.
- [ ] Files changed only after approval or `--apply`.
- [ ] Each applied change has a regression test, and the existing tests still pass.
- [ ] Gains within measurement noise are not claimed.
