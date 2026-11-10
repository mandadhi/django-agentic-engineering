# Data processing fundamentals

> **Scope:** The conceptual foundation for every data job in a Django system: the processing
> lifecycle, processing styles (batch, stream, real-time, near-real-time), sync vs async, ETL vs
> ELT as concepts, pipelines and orchestration, lineage and provenance, data-quality dimensions,
> consistency, idempotency, delivery guarantees, and how to choose the simplest architecture that
> meets the requirement. **Not here:** ingestion mechanics ([data-ingestion-engineering.md](data-ingestion-engineering.md)),
> pipeline state and stage contracts ([etl-and-elt-pipelines.md](etl-and-elt-pipelines.md)),
> Celery task design ([django-background-processing.md](django-background-processing.md)),
> loading/UPSERT mechanics ([database-loading-and-persistence.md](database-loading-and-persistence.md)),
> failure recovery runbooks ([processing-reliability-and-recovery.md](processing-reliability-and-recovery.md)).

## 1. Purpose and scope

Use this reference before designing any import, sync, export, ETL or scheduled computation. It
answers three questions that must be settled before code is written:

1. **What lifecycle stages does this data pass through**, and which are actually needed?
2. **What processing style and execution vehicle** (request, management command, Celery, external
   orchestrator, streaming platform) is the *simplest* that satisfies latency, volume and
   operational constraints?

   Origin labels for tools named in this reference: management commands and `transaction` are
   [Django]; Celery `[third-party: celery]`; Airflow `[third-party: apache-airflow]`; Dagster
   `[third-party: dagster]`; Polars `[third-party: polars]`; PyArrow `[third-party: pyarrow]`;
   Kafka/Kinesis/Event Hubs are external platforms. None is required by default.
3. **What correctness guarantee is required** (at-least-once with idempotent effects is the
   realistic default) and how will correctness be *verified*, not just assumed from "the job
   finished without an exception"?

Everything else in this skill is an elaboration of one lifecycle stage or one guarantee defined
here.

## 2. Core concepts

### 2.1 The data processing lifecycle

| Stage | What happens | Output artefact | Owning reference |
|---|---|---|---|
| Ingest | Obtain data from a source (API, file, DB, webhook, storage drop) | Raw bytes/records + `IngestionBatch` | [data-ingestion-engineering.md](data-ingestion-engineering.md) |
| Extract | Parse format into records (CSV rows, JSON objects, Parquet batches) | Iterator of raw dicts | [file-processing-engineering.md](file-processing-engineering.md), [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md) |
| Validate | Check structure, types, ranges, business rules, references | Valid records + `RejectedRecord`s | [data-validation-and-cleansing.md](data-validation-and-cleansing.md) |
| Cleanse | Normalise whitespace, case, formats; fill/flag missing values | Clean records | [data-validation-and-cleansing.md](data-validation-and-cleansing.md) |
| Transform | Type conversion, derived fields, aggregation | Canonical records | [data-transformation-and-mapping.md](data-transformation-and-mapping.md) |
| Map | Source schema → target schema, code/enum mapping | Target-shaped records | [data-transformation-and-mapping.md](data-transformation-and-mapping.md) |
| Enrich | Join reference data, look up FKs, call enrichment services | Complete records | [data-transformation-and-mapping.md](data-transformation-and-mapping.md) |
| Persist | Staging → bulk insert/UPSERT → promotion | Rows in target tables | [database-loading-and-persistence.md](database-loading-and-persistence.md) |
| Export | Produce files/feeds for consumers | Files, streams, download links | [data-import-and-export-systems.md](data-import-and-export-systems.md) |
| Reconcile | Prove target matches source (counts, sums, hashes) | `ReconciliationRun` | [data-reconciliation-engineering.md](data-reconciliation-engineering.md) |
| Archive | Retain raw inputs and outputs per policy | Immutable objects in storage | [data-processing-security.md](data-processing-security.md) (retention) |
| Recover | Resume, replay or compensate after failure | Re-run from checkpoint | [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) |

