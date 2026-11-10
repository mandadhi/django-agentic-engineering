# Reporting Observability and Operations

> **Scope:** How to instrument, monitor, alert on and operate a Django reporting subsystem in production: report-specific metrics, structured lifecycle logs, correlation IDs, audit-trail operations, dashboards, alert rules, cleanup monitoring and operational runbooks. **Not here:** symptom-by-symptom diagnosis of wrong output or broken files ([report-generation-troubleshooting.md](report-generation-troubleshooting.md)), the cleanup task implementation ([background-report-generation.md](background-report-generation.md)), retention and audit *policy* ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)), and general logging/tracing setup ([django-core — logging-exceptions](../../django-core/references/logging-exceptions.md), [django-performance — profiling-and-observability](../../django-performance/references/profiling-and-observability.md)).

## 1. Purpose and scope

A green Celery dashboard says nothing about whether the monthly payroll report reached finance on
time, with the right numbers, to the right people. Reporting observability has to answer
business-level questions:

- Did every scheduled report for every tenant run, succeed and get delivered within its SLA?
- How long do users wait — in the queue *and* during generation — for on-demand reports?
- Which report definitions fail, why (classified error code), and is it getting worse?
- Are workers, memory, database and storage healthy enough for tomorrow's month-end peak?
- Who generated and downloaded which restricted report, and when?
- Are expired artifacts actually being deleted?

This reference covers the signals (metrics, logs, traces, audit records), how they are produced
from the canonical `ReportRun` / `ReportArtifact` / `ReportDelivery` / `ReportDownload` models, the
dashboards and alerts built on them, and the runbooks operators follow. General platform telemetry
(HTTP latency, broker health, PostgreSQL internals) is owned by the sibling skills linked in
section 12.

## 2. Core concepts

| Concept | Meaning in reporting |
|---|---|
| **Run lifecycle** | `pending → running → succeeded / failed / cancelled`, later `expired`. Every transition is an observable event. |
| **Queue wait** | `started_at − requested_at`. Dominated by worker capacity and queue routing, not by report code. |
| **Generation duration** | `finished_at − started_at`. Dominated by query time, rendering CPU and storage upload. |
| **End-to-end latency** | `finished_at − requested_at` for on-demand; delivery time minus scheduled time for scheduled reports. This is what users experience and what SLAs refer to. |
| **Business status vs task status** | Celery may report `SUCCESS` for a task that marked the run `failed` (handled error). Alert on `ReportRun.status`, not on the result backend. |
| **Error code** | A bounded, stable classification stored in `ReportRun.error_code` (`query_timeout`, `row_limit_exceeded`, `render_error`, `storage_error`, `permission_revoked`, `worker_lost`, `cancelled`). Metrics and alerts key on it; free-text messages go to logs only. |
| **Correlation ID** | One ID that ties the HTTP request (or schedule tick), the task execution(s), log lines, the artifact and deliveries together. Stored in `ReportRun.correlation_id`. |
| **Audit trail** | Durable, queryable records of *who* requested, generated, accessed and deleted *what*. Distinct from logs: audit has retention and integrity requirements; logs are operational and may be sampled. |
| **Freshness** | `data_as_of` lag relative to now — critical for reports built on read models or materialized views. |

Three signal types, three purposes:

- **Metrics** — aggregated, cheap, alertable; bounded label cardinality.
- **Logs** — per-event detail for diagnosis; carry `run_id`, `correlation_id`, `tenant_id`.
- **Traces** — span timing inside a run (query → render → upload → notify); optional but valuable
  for slow-report investigations.

## 3. Internal mechanics

### Where signals come from

```text
HTTP request / beat tick
   │ correlation_id (from X-Request-ID or generated)
   ▼
request_report() ── log: report.requested ── metric: report_requests_total
   │ transaction.on_commit → generate_report.apply_async(headers={"correlation_id": ...})
   ▼
broker queue  (queue depth / oldest message age: broker-level metrics)
   ▼
generate_report(run_id)
   ├─ claim → log: report.started      metric: report_queue_wait_seconds
   ├─ query → span: report.query       metric: report_rows (histogram)
   ├─ render→ span: report.render
   ├─ store → span: report.store       metric: report_artifact_bytes
   └─ done  → log: report.succeeded    metric: report_generation_seconds, report_runs_total{status}
                or report.failed{error_code}
   ▼
delivery tasks → log: report.delivery.sent/failed  metric: report_deliveries_total{channel,status}
   ▼
download view → ReportDownload row + log: report.downloaded  metric: report_downloads_total
   ▼
cleanup task → log: report.artifact.expired  metric: report_cleanup_deleted_total, gauge of overdue
```

