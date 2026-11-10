---
name: django-migration-safety
description: >-
  Shared reference for classifying the risk of Django migration operations, the read-only checks
  to run before merging a migration, and when a change needs an expand-backfill-contract migration
  plan.
kind: shared-reference
agents: [django-model-agent, django-performance-agent, django-code-review-agent]
project: django-agentic-engineering
abstract: >-
  Risk classification of Django migration operations, pre-merge verification commands, review
  red flags and escalation criteria for a migration plan, linked to the ORM skill and database rule.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Data]
---

# Django Migration Safety

## Purpose

Give the agents that write, review or recommend migrations one shared way to rate a migration's
risk and to decide whether it can ship as is or needs a phased
[migration plan](../templates/migration-plan.template.md).

## Scope

Django schema and data migrations (`migrations/` packages, `RunPython`, `RunSQL`). Exact locking
and rewrite behaviour depends on the database backend and its version, so this reference rates
operations by what to check, and the SQL from `sqlmigrate` is the evidence.

## How to use

1. Run the pre-merge checks.
2. Rate each operation with the risk table; the highest rating rates the migration.
3. `low`: record it in the [model specification](../templates/django-model-specification.template.md).
   `medium` or `high`: write a migration plan. Unknown table size counts as large.

## Pre-merge checks

| Check | Command (test database or no database) | Expected |
|---|---|---|
| Models and migrations agree | `python manage.py makemigrations --check --dry-run` | No changes detected |
| One leaf per app | `python manage.py showmigrations --plan` or the error from `makemigrations --check` | No conflict |
| The SQL that will run | `python manage.py sqlmigrate <app> <migration>` | Reviewed operation by operation |
| Applies and reverses | Run the project's tests; for data migrations, a test that migrates forward and back | Passes on the test database |
| Nothing already shared was edited | `git log --follow` on each changed migration file | Only new files, or files never deployed |

Agents run these against the test database only. Applying migrations anywhere else is a step for
a human.

## Operation risk

| Operation | Rating | What to check in the SQL and code |
|---|---|---|
| `CreateModel`, `AddField` nullable without default | low | Nothing else depends on the new structure yet |
| `AddField` with a default, or `NOT NULL` | medium | Whether the backend rewrites the table or backfills; whether old code can insert rows without the field |
| `AddIndex` / `index=True` / `db_index=True` | medium, high on large tables | Whether the build blocks writes; on PostgreSQL, `AddIndexConcurrently` in a non-atomic migration avoids that |
| `AddConstraint` (unique, check) on existing data | high | Existing rows that violate it; validation scan and lock duration |
| `AlterField` changing type, length or nullability | high | Table rewrite, lock, values that do not convert, old code writing old shapes |
| `RenameField`, `RenameModel`, `AlterModelTable` | high | Running code still uses the old name during deploy |
| `RemoveField`, `DeleteModel` | high | Old code still reads or writes it; data is unrecoverable after release |
| `RunPython` | medium, high on large tables | Uses `apps.get_model()`, batches, idempotent, has a reverse or a stated reason |
| `RunSQL` | high | Parameterised, reverse SQL given, backend-specific statements guarded |
| `SeparateDatabaseAndState` | high | State and database operations describe the same end result |
| Changing a primary key or `AUTH_USER_MODEL` | high | Every foreign key and dependent migration; needs a dedicated plan |

## Review red flags

| Red flag | Why it matters | Rule |
|---|---|---|
| An existing migration file edited | Environments that applied it diverge | `DB-M1` |
| `RunPython` without `reverse_code` and no stated reason | Rollback is impossible | `DB-M2` |
| Model classes imported into a migration | Migration breaks when the model changes later | `DB-M3` |
| Environment reads, `now()`, or random values deciding schema | Non-deterministic migrations | `DB-M5` |
| Rename or drop in the same release as the code change | Old code fails during deploy | `DB-M4` |
| Model change without its migration | Schema drift | `MOD-M10` |
| Migration in a different app's `migrations/` package | Ownership confusion and conflicts | `STR-M5` |

## Escalate to a migration plan when

- any operation is rated `high`, or `medium` on a large or busy table;
- a field or model is renamed or removed while code that uses it is deployed;
- a backfill touches more rows than one transaction should hold;
- the deploy must stay online and the operation may block writes.

## Related skills and rules

- Skills: [django-orm](../skills/django-orm/SKILL.md)
  ([migrations and schema evolution](../skills/django-orm/references/migrations-and-schema-evolution.md),
  [constraints and indexes](../skills/django-orm/references/database-constraints-and-indexes.md),
  [transactions](../skills/django-orm/references/transactions-and-concurrency.md)),
  [postgresql-engineering](../skills/postgresql-engineering/SKILL.md)
  ([indexing and query plans](../skills/postgresql-engineering/references/indexing-and-query-plans.md)).
- Rules: [django-database-standards](../rules/django-database-standards/SKILL.md),
  [django-model-standards](../rules/django-model-standards/SKILL.md),
  [django-project-structure](../rules/django-project-structure/SKILL.md).
