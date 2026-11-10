# Field Types and Options

> Scope: choosing Django model fields and field options, and the database-level consequences
> of each choice — column types, NULL semantics, defaults (`default` vs `db_default`),
> `choices`, `unique`, `db_index`, `db_column`, `editable`, `auto_now`/`auto_now_add`,
> `JSONField`, file fields, relation fields as *fields* (column, index, constraint), custom
> fields and validators.
> Not covered here: relationship semantics, `on_delete`, through models and reverse accessors
> ([model-relationships.md](./model-relationships.md)); multi-column constraints and index
> strategy ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md));
> storage backends and upload handling ([django-core file-handling](../../django-core/references/file-handling.md));
> form-level validation ([django-core forms-validation](../../django-core/references/forms-validation.md)).

## 1. Purpose and scope

A field choice is a schema decision: it fixes the column type, nullability, default,
implicit indexes and check constraints, and how values round-trip between Python and the
database. Changing it later means a migration that may rewrite or lock a large table. This
reference gives an agent the information to pick correctly the first time and to predict
what DDL each choice produces.

Baseline Django 5.2 LTS; SQL shown is approximately the PostgreSQL DDL Django emits.

## 2. Core concepts

- **Python-side vs database-side.** `default`, `choices`, `validators`, `blank`, `editable`
  and `auto_now*` are enforced (or not) in Python. `null`, `unique`, `db_index`,
  `db_default` (Django ≥ 5.0), `max_length` (for `varchar`), `db_comment` (Django ≥ 4.2)
  and `PositiveIntegerField`'s check reach the database.
- **`null` vs `blank`.** `null=True` → column allows `NULL`. `blank=True` → validation
  (`full_clean`, forms) allows empty input. They are independent.
- **Empty-string convention.** For string-based fields Django stores "no value" as `""`;
  `null=True` on `CharField`/`TextField` creates two empty states. Exception: a
  `CharField(unique=True, null=True, blank=True)` where many rows may be empty (NULLs don't
  collide in unique indexes; empty strings do).
- **Choices are not constraints.** `choices` validates in `full_clean()`/forms and drives
  `get_FOO_display()`; the column accepts anything. Add a `CheckConstraint` if it matters.

## 3. Internal mechanics

Each field converts values through a pipeline:

| Direction | Methods |
|---|---|
| Python → DB | `get_prep_value()` (Python normalisation) → `get_db_prep_value(connection)` (backend-specific) → `get_db_prep_save()` |
| DB → Python | `from_db_value()` (if defined) — called for every row loaded |
| Input → Python | `to_python()` (used by `clean_fields()` and deserialisation) |
| Schema | `db_type(connection)` (via `connection.data_types`), `deconstruct()` (for migrations) |

`deconstruct()` is why every field kwarg (including `choices`, `validators`, `help_text`)
appears in migrations: the autodetector diffs deconstructed state, so editing `choices`
produces a no-op `AlterField` migration (no DDL on most backends).

Column type mapping (selected):

