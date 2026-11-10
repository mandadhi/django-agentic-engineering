# Processing Reliability and Recovery

> **Scope:** How data-processing jobs fail safely and recover: failure isolation, error taxonomy,
> poison records, data-level dead-letter tables, idempotent and retry-safe execution, checkpoint
> resume, compensation, duplicate prevention, recovery workflows with step-by-step procedures,
> reprocessing and auditability. **Not here:** Celery task options, time limits and job state
> machine ([django-background-processing.md](django-background-processing.md)); broker/worker
> infrastructure and HTTP client resilience ([celery-and-background-integrations.md](../../django-integrations/references/celery-and-background-integrations.md),
> [integration-reliability-and-resilience.md](../../django-integrations/references/integration-reliability-and-resilience.md));
> delivery-semantics theory ([data-processing-fundamentals.md](data-processing-fundamentals.md));
> pipeline state models ([etl-and-elt-pipelines.md](etl-and-elt-pipelines.md)).

## 1. Purpose and scope

Data jobs fail in the middle. The question is never "will it fail" but "after it fails, can we
(a) know exactly what was done, (b) finish the rest without doing anything twice, and (c) undo what
was wrong?" This reference gives the design rules and concrete recovery procedures that make the
answer yes for imports, ETL loads, syncs and exports in a Django/PostgreSQL/Celery stack.

Three properties carry all of it:

1. **Isolation** — a failure is contained to the smallest unit (record, batch, partition, tenant).
2. **Idempotency** — re-executing any unit yields the same end state.
3. **Durable progress** — progress is recorded in the database in the same transaction as the work.

## 2. Core concepts

| Concept | Meaning in data processing |
|---|---|
| Failure isolation unit | Record → batch (chunk) → partition (day/file) → tenant → job. Choose the smallest unit whose partial success is *business-valid*. |
| Partial processing | Some units succeeded, some failed; job ends `PARTIAL` (see `ProcessingJob.Status`). |
| Idempotent execution | Natural-key UPSERT, "insert if absent", deterministic output paths; a replay changes nothing. |
| Retry safety | A retry may happen after the side effect committed but before the ack. Safe only if idempotent. |
| Checkpoint | Durable marker of the last fully committed unit (`last_pk`, page cursor, file byte offset, batch number). |
| Poison record | An input that fails deterministically and would crash/stall every retry. |
| Dead-letter (data-level) | A table of failed records/batches with payload reference, error class and replay status. |
| Broker DLQ | Message-level dead-letter (RabbitMQ DLX); holds *task messages*, not business records. |
| Compensation | A forward operation that semantically undoes an earlier committed effect (reversal entry, soft-delete by batch id). |
| Effectively-once | At-least-once delivery + idempotent effects + dedupe = the business sees exactly one effect. True exactly-once across a broker and a DB is not available. |

### Error taxonomy

| Class | Examples | Retry? | Isolation | Action |
|---|---|---|---|---|
| Transient infra | `OperationalError` connection reset, deadlock (`40P01`), serialization failure (`40001`), S3 5xx, timeouts | Yes, backoff + jitter, bounded | Batch | Retry batch; escalate after N |
| Rate limit | HTTP 429, `Retry-After` | Yes, honour header | Job/source | Pause source; reschedule, don't burn retries |
| Permanent data | Invalid date, unknown FK code, failed validation | No | Record | `RejectedRecord` / DLQ row; continue |
| Contract / schema | Missing column, new enum value, file layout changed | No | File/partition | Stop partition, alert owner; mass rejection is a contract error, not data errors |
| Bug | `TypeError`, `KeyError` in our code | No (would loop) | Batch → job | Fail fast, DLQ batch, alert engineers; fix + reprocess |
| Ambiguous | Timeout after a remote write; worker died after commit? | Only via idempotent check | Record/batch | Query state ("did it land?") before redoing |
| Resource | OOM kill, disk full, `SoftTimeLimitExceeded` | Yes, after change (smaller chunk) | Batch | Resume from checkpoint with reduced batch size |

## 3. Internal mechanics

### 3.1 Why "retry the task" is not enough

With Celery `acks_late=True` a message is acknowledged after the task returns. Failure windows:

```text
read batch ─▶ BEGIN ─▶ write rows ─▶ write checkpoint ─▶ COMMIT ─▶ ack message
     A                       B                              C            D
```

