# ETL and ELT pipelines

> **Scope:** Designing multi-stage data pipelines in Django: ETL vs ELT architecture, stage
> contracts, orchestration options, dependencies, scheduling, run state (`PipelineRun`,
> `PipelineStepRun`, `Watermark`), incremental/full/delta loads, checkpointing, watermark
> management, schema evolution, retries, recovery, observability hooks, and a full end-to-end
> example. **Not here:** ingestion mechanics ([data-ingestion-engineering.md](data-ingestion-engineering.md)),
> validation rules ([data-validation-and-cleansing.md](data-validation-and-cleansing.md)),
> mapping configuration ([data-transformation-and-mapping.md](data-transformation-and-mapping.md)),
> UPSERT/COPY mechanics ([database-loading-and-persistence.md](database-loading-and-persistence.md)),
> reconciliation methods ([data-reconciliation-engineering.md](data-reconciliation-engineering.md)),
> Celery task mechanics ([django-background-processing.md](django-background-processing.md)).

## 1. Purpose and scope

A pipeline is a sequence of stages, each with a typed input, a typed output and a recorded
outcome. This reference shows how to build pipelines that are **decomposed** (stages testable
without a database, broker or network), **restartable** at stage granularity from persisted run
state, **incremental** via transactionally managed watermarks, **observable** through run/step
records and hooks, and **orchestrated** with the least machinery that works — usually an
in-process runner invoked by a management command or a Celery task.

## 2. Core concepts

### 2.1 ETL vs ELT architecture

| Aspect | ETL | ELT |
|---|---|---|
| Where transformation runs | Python (Django services, Pandas/Polars) | Database (SQL over staging tables) |
| What lands first | Clean, conformed records | Raw records |
| Replay after a rule change | Re-extract (or replay from raw landing) | Re-run SQL over retained raw tables |
| Best for | Domain-rich rules, per-row validation with user-facing errors | Large set-based transforms, joins, aggregations |
| Risk | Row-by-row Python cost | Business rules split into SQL; harder unit tests |

