# ORM Performance Optimization

**Scope:** Diagnosing and fixing ORM-generated database load from a performance perspective: detecting N+1 patterns, choosing loading strategies by measured cost, trimming columns and rows, eliminating round trips, and validating the result. QuerySet mechanics (laziness, cloning, API semantics) are owned by [django-orm](../../django-orm/SKILL.md); this reference decides *which* technique to apply and *how to prove* it helped.

## 1. Purpose and scope

The ORM makes database access easy to write and easy to multiply. Most slow Django endpoints are slow because of one of four ORM-level problems:

1. **Too many queries** — N+1 from lazy relation access in loops, templates or serializers.
2. **Too much data per query** — wide rows, unused columns, JOIN row duplication, unbounded result sets.
3. **Expensive single queries** — aggregations with join fan-out, correlated subqueries, missing indexes (index work → [database-performance.md](database-performance.md)).
4. **Unnecessary round trips** — loading rows to count, check existence, or update in Python.

Covered here: detection, loading strategy choice, column/row trimming, aggregation cost, bulk and iterator processing from a cost perspective, and `QuerySet.explain()` as the bridge to the database reference.

Not covered: QuerySet API semantics ([../../django-orm/references/queryset-api.md](../../django-orm/references/queryset-api.md)), evaluation internals ([../../django-orm/references/queryset-internals.md](../../django-orm/references/queryset-internals.md)), bulk API details ([../../django-orm/references/bulk-operations-and-batching.md](../../django-orm/references/bulk-operations-and-batching.md)), index DDL ([database-performance.md](database-performance.md)).

## 2. Core concepts

| Concept | Performance meaning |
|---|---|
| Round trip | Each query pays network latency + parse/plan + execution + result transfer. At ~0.3–1 ms per trivial query on a same-AZ PostgreSQL (illustrative), 300 queries cost 100–300 ms before any real work. |
| N+1 | 1 query for the parent list + N queries for a relation accessed per row. Cost grows linearly with page size and data. |
| Query count vs query cost | Count measures round trips; cost measures DB time, rows scanned, bytes transferred. Fewer queries is **not** automatically faster. |
| Row duplication | A JOIN to a to-many relation repeats parent columns once per child row; data transfer and Python model instantiation grow multiplicatively. |
| Model instantiation | Building a model instance costs roughly tens of microseconds per row (illustrative; depends on field count and Python version). 50 000 rows ≈ seconds of CPU before serialization. |
| Query budget | A fixed, asserted maximum query count per endpoint/selector that does not grow with data. |

The central diagnostic question: **is time spent in the database, in transfer, or in Python building objects?** Each needs a different fix.

## 3. Internal mechanics

Only the mechanics that drive cost; full detail in [../../django-orm/references/queryset-internals.md](../../django-orm/references/queryset-internals.md).

- **Lazy relation access** — `order.customer` on a non-loaded FK issues `SELECT ... WHERE id = %s`; `order.lines.all()` on a non-prefetched reverse relation issues one query per call. Inside a loop of N rows this is N queries.
- **`select_related`** — adds a SQL JOIN and builds related instances from the same row. One round trip; row width grows with each joined table. Only for forward FK and one-to-one (and reverse one-to-one).
- **`prefetch_related`** — one extra query per relation level using `WHERE fk IN (...)`, then joins in Python. Round trips = 1 + number of prefetched levels, independent of N. The `IN` list grows with N (very large lists can produce slow plans; batch via pagination or `iterator(chunk_size=...)`).
- **Result cache** — evaluating a QuerySet caches rows on that object. Any chained method (`.filter()`, `.order_by()`, `.exists()` on an unevaluated clone) creates a new QuerySet and a new query. `obj.rel.all()` uses the prefetch cache; `obj.rel.filter(...)` bypasses it.
- **Deferred fields** — `only()`/`defer()` omit columns; accessing an omitted column triggers `refresh_from_db(fields=[...])`: **one query per instance**.
- **`values()` / `values_list()`** — skip model instantiation; return dicts/tuples. Cheapest Python path, but lose model methods, properties and relation access.
- **Aggregation with multiple to-many joins** — `annotate(Count("a"), Count("b"))` joins both relations, producing `|a| × |b|` rows per parent before grouping: wrong results *and* exponential work ([../../django-orm/references/aggregation-and-annotation.md](../../django-orm/references/aggregation-and-annotation.md)).
- **`iterator()`** — streams rows without populating the result cache; on PostgreSQL uses a server-side cursor (unless `DISABLE_SERVER_SIDE_CURSORS`). With `prefetch_related`, prefetching happens per chunk when `chunk_size` is given (Django ≥ 4.1). In Django ≥ 5.0, calling `iterator()` on a prefetching QuerySet without `chunk_size` raises an error rather than silently skipping prefetch — `VERIFY` exact exception type against the 5.0 release notes.

