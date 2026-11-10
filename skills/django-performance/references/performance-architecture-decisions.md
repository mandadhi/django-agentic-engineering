# Performance Architecture Decisions

**Scope:** Structural, hard-to-reverse choices made for performance reasons — process
boundaries, sync vs async execution, cache vs database reads, denormalisation, precomputation,
materialised views, read models/CQRS, event-driven processing and cost. Each is framed as a
decision record with forces, options, measurement criteria and consequences. General
architecture patterns are owned by [django-architecture](../../django-architecture/SKILL.md).

## 1. Purpose and scope

Local optimisations (an index, a prefetch) are cheap to undo; architectural ones (a read model,
a second service, an event pipeline) are not. This reference helps an agent:

- Recognise when a performance problem is structural rather than local.
- Compare options with explicit forces, measurements and consequences.
- Prefer the least invasive option that meets the target.
- Record the decision in an ADR ([architecture-decision-records](../../django-architecture/references/architecture-decision-records.md)).

Rule: exhaust local fixes ([orm-performance-optimization.md](orm-performance-optimization.md),
[database-performance.md](database-performance.md), [caching-engineering.md](caching-engineering.md))
and have a measured gap to the SLO before proposing anything in this reference.

## 2. Core concepts

| Concept | Performance meaning |
|---|---|
| Work placement | Where computation happens: request, background task, database, precomputed store |
| Freshness budget | How stale a read may be — the main currency spent for speed |
| Write amplification | Extra writes per logical change (denormalised copies, read models, indexes) |
| Network hop | Each remote call adds latency (~0.5–2 ms in-region, illustrative) plus failure modes |
| Read/write ratio | High ratios justify precomputation; low ratios punish it |
| Reversibility | Cost of undoing the decision if the forces change |

Every option here **moves cost**, it does not remove it: from read to write, from request to
background, from compute to storage, or from latency to consistency.

## 3. Internal mechanics

- **In-process call:** nanoseconds to microseconds, shares a transaction.
- **Database round trip:** ~0.2–1 ms on a local network plus query time (illustrative).
- **Service call:** serialisation + network + remote queueing; tail latency compounds — a page
  calling 5 services each with p99 = 200 ms sees p99 well above 200 ms because the slowest
  dominates (fan-out amplification).
- **Denormalised column:** reads become single-row; every write to the source must update it,
  under concurrency, inside the same transaction or via eventual reconciliation.
- **Materialised view (PostgreSQL):** stores query results; `REFRESH MATERIALIZED VIEW`
  recomputes fully and takes an exclusive lock that blocks reads; `REFRESH ... CONCURRENTLY`
  allows reads but requires a unique index on the view and is slower to run.
- **Read model / projection:** a separate table or store updated from events; read latency is
  minimal, freshness equals projection lag.

## 4. Engineering decisions and trade-offs

### Decision 1 — Monolith vs distributed processing

- **Forces:** one component's load profile differs radically (CPU-heavy rendering, ML
  inference); independent scaling; network cost; team boundaries.
- **Options:** (a) optimise in process; (b) separate worker pool/queue in the same codebase;
  (c) separate service.
- **Measure:** share of resource consumed by the component; impact on other endpoints' p95 when
  it spikes; added hop latency and error rate in a prototype.
- **Consequences:** (b) gives isolation with no network hop on reads and is usually enough;
  (c) adds serialisation, retries, timeouts, distributed tracing needs and partial failure.
  See [architectural-styles](../../django-architecture/references/architectural-styles.md).

### Decision 2 — Synchronous vs asynchronous execution

- **Forces:** does the user need the result in this response? Work duration vs request timeout;
  reliability of downstream dependencies.
- **Options:** inline; inline with timeout + fallback; background task with status polling or
  notification; `async def` for concurrent I/O within the request.
- **Measure:** share of request time spent in the candidate work; failure rate of the
  dependency; user-visible requirement.
