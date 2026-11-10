# Django background processing for data jobs

> **Scope:** Running data work outside the request: when a management command is enough, Celery
> task design for data jobs, decomposition and fan-out, idempotent tasks, retries with backoff and
> jitter, time limits, routing and priorities, periodic jobs, concurrency, results, dead-letter
> handling, long-running jobs, progress, cancellation, and job status via the `ProcessingJob`
> state machine. **Not here:** Celery/broker installation and infrastructure configuration
> ([../../django-integrations/references/celery-and-background-integrations.md](../../django-integrations/references/celery-and-background-integrations.md)),
> worker sizing and throughput tuning ([data-processing-performance.md](data-processing-performance.md),
> [../../django-performance/references/background-task-performance.md](../../django-performance/references/background-task-performance.md)),
> data-level DLQ and recovery runbooks ([processing-reliability-and-recovery.md](processing-reliability-and-recovery.md)),
> multi-stage pipeline state ([etl-and-elt-pipelines.md](etl-and-elt-pipelines.md)),
> user-facing import/export workflow ([data-import-and-export-systems.md](data-import-and-export-systems.md)).

## 1. Purpose and scope

Data jobs differ from ordinary background tasks (send an email, refresh a cache): they run for
minutes to hours, touch thousands to millions of rows, must report progress, must be cancellable,
and must leave data correct after crashes and redeliveries. This reference defines how to:

- pick the execution vehicle (management command vs Celery vs alternatives);
- model a job as a database row with a guarded state machine (`ProcessingJob`);
- design tasks that are idempotent under Celery's at-least-once delivery;
- decompose large work into chunks, checkpoint, re-enqueue and track progress;
- handle failure (retries, dead-letter, recovery) and cancellation cooperatively.

## 2. Core concepts

### 2.1 Execution vehicles

| Vehicle | Strength | Weakness |
|---|---|---|
| Management command | Simplest; same code path; good for ops, migrations, cron | No retry, no distribution, overlap under cron, killed on deploy |
| Celery task [third-party: celery] | Retries, queues, routing, beat scheduling, fan-out | Broker to run; at-least-once; visibility-timeout and time-limit pitfalls |
| `django.tasks` (Django ≥ 6.0) | Backend-agnostic `@task` / `.enqueue()` API | Built-in backends are for development/testing; production needs a third-party backend (VERIFY: maturity of the chosen backend) |
| RQ / Dramatiq | Lighter than Celery | Only if already used by the project |

### 2.2 The job row is the source of truth

The Celery result backend is not a job store: results expire, `update_state` requires a result
backend, and task ids change on re-enqueue. Status, progress, counts, checkpoints, errors and the
cancel flag live on a `ProcessingJob` row in PostgreSQL, which the UI/API reads.

### 2.3 Job state machine

```text
             enqueue (on_commit)          worker claims
  PENDING ───────────────────► QUEUED ─────────────────► RUNNING ──┬─► SUCCEEDED
     │                           │                         │  ▲    ├─► PARTIAL   (some records rejected)
     │ cancel                    │ cancel                  │  │    └─► FAILED
     ▼                           ▼                         │  │ re-enqueue (checkpoint)
  CANCELLED ◄────────────────────┴───── cancel observed ───┘  └── RUNNING → QUEUED
```

| From | To | Guard |
|---|---|---|
| PENDING | QUEUED | Enqueued after commit |
| QUEUED | RUNNING | Atomic claim: `UPDATE … WHERE status='QUEUED'` affects 1 row |
| RUNNING | QUEUED | Long job checkpointed and re-enqueued itself |
| RUNNING | SUCCEEDED / PARTIAL / FAILED | Only the claiming attempt (matching `attempt_token`) |
| PENDING / QUEUED / RUNNING | CANCELLED | Cancel requested and observed (RUNNING) or immediate (PENDING/QUEUED) |
| Terminal | anything | Never (re-processing creates a new job linked by `retry_of`) |

### 2.4 Delivery semantics recap

