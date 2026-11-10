# Background Task Performance

**Scope:** Performance engineering for work moved out of the request–response cycle: deciding
what to offload, designing Celery tasks and workers for throughput and bounded latency, and
measuring queues, retries and backpressure. Task mechanics (Celery setup, brokers, retries as an
integration concern) belong to [django-integrations](../../django-integrations/SKILL.md) and
pipelines to [django-data-processing](../../django-data-processing/SKILL.md).

## 1. Purpose and scope

Offloading turns a slow synchronous request into a fast one plus asynchronous work. It does not
make the work cheaper — it moves the cost to another fleet, adds queue latency, and changes the
consistency model. This reference helps an agent:

- Decide whether offloading actually helps the user-visible metric.
- Size and configure workers for the task's resource profile (CPU vs I/O).
- Keep queue latency bounded per latency class.
- Avoid retry storms, duplicate work and unbounded queue growth.
- Measure end-to-end task latency, not just task runtime.

Out of scope: Celery installation and broker wiring ([django-integrations](../../django-integrations/SKILL.md)),
testing tasks ([django-testing integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md)),
worker-process theory ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)).

Celery is **[third-party: celery]**. Flower is **[third-party: flower]**. Django 6.0 adds a
built-in Tasks framework (`django.tasks`) that defines a task API and backends; in Django 6.0 it
ships only development/immediate backends and production execution needs a third-party backend
(VERIFY against the 6.0 release notes). Django 5.2 has no built-in task queue.

## 2. Core concepts

| Term | Meaning | Why it matters |
|---|---|---|
| Queue latency (wait time) | `started_at − enqueued_at` | What users feel for "your export is ready"; grows non-linearly near saturation |
| Service time | Task runtime on a worker | Determines capacity per worker slot |
| End-to-end latency | Queue latency + service time + result delivery | The real SLO for async features |
| Throughput | Tasks completed / second | Must exceed arrival rate on average, or the queue grows without bound |
| Concurrency slot | One unit of parallel execution (process, thread, greenlet) | Capacity = slots ÷ mean service time |
| Prefetch | Messages a worker reserves before executing | Hides broker round trips; starves other workers on long tasks |
| Acknowledgement | When the broker considers a message done | Early ack loses work on crash; late ack re-delivers it |
| Retry amplification | Each failure generating extra load | Turns a dependency slowdown into an outage |
| Backpressure | Mechanism that slows producers when consumers lag | Without it, queues absorb overload until memory or SLOs break |

Capacity from Little's Law (see [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md)):
required slots ≈ arrival rate × mean service time. Illustrative: 40 thumbnail tasks/s × 0.25 s ≈
10 busy slots on average; provision headroom for peaks and the p99 service time, then verify
utilisation stays well below 100%, because queue wait rises sharply as utilisation approaches 1.

## 3. Internal mechanics

### Message lifecycle (Celery)

1. Producer serialises task name + args (JSON by default) and publishes to the broker.
2. Worker main process consumes messages up to `concurrency × worker_prefetch_multiplier`
   unacknowledged messages.
3. A pool slot executes the task. With default `acks_late=False` the message is acked
   **before** execution; with `acks_late=True` it is acked after the task returns.
4. Result (if not ignored) is written to the result backend.
5. On `self.retry()`, a **new message** is published with an ETA/countdown — retries are new
   load on the broker and on the queue.

### Pools

| Pool | Parallelism | Fits | Pitfalls |
|---|---|---|---|
| `prefork` (default) | Processes; true CPU parallelism | CPU-bound work, C extensions, isolation | Memory per process; fork-time state; `max_tasks_per_child` needed for leaky code |
| `threads` | OS threads, GIL-bound for Python code | I/O-bound tasks with thread-safe libs | No CPU parallelism; thread safety of clients |
| `gevent` / `eventlet` | Greenlets, cooperative | Very high I/O fan-out (HTTP calls) | Requires monkey-patching; one blocking C call stalls all; DB drivers must be green-compatible (VERIFY for psycopg 3) |
| `solo` | One task at a time in the main process | Debugging, very heavy single tasks, container-per-worker scaling | Worker can't respond to control commands during a task |

### ETA/countdown tasks

