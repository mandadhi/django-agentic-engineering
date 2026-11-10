# Database Performance

**Scope:** Diagnosing and improving database-side cost for Django applications on PostgreSQL: index selection against real workloads, reading execution plans, slow-query analysis, connection management and pooling, lock contention, transaction duration and write amplification. PostgreSQL internals (planner, MVCC, vacuum, replication) are owned by [postgresql-engineering](../../postgresql-engineering/SKILL.md); index and constraint API mechanics by [django-orm database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md).

## 1. Purpose and scope

Once ORM-level waste is removed ([orm-performance-optimization.md](orm-performance-optimization.md)), remaining database cost comes from: queries that scan or sort too much, connections that are slow to obtain or exhausted, transactions that hold locks too long, and writes that do more work than necessary. This reference gives an agent the workflow to find which one dominates and the Django-side levers to fix it.

Covered: index selection (B-tree, composite, partial, covering, expression; GIN/BRIN briefly), index write/storage cost, `EXPLAIN`/`EXPLAIN ANALYZE`, `pg_stat_statements`, `auto_explain`, slow-query logging, `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`, Django's native PostgreSQL pool (Django ≥ 5.1) vs PgBouncer, lock contention, transaction duration, write amplification, read/write workload characterisation.

Not covered: pool **sizing** math and worker counts ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)), read replicas and database scaling ([scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)), PostgreSQL configuration tuning (`shared_buffers`, `work_mem`, autovacuum) ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

Backend note: examples are PostgreSQL. MySQL/MariaDB and SQLite differ materially (see §4 and §8); do not transplant PostgreSQL-specific advice without checking.

## 2. Core concepts

| Concept | Meaning for performance |
|---|---|
| Selectivity | Fraction of rows a predicate keeps. Indexes help when selectivity is low (few rows) or when they provide order for `ORDER BY ... LIMIT`. |
| Seq scan vs index scan | Seq scan is often the *correct* plan for small tables or low-selectivity predicates. "Seq Scan" alone is not a bug. |
| Index-only scan | Answer from the index without visiting the heap; requires all needed columns in the index and a mostly all-visible table (vacuumed). |
| Composite index order | B-tree `(a, b, c)` serves predicates on `a`, `a,b`, `a,b,c` and ordering by those prefixes; equality columns first, then range/sort columns. |
| Write amplification | Every INSERT/UPDATE touches the heap plus every index on the table (unless a HOT update), plus WAL, plus replicas. |
| Connection cost | A new PostgreSQL connection forks a backend process: typically single-digit milliseconds plus TLS and auth (illustrative); each backend consumes memory. |
| Lock wait | Time a statement spends blocked on another transaction's row or table lock — shows as latency, not CPU. |
| Transaction duration | Long transactions hold row locks, block vacuum cleanup and pin connections. |

Read-heavy OLTP (most Django apps: 90%+ reads, illustrative) benefits from targeted indexes and caching. Write-heavy workloads (event ingestion, counters, audit logs) are hurt by every extra index and benefit from batching and fewer indexes.

## 3. Internal mechanics

Only what an agent needs to reason about cost; depth in [postgresql-engineering](../../postgresql-engineering/SKILL.md).

