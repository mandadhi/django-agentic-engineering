# QuerySet Internals

> Scope: how a QuerySet is built, cloned, chained, cached and evaluated; the complete list of
> evaluation triggers; slicing, boolean evaluation, `exists()`, `count()`, `len()` and
> `iterator()` mechanics; reuse and immutability conventions; how the `Query` tree becomes SQL.
> Not covered here: the overall ORM architecture and connection handling
> ([orm-fundamentals.md](./orm-fundamentals.md)); what each QuerySet method does
> ([queryset-api.md](./queryset-api.md)); fixing N+1 and measuring plans
> ([query-optimization.md](./query-optimization.md)); chunked batch processing
> ([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)).

## 1. Purpose and scope

Most ORM performance bugs are not bad SQL — they are SQL executed more often than the author
realised. This reference explains exactly when a QuerySet touches the database and when it reuses
memory, so an agent can predict query counts by reading code. Baseline Django 5.2; SQL shown is
approximately what PostgreSQL receives.

## 2. Core concepts

- **Laziness**: building a QuerySet does not query. `Order.objects.filter(...).exclude(...)`
  performs zero database work.
- **Cloning**: every chainable method returns a *new* QuerySet (`self._chain()` →
  `_clone()`), copying the `Query`. The original is unchanged.
- **Result cache**: `qs._result_cache` is `None` until the QuerySet is fully evaluated; afterwards
  it holds the list of results and the same object is served from memory.
- **Evaluation**: the moment the QuerySet's SQL runs and fills the cache.
- **Terminal methods**: methods that run their own query and return a non-QuerySet value
  (`count`, `exists`, `get`, `first`, `aggregate`, `update`, `delete`, `in_bulk`, `contains`,
  `iterator`). They usually *don't* fill the result cache.

## 3. Internal mechanics

### Object structure

```text
QuerySet
├── model            → Order
├── _db              → alias or None (router decides)
├── query            → sql.Query (WHERE tree, joins/alias_map, annotations, order_by, limits…)
├── _result_cache    → None | list
├── _prefetch_related_lookups
├── _iterable_class  → ModelIterable | ValuesIterable | ValuesListIterable | FlatValuesListIterable …
├── _fields          → set by values()/values_list()
└── _hints, _sticky_filter, _for_write, _known_related_objects …
```

`sql.Query` is internal API: attributes like `where`, `alias_map` and `annotations` change between
versions. Read them for debugging; never build features on them.

### Cloning and chaining

```python
base = Order.objects.filter(customer_id=42)
paid = base.filter(status=Order.Status.PAID)      # new object; base unchanged
assert base is not paid
assert "status" not in str(base.query)
```

