# Bulk Operations and Batching

> Scope: `bulk_create()`, `bulk_update()`, `QuerySet.update()`/`delete()` as bulk writes,
> `iterator()` and keyset batching for large reads, batch sizing, memory-efficient processing,
> bulk relationship (M2M/through) creation, conflict handling (upserts), database parameter
> limits, transactions around batches, and which model hooks and signals bulk operations bypass.
> Not covered here: per-method QuerySet semantics ([queryset-api.md](./queryset-api.md));
> iterator/result-cache internals ([queryset-internals.md](./queryset-internals.md)); locking
> ([transactions-and-concurrency.md](./transactions-and-concurrency.md)); set-based raw SQL
> ([raw-sql-and-database-access.md](./raw-sql-and-database-access.md)); end-to-end import/export
> pipelines, file parsing, task orchestration ([django-data-processing](../../django-data-processing/SKILL.md));
> `COPY` and engine tuning ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

Row-at-a-time ORM code (`for row in rows: Model(...).save()`) costs one round trip per row and
runs every hook. Bulk APIs collapse that into a few statements — at the price of bypassing
model-level behaviour. This reference teaches an agent to pick the right bulk primitive, size
batches, keep memory flat, handle conflicts, and **consciously replace** the hooks that bulk
operations skip.

SQL is approximately what Django emits on PostgreSQL; examples were not executed.

## 2. Core concepts

| Operation | SQL | Returns | Runs `save()`/`delete()`? | Signals |
|---|---|---|---|---|
| `Model.objects.bulk_create(objs, batch_size=, ignore_conflicts=, update_conflicts=, update_fields=, unique_fields=)` | multi-row `INSERT` | list of objs | No | No `pre_save`/`post_save` |
| `Model.objects.bulk_update(objs, fields, batch_size=)` | `UPDATE ... SET f = CASE WHEN pk=.. THEN .. END WHERE pk IN (...)` | rows matched (Django ≥ 4.0) | No | None |
| `qs.update(**kwargs)` | single `UPDATE ... WHERE` | rows matched | No | None |
| `qs.delete()` | `DELETE` (after Python-side cascade collection) | `(total, {label: n})` | No (model `delete()` not called) | `pre_delete`/`post_delete` **are** sent per object |
| `qs.iterator(chunk_size=)` | streamed `SELECT` | generator | — | — |
| `related_manager.add(*objs)` / `set()` | bulk insert into through table | `None` | — | `m2m_changed` sent |
| `Through.objects.bulk_create(...)` | multi-row `INSERT` | list | No | No `m2m_changed` |

### What bulk operations bypass

| Behaviour | `bulk_create` | `bulk_update` | `qs.update()` | `qs.delete()` |
|---|---|---|---|---|
| Overridden `Model.save()` / `delete()` | skipped | skipped | skipped | skipped |
| `full_clean()` / validators | skipped (never automatic anyway) | skipped | skipped | — |
| `pre_save` / `post_save` signals | skipped | skipped | skipped | — |
| `pre_delete` / `post_delete` signals | — | — | — | **sent** (forces object loading) |
| `auto_now_add` / `auto_now` | applied (insert compiler calls `field.pre_save(obj, add=True)` — [5.2 source](https://github.com/django/django/blob/stable/5.2.x/django/db/models/sql/compiler.py)) | **not** applied | **not** applied | — |
| Database constraints and triggers | enforced | enforced | enforced | enforced |
| Multi-table inheritance children | not supported | supported; ancestor fields cost one extra query per ancestor ([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-update)) | local table only | supported |

Database constraints are the safety net that remains — another reason invariants belong in the
schema ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md)).

## 3. Internal mechanics

