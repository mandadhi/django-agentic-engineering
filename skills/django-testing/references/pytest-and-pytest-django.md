# pytest and pytest-django

> **Scope:** Running Django tests with pytest — pytest's own configuration, fixtures, scopes,
> markers, parametrisation, selection and plugins, and what pytest-django adds on top
> (settings bootstrapping, database access control, Django-aware fixtures, test DB reuse).
> Also when to choose pytest over Django's unittest runner and how to migrate.
>
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · **Depends on:** **[pytest]**
> (third-party, but the de facto runner), **[third-party: pytest-django]**, optional plugins named
> per section. Django-runner equivalents: [django-testing-framework.md](django-testing-framework.md).

## 1. Purpose and scope

Load this when configuring pytest for a Django project, writing fixtures/conftest files,
choosing DB markers, adding plugins, or migrating a unittest suite.

Owned here: pytest configuration and mechanics; pytest-django markers, fixtures and CLI options;
plugin trade-offs; pytest-vs-unittest decision. Not owned: what data fixtures should contain
([fixtures-and-test-data.md](fixtures-and-test-data.md)), mocking with `monkeypatch`/pytest-mock
([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)), xdist/sharding in CI
([ci-cd-test-automation.md](ci-cd-test-automation.md)), pytest-asyncio
([async-testing.md](async-testing.md)), coverage ([coverage-and-quality-analysis.md](coverage-and-quality-analysis.md)).

## 2. Core concepts

### Who provides what

| Capability | **[pytest]** | **[pytest-django]** | **[Django]** (underlying) |
|---|---|---|---|
| Discovery & assertion rewriting | `test_*.py`, plain `assert` with introspection | — | unittest discovery |
| Fixtures | `@pytest.fixture`, scopes, `conftest.py` | `db`, `client`, `rf`, `settings`, `mailoutbox`, `live_server`, … | `setUp`, `setUpTestData` |
| DB access control | — | Blocks DB unless `django_db` marker / `db` fixture | `SimpleTestCase.databases` |
| DB isolation | — | Wraps in Django's `TestCase`/`TransactionTestCase` machinery | Atomic rollback / flush |
| Settings | — | `--ds`, `DJANGO_SETTINGS_MODULE`, `settings` fixture | `override_settings` |
| Test DB reuse | — | `--reuse-db`, `--create-db` | `--keepdb` |
| Query assertions | — | `django_assert_num_queries`, `django_assert_max_num_queries` | `assertNumQueries` |
| `on_commit` capture | — | `django_capture_on_commit_callbacks` | `captureOnCommitCallbacks` |
| Parametrisation | `@pytest.mark.parametrize` | — | `subTest` |
| Markers/selection | `-m`, `-k`, `--lf`, custom markers | `django_db`, `urls`, `ignore_template_errors` | `@tag`, `--tag` |
| Parallelism | via **[third-party: pytest-xdist]** | Suffixes test DB per worker | `--parallel` |

pytest-django runs **existing Django `TestCase` classes unchanged**, so migration is incremental.

### Fixtures and scopes **[pytest]**

A fixture is a function whose return/yield value is injected by parameter name. Scope controls
lifetime: `function` (default) < `class` < `module` < `package` < `session`. A fixture may only
depend on fixtures of equal or wider scope.

```python
@pytest.fixture
def tenant(db):                       # `db` grants DB access (pytest-django)
    return TenantFactory(slug="acme")

@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()

@pytest.fixture
def tenant_admin_client(api_client, tenant):
    user = UserFactory(tenant=tenant, role="admin")
    api_client.force_authenticate(user)
    yield api_client                  # code after yield = teardown
    api_client.force_authenticate(None)
```

Fixture resolution is by name through the `conftest.py` hierarchy: a fixture in
`apps/orders/tests/conftest.py` overrides one with the same name in the root `conftest.py` for
that subtree. Keep root fixtures generic (clients, users); keep domain fixtures next to the app.

### Database access **[pytest-django]**

Tests have **no DB access by default**; touching the DB raises
`RuntimeError: Database access not allowed, use the "django_db" mark…`. Grant it explicitly:

