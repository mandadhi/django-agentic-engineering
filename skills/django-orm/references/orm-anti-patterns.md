# ORM Anti-patterns

> Scope: a diagnostic catalogue of recurring Django ORM mistakes — each with problem, cause,
> impact, bad and better implementation, and how to detect it — plus a review checklist.
> Not covered here: the full explanation of each fix, which lives in the owning reference linked
> from every entry; architecture-level anti-patterns such as fat models or god apps
> ([django-architecture: architecture-anti-patterns](../../django-architecture/references/architecture-anti-patterns.md));
> security review beyond SQL injection ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 1. Purpose and scope

Use this reference when reviewing ORM code, investigating a slow or incorrect query, or
auditing a codebase. Entries are deliberately compact: recognise the pattern, apply the better
implementation, then load the owning reference if the situation is non-trivial. Examples use the
canonical `customers`/`catalog`/`inventory`/`orders`/`payments`/`billing` models.

## 2. Core concepts

Most ORM anti-patterns fall into four families:

| Family | Root cause | Entries |
|---|---|---|
| Hidden round trips | Lazy evaluation and relation access are invisible in Python code | AP-1 – AP-6, AP-20 |
| Wrong place for work | Filtering, counting or aggregating in Python instead of SQL — or the reverse, cramming everything into one giant query | AP-6, AP-9 – AP-14 |
| Missing guarantees | Invariants checked in Python, not enforced by the database or atomic statements | AP-8, AP-15 |
| Accidental complexity | Abstractions, raw SQL or migrations that add risk without value | AP-7, AP-16 – AP-19 |

## 3. Internal mechanics

The mechanics behind these failures, in one place:

- A QuerySet executes on iteration, `len()`, `list()`, `bool()`, slicing with a step, `repr()`
  and pickling; related-object access on an instance executes unless cached
  ([queryset-internals.md](./queryset-internals.md)).
- Every chained call returns a **new** QuerySet with an empty result cache; `qs.filter()` after
  evaluation re-queries.
- Joins across multi-valued relations multiply rows before aggregation
  ([aggregation-and-annotation.md](./aggregation-and-annotation.md)).
- Python-side checks are not atomic with the following write
  ([transactions-and-concurrency.md](./transactions-and-concurrency.md)).

## 4. Engineering decisions and trade-offs

Not every listed pattern is always wrong. A loop that runs a query per item over a guaranteed
3-element list is fine; a `count()` that drives a UI label is necessary. Fix an anti-pattern
when (a) it is on a hot or growing path, (b) it risks correctness or security, or (c) the fix
is equally simple. Correctness and security entries (AP-8, AP-14, AP-15, AP-18) are fixed
unconditionally; performance entries are fixed on evidence ([query-optimization.md](./query-optimization.md)).

## 5. Django implementation patterns

Catalogue format: **Problem · Why · Impact · Bad · Better · Detect**.

### AP-1 N+1 queries
- **Problem**: related objects loaded one query per row.
- **Why**: attribute access (`order.customer`) looks free.
- **Impact**: latency grows linearly with rows; DB saturated by tiny queries.
```python
# Bad
for order in Order.objects.filter(status="paid"):
    send(order.customer.email)
# Better
for order in Order.objects.filter(status="paid").select_related("customer"):
    send(order.customer.email)
```
- **Detect**: query count scales with page size; repeated identical SQL in debug toolbar/
  `CaptureQueriesContext`; `assertNumQueries`. → [query-optimization.md](./query-optimization.md)

### AP-2 Querying inside loops
- **Problem**: explicit `get()`/`filter()` per item.
- **Why**: code written item-by-item, often in services processing lists.
- **Impact**: same as N+1; also non-atomic multi-step writes.
```python
# Bad
for pid, qty in items:
    product = Product.objects.get(pk=pid)
# Better
products = Product.objects.in_bulk([pid for pid, _ in items])
```
- **Detect**: ORM calls lexically inside `for` loops; query logs with repeated shapes.

### AP-3 Unnecessary `.all()` evaluation / double evaluation
- **Problem**: `list(qs.all())` then re-filtering the QuerySet, or evaluating the same QuerySet
  twice by rebuilding it.
