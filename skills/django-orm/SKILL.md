---
name: django-orm
description: >-
  Django ORM engineering specialist — ORM architecture and QuerySet internals, model design,
  fields and relationships, the QuerySet API, expressions, aggregation and annotation, managers,
  database constraints and indexes, migrations and schema evolution, transactions and
  concurrency, query optimisation, raw SQL, bulk operations and ORM anti-patterns. Load when
  defining or changing models, writing or reviewing non-trivial queries, deciding transaction
  boundaries or locking, authoring migrations, or diagnosing slow or incorrect database access.
project: django-agentic-engineering
abstract: >-
  The ORM is the data-access layer; the database enforces invariants; queries are designed from
  their SQL and measured, not guessed. A concise workflow and navigation guide here; sixteen
  focused references under references/ loaded one to three at a time, only when the task needs them.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Data]
---

# Django ORM Engineering Specialist

Engineering judgement for Django's data layer: model the data so the database protects it, write
queries whose SQL you can predict, keep transactions short and explicit, evolve the schema without
downtime, and optimise only what measurement shows is slow. Baseline: **Django 5.2 LTS**;
version-specific APIs are qualified "(Django ≥ X.Y)" in the references. Check the project's
version (`python -m django --version` or the lock file) and its database backend before
recommending a version- or backend-specific feature.

## Activate when

- Adding or changing models, fields, relationships, constraints or indexes.
- Writing queries beyond simple `filter()` — expressions, subqueries, aggregation, window functions.
- Investigating N+1 queries, slow queries, wrong counts/sums, or unexpected query volume.
- Deciding transaction boundaries, row locking, idempotency or concurrency control.
- Authoring, reviewing, squashing or deploying migrations, especially on large tables.
- Considering raw SQL, a custom manager/QuerySet, or bulk inserts/updates.

## Engineering philosophy

1. **The ORM is the default data-access layer.** No repository layer over it without a real seam.
2. **Raw SQL is a justified exception**, chosen when the ORM cannot express the query well or a
   measured gain exists — always parameterised.
3. **Know when SQL runs.** QuerySets are lazy; evaluation, caching and cloning are predictable.
4. **The database enforces integrity.** Constraints, `NOT NULL`, FKs and unique indexes — not only
   Python validation.
5. **Concurrency is designed, not hoped for.** Atomic updates, locks or version checks for every
   read-modify-write; transactions are short and owned by the use case.
6. **Measure before optimising.** Query count, query time, rows and plans — fewer queries is not
   automatically faster.
7. **Abstractions earn their place.** A custom manager/QuerySet only for reused, named query
   vocabulary or default scoping that must not be forgotten.
8. **Readable over clever.** A slightly longer query a reviewer understands beats a dense one.
9. **Migrations are production code.** Reviewed, reversible where possible, safe for the code
   version that is running while they apply.

## ORM engineering workflow

1. **Frame the task** — read/write path, data volume, concurrency, consistency requirement,
   Django version and backend.
2. **Load the matching references** from the table below — usually one to three, never all.
3. **Inspect only the affected models, QuerySets, services/selectors and migrations.**
4. **Design from the data outwards**: model and constraints → query shape (predict the SQL) →
   transaction and locking → migration plan.
5. **Implement** with the canonical patterns in the references; keep transactions in the service
   that owns the use case.
6. **Measure and validate** — query count/time in tests, `QuerySet.explain()` for hot queries,
   `makemigrations --check`, `sqlmigrate` for risky migrations.
7. **Explain** the trade-offs, backend assumptions and anything marked `VERIFY`.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [orm-fundamentals.md](references/orm-fundamentals.md) | ORM architecture, model↔table mapping, QuerySet lifecycle overview, connections, backends, ORM vs SQL |
| [model-design.md](references/model-design.md) | Inheritance (abstract/multi-table/proxy), Meta, primary keys, validation, model responsibilities |
| [field-types-and-options.md](references/field-types-and-options.md) | Field types, `null`/`blank`, defaults, choices, `db_index`/`unique`, custom fields, validators |
| [model-relationships.md](references/model-relationships.md) | FK/O2O/M2M, through models, reverse names, `on_delete`, self-referential relations |
| [queryset-internals.md](references/queryset-internals.md) | Laziness, cloning, result cache, evaluation triggers, `iterator()`, SQL compilation |
| [queryset-api.md](references/queryset-api.md) | Per-method semantics, return types and SQL of the QuerySet API |
| [advanced-query-expressions.md](references/advanced-query-expressions.md) | `F`, `Q`, `Case/When`, `Subquery`/`Exists`/`OuterRef`, `Window`, `Func`, custom expressions |
| [aggregation-and-annotation.md](references/aggregation-and-annotation.md) | `aggregate`/`annotate`, GROUP BY, filtered aggregates, join fan-out errors |
| [managers-and-querysets.md](references/managers-and-querysets.md) | Custom managers/QuerySets, `as_manager()`, base managers, soft delete, tenant scoping |
| [database-constraints-and-indexes.md](references/database-constraints-and-indexes.md) | Unique/check/conditional/deferrable constraints, index types and selection |
| [migrations-and-schema-evolution.md](references/migrations-and-schema-evolution.md) | Migration mechanics, data migrations, squashing, zero-downtime, expand/contract, rollback |
| [transactions-and-concurrency.md](references/transactions-and-concurrency.md) | `atomic`, savepoints, `on_commit`, isolation, locking, races, deadlocks, retries |
| [query-optimization.md](references/query-optimization.md) | N+1, `Prefetch`, column trimming, pagination, `explain()`, profiling, pooling |
| [raw-sql-and-database-access.md](references/raw-sql-and-database-access.md) | `raw()`, `RawSQL`, `cursor()`, parameterisation, safe dynamic SQL |
| [bulk-operations-and-batching.md](references/bulk-operations-and-batching.md) | `bulk_create`/`bulk_update`, batching, upserts, bypassed hooks, parameter limits |
| [orm-anti-patterns.md](references/orm-anti-patterns.md) | Catalogue of ORM anti-patterns with detection and fixes, linking to owners |

