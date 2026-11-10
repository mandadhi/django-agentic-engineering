# Celery and Background Integrations

**Scope:** How to run external-system work outside the request with Celery in Django — when a task queue is
(and is not) warranted, transaction-aware dispatch, routing per provider, retry and idempotency semantics,
workflows (chains, groups, chords), scheduling, long-running integrations and monitoring. Worker sizing and
throughput tuning belong to [django-performance](../../django-performance/references/background-task-performance.md);
detailed task test strategies belong to
[django-testing](../../django-testing/references/integration-and-background-task-testing.md); brokers used
for cross-system messaging are in [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md).

## 1. Purpose and scope

Use this reference when an integration call should not run on the request thread: it is slow, unreliable,
needs retries over minutes or hours, must be rate-limited per provider, or is scheduled. It shows the Django
mechanics that keep tasks consistent with the database and safe under redelivery.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x
([third-party: celery]). Broker choices: RabbitMQ (recommended for Celery reliability), Redis, or Amazon SQS.
Optional tooling: [third-party: django-celery-beat], [third-party: django-celery-results], [third-party: flower].

Celery-specific behaviour changes between minor versions; check the installed version before relying on a
setting or API flagged as version-dependent below.

## 2. Core concepts

### When NOT to use Celery

Celery is a task queue with a broker, workers, optional result backend and beat scheduler — four moving parts
to deploy and monitor. Do not add it reflexively:

| Need | Simpler alternative |
|---|---|
| One fast, reliable external call the user waits for anyway | Call synchronously with a tight timeout |
| Fire-and-forget email at low volume | Synchronous send with timeout, or an email provider API with its own queue |
| Nightly batch job | Management command scheduled by cron / Kubernetes CronJob / cloud scheduler |
| Dozens of jobs per hour, transactional with the DB | DB-backed queue (`select_for_update(skip_locked=True)` worker loop) |
| Concurrent fan-out of I/O within a request | `httpx.AsyncClient` + `asyncio.gather` with bounded concurrency |
| Cross-team event distribution | A message broker topic, not Celery tasks |
| Django ≥ 6.0 simple background tasks | Django's built-in Tasks framework (`django.tasks`) with a suitable backend — `VERIFY` backend maturity before production |

Reach for Celery when you need: retries with backoff over long windows, per-provider routing and rate limits,
many workers, workflows, or scheduling with visibility — and you are prepared to operate it.

### Architecture

```text
Django (producer) ──apply_async──► broker queue(s) ──► worker process(es) ──► external system
        │                                                  │
        └─ on_commit                                       ├─ result backend (optional)
                                                           └─ beat (scheduler, exactly one instance)
```

- **Task message**: task name + args/kwargs + headers (id, retries, eta, correlation headers) serialized as
  JSON. Arguments must be small and serializable — pass primary keys, not model instances.
- **Acknowledgement**: by default Celery acks a task **when the worker receives it** (early ack =
  at-most-once for crashes). `acks_late=True` acks **after** the task returns (at-least-once).
- **Result backend**: stores return values/states. Most integration tasks should ignore results and record
  outcomes in domain tables instead.

### Delivery semantics in practice

Celery with `acks_late=True` is at-least-once: worker crash, OOM kill, deploy restart, or broker visibility
expiry redelivers the task. Celery's own retries (`self.retry`) publish a **new** message. Therefore every
integration task must be idempotent. Celery never provides exactly-once execution.

### Transaction-aware dispatch

A task dispatched inside a transaction may run **before the commit** (worker reads a row that is not yet
visible → `DoesNotExist`) or **after a rollback** (acts on data that never existed). Dispatch with
`transaction.on_commit()`. Celery ≥ 5.4 adds `task.delay_on_commit()` / `apply_async_on_commit()` for Django
(`VERIFY` exact version and argument support). Neither guarantees delivery if the process dies between commit
and publish — use an outbox for that ([integration-architecture-patterns.md](integration-architecture-patterns.md)).

## 3. Internal mechanics

### Early ack vs late ack

| Setting | Ack timing | Crash during task | Use for |
|---|---|---|---|
| default (`acks_late=False`) | On receipt | Task lost | Truly disposable work |
| `acks_late=True` | After return | Redelivered | Integration work (with idempotency) |
| `acks_late=True` + `reject_on_worker_lost=True` | After return; requeue if worker process killed | Redelivered (risk of loop for poison tasks) | Critical work with a delivery limit |