| Need | Marker / fixture | Django equivalent |
|---|---|---|
| Normal DB test | `@pytest.mark.django_db` or request `db` | `TestCase` |
| Real commits | `@pytest.mark.django_db(transaction=True)` or `transactional_db` | `TransactionTestCase` |
| Reset sequences (predictable PKs) | `django_db(reset_sequences=True)` (implies transactional) | `reset_sequences = True` |
| Several aliases | `django_db(databases=["default", "reports"])` or `"__all__"` | `databases = {...}` |
| Restore data-migration rows after flush | `django_db(serialized_rollback=True)` | `serialized_rollback = True` |
| Limit flushed apps | `django_db(available_apps=[...])` — pytest-django ≥ 4.6 (`VERIFY`) | `available_apps` |

Apply to a whole module with `pytestmark = pytest.mark.django_db`.

## 3. Internal mechanics

### Bootstrap

1. pytest-django reads the settings module from `--ds`, then `DJANGO_SETTINGS_MODULE` env var,
   then the ini option `DJANGO_SETTINGS_MODULE` (CLI > env > ini).
2. Calls `django.setup()` before collection (so models import cleanly).
3. Calls Django's `setup_test_environment()` (locmem email, `testserver` host, `DEBUG=False`
   unless `--django-debug-mode`).
4. The session-scoped `django_db_setup` fixture creates test databases **lazily** — only when the
   first test requesting DB access runs. A run selecting only non-DB tests creates no DB.
5. Each `django_db` test is wrapped by a Django `TestCase`/`TransactionTestCase` instance under
   the hood, so isolation semantics are identical to Django's.

### Ordering

pytest-django reorders tests like Django: non-transactional DB tests first, then transactional,
then non-DB — preserving rollback-test isolation from flushed tables. Plugins that shuffle order
(pytest-randomly) shuffle within these groups (`VERIFY` exact interaction for your versions).

### DB reuse and xdist

- `--reuse-db` keeps the test DB at session end and reuses it next time (applying new
  migrations); `--create-db` forces recreation (use after branch switches). Put `--reuse-db` in
  `addopts` locally; CI usually starts fresh anyway.
- `--no-migrations` (`--nomigrations`) builds tables directly from models — same trade-off as
  Django's `TEST["MIGRATE"] = False`.
- Under pytest-xdist each worker gets its own test DB with a `_gw0`, `_gw1`… suffix.

### Session-scoped DB data

DB fixtures are function-scoped because the DB marker is per test. For expensive read-only
reference data, extend `django_db_setup`:

```python
# conftest.py
import pytest
from django.core.management import call_command

@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        call_command("loaddata", "currencies.json")   # committed once, visible to all tests
```

Data loaded here is committed and **survives** rollback tests but is **flushed** by the first
transactional test (unless serialized rollback is used) — keep it in non-transactional suites or
re-seed. For class-level shared data, Django `TestCase.setUpTestData` is often simpler.

## 4. Engineering decisions and trade-offs

### pytest vs Django's runner

| Factor | pytest + pytest-django | `manage.py test` |
|---|---|---|
| Fixtures/composition | Dependency-injected fixtures, scopes, overrides | `setUp`/`setUpTestData`, inheritance/mixins |
| Parametrisation | First-class, separate test IDs | `subTest` (one test, multiple failures) |
| Assertion output | Rich introspection of plain `assert` | `assertX` methods |
| Plugins | Large ecosystem (xdist, cov, randomly, timeout, socket) | Few; custom runner needed |
| Dependencies | pytest + pytest-django (+ plugins) | None |
| DB-access default | Denied (explicit) | Allowed in `TestCase` |
| Existing `TestCase` suites | Run unchanged | Native |

Default to pytest for new projects; in an established unittest suite, switch the runner first and
write new tests in pytest style — don't mix styles within one test module.

### Fixture design

| Choice | Prefer | Avoid |
|---|---|---|
| Data creation | Function-scoped fixtures calling factories | Session-scoped mutable model instances |
| Variation | Factory arguments / "factory as fixture" | Many near-identical fixtures (`paid_order`, `paid_order_with_two_lines`, …) |
| Implicit setup | `autouse=True` only for resets (cache, time) | Autouse fixtures that create data |
| Depth | 1–3 levels of fixture dependency | Deep chains that hide what a test relies on |

