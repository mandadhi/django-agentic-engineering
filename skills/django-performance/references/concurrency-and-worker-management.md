# Concurrency and Worker Management

**Scope:** How Django serves concurrent requests — process, thread and event-loop models under
WSGI and ASGI servers, how to size workers and connection pools from measurement, and how to
detect and handle saturation. Server mechanics and configuration basics live in
[django-core asgi-wsgi](../../django-core/references/asgi-wsgi.md); async view design lives in
[async-django-performance.md](async-django-performance.md).

## 1. Purpose and scope

Use this reference when:

- Choosing a Gunicorn/Uvicorn worker class or changing worker/thread counts.
- Requests queue under load while CPU is idle (or CPU is pegged while latency explodes).
- PostgreSQL reports `FATAL: sorry, too many clients already` or pool-timeout errors.
- A deploy scaled out instances and the database fell over.
- Investigating race conditions caused by shared mutable state in threaded workers.

Out of scope: theory of queueing (see [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md)),
pool configuration syntax (see [database-performance.md](database-performance.md)), Celery worker
concurrency (see [background-task-performance.md](background-task-performance.md)), autoscaling
policies (see [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)).

## 2. Core concepts

| Concept | Meaning for a Django service |
|---|---|
| **Worker process** | An OS process with its own interpreter, GIL, memory and DB connections. Parallelism for CPU work. |
| **Thread** | Shares the process's memory and GIL. Concurrency for I/O waits; no CPU parallelism for pure-Python code. |
| **Event loop** | One thread multiplexing many coroutines. Concurrency only while coroutines `await` I/O. |
| **Concurrency slots** | Requests a single instance can be *executing* at once: `workers × threads` (sync/gthread), or bounded by the loop + thread pool (ASGI). |
| **Queue time** | Time a request waits before a slot picks it up — invisible to in-app timers. |
| **Saturation** | Arrival rate × service time ≥ slots; queue time grows without bound. |

### The GIL in practice

- CPython releases the GIL during blocking I/O (socket reads, DB driver waits via psycopg's C
  layer, file reads) and in many C extensions. Threads therefore overlap **waiting**.
- Pure-Python CPU work (template rendering, serializer loops, JSON encoding in Python) holds the
  GIL. Two threads doing it run at ~1× total throughput, plus switching overhead.
- Free-threaded CPython (PEP 703, Python 3.13+ as an experimental build, `python3.13t`) removes
  the GIL. It is **not** a Django production baseline: extension compatibility (psycopg, lxml,
  Pillow…) and Django's own thread-safety assumptions are still maturing. `VERIFY` support status
  for the specific Python/Django/driver versions before considering it.

### Little's Law for sizing

`L = λ × W` — average concurrent requests in the system = arrival rate × average time in system.

Illustrative: 400 req/s at p50 service time 50 ms → `400 × 0.05 = 20` requests in flight on
average. Bursts and tail latency mean you need headroom well above the average; size against a
high percentile of service time and peak arrival rate, not means.

## 3. Internal mechanics

### Gunicorn (WSGI)

Gunicorn runs a master (arbiter) that forks N workers; the master never handles requests. Each
worker accepts from the shared listen socket.

| Worker class | Concurrency per worker | Fits | Caveats |
|---|---|---|---|
| `sync` | 1 request | CPU-heavy views; simplest; behind a buffering proxy | A slow client or slow upstream ties up the worker; needs a buffering proxy (nginx) in front |
| `gthread` | `--threads` requests | Mixed I/O-bound Django apps (DB, cache, HTTP calls) | Shared memory → thread-safety matters; each thread may hold its own DB connection |
| `gevent` / `eventlet` [third-party] | Hundreds of greenlets | Many slow outbound calls on legacy sync code | Monkey-patching; psycopg 3 needs gevent-compatible waiting (psycopg 2 needs `psycogreen` [third-party]) — `VERIFY` for your driver version; CPU work blocks all greenlets; incompatible with Django's async-safety checks in surprising ways |
| `uvicorn` worker | Event loop + thread pool | ASGI under Gunicorn's process management | See below |

