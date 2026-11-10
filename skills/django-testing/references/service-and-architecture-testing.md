# Service and Architecture Testing

> **Scope:** How to test service-layer use cases, selectors, business rules, domain events, transaction boundaries and dependency seams, and how to enforce modular-monolith boundaries with automated fitness tests. Service and boundary *design* lives in django-architecture.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · **Depends on:** **[Django]** `django.db.transaction`, `django.test`; **[pytest]** fixtures, parametrize; **[pytest-django]** `django_db`, `django_capture_on_commit_callbacks`, `settings`; **[Python]** `unittest.mock`; **[third-party: import-linter]** optional boundary contracts; **[third-party: factory_boy]** factories.

## 1. Purpose and scope

The service layer is where business behaviour lives, so it carries most of a Django suite's
value. Tests here should:

- Exercise each use case (`place_order`, `refund_payment`, `transfer_stock`) through its public function, against the real test database.
- Prove each business rule with a table of cases, including boundaries.
- Prove **all-or-nothing** behaviour: a failure midway leaves no partial writes.
- Prove side effects (events, tasks, emails, gateway calls) happen **after commit**, exactly once, and not at all on failure.
- Replace only external gateways with fakes, injected through explicit seams.
- Keep app boundaries honest with automated architecture tests.

Out of scope: HTTP concerns (→ [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md), [django-rest-framework-testing.md](django-rest-framework-testing.md)), `on_commit` mechanics (→ [database-and-transaction-testing.md](database-and-transaction-testing.md)), mock API details (→ [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)).

## 2. Core concepts

| Concept | Test implication |
|---|---|
| **Service** = use case with a transaction boundary | Test input → DB state + outcomes + side effects; never via HTTP. |
| **Selector** = scoped, shaped read | Test row inclusion/exclusion per actor, returned shape, query count. |
| **Business rule** | Pure function or model method where possible → table-driven tests without DB. |
| **Domain exception** | Part of the contract: assert type and code, and that state is unchanged. |
| **Gateway / port** | The seam for external systems; tests inject a fake. |
| **Fake vs mock** | Fake = working lightweight implementation (in-memory bus, fake payment gateway); mock = records calls. Use fakes for state, mocks only to assert an interaction at the edge. |
| **Fitness function** | Automated test of an architectural property (no imports of `apps.billing.models` from `apps.orders`). |

## 3. Internal mechanics

- Service tests run inside the per-test transaction **[Django]** `TestCase` / **[pytest-django]** `django_db`. The service's own `transaction.atomic()` becomes a **savepoint** inside it, so rollback-on-error is real and observable within the test.
- `transaction.on_commit()` callbacks registered by the service never fire under rollback isolation, because the outer test transaction never commits. **[Django]** `TestCase.captureOnCommitCallbacks(execute=True)` / **[pytest-django]** `django_capture_on_commit_callbacks(execute=True)` captures and runs them at block exit. A callback registered inside a savepoint that is later rolled back is discarded — which is exactly what you want to assert on the failure path.
- `mock.patch("apps.orders.services.charge")` replaces the name **where it is looked up**; injected dependencies avoid that coupling entirely.
- import-linter **[third-party]** statically analyses the import graph (via grimp) — no Django setup required, so it runs in milliseconds in CI.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Avoid | Why |
|---|---|---|---|
| DB in service tests | Real test DB | Mocking `Model.objects` | ORM mocks test the mock and break on every query refactor |
| External gateway | Injected fake (parameter or settings-driven factory) | `patch` deep in the call graph | Fakes survive refactors; patch paths couple tests to module layout |
| Rule coverage | `parametrize` table on a pure rule function | One test per case via full service | Cheap, readable, exhaustive at boundaries |
| Event assertion | In-memory bus fake + commit capture | Asserting `signal.send` called | Asserts the outcome subscribers see |
| Boundary enforcement | import-linter contracts in CI | Code review only | Reviews miss transitive imports |
| Service test granularity | One test per behaviour/outcome | One mega-test per service | Failures name the broken rule |

Use mocks (`create_autospec`) when the only observable outcome is "we told the gateway X" and no fake exists yet. More than ~2 mocks in a service test is a design smell: the service has too many collaborators, or you are mocking your own code.