### Plugins (all **third-party**)

| Plugin | Purpose | Trade-off |
|---|---|---|
| pytest-xdist | Parallel workers (`-n auto`) | Requires isolation; per-worker DB creation cost; harder debugging |
| pytest-randomly | Random order + reseeds `random`/Faker each test | Surfaces hidden coupling (good) — expect initial failures |
| pytest-mock | `mocker` fixture wrapping `unittest.mock` with auto-undo | Another dependency; same pitfalls as `patch` |
| pytest-cov | Coverage integrated with xdist | Small overhead; config split with coverage.py |
| pytest-timeout | Kill hanging tests (`--timeout=60`) | Thread method may not interrupt C calls; signal method is POSIX-only |
| pytest-socket | `--disable-socket` blocks network | Needs `--allow-hosts` for PostgreSQL/Redis over TCP |
| pytest-rerunfailures | `--reruns 2` | Hides real races — quarantine-only, never global |
| pytest-env | Set env vars in config | Settings modules are usually clearer |
| pytest-split | Duration-based sharding across CI jobs | Needs a maintained durations file |

## 5. Django implementation patterns

### Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"   # pytest-django ini option
minversion = "8.0"
testpaths = ["apps", "tests"]
python_files = ["test_*.py"]
addopts = [
    "--strict-markers",       # unknown marker = error (catches @pytest.mark.djnago_db typos)
    "--strict-config",
    "-ra",                    # summary of skips/xfails/errors
    "--reuse-db",             # pytest-django
]
markers = [
    "slow: long-running tests excluded from the fast suite",
    "e2e: browser tests",
    "integration: talks to real sandboxes; opt-in",
]
filterwarnings = [
    "error",                                              # warnings fail the build…
    "ignore::DeprecationWarning:some_third_party_pkg.*",  # …except known third-party noise
]
xfail_strict = true            # an xfail that passes is a failure (stale xfail)
```

pytest ≥ 9 is reported to support a native `[tool.pytest]` TOML table in addition to
`[tool.pytest.ini_options]` — `VERIFY` against the installed pytest before using it.
Equivalent `pytest.ini` uses a `[pytest]` section. Only one config file is used — the first found
(`pytest.ini` wins over `pyproject.toml`); stray `pytest.ini` files silently override pyproject.

### Root conftest

```python
# conftest.py
import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(email="u@example.test", password="pw")


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client
```

### pytest-django fixtures worth knowing **[pytest-django]**

| Fixture | Gives | Note |
|---|---|---|
| `client` / `async_client` | Django `Client` / `AsyncClient` | `admin_client` is logged in as `admin_user` |
| `rf` / `async_rf` | `RequestFactory` / `AsyncRequestFactory` | No middleware: set `request.user` yourself |
| `django_user_model`, `django_username_field` | Active user model / username field | Works with custom user models |
| `settings` | Mutable proxy; changes reverted after test | Same caveat as `override_settings` for import-time copies |
| `mailoutbox` | `django.core.mail.outbox`, cleared per test | |
| `live_server` | Running server (transactional DB) | For browser tests; session-scoped server |
| `django_assert_num_queries(n)` / `django_assert_max_num_queries(n)` | Query-count context manager | Failure message lists the SQL |
| `django_capture_on_commit_callbacks(execute=True)` | Run `on_commit` callbacks without real commit | pytest-django ≥ 4.4 (`VERIFY`) |
| `django_db_blocker` | `.unblock()` DB access in session fixtures | |
| `django_db_setup` | Session fixture creating test DBs | Override to seed or point to external DB |

```python
@pytest.mark.django_db
def test_order_list_is_scoped_and_bounded(auth_client, user, django_assert_max_num_queries):
    OrderFactory.create_batch(5, customer__user=user)
    OrderFactory()                                     # another customer's
    with django_assert_max_num_queries(4):
        response = auth_client.get("/orders/")
    assert response.status_code == 200
    assert len(response.context["orders"]) == 5


