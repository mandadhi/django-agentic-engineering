# Fixtures and Test Data

> **Scope:** How to create, share and isolate test data in Django suites: pytest data fixtures,
> Django JSON fixtures, factory_boy, model_bakery, Faker, builders for complex aggregates, shared
> and session-scoped data, and large datasets.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · factory_boy ≥ 3.3 · model_bakery ≥ 1.x ·
> Faker · **Depends on:** **[Django]** fixtures/`loaddata`/`setUpTestData` · **[pytest]** fixtures ·
> **[pytest-django]** `db`, `django_db_setup`, `django_db_blocker` · **[third-party: factory_boy]** ·
> **[third-party: model_bakery]** · **[third-party: Faker]**

## 1. Purpose and scope

Test data is the largest source of test fragility. A test that relies on "the user with id 3
from `users.json`" breaks when someone edits the fixture for an unrelated test; a factory
that creates ten related rows to build one order makes every test slow. This reference owns
**how test data is created and shared**:

- Choosing between pytest fixtures, factories, builders and Django JSON fixtures.
- Writing factories that are minimal, valid, tenant-consistent and fast.
- Sharing data across tests (conftest hierarchy, `setUpTestData`, session DB setup) without
  leaking state.
- Generating large datasets for query-count and performance tests.

Out of scope: test doubles for external systems ([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)),
fixture scope *mechanics* in pytest ([pytest-and-pytest-django.md](pytest-and-pytest-django.md)),
and how the test DB is rolled back ([database-and-transaction-testing.md](database-and-transaction-testing.md)).

## 2. Core concepts

| Concept | Meaning | Provenance |
|---|---|---|
| **pytest fixture** | A function that provides a value (object, client, config) to tests by name; has a scope and teardown | **[pytest]** |
| **Django fixture** | Serialized rows (JSON/YAML/XML) loaded with `loaddata` or `TestCase.fixtures` | **[Django]** |
| **Factory** | A class that builds a valid model instance with defaults; tests override only relevant fields | **[third-party: factory_boy]** / **[third-party: model_bakery]** |
| **Builder** | Plain Python helper that assembles a complex aggregate step by step (`OrderBuilder().with_line(...).paid().build()`) | Project code |
| **Fake data** | Realistic random values (names, emails, addresses) | **[third-party: Faker]** |
| **Reference data** | Static lookup rows the application assumes exist (countries, currencies, plan tiers) | Usually a data migration |

Key rules:

1. **A test states what it asserts on.** If a test checks that overdue invoices are flagged,
   the `due_date` must appear in the test body — not hidden in a fixture file.
2. **Defaults are minimal and valid.** A factory creates the smallest object that passes
   `full_clean()` and DB constraints. Optional relations are off by default.
3. **Data is per-test unless proven expensive.** Shared data is an optimisation, not a default.
4. **Nothing depends on primary keys, sequence values, or global row counts.**

## 3. Internal mechanics

### pytest fixture resolution

**[pytest]** resolves fixtures by name through the conftest hierarchy (test module → package
`conftest.py` files → root `conftest.py` → plugins). Narrower definitions override wider ones,
which lets an app override `user` with a tenant-aware variant. Scope (`function`, `class`,
`module`, `package`, `session`) controls how often the fixture runs; a fixture cannot depend
on a narrower-scoped fixture.

### DB access and fixtures

**[pytest-django]** blocks DB access unless the test (or a fixture it depends on) requests
`db`/`transactional_db` or carries `@pytest.mark.django_db`. A function-scoped fixture that
creates rows runs **inside** the test's transaction and is rolled back with it. A
session-scoped fixture cannot request `db` (function scope) — it must use
`django_db_blocker.unblock()` inside `django_db_setup`, and those rows are **committed** to
the test database and visible to every test.

### `setUpTestData`

