# Production Architectures: Imports and Pipelines

> **Scope:** Four reusable reference architectures — (1) large CSV/Excel import system,
> (2) multi-source data ingestion platform, (3) ETL processing platform, (8) scheduled data
> processing platform — each from a minimum viable variant up to a scaled variant, with
> requirements, components, data flow, failure handling, persistence, security, scaling,
> monitoring and trade-offs. **Not here:** the mechanics each component relies on — import/export
> UX and models → [data-import-and-export-systems.md](data-import-and-export-systems.md), ingestion
> → [data-ingestion-engineering.md](data-ingestion-engineering.md), pipeline state →
> [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md), tasks →
> [django-background-processing.md](django-background-processing.md), loading →
> [database-loading-and-persistence.md](database-loading-and-persistence.md). Architectures 4–7 →
> [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md).

## 1. Purpose and scope

These blueprints are starting points for design reviews and ADRs
([architecture-decision-records.md](../../django-architecture/references/architecture-decision-records.md)).
Each is presented as:

1. **Minimum viable (MV)** — Django + PostgreSQL + a management command or one Celery queue.
   Most teams should ship this first.
2. **Scaled** — added only when a measured requirement (volume, latency, isolation, sources)
   forces it, with the escalation trigger stated explicitly.

Numbering (1, 2, 3, 8) matches the skill-wide catalogue; 4–7 live in the companion file.

## 2. Core concepts

Shared building blocks used by all four architectures (defined in their owning references):

| Building block | Role | Owner |
|---|---|---|
| Upload / landing storage | Immutable copy of the source bytes (object storage or `FileField`) | [data-ingestion-engineering.md](data-ingestion-engineering.md) |
| Job record | Durable state machine: `ProcessingJob` / `ImportJob` / `PipelineRun` | [django-background-processing.md](django-background-processing.md), [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) |
| Parser | Streaming bytes → records | [file-processing-engineering.md](file-processing-engineering.md) |
| Validator | Row and dataset rules; `RejectedRecord` | [data-validation-and-cleansing.md](data-validation-and-cleansing.md) |
| Mapper / transformer | Source schema → target schema; `MappingDefinition` | [data-transformation-and-mapping.md](data-transformation-and-mapping.md) |
| Staging | Raw/typed rows outside production tables | [database-loading-and-persistence.md](database-loading-and-persistence.md) |
| Loader | Batched UPSERT / COPY / MERGE into production tables | [database-loading-and-persistence.md](database-loading-and-persistence.md) |
| Checkpoint / watermark | Resume point; incremental boundary (`Watermark`) | [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) |
| Reconciler | Counts/totals source vs target | [data-reconciliation-engineering.md](data-reconciliation-engineering.md) |
| Observability | Metrics, structured logs, correlation IDs | [data-processing-observability.md](data-processing-observability.md) |

**Invariant shared by every architecture:** source bytes are stored before processing; every
stage is restartable from durable state; loads are idempotent on a natural/external key; the job
record is the single source of truth for status shown to users.

## 3. Internal mechanics

### 3.1 The common control loop

```text
 trigger (upload | schedule | webhook | operator)
     │
     ▼
 create job row (PENDING) ──commit──► on_commit: enqueue(job_id)        [transaction.on_commit]
     │
     ▼
 worker: claim job (UPDATE … WHERE status IN (PENDING,QUEUED) → RUNNING)   [atomic claim]
     │
     ├─► stage k: read input from durable store → process → write output durably
     │          → update checkpoint (same transaction as output where possible)
     │
     ▼
 terminal state: SUCCEEDED | PARTIAL | FAILED | CANCELLED  + counters + error report
```

The atomic claim (conditional `UPDATE` [Django] `QuerySet.update()` returning rows affected, or
`select_for_update(skip_locked=True)`) makes duplicate deliveries harmless — Celery is
at-least-once with `acks_late=True` [third-party: celery].

### 3.2 Where state lives

