# Performance and reliability testing

> **Scope:** Using the test suite to guard performance (query-count budgets, N+1 detection,
> micro-benchmarks) and keeping the suite itself fast and trustworthy (profiling slow tests,
> speed-ups, flaky-test detection and quarantine). Also defines where unit tests stop and
> dedicated load/performance tooling must take over.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x ·
> **Depends on:** **[Django]** `assertNumQueries`, `CaptureQueriesContext`, `--durations` (≥ 5.0) ·
> **[pytest]** `--durations`, markers · **[pytest-django]** `django_assert_num_queries`,
> `django_assert_max_num_queries` · optional **[third-party: pytest-xdist, pytest-rerunfailures,
> pytest-repeat, pytest-flakefinder, pytest-benchmark, django-zeal, pyinstrument]**

## 1. Purpose and scope

Two jobs share this reference because they share tooling and mindset (measure, budget, alert):

1. **Performance guards inside the suite** — query-count assertions, N+1 detection, a small
   number of benchmark tests for hot pure-Python code.
2. **Suite health** — how fast it runs, which tests are slow, which are flaky, and the policy for
   handling them.

Out of scope: *fixing* N+1 queries (`select_related`, `Prefetch`) →
[django-orm query-optimization.md](../../django-orm/references/query-optimization.md);
production profiling and load testing → [django-performance](../../django-performance/SKILL.md);
root-cause debugging of a specific flaky test →
[test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md); CI
parallelisation and sharding → [ci-cd-test-automation.md](ci-cd-test-automation.md).

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Query-count assertion | Fails when a block issues a different (or larger) number of SQL queries than expected. Deterministic, cheap, catches N+1 regressions. |
| Query budget | A documented maximum per endpoint/selector, asserted in tests ("order list ≤ 4 queries regardless of page size"). |
| Scaling test | Assert the count is the same for N=1 and N=10 rows — the real signature of "no N+1", independent of the exact number. |
| Wall-clock assertion | Asserting elapsed time. Almost always flaky in shared CI; avoid in the main suite. |
| Benchmark | Repeated timing with statistics (pytest-benchmark); compared against a stored baseline, not an absolute. |
| Flaky test | Passes and fails on the same code. A symptom of nondeterminism in the test or a real race in the code. |
| Quarantine | Moving a known-flaky test out of the blocking gate, with an owner and expiry, while it is fixed. |
| Flake rate | Failures-that-pass-on-retry ÷ runs, per test, tracked over time. |

Provenance of the query tools:

| Tool | Provenance | Notes |
|---|---|---|
| `self.assertNumQueries(n, using="default")` | **[Django]** `TransactionTestCase` | Exact count; context manager or callable form |
| `CaptureQueriesContext(connection)` | **[Django]** `django.test.utils` | Records `captured_queries` (SQL + time); works outside test classes |
| `django_assert_num_queries(n)` | **[pytest-django]** | Exact; `exact=False` makes it a maximum; failure message lists the SQL |
| `django_assert_max_num_queries(n)` | **[pytest-django]** | Upper bound |
| django-zeal | **[third-party: django-zeal]** | Raises on N+1 patterns at runtime in tests/dev |
| nplusone | **[third-party: nplusone]** | Older N+1 detector; appears unmaintained (`VERIFY` before adopting) |

## 3. Internal mechanics

- Django records queries in `connection.queries_log` only when `DEBUG=True` **or** when a
  `CaptureQueriesContext` forces debug cursors (`connection.force_debug_cursor = True`). The test
  runner sets `DEBUG=False`, so query capture works only through these context managers.
- Each context manager counts queries on **one connection alias**. Queries to a replica or second
  database need `using="replica"` (Django) or a separate `CaptureQueriesContext(connections["replica"])`.
- Counts include savepoint statements only if they are executed through the cursor wrapper —
  `SAVEPOINT`/`RELEASE` from nested `atomic()` blocks **are** counted, which surprises people
  when a service adds `transaction.atomic()` (`VERIFY` per backend).
- Query results cached by the QuerySet result cache, `cached_property` or the cache framework do
  not hit the DB — a test that warms caches before the assertion will under-count. Use a clean
  locmem cache per test.
- `--durations=N` (**[pytest]**; **[Django]** runner ≥ 5.0 on Python ≥ 3.12) reports setup, call
  and teardown separately — slow **setup** usually means fixture/factory cost, slow **teardown**
  usually means `TransactionTestCase` truncation.
- pytest-rerunfailures re-executes a failing test in the same process; fixtures with broader scope
  are not recreated, so a rerun can pass because of state left by the first attempt — reruns are
  diagnostic signals, not proofs.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Avoid | Why |