## 4. Engineering decisions and trade-offs

### select_related vs prefetch_related

| Situation | Prefer | Why |
|---|---|---|
| Forward FK, small related row (`order.customer`) | `select_related` | One round trip; marginal row width. |
| Several chained FKs (`line.product.category`) | `select_related("product__category")` | Still one row per line. |
| Reverse FK / M2M | `prefetch_related` / `Prefetch` | JOIN would duplicate parents. |
| Forward FK to a **wide** table (large text/JSON columns) repeated across many rows | `prefetch_related` or `select_related` + `only()` | JOIN copies the wide row onto every parent row; a separate query fetches each distinct related row once. |
| Forward FK where most parents share few related rows (1 000 orders, 3 warehouses) | `prefetch_related` | Transfers 3 warehouse rows instead of 1 000 copies. |
| Nullable FK across large tables | Measure both | LEFT OUTER JOIN can change the plan unfavourably. |

**When a JOIN is worse:** wide joined rows, high duplication, or a JOIN that forces the planner off a good index on the base table. Validate with `explain(analyze=True)` and total response time — not query count.

### Fewer queries is not always faster

- Collapsing 3 cheap indexed queries into one query with 4 JOINs and a `DISTINCT` can be slower than the three.
- A giant `Subquery` annotation evaluated per row may cost more than one extra prefetch query.
- Rule: optimise **total DB time + transfer + Python time**, and use query count as a regression budget, not a goal.

### Column and row trimming

| Technique | Use when | Risk |
|---|---|---|
| `only()`/`defer()` | Wide tables (large `TextField`/`JSONField`) on listing pages | Deferred-field N+1 if a template/serializer touches an omitted field. |
| `values()`/`values_list()` | Exports, dropdowns, aggregates, internal pipelines | No model behaviour; serializers must accept dicts. |
| `.exists()` | Only need presence | Doing `.exists()` then iterating the same QuerySet = two queries; if you will iterate anyway, evaluate once and test truthiness. |
| `.count()` | Need count without rows | If rows are already loaded, `len(qs)` is free; `.count()` would add a query. |
| `in_bulk(ids)` | Lookup map by id | Large id lists → large `IN` clause. |

### Subquery / Exists vs JOIN + aggregate

- `Exists(...)` is usually cheaper than `annotate(Count(...)) > 0`: the planner can stop at the first match (semi-join).
- Correlated `Subquery(...[:1])` for "latest X per parent" is fine for paged lists (20–100 parents) with an index on `(parent_id, created_at)`; for 100 000 parents a window function or `DISTINCT ON` (PostgreSQL) is often cheaper — `VERIFY` with `explain()` per dataset.

### Bulk operations

- `bulk_create(objs, batch_size=...)` replaces N INSERT round trips with ⌈N/batch⌉. Batch sizes of 500–2 000 are a typical starting point (illustrative); too large batches create long transactions, big WAL bursts and lock holding; SQLite's parameter limit caps batch size automatically.
- `QuerySet.update(field=F("field") + 1)` performs a set-based write in one statement and avoids read-modify-write races.
- Trade-off: bulk paths skip `save()`, signals and (for `bulk_create` on some backends) PK population — see [../../django-orm/references/bulk-operations-and-batching.md](../../django-orm/references/bulk-operations-and-batching.md).

## 5. Django implementation patterns