- Crash at A/B: transaction rolls back; redelivery redoes the batch. Safe.
- Crash at C (after COMMIT, before ack): redelivery redoes a committed batch → duplicates unless
  writes are idempotent **or** the checkpoint (committed atomically with the rows) is read first.
- Side effects outside the DB (email, upload, API POST) are not covered by the transaction: they
  can happen twice or be lost. Use an outbox row committed with the data and a separate idempotent
  sender, or idempotency keys on the remote API.

### 3.2 Checkpoints committed with the data

The checkpoint must be written **in the same transaction** as the batch it describes. Then
"checkpoint says batch 41 done" ⇔ "batch 41's rows are committed". A checkpoint in Redis or the
Celery result backend can disagree with the DB after a crash.

### 3.3 Deadlocks and serialization failures

PostgreSQL aborts one transaction in a deadlock with SQLSTATE `40P01`; under `SERIALIZABLE` or
`REPEATABLE READ`, conflicts raise `40001`. Both are safe to retry *the whole transaction*. In
Django they surface as `django.db.OperationalError` [Django] wrapping the driver error; inspect
`exc.__cause__` for `sqlstate`/`pgcode` (VERIFY: attribute name for psycopg 3 `.sqlstate` vs
psycopg2 `.pgcode`). Deadlocks in bulk UPSERTs usually come from two workers writing overlapping
keys in different orders — sort rows by key inside each batch and partition work so concurrent
workers do not share keys.

### 3.4 Broker DLQ vs data DLQ

| | Broker DLQ (RabbitMQ DLX) | Data-level DLQ table |
|---|---|---|
| Unit | Task message | Record or batch |
| Contains | Serialized task args | Payload ref, error class, traceback hash, attempt count |
| Queryable / per-tenant | Poor | Yes (SQL, admin, UI) |
| Replay | Re-publish message | Replay job selects rows by filter |
| Needed for | Malformed/unprocessable messages | Business data that failed processing |

Celery has no built-in DLQ; a RabbitMQ dead-letter exchange plus `Reject(requeue=False)` covers
messages (VERIFY: queue arguments in your broker setup — see django-integrations). For data, use
the table below; it is the one operators will actually work from.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Records independent (customer import) | Record-level isolation: reject bad rows, commit good | Business requires all-or-nothing (bank file) |
| Records interdependent (journal lines must balance) | Group-level isolation (whole journal) | Isolating single lines (unbalanced ledger) |
| Payroll / payment batches | Whole-file atomicity via staging → validate → promote | Partial promotion of a payment file |
| Long import (hours) | Chunk commits + checkpoint, resumable | One giant transaction (locks, WAL bloat, total loss on failure) |
| Multi-tenant nightly job | One task per tenant (tenant isolation) | One loop over tenants where tenant 3 failing stops 4..N |
| Remote write with unknown outcome | Idempotency key + status query | Blind retry of non-idempotent POST |
| Bad data loaded, already consumed downstream | Compensation (reversal / superseding version) | Silent DELETE (breaks audit, downstream mismatch) |
| Bad data loaded, not yet consumed | Delete by `batch_id` + reload | Manual SQL edits without audit |
| Poison record | DLQ after first deterministic failure | Retrying N times (wastes time, delays batch) |

### Transactions per unit

| Strategy | Recovery | Cost |
|---|---|---|
| One transaction per job | Clean rollback; restart from zero | Long locks, huge rollback, no progress on retry |
| Transaction per batch + checkpoint | Resume from last batch | Partial visibility during run (use staging to hide) |
| Savepoint per record within a batch | Isolate bad rows without losing the batch | Savepoints cost; thousands per batch is slow |
| Staging + atomic promote | Production never sees partial data | Extra copy, more disk |

Savepoint-per-record: prefer validating before writing so rows rarely fail at the DB; fall back to
per-row savepoints only for the batch that failed ("bisect on failure").

## 5. Django/Python implementation patterns

### 5.1 Data-level dead-letter table

