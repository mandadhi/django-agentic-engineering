# Async testing

> **Scope:** How to test async Django code — async views, async service functions, async ORM
> access, async HTTP clients and concurrency scenarios — with Django's test tools, pytest-django
> and pytest-asyncio, including the database-visibility and event-loop pitfalls specific to async tests.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · pytest-asyncio ≥ 0.24 · Python ≥ 3.11 ·
> **Depends on:** **[Django]** `AsyncClient`, `AsyncRequestFactory`, async test methods ·
> **[pytest-django]** `async_client`, `async_rf`, `django_db` · **[third-party: pytest-asyncio]** or
> **[third-party: anyio]** pytest plugin · **[Python]** `asyncio`, `unittest.mock.AsyncMock` ·
> optional **[third-party: httpx, respx, channels]**

## 1. Purpose and scope

Use this reference when the code under test contains `async def` views, async service functions,
async ORM calls (`aget`, `acreate`, `async for`), outbound calls with an async HTTP client, or
concurrency built on `asyncio`. It covers:

- Which tool runs an async test: Django's `TestCase` (built-in support) versus pytest with an
  async plugin (pytest-asyncio or anyio).
- How the async ORM interacts with the test transaction, and why data can be invisible.
- Testing async services, async HTTP integrations, timeouts and concurrent execution.
- Streaming responses and (briefly) Channels consumers.

Out of scope here: how Django's async stack works (event loop per request, `sync_to_async`
thread-sensitivity, ASGI handler) → [django-core asynchronous-django.md](../../django-core/references/asynchronous-django.md)
and [asgi-wsgi.md](../../django-core/references/asgi-wsgi.md). Transaction mechanics in general →
[database-and-transaction-testing.md](database-and-transaction-testing.md). Mocking mechanics →
[mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md).

## 2. Core concepts

| Concept | Provenance | Meaning for tests |
|---|---|---|
| `AsyncClient` | **[Django]** (≥ 3.1) | Like `Client`, but `await client.get(...)`; runs the request through the ASGI handler, full middleware stack. |
| `AsyncRequestFactory` | **[Django]** (≥ 3.1) | Builds an `ASGIRequest`; call an async view directly, no middleware. |
| `async def test_*` in `SimpleTestCase`/`TestCase` | **[Django]** (≥ 3.1) | Django wraps the coroutine with `async_to_sync`; no plugin required. |
| `async_client`, `async_rf` fixtures | **[pytest-django]** | pytest equivalents of `AsyncClient` and `AsyncRequestFactory` (pytest-django ≥ 4.6 — `VERIFY` exact version). |
| pytest-asyncio | **[third-party: pytest-asyncio]** | Runs `async def` tests/fixtures in an event loop. Modes: `strict` (default, needs `@pytest.mark.asyncio`) and `auto`. |
| anyio plugin | **[third-party: anyio]** | `@pytest.mark.anyio`; ships with anyio (a dependency of httpx/Starlette). Alternative to pytest-asyncio — use one, not both. |
| Async ORM | **[Django]** (≥ 4.1) | `aget`, `acreate`, `afirst`, `acount`, `aexists`, `async for`, … Still executes the query synchronously in a worker thread via `sync_to_async`. |
| `SynchronousOnlyOperation` | **[Django]** | Raised when sync-only code (e.g. ORM) is called from a thread with a running event loop. |
| `AsyncMock` | **[Python]** (≥ 3.8) | Mock whose calls return awaitables; supports `assert_awaited_once_with`. |

Key idea: an async test is still a test of **behaviour**. The extra concerns are *which thread
and connection* runs each query and *which loop* owns each coroutine/fixture.

## 3. Internal mechanics

### How Django runs async tests

`SimpleTestCase` detects coroutine test methods and wraps them with `async_to_sync`. The test
method runs in a fresh event loop, and `sync_to_async(thread_sensitive=True)` calls (which is what
the async ORM uses) are routed **back to the main thread** — the same thread, and therefore the
same DB connection, that opened the `TestCase` transaction. Result: data from `setUpTestData` or
created earlier in the test is visible to `await Model.objects.aget(...)`.

