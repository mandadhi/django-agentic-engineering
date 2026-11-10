# Async Django Performance

**Scope:** When asynchronous Django improves latency or capacity, when it makes things worse, and
how to measure the difference — covering async views, sync/async boundaries, the async ORM
interface, concurrent I/O and ASGI deployment. API mechanics of async Django are owned by
[django-core asynchronous-django](../../django-core/references/asynchronous-django.md).

## 1. Purpose and scope

Use this reference when:

- Someone proposes converting views to `async def` "for performance".
- A view calls several slow external services sequentially.
- The app needs many long-lived connections (SSE, long-poll, streaming).
- An ASGI deployment is slower than the WSGI one it replaced.
- The event loop appears stalled (all requests on a process slow at once).

Out of scope: worker/process sizing ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)),
WebSockets/Channels design ([django-core asgi-wsgi](../../django-core/references/asgi-wsgi.md)),
async test mechanics ([django-testing async-testing](../../django-testing/references/async-testing.md)).

## 2. Core concepts

- **Async buys concurrency of waiting, not speed.** A single request that does one DB query and
  renders a template is not faster as a coroutine; coroutine scheduling and thread hops add
  overhead. The win comes when (a) one request awaits several independent I/O operations
  concurrently, or (b) one process must hold many mostly-idle connections.
- **Cooperative scheduling.** A coroutine runs until it `await`s. Any blocking call (sync ORM,
  `requests`, `time.sleep`, heavy CPU) stalls *every* coroutine on that event loop.
- **Boundaries cost.** Each sync→async or async→sync hop is a thread handoff (tens to hundreds of
  microseconds, illustrative); hops per request multiply.
- **Fan-out latency.** Sequential: `t = Σ tᵢ`. Concurrent: `t ≈ max(tᵢ)` + overhead. Three 200 ms
  upstream calls: ~600 ms sequential vs ~200 ms concurrent.

| Workload | Async likely helps? |
|---|---|
| Fan-out to several external HTTP APIs per request | Yes |
| SSE / long-poll / streaming to many clients | Yes |
| Proxying/gateway endpoints dominated by upstream waits | Yes |
| CRUD on the ORM, one or two queries | No — usually slightly slower |
| CPU-heavy rendering, serialization, image work | No — move to processes/tasks |
| Code relying on sync-only libraries | No — every call becomes a thread hop |

## 3. Internal mechanics

### Request path under ASGI

1. The ASGI server (Uvicorn, Daphne, Hypercorn) calls Django's `ASGIHandler` on the event loop.
2. Django builds the middleware chain. Each middleware is adapted to the mode of the next layer:
   a sync-only middleware in an async stack is wrapped with `sync_to_async`, an async view under
   sync middleware is wrapped with `async_to_sync` — each adaptation is a thread switch. Django
   logs adaptations at DEBUG level on the `django.request` logger ("Asynchronous handler adapted
   for middleware …").
3. A **sync view** under ASGI runs via `sync_to_async(thread_sensitive=True)`: all such calls in a
   process share a single executor thread, so sync views in an ASGI process are effectively
   serialised. This is why a sync app under ASGI usually has worse throughput than under gthread.
4. An **async view** runs on the loop; its awaited I/O interleaves with other requests.

### `sync_to_async` / `async_to_sync` (asgiref)

- `sync_to_async(fn, thread_sensitive=True)` (default) runs `fn` in the one shared
  thread-sensitive thread (or the outer sync thread when nested inside `async_to_sync`). Required
  for code that touches the ORM connection or non-thread-safe state.
- `thread_sensitive=False` runs on a thread pool — real parallelism for thread-safe blocking I/O
  (e.g. a sync SDK), but must not touch Django DB connections.
- Django raises `SynchronousOnlyOperation` if sync-only code (ORM queries) is called directly from
  a running event loop. `DJANGO_ALLOW_ASYNC_UNSAFE` disables the check — never in production.

### Async ORM interface (Django ≥ 4.1)

- `aget()`, `acreate()`, `acount()`, `aexists()`, `async for` over QuerySets, `asave()`/`adelete()`
  (Django ≥ 4.2), async related managers (Django ≥ 5.0) — `VERIFY` exact method list per version.
- Internally these still call the sync ORM through `sync_to_async`: queries execute on a thread,
  not on the loop. They remove boilerplate and prevent blocking, but do **not** make DB access
  concurrent within one request; `asyncio.gather` over several `aget()`s still runs them through
  the single thread-sensitive thread, one after another.
- Transactions: `transaction.atomic()` is not supported in async code; wrap the transactional
  unit in a sync function and call it via `sync_to_async`. `VERIFY` whether Django 6.0 changes
  this; do not assume an async-native driver path exists in 5.2.

### Connections under ASGI

- Django closes connections at request end per `CONN_MAX_AGE`; with async code the DB work
  happens in executor threads and persistent connections are tied to those threads. Django's docs
  advise `CONN_MAX_AGE = 0` (or a pool) under ASGI because persistent connections are not
  reliably reused/closed in async mode — `VERIFY` the current wording for 5.2.
- Prefer the Django ≥ 5.1 psycopg pool or PgBouncer for ASGI deployments
  ([database-performance.md](database-performance.md)).

### Streaming

`StreamingHttpResponse` accepts an async iterator (Django ≥ 4.2). Under ASGI a sync iterator is
consumed in a thread (and buffered/converted, with a warning — `VERIFY`); under WSGI an async
iterator is consumed via `async_to_sync`. Match the iterator to the server type.

## 4. Engineering decisions and trade-offs

**Decide with a benchmark, not a belief.** For the candidate endpoint, implement the async
version, load-test both under the target server, and compare p50/p95/p99 and throughput at the
same resource budget ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).

