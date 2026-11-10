# Data Processing Observability

> **Scope:** Making pipelines and jobs observable: pipeline/job metrics catalogue, structured
> processing logs, correlation IDs across web → task → batch, audit records, error categorisation
> in telemetry, dashboards, alert rules and operational runbooks. **Not here:** integration-level
> monitoring of external APIs, brokers and webhooks
> ([integration-observability-and-operations.md](../../django-integrations/references/integration-observability-and-operations.md));
> Django logging configuration basics ([logging-exceptions.md](../../django-core/references/logging-exceptions.md));
> profiling ([profiling-and-observability.md](../../django-performance/references/profiling-and-observability.md));
> error taxonomy definition ([processing-reliability-and-recovery.md](processing-reliability-and-recovery.md)).

## 1. Purpose and scope

A data job that "ran" is not a data job that worked. Observability for data processing must answer,
without opening a shell:

1. **Is it running?** Status, progress, heartbeat, queue depth and age.
2. **Is it correct?** Processed / rejected / skipped / failed counts; reconciliation status.
3. **Is it on time?** Duration vs baseline, watermark lag, SLA for daily loads.
4. **What happened to record X?** Correlation from upload → job → batch → row → reject/exception.
5. **Who did what?** Audit: who triggered, approved, reprocessed, with which mapping/code version.

Three signal types, each with a distinct job:

| Signal | Answers | Store | Cardinality budget |
|---|---|---|---|
| Metrics | Trends, alerting | Prometheus / cloud metrics | Low (bounded labels) |
| Logs (structured) | What happened in this run | Log platform (JSON lines) | High (job/batch ids fine) |
| Job/audit records | Durable state, per-tenant UI, compliance | PostgreSQL | Unlimited, retained per policy |
| Traces (optional) | Latency breakdown across services | OpenTelemetry backend | Sampled |

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Job metrics | Counters/gauges about one run, stored on the job row (`processed`, `rejected`, `skipped`, `failed`, timings) |
| Pipeline metrics | Aggregated, low-cardinality time series across runs (`records_total{pipeline,outcome}`) |
| Throughput | Records/second per stage; compare with baseline to detect regressions |
| Queue depth / age | Messages waiting; age of the oldest — age is the better alert signal |
| Worker health | Liveness (heartbeats), memory, task failure rate, restarts |
| Watermark lag | `now - watermark` for incremental loads: how stale the target is |
| Correlation ID | `job_id` / `run_id` / `batch_id` attached to every log line and propagated across task boundaries |
| Audit record | Durable, append-only business event: actor, action, object, versions, outcome |
| Error category | Taxonomy class (TRANSIENT, RATE_LIMIT, DATA, CONTRACT, BUG, AMBIGUOUS, RESOURCE) used as a metric label and log field |
| Runbook | Step-by-step operator procedure linked from each alert |

**Skipped vs rejected vs failed:** *skipped* = intentionally not processed (unchanged hash,
filtered status); *rejected* = invalid data recorded as `RejectedRecord`; *failed* = processing
error recorded as a DLQ/`ProcessingFailure`. Invariant for each run:
`read == processed + skipped + rejected + failed`. Alert when it does not hold.

## 3. Internal mechanics

### 3.1 Correlation through contextvars

`contextvars` [stdlib] give each thread/asyncio task its own value. A logging `Filter` reads them
and stamps every `LogRecord`, so library code that logs without knowing about jobs is still
correlated. Across a Celery boundary the context is **not** inherited: the IDs must travel as task
arguments (or headers) and be set at task start, and reset at task end because worker processes
are reused.

```text
HTTP request (request_id) ─▶ ImportJob created (job_id) ─▶ task.delay(job_id, run_id)
   └▶ task prerun: set ctx(job_id, run_id, tenant_id) ─▶ batch loop: set ctx(batch_no)
        └▶ every log line / metric exemplar / RejectedRecord carries job_id + batch_no
```