|---|---|---|---|
| Exact vs max count | Exact for selectors/hot endpoints; max for broad flows | Max everywhere | Exact counts catch accidental *extra* queries and signal when a refactor changed loading strategy; max tolerates harmless changes |
| Absolute number vs scaling test | Both: scaling test proves no N+1; exact number documents the budget | Only absolute | An absolute count can be "correct" for 1 row while N+1 exists |
| Runtime N+1 detector (django-zeal) | As an extra guard in CI for the whole API suite | As the only guard | Catches unknown paths, but not every pattern and adds dependency |
| Timing assertions | Benchmarks in a separate, non-blocking job with a baseline | `assert elapsed < 0.2` in unit tests | CI machines vary 2–5× |
| Flaky handling | Detect → quarantine with owner/expiry → fix root cause | Global `--reruns 3` in the gate | Global reruns hide real races and double runtime of failing suites |
| `--no-migrations` | Local fast loops only, if models are simple | In CI | Skips data migrations, `RunSQL`, extensions, and migration correctness |
| Parallelism (xdist) | Once tests are isolated | As a fix for slowness of a non-isolated suite | Parallelism exposes shared-state bugs; see troubleshooting |

## 5. Django implementation patterns

### Query budget + scaling test for a selector

```python
# apps/orders/tests/test_selectors.py
import pytest

from apps.orders.selectors import order_list_for_user


@pytest.mark.django_db
@pytest.mark.parametrize("n_orders", [1, 10])
def test_order_list_query_count_is_constant(django_assert_num_queries, user, n_orders):
    OrderFactory.create_batch(n_orders, customer__user=user, lines=3)   # trait creating lines
    with django_assert_num_queries(2):          # orders (+customer via JOIN), prefetched lines
        orders = list(order_list_for_user(user=user))
        for order in orders:                     # touch everything the caller touches
            _ = order.customer.name
            _ = [line.product_id for line in order.lines.all()]
```

Iterate the attributes the *real caller* uses (serializer fields, template loops) inside the
block — otherwise lazy access after the block escapes the count.

### API endpoint budget (DRF)

```python
@pytest.mark.django_db
def test_order_list_endpoint_budget(auth_client, user, django_assert_max_num_queries):
    OrderFactory.create_batch(25, customer__user=user, lines=2)
    with django_assert_max_num_queries(6):       # session/auth + count + page + prefetches
        response = auth_client.get("/api/orders/?page_size=25")
    assert response.status_code == 200
```

Endpoint counts include authentication, session and permission queries — use a max here and an
exact count in the selector test.

### Inspecting the SQL when a count fails

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext


def test_debug_order_detail_queries(auth_client, order):
    with CaptureQueriesContext(connection) as ctx:
        auth_client.get(f"/api/orders/{order.pk}/")
    for q in ctx.captured_queries:
        print(q["time"], q["sql"])               # run with -s; delete before committing
```

pytest-django's failure message already lists captured queries; `CaptureQueriesContext` is for
asserting on SQL content (e.g. "no query touches `audit_log`") or debugging.

### Django `TestCase` equivalent

```python
class OrderSelectorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()
        OrderFactory.create_batch(10, customer__user=cls.user, lines=3)

    def test_query_count(self):
        with self.assertNumQueries(2):
            list(order_list_for_user(user=self.user))
```

### Runtime N+1 guard **[third-party: django-zeal]**

```python
# conftest.py — only if django-zeal is installed; check its docs for the current API (VERIFY)
import pytest
from zeal import zeal_context

@pytest.fixture(autouse=True)
def _detect_n_plus_one():
    with zeal_context():
        yield
