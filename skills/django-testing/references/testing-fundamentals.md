# Testing fundamentals and strategy

> **Scope:** The vocabulary, principles and decision method for testing Django applications —
> test levels, isolation, determinism, risk-based selection, what (not) to test, organisation and
> naming, and how to write a feature test strategy. Other references in this skill use the terms
> defined here.
>
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · **Depends on:** **[Django]**
> test framework, **[pytest]** + **[third-party: pytest-django]** (examples), **[third-party:
> factory_boy]** (examples only).

## 1. Purpose and scope

Use this reference when deciding *what* to test and *at which level*, reviewing whether a change
is adequately tested, or agreeing team conventions. It owns:

- Test levels as they apply to Django (unit, integration, functional/API, end-to-end).
- Isolation and determinism as engineering properties, and the sources that break them.
- Risk-based test selection and depth.
- What should and should not be tested.
- Test organisation, file layout and naming.
- The structure of a single test and of a feature test strategy.

It does **not** own framework APIs (see [django-testing-framework.md](django-testing-framework.md),
[pytest-and-pytest-django.md](pytest-and-pytest-django.md)), test data
([fixtures-and-test-data.md](fixtures-and-test-data.md)) or component-specific techniques (the
owner references in [SKILL.md](../SKILL.md)).

## 2. Core concepts

### Test levels in Django terms

Generic definitions ("a unit test touches no database") fit Django poorly: the ORM *is* the
data-access layer, and mocking it produces tests that verify the mock. Use these definitions:

| Level | What it exercises | Typical Django tools | Speed |
|---|---|---|---|
| **Unit** | One function/class/model method with its real collaborators *inside the process*, possibly including the test database | Plain function call, `SimpleTestCase`, `TestCase`/`django_db`, `RequestFactory` | ms |
| **Integration** | Several components wired together: service + ORM + constraints, view + middleware + URL routing, task + broker adapter | `Client`, `APIClient`, `TestCase`, transport-level HTTP mocks | 10–100 ms |
| **Functional / API** | A use case through its public entry point (HTTP endpoint, management command, task) asserting externally visible behaviour | `Client`/`APIClient`, `call_command`, task `.apply()` | 10–200 ms |
| **End-to-end (E2E)** | Real browser or real deployed stack, multiple processes | Playwright/Selenium + `StaticLiveServerTestCase` | seconds |
| **Contract** | Agreement between this system and another (API schema, third-party API shape) | Schema diff, sandbox tests, consumer-driven contracts | varies |

A "unit" test of `Order.total()` that creates rows in the test DB is still a unit test: the unit
is the behaviour, the database is a real collaborator. What makes a test **not** a unit test is
crossing a process boundary (network, browser, another service) or exercising several entry-point
layers at once.

### Pyramid vs trophy

- **Pyramid**: many unit tests, fewer integration tests, few E2E. Optimises speed and failure
  localisation.
- **Trophy** (static analysis at the base, integration-heavy middle): optimises confidence per
  test when units are thin glue.

For a typical Django service-layer codebase the effective shape is:

```text
          ▲  E2E (browser)            5–20 critical journeys
         ▲▲▲ API / view tests          every endpoint × (happy, invalid, authz)
       ▲▲▲▲▲▲ service + selector tests  every business rule and query scope (real DB)
     ▲▲▲▲▲▲▲▲ model / pure-function tests  invariants, calculations, constraints
   ▀▀▀▀▀▀▀▀▀▀ static analysis: ruff, mypy/django-stubs, makemigrations --check
```

Rule: push each assertion down to the **cheapest level that can catch the bug**. A tax-rounding
rule is proven in a service test, not by posting forms through a browser. An auth-decorator
omission is only caught at the view/API level — test it there.

### Properties of a good test

