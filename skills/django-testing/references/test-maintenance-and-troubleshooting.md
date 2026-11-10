# Test Maintenance and Troubleshooting

> **Scope:** Diagnosing why a Django test fails — reproducibly, intermittently, only in the
> suite, only in parallel or only in CI — and keeping a large suite healthy: state leakage,
> fixture errors, time and order dependence, environment drift and suite upkeep.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · **Depends on:** **[Django]**
> test runner, **[pytest]**, **[pytest-django]**, **[Python]** `pdb`/`unittest.mock`,
> **[third-party: pytest-randomly, pytest-xdist, detect-test-pollution, time-machine/freezegun]**.

## 1. Purpose and scope

This reference owns **root-cause diagnosis** of failing tests and the practices that keep a
large suite maintainable. It does not own:

- Flaky *detection*, rerun reporting and quarantine policy →
  [performance-and-reliability-testing.md](performance-and-reliability-testing.md).
- DB isolation mechanics (`TestCase` vs `TransactionTestCase`, `on_commit`) →
  [database-and-transaction-testing.md](database-and-transaction-testing.md).
- Mocking techniques → [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md).
- CI configuration → [ci-cd-test-automation.md](ci-cd-test-automation.md).

Load it when a test fails and the cause is not obvious from the assertion message, or when
planning cleanup of a slow, brittle or sprawling suite.

## 2. Core concepts

| Failure class | Definition | Typical root cause |
|---|---|---|
| **Deterministic failure** | Fails every run | Real regression, wrong test, environment mismatch |
| **Order-dependent (polluter/victim)** | Fails only after a specific other test ran | Leaked state: cache, settings, globals, patched objects, DB rows committed by a transactional test |
| **Isolation failure** | Passes alone, fails in the suite (or vice versa) | Same as above, or a test relying on data created by another |
| **Parallel-only failure** | Fails under `-n`/`--parallel` | Shared files, ports, process-global resources, fixed IDs across worker DBs |
| **Time-dependent** | Fails at certain clock times | `now()` near midnight/month-end/DST, timezone mismatch |
| **Environment-dependent** | Fails only in CI or on one machine | DB engine/version, locale, `TZ`, Python/Django version, missing service, hash seed |
| **Non-deterministic (flaky)** | Same code, same order, different outcome | Races, threads, unordered queries, randomness, network, timeouts |

A **polluter** is the test that leaves bad state; the **victim** is the test that fails because
of it. Fix the polluter; harden the victim only if it legitimately depends on global state.

## 3. Internal mechanics

Understanding where state lives explains most leaks:

| State | Reset automatically? | Notes |
|---|---|---|
| DB rows in `TestCase` / `django_db` | Yes — transaction rollback **[Django]** | Not for rows committed by transactional tests in other connections |
| DB rows in `TransactionTestCase` / `transaction=True` | Flushed after test | Flushing also removes data-migration rows unless `serialized_rollback` |
| DB sequences | **No** (unless `reset_sequences`) | Hardcoded `pk=1` passes alone, fails in suite |
| `settings` via `override_settings` / pytest-django `settings` | Yes | Direct `django.conf.settings.X = …` assignment leaks |
| `mail.outbox` | Emptied per test **[Django]** / `mailoutbox` **[pytest-django]** | |
| Cache (`locmem`, Redis) | **No** | Django runner does not clear caches between tests |
| URL resolver caches | Reset by `override_settings(ROOT_URLCONF=…)` signal handlers | Manual urlconf changes need `clear_url_caches()` |
| Active translation (`translation.activate`) | **No** — thread-local | Use `translation.override()` context manager |
| `ContentType` / `Site` caches | Cleared on `post_migrate`/flush; not per `TestCase` | Stale IDs after transactional flush `VERIFY` |
| Module globals, class attributes, `lru_cache` | **No** | Includes memoised feature flags and singletons |
| `mock.patch(...).start()` without `stop()` | **No** | Use context managers, decorators, `addCleanup`, `monkeypatch`, `mocker` |
| Signal receivers connected in a test | **No** | Disconnect in cleanup or use a context manager |
| `setUpTestData` attributes | Deep-copied per test since Django 3.2 | Before 3.2, in-memory mutation leaked across tests in the class |
| Environment variables, `os.chdir`, `sys.path` | **No** | `monkeypatch.setenv`/`chdir` restores them |
| Files written to `MEDIA_ROOT` | **No** | Use `tmp_path` + `settings.MEDIA_ROOT` or `InMemoryStorage` |