### Metrics from code vs metrics from the database

There are two complementary sources:

1. **Instrumented in code** (counters/histograms emitted at transitions) — real-time, but lost if a
   worker is killed before emitting, and multi-process aggregation must be configured
   (`prometheus_client` multiprocess mode for Gunicorn/Celery prefork) `[third-party: prometheus_client]`.
2. **Derived from the database** (a periodic collector querying `ReportRun`) — authoritative for
   state-based gauges: number of runs stuck in `running`, oldest `pending` age, overdue schedules,
   artifacts past `expires_at` but not deleted. A worker that died mid-run never emits "failed";
   only the database view reveals it.

Use code instrumentation for rates and latencies; use DB-derived gauges for "things that should
have happened but didn't".

### Celery signals `[third-party: celery]`

`task_prerun`, `task_postrun`, `task_failure`, `task_retry` signals give task-level timing and retry
counts across all tasks. They know nothing about report semantics — attach report-level metrics
inside the task where `report_key` and `format` are known.

### Correlation ID propagation

Celery messages carry headers; custom keys passed with `apply_async(headers={...})` are available
on the task request, but exactly how they are exposed differs between versions
[VERIFY: how custom message headers are exposed on Task.request in Celery 5.x — as attributes or under request.headers].
The robust approach used here: store `correlation_id` on `ReportRun` at request time, and have the
task read it from the row it loads anyway. Headers are then a convenience for logging, not the
source of truth.

## 4. Engineering decisions and trade-offs

| Decision | Options | Recommendation | Trade-off |
|---|---|---|---|
| Metric labels | `report_key`, `format`, `status`, `error_code`, `queue`, `tenant_tier` vs `run_id`, `user_id`, `tenant_id` | Only bounded labels. Never `run_id`/`user_id`; `tenant_id` only if tenant count is small and bounded | Per-tenant detail moves to logs and DB queries |
| Source of state gauges | Code counters vs DB collector | DB collector every 1–5 min for stuck/overdue/backlog gauges | Adds a periodic query; keep it indexed on `(status, requested_at)` |
| Duration buckets | Default histogram buckets vs report-specific | Custom buckets (1 s … 1 h) — default HTTP buckets top out too low | Bucket choice must be decided up front |
| Audit storage | Logs vs DB table vs append-only external store | DB table (`ReportDownload`, run fields) for queryability; ship to an append-only store if regulation requires tamper evidence | DB audit tables grow; need partitioning/retention |
| Tracing | None vs OpenTelemetry spans per stage | Spans for query/render/store when slow reports are a recurring problem `[third-party: opentelemetry-sdk]` | Instrumentation and backend cost |
| Alerting on failures | Every failure vs rate/SLA based | Page on SLA breach and failure *rate*; ticket on individual failures of critical scheduled reports | Single failures of ad-hoc reports are noise |
| Log volume | Per-row logging vs per-stage | Per-stage and per-chunk (every N rows) progress, never per row | Less granular forensic detail |

## 5. Implementation patterns

### Metric catalogue

| Metric | Type | Labels (bounded) | Meaning |
|---|---|---|---|
| `report_requests_total` | Counter | `report_key`, `format`, `trigger` (`user`,`schedule`,`api`) | Requests accepted (after dedupe) |
| `report_dedup_hits_total` | Counter | `report_key` | Requests served by an existing active/recent run |
| `report_runs_total` | Counter | `report_key`, `format`, `status`, `error_code` | Terminal outcomes; `error_code="none"` on success |
| `report_queue_wait_seconds` | Histogram | `queue`, `report_key` | `started_at − requested_at` |
| `report_generation_seconds` | Histogram | `report_key`, `format` | `finished_at − started_at` |
| `report_stage_seconds` | Histogram | `report_key`, `stage` (`query`,`render`,`store`) | Where time goes |
| `report_rows` | Histogram | `report_key` | Rows emitted per run |
| `report_artifact_bytes` | Histogram | `report_key`, `format` | Output size |
| `report_task_retries_total` | Counter | `report_key`, `error_code` | Retries scheduled |
| `report_deliveries_total` | Counter | `channel`, `status` | Delivery outcomes |
| `report_downloads_total` | Counter | `report_key`, `via` | Downloads (audit detail lives in DB) |
| `report_worker_peak_rss_bytes` | Histogram | `report_key`, `format` | Peak memory per run (sampled at end) |
| `report_runs_active` | Gauge (DB) | `status` (`pending`,`running`) | Current backlog |
| `report_oldest_pending_seconds` | Gauge (DB) | `queue` | Age of oldest pending run |
| `report_stale_running` | Gauge (DB) | — | Running with `heartbeat_at` older than threshold |
| `report_schedules_overdue` | Gauge (DB) | `frequency` | Active schedules with `next_run_at < now − grace` |
| `report_artifacts_overdue_deletion` | Gauge (DB) | — | `expires_at < now` and `deleted_at IS NULL` |
| `report_storage_bytes` | Gauge (DB) | `format` | Sum of `size_bytes` for non-deleted artifacts |
| `report_data_freshness_seconds` | Gauge | `source` (read model / MV name) | `now − last refresh` |