```python
from django.db import models

class ProcessingFailure(models.Model):          # data-level DLQ
    class Kind(models.TextChoices):
        TRANSIENT = "TRANSIENT"; RATE_LIMIT = "RATE_LIMIT"; DATA = "DATA"
        CONTRACT = "CONTRACT"; BUG = "BUG"; AMBIGUOUS = "AMBIGUOUS"; RESOURCE = "RESOURCE"

    class State(models.TextChoices):
        OPEN = "OPEN"; REPLAYED = "REPLAYED"; DISCARDED = "DISCARDED"

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    job_id = models.CharField(max_length=64)              # ProcessingJob / PipelineRun pk as text
    unit = models.CharField(max_length=20)                # "record" | "batch" | "partition"
    unit_key = models.CharField(max_length=255)           # row number, batch no., business key
    payload_ref = models.CharField(max_length=500)        # storage key, or raw_record id — not the PII payload
    kind = models.CharField(max_length=12, choices=Kind.choices)
    error_type = models.CharField(max_length=200)
    error_message = models.TextField()                    # redacted
    fingerprint = models.CharField(max_length=64)         # sha256(error_type + normalised message + code version)
    attempts = models.PositiveSmallIntegerField(default=1)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    replayed_by_job = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job_id", "unit", "unit_key"],
                                               name="uniq_failure_per_unit")]
```

Validation failures go to `RejectedRecord` ([data-validation-and-cleansing.md](data-validation-and-cleansing.md));
`ProcessingFailure` is for failures of *processing* (bugs, contract breaks, exhausted transient retries).

### 5.2 Error classification

```python
from django.db import OperationalError, IntegrityError, DataError   # [Django]

TRANSIENT_SQLSTATES = {"40P01", "40001", "57014", "08006", "08003"}  # deadlock, serialization, cancel, conn

class ContractError(Exception): ...          # raise from parsers on layout/schema breaks
class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after

def classify(exc: BaseException) -> str:
    match exc:
        case RateLimited(): return "RATE_LIMIT"
        case ContractError(): return "CONTRACT"
        case OperationalError():
            code = getattr(exc.__cause__, "sqlstate", None) or getattr(exc.__cause__, "pgcode", None)
            return "TRANSIENT" if code in TRANSIENT_SQLSTATES else "AMBIGUOUS"
        case IntegrityError() | DataError(): return "DATA"
        case MemoryError(): return "RESOURCE"
        case TimeoutError() | ConnectionError(): return "TRANSIENT"
        case _: return "BUG"
```

`MemoryError` is only raised for allocation failures Python sees; a kernel OOM kill (exit 137)
raises nothing — the job simply stops, which is why heartbeats and checkpoints matter.
`IntegrityError` may also be a bug (missing dedupe in batch) — fingerprint and alert if one
fingerprint dominates. Map HTTP client errors (5xx/timeout → TRANSIENT, 4xx → DATA/CONTRACT) in the
API client layer (django-integrations).

### 5.3 Checkpointed, resumable batch loop

```python
from itertools import batched                 # [stdlib] Python >= 3.12
from django.db import models, transaction

def run_import(job: "ProcessingJob", rows_from, batch_size: int = 1000) -> None:
    start = job.checkpoint.get("next_row", 0)            # resume point
    for batch in batched(rows_from(start), batch_size):  # rows_from yields (row_no, row)
        first_no = batch[0][0]
        try:
            with transaction.atomic():
                good, bad = validate_and_map(batch)
                upsert_rows(good)                         # idempotent UPSERT on natural key
                record_rejects(job, bad)
                job.checkpoint = {"next_row": batch[-1][0] + 1}
                job.processed = models.F("processed") + len(good)
                job.save(update_fields=["checkpoint", "processed"])
        except Exception as exc:
            kind = classify(exc)
            if kind in {"TRANSIENT", "RATE_LIMIT", "RESOURCE"}:
                raise                                     # task-level retry resumes from checkpoint
            isolate_batch(job, batch, exc, kind)          # bisect → DLQ only failing rows
            job.checkpoint = {"next_row": batch[-1][0] + 1}
            job.save(update_fields=["checkpoint"])
```

`job.processed = F(...)` leaves an expression on the instance — call `refresh_from_db` before
reading it. Retries/backoff config for the surrounding task →
[django-background-processing.md](django-background-processing.md).

### 5.4 Bisecting a failed batch to find poison rows

```python
def isolate_batch(job, batch: tuple, exc: BaseException, kind: str) -> None:
    if len(batch) == 1:
        row_no, row = batch[0]
        ProcessingFailure.objects.update_or_create(
            job_id=job.id, unit="record", unit_key=str(row_no),
            defaults={"tenant_id": job.tenant_id, "kind": kind,
                      "payload_ref": f"{job.source_key}#row={row_no}",
                      "error_type": type(exc).__name__, "error_message": redact(str(exc)),
                      "fingerprint": fingerprint(exc)})
        return
    mid = len(batch) // 2
    for half in (batch[:mid], batch[mid:]):
        try:
            with transaction.atomic():
                good, bad = validate_and_map(half)
                upsert_rows(good)
                record_rejects(job, bad)
        except Exception as inner:
            isolate_batch(job, half, inner, classify(inner))
```