- **Consequences:** background tasks reduce request latency but add queue latency, idempotency
  requirements and "pending" UX states. `async def` only helps concurrent I/O
  ([async-django-performance.md](async-django-performance.md),
  [background-task-performance.md](background-task-performance.md)).

### Decision 3 — Cache vs database reads

- **Forces:** read rate, query cost after optimisation, freshness budget, key cardinality.
- **Options:** optimise the query; cache-aside; read replica; precomputed table.
- **Measure:** query time at p95 after indexing; projected hit ratio from access logs (distinct
  keys per TTL window).
- **Consequences:** caches add invalidation risk; replicas add lag; precomputed tables add write
  amplification. Pick based on whether staleness tolerance is seconds (cache/replica) or
  minutes (precompute).

### Decision 4 — Denormalisation

- **Forces:** hot read path joins/aggregates over many rows; read/write ratio > ~10:1
  (illustrative).
- **Options:** annotation at query time; cached aggregate; denormalised counter column updated
  with `F()`; database trigger; `GeneratedField` (Django ≥ 5.0) for same-row derived values.
- **Measure:** read query time before/after; write latency and lock waits on the source and
  target rows; drift between denormalised and true values (reconciliation job).
- **Consequences:** hot counter rows become lock-contention points; `bulk_update`/raw SQL can
  bypass the maintenance code; drift must be detectable and repairable.

### Decision 5 — Precomputation and summary tables

- **Forces:** dashboards/reports aggregate millions of rows; acceptable delay minutes–hours.
- **Options:** scheduled rollup task into a summary model; incremental rollup per event;
  materialised view.
- **Measure:** aggregation runtime; freshness requirement; rollup cost per run; storage.
- **Consequences:** the rollup job becomes a critical batch process with its own monitoring;
  late-arriving data needs backfill logic.

### Decision 6 — Materialised views (PostgreSQL-specific)

- **Forces:** complex read query, tolerance for periodic refresh, desire to keep logic in SQL.
- **Options:** plain view; materialised view with periodic `REFRESH ... CONCURRENTLY`; summary
  table maintained by application code.
- **Measure:** refresh duration vs interval; lock waits during refresh; query time on the view.
- **Consequences:** full refresh cost scales with source size, not change size; backend-specific
  (no equivalent in SQLite; MySQL lacks native materialised views); schema changes need raw-SQL
  migrations.

### Decision 7 — CQRS and read models

- **Forces:** read shape differs heavily from write model (search, feeds, cross-aggregate views);
  read load dominates.
- **Options:** selectors over the normalised model; denormalised read tables in the same DB
  updated in the same transaction; asynchronous projections (outbox → consumer → read store,
  possibly Elasticsearch/OpenSearch [third-party]).
- **Measure:** read latency target vs current; projection lag; write path latency increase.
- **Consequences:** asynchronous projections give eventual consistency, need replay/rebuild
  tooling and idempotent consumers ([event-driven-architecture](../../django-architecture/references/event-driven-architecture.md)).

### Decision 8 — Event-driven processing

- **Forces:** decoupling producers from slow consumers; smoothing spikes; fan-out to many
  consumers.
- **Measure:** end-to-end latency (event produced → effect visible); consumer lag; duplicate
  and out-of-order rates.
- **Consequences:** better peak absorption and producer latency; worse end-to-end latency
  predictability and debugging complexity.

### Decision 9 — Cost vs performance

- **Forces:** cloud cost per request; diminishing returns (p99 from 300 → 250 ms may cost more
  than 900 → 300 ms); engineering time.
- **Measure:** cost per 1k requests per tier; latency vs cost curve from load tests at different
  instance sizes; business value of latency (conversion, SLA penalties).
- **Consequences:** stop optimising once the SLO is met with headroom; record the cost model in
  the ADR.

## 5. Django implementation patterns

### Denormalised counter with atomic update

