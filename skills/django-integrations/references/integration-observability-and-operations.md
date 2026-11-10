# Integration Observability and Operations

**Scope:** How to make every external integration observable and operable — structured integration logs,
correlation IDs across requests, tasks and messages, OpenTelemetry tracing, per-provider metrics, queue depth
and consumer lag, DLQ monitoring, health checks, alerting, dashboards and runbooks. General profiling and
performance investigation belong to [django-performance](../../django-performance/references/profiling-and-observability.md);
Django logging configuration mechanics belong to [django-core](../../django-core/references/logging-exceptions.md);
security event auditing belongs to [django-security-engineering](../../django-security-engineering/references/security-logging-and-auditing.md).

## 1. Purpose and scope

Integrations fail in ways the application cannot see by default: a partner slows down, a token expires, a
webhook stops arriving, a DLQ fills silently. This reference defines the telemetry an integration must emit,
how to connect it across process boundaries, and how to turn it into alerts and runbooks that let an on-call
engineer answer "is it us or them, and what do we do?"

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Telemetry packages are
third-party: [third-party: opentelemetry-sdk], [third-party: opentelemetry-instrumentation-django],
[third-party: opentelemetry-instrumentation-httpx], [third-party: opentelemetry-instrumentation-requests],
[third-party: opentelemetry-instrumentation-celery], [third-party: prometheus-client] or a vendor agent,
[third-party: structlog] or [third-party: python-json-logger]. Several OpenTelemetry Python instrumentations
are still marked beta/experimental — `VERIFY` stability status for the versions in use.

## 2. Core concepts

### The three signals for integrations

| Signal | Answers | Integration-specific content |
|---|---|---|
| Logs | What happened to *this* call/event? | provider, operation, outcome, status, attempt, duration, external id, correlation id |
| Metrics | How is the integration behaving *overall*? | rate, errors by category, latency percentiles, retries, breaker state, queue depth, lag |
| Traces | Where did the time go across systems? | span per outbound call, task, message publish/consume, linked via context propagation |

### Correlation ID vs trace context

- **Correlation ID**: an application-level identifier (often from `X-Request-ID` or generated) carried in
  logs, task headers, message envelopes and outbound requests. Works without a tracing backend; useful for
  support tickets and partner conversations.
- **Trace context**: W3C `traceparent`/`tracestate` headers propagated by OpenTelemetry; links spans into a
  trace. When tracing is enabled, log the `trace_id` too so logs and traces join.
- Partners often have their own request id (`x-request-id`, `request-id`, `Stripe-Request-Id`): **always log
  the provider's request id** — it is the only key their support can search.

### RED and USE for integrations

- **RED per provider × operation**: Rate, Errors (by category), Duration (p50/p95/p99).
- **Saturation of integration resources**: connection pool usage, concurrency limiter occupancy, queue depth,
  consumer lag, rate-limit headroom (`X-RateLimit-Remaining`).

### Error categories

Classify outcomes once, consistently with the exception taxonomy in `apps/integrations/errors.py`:

| Category | Source | Typical owner |
|---|---|---|
| `success` | 2xx and business-valid | — |
| `transient` | 5xx, connect errors, 429 | Usually provider; watch duration |
| `rate_limited` | 429 / quota | Us (traffic shaping) or provider contract |
| `auth` | 401/403, token refresh failure | Us (credentials/rotation) |
| `client_error` | 4xx validation | Us (bug or bad data) |
| `contract` | Response failed schema validation | Provider change or our parser |
| `ambiguous` | Timeout after send | Needs reconciliation |
| `circuit_open` | Breaker short-circuit | Consequence, not cause |

A dashboard that splits by category tells on-call immediately whether to page the partner or the team.

### Business-level signals

HTTP success is not business success. Also track: webhooks received per type per hour (absence detection),
payments pending reconciliation, records failing sync, notifications undelivered after N minutes. These catch
silent failures that technical metrics miss.

## 3. Internal mechanics

### Context propagation path

```text
Inbound request (traceparent, X-Request-ID)
  └─ Django middleware: set correlation_id contextvar; OTel server span
      ├─ outbound httpx call: OTel client span; inject traceparent + X-Request-ID
      ├─ on_commit → Celery apply_async: OTel injects context into task headers; we add correlation_id
      │     └─ worker: extract context → task span → outbound calls → child spans
      └─ outbox row: store correlation_id + traceparent in headers JSON
            └─ relay publish: message headers carry them
                  └─ consumer: extract → consumer span (span link if batch)
```