### Detect: capture queries for a code path

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

with CaptureQueriesContext(connection) as ctx:
    rows = list(order_list(user=user))           # force evaluation
total_ms = sum(float(q["time"]) for q in ctx.captured_queries) * 1000
print(len(ctx.captured_queries), f"{total_ms:.1f} ms")
for q in sorted(ctx.captured_queries, key=lambda q: -float(q["time"]))[:5]:
    print(q["time"], q["sql"][:200])
```

`CaptureQueriesContext` works without `DEBUG=True`. Group SQL by normalised text to spot N+1: the same statement repeated N times with different parameters.

### Enforce: query budgets in tests

```python
# Django TestCase
def test_order_list_query_budget(self):
    OrderFactory.create_batch(30, lines=3)
    with self.assertNumQueries(4):     # orders, customers(join), lines, products
        self.client.get("/orders/")
```

```python
# [pytest-django]
def test_order_list_budget(client, django_assert_max_num_queries):
    OrderFactory.create_batch(30)
    with django_assert_max_num_queries(5):
        client.get("/orders/")
```

Create **more than one** parent row; a budget with one row cannot detect N+1. Test design → [../../django-testing/SKILL.md](../../django-testing/SKILL.md).

Optional detectors: [third-party: nplusone] flags lazy loads at runtime; [third-party: django-zen-queries] forbids queries inside template rendering/serialization blocks.

### Fix: loading strategy in the selector, not the template

```python
from django.db.models import Count, Exists, OuterRef, Prefetch, Q

def order_list(*, user):
    open_lines = (OrderLine.objects
                  .select_related("product")
                  .only("id", "order_id", "quantity", "product__id", "product__name"))
    return (Order.objects.for_user(user)
            .select_related("customer")
            .only("id", "reference", "status", "created_at", "customer__id", "customer__name")
            .prefetch_related(Prefetch("lines", queryset=open_lines, to_attr="line_list"))
            .annotate(has_refund=Exists(Refund.objects.filter(order=OuterRef("pk"))))
            .order_by("-created_at", "-id"))
```

- `to_attr` stores a plain list → consumers use `order.line_list`, which cannot accidentally re-query.
- `only()` on the prefetch must include the FK used to attach rows (`order_id`), or each attach triggers a deferred load.

### Fix: per-row filtering and counting

```python
# Anti-pattern: 1 + N queries, bypasses any prefetch
for p in projects:
    p.open_count = p.tasks.filter(done=False).count()

# One query, aggregation in SQL
projects = projects.annotate(open_count=Count("tasks", filter=Q(tasks__done=False)))
```

When annotating counts over **two** to-many relations, use subqueries to avoid fan-out:

```python
from django.db.models import IntegerField, Subquery
from django.db.models.functions import Coalesce

def count_of(model, fk):
    return Coalesce(Subquery(
        model.objects.filter(**{fk: OuterRef("pk")})
        .values(fk).annotate(c=Count("*")).values("c"),
        output_field=IntegerField()), 0)

Project.objects.annotate(task_count=count_of(Task, "project"),
                         member_count=count_of(Membership, "project"))
```

### Round-trip elimination

```python
# Existence
if Order.objects.filter(customer=c, status="pending").exists(): ...

# Atomic increment, no read
Product.objects.filter(pk=pid).update(stock=F("stock") - qty)

# Lookup map instead of get() in a loop
products = Product.objects.in_bulk(product_ids)       # {id: Product}

# Already-loaded instances: prefetch after the fact
from django.db.models import prefetch_related_objects
prefetch_related_objects(orders, "lines__product")
```

### Large result sets

```python
qs = (Event.objects.filter(created_at__date=day)
      .select_related("account")
      .prefetch_related("tags")
      .order_by("pk"))
for event in qs.iterator(chunk_size=2000):     # prefetch per chunk (Django ≥ 4.1)
    process(event)
```

Or keyset batching when each batch should be a short independent transaction:

```python
last_pk = 0
while batch := list(Event.objects.filter(pk__gt=last_pk).order_by("pk")[:2000]):
    handle(batch)
    last_pk = batch[-1].pk