**[Django]** `TestCase.setUpTestData()` runs once per class inside the class-level atomic
block; each test then runs in a nested savepoint that is rolled back. Since Django 3.2,
attributes assigned in `setUpTestData` are **deep-copied per test** on first access, so
in-memory mutation in one test does not leak to the next — objects must be deep-copyable
(e.g. no open file handles or locks).

### Django JSON fixtures

**[Django]** `loaddata` deserializes rows and saves them with `save_base(raw=True)`: model
`save()` overrides are **not** called, `pre_save`/`post_save` signals fire with `raw=True`,
and validation is skipped. `TestCase.fixtures = [...]` loads them once per class (Django ≥ 3.2
loads class-level fixtures in `setUpClass`, not per test — VERIFY for your version's exact
lifecycle).

### factory_boy lifecycle

**[third-party: factory_boy]** For `DjangoModelFactory`:

1. Declarations are resolved in order (`Sequence`, `LazyAttribute`, `SubFactory`, ...).
2. `_create()` calls `Model.objects.create(**kwargs)` (or `_get_or_create` with
   `django_get_or_create`).
3. `post_generation` hooks and `RelatedFactory` run **after** creation.
4. Before 3.3 the factory then called `instance.save()` again if any post-generation hook ran;
   factory_boy 3.3 deprecated that implicit save and added `skip_postgeneration_save = True`
   in `Meta` to opt out explicitly.

`build()` constructs without saving (SubFactories are also built, not saved), `create()` saves,
`stub()` returns a plain object with attributes only.

## 4. Engineering decisions and trade-offs

### Choosing a data mechanism