Key settings: `workers`, `threads`, `timeout` (worker silent for N seconds is killed and
restarted — for `sync` that means a single request exceeding it), `graceful_timeout` (time to
finish in-flight requests on restart), `keepalive`, `backlog` (listen queue depth, default 2048),
`max_requests`/`max_requests_jitter` (recycling — see
[memory-and-resource-management.md](memory-and-resource-management.md)), `preload_app`.

### Uvicorn / ASGI

- `uvicorn --workers N` runs N processes, each with one event loop.
- Running Uvicorn under Gunicorn used the `uvicorn.workers.UvicornWorker` class; this class has
  been deprecated in favour of the separate `uvicorn-worker` [third-party: uvicorn-worker]
  package (`uvicorn_worker.UvicornWorker`). `VERIFY` for the installed Uvicorn version.
- Django's ASGI handler runs sync views (and sync middleware) through `sync_to_async`; with the
  default `thread_sensitive=True` they execute on **one shared thread per process**, serialising
  sync work. A fully sync app under ASGI therefore often has *less* concurrency than gthread.
- Daphne (Channels) and Hypercorn are alternatives; Hypercorn supports HTTP/2/3. Choose by
  protocol need, then benchmark; raw server speed rarely dominates Django latency.

### Where requests wait

```text
client → LB/ingress queue → proxy (nginx) buffer → listen socket backlog
       → worker accept → [thread/loop slot] → Django middleware/view → DB pool wait → DB
```

Each arrow is a queue. In-app request timers only see the last segments. Queue time hides in
the proxy and the socket backlog.

## 4. Engineering decisions and trade-offs

### Choosing the model

| Workload (measured) | Default choice | Why |
|---|---|---|
| Mostly DB/cache waits, sync code | Gunicorn `gthread`, few workers × several threads | Overlaps I/O waits; memory-efficient vs more processes |
| CPU-heavy (rendering, PDF, heavy serialization) | Gunicorn `sync`/`gthread` with threads=1–2, workers ≈ available cores | Only processes give CPU parallelism |
| Long-held connections (SSE, WebSockets, long-poll), concurrent outbound I/O fan-out | ASGI (Uvicorn) with async views | Thousands of idle connections don't consume threads |
| Legacy sync code calling slow external APIs | First move the call to a task; gevent only if that is impossible | Monkey-patching is a global, hard-to-debug change |

### Sizing method (no magic numbers)

Gunicorn's docs suggest `(2 × cores) + 1` workers as a *starting point*. Treat it as a first
guess to benchmark, not a recommendation. Derive instead:

1. Measure per-request **CPU time** vs **wall time** for the dominant endpoints (APM span
   breakdown, or `time.process_time()` vs `time.perf_counter()` in middleware).
   CPU fraction `c = cpu_time / wall_time`.
2. Processes needed to use the CPU: ≈ available cores (container CPU limit, not host cores).
3. Concurrency per process needed to keep that core busy ≈ `1 / c` (e.g. `c = 0.25` → ~4
   threads). Higher than this only adds GIL contention and memory.
4. Required slots from Little's Law at peak with headroom; scale instances when per-instance
   slots are exhausted.
5. Check memory: `workers × RSS_per_worker` (+ thread stacks) must fit the container limit with
   margin for growth.
6. Check DB connections (below).
7. Load-test the candidate configuration; compare p95/p99 and throughput against the baseline
   ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).

**When more workers make it worse:** CPU-bound workers beyond cores add context switching; more
threads than `1/c` add GIL contention; every slot may add a DB connection and push PostgreSQL
into contention; more processes multiply memory and can trigger OOM kills.

### Database connection budget