| Option | Bottleneck addressed | Trade-offs | Makes it worse when |
|---|---|---|---|
| Async view with `asyncio.gather` over HTTP calls | Sequential upstream waits | Error handling and cancellation complexity; upstream load spikes (N calls at once) | Upstreams are rate-limited, or calls are dependent |
| Keep sync view, parallelise with `ThreadPoolExecutor` | Same, on WSGI | Threads per request; limit pool size | Under heavy load: threads × requests explode |
| Move the call to a background task | Upstream latency on the request path | Eventual consistency; UI polling | User needs the result now |
| ASGI for SSE/long-poll | Idle connections pinning sync workers | Async-only code in that path; separate deployment advisable | Endpoint does blocking work per event |
| Full-ASGI migration of a sync app | — | Every sync view serialised through one thread per process | Almost always, unless views are converted |

A common sound design: keep the main app on WSGI (gthread), and route a small set of streaming
or fan-out endpoints to a separate ASGI deployment of the same codebase.

## 5. Django implementation patterns

### Concurrent fan-out with bounded concurrency and timeouts

```python
import asyncio
import httpx  # [third-party: httpx]
from django.http import JsonResponse

# One client per process: reuses TCP/TLS connections. Create lazily; close at shutdown.
_client: httpx.AsyncClient | None = None

def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client

async def quote_view(request):
    sku = request.GET["sku"]
    async def fetch(url: str):
        r = await client().get(url, params={"sku": sku})
        r.raise_for_status()
        return r.json()

    # Python ≥ 3.11: TaskGroup cancels siblings on failure; asyncio.timeout bounds the whole fan-out
    async with asyncio.timeout(4):
        async with asyncio.TaskGroup() as tg:
            price = tg.create_task(fetch(PRICING_URL))
            stock = tg.create_task(fetch(STOCK_URL))
            eta = tg.create_task(fetch(SHIPPING_URL))
    return JsonResponse({"price": price.result(), "stock": stock.result(), "eta": eta.result()})
```

Creating an `AsyncClient` per request discards connection reuse and adds TLS handshakes; a
module-level client is safe per event loop. With multiple loops (tests, `async_to_sync`) bind the
client to the loop or create it in an ASGI lifespan handler — `VERIFY` your server's lifespan
support for Django (Django itself does not implement lifespan events in 5.2).

Use `asyncio.gather(..., return_exceptions=True)` instead of TaskGroup when partial results are
acceptable (degrade gracefully).

### Bounding fan-out to protect upstreams

```python
_sem = asyncio.Semaphore(20)   # per process; size from the upstream's rate limit

async def fetch_limited(url):
    async with _sem:
        return await client().get(url)
```

### ORM access in async views