`filter()` → `_filter_or_exclude()` → clone → `query.add_q(Q(...))`. Each lookup string such as
`lines__product__category__name__iexact` is resolved by `Query.names_to_path()` into joins and a
final lookup class. Multi-valued relations inside **one** `filter()` call share a join; split
across **two** `filter()` calls they create separate joins (different semantics — see
[queryset-api.md](./queryset-api.md#filter-and-exclude)).

### SQL generation

`QuerySet.__iter__` → `_fetch_all()` → `list(self._iterable_class(self))` →
`ModelIterable.__iter__` → `compiler = query.get_compiler(using=db)` →
`compiler.execute_sql(chunked_fetch=..., chunk_size=...)` → `compiler.as_sql()`.

`as_sql()` resolves expressions (`F`, aggregates, subqueries) against the query, setups joins
(promoting INNER → LEFT OUTER where a nullable path requires), builds `SELECT`, `FROM`, `WHERE`,
`GROUP BY` (when aggregates are present), `HAVING`, `ORDER BY`, `LIMIT/OFFSET`, and returns
`(sql, params)`. Values are always in `params`; the backend driver binds them.

Rows come back in chunks of `GET_ITERATOR_CHUNK_SIZE` (100) via `cursor.fetchmany()`.
`ModelIterable` builds instances with `Model.from_db(db, field_names, values)`, attaches
`select_related` objects from the same row, then `_fetch_all()` runs `prefetch_related` lookups
as additional queries.

### Evaluation triggers

| Operation | Queries? | Fills cache? | Notes |
|---|---|---|---|
| `for o in qs` | Yes (first time) | Yes | Fetches all rows into cache, then iterates |
| `list(qs)`, `len(qs)` | Yes | Yes | `len` loads every row — use `count()` if you only need the number |
| `bool(qs)`, `if qs:` | Yes | Yes | Loads every row; `exists()` if you don't need them |
| `repr(qs)` | Yes | No | Fetches up to 21 rows (`REPR_OUTPUT_SIZE` + 1); shell/debugger/logging can trigger it |
| `qs[5]` | Yes, `LIMIT 1 OFFSET 5` | No | Uses cache if already evaluated |
| `qs[:10]` | No | — | Returns new QuerySet with LIMIT; lazy |
| `qs[:10:2]` (step) | Yes | — | Returns a list |
| `pickle.dumps(qs)` | Yes | Yes | Pickles results; unpickled QS is not re-run |
| `qs.count()` | Yes `SELECT COUNT(*)` | No | Returns `len(_result_cache)` if already evaluated |
| `qs.exists()` | Yes `SELECT 1 … LIMIT 1` | No | Uses cache if already evaluated |
| `qs.contains(obj)` (Django ≥ 4.0) | Yes | No | Uses cache if evaluated |
| `qs.get(...)`, `first()`, `last()`, `earliest()`, `latest()` | Yes | No | |
| `qs.aggregate(...)`, `update()`, `delete()`, `in_bulk()` | Yes | No | `delete()` may also run collector queries |
| `qs.iterator()` | Yes | **No** | Streams; ignores existing cache |
| `obj.lines.all()` on an instance | Yes (each new `all()`) | per new QS | Unless prefetched |

### Caching rules and their limits

```python
qs = Order.objects.filter(status="paid")
list(qs)           # query 1
list(qs)           # cache hit — no query
qs.filter(total__gt=100)   # new QuerySet, new query when evaluated — cache is NOT reused

Order.objects.filter(status="paid")[0]   # query
Order.objects.filter(status="paid")[0]   # another query — different object every time
```

- The cache belongs to the **object**, not the SQL. Re-building the same QuerySet (e.g. calling a
  selector twice, or `customer.orders.all()` twice) queries twice.
- Partial evaluation doesn't populate it: `qs[0]` on an unevaluated QuerySet runs `LIMIT 1` and
  leaves the cache empty; so does `count()`/`exists()`.
- Cached data goes stale: the cache never refreshes. A long-lived QuerySet (module-level, class
  attribute) serves outdated rows forever and holds memory.
- Related managers return new QuerySets: `order.lines.all()` is a fresh QuerySet each call. With
  `prefetch_related("lines")` the *prefetch* cache (`_prefetched_objects_cache`) is reused by
  `.all()` but **discarded** by any further `.filter()`/`.order_by()` on it.
- Forward FK access caches on the instance (`order.customer` once → stored in
  `_state.fields_cache`). Reverse/M2M access does not, without prefetch.

### `iterator()` mechanics

`iterator(chunk_size=None)` bypasses `_result_cache`, so memory stays bounded to one chunk of
model instances. Behaviour by backend:

- **PostgreSQL**: uses a **server-side cursor** (named cursor) so the DB streams rows; chunk size
  defaults to 2000. Requires the iteration to stay inside one transaction when connection
  pooling in transaction mode is used — set `DISABLE_SERVER_SIDE_CURSORS = True` behind
  PgBouncer transaction pooling.
- **Oracle**: server-side cursor semantics similar.
- **MySQL / MariaDB**: no server-side cursors; "MySQL doesn't support streaming results, hence the
  Python database driver loads the entire result set into memory" — the memory benefit is only
  from not building the result cache and model instances all at once.
- **SQLite**: fetches in batches with `fetchmany()`, but there is no isolation between queries on
  one connection — rows written to the table during iteration may be skipped or seen twice
  ([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator)).
- **`prefetch_related` with `iterator()`**: supported only when `chunk_size` is given (Django ≥ 4.1);
  prefetching happens per chunk. Omitting `chunk_size` was deprecated in 4.1 and since Django 5.0
  raises `ValueError` ([5.0 release notes](https://docs.djangoproject.com/en/5.0/releases/5.0/#features-removed-in-5-0);
  exception type from source, `django/db/models/query.py`).

### `exists()` vs `count()` vs `len()` vs `bool()`

```sql
-- qs.exists()
SELECT 1 AS "a" FROM "orders_order" WHERE "orders_order"."customer_id" = %s LIMIT 1
-- qs.count()
SELECT COUNT(*) AS "__count" FROM "orders_order" WHERE "orders_order"."customer_id" = %s
-- len(qs) / bool(qs)
SELECT "orders_order"."id", ... all columns ... FROM "orders_order" WHERE ...
```

Choose by what happens next:

- Only need yes/no → `exists()`.
- Only need the number → `count()` (on PostgreSQL `COUNT(*)` over a large filtered set still
  scans; it is not free).
- Will iterate the rows anyway → evaluate once (`orders = list(qs)`), then use
  `if orders:` / `len(orders)` — no extra query.

## 4. Engineering decisions and trade-offs

- **Return QuerySets from selectors, evaluate at the edge.** Keeps them composable (pagination,
  extra filters, `select_related`) — but the caller must know it holds a lazy object. Return a
  `list` when the function's contract is "data, evaluated now" (e.g. before leaving an `atomic`
  block or crossing into async code).
- **Materialise once when reused.** If a template loops over the same QuerySet twice and also
  checks `{% if orders %}`, pass one evaluated QuerySet (or list), not three expressions.
- **`iterator()` vs cache**: `iterator()` saves memory but defeats reuse and (without
  `chunk_size`) prefetching; use it for one-pass processing of large sets, not for page-sized
  results.
- **`exists()` then iterate is two queries** — fine when the "no rows" branch is common and the
  result set is large; wasteful when you'll fetch anyway.

## 5. Django implementation patterns

### Evaluate once, branch on the evaluated result

```python
# Bad: 3 queries (exists, count, iterate)
qs = Order.objects.filter(customer=customer, status="pending")
if qs.exists():
    log.info("pending=%s", qs.count())
    for order in qs: ...

# Better: 1 query
pending = list(Order.objects.filter(customer=customer, status="pending"))
if pending:
    log.info("pending=%s", len(pending))
    for order in pending: ...
```

### Lazy composition in selectors

```python
# apps/orders/selectors.py
def order_list(*, customer_id: int, status: str | None = None):
    qs = Order.objects.filter(customer_id=customer_id).select_related("customer")
    if status:
        qs = qs.filter(status=status)
    return qs.order_by("-created_at")   # caller slices/paginates; still lazy
```

### Streaming a large export

```python
def export_order_lines(writer, since):
    qs = (OrderLine.objects
          .filter(order__created_at__gte=since)
          .values_list("order_id", "product__sku", "quantity", "unit_price")
          .order_by("pk"))
    for row in qs.iterator(chunk_size=5000):
        writer.writerow(row)
```

### Avoid hidden `repr()` evaluation

```python
logger.debug("orders: %s", qs)    # evaluates up to 21 rows when the record is formatted
logger.debug("orders query: %s", qs.query)   # no DB hit (just compiles SQL)
```

## 6. Production use cases

- **Template double evaluation**: `{% if order.lines.all %}…{% for l in order.lines.all %}` makes
  two queries per order; prefetch or `{% with lines=order.lines.all %}`.
- **Admin/list views**: `len(page.object_list)` vs `paginator.count` — the paginator runs a
  `COUNT(*)`; on huge tables that dominates. See pagination in
  [query-optimization.md](./query-optimization.md).
- **Celery task serialising a QuerySet**: pickling evaluates it in the producer and ships rows;
  pass IDs instead.
- **Cache poisoning by module-level QuerySet**: `ACTIVE_PRODUCTS = Product.objects.filter(is_active=True)`
  at import; first evaluation caches forever per process.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Zero cost to build and refine queries | Query execution point is implicit |
| Safe sharing: clones never mutate the original | Clone cost is non-zero in very hot loops (deep `Query` copy) |
| Result cache prevents repeated queries on the same object | Cache is per-object, stale, and easy to bypass accidentally |
| `iterator()` bounds memory | Defeats cache; backend-dependent streaming |

## 8. Security and performance considerations

- `repr()` of QuerySets in error reports/log messages may execute queries and leak row data into
  logs; log IDs and counts instead ([django-core logging-exceptions](../../django-core/references/logging-exceptions.md)).
- `len(qs)` and `bool(qs)` on unbounded QuerySets load every row: a memory DoS vector if the
  filter is user-controlled. Always bound user-facing QuerySets (pagination, `[:N]`).
- Building QuerySets in a tight loop (thousands of clones) shows up in profiles; hoist the
  invariant part out of the loop.
- Server-side cursors hold a transaction/snapshot open on PostgreSQL for the length of the loop —
  long exports delay vacuum ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 9. Common mistakes and anti-patterns

- `if qs:` followed by `for x in qs` on a *different* expression (re-built QuerySet) — two full
  queries.
- `qs.count()` after `list(qs)` was already evaluated on a different object.
- Calling `.all()` "to refresh" a QuerySet and expecting the same cache — it's a new object
  without cache (this *is* the way to force a re-query, intentionally).
- Filtering a prefetched relation (`order.lines.filter(...)`) inside a loop → bypasses prefetch,
  N queries. Use `Prefetch(..., queryset=...)` or filter in Python on `order.lines.all()`.
- Indexing a QuerySet repeatedly (`qs[0]`, `qs[1]`, …) — one query per index.
- Using `iterator()` for a 20-row page.
- Assuming `qs.update()` / `qs.delete()` refresh already-evaluated cached instances — they don't.

## 10. Troubleshooting and validation

- Count queries around the suspect code with `CaptureQueriesContext` or
  `self.assertNumQueries(n)` ([django-testing](../../django-testing/SKILL.md)).
- Check whether a QuerySet is evaluated: `qs._result_cache is not None` (debug only; private).
- Print compiled SQL: `print(qs.query)` or `qs.query.sql_with_params()`.
- For memory blow-ups, look for `list()`, `len()`, `bool()` on unbounded QuerySets and
  `prefetch_related` on large parents.
- In tests, assert the same query count for 1 and N objects to detect per-row queries.

```python
def test_order_page_query_count(self):
    OrderFactory.create_batch(10)
    with self.assertNumQueries(2):          # orders + prefetched lines
        render_order_page()
```

## 11. Related references and skills

- [orm-fundamentals.md](./orm-fundamentals.md) — lifecycle overview.
- [queryset-api.md](./queryset-api.md) — method semantics and return types.
- [query-optimization.md](./query-optimization.md) — N+1, `Prefetch`, `explain()`, pagination.
- [bulk-operations-and-batching.md](./bulk-operations-and-batching.md) — chunked processing.
- [orm-anti-patterns.md](./orm-anti-patterns.md).
- [django-performance](../../django-performance/SKILL.md) — profiling beyond the ORM.

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/ref/models/querysets/#when-querysets-are-evaluated
- https://docs.djangoproject.com/en/stable/topics/db/queries/#caching-and-querysets
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#iterator
- https://docs.djangoproject.com/en/stable/ref/databases/#transaction-pooling-and-server-side-cursors
- https://docs.djangoproject.com/en/stable/topics/db/optimization/