Not every job needs every stage. A nightly "recalculate loyalty tiers" job is transform → persist
→ reconcile. A supplier price-file import uses all twelve. Name the stages you skip, and why.

### 2.2 Processing styles

| Style | Definition | Typical latency | Django vehicle |
|---|---|---|---|
| Batch | Bounded dataset processed as a unit | Minutes–hours | Management command, Celery task, cron |
| Micro-batch / near-real-time | Small bounded batches on a short interval | Seconds–minutes | Celery beat every N min, polling worker |
| Stream | Unbounded sequence processed record by record, continuously | Sub-second–seconds | Dedicated consumer process (Kafka/Kinesis/Event Hubs) |
| Real-time (request-time) | Processed inside the user's request | Milliseconds | View/serializer + service |

**Batch vs streaming:** batch has a known end, so it can count, reconcile and commit atomically
per batch; a stream never ends, so correctness is defined per window or per event, and
"reconcile" becomes a continuous comparison against a watermark. Most Django business systems
need batch or micro-batch; true streaming is warranted only when the business acts on events
within seconds at sustained high volume.

### 2.3 Other distinctions that drive design

- **In-memory vs disk-backed processing.** In-memory (a list of dicts, a full DataFrame) is fast
  and simple while data fits comfortably in RAM with headroom for worker concurrency. Disk-backed
  (streaming file reads, staging tables, temp files, Arrow/Parquet spill) bounds memory regardless
  of input size. Rule: if input size is user-controlled or grows over time, design disk-backed
  from the start. See [large-dataset-processing.md](large-dataset-processing.md).
- **Synchronous vs asynchronous execution.** Synchronous: the caller waits for the result (a
  request, a CLI command). Asynchronous: the caller receives a handle (job id) and polls or is
  notified. Note that Python `async` (asyncio) is a *concurrency model*, not the same thing as
  asynchronous *job execution*; asyncio helps only when the work is dominated by concurrent I/O
  waits ([../../django-core/references/asynchronous-django.md](../../django-core/references/asynchronous-django.md)).
- **Successful execution vs verified correctness.** "Exit code 0" or Celery `SUCCESS` means no
  exception escaped. It says nothing about whether 10,000 source rows became 10,000 target rows,
  whether totals match, or whether rows were silently dropped by `ignore_conflicts=True`. Every
  non-trivial job ends with a verification step (counts in = counts out + rejected; control
  totals; checksums) whose result is stored, not just logged.

### 2.4 ETL vs ELT (concepts)

- **ETL:** transform in application code before loading; target receives only clean, conformed
  data. Natural fit for Django services with domain rules in Python.
- **ELT:** load raw data into the database (staging/landing tables) first, transform with SQL
  inside the database. Natural fit when the database is the strongest compute engine available
  (set-based SQL on millions of rows) and raw data must be re-transformable later.
- Most Django systems are **hybrid**: land raw (ELT-style, for replay and audit), validate and map
  in Python (ETL-style), then set-based promotion in SQL. Detail and stage contracts:
  [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md).

### 2.5 Pipelines, orchestration, lineage, provenance

- **Pipeline:** an ordered graph of stages with explicit input/output contracts. A pipeline *run*
  is one execution over one input scope (a file, a time window, a tenant).
- **Orchestration:** deciding when stages run, in what order, with what dependencies, retries and
  concurrency. Options range from a Python loop over stage objects, to Celery chains, to external
  orchestrators. Choose the least machinery that gives you restartability.
- **Provenance:** where a record came from — source system, file name/hash, batch id, row number,
  extraction time. Stored *on or alongside* the record (`source_batch_id`, `source_row`).
- **Lineage:** how data flowed and was transformed — which pipeline run, which mapping version,
  which upstream datasets produced this output. Provenance is per-record origin; lineage is the
  derivation graph. Both are needed to answer "why does this payroll line say £4,212.50?"