`task_acks_on_failure_or_timeout` (default True) means a task that **raises** is still acked — failures are
not redelivered by the broker; only your retry logic re-runs them.

### Visibility timeout pitfalls (Redis and SQS brokers)

Redis and SQS emulate acknowledgement with a **visibility timeout**: unacked messages reappear after it
expires (Redis transport default 1 hour `VERIFY`). Consequences with `acks_late`:

- A task running longer than the visibility timeout is **delivered again to another worker** while still
  running → concurrent duplicate execution.
- Tasks with `eta`/`countdown` longer than the visibility timeout are redelivered repeatedly — schedule far
  future work with beat or a DB scheduler, not long countdowns.
- Set `broker_transport_options = {"visibility_timeout": <max task duration + margin>}` and keep integration
  tasks short. RabbitMQ has no visibility timeout but does enforce a consumer ack timeout
  (`consumer_timeout`, 30 min default in recent versions `VERIFY`).

### Prefetch

Workers reserve `worker_prefetch_multiplier × concurrency` messages. With long integration tasks, prefetched
messages wait behind a slow one; set `worker_prefetch_multiplier=1` for long-running queues. Sizing details:
[background-task-performance](../../django-performance/references/background-task-performance.md).

### Retry mechanics

`self.retry(exc=..., countdown=...)` raises `Retry`, which republishes the task with `retries + 1` and an ETA.
`autoretry_for` wraps the task body to do this for listed exceptions; `retry_backoff=True` gives exponential
delays, `retry_backoff_max` caps them, `retry_jitter=True` (default when backoff is on) randomises them,
`max_retries` bounds attempts (default 3). After the last retry the original exception propagates and the task
is `FAILURE`.

### Serialization

Use JSON (`task_serializer="json"`, `accept_content=["json"]`). Never enable `pickle` — a broker message
becomes code execution. JSON cannot encode `Decimal`, `datetime` or UUID natively as types: pass strings and
convert in the task.

## 4. Engineering decisions and trade-offs

### Broker choice for Celery

| Broker | Strengths | Caveats |
|---|---|---|
| RabbitMQ | True acks, no visibility timeout, mature Celery support, quorum queues | Another service to run |
| Redis | Often already present, simple | Visibility-timeout redelivery, data loss on failover, memory-bound |
| SQS | Managed, scales | Visibility timeout, no `celery inspect`/remote control `VERIFY`, polling latency |

For business-critical integrations prefer RabbitMQ; use Redis when acceptable loss/duplication is understood.

### Task granularity

- **One task per external operation per entity** (`charge_payment(payment_id)`), not one task per batch of
  1000 calls — retries then repeat only the failed unit.
- Fan out large syncs into chunks (e.g. 100 records per task) to balance overhead vs retry blast radius.

### Where retries live

| Layer | Use |
|---|---|
| HTTP client (transport `retries=`) | Connection establishment failures only |
| In-task loop (`retry_call`) | A few fast retries (≤ ~10 s total) for idempotent calls |
| Celery `self.retry` | Longer backoff (minutes–hours), frees the worker while waiting |

