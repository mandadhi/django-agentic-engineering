# Background Report Generation

> **Scope:** Generating reports outside the request with Celery: the end-to-end flow, the
> difference between task execution status and report business status, atomic claiming and
> idempotency, duplicate-run prevention, retry classification and limits, time limits, worker
> crashes and the heartbeat reaper, progress, cooperative cancellation, per-tenant concurrency,
> artifact creation, expiry/cleanup task implementation, failure recovery and completion
> notification. **Not here:** Celery/broker fundamentals
> ([django-integrations — celery-and-background-integrations](../../django-integrations/references/celery-and-background-integrations.md)),
> generic data-task design ([django-data-processing — django-background-processing](../../django-data-processing/references/django-background-processing.md)),
> schedules and distribution ([scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md)),
> download endpoints ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)).

## 1. Purpose and scope

Reports move to the background when generation is slower than the request budget, uses more
memory than a web worker should, must survive retries, or must produce a stored, auditable
artifact. This reference turns a Celery task into a **reliable report job**: exactly one
successful artifact per run, no duplicate work for identical concurrent requests, honest status
for users, and automatic recovery from crashed workers.

It uses the canonical `ReportRun`, `ReportArtifact` and `ReportSpec` from
[reporting-architecture-and-design.md](reporting-architecture-and-design.md). Celery is a
baseline, not a requirement — the same state machine works with a management command, Django
≥ 6.0 Tasks with a production backend, or another queue. [VERIFY: Django 6.0 `django.tasks` backend availability for production use]

## 2. Core concepts

### Why background processing

| Pressure | Request-bound outcome | Background outcome |
|---|---|---|
| Runtime > ~2–10 s | Proxy/Gunicorn timeout, user retries, duplicate load | Bounded by task time limits |
| Memory (PDF/XLSX DOM) | Web worker OOM affects all users | Isolated worker pool sized for it |
| CPU rendering | Starves request handling | Separate queue/concurrency |
| Retry on transient DB/storage error | User sees 500 | Automatic bounded retry |
| Audit and re-download | Lost after response | Stored artifact with metadata |

### Task execution status vs report business status

| Aspect | Task execution status (Celery) | Report business status (`ReportRun.status`) |
|---|---|---|
| Owner | Broker/worker/result backend [third-party: celery] | Your database [Django] |
| Values | PENDING (also "unknown"), STARTED, RETRY, SUCCESS, FAILURE, REVOKED | pending, running, succeeded, failed, cancelled, expired |
| Granularity | One per task *attempt/message* | One per *report request* across all attempts |
| Lifetime | Result backend TTL (`result_expires`, default 1 day [VERIFY: Celery `result_expires` default]) | As long as retention requires |
| Meaning of success | Function returned without raising | Artifact stored, checksummed, metadata committed |
| Visible to users | Never directly | Yes — the API and UI read this |
| Tenant-scoped | No | Yes |

Rules: users and APIs read **only** `ReportRun.status`. Celery `PENDING` is also returned for
unknown task IDs, so it cannot distinguish "queued" from "lost". Store `celery_task_id` for
diagnostics only. Run the result backend with `ignore_result=True` for report tasks unless
something consumes the result.

### Business state machine

```text
             request_report()             claim (conditional UPDATE)
  (none) ──────────────────▶ PENDING ─────────────────────────────▶ RUNNING
                               │  ▲                                  │  │  │
                cancel before  │  │ reaper: stale heartbeat,          │  │  │ cancel_requested
                claim          │  └──── attempts left ────────────────┘  │  │ observed
                               ▼                                         │  ▼
                           CANCELLED ◀───────────────────────────────────┘ CANCELLED
                                                     success │  │ permanent error / attempts exhausted
                                                             ▼  ▼
                                                     SUCCEEDED  FAILED
                                                         │
                                         expires_at passed│ cleanup task
                                                         ▼
                                                      EXPIRED
```

Transitions are performed only by conditional `UPDATE ... WHERE status = <expected>`; every
other writer loses the race harmlessly.

## 3. Internal mechanics

### End-to-end flow