| Mechanism | Use when | Avoid when |
|---|---|---|
| Inline factory call in the test | The data is what the test asserts on (most tests) | Never — this is the default |
| Function-scoped pytest fixture | Several tests in a module need the same *role* (`customer`, `staff_user`, `auth_client`) | It hides fields the test asserts on |
| Builder | Aggregates with invariants (order + lines + payment + shipment) | Single models — a factory is enough |
| `setUpTestData` / class-scoped data | Many read-only tests on the same expensive graph, unittest style | Tests mutate the data in the DB in ways later tests notice (they won't — rollback — but readers think they might) |
| Session data via `django_db_setup` | Large, read-only reference datasets needed by many tests | Any test writes to those rows; parallel workers need different data |
| Django JSON fixtures | Static reference data that mirrors production (country codes) and has no better source | Business data, anything with FKs to users, anything that changes with the schema |
| Data migration | Reference data the app *requires* in every environment | Test-only data |

### factory_boy vs model_bakery

| Aspect | factory_boy | model_bakery |
|---|---|---|
| Style | Declarative factory class per model | `baker.make(Model, **overrides)`; optional recipes |
| Defaults | Explicit, reviewed; deterministic with sequences | Auto-generates random values for required fields |
| Readability of intent | High — the factory documents valid defaults | High for quick tests; implicit for complex models |
| Traits / variants | `Params` + `Trait` | Recipes with `extend` |
| Relationships | `SubFactory`, `RelatedFactory`, `post_generation` | Auto-creates required FKs; `make_m2m=True` |
| Risk | Factories drift into "god factories" | Random values mask fields the test silently depends on |
| Fit | Long-lived domain-heavy codebases | Prototypes, CRUD-heavy apps, migration from fixtures |

Pick **one** per codebase. Mixing them doubles the learning cost and produces two inconsistent
definitions of a "valid order".

### Random vs deterministic values

Random fake values catch assumptions (e.g. name length) but make failures non-reproducible
unless the seed is logged. Prefer `Sequence` for unique identifiers and fixed literals for
anything a test asserts on; use Faker for incidental fields only, with a fixed seed.

## 5. Django implementation patterns

### Minimal factories with traits (factory_boy)

```python
# apps/orders/tests/factories.py
import datetime as dt
from decimal import Decimal

import factory
from factory.django import DjangoModelFactory


class TenantFactory(DjangoModelFactory):
    class Meta:
        model = "tenants.Tenant"

    slug = factory.Sequence(lambda n: f"tenant-{n}")
    name = factory.LazyAttribute(lambda o: o.slug.title())


class UserFactory(DjangoModelFactory):
    class Meta:
        model = "accounts.User"
        skip_postgeneration_save = True          # factory_boy >= 3.3

    tenant = factory.SubFactory(TenantFactory)
    email = factory.Sequence(lambda n: f"user{n}@example.test")
    password = factory.django.Password("pw")      # factory_boy >= 3.3; hashed once


class ProductFactory(DjangoModelFactory):
    class Meta:
        model = "catalog.Product"

    tenant = factory.SubFactory(TenantFactory)
    sku = factory.Sequence(lambda n: f"SKU-{n:05d}")
    price = Decimal("10.00")


class OrderFactory(DjangoModelFactory):
    class Meta:
        model = "orders.Order"

    customer = factory.SubFactory(UserFactory)
    # Keep the order in the customer's tenant — never two independent tenants
    tenant = factory.SelfAttribute("customer.tenant")
    reference = factory.Sequence(lambda n: f"ORD-{n:06d}")
    status = "pending"

    class Params:
        paid = factory.Trait(
            status="paid",
            paid_at=factory.LazyFunction(lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        )
        cancelled = factory.Trait(status="cancelled")


class OrderLineFactory(DjangoModelFactory):
    class Meta:
        model = "orders.OrderLine"

    order = factory.SubFactory(OrderFactory)
    product = factory.SubFactory(
        ProductFactory, tenant=factory.SelfAttribute("..order.tenant")
    )
    quantity = 1
    unit_price = factory.SelfAttribute("product.price")
```

Usage:

```python
def test_paid_orders_are_not_cancellable(db):
    order = OrderFactory(paid=True)
    with pytest.raises(OrderStateError):
        cancel_order(order=order, actor=order.customer)
```

`SelfAttribute("..order.tenant")` reaches into the parent factory's context; this is the key
tool for **tenant-consistent graphs**. Without it each `SubFactory` creates its own tenant and
tests pass on data that could never exist in production (and miss cross-tenant bugs).

### Many-to-many and reverse relations

```python
class TeamFactory(DjangoModelFactory):
    class Meta:
        model = "teams.Team"
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"team-{n}")

    @factory.post_generation
    def members(self, create, extracted, **kwargs):
        if not create or not extracted:
            return                      # no members by default — keep it minimal
        self.members.add(*extracted)


team = TeamFactory(members=[alice, bob])
```

`RelatedFactory` creates a reverse-FK object after the parent (e.g. a `Profile` for each
`User`). Use it only if the application invariant really requires the related row — otherwise
every test pays for it.

```python
class UserWithProfileFactory(UserFactory):
    profile = factory.RelatedFactory("apps.accounts.tests.factories.ProfileFactory", factory_related_name="user")
```

### build vs create vs stub

```python
def test_display_name_prefers_full_name():
    user = UserFactory.build(first_name="Ada", last_name="Lovelace")   # no DB
    assert user.display_name == "Ada Lovelace"
```

`build()` is the right default for pure model methods and properties — no `db` fixture, no
transaction, an order of magnitude faster. Note that `build()` still *builds* SubFactories;
accessing `user.tenant.pk` gives `None`.

### `django_get_or_create` pitfalls

```python
class CurrencyFactory(DjangoModelFactory):
    class Meta:
        model = "billing.Currency"
        django_get_or_create = ("code",)
    code = "EUR"
```

Useful for natural-key lookup rows. Pitfalls: overrides of non-key fields are **ignored** when
the row exists (`CurrencyFactory(code="EUR", decimals=3)` silently returns the existing EUR);
and it hides tests that accidentally share rows. Don't use it on business entities.

### model_bakery equivalents

```python
from model_bakery import baker
from model_bakery.recipe import Recipe, foreign_key

order = baker.make("orders.Order", status="pending")           # saves, fills required fields
draft = baker.prepare("orders.Order")                           # unsaved
orders = baker.make("orders.Order", _quantity=20)               # many
rows = baker.make("orders.OrderLine", _quantity=500, _bulk_create=True)

paid_order = Recipe("orders.Order", status="paid", customer=foreign_key(Recipe("accounts.User")))
```

`_bulk_create=True` uses `bulk_create` (no `save()`, no signals). model_bakery fills
required fields with random values — assert only on fields you set.

### Faker: seeded and incidental

```python
# conftest.py
import factory.random

@pytest.fixture(autouse=True, scope="session")
def _seed_factories():
    factory.random.reseed_random("django-testing")   # seeds factory_boy's Faker + fuzzy
```

```python
class AddressFactory(DjangoModelFactory):
    class Meta:
        model = "customers.Address"
    line1 = factory.Faker("street_address")
    city = factory.Faker("city")
    postcode = factory.Faker("postcode", locale="en_GB")
```

**[third-party: Faker]** ships a pytest plugin with a `faker` fixture; it is seeded to `0` by
default per test unless `faker_seed` is overridden (VERIFY against the installed Faker
version). Use `faker.unique.email()` when uniqueness matters — but a `Sequence` is cheaper and
cannot exhaust.

### Builders for aggregates

When an aggregate has invariants (totals equal line sums, a paid order has a payment), a
builder keeps tests readable and valid:

```python
# apps/orders/tests/builders.py
from dataclasses import dataclass, field
from decimal import Decimal

@dataclass
class OrderBuilder:
    customer: "User | None" = None
    lines: list[tuple[str, int, Decimal]] = field(default_factory=list)
    _paid: bool = False

    def with_line(self, sku="SKU-1", qty=1, price=Decimal("10.00")):
        self.lines.append((sku, qty, price))
        return self

    def paid(self):
        self._paid = True
        return self

    def build(self):
        customer = self.customer or UserFactory()
        order = OrderFactory(customer=customer)
        for sku, qty, price in self.lines or [("SKU-1", 1, Decimal("10.00"))]:
            product = ProductFactory(tenant=customer.tenant, sku=sku, price=price)
            OrderLineFactory(order=order, product=product, quantity=qty, unit_price=price)
        if self._paid:
            # use the real service so the aggregate stays valid
            mark_order_paid(order=order, payment_ref="test-pay-1")
        order.refresh_from_db()
        return order


def test_refund_cannot_exceed_paid_total(db):
    order = OrderBuilder().with_line(qty=2, price=Decimal("5.00")).paid().build()
    with pytest.raises(RefundError):
        refund(order=order, amount=Decimal("10.01"))
```

Routing state transitions through the real service (`mark_order_paid`) prevents builders from
creating states the domain cannot produce. The cost is coupling builder speed to service speed.

### Role fixtures in the conftest hierarchy

```python
# conftest.py (project root) — cross-app roles only
@pytest.fixture
def tenant(db):
    return TenantFactory()

@pytest.fixture
def user(tenant):
    return UserFactory(tenant=tenant)

@pytest.fixture
def other_tenant_user(db):
    return UserFactory()            # deliberately a different tenant

# apps/billing/tests/conftest.py — app-specific
@pytest.fixture
def billing_admin(tenant):
    return UserFactory(tenant=tenant, role="billing_admin")
```

Keep the root conftest small: users, tenants, clients. App-specific data belongs next to the app.

### Expensive read-only data

```python
# unittest style
class ReportSelectorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = TenantFactory()
        OrderFactory.create_batch(50, customer__tenant=cls.tenant, paid=True)

    def test_revenue_total(self):
        ...
```

```python
# pytest style: session data committed once (visible to every test in the session)
@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        call_command("loaddata", "countries.json")     # read-only reference data only
```

Risks of session data: tests that `delete()`/`update()` it break later tests (rollback does
not restore committed session data); `TransactionTestCase`/`transaction=True` tests **flush**
all tables after they run, deleting it for later tests; with `--reuse-db` it is reloaded on
every run unless guarded. Prefer `serialized_rollback` or recreate per class if transactional
tests coexist ([database-and-transaction-testing.md](database-and-transaction-testing.md)).

### Large datasets

```python
@pytest.mark.slow
def test_export_streams_100k_rows(db, tenant):
    product = ProductFactory(tenant=tenant)
    orders = OrderFactory.create_batch(10, customer__tenant=tenant)
    lines = [
        OrderLineFactory.build(order=orders[i % 10], product=product)
        for i in range(100_000)
    ]
    OrderLine.objects.bulk_create(lines, batch_size=5_000)
    ...
```

`build()` + `bulk_create()` skips per-row `save()`, signals and factory post-generation —
make sure the code under test doesn't depend on them. For query-count tests, 10–20 rows are
enough to expose an N+1; large volumes belong in a marked slow suite
([performance-and-reliability-testing.md](performance-and-reliability-testing.md)).

### Django JSON fixtures, when acceptable

```python
class CountryLookupTests(TestCase):
    fixtures = ["countries"]          # apps/geo/fixtures/countries.json
```

Acceptable for stable reference data with no FKs to mutable entities. Generate it with
`dumpdata geo.Country --natural-primary --indent 2` and regenerate when the model changes.

## 6. Production use cases

- **Multi-tenant SaaS:** factories derive `tenant` via `SelfAttribute` from the root object;
  an `other_tenant_user` fixture exists in every app's tests so isolation tests are one line.
- **E-commerce checkout:** `OrderBuilder` produces valid paid/shipped/refunded orders through
  real services; permission and refund tests read like specifications.
- **Reporting:** `setUpTestData` creates one representative month of orders per class; tests
  assert aggregates read-only.
- **Legacy suite migration:** replace a 4,000-line `initial_data.json` with factories one app at
  a time; keep only reference data as a fixture or data migration.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Factories | Explicit, isolated, schema-change-resilient (one place to fix), composable | Can grow hidden graphs; cost per object if SubFactories cascade |
| JSON fixtures | Zero code, realistic snapshots | Silent drift from the schema, `save()`/validation bypassed, magic PKs, shared by all tests |
| Builders | Express domain invariants; readable | More code to maintain; slower if routed through services |
| Session data | Fast for large read-only sets | Leaks across tests, breaks with transactional tests and parallel workers if mutated |
| Random fake data | Finds hidden assumptions | Irreproducible failures without seeding; flaky uniqueness collisions |

## 8. Security and performance considerations

- **Never use real personal data** (production dumps, real emails, real card numbers) in
  fixtures or factories. Use `.test`/`example.com` domains and documented test card numbers.
  Production snapshots in the repo are a data-protection incident.
- Use a fast password hasher in test settings (`MD5PasswordHasher`) — password hashing is
  often the single largest cost of user factories. `factory.django.Password` hashes once per
  factory call.
- Avoid cascading `SubFactory` chains: a `LineFactory` that creates order → customer → tenant
  → plan → ... per line makes `create_batch(100)` create hundreds of rows. Pass the parent
  explicitly.
- Prefer `build()` where no DB is needed; prefer `bulk_create` for volume.
- `create_batch(n)` issues at least `n` INSERTs; for > ~500 rows use `build_batch` +
  `bulk_create`.

## 9. Common mistakes and anti-patterns

- **God factory:** `OrderFactory` creates lines, payments, shipments and a coupon by default.
  Every test pays and some assert on accidental data. Use traits.
- **Asserting on factory defaults:** `assert order.total == Decimal("10.00")` where `10.00`
  comes from the factory. Set it in the test.
- **Magic IDs:** `Order.objects.get(pk=1)`, `reverse("order-detail", args=[1])` — IDs depend on
  sequences and test order. Use the created object's `pk`.
- **Counting global tables:** `assert User.objects.count() == 3` breaks when a fixture or data
  migration adds a row. Count a scoped queryset or compare sets of IDs.
- **Independent tenants in one graph:** each `SubFactory` spawns a new tenant; cross-tenant
  bugs become invisible.
- **`django_get_or_create` on entities:** silently shares rows between tests and ignores overrides.
- **Session data mutated by a test** or deleted by a transactional test's flush.
- **Unseeded random values** in fields the code branches on (e.g. random `status`), producing
  intermittent failures.
- **Fixtures that hide the arrange step:** a test with six fixture parameters and no visible
  setup cannot be read on its own.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| `IntegrityError: duplicate key` from a factory | Static value for a unique field; `faker.unique` exhausted; reused `django_get_or_create` | `Sequence`; pass explicit values |
| `RuntimeError: Database access not allowed` in a fixture | Fixture creates rows without depending on `db` | Add `db` to the fixture signature |
| `ScopeMismatch` | Session/module fixture depends on function-scoped `db` | Use `django_db_setup` + `django_db_blocker`, or narrow the scope |
| Data from `setUpTestData` "changes" between tests | Pre-3.2 Django or non-deep-copyable attribute | Upgrade / store IDs and re-fetch |
| Test passes alone, fails in suite | Session data mutated; transactional test flushed tables | See [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |
| Factory warns about `_after_postgeneration` save deprecation | factory_boy ≥ 3.3 implicit save | Set `skip_postgeneration_save = True` and save explicitly in hooks if needed |
| JSON fixture fails after a migration | Fixture predates schema change | Regenerate with `dumpdata`, or replace with factories |

Validation checklist for a factories module:

- Every factory's default object passes `full_clean()` and DB constraints (one parametrized
  smoke test over all factories catches drift).
- Default `create()` of each factory produces a bounded number of rows (assert with
  `django_assert_num_queries` in the smoke test).
- Graphs are tenant-consistent.

```python
@pytest.mark.parametrize("factory_cls", [UserFactory, OrderFactory, OrderLineFactory])
def test_factory_defaults_are_valid(db, factory_cls):
    obj = factory_cls()
    obj.full_clean()
```

## 11. Related references and skills

- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — fixture scopes, conftest mechanics, DB markers.
- [django-testing-framework.md](django-testing-framework.md) — `TestCase.fixtures`, `setUpTestData` in the class hierarchy.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — rollback, flush, `serialized_rollback`.
- [model-and-orm-testing.md](model-and-orm-testing.md) — testing the models these factories build.
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — dataset sizes for query-count and slow tests.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — time and randomness control.
- [django-orm: model-relationships.md](../../django-orm/references/model-relationships.md),
  [django-orm: bulk-operations-and-batching.md](../../django-orm/references/bulk-operations-and-batching.md) — relationship and bulk-insert mechanics.
- [django-security-engineering: multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md) — why tenant-consistent data matters.

## 12. Official documentation and external references

- Django — test fixtures and `setUpTestData`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#fixture-loading>,
  <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#django.test.TestCase.setUpTestData>
- Django — providing initial data: <https://docs.djangoproject.com/en/5.2/howto/initial-data/>
- pytest — fixtures: <https://docs.pytest.org/en/stable/how-to/fixtures.html>
- pytest-django — database helpers (`django_db_setup`, `django_db_blocker`): <https://pytest-django.readthedocs.io/en/latest/database.html>
- factory_boy: <https://factoryboy.readthedocs.io/en/stable/> · Django integration: <https://factoryboy.readthedocs.io/en/stable/orms.html> · changelog: <https://factoryboy.readthedocs.io/en/stable/changelog.html>
- model_bakery: <https://model-bakery.readthedocs.io/en/latest/>
- Faker: <https://faker.readthedocs.io/en/master/> · pytest plugin: <https://faker.readthedocs.io/en/master/pytest-fixtures.html>
