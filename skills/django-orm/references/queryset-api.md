# QuerySet API

> Scope: engineering semantics of the core QuerySet methods — `all`, `filter`, `exclude`, `get`,
> `create`, `get_or_create`, `update_or_create`, `update`, `delete`, `values`, `values_list`,
> `distinct`, `order_by`, `reverse`, `defer`, `only`, `select_related`, `prefetch_related`,
> `select_for_update`, `union`, `intersection`, `difference` — when to use each, SQL produced,
> return types, execution behaviour and pitfalls.
> Not covered here: when SQL runs and caching ([queryset-internals.md](./queryset-internals.md));
> N+1 strategy and `Prefetch` objects ([query-optimization.md](./query-optimization.md));
> lock semantics and isolation ([transactions-and-concurrency.md](./transactions-and-concurrency.md));
> `F`/`Q`/`Subquery` ([advanced-query-expressions.md](./advanced-query-expressions.md));
> `annotate`/`aggregate` ([aggregation-and-annotation.md](./aggregation-and-annotation.md));
> `bulk_create`/`bulk_update` ([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)).

## 1. Purpose and scope

A reference for choosing the right QuerySet method and predicting its SQL and side effects. It is
not a method catalogue — the official API reference is. Baseline Django 5.2; SQL is approximate
(PostgreSQL). Examples use the canonical orders/catalog/inventory domain.

## 2. Core concepts

Methods fall into three groups:

| Group | Methods | Returns | Executes SQL? |
|---|---|---|---|
| **Refining** (chainable, lazy) | `all`, `filter`, `exclude`, `order_by`, `reverse`, `distinct`, `values`, `values_list`, `defer`, `only`, `select_related`, `prefetch_related`, `select_for_update`, `union`, `intersection`, `difference`, `annotate`, `alias`, `using` | `QuerySet` | No |
| **Retrieving** (terminal) | `get`, `first`, `last`, `earliest`, `latest`, `exists`, `count`, `in_bulk`, `iterator`, `aggregate`, `contains` | object / value / dict / generator | Yes |
| **Writing** (terminal) | `create`, `get_or_create`, `update_or_create`, `update`, `delete`, `bulk_create`, `bulk_update` | object / tuple / int | Yes |

Every method has an async twin for terminal operations (`aget`, `acreate`, `aget_or_create`,
`aupdate`, `adelete`, `acount`, `aexists`, …) — see
[django-core asynchronous-django](../../django-core/references/asynchronous-django.md).

## 3. Internal mechanics

- Refining methods clone the QuerySet and modify `query`; they may also switch `_iterable_class`
  (`values()` → `ValuesIterable`).
- Some refinements are restricted after slicing: `filter`, `exclude`, `order_by` on a sliced
  QuerySet raise `TypeError("Cannot filter a query once a slice has been taken.")`.
- Write methods on a QuerySet (`update`, `delete`) issue set-based SQL and do **not** call model
  `save()`/`delete()`. `delete()` does run Django's `Collector`, which may load objects to send
  `pre_delete`/`post_delete` signals and to emulate `on_delete` cascades.
- `create`/`get_or_create`/`update_or_create` go through `Model.save()` (signals fire, `save()`
  overrides run), but not `full_clean()`.

## 4. Engineering decisions and trade-offs

| Need | Prefer | Avoid |
|---|---|---|
| Change many rows by rule | `qs.update(...)` with `F()` | Loop + `save()` |
| Change one loaded instance's fields | `obj.save(update_fields=[...])` | Bare `save()` (writes all columns) |
| Idempotent create on natural key | `get_or_create` backed by a unique constraint | `exists()` then `create()` |
| Read-only data for export/API | `values`/`values_list` | Full model instances |
| Wide rows with a large unused column | `defer("description")` / `only(...)` | `only()` then accessing deferred fields |
| FK / O2O traversal for every row | `select_related` | Attribute access in loop |
| Reverse FK / M2M for every row | `prefetch_related` | `select_related` (not allowed) / loop queries |
| Combine disjoint result sets | `union()` | Two lists concatenated and re-sorted in Python (unless small) |