| Property | Meaning | Broken by |
|---|---|---|
| **Isolated** | Outcome independent of other tests and execution order | Shared rows, module globals, cache, settings mutation, class attributes |
| **Deterministic** | Same code → same result, every run | Wall clock, unseeded randomness, unordered querysets, threads, network |
| **Behavioural** | Asserts what callers observe | Asserting private calls, SQL text, internal attributes |
| **Focused** | One reason to fail | Several behaviours per test, assertions on incidental data |
| **Fast** | Cheap enough to run on every save | Unnecessary transactional DB, real I/O, oversized fixtures |
| **Readable** | Failure message explains the regression | Magic data, deep fixture chains, helper indirection |

### Test boundaries and dependencies

Classify every dependency of the code under test:

| Dependency | In tests | Why |
|---|---|---|
| Your own code in the same app | Real | Mocking it tests the wiring of mocks |
| ORM + database | Real test DB (production engine in CI) | Constraints, transactions and query behaviour are part of the behaviour |
| Another app's public interface | Real by default; fake only if expensive or non-deterministic | Boundaries are verified, not assumed |
| Cache, email, storage | Django's test backends (locmem, in-memory) | Built-in fakes, deterministic |
| HTTP APIs, cloud SDKs, payment providers | Transport-level mock or fake gateway; separate contract/sandbox tests | Network is slow, flaky, costs money |
| Time, randomness, UUIDs | Injected clock / frozen time / seeded RNG | Determinism |
| Broker, Celery workers | Call the task directly; capture enqueues | Avoid process boundaries in unit tests |

Details: [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md),
[integration-and-background-task-testing.md](integration-and-background-task-testing.md).

## 3. Internal mechanics

### Why tests become nondeterministic in Django

| Source | Mechanism | Prevention |
|---|---|---|
| Unordered querysets | SQL without `ORDER BY` has no defined order; PostgreSQL order changes after updates/vacuum | `order_by()` in code or compare sets |
| Sequence-dependent IDs | Primary keys continue across tests (sequences are not rolled back) | Never hard-code `pk=1`; use the created object's `pk` |
| Wall-clock time | `timezone.now()` crosses midnight, month-end, DST during the run | Freeze time or inject a clock |
| Randomness | Faker / `random` / `uuid4` without seed | Seed per test, assert on properties not values |
| Cache state | locmem cache persists across tests in one process | `cache.clear()` in an autouse fixture |
| Settings mutation | Direct assignment to `settings.X` leaks | `override_settings` / pytest-django `settings` fixture |
| Module-level state | Registries, memoised clients, `lru_cache` | Reset in fixture teardown; inject instead of memoise |
| Threads / async | Scheduling order, separate DB connections | Explicit synchronisation; transactional tests (see [database-and-transaction-testing.md](database-and-transaction-testing.md)) |
| External services | Network latency, rate limits, data changes | Mock at transport; contract suite separately |
| Test order | Hidden dependency on data from an earlier test | Random order in CI (`--shuffle` / pytest-randomly) |

### How the cost of a test grows

Cost per test is dominated by: process boundaries (browser ≫ HTTP ≫ in-process), database
strategy (truncation ≫ rollback ≫ none), and data volume created per test. A suite of 3,000 tests
at 50 ms is 2.5 minutes serial; the same suite where 10 % use `TransactionTestCase` at 400 ms
adds 2 minutes. Level selection is therefore a performance decision as well as a confidence one
(see [performance-and-reliability-testing.md](performance-and-reliability-testing.md)).

## 4. Engineering decisions and trade-offs

### Risk-based test depth

Score each behaviour by **likelihood of defect** (complexity, change frequency, past bugs,
concurrency, external dependencies) and **impact** (money, security, data loss, legal, user
reach). Depth follows the product.

| | Low impact | Medium impact | High impact (money, auth, PII, data integrity) |
|---|---|---|---|
| **High likelihood** | Happy path + main edge cases | Full equivalence classes + boundaries | Exhaustive rules, concurrency, failure injection, E2E journey, mutation testing on the module |
| **Medium likelihood** | Happy path | Happy + key failure paths | Full rules + authz matrix + regression suite |
| **Low likelihood** | Smoke via higher-level test | Happy path + one failure | Rules + authz + a contract test |