```python
from django.db.models import F

def add_comment(*, post_id: int, author, body: str) -> Comment:
    with transaction.atomic():
        comment = Comment.objects.create(post_id=post_id, author=author, body=body)
        Post.objects.filter(pk=post_id).update(comment_count=F("comment_count") + 1)
    return comment

def reconcile_comment_counts():               # scheduled; detects and repairs drift
    Post.objects.update(comment_count=Subquery(
        Comment.objects.filter(post=OuterRef("pk")).values("post")
        .annotate(c=Count("*")).values("c")[:1]))   # NULL if no comments — wrap in Coalesce
```

For very hot rows (e.g. a viral post), counter updates serialise on the row lock; shard the
counter (N rows summed on read) or buffer increments in Redis and flush periodically.

### Same-row derived value (Django ≥ 5.0)

```python
class OrderLine(models.Model):
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.GeneratedField(
        expression=F("quantity") * F("unit_price"),
        output_field=models.DecimalField(max_digits=14, decimal_places=2),
        db_persist=True,                       # stored: indexable, costs write + storage
    )
```

`db_persist=False` (virtual) is not supported on PostgreSQL; check backend support (VERIFY per
backend in the Django docs table).

### Summary table rollup

```python
@shared_task(acks_late=True)
def rollup_daily_sales(day_iso: str):
    day = date.fromisoformat(day_iso)
    rows = (Order.objects.filter(created_at__date=day, status="paid")
            .values("tenant_id").annotate(total=Sum("amount"), orders=Count("id")))
    with transaction.atomic():
        DailySales.objects.filter(day=day).delete()
        DailySales.objects.bulk_create(DailySales(day=day, **r) for r in rows)   # idempotent per day
```

### Materialised view as an unmanaged model

```python
class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            """
            CREATE MATERIALIZED VIEW tenant_revenue_mv AS
              SELECT tenant_id, date_trunc('month', created_at) AS month, sum(amount) AS revenue
              FROM orders_order WHERE status = 'paid' GROUP BY 1, 2;
            CREATE UNIQUE INDEX tenant_revenue_mv_uniq ON tenant_revenue_mv (tenant_id, month);
            """,
            reverse_sql="DROP MATERIALIZED VIEW tenant_revenue_mv;",
        )
    ]

class TenantRevenue(models.Model):
    tenant_id = models.BigIntegerField()
    month = models.DateTimeField()
    revenue = models.DecimalField(max_digits=18, decimal_places=2)
    class Meta:
        managed = False
        db_table = "tenant_revenue_mv"

def refresh_tenant_revenue():
    with connection.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY tenant_revenue_mv")
```

Django requires a primary key; the unmanaged model gets an implicit `id` that the view lacks —
add a synthetic unique column or use `CompositePrimaryKey` (Django ≥ 5.2) / a designated
`primary_key=True` field (VERIFY behaviour of composite PKs on unmanaged models).

## 6. Production use cases

| Situation | Decision | Measured result (illustrative) |
|---|---|---|
| Tenant dashboard aggregating 40M orders, 2.5 s | Monthly materialised view, refresh every 10 min | 2.5 s → 15 ms; 10 min staleness accepted in ADR |
| Checkout calls tax, fraud, email inline (1.4 s) | Tax inline with timeout, fraud async review, email after commit | p95 1.4 s → 380 ms |
| Post list shows comment counts (COUNT per row) | Denormalised `comment_count` + nightly reconcile | 30 queries → 1; drift < 0.01% |
| PDF rendering starves web workers | Dedicated Celery queue + worker pool (same codebase) | Web p95 stable during report peaks |
| Proposal: split search into a microservice | Rejected; Postgres full-text + read table met SLO | Avoided new service, cost unchanged |

Failure scenarios:

- **Refresh lock outage:** non-concurrent `REFRESH MATERIALIZED VIEW` blocked dashboard reads for
  90 s each run. Fix: `CONCURRENTLY` with unique index, or swap-table pattern.
- **Projection lag spiral:** a slow consumer fell hours behind after a backfill; users saw stale
  feeds. Fix: separate backfill queue, lag alerting, autoscale consumers.
- **Counter hotspot:** denormalised `like_count` caused lock waits during a viral spike.

