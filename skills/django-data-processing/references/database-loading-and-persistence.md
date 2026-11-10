# Database Loading and Persistence

> **Scope:** Getting validated, transformed records into PostgreSQL correctly and repeatably:
> staging tables, bulk insert/update, UPSERT (`update_conflicts`, `ON CONFLICT`, `MERGE`), COPY,
> conflict handling, transaction boundaries (single vs batched commits), idempotent loading,
> deduplication, FK/reference resolution, constraints as final guard, rollback strategies and
> staging→production promotion. **Not here:** ORM bulk internals and transaction API details
> ([django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md),
> [transactions](../../django-orm/references/transactions-and-concurrency.md)), COPY/vacuum/
> partitioning internals ([postgresql-engineering](../../postgresql-engineering/SKILL.md)), row
> validation ([data-validation-and-cleansing.md](data-validation-and-cleansing.md)), streaming and
> batching of input ([large-dataset-processing.md](large-dataset-processing.md)).

## 1. Purpose and scope

The load step is where data processing errors become permanent business facts: duplicated
payments, orphaned order lines, overwritten customer records. This reference defines how to load
so that every run is **idempotent** (re-running the same input yields the same target state),
**bounded** (locks and transactions are short) and **recoverable** (a failed run can be resumed or
reversed by run id).

Load path options, from simplest to most scalable:

1. `bulk_create` / `bulk_update` batches directly into the target model.
2. `bulk_create(update_conflicts=True, ...)` UPSERT batches.
3. Raw `INSERT … ON CONFLICT` / `MERGE` for logic the ORM cannot express.
4. COPY into a staging table, then set-based promotion (`INSERT … SELECT`, `UPDATE … FROM`, `MERGE`).

## 2. Core concepts

| Concept | Definition |
|---|---|
| Staging table | Intermediate table holding raw/validated rows for a run before promotion |
| Natural key | Business identifier unique per tenant (e.g. `(tenant_id, employee_number)`) used for UPSERT |
| Run id | UUID of the `ImportJob`/`PipelineRun` stamped on staged and loaded rows |
| Content hash | Hash of the normalised payload; equal hash ⇒ no-op update can be skipped |
| UPSERT | Insert-or-update on conflict of a unique constraint |
| Promotion | Set-based move of rows from staging to production tables |
| Partial commit | Some batches committed while later ones failed |
| Compensating action | Reversal performed after commit (e.g. delete rows by run id) |

Idempotency concept and delivery semantics are owned by
[data-processing-fundamentals.md](data-processing-fundamentals.md); this file implements it at the
storage layer.

## 3. Internal mechanics

### 3.1 What bulk operations skip

`bulk_create()` and `bulk_update()` [Django] issue SQL directly: **no `Model.save()`, no
`pre_save`/`post_save` signals, no `full_clean()`**. `bulk_create` cannot write M2M and does not
support multi-table-inheritance children. `QuerySet.update()` sends no signals either. Anything
those hooks did — search indexing, audit rows, cache invalidation, computed fields — must be done
explicitly per batch, and validation must have happened earlier in the pipeline.

### 3.2 `bulk_create` UPSERT

`bulk_create(objs, update_conflicts=True, unique_fields=[...], update_fields=[...])`
(Django ≥ 4.1) compiles on PostgreSQL to
`INSERT … VALUES (…),(…) ON CONFLICT (unique_fields) DO UPDATE SET f = EXCLUDED.f`. PostgreSQL
raises `ON CONFLICT DO UPDATE command cannot affect row a second time` if two rows in one statement
share a conflict key — **dedupe within the batch first**. With `ignore_conflicts=True` returned
objects have no PKs; whether PKs are populated with `update_conflicts=True` is version/backend
dependent (VERIFY: for Django 5.2 + PostgreSQL). `unique_fields` must match a unique constraint or
unique index exactly (partial unique indexes need raw SQL with the `WHERE` predicate — VERIFY: Django support for partial-index conflict targets).

### 3.3 `bulk_update` SQL shape

`bulk_update(objs, fields, batch_size=n)` produces per batch one
`UPDATE t SET f = CASE WHEN id=1 THEN … WHEN id=2 THEN … END WHERE id IN (…)`. The statement
grows with `n × len(fields)`; planning and execution degrade on large batches, so keep `batch_size`
in the hundreds to low thousands. It returns the number of rows matched (Django ≥ 4.0). It cannot
change PKs. For 10⁵+ updates, stage the new values and run one `UPDATE … FROM staging`.

### 3.4 COPY