Examples: payment capture and refunds → high/high; tenant scoping on every list endpoint → high
impact regardless of likelihood; an internal admin report's column labels → low/low (a smoke
test that the page renders is enough).

### What to test

- Business rules and invariants (pricing, state machines, eligibility, quotas).
- Every authorisation decision, including the negative case (other user, other tenant).
- Input validation at each entry point (forms, serializers, command arguments, webhook payloads).
- Database constraints you rely on (prove they reject bad data).
- Query scoping and query counts on hot paths.
- Integration failure modes: timeouts, 5xx, malformed payloads, retries, duplicates.
- Transaction behaviour: rollback on failure, side effects only after commit.
- Migrations that transform data.
- Every bug that reached production (regression test).

### What not to test

| Don't test | Instead |
|---|---|
| Django/DRF behaviour itself (`CharField` max length, `ModelSerializer` field mapping) | Test *your* configuration only where it encodes a rule (e.g. a field is read-only) |
| Auto-generated admin pages one by one | A single smoke test that registered admin changelists render; test custom admin actions |
| Trivial properties and `__str__` without logic | Nothing, or covered incidentally |
| Private helpers | Test through the public function that uses them |
| Exact SQL strings | Query counts and results |
| Third-party library internals | Your adapter's behaviour against a fake or recorded response |
| Framework-generated migrations | `makemigrations --check` in CI; test only data migrations |

### Level selection trade-offs

| Choice | Gain | Cost |
|---|---|---|
| More unit/service tests | Fast, precise failure location | Can miss wiring (URL, permission class, middleware) |
| More API tests | Catch wiring and contract regressions | Slower; failures less localised |
| E2E tests | Prove real user journeys including JS | Slow, flaky, expensive to maintain |
| Mocks for collaborators | Speed, isolation | Tests coupled to implementation; false confidence |
| Real DB everywhere | Realistic behaviour | Slower than pure unit tests; needs DB in CI |

## 5. Django implementation patterns

### Test organisation

Two viable layouts; pick one per repository.

```text
# A) Tests inside each app — preferred for modular monoliths (ownership travels with the app)
apps/orders/
├── models.py, services.py, selectors.py, api.py
└── tests/
    ├── __init__.py
    ├── factories.py
    ├── test_models.py        # invariants, model methods, constraints
    ├── test_services.py      # use cases, domain errors, transactions
    ├── test_selectors.py     # scoping, ordering, query counts
    ├── test_api.py           # HTTP contract, permissions
    └── test_tasks.py
conftest.py                    # project-wide fixtures (clients, users, autouse resets)

# B) Top-level tests/ mirroring the package tree — preferred when apps are thin or packaged separately
tests/
├── conftest.py
├── orders/test_services.py
└── e2e/test_checkout_journey.py
```

Keep E2E and slow suites in a separately markable location (`tests/e2e/`, `@pytest.mark.e2e`)
so CI can split them ([ci-cd-test-automation.md](ci-cd-test-automation.md)).

### Naming

- Files: `test_<component>.py`. Classes group one unit: `TestPlaceOrder`, `TestOrderListAPI`.
- Functions describe behaviour and condition: `test_<behaviour>_when_<condition>` or
  `test_<outcome>_for_<case>`.

```python
# Good — the failure line is a specification
def test_place_order_rejects_quantity_above_stock(): ...
def test_order_list_excludes_other_tenants_orders(): ...
def test_refund_is_idempotent_when_webhook_redelivered(): ...

# Bad — says nothing when it fails
def test_order(): ...
def test_api_2(): ...
def test_place_order_works(): ...
```

### Structure of one test: Arrange–Act–Assert