def test_feature_flag_hides_banner(settings, rf):      # no DB needed
    settings.SHOW_PROMO_BANNER = False
    request = rf.get("/")
    assert "promo" not in render_home(request).content.decode()


@pytest.mark.django_db
def test_signup_sends_welcome_email(client, mailoutbox):
    client.post("/signup/", {"email": "new@example.test", "password1": "x9!Kq…", "password2": "x9!Kq…"})
    assert [m.to for m in mailoutbox] == [["new@example.test"]]
```

### Parametrisation **[pytest]**

```python
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "expected_status"),
    [("viewer", 403), ("editor", 200), ("owner", 200)],
    ids=["viewer-denied", "editor-allowed", "owner-allowed"],
)
def test_invoice_update_by_role(api_client, role, expected_status):
    member = MembershipFactory(role=role)
    invoice = InvoiceFactory(tenant=member.tenant)
    api_client.force_authenticate(member.user)
    response = api_client.patch(f"/api/invoices/{invoice.pk}/", {"note": "x"}, format="json")
    assert response.status_code == expected_status
```

`indirect=True` routes a parameter through a fixture of the same name (useful to build data from
a compact parameter, e.g. `indirect=["order"]` with `request.param` inside the fixture).
`pytest.param(..., marks=pytest.mark.xfail(reason=...))` marks individual cases.

### Factory as fixture

```python
@pytest.fixture
def make_order(db):
    def _make(*, lines=1, **kwargs):
        order = OrderFactory(**kwargs)
        OrderLineFactory.create_batch(lines, order=order)
        return order
    return _make

def test_total_sums_lines(make_order):
    order = make_order(lines=3)
    ...
