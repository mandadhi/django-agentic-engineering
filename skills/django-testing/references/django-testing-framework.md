# Django testing framework

> **Scope:** Django's built-in, unittest-based test framework — the `DiscoverRunner`, test
> discovery, test settings, the test database lifecycle, the four test-case classes and how to
> choose between them, settings overrides, tags, and the runner's execution options
> (parallel, shuffle, durations, keepdb).
>
> **Baseline:** Django 5.2 LTS · Python ≥ 3.10 · **Depends on:** **[Django]** `django.test`,
> **[Python]** `unittest`. pytest equivalents are in
> [pytest-and-pytest-django.md](pytest-and-pytest-django.md).

## 1. Purpose and scope

Load this when configuring how tests run under `manage.py test`, choosing a test-case class,
designing a test settings module, debugging test-database creation, or using Django test
utilities (`override_settings`, `isolate_apps`, `CaptureQueriesContext`) — including from pytest,
since pytest-django builds on the same machinery.

Owned here: runner flow, discovery, test settings, test DB lifecycle and `TEST` options, test
classes, `setUpTestData`, `databases`, settings overrides, tags, runner flags, custom runners.
Not owned: transaction semantics inside tests in depth
([database-and-transaction-testing.md](database-and-transaction-testing.md)), client/view
assertions ([views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md)),
CI wiring ([ci-cd-test-automation.md](ci-cd-test-automation.md)).

## 2. Core concepts

### The four test-case classes **[Django]**

| Class | DB access | Isolation mechanism | Use for |
|---|---|---|---|
| `SimpleTestCase` | Blocked (raises on query) unless `databases` set | None needed | Pure logic, forms without DB, URL resolving, template rendering, utility functions |
| `TransactionTestCase` | Allowed | Truncates (flushes) all tables **after** each test | Code that needs real commits: `on_commit` across connections, threads, `select_for_update` contention, DB-level deferred constraint checks at commit |
| `TestCase` | Allowed | Class wrapped in `atomic()`, each test in a savepoint rolled back | Default for anything touching the DB |
| `LiveServerTestCase` | Allowed (it is a `TransactionTestCase`) | Truncation + a live HTTP server thread | Browser tests (use `StaticLiveServerTestCase` from `django.contrib.staticfiles.testing` to serve static files) |

Inheritance: `SimpleTestCase` → `TransactionTestCase` → `TestCase`; `TransactionTestCase` →
`LiveServerTestCase` → `StaticLiveServerTestCase`.

### Choosing a class

```text
Does the test touch the database?
├── No  → SimpleTestCase (or a plain pytest function)
└── Yes → Does it need a real COMMIT to observe the behaviour?
          (on_commit with real commit, other threads/processes/connections, row-lock contention,
           DEFERRABLE INITIALLY DEFERRED constraints, LISTEN/NOTIFY)
          ├── No  → TestCase  (use captureOnCommitCallbacks for on_commit)
          └── Yes → Does it need an HTTP server for a browser/external client?
                    ├── No  → TransactionTestCase
                    └── Yes → StaticLiveServerTestCase / LiveServerTestCase
```

### Test runner **[Django]**

`manage.py test` instantiates `settings.TEST_RUNNER` (default
`django.test.runner.DiscoverRunner`), which:

1. `setup_test_environment()` — installs the instrumented template renderer (so
   `response.templates`/`context` exist), sets `EMAIL_BACKEND` to the locmem backend
   (`django.core.mail.outbox`), adds `testserver` to `ALLOWED_HOSTS`, and forces
   `settings.DEBUG = False` regardless of your settings (unless `--debug-mode`).
2. `build_suite()` — discovers tests.
3. `setup_databases()` — creates test databases (or reuses with `--keepdb`).
4. `run_checks()` — runs system checks against the test databases.
5. `run_suite()` — executes, reordering by class: `TestCase` (and subclasses) first, then
   `TransactionTestCase`-based, then other unittest cases — so transactional tests that flush
   tables cannot leave data the rollback-based tests would see.
6. `teardown_databases()` and `teardown_test_environment()`.

Because `DEBUG` is forced to `False`, tests exercise production-like error handling; a test
that "passes locally with DEBUG" behaves the same under the runner.

## 3. Internal mechanics

### Discovery

- Default pattern `test*.py`; override with `-p/--pattern "tests_*.py"`.
- Test labels narrow the run: `apps.orders`, `apps.orders.tests.test_services`,
  `apps.orders.tests.test_services.TestPlaceOrder.test_rejects_overselling`, or a directory path.