## 7. Advantages and disadvantages

| Option | Advantages | Disadvantages |
|---|---|---|
| Stay monolithic, separate queues | No network hops, single transaction | Shared database remains a coupling point |
| Background execution | Fast responses, retries | Queue latency, idempotency, pending UX |
| Denormalisation | O(1) reads | Write amplification, drift, hot rows |
| Precomputation / MV | Fast complex reads | Staleness, refresh cost, backend lock-in |
| CQRS read models | Read shapes optimised independently | Eventual consistency, rebuild tooling |
| Event-driven | Spike absorption, decoupling | End-to-end latency, debuggability |

## 8. Security and performance considerations

- Read models and summary tables must keep tenant scoping and permission semantics of the
  source; a precomputed table without `tenant_id` filtering is a cross-tenant leak.
- Data-retention and erasure must propagate to denormalised copies, caches and projections.
- Event payloads may carry personal data into brokers and logs — minimise fields.
- Every additional store expands the attack surface and credential set.

## 9. Common mistakes and anti-patterns

- Proposing microservices or CQRS for a problem an index or prefetch would solve.
- Precomputing data with a low read/write ratio.
- Denormalising without a reconciliation path or drift metric.
- Async projections without an SLO for lag or a replay mechanism.
- Materialised views refreshed non-concurrently on a read path.
- Moving work to the background but having the request poll synchronously for it.
- No ADR: the next engineer removes the "weird" summary table, and dashboards time out.

## 10. Troubleshooting and validation

- Define the acceptance metric before building: target p95, freshness budget, cost ceiling.
- Prototype on production-like data volumes; compare to baseline under the same load
  ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).
- Monitor after rollout: freshness/lag gauges, refresh durations, drift counts, write latency
  on affected tables, cost per request.
- Revisit when forces change (read/write ratio, data size, team) — ADRs should state the
  conditions under which the decision should be reconsidered.

| Symptom | Check |
|---|---|
| Read model stale | Consumer lag, failed events, dead-letter queue |
| Write latency increased after denormalisation | Lock waits on counter rows (`pg_locks`, `log_lock_waits`) |
| MV refresh overlaps itself | Refresh duration vs schedule; add a lock around the job |
| Costs rose without latency gains | Diminishing returns — revert or right-size |

## 11. Related references and skills

- [django-architecture architectural-styles](../../django-architecture/references/architectural-styles.md),
  [event-driven-architecture](../../django-architecture/references/event-driven-architecture.md),
  [scalability-patterns](../../django-architecture/references/scalability-patterns.md),
  [architecture-decision-records](../../django-architecture/references/architecture-decision-records.md).
- [caching-engineering.md](caching-engineering.md) · [background-task-performance.md](background-task-performance.md) ·
  [async-django-performance.md](async-django-performance.md) · [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md).
- [django-orm](../../django-orm/SKILL.md) — expressions, `F()`, transactions and locking.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — materialised views, triggers, partitioning.

## 12. Official documentation and external references

- Django `GeneratedField`: https://docs.djangoproject.com/en/5.2/ref/models/fields/#generatedfield
- Django unmanaged models (`Meta.managed`): https://docs.djangoproject.com/en/5.2/ref/models/options/#managed
- Django composite primary keys (5.2): https://docs.djangoproject.com/en/5.2/topics/composite-primary-key/
- Django `F()` expressions and race conditions: https://docs.djangoproject.com/en/5.2/ref/models/expressions/#avoiding-race-conditions-using-f
- Django `RunSQL`: https://docs.djangoproject.com/en/5.2/ref/migration-operations/#runsql
- PostgreSQL materialized views: https://www.postgresql.org/docs/current/rules-materializedviews.html
- PostgreSQL `REFRESH MATERIALIZED VIEW`: https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html
- Celery task best practices: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Dean & Barroso, "The Tail at Scale" (CACM 2013): https://research.google/pubs/the-tail-at-scale/
- Martin Fowler, CQRS: https://martinfowler.com/bliki/CQRS.html