Bisection costs O(k log n) extra transactions for k poison rows. If more than ~5 % of a batch
fails, stop bisecting and treat it as a CONTRACT or BUG failure for the partition.

### 5.5 Duplicate prevention for task delivery

```python
from django.db import IntegrityError, transaction

class ProcessedMessage(models.Model):        # dedupe ledger
    key = models.CharField(max_length=200, unique=True)   # e.g. f"{job_id}:{batch_no}"
    created_at = models.DateTimeField(auto_now_add=True)

def process_once(key: str, work) -> bool:
    try:
        with transaction.atomic():
            ProcessedMessage.objects.create(key=key)       # fails on duplicate
            work()                                         # same transaction as the marker
        return True
    except IntegrityError:
        return False                                       # already done: ack and move on
```

This holds only for effects inside the same database transaction. Purge old keys on a schedule.

### 5.6 Compensation by batch lineage

Every loaded row carries `ingestion_batch_id` and `mapping_version`. Compensation then becomes a
set-based operation:

```python
from django.utils import timezone   # [Django]

def compensate_batch(batch_id: int, reason: str, actor_id: int) -> int:
    with transaction.atomic():
        n = (Employee.objects.filter(source_batch_id=batch_id, superseded_at__isnull=True)
             .update(superseded_at=timezone.now(), superseded_reason=reason))
        AuditEvent.objects.create(action="compensate_batch", object_ref=f"batch:{batch_id}",
                                  actor_id=actor_id, detail={"rows": n, "reason": reason})
    return n
```

For financial data never update/delete posted entries — post reversing entries referencing the
originals, then post corrected entries.

### 5.7 Generic recovery workflow

```text
1. Detect      alert / reconciliation / user report → open incident, record job & run ids
2. Contain     pause schedule or tenant, disable bad mapping/code path, block downstream consumers
3. Scope       query lineage: affected batch ids, tenants, periods, mapping/code versions
4. Preserve    snapshot affected rows + DLQ state (evidence before mutation)
5. Fix cause   deploy fix / restore config; classify root cause in the taxonomy
6. Recover     resume from checkpoint | replay DLQ | re-transform from raw | compensate
7. Verify      counts, reconciliation BALANCED, downstream consumers re-synced
8. Record      audit event: actor, reason, ids, before/after counts; post-incident review
```

Effectively-once is achieved per *effect*: DB writes via UPSERT/dedupe ledger; files via
deterministic keys + atomic finalise; remote calls via idempotency keys. Anything without one of
these (an email, a webhook to a partner without idempotency support) is at-least-once — design the
consumer or the business process to tolerate duplicates, or accept occasional loss (at-most-once)
explicitly.

## 6. Production use cases

Each scenario: detection → containment → recovery → verification.

### 6.1 Worker OOM-killed mid-import (180 k-row employee CSV)
1. Detect: job `RUNNING` with no heartbeat for > 10 min; worker log shows `SIGKILL` / exit 137;
   with `acks_late` + `task_reject_on_worker_lost` the message is redelivered (VERIFY: your
   broker/worker settings).
2. Contain: redelivered task reads `job.checkpoint.next_row`, not row 0.
3. Recover: if OOM was from a non-streaming reader (e.g. `read_excel`), lower `batch_size` and
   switch to a streaming reader before resuming ([large-dataset-processing.md](large-dataset-processing.md)).
4. Verify: `processed + rejected == source_row_count`; run count reconciliation.

### 6.2 Database deadlock in a batch UPSERT (inventory sync, 4 parallel workers)
1. Detect: `OperationalError` SQLSTATE `40P01`, classified TRANSIENT.
2. Contain: transaction rolled back; checkpoint unchanged.
3. Recover: task retries with jitter. Permanent fix: sort rows by `(warehouse_id, sku)` within each
   batch and partition work by warehouse so workers never touch the same keys.
4. Verify: deadlock count metric back to zero; WMS vs ERP aggregate reconciliation.

### 6.3 Source API outage mid-pagination (CRM contacts, page 312 of 900)
1. Detect: repeated 503/timeouts; retries exhausted.
2. Contain: page cursor checkpoint committed after each page's rows; job → `FAILED` with kind
   TRANSIENT, watermark **not** advanced (incremental window stays open).