```text
max_db_connections_needed =
    Σ over web instances (workers × threads)       # persistent conns / pool max
  + Σ over Celery workers (concurrency)            # prefork: one per child
  + cron/management commands, migrations, admin tools, replicas' monitors
  ≤ PostgreSQL max_connections − superuser_reserved_connections − headroom
```

Illustrative: 6 pods × 4 workers × 8 threads = 192 + Celery 3 × 16 = 48 → 240 connections. A
PostgreSQL with `max_connections=200` fails at peak — or, worse, only after the HPA scales to 6
pods. Remedies, in order: fewer idle slots (right-size threads), Django ≥ 5.1 psycopg connection
pool with a bounded `max_size` per process, or an external pooler (PgBouncer). Pool semantics,
`CONN_MAX_AGE`, `CONN_HEALTH_CHECKS` and PgBouncer transaction-mode caveats are in
[database-performance.md](database-performance.md); PostgreSQL-side tuning in
[postgresql-engineering](../../postgresql-engineering/SKILL.md).

More connections is not more throughput: past a point (often a small multiple of DB cores —
`VERIFY` against your own benchmark), PostgreSQL throughput drops from contention.

### Timeouts form a chain

Inner timeouts must be shorter than outer ones, or the outer layer gives up while the inner work
continues wasting capacity:

```text
client/LB idle timeout > proxy_read_timeout > gunicorn timeout > statement_timeout / HTTP client timeout
```

## 5. Django implementation patterns

### Gunicorn configuration as code

```python
# gunicorn.conf.py — values are illustrative; derive from measurement
import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.environ.get("WEB_WORKERS", "2"))      # ≈ CPU limit of the container
threads = int(os.environ.get("WEB_THREADS", "4"))      # ≈ 1 / cpu_fraction, measured
timeout = int(os.environ.get("WEB_TIMEOUT", "30"))     # < proxy_read_timeout
graceful_timeout = 25                                  # < orchestrator termination grace period
keepalive = 5                                          # > 0 only behind a proxy that reuses conns; < LB idle timeout
max_requests = 2000
max_requests_jitter = 200                              # avoid synchronized restarts
accesslog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus'  # %(D)s = request time in microseconds
```

Keep worker settings in environment-driven config so capacity changes do not need a code deploy.

### Database settings consistent with the slot count

```python
# settings.py — Django ≥ 5.1 native pool (psycopg 3, PostgreSQL only)
DATABASES["default"]["OPTIONS"] = {
    "pool": {
        "min_size": 2,
        "max_size": int(os.environ.get("DB_POOL_MAX", "8")),  # ≥ threads per process
        "timeout": 5,                                         # fail fast instead of queueing forever
    },
}
# With a pool, CONN_MAX_AGE must stay 0 (Django raises an error otherwise — VERIFY message per version).
```

### Measuring queue time

Have the proxy stamp arrival time and compute the wait in middleware:

```nginx
proxy_set_header X-Request-Start "t=${msec}";
```

```python
import time
import logging

logger = logging.getLogger("perf.queue")

class QueueTimeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.META.get("HTTP_X_REQUEST_START", "")
        if raw.startswith("t="):
            try:
                queued_ms = (time.time() - float(raw[2:])) * 1000
                request.queue_time_ms = queued_ms
                if queued_ms > 100:          # illustrative alert threshold
                    logger.warning("request queued %.0f ms", queued_ms)
            except ValueError:
                pass
        return self.get_response(request)
```

Clock skew between proxy and app hosts distorts this; use it for trends on the same host, or
rely on the APM agent's queue-time support. Never trust the header from the public internet —
set it at your own edge and strip client-supplied values.

### Thread-safe shared state