```python
from asgiref.sync import sync_to_async
from django.db import transaction

async def order_summary(request, pk):
    order = await Order.objects.select_related("customer").aget(pk=pk, customer__user=request.user)
    lines = [line async for line in order.lines.all()]       # async iteration (Django ≥ 4.1)
    return JsonResponse({...})

@sync_to_async            # thread_sensitive=True by default — required for ORM + transactions
def _place_order(user_id, payload):
    with transaction.atomic():
        ...

async def place_order_view(request):
    order_id = await _place_order(request.user.id, json.loads(request.body))
    return JsonResponse({"id": order_id}, status=201)
```

Batch ORM work into one `sync_to_async` call rather than many small hops.

Note: `request.user` is lazy and sync; in async views use `await request.auser()` (Django ≥ 5.0).

### Wrapping a blocking SDK without blocking the loop

```python
upload = sync_to_async(s3_client.upload_fileobj, thread_sensitive=False)  # thread-safe, no ORM
await upload(fileobj, bucket, key)
```

### Async streaming (SSE)

```python
from django.http import StreamingHttpResponse

async def events(request):
    async def stream():
        async for event in subscribe(request.user.id):     # e.g. redis.asyncio pub/sub [third-party: redis]
            yield f"data: {event}\n\n"
    resp = StreamingHttpResponse(stream(), content_type="text/event-stream")  # Django ≥ 4.2
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"    # disable nginx buffering for this response
    return resp
```

Client disconnects raise `asyncio.CancelledError` in the generator (Django ≥ 5.0 handles
disconnect for streaming responses — `VERIFY`); release subscriptions in `finally`.

### Keep the middleware stack async-capable

```python
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.utils.decorators import sync_and_async_middleware

@sync_and_async_middleware
def timing_middleware(get_response):
    if iscoroutinefunction(get_response):
        async def middleware(request):
            start = time.perf_counter()
            response = await get_response(request)
            response["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
            return response
    else:
        def middleware(request):
            start = time.perf_counter()
            response = get_response(request)
            response["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
            return response
    return middleware
```

Audit third-party middleware: one sync-only middleware forces a thread hop for every request.

### Detecting loop blocking

```python
# Development/staging only: asyncio debug mode logs callbacks that block the loop
# PYTHONASYNCIODEBUG=1 uvicorn config.asgi:application
import asyncio, logging
loop = asyncio.get_running_loop()
loop.slow_callback_duration = 0.05        # log anything that blocks > 50 ms
logging.getLogger("asyncio").setLevel(logging.WARNING)
```

