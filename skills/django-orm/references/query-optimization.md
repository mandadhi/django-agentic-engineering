# Query Optimization

> Scope: making ORM data access fast *on evidence* — finding and fixing N+1 queries,
> `select_related`/`prefetch_related`/`Prefetch` strategy, trimming columns, avoiding redundant
> queries, pagination performance, index-aware filtering, `QuerySet.explain()`, query logging and
> profiling, connection pooling, caching boundaries, bulk query patterns and measuring the result.
> Not covered here: evaluation triggers and result-cache mechanics
> ([queryset-internals.md](./queryset-internals.md)), per-method semantics
> ([queryset-api.md](./queryset-api.md)), index and constraint design
> ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md)), bulk writes
> ([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)), application caching
> ([django-core: caching](../../django-core/references/caching.md)), logging configuration
> ([django-core: logging-exceptions](../../django-core/references/logging-exceptions.md)), load testing
> and profiling methodology ([django-performance](../../django-performance/SKILL.md)), serializer
> query tuning ([django-rest-framework](../../django-rest-framework/SKILL.md)), PostgreSQL planner
> internals ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

Optimization is a loop: **measure → find the dominant cost → change one thing → measure again**.
The ORM makes it easy to issue too many queries (N+1) and also easy to issue too few, too large
queries (giant joins, huge prefetches). This reference gives the agent a workflow and the
concrete Django tools to diagnose and fix both, without premature optimization. SQL and plans
shown are illustrative PostgreSQL output, not executed results.

## 2. Core concepts

- **Query count** — round trips; each costs network latency + parse/plan + execution.
- **Query cost** — rows scanned, joins, sorts, bytes transferred, Python model instantiation.
- **N+1** — 1 query for a list + 1 per item for a related object.
- **`select_related`** — SQL `JOIN`, single query; forward FK and one-to-one (both directions).
- **`prefetch_related`** — one extra query per relation (`WHERE fk IN (...)`), joined in Python;
  for reverse FK and M2M (and GenericRelation).
- **`Prefetch` objects** — control the queryset used for a prefetch and store results under
  `to_attr`.
- **Column trimming** — `only()`, `defer()`, `values()`, `values_list()`.
- **Query plan** — the database's chosen strategy; `QuerySet.explain()` exposes it.

**Why fewer queries is not automatically faster**: collapsing N queries into one can produce a
join that multiplies rows (fan-out), transfers duplicated parent columns for every child row,
defeats indexes, forces a large sort or hash, or materialises thousands of model instances
nobody uses. A 3-query page doing 2 ms each beats a 1-query page doing a 400 ms join. Measure
time and rows, not just count.

## 3. Internal mechanics

- `select_related("customer")` adds a `JOIN` (INNER for non-null FK, LEFT OUTER for nullable)
  and populates the related instance cache from the same row.
- `prefetch_related("lines")` evaluates the main query, collects pks, runs
  `SELECT ... FROM orders_orderline WHERE order_id IN (...)`, and assigns lists to each
  instance's prefetch cache. Calling `.filter()` on the related manager afterwards **ignores
  the cache** and queries again — use `Prefetch(queryset=...)` instead.
