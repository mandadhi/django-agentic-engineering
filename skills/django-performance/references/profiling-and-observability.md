# Profiling and Observability

**Scope:** How to obtain the numbers every performance decision needs: code profilers (cProfile,
pyinstrument, py-spy), development inspectors (Debug Toolbar, Silk), request and query timing,
metrics, distributed tracing with OpenTelemetry, flame graphs and production-safe profiling. This
reference owns tool usage; other references link here instead of re-explaining tools.

## 1. Purpose and scope

Use when you need to locate a bottleneck, attribute latency to a component, instrument a Django
service, profile in production without hurting it, or follow a slow request across services.

Not covered here: memory profiling in depth (tracemalloc, memray →
[memory-and-resource-management.md](memory-and-resource-management.md)), PostgreSQL-side analysis
(`EXPLAIN`, `pg_stat_statements` → [database-performance.md](database-performance.md)), load
generation ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)),
logging configuration mechanics ([django-core logging](../../django-core/references/logging-exceptions.md)).

## 2. Core concepts

### Four tool families — do not confuse them

| Family | Question it answers | Examples | Where |
|---|---|---|---|
| **Profilers** | *Which functions* consume CPU/wall time inside one process? | cProfile (stdlib), pyinstrument [third-party], py-spy [third-party] | Dev, staging; py-spy also prod |
| **Dev inspectors** | What did *this request* do (SQL, templates, cache, signals)? | Django Debug Toolbar [third-party: django-debug-toolbar], Silk [third-party: django-silk] | Dev/staging only |
| **Metrics/monitoring** | *How often* and *how slow* across all traffic; resource saturation? | Prometheus, OpenTelemetry metrics, cloud monitoring, `pg_stat_statements` | Prod |
| **Tracing** | *Where* in a distributed request did time go? | OpenTelemetry traces → Jaeger/Tempo/vendor APM | Prod (sampled) |

Logs complete the picture: they explain *why* a specific event happened (error, slow-query details),
correlated to traces by trace ID.

A profiler tells you `serialize_order` takes 60 % of CPU; monitoring tells you `/api/orders/` p95
doubled at 14:05; tracing tells you the extra time was a call to the pricing service. Infrastructure
monitoring (node CPU, container memory) is not application profiling: high node CPU says a process is
busy, not which code.

### Deterministic vs sampling profilers

| | Deterministic (cProfile) | Statistical/sampling (pyinstrument, py-spy) |
|---|---|---|
| Mechanism | Hooks every call/return | Snapshots stacks every N ms |
| Overhead | High (often 1.5–3×, worse for many small calls — illustrative) | Low (py-spy runs out-of-process; pyinstrument ~ms interval) |
| Output | Exact call counts, per-function totals | Proportional time, call trees, flame graphs |
| Distortion | Over-weights tiny hot functions | Misses very short functions; statistically accurate for hot paths |
| Prod-safe | No | py-spy yes (with care); pyinstrument on sampled requests only |

### Wall time vs CPU time

Profilers in **wall** mode include I/O waits (DB, HTTP) — what users feel. **CPU** mode shows only
computation. pyinstrument reports wall time by default; py-spy samples all threads and with `--idle`
includes waiting threads. Compare both to classify a request as CPU- or I/O-bound
([performance-engineering-fundamentals.md](performance-engineering-fundamentals.md)).

## 3. Internal mechanics

- **cProfile** uses `sys.setprofile`-style C hooks; per-call accounting cost makes code with many
  cheap calls (template rendering, serializers) look worse than it is.
- **pyinstrument** uses a timer-driven stack sampler inside the process, then aggregates into a call
  tree and hides library frames by default; its Django middleware profiles selected requests.
- **py-spy** reads the target process memory via `ptrace`/`process_vm_readv` (Linux) and walks the
  interpreter's frame structures — no code change, no restart. In containers it needs the
  `SYS_PTRACE` capability or a shared PID namespace sidecar.
- **Python 3.12+** supports the Linux `perf` profiler via `-X perf` / `PYTHONPERFSUPPORT=1`, letting
  system profilers show Python frames. Python 3.15 adds a stdlib sampling profiler (PEP 799,
  `profiling.sampling`) — `VERIFY` availability and CLI for your interpreter version.
- **Debug Toolbar** patches the DB cursor and wraps template rendering and cache calls per request;
  each panel adds overhead, and SQL explain/select panels re-run queries.
- **Silk** intercepts requests via middleware and stores request/response and SQL in the **database**
  — every profiled request performs extra writes.