Tasks with an ETA are held **in worker memory** until due (on Redis/SQS they are bound by the
`visibility_timeout`). Large numbers of far-future ETAs inflate worker memory and can be
re-delivered if they exceed the visibility timeout (VERIFY default visibility timeout for the
Redis transport — historically 1 hour). Use a scheduler or a DB "due_at" table for far-future
work.

### Time limits

`task_soft_time_limit` raises `SoftTimeLimitExceeded` inside the task so it can clean up;
`task_time_limit` kills the process/slot (prefork only enforces hard kills reliably). Without
limits, a hung HTTP call permanently consumes a slot.

## 4. Engineering decisions and trade-offs

### Should this work leave the request?

Offload when **at least one** holds:

- The user does not need the result in the response (email, webhook, audit fan-out, search
  indexing, thumbnails).
- The work is slow relative to the endpoint's latency budget (e.g. budget p95 300 ms, PDF takes
  2 s).
- The work calls an unreliable dependency whose failure should not fail the request.
- The work must be rate-limited or serialised independently of web traffic.

Keep it synchronous when:

- The user needs the result immediately and it is fast enough — offloading adds polling UX,
  queue latency and a second failure mode.
- Correctness requires the result in the same transaction (offloading makes it eventually
  consistent).
- The task would be tiny and frequent — broker round trip + serialisation can exceed the work.

### Latency classes and queues

| Class | Example | Target queue latency (illustrative) | Isolation |
|---|---|---|---|
| Interactive | Password reset email, "export ready" for small exports | < 5 s | Dedicated `critical` queue + workers |
| Default | Webhooks, indexing | < 1 min | `default` |
| Bulk | Nightly reports, re-indexing, imports | Minutes–hours | `bulk` queue, separate workers, lower priority |

One shared queue means a 50,000-task backfill delays password resets by the time it takes to
drain. Separate queues **with separate worker processes** — routing alone doesn't help if the
same workers consume every queue in round-robin.

### Prefetch and acknowledgement

| Workload | Setting | Why |
|---|---|---|
| Many short tasks (ms) | Default prefetch multiplier (4) | Amortises broker round trips |
| Long or highly variable tasks | `worker_prefetch_multiplier=1`, `task_acks_late=True` | Prevents one worker hoarding messages while others idle; re-delivers on crash |
| Non-idempotent tasks | Avoid `acks_late` or make them idempotent first | Late ack guarantees redelivery after worker loss → duplicates |

`acks_late` + `task_reject_on_worker_lost=True` re-queues tasks killed by OOM — which can loop
forever on a task that always OOMs. Cap with a delivery count / dead-letter strategy.

### Broker choice (performance notes only)

- **Redis**: low latency, simple ops, shares memory with cache unless separated; no native
  priority beyond multiple lists; reliability depends on visibility timeout semantics (VERIFY).
- **RabbitMQ**: per-message acks, priorities, dead-lettering, better at large backlogs; more
  ops. Quorum queues change throughput characteristics (VERIFY).
- Never share one Redis instance between cache (with eviction policy `allkeys-lru`) and broker:
  eviction silently drops queued tasks.

### Result backend

Storing results costs a write per task plus memory/rows until expiry. Default to
`task_ignore_result=True`; opt in per task when a caller actually reads the result. Set
`result_expires`. Polling `AsyncResult.get()` inside a web request blocks a worker and defeats
offloading.

## 5. Django implementation patterns

### Enqueue after commit, pass IDs

```python
# orders/services.py
from django.db import transaction
from .tasks import send_order_confirmation

def place_order(*, cart, user) -> Order:
    with transaction.atomic():
        order = Order.objects.create_from_cart(cart=cart, user=user)
    # Enqueue only once the row is visible to other connections.
    transaction.on_commit(lambda: send_order_confirmation.delay(order.pk))
    return order
```

Bottleneck addressed: request latency (email ~300–800 ms illustrative) and the race where the
worker runs before commit and gets `DoesNotExist`. Passing a PK keeps messages small and forces
the task to read current state. Validate: endpoint p95 before/after, and task error rate for
`DoesNotExist` drops to zero. On Django ≥ 5.0, `transaction.on_commit(..., robust=True)` keeps
a failing callback from breaking later callbacks (VERIFY exact version).