### How pytest-asyncio runs async tests

pytest-asyncio drives the coroutine on an event loop it manages in the **main thread**. When the
code awaits `sync_to_async(thread_sensitive=True)` and there is no outer `async_to_sync` context,
asgiref executes the sync function in a **single shared executor thread**, not the main thread.
Django connections are per-thread, so the async ORM call uses a *different connection* from the
one holding pytest-django's open test transaction.

Consequences (behaviour differs by backend — `VERIFY` against your setup):

- Rows created in the test (main-thread connection, uncommitted) are **invisible** to async ORM
  calls → `DoesNotExist`, empty querysets, or the test hanging on a row lock (PostgreSQL).
- Writes made by async ORM calls are on another connection and are **not rolled back** by the
  test's transaction.

The robust fix is `@pytest.mark.django_db(transaction=True)` (truncate-based isolation; every
statement commits, every connection sees it). Slower, but correct. Alternatively write async
tests as Django `TestCase` methods, which avoid the problem.

### Event-loop scope (pytest-asyncio)

- Each async test runs in a loop with a configurable **loop scope** (function by default).
  pytest-asyncio ≥ 0.24 introduced `loop_scope=` on `@pytest.mark.asyncio` and on
  `@pytest_asyncio.fixture`, and the ini option `asyncio_default_fixture_loop_scope` (`VERIFY`).
- An async fixture and the test that uses it must share a loop. A session-scoped async client
  created in one loop and awaited in another fails with "attached to a different loop" /
  "Event loop is closed".
- pytest-asyncio 1.0 removed the long-deprecated `event_loop` fixture; custom loops are
  configured via loop scopes and `event_loop_policy` instead (`VERIFY` for the installed version).

### Async ORM is not concurrent DB access

`await Order.objects.acount()` is `sync_to_async(count)()`. Thread-sensitive calls are
serialised on one thread, so `asyncio.gather` of ten ORM calls does **not** run ten queries in
parallel. Concurrency tests that need parallel DB work must use threads and transactional tests
([database-and-transaction-testing.md](database-and-transaction-testing.md)).

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Choose |
|---|---|---|---|
| Async test runner | Django `TestCase` async methods | pytest + pytest-asyncio / anyio | `TestCase` when async tests need DB fixtures cheaply; pytest when the suite is pytest and fixtures matter more. Both are valid in one pytest run. |
| DB isolation for pytest async tests | `django_db` (rollback) | `django_db(transaction=True)` | `transaction=True` whenever the async code path touches the ORM; plain `django_db` only for async tests that do no async ORM I/O. |
| Plugin | pytest-asyncio | anyio | Whichever the project already has; anyio if code uses anyio/trio APIs. Never both active on one test. |
| Test the view | `async_client` (full ASGI stack) | `AsyncRequestFactory` (view only) | Client for behaviour (middleware, auth, routing); factory for isolated view logic or when middleware setup is costly. |
| Async service under test with sync collaborators | Real DB via async ORM | Fake repository/gateway | Real DB for query correctness; fakes for external I/O only. |
| Concurrency | `asyncio.gather` in one loop | Threads + `TransactionTestCase` | `gather` to test cooperative concurrency logic (fan-out, timeouts, cancellation); threads for DB races. |

When **not** to write async tests: a sync view running under ASGI does not need async tests —
test it with the normal `client`. Write async tests only where the code under test is a coroutine.

## 5. Django implementation patterns

### Configuration (pytest-asyncio)

```toml
# pyproject.toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"
asyncio_mode = "strict"                       # explicit @pytest.mark.asyncio
asyncio_default_fixture_loop_scope = "function"   # pytest-asyncio >= 0.24 (VERIFY)
```

`auto` mode marks every `async def` test and fixture as asyncio automatically — convenient, but it
conflicts with anyio-based tests in the same project.