3. Recover: once the API is healthy, re-enqueue the same job id; it resumes at the saved cursor. If
   the cursor expired, restart the window from the last watermark — UPSERTs make repeats harmless.
4. Verify: compare API-reported total count to rows landed for the window.

### 6.4 Bad mapping version loaded wrong data for 3 days (payroll: `gross` mapped into `net`)
1. Detect: reconciliation payroll vs bank file UNBALANCED; or HR reports wrong figures.
2. Contain: deactivate mapping v14, reactivate v13 (`MappingDefinition`); pause scheduled loads;
   block downstream exports/payments for affected periods.
3. Scope: `SELECT DISTINCT source_batch_id FROM payroll_line WHERE mapping_version = 14` → affected
   batches and tenants. Snapshot affected rows to an audit table.
4. Recover: if not yet paid — supersede rows from those batches and re-transform from **raw
   landed records** (`RawRecord`) with v13 (no re-fetch needed). If already paid — compensation:
   reversal entries + corrected entries; finance decides on payment adjustments.
5. Verify: re-run reconciliation for the 3 days; diff old vs new figures; record incident with
   batch ids, mapping versions and actor in the audit log.
   Raw-landing makes this recovery possible; without it you depend on the source's history.

### 6.5 Duplicate task delivery (visibility timeout shorter than task runtime)
1. Detect: two workers processing the same job id; duplicate-key `IntegrityError`s or doubled
   counts.
2. Contain: job-level lock — claim the job with `select_for_update(skip_locked=True)` and a
   `RUNNING` transition guarded by `filter(status=QUEUED).update(...) == 1`.
3. Recover: UPSERT semantics mean data is correct; fix counters by recomputing from the table.
4. Prevent: raise Redis/SQS `visibility_timeout` above the longest task; keep tasks short.

### 6.6 Poison row crashes the parser (embedded NUL / 10 MB field in an attendance CSV)
1. Detect: same batch fails with the same fingerprint on every retry (classified BUG/DATA).
2. Contain: first deterministic failure → bisect → single row to `ProcessingFailure`; rest commit.
   For parser-level crashes (no row boundary), record the byte offset and skip to the next line
   ending, or fail the file as CONTRACT if the structure cannot be recovered.
3. Recover: fix parser (e.g. `csv.field_size_limit`), then replay `ProcessingFailure` rows.
4. Verify: replayed rows `REPLAYED`; counts reconcile.

### 6.7 Partial export upload (multipart upload to object storage interrupted)
1. Detect: export job FAILED during upload; object missing or incomplete multipart upload pending.
2. Contain: exports are written to a temporary key (`exports/tmp/<job>/…`) and only
   copied/renamed to the final key after checksum verification; download links are issued only for
   finalised keys.
3. Recover: abort the incomplete multipart upload (lifecycle rule as backstop), regenerate from the
   same snapshot (deterministic query with `as_of`), re-upload.
4. Verify: stored object size/checksum equals the locally computed digest; `ExportJob` → SUCCEEDED.

## 7. Advantages and disadvantages

| Pattern | Advantages | Disadvantages |
|---|---|---|
| Per-record isolation | Max throughput of good data | Partial datasets may be business-invalid |
| Checkpoint per batch | Cheap resume, bounded rework | Partial visibility unless staged |
| Data DLQ table | Queryable, replayable, per-tenant | Must be owned/triaged or it grows forever |
| Dedupe ledger | Simple effectively-once in one DB | Extra write per unit; purge needed |
| Compensation | Preserves audit history | More complex than delete; downstream must understand reversals |
| Staging + promote | Production never partial | Disk, extra step, promotion latency |

## 8. Security and performance considerations

**Security**
- DLQ rows store `payload_ref`, not raw payloads with PII; error messages are redacted before
  storage (driver errors often echo the offending values) — see [data-processing-security.md](data-processing-security.md).
