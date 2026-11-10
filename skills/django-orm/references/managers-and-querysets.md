# Managers and QuerySets

> Scope: default and base managers, custom managers, custom QuerySet classes, `as_manager()` and
> `Manager.from_queryset()`, manager inheritance, what belongs on a manager or QuerySet,
> composing reusable query logic, soft-delete and tenant-scoping considerations, and manager
> anti-patterns.
> Not covered here: QuerySet method semantics ([queryset-api.md](./queryset-api.md)); evaluation
> and cloning ([queryset-internals.md](./queryset-internals.md)); where business logic belongs
> (services vs selectors) ([django-architecture service-layer](../../django-architecture/references/service-layer.md));
> manager basics in the model lifecycle ([django-core models-application-lifecycle](../../django-core/references/models-application-lifecycle.md));
> authorisation design ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 1. Purpose and scope

Custom QuerySets give a project a shared, composable query vocabulary (`Order.objects.paid().for_customer(c)`).
Custom managers change what a model's entry points return. Both are easy to overuse. This
reference tells an agent when they're justified, how to build them so they compose, and the
traps — especially around default/base managers, soft deletion and tenant filtering, where a
mistake silently hides or leaks data. Baseline Django 5.2.

## 2. Core concepts

| Term | Meaning |
|---|---|
| **Manager** | `models.Manager` instance attached to the model class; `get_queryset()` is the root of every query through it |
| **Default manager** | `Model._default_manager`: first manager defined (or `Meta.default_manager_name`). Used by admin, generic views, `dumpdata`, `ModelChoiceField`, related managers' class, and third-party code |
| **Base manager** | `Model._base_manager`: plain `Manager` (or `Meta.base_manager_name`). Used for related-object access (`line.order`) and in `save()`/`refresh_from_db` internals — must not filter rows out |
| **Custom QuerySet** | `models.QuerySet` subclass with chainable methods |
| **`QuerySet.as_manager()`** | Builds a manager exposing the QuerySet's public methods |
| **`Manager.from_queryset(QS)`** | Builds a manager class combining custom manager methods/`get_queryset()` with QuerySet methods |

## 3. Internal mechanics

- `Order.objects.paid()` with `objects = OrderQuerySet.as_manager()`: the generated manager class
  copies public QuerySet methods as proxies that call `self.get_queryset().paid()`. Names starting
  with `_` are not copied unless the method sets `queryset_only = False`; a public method with
  `queryset_only = True` stays QuerySet-only (Django's own `delete()` uses this).
- Related managers (`customer.orders`) are built dynamically by subclassing the **default
  manager's class** of the related model (`Order._default_manager.__class__`), so custom QuerySet
  methods are available: `customer.orders.paid()`.
- Forward relation access (`order_line.order`) uses `Order._base_manager`, so a default manager
  that filters out soft-deleted rows does **not** hide them through FK access — intentionally, to
  avoid `DoesNotExist` on existing FKs.
- Manager inheritance: managers on abstract bases are inherited; with multi-table inheritance,
  managers from concrete parents are inherited too (Django ≥ 1.10 semantics). The default manager
  is the first one found by MRO unless `Meta.default_manager_name` sets it.
- Managers are only on the class: `order.objects` raises `AttributeError` — managers separate
  table-level operations from row-level operations.
- `Manager` subclasses must be importable for migrations if `use_in_migrations = True`; then
  migrations serialise the manager so `RunPython` code can call its methods.

## 4. Engineering decisions and trade-offs

### When a custom QuerySet is justified

- The same filter/annotation combination appears in **two or more** call sites, or encodes a
  domain rule that must be applied consistently (`active()`, `overdue()`, `visible_to(user)`).
- The logic is purely query-building: returns a QuerySet, takes simple arguments, no side effects.

### When a custom manager (beyond `as_manager()`) is justified

- Changing `get_queryset()` for a **secondary** manager (e.g. `Product.active` returning only
  active products) while `objects` stays unfiltered.
- Table-level creation helpers that encapsulate invariant-preserving construction
  (`create_user`-style) — though a service function is usually clearer for anything with
  side effects or multiple models.

### When neither is justified

- One-off queries used in a single selector — keep them in the selector.
- Business workflows (place order, refund payment) — these are services: they span models, need
  transactions and side effects ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).
- Wrapping every `filter()` in a named method "for consistency", or creating a repository class
  over the manager. The QuerySet API *is* the data-access layer.

### Filtering default managers: almost never

Replacing `objects` with a filtered manager (soft delete, tenant) affects admin, `dumpdata`,
uniqueness validation, generic views, third-party apps and reverse relations in ways that are hard
to audit. Prefer an unfiltered `objects` plus explicit, named scopes — or, for tenants, a
mandatory scoped entry point enforced by review/linting.