## Task-based loading

| Task | Load |
|---|---|
| Understanding how the ORM turns code into SQL | orm-fundamentals.md, queryset-internals.md |
| Designing new models | model-design.md, field-types-and-options.md |
| Modelling relationships (orders, customers, products, inventory) | model-relationships.md (+ field-types-and-options.md) |
| Enforcing business invariants in the database | database-constraints-and-indexes.md (+ model-design.md for validation) |
| Writing everyday queries | queryset-api.md |
| Writing complex queries (conditions, subqueries, ranking) | queryset-api.md, advanced-query-expressions.md |
| Reports, dashboards, totals and counts | aggregation-and-annotation.md (+ advanced-query-expressions.md) |
| Wrong counts or sums after adding a join | aggregation-and-annotation.md |
| Debugging N+1 or unexpected queries | query-optimization.md, queryset-internals.md |
| Slow query in production | query-optimization.md, database-constraints-and-indexes.md |
| Choosing indexes for a query pattern | database-constraints-and-indexes.md (+ query-optimization.md) |
| Handling concurrent updates (stock, balances, payments) | transactions-and-concurrency.md, database-constraints-and-indexes.md |
| Making an operation idempotent / retry-safe | transactions-and-concurrency.md |
| Reusable query vocabulary, soft delete, tenant scoping | managers-and-querysets.md |
| Planning production migrations | migrations-and-schema-evolution.md |
| Adding a constraint or index to a large table | migrations-and-schema-evolution.md, database-constraints-and-indexes.md |
| Designing bulk imports or mass updates | bulk-operations-and-batching.md (+ transactions-and-concurrency.md) |
| Needing SQL the ORM cannot express | raw-sql-and-database-access.md (+ advanced-query-expressions.md first) |
| Reviewing ORM code | orm-anti-patterns.md, then the owning reference for each finding |

## Handoffs

| Topic | Skill |
|---|---|
| General model lifecycle, `save()`/signals mechanics, app loading, caching framework, logging | [django-core](../django-core/SKILL.md) |
| App boundaries, service layer and transaction ownership, selectors, repositories | [django-architecture](../django-architecture/SKILL.md) |
| Serializers, API pagination, API-level query optimisation | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Application security, authorisation, threat modelling | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test strategy, fixtures/factories, query-count assertions in CI | [django-testing](../django-testing/SKILL.md) |
| Profiling, load testing, end-to-end performance work | [django-performance](../django-performance/SKILL.md) |
| PostgreSQL internals, plan reading in depth, vacuum, partitioning, replication | [postgresql-engineering](../postgresql-engineering/SKILL.md) |
| Import/export pipelines, ETL, large-file processing | [django-data-processing](../django-data-processing/SKILL.md) |

## Validation checklist

- [ ] Invariants that must always hold are database constraints, each with a stable `name`.
- [ ] Every read-modify-write is an atomic update, a locked read, or a version-checked write.
- [ ] Transactions are explicit, short, contain no network I/O; side effects use `on_commit`.
- [ ] Hot paths have a known query count (asserted in tests) and no N+1.
- [ ] New filters/orderings on large tables are backed by an index verified with `explain()`.
- [ ] Aggregations over multiple relations were checked for join fan-out.
- [ ] Raw SQL is justified, parameterised, and identifiers come from an allow-list.
- [ ] Bulk operations account for skipped `save()`, signals and validation.
- [ ] `makemigrations --check --dry-run` is clean; risky migrations reviewed with `sqlmigrate`
      and are safe for the currently deployed code.
- [ ] Version- and backend-specific features match the project's Django version and database.
- Docs: <https://docs.djangoproject.com/en/stable/topics/db/> (match the URL version to the project's).