```text
 Browser/API            Django web                     Broker            Worker (reports queue)          Storage
     │  POST /runs         │                              │                     │                           │
     │────────────────────▶│ validate params (ReportSpec) │                     │                           │
     │                     │ check permission             │                     │                           │
     │                     │ atomic: INSERT ReportRun     │                     │                           │
     │                     │   PENDING (dedupe constraint)│                     │                           │
     │                     │ on_commit → generate_report  │                     │                           │
     │◀── 202 + Location ──│─────────────────────────────▶│                     │                           │
     │                     │                              │──── deliver ───────▶│ claim: PENDING→RUNNING     │
     │                     │                              │                     │ re-check permission        │
     │                     │                              │                     │ selector (snapshot txn)    │
     │                     │                              │                     │ renderer → temp file       │
     │                     │                              │                     │ heartbeat + progress       │
     │                     │                              │                     │──── save storage_name ────▶│
     │                     │                              │                     │ INSERT ReportArtifact      │
     │                     │                              │                     │ RUNNING→SUCCEEDED          │
     │                     │                              │                     │ on_commit → notify         │
     │  GET /runs/{id} ───▶│ status=succeeded, link       │                     │                           │
```

### Message delivery semantics [third-party: celery]

- With `acks_late=True` the message is acknowledged **after** the task returns; a worker crash
  (OOM kill, node loss) leaves it unacknowledged and the broker redelivers it. Without
  `acks_late`, a crash loses the message and the run stays `RUNNING` forever — only the reaper
  can recover it.
- `reject_on_worker_lost=True` makes a task whose worker process died be re-queued rather than
  marked failed. Combined with a task that OOMs deterministically, this is a **redelivery loop**
  — the attempt counter in `ReportRun` must cap it.
- Redis/SQS brokers use a **visibility timeout**: an unacknowledged message is redelivered after
  it elapses (Redis default 1 hour [VERIFY: Celery Redis transport `visibility_timeout` default]).
  If a report legitimately runs longer than the visibility timeout, a second worker receives the
  same message while the first is still working. The atomic claim prevents duplicate work; also
  keep `time_limit` below the visibility timeout.
- `worker_prefetch_multiplier=1` (with `acks_late`) stops one worker reserving several long
  reports while others are idle.

Delivery is at-least-once; correctness comes from the claim and idempotent artifact writes, not
from the broker.

### Time limits [third-party: celery]

- `soft_time_limit` raises `SoftTimeLimitExceeded` inside the task — catch it to mark the run
  failed with `error_code="timeout"` and clean temp files.
- `time_limit` hard-kills the worker process; nothing in the task runs afterwards. The reaper must
  handle it.
- Soft limits depend on signals and are not supported by every pool/platform (e.g. not on
  Windows, limited in thread pools). [VERIFY: soft time limit support per pool in Celery 5.x]

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Alternative | Trade-off |
|---|---|---|---|
| Enqueue point | `transaction.on_commit(lambda: generate_report.delay(str(run.pk)))` | `delay_on_commit` (Celery ≥ 5.4 [VERIFY: version that added `Task.delay_on_commit`]) | Enqueue before commit → worker can't see the row |
| Task argument | `run_id` only | Full params in message | Message params drift from DB; PII in broker |
| Dedupe | Partial unique constraint on active runs + reuse recent succeeded artifact | Redis lock | DB constraint survives restarts and is transactional |
| Claim | Conditional `UPDATE` returning rowcount | `select_for_update` | UPDATE is one round trip, no long-held lock |
| Retry | Only classified transient errors, max 3, exponential backoff + jitter | Retry everything | Retrying a bad query/permission error wastes capacity |
| Hard-crash recovery | `acks_late` + reaper with heartbeat | `acks_late` alone | Visibility timeout may be hours; reaper bounds detection time |
| Cancellation | Cooperative `cancel_requested` flag checked per chunk | `revoke(terminate=True)` | Termination kills mid-write, works only for some pools, revocations are per-worker memory |
| Progress | Coarse (row count / estimated total) every N rows | Per-row updates | Per-row writes hammer the DB |
| Queue | Dedicated `reports` queue, separate worker pool | Shared default queue | Isolates memory-heavy rendering from latency-sensitive tasks |
| Tenant fairness | Per-tenant active-run cap at request time | Per-tenant queues | Queues per tenant don't scale; caps are simple and visible |