Worker CPU/memory utilisation and broker queue depth come from the platform (container metrics,
Celery/broker exporters) — see [django-integrations — integration-observability-and-operations](../../django-integrations/references/integration-observability-and-operations.md).

### Instrumenting the task

```python
# reports/telemetry.py
import logging
import resource  # [stdlib] Unix only
from prometheus_client import Counter, Histogram  # [third-party: prometheus_client]

log = logging.getLogger("reports.lifecycle")

RUNS = Counter("report_runs_total", "Terminal report runs",
               ["report_key", "format", "status", "error_code"])
QUEUE_WAIT = Histogram("report_queue_wait_seconds", "Queue wait", ["queue", "report_key"],
                       buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600))
GEN = Histogram("report_generation_seconds", "Generation time", ["report_key", "format"],
                buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600))

def lifecycle(event: str, run, **extra) -> None:
    log.info(event, extra={
        "event": event, "run_id": str(run.pk), "tenant_id": run.tenant_id,
        "report_key": run.report_key, "report_version": run.report_version,
        "format": run.format, "status": run.status, "attempt": run.attempt,
        "correlation_id": run.correlation_id, **extra,
    })

def peak_rss_bytes() -> int:
    # ru_maxrss is KiB on Linux, bytes on macOS [VERIFY: platform units of ru_maxrss]
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
```

`ru_maxrss` is the process peak, not per-run; with `worker_max_tasks_per_child=1` or a dedicated
heavy queue it approximates per-run peak, otherwise treat it as an upper bound. For precise
per-run allocation tracking in investigations, use `tracemalloc` `[stdlib]` temporarily — it adds
overhead and should not run permanently.

```python
# inside reports/tasks.py generate_report, after the run is claimed
QUEUE_WAIT.labels(queue=REPORT_QUEUE, report_key=run.report_key).observe(
    (run.started_at - run.requested_at).total_seconds())
lifecycle("report.started", run)
...
lifecycle("report.succeeded", run, rows=run.row_count,
          bytes=artifact.size_bytes, duration_s=duration)
RUNS.labels(run.report_key, run.format, "succeeded", "none").inc()
GEN.labels(run.report_key, run.format).observe(duration)
```

### DB-derived gauges collector

```python
# reports/tasks.py — scheduled every minute on a light queue
from celery import shared_task
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

@shared_task(ignore_result=True)
def collect_report_gauges() -> None:
    now = timezone.now()
    active = ReportRun.objects.filter(status__in=["pending", "running"]).aggregate(
        pending=Count("pk", filter=Q(status="pending")),
        running=Count("pk", filter=Q(status="running")),
        oldest_pending=Min("requested_at", filter=Q(status="pending")),
        stale=Count("pk", filter=Q(status="running", heartbeat_at__lt=now - STALE_AFTER)),
    )
    overdue_delete = ReportArtifact.objects.filter(
        expires_at__lt=now, deleted_at__isnull=True).count()
    stored = ReportArtifact.objects.filter(deleted_at__isnull=True).aggregate(
        b=Sum("size_bytes", default=0))["b"]
    # set Gauge values here; in multiprocess mode gauges need an explicit mode
    # [VERIFY: prometheus_client multiprocess gauge modes such as "liveall"/"max"]
```

Indexes that keep this cheap: partial index on `ReportRun(status, requested_at)` where status in
active states; `ReportArtifact(expires_at)` where `deleted_at IS NULL`.

### Structured lifecycle events