```python
# BAD: class attribute mutated per request — shared by every thread in the process
class ReportView(View):
    rows = []                      # shared!
    def get(self, request):
        self.rows.append(...)      # races, leaks data between users

# GOOD: per-request state is local; process-wide caches are bounded and locked
import threading
from functools import lru_cache

_rates_lock = threading.Lock()
_rates: dict[str, float] = {}

def get_rate(code: str) -> float:
    with _rates_lock:              # short critical section; never hold during I/O
        if code in _rates:
            return _rates[code]
    value = fetch_rate(code)       # I/O outside the lock
    with _rates_lock:
        _rates.setdefault(code, value)
        return _rates[code]

@lru_cache(maxsize=1024)           # bounded; lru_cache is thread-safe for its own bookkeeping
def parse_schema(name: str) -> dict: ...
```

Django's CBV `as_view()` creates a new instance per request, so *instance* attributes are safe;
*class* attributes and module globals are not. `django.core.cache.caches[...]` returns a
per-thread backend instance; `LocMemCache` is per-process (not shared across workers).

### Backpressure: fail fast instead of queueing forever

- Bound queues explicitly: proxy `limit_conn`/`limit_req`, Gunicorn `backlog` sized so excess
  load gets rejected early rather than timing out after 30 s.
- Short DB pool `timeout` → convert to `503` with `Retry-After` instead of piling up threads.
- Put expensive endpoints behind a separate worker pool/deployment so they cannot starve cheap
  ones (bulkheading). Load-shedding policy: [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md).

## 6. Production use cases

1. **I/O-heavy B2B API, latency rising at 60% CPU.** Sync workers (4 per pod) each spend 70% of
   wall time waiting on PostgreSQL and Redis. Switching to `gthread` with 4 workers × 4 threads
   raised throughput per pod ~3× (illustrative) at the same CPU; DB connections rose from 4 to 16
   per pod, so the pool and `max_connections` budget were recomputed first.
2. **CPU-bound HTML rendering.** Adding threads did nothing; py-spy showed 90% CPU in template
   rendering. Correct fix: cache fragments and scale processes to cores, not threads.
3. **Scale-out outage.** HPA scaled from 4 to 12 pods during a campaign; connection count tripled
   and PostgreSQL rejected connections. Fix: PgBouncer + per-process pool caps + an HPA max derived
   from the connection budget.
4. **Slow third-party webhook verification.** A sync view called a partner API with no timeout;
   a partner outage pinned every worker for 30 s until Gunicorn killed them. Fix: 3 s client
   timeout, circuit breaker, move the call to a task.
5. **Data leak via class attribute.** A view cached `self.__class__.current_tenant`; under
   `gthread` concurrent requests read another tenant's ID. Found via code review after a support
   ticket; fix: request-local state, regression test with concurrent requests.

## 7. Advantages and disadvantages

| Model | Advantages | Disadvantages |
|---|---|---|
| Processes (sync) | True CPU parallelism, isolation, simple debugging | High memory per slot; slow clients block slots |
| Threads (gthread) | Cheap I/O concurrency, lower memory | GIL limits CPU work; thread-safety burden; more DB connections |
| Greenlets (gevent) | Very high I/O concurrency for sync code | Monkey-patching, driver compatibility, CPU blocks everything |
| ASGI event loop | Massive idle-connection concurrency, native async I/O | Sync code funnels through a thread; blocking calls stall the loop; more complex failure modes |

## 8. Security and performance considerations

- Slow-client attacks (slowloris) exhaust `sync` workers — always run behind a buffering reverse
  proxy with client body/header timeouts.
- Unbounded request bodies tie up workers: enforce `client_max_body_size` at the proxy and
  `DATA_UPLOAD_MAX_MEMORY_SIZE` in Django.
- Timeouts on every outbound call (DB `statement_timeout`, HTTP client timeouts) are a resilience
  control as much as a performance one.
- Thread-unsafe shared state is a confidentiality bug (cross-user data), not just a correctness one.
- Trust `X-Request-Start`/`X-Forwarded-*` only from your own proxy layer.

## 9. Common mistakes and anti-patterns

- Copying `(2 × cores) + 1` without measuring, or using host cores inside a CPU-limited container
  (`os.cpu_count()` reports host CPUs; `os.process_cpu_count()` exists in Python ≥ 3.13 but still
  may not reflect cgroup quotas — `VERIFY`).