### Async view through the full stack

```python
# apps/catalog/tests/test_async_views.py
import pytest
from asgiref.sync import sync_to_async
from django.urls import reverse

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def test_product_detail_returns_price(async_client):
    product = await sync_to_async(ProductFactory)(price="12.50")   # factories are sync
    response = await async_client.get(reverse("catalog:product-detail", args=[product.pk]))
    assert response.status_code == 200
    assert response.json()["price"] == "12.50"
```

factory_boy has no native async API (`VERIFY` for your version). Alternatively create data in a
sync fixture that depends on `transactional_db`:

```python
@pytest.fixture
def product(transactional_db):
    return ProductFactory(price="12.50")      # sync fixture, committed (transactional_db)

async def test_product_detail(async_client, product):
    response = await async_client.get(f"/products/{product.pk}/")
    assert response.status_code == 200
```

### Same test with Django's TestCase (no plugin, rollback isolation)

```python
from django.test import TestCase


class ProductDetailAsyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.product = ProductFactory(price="12.50")

    async def test_returns_price(self):
        response = await self.async_client.get(f"/products/{self.product.pk}/")
        self.assertEqual(response.status_code, 200)
        # async ORM sees setUpTestData rows: same thread/connection
        self.assertTrue(await Product.objects.filter(pk=self.product.pk).aexists())
```

`self.async_client` is an `AsyncClient` attribute on `SimpleTestCase` (Django ≥ 3.1).

### Authenticated async requests

```python
async def test_requires_login(async_client, user):
    response = await async_client.get("/orders/")
    assert response.status_code == 302

    await async_client.aforce_login(user)       # Django >= 5.0 (VERIFY); else sync_to_async(client.force_login)
    response = await async_client.get("/orders/")
    assert response.status_code == 200
```

### Calling an async view directly with AsyncRequestFactory

```python
from django.contrib.auth.models import AnonymousUser


async def test_health_view_reports_degraded_cache(async_rf, monkeypatch):
    monkeypatch.setattr("apps.ops.views.ping_cache", AsyncMock(return_value=False))
    request = async_rf.get("/health/")
    request.user = AnonymousUser()            # no middleware ran
    response = await health(request)
    assert response.status_code == 503
```

### Async service with an async gateway

```python
from unittest.mock import AsyncMock

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_inventory_marks_out_of_stock():
    product = await sync_to_async(ProductFactory)(sku="SKU-1", stock=5)
    gateway = AsyncMock(spec=InventoryGateway)
    gateway.fetch_levels.return_value = {"SKU-1": 0}

    await sync_inventory(gateway=gateway)

    await product.arefresh_from_db()               # Django >= 5.0 (VERIFY)
    assert product.stock == 0
    gateway.fetch_levels.assert_awaited_once_with(["SKU-1"])
```

Use `spec=`/`create_autospec` so a renamed gateway method fails the test instead of silently
returning a new mock.

### Async HTTP client mocked at the transport

```python
import httpx
import respx   # third-party

@pytest.mark.asyncio
@respx.mock
async def test_fetch_rate_retries_once_on_503():
    route = respx.get("https://rates.example.test/v1/EUR").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"rate": "1.08"})]
    )
    async with httpx.AsyncClient(timeout=2) as client:
        assert await fetch_rate(client, "EUR") == Decimal("1.08")
    assert route.call_count == 2
```

Mocks prove your retry logic, not the provider's behaviour — see
[integration-and-background-task-testing.md](integration-and-background-task-testing.md) for contract tests.

### Timeouts and cancellation

```python
import asyncio

@pytest.mark.asyncio
async def test_aggregator_returns_partial_results_on_slow_provider():
    async def slow(*_):
        await asyncio.sleep(10)

    providers = [AsyncMock(return_value=["a"]), AsyncMock(side_effect=slow)]
    async with asyncio.timeout(1):                         # Python >= 3.11
        results = await aggregate_quotes(providers, per_provider_timeout=0.05)
    assert results == ["a"]
```