`COPY t (cols) FROM STDIN` streams rows in the PostgreSQL wire format with almost no per-row
statement overhead; it is typically several times faster than multi-row INSERT (VERIFY: with your
own benchmark). psycopg 3 [third-party: psycopg] exposes it as
`with cursor.copy("COPY t (a, b) FROM STDIN") as copy: copy.write_row((a, b))`. COPY is
all-or-nothing per statement and has no conflict handling (PostgreSQL ≥ 17 adds `ON_ERROR ignore`
for data type errors only — VERIFY: option name and scope), so COPY into **staging**, never straight into production
tables with unique constraints.

### 3.5 Transactions, WAL and bloat

Every inserted/updated row writes WAL and, for updates, a new tuple version; the old version is
dead until VACUUM, which cannot reclaim tuples newer than the oldest open snapshot. A long
transaction therefore (a) holds row locks on everything it updated, (b) holds back vacuum
cluster-wide, (c) commits nothing if it fails at 95 %. Batched commits trade atomicity for short
locks and incremental progress.

### 3.6 Constraint checks

Unique, FK, CHECK and NOT NULL constraints are evaluated per row at statement time (or at commit
for `DEFERRABLE INITIALLY DEFERRED` constraints). FK checks each perform an index lookup on the
referenced table; on huge loads into empty tables, building indexes after load can be faster,
but in a live multi-tenant table you keep constraints — they are the final integrity guard.
See [django-orm constraints](../../django-orm/references/database-constraints-and-indexes.md).

## 4. Engineering decisions and trade-offs

### 4.1 Single transaction vs multiple committed batches

| Dimension | Single transaction | Multiple committed batches |
|---|---|---|
| Atomicity | All-or-nothing; no partial state visible | Partial state visible until run completes; needs run-id rollback |
| Throughput | Good for small loads; degrades as lock/WAL pressure grows | Steady; enables parallel batches |
| Lock duration | Locks held until end — blocks concurrent writers for minutes | Locks held per batch (ms–s) |
| WAL / bloat | One huge WAL burst, vacuum held back for whole run | WAL spread out; vacuum can progress |
| Recovery | Failure discards everything; re-run from scratch | Resume from last committed batch (checkpoint) |
| Consistency for readers | Readers see old or new, never mixed | Readers may see a mix — hide via staging/promotion or `is_active` flag |

Rule of thumb: single transaction when the load is small (≈ < 50 k rows / < 10 s, VERIFY: against
your lock tolerance) **and** business needs all-or-nothing (e.g. a payroll run). Otherwise batched
commits, with all-or-nothing semantics recovered by **staging + one short promotion transaction**.

### 4.2 Choosing a load method

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| < 100 k new rows, ORM models, no conflicts | `bulk_create(batch_size=1000–5000)` | Need signals → explicit side effects |
| Insert-or-update by natural key | `bulk_create(update_conflicts=…)` | Conditional update logic → raw `ON CONFLICT … WHERE` |
| Update changed fields of known rows | `bulk_update` modest batches | > 100 k rows → staging + `UPDATE … FROM` |
| Millions of rows, nightly | COPY → staging → set-based promotion | Small interactive imports (overkill) |
| Insert/update/delete in one sync | `MERGE` (PostgreSQL ≥ 15) | Need `RETURNING` on older PG (MERGE `RETURNING` is PG ≥ 17 — VERIFY: version) |
| Must be all-or-nothing and large | Staging + single promotion transaction | Promotion itself too long → partition promotion |

### 4.3 Rollback strategies

| Strategy | How | Use when | Cost |
|---|---|---|---|
| Transactional rollback | Exception inside `atomic()` | Single-transaction loads | Lose all work |
| Job-scoped soft rollback | Rows carry `import_job_id`; mark `is_active=False`/status reverted | Users must "undo import" days later | Every query must filter active rows |
| Compensating delete by run id | `DELETE … WHERE import_job_id = %s` (batched) | Inserted-only loads | Cannot restore overwritten values |
| Restore from history | Keep prior values in a history/audit table | Updates must be undoable | Storage, complexity |

Updates are not reversible by delete: if "undo" is a requirement, store before-images
(`previous_values` JSON per row in a history table) at load time.

## 5. Django/Python implementation patterns

### 5.1 Staging model keyed by run id

```python
import uuid
from django.db import models

class EmployeeStaging(models.Model):
    """Unmanaged: created by a migration with RunSQL as UNLOGGED."""
    run_id = models.UUIDField(db_index=True)
    tenant_id = models.BigIntegerField()
    employee_number = models.CharField(max_length=32)
    full_name = models.CharField(max_length=200)
    department_code = models.CharField(max_length=20)
    salary = models.DecimalField(max_digits=12, decimal_places=2)
    row_hash = models.CharField(max_length=64)
    source_line = models.PositiveIntegerField()

    class Meta:
        managed = False
        db_table = "staging_employee"
```