Python `contextvars` carry the correlation id within a thread or asyncio task; it must be **explicitly** copied
into anything that crosses a process boundary (task headers, message headers, outbox rows).

### OpenTelemetry pieces

- **TracerProvider + exporter** (OTLP to a collector) configured once per process.
- **Instrumentations** patch Django (server spans), httpx/requests (client spans + header injection),
  Celery (publish/run spans + header propagation), psycopg (DB spans), and Redis.
- **Semantic conventions** name attributes (`http.request.method`, `server.address`,
  `http.response.status_code`, `messaging.system`, `messaging.destination.name`). HTTP semantic conventions
  were stabilised with renamed attributes; instrumentations may emit old or new names depending on version and
  `OTEL_SEMCONV_STABILITY_OPT_IN` `VERIFY`.
- **Process model caveat**: with Gunicorn/Celery prefork, initialise the SDK **after fork**
  (`post_fork` hook / `worker_process_init` signal); batch span processors started before fork do not work in
  children.

### Metrics collection

Prometheus pull model with multiprocess workers needs `PROMETHEUS_MULTIPROC_DIR` and multiprocess collectors;
OTel metrics push via OTLP avoids that. Choose one per platform; do not emit the same metric twice.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Instrumentation | Auto-instrumentation vs manual spans | Auto for HTTP/DB/Celery; add manual spans around domain operations and gateway methods for business names |
| Log format | JSON vs text | JSON in production; text locally |
| Payload logging | None / redacted / full | Default none; redacted summaries for debugging; full bodies only in short-lived, access-controlled debug mode |
| Metric cardinality | Labels per provider/operation/category | Never label by user id, URL with ids, external id, or error message |
| Trace sampling | Head-based ratio vs tail-based in collector | Head 1–10% for high volume; tail-based to keep all errors and slow traces |
| Health checks | Liveness only vs dependency checks | Liveness must not call providers; dependency status goes to a separate readiness/diagnostic endpoint and metrics |
| Alert source | Symptoms (SLO burn) vs causes | Page on symptoms; ticket on causes |

Cost: tracing every outbound call at 100% sampling and logging full bodies is expensive in ingest and storage;
sample deliberately and budget telemetry like any other infrastructure.

## 5. Django implementation patterns

### Correlation ID middleware

```python
# apps/integrations/observability/context.py
import contextvars
import uuid

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


def current_correlation_id() -> str:
    cid = correlation_id.get()
    if cid is None:
        cid = str(uuid.uuid4())
        correlation_id.set(cid)
    return cid
```

```python
# apps/integrations/observability/middleware.py
import re
import uuid

from .context import correlation_id

_VALID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        cid = incoming if _VALID.match(incoming) else str(uuid.uuid4())   # never trust arbitrary header content
        token = correlation_id.set(cid)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = cid
            return response
        finally:
            correlation_id.reset(token)
```

A logging filter adds it to every record:

```python
import logging

from .context import correlation_id


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get() or "-"
        return True
```

### Propagating into Celery

```python
# apps/integrations/observability/celery_signals.py
from celery.signals import before_task_publish, task_postrun, task_prerun

from .context import correlation_id, current_correlation_id

_tokens: dict[str, object] = {}


@before_task_publish.connect
def add_correlation_header(headers=None, **_):
    if headers is not None:
        headers.setdefault("correlation_id", current_correlation_id())


@task_prerun.connect
def restore_correlation(task_id=None, task=None, **_):
    cid = getattr(task.request, "correlation_id", None) or task_id
    _tokens[task_id] = correlation_id.set(cid)


@task_postrun.connect
def clear_correlation(task_id=None, **_):
    token = _tokens.pop(task_id, None)
    if token is not None:
        correlation_id.reset(token)
```

Custom message headers are exposed on `task.request` for protocol 2 messages `VERIFY` (access via
`task.request.get("correlation_id")` if attribute access differs in your version). Import this module from
`AppConfig.ready()` so the signals connect in both web and worker processes.

### Outbound call instrumentation via httpx event hooks

