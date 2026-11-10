# Asynchronous Django

> Scope: async views, async-capable middleware (capability flags only), the async ORM interface,
> `sync_to_async` / `async_to_sync`, thread sensitivity, blocking calls, transactions in async
> code, async external I/O and testing. Not covered here: server interfaces and deployment
> ([asgi-wsgi.md](./asgi-wsgi.md)), writing middleware ([middleware.md](./middleware.md)), ORM
> query design ([django-orm](../../django-orm/SKILL.md)), background job systems
> ([django-integrations](../../django-integrations/SKILL.md)).

## Purpose and overview

Async Django lets one worker serve many requests that are *waiting* — on HTTP APIs, slow
upstreams, streaming, long-polling — without one thread per request. It does **not** make CPU
work or the ORM faster. Async support has been added incrementally; always check the installed
version (`python -m django --version`) before using an API below.

| Capability | Status |
|---|---|
| `async def` views (FBV and CBV handlers) | Django ≥ 3.1 |
| Async-capable middleware | ≥ 3.1 |
| Async cache API (`aget`, `aset`, …) | ≥ 4.0 |
| Async ORM query interface (`aget`, `acreate`, `acount`, `async for`) | ≥ 4.1 |
| `Model.asave()` / `adelete()`, async related-manager methods | ≥ 4.2 |
| Async iterators in `StreamingHttpResponse` | ≥ 4.2 |
| `Signal.asend()` / `asend_robust()`, async receivers | ≥ 5.0 |
| `aauthenticate`, `alogin`, `alogout`, `aget_user`, `request.auser()` | ≥ 5.0 |
| Async session methods (`aget`, `aset`, …) | 5.1+ — verify in release notes |
| Async transactions (`atomic` in async code) | **Not supported** (as of 5.2/6.0) |
| Native async database drivers | **Not used** — ORM calls run sync in a thread |

## How it works internally

- Django detects coroutine functions with `asgiref.sync.iscoroutinefunction`. Under **ASGI**, an
  async view runs directly on the event loop; a sync view is wrapped in
  `sync_to_async(thread_sensitive=True)` and runs in a shared thread.
- Under **WSGI**, an async view is run in a one-off event loop per request — it works, but you
  get no concurrency benefit.
- The middleware chain is adapted at startup: each middleware declares `sync_capable` /
  `async_capable`. Wherever adjacent layers disagree, Django inserts an adapter. Every switch
  costs roughly a thread hop (~1 ms), so **one sync-only middleware in an async stack forces the
  whole request through a thread** around it.
- The async ORM methods (`aget`, `acount`, …) are wrappers that call the sync implementation via
  `sync_to_async`. The database driver is still blocking; Django runs it in a thread so the loop
  isn't blocked.
- `sync_to_async(thread_sensitive=True)` (the default) runs all thread-sensitive calls for a
  request in **one** thread (the main thread or a per-request executor), because database
  connections and many C libraries are bound to the thread that created them.
- Code that touches the DB from inside a running event loop without going through the adapters
  raises `SynchronousOnlyOperation` — Django's guard against blocking the loop.

## Core concepts and APIs

```python
from asgiref.sync import sync_to_async, async_to_sync

sync_to_async(func, thread_sensitive=True)   # call blocking code from async
async_to_sync(coro_func)                     # call async code from sync (e.g. a management command)
```

- `thread_sensitive=False` runs in a thread pool — only for code that is thread-safe and doesn't
  touch the ORM or thread-local state.
- `async_to_sync` cannot be used inside a running event loop (it raises); use `await` there.
- Context variables (`contextvars`) propagate across both adapters; `threading.local` does not
  reliably — prefer `contextvars` or `asgiref.local.Local` for per-request state.
- QuerySet building (`filter`, `exclude`, `select_related`) is lazy and safe in async code; only
  **evaluation** must be async (`async for`, `aget`, `afirst`, `aexists`, `acount`,
  `ain_bulk`, `aaggregate`, `aupdate`, `adelete`, `acreate`, `aget_or_create`, `abulk_create`, …).