### 2.6 Data-quality dimensions

| Dimension | Question | Example check |
|---|---|---|
| Completeness | Are required values present? | `email` not null for active customers |
| Validity | Do values conform to type/format/domain? | ISO-4217 currency code, date parses |
| Accuracy | Do values reflect reality? | Price matches supplier contract |
| Consistency | Do related values agree? | `end_date >= start_date`; totals = sum of lines |
| Uniqueness | Is each entity represented once? | One row per `(tenant, employee_number)` |
| Timeliness | Is data fresh enough for its use? | Stock feed < 15 minutes old |
| Integrity | Do references resolve? | `department_code` exists in reference table |

Detailed rules and scoring: [data-validation-and-cleansing.md](data-validation-and-cleansing.md).

### 2.7 Consistency, idempotency, fault tolerance, scalability

- **Consistency:** within one database transaction PostgreSQL gives you atomicity; across a job
  that commits per batch you have *eventual* consistency of the job's output — readers can see a
  half-loaded dataset unless you load into staging and promote atomically.
- **Idempotency:** running an operation N times has the same effect as once. Achieved with
  natural/business keys + UPSERT, idempotency keys, state checks before acting, and
  "replace partition" semantics (delete-then-insert within one transaction for a bounded scope).
- **Fault tolerance:** the job survives worker crashes, retries and redeliveries without
  corrupting data — requires idempotency plus checkpoints.
- **Scalability:** throughput grows by adding workers/partitions without changing correctness —
  requires that partitions are independent (no two workers write the same key concurrently).

## 3. Internal mechanics

### 3.1 Delivery and processing guarantees

| Guarantee | Mechanism | Failure behaviour | Where it occurs |
|---|---|---|---|
| At-most-once | Acknowledge before processing | Crash after ack → record lost | Celery default `acks_late=False` (ack on receipt) |
| At-least-once | Acknowledge after processing | Crash before ack → redelivered, processed twice | Celery `acks_late=True`, SQS, Kafka consumer committing after processing |
| Exactly-once | Not achievable end-to-end across independent systems | — | Marketing term outside a single transactional boundary |
| Effectively-once | At-least-once delivery + idempotent sink + dedupe | Duplicates delivered but have no extra effect | The practical target |

Why exactly-once is impossible across boundaries: the "process" step (write to PostgreSQL, call a
payment API) and the "acknowledge" step (broker ack, Kafka offset commit) live in different
systems. There is always a window where one has happened and the other has not. Crashing in that
window forces a choice: re-deliver (duplicate risk) or not (loss risk). Kafka transactions give
exactly-once *within Kafka* (read-process-write between topics); once a side effect leaves that
system, you are back to effectively-once (VERIFY: current Kafka EOS scope for the client library
in use).

### 3.2 Effectively-once recipe

```text
1. Every unit of work has a stable identity:  (source, natural_key)  or  idempotency_key
2. The sink enforces uniqueness:               UNIQUE constraint / ON CONFLICT
3. The effect and the "done" marker commit together in ONE database transaction:
       BEGIN;
         INSERT ... ON CONFLICT (tenant_id, external_id) DO UPDATE ...;
         UPDATE checkpoint SET last_key = ... WHERE job_id = ...;
       COMMIT;
4. Acknowledge the message/advance the external cursor only AFTER commit.
5. External side effects (emails, payments) carry an idempotency key the receiver honours,
   or are recorded in an outbox table inside step 3 and dispatched separately.
```

The crucial mechanic is step 3: storing the checkpoint/dedupe marker in the *same* database
transaction as the data change. Then a redelivery either sees the marker (skip) or the whole
transaction rolled back (redo safely).

### 3.3 What "successful" actually guarantees

```text
Celery task state SUCCESS     → function returned without raising
Management command exit 0     → handle() returned without raising
transaction committed         → these rows are durable
reconciliation PASSED         → target matches source within tolerance   ← correctness
```