## 5. Django implementation patterns

### Service test: outcome, state, side effects

```python
import pytest
from decimal import Decimal
from apps.orders.services import place_order, LineInput
from apps.orders.models import Order
from apps.inventory.models import StockItem

@pytest.mark.django_db
def test_place_order_reserves_stock_and_emits_event(django_capture_on_commit_callbacks, event_bus):
    customer = CustomerFactory()
    item = StockItemFactory(product__price=Decimal("10.00"), quantity=5)

    with django_capture_on_commit_callbacks(execute=True):
        order = place_order(customer=customer, lines=[LineInput(product_id=item.product_id, quantity=2)])

    assert order.total == Decimal("20.00")
    item.refresh_from_db()
    assert item.quantity == 3
    assert event_bus.published == [("order.placed", {"order_id": order.pk})]
```

### Failure path: all-or-nothing

```python
@pytest.mark.django_db
def test_place_order_insufficient_stock_writes_nothing(django_capture_on_commit_callbacks, event_bus):
    customer = CustomerFactory()
    ok = StockItemFactory(quantity=5)
    short = StockItemFactory(quantity=1)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(InsufficientStock) as exc:
            place_order(customer=customer, lines=[
                LineInput(product_id=ok.product_id, quantity=2),     # would succeed
                LineInput(product_id=short.product_id, quantity=3),  # fails second
            ])

    assert exc.value.code == "insufficient_stock"
    assert not Order.objects.filter(customer=customer).exists()
    ok.refresh_from_db(); assert ok.quantity == 5        # first reservation rolled back
    assert callbacks == [] and event_bus.published == []
```

Ordering the failing line **second** is what makes this test valuable: it proves partial work is rolled back. If the service catches the exception internally, wrap the assertion in a separate `transaction.atomic()` only if the service doesn't own one — but a service without its own `atomic()` is itself the bug (→ [service-layer.md](../../django-architecture/references/service-layer.md)).

### Business rules as tables

```python
@pytest.mark.parametrize("subtotal, tier, coupon, expected", [
    (Decimal("99.99"),  "standard", None,     Decimal("0.00")),
    (Decimal("100.00"), "standard", None,     Decimal("5.00")),   # threshold
    (Decimal("100.00"), "gold",     None,     Decimal("10.00")),
    (Decimal("100.00"), "gold",     "SAVE20", Decimal("20.00")),  # coupon beats tier
    (Decimal("10.00"),  "gold",     "SAVE20", Decimal("2.00")),
], ids=["below-threshold", "at-threshold", "gold", "coupon-wins", "small-coupon"])
def test_discount_rule(subtotal, tier, coupon, expected):
    assert compute_discount(subtotal=subtotal, tier=tier, coupon=coupon) == expected
```

No DB marker: the rule is pure. If the rule needs the DB, it probably belongs split into "load" (selector) + "decide" (pure function).

### Selectors: scope, shape, cost

```python
@pytest.mark.django_db
def test_order_list_for_user_scoped_and_bounded(django_assert_num_queries):
    user = UserFactory()
    mine = OrderFactory.create_batch(3, customer__user=user, lines=2)
    OrderFactory(lines=2)                                  # other user
    OrderFactory(customer__user=user, status="draft")      # excluded status

    with django_assert_num_queries(2):  # orders + prefetched lines
        rows = list(order_list_for_user(user=user))
        _ = [line.product_id for o in rows for line in o.lines.all()]

    assert {o.pk for o in rows} == {o.pk for o in mine}
```

Access attributes the caller will use *inside* the query budget block — otherwise lazy loads happen outside and the N+1 goes unseen. Budget philosophy → [performance-and-reliability-testing.md](performance-and-reliability-testing.md).

### Dependency injection seams

Explicit parameter (simplest):

```python
def refund_payment(*, payment_id: int, gateway: PaymentGateway | None = None) -> Refund:
    gateway = gateway or get_payment_gateway()
    ...

def test_refund_marks_payment_refunded(db):
    gateway = FakePaymentGateway()
    payment = PaymentFactory(status="captured", external_id="ch_1")
    refund_payment(payment_id=payment.pk, gateway=gateway)
    payment.refresh_from_db()
    assert payment.status == "refunded"
    assert gateway.refunds == ["ch_1"]
```