- **OpenTelemetry** instrumentation libraries wrap Django's request handling (a middleware inserted
  by `DjangoInstrumentor`), DB drivers (cursor execute), HTTP clients and Celery, creating spans.
  Context (trace ID, parent span) propagates via the W3C `traceparent` header across HTTP and via
  message headers across Celery. A **sampler** decides which traces are recorded; spans are batched
  and exported asynchronously (OTLP) by `BatchSpanProcessor`.
- **`connection.execute_wrapper()`** (Django ≥ 2.0) installs a function around every
  `cursor.execute`/`executemany` on a connection — the supported hook for query timing without
  patching drivers.

## 4. Engineering decisions and trade-offs

| Need | Use | Avoid |
|---|---|---|
| "Why is this view slow?" locally | pyinstrument middleware, Debug Toolbar (HTML) / Silk (APIs) | cProfile on template-heavy code (distorted) |
| Exact call counts for an algorithm | cProfile + pstats | Sampling (no counts) |
| Production CPU hotspot now | `py-spy top` / `py-spy record` on one worker | Enabling Silk/Toolbar in prod |
| Which endpoint regressed | RED metrics per route (histograms) | Log grepping |
| Latency across services/queues | OpenTelemetry traces | Per-service profilers |
| Which SQL is slowest overall | `pg_stat_statements` ([database-performance.md](database-performance.md)) | Per-request inspectors (biased sample) |
| Continuous profiling | Vendor continuous profiler / Pyroscope [third-party] | Always-on deterministic profiling |

Trace sampling trade-off: head sampling at 1–10 % is cheap but drops most slow traces; tail-based
sampling (in the OpenTelemetry Collector) keeps errors and slow traces but needs a collector tier
with memory to buffer traces.

## 5. Django implementation patterns

### Request timing middleware with `Server-Timing`

```python
import time
from django.db import connections

class TimingMiddleware:
    """Records wall, CPU and DB time per request. Place first in MIDDLEWARE."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        db = {"time": 0.0, "count": 0}

        def timer(execute, sql, params, many, context):
            t0 = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db["time"] += time.perf_counter() - t0
                db["count"] += 1

        wall0, cpu0 = time.perf_counter(), time.process_time()
        with _wrap_all(timer):
            response = self.get_response(request)
        wall = (time.perf_counter() - wall0) * 1000
        cpu = (time.process_time() - cpu0) * 1000     # process-wide: approximate with threads
        response["Server-Timing"] = (
            f'app;dur={wall:.1f}, db;dur={db["time"]*1000:.1f};desc="{db["count"]} queries", '
            f"cpu;dur={cpu:.1f}"
        )
        return response

from contextlib import ExitStack, contextmanager

@contextmanager
def _wrap_all(wrapper):
    with ExitStack() as stack:
        for conn in connections.all(initialized_only=True):   # kwarg added in Django 4.1 — VERIFY
            stack.enter_context(conn.execute_wrapper(wrapper))
        yield
```

Notes: connections opened lazily *during* the request are not wrapped by this sketch — wrap the
`default` alias explicitly if it may not yet be initialised. Expose `Server-Timing` only to internal
callers (it reveals DB cost): strip it at the edge or gate it on staff/internal IPs. Emit the same
numbers as a structured log line or metric for aggregation.

### pyinstrument for selected requests

```python
# settings/dev.py  [third-party: pyinstrument]
MIDDLEWARE = ["pyinstrument.middleware.ProfilerMiddleware", *MIDDLEWARE]
PYINSTRUMENT_PROFILE_DIR = "profiles"      # write HTML per request instead of replacing the response
# or request /some/path?profile to view the profile inline (default trigger in DEBUG)
```

```python
# Ad hoc around a selector or service call
from pyinstrument import Profiler

with Profiler(interval=0.001) as p:
    build_dashboard(tenant_id=42)
p.print(show_all=False)
```

### cProfile for management commands and scripts

```bash
python -m cProfile -o rebuild.prof manage.py rebuild_search_index --tenant 42
python -c "import pstats; pstats.Stats('rebuild.prof').sort_stats('cumulative').print_stats(25)"
# visualise: snakeviz rebuild.prof   [third-party: snakeviz]
```

### py-spy in production (one worker, short window)

```bash
py-spy top --pid 4812                                  # live view, like top for functions
py-spy dump --pid 4812 --locals                        # where is every thread stuck right now?
py-spy record --pid 4812 --duration 30 --rate 100 -o flame.svg            # flame graph
py-spy record --pid $(pgrep -f 'gunicorn: master') --subprocesses -o all.svg  # all workers
```

`dump` is the fastest diagnosis for hung workers (lock waits, blocked HTTP calls without timeouts).
Use `--gil` to show only threads holding the GIL (CPU contention), `--idle` to include waiting threads.