### 3.2 Prometheus in a multi-process worker
`prometheus-client` [third-party: prometheus-client] keeps metrics in process memory. Celery
prefork and gunicorn run many processes; you need multiprocess mode (shared directory via
`PROMETHEUS_MULTIPROC_DIR`) or push per-job aggregates elsewhere (VERIFY: multiprocess mode
limitations for gauges in your client version). Short-lived management commands need a
Pushgateway or must write metrics to the job row and let an exporter read them. Many teams derive
pipeline metrics from the **job table** via a small exporter or SQL-backed dashboard — accurate,
durable and cardinality-safe.

### 3.3 Label cardinality
Every unique label combination is a separate time series. `job_id`, `batch_id`, `file_name`,
`user_id` or an unbounded `tenant_id` label creates millions of series and can take down the
metrics backend. Put high-cardinality identifiers in **logs and the job table**, not in metric
labels. Tenant labels are acceptable only for a small, bounded set (e.g. top-tier tenants mapped,
everyone else `other`).

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Per-tenant job progress in the UI | Job table fields + polling endpoint | Prometheus per-tenant series |
| Fleet-wide alerting on rejects/failures | Low-cardinality counters by pipeline + outcome | Alerting on individual log lines |
| "Why did job 8812 reject 4 k rows?" | Logs filtered by `job_id` + `RejectedRecord` summary | Metrics (no per-job detail) |
| Compliance evidence | Audit table (append-only) with retention | Log platform with 14-day retention |
| Simple stack, no Prometheus | JSON logs + job table + scheduled health-check task that emails/pages | Building a metrics stack for 3 nightly jobs |
| Cross-service latency (API → worker → storage) | OpenTelemetry traces [third-party: opentelemetry] | Tracing every row (span explosion) — trace per batch |
| JSON logging | stdlib `logging.Formatter` subclass (no dependency) | Adding structlog only for JSON — it's worth it for bound context and processors |

## 5. Django/Python implementation patterns

### 5.1 Context, filter and JSON formatter (stdlib only)

```python
# observability/logging.py
import contextvars, json, logging                      # [stdlib]
from datetime import datetime, UTC

_ctx: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar("proc_ctx", default={})

def bind(**fields: object) -> contextvars.Token:
    return _ctx.set({**_ctx.get(), **fields})

def reset(token: contextvars.Token) -> None:
    _ctx.reset(token)

class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in _ctx.get().items():
            setattr(record, k, v)
        return True

_STD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        doc.update({k: v for k, v in vars(record).items() if k not in _STD})
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, ensure_ascii=False)
```

```python
# settings.py  [Django] LOGGING dictConfig
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "filters": {"ctx": {"()": "observability.logging.ContextFilter"}},
    "formatters": {"json": {"()": "observability.logging.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json",
                            "filters": ["ctx"]}},
    "root": {"handlers": ["stdout"], "level": "INFO"},
}
```

Attach the filter to the **handler** (as above) so records from all loggers get context. Structured
fields passed via `extra={...}` appear as top-level keys. structlog [third-party: structlog]
offers the same with `contextvars` integration and processor pipelines (VERIFY:
`structlog.contextvars.bind_contextvars` in your version).

### 5.2 Propagating IDs into Celery tasks

```python
import contextvars, logging
from celery import shared_task                        # [third-party: celery]
from observability.logging import bind, reset
from celery.signals import task_prerun, task_postrun

_tokens: dict[str, contextvars.Token] = {}

@task_prerun.connect
def _bind_ctx(task_id=None, task=None, kwargs=None, **_):
    kw = kwargs or {}
    _tokens[task_id] = bind(task_id=task_id, task=task.name,
                            job_id=kw.get("job_id"), run_id=kw.get("run_id"),
                            tenant_id=kw.get("tenant_id"))

@task_postrun.connect
def _reset_ctx(task_id=None, **_):
    if (tok := _tokens.pop(task_id, None)) is not None:
        reset(tok)

@shared_task(bind=True, acks_late=True)
def load_partition(self, *, tenant_id: int, job_id: str, run_id: str, partition: str) -> None:
    log = logging.getLogger("pipeline.load")
    for batch_no, batch in enumerate(read_batches(partition)):
        tok = bind(batch_no=batch_no)
        try:
            ...
            log.info("batch_committed", extra={"rows_in": len(batch), "rows_loaded": loaded,
                                               "rows_rejected": rejected, "duration_ms": ms})
        finally:
            reset(tok)
```