- Raising `timeout` to "fix" worker kills, hiding a slow dependency and lowering capacity.
- Increasing threads for CPU-bound views.
- Running a fully sync Django app under ASGI and expecting more concurrency.
- Scaling instances without recomputing the DB connection budget.
- Setting `CONN_MAX_AGE=None` with many threads and many pods → thousands of idle connections.
- Mutating module globals or class attributes per request.
- Holding a lock while doing I/O, serialising all threads behind the slowest call.
- Unbounded queues everywhere: requests wait 30 s and then fail anyway, after consuming resources.
- Gunicorn `graceful_timeout` longer than the orchestrator's termination grace period → in-flight
  requests SIGKILLed on every deploy.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| High latency, low CPU, low DB load | Queueing before Django (too few slots) | Queue-time metric; `ss -lnt` Recv-Q on the listen socket; proxy upstream response vs request time |
| High latency, CPU at limit | CPU-bound work or GIL contention | py-spy top/dump on a worker ([profiling-and-observability.md](profiling-and-observability.md)); CPU throttling metrics (`container_cpu_cfs_throttled_seconds_total`) |
| `WORKER TIMEOUT` in Gunicorn logs | A request exceeded `timeout` (often an external call) | Correlate with slow spans; check outbound client timeouts |
| `too many clients already` / pool timeout | Connection budget exceeded | `SELECT count(*), state FROM pg_stat_activity GROUP BY state;` vs computed budget |
| Many `idle in transaction` connections | Code holding transactions across I/O | `pg_stat_activity` `state`, `xact_start`; see [database-performance.md](database-performance.md) |
| Intermittent wrong data between users | Shared mutable state | Grep for class-level mutable attributes and module-level dicts; concurrency test |
| Errors only during deploys | Graceful shutdown misconfigured | Compare `graceful_timeout`, `terminationGracePeriodSeconds`, LB deregistration delay |

Validation procedure for any concurrency change:

1. Record baseline: throughput at fixed p95 target, p50/p95/p99, CPU, RSS per worker, DB
   connections, queue time.
2. Change one variable (worker class, workers, threads).
3. Re-run the same load profile ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)),
   including a step past expected peak to find the saturation knee.
4. Accept only if throughput at the latency SLO improved and memory/DB connection budgets hold.

## 11. Related references and skills

- [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md) — Little's Law, queueing, percentiles.
- [async-django-performance.md](async-django-performance.md) — async views and ASGI behaviour.
- [database-performance.md](database-performance.md) — pooling, `CONN_MAX_AGE`, PgBouncer.
- [memory-and-resource-management.md](memory-and-resource-management.md) — worker memory, recycling.
- [background-task-performance.md](background-task-performance.md) — Celery concurrency.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — scaling, load shedding.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — worker/pool exhaustion incidents.
- [django-core asgi-wsgi](../../django-core/references/asgi-wsgi.md) — server mechanics and deployment basics.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — `max_connections`, PgBouncer internals.

## 12. Official documentation and external references

- Django deployment: https://docs.djangoproject.com/en/5.2/howto/deployment/
- Django database connection management / pooling: https://docs.djangoproject.com/en/5.2/ref/databases/#connection-management
- Gunicorn settings: https://docs.gunicorn.org/en/stable/settings.html
- Gunicorn design (worker types, how many workers): https://docs.gunicorn.org/en/stable/design.html
- Uvicorn deployment: https://www.uvicorn.org/deployment/
- uvicorn-worker [third-party]: https://github.com/Kludex/uvicorn-worker
- ASGI specification: https://asgi.readthedocs.io/en/latest/
- Python `threading`: https://docs.python.org/3/library/threading.html
- Free-threaded CPython: https://docs.python.org/3/howto/free-threading-python.html
- PostgreSQL connection settings: https://www.postgresql.org/docs/current/runtime-config-connection.html
- psycopg 3 pool: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