## 5. Implementation patterns

### Requesting a run with dedupe [Django]

```python
ACTIVE = [ReportRun.Status.PENDING, ReportRun.Status.RUNNING]

def request_report(*, user, tenant, key, params, fmt) -> ReportRun:
    # Same as the canonical service in reporting-architecture-and-design.md, plus a per-tenant cap.
    spec = get_spec(key)
    if not user.has_perm(spec.permission):
        raise PermissionDenied
    clean = validate_params(spec, params, tenant=tenant, user=user)   # spec.params_form → canonical JSON
    scope = f"user:{user.pk}" if spec.user_scoped else "tenant"
    digest = params_hash(key=key, version=spec.version, fmt=fmt, scope=scope, params=clean)
    if ReportRun.objects.filter(tenant=tenant, status__in=ACTIVE).count() >= settings.REPORTS_MAX_ACTIVE_PER_TENANT:
        raise TooManyActiveReports
    try:
        with transaction.atomic():
            run = ReportRun.objects.create(
                tenant=tenant, requested_by=user, report_key=key, report_version=spec.version,
                format=fmt, params=clean, params_hash=digest,
                correlation_id=current_correlation_id())
            transaction.on_commit(lambda: generate_report.delay(str(run.pk)))
    except IntegrityError:                                    # uniq_active_report_run
        run = ReportRun.objects.get(tenant=tenant, report_key=key, params_hash=digest,
                                    format=fmt, status__in=ACTIVE)
    return run
```

- The partial `UniqueConstraint(fields=["tenant", "report_key", "params_hash", "format"],
  condition=Q(status__in=["pending", "running"]), name="uniq_active_report_run")` makes
  concurrent identical requests collapse onto one run. Whether two *users* may share one run
  depends on row-level security: include `requested_by` in the constraint when output differs per
  user.
- Optionally reuse a recent `SUCCEEDED` run with the same hash when `data_as_of` is still within
  freshness tolerance — see [report-generation-performance.md](report-generation-performance.md).

### The task [third-party: celery]

```python
TRANSIENT = (OperationalError, ConnectionError, StorageTransientError)

@shared_task(bind=True, queue="reports", acks_late=True, reject_on_worker_lost=True,
             soft_time_limit=1500, time_limit=1560, ignore_result=True,
             max_retries=3)
def generate_report(self, run_id: str) -> None:
    run = claim_run(run_id)
    if run is None:
        return                                                  # already claimed/finished/cancelled
    try:
        build_and_store(run)
    except SoftTimeLimitExceeded:
        fail_run(run, code="timeout")
    except CancelledByUser:
        finish(run, ReportRun.Status.CANCELLED)
    except TRANSIENT as exc:
        if run.attempt >= settings.REPORTS_MAX_ATTEMPTS:
            fail_run(run, code="transient_exhausted")
            return
        release_for_retry(run)                                  # RUNNING → PENDING
        raise self.retry(exc=exc, countdown=backoff_with_jitter(run.attempt))
    except Exception:
        log.exception("report.failed", extra={"run_id": run_id})
        fail_run(run, code="internal_error")                    # permanent: no retry
```

### Atomic claim [Django]

```python
def claim_run(run_id: str) -> ReportRun | None:
    now = timezone.now()
    claimed = (ReportRun.objects
               .filter(pk=run_id, status=ReportRun.Status.PENDING, cancel_requested=False)
               .update(status=ReportRun.Status.RUNNING, started_at=now, heartbeat_at=now,
                       attempt=F("attempt") + 1))
    if not claimed:
        return None
    return ReportRun.objects.select_related("tenant", "requested_by").get(pk=run_id)
```

Exactly one worker gets `claimed == 1`. A redelivered message after success finds `SUCCEEDED`
and exits. `attempt` counts claims, including those after crashes, so it caps redelivery loops.

### Build and store idempotently