- **Why**: misunderstanding that each chained call is a fresh query.
- **Impact**: duplicate queries, wasted memory.
```python
# Bad
if Order.objects.filter(customer=c).count():
    for o in Order.objects.filter(customer=c): ...     # 2 queries
# Better
orders = list(Order.objects.filter(customer=c))
if orders:
    for o in orders: ...                               # 1 query
```
- **Detect**: identical SQL twice in one request. → [queryset-internals.md](./queryset-internals.md)

### AP-4 Calling `count()` unnecessarily
- **Problem**: `count()` used for existence or before iterating rows you'll load anyway.
- **Why**: habit; `if qs.count() > 0`.
- **Impact**: `COUNT(*)` scans all matching rows; extra round trip.
```python
# Bad
if Payment.objects.filter(order=o).count() > 0: ...
# Better
if Payment.objects.filter(order=o).exists(): ...
```
- **Detect**: `SELECT COUNT(*)` immediately followed by the same `WHERE` in logs.

### AP-5 Calling `exists()` unnecessarily
- **Problem**: `exists()` followed by evaluating the same QuerySet.
- **Why**: over-applying the "use exists()" rule.
- **Impact**: two queries where one suffices.
```python
# Bad
lines = order.lines.all()
if lines.exists():
    render(lines)          # second query
# Better
lines = list(order.lines.all())
if lines:
    render(lines)
```
- **Detect**: `SELECT 1 ... LIMIT 1` then same query without limit.

### AP-6 Python-side filtering of large QuerySets
- **Problem**: loading rows then filtering/sorting/summing in Python.
- **Why**: unfamiliarity with lookups, `Q`, annotations.
- **Impact**: transfers and instantiates rows that are discarded; O(n) memory.
```python
# Bad
paid = [o for o in Order.objects.all() if o.status == "paid" and o.total > 100]
revenue = sum(o.total for o in paid)
# Better
from django.db.models import Sum
revenue = Order.objects.filter(status="paid", total__gt=100).aggregate(s=Sum("total"))["s"]
```
- **Detect**: list comprehensions over querysets with conditions; large result sets vs small
  output. → [aggregation-and-annotation.md](./aggregation-and-annotation.md)

### AP-7 Unnecessary raw SQL
- **Problem**: `raw()`/`cursor.execute()` for queries the ORM expresses.
- **Why**: SQL familiarity; outdated belief the ORM can't do subqueries/windows.
- **Impact**: bypasses tenant scoping managers, breaks on schema changes, backend lock-in,
  injection risk.
```python
# Bad
Customer.objects.raw("SELECT * FROM customers_customer c WHERE EXISTS "
                     "(SELECT 1 FROM orders_order o WHERE o.customer_id = c.id AND o.status='pending')")
# Better
from django.db.models import Exists, OuterRef
Customer.objects.filter(Exists(Order.objects.filter(customer=OuterRef("pk"), status="pending")))
```
- **Detect**: grep `raw(`, `cursor(`, `extra(`. → [raw-sql-and-database-access.md](./raw-sql-and-database-access.md)

### AP-8 Missing database constraints
- **Problem**: uniqueness/ranges enforced only in forms, serializers or `clean()`.
- **Why**: validation feels sufficient; constraints forgotten.
- **Impact**: duplicates and invalid rows from races, bulk ops, shell, other services.
```python
# Bad: only a serializer check that SKU is unique
# Better
class StockItem(models.Model):
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "warehouse"], name="stockitem_product_warehouse_uniq"),
            models.CheckConstraint(condition=Q(quantity_reserved__lte=F("quantity_on_hand")),
                                   name="stockitem_reserved_lte_on_hand"),
        ]
```
- **Detect**: query production for violating rows; review models for invariants stated only in
  validators. → [database-constraints-and-indexes.md](./database-constraints-and-indexes.md)

### AP-9 Incorrect index usage
- **Problem**: missing indexes on filter/order columns, wrong column order in composites, or
  indexes on low-selectivity columns; queries that can't use an index (`__icontains`,
  `__date`, functions on columns).
- **Why**: indexes added by guess, not from query patterns.
- **Impact**: seq scans on large tables, or write amplification from useless indexes.
```python
# Bad: index on status alone (4 values), query filters customer + orders by created_at
indexes = [models.Index(fields=["status"], name="order_status_idx")]
# Better: matches WHERE customer_id = ? ORDER BY created_at DESC
indexes = [models.Index(fields=["customer", "-created_at"], name="order_customer_created_idx")]
```
- **Detect**: `explain()` showing `Seq Scan` + `Sort`; PostgreSQL `pg_stat_user_indexes` with
  `idx_scan = 0`. → [database-constraints-and-indexes.md](./database-constraints-and-indexes.md)