Only the last line is a correctness statement. Design jobs so that the terminal status
(`SUCCEEDED` / `PARTIAL` / `FAILED` on `ProcessingJob`) is set from verification results, not
from "no exception".

### 3.4 How each architecture fails

| Vehicle | Failure mode | Consequence |
|---|---|---|
| Request-time | Gateway timeout (often 30–60 s), client disconnect | Partial work if not atomic; user retries → duplicates |
| Management command | Process killed on deploy/OOM; no retry | Must be restartable from checkpoint; cron may overlap |
| Celery task | Redelivery after visibility timeout; worker lost; retries | Duplicate execution — idempotency mandatory |
| External orchestrator | Task retries at stage level | Stage must be idempotent; extra infra to operate |
| Streaming consumer | Rebalance, offset replay, poison message blocks partition | Dedupe store, DLQ topic, ordering per key only |

## 4. Engineering decisions and trade-offs

### 4.1 Architecture selection (prefer the simplest)

Answer in order; stop at the first architecture that satisfies every constraint.

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| < ~1–2 s of work, user needs the result now (validate one form, small JSON post) | Request-time service call inside `transaction.atomic()` | Work depends on external I/O with unbounded latency; payload size is user-controlled |
| Operator-run or cron-run job, single tenant/dataset, minutes–hours, no user waiting | Management command + DB checkpoint + lock | Needs automatic retry, fan-out, or is triggered by user actions → Celery |
| User-triggered import/export, needs progress, retries, isolation from web workers | Celery task + `ProcessingJob` row | Single job > hours with many dependent stages → pipeline runner (still on Celery) |
| Multi-stage pipeline, several sources, daily/hourly, needs stage-level restart | In-process stage runner with `PipelineRun`/`PipelineStepRun` executed by Celery or a command | Dozens of cross-team DAGs, backfills, SLAs → external orchestrator (Airflow, Dagster) |
| Periodic micro-batch (every 1–5 min) on moderate volume | Celery beat + watermark-based incremental task | Sustained > thousands of events/s or sub-second SLA → streaming |
| Continuous event feed, sub-second reaction, high volume, replay required | Streaming platform consumer (separate process) writing via idempotent UPSERT | Team cannot operate Kafka/Kinesis; requirement is actually "within 5 minutes" |
| Heavy analytical transforms over tens of millions of rows | ELT in PostgreSQL (set-based SQL) or Polars/PyArrow in a worker | Results needed in an OLAP warehouse anyway → load there instead |

### 4.2 Requirement-driven questions

| Driver | Ask | Implication |
|---|---|---|
| Business requirement | Is partial success acceptable? Who fixes rejected rows? | `PARTIAL` status + rejected-record workflow vs all-or-nothing |
| Data volume | Rows/day now and in 2 years? Max single input size? | In-memory vs streaming; batch size; partitioning |
| Latency | Freshness the business actually acts on? | Request vs micro-batch vs stream |
| Correctness | Financial/regulatory? | Reconciliation, audit trail, immutable raw landing |
| Operational complexity | Who is on call? What infra already exists? | Do not add a broker or orchestrator for one nightly job |
| Ordering | Must events apply in order per entity? | Single partition per key; sequence numbers; last-writer-wins guards |

### 4.3 Trade-offs of common choices

| Choice | Gain | Cost |
|---|---|---|
| All-or-nothing single transaction | Simple consistency, trivial rollback | Long locks, WAL bloat, one bad row fails everything, timeouts |
| Commit per batch + checkpoint | Bounded locks, resumable, progress | Readers see partial data unless staged; must be idempotent |
| Staging + atomic promotion | Readers see old or new, never half | Extra storage, promotion step, more SQL |
| At-least-once + idempotent sink | Robust against crashes | Requires stable keys and unique constraints |
| ELT (SQL transforms) | Fast set-based processing, replayable raw | Business rules split between Python and SQL |
| ETL (Python transforms) | Domain logic reused, testable | Row-by-row Python cost; needs chunking for volume |