| Approach | Safety against forgetting the filter | Surprise factor |
|---|---|---|
| Unfiltered `objects` + `.active()` / `.for_tenant(t)` | Low (callers must remember) | Low |
| Secondary filtered manager (`Product.available`) | Medium (named, visible) | Low |
| Filtered default manager | High for direct queries | High (admin, dumps, `validate_unique`, related lookups) |
| Database-level row security (PostgreSQL RLS) | Highest | Operational complexity ([postgresql-engineering](../../postgresql-engineering/SKILL.md)) |

## 5. Django implementation patterns

### Composable QuerySet exposed with `as_manager()`

```python
# apps/orders/models.py
from django.db import models
from django.db.models import Exists, OuterRef, Q, Sum, F

class OrderQuerySet(models.QuerySet):
    def for_customer(self, customer):
        return self.filter(customer=customer)

    def paid(self):
        return self.filter(status=Order.Status.PAID)

    def open(self):
        return self.exclude(status__in=[Order.Status.SHIPPED, Order.Status.CANCELLED])

    def placed_between(self, start, end):
        return self.filter(placed_at__gte=start, placed_at__lt=end)

    def with_line_totals(self):
        return self.annotate(
            lines_total=Sum(F("lines__quantity") * F("lines__unit_price"))
        )

    def with_unpaid_balance(self):
        from apps.payments.models import Payment
        return self.annotate(
            has_settled_payment=Exists(
                Payment.objects.filter(order=OuterRef("pk"), status="settled")
            )
        ).filter(has_settled_payment=False)


class Order(models.Model):
    ...
    objects = OrderQuerySet.as_manager()
```

Usage composes in any order and on related managers:

```python
Order.objects.paid().placed_between(start, end).with_line_totals()
customer.orders.open().order_by("-created_at")
```

Rules: each method returns `self.filter(...)`/`self.annotate(...)` (a QuerySet), never a list,
never evaluates, never writes. Keep annotations that join multi-valued relations in separate
methods and document fan-out risk when combined ([aggregation-and-annotation.md](./aggregation-and-annotation.md)).

### Manager with custom `get_queryset()` plus QuerySet methods

```python
# apps/catalog/models.py
class ProductQuerySet(models.QuerySet):
    def in_category(self, category):
        return self.filter(category=category)

    def in_stock(self):
        from apps.inventory.models import StockItem
        return self.filter(Exists(
            StockItem.objects.filter(
                product=OuterRef("pk"),
                quantity_on_hand__gt=F("quantity_reserved"),
            )
        ))

class AvailableProductManager(models.Manager.from_queryset(ProductQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)

class Product(models.Model):
    ...
    objects = ProductQuerySet.as_manager()        # first → default manager, unfiltered
    available = AvailableProductManager()         # explicit, visible scope

# Product.available.in_category(c).in_stock()
```

Declaring `objects` first keeps it the default manager. Make it explicit with
`Meta.default_manager_name = "objects"` when a model has several managers.

### Tenant-aware filtering

```python
class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)

# apps/orders/selectors.py — the single entry point used by views and APIs
def order_list(*, tenant, user):
    return Order.objects.for_tenant(tenant).filter(...).select_related("customer")
```

Enforce the scope where data enters the request: selectors take `tenant` as a required
keyword argument; tests assert cross-tenant access returns nothing. Thread-local "current tenant"
injected into a default manager is tempting but breaks in async code, management commands,
Celery tasks and tests, and hides the filter from readers — document it as a deliberate
architectural decision if adopted. Authorisation design → [django-security-engineering](../../django-security-engineering/SKILL.md).

### Soft deletion

```python
from django.db.models.functions import Now

class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def soft_delete(self):
        return self.update(deleted_at=Now())

class Customer(models.Model):
    ...
    deleted_at = models.DateTimeField(null=True, blank=True)
    objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["email"], condition=Q(deleted_at__isnull=True),
                name="customer_email_unique_alive",
            ),
        ]
        indexes = [models.Index(fields=["email"], condition=Q(deleted_at__isnull=True),
                                name="customer_email_alive_idx")]
```

(This replaces `email unique=True` on the canonical `Customer` — a plain unique constraint would
block re-registering a soft-deleted email.)

Considerations an agent must raise before adding soft delete:

- `on_delete` cascades don't run for soft delete; related rows stay "alive" unless handled.
- Unique constraints must become conditional (above) — partial unique constraints are
  PostgreSQL/SQLite; not MySQL ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md)).
- Every query, aggregate and report must remember `.alive()` — or the default manager filters it,
  with the surprises listed in §4.
- Privacy law may require real deletion; soft delete is not erasure.
- Often an archive table or status field (`is_active`) is the simpler design.

### Making managers available in migrations

```python
class OrderManager(models.Manager.from_queryset(OrderQuerySet)):
    use_in_migrations = True
```

Only for logic stable enough to freeze in historical migrations; otherwise write the filter
inline in `RunPython` ([migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md)).

## 6. Production use cases