In production, use a sampling profiler (py-spy dump shows the loop thread's stack) or APM loop-lag
metrics ([profiling-and-observability.md](profiling-and-observability.md)). A cheap loop-lag probe:
schedule `asyncio.sleep(1)` repeatedly and record overshoot.

## 6. Production use cases

1. **Price comparison endpoint.** Four partner APIs called sequentially, p95 1.4 s. Async view
   with TaskGroup and a 1.5 s overall timeout: p95 450 ms (illustrative); partner B's rate limit
   required a 10-slot semaphore after a 429 storm.
2. **"Async migration" regression.** A team moved a CRUD DRF app to Uvicorn without converting
   views (DRF 3.16 views are sync). Throughput per pod halved: every view serialised through the
   thread-sensitive executor. Rolled back to Gunicorn gthread.
3. **Notification stream.** SSE endpoint on WSGI pinned one thread per connected browser;
   2,000 users exhausted all pods. Moved to a dedicated ASGI deployment; web pods untouched.
4. **Stalled event loop.** A helper used `requests.get` inside an async view; one slow partner
   froze all requests on the process. Detected via loop-lag metric spikes coinciding with partner
   latency; fixed with `httpx.AsyncClient`.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Latency ≈ max instead of sum for independent I/O | No gain (often loss) for ORM/CPU-bound views |
| Cheap long-lived connections | Async ORM still thread-backed; no async transactions in 5.2 |
| Fine-grained timeouts and cancellation | One blocking call stalls the whole process |
| Lower memory per concurrent connection | DRF views are sync; mixed stacks add thread hops |
|  | Harder debugging, testing and profiling; library compatibility audit needed |

## 8. Security and performance considerations

- Concurrency limits are security controls: unbounded fan-out lets one request amplify into N
  upstream calls (cost and abuse risk). Bound with semaphores and validated input sizes.
- Timeouts on every await prevent resource exhaustion from slow upstreams.
- Long-lived streams need authentication checked at connect and re-checked/expired for long
  sessions; cap connections per user.
- `DJANGO_ALLOW_ASYNC_UNSAFE` in production can corrupt shared connection state.
- Cancellation (client disconnect) must not leave half-applied writes — keep writes inside one
  sync transactional function.

## 9. Common mistakes and anti-patterns

- `async def` on views that only use the ORM, then claiming a speed-up without measurement.
- Calling sync libraries (`requests`, sync boto3, sync Redis client) from coroutines.
- Many tiny `sync_to_async` calls per request instead of one batched sync function.
- `thread_sensitive=False` around ORM code.
- `asyncio.gather` over ORM calls expecting parallel queries.
- New `httpx.AsyncClient()` per request.
- No overall timeout on a fan-out; one upstream hangs the request until the server timeout.
- Running Uvicorn with sync-only middleware (some third-party packages) and not noticing hops.
- Persistent DB connections (`CONN_MAX_AGE > 0`) under ASGI leading to connection leaks.
- Comparing async vs sync under WSGI (`async` views on WSGI run in a per-request event loop and
  are strictly slower).

## 10. Troubleshooting and validation

| Symptom | Check |
|---|---|
| ASGI slower than WSGI | Count sync views/middleware; DEBUG log on `django.request` for "adapted" messages; compare per-endpoint |
| All requests on a pod slow simultaneously | Loop lag; py-spy dump for blocking calls on the main thread |
| `SynchronousOnlyOperation` | ORM or sync-only code called on the loop — wrap or use `a`-methods |
| Rising DB connections under ASGI | `CONN_MAX_AGE`; switch to pool/PgBouncer; `pg_stat_activity` by `application_name` |
| `RuntimeError: Event loop is closed` / client errors in tests | HTTP client bound to another loop |
| Upstream 429s after async rollout | Missing concurrency bound |

Validation:

1. Benchmark sync and async variants of the same endpoint on the intended server, same CPU/memory.
2. Measure p50/p95/p99 and throughput, plus loop lag and thread-pool queue depth.
3. Load-test failure modes: one upstream slow (2 s), one returning errors, client disconnects.
4. Verify connection counts (DB, HTTP) stay bounded through a soak test.
5. Keep the async version only if it wins on the metric that motivated it.

## 11. Related references and skills

- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — server/worker models, GIL, sizing.
- [database-performance.md](database-performance.md) — pooling under ASGI.
- [api-performance.md](api-performance.md) — DRF serialization cost (DRF views are sync).
- [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md) — streaming and proxy buffering.
- [background-task-performance.md](background-task-performance.md) — moving slow I/O off the request.
- [profiling-and-observability.md](profiling-and-observability.md) — profiling async code.
- [django-core asynchronous-django](../../django-core/references/asynchronous-django.md) — async API mechanics.
- [django-core asgi-wsgi](../../django-core/references/asgi-wsgi.md) — ASGI servers, Channels.
- [django-testing async-testing](../../django-testing/references/async-testing.md) — testing async views.
- [django-integrations](../../django-integrations/SKILL.md) — external API client design.

## 12. Official documentation and external references

- Django async support: https://docs.djangoproject.com/en/5.2/topics/async/
- Async ORM queries: https://docs.djangoproject.com/en/5.2/topics/async/#queries-the-orm
- Asynchronous middleware: https://docs.djangoproject.com/en/5.2/topics/http/middleware/#asynchronous-support
- ASGI deployment: https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
- Persistent connections caveat: https://docs.djangoproject.com/en/5.2/ref/databases/#persistent-connections
- StreamingHttpResponse: https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects
- asgiref sync utilities: https://github.com/django/asgiref
- ASGI specification: https://asgi.readthedocs.io/en/latest/
- Python asyncio (TaskGroup, timeout, debug mode): https://docs.python.org/3/library/asyncio-task.html · https://docs.python.org/3/library/asyncio-dev.html
- Uvicorn: https://www.uvicorn.org/
- HTTPX async client [third-party]: https://www.python-httpx.org/async/