### Task with limits, backoff and idempotency

```python
# notifications/tasks.py
from celery import shared_task
from django.core.cache import cache

@shared_task(
    bind=True,
    acks_late=True,
    autoretry_for=(TransientHTTPError,),
    retry_backoff=True,          # exponential: 1s, 2s, 4s, ...
    retry_backoff_max=600,
    retry_jitter=True,           # spread retries to avoid synchronised waves
    max_retries=6,
    soft_time_limit=20,
    time_limit=30,
    ignore_result=True,
)
def send_order_confirmation(self, order_id: int) -> None:
    order = Order.objects.select_related("customer").get(pk=order_id)
    if order.confirmation_sent_at:          # idempotency guard: safe on redelivery
        return
    email_gateway.send(order)              # gateway sets its own HTTP timeout < soft limit
    Order.objects.filter(pk=order_id, confirmation_sent_at__isnull=True).update(
        confirmation_sent_at=timezone.now()
    )
```

The guard + conditional update makes redelivery safe; for exactly-once side effects against
external systems use an idempotency key the provider honours (see
[django-integrations](../../django-integrations/SKILL.md) and
[django-orm transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)).

### Routing latency classes

```python
# settings.py
CELERY_TASK_ROUTES = {
    "notifications.tasks.*": {"queue": "critical"},
    "search.tasks.*": {"queue": "default"},
    "reports.tasks.*": {"queue": "bulk"},
}
CELERY_TASK_IGNORE_RESULT = True
CELERY_RESULT_EXPIRES = 3600
CELERY_WORKER_PREFETCH_MULTIPLIER = 1      # long/variable tasks dominate this project
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 270
```

```bash
# Separate processes per class; size each from measured arrival rate × service time.
celery -A config worker -Q critical --concurrency=$CRITICAL_SLOTS -n critical@%h
celery -A config worker -Q default  --concurrency=$DEFAULT_SLOTS  -n default@%h
celery -A config worker -Q bulk     --concurrency=$BULK_SLOTS --max-tasks-per-child=200 -n bulk@%h
```

The settings namespace assumes `app.config_from_object("django.conf:settings", namespace="CELERY")`.

### Batching instead of task-per-row

```python
@shared_task(ignore_result=True, soft_time_limit=120, time_limit=150)
def reindex_products(product_ids: list[int]) -> None:
    products = Product.objects.filter(pk__in=product_ids).select_related("brand")
    search_client.bulk_index([to_document(p) for p in products])

def enqueue_full_reindex(batch_size: int = 500) -> None:
    ids = Product.objects.values_list("pk", flat=True).order_by("pk").iterator(chunk_size=5000)
    batch: list[int] = []
    for pk in ids:
        batch.append(pk)
        if len(batch) == batch_size:
            reindex_products.apply_async(args=[batch], queue="bulk")
            batch = []
    if batch:
        reindex_products.apply_async(args=[batch], queue="bulk")
```

Bottleneck: per-task overhead (publish, deserialise, DB connection checkout, one query, one HTTP
call) dominating 1M tiny tasks. Batching amortises it; trade-off: one failing item fails or
retries the whole batch — make the batch idempotent and log per-item failures. Choose batch size
by measuring task runtime against the soft limit and the downstream bulk API's limits.

### Canvas costs

`group` fans out freely; `chord` requires a result backend and a synchronisation step whose cost
grows with header size (with Redis backend it is implemented via counters; other backends poll —
VERIFY). Avoid chords of thousands of tasks; prefer a batch that writes progress to a DB row, or
a final "check completeness" task.

### Measuring queue latency

```python
# config/celery_signals.py
import time
from celery.signals import before_task_publish, task_prerun

@before_task_publish.connect
def stamp_enqueue_time(headers=None, **kwargs):
    headers["enqueued_at"] = time.time()

@task_prerun.connect
def record_queue_latency(task=None, **kwargs):
    enqueued = getattr(task.request, "enqueued_at", None)
    if enqueued:
        metrics.histogram("celery.queue_latency_seconds", time.time() - enqueued,
                          tags={"task": task.name, "queue": task.request.delivery_info.get("routing_key")})
```