- Discovery imports modules; a module-level side effect (DB query, network client creation)
  runs during discovery — before test databases exist — and can hit the real database.
- `-k <substring>` (Django ≥ 3.0) filters test methods/classes by name pattern.

### Test database lifecycle

- For each alias in `DATABASES`, Django creates `test_<NAME>` (unless `TEST["NAME"]` is set),
  runs **migrations** (or syncdb-style creation when `TEST["MIGRATE"] = False`, Django ≥ 3.1),
  then runs the suite and destroys it.
- SQLite: the test DB is in-memory by default (`:memory:` / shared-cache URI) unless
  `TEST["NAME"]` points to a file — fast, but not the production engine.
- PostgreSQL: requires a role with `CREATEDB`; Django connects to the `postgres` maintenance DB
  to create the test DB (falling back to the first configured database if unavailable).
- `--keepdb`: keep the test DB between runs and skip creation; migrations not yet applied are
  still applied. Stale schemas from deleted migrations are **not** cleaned — recreate after
  switching branches with divergent migrations.
- `--parallel N`: databases are cloned per worker (`test_app_1`, `test_app_2` …); PostgreSQL
  clones via `CREATE DATABASE … TEMPLATE`, SQLite via in-memory copies.

### `TEST` dictionary options **[Django]**

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        # ...
        "TEST": {
            "NAME": "test_app",          # explicit name (CI isolation per job, e.g. f"test_app_{CI_JOB_ID}")
            "MIGRATE": True,             # False = create tables from models, skip migrations (Django ≥ 3.1)
            "TEMPLATE": "template0",     # PostgreSQL-only: template for CREATE DATABASE
            "CHARSET": "UTF8",
            # "COLLATION": ...           # MySQL/SQLite-specific behaviour; see docs
            # "DEPENDENCIES": [...]      # creation order between aliases
            # "MIRROR": "default"        # replica alias shares the default test DB
        },
    },
    "replica": {
        # ...
        "TEST": {"MIRROR": "default"},   # reads in tests see writes made via "default"
    },
}
```

`MIGRATE = False` speeds creation for large migration histories but skips data migrations and
`RunSQL` (extensions, triggers, custom functions) — tests then run against a schema production
never had. Prefer squashing migrations or `--keepdb`/`--reuse-db`; if you disable migrations,
keep a CI job that runs with them.

### Isolation inside `TestCase`

```text
setUpClass:  atomic() begins  ← class-level transaction
             setUpTestData()  ← runs once per class
test_a:      atomic() savepoint → test body → rollback to savepoint
test_b:      atomic() savepoint → test body → rollback to savepoint
tearDownClass: rollback class-level transaction
```

Consequences:

- `transaction.on_commit` callbacks never fire (nothing commits) — use
  `self.captureOnCommitCallbacks(execute=True)` (Django ≥ 3.2).
- Code that catches `IntegrityError` without its own `atomic()` breaks the outer transaction
  ("current transaction is aborted" on PostgreSQL) — wrap expected DB errors in `atomic()`.
- Other connections/threads cannot see uncommitted test data.

`setUpTestData` objects are assigned to class attributes; since Django 3.2 each test receives a
`copy.deepcopy()` of them on first access, so in-memory mutations do not leak between tests.
Objects that cannot be deep-copied (e.g. holding locks, open files) raise errors — keep them out
of `setUpTestData`.

`TransactionTestCase` flushes all tables after each test. Data created by data migrations is
gone for later tests unless `serialized_rollback = True`, which serialises DB content at setup
and reloads it — correct but slow. pytest-django exposes the same as
`django_db(serialized_rollback=True)`.

### The `databases` attribute

`databases = {"default", "reports"}` (or `"__all__"`) declares which aliases a test case may
query; queries to undeclared aliases raise. `SimpleTestCase` defaults to none, `TestCase` to
`{"default"}`. Replaces the removed `multi_db` / `allow_database_queries` attributes.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Guidance |
|---|---|---|---|
| Default DB class | `TestCase` | `TransactionTestCase` | `TestCase`; transactional only with a stated reason (often 5–20× slower per test) |
| Shared data | `setUpTestData` | `setUp` | `setUpTestData` for read-mostly data used by many tests in the class; `setUp` when each test mutates it heavily |
| Test DB engine | SQLite in-memory | Production engine | Production engine in CI always; SQLite locally only if the code uses no backend-specific features |
| Migrations in tests | Run (`MIGRATE=True`) | Skip | Run; skip only with a guard job that runs them |
| Runner | `manage.py test` | pytest | See [pytest-and-pytest-django.md](pytest-and-pytest-django.md); Django test classes run under both |
| Parallelism | `--parallel` | pytest-xdist | Equivalent goal; pick per runner. Both require isolation first |

## 5. Django implementation patterns

### Test settings module

```python
# config/settings/test.py
from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-not-secret"
ALLOWED_HOSTS = ["testserver", "localhost"]

