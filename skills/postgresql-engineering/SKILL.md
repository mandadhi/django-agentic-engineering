---
name: postgresql-engineering
description: >-
  PostgreSQL engineering for Django applications — schema and type choices, B-tree/partial/
  covering/GIN/BRIN indexes, reading EXPLAIN (ANALYZE, BUFFERS), query optimisation, isolation
  levels, row locks and deadlocks, timeouts, connection management (CONN_MAX_AGE, psycopg 3
  pooling, PgBouncer), full-text search and operational hygiene (autovacuum, statistics,
  pg_stat_statements). Load when designing tables, diagnosing a slow query, choosing an index,
  tuning connections or investigating locking.
project: django-agentic-engineering
abstract: >-
  The PostgreSQL side of a Django app: types and indexes that match the queries, plans read
  before and after a change, short transactions with timeouts, and bounded connection usage.
sdlc_phase: Development
tech_stack: [PostgreSQL, Django, psycopg]
domain: [Software Engineering, Data]
---

# PostgreSQL engineering

PostgreSQL is the default database. Django-level modelling is in
[django-orm](../django-orm/SKILL.md); this skill covers what happens in the engine. Generic,
multi-engine tuning rules live in the reused `db-query-tuning` and `db-schema-conventions`
plugin skills — this skill adds the PostgreSQL + Django specifics.

## When to use

- A query is slow, a page is slow because of the DB, or `pg_stat_statements` shows a hot query.
- Choosing an index type or column order; adding full-text or JSON search.
- Configuring connections, pooling, timeouts, or diagnosing "too many connections".
- Lock waits, deadlocks, serialization failures, or long-running transactions.

## Core guidelines

1. **Measure first.** Get the real SQL (`str(qs.query)`, `qs.explain(analyze=True, buffers=True)`,
   `pg_stat_statements`) and its plan before changing anything; compare the plan after.
2. **Index for the query, not the column.** Equality columns first, then range/order column;
   partial indexes for skewed predicates (`WHERE status = 'pending'`); `INCLUDE` for
   index-only scans. Every index costs writes — drop unused ones.
3. **Correct types:** `timestamptz` (Django default with `USE_TZ`), `numeric` for money,
   `text`/`varchar(n)` (no performance difference), `jsonb` not `json`, `bigint`/identity keys.
4. **Short transactions with timeouts.** Set `statement_timeout`, `lock_timeout` and
   `idle_in_transaction_session_timeout` per role or connection; never hold a transaction
   open across network calls.
5. **Lock in a consistent order** and use the weakest lock that works (`SELECT ... FOR UPDATE`
   via `select_for_update`, `FOR NO KEY UPDATE` via `select_for_update(no_key=True)`).
6. **Bound connections.** Total = processes × threads × pool size ≤ `max_connections` minus
   headroom. Use psycopg 3's pool (Django ≥ 5.1) or PgBouncer in transaction mode — not both
   without understanding the interaction.
7. **Keep statistics fresh and vacuum healthy:** monitor dead tuples, autovacuum lag and
   table bloat on high-churn tables; `ANALYZE` after bulk loads.
8. **Use PostgreSQL features through Django where they exist:** `django.contrib.postgres`
   (`ArrayField`, range fields, `ExclusionConstraint`, `GinIndex`, `SearchVector`,
   `AddIndexConcurrently`).

## Recommended patterns

```python
DATABASES["default"]["OPTIONS"] = {
    "pool": {"min_size": 2, "max_size": 10, "timeout": 10},    # psycopg 3 + Django ≥ 5.1
    "options": "-c statement_timeout=15000 -c lock_timeout=5000 "
               "-c idle_in_transaction_session_timeout=60000",
}
DATABASES["default"]["CONN_MAX_AGE"] = 0      # must be 0 when Django's pool is enabled
```

```python
# See the plan from Django
print(Order.objects.filter(tenant_id=t, status="pending").order_by("-created_at")[:50]
      .explain(analyze=True, buffers=True))
```

```python
class Meta:
    indexes = [
        models.Index(fields=["tenant", "-created_at"], name="order_tenant_recent_idx",
                     include=["status", "total"]),                       # covering
        models.Index(fields=["created_at"], condition=Q(status="pending"),
                     name="order_pending_created_idx"),                  # partial
        BrinIndex(fields=["recorded_at"], name="event_recorded_brin"),   # append-only time series
    ]
```

## Anti-patterns

- Adding an index without checking the plan, or one index per column "just in case".
- Leading-wildcard `LIKE '%x%'` / `icontains` on large tables without a trigram (`pg_trgm`)
  GIN index or full-text search.
- Functions on indexed columns in `WHERE` (`UPPER(email) = ...`) without a matching expression index.
- `OFFSET` pagination deep into large tables; `COUNT(*)` on every page request.
- Long transactions (or `idle in transaction` sessions) blocking vacuum and DDL.
- `max_connections = 1000` instead of pooling.
- Running DDL that takes `ACCESS EXCLUSIVE` locks on hot tables without `lock_timeout`.

## Security and performance

- Application role has only DML on its schema; migrations run with a separate, more privileged
  role where the platform allows it. No superuser for the app.
- Require TLS to the database (`sslmode=verify-full` with a CA where possible).
- Row-level security is an option for strict multi-tenancy, but Django queries must then set the
  tenant per connection — design it deliberately, don't bolt it on.
- Parameterised queries only; see [django-security-engineering](../django-security-engineering/SKILL.md).

## Testing and production

- Run tests against PostgreSQL (same major version as production), never SQLite, when the code
  uses PostgreSQL features or constraints.
- Enable `pg_stat_statements` and slow-query logging (`log_min_duration_statement`) in production.
- Before a migration on a large table, rehearse on a production-sized copy and record the lock
  and duration; see [django-orm migrations](../django-orm/references/migrations.md).
- Backups with point-in-time recovery, and a tested restore — owned by the platform, verified by the team.

## References

- [references/indexing-and-query-plans.md](references/indexing-and-query-plans.md) — index
  types, column order, EXPLAIN reading, full-text and trigram search, JSONB.
- [references/transactions-locking-connections.md](references/transactions-locking-connections.md)
  — isolation levels, lock modes, deadlocks, timeouts, pooling, PgBouncer, monitoring queries.
- Reused plugin skills: `db-query-tuning`, `db-schema-conventions` (`plugins/database`).
- PostgreSQL docs: [Indexes](https://www.postgresql.org/docs/current/indexes.html),
  [EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html),
  [Transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html),
  [Explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html),
  [Routine vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html).
- Django docs: [PostgreSQL notes](https://docs.djangoproject.com/en/stable/ref/databases/#postgresql-notes),
  [contrib.postgres](https://docs.djangoproject.com/en/stable/ref/contrib/postgres/).

## Related skills

[django-orm](../django-orm/SKILL.md) · [django-performance](../django-performance/SKILL.md) ·
[django-report-generation](../django-report-generation/SKILL.md)