Celery with `acks_late=True` is at-least-once: a task can run twice (worker lost, visibility
timeout expiry on Redis/SQS, retry after a partial side effect). Never exactly-once. Therefore:
claim atomically, write idempotently, checkpoint transactionally. Concepts:
[data-processing-fundamentals.md](data-processing-fundamentals.md#31-delivery-and-processing-guarantees).

## 3. Internal mechanics

### 3.1 What happens on a Celery data task

```text
web request  → INSERT ProcessingJob(PENDING) ; on_commit → task.delay(job_id) ; job → QUEUED
broker       → message stored; worker prefetches (prefetch_multiplier=1 for long tasks)
worker       → executes; with acks_late the message is acked only after return/raise
crash        → message redelivered (RabbitMQ: on connection loss; Redis/SQS: after visibility_timeout)
soft limit   → SoftTimeLimitExceeded raised inside the task (cleanup chance)
hard limit   → worker child process killed; no cleanup; with acks_late + reject_on_worker_lost → redelivered
```

Time limits rely on signals and are not enforced on Windows or with some pools (threads/gevent/
eventlet) (VERIFY: per pool in your Celery version). On Redis/SQS, a task running longer than
`visibility_timeout` is delivered to a second worker *while the first still runs* — the classic
duplicate-execution bug. Long jobs must therefore either finish well within the timeout or
checkpoint and re-enqueue.

### 3.2 Retry, backoff and jitter

`retry_backoff=True` makes delays grow exponentially (1, 2, 4, 8 … seconds, scaled by the
backoff factor) capped by `retry_backoff_max`; `retry_jitter=True` randomises each delay in
`[0, delay]` to avoid thundering herds when many tasks fail together (VERIFY: exact jitter formula
for your Celery version). Each retry is a **new message** (with the same task id) — the current
execution ends. `max_retries` bounds attempts; after exhaustion the original exception is raised
and the task is marked failed.

### 3.3 Dead-letter handling

Celery has no built-in DLQ. Options:

1. **Domain-level (recommended baseline):** on final failure, mark `ProcessingJob` `FAILED` with
   error class/message and keep the inputs; an operator or automated policy re-runs it.
2. **Broker-level (RabbitMQ):** declare queues with `x-dead-letter-exchange`; raise
   `celery.exceptions.Reject(requeue=False)` to route the message there (requires `acks_late`).
3. **Record-level:** failing records go to a failed-record table while the job continues —
   owned by [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

## 4. Engineering decisions and trade-offs

### 4.1 Command vs Celery

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Nightly ops job, one instance, operator reads logs | Management command via platform cron/K8s CronJob + DB lock | Needs retry/fan-out or user-triggering → Celery |
| One-off backfill / data migration | Management command with checkpoint and `--resume` | — |
| User-triggered import/export with progress | Celery task + `ProcessingJob` | — |
| Periodic job every few minutes | Celery beat (single beat) | Platform scheduler already standard → cron calling a command |
| Huge job (> visibility timeout) | Chunked tasks with checkpoint/re-enqueue | Truly continuous → streaming consumer process |
| New project wanting minimal API | `django.tasks` (Django ≥ 6.0) with a production backend | Need canvas, routing, beat → Celery |

### 4.2 Task design choices

| Choice | Gain | Cost / risk |
|---|---|---|
| One task per job, loop over chunks | Simple ordering and progress | Long runtime → visibility/time-limit issues |
| Fan-out task per chunk | Parallelism, isolated retries | Coordination; DB contention; must aggregate counts |
| Chord to aggregate | Built-in join | Requires reliable result backend; a failed header task can block the callback |
| Counter-based completion (DB) | No result backend; robust | Must design atomic counter + "last one finishes" check |
| `acks_late=True` | No loss on crash | Duplicates → idempotency mandatory |
| `ignore_result=True` | No result-backend load | Can't use chords/`AsyncResult` (fine with job rows) |
| Separate queues (`bulk`, `default`, `priority`) | Slow jobs don't starve fast tasks | More worker pools to run |

### 4.3 Retry policy by error class

| Error | Class | Action |
|---|---|---|
| `OperationalError`, connection reset, timeout, HTTP 429/503 | Transient | Retry with backoff + jitter, bounded |
| Deadlock / serialization failure | Transient | Retry the chunk transaction (small countdown) |
| Validation error on a record | Data | Reject record, continue; never retry |
| Missing file / 404 source / bad config | Permanent | Fail job, no retry |
| `SoftTimeLimitExceeded` | Resource | Checkpoint and re-enqueue, or fail with clear message |
| Unknown exception | Unknown | Fail job, alert; don't blindly retry |

## 5. Django/Python implementation patterns

### 5.1 `ProcessingJob` model

```python
import uuid  # [stdlib]
from django.db import models  # [Django]


class ProcessingJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING"
        QUEUED = "QUEUED"
        RUNNING = "RUNNING"
        SUCCEEDED = "SUCCEEDED"
        PARTIAL = "PARTIAL"
        FAILED = "FAILED"
        CANCELLED = "CANCELLED"

    TERMINAL = {Status.SUCCEEDED, Status.PARTIAL, Status.FAILED, Status.CANCELLED}

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    kind = models.CharField(max_length=64)                       # "employee_import", "orders_export"
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING, db_index=True)
    params = models.JSONField(default=dict)                      # ids/keys only, no secrets
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT, null=True)
    attempt_token = models.UUIDField(null=True)                  # identifies the claiming execution
    task_id = models.CharField(max_length=64, blank=True)
    checkpoint = models.JSONField(default=dict)                  # e.g. {"last_pk": 120000}
    total = models.PositiveIntegerField(null=True)
    processed = models.PositiveIntegerField(default=0)
    succeeded_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    error_class = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    result_uri = models.CharField(max_length=1024, blank=True)   # export file key, error report
    retry_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, related_name="+")
    heartbeat_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "kind", "-created_at"])]
        constraints = [models.UniqueConstraint(           # one active job per tenant+kind
            fields=["tenant", "kind"],
            condition=models.Q(status__in=["PENDING", "QUEUED", "RUNNING"]),
            name="one_active_job_per_kind",
        )]
```

### 5.2 Guarded transitions via conditional UPDATE

```python
from django.db.models import F
from django.utils import timezone

S = ProcessingJob.Status


def transition(job_id: int, *, frm: set[str], to: str, token: uuid.UUID | None = None,
               **fields) -> bool:
    """Compare-and-set: succeeds only if the row is still in an allowed state (and owned)."""
    qs = ProcessingJob.objects.filter(pk=job_id, status__in=frm)
    if token is not None:
        qs = qs.filter(attempt_token=token)
    return qs.update(status=to, **fields) == 1


def claim(job_id: int, task_id: str) -> uuid.UUID | None:
    token = uuid.uuid4()
    ok = transition(job_id, frm={S.QUEUED}, to=S.RUNNING, attempt_token=token, task_id=task_id,
                    started_at=timezone.now(), heartbeat_at=timezone.now())
    return token if ok else None          # None → duplicate delivery, cancelled, or already done


def report_progress(job_id: int, token: uuid.UUID, *, done: int, ok: int, rejected: int,
                    checkpoint: dict) -> bool:
    """Returns False if the job was cancelled or taken over — caller must stop."""
    updated = ProcessingJob.objects.filter(
        pk=job_id, attempt_token=token, status=S.RUNNING, cancel_requested=False,
    ).update(processed=F("processed") + done, succeeded_count=F("succeeded_count") + ok,
             rejected_count=F("rejected_count") + rejected, checkpoint=checkpoint,
             heartbeat_at=timezone.now())
    return updated == 1
```

`UPDATE … WHERE status = 'QUEUED'` is atomic in PostgreSQL: of two concurrent deliveries only
one sees `rowcount == 1`. No `select_for_update` round trip is needed. The `attempt_token`
prevents a stale execution (one that outlived its visibility timeout) from overwriting the state
of the execution that replaced it.

### 5.3 Enqueue after commit

```python
from django.db import IntegrityError, transaction


def start_job(*, tenant_id: int, kind: str, params: dict, user_id: int) -> ProcessingJob:
    try:
        with transaction.atomic():
            job = ProcessingJob.objects.create(tenant_id=tenant_id, kind=kind, params=params,
                                               created_by_id=user_id)
    except IntegrityError:
        raise JobAlreadyActive(kind)      # one_active_job_per_kind constraint
    transaction.on_commit(lambda: _enqueue(job.pk))
    return job


def _enqueue(job_id: int) -> None:
    if transition(job_id, frm={S.PENDING}, to=S.QUEUED):
        run_job.apply_async(args=[job_id], queue="bulk")
```

A periodic sweeper re-enqueues jobs stuck in `PENDING`/`QUEUED` (lost message) and marks
`RUNNING` jobs with a stale `heartbeat_at` as re-queueable (resume from checkpoint) or `FAILED`.

### 5.4 Task skeleton: claim, chunk, checkpoint, re-enqueue, cancel

```python
from celery import shared_task                              # [third-party: celery]
from celery.exceptions import SoftTimeLimitExceeded
from django.db import OperationalError

CHUNK_BUDGET_SECONDS = 20 * 60                              # well below soft limit & visibility timeout


@shared_task(bind=True, acks_late=True, ignore_result=True,
             autoretry_for=(OperationalError,), retry_backoff=True, retry_backoff_max=600,
             retry_jitter=True, max_retries=5, soft_time_limit=1800, time_limit=1900)
def run_job(self, job_id: int) -> None:
    token = claim(job_id, self.request.id)
    if token is None:
        return                                              # duplicate/cancelled/finished — no-op
    job = ProcessingJob.objects.get(pk=job_id)
    handler = HANDLERS[job.kind]                            # service object; no Celery inside
    deadline = time.monotonic() + CHUNK_BUDGET_SECONDS
    try:
        for chunk_result in handler.run_chunks(job):        # yields after each committed chunk
            if not report_progress(job_id, token, **chunk_result.as_progress()):
                _finish_cancel_or_abandon(job_id, token)
                return
            if time.monotonic() > deadline:                 # long job: hand off to a fresh task
                if transition(job_id, frm={S.RUNNING}, to=S.QUEUED, token=token):
                    run_job.apply_async(args=[job_id], queue="bulk")
                return
        job.refresh_from_db()
        final = S.PARTIAL if job.rejected_count else S.SUCCEEDED
        transition(job_id, frm={S.RUNNING}, to=final, token=token, finished_at=timezone.now())
    except SoftTimeLimitExceeded:
        transition(job_id, frm={S.RUNNING}, to=S.FAILED, token=token,
                   error_class="SoftTimeLimitExceeded", finished_at=timezone.now())
    except OperationalError:
        transition(job_id, frm={S.RUNNING}, to=S.QUEUED, token=token)   # allow re-claim
        raise                                                            # autoretry
    except Exception as exc:
        transition(job_id, frm={S.RUNNING}, to=S.FAILED, token=token,
                   error_class=type(exc).__name__, error=str(exc)[:2000],
                   finished_at=timezone.now())
        raise
```

`import time` and `from django.utils import timezone` are assumed. The handler resumes from
`job.checkpoint`, so a re-enqueued or retried task continues where the last committed chunk
ended. Each chunk's data writes and its checkpoint must commit together (inside the handler),
or a crash between them replays a chunk — acceptable only if chunk writes are idempotent (UPSERT).

### 5.5 Cooperative cancellation

```python
def request_cancel(job_id: int) -> None:
    # Not yet running: cancel immediately.
    if transition(job_id, frm={S.PENDING, S.QUEUED}, to=S.CANCELLED, finished_at=timezone.now()):
        return
    # Running: set the flag; the worker observes it at the next chunk boundary.
    ProcessingJob.objects.filter(pk=job_id, status=S.RUNNING).update(cancel_requested=True)


def _finish_cancel_or_abandon(job_id: int, token: uuid.UUID) -> None:
    transition(job_id, frm={S.RUNNING}, to=S.CANCELLED, token=token, finished_at=timezone.now())
    # If the token no longer matches, another execution owns the job — just exit.
```

Committed chunks stay committed; if the business needs rollback on cancel, write into staging
and only promote on success ([database-loading-and-persistence.md](database-loading-and-persistence.md)).
`revoke(terminate=True)` kills the process mid-transaction, works only on prefork, and is a last
resort.

### 5.6 Fan-out by partition without a chord

```python
@shared_task(acks_late=True, ignore_result=True)
def export_partition(job_id: int, partition: str) -> None:
    ...  # write part file for partition, idempotently (overwrite same key)
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().get(pk=job_id)
        parts = set(job.checkpoint.get("done_parts", []))
        parts.add(partition)                                # set → duplicate delivery is harmless
        job.checkpoint = {**job.checkpoint, "done_parts": sorted(parts)}
        job.save(update_fields=["checkpoint"])
        all_done = len(parts) == job.params["partition_count"]
    if all_done:
        transaction.on_commit(lambda: finalize_export.delay(job_id))  # finalize is idempotent
```

The row lock serialises completion bookkeeping; the set makes redelivery idempotent. Use a chord
instead only when a result backend is already operated reliably.

### 5.7 Routing, priorities, periodic jobs

```python
from celery.schedules import crontab         # [third-party: celery]

CELERY_TASK_ROUTES = {
    "imports.tasks.*": {"queue": "bulk"},
    "exports.tasks.*": {"queue": "bulk"},
    "jobs.tasks.sweep_stuck_jobs": {"queue": "default"},
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1        # long tasks: don't hoard messages
CELERY_BEAT_SCHEDULE = {
    "sweep-stuck-jobs": {"task": "jobs.tasks.sweep_stuck_jobs",
                         "schedule": 300.0, "options": {"expires": 240}},
    "nightly-attendance-rollup": {"task": "attendance.tasks.rollup",
                                  "schedule": crontab(hour=2, minute=15)},
}
```

- Prefer separate queues and worker pools over message priorities; priority support varies by
  broker (RabbitMQ `x-max-priority`; Redis emulates it) (VERIFY: broker priority semantics).
- Exactly **one** beat process; periodic tasks must tolerate being skipped or run twice (use the
  active-job constraint or a lock). `expires` drops stale periodic messages.
- Broker settings (`visibility_timeout`, serializers, result backend) belong in
  [../../django-integrations/references/celery-and-background-integrations.md](../../django-integrations/references/celery-and-background-integrations.md).

## 6. Production use cases

### 6.1 Large import: 2M-row employee/payroll inputs

- Upload stored → `ProcessingJob(kind="employee_import")` → `run_job` on `bulk` queue.
- Handler streams the file ([file-processing-engineering.md](file-processing-engineering.md)),
  validates per row, UPSERTs 2,000 rows per transaction keyed by `(tenant, employee_number)`,
  writes `RejectedRecord`s, and commits `checkpoint={"row": n}` with each chunk.
- Every ~20 minutes the task re-enqueues itself; a worker restart during deploy resumes from the
  last row. Final status `PARTIAL` with downloadable error report if any rows were rejected.
- Verification: `processed == succeeded + rejected` and file row count equals `processed`.

### 6.2 Large export: 5M orders for finance

- `ProcessingJob(kind="orders_export", params={"from": …, "to": …})`.
- Fan out by month partitions; each `export_partition` reads with keyset pagination
  (`pk__gt=last_pk`), streams CSV rows into a temp file, uploads to object storage under a
  deterministic key `exports/{job_id}/part-{partition}.csv` (overwrite = idempotent).
- `finalize_export` concatenates or zips parts, sets `result_uri`, transitions to `SUCCEEDED`, and
  notifies on commit; downloads are served via short-lived signed URLs
  ([data-import-and-export-systems.md](data-import-and-export-systems.md)).
- CSV formula injection neutralised on write ([data-processing-security.md](data-processing-security.md)).

### 6.3 Other scenarios

| Scenario | Vehicle | Notes |
|---|---|---|
| Nightly inventory valuation | Management command via K8s CronJob | Advisory lock; idempotent per valuation date |
| Hourly hospital doctor-roster sync | Celery beat → pipeline task | One active job per tenant |
| Customer dedupe/merge backfill | Command with `--resume` checkpoint | Run once; operator-monitored |
| Payment settlement reconciliation | Celery task after file ingest | Fails loud on mismatch; no auto-retry on data errors |

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Job row + guarded transitions | Correct under duplicates; UI-friendly; auditable | More code than `task.delay()` |
| Checkpoint + re-enqueue | Survives deploys, avoids visibility-timeout duplicates | Handler must be resumable |
| Cooperative cancel | Safe, consistent stop at chunk boundary | Latency up to one chunk |
| Fan-out | Throughput, isolated failures | Contention; aggregation logic |
| Management commands | Zero infra | No retry/distribution |

## 8. Security and performance considerations

**Security**

- Task arguments are ids and primitives only; JSON serializer, never pickle (pickle from a
  compromised broker is code execution).
- Re-check authorisation and tenant ownership inside the task using `job.created_by`/`tenant` —
  the enqueuing request's checks may be stale ([../../django-security-engineering/references/authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- `ProcessingJob.error` and `params` are shown to users: truncate, strip stack traces and PII.
- Cancel/retry/re-run endpoints require permission on the job's tenant; audit who did it.
- Broker credentials and TLS are infrastructure concerns — see django-integrations.

**Performance**

- `worker_prefetch_multiplier=1` and a dedicated `bulk` queue keep long jobs from starving short
  tasks; size `concurrency` by per-task memory and DB connections, not CPU count alone
  ([../../django-performance/references/concurrency-and-worker-management.md](../../django-performance/references/concurrency-and-worker-management.md)).
- Progress updates once per chunk, not per row (each is a write + WAL).
- Fan-out width bounded by DB connection pool and lock contention; partitions must not overlap
  keys.
- Close DB connections before forking any multiprocessing pool inside a task
  (`django.db.connections.close_all()`); long tasks should not hold a transaction across chunks.
- `ignore_result=True` unless results are consumed; result backends grow and cost I/O.

## 9. Common mistakes and anti-patterns

- `task.delay()` inside `atomic()` → worker reads before commit, job not found → `on_commit`.
- Status checks with read-then-write (`if job.status == QUEUED: job.status = RUNNING; save()`) →
  two workers both run → conditional `UPDATE … WHERE status=…`.
- Tasks longer than the Redis/SQS visibility timeout → second worker starts the same job →
  checkpoint and re-enqueue within budget.
- Progress stored via `update_state` in the result backend → lost on expiry, unavailable without
  backend → progress on the job row.
- Retrying validation errors → same failure five times, delayed alert → classify errors.
- `revoke(terminate=True)` as normal cancellation → half-written chunks, inconsistent state →
  cooperative flag.
- Chord without a reliable result backend → callback never fires → DB counter / done-set.
- Two beat processes → every periodic job runs twice → single beat + idempotent jobs.
- Passing model instances or querysets to tasks → stale data, serialisation errors → pass ids.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Job stuck `QUEUED` | Message lost / no worker on queue | Check routes and worker `-Q`; sweeper re-enqueues |
| Job stuck `RUNNING`, no heartbeat | Hard-killed worker without redelivery | Sweeper by `heartbeat_at`; resume from checkpoint |
| Same job running twice | Visibility timeout < runtime; duplicate enqueue | Shorten chunks; verify claim uses conditional update |
| Time limits ignored | Windows or non-prefork pool | Run prefork on Linux; enforce budget in code |
| Short tasks delayed for hours | Long jobs on same queue with prefetch | Separate `bulk` queue; prefetch 1 |
| Retries hammer a down API | No jitter/cap | `retry_jitter`, `retry_backoff_max`, `max_retries` |
| Cancel has no effect | Handler never checks flag | `report_progress` per chunk returns False → stop |
| Counts don't add up after retry | Counters incremented for replayed chunk | Commit counters with chunk data + checkpoint |

**Validation checklist**

- [ ] Job created in a transaction, enqueued with `on_commit`.
- [ ] Claim and all transitions use conditional updates; terminal states final.
- [ ] Task args are ids; handler holds the logic and is tested without Celery.
- [ ] Retries only for transient errors; bounded with backoff + jitter.
- [ ] Each chunk's writes and checkpoint commit together; re-run is idempotent.
- [ ] Runtime per task < soft limit < hard limit < visibility timeout.
- [ ] Cancel flag honoured at chunk boundaries; sweeper for stuck jobs scheduled.
- [ ] Final status derived from verified counts.

## 11. Related references and skills

- In this skill: [data-processing-fundamentals.md](data-processing-fundamentals.md),
  [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md),
  [data-import-and-export-systems.md](data-import-and-export-systems.md),
  [large-dataset-processing.md](large-dataset-processing.md),
  [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md),
  [data-processing-performance.md](data-processing-performance.md),
  [data-processing-observability.md](data-processing-observability.md),
  [data-processing-testing.md](data-processing-testing.md),
  [production-architectures-imports-and-pipelines.md](production-architectures-imports-and-pipelines.md).
- Other skills: [django-integrations Celery infrastructure](../../django-integrations/references/celery-and-background-integrations.md),
  [django-core management commands](../../django-core/references/signals-management-commands.md),
  [django-orm transactions](../../django-orm/references/transactions-and-concurrency.md),
  [django-performance background tasks](../../django-performance/references/background-task-performance.md),
  [django-testing background tasks](../../django-testing/references/integration-and-background-task-testing.md).

## 12. Official documentation and external references

- Celery tasks (acks_late, retries, time limits): https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery canvas: https://docs.celeryq.dev/en/stable/userguide/canvas.html
- Celery routing: https://docs.celeryq.dev/en/stable/userguide/routing.html
- Celery periodic tasks: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- Celery workers (revoke, time limits, concurrency): https://docs.celeryq.dev/en/stable/userguide/workers.html
- Celery configuration: https://docs.celeryq.dev/en/stable/userguide/configuration.html
- Django `transaction.on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Django management commands: https://docs.djangoproject.com/en/5.2/howto/custom-management-commands/
- Django tasks framework (Django ≥ 6.0): https://docs.djangoproject.com/en/6.0/topics/tasks/
- PostgreSQL `UPDATE`: https://www.postgresql.org/docs/current/sql-update.html