- **Planner** chooses plans from table statistics (`ANALYZE`). Stale statistics after bulk loads produce bad row estimates → bad joins. Compare `rows=` estimated vs actual in `EXPLAIN ANALYZE`; >10× divergence is a red flag.
- **MVCC**: an UPDATE writes a new row version. If no indexed column changes and the page has free space, PostgreSQL performs a **HOT** update and skips index maintenance. Indexing a frequently updated column (e.g. `last_seen_at`) disables HOT for those updates.
- **Index maintenance**: each additional index adds work to every INSERT and to non-HOT UPDATEs, adds WAL volume, adds storage and competes for cache.
- **Locks**: `SELECT ... FOR UPDATE` and UPDATE/DELETE take row locks held until commit. DDL like `CREATE INDEX` (non-concurrent) takes a lock that blocks writes for the build duration.
- **Connections**: Django opens a connection lazily per thread per alias. With `CONN_MAX_AGE=0` it closes at the end of each request; with `CONN_MAX_AGE>0` it reuses it until the age limit; with `CONN_MAX_AGE=None` indefinitely. `CONN_HEALTH_CHECKS=True` (Django ≥ 4.1) checks a reused connection before the first query of a request.
- **Native pool** (Django ≥ 5.1, PostgreSQL with psycopg 3 + `psycopg[pool]`): `OPTIONS["pool"]` enables a `psycopg_pool.ConnectionPool` per process; connections are returned to the pool at request end instead of closed. Persistent connections (`CONN_MAX_AGE > 0`) are not supported together with pooling — `VERIFY` exact error against Django 5.1 docs.
- **Server-side cursors**: `QuerySet.iterator()` on PostgreSQL uses a named cursor that must live inside one transaction on one server connection — incompatible with transaction-mode PgBouncer unless disabled.

## 4. Engineering decisions and trade-offs

### Choosing an index

Start from the query, not the column:

1. Find the statement in `pg_stat_statements` ordered by `total_exec_time` (aggregate cost), not only `mean_exec_time`.
2. Read its plan: what is scanned, filtered, sorted?
3. Design the narrowest index that removes the expensive node.

| Query shape | Index | Notes |
|---|---|---|
| `WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT 20` | `(tenant_id, status, created_at)` | Equality columns first, sort column last; eliminates sort, stops after 20 rows. |
| `WHERE status = 'pending'` where 1% of rows are pending | Partial: `Index(fields=["created_at"], condition=Q(status="pending"))` | Small, cheap to maintain; only used when the query repeats the condition. |
| Listing needing 3 small columns | Covering: `Index(fields=["tenant_id", "created_at"], include=["reference", "total"])` (PostgreSQL) | Enables index-only scans; widens the index. |
| `WHERE lower(email) = ?` | Expression: `Index(Lower("email"), name="...")` | Query must use the same expression. |
| `JSONField` containment / array / full text | GIN (`django.contrib.postgres.indexes.GinIndex`) | Expensive writes; consider `fastupdate` and write rate. |
| Append-only time series, range scans by time | BRIN | Tiny, needs physical correlation with insertion order; poor for random updates. |

**When an index makes things worse:**

- Write-heavy tables: each index can add a measurable fraction to insert latency and WAL (benchmark; e.g. 8 indexes on an events table cut ingestion throughput by ~40%, illustrative).
- Indexing frequently updated columns disables HOT updates.
- Redundant indexes: `(a)` is redundant when `(a, b)` exists (for B-tree prefix use), but still costs writes.
- Low-selectivity indexes (`is_active` with 95% true) are ignored by the planner yet maintained on every write.
- Large index builds on hot tables without `CONCURRENTLY` block writes.

### EXPLAIN vs EXPLAIN ANALYZE

| | `EXPLAIN` | `EXPLAIN (ANALYZE, BUFFERS)` |
|---|---|---|
| Executes query | No | **Yes** — writes happen (wrap DML in `BEGIN ... ROLLBACK`) |
| Shows | Estimated costs and rows | Actual time, rows, loops, buffer hits/reads |
| Production safety | Safe | Adds the full query cost; timing overhead on some systems |

### Connection management