Custom header propagation into `task.request` depends on protocol version (VERIFY attribute
access; fallback: pass `enqueued_at` as a kwarg). Combine with task runtime histograms; OpenTelemetry's
Celery instrumentation [third-party: opentelemetry-instrumentation-celery] links producer and
consumer spans so a trace shows the request, the queue wait and the task.

### Scheduling without overlap

```python
@shared_task(ignore_result=True)
def refresh_dashboard_aggregates() -> None:
    lock_key = "lock:refresh_dashboard_aggregates"
    if not cache.add(lock_key, "1", timeout=600):   # previous run still going
        return
    try:
        rebuild_aggregates()
    finally:
        cache.delete(lock_key)
```

Celery beat publishes on schedule regardless of whether the last run finished; a 5-minute
schedule on a task that degrades to 7 minutes piles up runs that contend for the same rows. Run
exactly **one** beat process. The lock TTL must exceed worst-case runtime or two runs overlap; a
cache-based lock is advisory (lost on cache flush) — use a PostgreSQL advisory lock when overlap
would corrupt data.

### Backpressure at the producer

```python
def enqueue_export(user_id: int) -> bool:
    depth = broker_queue_depth("bulk")              # LLEN for Redis, queue_declare passive for AMQP
    if depth > settings.BULK_QUEUE_MAX_DEPTH:
        return False                                # caller returns 503 / "try later"
    generate_export.apply_async(args=[user_id], queue="bulk")
    return True
```

Also bound per-user concurrency (one in-flight export per user) — cheaper and fairer than a
global depth check.

## 6. Production use cases

- **Checkout email in request**: p95 1.4 s → 220 ms (illustrative) after moving email to
  `on_commit` + task; validated by endpoint histogram and an email "sent within 10 s" SLI.
- **Backfill starves critical queue**: password-reset emails delayed 40 min because a backfill
  shared `default`. Fix: `bulk` queue on separate workers; alert on `critical` queue latency p95.
- **Retry storm**: payment provider degraded; 20k webhook tasks retried with fixed 5 s countdown
  → synchronized waves tripled provider load and filled Redis memory. Fix: exponential backoff
  with jitter, `max_retries`, a circuit breaker that pauses the queue, dead-letter after cap.
- **Prefetch starvation**: 4 workers × multiplier 4 reserved 16 one-hour report tasks while new
  short tasks waited behind them. Fix: multiplier 1 + `acks_late` on the report queue.
- **Worker OOM loop**: image task on huge TIFF OOM-killed; `reject_on_worker_lost` re-queued it
  forever. Fix: size validation before enqueue, `max_tasks_per_child`, delivery-count cap.
- **Result backend bloat**: millions of unread results in Redis; memory alarms. Fix:
  `ignore_result=True` default, `result_expires`.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Request latency decoupled from slow work | Adds queue latency and an eventual-consistency window |
| Failure isolation from flaky dependencies | Another fleet to deploy, scale, monitor and secure |
| Independent scaling per workload class | Duplicate execution must be designed for (at-least-once) |
| Natural place for batching and rate limiting | Debugging spans two processes; needs trace propagation |
| Smooths load spikes via buffering | Buffers hide overload until SLOs or memory break |

## 8. Security and performance considerations

- Task arguments are stored in the broker (and results in the backend) — never pass secrets or
  personal data payloads; pass IDs and re-read with authorisation scoping.
- Use JSON serialisation; pickle allows code execution if the broker is reachable.
- Rate-limit user-triggered task creation (exports, imports): a user can otherwise enqueue
  unbounded work — a cheap DoS against the worker fleet.
- Workers hold DB connections: total connections = web + workers. Account for them in pool
  sizing ([database-performance.md](database-performance.md)); with `CONN_MAX_AGE` in long-lived
  worker processes, stale connections need `close_old_connections()` semantics (Celery's Django
  fixup handles this per task — VERIFY for custom pools).
- Long tasks inside one `atomic()` hold locks and bloat MVCC; commit in batches.

## 9. Common mistakes and anti-patterns

- `task.delay(obj)` passing a model instance — large message, stale data, pickle temptation.
- Enqueuing inside `atomic()` without `on_commit` — worker reads before commit, or the task runs
  for a rolled-back write.