# Hashing with PBKDF2 dominates user-creation cost; MD5 is fine because test passwords are fake.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"   # runner sets this anyway

STORAGES = {                                                       # Django ≥ 4.2
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

CELERY_TASK_ALWAYS_EAGER = False      # tests call tasks explicitly; see integration reference
LOGGING_CONFIG = None                 # or a quiet config; keep WARNING+ visible
DATABASES["default"]["TEST"] = {"NAME": env("TEST_DB_NAME", default="test_app")}  # noqa: F405
```

Rules: no production hosts, keys or buckets; every external integration points at a fake or a
sandbox; the module is selected explicitly (`--settings` / `DJANGO_SETTINGS_MODULE`) — never by
"if 'test' in sys.argv".

### A `TestCase` with `setUpTestData`

```python
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse

from apps.orders.tests.factories import CustomerFactory, OrderFactory


class OrderDetailViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = CustomerFactory()
        cls.order = OrderFactory(customer=cls.customer, total=Decimal("42.00"))

    def setUp(self):
        self.client.force_login(self.customer.user)

    def test_shows_order_total(self):
        response = self.client.get(reverse("orders:detail", args=[self.order.pk]))
        self.assertContains(response, "42.00")

    def test_other_customers_order_is_404(self):
        other = OrderFactory()
        response = self.client.get(reverse("orders:detail", args=[other.pk]))
        self.assertEqual(response.status_code, 404)
```

### `on_commit` inside `TestCase`

```python
from unittest import mock

class PlaceOrderTests(TestCase):
    def test_confirmation_sent_after_commit(self):
        with mock.patch("apps.orders.services.send_confirmation.delay") as send, \
             self.captureOnCommitCallbacks(execute=True) as callbacks:
            order = place_order(customer=CustomerFactory(), lines=[...])
        self.assertEqual(len(callbacks), 1)
        send.assert_called_once_with(order.pk)
```

### Settings overrides **[Django]**

```python
from django.test import override_settings, modify_settings

@override_settings(ORDERS_MAX_LINES=2)
class OrderLimitTests(TestCase):
    def test_rejects_third_line(self): ...

    @override_settings(ORDERS_MAX_LINES=5)       # method-level wins
    def test_allows_five_lines(self): ...

@modify_settings(MIDDLEWARE={"append": "apps.tenants.middleware.TenantMiddleware"})
class TenantMiddlewareTests(TestCase): ...
```

Gotchas:

- Overrides take effect through `django.conf.settings` and the `setting_changed` signal. Values
  **copied at import time** (`MAX = settings.ORDERS_MAX_LINES` at module level, DRF throttle rates
  read into class attributes, cached clients built from settings) do not change. Read settings at
  call time or reset caches in a `setting_changed` receiver.
- `@override_settings` on a non-`SimpleTestCase` class is not applied — decorate methods or use
  the context manager.
- Overriding `DATABASES` or `INSTALLED_APPS` dynamically is limited/unsupported for DB setup;
  `INSTALLED_APPS` overrides reload the app registry (slow; prefer `isolate_apps`).

### Temporary models: `isolate_apps` **[Django]**

```python
from django.db import models
from django.test import SimpleTestCase
from django.test.utils import isolate_apps

@isolate_apps("core")                     # app *label*, not the dotted module path
class SoftDeleteMixinTests(SimpleTestCase):
    def test_mixin_adds_manager(self):
        class Thing(SoftDeleteMixin, models.Model):
            class Meta:
                app_label = "core"
        self.assertTrue(hasattr(Thing, "all_objects"))
```

Use for abstract base classes and mixins; models defined this way have no tables unless created
with a schema editor in a transactional test.

### Tags and selection

```python
from django.test import tag

@tag("slow", "integration")
class ExportTests(TestCase): ...
```

```bash
python manage.py test --exclude-tag=slow                   # fast local loop
python manage.py test --tag=integration --parallel auto
python manage.py test apps.orders -k refund --failfast
python manage.py test --shuffle                            # Django ≥ 4.0; prints the seed
python manage.py test --shuffle 1234                       # reproduce an order-dependent failure
python manage.py test --durations 20                       # Django ≥ 5.0, Python ≥ 3.12
python manage.py test --keepdb --buffer --pdb
python manage.py test apps.reports --debug-sql -v 2        # print SQL of failing tests
python manage.py test --reverse                            # quick order-dependence check
```

### Parallel execution **[Django]**

- `--parallel N` or `--parallel auto` (worker count from CPU count; `DJANGO_TEST_PROCESSES`
  env var overrides). Parallelism is per **test case class**, so one huge class does not split.
- Requires picklable test results (tracebacks with unpicklable objects cause errors —
  `tblib` (**[third-party: tblib]**) improves traceback transfer).
- macOS/Windows use the `spawn` start method; support for parallel runs under `spawn` was added
  in Django 4.1 — `VERIFY` for your version. Module-level state set in the parent is not inherited
  under `spawn`.
- Each worker gets its own cloned DB; shared external resources (files on disk, Redis DB 0,
  fixed ports) must be namespaced per worker.

### Query assertions **[Django]**

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

def test_list_queries(self):
    with self.assertNumQueries(3):
        self.client.get(reverse("orders:list"))

    with CaptureQueriesContext(connection) as ctx:
        list(order_list(user=self.customer.user))
    self.assertLessEqual(len(ctx.captured_queries), 2, [q["sql"] for q in ctx.captured_queries])
```

Budget design is owned by [performance-and-reliability-testing.md](performance-and-reliability-testing.md).

### Custom test runner

```python
# config/test_runner.py
from django.test.runner import DiscoverRunner

class ProjectRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        import socket
        def guard(*args, **kw):
            raise RuntimeError("Network access in tests")  # crude guard; prefer transport mocks
        socket.create_connection = guard   # VERIFY: also block socket.socket.connect for full coverage
```

Set `TEST_RUNNER = "config.test_runner.ProjectRunner"`. Keep custom runners small; prefer
settings and fixtures over runner hacks. A JUnit XML reporter is available via
**[third-party: unittest-xml-reporting]** (`xmlrunner.extra.djangotestrunner.XMLTestRunner`).

### Renamed / version-sensitive assertions

| API | Note |
|---|---|
| `assertQuerySetEqual` | Renamed from `assertQuerysetEqual` in Django 4.2; old name removed in 5.1 |
| `assertFormError(form, field, errors)` | Signature changed in Django 4.1 (form object, not response); old form removed in 5.0 |
| `assertNumQueries(using=...)` | Count per alias |
| `captureOnCommitCallbacks` | Django ≥ 3.2; captures callbacks registered during execution of callbacks since 4.0 (`VERIFY`) |
| `assertMessages` (`MessagesTestMixin`) | Django ≥ 5.0 |

## 6. Production use cases

- **Legacy unittest suite with 4,000 `TestCase`s**: keep the classes; run them under pytest for
  fixtures and plugins without rewriting; enable `--parallel` or xdist after an isolation pass
  with `--shuffle`.
- **Read replica routing**: `TEST: {"MIRROR": "default"}` on the replica so tests of
  replica-routed selectors see data written to the primary; separate tests assert the router's
  choice.
- **PostgreSQL extensions** (`pg_trgm`, `citext`): created in a migration (`TrigramExtension()`),
  so `MIGRATE=False` breaks tests; keep migrations on and use `--keepdb` for speed.
- **Feature flags in settings**: `override_settings` per test; flags read at call time.

## 7. Advantages and disadvantages

| Aspect | Advantages | Disadvantages |
|---|---|---|
| Django runner | Zero extra dependencies; class ordering protects isolation; built-in `--parallel`, `--shuffle`, `--durations` | unittest-style setup; fewer plugins; parallel granularity per class |
| `TestCase` rollback | Fast, automatic isolation | No real commit; hides commit-time behaviour |
| `TransactionTestCase` | Real commit semantics | Slow; flushes data-migration rows |
| `setUpTestData` | Big speed-up for shared read data | Must be deep-copyable; encourages oversized shared fixtures |
| `override_settings` | Scoped, reversible | Ineffective for values captured at import |

## 8. Security and performance considerations

- Test settings must not inherit production secrets or endpoints; a missing env var should fail
  loudly, not fall back to production values.
- The test DB role needs `CREATEDB` — use a dedicated CI/dev role, never a production role.
- `--keepdb` databases persist test data on shared hosts; use per-developer or containerised DBs.
- Performance levers, in order: fast password hasher → `TestCase` over `TransactionTestCase` →
  `setUpTestData` → `--keepdb` → `--parallel` → fewer migrations (squash).
- `--debug-sql` and `-v 3` may print sensitive test data in CI logs — acceptable only with
  synthetic data.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| `TransactionTestCase` "to make on_commit work" | Slow suite | `captureOnCommitCallbacks(execute=True)` |
| Catching `IntegrityError` without nested `atomic()` | `TransactionManagementError` / aborted transaction later in test | `with self.assertRaises(IntegrityError), transaction.atomic():` |
| Assigning `settings.X = …` directly | Leaks to later tests | `override_settings` |
| Module-level queries (e.g. `CHOICES = list(Category.objects…)`) | Hit the real DB at import/discovery, or fail with no tables | Lazy evaluation; callable choices |
| `setUpTestData` storing non-copyable objects | Errors on access (Django ≥ 3.2) | Store only model instances/plain data |
| Hard-coded `pk=1` | Fails with sequences, parallel runs, `--keepdb` | Use created objects |
| `MIGRATE=False` everywhere | Tests miss extensions, triggers, data migrations | Keep migrations; squash |
| Relying on test order | Breaks under `--shuffle`/`--parallel` | Isolate data per test |
| Huge test classes | Can't parallelise (per-class granularity) | Split by behaviour |

## 10. Troubleshooting and validation

| Symptom | Cause | Action |
|---|---|---|
| `Got an error creating the test database: permission denied` | Role lacks `CREATEDB` | Grant in dev/CI or set `TEST["NAME"]` to a pre-created DB |
| `database "test_app" already exists` | Previous run aborted | Answer prompt / `--noinput` (destroys) or `--keepdb` |
| Tests pass with `--keepdb`, fail fresh (or vice versa) | Stale schema or missing migration | Run once without `--keepdb`; `makemigrations --check` |
| `AssertionError: Database queries to 'default' are not allowed in SimpleTestCase` | DB use in a `SimpleTestCase` | Use `TestCase` or set `databases` |
| `TransactionManagementError: An error occurred in the current transaction` | Swallowed DB error inside the test transaction | Wrap expected error in `atomic()` |
| Data from data migration missing in later tests | A `TransactionTestCase` flushed tables | `serialized_rollback = True` on that class, or recreate data in tests |
| Passes serially, fails with `--parallel` | Shared files/ports/cache/Redis; unpicklable failures | Namespace per worker; see [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |
| Override has no effect | Value captured at import | Read at call time; reset in `setting_changed` receiver |

Validation: run `python manage.py test --shuffle --parallel auto` periodically; inspect
`--durations 20`; confirm `python manage.py check` and `makemigrations --check --dry-run` pass
under test settings.

## 11. Related references and skills

- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — pytest equivalents of everything here.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — commit semantics,
  locking and multi-DB in depth.
- [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md) — test `Client`
  and response assertions.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — leakage
  and order bugs.
- [django-core: settings-configuration](../../django-core/references/settings-configuration.md) —
  settings module structure.
- [django-orm: transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md) —
  `atomic`, savepoints and `on_commit` mechanics.
- [django-orm: migrations-and-schema-evolution](../../django-orm/references/migrations-and-schema-evolution.md) —
  squashing and migration design.

## 12. Official documentation and external references

- Writing and running tests: <https://docs.djangoproject.com/en/5.2/topics/testing/overview/>
- Testing tools (`TestCase`, client, assertions): <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Advanced testing (runner, `RequestFactory`, `isolate_apps`): <https://docs.djangoproject.com/en/5.2/topics/testing/advanced/>
- `test` command options: <https://docs.djangoproject.com/en/5.2/ref/django-admin/#test>
- `DATABASES["TEST"]`: <https://docs.djangoproject.com/en/5.2/ref/settings/#test>
- Django 4.2 / 5.0 / 5.1 / 5.2 release notes (assertion renames, `--durations`): <https://docs.djangoproject.com/en/5.2/releases/>
- Python `unittest`: <https://docs.python.org/3/library/unittest.html>