```python
def build_and_store(run: ReportRun) -> None:
    spec = get_spec(run.report_key)
    if run.requested_by and not run.requested_by.has_perm(spec.permission):
        raise PermissionRevoked                                 # permanent → failed
    storage_name = f"reports/{run.tenant_id}/{run.pk}.{run.format}"   # deterministic per run
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as tmp:
        with snapshot(using="reports"):                         # see data-acquisition reference
            rows = spec.selector(tenant=run.tenant, user=run.requested_by, params=run.params)
            stats = render(spec, run.format, rows, tmp,
                           on_progress=lambda n: heartbeat(run, rows_done=n))
        tmp.seek(0)
        digest = sha256_of(tmp); tmp.seek(0)
        if report_storage.exists(storage_name):
            report_storage.delete(storage_name)                 # leftover from crashed attempt
        report_storage.save(storage_name, File(tmp))
    with transaction.atomic():
        updated = (ReportRun.objects.filter(pk=run.pk, status=ReportRun.Status.RUNNING)
                   .update(status=ReportRun.Status.SUCCEEDED, finished_at=timezone.now(),
                           row_count=stats.rows, data_as_of=stats.as_of))
        if not updated:                                         # reaped or cancelled meanwhile
            report_storage.delete(storage_name)
            return
        ReportArtifact.objects.update_or_create(run_id=run.pk, defaults=dict(
            storage_name=storage_name, filename=report_filename_for(run), content_type=CT[run.format],
            size_bytes=stats.size, sha256=digest, expires_at=timezone.now() + spec.retention))
        transaction.on_commit(lambda: notify_report_ready.delay(str(run.pk)))
```

- `Storage.save()` may rename on collision (`get_available_name`) — delete first or use a storage
  configured with `file_overwrite`. Verify the stored name equals `storage_name`. [VERIFY: overwrite behaviour of the configured storage backend]
- File upload happens **outside** the DB transaction; the transaction only flips status and
  writes metadata. No network I/O inside `atomic()`.
- The selector runs inside a read snapshot so multi-query reports are consistent
  ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)).

### Heartbeat, progress and cooperative cancellation

```python
def heartbeat(run: ReportRun, *, rows_done: int) -> None:
    if rows_done % 5_000:
        return
    state = (ReportRun.objects.filter(pk=run.pk)
             .values_list("cancel_requested", flat=True).first())
    ReportRun.objects.filter(pk=run.pk, status=ReportRun.Status.RUNNING).update(
        heartbeat_at=timezone.now(), row_count=rows_done)
    if state:
        raise CancelledByUser
```

- Progress is `row_count` vs an optional estimated total (`COUNT(*)` before rendering is a cost
  decision — skip it for huge exports, show rows processed instead).
- Cancellation: the API sets `cancel_requested=True`; a `PENDING` run is moved directly to
  `CANCELLED` with a conditional update; a `RUNNING` run stops at the next heartbeat. Rendering
  phases that cannot be interrupted (e.g. a single WeasyPrint `write_pdf()` call) only observe
  cancellation before and after.

### Reaper for crashed workers (beat every minute)

```python
@shared_task(queue="reports-maintenance", ignore_result=True)
def reap_stale_runs() -> None:
    cutoff = timezone.now() - timedelta(minutes=10)            # > heartbeat interval × safety margin
    stale = ReportRun.objects.filter(status=ReportRun.Status.RUNNING, heartbeat_at__lt=cutoff)
    for run_id, attempt in stale.values_list("pk", "attempt"):
        if attempt >= settings.REPORTS_MAX_ATTEMPTS:
            ReportRun.objects.filter(pk=run_id, status=ReportRun.Status.RUNNING).update(
                status=ReportRun.Status.FAILED, error_code="worker_lost", finished_at=timezone.now())
        elif ReportRun.objects.filter(pk=run_id, status=ReportRun.Status.RUNNING,
                                      heartbeat_at__lt=cutoff).update(status=ReportRun.Status.PENDING):
            generate_report.delay(str(run_id))
```

