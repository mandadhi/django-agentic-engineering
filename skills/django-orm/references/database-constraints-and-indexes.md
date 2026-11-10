# Database Constraints and Indexes

> Scope: enforcing invariants in the database and choosing indexes from query patterns —
> `UniqueConstraint`, `CheckConstraint`, primary-key and foreign-key constraints, composite,
> conditional and deferrable constraints, expression constraints, B-tree/composite/partial/
> functional/covering/unique indexes, index ordering, selectivity, maintenance cost, backend
> differences, and application validation versus database enforcement.
> Not covered here: building indexes and adding constraints safely on large production tables
> ([migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md)); reading query
> plans and profiling ([query-optimization.md](./query-optimization.md)); handling
> `IntegrityError` under concurrency ([transactions-and-concurrency.md](./transactions-and-concurrency.md));
> PostgreSQL index internals, GIN/GiST/BRIN tuning, bloat and vacuum
> ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

Constraints make invalid states unrepresentable, regardless of which code path, script,
admin action, bulk operation or concurrent request writes the row. Indexes make the queries
the application actually runs cheap — and make every write more expensive. This reference
helps an agent declare both in `Meta`, decide which are worth it, and understand what each
backend really enforces.

Baseline Django 5.2 LTS. SQL is approximately what Django emits for PostgreSQL.

## 2. Core concepts