Pass IDs as **keyword** arguments so the signal handler can find them. Signal handler argument
names follow Celery's documented signatures (VERIFY: `task_prerun` sender args in your Celery 5.x).

### 5.3 Structured processing log examples (JSON lines)

```json
{"ts":"2026-10-28T02:00:01.204+00:00","level":"INFO","logger":"pipeline.run","msg":"run_started","run_id":"r-7f3a","job_id":"9b1e","tenant_id":42,"pipeline":"payroll_import","mapping_version":13,"source_ref":"imports/42/9b1e.xlsx","source_sha256":"9f2c41…","triggered_by":"user:118"}
{"ts":"2026-10-28T02:00:04.880+00:00","level":"INFO","logger":"pipeline.load","msg":"batch_committed","run_id":"r-7f3a","job_id":"9b1e","tenant_id":42,"batch_no":0,"rows_in":1000,"rows_loaded":997,"rows_rejected":3,"duration_ms":612}
{"ts":"2026-10-28T02:00:05.102+00:00","level":"WARNING","logger":"pipeline.validate","msg":"rows_rejected","run_id":"r-7f3a","job_id":"9b1e","batch_no":0,"rule":"cost_centre_exists","count":3,"sample_rows":[118,402,977]}
{"ts":"2026-10-28T02:03:12.554+00:00","level":"ERROR","logger":"pipeline.load","msg":"batch_failed","run_id":"r-7f3a","job_id":"9b1e","batch_no":41,"error_category":"TRANSIENT","error_type":"OperationalError","sqlstate":"40P01","attempt":2,"will_retry":true}
{"ts":"2026-10-28T02:09:40.011+00:00","level":"INFO","logger":"pipeline.run","msg":"run_finished","run_id":"r-7f3a","job_id":"9b1e","status":"PARTIAL","rows_read":48210,"rows_processed":48101,"rows_skipped":0,"rows_rejected":109,"rows_failed":0,"duration_s":579.1,"throughput_rps":83.3}
```

Rules: event name in `msg` (stable, snake_case, greppable); numbers as numbers; row **numbers**
and rule names, never row values; one `run_finished` summary line per run with the counts
invariant.

### 5.4 Metrics catalogue

| Name | Type | Labels | Meaning |
|---|---|---|---|
| `dp_records_total` | Counter | `pipeline`, `stage`, `outcome` (processed/skipped/rejected/failed) | Records by outcome |
| `dp_runs_total` | Counter | `pipeline`, `status` | Completed runs by final status |
| `dp_run_duration_seconds` | Histogram | `pipeline` | End-to-end run time |
| `dp_batch_duration_seconds` | Histogram | `pipeline`, `stage` | Per-batch latency; detects slow DB/IO |
| `dp_errors_total` | Counter | `pipeline`, `category` | Errors by taxonomy class |
| `dp_jobs_running` | Gauge | `pipeline` | Currently RUNNING jobs |
| `dp_oldest_running_job_age_seconds` | Gauge | `pipeline` | Stuck-job detector (from job table) |
| `dp_watermark_lag_seconds` | Gauge | `pipeline`, `source` | Staleness of incremental loads |
| `dp_queue_depth` | Gauge | `queue` | Messages waiting (broker exporter) |
| `dp_queue_oldest_message_age_seconds` | Gauge | `queue` | Saturation; better than depth |
| `dp_dlq_open` | Gauge | `pipeline`, `category` | Open `ProcessingFailure` rows |
| `dp_reconciliation_runs_total` | Counter | `check`, `status` | BALANCED/UNBALANCED/FAILED |
| `dp_reconciliation_exceptions_open` | Gauge | `check`, `severity` | Backlog of open exceptions |
| `dp_worker_heartbeat_timestamp` | Gauge | `worker_pool` | Liveness |