```sql
CREATE UNLOGGED TABLE staging_employee (
    id bigserial PRIMARY KEY, run_id uuid NOT NULL, tenant_id bigint NOT NULL,
    employee_number varchar(32) NOT NULL, full_name varchar(200) NOT NULL,
    department_code varchar(20) NOT NULL, salary numeric(12,2) NOT NULL,
    row_hash char(64) NOT NULL, source_line integer NOT NULL);
CREATE INDEX ON staging_employee (run_id, tenant_id, employee_number);
```

UNLOGGED skips WAL (fast) but is truncated after a crash and not replicated to standbys — fine for
staging, which is re-creatable from the source file. `TEMP` tables are per-session: they vanish
when the connection is returned to a pooler, so avoid them with PgBouncer transaction pooling.
Delete staging rows by `run_id` after promotion (or `TRUNCATE` if one run at a time).

### 5.2 COPY into staging (psycopg 3)

```python
from django.db import connection, transaction

def copy_to_staging(run_id: uuid.UUID, tenant_id: int, rows: Iterable[EmployeeRow]) -> int:
    n = 0
    with transaction.atomic(), connection.cursor() as cur:
        raw = cur.cursor   # underlying psycopg 3 cursor (VERIFY: attribute on your Django version)
        with raw.copy(
            "COPY staging_employee (run_id, tenant_id, employee_number, full_name, "
            "department_code, salary, row_hash, source_line) FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row((run_id, tenant_id, r.number, r.name, r.dept,
                                r.salary, r.row_hash, r.line))
                n += 1
    return n
```

Table and column names are literals here. If they come from configuration, allow-list them and
compose with `psycopg.sql.Identifier` — never f-strings with user input.

### 5.3 Dedup within batch, then UPSERT

```python
def dedupe_last_wins(rows: list[Customer], key=lambda c: (c.tenant_id, c.email)) -> list[Customer]:
    seen: dict[tuple, Customer] = {}
    for r in rows:                     # file order: later row wins (document this rule)
        seen[key(r)] = r
    return list(seen.values())

@transaction.atomic
def upsert_customers(batch: list[Customer]) -> None:
    Customer.objects.bulk_create(
        dedupe_last_wins(batch),
        update_conflicts=True,
        unique_fields=["tenant", "email"],          # must match a UniqueConstraint
        update_fields=["name", "credit_limit", "row_hash", "last_import_job"],
        batch_size=2000,
    )
```

Duplicates within a file are also a data-quality signal: record them as warnings via
[data-validation-and-cleansing.md](data-validation-and-cleansing.md) rather than silently merging.

### 5.4 Raw ON CONFLICT that skips no-op updates

```python
UPSERT_SQL = """
INSERT INTO crm_customer (tenant_id, email, name, credit_limit, row_hash, last_import_job_id)
SELECT s.tenant_id, s.email, s.name, s.credit_limit, s.row_hash, %(job)s
FROM staging_customer s WHERE s.run_id = %(run)s AND s.tenant_id = %(tenant)s
ON CONFLICT (tenant_id, email) DO UPDATE
SET name = EXCLUDED.name, credit_limit = EXCLUDED.credit_limit,
    row_hash = EXCLUDED.row_hash, last_import_job_id = EXCLUDED.last_import_job_id
WHERE crm_customer.row_hash IS DISTINCT FROM EXCLUDED.row_hash
"""

def promote_customers(run_id: uuid.UUID, tenant_id: int, job_id: int) -> int:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(UPSERT_SQL, {"run": run_id, "tenant": tenant_id, "job": job_id})
        return cur.rowcount     # inserted + actually-changed rows
```

The `WHERE … IS DISTINCT FROM` clause makes re-runs cheap and idempotent: unchanged rows produce
no new tuple version, no WAL, no bloat, and `updated_at` triggers do not fire. Staging must
already be deduplicated per key (`SELECT DISTINCT ON (tenant_id, email) … ORDER BY …, source_line DESC`).

### 5.5 MERGE for full synchronisation (PostgreSQL ≥ 15)

```sql
MERGE INTO hr_employee AS t
USING (SELECT DISTINCT ON (employee_number) * FROM staging_employee
       WHERE run_id = %(run)s AND tenant_id = %(tenant)s
       ORDER BY employee_number, source_line DESC) AS s
ON t.tenant_id = s.tenant_id AND t.employee_number = s.employee_number
WHEN MATCHED AND t.row_hash IS DISTINCT FROM s.row_hash THEN
    UPDATE SET full_name = s.full_name, salary = s.salary, row_hash = s.row_hash
WHEN NOT MATCHED THEN
    INSERT (tenant_id, employee_number, full_name, salary, row_hash)
    VALUES (s.tenant_id, s.employee_number, s.full_name, s.salary, s.row_hash);
```