| Concept | Django API | Enforced by |
|---|---|---|
| Not null | `null=False` (default) | DB |
| Primary key | `primary_key=True`, auto `id`, `CompositePrimaryKey` (Django ≥ 5.2) | DB |
| Foreign key | `ForeignKey`/`OneToOneField` | DB (`NO ACTION`, deferred on PostgreSQL/Oracle) + ORM `on_delete` |
| Unique (single) | `unique=True` | DB unique constraint/index |
| Unique (composite, conditional, expression) | `UniqueConstraint` | DB |
| Check | `CheckConstraint(condition=Q(...))` (`condition=` Django ≥ 5.1; `check=` deprecated) | DB on PostgreSQL, SQLite, Oracle, MariaDB; MySQL enforces CHECK only from [8.0.16](https://dev.mysql.com/doc/refman/8.0/en/create-table-check-constraints.html) (Django 5.2 accepts MySQL ≥ 8.0.11 — older servers raise [`models.W027`](https://docs.djangoproject.com/en/5.2/ref/checks/#models)) |
| Exclusion | `django.contrib.postgres.constraints.ExclusionConstraint` | PostgreSQL only |
| Index | `Meta.indexes = [Index(...)]`, `db_index=True` | DB |

**Application validation vs database enforcement.** Validation (forms, serializers,
`full_clean()`) produces good error messages but checks a snapshot: two requests can both
pass "email not taken" and both insert. Only a unique constraint makes the second insert
fail. Treat validation as UX and constraints as correctness. Since Django 4.1,
`Model.validate_constraints()` (part of `full_clean()`) evaluates `Meta.constraints` in
Python, so one declaration serves both — but the Python check is still racy.

## 3. Internal mechanics

### How Django emits constraints

```python
class StockItem(models.Model):
    ...
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "warehouse"], name="stockitem_product_warehouse_uniq"),
            models.CheckConstraint(
                condition=Q(quantity_reserved__lte=F("quantity_on_hand")),
                name="stockitem_reserved_lte_on_hand",
            ),
        ]
```

```sql
ALTER TABLE inventory_stockitem
  ADD CONSTRAINT stockitem_product_warehouse_uniq UNIQUE (product_id, warehouse_id);
ALTER TABLE inventory_stockitem
  ADD CONSTRAINT stockitem_reserved_lte_on_hand CHECK (quantity_reserved <= quantity_on_hand);
```

A `UniqueConstraint` with `condition`, `expressions`, `include` or `opclasses` cannot be a
table constraint on PostgreSQL, so Django emits `CREATE UNIQUE INDEX ... WHERE ...` instead.
The effect on writes is identical; the difference matters only for `ON CONFLICT` targets and
deferrability (unique *indexes* cannot be deferred).

### How a B-tree index serves a query

A composite B-tree on `(customer_id, created_at DESC)` stores entries sorted by
`customer_id`, then `created_at` descending. It can serve:

- `WHERE customer_id = ?` (leftmost prefix),
- `WHERE customer_id = ? ORDER BY created_at DESC LIMIT 20` (no sort needed),
- `WHERE customer_id = ? AND created_at > ?` (range on second column).

It cannot efficiently serve `WHERE created_at > ?` alone (no leftmost column) — PostgreSQL
can use a B-tree skip scan from PostgreSQL 18 ([release notes](https://www.postgresql.org/docs/18/release-18.html)),
which helps mainly when the leading column has few distinct values; planners in general
will often seq-scan instead.

**Rule of thumb for column order:** equality predicates first, then the range or sort
column; among equality columns, the one used by the most queries first.

### Selectivity

An index is useful when it narrows the scan to a small fraction of the table. `is_active`
with 95 % `True` is low-selectivity — the planner ignores an index on it for `True`, but a
**partial index** on the rare value (`WHERE is_active = false`) or on the predicate you
actually query (`WHERE status = 'pending'`) is small and selective.

### Maintenance cost

Every index is updated on every INSERT, on DELETE (eventually, via vacuum on PostgreSQL),
and on UPDATE of an indexed column (on PostgreSQL, updating any indexed column also defeats
HOT updates). Indexes cost disk, cache memory, replication bandwidth and write latency.
Unused indexes are pure cost.

## 4. Engineering decisions and trade-offs

### What to enforce in the database

| Invariant | Enforce with |
|---|---|
| Natural uniqueness (`Customer.email`, `Product.sku`, `Warehouse.code`) | `unique=True` (case-insensitive: functional unique on `Lower("email")`) |
| Uniqueness within a parent (`OrderLine(order, product)`) | `UniqueConstraint(fields=[...])` |
| At most one active/primary/successful row | Conditional `UniqueConstraint(condition=Q(...))` |
| Ranges and cross-column rules | `CheckConstraint` |
| State/field consistency (`status != pending ⇒ placed_at not null`) | `CheckConstraint` with `Q` disjunction |
| Non-overlapping ranges (bookings) | `ExclusionConstraint` (PostgreSQL) |
| Rules spanning tables (order total = sum of lines) | Not a constraint — service + transaction; or trigger via `RunSQL` if critical |

Skip a DB constraint only when the rule genuinely changes often, depends on external state,
or is purely advisory.

### Choosing indexes from query patterns

Derive indexes from the queries, not from the model:

1. List the hot queries (selectors, list endpoints, background jobs) and their filters,
   orderings and limits — from code and from production query statistics
   (`pg_stat_statements`; see [query-optimization.md](./query-optimization.md)).
2. For each, design the index that satisfies `WHERE` equality columns, then range/order.
3. Merge overlapping candidates (an index on `(a, b)` makes `(a)` redundant).
4. Check `QuerySet.explain()` on production-like data volumes before and after.
5. Remove indexes that statistics show are never scanned.

| Index type | Django | Use when | Backend notes |
|---|---|---|---|
| B-tree (default) | `Index(fields=[...])` | Equality, ranges, sorting | All backends. |
| Composite | `Index(fields=["customer", "-created_at"])` | Multi-column filters + order | Leading `-` sets `DESC`. |
| Partial | `Index(..., condition=Q(...))` | Queries target a small subset | PostgreSQL, SQLite; silently ignored on MySQL/MariaDB; not supported on Oracle (emulate with a functional index over `Case`) — [indexes docs](https://docs.djangoproject.com/en/5.2/ref/models/indexes/#condition). |
| Functional | `Index(Lower("email"), name=...)` | Filter on an expression | PostgreSQL, SQLite, MySQL ≥ 8.0.13, Oracle; ignored on MariaDB. |
| Covering | `Index(..., include=["total"])` | Index-only scans for small extra columns | PostgreSQL only (ignored elsewhere). |
| Unique | `UniqueConstraint` (preferred) or `unique=True` | Enforce + look up | Prefer constraints over `Index` for uniqueness. |
| GIN / GiST / BRIN / Hash | `django.contrib.postgres.indexes.*` | JSONB/arrays/full-text, ranges, append-only time series | PostgreSQL only. |

### Deferrable constraints

`UniqueConstraint(..., deferrable=models.Deferrable.DEFERRED)` (PostgreSQL only; ignored
elsewhere, and incompatible with `condition`/`include`/`expressions` because those become
indexes) checks at commit. Use when a transaction temporarily violates uniqueness, e.g.
swapping `position` values of two rows. Costs: errors surface at commit (far from the
cause), and a deferred unique constraint cannot be an `ON CONFLICT` arbiter.

## 5. Django implementation patterns

### Orders and payments

```python
# apps/payments/models.py
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    provider_reference = models.CharField(max_length=128, unique=True, null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_gt_0"),
            # At most one successful payment per order — conditional unique (partial unique index)
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status="succeeded"),
                name="payment_one_success_per_order",
                violation_error_message="Order already has a successful payment.",
            ),
        ]
        indexes = [
            # Worker query: pending payments older than N minutes
            models.Index(
                fields=["created_at"],
                condition=Q(status="pending"),
                name="payment_pending_created_idx",
            ),
        ]
```

```sql
CREATE UNIQUE INDEX payment_one_success_per_order
  ON payments_payment (order_id) WHERE status = 'succeeded';
CREATE INDEX payment_pending_created_idx
  ON payments_payment (created_at) WHERE status = 'pending';
```

On MySQL, `condition` on `UniqueConstraint` and `Index` is not supported and Django skips
those (with a system-check warning). Do not rely on conditional constraints for correctness
on MySQL — redesign (e.g. a nullable `successful_for_order` column with a plain unique).

### Case-insensitive uniqueness

```python
# apps/customers/models.py
class Customer(models.Model):
    email = models.EmailField()
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="customer_email_ci_uniq"),
        ]
```

Lookups must use the same expression to hit the index:
`Customer.objects.filter(email__iexact=value)` compiles to `UPPER("email"::text) = UPPER(%s)` on
PostgreSQL, **not** `LOWER(...)` (Django source: `postgresql/operations.py` `lookup_cast` and
`postgresql/base.py` `operators`, 5.2), so it will not use this index — filter with
`.annotate(email_l=Lower("email")).filter(email_l=value.lower())` or simply store emails
normalised and use plain `unique=True`. Other backends compile `iexact` differently (e.g.
MySQL relies on a case-insensitive collation), so check `str(qs.query)` per backend. (On PostgreSQL, `CICharField` was deprecated in
Django 4.2 and removed in 5.1 except in historical migrations; the documented replacement is
`CharField(db_collation=...)` with a case-insensitive non-deterministic collation —
[5.1 notes](https://docs.djangoproject.com/en/5.2/releases/5.1/#features-removed-in-5-1).)

### Composite index for list queries

```python
class Order(models.Model):
    ...
    class Meta:
        indexes = [
            # selector: customer's orders, newest first, paginated
            models.Index(fields=["customer", "-created_at"], name="order_customer_recent_idx"),
            # ops dashboard: orders in a status, oldest first
            models.Index(fields=["status", "created_at"], name="order_status_created_idx"),
        ]
```

Here the auto-created index on `customer_id` is redundant with `order_customer_recent_idx`
(same leading column). Removing it (`db_index=False` on the FK) saves write cost; keep it if
other queries need a narrow index — measure.

### Covering index (PostgreSQL)

```python
models.Index(
    fields=["customer", "-created_at"],
    include=["status", "total"],
    name="order_customer_recent_cov_idx",
)
```

Enables an index-only scan for
`Order.objects.filter(customer=c).order_by("-created_at").values("status", "total")[:20]`,
provided the visibility map is current (vacuum). Larger index; only for very hot reads.

### Expression check constraints

```python
models.CheckConstraint(
    condition=Q(placed_at__isnull=True) | Q(placed_at__gte=F("created_at")),
    name="order_placed_after_created",
)
```

### Constraint names in abstract bases

```python
class Meta:
    abstract = True
    constraints = [
        models.CheckConstraint(
            condition=Q(updated_at__gte=F("created_at")),
            name="%(app_label)s_%(class)s_updated_after_created",
        ),
    ]
```

### Handling violations

```python
from django.db import IntegrityError, transaction

def record_success(*, payment: Payment) -> None:
    try:
        with transaction.atomic():
            Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.SUCCEEDED)
    except IntegrityError as exc:
        # payment_one_success_per_order fired — another payment already succeeded
        raise DuplicateSuccessfulPayment(payment.order_id) from exc
```

Map the constraint to a domain error; inspect the constraint name on PostgreSQL via
`exc.__cause__.diag.constraint_name` if you need to discriminate — Django re-raises the driver
error with `from`, so `__cause__` is the driver exception; `diag.constraint_name` exists on both
[psycopg 3](https://www.psycopg.org/psycopg3/docs/api/errors.html) and
[psycopg2 ≥ 2.5](https://www.psycopg.org/docs/extensions.html) and is `None` when the server
does not send it.

## 6. Production use cases

- **Inventory never negative / never over-reserved**: `PositiveIntegerField` + the
  `reserved <= on_hand` check makes over-reservation raise `IntegrityError` even under a race
  that slipped past application checks ([transactions-and-concurrency.md](./transactions-and-concurrency.md)).
- **Idempotent payment creation**: `unique=True` on `idempotency_key`; the second request's
  insert fails and the service returns the existing payment.
- **Pending-work queues**: partial index on `status='pending'` keeps worker polling cheap as
  the completed history grows to millions of rows.
- **Soft-delete with uniqueness**: `UniqueConstraint(fields=["sku"], condition=Q(deleted_at__isnull=True))`
  allows re-creating a SKU after soft deletion.
- **Bookings**: `ExclusionConstraint` with `RangeOperators.OVERLAPS` prevents double-booking
  on PostgreSQL.

## 7. Advantages and disadvantages

| Mechanism | Advantages | Disadvantages |
|---|---|---|
| DB constraints | Race-proof, path-independent, self-documenting | Migrations to change; generic error messages; backend support varies. |
| Python validation only | Friendly, flexible | Racy, bypassable by bulk/raw writes. |
| Partial index | Small, selective | Query predicate must match the index condition; not on MySQL. |
| Covering index | Index-only scans | Bigger index; PostgreSQL only. |
| Many indexes | Fast reads | Slower writes, more storage, vacuum and replication load. |
| Deferrable constraint | Allows transient violations | Late errors; no `ON CONFLICT`. |

## 8. Security and performance considerations

- Constraints are a defence-in-depth layer against logic bugs and malicious inputs that
  bypass validation (mass-assignment, admin, scripts).
- Unique constraints are the correct foundation for idempotency and de-duplication.
- `IntegrityError` messages contain table, constraint and sometimes values — do not echo
  them to clients ([django-core logging-exceptions](../../django-core/references/logging-exceptions.md)).
- Adding an index or a validated constraint locks or scans the table; on large tables follow
  [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md)
  (`AddIndexConcurrently`, `NOT VALID` + validate).
- Index bloat and unused indexes are found with PostgreSQL statistics views
  (`pg_stat_user_indexes.idx_scan`) — [postgresql-engineering](../../postgresql-engineering/SKILL.md).
- `CHECK` constraints are cheap to evaluate; unique constraints cost an index lookup per write.

## 9. Common mistakes and anti-patterns

- Relying on `validate_unique()`/serializer validators for uniqueness under concurrency.
- Indexing every column "just in case", or every FK twice.
- Composite index with the range column first (`(created_at, customer)`).
- Boolean/low-cardinality indexes instead of partial indexes.
- Partial index whose condition the query does not repeat (`status='pending'` index, query
  filters `status__in=[...]`) — the planner cannot use it.
- Functional index on `Lower(email)` while querying with `iexact` (different expression).
- `unique_together`/`index_together` in new code (legacy; `index_together` removed in 5.1 — [notes](https://docs.djangoproject.com/en/5.2/releases/5.1/#features-removed-in-5-1)).
- Conditional constraints relied upon on MySQL, where Django silently skips them.
- Unnamed or auto-named constraints that later collide or can't be referenced in migrations.
- Catching `IntegrityError` without a nested `atomic()` — breaks the outer transaction.

## 10. Troubleshooting and validation

| Symptom | Cause | Action |
|---|---|---|
| Index not used | Wrong column order, low selectivity, expression mismatch, stale statistics, small table | `qs.explain(analyze=True)` on realistic data; `ANALYZE`. |
| System-check warnings `models.W027` (check), `W036` (conditional unique), `W037` (conditional index), `W038` (deferrable unique), `W039`/`W040` (`include`), `W043`/`W044` (expressions), `W047` (`nulls_distinct`) | Backend lacks that feature — the constraint/index is not created | Redesign for that backend ([system checks](https://docs.djangoproject.com/en/5.2/ref/checks/#models)). |
| `IntegrityError` at commit, not at the statement | Deferred FK or deferrable constraint | Expected; check earlier or use `SET CONSTRAINTS ... IMMEDIATE` via raw SQL. |
| Migration adding constraint fails | Existing data violates it | Clean data in a prior data migration; or `NOT VALID` then validate. |
| Writes slowed after deploy | New indexes | Check `pg_stat_user_indexes`; drop unused. |

Validation workflow:

```python
qs = Order.objects.filter(customer_id=42).order_by("-created_at")[:20]
print(qs.explain(analyze=True))   # PostgreSQL; actually executes the query
# Expect: Index Scan using order_customer_recent_idx ... (no Sort node)
```

Tests: one test per constraint asserting `IntegrityError` inside `transaction.atomic()`;
run against the production backend, since SQLite and MySQL differ
([django-testing](../../django-testing/SKILL.md)). Use `sqlmigrate` to review DDL.

## 11. Related references and skills

- [model-design.md](./model-design.md) — validation layering, Meta.
- [field-types-and-options.md](./field-types-and-options.md) — `unique`, `db_index`, `null`.
- [model-relationships.md](./model-relationships.md) — FK constraints vs `on_delete`.
- [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md) — building indexes/constraints safely.
- [query-optimization.md](./query-optimization.md) — `explain()`, plan reading in Django.
- [transactions-and-concurrency.md](./transactions-and-concurrency.md) — constraints as concurrency control.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — index types, bloat, statistics.

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/ref/models/constraints/
- https://docs.djangoproject.com/en/stable/ref/models/indexes/
- https://docs.djangoproject.com/en/stable/ref/contrib/postgres/constraints/
- https://docs.djangoproject.com/en/stable/ref/contrib/postgres/indexes/
- https://docs.djangoproject.com/en/stable/ref/databases/
- https://www.postgresql.org/docs/current/indexes.html
- https://www.postgresql.org/docs/current/ddl-constraints.html