The outer `asyncio.timeout` guards the test itself; the inner per-provider timeout is the
behaviour under test. Keep test timeouts tiny — never real-world seconds.

### Concurrency in one loop

```python
@pytest.mark.asyncio
async def test_dedupe_cache_calls_upstream_once_for_concurrent_requests():
    upstream = AsyncMock(return_value={"id": 1})
    cache = SingleFlight(upstream)
    results = await asyncio.gather(*(cache.get("k") for _ in range(20)))
    assert all(r == {"id": 1} for r in results)
    upstream.assert_awaited_once()
```

### Streaming responses (Django ≥ 4.2 async iterators)

```python
@pytest.mark.asyncio
async def test_export_streams_csv_rows(async_client, transactional_db):
    OrderFactory.create_batch(3)
    response = await async_client.get("/exports/orders.csv")
    assert response.streaming
    chunks = [chunk async for chunk in response.streaming_content]
    body = b"".join(chunks).decode()
    assert body.count("\n") == 4                            # header + 3 rows
```

Consuming an async iterator through the sync `Client` triggers a warning and buffering; use the
async client for async streaming views.

### Channels consumers **[third-party: channels]**

```python
from channels.testing import WebsocketCommunicator

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_order_updates_socket_rejects_anonymous():
    communicator = WebsocketCommunicator(application, "/ws/orders/")
    connected, _ = await communicator.connect()
    assert not connected
    await communicator.disconnect()
```

`ChannelsLiveServerTestCase` exists for browser tests against a real ASGI server; prefer
`WebsocketCommunicator` for consumer logic.

### Sync views under ASGI

Sync views still work under ASGI (Django runs them in a thread). Test them with the normal
`client`; add one smoke test with `async_client` only if middleware is async-capable and you need
to prove the ASGI path (e.g. an async-only middleware).

## 6. Production use cases

- **Aggregator endpoint** calling three providers concurrently: test partial-failure handling,
  per-provider timeouts and cancellation with `AsyncMock` + `asyncio.timeout`.
- **Server-sent events / streaming export**: assert chunk format, termination and that the DB
  cursor is closed on client disconnect.
- **Async webhook receiver** that verifies a signature and enqueues work: `async_client.post`
  with a valid and an invalid signature; assert enqueue only on valid.
- **Health checks** that ping cache, DB and broker concurrently: assert 503 when any dependency
  times out.
- **Channels notifications**: auth rejection, group broadcast on order status change.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Django `TestCase` async methods | No plugin; rollback isolation works with async ORM; `setUpTestData` | No pytest fixtures for the test body; one loop per test |
| pytest-asyncio | Async fixtures, parametrisation, loop scopes | Needs `transaction=True` for async ORM; loop-scope mismatches; plugin version churn |
| anyio plugin | Backend-agnostic (asyncio/trio); already installed with httpx | Less familiar; conflicts with pytest-asyncio auto mode |
| `AsyncClient` | Exercises ASGI handler and middleware | Slower than calling the view; hides which layer failed |
| `AsyncRequestFactory` | Fast, isolated | No middleware: must set `user`, `session` manually |

## 8. Security and performance considerations

- **Security:** async views use the same auth/CSRF middleware — run access tests (anonymous,
  wrong user, other tenant) through `async_client`, not only the factory, so middleware ordering
  is exercised. `AsyncClient` does not enforce CSRF by default (`enforce_csrf_checks=True`).