- Replay and compensation are privileged operations: separate permission, tenant-scoped,
  audited with actor and reason ([authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- A replay task re-scopes by `tenant_id`; never replay "all OPEN failures" across tenants in one
  transaction.

**Performance**
- Checkpoint writes add one UPDATE per batch — negligible at batch sizes ≥ 500.
- Savepoints per record are expensive; bisect only on failure.
- Retry storms: exponential backoff + jitter + cap; on rate limits pause the source, not each task.
- Deadlock avoidance (sorted keys, disjoint partitions) is cheaper than retrying deadlocks.
- Dedupe tables and DLQ need indexes and purge jobs; unbounded growth degrades inserts.
- Sizing and throughput → [data-processing-performance.md](data-processing-performance.md).

## 9. Common mistakes and anti-patterns

- Checkpoint in Redis/cache, data in PostgreSQL → disagree after crash → commit checkpoint in the
  same DB transaction.
- `autoretry_for=(Exception,)` → bugs and poison rows retried forever, blocking queues → retry only
  TRANSIENT/RATE_LIMIT classes.
- One transaction for a 2 M-row load → hour-long locks, total rollback → batch commits + staging.
- `except Exception: pass` per row → silent data loss, job reports SUCCEEDED → record every failure
  and count it.
- Advancing the watermark when a job ends `PARTIAL`/`FAILED` → permanent gap → advance only on
  success of the full window.
- Deleting bad data with ad-hoc SQL → no audit, downstream inconsistency → batch-lineage
  compensation with audit event.
- Assuming Celery gives exactly-once → duplicate side effects → idempotent writes + dedupe ledger.
- Re-fetching from source to recover from a mapping bug → source has changed since → re-transform
  from raw landed records.
- Retrying a non-idempotent remote POST after a timeout → double charges/records → idempotency
  key + status query.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Job stuck `RUNNING` forever | Worker died, no heartbeat/timeout handling | Heartbeat column + reaper that marks stale jobs FAILED and re-enqueues |
| Duplicate rows after a retry | Non-idempotent insert; checkpoint not atomic with data | UPSERT on natural key; atomic checkpoint |
| Same batch fails every retry | Poison record or bug | Fingerprint; stop retrying; bisect |
| Frequent `40P01` | Overlapping keys across workers, unsorted batches | Sort keys; partition by key range |
| Watermark gap (missing day) | Watermark advanced on partial failure | Recompute window from source; fix advance condition |
| Task runs twice concurrently | Visibility timeout < runtime | Increase timeout; job claim guard |
| DLQ grows unbounded | No triage owner/purge | Dashboard + alert + retention |
| Counts off after resume | `processed` counter double-incremented | Recompute from DB at job end |

**Validation checklist**
- [ ] Every write path is idempotent (natural-key UPSERT or dedupe ledger).
- [ ] Checkpoints committed atomically with the data they describe.
- [ ] Error classes mapped; only transient classes auto-retry; retries bounded with jitter.
- [ ] Poison-record handling tested (bisection, DLQ, replay).
- [ ] Loaded rows carry batch id + mapping version for scoped compensation.
- [ ] Recovery runbooks exist for the scenarios in §6 and have been rehearsed.
- [ ] Reconciliation runs after every recovery ([data-reconciliation-engineering.md](data-reconciliation-engineering.md)).
- Fault-injection tests → [data-processing-testing.md](data-processing-testing.md).

## 11. Related references and skills

- [django-background-processing.md](django-background-processing.md) — task retries, time limits, `ProcessingJob`.
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — `PipelineRun`, `Watermark`, pipeline-level recovery.
- [data-ingestion-engineering.md](data-ingestion-engineering.md) — `RawRecord` landing, ingestion checkpoints.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — UPSERT, staging promotion.
- [data-reconciliation-engineering.md](data-reconciliation-engineering.md) — proving recovery succeeded.
- [data-processing-observability.md](data-processing-observability.md) — alerts and runbook templates.
- [django-integrations: integration-reliability-and-resilience.md](../../django-integrations/references/integration-reliability-and-resilience.md)
- [django-integrations: celery-and-background-integrations.md](../../django-integrations/references/celery-and-background-integrations.md)
- [django-orm: transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md)
- [django-architecture: distributed-systems.md](../../django-architecture/references/distributed-systems.md)

## 12. Official documentation and external references

- Django transactions: https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Django `select_for_update`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update
- Celery tasks (retries, acks_late): https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery configuration (`task_reject_on_worker_lost`, visibility timeout): https://docs.celeryq.dev/en/stable/userguide/configuration.html
- PostgreSQL error codes: https://www.postgresql.org/docs/current/errcodes-appendix.html
- PostgreSQL explicit locking / deadlocks: https://www.postgresql.org/docs/current/explicit-locking.html
- PostgreSQL serialization failure handling: https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html
- Python `itertools.batched`: https://docs.python.org/3/library/itertools.html#itertools.batched
- AWS S3 aborting incomplete multipart uploads: https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html