## 5. Django implementation patterns

### all()

Returns a copy of the QuerySet. Useful to get a fresh, un-cached clone or a QuerySet from a
manager (`Order.objects.all()`); redundant elsewhere — `Order.objects.filter(...)` doesn't need
`.all()` in front. On a related manager (`order.lines.all()`) it's the way to get a QuerySet and to
read the prefetch cache.

### filter() and exclude()

```python
Order.objects.filter(customer_id=42, status=Order.Status.PAID)
# WHERE customer_id = %s AND status = %s

Order.objects.exclude(status=Order.Status.CANCELLED)
# WHERE NOT (status = %s)
```

Multi-valued relationships change semantics depending on whether conditions share a call:

```python
# Orders having at least one line that is BOTH product X AND quantity >= 5 (one join)
Order.objects.filter(lines__product__sku="X-1", lines__quantity__gte=5)

# Orders having a line for product X and (possibly another) line with quantity >= 5 (two joins)
Order.objects.filter(lines__product__sku="X-1").filter(lines__quantity__gte=5)
```

`exclude()` with multi-valued relations compiles to a `NOT IN (subquery)` / `NOT EXISTS` and does
**not** mirror the single-`filter` semantics; when it matters, use `~Exists(...)` explicitly
([advanced-query-expressions.md](./advanced-query-expressions.md)).

On a nullable column Django compiles `exclude(placed_at=x)` to roughly
`NOT (placed_at = %s AND placed_at IS NOT NULL)` so rows with NULL are *kept* (matching Python
intuition rather than SQL three-valued logic) — check `str(qs.query)` when NULLs matter.

Joins across multi-valued relations can duplicate parent rows: `Order.objects.filter(lines__quantity__gte=5)`
returns an order once per matching line. Add `.distinct()` or use `Exists`.

### get()

Returns exactly one object or raises `Model.DoesNotExist` / `MultipleObjectsReturned`. Django
limits the fetch (`LIMIT 21`) to report the count. Use only on unique lookups (pk, unique fields,
or a `UniqueConstraint`). `first()` is not a substitute when uniqueness is expected — it hides
data bugs.

```python
try:
    product = Product.objects.get(sku=sku)
except Product.DoesNotExist:
    raise UnknownSku(sku) from None
```

`get_object_or_404` is the view-layer shortcut; services should raise domain exceptions.

### create()

`Model(**kwargs).save(force_insert=True)` — one `INSERT … RETURNING "id"` on PostgreSQL. Does not
run `full_clean()`; rely on constraints for integrity, call `full_clean()` explicitly when you
want model validation ([model-design.md](./model-design.md)).

### get_or_create() and update_or_create()

```python
customer, created = Customer.objects.get_or_create(
    email=email.lower(),                 # lookup — must be backed by a unique constraint
    defaults={"name": name},             # used only on insert
)
```

Mechanics: `get()`; on `DoesNotExist`, `create()` inside a savepoint (`atomic`); on
`IntegrityError` (a concurrent insert won), `get()` again. This is race-safe **only if** the
lookup fields are covered by a unique constraint — otherwise two concurrent callers both insert.