```

### Per-test URLconf

```python
@pytest.mark.urls("apps.webhooks.tests.urls")      # pytest-django; Django: @override_settings(ROOT_URLCONF=...)
def test_webhook_endpoint(client): ...
```

### Template variable checks

`--fail-on-template-vars` (pytest-django) fails tests that render undefined template variables;
`@pytest.mark.ignore_template_errors` exempts a test. Useful during template refactors.

### Selection **[pytest]**

```bash
pytest apps/orders                       # by path
pytest -k "refund and not webhook"       # by name expression
pytest -m "not slow and not e2e"         # by marker
pytest --lf                               # last failed only
pytest --ff -x                            # failed first, stop at first failure
pytest --sw                               # stepwise: resume from last failure
pytest --create-db -p no:randomly         # fresh DB, disable a plugin for this run
pytest --durations=20 --durations-min=0.5
pytest --ds=config.settings.test_postgres
```

### Migrating unittest → pytest

1. Install pytest + pytest-django; add config; run the existing suite unchanged with `pytest`.
2. Fix collection differences (files not matching `python_files`, `__init__.py` gaps causing
   duplicate basenames).
3. Add `--strict-markers` and shared fixtures in `conftest.py`.
4. Write new tests as functions; convert old ones opportunistically when touched
   (`self.assertEqual(a, b)` → `assert a == b`; `setUpTestData` → fixtures or keep the class).
5. Replace the `manage.py test` CI command last, once both pass identically.

## 6. Production use cases

- **Large monorepo**: root conftest with clients/users; per-app conftest with domain fixtures;
  `-m "not slow"` locally; full suite with xdist in CI.
- **Shared reference data (currencies, countries)**: seeded once in a `django_db_setup` override
  for a read-only suite, or created by a data migration (kept intact by rollback tests).
- **Strangler migration from unittest**: both styles coexist per module for months; pytest runs
  everything.
- **Template-heavy legacy site**: `--fail-on-template-vars` in CI catches context-name typos.

## 7. Advantages and disadvantages

| | Advantages | Disadvantages |
|---|---|---|
| pytest | Concise tests, fixtures, parametrisation, plugins, rich failure output | Fixture "magic" by name; deep fixture graphs hurt readability |
| pytest-django | Explicit DB access, Django-aware fixtures, DB reuse, runs `TestCase` classes | Another layer to version; some Django features lag behind |
| Autouse fixtures | Guaranteed resets | Invisible setup; cost paid by every test |
| Session fixtures | Speed | Shared state; interacts badly with transactional tests |

## 8. Security and performance considerations

- `--disable-socket` (pytest-socket) or a transport-level guard prevents accidental calls to real
  services with test data; allow only the DB/cache hosts.
- Never point `--ds` at a settings module with production credentials; CI should fail if
  `DJANGO_SETTINGS_MODULE` is not the test module.
- Speed: `--reuse-db` locally; avoid `transaction=True`; keep function-scoped data small;
  `-p no:cacheprovider` only if the `.pytest_cache` dir is a problem (it powers `--lf`).
- Autouse fixtures run for every test — keep them O(1).

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| Typo in marker (`django_bd`) without `--strict-markers` | Marker ignored; test fails or skips oddly | `--strict-markers` |
| `transaction=True` by default | Slow suite, flushed reference data | Use only for real-commit needs |
| Session-scoped fixture returning a model instance used with `db` | Instance refers to rolled-back row / DB access error | Function scope or `django_db_setup` seeding |
| Mutating `settings` module directly | Leaks | `settings` fixture |
| Fixture with same name in nested conftest by accident | Silent override | Distinct names; `pytest --fixtures-per-test` to inspect |
| `pytest.ini` left behind next to pyproject config | pyproject config ignored | One config file |
| Global `--reruns` | Masks races | Quarantine individual tests |
| Mixing `TestCase` methods and pytest fixtures in a class | Fixtures (other than autouse) aren't injected into unittest methods | Use pure pytest functions, or `@pytest.mark.usefixtures` + `request` patterns sparingly |

## 10. Troubleshooting and validation

| Symptom | Cause | Action |
|---|---|---|
| `Database access not allowed` | Missing marker/`db` fixture (often in a fixture) | Add `db` to the fixture's parameters |
| `ImproperlyConfigured: Requested setting …` at collection | Settings module not found | Check `--ds`/env/ini precedence; `pytest --version` shows plugin loaded |
| `fixture 'mocker' not found` | pytest-mock not installed | Install or use `unittest.mock` |
| Tests pass with `--reuse-db`, fail with `--create-db` | Stale DB schema | Recreate; check migrations |
| Duplicate test module basename errors | Missing `__init__.py` in test dirs (rootdir-based import) | Add packages or use `--import-mode=importlib` |
| Very slow first test | Lazy DB creation + migrations | Expected; `--reuse-db`; squash migrations |
| `RuntimeWarning: coroutine … was never awaited` | Async test without pytest-asyncio/anyio | See [async-testing.md](async-testing.md) |

Inspection commands: `pytest --fixtures` (available fixtures), `pytest --fixtures-per-test path`
(what each test uses), `pytest --setup-show` (fixture setup/teardown order),
`pytest --collect-only -q` (what will run), `pytest --markers`.

## 11. Related references and skills

- [django-testing-framework.md](django-testing-framework.md) — the Django machinery pytest-django wraps.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — what data fixtures and factories should build.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — `monkeypatch`, `mocker`.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — xdist, sharding, CI invocation.
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — durations, query budgets.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — order and leakage bugs.
- [django-rest-framework: drf-testing-and-debugging](../../django-rest-framework/references/drf-testing-and-debugging.md).

## 12. Official documentation and external references

- pytest docs: <https://docs.pytest.org/en/stable/>
- pytest fixtures: <https://docs.pytest.org/en/stable/how-to/fixtures.html>
- pytest parametrize: <https://docs.pytest.org/en/stable/how-to/parametrize.html>
- pytest configuration: <https://docs.pytest.org/en/stable/reference/customize.html>
- pytest-django: <https://pytest-django.readthedocs.io/en/latest/>
- pytest-django helpers (markers, fixtures): <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- pytest-django database: <https://pytest-django.readthedocs.io/en/latest/database.html>
- pytest-xdist: <https://pytest-xdist.readthedocs.io/>
- pytest-randomly: <https://github.com/pytest-dev/pytest-randomly>
- pytest-mock: <https://pytest-mock.readthedocs.io/>
- pytest-socket: <https://github.com/miketheman/pytest-socket>
- pytest-timeout: <https://github.com/pytest-dev/pytest-timeout>
- Django testing tools: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