`MERGE` is not a drop-in for `ON CONFLICT` under concurrency: concurrent inserts of the same key
can raise unique violations rather than being merged (VERIFY: against PostgreSQL docs for your
version). Use it in a single-writer-per-tenant job; use `ON CONFLICT` when writers race.
"Delete rows missing from source" (`WHEN NOT MATCHED BY SOURCE`, PostgreSQL ≥ 17 — VERIFY: version) is
dangerous with partial files — require an explicit full-snapshot flag and a max-delete threshold.

### 5.6 Foreign-key resolution per batch

```python
def resolve_departments(tenant_id: int, codes: set[str],
                        cache: dict[str, int]) -> tuple[dict[str, int], set[str]]:
    missing = codes - cache.keys()
    if missing:
        cache.update(Department.objects.filter(tenant_id=tenant_id, code__in=missing)
                     .values_list("code", "pk"))
    unresolved = codes - cache.keys()
    return cache, unresolved
```

One query per batch, a run-scoped natural-key cache (bounded: reference tables are small; if not,
use an LRU or resolve in SQL via a join in promotion). Unresolved FK policy must be explicit per
importer: **reject row** (default), **create placeholder** (only for owned reference data, flagged
for review), or **defer** (park in staging and retry after the parent import). Never silently null
a required FK.

### 5.7 Batched load with checkpoint and error classification

```python
from django.db import DatabaseError, IntegrityError, OperationalError

def load_in_batches(job: ImportJob, batches: Iterator[list[Row]]) -> None:
    for n, batch in enumerate(batches):
        if n < job.checkpoint_batch:          # resume: skip already-committed batches
            continue
        try:
            with transaction.atomic():
                upsert(batch, job)
                ImportJob.objects.filter(pk=job.pk).update(checkpoint_batch=n + 1)
        except IntegrityError:
            # permanent for some rows: bisect the batch to isolate offenders
            isolate_bad_rows(batch, job)
        except OperationalError:
            raise                             # transient (deadlock, connection): task retries
```