`pipeline`, `stage`, `queue`, `check` are code-defined, bounded sets. **No `job_id`, `batch_id`,
`file`, `user` or unbounded `tenant` labels.** Gauges derived from the job table (oldest running,
DLQ open, watermark lag) are best computed by one exporter/collector querying PostgreSQL every
30–60 s rather than by workers.

```python
from prometheus_client import Counter, Histogram   # [third-party: prometheus-client]

RECORDS = Counter("dp_records_total", "Records by outcome", ["pipeline", "stage", "outcome"])
BATCH_SECONDS = Histogram("dp_batch_duration_seconds", "Batch latency", ["pipeline", "stage"],
                          buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120))

def observe_batch(pipeline: str, stage: str, loaded: int, rejected: int, seconds: float) -> None:
    RECORDS.labels(pipeline, stage, "processed").inc(loaded)
    RECORDS.labels(pipeline, stage, "rejected").inc(rejected)
    BATCH_SECONDS.labels(pipeline, stage).observe(seconds)
```

### 5.5 Audit records

```python
class AuditEvent(models.Model):                       # append-only; no update/delete permission
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_kind = models.CharField(max_length=10)      # user | schedule | system
    action = models.CharField(max_length=50)          # import_started, import_approved, reprocess, export_download
    object_ref = models.CharField(max_length=100)     # "importjob:9b1e"
    run_id = models.CharField(max_length=64, blank=True)
    versions = models.JSONField(default=dict)         # {"mapping": 13, "code": "git:4c1d2e"}
    detail = models.JSONField(default=dict)           # counts, reason — no PII payloads
```

Logs answer "how"; audit answers "who/what/when, provably". Broader audit principles →
[security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md).

### 5.6 Dashboard panels

| Panel | Query basis | Why |
|---|---|---|
| Runs by status (last 24 h, per pipeline) | `dp_runs_total` | Spot FAILED/PARTIAL spikes |
| Records by outcome, stacked | `dp_records_total` | Rejection-rate trend |
| Rejection rate % | rejected / (processed + rejected) | Data-quality regression after source change |
| Run duration p50/p95 vs 7-day baseline | histogram | Performance regressions |
| Throughput (records/s) per stage | rate of `dp_records_total` | Bottleneck stage |
| Queue depth + oldest message age | broker exporter | Saturation/backlog |
| Running jobs + oldest running age | job-table gauge | Stuck jobs |
| Watermark lag per source | job-table gauge | Freshness SLA |
| Open DLQ by category | job-table gauge | Triage backlog |
| Reconciliation status + open exceptions by severity | recon gauges | Correctness |
| Worker memory / restarts | infra metrics | OOM kills |

## 6. Production use cases

### 6.1 Alert rules

| Alert | Condition (example thresholds) | Severity | Runbook |
|---|---|---|---|
| Stuck job | `dp_oldest_running_job_age_seconds > 2 × p95 duration` or no heartbeat 10 min | High | RB-01 |
| Rejection spike | rejection rate > 5 % for a run, or > 3× 7-day median | Medium (High for payroll) | RB-02 |
| Run failed | any FAILED for `pipeline=~"payroll.*|payments.*"` | High | RB-03 |
| Watermark lag | `dp_watermark_lag_seconds > 2 × schedule interval` | Medium | RB-04 |
| Queue age | `dp_queue_oldest_message_age_seconds > 900` for 10 min | Medium | RB-05 |
| Reconciliation | UNBALANCED with CRITICAL exceptions, or recon FAILED | Critical | RB-06 |
| DLQ growth | `dp_dlq_open` increased by > 100 in 1 h | Medium | RB-07 |
| Missing run | expected daily run not started by SLA time ("dead man's switch") | High | RB-03 |
| Counts invariant broken | `read != processed+skipped+rejected+failed` | High | RB-02 |