### Reading flame graphs

- x-axis = proportion of samples (not time order); y-axis = stack depth.
- Look for **wide plateaus**: wide frames near the top are where time is spent.
- Wide `psycopg`/`socket.recv` frames = waiting on DB/network → query/round-trip problem, not CPU.
- Many narrow towers under template/serializer frames = per-row overhead → batch or simplify.
- Compare before/after flame graphs at the same rate and load (differential flame graphs).

### Debug Toolbar and Silk (dev/staging)

```python
# settings/dev.py only  [third-party: django-debug-toolbar]
if DEBUG:
    INSTALLED_APPS += ["debug_toolbar"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INTERNAL_IPS = ["127.0.0.1"]
```

```python
# settings/staging.py  [third-party: django-silk]
INSTALLED_APPS += ["silk"]
MIDDLEWARE.insert(0, "silk.middleware.SilkyMiddleware")
SILKY_INTERCEPT_PERCENT = 10           # profile a fraction of requests
SILKY_PYTHON_PROFILER = False          # enable only when needed (cProfile overhead)
SILKY_MAX_RECORDED_REQUESTS = 10_000   # bound DB growth
SILKY_AUTHENTICATION = SILKY_AUTHORISATION = True
```

Toolbar: duplicated/similar query highlighting, per-template timing, cache calls. Silk: suits
API-only projects; records SQL with stack traces.

### OpenTelemetry instrumentation

```python
# wsgi.py / asgi.py — before get_wsgi_application()   [third-party: opentelemetry-*]
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

def init_tracing():
    provider = TracerProvider(
        resource=Resource.create({"service.name": "orders-api"}),
        sampler=ParentBasedTraceIdRatio(0.05),          # honour upstream decision, else 5 %
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument()                 # psycopg 3; psycopg2 has its own package
    RedisInstrumentor().instrument()
```

With Gunicorn `--preload`, call `init_tracing()` in the `post_fork` hook: the batch exporter's
background thread does not survive `fork()`. Add instrumentation for the HTTP client in use
(`requests`, `httpx`) and Celery so context propagates to tasks. Zero-code alternative:
`opentelemetry-instrument gunicorn ...` (VERIFY Django-specific settings env var
`DJANGO_SETTINGS_MODULE` must be set).

Custom spans for business-relevant stages:

```python
tracer = trace.get_tracer(__name__)

def build_invoice(order):
    with tracer.start_as_current_span("invoice.render_pdf") as span:
        span.set_attribute("invoice.lines", order.lines.count())
        return render_pdf(order)
```

### Route-level metrics

Label metrics by **route template** (`request.resolver_match.route`, e.g. `api/orders/<int:pk>/`),
never raw path — raw paths create unbounded cardinality.

## 6. Production use cases

- **Endpoint p95 doubled after a deploy.** RED dashboard isolates `GET /api/orders/`; traces show a
  new span `pricing.get_quote` of 180 ms called per line item → N+1 HTTP calls; fix by batching the
  pricing request ([performance-architecture-decisions.md](performance-architecture-decisions.md)).
- **CPU at 95 % on all web pods, latency fine yesterday.** `py-spy record` on one worker for 30 s:
  60 % of samples in `re._compile` from a regex built per request in a validator. Precompile at module
  level; CPU drops to 50 % (illustrative).
- **Workers hang intermittently.** `py-spy dump` shows threads blocked in `ssl.read` inside an SDK
  call without timeout. Add timeouts; see [production-performance-troubleshooting.md](production-performance-troubleshooting.md).
- **Celery task latency unexplained.** Trace links the HTTP request span to the task span; the gap
  between `apply_async` and task start is queue wait, not execution
  ([background-task-performance.md](background-task-performance.md)).

## 7. Advantages and disadvantages

| Tool | Advantages | Disadvantages |
|---|---|---|
| cProfile | Stdlib, exact counts | High overhead, distortion, poor for async |
| pyinstrument | Readable wall-time trees, Django middleware, async-aware | In-process; per-request only |
| py-spy | No code change, prod-safe, flame graphs, `dump` | Needs ptrace privileges; Linux/macOS/Windows support differs |
| Debug Toolbar | Rich per-request SQL/template insight | HTML pages only, dev only, slows requests |
| Silk | API request history with SQL | DB writes per request, dev/staging only |
| OpenTelemetry | Vendor-neutral, cross-service, metrics+traces+logs | Setup complexity; sampling and cardinality need design; export overhead |

## 8. Security and performance considerations

