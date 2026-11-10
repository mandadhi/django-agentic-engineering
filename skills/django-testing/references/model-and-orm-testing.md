# Model and ORM Testing

> **Scope:** How to test models, validation, constraints, relationships, QuerySets, managers, database defaults, signals and migrations — what to assert, at which level, and how to keep those tests fast and backend-honest. ORM mechanics themselves live in django-orm.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · factory_boy 3.3 · **Depends on:** **[Django]** `django.test`, `django.db`; **[pytest]**; **[pytest-django]** `django_db`, `django_assert_num_queries`; **[third-party: factory_boy]** factories; **[third-party: django-test-migrations]** optional migration tests.

## 1. Purpose and scope

Model-layer tests prove that **the data layer protects the business invariants** and that
**named query vocabulary returns the right rows**. They answer:

- Does validation reject what the business forbids, with the error code callers depend on?
- Does the *database* reject it too, when code bypasses validation (`bulk_create`, `update()`, raw SQL, a second process)?
- Do custom QuerySets/managers include exactly the intended rows — and exclude the rest (soft-deleted, other tenant, inactive)?
- Do relationships cascade, protect or nullify as designed?
- Do data migrations transform real-shaped legacy data correctly and reversibly?

Out of scope here: transaction/locking behaviour (→ [database-and-transaction-testing.md](database-and-transaction-testing.md)),
query budgets and N+1 detection (→ [performance-and-reliability-testing.md](performance-and-reliability-testing.md)),
factories (→ [fixtures-and-test-data.md](fixtures-and-test-data.md)), and how the ORM works
(→ [django-orm](../../django-orm/SKILL.md)).

## 2. Core concepts

| Concept | What it means for tests |
|---|---|
| **Two validation layers** | `Model.full_clean()` (Python, opt-in) vs database constraints (always enforced). Test both for invariants that matter. |
| **`save()` does not validate** | **[Django]** `save()` never calls `full_clean()`. A test that only calls `save()` proves only the DB constraint. |
| **Constraint validation in `full_clean()`** | **[Django]** `full_clean()` runs `validate_constraints()` for `UniqueConstraint`/`CheckConstraint` (Django ≥ 4.1). Conditional/expression constraints may be skipped or partially validated in Python depending on the expression — `VERIFY` per constraint. |
| **Error codes** | `violation_error_code` on constraints (Django ≥ 5.0) and `ValidationError(code=...)` give stable assertions; prefer codes over message text. |
| **Behaviour vs mechanism** | Test "an archived order is not listed", not "the manager calls `.filter(archived_at__isnull=True)`". |
| **Pure vs persisted** | Model methods that use only instance fields need no DB — use `Factory.build()`. |
| **Historical models** | Data migrations run against `apps.get_model()` snapshots, not your current classes; tests must use the same. |

## 3. Internal mechanics

### What actually runs

- `Model.full_clean(exclude=None, validate_unique=True, validate_constraints=True)` calls
  `clean_fields()` → `clean()` → `validate_unique()` → `validate_constraints()` **[Django]**.
  Uniqueness and constraint validation issue **SELECT queries** — so `full_clean()` tests need
  DB access even when nothing is saved.
- `IntegrityError` is raised by the database adapter at `INSERT`/`UPDATE` time (or at
  `COMMIT` for deferred constraints — see [database-and-transaction-testing.md](database-and-transaction-testing.md)).
- Under **[Django]** `TestCase` / **[pytest-django]** `django_db`, each test runs inside a
  transaction. On PostgreSQL, after an `IntegrityError` the transaction is *aborted*: every
  subsequent query raises `TransactionManagementError` / "current transaction is aborted"
  until rollback. Wrapping the failing statement in `transaction.atomic()` creates a savepoint
  that is rolled back alone, leaving the test transaction usable.
- `db_default` (Django ≥ 5.0) and `GeneratedField` (Django ≥ 5.0) values are computed by the
  database. After `save()` the Python instance may hold a `DatabaseDefault` sentinel or no
  value; `refresh_from_db()` is required before asserting. On backends supporting
  `RETURNING`, Django may populate some values automatically — `VERIFY` per backend/field.