- **`bulk_create`** splits `objs` into batches (`batch_size` or the backend's computed maximum),
  issues one `INSERT ... VALUES (...), (...)` per batch, and wraps all batches in
  `transaction.atomic(savepoint=False)` — a failure rolls back every batch. Primary keys are set
  on the objects when the backend supports `RETURNING` for bulk inserts: PostgreSQL, MariaDB
  10.5+, SQLite 3.35+. On MySQL/older backends, PKs of new objects stay `None` (unless you
  assigned them, e.g. UUIDs). With `ignore_conflicts=True`, PKs are not set.
- **`bulk_update`** builds, per batch, a `CASE` expression per field keyed on PK. Statement size
  grows with `objs × fields`; very large batches generate huge SQL that is slow to parse and plan.
  Duplicate objects in `objs`: only the first is updated (docs). PKs cannot be updated.
- **`qs.update()`** is one statement; it can only set columns on the QuerySet's model table, but
  may filter across relations (Django compiles a subquery/join for the WHERE).
- **`qs.delete()`** uses `Collector`: if no signals are connected and no cascades need Python
  handling, it "fast deletes" with a single `DELETE ... WHERE`. Otherwise it fetches the objects
  (and cascaded relations) into memory to send signals and resolve `on_delete` — deleting a
  million rows with a `post_delete` receiver loads a million objects.
- **`iterator()`** skips the result cache. PostgreSQL uses server-side cursors (unless
  `DISABLE_SERVER_SIDE_CURSORS` is set, required with transaction-mode PgBouncer); Oracle always
  uses server-side cursors. MySQL/MariaDB do **not** stream: the driver loads the entire result
  set into memory and Django converts it in `chunk_size` batches, so `iterator()` saves model
  instances, not the raw result set. SQLite fetches in batches with `fetchmany()` but provides no
  isolation between queries on one connection — don't write to the table being iterated.
  Default `chunk_size` is 2000. Since Django 4.1, `prefetch_related()` works with `iterator()`
  when `chunk_size` is given; without it, 4.1–4.2 deprecated the call and Django ≥ 5.0 raises
  `ValueError` ([iterator docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator),
  [5.0 notes](https://docs.djangoproject.com/en/5.2/releases/5.0/)).
- **Parameter limits**: each value is a bind parameter. SQLite's historical limit is 999 (newer
  builds 32766) and Django computes a safe batch size there; Oracle has its own limit; PostgreSQL's
  wire protocol caps a statement at 65535 parameters (Int16 count in the
  [Bind message](https://www.postgresql.org/docs/current/protocol-message-formats.html)) and
  Django does not cap bulk batch size automatically on PostgreSQL — the base
  `bulk_batch_size()` returns `len(objs)` and the PostgreSQL backend doesn't override it (5.2
  source); only SQLite and Oracle limit it
  ([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create)). Always pass an explicit `batch_size` —
  `batch_size × number_of_fields` must stay below the backend limit.

## 4. Engineering decisions and trade-offs

| Situation | Choose |
|---|---|
| Same change to all matching rows | `qs.update()` (with `F()`/`Case` if values vary by rule) |
| Different values per row, values computed in Python | `bulk_update(objs, fields, batch_size=500–1000)` |
| Insert many new rows | `bulk_create(batch_size=...)` |
| Insert or update by natural key (idempotent import) | `bulk_create(update_conflicts=True, unique_fields=..., update_fields=...)` (Django ≥ 4.1) |
| Insert, silently skip existing | `bulk_create(ignore_conflicts=True)` — loses PKs and *all* error types the DB treats as conflicts |
| Need `save()` logic / signals per row | Loop with `save()`, or bulk + explicit replacement of the side effects |
| Read a very large table | `iterator(chunk_size=...)` or keyset batches |
| Millions of rows on PostgreSQL | `COPY` via psycopg — see [postgresql-engineering](../../postgresql-engineering/SKILL.md) and [django-data-processing](../../django-data-processing/SKILL.md) |

**One big transaction vs many small ones.** One transaction gives all-or-nothing semantics but
holds locks and generates a long-running transaction (bloat, replication lag, blocking DDL).
Committing per batch keeps the system responsive but needs **restartability** — process in a
deterministic order, record progress (last PK), and make each batch idempotent.

**Batch size.** Start at 500–2000 rows and measure. Larger batches reduce round trips but raise
statement size, lock duration and memory; returns diminish quickly.

## 5. Django implementation patterns

### bulk_create with explicit batch size

```python
from django.db import transaction

def create_order_lines(order: Order, items: list[CartItem]) -> list[OrderLine]:
    lines = [
        OrderLine(order=order, product_id=i.product_id, quantity=i.quantity, unit_price=i.unit_price)
        for i in items
    ]
    for line in lines:
        line.full_clean(exclude=["order"])        # validation is not automatic — do it deliberately
    return OrderLine.objects.bulk_create(lines, batch_size=500)
```

### Idempotent upsert by natural key (Django ≥ 4.1)

```python
def upsert_products(rows: list[dict]) -> None:
    products = [Product(sku=r["sku"], name=r["name"], price=r["price"],
                        category_id=r["category_id"], is_active=True) for r in rows]
    Product.objects.bulk_create(
        products,
        batch_size=1000,
        update_conflicts=True,
        unique_fields=["sku"],                     # must match a unique constraint/index
        update_fields=["name", "price", "category_id", "is_active"],
    )
```

```sql
INSERT INTO catalog_product (sku, name, price, category_id, is_active)
VALUES (%s, %s, %s, %s, %s), ...
ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name, price = EXCLUDED.price, ...
RETURNING id;
```

Backend notes: PostgreSQL and SQLite require `unique_fields`; MySQL/MariaDB use
`ON DUPLICATE KEY UPDATE` and do not accept `unique_fields` — any unique key conflict triggers
the update. Oracle does not support `update_conflicts`/`ignore_conflicts`
([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create)). PostgreSQL
raises "ON CONFLICT DO UPDATE command cannot affect row a second time" if the same key appears
twice **within one statement** — deduplicate the input first.

### bulk_update for per-row computed values

```python
def reprice(products: Iterable[Product], factor: Decimal) -> int:
    changed = []
    for p in products:
        p.price = (p.price * factor).quantize(Decimal("0.01"))
        changed.append(p)
    return Product.objects.bulk_update(changed, ["price"], batch_size=500)
```

If the new value is a pure function of existing columns, `qs.update(price=F("price") * factor)`
is one statement and avoids loading rows at all. `bulk_update` does not refresh `auto_now`
fields — set `updated_at` explicitly and include it in `fields`.

### Memory-efficient read-process-write with keyset batches

```python
def backfill_order_totals(batch_size: int = 1000) -> None:
    last_pk = 0
    while True:
        batch = list(
            Order.objects.filter(pk__gt=last_pk)
            .order_by("pk")
            .prefetch_related("lines")[:batch_size]
        )
        if not batch:
            break
        for order in batch:
            order.total = sum((l.quantity * l.unit_price for l in order.lines.all()), Decimal("0"))
        with transaction.atomic():
            Order.objects.bulk_update(batch, ["total"], batch_size=batch_size)
        last_pk = batch[-1].pk
```

Keyset batching (`pk__gt` + `order_by("pk")`) stays fast at any depth — `OFFSET` batching gets
slower each page and skips/duplicates rows if data changes. Each batch commits independently,
so the job is resumable from `last_pk`. (A pure-SQL alternative with a `Subquery` sum would
avoid loading lines at all — prefer it when it fits.)

### iterator() for streaming reads

```python
for customer in (Customer.objects.filter(is_active=True)
                 .only("pk", "email").iterator(chunk_size=2000)):
    export_writer.writerow([customer.pk, customer.email])

# values_list avoids model instantiation entirely
for pk, email in Customer.objects.filter(is_active=True).values_list("pk", "email").iterator(chunk_size=5000):
    ...
```

Do not iterate a server-side cursor for minutes inside a transaction on a busy primary — it
holds a snapshot. Prefer keyset batches for long jobs.

### Bulk relationship creation

```python
# Plain M2M (Product.tags = ManyToManyField(Tag), illustrative)
product.tags.add(*tags)                    # one INSERT, skips existing pairs, sends m2m_changed

# Many products × many tags — go through the auto-created through model
Through = Product.tags.through
Through.objects.bulk_create(
    [Through(product_id=p.pk, tag_id=t.pk) for p in products for t in tags],
    batch_size=1000,
    ignore_conflicts=True,                 # pairs already present
)                                           # no m2m_changed signal
```

Parent objects need PKs before children reference them: on backends without bulk `RETURNING`,
create parents individually or use client-generated keys (UUIDs / natural-key lookups).

### Bulk delete without loading objects

```python
# Fast path only when no signals/cascades require collection.
deleted, per_model = Payment.objects.filter(status="failed", created_at__lt=cutoff).delete()

# Huge purges: delete in PK batches to keep transactions and lock sets small.
while True:
    ids = list(Payment.objects.filter(status="failed", created_at__lt=cutoff)
               .values_list("pk", flat=True)[:5000])
    if not ids:
        break
    Payment.objects.filter(pk__in=ids).delete()
```

## 6. Production use cases

- **Catalogue sync** from a supplier feed: dedupe by `sku`, `bulk_create(update_conflicts=True)`
  in batches of 1000, each batch in its own transaction, progress recorded.
- **Inventory snapshot import**: upsert `StockItem` on `(product, warehouse)` — the
  `UniqueConstraint` is what makes `unique_fields=["product", "warehouse"]` valid.
- **Nightly expiry**: `Order.objects.filter(status="pending", created_at__lt=cutoff).update(status="cancelled")`
  then emit events explicitly.
- **Data backfills** for new columns during expand/contract migrations — see
  [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md).
- **Exports**: `values_list().iterator()` streamed to a file/response.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Orders of magnitude fewer round trips | Skips `save()`, signals, `auto_now` (update) — easy to break invariants kept in Python |
| Atomic per call; predictable SQL | Large statements: parse/plan cost, lock footprint |
| Native upserts for idempotent imports | Backend differences (PK return, conflict syntax) |
| Flat memory with `iterator()`/keysets | Error in one row fails the whole batch; locating it needs bisecting |

## 8. Security and performance considerations

- Validate untrusted input **before** bulk writes — no model validation runs. Constraints catch
  only what they encode.
- Audit logging, cache invalidation, search indexing and outbound events that live in signals or
  `save()` will silently not happen — call them explicitly, after commit
  ([django-core signals](../../django-core/references/signals-management-commands.md)).
- Tenant scoping: bulk updates/deletes must carry the tenant filter; a missing filter affects
  every tenant in one statement.
- Long transactions block autovacuum and DDL; commit per batch for big jobs.
- On MySQL and MariaDB, `ignore_conflicts=True` turns certain non-duplicate-key errors (e.g.
  invalid values, non-nullable violations) into warnings, even in Strict Mode
  ([docs](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create)). Validate
  input first and prefer explicit `unique_fields` upserts where supported.
- Indexes and constraints slow inserts; for one-off huge loads, PostgreSQL-specific strategies
  (drop/recreate indexes, `COPY`) belong to [postgresql-engineering](../../postgresql-engineering/SKILL.md).

## 9. Common mistakes and anti-patterns

- `for obj in objs: obj.save()` for thousands of rows.
- Omitting `batch_size` and hitting parameter limits or giant statements.
- Expecting `post_save` receivers (audit, search index) to fire after `bulk_create`.
- Assuming `bulk_update` sets `updated_at` (`auto_now`).
- Using `bulk_update` where `qs.update(F(...))` would avoid loading rows.
- `OFFSET`-based batching over a changing table.
- `list(Model.objects.all())` to "process everything".
- Upserting input with duplicate keys in the same batch.
- Deleting huge sets with `qs.delete()` while `post_delete` receivers exist (loads every object).
- One transaction around an hour-long import.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| `too many SQL variables` (SQLite) / protocol errors on huge inserts | Parameter limit | Smaller `batch_size` |
| Created objects have `pk=None` | Backend without bulk RETURNING, or `ignore_conflicts` | Client-side keys or re-query by natural key |
| `ON CONFLICT ... cannot affect row a second time` | Duplicate keys in batch | Deduplicate input |
| `ValueError` about `unique_fields` | Backend mismatch (MySQL) or fields not unique | Match backend rules and existing constraint |
| Memory climbs during export | Result cache (`for x in qs` / `list(qs)`) | `iterator()` or keysets |
| Delete takes minutes and lots of memory | Signal receivers / Python cascades | Batch deletes; review receivers; DB-level cascade where appropriate |
| Audit trail missing rows | Bulk bypassed signals | Explicit audit calls |

Validation: assert query counts (`assertNumQueries`) for batch paths; test with `batch_size`
smaller than the input to exercise multi-batch behaviour; test reruns are idempotent; compare
row counts and checksums before/after imports ([django-testing](../../django-testing/SKILL.md)).

## 11. Related references and skills

- [queryset-api.md](./queryset-api.md) — `update()`, `delete()` semantics.
- [queryset-internals.md](./queryset-internals.md) — result cache and `iterator()` mechanics.
- [transactions-and-concurrency.md](./transactions-and-concurrency.md) — batch transactions, locks.
- [advanced-query-expressions.md](./advanced-query-expressions.md) — `F`/`Case` in `update()`.
- [orm-anti-patterns.md](./orm-anti-patterns.md) — queries in loops, unbounded QuerySets.
- [django-data-processing](../../django-data-processing/SKILL.md) — import/export pipelines.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — `COPY`, bloat, vacuum.

## 12. Official documentation and external references

- `bulk_create`: https://docs.djangoproject.com/en/stable/ref/models/querysets/#bulk-create
- `bulk_update`: https://docs.djangoproject.com/en/stable/ref/models/querysets/#bulk-update
- `iterator`: https://docs.djangoproject.com/en/stable/ref/models/querysets/#iterator
- Updating multiple objects at once: https://docs.djangoproject.com/en/stable/topics/db/queries/#updating-multiple-objects-at-once
- Database access optimization: https://docs.djangoproject.com/en/stable/topics/db/optimization/
- Server-side cursors and PgBouncer: https://docs.djangoproject.com/en/stable/ref/databases/#transaction-pooling-server-side-cursors
- PostgreSQL `INSERT ... ON CONFLICT`: https://www.postgresql.org/docs/current/sql-insert.html
- PostgreSQL populating a database: https://www.postgresql.org/docs/current/populate.html