The heartbeat interval must be shorter than the slowest uninterruptible phase plus margin, or the
reaper re-queues healthy runs (the claim then makes the duplicate a no-op, but work is wasted).
Also re-queue `PENDING` runs older than a threshold — the enqueue may have been lost if the
process died between commit and `on_commit`.

### Expiry and cleanup task

```python
@shared_task(queue="reports-maintenance", ignore_result=True)
def expire_report_artifacts(batch: int = 500) -> None:
    now = timezone.now()
    due = (ReportArtifact.objects.filter(expires_at__lte=now, deleted_at__isnull=True)
           .select_related("run").order_by("expires_at")[:batch])
    for artifact in due:
        report_storage.delete(artifact.storage_name)           # idempotent if already gone
        ReportArtifact.objects.filter(pk=artifact.pk, deleted_at__isnull=True).update(deleted_at=now)
        ReportRun.objects.filter(pk=artifact.run_id, status=ReportRun.Status.SUCCEEDED).update(
            status=ReportRun.Status.EXPIRED)
```

Delete the object **before** marking it deleted, so a crash leaves a row that is retried rather
than an orphaned file. Also sweep orphaned storage objects without a `ReportArtifact` row
(prefix listing, older than a grace period). Retention *policy* is owned by
[reporting-security-and-compliance.md](reporting-security-and-compliance.md); monitoring of the
cleanup by [reporting-observability-and-operations.md](reporting-observability-and-operations.md).

### Completion notification

`notify_report_ready` runs after commit, reads the run, re-checks it is `SUCCEEDED`, and sends an
in-app notification and/or email with a link to the run page — never the file and never a
long-lived presigned URL. Notification idempotency (one notification per run) uses a unique key
such as `f"report-ready:{run_id}"`; provider mechanics in
[django-integrations — email-sms-and-notification-integrations](../../django-integrations/references/email-sms-and-notification-integrations.md).
Failures notify too (`error_code` mapped to a user-safe message).

## 6. Production use cases

| Scenario | Design notes |
|---|---|
| 300k-row orders XLSX | XlsxWriter `constant_memory` or openpyxl write-only into a spooled temp file; `reports` queue, concurrency sized by memory |
| Monthly payroll register PDF per department | One run per department; parent run tracks children; fails as a whole if any child fails permanently |
| Ledger export during month-end close | REPEATABLE READ snapshot on replica alias; `data_as_of` recorded |
| Same dashboard export clicked by 40 users | Dedupe constraint collapses identical active requests; reuse of fresh succeeded run |
| Worker node evicted mid-render | `acks_late` redelivery + claim; or reaper re-queues after heartbeat staleness |
| User cancels a 20-minute export | `cancel_requested` observed at next chunk; temp file discarded |

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Request latency independent of report size | Users wait and poll; needs notification UX |
| Isolated resources, retries, stored artifacts | Broker, workers, reaper, cleanup to operate |
| Dedupe and reuse save database load | Status model and races must be engineered carefully |
| Auditable, reproducible outputs | Storage cost and retention obligations |

## 8. Security considerations

- Re-check **report-level permission inside the task**; the requester may have lost access
  between request and execution. Scheduled runs re-check per recipient at delivery time.
- The selector receives `tenant` and `user` from the `ReportRun` row, never from the message.
- Message payloads contain only the run ID — no PII, no parameters, no credentials in the broker
  or in Celery's result backend.
- Temp files go to a private temp directory on an encrypted volume; `SpooledTemporaryFile`
  deletes on close. Never write artifacts to a shared `/tmp` path derived from user input.
- `error_message` shown to users is a mapped, safe string; stack traces only in logs.
- Storage path is deterministic and server-generated (`reports/<tenant>/<run>.<ext>`).

## 9. Performance considerations

- Separate `reports` queue; `worker_prefetch_multiplier=1`; `--max-tasks-per-child` or
  `--max-memory-per-child` to recycle workers that fragment memory on large renders.
  [VERIFY: `worker_max_memory_per_child` semantics (KiB, checked after task) in Celery 5.x]