| State | Store | Why |
|---|---|---|
| Source bytes | Object storage (S3/Azure via [third-party: django-storages]) or local disk for MV | Replay, audit, large size |
| Job status, counters, checkpoint | PostgreSQL | Transactional with loaded data |
| Intermediate rows | PostgreSQL staging tables (MV) → Parquet in object storage (scaled) | Restartability without re-parsing |
| Rejects | `RejectedRecord` table + downloadable error report | User remediation |
| Broker messages | Redis/RabbitMQ — **never** the source of truth | Messages can be lost or duplicated |

## 4. Engineering decisions and trade-offs

### 4.1 Selecting an architecture

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Users upload files (≤ ~1M rows) for their tenant | (1) Large import system, MV | Escalate to scaled (1) when p95 import > SLA or concurrent imports starve the web tier |
| Many external sources (APIs, SFTP, webhooks) feeding one model | (2) Multi-source ingestion | Don't build it for 1–2 sources — a scheduled command per source is enough |
| Multi-step transforms with dependencies, lineage and reprocessing | (3) ETL platform | Escalate to an external orchestrator/warehouse when DAGs > ~20 steps or data > single Postgres comfort |
| Recurring jobs (nightly, hourly) with SLAs | (8) Scheduled platform | Cron + management command until you need overlap protection, history and alerting across >5 jobs |
| Analytical aggregation over 100M+ rows | ELT into a warehouse | Doing it row-by-row in Django workers |

### 4.2 Cross-cutting decisions

| Decision | MV choice | Scaled choice | Escalation trigger |
|---|---|---|---|
| Executor | Management command or single Celery queue | Dedicated queues per workload, autoscaled workers | Queue latency > SLA; one tenant starves others |
| Staging | Postgres UNLOGGED/regular staging table | Parquet files in object storage + Postgres for final load | Staging tables bloat / vacuum pressure |
| Load | `bulk_create(update_conflicts=True)` batches | COPY into staging + `MERGE`/`INSERT … ON CONFLICT` | Load phase dominates runtime |
| Orchestration | Linear stages in one service function | `PipelineRun`/`PipelineStepRun` with per-step retries; chains/chords | Steps need independent retry or fan-out |
| Scheduling | OS cron / platform scheduler → command | Celery beat (single instance) or external scheduler | Need history, overlap locks, catch-up |

## 5. Django/Python implementation patterns

### 5.1 Architecture 1 — Large CSV/Excel import system

#### Requirements

- Tenant users upload CSV/XLSX (up to e.g. 200 MB / 1M rows); preview, column mapping,
  validation preview, confirm, background import, downloadable error report.
- Partial success allowed per policy (reject dataset vs isolate records).
- Re-upload of the same file must not duplicate data; imports resumable after worker crash.
- Web requests stay < 1 s regardless of file size.

#### Components

- Upload endpoint (DRF [DRF] or form view) with size/type checks; `ImportJob` + `ColumnMapping`
  ([data-import-and-export-systems.md](data-import-and-export-systems.md)).
- Object storage for the source file; streaming parser (`csv` [stdlib] / openpyxl read-only
  [third-party: openpyxl]).
- Validator + mapper; `RejectedRecord`; batched loader.
- Executor: MV = one Celery queue `imports` (or `manage.py run_import <job_id>` for internal tools);
  scaled = per-size queues (`imports_small`, `imports_large`).

#### Data flow

```text
 Browser ──upload──► Django view ──► storage: imports/<tenant>/<job>/source.xlsx
                         │              (sha256 recorded; duplicate-file check)
                         ▼
                    ImportJob(PENDING) ──► preview task: first 50 rows, header detection
                         │
             user maps columns + confirms (ColumnMapping saved, versioned)
                         ▼
          on_commit ─► Celery "imports" ─► worker
                                             │ stream rows (generator)
                                             │ validate+map per row ──► RejectedRecord
                                             │ batched(rows, 1000)
                                             ▼
                                   atomic per batch: UPSERT target rows
                                   + job.checkpoint = batch_no + counters
                                             ▼
                          job → SUCCEEDED | PARTIAL | FAILED; error report CSV → storage
                                             ▼
                               notification + download link (signed, short-lived)
```

#### Processing stages