| Event | When | Required fields beyond the common set |
|---|---|---|
| `report.requested` | Run row created | `trigger`, `requested_by`, `params_hash`, `dedup_hit` |
| `report.started` | Claim succeeded | `queue_wait_s`, `worker`, `celery_task_id` |
| `report.progress` | Every N rows / stage end | `rows_so_far`, `stage` |
| `report.retrying` | Retry scheduled | `error_code`, `countdown_s` |
| `report.succeeded` | Artifact stored | `rows`, `bytes`, `sha256`, `duration_s`, `data_as_of` |
| `report.failed` | Terminal failure | `error_code`, `exception_type` (message redacted) |
| `report.cancelled` | Cancel observed | `stage` |
| `report.delivery.sent` / `.failed` | Per recipient | `delivery_id`, `channel`, `attempt` (no clear-text email address if policy forbids) |
| `report.downloaded` | Download served | `artifact_id`, `via`, `user_id` |
| `report.artifact.expired` | Cleanup deleted file | `artifact_id`, `age_days`, `bytes` |
| `report.reaped` | Reaper reset/failed a stale run | `previous_status`, `heartbeat_age_s` |

Never log report *contents*, parameter values that contain PII (employee names, free-text search),
or signed URLs. Log `params_hash` and the parameter *keys*; the parameters themselves are on the
`ReportRun` row behind access control.

### Correlation ID propagation

- The view takes the ID from request middleware (`X-Request-ID` from the proxy, or generated) and
  passes it explicitly to `request_report(...)`, which stores it on the run — services do not
  read `request`.
- `request_report` enqueues with `generate_report.apply_async(args=[str(run.pk)],
  headers={"correlation_id": run.correlation_id})` inside `transaction.on_commit`.
- Scheduled runs get a fresh ID per schedule tick; all deliveries of that run share it.
- Store `correlation_id` in artifact storage metadata (object tags/metadata where supported) so a
  file found in a bucket can be traced back [VERIFY: metadata support differs by storage backend and django-storages version].
- A `logging.Filter` that injects the current correlation ID from a `contextvars.ContextVar`
  `[stdlib]` keeps every log line in the task tagged without passing it around.

### Audit trail operations

Audit records for reporting are: `ReportRun` (who requested what, with which parameters and
version), `ReportDownload` (who accessed which artifact, via which channel), `ReportDelivery`
(who was sent what), and cleanup events (when the artifact was destroyed). Operationally:

- Partition or archive `ReportDownload` by month when it grows; keep it out of the hot path.
- Restrict audit tables to append-only through the application (no update/delete code paths);
  stronger guarantees need database grants or an external append-only store.
- Provide an operator query or admin view, not ad-hoc SQL, for "who accessed report X in period Y".
- Audit records outlive artifacts: deleting the file must not delete its audit rows
  (`on_delete=PROTECT`, soft-delete the artifact via `deleted_at`).

### Dashboard layout

```text
Row 1 — Service health (last 24 h)
  [Success rate by report_key]  [p50/p95 end-to-end latency]  [Active pending / running]
Row 2 — Queue and workers
  [Queue wait p95 by queue]  [Oldest pending age]  [Worker CPU / RSS]  [Broker queue depth]
Row 3 — Failures
  [Failures by error_code (stacked)]  [Retries by error_code]  [Stale running count]
Row 4 — Output
  [Rows p95 by report_key]  [Artifact bytes p95]  [Generation time p95 by format]
Row 5 — Scheduled & delivery
  [Schedules overdue]  [Deliveries by status/channel]  [Late scheduled reports vs SLA]
Row 6 — Storage & lifecycle
  [Stored bytes]  [Artifacts overdue for deletion]  [Cleanup deleted/day]  [Data freshness]
```

Add a per-report drill-down driven by logs (filter by `report_key`) and a tenant search driven by
DB queries — tenant is not a metric label.

## 6. Production use cases

- **Month-end payroll** — SLA "payroll register delivered to finance by 06:00 tenant-local on
  the first business day". Alert on `report_schedules_overdue{frequency="monthly"}` and on any
  `failed` run with `report_key="payroll.register"`; page, because a missed payroll report blocks
  payment approval.
- **On-demand sales export** — users click "Export". Watch queue-wait p95; if it exceeds 60 s at
  peak, the heavy queue is starved (see runbook 1).
- **Regulatory submission** — every generation and download must be attributable; operations
  periodically verify that `ReportDownload` rows exist for every signed URL issued (gap = audit
  defect).
