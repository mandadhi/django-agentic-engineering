# Model Design

> Scope: how to design Django models as the persistence representation of a domain — model
> responsibilities, inheritance (abstract, multi-table, proxy), `Meta` options, primary-key
> strategy (auto, custom, UUID, composite), model validation (`clean()`, `full_clean()`,
> `save()`), the ORM-relevant parts of the model lifecycle, model methods and reusable model
> design.
> Not covered here: individual field types and options ([field-types-and-options.md](./field-types-and-options.md)),
> relationship semantics and `on_delete` ([model-relationships.md](./model-relationships.md)),
> constraints and indexes ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md)),
> custom managers ([managers-and-querysets.md](./managers-and-querysets.md)), the general
> `save()`/`delete()`/`AppConfig.ready()` lifecycle ([django-core models-application-lifecycle](../../django-core/references/models-application-lifecycle.md)),
> where business logic lives ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).

## 1. Purpose and scope

A model is three things at once: a Python class with behaviour, a table definition the
migration autodetector diffs against, and the metadata (`_meta`) the ORM compiles queries
from. Good model design keeps those aligned — the table enforces what must always be true,
the class exposes behaviour that depends only on the instance, and everything that spans
models, needs a transaction or talks to the outside world lives in a service.

This reference helps an agent decide: which inheritance style (if any), which primary key,
which `Meta` options, what goes in `clean()` versus a database constraint, and what does
*not* belong on the model.

Baseline: Django 5.2 LTS. SQL in this skill is approximately what Django emits on
PostgreSQL; other backends differ in quoting and types. Examples are illustrative and were
not executed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Concrete model | Has a table. Every non-abstract, non-proxy `models.Model` subclass. |
| Abstract base model | `Meta.abstract = True`; no table; fields are copied into each child's table. |
| Multi-table inheritance (MTI) | Concrete parent and concrete child; child table has an implicit `OneToOneField` (`<parent>_ptr`) to the parent. |
| Proxy model | `Meta.proxy = True`; same table as parent, different Python behaviour/manager/ordering. |
| `_meta` (`Options`) | Model metadata: fields, relations, constraints, indexes, `db_table`, `pk`. |
| Model validation | `full_clean()` = `clean_fields()` + `clean()` + `validate_unique()` + `validate_constraints()` (Django ≥ 4.1). Never called by `save()`. |
| Model state | `instance._state.adding` (not yet saved) and `instance._state.db` (alias it came from). |

Model **responsibilities** (keep): field definitions, invariants expressible as constraints,
`__str__`, `get_absolute_url`, small derived properties (`order.is_cancellable`), state checks
that read only the instance. Model **non-responsibilities** (move out): multi-model
workflows, payment/email/HTTP calls, permission checks, request awareness, cross-app writes.

## 3. Internal mechanics

### Class creation

`ModelBase.__new__` builds `_meta`, calls `contribute_to_class()` on every field (which
creates descriptors such as `ForeignKeyDeferredAttribute` and reverse accessors), adds a
default manager `objects` if none is declared, and registers the model with the app
registry. Relations given as strings (`"orders.Order"`) are resolved lazily once both models
are registered — this is what allows cross-app references without circular imports.

### Inheritance at the table level

```python
# Abstract: one table per child, fields duplicated
class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True

class Product(TimeStampedModel):          # catalog_product has created_at, updated_at
    ...
```

```python
# MTI: two tables, joined on every query
class Payment(models.Model):              # payments_payment
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)

class CardPayment(Payment):               # payments_cardpayment(payment_ptr_id PK/FK, last4)
    last4 = models.CharField(max_length=4)
```

`CardPayment.objects.all()` compiles to approximately:

```sql
SELECT p.id, p.order_id, p.amount, c.payment_ptr_id, c.last4
FROM payments_cardpayment c
INNER JOIN payments_payment p ON c.payment_ptr_id = p.id;
```