- **`DJANGO_ALLOW_ASYNC_UNSAFE`** disables the `SynchronousOnlyOperation` guard. Set it only in
  test processes that genuinely need it (e.g. Playwright's sync API), never in production
  settings or images — it hides real blocking-I/O bugs.
- **Performance:** `transaction=True` tests truncate all tables after each test — keep the async
  DB-touching tests few and focused; test pure async logic (no DB) without any DB marker.
- Blocking calls inside async code (sync HTTP, `time.sleep`) pass tests but stall production
  loops. Enable asyncio debug mode in a CI job (`PYTHONASYNCIODEBUG=1`) to log slow callbacks.

## 9. Common mistakes and anti-patterns

- Async test without a marker in strict mode → the coroutine is never awaited; pytest warns and
  the test "passes". Treat `PytestUnraisableExceptionWarning`/"coroutine was never awaited" as errors.
- Plain `@pytest.mark.django_db` with async ORM under pytest-asyncio → flaky `DoesNotExist` or a
  hang on PostgreSQL row locks.
- Session-scoped async fixtures with a function-scoped loop → "attached to a different loop".
- Wrapping a sync view test in `async def` for no reason.
- `asyncio.sleep(0.5)` to "wait" for a background coroutine — await the task or use an `Event`.
- Using `MagicMock` for an awaited dependency → `TypeError: object MagicMock can't be used in
  'await' expression`; use `AsyncMock` (or autospec of an async function, which yields one).
- Asserting `assert_called_once` on an `AsyncMock` instead of `assert_awaited_once` — a called
  but never-awaited coroutine passes the former.
- Expecting `asyncio.gather` over async ORM calls to exercise DB concurrency.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| `SynchronousOnlyOperation` | Sync ORM call inside a coroutine (often a lazy FK access or a sync factory) | Use `a*` methods / `sync_to_async`; `select_related` before the async boundary |
| `DoesNotExist` in async test, row visibly created | Different connection (pytest-asyncio + rollback isolation) | `django_db(transaction=True)` or Django `TestCase` |
| Test hangs on PostgreSQL | Async ORM thread waits on a lock held by the uncommitted test transaction | Same as above |
| "Event loop is closed" / "different loop" | Fixture and test loop scopes differ | Align `loop_scope`; avoid caching loop-bound clients at session scope |
| "coroutine was never awaited" | Missing `asyncio` marker in strict mode, or missing `await` | Add marker; `filterwarnings = ["error::RuntimeWarning"]` |
| Data persists between async tests | Writes made on another connection escaped rollback | `transaction=True` |
| Async test passes alone, fails in xdist | Shared module-level client/loop | Create clients per test via fixtures |

Validation: run the async subset with `-p no:randomly` and then randomised; ensure no
"never awaited" warnings; grep for `DJANGO_ALLOW_ASYNC_UNSAFE` outside test config.

## 11. Related references and skills

- [database-and-transaction-testing.md](database-and-transaction-testing.md) — rollback vs truncate isolation, thread-based race tests.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — `AsyncMock`, autospec, patch targets.
- [integration-and-background-task-testing.md](integration-and-background-task-testing.md) — contract tests for real providers.
- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — fixtures, markers, plugin configuration.
- [browser-and-end-to-end-testing.md](browser-and-end-to-end-testing.md) — Playwright sync API and `DJANGO_ALLOW_ASYNC_UNSAFE`.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — flaky async tests.
- django-core: [asynchronous-django.md](../../django-core/references/asynchronous-django.md), [asgi-wsgi.md](../../django-core/references/asgi-wsgi.md).
- [django-integrations](../../django-integrations/SKILL.md) — async HTTP clients, Channels in production.

## 12. Official documentation and external references

- Django — Testing asynchronous code: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#testing-asynchronous-code>
- Django — Async support and async ORM: <https://docs.djangoproject.com/en/5.2/topics/async/>
- pytest-django — helpers (`async_client`, `async_rf`): <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- pytest-asyncio: <https://pytest-asyncio.readthedocs.io/en/stable/>
- AnyIO pytest plugin: <https://anyio.readthedocs.io/en/stable/testing.html>
- Python `unittest.mock.AsyncMock`: <https://docs.python.org/3/library/unittest.mock.html#unittest.mock.AsyncMock>
- Python `asyncio.timeout`: <https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout>
- RESPX: <https://lundberg.github.io/respx/>
- Channels testing: <https://channels.readthedocs.io/en/latest/topics/testing.html>