## 5. Django/Python implementation patterns

### 5.1 Make guarantees explicit in the service signature

```python
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Verified result of a processing run — set terminal status from this, not from 'no exception'."""
    received: int
    persisted: int
    rejected: int
    skipped_duplicates: int
    control_total_source: Decimal
    control_total_target: Decimal
    warnings: list[str] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        counts_ok = self.received == self.persisted + self.rejected + self.skipped_duplicates
        return counts_ok and self.control_total_source == self.control_total_target

    @property
    def status(self) -> str:
        if not self.balanced:
            return "FAILED"           # unexplained loss is a failure, even with no exception
        return "PARTIAL" if self.rejected else "SUCCEEDED"
```

### 5.2 Idempotent effect + checkpoint in one transaction

```python
from django.db import transaction  # [Django]

from .models import Checkpoint, Payment  # illustrative domain models


def apply_payment_batch(job_id: int, batch: list[dict], last_key: str) -> int:
    """Effectively-once: effect and checkpoint commit atomically; replays are no-ops."""
    with transaction.atomic():
        cp = Checkpoint.objects.select_for_update().get(job_id=job_id)
        if cp.last_key is not None and last_key <= cp.last_key:
            return 0  # this batch was already committed by an earlier delivery
        objs = [
            Payment(tenant_id=r["tenant_id"], external_id=r["external_id"], amount=r["amount"])
            for r in batch
        ]
        Payment.objects.bulk_create(
            objs,
            update_conflicts=True,                       # (Django ≥ 4.1)
            unique_fields=["tenant", "external_id"],
            update_fields=["amount"],
        )
        cp.last_key = last_key
        cp.save(update_fields=["last_key"])
    return len(objs)
```

Dedupe `batch` by `(tenant_id, external_id)` before calling — PostgreSQL rejects the same
conflict key twice in one `INSERT … ON CONFLICT DO UPDATE`. Loading detail:
[database-loading-and-persistence.md](database-loading-and-persistence.md).

### 5.3 Lineage/provenance fields on target rows

```python
from django.db import models  # [Django]


class ProvenanceMixin(models.Model):
    source_system = models.CharField(max_length=50)
    source_batch = models.ForeignKey("ingestion.IngestionBatch", on_delete=models.PROTECT,
                                     null=True, related_name="+")
    source_row = models.PositiveIntegerField(null=True)        # line/row number in the input
    pipeline_run = models.ForeignKey("pipelines.PipelineRun", on_delete=models.SET_NULL,
                                     null=True, related_name="+")
    mapping_version = models.PositiveIntegerField(null=True)   # MappingDefinition version used
    loaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
```

Keep provenance on the row when consumers query it often; otherwise put it in a side table keyed
by target pk to avoid widening hot tables.

### 5.4 Choosing the vehicle in code: one service, many entrypoints

```python
# services.py — the only place the logic lives
def import_attendance(*, tenant_id: int, source_uri: str, job_id: int) -> RunOutcome: ...

# management/commands/import_attendance.py — operator / cron entrypoint
# tasks.py — Celery entrypoint wrapping the same service
# views.py — request entrypoint only for tiny payloads; otherwise creates ProcessingJob + enqueues
```

The service is pure Python plus ORM; entrypoints only parse arguments, acquire locks, and record
job status. This keeps the architecture decision reversible: moving from a command to Celery is
a new 15-line wrapper, not a rewrite. See [../../django-architecture/references/service-layer.md](../../django-architecture/references/service-layer.md).

## 6. Production use cases

