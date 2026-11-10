# Models and application lifecycle

> Scope: model definition, fields, inheritance, basic managers, `Meta`, validation, `save()`/`delete()` semantics, `AppConfig`, the app registry and startup. Not covered here: querysets, indexes, migrations and query optimisation ([django-orm](../../django-orm/SKILL.md)); signal mechanics ([signals-management-commands.md](./signals-management-commands.md)); form validation ([forms-validation.md](./forms-validation.md)); app boundaries ([project-structure.md](./project-structure.md), [django-architecture](../../django-architecture/SKILL.md)).

## Purpose and overview

A model is a Python class that describes one database table *and* the domain rules that are cheap to express next to the data: field types, nullability, choices, constraints, invariants checked in `clean()`. An `AppConfig` describes the Django application that owns those models and is the only sanctioned place for startup wiring.

Two things trip agents up most often:

1. **Model validation is not automatic.** `save()` does not call `full_clean()`. Forms and DRF serializers do; your services and commands must decide explicitly.
2. **Startup happens in phases.** Code that runs at import time cannot use models or the database, and `ready()` is not a general "on boot" hook.

Check the target version first (`python -m django --version`); this file assumes Django 5.2 LTS and notes 6.0 differences.

## How it works internally

**Model class creation.** `ModelBase` (the metaclass) runs when `models.py` is imported. It builds `_meta` (an `Options` instance), calls `contribute_to_class()` on each field (fields install descriptors and register with `_meta`), adds the default manager, sets up reverse relations lazily, and registers the class in the app registry (`apps.register_model`). This is why importing a model before its app is registered raises `AppRegistryNotReady`.

**Instance creation.** `Model.__init__` assigns field values positionally or by keyword; `Model.from_db()` is used when loading rows, which sets `instance._state.adding = False` and `_state.db`. `_state.adding` is how Django (and you) can tell "INSERT pending" from "already persisted" — more reliable than `pk is None` when primary keys have Python-side defaults such as `uuid4`.

**App registry population** (`django.setup()` → `apps.populate(INSTALLED_APPS)`), triggered by `manage.py`, `wsgi.py`/`asgi.py` or the test runner:

| Phase | What happens | Safe to use |
|---|---|---|
| 1. App configs | Import each app package, instantiate its `AppConfig` | Nothing app-related; settings only |
| 2. Models | Import each app's `models` module; models register | Model classes (no queries) |
| 3. `ready()` | Call `AppConfig.ready()` on each app in `INSTALLED_APPS` order | Models, signals, checks — but **no queries** |

After phase 3, `apps.ready` is `True`. The database is not guaranteed to exist or be migrated at this point (think `migrate` on an empty DB, `collectstatic` in a build container), which is why `ready()` must not query.

## Core concepts and APIs

**Fields.** Choose by semantics, not storage: `DecimalField` for money (never `FloatField`), `DateTimeField` with `USE_TZ = True`, `TextField` instead of a guessed `max_length`, `UUIDField` for externally exposed identifiers. Key options:

| Option | Layer | Note |
|---|---|---|
| `null` | DB | Avoid on string fields; use `""` as the empty value |
| `blank` | Validation | Affects forms/`full_clean`, not the DB |
| `default` | Python | Callable for mutable values (`default=dict`, not `{}`) |
| `db_default` | DB (≥ 5.0) | Database-computed default; survives raw inserts |
| `choices` | Validation | Use `models.TextChoices`/`IntegerChoices` |
| `unique`, `Meta.constraints` | DB | Real integrity; validation is a courtesy on top |
| `editable=False` | Forms | Excludes from ModelForms and `full_clean` |
| `GeneratedField` | DB (≥ 5.0) | Computed column; backend support varies |

**Inheritance.**

| Style | Tables | Use when | Avoid when |
|---|---|---|---|
| Abstract base (`Meta.abstract = True`) | One per child | Sharing fields/behaviour (timestamps, soft-delete flags) | You need to query across children |
| Multi-table | Parent + child, joined by implicit `OneToOne` | Genuinely polymorphic rows queried via parent | Hot paths — every child load joins |
| Proxy (`Meta.proxy = True`) | None new | Different default manager, ordering, admin, methods | You need new fields |

**Managers.** A manager is the table-level API (`Order.objects`). Add domain query entry points by building a custom `QuerySet` and exposing it with `QuerySet.as_manager()` or `Manager.from_queryset()`; detailed queryset design lives in [django-orm](../../django-orm/SKILL.md). The *first* manager defined becomes `_default_manager`, which related lookups and dumpdata use — never make a filtering manager the default.