### AP-10 Overusing `select_related()`
- **Problem**: joining many or wide tables, or relations not used.
- **Why**: "joins are free" belief; argument-less `select_related()`.
- **Impact**: wide rows, large sorts, slower than a few small queries.
```python
# Bad
Order.objects.select_related()   # follows every non-null FK, recursively to depth limit
# Better
Order.objects.select_related("customer")   # exactly what the template uses
```
- **Detect**: `explain` shows many joins; columns selected ≫ columns used.

### AP-11 Overusing `prefetch_related()`
- **Problem**: prefetching relations not used, prefetching whole child tables to count them, or
  prefetching then filtering the related manager.
- **Why**: blanket "optimisation" in a base selector.
- **Impact**: extra queries, huge `IN` lists, memory.
```python
# Bad
for c in Customer.objects.prefetch_related("orders"):
    recent = c.orders.filter(status="pending")       # ignores prefetch → N+1 anyway
# Better
Customer.objects.prefetch_related(
    Prefetch("orders", queryset=Order.objects.filter(status="pending"), to_attr="pending_orders"))
```
- **Detect**: prefetch queries returning far more rows than rendered; N+1 despite prefetch.

### AP-12 Huge unbounded QuerySets
- **Problem**: iterating `Model.objects.all()` on large tables, or returning unbounded lists from
  endpoints.
- **Why**: works in development with small data.
- **Impact**: memory exhaustion, timeouts, DoS surface.
```python
# Bad
for p in Product.objects.all(): export(p)
# Better
for row in Product.objects.values_list("sku", "price").order_by("pk").iterator(chunk_size=2000):
    export(row)
```
- **Detect**: memory spikes; endpoints without pagination. → [bulk-operations-and-batching.md](./bulk-operations-and-batching.md)

### AP-13 Inefficient pagination
- **Problem**: deep `OFFSET` pages and `COUNT(*)` on every page; unstable ordering.
- **Why**: default `Paginator` used for infinite feeds and huge tables.
- **Impact**: cost grows with page number; duplicates/missing rows across pages without a
  unique tiebreaker.
```python
# Bad
Order.objects.order_by("created_at")[offset:offset + 50]        # non-unique ordering, deep offset
# Better: keyset on (created_at, id)
Order.objects.filter(Q(created_at__gt=ts) | Q(created_at=ts, id__gt=last_id)).order_by("created_at", "id")[:50]
```
- **Detect**: latency rising with page number; `OFFSET` large in logs. → [query-optimization.md](./query-optimization.md)

### AP-14 Incorrect aggregation (join fan-out)
- **Problem**: aggregating over two multi-valued relations in one `annotate()`, or `values()`
  placed after `annotate()` changing grouping, or default ordering leaking into `GROUP BY`.
- **Why**: each `annotate` joins; joins multiply rows.
- **Impact**: silently wrong totals — a correctness bug, not a performance one.
```python
# Bad: lines × payments rows per order → both sums inflated
Order.objects.annotate(items=Sum("lines__quantity"), paid=Sum("payments__amount"))
# Better: aggregate one relation in a subquery
from django.db.models import OuterRef, Subquery, Sum
paid = (Payment.objects.filter(order=OuterRef("pk")).values("order")
        .annotate(s=Sum("amount")).values("s"))
Order.objects.annotate(items=Sum("lines__quantity"), paid=Subquery(paid))
```
- **Detect**: compare against a single-relation aggregate; inspect `str(qs.query)` for two
  multi-valued joins. → [aggregation-and-annotation.md](./aggregation-and-annotation.md)

### AP-15 Race-prone read-then-write
- **Problem**: reading a value, deciding in Python, writing back.
- **Why**: the natural way to write code; works in single-user testing.
- **Impact**: lost updates, oversell, double-spend.
```python
# Bad
item = StockItem.objects.get(product=p, warehouse=w)
if item.quantity_on_hand - item.quantity_reserved >= qty:
    item.quantity_reserved += qty
    item.save()
# Better
updated = StockItem.objects.filter(
    product=p, warehouse=w, quantity_on_hand__gte=F("quantity_reserved") + qty,
).update(quantity_reserved=F("quantity_reserved") + qty)
```
- **Detect**: `get()` → `if` → `save()` on shared counters/balances/status; concurrency tests
  with `TransactionTestCase`. → [transactions-and-concurrency.md](./transactions-and-concurrency.md)