| Scenario | Lifecycle stages used | Style / vehicle | Guarantee and verification |
|---|---|---|---|
| Monthly payroll calculation for 40k employees | transform → persist → reconcile → export | Batch; Celery job per pay group | Effectively-once per `(pay_period, employee)`; gross/net control totals must match pre-run estimate |
| Card-payment settlement file from acquirer (daily) | ingest → extract → validate → map → persist → reconcile → archive | Batch; command or Celery after file arrives | Idempotent by `(settlement_date, transaction_ref)`; sum of settled amounts = file trailer total |
| Hospital doctor rota sync from HR system | ingest → validate → map → persist | Micro-batch every 10 min, watermark on `updated_at` | Last-writer-wins guarded by source `version`; count of active doctors compared nightly |
| ERP → CRM customer sync | ingest (CDC) → map → enrich → persist | Near-real-time via webhook + nightly full reconcile | At-least-once webhooks, UPSERT on ERP id; nightly hash comparison catches missed events |
| Product catalog supplier feed (2M SKUs) | ingest → extract → validate → cleanse → transform → persist | ELT: COPY into staging, SQL promotion | Replace-partition per supplier; row count and checksum of key fields |
| Warehouse inventory movements from scanners | ingest → validate → persist | Stream (if > thousands/s) else 1-min micro-batch | Dedupe on scanner event id; stock levels reconciled with cycle counts |
| Employee attendance CSV upload by managers | ingest → extract → validate → persist | Celery task, `ProcessingJob` with progress | `PARTIAL` with downloadable rejected rows; received = persisted + rejected |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Request-time processing | No infra, immediate feedback | Timeouts, ties up web workers, no retry, abuse vector |
| Management command | Simple, debuggable, uses same code | No built-in retry/distribution; overlap risk under cron |
| Celery jobs | Retries, routing, fan-out, isolation from web | Broker to operate; at-least-once forces idempotency; visibility-timeout pitfalls |
| External orchestrator | DAG UI, backfills, SLAs, cross-team scheduling | Heavy to operate; Django code must be packaged for it |
| Streaming platform | Low latency, replay, high throughput | Highest operational cost; ordering/rebalance complexity |
| Explicit verification step | Detects silent loss, gives auditors evidence | Extra queries and code per pipeline |

## 8. Security and performance considerations

**Security**

- Every ingested byte is untrusted until validated — including data from "internal" systems,
  which may have been compromised or contain user-supplied text. Validation is a stage, not an
  option ([data-processing-security.md](data-processing-security.md)).
- Provenance and lineage records are audit evidence; make them append-only and protect them
  from tenant-crossing reads ([../../django-security-engineering/references/multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)).
- Request-time processing of user-supplied files is a denial-of-service vector (CPU, memory,
  connection hold). Move anything unbounded off the request path.
- Raw landing zones and archives contain the most sensitive, least-filtered data: restrict
  access, encrypt at rest, and apply retention.

**Performance**

- The dominant costs are usually per-row Python overhead and per-statement database round trips,
  not the "architecture". Batching (hundreds to thousands of rows per statement) beats adding
  workers. See [data-processing-performance.md](data-processing-performance.md).
- Long single transactions hold locks and prevent vacuum from reclaiming dead tuples; prefer
  commit-per-batch with checkpoints, or staging + short promotion.
- Concurrency multiplies memory: 8 workers × an in-memory 1 GB DataFrame = 8 GB. Size workers by
  peak per-task memory ([../../django-performance/references/memory-and-resource-management.md](../../django-performance/references/memory-and-resource-management.md)).
- Streaming platforms add latency budget for consumer lag; measure end-to-end freshness, not
  just processing time.

## 9. Common mistakes and anti-patterns

- Choosing Kafka/Airflow "for scale" for a nightly 50k-row job → months of operational overhead,
  no correctness gain → start with a command or Celery task; escalate on measured need.
- Treating Celery `SUCCESS` as proof of correctness → silent row loss goes unnoticed → end every
  job with count/control-total verification stored on the job.
- Assuming exactly-once delivery → duplicate payments/emails after a redelivery → at-least-once
  + unique constraints + idempotency keys.
- Storing the checkpoint in a different system (Redis) from the data (PostgreSQL) → crash between
  writes causes skip or replay → commit checkpoint and data in the same transaction.