```python
# apps/integrations/http.py (excerpt)
import logging
import time

import httpx

from .metrics import record_call
from .observability.context import current_correlation_id

logger = logging.getLogger("integrations.http")


def build_client(name: str, *, base_url: str, timeout: httpx.Timeout, auth=None) -> httpx.Client:
    def on_request(request: httpx.Request) -> None:
        request.headers["X-Request-ID"] = current_correlation_id()
        request.extensions["start"] = time.perf_counter()

    def on_response(response: httpx.Response) -> None:
        start = response.request.extensions.get("start", time.perf_counter())
        duration = time.perf_counter() - start
        record_call(provider=name, operation=_operation(response.request), status=response.status_code,
                    duration=duration)
        logger.info(
            "integration.call",
            extra={
                "provider": name,
                "method": response.request.method,
                "path": response.request.url.path,          # path only — no query string (may hold tokens)
                "status": response.status_code,
                "duration_ms": round(duration * 1000),
                "provider_request_id": response.headers.get("x-request-id"),
            },
        )

    return httpx.Client(
        base_url=base_url, timeout=timeout, auth=auth,
        event_hooks={"request": [on_request], "response": [on_response]},
    )


def _operation(request: httpx.Request) -> str:
    return request.extensions.get("operation", "unknown")   # set per call: extensions={"operation": "capture"}
```

Transport exceptions (timeouts, connect errors) do not reach response hooks — record them where the client
normalises errors, using the same metric with `category="transient"` or `"ambiguous"`.

### Metrics

```python
# apps/integrations/metrics.py   [third-party: prometheus-client]
from prometheus_client import Counter, Gauge, Histogram

CALLS = Counter(
    "integration_calls_total", "Outbound integration calls",
    ["provider", "operation", "category"],
)
LATENCY = Histogram(
    "integration_call_duration_seconds", "Outbound call latency",
    ["provider", "operation"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
RETRIES = Counter("integration_retries_total", "Retries attempted", ["provider", "operation", "reason"])
BREAKER_OPEN = Gauge("integration_circuit_open", "1 if breaker open", ["provider"])
WEBHOOKS = Counter("webhook_events_total", "Inbound webhook events", ["provider", "type", "outcome"])
OUTBOX_PENDING = Gauge("outbox_pending_messages", "Unpublished outbox rows")


def category_for(status: int) -> str:
    if status < 400:
        return "success"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "transient"
    return "client_error"


def record_call(*, provider: str, operation: str, status: int, duration: float) -> None:
    CALLS.labels(provider, operation, category_for(status)).inc()
    LATENCY.labels(provider, operation).observe(duration)
```

Gauges for backlog (outbox pending, `WebhookEvent` in `received` older than N minutes, DLQ depth) are best
computed by a periodic collector task or scraped from the broker's own exporter, not on every request.

### OpenTelemetry setup

```python
# config/otel.py   — call from gunicorn post_fork and Celery worker_process_init
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_tracing(service_name: str) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))   # endpoint via OTEL_EXPORTER_OTLP_ENDPOINT
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    CeleryInstrumentor().instrument()
```

```python
# celery worker hook
from celery.signals import worker_process_init


@worker_process_init.connect(weak=False)
def init_tracing(*_, **__):
    from config.otel import configure_tracing
    configure_tracing("orders-worker")
```

Manual spans around gateway operations give business-meaningful names:

```python
from opentelemetry import trace

tracer = trace.get_tracer("apps.payments")


def capture(self, *, authorization_id: str, amount, idempotency_key: str):
    with tracer.start_as_current_span("acmepay.capture") as span:
        span.set_attribute("integration.provider", "acmepay")
        span.set_attribute("integration.idempotency_key_hash", _hash(idempotency_key))
        ...
```

Never put tokens, PII or full payloads in span attributes — trace backends are broadly readable.

### Health checks: liveness vs dependency status

```python
# apps/integrations/views.py
from django.http import JsonResponse

from .health import dependency_status   # reads cached results from a periodic probe task


def liveness(request):
    return JsonResponse({"status": "ok"})                 # process is up; no external calls


def integrations_status(request):                         # internal/authenticated; not used for restarts
    status = dependency_status()                          # {"acmepay": {"state": "degraded", "checked_at": ...}}
    code = 200 if all(s["state"] != "down" for s in status.values()) else 503
    return JsonResponse(status, status=code)
```

- **Liveness** never checks external providers — a partner outage must not make the orchestrator restart all
  your pods.
- **Readiness** checks only dependencies without which this instance cannot serve *any* traffic (usually the
  DB), not optional providers.
- **Provider health** is computed asynchronously (periodic probe of a cheap authenticated endpoint, or derived
  from recent error rates and breaker state) and exposed for dashboards, status pages and feature degradation.

### Alert design