- Concurrency of the reports pool = available memory ÷ peak memory per largest report, not CPU count.
- Per-tenant active-run caps prevent one tenant from monopolising the queue.
- Long-running read transactions on the primary hold back vacuum; prefer a replica or bounded
  snapshot duration.
- Heartbeat and progress writes are rate-limited (every N rows / seconds).
- Throughput and sizing depth: [report-generation-performance.md](report-generation-performance.md),
  [django-performance — background-task-performance](../../django-performance/references/background-task-performance.md).

## 10. Common mistakes and anti-patterns

- `generate_report.delay(run.pk)` inside the `atomic()` block (worker reads a row that isn't committed).
- Using Celery task state as the user-visible status.
- Passing the queryset, the user object or parameters in the message.
- `autoretry_for=(Exception,)` — retries permission errors, bad parameters and bugs.
- No attempt cap with `reject_on_worker_lost=True` — an OOM report loops forever.
- `time_limit` longer than the broker visibility timeout.
- `revoke(terminate=True)` as the cancellation feature.
- Writing the artifact with a random name per attempt — orphaned files accumulate.
- Marking `SUCCEEDED` before the upload completes, or uploading inside a DB transaction.
- Unconditional `run.status = ...; run.save()` that overwrites a concurrent cancel or reap.
- One task generating every tenant's monthly report sequentially (no isolation, no partial retry).

## 11. Troubleshooting and validation

| Symptom | Likely cause | Diagnose |
|---|---|---|
| Runs stuck `PENDING` | Enqueue lost (no `on_commit` dispatch), worker not consuming `reports` queue | Broker queue length; worker `-Q`; age of oldest pending |
| Runs stuck `RUNNING` | Hard kill without `acks_late`, reaper not scheduled | `heartbeat_at` age; worker OOM logs |
| Same report generated twice | Missing claim or dedupe constraint; visibility timeout < runtime | Two `started_at` for one run; logs by `run_id` |
| Endless retries | Permanent error classified transient; no attempt cap | `attempt` column; error codes |
| Succeeded but download 404 | Storage renamed file; wrong storage backend in worker settings | Compare `storage_name` to storage listing |
| Worker memory climbs over hours | Renderer leaks / fragmentation | Enable per-child recycling; profile ([report-generation-performance.md](report-generation-performance.md)) |

Validation checklist:

- [ ] Task takes only `run_id`; enqueued on commit.
- [ ] Claim is a conditional UPDATE; all status transitions are conditional.
- [ ] Partial unique constraint `uniq_active_report_run` exists with a migration.
- [ ] Transient vs permanent errors classified; attempts capped; backoff with jitter.
- [ ] `soft_time_limit < time_limit < visibility_timeout`.
- [ ] Reaper and cleanup tasks scheduled and monitored.
- [ ] Permission re-check inside the task; tested with revoked permission.
- [ ] Tests call the task function directly, simulate crash (stale heartbeat), duplicate delivery,
      cancellation and retry ([report-generation-testing.md](report-generation-testing.md)).

## 12. Related references and official documentation

- [reporting-architecture-and-design.md](reporting-architecture-and-design.md) — models, state machine ownership.
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — snapshots, iteration.
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) — status and download endpoints.
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) — schedule-triggered runs.
- [report-generation-performance.md](report-generation-performance.md) — worker sizing, artifact reuse.
- [reporting-observability-and-operations.md](reporting-observability-and-operations.md) — metrics, alerts, runbooks.
- [django-data-processing — django-background-processing](../../django-data-processing/references/django-background-processing.md)
- [django-data-processing — processing-reliability-and-recovery](../../django-data-processing/references/processing-reliability-and-recovery.md)
- [django-integrations — celery-and-background-integrations](../../django-integrations/references/celery-and-background-integrations.md)
- [django-orm — transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)
- [django-testing — integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md)
- <https://docs.celeryq.dev/en/stable/userguide/tasks.html>
- <https://docs.celeryq.dev/en/stable/userguide/configuration.html>
- <https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html>
- <https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit>
- <https://docs.djangoproject.com/en/5.2/ref/models/constraints/#uniqueconstraint>
- <https://docs.python.org/3/library/tempfile.html>