- Signals are dispatched synchronously in-process by `Model.save()`/`delete()`; `bulk_create`,
  `QuerySet.update()` and `QuerySet.delete()` (for `pre_save`/`post_save`) bypass them.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Choose |
|---|---|---|---|
| Validate invariant | `full_clean()` test | `IntegrityError` test | **Both** for money, uniqueness, state — DB proves it holds against bypasses; `full_clean` proves users get a 400, not a 500. |
| Model method test | `build()` (no DB) | `create()` | `build()` whenever the method reads only instance fields — 10–100× faster. |
| QuerySet assertion | set of pks | ordered list | Set unless ordering is part of the contract; then `order_by` must be explicit in the code. |
| Signal tests | call receiver directly | trigger via `save()` | Receiver directly for logic; **one** wiring test per signal via `save()`. |
| Data migration test | `MigrationExecutor` hand-rolled | django-test-migrations `migrator` | Library if installed (cleaner); executor otherwise. Both are slow — test only non-trivial migrations. |
| Test DB creation | run migrations | `TEST: {"MIGRATE": False}` | Run migrations in CI (proves they apply); skipping is a local speed hack only. |

## 5. Django implementation patterns

### Validation: Python layer

```python
import pytest
from django.core.exceptions import ValidationError
from apps.orders.tests.factories import OrderFactory

@pytest.mark.django_db  # full_clean() queries for uniqueness/constraints
def test_discount_cannot_exceed_subtotal():
    order = OrderFactory.build(subtotal=100, discount=150, customer=CustomerFactory())
    with pytest.raises(ValidationError) as exc:
        order.full_clean()
    assert exc.value.error_dict["__all__"][0].code == "discount_exceeds_subtotal"
```

Assert on `code`, not message text — messages are translated and reworded.

### Validation: database layer

```python
from django.db import IntegrityError, transaction

@pytest.mark.django_db
def test_invoice_number_unique_per_tenant():
    first = InvoiceFactory(number="INV-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceFactory(number="INV-1", tenant=first.tenant)
    # Test transaction is still usable because the savepoint rolled back:
    InvoiceFactory(number="INV-1")  # different tenant → allowed
```

`CheckConstraint` (note: `condition=` replaces `check=` in Django ≥ 5.1; `check=` deprecated):

```python
@pytest.mark.django_db
def test_stock_quantity_cannot_be_negative_even_via_update():
    item = StockItemFactory(quantity=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        StockItem.objects.filter(pk=item.pk).update(quantity=-1)  # bypasses full_clean
```

Testing via `QuerySet.update()` is deliberate: it proves the invariant survives code paths that skip Python validation.

### Conditional unique constraints

```python
@pytest.mark.django_db
class TestOneActiveSubscriptionPerCustomer:
    def test_second_active_rejected(self):
        sub = SubscriptionFactory(status="active")
        with pytest.raises(IntegrityError), transaction.atomic():
            SubscriptionFactory(customer=sub.customer, status="active")

    def test_cancelled_ones_do_not_count(self):
        sub = SubscriptionFactory(status="cancelled")
        SubscriptionFactory(customer=sub.customer, status="cancelled")
        SubscriptionFactory(customer=sub.customer, status="active")  # allowed
```

Always pair "rejected" with "allowed" cases — a constraint that rejects everything passes the first test.

### Model methods and properties (no DB)

```python
@pytest.mark.parametrize("due, paid, today, expected", [
    (date(2026, 1, 10), None,              date(2026, 1, 11), True),
    (date(2026, 1, 10), None,              date(2026, 1, 10), False),  # boundary
    (date(2026, 1, 10), date(2026, 1, 12), date(2026, 1, 20), False),
])
def test_invoice_is_overdue(due, paid, today, expected):
    invoice = InvoiceFactory.build(due_date=due, paid_on=paid)
    assert invoice.is_overdue(today=today) is expected
```

No `django_db` marker: pytest-django will **fail** the test if it touches the DB, which keeps the test honest. Note `build()` still builds `SubFactory` objects unsaved — accessing an FK that requires a query will fail; pass related objects explicitly or use `build` strategies.