- Very large `IN` lists hit backend parameter limits (SQLite: `SQLITE_MAX_VARIABLE_NUMBER`
  defaults to 999 before 3.32.0 and 32766 after, and builds may lower it —
  [SQLite limits](https://www.sqlite.org/limits.html#max_variable_number); Oracle: 1000 per `IN`
  list — Django splits lists only on Oracle, source: `backends/oracle/operations.py`).
- `only()`/`defer()` return model instances with deferred fields; touching a deferred field
  triggers **one query per instance** (`refresh_from_db(fields=[...])`).
- `iterator()` streams rows (server-side cursors on PostgreSQL unless
  `DISABLE_SERVER_SIDE_CURSORS`); with `prefetch_related` it requires `chunk_size`
  (Django ≥ 4.1 supports prefetching per chunk). Details in [queryset-internals.md](./queryset-internals.md).
- `connection.queries` records SQL and time only when `DEBUG=True`; the
  `django.db.backends` logger emits each query at DEBUG level.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid |
|---|---|---|
| Forward FK / O2O needed for every row | `select_related` | Prefetch (extra round trip for no benefit) |
| Reverse FK / M2M | `prefetch_related` / `Prefetch` | `select_related` (not possible) or annotating rows by join |
| Need only aggregates of children | `annotate(Count/Sum)` or `Subquery` | Prefetching all children to count in Python |
| Wide rows, few columns used | `only()` / `values()` | `defer()` everywhere — brittle and causes per-row queries |
| Read-only report data | `values()`/`values_list()` | Full model instantiation |
| Deep pagination on large tables | Keyset (seek) pagination | `OFFSET` of 100k rows |
| Same expensive aggregate on every request | App cache ([caching](../../django-core/references/caching.md)) or a materialised summary | Recomputing per request |
| Many `select_related` joins to wide tables | Split into `prefetch_related` | 6-way join with duplicated columns |

## 5. Django implementation patterns

### N+1: before and after

```python
# BEFORE — 1 + N (customer) + N (lines) + N*M (product) queries
orders = Order.objects.filter(status=Order.Status.PAID)[:50]
for order in orders:
    print(order.customer.email)
    for line in order.lines.all():
        print(line.product.sku, line.quantity)
```

```python
# AFTER — 3 queries total, independent of N
from django.db.models import Prefetch

orders = (
    Order.objects.filter(status=Order.Status.PAID)
    .select_related("customer")
    .prefetch_related(
        Prefetch("lines", queryset=OrderLine.objects.select_related("product").only(
            "id", "order_id", "quantity", "unit_price", "product__sku", "product__name",
        ))
    )
    .order_by("-created_at")[:50]
)
```

```sql
SELECT o.*, c.* FROM orders_order o INNER JOIN customers_customer c ON c.id = o.customer_id
 WHERE o.status = 'paid' ORDER BY o.created_at DESC LIMIT 50;
SELECT l.id, l.order_id, l.quantity, l.unit_price, p.id, p.sku, p.name
  FROM orders_orderline l INNER JOIN catalog_product p ON p.id = l.product_id
 WHERE l.order_id IN (...50 ids...);
```

(Fields used in `only()` on the inner queryset must include the FK back to the parent —
`order_id`. Django does not add it for you: the reverse-FK prefetch reads the FK value of each
fetched row to match it to its parent, and a deferred FK is then loaded lazily, one query per
row (source: `get_prefetch_querysets` in `django/db/models/fields/related_descriptors.py`, 5.2;
deferred loading — [docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#defer).)

### Filtered prefetch with `to_attr`

```python
customers = Customer.objects.filter(is_active=True).prefetch_related(
    Prefetch(
        "orders",
        queryset=Order.objects.filter(status=Order.Status.PENDING).order_by("-created_at"),
        to_attr="pending_orders",           # list attribute; customer.orders.all() stays unfiltered
    )
)
for c in customers:
    c.pending_orders                         # no query
```

### Aggregate instead of prefetch

```python
# BEFORE: loads every order row to count them
for c in Customer.objects.prefetch_related("orders"):
    n = len(c.orders.all())

# AFTER: one grouped query
from django.db.models import Count, Q
customers = Customer.objects.annotate(
    paid_orders=Count("orders", filter=Q(orders__status=Order.Status.PAID))
)
```

Join fan-out rules when annotating several relations: [aggregation-and-annotation.md](./aggregation-and-annotation.md).

### Avoiding redundant queries

```python
# exists() vs count() vs evaluating
if order.lines.exists(): ...              # SELECT 1 ... LIMIT 1 — when you won't use the rows
lines = list(order.lines.all())           # when you will use them anyway: evaluate once, test len
if lines: ...

# Use the FK id without loading the related object
order.customer_id                          # no query
order.customer.id                          # query (unless select_related)

# Fetch a dict by pk in one query
products = Product.objects.in_bulk(product_ids)
```

### Pagination

```python
# OFFSET pagination: cost grows with page number; COUNT(*) on every page
Paginator(Order.objects.order_by("-created_at", "-id"), 50).page(2000)

# Keyset pagination: constant cost using the index on (created_at, id)
def orders_after(*, created_at, last_id, limit=50):
    return (Order.objects
            .filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=last_id))
            .order_by("-created_at", "-id")[:limit])
```

Keyset needs a deterministic, unique ordering and a supporting composite index; it can't jump to
arbitrary pages. For admin/list views with huge tables, `Paginator`'s `COUNT(*)` can dominate —
consider an estimated count (backend-specific) or removing total-page UI. API pagination classes →
[django-rest-framework](../../django-rest-framework/SKILL.md).

### Index-aware filtering

Write filters the index can use:

```python
# Index on created_at can't be used by a function on the column (without a functional index)
Order.objects.filter(created_at__date=today)                    # cast/tz-convert applied to the column (SQL is backend-specific)
Order.objects.filter(created_at__gte=start, created_at__lt=end)  # sargable range — uses the index

Customer.objects.filter(email__iexact=email)  # UPPER(email) = UPPER(...) — needs functional index on Upper("email")
Product.objects.filter(name__icontains="bolt")  # leading wildcard: no B-tree; consider trigram (PostgreSQL) or search
```

`__date` wraps the column in a date cast (after a time-zone conversion when `USE_TZ = True` —
[docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#date)); the exact SQL differs
per backend. On PostgreSQL a plain B-tree on `created_at` only serves predicates on the bare
column; an expression needs a matching expression index
([PostgreSQL](https://www.postgresql.org/docs/current/indexes-expressional.html)). Confirm the
plan with `explain()` on your backend.

Index selection: [database-constraints-and-indexes.md](./database-constraints-and-indexes.md).

### Reading a plan with `explain()`

```python
print(Order.objects.filter(customer_id=42, status="paid").order_by("-created_at")[:20]
      .explain(analyze=True, buffers=True))   # PostgreSQL options; ANALYZE executes the query
```

```text
Limit  (cost=0.43..12.10 rows=20) (actual time=0.05..0.11 rows=20 loops=1)
  ->  Index Scan using order_customer_created_idx on orders_order
        Filter: (status = 'paid')
        Rows Removed by Filter: 3
```

Look for: `Seq Scan` on large tables with selective filters, large `Rows Removed by Filter`,
estimated vs actual row mismatches, `Sort` spilling to disk, nested loops with huge outer
counts. Options are backend-specific: PostgreSQL accepts `analyze`, `buffers`, `verbose`, etc.
and `format` `TEXT`/`JSON`/`YAML`/`XML`; MySQL and MariaDB accept `format` `TEXT`/`JSON` (MySQL
8.0.16+ also `TREE`); `analyze` is supported by PostgreSQL, MariaDB and MySQL 8.0.18+; Oracle does
not support `explain()` at all
([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#explain)). Never run
`analyze=True` on a write query in production — it executes it (even a `SELECT` can change data
via triggers or functions).

### Query logging and counting

```python
# settings (dev only) — logger config lives with django-core logging guidance
LOGGING["loggers"]["django.db.backends"] = {"level": "DEBUG", "handlers": ["console"]}
```

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

with CaptureQueriesContext(connection) as ctx:
    render_order_list()
print(len(ctx.captured_queries), sum(float(q["time"]) for q in ctx.captured_queries))
```

In tests, lock in the fix with `assertNumQueries` ([django-testing](../../django-testing/SKILL.md)).
In production use APM tracing or PostgreSQL `pg_stat_statements` for top queries by total time;
`auto_explain` for slow plans. Never enable `DEBUG` to get `connection.queries` in production.

### Connection management

- `CONN_MAX_AGE` (seconds; `None` = unlimited) keeps connections per worker thread; set
  `CONN_HEALTH_CHECKS = True` (Django ≥ 4.1) to discard dead persistent connections.
- Native pooling for PostgreSQL with psycopg 3: `DATABASES["default"]["OPTIONS"]["pool"] = True`
  or a dict of `psycopg_pool.ConnectionPool` kwargs (Django ≥ 5.1; needs `psycopg[pool]`,
  ignored with psycopg2 — [docs](https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-pool)).
  Requires `CONN_MAX_AGE = 0`; any other value raises `ImproperlyConfigured` (source:
  `django/db/backends/postgresql/base.py`, 5.2).
- With an external pooler (PgBouncer in transaction mode) disable server-side cursors
  (`DISABLE_SERVER_SIDE_CURSORS = True`) and avoid session-level state.
- Total connections = processes × threads (× pool size); size against the database's
  `max_connections`. Production settings → [django-core: production-configuration](../../django-core/references/production-configuration.md).

### Caching boundaries

Cache **results of business reads** at the application level (per-view, fragment, low-level
`cache.get_or_set`) — see [django-core: caching](../../django-core/references/caching.md). The
QuerySet result cache is per object and per request only; it is not a cache layer. Don't cache
QuerySets themselves (they pickle with their results and go stale); cache computed values or
lists of ids, and invalidate on commit.

## 6. Production use cases

- **Order history page**: the query log shows 301 queries; `select_related` + `Prefetch` brings
  it to 3, but the page is still slow. `explain` shows a `Sort` over all of the customer's orders
  — the FK index on `customer_id` (Django indexes FKs by default) doesn't serve
  `ORDER BY created_at DESC`; a composite `(customer, -created_at)` index removes the sort.
- **Dashboard counts**: replaced Python loops with conditional `Count(filter=Q(...))` in one
  grouped query.
- **Export of 2M rows**: `values_list(...).iterator(chunk_size=5000)` streaming instead of
  `list(qs)` — memory flat ([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)).
- **Infinite-scroll feed**: OFFSET → keyset with `(created_at, id)` index.

## 7. Advantages and disadvantages

| Technique | Advantage | Disadvantage |
|---|---|---|
| `select_related` | One round trip | Row width and fan-out grow with each join |
| `prefetch_related` | No row duplication; flexible querysets | Extra round trips; large `IN` lists; Python-side join memory |
| `only`/`defer` | Less I/O for wide rows | Hidden per-row queries if a deferred field is touched |
| `values()` | No model overhead | Lose model methods; dicts leak into templates/APIs |
| Keyset pagination | Constant cost | No page jumps; needs unique ordering |
| Persistent connections/pool | Removes connect latency | Connection count planning; stale-state risk |

## 8. Security and performance considerations

- Unbounded querysets in user-facing endpoints are a DoS vector — always slice or paginate and
  cap page size.
- `explain(analyze=True)` executes the statement; only on reads, and never with user-supplied
  SQL.
- Query logs contain parameter values (emails, tokens) — scrub before shipping
  ([django-core: logging-exceptions](../../django-core/references/logging-exceptions.md)).
- Optimizations must not drop tenant/permission filters — e.g. a `Prefetch` queryset must apply
  the same scoping as the parent selector ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 9. Common mistakes and anti-patterns

- Optimising without a baseline measurement or a reproducible slow case.
- Adding `select_related()` with no arguments (follows all non-null FKs — unpredictable joins).
- `prefetch_related("lines")` then `order.lines.filter(...)` in the loop — the prefetch is wasted.
- `only()` then accessing a deferred field in templates → N+1 in disguise.
- `len(qs)` to count when rows aren't needed; `qs.count()` followed by iterating the same qs
  when rows are needed (two queries).
- Caching a QuerySet object, or caching without an invalidation plan.
- Using `OFFSET` pagination for deep pages on large tables.
- Judging success by query count alone.

## 10. Troubleshooting and validation

1. Reproduce with production-like data volumes (plans change with table size and statistics).
2. Capture queries (`CaptureQueriesContext`, debug toolbar in dev, APM in prod).
3. Group by SQL shape: repeated identical shapes = N+1; one slow shape = plan/index problem.
4. `explain(analyze=True)` the slow shape on a production-like copy.
5. Apply one change; re-measure time, rows and count; keep it only if it improves the metric that
   matters.
6. Add an `assertNumQueries` regression test for N+1 fixes.

| Symptom | Likely cause |
|---|---|
| Query count scales with rows | N+1 — missing `select_related`/`prefetch_related` |
| One query, slow, many rows returned then discarded | Missing filter/limit; Python-side filtering |
| Slow single query with `Sort` | Missing index matching `ORDER BY` |
| Memory spikes on export | Result cache on huge queryset — use `iterator()` |
| Intermittent connection errors with pool | Stale connections — `CONN_HEALTH_CHECKS`, pool `check` |

## 11. Related references and skills

- [queryset-internals.md](./queryset-internals.md) · [queryset-api.md](./queryset-api.md)
- [aggregation-and-annotation.md](./aggregation-and-annotation.md) — fan-out in annotations.
- [database-constraints-and-indexes.md](./database-constraints-and-indexes.md) — which index.
- [bulk-operations-and-batching.md](./bulk-operations-and-batching.md) — large reads/writes.
- [orm-anti-patterns.md](./orm-anti-patterns.md) — detection checklist.
- [django-core: caching](../../django-core/references/caching.md) · [django-core: logging-exceptions](../../django-core/references/logging-exceptions.md)
- [django-performance](../../django-performance/SKILL.md) · [postgresql-engineering](../../postgresql-engineering/SKILL.md) · [django-rest-framework](../../django-rest-framework/SKILL.md)

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/topics/db/optimization/
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#select-related
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#prefetch-related
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#explain
- https://docs.djangoproject.com/en/stable/ref/databases/#persistent-connections
- https://docs.djangoproject.com/en/stable/ref/databases/#postgresql-pool
- https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TransactionTestCase.assertNumQueries
- https://www.postgresql.org/docs/current/using-explain.html
- https://www.postgresql.org/docs/current/pgstatstatements.html