1. **Accept**: size limit, extension + content sniffing, hash, store. 2. **Preview**: parse
first N rows only. 3. **Map**: persist `ColumnMapping`. 4. **Validate** (optional dry run: full
pass without writes, counts only). 5. **Load** in batches with checkpoint. 6. **Report**: error
file, counters, notification.

#### Failure handling

| Failure | Class | Response |
|---|---|---|
| Invalid row | Data | `RejectedRecord`, continue (isolate policy) or fail job (reject-dataset policy) |
| Corrupt/unsupported file | Permanent | FAILED before any writes; user-facing message |
| DB deadlock/connection drop | Transient | Retry batch with backoff; checkpoint makes it resumable |
| Worker killed (OOM, deploy) | System | `acks_late` redelivery → atomic claim → resume at `checkpoint + 1` |
| Constraint violation in batch | Permanent for batch | Bisect batch to isolate offending rows, or reject batch per policy |

Details: [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

#### Persistence strategy

MV: per-batch `transaction.atomic()` with checkpoint in the same transaction; UPSERT on
`(tenant_id, natural_key)` after in-batch dedupe. Scaled: COPY [third-party: psycopg] into a
per-job staging table, set-based validation in SQL, then one `INSERT … ON CONFLICT` /
`MERGE` (PostgreSQL ≥ 15) promotion. See
[database-loading-and-persistence.md](database-loading-and-persistence.md).

#### Security

Size/row/column limits before parsing; content sniffing (XLSX = ZIP → ratio/member limits);
defusedxml [third-party: defusedxml] installed for openpyxl; tenant-scoped storage keys and job
queries; error reports may contain PII → access-controlled, expiring. Formula neutralisation on
the error report export. See [data-processing-security.md](data-processing-security.md).

#### Scaling

| Volume | Variant |
|---|---|
| < 50k rows, few imports/day | MV: single queue, 2 workers, ORM UPSERT |
| 50k–2M rows, dozens concurrent | Separate small/large queues, `worker_prefetch_multiplier=1`, per-tenant concurrency cap |
| > 2M rows or XLSX > 100 MB | Convert to CSV/Parquet first stage; COPY + set-based validation; consider chunk fan-out with a chord (needs result backend) |

#### Monitoring

Rows/sec per job, job duration p95, queue wait time, reject ratio per tenant, stuck RUNNING jobs
(heartbeat older than N minutes). [data-processing-observability.md](data-processing-observability.md).

#### Trade-offs

- **Simpler alternative:** synchronous import in the request for < 1k rows (admin-only tools)
  — acceptable if bounded by a hard row limit.
- **Escalate** to staging+COPY when the load phase > 50 % of runtime or ORM UPSERT exceeds SLA.
- XLSX streaming is slower than CSV and `read_excel` cannot chunk; offering a CSV template is
  often the cheapest optimisation.

```python
# MV executor: one task, service does the work, PK-only argument.
from celery import shared_task
from imports.services import run_import, ImportTransientError

@shared_task(
    bind=True, acks_late=True, autoretry_for=(ImportTransientError,),
    retry_backoff=True, retry_backoff_max=600, retry_jitter=True, max_retries=5,
    soft_time_limit=3_300, time_limit=3_600, queue="imports",
)
def process_import(self, job_id: int) -> None:
    run_import(job_id)  # claims atomically, resumes from checkpoint, idempotent
```

Time limits depend on signals and are not enforced on Windows or some pools (VERIFY: your
worker pool); broker `visibility_timeout` (Redis/SQS) must exceed `time_limit` or the job is
redelivered mid-run.

### 5.2 Architecture 2 — Multi-source data ingestion platform

#### Requirements

- 5–50 sources: partner REST APIs (paginated, rate-limited), SFTP drops, webhooks, cloud buckets.
- Normalise into one canonical model (e.g. `Customer`, `Product`) with provenance per field.
- Per-source schedules, credentials, watermarks; one source failing must not block others.
- Raw payload retained for replay (retention per policy).

#### Components

- `Source` config (type, schedule, credentials reference, mapping version).
- Connectors per source type (pull: API/SFTP/bucket; push: webhook receiver)
  — mechanics in [data-ingestion-engineering.md](data-ingestion-engineering.md); HTTP clients and
  webhook verification in [http-and-rest-api-integrations.md](../../django-integrations/references/http-and-rest-api-integrations.md)
  and [webhooks-and-inbound-integrations.md](../../django-integrations/references/webhooks-and-inbound-integrations.md).
- Raw landing: `IngestionBatch` + `RawRecord` (or raw files in object storage).
- Normaliser: per-source `MappingDefinition` → canonical records.
- Matcher/merger: dedupe across sources, survivorship rules.
- Queues: `ingest_pull`, `ingest_normalise`, `ingest_webhook`.

#### Data flow

```text
 Partner API ─┐ pull (beat → per-source task, watermark)
 SFTP drop  ──┤                                         ┌──► RawRecord (payload JSON, hash,
 S3 bucket  ──┤──► connector ──► IngestionBatch ────────┤     source_key, received_at)
 Webhook  ────┘  (verify sig, 2xx fast, enqueue)        └──► raw file in object storage
                                                              │
                                     normalise task (per batch, per source mapping version)
                                                              ▼
                                     canonical staging ──► match/merge (external IDs,
                                                              │       survivorship)
                                                              ▼
                                     UPSERT canonical tables + provenance
                                                              ▼
                                     watermark advance (after durable load) ──► metrics
```

#### Processing stages

1. **Acquire** (connector; watermark-bounded). 2. **Land raw** (append-only, hashed, deduped by
`(source, source_key, payload_hash)`). 3. **Normalise** (mapping version recorded). 4. **Validate**.
5. **Match/merge**. 6. **Load** + provenance. 7. **Advance watermark** only after step 6 commits.

#### Failure handling

- Per-source isolation: separate tasks and circuit breakers; a failing source marks its
  `IngestionBatch` FAILED and alerts, others continue
  ([integration-reliability-and-resilience.md](../../django-integrations/references/integration-reliability-and-resilience.md)).
- 429/5xx → transient, backoff honouring `Retry-After`; 401/403 → permanent, page owner.
- Schema drift (unknown/missing fields) → land raw anyway, fail normalisation for that batch,
  replay after mapping update — raw landing is what makes replay possible.
- Webhooks: acknowledge only after the raw record is durably stored; duplicates removed by hash.

#### Persistence strategy

Raw layer append-only (partition `RawRecord` by month if large — see
[postgresql-engineering](../../postgresql-engineering/SKILL.md)); canonical layer UPSERT by
`(tenant, canonical_key)`; cross-reference table `(source, external_id) → canonical_id`; watermark
row per source updated in the same transaction as the load.

#### Security

Credentials in a secrets manager referenced by name, never in `Source` rows in plain text
([secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md));
webhook signature verification and replay window; SSRF protection if source URLs are
user-configurable; raw payloads may hold PII → encryption at rest, retention job, restricted
admin access.

#### Scaling

| Pressure | Response |
|---|---|
| Many slow APIs | I/O-bound → more worker concurrency (threads/gevent pool) on `ingest_pull` only (VERIFY: pool compatibility with your DB driver) |
| Burst webhooks | Receiver writes raw + enqueues; normalisation batched every N seconds |
| Raw table growth | Partitioning + retention; move cold raw to object storage |
| Heavy normalisation | Separate CPU queue with prefork workers |

#### Monitoring

Per-source freshness (now − watermark), records acquired/normalised/rejected per run, error class
counts, API quota remaining, webhook lag. Alert on freshness SLA breach, not on single failures.

#### Trade-offs

- **Simpler alternative:** one management command per source on cron writing directly to the
  canonical table — fine for ≤ 3 stable sources with no replay requirement.
- **Escalate** to a raw layer + mapping versions when sources change schema without notice or
  you must reprocess history. Escalate to a streaming platform/CDC tooling only when latency
  requirements are seconds and volume exceeds what Postgres-backed queues sustain
  ([messaging-and-event-driven-integrations.md](../../django-integrations/references/messaging-and-event-driven-integrations.md)).

### 5.3 Architecture 3 — ETL processing platform

#### Requirements

- Defined pipelines (e.g. nightly "orders → finance ledger"): extract, validate, transform,
  stage, load, reconcile, report — with step dependencies and lineage.
- Re-run any run or step; full and incremental modes; schema evolution without downtime.
- Auditable: which input produced which output, with which mapping version.

#### Components

- Pipeline definitions in code (registry of step callables); `PipelineRun`, `PipelineStepRun`,
  `Watermark` ([etl-and-elt-pipelines.md](etl-and-elt-pipelines.md)).
- Executor: MV = one task running steps sequentially; scaled = Celery chain/chord per run.
- Staging schema in PostgreSQL; optional Parquet intermediates via [third-party: pyarrow].
- Reconciliation step ([data-reconciliation-engineering.md](data-reconciliation-engineering.md)).

#### Data flow

```text
 schedule/operator ─► PipelineRun(PENDING, mode=incremental, window=[wm, now))
                         │
   ┌─────────────────────┼─────────────────────────────────────────────┐
   │ step: extract   ──► staging.orders_raw (run_id)                   │ each step:
   │ step: validate  ──► staging.orders_valid + RejectedRecord         │  PipelineStepRun
   │ step: transform ──► staging.ledger_lines (mapping v7)             │  RUNNING→SUCCEEDED
   │ step: load      ──► MERGE into ledger (by run_id, line_key)       │  input/output counts
   │ step: reconcile ──► ReconciliationRun (counts, Σ amounts)         │
   │ step: report    ──► summary + alerts                              │
   └─────────────────────┼─────────────────────────────────────────────┘
                         ▼
          Watermark advanced iff load+reconcile succeeded (same transaction as final status)
```

#### Processing stages

As above; each step reads only from durable outputs of its predecessor (never in-memory hand-off
between tasks), writes outputs keyed by `run_id`, and is idempotent (delete-then-insert by
`run_id` in staging, MERGE by business key in production).

#### Failure handling

- Step retry policy per step (extract: transient retries; transform: none — data bugs need a fix).
- Failed run → resume from first non-SUCCEEDED step; staging from earlier steps reused.
- Reconciliation mismatch beyond tolerance → run PARTIAL/FAILED, watermark **not** advanced,
  exceptions queued for review.
- Backfill: new run with explicit window; must not move the live watermark backwards.

#### Persistence strategy

Staging tables carry `run_id`; cleaned by a retention job. Production load in one transaction per
run when size allows (simplest rollback), else per-partition (e.g. per day) with step checkpoints.
Lineage: `PipelineStepRun` stores input/output row counts, mapping version, code version (git SHA).

#### Security

Pipelines run with a dedicated DB role limited to staging + target tables; secrets for sources as
in Architecture 2; staging tables containing PII follow the same retention/masking rules as
production ([data-processing-security.md](data-processing-security.md)).

#### Scaling

MV handles tens of millions of rows/night on one Postgres if steps are set-based SQL. Scale by:
pushing transforms into SQL (ELT style), partition-parallel steps (chord over days/tenants),
Polars/PyArrow for CPU-heavy file transforms
([large-dataset-processing.md](large-dataset-processing.md)), and finally a warehouse for
analytical workloads.

#### Monitoring

Run duration vs SLA, step durations, rows in/out per step (sudden drops = upstream issue),
reject ratio, reconciliation deltas, watermark lag.

#### Trade-offs

- **Simpler alternative:** a single management command with explicit stages and a `PipelineRun`
  row — no chains, no per-step tasks.
- **Escalate** to an external orchestrator (Airflow/Dagster-class tools) when you have many teams'
  pipelines, cross-system dependencies or need a DAG UI; this skill does not cover them.

```python
# MV ETL executor: sequential steps, resumable from first unfinished step.
from etl.models import PipelineRun, PipelineStepRun
from etl.registry import PIPELINES

def execute_run(run_id: int) -> None:
    run = PipelineRun.objects.get(pk=run_id)
    for name, step in PIPELINES[run.pipeline]:
        step_run, _ = PipelineStepRun.objects.get_or_create(run=run, name=name)
        if step_run.status == PipelineStepRun.Status.SUCCEEDED:
            continue                          # resume: skip completed steps
        step_run.mark_running()
        try:
            stats = step(run)                 # idempotent: keyed by run.pk
        except Exception as exc:
            step_run.mark_failed(exc)
            run.mark_failed(step=name)
            raise
        step_run.mark_succeeded(stats)
    run.finish_and_advance_watermark()        # atomic: status + Watermark
```

### 5.4 Architecture 8 — Scheduled data processing platform

#### Requirements

- 10–100 recurring jobs (nightly payroll pre-checks, hourly inventory snapshots, monthly
  invoicing, attendance roll-ups) with defined SLAs and owners.
- No overlapping runs of the same job; missed runs detected and optionally caught up.
- Run history, manual re-run, pause/resume, alerting on failure or lateness.

#### Components

- Scheduler: MV = OS cron / platform scheduler (Kubernetes CronJob, cloud scheduler) invoking
  `manage.py <command>` ([signals-management-commands.md](../../django-core/references/signals-management-commands.md));
  scaled = Celery beat (exactly one instance) enqueuing tasks
  ([celery-and-background-integrations.md](../../django-integrations/references/celery-and-background-integrations.md)).
- `ScheduledJob` registry (name, schedule, owner, SLA, enabled) and run history (reuse
  `ProcessingJob`).
- Overlap lock: PostgreSQL advisory lock or unique partial index on "running" rows.
- Business-date logic in `zoneinfo` [stdlib] tenant time zones.

#### Data flow

```text
 cron / beat tick ──► "run job X for business_date D"
        │
        ▼
 acquire lock(X)  ──fail──► record SKIPPED_OVERLAP, exit
        │ ok
        ▼
 ProcessingJob(X, D) get_or_create  ── already SUCCEEDED? ──► exit (idempotent)
        │
        ▼
 execute (service function / ETL run / import) ─► status, counters
        │
        ▼
 release lock ─► metrics: last_success_at[X], duration ─► SLA monitor
                                                          (separate check: "X not succeeded
                                                           for D by deadline" → alert)
```

#### Processing stages

1. Tick → compute logical run key `(job, business_date)`. 2. Lock. 3. Idempotency check on run
key. 4. Execute. 5. Record outcome. 6. Independent lateness check.

#### Failure handling

- Run key makes retries and duplicate ticks safe (two beat instances, cron overlap after slow run).
- Failed run: retry within window per job policy; after window → alert owner; next tick does not
  silently skip the failed date — catch-up runs `D-1` first when ordering matters.
- Lateness is detected by an **absence** check (dead-man's switch), since a job that never starts
  emits no failure.
- DST: schedule in UTC or explicitly handle non-existent/ambiguous local times.

#### Persistence strategy

Run history in `ProcessingJob` with unique constraint `(job_name, run_key)`; outputs follow the
owning architecture (1 or 3). Prune history by retention.

#### Security

Commands run as a least-privilege service account; manual re-run restricted to operators
(permission-checked admin action) and audited
([security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)).

#### Scaling

Spread heavy jobs across the night with explicit start times; route by queue; cap concurrent
scheduled jobs; for per-tenant fan-out, the scheduled task enqueues one task per tenant with a
per-tenant run key.

#### Monitoring

`last_success_timestamp{job}`, duration, rows processed, SLA breach count; alert on "no success
since". [data-processing-observability.md](data-processing-observability.md).

#### Trade-offs

- **Simpler alternative:** cron + management commands + unique run-key rows — handles most
  needs with no Celery.
- **Escalate** to beat/tasks when jobs need retries with backoff, fan-out, or share worker
  capacity with other workloads; to an external orchestrator when jobs have cross-job DAGs.
- Beat must run as a single instance; duplicate beats double-enqueue (run key protects you).

```python
# Overlap-safe, idempotent scheduled command (MV).
from datetime import date
from django.core.management.base import BaseCommand
from django.db import connection, transaction, IntegrityError
from jobs.models import ProcessingJob
from attendance.services import roll_up_attendance

LOCK_ID = 82_001  # unique per job

class Command(BaseCommand):
    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="business_date", type=date.fromisoformat, required=True)

    def handle(self, *args, business_date: date, **opts) -> None:
        run_key = business_date.isoformat()
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            if not cur.fetchone()[0]:
                self.stdout.write("overlap: skipped"); return
        try:
            try:
                with transaction.atomic():
                    job = ProcessingJob.objects.create(name="attendance_rollup", run_key=run_key)
            except IntegrityError:
                job = ProcessingJob.objects.get(name="attendance_rollup", run_key=run_key)
                if job.status == ProcessingJob.Status.SUCCEEDED:
                    return
            roll_up_attendance(job, business_date=business_date)
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
```

Session-level advisory locks are tied to the DB connection; with PgBouncer transaction pooling
they are unsafe — use a lock row instead (VERIFY: your pooler mode).

## 6. Production use cases

| Business scenario | Architecture | Variant | Notes |
|---|---|---|---|
| HR uploads monthly employee master (30k rows XLSX) | 1 | MV | Isolate-records policy; UPSERT on employee number |
| Payroll bureau ingests 200 client payroll files/month | 1 | Scaled | Per-tenant concurrency cap; reject-dataset policy (payroll must be complete) |
| Hospital group consolidating doctor rosters from 30 site systems (API + SFTP) | 2 | Scaled | Per-site freshness SLA; survivorship by site authority |
| Product catalog from 12 supplier feeds (CSV, JSON API, XML) | 2 | MV→Scaled | Raw landing essential: suppliers change formats unannounced |
| Nightly orders → finance ledger with reconciliation | 3 | MV | Set-based SQL steps; watermark advances only after reconcile |
| Multi-tenant analytics marts | 3 → ELT/warehouse | Escalated | Postgres for operational, warehouse for analytical |
| Nightly attendance roll-up, monthly invoice run, hourly inventory snapshot | 8 | MV | Cron + commands + run keys |
| 80 scheduled jobs across teams with SLAs | 8 | Scaled | Beat, registry, dead-man's-switch alerts |

## 7. Advantages and disadvantages

| Architecture | Advantages | Disadvantages |
|---|---|---|
| 1 Import system | User self-service; strong feedback loop (preview, error report); resumable | UX + mapping complexity; XLSX parsing cost |
| 2 Multi-source ingestion | Isolation per source; replay from raw; provenance | Raw storage cost; mapping version management; matching is hard |
| 3 ETL platform | Restartable steps, lineage, reconciliation built in | More tables/state; temptation to rebuild an orchestrator |
| 8 Scheduled platform | Predictable, auditable recurring work | Lateness/overlap/DST edge cases; beat single point of failure |
| MV variants generally | Few moving parts, transactional with app DB | Shares DB with OLTP traffic; vertical limits |

## 8. Security and performance considerations

**Security**
- Tenant isolation end-to-end: storage keys, job queries, staging rows, error reports and download
  links all scoped by tenant ([multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)).
- Validate untrusted files before parsing (size, type sniffing, ZIP limits, XML entity handling);
  never trust extensions ([file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)).
- Source credentials in a secrets manager; least-privilege DB roles for pipeline steps.
- Raw/staging layers hold the same sensitive data as production → same encryption, masking and
  retention. Error reports and rejected rows are often the most sensitive artefacts.
- Operator actions (re-run, cancel, reprocess) permission-checked and audited.

**Performance**
- Stream everything (generators, `iter_rows(values_only=True)`, `iterator(chunk_size=…)` or keyset
  pagination); memory must not scale with input size.
- Batch DB writes (500–5 000 rows); measure. COPY for bulk staging loads.
- Separate queues for heavy vs interactive work; `worker_prefetch_multiplier=1` for long tasks;
  broker `visibility_timeout` > longest task.
- Keep imports off the web tier; protect the OLTP database with per-tenant concurrency caps and
  off-peak scheduling of heavy ETL.
- Details: [data-processing-performance.md](data-processing-performance.md),
  [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md).

## 9. Common mistakes and anti-patterns

- Building the scaled variant first → months of infrastructure, no users → ship MV, record
  escalation triggers in an ADR.
- Processing the upload inside the request → timeouts, duplicate submissions → store, enqueue,
  return job ID.
- Passing file contents or model instances to Celery → broker bloat, stale data → pass job PK.
- Advancing the watermark before the load commits → silent data loss on failure → advance in the
  same transaction as the final load/status.
- No raw landing in multi-source ingestion → cannot replay after mapping bug → land raw first.
- Steps handing data in memory across tasks → not restartable → durable intermediates keyed by
  run ID.
- Two beat instances → double runs → single beat + run-key uniqueness.
- Monitoring only failures → a job that never runs is invisible → "last success" freshness alerts.
- One queue for everything → a 2M-row import blocks password-reset emails → route by workload.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Import stuck RUNNING forever | Worker killed; no heartbeat/timeout sweep | Heartbeat column + sweeper re-queues; check `acks_late`, `visibility_timeout` |
| Same import loaded twice | Redelivery without atomic claim / non-idempotent load | Conditional status claim; UPSERT on natural key |
| Source data missing for a window | Watermark advanced before failed load | Move watermark update into load transaction; backfill window |
| Nightly ETL overruns into business hours | Row-by-row ORM in transform/load | Set-based SQL, COPY, partition parallelism |
| Scheduled job ran twice | Overlapping cron / duplicate beat | Advisory lock or run-key unique constraint |
| Job silently didn't run | Scheduler down, DST gap | Dead-man's-switch freshness alert; UTC schedules |
| One tenant's import slows everyone | Shared queue, no caps | Per-tenant concurrency limits; separate large-import queue |

**Validation checklist**
- [ ] MV variant chosen unless an escalation trigger is measured and documented.
- [ ] Source bytes durably stored before processing; job PK is the only task argument.
- [ ] Atomic job claim; checkpoints committed with outputs; loads idempotent.
- [ ] Watermarks advance only after successful load (and reconcile, where present).
- [ ] Per-source/per-tenant isolation of failures and capacity.
- [ ] Freshness/SLA alerts in addition to failure alerts.
- [ ] Security review: limits, sniffing, tenant scoping, secrets, retention of raw/staging/error data.
- [ ] Tests per [data-processing-testing.md](data-processing-testing.md): partial failure, rerun, idempotency.

## 11. Related references and skills

- In this skill: [data-import-and-export-systems.md](data-import-and-export-systems.md),
  [data-ingestion-engineering.md](data-ingestion-engineering.md),
  [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md),
  [django-background-processing.md](django-background-processing.md),
  [database-loading-and-persistence.md](database-loading-and-persistence.md),
  [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md),
  [data-processing-fundamentals.md](data-processing-fundamentals.md),
  [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md).
- [django-architecture](../../django-architecture/SKILL.md):
  [service-layer.md](../../django-architecture/references/service-layer.md),
  [distributed-systems.md](../../django-architecture/references/distributed-systems.md).
- [django-integrations](../../django-integrations/SKILL.md):
  [file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md),
  [integration-observability-and-operations.md](../../django-integrations/references/integration-observability-and-operations.md).
- [django-orm: bulk-operations-and-batching.md](../../django-orm/references/bulk-operations-and-batching.md),
  [postgresql-engineering](../../postgresql-engineering/SKILL.md).

## 12. Official documentation and external references

- Django file storage: https://docs.djangoproject.com/en/5.2/ref/files/storage/
- Django transactions (`on_commit`): https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Django management commands: https://docs.djangoproject.com/en/5.2/howto/custom-management-commands/
- Django `bulk_create`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- Celery tasks: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery periodic tasks: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- PostgreSQL COPY: https://www.postgresql.org/docs/current/sql-copy.html
- PostgreSQL MERGE: https://www.postgresql.org/docs/current/sql-merge.html
- PostgreSQL advisory locks: https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS
- django-storages: https://django-storages.readthedocs.io/en/latest/
- openpyxl optimised modes: https://openpyxl.readthedocs.io/en/stable/optimized.html