- Lazy relation access (`order.customer` not prefetched) evaluates synchronously →
  `SynchronousOnlyOperation`. Use `select_related`/`prefetch_related` or `await` an explicit query.

## Implementation examples

Concurrent external I/O — the main reason to go async:

```python
# apps/orders/views.py
import asyncio
import httpx
from django.http import JsonResponse

async def shipping_quotes(request, order_id: int):
    order = await Order.objects.select_related("customer").aget(pk=order_id, customer=await request.auser())
    async with httpx.AsyncClient(timeout=5.0) as client:      # third-party async HTTP client
        results = await asyncio.gather(
            client.get(f"https://carrier-a.example/quote/{order.pk}"),
            client.get(f"https://carrier-b.example/quote/{order.pk}"),
            return_exceptions=True,
        )
    quotes = [r.json() for r in results if not isinstance(r, Exception) and r.is_success]
    return JsonResponse({"order": order.pk, "quotes": quotes})
```

Transactions — wrap the whole unit of work in one sync function:

```python
# Bad: atomic() around awaits is not supported; the awaited calls may run on different
# threads/connections, so the "transaction" protects nothing.
async def checkout(request):
    with transaction.atomic():
        order = await Order.objects.acreate(...)
        await Payment.objects.acreate(order=order, ...)

# Good: keep the transactional service sync and call it once.
async def checkout(request):
    user = await request.auser()
    order = await sync_to_async(place_order)(customer=user, items=parse_items(request))
    return JsonResponse({"id": order.pk}, status=201)
```

Class-based view — all handlers must be async or all sync (mixing raises `ImproperlyConfigured`):

```python
from django.views import View

class OrderStatusView(View):
    async def get(self, request, pk: int):
        status = await Order.objects.filter(pk=pk).values_list("status", flat=True).afirst()
        return JsonResponse({"status": status})
```

Calling async code from sync code (management command, Celery task):

```python
def handle(self, *args, **options):
    quotes = async_to_sync(fetch_all_quotes)(options["order_id"])
```

Async streaming (≥ 4.2):

```python
async def order_events(request):
    async def stream():
        async for event in OrderEvent.objects.filter(order_id=request.GET["id"]).aiterator():
            yield f"data: {event.payload}\n\n"
    return StreamingHttpResponse(stream(), content_type="text/event-stream")
```

## Production patterns

- Deploy under ASGI (see [asgi-wsgi.md](./asgi-wsgi.md)) and make the **middleware stack fully
  async-capable**; audit third-party middleware. Turn on the `django.request` DEBUG log to see
  "Asynchronous handler adapted for middleware …" messages.
- **Database connections under ASGI:** persistent connections (`CONN_MAX_AGE > 0`) are not
  well-suited to async because the thread pool can create many connections; the docs recommend
  disabling them in async mode and using pooling — the backend's pool (PostgreSQL
  `OPTIONS={"pool": True}` with psycopg 3, Django ≥ 5.1) or an external pooler. See
  [postgresql-engineering](../../postgresql-engineering/SKILL.md).
- Always set **timeouts** on outbound async calls; one hung upstream otherwise holds a request
  open indefinitely.
- Background work is not "fire-and-forget `asyncio.create_task`" in a view — the task may be
  cancelled when the request ends or the worker restarts. Use a task queue. Django 6.0 adds a
  Tasks framework (`django.tasks`) defining the interface; a real backend/worker is still needed —
  check the 6.0 release notes.
- Handle client disconnects in long streams: ASGI servers cancel the request; let
  `asyncio.CancelledError` propagate after cleanup.

## Decision guidance

| Situation | Choose |
|---|---|
| Typical CRUD, ORM-bound pages | Sync views (WSGI or ASGI) — simpler, no benefit from async |
| View awaits several network calls | Async view, `asyncio.gather` with timeouts |
| Streaming / SSE / long-polling | Async view under ASGI |
| WebSockets | Django Channels on ASGI ([asgi-wsgi.md](./asgi-wsgi.md)) |
| CPU-heavy work | Neither — offload to a task queue or process pool |
| Multi-step DB writes needing atomicity | Sync service in `transaction.atomic`, called via `sync_to_async` |
| Blocking SDK with no async version | `sync_to_async(..., thread_sensitive=False)` if thread-safe |