**Meta.** `db_table`, `ordering` (costs a sort on every query — prefer explicit `order_by`), `constraints`, `indexes`, `verbose_name`, `default_related_name`, `get_latest_by`, `permissions`, `default_permissions`, `abstract`, `proxy`, `managed`.

**Validation.** `full_clean(exclude=None, validate_unique=True, validate_constraints=True)` runs `clean_fields()` → `clean()` → `validate_unique()` → `validate_constraints()` (the last since 4.1). Raise `ValidationError({"field": "message"})` from `clean()` to attach errors to fields.

**save().** Decides INSERT vs UPDATE (`_state.adding`, `force_insert`, `force_update`), calls `pre_save()` on each field (e.g. `auto_now`), emits `pre_save`/`post_save` signals. Use `update_fields=[...]` to write only changed columns. Passing `save()` arguments positionally is deprecated since 5.1 — use keywords.

**delete().** Instance `delete()` runs the `Collector`, which resolves `on_delete` behaviour for related rows and sends `pre_delete`/`post_delete` for each collected object.

**Bulk operations bypass instance methods.** `QuerySet.update()`, `bulk_create()`, `bulk_update()` never call `save()` and send no save signals. `QuerySet.delete()` does not call your overridden `Model.delete()` (it does still emit delete signals for collected objects). Logic that must always run therefore belongs in a service, not in `save()`.

## Implementation examples

```python
# apps/orders/models.py
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OrderQuerySet(models.QuerySet):
    def for_user(self, user):
        return self.filter(customer=user)

    def unpaid(self):
        return self.filter(status=Order.Status.PENDING)


class Order(TimeStamped):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    objects = OrderQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(total__gte=0), name="order_total_non_negative"),
        ]

    def clean(self):
        if self.discount > self.total:
            raise ValidationError({"discount": "Discount cannot exceed the order total."})

    def __str__(self) -> str:
        return f"Order {self.pk} ({self.status})"
```

Version notes: `choices=Status` (enum class directly) works from 5.0; older versions need `Status.choices`. `CheckConstraint(condition=...)` is 5.1+; earlier versions use `check=` (deprecated in 5.1).

**save() override — bad vs good.**

```python
# Bad: side effects hidden in save(); skipped by bulk ops, runs on every admin edit,
# fires before the transaction commits.
def save(self, *args, **kwargs):
    super().save(*args, **kwargs)
    send_receipt_email(self)

# Good: save() only normalises its own data; side effects live in the service.
def save(self, *args, **kwargs):
    self.reference = self.reference.strip().upper()
    super().save(*args, **kwargs)
```

If an override sets fields, honour `update_fields`: add the computed field to `kwargs["update_fields"]` when present, otherwise the change is silently not written.

**Service owns validation and persistence.**

```python
# apps/orders/services.py
from django.db import transaction

@transaction.atomic
def place_order(*, customer, total, discount) -> Order:
    order = Order(customer=customer, total=total, discount=discount)
    order.full_clean()        # explicit: save() will not do this
    order.save()
    transaction.on_commit(lambda: notify_order_placed.delay(order.pk))
    return order
```

**AppConfig.**

```python
# apps/orders/apps.py
from django.apps import AppConfig

class OrdersConfig(AppConfig):
    name = "apps.orders"
    label = "orders"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import checks, signals  # noqa: F401  import registers receivers/checks
```

Since 3.2 Django auto-detects a single `AppConfig` subclass in `apps.py`; list `"apps.orders"` in `INSTALLED_APPS`. `label` must be unique project-wide and is what migrations and `AUTH_USER_MODEL` reference — changing it later is a migration project, so pick it once.

## Production patterns

- Set `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"` explicitly. Django 6.0 changes the global default to `BigAutoField`; being explicit keeps behaviour stable across upgrades (check the 6.0 release notes for migration impact).
- Put invariants in `Meta.constraints` (DB enforced) **and** mirror the user-facing ones in `clean()` for readable errors. Constraints survive bulk writes, raw SQL and race conditions; `clean()` does not.
- Expose domain queries via queryset methods (`Order.objects.for_user(user).unpaid()`), so authorisation scoping is reusable.
- Keep `__str__` cheap — it must not trigger queries (admin and logs call it constantly).
- Use `select_for_update()` inside `transaction.atomic()` when a service reads-then-writes a row that can be modified concurrently (details in django-orm).
- Long-lived app initialisation (caches, clients) is lazy: create on first use, not in `ready()`.