### AP-16 Overcomplicated managers
- **Problem**: business workflows, side effects, or dozens of one-off methods on managers; or
  overriding `get_queryset()` on the default manager to hide rows.
- **Why**: managers treated as the "model service layer".
- **Impact**: hidden filtering surprises (admin, related lookups, `dumpdata`), untestable logic,
  side effects during queries.
```python
# Bad
class OrderManager(models.Manager):
    def place(self, customer, items): ...   # transactions, emails, payments
# Better: chainable query vocabulary only; the use case lives in a service
class OrderQuerySet(models.QuerySet):
    def pending(self): return self.filter(status=Order.Status.PENDING)
    def for_customer(self, customer): return self.filter(customer=customer)
```
- **Detect**: managers importing services/tasks/email; default manager filtering rows.
  → [managers-and-querysets.md](./managers-and-querysets.md)

### AP-17 Unnecessary repository abstractions
- **Problem**: `OrderRepository.get_by_id()` wrapping `Order.objects.get()` one-to-one.
- **Why**: patterns imported from other ecosystems.
- **Impact**: loses QuerySet composition (`select_related`, chaining), duplicates the ORM API,
  hides query cost.
```python
# Bad
class OrderRepository:
    def get(self, pk): return Order.objects.get(pk=pk)
# Better: use the ORM directly; extract a selector only when scoping/loading logic is shared
def order_detail(*, user, pk):
    return Order.objects.for_customer(user.customer).select_related("customer").get(pk=pk)
```
- **Detect**: classes whose methods each call one ORM method. → [django-architecture: design-patterns](../../django-architecture/references/design-patterns.md)

### AP-18 Unsafe dynamic SQL
- **Problem**: f-strings/`%`/`.format()` building SQL with user input; dynamic `order_by` from
  query params without a whitelist.
- **Why**: convenience; parameter placeholders don't work for identifiers.
- **Impact**: SQL injection — data exfiltration or destruction.
```python
# Bad
cursor.execute(f"SELECT * FROM catalog_product WHERE name = '{name}'")
Product.objects.order_by(request.GET["sort"])            # arbitrary field/relation traversal
# Better
cursor.execute("SELECT * FROM catalog_product WHERE name = %s", [name])
SORTS = {"price": "price", "-price": "-price", "name": "name"}
Product.objects.order_by(SORTS.get(request.GET.get("sort"), "name"))
```
- **Detect**: grep for `execute(f"`, `raw(f"`, `extra(`, `RawSQL(` with formatting; Bandit
  B608. → [raw-sql-and-database-access.md](./raw-sql-and-database-access.md)

### AP-19 Migration anti-patterns
- **Problem**: importing app models in data migrations; editing applied migrations; dropping a
  column in the same release as the code change; non-concurrent index on a hot table; huge
  backfills inside an atomic migration.
- **Why**: migrations treated as throwaway scripts.
- **Impact**: failed deploys, downtime from locks, unreproducible schema.
```python
# Bad
from apps.orders.models import Order
def forwards(apps, schema_editor):
    for o in Order.objects.all(): o.placed_at = o.created_at; o.save()
# Better
def forwards(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Order.objects.filter(placed_at__isnull=True).update(placed_at=F("created_at"))
```
- **Detect**: review `sqlmigrate`; CI `makemigrations --check`; grep migrations for `from apps.`.
  → [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md)

### AP-20 Unnecessary database round trips
- **Problem**: loading a related object to read its id; `get()` then `save()` to change one
  field; `save()` writing every column; re-fetching what you just created.
- **Why**: instance-centric habits.
- **Impact**: extra queries and wider writes; overwrites concurrent changes to other columns.
```python
# Bad
order = Order.objects.get(pk=pk); order.status = "cancelled"; order.save()   # SELECT + UPDATE all columns
cid = order.customer.id                                                      # extra SELECT
# Better
Order.objects.filter(pk=pk, status="pending").update(status="cancelled")     # one guarded UPDATE
cid = order.customer_id
order.save(update_fields=["status"])     # when you must go through save()
```
- **Detect**: `SELECT` immediately before `UPDATE` of the same pk; `UPDATE` setting every column.