### Relationships and `on_delete`

```python
@pytest.mark.django_db
def test_deleting_customer_with_orders_is_protected():
    order = OrderFactory()
    with pytest.raises(ProtectedError):
        order.customer.delete()

@pytest.mark.django_db
def test_deleting_order_cascades_lines_but_keeps_products():
    line = OrderLineFactory()
    product_id = line.product_id
    line.order.delete()
    assert not OrderLine.objects.filter(pk=line.pk).exists()
    assert Product.objects.filter(pk=product_id).exists()
```

Test `on_delete` only where it encodes a business decision (`PROTECT`, `SET_NULL` on audit records, `RESTRICT`). `CASCADE` on a trivial child is Django behaviour, not yours.

### QuerySets and managers

```python
@pytest.mark.django_db
def test_overdue_queryset(django_assert_num_queries):
    overdue = InvoiceFactory(due_date=date(2026, 1, 1), paid_on=None)
    InvoiceFactory(due_date=date(2026, 1, 1), paid_on=date(2026, 1, 2))   # paid
    InvoiceFactory(due_date=date(2026, 3, 1), paid_on=None)               # not yet due
    with django_assert_num_queries(1):
        result = set(Invoice.objects.overdue(today=date(2026, 2, 1)).values_list("pk", flat=True))
    assert result == {overdue.pk}
```

Rules: create at least one row per exclusion reason; compare **sets of pks**; never rely on insertion order. If order is the contract:

```python
# Django ≥ 4.2: assertQuerySetEqual (assertQuerysetEqual removed in 5.1)
self.assertQuerySetEqual(Invoice.objects.by_due_date(), [b, a, c])
```

In pytest: `assert list(qs.values_list("pk", flat=True)) == [b.pk, a.pk, c.pk]`.

### Default-scoping and soft-delete managers

```python
@pytest.mark.django_db
def test_soft_deleted_documents_hidden_from_default_manager():
    doc = DocumentFactory()
    doc.soft_delete()
    assert not Document.objects.filter(pk=doc.pk).exists()
    assert Document.all_objects.filter(pk=doc.pk).exists()

@pytest.mark.django_db
def test_soft_deleted_documents_hidden_through_relations():
    doc = DocumentFactory(); doc.soft_delete()
    assert doc not in doc.folder.documents.all()   # related manager behaviour
```

The second test matters: related managers use the default manager, but some paths (e.g. `_base_manager`, `select_related` joins) do not filter — see [managers-and-querysets.md](../../django-orm/references/managers-and-querysets.md). Tenant-scoping managers get isolation tests in [security-testing.md](security-testing.md).

### `db_default` and `GeneratedField` (Django ≥ 5.0)

```python
@pytest.mark.django_db
def test_order_created_at_set_by_database():
    order = Order.objects.create(customer=CustomerFactory())
    order.refresh_from_db()
    assert order.created_at is not None

@pytest.mark.django_db
def test_line_total_generated_column():
    line = OrderLineFactory(quantity=3, unit_price=Decimal("2.50"))
    line.refresh_from_db()
    assert line.total == Decimal("7.50")
```

`GeneratedField` expressions can behave differently across backends (decimal rounding, functions) — run these on the production engine.

### Signals

```python
# 1. Logic: call the receiver directly
@pytest.mark.django_db
def test_stock_reserved_receiver_creates_audit_row():
    item = StockItemFactory()
    on_stock_changed(sender=StockItem, instance=item, created=False)
    assert StockAudit.objects.filter(item=item).count() == 1

# 2. Wiring: one test proving the signal is connected
@pytest.mark.django_db
def test_stock_audit_signal_is_connected():
    item = StockItemFactory()
    item.quantity = 5
    item.save()
    assert StockAudit.objects.filter(item=item).exists()
```

Disabling a noisy receiver in unrelated tests: prefer a settings flag checked in the receiver, or a fixture that disconnects and **always** reconnects:

```python
@pytest.fixture
def no_search_indexing():
    post_save.disconnect(index_document, sender=Document)
    yield
    post_save.connect(index_document, sender=Document)
```

Factory-level alternative: `factory.django.mute_signals(post_save)` **[third-party: factory_boy]**. Disconnecting globally in `conftest.py` hides real wiring bugs.

### Migrations

CI guard **[Django]**:

```bash
python manage.py makemigrations --check --dry-run
```

Data migration with django-test-migrations **[third-party: django-test-migrations]**:

```python
@pytest.mark.django_db
def test_backfill_order_currency(migrator):
    old_state = migrator.apply_initial_migration(("orders", "0041_order_currency_nullable"))
    Order = old_state.apps.get_model("orders", "Order")
    Customer = old_state.apps.get_model("customers", "Customer")
    c = Customer.objects.create(name="Acme", country="DE")
    o = Order.objects.create(customer=c, currency=None)

    new_state = migrator.apply_tested_migration(("orders", "0042_backfill_currency"))
    Order = new_state.apps.get_model("orders", "Order")
    assert Order.objects.get(pk=o.pk).currency == "EUR"
    migrator.reset()
```

Without the library, the same with **[Django]** `MigrationExecutor`:

```python
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

@pytest.mark.django_db(transaction=True)
def test_backfill_currency_executor():
    executor = MigrationExecutor(connection)
    executor.migrate([("orders", "0041_order_currency_nullable")])
    apps = executor.loader.project_state([("orders", "0041_order_currency_nullable")]).apps
    # ... create rows with historical models ...
    executor.loader.build_graph()
    executor.migrate([("orders", "0042_backfill_currency")])
    apps = executor.loader.project_state([("orders", "0042_backfill_currency")]).apps
    # ... assert ...
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())  # restore latest schema
```

Migration tests need `transaction=True` (schema changes, and on SQLite DDL inside atomic has constraints) and are slow — mark them (`@pytest.mark.migrations`) and run them in a dedicated CI step. Reversibility: migrate forward, then back to the previous node, assert no error and data restored where the reverse function is meaningful.

## 6. Production use cases

- **Payments:** `CheckConstraint(amount > 0)` + `UniqueConstraint(idempotency_key)`; test both rejected and allowed rows and that `update()` cannot bypass them.
- **Inventory:** `quantity >= 0` check constraint tested via concurrent decrements (→ [database-and-transaction-testing.md](database-and-transaction-testing.md)).
- **SaaS:** tenant-scoped default manager — test that every relation path stays scoped.
- **Legacy cleanup:** data migration splitting `full_name` into `first_name`/`last_name`, tested with real-shaped edge cases (empty, unicode, single word).
- **Audit:** `on_delete=SET_NULL` on `AuditEntry.actor` — deleting a user must keep audit rows.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Constraint tests via `IntegrityError` | Proves the real guarantee; catches missing migrations | Needs DB; backend-specific error timing |
| `full_clean()` tests | Asserts user-facing error codes | Not a guarantee — bypassable |
| `build()` model tests | Fast, no DB | Can't cover queries or DB-computed values |
| QuerySet tests with real rows | Behavioural, refactor-safe | Need careful data for each exclusion branch |
| Migration tests | Catch data-loss bugs before production | Slow, brittle as the migration graph grows; delete after squashing |

## 8. Security and performance considerations

- **Security:** default managers that scope by tenant or soft-delete are security controls — test related-manager, `select_related`, admin and `_base_manager` paths. Unique constraints on tokens/emails protect against duplicate-account and replay bugs; test case-insensitive uniqueness (`Lower("email")` expression constraint) explicitly.
- **Performance:** keep model tests DB-free where possible; use `setUpTestData` / module fixtures for read-only reference data (→ [fixtures-and-test-data.md](fixtures-and-test-data.md)). Wrap selector tests with a query budget; full N+1 guidance in [performance-and-reliability-testing.md](performance-and-reliability-testing.md).
- Migration tests multiply suite time; budget them and run them only when `migrations/` changes if CI supports path filters.

## 9. Common mistakes and anti-patterns