- Debug Toolbar and Silk expose SQL, settings, headers and request bodies — never in production;
  guard staging behind auth. Silk's stored bodies may contain PII/credentials.
- Span attributes and logs: do not record SQL parameters, tokens, or personal data; configure DB
  instrumentation to omit parameters (default for most) and sanitise URLs.
- `Server-Timing` and profile HTML must not reach untrusted clients.
- py-spy requires elevated privileges (`SYS_PTRACE`); grant temporarily, audit usage.
- Observability has cost: exporters consume CPU/network; unbounded metric labels can take down the
  metrics backend. Budget instrumentation overhead (measure p50 with and without; aim for low
  single-digit % — illustrative).

## 9. Common mistakes and anti-patterns

- Optimising from a cProfile of template code, where hook overhead dominates the picture.
- Profiling with `DEBUG=True` and drawing production conclusions (`DEBUG` records every query in
  `connection.queries`, disables some caching, changes behaviour).
- Profiling on tiny dev data; N+1 and plan problems appear only with realistic volumes.
- Treating Debug Toolbar's query count as the production picture (cache state, data and settings differ).
- Running Silk in production "just for a day".
- Head-sampling traces at 1 % and concluding "no slow traces found".
- Labelling metrics with user ID, raw path or query string.
- Initialising OpenTelemetry before fork with `--preload`, silently exporting nothing.
- Averaging per-pod p95s on dashboards ([performance-engineering-fundamentals.md](performance-engineering-fundamentals.md)).

## 10. Troubleshooting and validation

| Problem | Check |
|---|---|
| No traces from workers | Provider initialised post-fork? Exporter endpoint reachable? Sampler ratio? |
| Traces lack DB spans | Driver instrumentation matches installed driver (psycopg vs psycopg2) |
| Broken trace across services | `traceparent` propagated by the HTTP client instrumentation; proxies not stripping headers |
| py-spy "Permission denied" | Container lacks `SYS_PTRACE`; run as same user or root; `kernel.yama.ptrace_scope` |
| Profile shows mostly `{built-in method select}` | I/O wait — move to DB/HTTP analysis, not CPU optimisation |
| Toolbar not shown | `INTERNAL_IPS` vs Docker gateway IP; response must be HTML with `</body>` |

Validation:

- [ ] Bottleneck attributed with at least two independent signals (e.g. trace + profile, or metric + `pg_stat_statements`).
- [ ] Profiled under realistic data and settings (`DEBUG=False`).
- [ ] Instrumentation overhead measured and acceptable.
- [ ] No dev inspectors in production `INSTALLED_APPS`/`MIDDLEWARE` (`manage.py diffsettings` or settings test).
- [ ] Route metrics use bounded labels; dashboards compute percentiles from merged histograms.

## 11. Related references and skills

- [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md) — percentiles, RED/USE.
- [database-performance.md](database-performance.md) — `EXPLAIN`, `pg_stat_statements`, slow-query logs.
- [memory-and-resource-management.md](memory-and-resource-management.md) — tracemalloc, memray.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — using these tools during incidents.
- [performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md) — profiling under load.
- [django-core logging](../../django-core/references/logging-exceptions.md) — log configuration, request IDs.
- [django-integrations](../../django-integrations/SKILL.md) — HTTP clients and Celery wiring.
- [django-security-engineering](../../django-security-engineering/SKILL.md) — handling sensitive data in telemetry.

## 12. Official documentation and external references

- Python profilers (cProfile, pstats): https://docs.python.org/3/library/profile.html
- Python Linux perf support: https://docs.python.org/3/howto/perf_profiling.html
- PEP 799 (sampling profiler, Python 3.15): https://peps.python.org/pep-0799/
- Django — Database instrumentation (`execute_wrapper`): https://docs.djangoproject.com/en/5.2/topics/db/instrumentation/
- Django — Performance and optimization: https://docs.djangoproject.com/en/5.2/topics/performance/
- pyinstrument: https://pyinstrument.readthedocs.io/
- py-spy: https://github.com/benfred/py-spy
- Django Debug Toolbar: https://django-debug-toolbar.readthedocs.io/
- django-silk: https://github.com/jazzband/django-silk
- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/
- OpenTelemetry Django instrumentation: https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/django/django.html
- OpenTelemetry sampling: https://opentelemetry.io/docs/concepts/sampling/
- W3C Trace Context: https://www.w3.org/TR/trace-context/
- MDN `Server-Timing`: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing
- Brendan Gregg — Flame Graphs: https://www.brendangregg.com/flamegraphs.html
- Gunicorn server hooks (`post_fork`): https://docs.gunicorn.org/en/stable/settings.html#post-fork