```

Memory side of this → [memory-and-resource-management.md](memory-and-resource-management.md).

### Bridge to the database: explain()

```python
qs = Order.objects.filter(customer_id=42, status="open").order_by("-created_at")[:20]
print(qs.explain())                                     # estimated plan
print(qs.explain(analyze=True, buffers=True))           # PostgreSQL: executes the query
```

`analyze=True` **executes** the statement; never run it on a writing QuerySet in production. Plan reading → [database-performance.md](database-performance.md).

## 6. Production use cases

- **Admin changelist at 8 s:** `list_display` included `customer_name` (a method) and `line_count`. Fix: `list_select_related = ["customer"]` and `get_queryset().annotate(line_count=Count("lines"))`. Queries: 203 → 3; p95 8 s → 350 ms (illustrative).
- **Serializer N+1 introduced by a new field:** a `SerializerMethodField` calling `obj.tags.filter(active=True)` added 50 queries per page. Fix: `Prefetch("tags", queryset=Tag.objects.filter(active=True), to_attr="active_tags")`; a query-budget test now guards it. API-specific details → [api-performance.md](api-performance.md).
- **Prefetch slowed a page:** `select_related("warehouse")` duplicated a 6 KB JSON `config` column onto 5 000 rows (30 MB transfer). Switching to `prefetch_related("warehouse")` (3 rows) cut DB transfer time from 900 ms to 40 ms.
- **Nightly job OOM:** `for row in Model.objects.all()` loaded 4 M instances into the result cache. Fix: `.iterator(chunk_size=5000)` and `values_list` for columns needed; RSS dropped from 9 GB to ~300 MB (illustrative).
- **Dashboard count fan-out:** `annotate(Count("orders"), Count("tickets"))` produced inflated counts and a 40 M-row intermediate; subquery counts fixed correctness and cut query time 12 s → 180 ms.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| `select_related` | One round trip, simple | Wider rows; duplication of shared related rows; can change plans |
| `prefetch_related` / `Prefetch` | Bounded queries; filtered/ordered relations; `to_attr` safety | Extra round trips; Python-side joining; large `IN` lists |
| `only`/`defer` | Less transfer for wide tables | Silent per-row queries if an omitted field is accessed; brittle as code evolves |
| `values`/`values_list` | Skip instantiation (often 2–5× less CPU, illustrative) | No model behaviour; different consumer contract |
| Annotations/subqueries | Work moves to SQL | Heavier single query; fan-out mistakes; harder to read |
| Bulk writes / `update()` | Orders of magnitude fewer round trips | Bypass `save()`, signals, validation |
| `iterator()` | Flat memory | No result cache; server-side cursor holds a transaction/connection; PgBouncer caveats |

## 8. Security and performance considerations

- **Scoping is part of the query.** Optimising a selector must keep tenant/ownership filters on *every* prefetched queryset; a `Prefetch(queryset=Model.objects.all())` can expose another tenant's child rows. → [../../django-security-engineering/SKILL.md](../../django-security-engineering/SKILL.md).
- **Unbounded inputs drive query cost.** `?ids=` lists, page sizes and filter combinations must be bounded; attackers will pick the most expensive combination.
- **`explain(analyze=True)`** runs the statement; restrict it to read-only queries and non-production or replica environments.
- **Logging SQL** (Debug Toolbar, `django.db.backends` logger at DEBUG) exposes parameters, which may include personal data; keep it out of production logs.
- `values()` with user-controlled field names is an information-disclosure risk; allow-list fields.

## 9. Common mistakes and anti-patterns

- Adding `select_related`/`prefetch_related` everywhere "just in case" — extra JOINs and queries for relations nobody reads.
- `prefetch_related("x")` then `obj.x.filter(...)`/`.order_by(...)` in the loop — discards the prefetch (use `Prefetch(queryset=..., to_attr=...)`).
- Query-budget tests with one fixture row.
- `if qs:` followed by `qs.count()` and iteration on different clones — three queries.
- `len(Model.objects.all())` to count; Python `sum()` over loaded rows instead of `aggregate()`.
- `only()` on a listing whose serializer later adds a field → N deferred loads that no test catches.
- Two to-many `Count()`s in one `annotate()` — wrong numbers and huge intermediate results.
- `bulk_create` of 1 M rows in one call and one transaction — WAL spike, replication lag, lock holding.
- Using `iterator()` without `chunk_size` on prefetching QuerySets (silently no prefetch on old versions; an error on Django ≥ 5.0 — `VERIFY`).
- Optimising query count on a page whose time is actually spent in template rendering or serialization — profile first ([profiling-and-observability.md](profiling-and-observability.md)).

## 10. Troubleshooting and validation

**Diagnosis sequence**

1. Reproduce with realistic volume (page sizes and relation fan-out matching production).
2. Capture: query count, total DB time, top 5 queries by time, duplicated statements.
3. Classify:
   - Same SQL repeated N times → N+1 → loading strategy.
   - One query dominates → `explain(analyze=True, buffers=True)` → [database-performance.md](database-performance.md).
   - DB time small but request slow → Python time (instantiation, serialization, templates) → profile.
4. Apply one change; re-measure the same scenario three or more times; compare medians and p95.
5. Lock in: query-budget test + (for hot paths) a benchmark in [performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md).

**Validation checklist**

- [ ] Query count constant as rows grow (test with 1, 10 and 100 parents).
- [ ] Total DB time and response time improved, not only query count.
- [ ] Every prefetched queryset preserves the original scoping filters.
- [ ] No deferred-field loads (search captured SQL for single-column `SELECT ... WHERE id =`).
- [ ] Aggregations over multiple relations checked for fan-out (compare counts against a simple query).
- [ ] Batch sizes justified by transaction duration and memory measurements.

**Production signals:** per-endpoint DB span count and DB time in APM traces; `pg_stat_statements` `calls` exploding for a single statement shape (N+1 signature: very high calls, low mean time).

## 11. Related references and skills

- [database-performance.md](database-performance.md) — indexes, plans, connections, locks.
- [api-performance.md](api-performance.md) — serializer-driven N+1 and pagination cost.
- [profiling-and-observability.md](profiling-and-observability.md) — Debug Toolbar, Silk, APM query timing.
- [memory-and-resource-management.md](memory-and-resource-management.md) — large QuerySet memory.
- [caching-engineering.md](caching-engineering.md) — when to cache instead of optimise.
- [django-orm](../../django-orm/SKILL.md): [query-optimization.md](../../django-orm/references/query-optimization.md), [queryset-internals.md](../../django-orm/references/queryset-internals.md), [aggregation-and-annotation.md](../../django-orm/references/aggregation-and-annotation.md), [advanced-query-expressions.md](../../django-orm/references/advanced-query-expressions.md), [bulk-operations-and-batching.md](../../django-orm/references/bulk-operations-and-batching.md).
- [django-testing](../../django-testing/SKILL.md) — query-count assertions in CI.

## 12. Official documentation and external references

- Database access optimization: https://docs.djangoproject.com/en/5.2/topics/db/optimization/
- QuerySet API (`select_related`, `prefetch_related`, `only`, `iterator`, `explain`): https://docs.djangoproject.com/en/5.2/ref/models/querysets/
- `Prefetch` / `prefetch_related_objects`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#prefetch-objects
- Aggregation: https://docs.djangoproject.com/en/5.2/topics/db/aggregation/
- Query expressions (`Subquery`, `Exists`, `F`): https://docs.djangoproject.com/en/5.2/ref/models/expressions/
- Testing tools (`assertNumQueries`, `CaptureQueriesContext`): https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- Django 4.1 / 5.0 release notes (iterator + prefetch): https://docs.djangoproject.com/en/5.2/releases/
- pytest-django fixtures: https://pytest-django.readthedocs.io/en/latest/helpers.html
- PostgreSQL EXPLAIN: https://www.postgresql.org/docs/current/sql-explain.html
- [third-party] nplusone: https://github.com/jmcarp/nplusone · django-zen-queries: https://github.com/dabapps/django-zen-queries