Missing-run alerts matter: a scheduler that silently stopped produces no failures and no metrics.
Alert on the **absence** of `run_finished` for scheduled pipelines.

### 6.2 Runbook template

```text
RB-<id>: <Alert name>
Impact:        who/what is affected (e.g. payroll for tenant group EU unpaid)
Signals:       alert expression; dashboards; log query: job_id=<id> msg=batch_failed
Triage (≤10m): 1. ... 2. ... 3. decide: wait / retry / contain
Contain:       pause schedule / tenant; block downstream exports
Recover:       link to procedure in processing-reliability-and-recovery.md §6.x
Verify:        counts invariant; reconciliation BALANCED; alert cleared
Escalate:      owner team + on-call; business contact for data owners
Record:        AuditEvent(action="incident_recovery"), incident ticket
```

### 6.3 RB-01 Stuck job (employee import RUNNING for 2 h, p95 is 12 min)
1. `SELECT id, status, heartbeat_at, checkpoint FROM importjob WHERE status='RUNNING' ORDER BY started_at`.
2. Logs: `job_id=<id>` — last line? If last is `batch_committed` long ago → worker died (check
   worker restarts / exit 137) or task blocked on a lock (`pg_stat_activity` wait_event for the
   job's connection, `pg_locks`).
3. Worker dead: mark job FAILED (reason `worker_lost`), re-enqueue; resumes from checkpoint.
4. Blocked on lock: identify blocker; cancel the blocker if it is safe, otherwise wait.
5. Verify: job completes; counts invariant holds; row count reconciliation.

### 6.4 RB-02 Rejection spike (attendance import: 38 % rejected, baseline 0.4 %)
1. Logs `msg=rows_rejected job_id=<id>` grouped by `rule` → one rule dominates
   (`shift_date_parse`) → likely contract change (new date format from device vendor).
2. Compare `source_sha256`/file header with last good run; check `MappingDefinition` changes.
3. Contain: mark run PARTIAL is acceptable? For attendance feeding payroll — no: hold promotion.
4. Fix: new mapping version with the new format; re-run from raw landed file.
5. Verify: rejection back to baseline; attendance reconciliation vs HR BALANCED.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Job-table-derived metrics | Durable, accurate, cardinality-safe, per-tenant UI | Polling load on DB; coarse granularity |
| In-process Prometheus | Fine-grained, real-time | Multiprocess complexity; lost on crash; cardinality risk |
| JSON stdlib logging | No dependency; works everywhere | Manual context handling; no processors |
| structlog | Bound context, processors (redaction), dev/prod renderers | Another dependency and config surface |
| OpenTelemetry traces | Cross-service latency breakdown | Instrumentation effort; sampling decisions; cost |
| Audit table | Compliance-grade, queryable | Storage growth; retention policy needed |

## 8. Security and performance considerations

**Security**
- Logs are a data-exfiltration channel: never log row values, file contents, signed URLs or
  tokens. Log row numbers, field names, rule ids. Add a redaction filter as backstop
  ([data-processing-security.md](data-processing-security.md)).
- `tenant_id` in logs is fine; tenant **names** and user emails usually are not — use ids.
- Restrict log platform and dashboard access; audit tables are append-only (DB grants or
  permissions; no admin delete).
- Exception trackers capture local variables by default in some SDKs — disable or scrub for
  processing workers (VERIFY: your error tracker's local-variable capture setting).

**Performance**
- Log per batch, not per row: 10 M rows × 1 log line = log platform cost and I/O bottleneck.
  Per-row detail belongs in `RejectedRecord`.
- `json.dumps` per line is cheap compared with I/O; avoid synchronous network log handlers in
  workers — log to stdout and let the platform ship.
- Updating job progress every row causes write amplification and row-lock contention; update
  per batch or every N seconds.
- Histogram bucket count × label combinations = series count; keep both small.
- Job-table collectors must use indexed queries (`status`, `started_at`) and run at 30–60 s.

## 9. Common mistakes and anti-patterns

- `job_id` as a Prometheus label → series explosion, backend outage → ids in logs/job table only.
- Logging the rejected row's content → PII in logs → row number + rule id.
- Only alerting on FAILED → silent PARTIAL with 40 % rejects goes unnoticed → rejection-rate and
  invariant alerts.
- No alert for a run that never started → scheduler outage invisible for days → dead-man's-switch.
- Context set in the web request assumed in the task → logs without job_id → pass ids as task
  kwargs and bind at prerun.
- Not resetting contextvars in reused workers → next task logs the previous job's id → reset at postrun.
- Alerting on queue depth alone → noisy during expected nightly bursts → alert on oldest message age.
- Alerts without runbooks → slow, inconsistent response → every alert links an RB.
- Progress percentage computed from an unknown total → stuck at 99 % → show processed count and
  rate when total is unknown.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Log lines missing `job_id` | Filter on logger not handler; context not bound in task | Attach filter to handler; prerun binding |
| Wrong `job_id` on lines | Context not reset between tasks | Postrun reset; tokens per task |
| Metrics backend slow / OOM | High-cardinality labels | Inspect series count per metric; drop labels |
| Counters reset or jump | Prefork processes without multiprocess mode | Configure multiprocess dir or derive from job table |
| Dashboard shows success but users complain | Only status tracked, not reconciliation | Add recon panels/alerts |
| Alert storm during backfill | Thresholds tuned for daily volume | Silence/annotate backfills; per-pipeline thresholds |
| No visibility into a stuck task | No heartbeat | Heartbeat column updated per batch |

**Validation checklist**
- [ ] Every log line from processing code has `run_id`/`job_id` (+ `batch_no` in loops) and `tenant_id`.
- [ ] One `run_started` and one `run_finished` line per run with full counts.
- [ ] Counts invariant enforced and alerted.
- [ ] Metrics catalogue documented; no unbounded labels.
- [ ] Alerts for stuck, failed, missing, rejection spike, watermark lag, queue age, reconciliation, DLQ growth.
- [ ] Each alert links a runbook; runbooks rehearsed.
- [ ] Audit events for trigger, approve, reprocess, download, with versions.
- [ ] No PII/secrets in logs, metrics or traces.
- Testing log/metric emission → [data-processing-testing.md](data-processing-testing.md).

## 11. Related references and skills

- [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) — error taxonomy, recovery procedures referenced by runbooks.
- [django-background-processing.md](django-background-processing.md) — `ProcessingJob` status, heartbeats, progress.
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — `PipelineRun`, `Watermark`.
- [data-reconciliation-engineering.md](data-reconciliation-engineering.md) — reconciliation status feeding alerts.
- [data-processing-performance.md](data-processing-performance.md) — throughput baselines and capacity.
- [data-processing-security.md](data-processing-security.md) — redaction rules.
- [django-integrations: integration-observability-and-operations.md](../../django-integrations/references/integration-observability-and-operations.md)
- [django-core: logging-exceptions.md](../../django-core/references/logging-exceptions.md)
- [django-performance: profiling-and-observability.md](../../django-performance/references/profiling-and-observability.md)
- [django-security-engineering: security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)

## 12. Official documentation and external references

- Python `logging` (filters, LogRecord): https://docs.python.org/3/library/logging.html
- Python logging cookbook (contextual information): https://docs.python.org/3/howto/logging-cookbook.html
- Python `contextvars`: https://docs.python.org/3/library/contextvars.html
- Django logging: https://docs.djangoproject.com/en/5.2/topics/logging/
- Celery signals: https://docs.celeryq.dev/en/stable/userguide/signals.html
- Celery monitoring: https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- PostgreSQL `pg_stat_activity` / monitoring: https://www.postgresql.org/docs/current/monitoring-stats.html
- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/
- OpenTelemetry metrics concepts (attributes/cardinality): https://opentelemetry.io/docs/concepts/signals/metrics/