- One giant transaction for a million-row import → lock contention, timeouts, total rollback on
  one bad row → batch commits + staging + reject-records policy.
- No provenance → impossible to answer "where did this value come from" during an audit →
  record source batch, row and mapping version.
- Using asyncio to "speed up" CPU-bound parsing → no speedup, blocked event loop → processes or
  vectorised libraries.
- Processing in the request because "files are small today" → timeouts when one customer uploads
  500 MB → enforce size limits and route to background jobs above a threshold.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Target has more rows than source | Duplicate delivery without unique constraint | Add unique constraint on natural key; dedupe; UPSERT |
| Target has fewer rows, job "succeeded" | `ignore_conflicts=True` swallowing rows; filter bug; rejected rows not counted | Compare received/persisted/rejected; avoid silent ignore |
| Totals off by cents | Float arithmetic or inconsistent rounding | `Decimal` end to end; single rounding rule |
| Readers see half-loaded data | Commit-per-batch directly into live tables | Load into staging; promote atomically |
| Job runs twice concurrently | Cron overlap or duplicate enqueue | DB/advisory lock per job scope; unique active-job constraint |
| Data stale despite job running | Watermark not advanced or advanced too early | Inspect `Watermark`; advance only after commit |
| Request timeouts on import endpoint | Processing on request path | Move to background job; return job id |

**Validation checklist**

- [ ] Lifecycle stages listed; skipped stages justified.
- [ ] Simplest viable vehicle chosen and the escalation trigger written down.
- [ ] Every unit of work has a stable identity; sink enforces uniqueness.
- [ ] Checkpoint/dedupe marker commits in the same transaction as the effect.
- [ ] Terminal status derived from verification (counts, control totals), not from "no exception".
- [ ] Provenance (source, batch, row) and lineage (run, mapping version) recorded.
- [ ] Memory bounded independent of input size; concurrency × per-task memory fits the host.

## 11. Related references and skills

- In this skill: [data-ingestion-engineering.md](data-ingestion-engineering.md),
  [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md),
  [django-background-processing.md](django-background-processing.md),
  [large-dataset-processing.md](large-dataset-processing.md),
  [data-validation-and-cleansing.md](data-validation-and-cleansing.md),
  [database-loading-and-persistence.md](database-loading-and-persistence.md),
  [data-reconciliation-engineering.md](data-reconciliation-engineering.md),
  [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md),
  [data-processing-observability.md](data-processing-observability.md),
  [production-architectures-imports-and-pipelines.md](production-architectures-imports-and-pipelines.md).
- Other skills: [django-architecture distributed systems](../../django-architecture/references/distributed-systems.md),
  [event-driven architecture](../../django-architecture/references/event-driven-architecture.md),
  [architecture decision records](../../django-architecture/references/architecture-decision-records.md),
  [django-orm transactions](../../django-orm/references/transactions-and-concurrency.md),
  [django-integrations messaging](../../django-integrations/references/messaging-and-event-driven-integrations.md),
  [django-integrations sync and reconciliation](../../django-integrations/references/data-synchronization-and-reconciliation.md).

## 12. Official documentation and external references

- Django transactions: https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Django `bulk_create`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- Django management commands: https://docs.djangoproject.com/en/5.2/howto/custom-management-commands/
- Celery tasks (acks_late, idempotency notes): https://docs.celeryq.dev/en/stable/userguide/tasks.html
- PostgreSQL transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL `INSERT … ON CONFLICT`: https://www.postgresql.org/docs/current/sql-insert.html
- Python `decimal`: https://docs.python.org/3/library/decimal.html
- AWS Well-Architected data analytics lens (batch vs streaming): https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/analytics-lens.html (VERIFY: URL path current)
- Azure architecture — batch vs stream processing: https://learn.microsoft.com/en-us/azure/architecture/data-guide/big-data/batch-processing
- OpenTelemetry concepts (tracing pipelines): https://opentelemetry.io/docs/concepts/