Randomised order surfaces these leaks: **[third-party: pytest-randomly]** shuffles modules,
classes and tests and reseeds `random`, Faker and factory_boy each test; **[Django]**
`manage.py test --shuffle [SEED]` (Django ≥ 4.0) shuffles within test-class groupings. Django's
runner also always orders `TestCase` before `TransactionTestCase` before other tests, precisely
because transactional tests can leave committed state.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Fix vs quarantine a failing test | Fix now / quarantine with owner | Fix deterministic failures immediately. Quarantine only via the policy in [performance-and-reliability-testing.md](performance-and-reliability-testing.md). |
| Global reset fixture vs per-test cleanup | `autouse` reset of caches etc. | Autouse resets for *process-global* state (cache, translation); explicit fixtures for everything else. Autouse fixtures cost on every test — keep them cheap. |
| Delete vs repair an old test | — | Delete when another test asserts the same behaviour at a cheaper level, or the behaviour is gone. Repair when it guards a real requirement. Record the reason in the commit. |
| Strict warnings | `-W error::DeprecationWarning` | Turn Django/DRF deprecations into errors in CI so upgrades are planned, not discovered. |
| Random order always on | pytest-randomly in all runs | Yes, once existing pollution is fixed; print and keep the seed. |

## 5. Django implementation patterns

### Debugging workflow

1. **Reproduce in isolation** — run only the failing test.
2. **Reproduce in context** — same order and seed as the failing run; same `-n` setting.
3. **Classify** using the catalogue in §10.
4. **Bisect** if order-dependent; **freeze** if time-dependent; **diff environments** if CI-only.
5. **Fix the cause**, add a guard (autouse reset, stricter fixture), rerun with several seeds.

```bash
# pytest [pytest] — isolate and inspect
pytest apps/orders/tests/test_services.py::test_cancel_refunds_payment -vv
pytest --lf -x                       # rerun last failures, stop at first
pytest -x --tb=long -l               # long tracebacks with locals (-l == --showlocals)
pytest --pdb                         # post-mortem debugger on failure
pytest --trace                       # break at start of each selected test
pytest -p no:randomly -n0            # remove ordering and parallelism as variables
pytest --setup-show test_x.py        # show fixture setup/teardown order
pytest --fixtures-per-test test_x.py # which fixtures (and from where) a test uses
pytest -k "cancel and not slow" -rA  # selection + summary of all outcomes
pytest --randomly-seed=1234          # replay a CI order (pytest-randomly) [third-party]
pytest --randomly-seed=last          # replay previous run's seed
```

```bash
# Django runner [Django]
python manage.py test apps.orders.tests.test_services.CancelTests.test_refund --pdb
python manage.py test --debug-sql -v 2     # print SQL for failing tests
python manage.py test --buffer             # hide stdout/stderr of passing tests
python manage.py test --reverse            # reverse order: quick pollution check
python manage.py test --shuffle 1234       # replay a shuffled order (Django ≥ 4.0)
python manage.py test --parallel 1         # remove parallelism
python manage.py test --durations 10       # slowest tests (Django ≥ 5.0, Python ≥ 3.12)
```

### Finding a polluter

```bash
# Run the suspected polluter set + victim in fixed order
pytest -p no:randomly tests/test_a.py tests/test_b.py::test_victim

# Automated bisection [third-party: detect-test-pollution]
detect-test-pollution --failing-test tests/test_b.py::test_victim --tests tests/
```

Manual bisection: list the tests that ran before the victim in the failing order
(`pytest --collect-only -q --randomly-seed=<seed>`), halve the list, keep the half that still
fails the victim.

### Autouse reset fixtures for process-global state

```python
# conftest.py
import pytest
from django.core.cache import caches
from django.utils import translation


@pytest.fixture(autouse=True)
def _reset_caches():
    yield
    for cache in caches.all():
        cache.clear()


@pytest.fixture(autouse=True)
def _reset_translation():
    yield
    translation.deactivate()
```