| Option | Use when | Trade-off |
|---|---|---|
| `CONN_MAX_AGE=0` | Very low traffic, or an external pooler handles reuse | Connect cost on every request |
| `CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS=True` | Sync WSGI workers with a stable count | One idle connection per worker thread; connections = workers × threads per host |
| Native pool `OPTIONS["pool"]` (Django ≥ 5.1) | psycopg 3 deployments wanting reuse without an extra hop; ASGI/threaded deployments | Pool is per process — total connections still multiply by processes; no cross-process sharing |
| PgBouncer (transaction mode) | Many processes/hosts, connection count is the bottleneck | Breaks session state: server-side cursors, session `SET`, advisory locks, `LISTEN`; prepared-statement handling depends on PgBouncer version (≥ 1.21 supports protocol-level prepared statements — `VERIFY` for your version) |
| PgBouncer (session mode) | Need session features | Little multiplexing benefit |

Sizing these (connections ≤ `max_connections` minus headroom, pool size vs worker threads) → [concurrency-and-worker-management.md](concurrency-and-worker-management.md).

### Transaction scope

- `ATOMIC_REQUESTS=True` wraps the whole view — including template rendering, serialization and any external HTTP call — in a transaction. Simple, but lengthens lock holding and connection pinning. Prefer explicit `transaction.atomic()` around the write in the service ([../../django-orm/references/transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md)).
- Never perform network I/O inside a transaction; use `transaction.on_commit`.

### Backend differences

| Feature | PostgreSQL | MySQL/MariaDB (InnoDB) | SQLite |
|---|---|---|---|
| Partial index (`condition=`) | Yes | Not supported — Django skips creating it (`VERIFY` behaviour per version) | Yes |
| Covering (`include=`) | Yes | No (InnoDB secondary indexes implicitly include PK) | No |
| Expression index | Yes | MySQL ≥ 8.0.13 functional key parts | Yes |
| Concurrent index build | `AddIndexConcurrently` | Online DDL via `ALGORITHM=INPLACE` (no Django operation) | N/A |
| Write concurrency | Row-level MVCC | Row-level, gap locks under REPEATABLE READ | Single writer per database |
| `select_for_update(skip_locked=True)` | Yes | MySQL ≥ 8.0.1 | No (ignored/unsupported) |

## 5. Django implementation patterns

### Index declarations driven by queries

```python
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

class Order(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE)
    status = models.CharField(max_length=20)
    reference = models.CharField(max_length=32)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    email = models.EmailField()
    created_at = models.DateTimeField(db_index=False)

    class Meta:
        indexes = [
            # Serves: tenant order list filtered by status, newest first
            models.Index(fields=["tenant", "status", "-created_at"], name="order_tenant_status_created"),
            # Serves: queue of pending orders only (~1% of rows)
            models.Index(fields=["created_at"], condition=Q(status="pending"), name="order_pending_created"),
            # Serves: index-only scans for the list API (PostgreSQL)
            models.Index(fields=["tenant", "created_at"], include=["reference", "total"],
                         name="order_tenant_created_cov"),
            # Serves: case-insensitive email lookup
            models.Index(Lower("email"), name="order_email_lower"),
        ]
```

Name every index; comment which query it serves so future reviewers can remove dead ones.

### Building indexes on large tables without blocking writes

```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

class Migration(migrations.Migration):
    atomic = False                       # CONCURRENTLY cannot run in a transaction
    dependencies = [("orders", "0042_previous")]
    operations = [
        AddIndexConcurrently(
            "order",
            models.Index(fields=["tenant", "status", "-created_at"], name="order_tenant_status_created"),
        ),
    ]
```

A failed concurrent build leaves an `INVALID` index that must be dropped and rebuilt. Deployment sequencing → [../../django-orm/references/migrations-and-schema-evolution.md](../../django-orm/references/migrations-and-schema-evolution.md).

### Plans from Django

```python
qs = Order.objects.filter(tenant_id=7, status="open").order_by("-created_at")[:20]
print(qs.explain(analyze=True, buffers=True))   # PostgreSQL options; executes the SELECT
```

What to look for:

| Plan signal | Likely cause | Action |
|---|---|---|
| `Seq Scan` + `Filter` removing most rows on a large table | Missing/unusable index | Composite or partial index matching predicates |
| `Sort` with `external merge Disk` | Sort exceeds `work_mem` | Index providing order, or fewer rows before sort |
| `Nested Loop` with large `loops=` and inner seq scan | Missing index on join key, bad estimate | Index FK column (Django indexes FKs by default), `ANALYZE` |
| Estimated rows ≪ actual | Stale or insufficient statistics | `ANALYZE`; extended statistics (postgresql-engineering) |
| `Buffers: shared read=` high | Data not in cache, I/O-bound | Covering index, smaller working set, caching |
| `Index Scan` but slow, `Heap Fetches` high | Index-only scan blocked by visibility map | Vacuum health (postgresql-engineering) |

### Slow-query visibility (PostgreSQL configuration, owned by DBAs/infra)

```sql
-- Top statements by aggregate cost (requires the pg_stat_statements extension)
SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time, 2) AS mean_ms,
       rows, left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

- `log_min_duration_statement = 500ms` logs statements above a threshold.
- `auto_explain` (`auto_explain.log_min_duration`, optionally `log_analyze`) logs plans of slow statements; `log_analyze` adds overhead to *all* statements — sample or enable temporarily.
- Tag queries with their origin: OpenTelemetry DB spans or SQL comments ([third-party: sqlcommenter]) make `pg_stat_statements`/logs attributable to views — see [profiling-and-observability.md](profiling-and-observability.md).

### Connection settings

```python
# Persistent connections (sync workers)
DATABASES["default"].update({
    "CONN_MAX_AGE": 60,
    "CONN_HEALTH_CHECKS": True,                         # Django ≥ 4.1
    "OPTIONS": {"options": "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000"},
})
```

```python
# Native pool (Django ≥ 5.1, psycopg 3 with the pool extra installed)
DATABASES["default"].update({
    "CONN_MAX_AGE": 0,
    "OPTIONS": {"pool": {"min_size": 2, "max_size": 8, "timeout": 5}},
})
```

```python
# Behind PgBouncer in transaction mode
DATABASES["default"].update({
    "CONN_MAX_AGE": 0,                        # or small; PgBouncer does the pooling
    "DISABLE_SERVER_SIDE_CURSORS": True,      # iterator() would break otherwise
})
```

`statement_timeout` caps runaway queries; set per role or per connection, and use a larger value for migration/maintenance roles.

### Lock contention

```sql
-- Who is blocking whom
SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
       now() - blocked.query_start AS waiting, left(blocked.query, 80) AS blocked_query,
       blocking.state AS blocking_state, left(blocking.query, 80) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid));

-- Long or idle-in-transaction sessions
SELECT pid, state, now() - xact_start AS xact_age, left(query, 80)
FROM pg_stat_activity
WHERE xact_start IS NOT NULL ORDER BY xact_age DESC LIMIT 10;
```

Queue-style work without contention:

```python
with transaction.atomic():
    job = (Job.objects.select_for_update(skip_locked=True)
           .filter(status="ready").order_by("id").first())
    if job:
        job.status = "running"
        job.save(update_fields=["status"])