- Asserting `IntegrityError` **without** `transaction.atomic()` → following queries fail with "current transaction is aborted" (PostgreSQL), producing confusing errors.
- Testing constraints with `save()` only and calling it "validation is tested" — the API path calls `full_clean()`/serializer validation, which is untested.
- `assert qs.count() == 1` — passes when the wrong row is returned. Assert identity.
- Asserting `list(qs) == [a, b]` without `order_by` → flaky across backends and after `VACUUM`.
- Testing Django itself: `CharField(max_length=50)` rejects 51 characters, `auto_now` sets a date.
- Asserting `db_default`/generated values without `refresh_from_db()`.
- Globally disconnecting signals in `conftest.py`.
- Data migration code importing current model classes (`from apps.orders.models import Order`) — tests may pass today and the migration breaks after the next schema change.
- Running model tests on SQLite while production is PostgreSQL — constraint and type behaviour differ (→ [database-and-transaction-testing.md](database-and-transaction-testing.md)).

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| `TransactionManagementError: An error occurred in the current transaction` | `IntegrityError` caught without savepoint | Wrap the failing call in `transaction.atomic()` |
| Constraint test passes on SQLite, fails on PostgreSQL (or vice versa) | Backend-specific enforcement/collation | Run on production engine |
| `full_clean()` does not raise for a conditional constraint | Python-side validation skipped for that expression | Assert the DB-level `IntegrityError`; `VERIFY` constraint's `validate()` support |
| `RuntimeError: Database access not allowed` | Missing `django_db` marker (pytest-django) | Add the marker or use `build()` |
| Field value is `DatabaseDefault` object | `db_default` not refreshed | `refresh_from_db()` |
| Migration test leaves schema at old state, later tests fail | Missing reset/migrate-to-latest | `migrator.reset()` or migrate to leaf nodes in teardown |
| Signal receiver not firing in test | `bulk_create`/`update()` used, or receiver module not imported in `AppConfig.ready()` | Use `save()`; check `ready()` import |

Validation: run `makemigrations --check --dry-run`; ensure each `Meta.constraints` entry has a rejected+allowed test pair; run selector tests with a query budget.

## 11. Related references and skills

- Siblings: [fixtures-and-test-data.md](fixtures-and-test-data.md) · [database-and-transaction-testing.md](database-and-transaction-testing.md) · [performance-and-reliability-testing.md](performance-and-reliability-testing.md) · [service-and-architecture-testing.md](service-and-architecture-testing.md) · [security-testing.md](security-testing.md)
- django-orm: [model-design.md](../../django-orm/references/model-design.md) · [database-constraints-and-indexes.md](../../django-orm/references/database-constraints-and-indexes.md) · [managers-and-querysets.md](../../django-orm/references/managers-and-querysets.md) · [migrations-and-schema-evolution.md](../../django-orm/references/migrations-and-schema-evolution.md) · [field-types-and-options.md](../../django-orm/references/field-types-and-options.md)
- django-core: [signals-management-commands.md](../../django-core/references/signals-management-commands.md) · [models-application-lifecycle.md](../../django-core/references/models-application-lifecycle.md)

## 12. Official documentation and external references

- Django — Validating objects: <https://docs.djangoproject.com/en/5.2/ref/models/instances/#validating-objects>
- Django — Constraints reference: <https://docs.djangoproject.com/en/5.2/ref/models/constraints/>
- Django — `db_default`: <https://docs.djangoproject.com/en/5.2/ref/models/fields/#db-default>
- Django — Testing tools (`assertQuerySetEqual`, `assertNumQueries`): <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Django — Writing migrations / data migrations: <https://docs.djangoproject.com/en/5.2/howto/writing-migrations/>
- Django — Signals: <https://docs.djangoproject.com/en/5.2/topics/signals/>
- pytest-django — database access: <https://pytest-django.readthedocs.io/en/latest/database.html>
- factory_boy — Django integration, `mute_signals`: <https://factoryboy.readthedocs.io/en/stable/orms.html>
- django-test-migrations (third-party): <https://github.com/wemake-services/django-test-migrations>