`update_or_create(defaults=..., create_defaults=...)` (`create_defaults` Django ≥ 5.0): locks the
row with `select_for_update()` inside `atomic`, then updates via `save()`, or creates. Since
Django 4.2 the `save()` receives `update_fields` = all `defaults` keys (changed or not) plus
fields with a custom `pre_save()` such as `auto_now`; if any key is not a concrete field it falls
back to a full `save()` ([4.2 release notes](https://docs.djangoproject.com/en/4.2/releases/4.2/#setting-update-fields-in-model-save-may-now-be-required);
details from source, `django/db/models/query.py`). A custom `save()` that modifies other fields
must add them to `update_fields`. It issues 2–3 queries; for
many rows prefer `bulk_create(update_conflicts=True, ...)`
([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)).

```python
Payment.objects.update_or_create(
    provider_reference=event["id"],
    defaults={"status": event["status"]},
    create_defaults={"status": event["status"], "order_id": order_id,
                     "amount": amount, "idempotency_key": event["id"]},
)
```

### update()

```python
updated = (StockItem.objects
           .filter(product_id=pid, warehouse_id=wid,
                   quantity_on_hand__gte=F("quantity_reserved") + qty)
           .update(quantity_reserved=F("quantity_reserved") + qty))
if updated == 0:
    raise InsufficientStock(pid)
```

```sql
UPDATE "inventory_stockitem"
SET "quantity_reserved" = ("inventory_stockitem"."quantity_reserved" + %s)
WHERE ("product_id" = %s AND "warehouse_id" = %s
       AND "quantity_on_hand" >= ("quantity_reserved" + %s))
```

Returns the number of matched rows , which may exceed the rows actually changed
([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#update)). MySQL would
natively report *changed* rows; Django's MySQL backend sets `CLIENT.FOUND_ROWS` so it reports
matched rows too (source: `django/db/backends/mysql/base.py`, 5.2). Bypasses `save()`, signals and `auto_now` (set `updated_at=Now()`
explicitly). Can only update columns of the QuerySet's own model table (no joined-table updates;
filtering across relations is fine). Cannot be called on a sliced QuerySet.

### delete()

Returns `(total_deleted, {"orders.OrderLine": n, "orders.Order": m})`. Django's collector emulates
`on_delete` in Python: it may `SELECT` related rows, run `pre_delete`/`post_delete` signals, and
issue several `DELETE`s. Fast path (single `DELETE ... WHERE`) only when there are no signals
and no cascades to collect. `PROTECT`/`RESTRICT` raise `ProtectedError`/`RestrictedError`.
Large deletes: batch them ([bulk-operations-and-batching.md](./bulk-operations-and-batching.md)).

### values() and values_list()

```python
Order.objects.filter(status="paid").values("id", "customer__email", "total")
# → QuerySet of dicts: {"id": 1, "customer__email": "...", "total": Decimal("10.00")}

Product.objects.filter(is_active=True).values_list("sku", flat=True)
# → QuerySet of strings

Product.objects.values_list("sku", "price", named=True)   # namedtuple-like Row objects
```

Return QuerySets (lazy, chainable). No model instances: no methods, no `save()`, cheaper to build.
Traversing a reverse/M2M relation in `values()` returns one row per related row. `values()` before
`annotate()` defines GROUP BY ([aggregation-and-annotation.md](./aggregation-and-annotation.md)).

### distinct()

`SELECT DISTINCT` over all selected columns — combined with `order_by` on a related field, the
ordering column is added to the select list and can defeat de-duplication.
`distinct("customer_id")` → `DISTINCT ON` (PostgreSQL only) and requires `order_by` to start with
the same fields:

```python
# latest order per customer (PostgreSQL)
Order.objects.order_by("customer_id", "-created_at").distinct("customer_id")
```

Prefer `Exists` over `filter(...).distinct()` for "parents having a child that…": no duplicates
to remove, usually a cheaper plan.

### order_by() and reverse()

- No ordering → undefined row order; never paginate without `order_by` and a unique tiebreaker
  (`order_by("-created_at", "-pk")`).
- `Meta.ordering` adds an `ORDER BY` to every query that doesn't specify one — a sort cost on
  large tables. Since Django 3.1 it is no longer applied to `GROUP BY`/aggregation queries. Keep
  `Meta.ordering` off large tables; order explicitly in selectors.
- `order_by()` with no args clears ordering — useful before `aggregate`, `distinct`, or in
  subqueries.
- `order_by("?")` = random sort of the whole result — expensive on large tables.
- Ordering by a related field adds a join.
- `reverse()` flips the existing ordering; has no effect without one. `last()` uses it.

### defer() and only()

```python
Product.objects.only("id", "sku", "price")           # load these; others deferred
Product.objects.defer("description", "spec_json")    # load all but these
```

Accessing a deferred field triggers **one query per instance** (`refresh_from_db(fields=[...])`).
Use only when profiling shows wide rows (large `TextField`/`JSONField`) cost real time and the
code path provably doesn't touch them. Prefer `values()` for pure reads. `only()` with
`select_related` must include the FK field (`only("sku", "category__name")` loads `category_id`).

### select_related()

Follows forward `ForeignKey` / `OneToOneField` (and reverse `OneToOne`) with SQL joins in the same
query. Nullable FKs use `LEFT OUTER JOIN`.

```python
OrderLine.objects.select_related("order__customer", "product")
```

Every relation multiplies columns per row; joining several wide tables can make one query slower
than two. `select_related()` without arguments follows all non-null FKs — avoid in production
code. Strategy: [query-optimization.md](./query-optimization.md).

### prefetch_related()

Runs one additional query per lookup and joins in Python. Works for reverse FK, M2M, generic
relations, and forward FKs too.

```python
orders = (Order.objects.filter(customer_id=42)
          .prefetch_related("lines__product"))      # 3 queries total
```

```sql
SELECT ... FROM "orders_order" WHERE "customer_id" = %s;
SELECT ... FROM "orders_orderline" WHERE "order_id" IN (%s, %s, ...);
SELECT ... FROM "catalog_product" WHERE "id" IN (%s, %s, ...);
```

Large `IN` lists can hit backend parameter limits or poor plans
([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#prefetch-related)). Django
splits an `IN` list only where the backend declares `max_in_list_size` — Oracle (1000 per list,
OR-ed together); it does **not** batch on SQLite, where the statement is bounded by
`SQLITE_MAX_VARIABLE_NUMBER` (999 before SQLite 3.32.0, 32766 after —
[SQLite limits](https://www.sqlite.org/limits.html#max_variable_number)) (source:
`django/db/models/lookups.py`, `backends/oracle/operations.py`, 5.2). Filtering/ordering the prefetched set needs `Prefetch(lookup, queryset=...)`
([query-optimization.md](./query-optimization.md)).

### select_for_update()

`SELECT … FOR UPDATE` — must run inside `transaction.atomic()` (raises
`TransactionManagementError` in autocommit when evaluated). Options: `nowait=True`,
`skip_locked=True`, `of=("self",)`, `no_key=True` (PostgreSQL `FOR NO KEY UPDATE`). Backend
support varies; SQLite ignores it. With `select_related`, joined rows are locked too unless `of`
restricts it (PostgreSQL). Lock semantics and patterns:
[transactions-and-concurrency.md](./transactions-and-concurrency.md).

```python
with transaction.atomic():
    item = (StockItem.objects.select_for_update()
            .get(product_id=pid, warehouse_id=wid))
```

### union(), intersection(), difference()

```python
recent = Order.objects.filter(created_at__gte=since).values_list("id", "total")
large = Order.objects.filter(total__gte=1000).values_list("id", "total")
recent.union(large).order_by("-total")[:50]           # UNION (deduplicated)
recent.union(large, all=True)                         # UNION ALL
```

- All parts must select the same number/types of columns.
- Resulting QuerySet allows only `LIMIT/OFFSET`, `count()`, `order_by()` (by selected column
  names), `values()`/`values_list()`, `exists()` — no `filter()`, `annotate()`, etc.
- `intersection`/`difference` are not supported on MySQL < 8.0.31 / some backends — check
  `connection.features.supports_select_intersection` / `supports_select_difference`.
- Often a single `filter(Q(...) | Q(...))` is simpler and lets the planner use one index scan or
  bitmap OR; use set operations when the parts hit different tables or need different
  projections.

## 6. Production use cases

- **Checkout**: `get_or_create` for a customer by email (unique constraint) → `select_for_update`
  on stock rows → conditional `update()` with `F()` → `create()` order and lines in one `atomic`.
- **Webhook ingest**: `update_or_create(provider_reference=...)` makes retries idempotent.
- **Nightly expiry**: `Order.objects.filter(status="pending", created_at__lt=cutoff).update(status="cancelled")`
  — one statement instead of loading thousands of rows.
- **CSV export**: `values_list(...).iterator(chunk_size=5000)`.
- **Worker queue**: `select_for_update(skip_locked=True)` on a jobs table so workers don't block
  each other.

## 7. Advantages and disadvantages

| Method family | Advantage | Disadvantage |
|---|---|---|
| `update`/`delete` | One statement, atomic, race-free with `F()` | Skips `save()`, signals, `auto_now`; collector may still be expensive |
| `get_or_create` | Race-safe with constraint | Not race-safe without it; savepoint overhead |
| `values`/`values_list` | Cheap rows | Lose model behaviour; dict keys tied to lookup names |
| `only`/`defer` | Smaller rows | Hidden per-instance queries |
| `select_related` | One round trip | Wide rows, join fan-out cost |
| `prefetch_related` | No row multiplication | Extra queries, large `IN` lists, memory |
| Set ops | Express unions directly | Restricted follow-up API, backend variation |

## 8. Security and performance considerations

- Never pass raw user input to `order_by()`, `values()`, or `filter(**user_dict)`: field names
  and lookups are not parameters — users can traverse relations (`customer__password`) or cause
  expensive queries. Whitelist allowed fields ([django-security-engineering](../../django-security-engineering/SKILL.md)).
- Tenant/ownership scoping belongs in the QuerySet (`filter(customer=request.user.customer)`)
  before `get()`, not after.
- `get_or_create` without a unique constraint is a duplicate-row bug under load.
- `delete()` on a QuerySet with many cascades can lock large parts of the database and load many
  objects into memory for signals.
- `order_by("?")`, unbounded `distinct()`, and `values()` across reverse relations scale badly.

## 9. Common mistakes and anti-patterns

- `.all().filter(...)` / `.filter().all()` noise.
- `Model.objects.filter(pk=pk)[0]` instead of `get()` (hides missing/multiple rows as IndexError).
- Relying on `first()` to pick "the latest" — on an unordered QuerySet it orders by pk, which is
  not creation time for UUID keys; order explicitly.
- `filter(lines__...)` without `distinct()`/`Exists`, then counting → inflated numbers.
- `update_or_create` inside a loop for thousands of rows.
- Calling `save()` on objects from `values()` — they're dicts.
- `select_for_update()` outside a transaction, or on SQLite in tests and assuming it locked.
- `qs.update()` expecting `updated_at` (`auto_now`) to change.
- `only()` then touching deferred fields in a template loop.

## 10. Troubleshooting and validation

| Symptom | Likely cause |
|---|---|
| Duplicate objects in results | Join over multi-valued relation; add `distinct()` or use `Exists` |
| `TypeError: Cannot filter a query once a slice has been taken` | Filter before slicing |
| `TransactionManagementError: select_for_update cannot be used outside of a transaction` | Wrap in `atomic` |
| Duplicates despite `get_or_create` | Missing unique constraint on lookup fields |
| `MultipleObjectsReturned` from `get_or_create` | Same — or lookup broader than the constraint |
| Query count grows per row | Deferred field access or missing `select_related`/`prefetch_related` |
| `updated_at` not changing | `update()` bypasses `auto_now` |

Validate with `str(qs.query)`, `assertNumQueries`, and `qs.explain()` for plans
([query-optimization.md](./query-optimization.md)).

## 11. Related references and skills

- [queryset-internals.md](./queryset-internals.md) — evaluation and caching.
- [advanced-query-expressions.md](./advanced-query-expressions.md),
  [aggregation-and-annotation.md](./aggregation-and-annotation.md).
- [query-optimization.md](./query-optimization.md) — `Prefetch`, `explain()`, pagination.
- [transactions-and-concurrency.md](./transactions-and-concurrency.md) — locking.
- [bulk-operations-and-batching.md](./bulk-operations-and-batching.md).
- [managers-and-querysets.md](./managers-and-querysets.md) — packaging reusable filters.
- [django-rest-framework](../../django-rest-framework/SKILL.md) — API filtering/ordering whitelists.

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/ref/models/querysets/
- https://docs.djangoproject.com/en/stable/topics/db/queries/
- https://docs.djangoproject.com/en/stable/topics/db/queries/#spanning-multi-valued-relationships
- https://docs.djangoproject.com/en/stable/ref/databases/ (backend notes)