| Field | PostgreSQL | MySQL | SQLite | Notes |
|---|---|---|---|---|
| `CharField(max_length=n)` | `varchar(n)` | `varchar(n)` | `varchar(n)` (not enforced) | `max_length` optional on PostgreSQL in Django ≥ 4.2 and on SQLite in Django ≥ 5.2 (unbounded `varchar`); required on other backends ([docs](https://docs.djangoproject.com/en/5.2/ref/models/fields/#django.db.models.CharField.max_length)). |
| `TextField` | `text` | `longtext` | `text` | MySQL cannot index/unique `longtext` without a prefix. |
| `IntegerField`/`BigIntegerField` | `integer`/`bigint` | `int`/`bigint` | `integer` | |
| `PositiveIntegerField` | `integer` + `CHECK (col >= 0)` | `int UNSIGNED` | `integer` + CHECK | |
| `DecimalField(max_digits, decimal_places)` | `numeric(p,s)` | `numeric(p,s)` | `decimal` (no real decimal type: values stored as 8-byte `REAL`, no correctly-rounded decimal arithmetic — [docs](https://docs.djangoproject.com/en/5.2/ref/databases/#sqlite-decimal-handling)) | Use for money; don't trust SQLite for money arithmetic tests. |
| `FloatField` | `double precision` | `double precision` | `real` | Binary float — not for money. |
| `BooleanField` | `boolean` | `bool` (tinyint) | `bool` | |
| `DateTimeField` | `timestamp with time zone` | `datetime(6)` | `datetime` | With `USE_TZ=True` values are aware UTC. |
| `UUIDField` | `uuid` | `char(32)` | `char(32)` | MariaDB ≥ 10.7 native `uuid` in Django ≥ 5.0 (existing `char(32)` columns need a migration path); Oracle `VARCHAR2(32)` ([docs](https://docs.djangoproject.com/en/5.2/ref/models/fields/#uuidfield), [5.0 notes](https://docs.djangoproject.com/en/5.2/releases/5.0/#migrating-existing-uuidfield-on-mariadb-10-7)). |
| `JSONField` | `jsonb` | `json` | `text` + JSON1 functions | Oracle: `NCLOB` column (Django 5.2 source, `backends/oracle/base.py`); Oracle cannot store JSON scalars, only objects/arrays ([docs](https://docs.djangoproject.com/en/5.2/ref/models/fields/#jsonfield)). |
| `BinaryField` | `bytea` | `longblob` | `BLOB` | |
| `ForeignKey` | `bigint` (matches target pk) + FK constraint + index | same | same (FK enforced if PRAGMA on) | |

## 4. Engineering decisions and trade-offs

| Decision | Recommended default | Alternative and when |
|---|---|---|
| Money | `DecimalField(max_digits=12, decimal_places=2)` | Integer minor units (`amount_cents`) when you integrate with providers that use minor units and do no fractional maths. Never `FloatField`. |
| Short text | `CharField(max_length=…)` with a meaningful limit | `TextField` for unbounded prose. On PostgreSQL there is no performance difference between `varchar(n)` and `text`. |
| Optional string | `blank=True, default=""` | `null=True` only with `unique=True` on optional identifiers. |
| Optional number/date | `null=True, blank=True` | A sentinel value — avoid. |
| Enumeration | `TextChoices` stored as short strings + `CheckConstraint` | `IntegerChoices` for compact storage; less readable in SQL. Lookup table + FK when values are user-managed. |
| Timestamps | `DateTimeField` with `USE_TZ=True` | `DateField` only for calendar dates (birthdays, due dates). |
| Creation time | `db_default=Now()` (Django ≥ 5.0) | `auto_now_add=True` — Python-side, not applied to raw/bulk SQL inserts outside Django. |
| Semi-structured data | Normal columns | `JSONField` for genuinely variable payloads (provider webhooks, user preferences). |
| Files | `FileField` (path in DB, bytes in storage) | `BinaryField` only for small blobs that must be transactional with the row. |
| Identifiers exposed publicly | `UUIDField(default=uuid.uuid4, unique=True, editable=False)` | See pk discussion in [model-design.md](./model-design.md). |

### `default` vs `db_default`

```python
from django.db.models.functions import Now

class Customer(models.Model):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)           # Python fills it; DDL has no DEFAULT
    created_at = models.DateTimeField(db_default=Now())     # DDL: DEFAULT statement_timestamp()/now()
```

- `default` is applied when the instance is constructed; raw SQL inserts and other
  applications get no default. During `AddField` migrations Django temporarily sets a DB
  default to fill existing rows, then drops it.
- `db_default` is in the DDL, so every writer gets it. Before `save()` an unset field holds a
  `DatabaseDefault` object ([docs](https://docs.djangoproject.com/en/5.2/ref/models/fields/#db-default)).
  On insert, Django returns the computed value when the backend can return columns from an
  INSERT (`Field.db_returning` checks `can_return_columns_from_insert` — Django 5.2 source;
  PostgreSQL, SQLite ≥ 3.35, MariaDB and Oracle do, MySQL does not); otherwise call
  `refresh_from_db()`. If both are set, `default` wins in Python-created rows.

### Adding a field to a large existing table

`AddField(null=False, default=X)` on PostgreSQL ≥ 11 is a metadata-only change for
non-volatile defaults; a volatile default (e.g. `uuid.uuid4` evaluated per row by Django)
forces a backfill. Plan these via [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md).

## 5. Django implementation patterns

### Product with deliberate field choices

```python
# apps/catalog/models.py
from decimal import Decimal
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q


sku_validator = RegexValidator(r"^[A-Z0-9-]{4,32}$", "SKU must be 4-32 chars A-Z, 0-9 or '-'.")


class Product(models.Model):
    sku = models.CharField(max_length=32, unique=True, validators=[sku_validator])
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="products")
    price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    is_active = models.BooleanField(default=True, db_index=False)
    attributes = models.JSONField(default=dict, blank=True)   # callable default, never {}
    image = models.ImageField(upload_to="products/%Y/%m/", blank=True)
    legacy_code = models.CharField(max_length=20, db_column="LEGACY_CD", blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="product_price_gte_0"),
        ]
```

Approximate DDL:

```sql
CREATE TABLE "catalog_product" (
  "id" bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  "sku" varchar(32) NOT NULL UNIQUE,
  "name" varchar(200) NOT NULL,
  "description" text NOT NULL,
  "category_id" bigint NOT NULL,
  "price" numeric(10, 2) NOT NULL,
  "is_active" boolean NOT NULL,
  "attributes" jsonb NOT NULL,
  "image" varchar(100) NOT NULL,
  "LEGACY_CD" varchar(20) NOT NULL,
  CONSTRAINT "product_price_gte_0" CHECK ("price" >= 0)
);
-- FK constraint added DEFERRABLE INITIALLY DEFERRED; index on category_id;
-- plus a varchar_pattern_ops index for sku (LIKE support) on PostgreSQL.
```

Observations an agent should make: `validators` produced no DDL; `unique=True` produced a
unique constraint (and PostgreSQL also creates a `_like` pattern-ops index for `CharField`
uniques/indexes); `FileField` is `varchar(100)` by default — raise `max_length` if paths can
be long.

### `choices` with enforcement

```python
class OrderStatus(models.TextChoices):      # module level so Meta can reference it
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    SHIPPED = "shipped", "Shipped"
    CANCELLED = "cancelled", "Cancelled"


class Order(models.Model):
    Status = OrderStatus                      # keeps the Order.Status.PAID spelling

    status = models.CharField(max_length=16, choices=OrderStatus, default=OrderStatus.PENDING)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=OrderStatus.values),
                name="order_status_valid",
            ),
        ]
```

Adding a new status then requires a migration that alters the constraint — a feature: the
schema documents legal states. Callable `choices` (Django ≥ 5.0, [notes](https://docs.djangoproject.com/en/5.2/releases/5.0/#more-options-for-declaring-field-choices)) avoid migration churn for
the `choices` kwarg itself, not for the constraint.

### `auto_now` and `auto_now_add`

- `auto_now_add=True` sets the value on first save; `auto_now=True` on every `save()`.
- Both imply `editable=False, blank=True`, cannot be overridden on save, and are **not**
  applied by `QuerySet.update()` (and only applied by `bulk_update` if you set them).
- Prefer `db_default=Now()` for creation time; keep `auto_now` for `updated_at` but include
  it in `update_fields` and set it explicitly in `update()` calls:
  `qs.update(status=..., updated_at=Now())`.

### `JSONField` responsibly

```python
# Querying keys — PostgreSQL uses jsonb operators
Product.objects.filter(attributes__colour="red")
Product.objects.filter(attributes__has_key="warranty_months")
```

Rules: use `default=dict` (callable); validate shape at the boundary (schema/pydantic or a
validator); add a GIN index (PostgreSQL, `django.contrib.postgres.indexes.GinIndex`) only
when containment/key queries are frequent; promote keys you filter, sort or join on to real
columns. On SQLite JSON support requires the JSON1 extension (bundled in modern builds).

### Relation fields as fields

| Field | Column / DDL | Options with schema effect |
|---|---|---|
| `ForeignKey` | `<name>_id` + FK constraint + index | `db_index=False` (drop the auto index when a composite index covers it), `db_constraint=False` (no FK constraint — rarely justified), `to_field`, `db_column`. |
| `OneToOneField` | FK + **unique** constraint | Same as FK. |
| `ManyToManyField` | Separate join table with two FKs + unique(pair) | `db_table`, `through` — see [model-relationships.md](./model-relationships.md). |

### Custom fields (sparingly)

Write one only when a value type recurs and needs conversion logic the built-ins lack.

```python
# apps/core/fields.py
from decimal import Decimal
from django.db import models


class MoneyField(models.DecimalField):
    """Decimal with house-standard precision. No new column type; just consistent options."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Omit defaults so migrations stay clean and stable if the class defaults change.
        if kwargs.get("max_digits") == 12:
            kwargs.pop("max_digits")
        if kwargs.get("decimal_places") == 2:
            kwargs.pop("decimal_places")
        return name, path, args, kwargs
```

Requirements: implement `deconstruct()` correctly, keep the import path stable (migrations
import it forever), and implement `from_db_value`/`get_prep_value`/`to_python` together
when the Python type differs from the column type. Often a helper function returning a
configured built-in field is simpler than a subclass.

### Validators

```python
from django.core.validators import MaxValueValidator, MinValueValidator

quantity = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(10_000)])
```

Validators run in `full_clean()` and forms only; they are serialised into migrations (must
be deconstructible: functions at module level or `@deconstructible` classes). Mirror
critical ones as `CheckConstraint`s.

## 6. Production use cases

- **Payment provider reference** — `CharField(max_length=128, unique=True)`; an optional
  external id that arrives later uses `null=True, unique=True, blank=True` so many pending
  rows can coexist.
- **Idempotency key** — `CharField(max_length=64, unique=True)` on `Payment`; the unique
  constraint, not a pre-check, provides idempotency ([transactions-and-concurrency.md](./transactions-and-concurrency.md)).
- **Inventory counts** — `PositiveIntegerField` gives a free `>= 0` check on PostgreSQL and
  SQLite (unsigned on MySQL) so an over-decrement raises `IntegrityError`.
- **Webhook payload archive** — `JSONField` + `created_at` index; never queried by key.
- **Legacy integration** — `db_column` to match an existing schema without renaming Python
  attributes.

## 7. Advantages and disadvantages

| Choice | Advantages | Disadvantages |
|---|---|---|
| `db_default` | Applies to all writers; no backfill for constants | Django ≥ 5.0; instance may need refresh. |
| `JSONField` | Flexible; queryable on PostgreSQL | Weak typing; harder constraints/indexes; schema drift. |
| `TextChoices` + CHECK | Readable, enforced | Migration per new value. |
| `UUIDField` | Global uniqueness | Larger, slower indexes than bigint. |
| Custom field | Encapsulates conversion | Permanent migration dependency on its import path. |
| `db_constraint=False` | Cross-database / legacy | Orphans possible; integrity becomes your code's job. |

## 8. Security and performance considerations

- `editable=False` and `choices` are not authorisation or input sanitisation; validate at
  the API boundary ([django-rest-framework](../../django-rest-framework/SKILL.md)).
- `FileField` stores a path: never build paths from unsanitised user input; Django's
  storage `get_valid_name()` helps, but upload validation belongs to
  [django-core file-handling](../../django-core/references/file-handling.md).
- Store secrets/PII deliberately; `JSONField` blobs often accumulate PII unnoticed
  ([django-security-engineering](../../django-security-engineering/SKILL.md)).
- `db_index=True` on low-selectivity booleans rarely helps and costs writes — prefer a
  partial index ([database-constraints-and-indexes.md](./database-constraints-and-indexes.md)).
- Large `TextField`/`JSONField`/`BinaryField` columns inflate every default `SELECT`; use
  `defer()`/`only()` ([queryset-api.md](./queryset-api.md)) or move them to a side table.
- `from_db_value` runs per row — keep custom-field conversion cheap.

## 9. Common mistakes and anti-patterns

- `FloatField` for money; `DecimalField` without enough `max_digits` for totals.
- `null=True` on `CharField`/`TextField` without the unique-optional reason.
- Mutable defaults: `default={}` or `default=[]` (shared object) — use `dict`/`list`.
- `default=timezone.now()` (evaluated once at import) instead of `default=timezone.now`.
- Treating `choices`/`validators` as database guarantees.
- `unique=True` plus `db_index=True` (redundant: unique already creates an index).
- `JSONField` as a substitute for modelling (orders stored as a JSON blob).
- Changing `max_length` downward on a populated table without checking data.
- Custom fields without `deconstruct()` → migrations that cannot be regenerated.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| Migration generated after only editing `choices`/`help_text` | `deconstruct()` includes them | Expected; no DDL (`sqlmigrate` shows nothing or `-- (no-op)`). |
| `IntegrityError: null value in column` | `null=False` with no default during insert/raw SQL | Add `default`/`db_default` or supply the value. |
| Rounded money | `FloatField` or wrong `decimal_places` | Migrate to `DecimalField`; audit stored values. |
| `DataError: value too long` | Exceeds `max_length` (enforced on PG/MySQL strict, not SQLite) | Validate earlier; test on the production backend. |
| Duplicate empty strings violate unique | Optional unique `CharField` without `null=True` | Use `null=True` and store `None`, or a conditional `UniqueConstraint`. |
| `DatabaseDefault` object on instance | `db_default` not refreshed | `refresh_from_db(fields=[...])`. |

Validate with `python manage.py sqlmigrate app 00NN` to read the DDL and with tests on the
production database engine — SQLite hides `max_length`, many type and constraint differences
([django-testing](../../django-testing/SKILL.md)).

## 11. Related references and skills

- [model-design.md](./model-design.md) — primary keys, Meta, validation layering.
- [model-relationships.md](./model-relationships.md) — FK/O2O/M2M semantics.
- [database-constraints-and-indexes.md](./database-constraints-and-indexes.md) — CHECK, unique, partial indexes.
- [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md) — altering fields safely.
- [django-core file-handling](../../django-core/references/file-handling.md) — storage and uploads.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — type internals, TOAST, jsonb.

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/stable/ref/models/fields/
- https://docs.djangoproject.com/en/stable/howto/custom-model-fields/
- https://docs.djangoproject.com/en/stable/ref/validators/
- https://docs.djangoproject.com/en/stable/topics/db/queries/#querying-jsonfield
- https://docs.djangoproject.com/en/stable/ref/databases/ (backend-specific notes)
- https://www.postgresql.org/docs/current/datatype.html