- **Storage budget** — a SaaS with per-tenant exports watches `report_storage_bytes` growth and
  `report_artifacts_overdue_deletion`; growth with flat request volume means cleanup is failing.
- **Read-model freshness** — dashboards built on a nightly materialized view alert if
  `report_data_freshness_seconds > 26 h`.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Code-emitted metrics | Real-time, precise latency | Lost on hard kill; multiprocess setup complexity |
| DB-derived gauges | Authoritative for stuck/overdue; survives crashes | Polling delay; extra queries |
| Structured logs | Full per-run context | Volume/cost; must be redacted |
| Tracing | Stage-level timing, cross-service view | Extra dependency; sampling may miss the slow run |
| DB audit tables | Queryable, joins with runs | Growth; not tamper-evident by itself |
| SLA-based alerting | Few, meaningful pages | Requires defined SLAs per report |

## 8. Security considerations

- Telemetry is a leakage channel. No report content, PII, parameter values, signed URLs, storage
  keys containing tenant names, or email addresses in logs or metric labels unless policy allows.
- Error messages stored on `ReportRun.error_message` are shown to users — keep them generic; the
  detailed exception goes to logs with access restricted to operators.
- Tracing attributes such as `db.statement` can capture SQL with literal values; configure the
  instrumentation to omit parameters [VERIFY: OpenTelemetry Django/psycopg instrumentation options for statement sanitisation].
- Dashboards broken down by tenant are themselves sensitive in multi-tenant SaaS; restrict them.
- Audit records must be protected from the users they describe: a user can see their own
  download history but not delete it. See [reporting-security-and-compliance.md](reporting-security-and-compliance.md)
  and [django-security-engineering — security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).

## 9. Performance considerations

- Instrumentation must not scale with rows: emit progress every N rows (e.g. 10 000) or every few
  seconds, never per row.
- Heartbeat writes (`heartbeat_at`) are one `UPDATE` per interval; keep the interval ≥ 15–30 s.
- The gauge collector must use indexed predicates; a sequential scan of `ReportRun` every minute
  on a multi-million-row table is a self-inflicted load problem. Archive terminal runs older than
  the audit horizon to a history table if needed.
- Histogram label sets multiply series: `report_key × format × buckets`. With 200 report keys,
  4 formats and 12 buckets that is ~10 000 series per histogram — acceptable, but don't add more
  dimensions.
- `tracemalloc` slows Python allocation noticeably; enable only for targeted investigation.

## 10. Common mistakes and anti-patterns

- Alerting on Celery task state instead of `ReportRun.status`.
- `run_id`, `user_id` or `tenant_id` as metric labels — cardinality explosion.
- No DB-derived gauges, so crashed runs stay invisible in `running` forever.
- Measuring only generation time; users complain about the queue wait, which nobody graphs.
- Logging the signed download URL "for debugging" — it is a bearer credential until it expires.
- Free-text exception messages as the failure label; dashboards cannot group them.
- Cleanup job with no metric: storage grows silently until the bucket bill or quota alerts.
- Audit rows deleted by `CASCADE` when the artifact is cleaned up.
- One undifferentiated alert per failure, so month-end produces 300 pages and the real problem
  (database timeout) is buried.

## 11. Troubleshooting and validation

### Alert rules (example thresholds — tune to your SLAs)

| Alert | Condition (example) | Severity |
|---|---|---|
| Report SLA breach | Scheduled run for a critical `report_key` not `succeeded` by deadline | Page |
| High failure rate | `failed / terminal > 10 %` over 15 min for any `report_key` with ≥ 5 runs | Page |
| Queue backlog | `report_oldest_pending_seconds > 600` for 10 min | Page (business hours) / ticket |
| Stale running | `report_stale_running > 0` for 15 min | Ticket |
| Retry storm | Retry rate over 10 min > 3× baseline | Ticket |
| Delivery failures | `failed` deliveries > 5 % over 30 min for a channel | Ticket; page if critical schedule |
| Cleanup lagging | `report_artifacts_overdue_deletion > 0` for 24 h | Ticket |
| Storage growth | `report_storage_bytes` +X % week over week with flat requests | Ticket |
| Stale data | `report_data_freshness_seconds` > refresh interval × 1.5 | Ticket / page for financial |
| Overdue schedules | `report_schedules_overdue > 0` for 15 min | Page (dispatcher likely down) |

### Operational runbooks