```python
import pytest
from decimal import Decimal

from apps.orders.services import place_order, OutOfStock
from apps.orders.tests.factories import CustomerFactory, ProductFactory


@pytest.mark.django_db
def test_place_order_rejects_quantity_above_stock():
    # Arrange — only the data this rule depends on
    customer = CustomerFactory()
    product = ProductFactory(stock=2, price=Decimal("10.00"))

    # Act — exactly one call to the behaviour under test
    with pytest.raises(OutOfStock) as exc:
        place_order(customer=customer, lines=[(product.pk, 3)])

    # Assert — the outcome and the absence of side effects
    assert exc.value.product_id == product.pk
    product.refresh_from_db()
    assert product.stock == 2
    assert not customer.orders.exists()
```

One behaviour per test does not mean one `assert`: asserting the return value *and* the DB effect
of the same behaviour is one reason to fail. Two different acts in one test are two tests.

### Parametrise equivalence classes and boundaries

```python
@pytest.mark.parametrize(
    "subtotal, expected_shipping",
    [
        (Decimal("0.01"), Decimal("4.95")),    # lower bound
        (Decimal("49.99"), Decimal("4.95")),   # just below threshold
        (Decimal("50.00"), Decimal("0.00")),   # boundary: free shipping
        (Decimal("10000"), Decimal("0.00")),   # large
    ],
    ids=["min", "below-threshold", "at-threshold", "large"],
)
def test_shipping_fee(subtotal, expected_shipping):
    assert shipping_fee(subtotal) == expected_shipping
```

### Feature test strategy template

Write this (in the PR description or a short doc) for any non-trivial feature:

```markdown
### Test strategy: <feature>
**Acceptance criteria** → AC1 … ACn
**Risk**: impact <H/M/L> (why), likelihood <H/M/L> (why)
**Business rules** → test level and file
| Rule / AC | Level | Test |
|---|---|---|
| Quantity ≤ stock | service | test_services.py::test_place_order_rejects_quantity_above_stock |
| Only owner sees order | API | test_api.py::test_retrieve_other_customers_order_returns_404 |
**Failure paths**: invalid input, unauthorised, other tenant, provider timeout, duplicate request
**Data**: factories used / new traits needed
**Dependencies replaced**: payment gateway (fake), email (locmem)
**Not tested (and why)**: admin labels — low risk
**Non-functional**: query budget for list endpoint ≤ 4; migration tested forward
```

### Regression test per bug

1. Reproduce the bug as a failing test at the lowest level that shows it.
2. Name it after the behaviour, reference the issue in a comment, not in the name.
3. Fix; confirm the test fails on the old code (stash the fix and run it once).

```python
def test_invoice_total_rounds_half_up_per_line():
    # Regression: ISSUE-1423 — totals rounded once at the end produced 0.01 drift.
    ...
```

## 6. Production use cases

- **Checkout with payments**: high/high risk. Service tests for pricing, stock and idempotency;
  API tests for authz and validation; a fake payment gateway; one sandbox contract test; one
  browser journey for the checkout page.
- **Multi-tenant SaaS list endpoints**: every list/detail endpoint has a "other tenant's object
  is invisible" test; selectors have scoping tests; parametrised role matrix.
- **Internal back-office report**: selector test for the aggregation, query-count assertion, one
  view smoke test. No E2E.
- **Data migration splitting a field**: migration test from the previous node with historical
  models ([model-and-orm-testing.md](model-and-orm-testing.md)).

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Risk-based depth | Effort goes where defects cost most | Requires judgement; must be revisited as risk changes |
| Behavioural assertions | Refactor-safe tests | Sometimes need more setup than asserting a call |
| Real DB in "unit" tests | Catches constraint, query and transaction bugs | Needs DB infrastructure; slower than pure functions |
| Tests inside apps | Ownership and deletion travel with code | Cross-app journeys need a home (`tests/e2e/`) |
| Strategy document per feature | Reviewable intent, fewer gaps | Overhead for small changes — skip for trivial ones |

## 8. Security and performance considerations

- Authorisation is high impact by definition: negative tests (anonymous, wrong role, other
  owner/tenant) are mandatory regardless of likelihood — see [security-testing.md](security-testing.md).