Clear *after* the test (`yield` then clear) so the test itself starts from what the previous
cleanup left — and also clear before, if a module-level warm-up could populate it. Avoid an
autouse fixture that requests `db`; it silently grants DB access to every test.

Memoised helpers need an explicit reset:

```python
@pytest.fixture(autouse=True)
def _reset_feature_flags():
    from apps.flags import loader
    yield
    loader.get_flags.cache_clear()   # functools.lru_cache [Python]
```

### Safe patching and settings

```python
# Leaks: started patch never stopped if the test fails before stop()
p = mock.patch("apps.billing.gateway.charge"); p.start()

# Safe: unittest
def setUp(self):
    patcher = mock.patch("apps.billing.gateway.charge")
    self.charge = patcher.start()
    self.addCleanup(patcher.stop)

# Safe: pytest
def test_charge(monkeypatch, settings):
    settings.BILLING_ENABLED = True               # [pytest-django] restores after test
    monkeypatch.setenv("BILLING_REGION", "eu")    # [pytest] restores after test
```

Signals:

```python
from django.db.models.signals import post_save

def test_without_audit_receiver():
    post_save.disconnect(audit_receiver, sender=Order)
    try:
        ...
    finally:
        post_save.connect(audit_receiver, sender=Order)
```

Better: make the receiver call a function you can patch, or gate it with a setting.

### Time-dependent tests

```python
import time_machine  # [third-party]
from datetime import datetime, UTC

@time_machine.travel(datetime(2026, 3, 29, 0, 30, tzinfo=UTC), tick=False)  # DST change in Europe
def test_daily_report_window_spans_dst(settings):
    settings.TIME_ZONE = "Europe/Berlin"
    ...
```

Diagnostic signals: failure only between 23:00–01:00 UTC, on the 1st/last day of a month, around
DST transitions, or on 29 February. Replace `datetime.now()`/`date.today()` with
`django.utils.timezone.now()` and freeze it, or inject a clock
([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)). Also check
`USE_TZ` and naive datetimes created by factories (`factory.Faker("date_time")` without
`tzinfo` produces naive values).

### Ordering assumptions

```python
# Fragile: DB returns rows in unspecified order without ORDER BY
assert [o.reference for o in Order.objects.all()] == ["A", "B"]

# Robust
assert {o.reference for o in Order.objects.all()} == {"A", "B"}
# or, when order is the behaviour under test, assert against an explicit order_by
```

PostgreSQL commonly returns insertion order for small tables, so the fragile version passes for
months and then fails after an `UPDATE`, a vacuum, or a switch to parallel workers.

### Parallel-only failures

| Shared resource | Fix |
|---|---|
| Fixed file paths (`/tmp/export.csv`, `MEDIA_ROOT`) | `tmp_path`; per-test storage |
| Fixed ports (live server, mock servers) | Port 0 / OS-assigned |
| External cache/Redis DB shared by workers | Per-worker key prefix or DB index using `worker_id` fixture **[third-party: pytest-xdist]** |
| Session-scoped fixture assumed to run once | It runs once *per worker* — make it idempotent |
| Hardcoded primary keys | Each worker DB has its own sequences; never assume `pk=1` |
| Test relies on another test's data in the same module | Make each test create its data |

```python
@pytest.fixture(autouse=True)
def _isolated_cache_prefix(settings, worker_id):
    settings.CACHES = {**settings.CACHES,
                       "default": {**settings.CACHES["default"], "KEY_PREFIX": f"test-{worker_id}"}}
```

### Dependency and environment drift

- Reproduce with the **lock file**, not a fresh resolve (`uv sync --frozen`, `pip install -r
  requirements.lock --require-hashes`).
- Compare `python -m django --version`, `pytest --version` (shows plugin versions in the header),
  DB server version (`SELECT version()`), `locale`, `TZ`, `PYTHONHASHSEED`.
- Upgrades: read pytest-django and Django release notes; run with
  `-W error::DeprecationWarning` on the current version first, fix warnings, then upgrade.
- A plugin that changed behaviour can be isolated with `-p no:<plugin>`.

### Maintaining a large suite

- **Ownership**: CODEOWNERS for `tests/` per app; failing tests route to an owner.
- **Delete redundancy**: the same rule asserted at model, service, API and E2E level is four
  maintenance costs for one guarantee. Keep the cheapest one plus one integration path.