Saving a `CardPayment` writes two rows (two INSERTs/UPDATEs); deleting cascades through the
parent link. Querying the parent (`Payment.objects.all()`) returns `Payment` instances;
reaching the child requires `payment.cardpayment` (one more query, `DoesNotExist` if it is a
different subtype).

### Proxy

A proxy shares the parent's table and `_meta.db_table`; migrations record it but create no
table. It may change `ordering`, default manager and methods, but not fields.

### Save path (ORM view)

`save()` → `pre_save` signal → each field's `pre_save()` (e.g. `auto_now` sets a timestamp)
→ `get_db_prep_save()` → if the pk is set, try an UPDATE (falling back to INSERT when it
matches zero rows); if the pk is unset, INSERT → `post_save`. Exception (Django ≥ 3.0): when
the instance is new (`_state.adding=True`) and the pk field has a `default` (e.g.
`uuid.uuid4`), Django skips the UPDATE attempt and always issues a single INSERT, as if
`force_insert=True` — so reusing an existing pk on a new instance raises `IntegrityError`
rather than updating the row ([3.0 release notes](https://docs.djangoproject.com/en/5.2/releases/3.0/#model-save-when-providing-a-default-for-the-primary-key)).
A pk assigned explicitly on a field *without* a default still takes the UPDATE-then-INSERT path.

The general lifecycle (signals, `delete()`, `refresh_from_db()`) is owned by
[django-core models-application-lifecycle](../../django-core/references/models-application-lifecycle.md).

## 4. Engineering decisions and trade-offs

### Inheritance or composition?

| Need | Choose | Why |
|---|---|---|
| Same columns on many tables (timestamps, audit fields, tenant FK) | Abstract base | No joins, no shared table, simple. |
| A real "is-a" with shared identity queried polymorphically, few subtypes, subtype-specific columns | MTI — *rarely* | Shared pk and FK targets; but every child query joins and every save writes two tables. |
| Same rows, different behaviour or default filtering (e.g. `PaidOrder`) | Proxy | Zero schema cost. |
| Optional extra data about an entity (profile, shipping details) | `OneToOneField` composition | Explicit, loaded only when needed, no implicit join. |
| Variable subtypes with few distinct fields | Single table + type column (+ `JSONField` for the rare extras) + constraints | One table, one query, CHECK constraints per type. |

Composition beats MTI in most production systems: the join is explicit, you choose when to
load it, and you can change it without rewriting parent rows. Use MTI only when other tables
must reference "any payment" by one FK *and* subtypes have meaningfully different columns.

### Primary keys

| Option | Use when | Cost |
|---|---|---|
| `BigAutoField` (set `DEFAULT_AUTO_FIELD` / `AppConfig.default_auto_field`) | Default for nearly everything | Enumerable ids — never rely on obscurity; authorise. |
| `UUIDField(primary_key=True, default=uuid.uuid4)` | Ids generated client-side/offline, merged across databases, exposed publicly | 16 bytes (PostgreSQL native `uuid`; `char(32)` on MySQL/SQLite/Oracle); random v4 keys scatter B-tree inserts → index bloat and cache misses on large tables. |
| Bigint pk + separate `public_id = UUIDField(unique=True)` | Public identifier needed, but hot-path joins should stay narrow | One extra unique index. Usually the best of both. |
| Natural key as pk (`sku`) | Practically never | Natural keys change; every FK column must be rewritten. Prefer surrogate pk + `unique=True`. |
| `CompositePrimaryKey` (Django ≥ 5.2) | Mapping legacy tables, or join tables whose identity *is* the pair | Limited support in 5.2: `ForeignKey`/`GenericForeignKey` cannot target a composite-pk model, it cannot be registered in the admin, migrating to/from a composite pk (or changing its fields) is unsupported, and most aggregate functions reject `pk` ([docs](https://docs.djangoproject.com/en/5.2/topics/composite-primary-key/)). |

Time-ordered UUIDs (v7) reduce insert scatter; Python's stdlib gained `uuid.uuid7()` in
Python 3.14 ([docs](https://docs.python.org/3.14/library/uuid.html#uuid.uuid7)); on earlier Python use a library. Changing pk type on an existing large table
is an expensive migration — decide early.

### Validation layering

| Layer | Runs when | Guarantees |
|---|---|---|
| Form/serializer validation | Input arrives via that form/serializer | Friendly errors; bypassed by scripts, admin actions, bulk ops. |
| `Model.full_clean()` | Only when explicitly called (ModelForm calls it) | Field + cross-field + unique + constraint validation in Python. |
| Database constraint | Every write, from every client | The only guarantee under concurrency and raw SQL. |

Rule: invariants go in the database ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md));
`clean()` mirrors them for user-friendly messages and adds rules the database cannot express
cheaply. Since Django 4.1, `validate_constraints()` checks `Meta.constraints` in Python, so
one declaration serves both layers.

### Should `save()` call `full_clean()`?

Usually no. It doubles queries for unique checks, runs on every save including internal
ones, and still does not prevent races. Call `full_clean()` in the service/entry point that
accepts external input; rely on constraints for the guarantee.

## 5. Django implementation patterns

### Canonical model with Meta options

```python
# apps/orders/models.py
import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"
        CANCELLED = "cancelled", "Cancelled"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="orders"
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(db_default=Now())      # Django ≥ 5.0
    placed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "orders_order"            # only when you must match an existing table
        ordering = []                         # avoid implicit ORDER BY on every query
        get_latest_by = "created_at"
        verbose_name = "order"
        constraints = [
            models.CheckConstraint(condition=Q(total__gte=0), name="order_total_gte_0"),
            models.CheckConstraint(
                condition=Q(status="pending") | Q(placed_at__isnull=False),
                name="order_non_pending_has_placed_at",
            ),
        ]
        indexes = [models.Index(fields=["customer", "-created_at"], name="order_customer_recent_idx")]

    def __str__(self) -> str:
        return f"Order {self.public_id}"

    @property
    def is_cancellable(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.PAID}
```

`choices=Status` (passing the enum class directly) is Django ≥ 5.0; earlier versions need
`choices=Status.choices` ([5.0 notes](https://docs.djangoproject.com/en/5.2/releases/5.0/#more-options-for-declaring-field-choices)).

### Meta options that matter in production

| Option | Production note |
|---|---|
| `ordering` | Adds `ORDER BY` to *every* query without explicit ordering, including those feeding `Subquery`/`exists`. Prefer explicit `order_by()` at the call site; leave `ordering` unset on large tables. Since Django 3.1, `Meta.ordering` is ignored in GROUP BY queries. |
| `db_table` / `db_table_comment` (Django ≥ 4.2) | Rename only with a migration plan; comments are useful for DBAs. |
| `constraints`, `indexes` | See [database-constraints-and-indexes.md](./database-constraints-and-indexes.md). `unique_together`/`index_together` are legacy (`index_together` removed in 5.1). |
| `managed = False` | Table owned elsewhere (views, legacy). Migrations create no DDL; you own the schema. |
| `default_related_name` | Consistent reverse names in apps with many FKs. |
| `select_on_save` | Restores pre-1.6 "SELECT then UPDATE/INSERT"; only for odd backends/triggers. |
| `base_manager_name` / `default_manager_name` | See [managers-and-querysets.md](./managers-and-querysets.md). |
| `required_db_features` / `required_db_vendor` | Skip a model on backends that cannot support it. |

### Model validation done properly

```python
from django.core.exceptions import ValidationError

class StockItem(models.Model):
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="stock_items")
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.PROTECT, related_name="stock_items")
    quantity_on_hand = models.PositiveIntegerField(default=0)
    quantity_reserved = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "warehouse"], name="stockitem_product_warehouse_uniq"),
            models.CheckConstraint(
                condition=Q(quantity_reserved__lte=models.F("quantity_on_hand")),
                name="stockitem_reserved_lte_on_hand",
                violation_error_message="Cannot reserve more than is on hand.",
            ),
        ]

    def clean(self):
        # Rules the DB cannot cheaply express, or friendlier messages. Read-only, no I/O.
        if self.product_id and not self.product.is_active:
            raise ValidationError({"product": "Inactive products cannot be stocked."})
```

```python
# apps/inventory/services.py — validate at the boundary, rely on the DB for the guarantee
def create_stock_item(*, product, warehouse, quantity: int) -> StockItem:
    item = StockItem(product=product, warehouse=warehouse, quantity_on_hand=quantity)
    item.full_clean()          # field, clean(), unique and constraint checks → ValidationError
    item.save()                # may still raise IntegrityError under a concurrent insert
    return item
```

`full_clean(exclude=[...], validate_unique=..., validate_constraints=...)` lets callers skip
checks they cannot satisfy yet (e.g. FK not assigned).

### Saving narrowly

```python
order.status = Order.Status.CANCELLED
order.save(update_fields=["status"])      # UPDATE ... SET status = %s WHERE id = %s
```

`update_fields` avoids overwriting concurrent changes to other columns and skips work, but
note `auto_now` fields update only if listed. For concurrency-safe counters use `F()`
([advanced-query-expressions.md](./advanced-query-expressions.md)).

### Reusable abstract models

```python
# apps/core/models.py
class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(db_default=Now(), editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantScopedModel(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True
```

Children inherit `Meta` only if they subclass it explicitly:
`class Meta(TimeStampedModel.Meta): ...`. Constraint and index `name`s inside abstract
bases must include `%(app_label)s` and `%(class)s` placeholders to stay unique.

### Proxy for behaviour, not data

```python
class PaidOrderManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(status=Order.Status.PAID)

class PaidOrder(Order):
    objects = PaidOrderManager()
    class Meta:
        proxy = True
```

Useful for a separate admin view of a subset; do not use proxies to model business state
transitions.

### GeneratedField (Django ≥ 5.0)

```python
from django.db.models import F

class OrderLine(models.Model):
    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="order_lines")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.GeneratedField(
        expression=F("quantity") * F("unit_price"),
        output_field=models.DecimalField(max_digits=12, decimal_places=2),
        db_persist=True,     # STORED; Django 5.2 docs: PostgreSQL only persisted, Oracle only virtual
    )
```

The value is computed by the database. On Django 5.x it is available on the instance after
`save()` only after `refresh_from_db()` ([5.2 docs](https://docs.djangoproject.com/en/5.2/ref/models/fields/#generatedfield)).
Django ≥ 6.0 refreshes `GeneratedField`s after `save()` via `RETURNING` on SQLite, PostgreSQL
and Oracle, and marks them deferred (reloaded on next access) on MySQL/MariaDB
([6.0 release notes](https://docs.djangoproject.com/en/6.0/releases/6.0/)). PostgreSQL itself
gained virtual generated columns in PostgreSQL 18
([PG docs](https://www.postgresql.org/docs/18/ddl-generated-columns.html)), but Django 5.2's
documentation still lists PostgreSQL as persisted-only, so keep `db_persist=True` there.

## 6. Production use cases

- **Order with immutable public id**: bigint pk for joins, `public_id` UUID in URLs and APIs.
- **Shared audit columns** across 40 tables: one abstract `TimeStampedModel`; no MTI.
- **Payment subtypes** (card, bank transfer, wallet): single `Payment` table with `method`
  choice + `CheckConstraint`s per method + a `details` `JSONField` for provider extras —
  rather than three MTI child tables joined on every query.
- **Legacy reporting table**: `managed = False`, explicit `db_table`, read-only usage.
- **Admin-only subset**: `PaidOrder` proxy registered separately in the admin.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Abstract base | No runtime cost; DRY | Changes ripple to many migrations; cannot query "all subclasses". |
| MTI | Polymorphic FK target; normalised | Join on every child query; two writes per save; downcasting costs queries; hard to undo. |
| Proxy | Free; separate admin/manager | Cannot add fields; easy to confuse with real subtypes. |
| UUID pk | Unguessable, mergeable | Larger indexes/FKs; random insert locality. |
| `full_clean()` in services | Good errors | Extra queries; still race-prone without constraints. |

## 8. Security and performance considerations

- Sequential ids are not a security boundary; enforce authorisation in scoped queries
  ([django-security-engineering](../../django-security-engineering/SKILL.md)). Use a UUID `public_id` to avoid
  leaking volume, not to replace authorisation.
- `editable=False` hides fields from ModelForms/admin but is not access control.
- `Meta.ordering` on large tables silently forces sorts — measure with `explain()`
  ([query-optimization.md](./query-optimization.md)).
- MTI turns every list into a join and every save into two statements; avoid on hot tables.
- Wide models (dozens of columns, large text/JSON) make every `SELECT *` heavier; split
  rarely-used bulky data into a `OneToOneField` side table or use `only()`/`defer()`.
- `save()` without `update_fields` writes every column — a lost-update risk under
  concurrency ([transactions-and-concurrency.md](./transactions-and-concurrency.md)).

## 9. Common mistakes and anti-patterns

- Business workflows in `save()` overrides (sending emails, charging cards). They run on
  every save, inside whatever transaction is open, and are bypassed by `update()`/bulk ops.
- Assuming `save()` validates. It does not call `full_clean()`.
- Validation only in `clean()` with no matching DB constraint.
- MTI chosen for code reuse that an abstract base would provide.
- Natural keys (`email`, `sku`) as primary keys.
- Global `Meta.ordering` on high-volume tables.
- Abstract-base constraint names without `%(app_label)s_%(class)s` → name clashes.
- Fat "god models" with hundreds of lines of cross-model logic — move to services
  ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).
- Repositories wrapping every model "for testability" — the ORM is the data-access layer.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Extra JOIN in every query on a model | MTI parent | `print(qs.query)`; look for `_ptr_id`. |
| `IntegrityError` on save of new object with explicit pk | Duplicate pk; Django inserted directly | Expected since 3.0 — use `update_or_create` or fetch first. |
| `ValidationError` never raised | `full_clean()` not called | Call it at the boundary. |
| Unexpected `ORDER BY` in subqueries/plans | `Meta.ordering` | `qs.query`; add `.order_by()` to clear. |
| `models.E0xx`/`fields.E3xx` system check errors | Clashing reverse accessors, bad Meta | `python manage.py check`. |
| Constraint name collision across children | Abstract base without placeholders | Use `%(app_label)s_%(class)s_...`. |

Validate: `python manage.py check`; `makemigrations --check --dry-run`;
`sqlmigrate <app> <migration>` to read the DDL; a test per constraint asserting
`IntegrityError` ([django-testing](../../django-testing/SKILL.md)).

## 11. Related references and skills

- [field-types-and-options.md](./field-types-and-options.md) — choosing fields.
- [model-relationships.md](./model-relationships.md) — FKs, `on_delete`, through models.
- [database-constraints-and-indexes.md](./database-constraints-and-indexes.md) — enforcing invariants.
- [managers-and-querysets.md](./managers-and-querysets.md) — managers on proxies and bases.
- [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md) — changing pks, splitting models.
- [django-core models-application-lifecycle](../../django-core/references/models-application-lifecycle.md) — save/delete lifecycle, `ready()`.
- [django-architecture domain-driven-design](../../django-architecture/references/domain-driven-design.md) — entities and aggregates.
- [django-architecture service-layer](../../django-architecture/references/service-layer.md) — where workflows go.

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/topics/db/models/
- https://docs.djangoproject.com/en/stable/ref/models/options/
- https://docs.djangoproject.com/en/stable/ref/models/instances/
- https://docs.djangoproject.com/en/stable/topics/db/models/#model-inheritance
- https://docs.djangoproject.com/en/stable/ref/models/fields/#generatedfield
- https://docs.djangoproject.com/en/stable/topics/composite-primary-key/
- https://docs.djangoproject.com/en/stable/releases/5.2/