| Alert | Condition (example) | Severity |
|---|---|---|
| Provider error budget burn | `transient`+`ambiguous` ratio > 5% for 10 min on a critical operation | Page |
| Auth failures | Any sustained `auth` category > 0 for 5 min | Page (credentials expired/rotated) |
| Circuit open | Breaker open > 5 min | Ticket / page if critical path |
| Latency | p95 > 2× baseline for 15 min | Ticket |
| Webhook silence | No events of expected type for N × usual interval | Ticket/page by business impact |
| DLQ | Depth > 0 for 30 min or growth rate > threshold | Ticket |
| Consumer lag | Lag in time > SLA (e.g. 5 min) | Page if SLA-bound |
| Outbox backlog | Oldest unpublished row > 5 min | Page (events not leaving) |
| Reconciliation mismatches | Unmatched records after daily run > 0 | Ticket to owning team |
| Certificate/secret expiry | Expires in < 21 days | Ticket |

Alert on **time-based** lag/backlog ("oldest message age") rather than counts — counts vary with traffic.

### Runbook template

```text
Runbook: <provider> — <operation> failures
Owner: <team>   Provider support: <url / contract id>   Status page: <url>

1. Triage
   - Dashboard: <link>; split errors by category.
   - auth → go to §3; transient/ambiguous → §2; contract → §4; rate_limited → §5.
   - Check provider status page and recent deploys of ours.
2. Provider degraded/outage
   - Confirm breaker open; verify graceful degradation is active (feature flag <name>).
   - Do not raise retry limits. Queue continues to back up: watch queue age vs retry window.
   - After recovery: drain gradually (concurrency <n>), watch rate limits.
3. Authentication
   - Check secret/certificate expiry and last rotation; token endpoint errors in logs.
   - Rotate per <secret rotation procedure>; restart not required if credentials are reloaded (<how>).
4. Contract violation
   - Capture a redacted sample; compare with schema; check provider changelog/API version header.
   - Pin API version or hotfix the mapper; replay failed records via <command>.
5. Rate limited
   - Compare our call rate with quota; identify the hot caller; lower concurrency on <queue>.
6. Recovery and verification
   - Replay DLQ / failed WebhookEvent / FAILED sync records: manage.py <command> --dry-run first.
   - Run reconciliation <command> for the incident window.
   - Confirm business metrics back to baseline; record timeline for postmortem.
```

## 6. Production use cases

1. **"Payments are slow"** — dashboard shows `acmepay.capture` p95 from 400 ms to 6 s, category still
   `success`, other providers normal → provider-side latency; traces confirm time is in the client span, not
   DB. Action: verify timeouts protect worker capacity, notify provider with request ids.
2. **Expired client secret** — `auth` category spikes at 00:00 UTC for the CRM; alert pages within 5 minutes;
   runbook §3 rotates the Key Vault secret. A secret-expiry alert would have prevented it.
3. **Silent webhook outage** — provider disabled the endpoint after repeated 500s during a deploy; the
   "no `payment.succeeded` webhooks in 30 min" alert catches it; reconciliation job backfills missed events.
4. **Consumer lag** — Kafka lag grows after a handler change added a synchronous HTTP call per message; lag-
   in-time alert fires; traces show per-message HTTP spans; fix moves the call to a batched task.
5. **DLQ growth** — a partner started sending a new enum value; `contract` errors land in the DLQ; the mapper
   is fixed and the DLQ redriven; inbox dedupe prevents duplicates.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Distinguishes "us vs them" in minutes | Telemetry ingest/storage cost grows with volume |
| Provider request ids speed vendor escalations | Instrumentation version drift (semantic conventions, beta packages) |
| Business-signal alerts catch silent failures | Risk of leaking secrets/PII into logs and traces |
| Traces across tasks and messages make async flows debuggable | Cardinality explosions if labels are careless |
| Runbooks reduce MTTR and on-call stress | Runbooks rot unless exercised and owned |

## 8. Security and performance considerations

### Security
- Redact before logging: Authorization headers, cookies, API keys, tokens, signatures, card data, personal
  data; log URL paths without query strings; hash identifiers when correlation is needed but the value is
  sensitive ([integration-security.md](integration-security.md),
  [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md)).
- Validate incoming `X-Request-ID` (length, charset) to prevent log injection.
- Don't propagate internal trace headers or correlation ids to untrusted third parties if they reveal
  internal topology; consider generating a separate outbound request id.
- Restrict access to telemetry backends, Flower and DLQ viewers — they contain business data.
- Status endpoints with dependency detail are internal or authenticated.