The common Django hybrid: **land raw → validate/map in Python per chunk → load staging → promote
with set-based SQL → reconcile**. Concepts: [data-processing-fundamentals.md](data-processing-fundamentals.md#24-etl-vs-elt-concepts).

### 2.2 Stage contracts

Each stage declares:

| Contract element | Example |
|---|---|
| Input | `IngestionBatch` id (LANDED) or an iterator of `RawRow` |
| Output | `StagedBatch` with counts, or rows in `staging_customer` keyed by `run_id` |
| Preconditions | Upstream step `SUCCEEDED`; watermark ≤ requested window |
| Postconditions | `in_count == out_count + rejected_count` |
| Idempotency rule | "Delete staging rows for `run_id`, then insert" or UPSERT on key |
| Failure classes | Transient → retry step; permanent → fail run; data → reject records, continue |

Stages communicate via **durable handles** (ids, table + run_id, storage keys) whenever they may
run in different processes; inside one process, pass iterators to keep memory bounded.

### 2.3 Load strategies

| Strategy | Mechanism | Detects deletes | Cost |
|---|---|---|---|
| Full refresh | Rebuild target (or a partition) from the complete source | Yes | Highest; simplest correctness |
| Incremental | Changes since watermark, UPSERT | No (unless tombstones) | Low |
| Delta | Compute diff between new snapshot and previous (hash per key) → insert/update/delete sets | Yes | Medium; needs prior snapshot |
| Replace partition | Delete + insert for one bounded scope (date, tenant, supplier) in one transaction | Yes, within scope | Medium; naturally idempotent |

### 2.4 State and metadata

`PipelineRun` = one execution for one scope (pipeline, tenant, window); `PipelineStepRun` = one
stage within it (status, attempts, handles, counts, checkpoint); `Watermark` = committed
high-water mark per `(tenant, pipeline, source)`, advanced only with a successful run's final status.

### 2.5 Orchestration options

From simplest to heaviest: (1) an **in-process stage runner** — a Python loop over stage objects
recording `PipelineStepRun`s, run by one command or Celery task; (2) a **Celery chain**
(`step1.si(run_id) | step2.si(run_id)`) so each stage gets its own queue, retry policy and time
limit; (3) **group/chord fan-out** over partitions (chord needs a result backend); (4) an
**external orchestrator** (Airflow, Dagster, Prefect) for DAG UIs, backfills and cross-system
dependencies — an option, never a prerequisite (VERIFY: how the chosen tool bootstraps Django —
`django.setup()` in its workers vs invoking a management command). Selection: §4.

## 3. Internal mechanics

### 3.1 Run lifecycle

```text
start_run(pipeline, scope)
  ├─ acquire lock (one active run per pipeline+tenant)    → UniqueConstraint on active runs
  ├─ read Watermark → compute window [wm - overlap, now - grace)
  ├─ for each stage in order:
  │     step = PipelineStepRun.get_or_create(run, stage.name)
  │     if step.status == SUCCEEDED: continue              ← resume skips completed stages
  │     step.status = RUNNING; attempts += 1
  │     output = stage.run(ctx, input_handle)              ← stage is idempotent for this run
  │     step.status = SUCCEEDED; output_handle, counts saved
  ├─ BEGIN
  │     run.status = SUCCEEDED | PARTIAL
  │     Watermark.value = window_end                       ← same transaction
  │  COMMIT
  └─ emit hooks (metrics, notification)
```

On failure the run is `FAILED` with the failing step recorded. An operator "resume" action resets
it to `PENDING` (allowed only if no other run is active) and re-enqueues it; the runner then
starts at the failed step because earlier steps are `SUCCEEDED` with durable output handles.

### 3.2 Why watermarks commit with run status

If the watermark advances and the run later fails (or vice versa), the next run either skips data
or reprocesses it under a stale view. Committing both in one transaction means the watermark
always describes data that is *fully* loaded and reconciled. Reprocessing overlap is harmless
because loads are idempotent.

### 3.3 Schema evolution

| Change at source | Effect | Handling |
|---|---|---|
| New optional field | Ignored by mapping | Land raw (kept in JSONB); add to mapping when needed |
| Field renamed | Mapping yields null → validation rejects | Versioned `MappingDefinition`; alias old name |
| Type change (int → string id) | Conversion error | Contract check stage fails fast with a clear message |
| Field removed | Required-field violations | Fail run (permanent) — don't silently load nulls |
| Target schema change (our migration) | Staging/target column mismatch | Migrate staging and target together; record `schema_version` on runs |

A **contract check** stage (sample the first N raw records, compare keys/types to an expected
schema) turns silent drift into an explicit failure before expensive work. Target-side migrations:
[../../django-orm/references/migrations-and-schema-evolution.md](../../django-orm/references/migrations-and-schema-evolution.md).

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| 3–6 stages, one source, hourly/daily | In-process runner in one Celery task or command | A stage needs a different queue/time limit → Celery chain |
| One stage CPU-heavy, others I/O | Chain with the heavy stage routed to a dedicated queue | — |
| Partitionable transform (per supplier, per day) | Fan-out per partition; join step polls `PipelineStepRun` rows | Use chord only if a result backend is already run reliably |
| Source supports reliable `updated_at` | Incremental + overlap | Deletes matter → weekly delta/full refresh |
| Small reference tables (< 100k rows) | Full refresh / replace-partition | — |
| Large fact data, deletes matter | Delta via per-key hash snapshot | Source provides CDC → use it |
| Dozens of pipelines, cross-team deps, backfill UI | External orchestrator calling Django services | Single team, few pipelines → don't add it |
| Business must see all-or-nothing per run | Load staging, promote in one transaction | Promotion too large for one transaction → partition swaps |

| Retry granularity | Gain | Cost |
|---|---|---|
| Whole run | Simple | Repeats expensive successful stages |
| Per step (resume) | Efficient; clear failure point | Steps must have durable outputs |
| Per chunk within step | Minimal rework for huge steps | Chunk checkpoints and idempotent chunk writes |

## 5. Django/Python implementation patterns

### 5.1 Models

```python
from django.db import models  # [Django]

class PipelineRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING"
        RUNNING = "RUNNING"
        SUCCEEDED = "SUCCEEDED"
        PARTIAL = "PARTIAL"
        FAILED = "FAILED"
        CANCELLED = "CANCELLED"

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    pipeline = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    mode = models.CharField(max_length=12, default="incremental")  # incremental|full|delta
    window_start = models.DateTimeField(null=True)
    window_end = models.DateTimeField(null=True)
    params = models.JSONField(default=dict)
    code_version = models.CharField(max_length=40, blank=True)      # git sha
    mapping_version = models.PositiveIntegerField(null=True)
    summary = models.JSONField(default=dict)                         # counts, control totals
    error = models.TextField(blank=True)
    correlation_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["tenant", "pipeline"],
            condition=models.Q(status__in=["PENDING", "RUNNING"]),
            name="one_active_run_per_pipeline",
        )]

class PipelineStepRun(models.Model):
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="steps")
    name = models.CharField(max_length=64)
    order = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=12, choices=PipelineRun.Status,
                              default=PipelineRun.Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    input_ref = models.JSONField(default=dict)       # durable handle(s) consumed
    output_ref = models.JSONField(default=dict)      # durable handle(s) produced
    checkpoint = models.JSONField(default=dict)      # chunk-level resume point
    in_count = models.PositiveIntegerField(default=0)
    out_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "name"], name="uniq_step_per_run")]
        ordering = ["order"]

class Watermark(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    pipeline = models.CharField(max_length=64)
    source = models.CharField(max_length=64)
    value = models.DateTimeField()                   # or CharField for opaque cursors/sequences
    updated_by_run = models.ForeignKey(PipelineRun, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "pipeline", "source"],
                                               name="uniq_watermark")]
```

### 5.2 Stage protocol and runner

```python
from dataclasses import dataclass, field
from typing import Any, Protocol

@dataclass(slots=True)
class StageContext:
    run_id: int
    tenant_id: int
    window_start: Any
    window_end: Any
    params: dict = field(default_factory=dict)
    step_run_id: int | None = None          # set by the runner before each stage executes

@dataclass(frozen=True, slots=True)
class StageResult:
    output_ref: dict
    in_count: int
    out_count: int
    rejected_count: int = 0

class Stage(Protocol):
    name: str
    def run(self, ctx: StageContext, input_ref: dict) -> StageResult: ...

class PermanentStageError(Exception): ...
class TransientStageError(Exception): ...
```

```python
from django.db import transaction
from django.utils import timezone

def execute(run: PipelineRun, stages: list[Stage], hooks: "PipelineHooks") -> PipelineRun:
    ctx = StageContext(run.pk, run.tenant_id, run.window_start, run.window_end, run.params)
    PipelineRun.objects.filter(pk=run.pk).update(status=PipelineRun.Status.RUNNING)
    input_ref: dict = {}
    for order, stage in enumerate(stages):
        step, _ = PipelineStepRun.objects.get_or_create(run=run, name=stage.name,
                                                        defaults={"order": order})
        if step.status == PipelineRun.Status.SUCCEEDED:
            input_ref = step.output_ref                       # resume: reuse durable output
            continue
        step.status, step.started_at = PipelineRun.Status.RUNNING, timezone.now()
        step.attempts += 1
        step.input_ref = input_ref
        step.save()
        hooks.step_started(run, step)
        ctx.step_run_id = step.pk
        try:
            result = stage.run(ctx, input_ref)
        except TransientStageError:
            step.status = PipelineRun.Status.FAILED
            step.save(update_fields=["status"])
            raise                                             # caller (Celery) retries the run
        except Exception as exc:                              # permanent / unexpected
            _fail(run, step, exc, hooks)
            return run
        step.status, step.finished_at = PipelineRun.Status.SUCCEEDED, timezone.now()
        step.output_ref, step.in_count = result.output_ref, result.in_count
        step.out_count, step.rejected_count = result.out_count, result.rejected_count
        step.save()
        hooks.step_finished(run, step)
        input_ref = result.output_ref
    _complete(run, hooks)
    return run

def _complete(run: PipelineRun, hooks: "PipelineHooks") -> None:
    rejected = sum(s.rejected_count for s in run.steps.all())
    with transaction.atomic():
        run.status = PipelineRun.Status.PARTIAL if rejected else PipelineRun.Status.SUCCEEDED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        Watermark.objects.update_or_create(
            tenant_id=run.tenant_id, pipeline=run.pipeline, source=run.params["source"],
            defaults={"value": run.window_end, "updated_by_run": run},
        )
    hooks.run_finished(run)
```

`_fail` sets step and run to `FAILED` (truncated error), calls `hooks.run_failed`, and never
touches the watermark. Advancing it on `PARTIAL` is a business decision — correct when rejected
records go through a separate reprocessing workflow ([processing-reliability-and-recovery.md](processing-reliability-and-recovery.md)).

### 5.3 Observability hooks

`PipelineHooks` is a small class with `step_started`, `step_finished`, `run_finished` and
`run_failed` methods. The default implementation emits structured `logging` [stdlib] events
carrying `run_id`, `pipeline`, `step`, `attempt`, counts and `correlation_id`; a metrics
implementation records duration, rows and status; an alerting implementation fires on
`run_failed`. Keeping hooks injectable means stages never import logging/metrics clients, and
tests pass a recording fake. Metric names, dashboards and alerts:
[data-processing-observability.md](data-processing-observability.md).

### 5.4 End-to-end example: supplier price feed → catalog

Business: hourly incremental sync of supplier prices (paginated REST API) into `Product`;
rejected rows go to purchasing, control totals are checked.

```text
[ingest]      AcmeConnector → RawRecord rows       out: {"batch_id": 812}
[validate]    RawRecord → ValidPrice | Rejected     out: {"batch_id": 812, "valid": 9_870}
[transform]   ValidPrice → CatalogRow (Decimal, currency, SKU mapping)
[stage]       CatalogRow → staging_price (run_id)   out: {"staging_run_id": 4411}
[load]        staging_price → Product UPSERT        out: {"inserted": 120, "updated": 9_750}
[reconcile]   counts + sum(price) staging vs target out: {"reconciliation_run_id": 77}
[report]      summary → PipelineRun.summary, notify purchasing
```

Pure transformation core — no Django imports, trivially unit-testable:

```python
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN  # [stdlib]

@dataclass(frozen=True, slots=True)
class ValidPrice:
    supplier_sku: str
    currency: str
    unit_price: Decimal
    raw_id: int

@dataclass(frozen=True, slots=True)
class Rejection:
    raw_id: int
    code: str
    message: str

def validate_price(raw_id: int, payload: dict) -> ValidPrice | Rejection:
    sku = str(payload.get("sku", "")).strip()
    if not sku:
        return Rejection(raw_id, "missing_sku", "sku is required")
    try:
        price = Decimal(str(payload["price"]))
    except (KeyError, InvalidOperation):
        return Rejection(raw_id, "bad_price", f"unparseable price {payload.get('price')!r}")
    if price <= 0:
        return Rejection(raw_id, "non_positive_price", str(price))
    currency = str(payload.get("currency", "")).upper()
    if currency not in {"GBP", "EUR", "USD"}:
        return Rejection(raw_id, "bad_currency", currency)
    return ValidPrice(sku, currency, price.quantize(Decimal("0.01"), ROUND_HALF_EVEN), raw_id)
```

Stage adapters — thin Django shells around the pure functions:

```python
from itertools import batched  # [stdlib] (Python ≥ 3.12)

class ValidateStage:
    name = "validate"

    def run(self, ctx: StageContext, input_ref: dict) -> StageResult:
        batch_id = input_ref["batch_id"]
        StagingPrice.objects.filter(run_id=ctx.run_id).delete()      # idempotent re-run
        RejectedRecord.objects.filter(job_type="pipeline_step", job_id=ctx.step_run_id,
                                      stage=RejectedRecord.Stage.VALIDATE).delete()
        seen = valid = rejected = 0
        qs = (RawRecord.objects.filter(batch_id=batch_id)
              .order_by("pk").values_list("pk", "payload"))
        for chunk in batched(qs.iterator(chunk_size=2000), 2000):
            ok, bad = [], []
            for raw_id, payload in chunk:
                out = validate_price(raw_id, payload)
                (ok if isinstance(out, ValidPrice) else bad).append(out)
            mapped = [to_staging_row(ctx, v) for v in ok]             # transform + map (pure)
            StagingPrice.objects.bulk_create(mapped, batch_size=2000)
            RejectedRecord.objects.bulk_create(
                [RejectedRecord(tenant_id=ctx.tenant_id, job_type="pipeline_step",
                                job_id=ctx.step_run_id, stage=RejectedRecord.Stage.VALIDATE,
                                locator=f"raw_record:{r.raw_id}", error_codes=[r.code],
                                errors=[{"code": r.code, "field": r.field, "message": r.message}],
                                raw_payload=None,                       # redacted copy if needed
                                payload_sha256=r.payload_sha256)
                 for r in bad],
                ignore_conflicts=True)                                  # uniq (job, stage, locator)
            seen, valid, rejected = seen + len(chunk), valid + len(ok), rejected + len(bad)
        return StageResult({"staging_run_id": ctx.run_id}, seen, valid, rejected)
```

Validate, transform and stage are fused into one pass while the *logic* stays in pure functions
(`validate_price`, `to_staging_row`). `RejectedRecord` is defined in
[data-validation-and-cleansing.md](data-validation-and-cleansing.md); here `job_id` holds the
`PipelineStepRun` id (`ctx.step_run_id`) and `validate_price` returns a rejection carrying
`raw_id`, `field`, `code`, `message` and `payload_sha256`. For very long iterations prefer keyset pagination over `iterator()`
([large-dataset-processing.md](large-dataset-processing.md)).

```python
from django.db import connection, transaction

class LoadStage:
    name = "load"

    def run(self, ctx: StageContext, input_ref: dict) -> StageResult:
        sql = """
            INSERT INTO catalog_product (tenant_id, supplier_sku, currency, unit_price, updated_at)
            SELECT tenant_id, supplier_sku, currency, unit_price, now()
            FROM pipelines_stagingprice WHERE run_id = %s
            ON CONFLICT (tenant_id, supplier_sku) DO UPDATE
               SET unit_price = EXCLUDED.unit_price, currency = EXCLUDED.currency,
                   updated_at = EXCLUDED.updated_at
             WHERE (catalog_product.unit_price, catalog_product.currency)
                   IS DISTINCT FROM (EXCLUDED.unit_price, EXCLUDED.currency)
        """
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [ctx.run_id])
            affected = cur.rowcount
        n = StagingPrice.objects.filter(run_id=ctx.run_id).count()
        return StageResult({"staging_run_id": ctx.run_id}, n, affected)
```

`cur.rowcount` = inserted + updated rows, excluding rows skipped by the guard (VERIFY: rowcount
semantics for `ON CONFLICT … DO UPDATE … WHERE` with your driver). Take table names from
`Model._meta.db_table`. Staging must be unique on `(run_id, tenant_id, supplier_sku)`. The
reconcile stage compares staged count and `SUM(unit_price)` with target rows, writes a
`ReconciliationRun` ([data-reconciliation-engineering.md](data-reconciliation-engineering.md)) and
raises `PermanentStageError` on mismatch; the report stage writes `run.summary` and notifies on commit.

### 5.5 Separating and testing stages independently

| Layer | What it contains | How to test |
|---|---|---|
| Pure functions (`validate_price`, `to_staging_row`) | Rules, conversions, mapping | Plain `pytest`, table-driven + property-based ([third-party: hypothesis]) |
| Stage adapters (`ValidateStage.run`) | Read input handle, call pure functions, write output | DB test: seed `RawRecord`s, run stage, assert staging/rejected rows and counts; run twice → same result |
| Connector | Source paging/auth | Recorded responses; no network |
| Runner | Ordering, resume, watermark | Fake stages (one raises) → assert step statuses, watermark untouched; re-run resumes |
| End-to-end | Whole pipeline on a small fixture | One integration test per pipeline with fake connector |

Rules that make this possible: stages receive `StageContext` + `input_ref`, never globals; side
effects go through the ORM or injected clients; time comes from `ctx` (window), not `now()`
inside logic. Details: [data-processing-testing.md](data-processing-testing.md).

### 5.6 Scheduling and invoking

```python
# tasks.py — one Celery task runs the whole in-process pipeline
from celery import shared_task  # [third-party: celery]

@shared_task(bind=True, acks_late=True, autoretry_for=(TransientStageError,),
             retry_backoff=True, retry_jitter=True, max_retries=5,
             soft_time_limit=3300, time_limit=3600)
def run_price_pipeline(self, run_id: int) -> None:
    run = PipelineRun.objects.get(pk=run_id)
    if run.status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
        return                                     # redelivery after completion
    execute(run, PRICE_STAGES, PipelineHooks())
```

A beat entry creates the `PipelineRun` (window from `Watermark`) and enqueues on commit; the
active-run constraint makes duplicate beats harmless (`IntegrityError` → skip). Celery details:
[django-background-processing.md](django-background-processing.md).

## 6. Production use cases

| Scenario | Architecture | Load strategy | Notes |
|---|---|---|---|
| Supplier price feed (above) | Hybrid ETL/ELT, in-process runner | Incremental + weekly full | Reconcile sum of prices |
| Nightly payroll inputs (timesheets + HR + rates) | Three ingest steps → join → calculate → stage → promote | Replace partition per pay period | All-or-nothing promotion; control totals signed off |
| ERP → CRM accounts | Ingest → map → load, every 15 min | Incremental + nightly delta | Delta catches deletes |
| Hospital bed occupancy reporting | ELT: COPY census CSV → SQL aggregation | Replace partition per day | SQL transforms tested with fixture tables |
| Inventory snapshot from 40 warehouses | Fan-out per warehouse, join step | Full per warehouse partition | Join waits on `PipelineStepRun` rows, no chord |

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| In-process runner | No extra infra; easy to debug; resume per step | Single process; long runs need time-limit care |
| Celery chain per stage | Per-stage queues/limits/retries | More moving parts; state passed only via handles |
| External orchestrator | Mature scheduling/backfill/UI | Operating cost; Django settings/bootstrap inside the orchestrator |
| Staging + promotion | Atomic visibility, easy reconcile | Extra storage and SQL |
| Watermark in DB with run | Consistent incremental semantics | Requires discipline: only runner advances it |

## 8. Security and performance considerations

**Security**

- Raw SQL in ELT stages uses parameters only; table names come from code constants, never from
  source data or user input ([../../django-security-engineering/references/injection-prevention.md](../../django-security-engineering/references/injection-prevention.md)).
- Staging tables hold unvalidated data: never expose via APIs; purge per run after promotion.
- `PipelineRun.params` and `error` must not contain secrets or full PII payloads; truncate and
  mask error text.
- Every stage query filters by `tenant_id`; cross-tenant pipelines run as separate runs.
- Manual re-run/backfill controls are privileged operations; audit who triggered them.

**Performance**

- Fuse adjacent row-wise stages in one pass (validate → transform → stage); keep logic as separate functions.
- Set-based promotion (`INSERT … SELECT … ON CONFLICT`) is typically far faster than ORM row
  loops; the `IS DISTINCT FROM` guard avoids rewriting unchanged rows (less WAL and bloat).
- COPY for very large staging loads; UNLOGGED staging when loss on crash is acceptable
  ([database-loading-and-persistence.md](database-loading-and-persistence.md)).
- `ANALYZE` staging after bulk load; index staging on `run_id` and the join/conflict key.
- Keep step duration below Celery time limits and visibility timeouts, or checkpoint per chunk and re-enqueue.

## 9. Common mistakes and anti-patterns

- Advancing the watermark at run start → failed runs lose data → advance in the completion transaction.
- Passing large objects between chain stages → broker bloat, serialisation errors → pass ids/handles.
- Appending to staging without clearing the run's output → duplicates on retry → delete-by-run_id, then insert.
- Business logic inside stage adapters → only testable with a DB → extract pure functions.
- `timezone.now()` inside transforms → non-deterministic reruns/tests → take time from the run window.
- Airflow for three pipelines → operational overhead → in-process runner until there is a real need.
- Silent schema drift (renamed field → null) → corrupt target data → contract check + versioned mapping.
- Chord without a reliable result backend → join never fires → poll `PipelineStepRun` rows instead.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Run stuck `RUNNING` | Worker killed; no heartbeat | Stale-run sweeper marks `FAILED` after timeout; resume |
| Duplicate target rows after retry | Non-idempotent stage | Inspect stage idempotency rule; add unique key |
| Watermark jumps but data missing | Watermark advanced outside the runner | Restrict writes to runner; audit `updated_by_run` |
| New run can't start | Active-run constraint held by dead run | Mark dead run `FAILED`; then start |
| Load stage slow | Missing index on staging `run_id`/conflict key; no `ANALYZE` | Add indexes; analyse after load |
| Reconcile fails after schema change | Mapping version mismatch | Compare `mapping_version` across runs |
| Counts don't add up | Rejections not counted, or filter in a stage | `in == out + rejected` asserted per step |

**Validation checklist**

- [ ] Every stage has declared input/output handles, idempotency rule and failure classes.
- [ ] Pure logic separated from adapters; unit tests without DB.
- [ ] Runner resumes from the failed step; completed steps skipped.
- [ ] Watermark updated in the same transaction as the terminal run status.
- [ ] One active run per pipeline/tenant enforced by constraint.
- [ ] Reconciliation stage present for financially/operationally significant data.
- [ ] Schema contract check before heavy processing.

## 11. Related references and skills

- In this skill: [fundamentals](data-processing-fundamentals.md) · [ingestion](data-ingestion-engineering.md) · [validation](data-validation-and-cleansing.md) · [transformation](data-transformation-and-mapping.md) · [loading](database-loading-and-persistence.md) · [reconciliation](data-reconciliation-engineering.md) · [background jobs](django-background-processing.md) · [reliability](processing-reliability-and-recovery.md) · [observability](data-processing-observability.md) · [testing](data-processing-testing.md) · [reference architectures](production-architectures-imports-and-pipelines.md)
- Other skills: [django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md) · [django-orm transactions](../../django-orm/references/transactions-and-concurrency.md) · [service layer](../../django-architecture/references/service-layer.md) · [Celery infrastructure](../../django-integrations/references/celery-and-background-integrations.md) · [postgresql-engineering](../../postgresql-engineering/SKILL.md) · [test fixtures](../../django-testing/references/fixtures-and-test-data.md)

## 12. Official documentation and external references

- Django raw SQL: https://docs.djangoproject.com/en/5.2/topics/db/sql/
- Django transactions: https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Celery canvas (chains, chords): https://docs.celeryq.dev/en/stable/userguide/canvas.html
- Celery periodic tasks: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- PostgreSQL `INSERT … ON CONFLICT`: https://www.postgresql.org/docs/current/sql-insert.html
- PostgreSQL `MERGE` (PostgreSQL ≥ 15): https://www.postgresql.org/docs/current/sql-merge.html
- PostgreSQL `COPY`: https://www.postgresql.org/docs/current/sql-copy.html
- Python `itertools.batched`: https://docs.python.org/3/library/itertools.html#itertools.batched