```

Scope it to API/view tests (e.g. in `apps/*/tests/api/conftest.py`) to keep noise down, and
allow-list intentional patterns explicitly.

### Finding slow tests

```bash
pytest --durations=25 --durations-min=0.5          # pytest
python manage.py test --durations 25               # Django runner, Django >= 5.0, Python >= 3.12
pytest apps/reports/tests/test_exports.py::test_big_export --profile   # needs pytest-profiling (third-party)
pyinstrument -m pytest apps/reports/tests/test_exports.py::test_big_export   # third-party: pyinstrument
```

Mark legitimately slow tests and separate them in CI ([ci-cd-test-automation.md](ci-cd-test-automation.md)):

```toml
[tool.pytest.ini_options]
addopts = "--strict-markers"
markers = [
  "slow: > 1 s; excluded from the pre-commit/fast job",
  "quarantine: known flaky; owner + issue required in reason",
]
```

```bash
pytest -m "not slow and not quarantine"     # PR fast gate
pytest -m slow                               # separate job
```

### Suite speed-ups (ordered by typical payoff)

| Speed-up | Provenance | Effect | Caveat |
|---|---|---|---|
| `PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]` in test settings | **[Django]** | Factories with passwords go from ~100 ms to ~0 | Never in non-test settings; keep one test of real hashing config |
| Reuse the test DB: `--reuse-db` / `--keepdb` | **[pytest-django]** / **[Django]** | Skips create + migrate | Use `--create-db` after migration changes; CI usually creates fresh |
| `TestCase`/`django_db` instead of `TransactionTestCase`/`transaction=True` | **[Django]**/**[pytest-django]** | Rollback vs truncate: often 10× | Only where real commits aren't needed |
| `setUpTestData` / class-scoped data | **[Django]** | Builds shared read-only data once per class | Objects must be deepcopy-able (Django ≥ 3.2) |
| `factory.build()` / `baker.prepare()` for non-DB tests | **[third-party]** | No INSERTs | Can't test DB behaviour |
| locmem cache and email; dummy/in-memory storage | **[Django]** | No network | Clear cache per test |
| Disable debug toolbar, silk, verbose logging in test settings | — | Removes middleware and I/O | — |
| `pytest -n auto` | **[third-party: pytest-xdist]** | Near-linear on cores | Requires isolation; DB per worker |
| `--no-migrations` | **[pytest-django]** | Skips migrations | Not in CI (see §4) |
| PostgreSQL CI tuning (`fsync=off`, `synchronous_commit=off`) | PostgreSQL | Faster commits | Throwaway CI DB only → [postgresql-engineering](../../postgresql-engineering/SKILL.md) |

### Flaky-test detection

```bash
# Re-run a suspect test many times, in random order
pytest apps/billing/tests/test_invoices.py --count 200 -x   # pytest-repeat (third-party); pytest-randomly, if installed, also shuffles order
pytest --flake-finder --flake-runs 100 apps/billing/tests/             # pytest-flakefinder (third-party)

# Nightly detection job (non-blocking): rerun failures and report which passed on retry
pytest --reruns 2 --junitxml=reports/nightly.xml                        # pytest-rerunfailures (third-party)
```

A test that passed only after a rerun is **flaky**, even though the job is green. Parse the JUnit
report (rerun entries) or the plugin's summary, and record flake rates per test node id.

### Quarantine policy

```python
@pytest.mark.quarantine(reason="BILL-482 race in invoice numbering; owner @billing-team; expires 2026-12-01")
@pytest.mark.django_db(transaction=True)
def test_concurrent_invoice_numbers_unique(): ...
```

Rules: quarantined tests still run in a non-blocking job; each has an issue, owner and expiry;
a CI check fails when an expiry has passed; the quarantine list size is a tracked metric. A
quarantined **security or data-integrity** test blocks release until fixed.

### Performance regression with benchmarks **[third-party: pytest-benchmark]**

```python
def test_price_engine_benchmark(benchmark):
    basket = build_basket(lines=200)            # pure Python; no DB
    result = benchmark(price_basket, basket)
    assert result.total > 0
```

```bash
pytest benchmarks/ --benchmark-autosave                              # on main: store baseline
pytest benchmarks/ --benchmark-compare --benchmark-compare-fail=mean:15%   # on PR: relative threshold
```

Run on dedicated or at least consistent runners; compare to a baseline from the same machine
class; use relative thresholds (10–20 %) and repeat before failing. Benchmark CPU-bound domain
code, not views or DB queries — those are dominated by I/O variance.

### Where unit tests stop

| Question | Tool | Environment |
|---|---|---|
| Does this endpoint issue N+1 queries? | Query-count tests | Main suite |
| Did this function get 30 % slower? | pytest-benchmark | Separate CI job |
| p95 latency at 200 RPS? Connection pool limits? Lock contention under load? | Locust / k6 **[third-party]** | Staging-like environment with production-sized data |
| Which query is slow in production? | APM, `pg_stat_statements`, `EXPLAIN` | Production / replica |

→ [django-performance](../../django-performance/SKILL.md).

## 6. Production use cases

- **Order list regression**: a new serializer field `customer.loyalty_tier` added one query per
  row. The scaling test (N=1 vs N=10) failed in the PR; fix was `select_related("customer__loyalty")`.
- **CI went from 6 to 22 minutes**: `--durations` showed 300 tests with 1.5 s setup from a
  session fixture using `TransactionTestCase` semantics and real password hashing; switching to
  MD5 and `django_db` cut 14 minutes.
- **Nightly flaky report**: a test failing 1 in 80 runs traced to `Order.objects.all()[0]`
  without `order_by` — PostgreSQL returned rows in different order after VACUUM.
- **Pricing engine**: pytest-benchmark job flagged a 40 % slowdown from replacing `Decimal`
  quantisation with repeated context creation.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Exact query counts | Deterministic, cheap, precise | Brittle to harmless refactors (e.g. extra savepoint); needs updates with intent |