Never stack all three with full budgets — attempts multiply (3 × 3 × 5 = 45 calls). Policy and budgets are in
[integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

### Results and state

Store integration outcome on the domain record (`Payment.status`, `external_id`, `last_error`) rather than in
the Celery result backend — domain state is queryable, survives result expiry, and is the source of truth.
Set `ignore_result=True` on integration tasks unless a workflow (chord) needs results.

## 5. Django implementation patterns

### Project wiring

```python
# config/celery.py
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# config/settings/base.py (excerpt)
CELERY_BROKER_URL = env("CELERY_BROKER_URL")              # amqps:// or rediss://
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_TIME_LIMIT = 300                              # hard kill
CELERY_TASK_SOFT_TIME_LIMIT = 240                         # raises SoftTimeLimitExceeded
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}   # Redis/SQS only
CELERY_TASK_ROUTES = {
    "apps.payments.tasks.*": {"queue": "integrations.payments"},
    "apps.notifications.tasks.*": {"queue": "integrations.notify"},
    "apps.crm.tasks.*": {"queue": "integrations.crm"},
}
CELERY_TASK_DEFAULT_QUEUE = "default"
```

### Routing per provider (bulkhead)

Separate queues per provider, each consumed by its own worker deployment with bounded concurrency:

```bash
celery -A config worker -Q integrations.payments --concurrency=4 -n payments@%h
celery -A config worker -Q integrations.crm --concurrency=2 -n crm@%h
```

A slow CRM no longer starves payment tasks. Celery's `rate_limit="10/s"` is **per worker**, not global; for a
provider-wide limit use a Redis-backed limiter ([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)).

### Transaction-aware dispatch

```python
# apps/payments/services.py
from django.db import transaction

from .models import Payment
from .tasks import capture_payment


def confirm_order(*, order) -> Payment:
    with transaction.atomic():
        payment = Payment.objects.create(order=order, amount=order.total, status=Payment.Status.PENDING)
        transaction.on_commit(lambda: capture_payment.delay(payment.pk))
        # Celery ≥ 5.4 alternative: capture_payment.delay_on_commit(payment.pk)   VERIFY
    return payment
```

Bind loop variables explicitly — `on_commit(lambda p=p: task.delay(p.pk))` — or every callback captures the
last value. Use `on_commit(..., robust=True)` (Django ≥ 4.2) when one failing dispatch must not prevent the
others.

### Idempotent integration task

```python
# apps/payments/tasks.py
import logging

from celery import shared_task
from django.db import transaction

from apps.integrations.errors import (
    AmbiguousOutcomeError,
    PermanentIntegrationError,
    RateLimitedError,
    TransientIntegrationError,
)

from .integrations.acmepay.gateway import get_payment_gateway
from .models import Payment

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    acks_late=True,
    autoretry_for=(TransientIntegrationError,),
    retry_backoff=5,            # 5, 10, 20, 40 ... seconds
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=8,
)
def capture_payment(self, payment_id: int) -> None:
    payment = Payment.objects.get(pk=payment_id)
    if payment.status in {Payment.Status.CAPTURED, Payment.Status.FAILED}:
        return                                            # idempotent: already terminal

    gateway = get_payment_gateway()
    try:
        result = gateway.capture(
            authorization_id=payment.authorization_id,
            amount=payment.amount,
            idempotency_key=f"capture:{payment.pk}",     # stable across retries and redelivery
        )
    except RateLimitedError as exc:
        raise self.retry(exc=exc, countdown=exc.retry_after or 30)
    except AmbiguousOutcomeError:
        # Timed out after sending: do not blindly retry; reconcile against the provider.
        # (Simplified: real code goes through the payment state machine's transition() —
        # see payment-and-financial-integrations.md.)
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.UNKNOWN)
        resolve_unknown_payment.apply_async(args=[payment.pk], countdown=60)
        return
    except PermanentIntegrationError as exc:
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.FAILED, last_error=str(exc)[:500])
        logger.warning("payment.capture_failed", extra={"payment_id": payment.pk, "error": type(exc).__name__})
        return

    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).exclude(status=Payment.Status.CAPTURED).update(
            status=Payment.Status.CAPTURED, external_id=result.capture_id
        )
```

Idempotency layers: terminal-state check (cheap short-circuit), provider idempotency key (protects the remote
side), conditional update (protects the local side). Key generation rules are in
[integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

### Mutual exclusion for non-idempotent providers

When a provider has no idempotency key and concurrent duplicate execution is harmful, take a short lock:

```python
from django.core.cache import cache


def run_exclusive(key: str, ttl: int, fn) -> bool:
    if not cache.add(key, "1", timeout=ttl):   # atomic SET NX on Redis cache backend
        return False
    try:
        fn()
        return True
    finally:
        cache.delete(key)
```

Locks reduce, not eliminate, duplicates (TTL expiry, Redis failover). Pair with a DB state check.

### Workflows: chain, group, chord

```python
from celery import chain, chord, group

# Sequential dependent steps: each receives the previous result unless .si() (immutable) is used.
chain(
    export_invoice.si(invoice_id),
    upload_to_erp.si(invoice_id),
    mark_invoice_synced.si(invoice_id),
).apply_async()

# Parallel fan-out, then aggregate (chord needs a result backend).
chord(
    group(sync_customer_page.s(page) for page in range(1, total_pages + 1)),
    finalize_customer_sync.s(run_id),
).apply_async()
```

Trade-offs: chords require a result backend and can stall if one header task never completes; failure
handling inside canvases is subtle (`link_error`). For multi-step business processes that must survive
deploys, track progress in a DB model (process manager) and dispatch the next step from it — state is then
visible and restartable ([integration-architecture-patterns.md](integration-architecture-patterns.md)).
Prefer `.si()` signatures and IDs over passing large results between tasks.

### Long-running integrations

- Split into resumable chunks with a checkpoint (`SyncCheckpoint.cursor`) so a killed worker resumes, not restarts
  ([data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)).
- Use `soft_time_limit` to save progress before the hard limit kills the process:

```python
from celery.exceptions import SoftTimeLimitExceeded


@shared_task(bind=True, soft_time_limit=240, time_limit=300, acks_late=True)
def sync_crm_contacts(self, run_id: int) -> None:
    checkpoint = SyncCheckpoint.objects.get(integration="crm.contacts")
    try:
        for page in crm_client.iter_contacts(updated_since=checkpoint.cursor):
            apply_contacts(page.items)
            SyncCheckpoint.objects.filter(pk=checkpoint.pk).update(cursor=page.next_cursor)
    except SoftTimeLimitExceeded:
        sync_crm_contacts.apply_async(args=[run_id])   # continue from the saved cursor
```

- Polling a long external job (report generation, bulk export): schedule `check_job_status` with
  `countdown` and a maximum number of polls; do not sleep inside the task.

### Scheduling with beat

```python
CELERY_BEAT_SCHEDULE = {
    "reconcile-payments-hourly": {
        "task": "apps.payments.tasks.reconcile_recent_payments",
        "schedule": 3600.0,
        "options": {"queue": "integrations.payments", "expires": 3000},
    },
}
```

- Run **exactly one** beat instance; two beats double every schedule. django-celery-beat stores schedules in
  the DB for runtime editing.
- Set `expires` so a backlog does not run stale scheduled tasks hours late.
- Scheduled tasks must themselves be idempotent and overlap-safe (lock or "already running" check).

### Propagating context

Pass the correlation id in task headers so logs and traces connect request → task → external call
([integration-observability-and-operations.md](integration-observability-and-operations.md)).

## 6. Production use cases

1. **Payment capture after order confirmation** — `on_commit` dispatch to `integrations.payments`, provider
   idempotency key, ambiguous timeouts routed to reconciliation.
2. **CRM sync every 15 minutes** — beat triggers a chunked, checkpointed sync on a dedicated queue with
   concurrency 2 to respect the CRM's rate limit.
3. **Transactional email** — task per message with a notification id as idempotency key; provider outage →
   exponential backoff for up to 2 hours, then failover provider
   ([email-sms-and-notification-integrations.md](email-sms-and-notification-integrations.md)).
4. **Webhook processing** — webhook view stores `WebhookEvent` and dispatches on commit; the task locks the
   row and applies it ([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md)).
5. **Invoice export to ERP** — chain of export → upload → mark synced, with each step idempotent.

Failure scenarios: worker OOM-killed mid-task (redelivery with `acks_late`), Redis failover losing queued
tasks (reconcile from domain state), deploy restarting workers (warm shutdown finishes current tasks within the
grace period), beat duplicated by two replicas, a provider outage producing a retry storm.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Removes slow/unreliable calls from the request path | Broker, workers, beat and monitoring to operate |
| Rich retry, routing, rate-limiting and scheduling | At-least-once: idempotency is mandatory |
| Per-provider isolation via queues | Visibility timeout and prefetch pitfalls |
| Large ecosystem and Django integration | Eventual consistency visible to users (pending states) |
| Workflows (canvas) for fan-out/fan-in | Canvas failure handling is complex; hard to observe |

## 8. Security and performance considerations

### Security
- Broker over TLS (`rediss://`, `amqps://`) with credentials from the environment; broker access equals code
  execution for workers — restrict network access.
- JSON serializer only; reject `pickle`.
- Do not pass secrets, tokens or PII in task arguments — they land in broker storage, logs, Flower and result
  backends. Pass IDs and load inside the task.
- Flower exposes task arguments and allows revoking tasks: put it behind authentication and network controls.
- Tenant context must travel in task arguments and be re-validated in the task.

### Performance
- `worker_prefetch_multiplier=1` for long integration tasks; higher for many short tasks.
- Concurrency per provider queue equals the provider's concurrency budget, not CPU count.
- Use `ignore_result=True`; result backends become a bottleneck at volume.
- Worker pool choice (prefork vs threads vs gevent) and sizing: see
  [concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md)
  and [background-task-performance](../../django-performance/references/background-task-performance.md).
- Long-lived workers need DB connection hygiene; Celery's Django fixup closes connections around tasks, but
  `CONN_MAX_AGE` and poolers must be configured consistently.

## 9. Common mistakes and anti-patterns

- `task.delay()` inside `transaction.atomic()` → task runs before commit or after rollback.
- Passing model instances or querysets as task arguments.
- Non-idempotent tasks with `acks_late=True` (duplicate charges/emails).
- Tasks longer than the Redis/SQS visibility timeout; long `countdown`/`eta` on Redis.
- Infinite or very high `max_retries` without backoff → retry storms during outages.
- Retrying on every exception (`autoretry_for=(Exception,)`) including validation and 4xx errors.
- Using `time.sleep()` in a task to wait for a remote job.
- Relying on `rate_limit` as a global provider limit.
- Two beat instances; scheduled tasks without `expires`.
- Storing integration outcomes only in the result backend.
- One shared queue for all providers — a slow provider blocks everything.
- Adding Celery for one nightly job that cron could run.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `DoesNotExist` in task for a just-created row | Dispatched before commit | `on_commit` usage |
| Same task runs twice concurrently | Visibility timeout < duration; duplicate dispatch | Broker transport options, task duration p99 |
| Tasks silently disappear | Early ack + worker crash; Redis eviction/failover | `acks_late`, Redis `maxmemory-policy` (must be `noeviction`) |
| Queue grows, workers idle | Prefetch hoarding, wrong queue names | `celery inspect active/reserved`, `-Q` flags |
| Scheduled job runs twice | Two beat processes | Deployment replicas |
| Retry storm after outage | No jitter/backoff cap, high max_retries | Task options, retry metrics |
| Tasks hang forever | No HTTP timeout, no `time_limit` | Client timeouts, soft/hard limits |

Validation checklist:
- [ ] Celery is justified over sync call, cron or DB queue.
- [ ] All dispatch from transactional code uses `on_commit`; critical work uses an outbox.
- [ ] Integration tasks are idempotent and take IDs, not objects; JSON serializer only.
- [ ] `acks_late` set deliberately; visibility timeout > max duration (Redis/SQS).
- [ ] Retries limited to transient errors, with backoff, jitter and max; ambiguous outcomes reconciled.
- [ ] Per-provider queues and concurrency; global rate limits where the provider requires them.
- [ ] Soft/hard time limits; long jobs checkpointed.
- [ ] One beat instance; schedules have `expires` and overlap protection.
- [ ] Queue depth, task failure rate and latency monitored; Flower secured.
- [ ] Tests cover retry, duplicate execution and on_commit behaviour
  ([django-testing](../../django-testing/references/integration-and-background-task-testing.md)).

## 11. Related references and skills

- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retry policy, idempotency keys, rate limiting.
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — brokers for cross-system events.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox, process manager, saga.
- [integration-observability-and-operations.md](integration-observability-and-operations.md) — task metrics, correlation.
- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — async webhook processing.
- [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md) — checkpoints, chunked sync.
- django-performance: [background-task-performance](../../django-performance/references/background-task-performance.md),
  [concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md).
- django-testing: [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md),
  [database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md).
- django-architecture: [service-layer](../../django-architecture/references/service-layer.md),
  [domain-events](../../django-architecture/references/domain-events.md).
- [django-data-processing](../../django-data-processing/SKILL.md) — bulk import/export pipelines.

## 12. Official documentation and external references

- Celery user guide — tasks: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery — using Celery with Django: https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
- Celery — routing: https://docs.celeryq.dev/en/stable/userguide/routing.html
- Celery — canvas (workflows): https://docs.celeryq.dev/en/stable/userguide/canvas.html
- Celery — periodic tasks: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- Celery — configuration reference: https://docs.celeryq.dev/en/stable/userguide/configuration.html
- Celery — Redis broker caveats: https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
- Celery — monitoring: https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- Flower: https://flower.readthedocs.io/
- django-celery-beat: https://django-celery-beat.readthedocs.io/
- Django transactions — `on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Django 6.0 Tasks framework: https://docs.djangoproject.com/en/6.0/topics/tasks/ `VERIFY`
- RabbitMQ consumer acknowledgement timeout: https://www.rabbitmq.com/docs/consumers#acknowledgement-timeout
