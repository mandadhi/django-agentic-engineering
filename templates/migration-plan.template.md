---
name: migration-plan
description: >-
  Output template for planning a risky Django schema or data migration: affected tables and data
  volume, operation risk, expand-backfill-contract phases, deployment sequence, verification,
  rollback and approvals.
kind: output-template
agents: [django-model-agent]
commands: [django-model]
project: django-agentic-engineering
abstract: >-
  Standard structure for sequencing Django migrations that lock, rewrite or backfill data so that
  old and new code keep working and every step can be verified and rolled back.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Data]
---

# Migration Plan Template

## Purpose

Plan how a Django schema or data change reaches each environment without breaking running code,
holding long locks or losing data, and how each step is verified and undone.

## When to use

- A migration touches a large or busy table, rewrites a table, takes a strong lock, backfills
  data, renames or removes a field or model, or changes a column type.
- A deploy must stay online while the schema changes.
- Not needed for additive changes classified as low risk by the
  [migration safety reference](../references/django-migration-safety.md); the
  [model specification](django-model-specification.template.md) is enough.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | The schema or data change | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Apps, models and tables affected | model specification |
| `{{prepared_by}}` | Agent or person producing the plan | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `draft`, `approved`, `in-progress` or `done` | producer |
| `{{sources}}` | Model specification, migration files | inputs |
| `{{database_backend}}` | Database engine and version, or `unknown` | settings and lock files |
| `{{data_volume}}` | Row counts or size per table, or `unknown` with how to obtain it | provided by the owner, never guessed |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Change summary | required | What changes and why, with the model specification link | — |
| Affected tables and volume | required | Each table, its size, its traffic, the code that reads and writes it | owner-provided figures |
| Operation risk | required | Each migration operation with lock, rewrite and backfill risk | `sqlmigrate` output |
| Phases | required | Expand, backfill, switch, contract steps with the code release each needs | migration and release names |
| Deployment sequence | required | Ordered steps across environments with go/no-go checks | runbook |
| Backfill | optional | Batch size, throttling, idempotency, progress tracking | data migration or command |
| Verification | required | Checks before, during and after each phase | named commands or queries |
| Rollback | required | How to undo each phase and the point after which it is forward-only | — |
| Approvals | required | Who must approve which step; commands a human runs | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Classify each operation with the
  [migration safety reference](../references/django-migration-safety.md).
- Data volume is never guessed; when unknown, the plan says how to obtain it and treats the table as large.
- Agents do not run migrations or SQL outside the test database (`DB-M8`, `SEC-A3`): production
  steps are written as commands for a human to run.
- Every phase is releasable on its own; old and new code must both work during it (`DB-M4`).

## Output structure

````markdown
# Migration Plan — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Change summary
- Database backend: {{database_backend}}

## Affected tables and volume
| Table | Size | Traffic | Read by | Written by |
|---|---|---|---|---|
| | {{data_volume}} | | | |

## Operation risk
| Migration | Operation | Lock | Rewrite | Backfill | Risk | Mitigation |
|---|---|---|---|---|---|---|

## Phases
| Phase | Migration(s) | Code release | Compatible with | Reversible |
|---|---|---|---|---|

## Deployment sequence
| Step | Environment | Action | Run by | Go/no-go check |
|---|---|---|---|---|

## Backfill
| Batch size | Throttle | Idempotency | Progress tracking |
|---|---|---|---|

## Verification
| Phase | Check | Expected result |
|---|---|---|

## Rollback
| Phase | Rollback action | Forward-only after |
|---|---|---|

## Approvals
| Step | Approver | Command for a human to run |
|---|---|---|
````

## Completion checklist

- [ ] Every operation has a lock, rewrite and backfill assessment based on `sqlmigrate` output.
- [ ] Every phase works with both the previous and the next code release.
- [ ] Every phase has a verification and a rollback, or is marked forward-only with the reason.
- [ ] Data volumes are owner-provided or marked `unknown`; none are invented.
- [ ] Steps outside the test database are assigned to a human with the exact command.