Settings-driven (when callers can't pass it):

```python
# test settings or per-test via pytest-django `settings` fixture
PAYMENT_GATEWAY = "apps.payments.gateways.fake.FakePaymentGateway"

def test_refund_uses_configured_gateway(db, settings):
    settings.PAYMENT_GATEWAY = "apps.payments.gateways.fake.FakePaymentGateway"
    ...
```

If the factory caches the instance (`functools.cache`), clear it in a fixture — cached gateways leak between tests.

The fake lives in production code (`gateways/fake.py`) or a shared `tests/fakes.py`, and has its **own contract tests** run against both fake and real (sandbox) implementations so the fake can't drift:

```python
@pytest.fixture(params=["fake", pytest.param("sandbox", marks=pytest.mark.external)])
def gateway(request):
    return FakePaymentGateway() if request.param == "fake" else StripeGateway(api_key=SANDBOX_KEY)

def test_refund_of_unknown_charge_raises(gateway):
    with pytest.raises(ChargeNotFound):
        gateway.refund("ch_does_not_exist")
```

Mocks do not validate the real integration; the `external` suite does (→ [integration-and-background-task-testing.md](integration-and-background-task-testing.md)).

### Domain events: in-memory bus fake

```python
# conftest.py
@pytest.fixture
def event_bus(monkeypatch):
    bus = InMemoryEventBus()
    monkeypatch.setattr("apps.platform.events.bus", bus)  # or settings-driven
    return bus
```

Assert three things: published **after commit** (inside capture block with `execute=True`), **not** published on rollback, and payload carries IDs not model instances. Subscriber logic is tested separately by calling the handler with an event object. Design → [domain-events.md](../../django-architecture/references/domain-events.md).

### Testing app public interfaces (modular monolith)

Tests for cross-app collaboration go through the owning app's public API (`apps.inventory.api`), not its models:

```python
@pytest.mark.django_db
def test_orders_uses_inventory_public_api_contract():
    product = inventory_api.create_product_for_tests(sku="SKU-1", quantity=3)  # or factory exported by inventory
    reservation = inventory_api.reserve(sku="SKU-1", quantity=2, reference="ORD-1")
    assert reservation.remaining == 1
    with pytest.raises(inventory_api.OutOfStock):
        inventory_api.reserve(sku="SKU-1", quantity=5, reference="ORD-2")
```

This is a **provider contract test**: owned by the inventory app, describing what consumers rely on. Consumers (orders) test against it or a fake honouring the same contract.

### Architecture fitness tests

import-linter **[third-party: import-linter]** (`pyproject.toml`):

```toml
[tool.importlinter]
root_packages = ["apps"]

[[tool.importlinter.contracts]]
name = "Layers"
type = "layers"
layers = ["apps.api", "apps.orders | apps.billing", "apps.inventory", "apps.platform"]

[[tool.importlinter.contracts]]
name = "Only public interfaces cross apps"
type = "forbidden"
source_modules = ["apps.orders"]
forbidden_modules = ["apps.billing.models", "apps.billing.services"]
```

Run `lint-imports` in CI. Without the library, a lightweight pytest check using `ast`:

```python
import ast, pathlib

PRIVATE = ("models", "services", "selectors")

def test_apps_only_import_other_apps_public_api():
    violations = []
    for path in pathlib.Path("apps").rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        owner = path.parts[1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("apps."):
                parts = node.module.split(".")
                if parts[1] != owner and len(parts) > 2 and parts[2] in PRIVATE:
                    violations.append(f"{path}: {node.module}")
    assert violations == []
```

Known limitation: misses dynamic imports and string model references (`"billing.Invoice"` FKs), which are legitimate.

## 6. Production use cases

- **Checkout:** `place_order` tested for totals, stock reservation, all-or-nothing on payment decline, single `order.placed` event after commit.
- **Refunds:** fake gateway records refunds; contract tests run nightly against the sandbox.
- **Pricing engine:** 40-row parametrised rule table maintained with product owners.
- **Monolith split preparation:** import-linter contracts added before extracting `billing`; violations become the migration backlog.
- **Multi-tenant selectors:** every selector tested with a second tenant's rows present.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Real-DB service tests | High confidence; refactor-safe | Slower than pure unit tests |
| Pure rule tables | Very fast, exhaustive | Needs rules extracted from ORM code |
| Fakes | Realistic, reusable, readable | Must be maintained; can drift without contract tests |
| Mocks | Zero setup | Couple to call shape; false confidence |
| import-linter | Cheap, catches transitive violations | Config upkeep; static only |

## 8. Security and performance considerations

- Authorisation enforced in services/selectors must be tested *there* (actor without permission → domain exception, other tenant → not found), not only at the API. Matrix patterns → [security-testing.md](security-testing.md).
- Fakes must not weaken security semantics (a fake gateway that accepts any webhook signature hides bugs).
- Service tests dominate suite time; use `build()` for inputs that don't need persistence, share read-only reference data via `setUpTestData`/scoped fixtures, and avoid `transaction=True`.

## 9. Common mistakes and anti-patterns

- Patching the service's own helpers (`patch("apps.orders.services._calculate_total")`) — tests the wiring, not the behaviour.
- Mocking `Order.objects.filter` — the test passes while the query is wrong.
- Testing services via the API only: a permission or serializer change hides service bugs, and each test is 3–5× slower.
- Happy-path only; no test that a mid-service failure leaves no partial state.
- Asserting `on_commit` side effects without capture → test passes because nothing runs, including on the success path.
- `assert_called()` without arguments — proves nothing about what was sent.
- Fakes with no contract tests; fakes living in one test module and copied around.
- Architecture rules documented but not executed.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| Event/task never observed | `on_commit` not executed under rollback isolation | `django_capture_on_commit_callbacks(execute=True)` |
| Event observed on failure path | Event published directly instead of via `on_commit`, or inside a savepoint not rolled back | Fix service; assert `callbacks == []` |
| Patch has no effect | Patched definition module instead of lookup module | Patch where used, or inject |
| Fake state leaks between tests | Module-level/cached gateway | Function-scoped fixture; clear caches |
| Partial writes after exception | Service lacks `atomic()` or catches inside it | Fix service boundary (django-architecture) |
| import-linter passes but app imports models | Contract doesn't list module, or `allow_indirect_imports` | Tighten contract |

Validation: every public service function has success + each domain-exception path; every selector has an other-actor exclusion and a query budget; fitness tests run in CI.

## 11. Related references and skills

- Siblings: [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) · [database-and-transaction-testing.md](database-and-transaction-testing.md) · [integration-and-background-task-testing.md](integration-and-background-task-testing.md) · [fixtures-and-test-data.md](fixtures-and-test-data.md) · [testing-fundamentals.md](testing-fundamentals.md)
- django-architecture: [service-layer.md](../../django-architecture/references/service-layer.md) · [domain-events.md](../../django-architecture/references/domain-events.md) · [dependency-management.md](../../django-architecture/references/dependency-management.md) · [modular-monolith.md](../../django-architecture/references/modular-monolith.md) · [django-application-boundaries.md](../../django-architecture/references/django-application-boundaries.md)
- django-orm: [transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md)

## 12. Official documentation and external references

- Django — Testing tools, `captureOnCommitCallbacks`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#django.test.TestCase.captureOnCommitCallbacks>
- Django — Database transactions: <https://docs.djangoproject.com/en/5.2/topics/db/transactions/>
- pytest — Parametrizing tests: <https://docs.pytest.org/en/stable/how-to/parametrize.html>
- pytest-django — Helpers (`django_capture_on_commit_callbacks`, `settings`): <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- Python — `unittest.mock` autospeccing: <https://docs.python.org/3/library/unittest.mock.html#autospeccing>
- import-linter (third-party): <https://import-linter.readthedocs.io/>
- Martin Fowler — Mocks Aren't Stubs: <https://martinfowler.com/articles/mocksArentStubs.html>