- **Order dashboard**: `Order.objects.for_customer(c).open().with_line_totals()` reused by HTML
  view, DRF viewset and CSV export — one definition of "open".
- **Storefront**: `Product.available` used by public catalogue; admin uses `objects` and can still
  see inactive products.
- **Multi-tenant SaaS**: every selector requires `tenant`; a test helper iterates all selectors
  with two tenants to prove isolation.
- **GDPR-sensitive customers**: soft delete for 30-day recovery, followed by a scheduled hard
  delete job.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| One definition of domain filters; composable chains | Indirection — readers must open the QuerySet to know the SQL |
| Available on related managers automatically | Filtered default managers create invisible behaviour |
| Testable in isolation (`assertQuerySetEqual`) | Methods that annotate across relations can combine into wrong aggregates |
| Keeps selectors short | Grows into a dumping ground if business logic creeps in |

## 8. Security and performance considerations

- A forgotten tenant/ownership filter is a data leak. Required-keyword selectors and tests are the
  baseline; a filtered default manager only covers queries that go through it (not raw SQL, not
  `_base_manager`, not other models' joins).
- `_base_manager` deliberately ignores filters — soft-deleted or other-tenant rows remain reachable
  through FK traversal. Don't rely on managers for authorisation of related objects.
- QuerySet methods that annotate with subqueries or joins add cost to every caller that chains
  them; keep expensive annotations opt-in (`with_line_totals()`), not inside `get_queryset()`.
- Indexes should match the filters your QuerySet methods encode (e.g. partial index on
  `deleted_at IS NULL`) — [database-constraints-and-indexes.md](./database-constraints-and-indexes.md).

## 9. Common mistakes and anti-patterns

- **Managers as service layer**: `Order.objects.place(cart, payment)` doing writes, emails and
  payment calls. Move to `apps/orders/services.py`.
- **QuerySet methods returning lists** or evaluating (`return list(self.filter(...))`) — breaks
  chaining and pagination.
- **Filtered default manager** for soft delete/tenant without auditing admin, `dumpdata`,
  `validate_unique`, generic views and reverse relations.
- **Overriding `get_queryset()` on the first manager** with `select_related`/annotations — every
  query in the system pays for them, including `exists()` and `count()` subqueries.
- **Duplicating methods** on both manager and QuerySet instead of `as_manager()`/`from_queryset()`.
- **One-method QuerySets** used in one place — premature abstraction.
- **Repository classes wrapping managers** — add a seam only for real alternate implementations
  ([django-architecture design-patterns](../../django-architecture/references/design-patterns.md)).
- **Thread-local tenant in `get_queryset()`** that silently returns all rows when unset.

## 10. Troubleshooting and validation

| Symptom | Check |
|---|---|
| Admin shows fewer rows than DB | Default manager filters; set `Meta.default_manager_name` or admin `get_queryset` |
| `DoesNotExist` on FK access to soft-deleted parent | Someone set `base_manager_name` to a filtered manager — revert |
| `AttributeError: 'Manager' object has no attribute 'paid'` | Manager not built from the QuerySet (`as_manager`/`from_queryset`) |
| Related manager lacks custom methods | Custom manager isn't the default manager of the related model |
| Migration fails calling manager method | Manager missing `use_in_migrations = True`, or use plain filters in `RunPython` |
| Unique violation re-creating a soft-deleted record | Constraint not conditional on `deleted_at IS NULL` |

Test QuerySet methods directly against the database:

```python
def test_open_excludes_shipped_and_cancelled(self):
    o1 = OrderFactory(status=Order.Status.PENDING)
    OrderFactory(status=Order.Status.SHIPPED)
    self.assertQuerySetEqual(Order.objects.open(), [o1])
```

Methodology → [django-testing](../../django-testing/SKILL.md).

## 11. Related references and skills

- [queryset-api.md](./queryset-api.md), [queryset-internals.md](./queryset-internals.md).
- [advanced-query-expressions.md](./advanced-query-expressions.md) — `Exists`/`Subquery` in methods.
- [database-constraints-and-indexes.md](./database-constraints-and-indexes.md) — conditional constraints for soft delete.
- [orm-anti-patterns.md](./orm-anti-patterns.md) — overcomplicated managers.
- [django-architecture service-layer](../../django-architecture/references/service-layer.md) — services vs selectors.
- [django-core models-application-lifecycle](../../django-core/references/models-application-lifecycle.md).

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/topics/db/managers/
- https://docs.djangoproject.com/en/stable/topics/db/managers/#calling-custom-queryset-methods-from-the-manager
- https://docs.djangoproject.com/en/stable/topics/db/managers/#custom-managers-and-model-inheritance
- https://docs.djangoproject.com/en/stable/ref/models/options/#default-manager-name
- https://docs.djangoproject.com/en/stable/ref/models/options/#base-manager-name
- https://www.postgresql.org/docs/current/ddl-rowsecurity.html
