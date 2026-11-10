---
name: django-database-standards
description: >-
  Mandatory Django engineering rule for migrations and database access: immutable applied
  migrations, reversibility, historical models in data migrations, zero-downtime schema changes,
  transaction scope and locking, raw SQL, and where migrations may run. Apply when writing
  migrations, transactions or raw SQL, or when touching database configuration.
project: django-agentic-engineering
abstract: >-
  Enforceable migration and database-access standards that keep schema evolution safe and
  repeatable on any Django-supported database.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Data]
---

# Rule: Django Database Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Change the schema and the data without losing data, without long outages, and with a way back.

## Scope

Applies to migrations, data migrations, transactions, locking, raw SQL and connection
settings. Model definitions are covered by `django-model-standards`.

## Mandatory requirements

- **DB-M1** A migration that may have been applied in any shared environment is never edited
  or deleted. Fix it forward with a new migration. Squashing needs a documented plan.
- **DB-M2** Every migration is reversible, or is explicitly marked irreversible with a stated
  reason. `RunPython` always supplies `reverse_code`, which may be `migrations.RunPython.noop`
  when that is safe.
- **DB-M3** Data migrations use historical models from `apps.get_model()`. They never import
  application models, services or settings-dependent code.
- **DB-M4** Changes that rewrite or lock large or busy tables follow expand → backfill →
  contract across deployments. On PostgreSQL, indexes on such tables are built with
  `AddIndexConcurrently` in a non-atomic migration.
- **DB-M5** Migrations are deterministic. They do not depend on environment variables, the
  current time, or external services.
- **DB-M6** Transactions are short. Network calls and other slow I/O MUST NOT run inside
  `atomic()`. `select_for_update()` is used only inside a transaction, and rows are locked in a
  consistent order.
- **DB-M7** Raw SQL is parameterised (see SEC-M2) and isolated in a clearly named function.
- **DB-M8** Agents and automated tools run migrations and SQL only against the test database.
  Migrations on shared environments are human-approved deployment steps.
- **DB-M9** Database credentials come from configuration (see `django-configuration-standards`),
  and the application's database role has only the privileges it needs.

## Recommendations

- **DB-R1** Keep one concern per migration, with a descriptive name (`--name`).
- **DB-R2** Review `sqlmigrate` output for every schema migration before merging.
- **DB-R3** On PostgreSQL, set `lock_timeout`/`statement_timeout` for migrations on large tables.
- **DB-R4** Choose `ATOMIC_REQUESTS` and `CONN_MAX_AGE` (or a pool) deliberately and document the choice.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| Editing `0007_...py` after it has shipped | New migration `0008_...` | DB-M1 |
| `from app.models import Order` in a data migration | `apps.get_model("app", "Order")` | DB-M3 |
| `RunPython(forwards)` with no reverse | `RunPython(forwards, reverse_code=...)` | DB-M2 |
| Adding a NOT NULL column and backfilling a large table in one migration | Expand, backfill, contract | DB-M4 |
| An HTTP call inside `transaction.atomic()` | Call after commit, or outside the transaction | DB-M6 |
| Running `migrate` against a shared database from an agent | Test database only | DB-M8 |

## Security and maintainability

Immutable, reversible migrations make every environment reproducible. Least-privilege database
roles limit the damage an injection or a leaked credential can do.

## Verification

`makemigrations --check --dry-run`, `sqlmigrate <app> <migration>`, and `showmigrations` on the
test database. Review each new migration for `RunPython` reverse functions, model imports, and
table-rewriting operations.

## Related skills

[django-orm](../../skills/django-orm/SKILL.md) (migrations and schema evolution, transactions and concurrency, raw SQL) ·
[postgresql-engineering](../../skills/postgresql-engineering/SKILL.md) (indexing, locking, connections).