### Performance
- Use `BatchSpanProcessor` and async exporters; never export synchronously on the request path.
- Sample high-volume, low-value spans (health checks, polling calls).
- Keep metric label sets bounded (`provider`, `operation`, `category`).
- Backlog gauges from periodic queries, with indexes supporting them (`status, received_at`).
- Profiling and deeper latency analysis: [profiling-and-observability](../../django-performance/references/profiling-and-observability.md),
  [production-performance-troubleshooting](../../django-performance/references/production-performance-troubleshooting.md).

## 9. Common mistakes and anti-patterns

- Logging full request/response bodies or headers "for debugging" in production.
- Not logging the provider's request id or our idempotency key.
- One generic "external API error" metric with no provider/operation/category split.
- Labelling metrics by URL with embedded ids or by error message → cardinality explosion.
- Liveness probes that call external providers → cascading restarts during a partner outage.
- Correlation id kept in a thread-local and not copied into Celery headers or message envelopes.
- Initialising OpenTelemetry before fork in Gunicorn/Celery prefork.
- Alerting on raw error counts instead of rates/SLO burn; alerting on queue length instead of age.
- DLQs and failed `WebhookEvent`s without alerts or owners.
- Treating 2xx rate as integration health while business outcomes (settled payments, delivered emails) fail.
- Runbooks that reference commands nobody has run.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Traces break at Celery boundary | Celery instrumentation missing in worker, or initialised pre-fork | `worker_process_init` hook, `traceparent` in task headers |
| Logs lack correlation id in tasks | Header not added or not restored | `before_task_publish` / `task_prerun` signals connected in worker |
| No spans exported from Gunicorn workers | SDK initialised in master before fork | `post_fork` hook |
| Metrics missing in multiprocess setup | Prometheus multiprocess mode not configured | `PROMETHEUS_MULTIPROC_DIR`, collector registry |
| Telemetry bill spike | Unbounded labels or 100% sampling | Series count per metric, sampler config |
| Alert fatigue | Count-based thresholds, cause-based paging | Convert to rate/age/SLO burn alerts |

Validation checklist:
- [ ] Every outbound call emits a log line and metrics with provider, operation, category, duration.
- [ ] Provider request id and our idempotency key (or its hash) are logged.
- [ ] Correlation id flows request → task → outbound call → message; traces connect across them.
- [ ] No secrets/PII in logs, span attributes or metric labels (tested with a redaction test).
- [ ] Dashboards per provider: RED, breaker state, retries, rate-limit headroom.
- [ ] Queue depth/age, consumer lag, DLQ depth, outbox backlog and webhook silence are monitored.
- [ ] Liveness has no external dependency; provider health exposed separately.
- [ ] Alerts are symptom-based, with runbook links; runbooks exercised at least once.
- [ ] Secret and certificate expiry monitored.

## 11. Related references and skills

- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — failure taxonomy, breaker, retry metrics sources.
- [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md) — client factory and error normalisation where metrics hook in.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — task monitoring, Flower.
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — lag, DLQ, envelope correlation fields.
- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — webhook delivery tracking.
- [integration-security.md](integration-security.md) — redaction and PII rules.
- [azure-integrations.md](azure-integrations.md) — Azure Monitor / Application Insights export.
- django-performance: [profiling-and-observability](../../django-performance/references/profiling-and-observability.md),
  [production-performance-troubleshooting](../../django-performance/references/production-performance-troubleshooting.md).
- django-core: [logging-exceptions](../../django-core/references/logging-exceptions.md),
  [production-configuration](../../django-core/references/production-configuration.md).
- django-security-engineering: [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).

## 12. Official documentation and external references

- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/
- OpenTelemetry Python contrib instrumentations: https://opentelemetry-python-contrib.readthedocs.io/
- OpenTelemetry semantic conventions (HTTP, messaging): https://opentelemetry.io/docs/specs/semconv/
- W3C Trace Context: https://www.w3.org/TR/trace-context/
- Django logging: https://docs.djangoproject.com/en/5.2/topics/logging/
- Python `contextvars`: https://docs.python.org/3/library/contextvars.html
- Python logging cookbook: https://docs.python.org/3/howto/logging-cookbook.html
- HTTPX event hooks: https://www.python-httpx.org/advanced/event-hooks/
- Celery signals: https://docs.celeryq.dev/en/stable/userguide/signals.html
- Celery monitoring: https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- Prometheus Python client (multiprocess mode): https://prometheus.github.io/client_python/multiprocess/
- Prometheus instrumentation practices: https://prometheus.io/docs/practices/instrumentation/
- Google SRE book — monitoring distributed systems: https://sre.google/sre-book/monitoring-distributed-systems/
- Kubernetes liveness/readiness probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- Azure Monitor OpenTelemetry for Python: https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python