- Test data must never contain real personal data, production dumps or live credentials; use
  `example.test` domains and generated values.
- Test settings must not reach shared or production resources (DB, buckets, SMTP, APIs).
- Level choice dominates suite time: prefer rollback-isolated DB tests, avoid E2E for rules that
  a service test can prove, and keep per-test data minimal.

## 9. Common mistakes and anti-patterns

| Anti-pattern | Why it hurts | Fix |
|---|---|---|
| Only happy-path tests | Most production incidents are failure paths | Enumerate failure paths in the strategy |
| `assert response.status_code == 200` alone | Passes while the payload or DB effect is wrong | Assert key fields and the side effect |
| Mocking the ORM or your own service | Tests the mocks; misses SQL and constraint bugs | Real test DB; mock only process boundaries |
| Ice-cream cone (mostly E2E) | Slow, flaky, poor failure localisation | Push assertions down a level |
| Tests named after methods (`test_save`) | No specification value | Name the behaviour and condition |
| Shared fixtures mutated by tests | Order-dependent failures | Fresh data per test via factories |
| Testing private helpers | Locks in implementation | Test via the public behaviour |
| Coverage-driven tests without assertions | Inflated coverage, no protection | Review assertions, use mutation testing selectively |
| Several acts per test | Unclear which behaviour broke | Split tests |
| Hard-coded IDs / ordering | Fails with sequences, parallelism, PostgreSQL | Use created objects; sets or explicit `order_by` |

## 10. Troubleshooting and validation

Review questions for a change's tests:

- [ ] Does each acceptance criterion map to a named test at the cheapest effective level?
- [ ] Is each failure path (invalid, unauthenticated, forbidden, other tenant, dependency failure,
      duplicate) present for high-impact behaviour?
- [ ] Would the test fail if the behaviour regressed? (Mentally revert the fix, or run it.)
- [ ] Does any test assert internals (call order, private attributes, SQL text)?
- [ ] Does any test depend on order, time, randomness or network?
- [ ] Does the test name read as a specification?

Symptoms and where to look:

| Symptom | Likely cause | Reference |
|---|---|---|
| Passes alone, fails in suite | Shared state leak | [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |
| Fails around midnight / month-end | Wall-clock dependence | [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) |
| High coverage, bugs still escape | Weak assertions, missing failure paths | [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) |
| Suite too slow to run locally | Wrong level / transactional DB overuse | [performance-and-reliability-testing.md](performance-and-reliability-testing.md) |

## 11. Related references and skills

- [django-testing-framework.md](django-testing-framework.md) and
  [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — the runners and test classes.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — building the "Arrange" step.
- [service-and-architecture-testing.md](service-and-architecture-testing.md) — where most rules are proven.
- [security-testing.md](security-testing.md) — mandatory negative tests.
- [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) — measuring gaps.
- [django-architecture: service-layer](../../django-architecture/references/service-layer.md) —
  why services/selectors are the main unit of test.
- [django-architecture: architecture-fundamentals](../../django-architecture/references/architecture-fundamentals.md) —
  coupling and cohesion that determine testability.
- [django-security-engineering: security-testing-and-incident-response](../../django-security-engineering/references/security-testing-and-incident-response.md).

## 12. Official documentation and external references

- Django — Writing and running tests: <https://docs.djangoproject.com/en/5.2/topics/testing/overview/>
- Django — Testing tools: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- pytest — Good integration practices (layout): <https://docs.pytest.org/en/stable/explanation/goodpractices.html>
- pytest — Anatomy of a test: <https://docs.pytest.org/en/stable/explanation/anatomy.html>
- pytest-django: <https://pytest-django.readthedocs.io/en/latest/>
- Martin Fowler — The Practical Test Pyramid: <https://martinfowler.com/articles/practical-test-pyramid.html>
- Martin Fowler — Eradicating Non-Determinism in Tests: <https://martinfowler.com/articles/nonDeterminism.html>
- Kent C. Dodds — The Testing Trophy: <https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications>