Committing the checkpoint **in the same transaction** as the data makes resume exact. Bisection
(split batch in halves, retry each with savepoints) isolates bad rows in O(k·log n) statements;
details in [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

## 6. Production use cases

- **Payroll run (all-or-nothing):** 30 k payslip lines validated fully into staging; one promotion
  transaction inserts all lines and flips the `PayrollRun` to POSTED. A failure leaves nothing visible.
- **ERP→CRM customer sync (nightly, 4 M rows):** COPY into UNLOGGED staging, `DISTINCT ON` dedupe,
  `ON CONFLICT … WHERE row_hash IS DISTINCT FROM` — typically < 2 % rows actually written.
- **Hospital doctor registry:** MERGE per hospital tenant; deletions disabled unless the feed
  declares a full snapshot and the delete count is under 5 % of the roster.
- **Inventory movements (append-only ledger):** `bulk_create` with `ignore_conflicts` on
  `(tenant, source_system, movement_id)` — duplicates from at-least-once delivery dropped safely.
- **Product catalog with categories:** categories imported first; product loader resolves category
  codes per batch, defers unresolved rows to a retry pass after the category import completes.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| ORM bulk ops | Portable, typed, simple | Skip hooks; per-batch statements; `bulk_update` scales poorly |
| Raw ON CONFLICT | Conditional updates, no-op skip, set-based | PostgreSQL-specific SQL to maintain and test |
| MERGE | Insert/update/delete in one statement | Concurrency semantics differ; PG ≥ 15 |
| COPY + staging | Fastest bulk path; enables set-based validation/promotion | Extra tables, raw SQL, psycopg-specific |
| Batched commits | Short locks, resumable | Partial visibility; needs run-id rollback |
| Single transaction | True atomicity | Long locks, bloat, total loss on failure |

## 8. Security and performance considerations

**Security**
- All raw SQL uses driver parameters (`%(name)s`); never interpolate values. Identifiers (table,
  column, sort direction) come from an allow-list and are composed with `psycopg.sql.Identifier`
  ([injection prevention](../../django-security-engineering/references/injection-prevention.md)).
- Every staging and promotion statement filters by `tenant_id` **and** `run_id`; a missing tenant
  predicate in `INSERT … SELECT` leaks or overwrites another tenant's data.
- Restrict which fields an import may overwrite (`update_fields` allow-list per importer); never let
  a file set `tenant_id`, `is_staff`, balances or status fields not in the importer schema.
- Staging tables contain raw, possibly sensitive data — purge after promotion and apply retention.

**Performance**
- Skip no-op updates (`IS DISTINCT FROM` on a row hash) — the single biggest win for re-syncs.
- Index the conflict target; avoid extra indexes on staging beyond `(run_id, key)`.
- Batch size 1 000–5 000 for INSERT/UPSERT, smaller for `bulk_update`; measure
  ([data-processing-performance.md](data-processing-performance.md)).
- Avoid triggers on target tables during bulk loads unless essential; each fires per row.
- Run `ANALYZE` on staging after large COPY before complex promotion joins.

## 9. Common mistakes and anti-patterns

- Duplicate keys in one UPSERT batch → "cannot affect row a second time" → dedupe within batch first.
- Relying on `post_save` for audit/search after `bulk_create` → silent gaps → explicit per-batch side effects.
- One `atomic()` around a 2 M-row loop → lock waits, vacuum held back, total loss on failure → batched commits + staging promotion.
- Checkpoint written outside the data transaction → duplicates or gaps on resume → commit checkpoint with the batch.
- Per-row FK lookups → N+1 queries → one `filter(code__in=…)` per batch with a cache.
- `ignore_conflicts=True` then using returned PKs → `None` PKs, broken child inserts → re-query by natural key.
- COPY directly into production with unique constraints → whole COPY aborts on one dup → COPY to staging.
- `TEMP` staging table behind PgBouncer → table disappears between statements → UNLOGGED table keyed by run id.
- "Undo import" by deleting rows that were updated (not inserted) → data loss → soft rollback or before-images.
- f-string table names from config → SQL injection → allow-list + `sql.Identifier`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `ON CONFLICT DO UPDATE command cannot affect row a second time` | Duplicate key in batch | Dedupe; log duplicate count |
| `there is no unique or exclusion constraint matching the ON CONFLICT specification` | `unique_fields` not matching a constraint | Add `UniqueConstraint`; check partial index predicate |
| Load slows over time | Index bloat, dead tuples, growing `bulk_update` CASE | `pg_stat_user_tables.n_dead_tup`; smaller batches; staging + `UPDATE … FROM` |
| Deadlocks between parallel loaders | Overlapping keys in different orders | Partition by key/tenant; sort batch by key |
| Re-run doubles rows | No natural-key constraint, or run-id used as key | Unique constraint on natural key |
| Re-run rewrites every row | No hash comparison | `IS DISTINCT FROM` guard |
| Unresolved FK spikes | Parent import not yet run, code mapping changed | Defer policy; check reference mapping version |

Validation checklist:
- [ ] Natural-key `UniqueConstraint` exists for every UPSERT target.
- [ ] Running the same file twice yields identical row counts and zero updates the second time.
- [ ] Killing the loader mid-run and resuming yields no duplicates or gaps.
- [ ] Rollback-by-run-id tested for insert-only and update cases.
- [ ] All raw SQL parameterised; identifiers allow-listed; every statement tenant-filtered.
- [ ] Post-load count/aggregate reconciliation ([data-reconciliation-engineering.md](data-reconciliation-engineering.md)).

## 11. Related references and skills

- [large-dataset-processing.md](large-dataset-processing.md) — streaming input into batches.
- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — `RejectedRecord`, row rules.
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — where load sits in the pipeline.
- [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) — bisection, compensation.
- [data-reconciliation-engineering.md](data-reconciliation-engineering.md) — post-load verification.
- [data-import-and-export-systems.md](data-import-and-export-systems.md) — `ImportJob` and undo UX.
- [django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md),
  [transactions](../../django-orm/references/transactions-and-concurrency.md),
  [constraints and indexes](../../django-orm/references/database-constraints-and-indexes.md),
  [migrations](../../django-orm/references/migrations-and-schema-evolution.md).
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — COPY internals, vacuum, partitioning.
- [django-security-engineering injection prevention](../../django-security-engineering/references/injection-prevention.md).

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-update
- https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- https://docs.djangoproject.com/en/5.2/topics/db/sql/
- https://docs.djangoproject.com/en/5.2/ref/models/options/#managed
- https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT
- https://www.postgresql.org/docs/current/sql-merge.html
- https://www.postgresql.org/docs/current/sql-copy.html
- https://www.postgresql.org/docs/current/sql-createtable.html#SQL-CREATETABLE-UNLOGGED
- https://www.postgresql.org/docs/current/populate.html
- https://www.postgresql.org/docs/current/functions-comparison.html