- One queue for everything; or separate queues consumed by the same worker processes.
- No time limits — hung HTTP calls silently eat all slots.
- `retry(countdown=5)` with no backoff, jitter or cap.
- `AsyncResult(...).get()` in a view — synchronous wait, worse than doing the work inline.
- Fan-out of one task per row for millions of rows; or chords with huge headers.
- Gevent pool for CPU-bound work; prefork with high concurrency for pure network I/O when memory
  is the constraint.
- Assuming "tasks are fast" because runtime is fast while queue latency is minutes.
- Multiple beat instances (duplicate schedules) or schedules shorter than worst-case runtime.

## 10. Troubleshooting and validation

| Symptom | Check | Likely cause |
|---|---|---|
| Queue depth rising steadily | Arrival rate vs completion rate per queue | Under-capacity, slow dependency, or retries feeding the queue |
| Queue latency high but workers idle | `celery inspect reserved`, prefetch settings | Prefetch hoarding by busy workers; workers not consuming that queue |
| Task runtime p99 ≫ p50 | Trace spans inside task; dependency latency | External call without timeout; lock waits |
| Duplicate side effects | `acks_late`, visibility timeout vs task duration | Redelivery; missing idempotency guard |
| Worker memory climbs | RSS per child over tasks; ETA task count | Leaks (use `max_tasks_per_child`), held ETA tasks — see [memory-and-resource-management.md](memory-and-resource-management.md) |
| DB "too many connections" after scaling workers | `pg_stat_activity` by application_name | Worker slots × hosts exceed pool budget |

Commands:

```bash
celery -A config inspect active          # what's running now
celery -A config inspect reserved        # prefetched, not yet running
celery -A config inspect stats           # pool, prefetch, processed counts
redis-cli -n 0 LLEN critical             # Redis broker queue depth (queue name = list key)
rabbitmqctl list_queues name messages messages_unacknowledged consumers
```

Validation after any change: compare queue-latency p95, task runtime p95, throughput and error
/retry rates per queue over a representative period; load test the producer path with realistic
arrival patterns ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).
Prove idempotency and retry behaviour in tests ([django-testing integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md)).

## 11. Related references and skills

- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — process/thread models, GIL, slot sizing.
- [database-performance.md](database-performance.md) — connection budgets, lock contention from long transactions.
- [memory-and-resource-management.md](memory-and-resource-management.md) — worker memory growth, recycling.
- [file-and-data-processing-performance.md](file-and-data-processing-performance.md) — expensive transformations run as tasks.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — queue scaling, load shedding.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — queue buildup incidents.
- [performance-architecture-decisions.md](performance-architecture-decisions.md) — sync vs async execution, event-driven processing.
- [django-integrations](../../django-integrations/SKILL.md) — Celery setup, brokers, external API retries.
- [django-data-processing](../../django-data-processing/SKILL.md) — pipeline and ETL task design.
- [django-orm transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md) — `on_commit`, idempotent writes.
- [django-architecture](../../django-architecture/SKILL.md) — outbox and event-driven patterns.

## 12. Official documentation and external references

- Celery — Optimizing: https://docs.celeryq.dev/en/stable/userguide/optimizing.html
- Celery — Tasks (retries, time limits, acks_late): https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery — Workers (pools, concurrency, max-tasks-per-child): https://docs.celeryq.dev/en/stable/userguide/workers.html
- Celery — Routing: https://docs.celeryq.dev/en/stable/userguide/routing.html
- Celery — Periodic tasks (beat): https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- Celery — Canvas: https://docs.celeryq.dev/en/stable/userguide/canvas.html
- Celery — Redis broker caveats: https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
- Celery — Signals: https://docs.celeryq.dev/en/stable/userguide/signals.html
- Django 5.2 — `transaction.on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Django 6.0 — Tasks framework (version-qualified, VERIFY): https://docs.djangoproject.com/en/6.0/topics/tasks/
- RabbitMQ — Consumer prefetch: https://www.rabbitmq.com/docs/consumer-prefetch
- OpenTelemetry Python Celery instrumentation: https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/celery/celery.html
- Flower: https://flower.readthedocs.io/