```

`nowait=True` fails fast instead of waiting — useful for user-facing paths where waiting is worse than an error.

### Reducing write amplification

- `save(update_fields=[...])` writes fewer columns and avoids rewriting unchanged values.
- Move high-churn columns (`last_seen_at`, counters) to a narrow side table or to Redis with periodic flush, so the main table keeps HOT updates and fewer bloated indexes.
- Batch inserts (`bulk_create(batch_size=...)`) for ingestion; see [orm-performance-optimization.md](orm-performance-optimization.md).
- Drop unused indexes: `pg_stat_user_indexes.idx_scan = 0` over a full business cycle (check replicas too — stats are per server).

## 6. Production use cases

- **Tenant order list p95 2.1 s → 45 ms:** plan showed `Seq Scan` + `Sort` over 12 M rows. Composite `(tenant_id, status, created_at DESC)` built with `AddIndexConcurrently`; plan became `Index Scan ... Limit`. Insert latency rose ~4% (illustrative), accepted.
- **Ingestion throughput regression:** a GIN index added for an admin search on a 50 k inserts/min events table doubled WAL and replica lag. Moved search to a nightly-refreshed summary table; dropped the index.
- **"Too many connections" after scaling out:** 20 pods × 4 workers × 8 threads with `CONN_MAX_AGE=600` = 640 connections vs `max_connections=500`. Introduced PgBouncer transaction mode, set `DISABLE_SERVER_SIDE_CURSORS`; sizing in [concurrency-and-worker-management.md](concurrency-and-worker-management.md).
- **Checkout stalls:** a reporting job held a transaction open for 15 min (`idle in transaction`), blocking vacuum and an `ALTER TABLE` that queued every checkout behind its lock. Fix: `idle_in_transaction_session_timeout`, `lock_timeout` on migrations, job split into short batches.
- **Hot row contention:** a single `inventory` row updated per sale serialised checkouts (lock waits 300 ms at peak). Fix: atomic `F()` updates, and sharded counters for the busiest SKUs.

## 7. Advantages and disadvantages

| Lever | Advantages | Disadvantages |
|---|---|---|
| Composite/partial indexes | Large read gains for targeted queries | Write and storage cost; must match query shape exactly |
| Covering indexes | Index-only scans, fewer heap reads | Wider index, more cache pressure, PostgreSQL-only in Django |
| GIN/BRIN | Enable JSON/array/full-text, tiny range indexes | GIN write cost; BRIN needs correlation |
| `CONN_MAX_AGE` | Removes connect cost | Idle connections scale with workers × threads |
| Native pool (Django ≥ 5.1) | No extra infrastructure, bounded per process | Per-process only; new feature — less operational history |
| PgBouncer | Many clients on few server connections | Session-feature restrictions; another hop and component |
| `statement_timeout` | Bounds worst-case latency and resource usage | Kills legitimate long queries unless scoped per role |

## 8. Security and performance considerations

- Slow-query logs and `pg_stat_statements` may capture literal values (emails, tokens) in some configurations; control access and retention.
- A user-controlled filter or ordering on an unindexed column is a denial-of-service vector; allow-list orderings and back them with indexes ([api-performance.md](api-performance.md)).
- `statement_timeout` and `lock_timeout` are resilience controls: they turn unbounded waits into fast failures that callers can handle.
- Running `EXPLAIN ANALYZE` executes the statement with the session's privileges; never on DML in production outside a rolled-back transaction.
- MySQL/SQLite: much of the plan-reading guidance differs (`EXPLAIN FORMAT=JSON`/`ANALYZE` in MySQL ≥ 8.0.18; SQLite `EXPLAIN QUERY PLAN`). SQLite's single-writer model makes it unsuitable for write-concurrent production workloads.

## 9. Common mistakes and anti-patterns

- Adding single-column indexes for every filtered field instead of one composite index matching the real query.
- Wrong composite order (range column before equality column), so only a prefix is usable.
- Partial index whose condition the query does not repeat literally — planner cannot use it.
- `CREATE INDEX` without `CONCURRENTLY` on a large hot table in a normal migration.
- Treating every `Seq Scan` as a problem; forcing indexes on small tables.
- Judging indexes by `mean_exec_time` only — a 2 ms query called 5 M times/day outweighs a 3 s query called twice.
- `CONN_MAX_AGE=None` with many threads and no pooler → connection exhaustion; or with network middleboxes that kill idle connections and no `CONN_HEALTH_CHECKS`.
- PgBouncer transaction mode with server-side cursors left enabled → intermittent "cursor does not exist" errors.
- `ATOMIC_REQUESTS=True` on views that call external APIs.
- Never removing indexes; never checking `idx_scan`.

## 10. Troubleshooting and validation

**Is the database the bottleneck?** Check DB span time as a share of request time (APM), DB CPU, active sessions vs connection limit, wait events in `pg_stat_activity.wait_event_type` (`Lock`, `IO`, `LWLock`, `Client`).

| Symptom | First check | Typical cause |
|---|---|---|
| One endpoint slow, DB time dominant | `explain(analyze=True, buffers=True)` | Missing/mismatched index, bad estimate |
| Many endpoints slow at peak | `pg_stat_activity` states and waits | Lock contention, CPU saturation, I/O |
| Errors "too many clients" / pool timeouts | Connection count per host | Workers × threads × pools exceeds limits |
| Latency spikes during deploys | Lock waits | Blocking DDL in migrations |
| Gradual slowdown over weeks | Table/index bloat, stale stats | Vacuum/analyze health (postgresql-engineering) |
| Writes slower after a release | New indexes, triggers, wider rows | Write amplification |

**Validation of an index change**

- [ ] Plan before/after captured on production-like data volume and statistics.
- [ ] Target query p95 improved; measured on the real endpoint, not only in `psql`.
- [ ] Write latency/throughput of the table measured before/after (insert benchmark or production metrics).
- [ ] Index size checked (`pg_relation_size`) and fits the cache budget.
- [ ] Built concurrently on large tables; no `INVALID` index left.
- [ ] `idx_scan` confirms use after a business cycle; otherwise remove.

**Validation of a connection change**

- [ ] Peak total connections computed and below `max_connections` with headroom.
- [ ] Pool wait time / timeout metrics monitored.
- [ ] `iterator()` and session-state features tested behind the pooler.

## 11. Related references and skills

- [orm-performance-optimization.md](orm-performance-optimization.md) — reduce queries before tuning them.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — pool and connection sizing.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — read replicas, database scaling limits.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — incident workflows.
- [profiling-and-observability.md](profiling-and-observability.md) — query timing and attribution.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — planner, MVCC, vacuum, configuration, partitioning.
- [django-orm](../../django-orm/SKILL.md): [database-constraints-and-indexes.md](../../django-orm/references/database-constraints-and-indexes.md), [migrations-and-schema-evolution.md](../../django-orm/references/migrations-and-schema-evolution.md), [transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md).

## 12. Official documentation and external references

- Django database settings (`CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`, `DISABLE_SERVER_SIDE_CURSORS`): https://docs.djangoproject.com/en/5.2/ref/settings/#databases
- Django PostgreSQL notes (connection pool, server-side cursors, PgBouncer): https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes
- Persistent connections: https://docs.djangoproject.com/en/5.2/ref/databases/#persistent-connections
- Model index reference (`condition`, `include`, expressions): https://docs.djangoproject.com/en/5.2/ref/models/indexes/
- PostgreSQL-specific indexes and operations: https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/indexes/ · https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/operations/
- `QuerySet.explain()`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#explain
- Django 5.1 release notes (connection pool): https://docs.djangoproject.com/en/5.2/releases/5.1/
- PostgreSQL: EXPLAIN https://www.postgresql.org/docs/current/sql-explain.html · Using EXPLAIN https://www.postgresql.org/docs/current/using-explain.html · Indexes https://www.postgresql.org/docs/current/indexes.html · pg_stat_statements https://www.postgresql.org/docs/current/pgstatstatements.html · auto_explain https://www.postgresql.org/docs/current/auto-explain.html · Lock monitoring https://www.postgresql.org/docs/current/monitoring-stats.html · Runtime config (timeouts, logging) https://www.postgresql.org/docs/current/runtime-config-client.html
- psycopg 3 connection pool: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- [third-party] PgBouncer features and modes: https://www.pgbouncer.org/features.html
- MySQL EXPLAIN: https://dev.mysql.com/doc/refman/8.0/en/explain.html · SQLite query planner: https://www.sqlite.org/eqp.html