| Max query counts | Tolerant | Can hide a new N+1 that stays under a loose bound |
| Scaling tests | Proves O(1) queries | Twice the setup cost |
| Runtime N+1 detector | Finds unknown paths | False positives; third-party dependency |
| Reruns for detection | Quantifies flakiness | Misused as a gate it hides bugs |
| Benchmarks | Catch CPU regressions | Noisy; require baselines and stable runners |

## 8. Security and performance considerations

- Query-count tests are cheap performance insurance; they also catch **missing scoping** bugs
  indirectly (an unexpected query to another tenant's table shows up in captured SQL).
- Flaky **permission** or **tenant-isolation** tests must never be quarantined silently — a
  flake there can be a real authorisation race.
- `MD5PasswordHasher` and `DEBUG`-style settings must live only in test settings; guard with a
  `check --deploy` run on production settings ([ci-cd-test-automation.md](ci-cd-test-automation.md)).
- Load tests against shared staging can trigger real emails, payments and webhooks — use
  sandbox credentials and isolated data.
- Printing captured SQL in CI logs can leak test data or secrets embedded in queries; remove
  debug prints before committing.

## 9. Common mistakes and anti-patterns

- Asserting query counts with only 1 row created — N+1 is invisible.
- Accessing related objects *after* the `with` block, so the lazy queries escape the count.
- `assert response.elapsed < 0.1` or `time.perf_counter()` thresholds in the main suite.
- Global `--reruns` in the PR gate "to make CI green".
- Caching warmed by a previous test changing counts (shared cache, no `cache.clear()`).
- `--no-migrations` in CI, so a broken migration or missing extension ships.
- Marking tests `slow` and never running them.
- Raising query budgets in a PR without explaining why (review the budget like an API change).
- Using `django_assert_num_queries` without `@pytest.mark.django_db` (fixture errors) or on the
  wrong DB alias.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Count differs by 1–2 locally vs CI | Savepoints, session/auth queries, cache state, different backend | Compare SQL lists; clear cache; same DB engine |
| Count changes when run in suite vs alone | Content-type / site / permission caches populated by earlier tests | Warm caches inside the test before the block, or assert max |
| Count lower than expected | QuerySet/result cache, `cached_property`, prefetch already done | Create fresh objects; query inside the block |
| Query capture empty | Wrong connection alias | `using=` / correct `connections[...]` |
| `--durations` shows slow teardown | `TransactionTestCase`/`transaction=True` truncation | Convert to rollback isolation where possible |
| Suite slower with `-n auto` | DB creation per worker dominates; too few tests per worker | `--reuse-db`; `-n 4`; check `--dist` |
| Rerun passes but test still failing intermittently | Real race or order dependence | Root-cause per [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |

Validation checklist: hot selectors/endpoints have scaling tests; budgets documented; top-25
durations reviewed periodically; no reruns in the blocking gate; quarantine list has owners and
unexpired dates.

## 11. Related references and skills

- [model-and-orm-testing.md](model-and-orm-testing.md) — QuerySet behaviour tests.
- [django-rest-framework-testing.md](django-rest-framework-testing.md) — API list/endpoint tests.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — isolation cost, concurrency tests.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — xdist, sharding, fast/slow jobs.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — flaky root causes.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — cheaper test data.
- django-orm: [query-optimization.md](../../django-orm/references/query-optimization.md).
- [django-performance](../../django-performance/SKILL.md) — profiling, load testing.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — CI database tuning, plans.

## 12. Official documentation and external references

- Django — `assertNumQueries`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#django.test.TransactionTestCase.assertNumQueries>
- Django — test runner options (`--durations`, `--parallel`): <https://docs.djangoproject.com/en/5.2/ref/django-admin/#test>
- pytest-django — query assertions: <https://pytest-django.readthedocs.io/en/latest/helpers.html#django-assert-num-queries>
- pytest — `--durations`: <https://docs.pytest.org/en/stable/how-to/usage.html#profiling-test-execution-duration>
- pytest — flaky tests: <https://docs.pytest.org/en/stable/explanation/flaky.html>
- pytest-xdist: <https://pytest-xdist.readthedocs.io/>
- pytest-rerunfailures: <https://github.com/pytest-dev/pytest-rerunfailures>
- pytest-repeat: <https://github.com/pytest-dev/pytest-repeat>
- pytest-benchmark: <https://pytest-benchmark.readthedocs.io/>
- django-zeal: <https://github.com/taobojlen/django-zeal>
- pyinstrument: <https://pyinstrument.readthedocs.io/>
- Locust: <https://docs.locust.io/> · k6: <https://grafana.com/docs/k6/latest/>