- **Fixture sprawl**: a `conftest.py` with 80 fixtures → move app-specific fixtures to app-level
  `conftest.py`, prefer factories with traits over bespoke fixtures, remove unused fixtures
  (`pytest --fixtures -v` lists them with locations).
- **Review checklist for test PRs**: asserts behaviour not internals; one reason to fail;
  factories not hand-built graphs; no sleeps/real network; DB marker minimal; name states the
  behaviour (`test_cancel_after_shipping_is_rejected`).
- **Speed budget**: track suite time per app; regressions are bugs
  ([performance-and-reliability-testing.md](performance-and-reliability-testing.md)).

## 6. Production use cases

- **"Passes locally, fails in CI on Mondays"** → tests computing "last week" with
  `date.today()`; CI ran in UTC, developers in UTC+5:30. Fixed by freezing time and using
  `timezone.localdate()`.
- **Random 404 in API test after enabling xdist** → test used `reverse("order-detail", args=[1])`;
  the PK differed per worker DB. Fixed by using the created object's `pk`.
- **Permission test passes alone, fails in suite** → an earlier test cached the user's
  permission set in a module-level dict; added `cache_clear` to an autouse fixture.
- **Upgrade to Django 5.x broke 40 tests** → old `assertFormError(response, "form", …)`
  signature removed in Django 5.0 (deprecated in 4.1); codemod to `assertFormError(form, field, errors)`.
- **Data-migration rows vanish** → a `TransactionTestCase` flushed tables seeded by a data
  migration; following `TestCase`s failed. Fixed with `serialized_rollback = True` or
  factory-created data.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Random order always on | Exposes pollution early | Initial fixing effort; seed must be recorded |
| Autouse resets | Systemic protection against leaks | Hidden behaviour; per-test overhead |
| Deprecations as errors | Upgrades become routine | Third-party warnings can block CI — filter by module |
| Deleting redundant tests | Faster, cheaper suite | Needs judgement and review; coverage may dip |
| Automated pollution bisection | Fast diagnosis in big suites | Extra tool, needs deterministic victim |

## 8. Security and performance considerations

- Debug output (`--showlocals`, `--debug-sql`, `--pdb` sessions) can print tokens, passwords and
  personal data from fixtures — keep test data synthetic and don't paste CI logs publicly.
- A "fix" that disables a security control (CSRF, permission check, `ALLOWED_HOSTS`) to make a
  test pass is a regression; fix the test client setup instead
  ([security-testing.md](security-testing.md)).
- Autouse fixtures run for every test; an expensive one (e.g. flushing Redis via network) can add
  minutes. Measure with `--durations` and `--setup-show`.
- Converting a leaking test to `transaction=True` "to be safe" slows it ~10× and can cause *more*
  leakage (committed data visible to other connections).

## 9. Common mistakes and anti-patterns

- Adding `time.sleep()` or retries to "fix" a failure without identifying the cause.
- Marking a test `skip` without an issue link and owner.
- Mutating `django.conf.settings` directly.
- Hardcoding PKs, IDs or sequence-derived values.
- Asserting list order without `order_by`.
- `mock.patch(...).start()` without guaranteed `stop()`.
- Connecting signals or monkeypatching modules at import time in test files.
- Fixing the victim instead of the polluter.
- Using `transaction=True` to work around a test design problem.
- Leaving `print`/`breakpoint()` in committed tests (`breakpoint()` hangs CI unless
  `PYTHONBREAKPOINT=0`).

## 10. Troubleshooting and validation

### Failure signature catalogue