**1. Queue backlog (oldest pending rising)**
1. Is the report queue being consumed? Check worker count and broker queue depth.
2. Look at `report_generation_seconds` p95: if a single `report_key` jumped, one slow report is
   occupying all workers — check its query plan and recent parameter patterns (e.g. "all time").
3. Check whether workers are alive but stuck (`report_stale_running` rising, CPU idle → waiting on
   DB locks or external I/O).
4. Mitigate: scale the report worker pool (within the DB connection budget), lower per-tenant
   concurrency limits, pause non-critical schedules. Do not raise prefetch.
5. Follow-up: route the offending report to its own queue; add a row limit or pre-aggregation.

**2. Mass generation failure**
1. Group `report.failed` by `error_code`: `query_timeout` → DB; `storage_error` → bucket/credentials;
   `render_error` across many keys → library/system dependency change (fonts, Pango) after a deploy.
2. Correlate the start time with deploys, migrations, credential rotation, MV refresh windows.
3. Stop the bleeding: if retries amplify load (e.g. DB timeouts), pause retries or the queue.
4. After the fix, re-queue failed runs via an admin action/management command that creates *new*
   runs with the same parameters (the active-run constraint prevents duplicates). Notify affected users.

**3. Storage growth / cleanup failing**
1. Check `report_artifacts_overdue_deletion` and `report.artifact.expired` log volume.
2. Typical causes: cleanup task not scheduled after a beat change, permission denied on delete
   (bucket policy), one failing object aborting the whole batch, `expires_at` not set for a
   new report type.
3. Fix the cause, then run cleanup in bounded batches; verify `deleted_at` and the object are both
   gone. Reconcile storage prefix listing vs `ReportArtifact` rows to find orphans (objects with no
   row) and ghosts (rows with no object).

**4. Delivery provider outage**
1. `report_deliveries_total{status="failed"}` spikes for one channel; provider status confirms.
2. Deliveries retry with backoff and idempotency keys; do not regenerate reports — the artifact
   already exists.
3. If the outage outlasts the link lifetime, issue fresh links on resend.
4. After recovery, verify every delivery for the window reached `sent`; idempotency keys prevent
   double emails. See [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md).

**5. Stuck RUNNING**
1. Find runs with `status=running` and `heartbeat_at` older than the threshold.
2. Check worker logs for the `celery_task_id`: OOM kill, deploy restart, `time_limit` hard kill.
3. The reaper should move them to `pending` (if `attempt < max`) or `failed` with
   `error_code="worker_lost"`. If no reaper exists, that is the defect — see
   [background-report-generation.md](background-report-generation.md).
4. If OOM: see [report-generation-performance.md](report-generation-performance.md).

### Validating the observability itself

- Kill a worker mid-run in staging: does `report_stale_running` rise and the reaper act?
- Disable the cleanup schedule in staging: does the overdue-deletion alert fire?
- Search a day of logs for `@`, `X-Amz-Signature`, `signature=` and known PII patterns — expect zero hits.
- Confirm every terminal run has exactly one `report.succeeded|failed|cancelled` event.

## 12. Related references and official documentation

- [background-report-generation.md](background-report-generation.md) — heartbeat, reaper, cleanup task
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) — delivery status and SLAs
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — audit and retention policy
- [report-generation-performance.md](report-generation-performance.md) — capacity and memory
- [report-generation-troubleshooting.md](report-generation-troubleshooting.md) — symptom catalogue
- [django-core — logging-exceptions](../../django-core/references/logging-exceptions.md)
- [django-performance — profiling-and-observability](../../django-performance/references/profiling-and-observability.md)
- [django-performance — background-task-performance](../../django-performance/references/background-task-performance.md)
- [django-integrations — integration-observability-and-operations](../../django-integrations/references/integration-observability-and-operations.md)
- [django-data-processing — data-processing-observability](../../django-data-processing/references/data-processing-observability.md)
- [django-security-engineering — security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md)
- <https://docs.djangoproject.com/en/5.2/topics/logging/>
- <https://docs.python.org/3/library/logging.html>
- <https://docs.python.org/3/library/contextvars.html>
- <https://docs.python.org/3/library/tracemalloc.html>
- <https://docs.celeryq.dev/en/stable/userguide/signals.html>
- <https://docs.celeryq.dev/en/stable/userguide/monitoring.html>
- <https://prometheus.github.io/client_python/>
- <https://opentelemetry.io/docs/languages/python/>