## Decision guidance

| Question | Prefer | Because |
|---|---|---|
| Where does a rule live? | DB constraint if it must never be violated; `clean()` for messages; service for multi-object/process rules | Only the DB sees every write path |
| Logic in `save()` or service? | Service, unless it is pure normalisation of the instance's own fields | Bulk ops and `update()` skip `save()` |
| Fat model methods? | Methods that compute from the instance (`order.amount_due()`); orchestration in services | Models stay import-cheap and testable |
| Custom manager vs queryset? | QuerySet + `as_manager()` | Methods chain |
| Abstract vs multi-table? | Abstract by default | Avoids implicit joins |

## Security considerations

- Mass-assignment: `Order(**request.POST)` lets clients set `status` or `customer`. Construct models from validated, whitelisted data (forms/serializers).
- `on_delete=CASCADE` on user-linked financial records can destroy audit data; use `PROTECT` or soft-delete.
- Do not store secrets or tokens in plain model fields; hash or encrypt (see [django-security-engineering](../../django-security-engineering/SKILL.md)).
- `ready()` runs in every process including management commands — never put credentials-fetching network calls there.

## Performance considerations

- `Meta.ordering` adds `ORDER BY` everywhere, including `count()` subqueries in some paths and admin.
- Multi-table inheritance joins on each access; proxy and abstract don't.
- `save()` without `update_fields` writes every column — costly for wide rows and causes lost updates under concurrency.
- Imports in `models.py` run at startup for every process; keep heavy imports (pandas, SDKs) inside functions.

## Testing

- Test `clean()` and constraints separately: `full_clean()` raising `ValidationError`, and saving invalid data raising `IntegrityError` inside `transaction.atomic()`.
- Use `TestCase` (transaction rollback per test) for model tests; `TransactionTestCase` only when testing `on_commit` or DB-level transaction behaviour (or use `self.captureOnCommitCallbacks(execute=True)`).
- Assert `ready()` wiring indirectly (receiver fires, check registered) rather than calling `ready()`.
- Infrastructure and factories: [django-testing](../../django-testing/SKILL.md).

```python
def test_discount_cannot_exceed_total(user):
    order = Order(customer=user, total=Decimal("10"), discount=Decimal("11"))
    with pytest.raises(ValidationError) as exc:
        order.full_clean()
    assert "discount" in exc.value.message_dict
```

## Common mistakes and anti-patterns

- Assuming `save()` validates. It doesn't.
- `null=True` on `CharField` → two empty states (`None` and `""`).
- Mutable defaults (`default={}`) shared across instances.
- Filtering default manager (`objects = ActiveManager()` as the first manager) hiding rows from related lookups and admin.
- Querying in `ready()` or at module level (`DEFAULT_PLAN = Plan.objects.get(...)`) — breaks `migrate`, tests and builds.
- Importing models from another app at the top of `apps.py`.
- Referencing `auth.User` directly instead of `settings.AUTH_USER_MODEL` / `get_user_model()`.
- Changing an app `label` or swapping the user model mid-project without a migration plan.
- Overriding `delete()` expecting it to run on `queryset.delete()`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AppRegistryNotReady: Apps aren't loaded yet` | Model imported at module level before `django.setup()` (standalone script, `apps.py`, settings) | Call `django.setup()` first, import inside functions |
| `RuntimeError: Model class ... doesn't declare an explicit app_label` | App not in `INSTALLED_APPS` or wrong `name` | Fix `INSTALLED_APPS`/`AppConfig.name` |
| Receivers fire twice | Signals module imported from two paths or `ready()` called twice | `dispatch_uid`, import only in `ready()` |
| Field change not persisted | `save(update_fields=[...])` missing the field | Include it |
| `auto_now` not updated | `QuerySet.update()` used | Set the field explicitly in `update()` |
| Validation passes but DB raises `IntegrityError` | Race between `validate_unique` and insert | Rely on the constraint; catch `IntegrityError` |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/db/models/
- https://docs.djangoproject.com/en/stable/ref/models/fields/
- https://docs.djangoproject.com/en/stable/ref/models/options/
- https://docs.djangoproject.com/en/stable/ref/models/instances/
- https://docs.djangoproject.com/en/stable/topics/db/managers/
- https://docs.djangoproject.com/en/stable/ref/applications/
- https://docs.djangoproject.com/en/stable/releases/ (verify version-specific behaviour)