## 6. Production use cases

- **Code review gate**: run through the checklist in section 10 on every PR touching
  `models.py`, `selectors.py`, `services.py` or `migrations/`.
- **Incident triage**: slow endpoint → AP-1/2/9/10/12/13; wrong numbers → AP-14; oversell or
  duplicates → AP-8/15; deploy outage → AP-19.
- **Codebase audit**: grep-based detection (raw SQL, migrations imports, `.save()` without
  `update_fields` in services) plus `assertNumQueries` coverage on list views.

## 7. Advantages and disadvantages

A catalogue speeds recognition and makes reviews consistent. Its risk is cargo-culting: applying
"better" implementations where the "bad" one is harmless (tiny lists, admin-only paths) adds
complexity. Pair every performance fix with a measurement.

## 8. Security and performance considerations

Security-relevant entries: AP-18 (injection), AP-12 (unbounded results as DoS), AP-15 (races as
double-spend), AP-16 (default-manager filtering that leaks or hides rows inconsistently), AP-7
(raw SQL bypassing tenant scoping). Performance entries require evidence before change; see
[query-optimization.md](./query-optimization.md).

## 9. Common mistakes and anti-patterns

Mistakes made while *fixing* anti-patterns:

- Replacing N+1 with a giant multi-join that is slower than the original (AP-10).
- Adding `exists()` everywhere, causing AP-5.
- Adding an index for every filter without checking selectivity or write cost.
- Wrapping everything in `select_for_update` to fix a race that a single `update()` solves.
- Adding a repository/manager layer to "centralise" fixes (AP-16/17).

## 10. Troubleshooting and validation

Review checklist:

- [ ] No ORM calls inside loops over unbounded collections; list views have `assertNumQueries`.
- [ ] Every relation used in a loop is `select_related`/`prefetch_related`ed — and nothing more.
- [ ] No Python-side filtering/aggregation of large querysets.
- [ ] Invariants have DB constraints; shared counters use `F()`/conditional `update()` or locks.
- [ ] Aggregations don't join two multi-valued relations.
- [ ] Queries in user-facing paths are bounded and paginated with stable ordering.
- [ ] Raw SQL is justified, parameterised, and identifiers are whitelisted.
- [ ] Managers/QuerySets only express queries; no repositories that mirror the ORM.
- [ ] Migrations use historical models, are reversible or documented, and deploy-safe.

Detection tools: `CaptureQueriesContext`/`assertNumQueries`, django-debug-toolbar (dev),
`QuerySet.explain()`, PostgreSQL `pg_stat_statements`, APM traces, grep/static analysis (Bandit).

## 11. Related references and skills

Owning references: [queryset-internals.md](./queryset-internals.md) ·
[queryset-api.md](./queryset-api.md) · [query-optimization.md](./query-optimization.md) ·
[aggregation-and-annotation.md](./aggregation-and-annotation.md) ·
[database-constraints-and-indexes.md](./database-constraints-and-indexes.md) ·
[transactions-and-concurrency.md](./transactions-and-concurrency.md) ·
[managers-and-querysets.md](./managers-and-querysets.md) ·
[raw-sql-and-database-access.md](./raw-sql-and-database-access.md) ·
[bulk-operations-and-batching.md](./bulk-operations-and-batching.md) ·
[migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md).

Skills: [django-architecture: architecture-anti-patterns](../../django-architecture/references/architecture-anti-patterns.md) ·
[django-security-engineering](../../django-security-engineering/SKILL.md) · [django-performance](../../django-performance/SKILL.md) ·
[django-testing](../../django-testing/SKILL.md).

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/topics/db/optimization/
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#when-querysets-are-evaluated
- https://docs.djangoproject.com/en/stable/topics/db/aggregation/#order-of-annotate-and-filter-clauses
- https://docs.djangoproject.com/en/stable/topics/security/#sql-injection-protection
- https://docs.djangoproject.com/en/stable/topics/db/sql/
- https://docs.djangoproject.com/en/stable/howto/writing-migrations/
- https://bandit.readthedocs.io/en/latest/plugins/b608_hardcoded_sql_expressions.html