Rule: introduce async where there is concurrent waiting to exploit; don't convert a codebase
"for performance" without measuring ([django-performance](../../django-performance/SKILL.md)).

## Security considerations

- Use `await request.auser()` (≥ 5.0) instead of `request.user` in async views; the latter is a
  lazy object that triggers a sync DB query.
- Decorators like `login_required`, `csrf_protect`, `require_POST` support async views in recent
  versions (most from 5.0) — verify per decorator in your version's docs before relying on them.
- `DJANGO_ALLOW_ASYNC_UNSAFE` disables the `SynchronousOnlyOperation` guard. Use it only in
  Jupyter/REPL environments; never in production — it hides blocking calls and allows
  cross-thread connection use.

## Performance considerations

- Blocking calls inside `async def` (`requests.get`, `time.sleep`, sync ORM via a library, file
  I/O on slow disks) freeze **every** request on that worker. Use async libraries or wrap with
  `sync_to_async`.
- Each sync↔async switch is a thread hop; many small `await Model.objects.aget(...)` calls are
  slower than one sync function doing several queries. Batch DB work into one `sync_to_async`.
- Thread-sensitive calls are serialised per request into one thread — they don't run in parallel
  with each other.
- Run with `PYTHONASYNCIODEBUG=1` / asyncio debug mode in dev to log slow callbacks.

## Testing

```python
from django.test import TestCase

class ShippingQuoteTests(TestCase):
    async def test_requires_owner(self):          # async test methods supported since 3.1
        user = await User.objects.acreate(username="a")
        await self.async_client.aforce_login(user)  # aforce_login: Django ≥ 5.0 — verify
        response = await self.async_client.get("/orders/999/quotes/")
        self.assertEqual(response.status_code, 404)
```

- `self.async_client` is an `AsyncClient`; `AsyncRequestFactory` builds ASGI requests for
  calling views directly.
- `TestCase` wraps each test in a transaction; async tests run the ORM through the same
  thread-sensitive executor so data is visible — but code that uses `thread_sensitive=False`
  for DB access will not see the test transaction.
- Mock outbound async HTTP (e.g. `respx` for httpx) and assert timeouts are handled.
- More patterns → [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- Making views `async` while every line inside is sync ORM — pure overhead.
- `async_to_sync` inside an async view (raises `RuntimeError`) — just `await`.
- Accessing un-prefetched relations or `request.user` attributes in async code →
  `SynchronousOnlyOperation`.
- `transaction.atomic()` around `await`s.
- `sync_to_async(..., thread_sensitive=False)` around ORM code (breaks connection/thread affinity).
- Using `threading.local` for request state; use `contextvars`.
- Sharing a single `httpx.AsyncClient` created at import time across event loops (tests create
  new loops) — create per request or manage lifecycle carefully.

## Troubleshooting

| Error / symptom | Cause → fix |
|---|---|
| `SynchronousOnlyOperation: You cannot call this from an async context` | Sync ORM/lazy relation in async code → `a*` method, `select_related`, or `sync_to_async` |
| `RuntimeError: You cannot use AsyncToSync in the same thread as an async event loop` | `async_to_sync` inside async code → `await` directly |
| Async view no faster | Running under WSGI, sync middleware adapters, or work is CPU/DB-bound |
| Throughput collapses under load | A blocking call in an async path; find with asyncio debug mode |
| "Too many connections" on ASGI | Persistent connections + thread pool → disable `CONN_MAX_AGE`, use pooling |
| `ImproperlyConfigured` on CBV | Mixed sync and async handlers on one view class |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/async/
- https://docs.djangoproject.com/en/stable/topics/db/queries/#asynchronous-queries
- https://docs.djangoproject.com/en/stable/topics/http/middleware/#asynchronous-support
- https://docs.djangoproject.com/en/stable/topics/testing/tools/#testing-asynchronous-code
- https://docs.djangoproject.com/en/stable/ref/databases/#connection-pool
- https://docs.python.org/3/library/asyncio.html
- https://docs.python.org/3/library/contextvars.html