| Signature | Cause | Fix |
|---|---|---|
| `RuntimeError: Database access not allowed, use the "django_db" mark…` **[pytest-django]** | Test/fixture touches DB without marker | `@pytest.mark.django_db` or request `db`; check fixtures that query at import |
| `DatabaseOperationForbidden` / "Database queries to 'default' are not allowed in SimpleTestCase" **[Django]** | DB query in `SimpleTestCase` | Use `TestCase` or set `databases` |
| `TransactionManagementError: An error occurred in the current transaction…` | Test continued after `IntegrityError` inside the test transaction | Wrap the failing statement in `transaction.atomic()` |
| `database "test_x" is being accessed by other users` | Lingering connections (threads, debug shell, previous crashed run) during teardown/creation | Close connections in thread code (`connection.close()`), kill stale sessions, `--create-db` |
| `relation "…" does not exist` with `--reuse-db` | Reused DB from before new migrations | `--create-db` once |
| `SynchronousOnlyOperation` | ORM call inside a running event loop (async test, Playwright sync API) | `sync_to_async`/async ORM; `DJANGO_ALLOW_ASYNC_UNSAFE` for E2E only ([async-testing.md](async-testing.md)) |
| `fixture 'x' not found` | conftest not in scope / plugin not installed / typo | `pytest --fixtures`; move fixture to a shared `conftest.py` |
| `ScopeMismatch` | Session/module fixture requests function-scoped `db` | Use `django_db_setup` + `django_db_blocker.unblock()` or lower the scope |
| Passes alone, fails in suite | Leaked state (§3 table) | Find polluter (§5); add reset |
| Fails only with `-n` | Shared resource (§5 table) | Per-worker isolation |
| Fails at midnight / month-end / DST | Unfrozen time, naive datetimes | Freeze time; timezone-aware factories |
| Fails only in CI | Engine/version/TZ/locale/missing service/missing env var | Diff environments; run CI image locally |
| `AssertionError` on `mail.outbox` length 0 | Email sent in `on_commit` under `TestCase` | `captureOnCommitCallbacks(execute=True)` ([database-and-transaction-testing.md](database-and-transaction-testing.md)) |
| `NoReverseMatch` only in some tests | `urls` override / `ROOT_URLCONF` changed without cache clear | `override_settings(ROOT_URLCONF=…)` or `@pytest.mark.urls` |
| Factory `IntegrityError` duplicate key on unique field | Non-sequenced unique value, or Faker collision | `factory.Sequence`, `django_get_or_create`, or `faker.unique` |
| Mock "not called" though code ran | Patched where defined, not where looked up | Patch the name in the module that uses it ([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)) |
| `assertNumQueries` off by one or two | Savepoints, session/auth queries, ContentType cache | Inspect captured queries; warm caches deliberately ([performance-and-reliability-testing.md](performance-and-reliability-testing.md)) |

### Validation after a fix

- [ ] Failing test reproduced before the fix and passes after.
- [ ] Passes alone, in its module, in the full suite, with `-n auto`, and with ≥ 3 random seeds.
- [ ] Root cause recorded in the commit message (polluter, leaked state, environment difference).
- [ ] Guard added where the class of bug can recur (autouse reset, lint rule, fixture change).

## 11. Related references and skills

- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — flaky detection, quarantine, suite speed.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — isolation, `on_commit`, transactional leaks.
- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — fixtures, scopes, markers.
- [django-testing-framework.md](django-testing-framework.md) — runner options, test class ordering.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — patch targets, clocks.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — factory uniqueness and sequences.
- [async-testing.md](async-testing.md) — event-loop and async DB failures.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — CI environment and seeds.
- [django-orm transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md) — transaction semantics.
- [django-core asynchronous-django](../../django-core/references/asynchronous-django.md) — async safety.

## 12. Official documentation and external references

- Django testing tools: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Django test runner options (`--shuffle`, `--reverse`, `--debug-sql`, `--pdb`, `--durations`): <https://docs.djangoproject.com/en/5.2/ref/django-admin/#test>
- Django test order: <https://docs.djangoproject.com/en/5.2/topics/testing/overview/#order-in-which-tests-are-executed>
- pytest how-to: failures and debugging: <https://docs.pytest.org/en/stable/how-to/failures.html>
- pytest CLI reference: <https://docs.pytest.org/en/stable/reference/reference.html#command-line-flags>
- pytest-django database access and helpers: <https://pytest-django.readthedocs.io/en/latest/database.html>
- pytest-randomly: <https://github.com/pytest-dev/pytest-randomly>
- detect-test-pollution: <https://github.com/asottile/detect-test-pollution>
- pytest-xdist `worker_id`: <https://pytest-xdist.readthedocs.io/en/stable/how-to.html>
- time-machine: <https://time-machine.readthedocs.io/>
- Python `unittest.mock` — where to patch: <https://docs.python.org/3/library/unittest.mock.html#where-to-patch>
