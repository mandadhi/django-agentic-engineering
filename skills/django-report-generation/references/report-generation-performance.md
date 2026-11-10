# Report Generation Performance

> **Scope:** Performance engineering specific to report generation: budgets, sync vs async cost, which formats and libraries can truly stream and which must buffer, memory and CPU cost of rendering, queues and worker concurrency per cost class, database connections and timeouts, report caching and artifact reuse, pre-aggregation as a performance decision, benchmarking, backpressure and resource exhaustion. **Not here:** general profiling, worker sizing theory and caching mechanics → [django-performance](../../django-performance/SKILL.md); report query design → [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md); task reliability → [background-report-generation.md](background-report-generation.md); metrics and alerts → [reporting-observability-and-operations.md](reporting-observability-and-operations.md).

## 1. Purpose and scope

Reports are the most uneven workload in a typical Django system: the same endpoint may produce 20
rows or 2 million, a one-page PDF or a 900-page statement. Performance work for reports is
therefore about **bounding cost per run** and **isolating expensive runs** from interactive
traffic, more than about shaving milliseconds. This reference gives the decision rules: where a
report runs, how much memory it may use, whether its format can stream, how many can run at once,
and when to precompute or reuse.

## 2. Core concepts

| Concept | Meaning for reports |
|---|---|
| Cost class | Bucket by expected rows × format cost: `small` (sync), `medium` (async, default queue), `bulk` (async, dedicated queue/workers) |
| Time to first byte vs time to complete | Streaming CSV improves TTFB; total DB and render time is unchanged |
| Buffering | Format/library must hold the whole document (or its index) before the file is valid |
| Peak RSS per run | Memory high-water mark of a worker process for one report — the number that sizes workers |
| Queue wait | `started_at − requested_at`; the user-visible latency component that more workers fix |
| Artifact reuse | Serving an already-generated artifact for identical (tenant, key, version, params_hash, data_as_of) |
| Backpressure | Refusing or delaying new runs when queues or tenants exceed limits instead of degrading everyone |

Cost drivers, in typical order of magnitude for large reports: database scan/aggregation → PDF
layout/rendering → chart rendering → XLSX styling/zip compression → CSV serialization.
Confirm per report by measuring — the order changes with query shape.

## 3. Internal mechanics

### Streaming vs buffering by format

| Format / library | Can produce bytes incrementally? | Memory profile | HTTP-streamable in practice | Notes |
|---|---|---|---|---|
| CSV via `csv.writer` `[stdlib]` | Yes, row by row | O(row) | Yes (`StreamingHttpResponse`) | Only format that streams end-to-end trivially |
| JSON Lines `[stdlib]` | Yes, one object per line | O(row) | Yes | Prefer over one big JSON array for large outputs |
| JSON array | Possible with hand-written `[`, `,`, `]` framing | O(row) if hand-streamed; O(n) with `json.dumps(list)` | Yes if hand-streamed | Error mid-stream leaves invalid JSON |
| XML via `xml.sax.saxutils.XMLGenerator` `[stdlib]` | Yes | O(depth) | Yes | `ElementTree` builds the full tree → O(n) |
| XLSX — openpyxl `write_only=True` `[third-party: openpyxl]` | Rows go to temporary files; the zip is assembled at `save()` | Roughly constant for rows; disk usage grows | No — the file is not valid until `save()`; write to temp file/storage, then serve | [VERIFY: openpyxl write-only internals — temp files per worksheet] |
| XLSX — XlsxWriter `constant_memory` `[third-party: XlsxWriter]` | Rows flushed per row to temp files; zip at `close()` | Roughly constant; strict row order | No, same reason | `in_memory=True` holds everything in RAM instead [VERIFY: option interaction] |
| XLSX — openpyxl normal mode | No | O(cells), large per-cell overhead | No | Only for small/medium workbooks or editing templates |
| PDF — WeasyPrint `[third-party: weasyprint]` | No | Full DOM + layout tree in memory; grows with page count | No | Long tables are expensive; split very large documents [VERIFY: memory scaling for 1000+ page documents] |
| PDF — ReportLab platypus `[third-party: reportlab]` | Pages are written as built, but the story (flowables list) is usually built in memory first | O(story) | No (file finalised at end) | Generate flowables lazily where possible; large `Table` objects are costly to split |
| PDF — ReportLab canvas | Page-by-page drawing | Low | No (cross-reference table written at end) | Lowest memory, most code |
| DOCX — python-docx `[third-party: python-docx]` | No | Full XML DOM | No | Not suitable for very large documents |
| Charts — Matplotlib/Plotly | N/A (one image each) | Per figure; leaks if figures not released | N/A | See [charts-and-data-visualization.md](charts-and-data-visualization.md) |

Rule: **only CSV, JSONL and SAX-generated XML are candidates for synchronous HTTP streaming.**
Every zip-based or paginated format is generated to a temp file or storage and served afterwards.

### Where the time goes in a sync request

```text
client ──► proxy (timeout) ──► app server worker (timeout) ──► Django view
                                                           ├── DB query (statement_timeout)
                                                           ├── Python transform
                                                           └── render / write
```

The **smallest** timeout in that chain is the real budget. A Gunicorn sync worker's `timeout`
(default 30 s [VERIFY: current Gunicorn default]) kills the worker mid-stream; a proxy read timeout
returns 504 while the worker keeps working. Streaming does not extend these budgets unless the
proxy and server are configured for long-lived responses (`X-Accel-Buffering: no` for nginx).

### Iteration and memory

- `QuerySet.iterator(chunk_size=…)` `[Django]` avoids the result cache; on PostgreSQL it uses a
  server-side cursor, which breaks under transaction-pooling PgBouncer unless
  `DISABLE_SERVER_SIDE_CURSORS = True` (then the whole result is fetched by the driver — memory
  returns to O(n)). Use keyset pagination when server-side cursors are unavailable
  ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)).
- `values_list()` tuples are far smaller than model instances; reports rarely need instances.
- `prefetch_related` with `iterator()` prefetches per chunk and requires `chunk_size`
  (Django ≥ 5.0 raises without it) [VERIFY: exact Django version of the error].

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Escalate when | Trade-off |
|---|---|---|---|
| Sync vs async | Sync only if p95 < ~2 s and output small (`spec.max_sync_rows`) | Above budget, or PDF/XLSX beyond a few thousand rows → async | Async adds queue latency, status UI, storage |
| Streaming CSV vs async CSV | Stream when the query itself is fast and rows ≤ low millions | Slow query (long time to first row) or very large → async + stored file | Streaming ties up a web worker and a DB connection for the whole duration |
| Queue layout | `reports_default` + `reports_bulk` | Per-tenant fairness issues → per-tenant concurrency caps | More queues = more worker pools to size |
| Precompute vs on demand | On demand with good indexes | Same expensive aggregation requested repeatedly, data changes slowly → pre-aggregated table or MV | Staleness, refresh jobs, storage |
| Artifact reuse | Reuse for identical key + params + data_as_of when classification allows | — | Must never reuse across users with different row scopes |
| PDF engine | WeasyPrint for template-driven layouts | Hundreds of pages, tight CPU budget → ReportLab | Developer productivity vs runtime cost |
| XLSX engine | openpyxl `write_only` / XlsxWriter `constant_memory` for large | Rich formatting on moderate data → normal mode | Memory vs features |

Every recommendation should state the bottleneck, how it was measured, and the validation —
the justification format from [django-performance](../../django-performance/SKILL.md).

## 5. Implementation patterns

### Performance budgets per cost class

| Class | Example | Execution | Budget (p95) | Memory cap per run | Admission rule |
|---|---|---|---|---|---|
| small | Invoice PDF, ≤ 5 000-row CSV | Sync | ≤ 2 s total | ≤ 100 MB | Reject sync if estimated rows > `max_sync_rows` |
| medium | Monthly sales XLSX, 50k rows | Async `reports_default` | ≤ 2 min run, ≤ 1 min queue wait | ≤ 512 MB | Per-user ≤ 3 active runs |
| bulk | Year of ledger entries CSV, 5M rows; 500-page statement | Async `reports_bulk` | ≤ 30 min run | ≤ 2 GB | Per-tenant ≤ 2 active bulk runs |

Numbers are illustrative starting points; derive real budgets from SLAs
([reporting-engineering-fundamentals.md](reporting-engineering-fundamentals.md)) and measured runs.

### Estimating before choosing the path

```python
def choose_execution(spec, qs, fmt: str) -> str:
    # Exact count is fine for moderate tables; measure its own cost on very large ones.
    n = qs.count()                                   # [Django]
    if n <= spec.max_sync_rows and fmt in ("csv", "xlsx"):
        return "sync"
    return "bulk" if n > 1_000_000 else "default"
```

On very large tables an exact `count()` can itself be slow; use date-range bounds or
`QuerySet.explain()`-based estimates when measured necessary.

### Routing and worker settings (Celery)

```python
# settings.py  [third-party: celery]
CELERY_TASK_ROUTES = {
    "reports.tasks.generate_report": {"queue": "reports_default"},
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # long tasks: don't reserve work you can't start
```

```bash
# Separate pools: low concurrency for memory-heavy rendering, recycle to cap fragmentation.
celery -A config worker -Q reports_bulk --concurrency=2 --max-memory-per-child=2000000
celery -A config worker -Q reports_default --concurrency=4 --max-tasks-per-child=50
```

`--max-memory-per-child` is in KiB and is checked after a task completes, so it is a recycle
policy, not a hard cap — the container memory limit is the hard cap [VERIFY: Celery worker docs].
Size: `concurrency × peak RSS per run + baseline ≤ container limit` with headroom.
Detail: [django-performance — background-task-performance](../../django-performance/references/background-task-performance.md),
[django-performance — concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md).

### Database connections and timeouts

```python
DATABASES["reports"] = {
    **DATABASES["default"],
    "HOST": env("REPORTS_DB_HOST"),            # read replica or reporting DB
    "OPTIONS": {"options": "-c statement_timeout=600000"},   # 10 min, [PostgreSQL]
    "CONN_MAX_AGE": 0,
}
```

- Every concurrent report holds a connection for its whole run (longer when streaming). Total
  `web workers + report workers × concurrency` must fit `max_connections` / pooler limits.
- A long statement on the primary holds back vacuum and can bloat hot tables; prefer a replica or
  a reporting database for bulk reports and accept replication lag explicitly in `data_as_of`.
- Interactive endpoints keep a short `statement_timeout`; reports get a longer one on their own alias.

### Report caching and artifact reuse

```python
existing = (ReportRun.objects
            .filter(tenant=run.tenant, report_key=run.report_key, report_version=run.report_version,
                    params_hash=run.params_hash, format=run.format,
                    status=ReportRun.Status.SUCCEEDED, data_as_of__gte=freshness_cutoff,
                    artifact__deleted_at__isnull=True)
            .order_by("-finished_at").first())
```

- Include `report_version` (output semantics), `data_as_of` freshness, and user scope when the
  selector is row-scoped (the `params_hash` includes scope — see
  [reporting-architecture-and-design.md](reporting-architecture-and-design.md)).
- For closed periods (last month's payroll after approval) the result is immutable — reuse
  indefinitely within retention.
- In-memory caches (`[Django]` cache framework) suit small aggregated *data* for dashboards, not
  file artifacts; files belong in storage.

### Pre-aggregation

Use a summary table or materialized view when the same expensive aggregation backs many runs.
Data design is in [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md);
the perf decision rule: precompute when `(runs per refresh interval × query cost) ≫ refresh cost`
and staleness up to the refresh interval is acceptable to the report's SLA. `REFRESH MATERIALIZED
VIEW CONCURRENTLY` `[PostgreSQL]` avoids blocking readers but needs a unique index and is slower
than a plain refresh.

### Backpressure and admission control

```python
from django.db.models import Q

ACTIVE = Q(status__in=[ReportRun.Status.PENDING, ReportRun.Status.RUNNING])

def admit(*, user, tenant, spec) -> None:
    if ReportRun.objects.filter(ACTIVE, requested_by=user).count() >= 3:
        raise TooManyActiveReports()      # → HTTP 429 with Retry-After
    if ReportRun.objects.filter(ACTIVE, tenant=tenant).count() >= tenant_report_cap(tenant):
        raise TooManyActiveReports()
    if queue_depth("reports_bulk") > settings.REPORTS_BULK_MAX_QUEUE:
        raise ReportingDegraded()         # → HTTP 503; scheduled runs deferred, not dropped
```

- Combined with the partial unique constraint on active identical runs, duplicates are refused
  before they queue ([background-report-generation.md](background-report-generation.md)).
- Throttle the request endpoint with DRF throttles ([django-rest-framework — throttling-and-rate-limiting](../../django-rest-framework/references/throttling-and-rate-limiting.md)).
- Scheduled runs are spread (jittered start times, per-tenant staggering) to avoid a midnight
  thundering herd ([scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md)).

### Image, font and chart cost

- Embed fonts once, subset where the library does so (WeasyPrint subsets by default [VERIFY: font subsetting default and options in the installed WeasyPrint version]).
- Downscale images to output DPI before embedding; a 12-megapixel logo repeated per page bloats
  PDFs and render time.
- Render each chart once per run and reuse the image bytes across formats; prefer SVG in
  PDFs when the renderer supports it, PNG at a fixed DPI for XLSX/DOCX.
- Matplotlib figure creation is CPU-heavy (tens to hundreds of ms); hundreds of charts per report
  belong in the bulk class.

### Benchmarking

1. Build a production-sized fixture dataset (row counts, distribution of customers per tenant,
   long text fields, Unicode) — see [report-generation-testing.md](report-generation-testing.md).
2. Measure per stage: query time, transform time, render time, upload time, peak RSS
   (`tracemalloc` `[stdlib]` for Python allocations; container metrics for RSS, which includes C
   libraries like Pango/Cairo that `tracemalloc` cannot see).
3. Record rows, output bytes, pages; compute ms/1k rows and MB/1k rows per format.
4. Re-run after changes on the same data and hardware; keep the numbers with the PR.
5. Turn the result into budgets and a regression check (e.g. nightly benchmark job with a
   threshold). Method: [django-performance — performance-testing-and-benchmarking](../../django-performance/references/performance-testing-and-benchmarking.md).

## 6. Production use cases

| Case | Bottleneck found | Fix |
|---|---|---|
| Orders CSV export 504s at 300k rows | Time to first row: unindexed date filter + sort | Index `(tenant_id, created_at)`; stream; later async for > 1M rows |
| Monthly statement PDFs OOM-kill workers | WeasyPrint rendering 800-page documents with 4 concurrent tasks | `reports_bulk` concurrency 1–2, split statements per account, memory limit sized from measured peak |
| Payroll XLSX slow (6 min) | openpyxl normal mode with per-cell style objects | `write_only` with shared named styles; 40 s |
| Dashboard exports hammer primary DB at 09:00 | Hundreds of identical requests | Artifact reuse by params_hash + data_as_of; nightly pre-aggregation |
| One tenant's bulk export delays all others | Single FIFO queue | Per-tenant active caps + separate bulk queue |
| Workers slowly grow memory over a day | Matplotlib figures not closed via pyplot | OO `Figure` API; `max-tasks-per-child` as mitigation |

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Streaming CSV | Immediate download, O(row) memory, no storage | Holds web worker + DB connection; mid-stream errors produce truncated files with 200 status |
| Async + stored artifact | Isolation, retries, reuse, any format | Queue latency, storage cost, status UI, cleanup |
| Dedicated bulk queue | Protects interactive and medium reports | Idle capacity; more pools to operate |
| Pre-aggregation / MV | Fast repeated reads | Staleness, refresh load, schema to maintain |
| Artifact reuse | Removes repeated work entirely | Correct cache key is security-critical |
| Admission control | Predictable latency under load | Users see 429/503; needs clear UX |

## 8. Security considerations

- Expensive reports are a DoS vector: bound parameters (date range, row caps), cap concurrency per
  user/tenant, throttle request endpoints.
- Reuse keys must include scope; a fast cache hit that returns another user's rows is a data leak
  ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)).
- Temp-file use for buffered formats must be private per worker and cleaned on failure.
- Long `statement_timeout` only on the reporting alias, never globally.
- Disk exhaustion from temp files is both an availability and a data-remanence issue — cap temp
  dir size and sweep on start.

## 9. Performance considerations

Key rules for this subsystem:

- Choose the execution mode from estimated size and format **before** running the query.
- Stream only CSV/JSONL/SAX-XML; buffer everything else to a temp file, not to `bytes` in RAM.
- One heavy report = one DB connection for its duration; size pools accordingly.
- Measure peak RSS per format and row count; size `concurrency` from it.
- Release memory between chunks: no lists of all rows, close figures, close workbooks/files.
- Compression: XLSX/DOCX are already zip; gzip of CSV helps transfer size but costs CPU and
  breaks Range/resume — let the web server/CDN negotiate `Content-Encoding` where appropriate
  ([django-performance — http-and-static-delivery-performance](../../django-performance/references/http-and-static-delivery-performance.md)).

## 10. Common mistakes and anti-patterns

- `list(qs)` or a DataFrame of the full dataset "to make it easier".
- Building XLSX/PDF into `io.BytesIO` for large outputs and then `getvalue()` (two copies in RAM).
- Treating `StreamingHttpResponse` as a fix for slow queries.
- Same Celery queue and concurrency for 10-row CSVs and 800-page PDFs.
- Default `worker_prefetch_multiplier` with long tasks → tasks reserved by busy workers while others idle.
- Raising container memory instead of measuring per-run peak.
- Exact `COUNT(*)` on a 100M-row table before every export.
- Caching report files in Redis.
- Regenerating identical immutable reports (closed periods) on every request.
- Benchmarking on 1 000-row fixtures and extrapolating linearly.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Diagnose | Fix |
|---|---|---|---|
| 502/504 on export | Proxy or Gunicorn timeout < generation time | Compare timeouts in chain with measured duration | Async path; or stream with configured timeouts |
| Truncated CSV with 200 status | Exception or worker kill mid-stream | Worker logs, `SIGKILL`/timeout entries | Async for large; validate row counts; trailer row if consumers support it |
| Worker OOM-killed | Buffered format, concurrency too high, leaks | Peak RSS per run vs limit; `tracemalloc` snapshots between chunks | Lower concurrency, write-only modes, close figures, split documents |
| Report queue wait grows daily | Arrival rate > capacity at peak | Queue wait metric vs run duration × concurrency | Add bulk workers, stagger schedules, reuse artifacts, precompute |
| DB CPU spikes at schedule times | Thundering herd | Query stats by time | Jitter schedules, pre-aggregate, replica |
| "Too many connections" | Report workers × concurrency + web exceed limits | Count connections by application_name | Pooler (session mode for server-side cursors), lower concurrency |
| Memory fine in tests, OOM in prod | Unrealistic fixture size or C-library memory invisible to tracemalloc | Container RSS metrics on prod-sized data | Benchmark with realistic data |

Validation checklist:

- [ ] Each report spec has a cost class, `max_sync_rows`, and a measured peak RSS and duration.
- [ ] Only streamable formats use `StreamingHttpResponse`; all others write to temp file/storage.
- [ ] Bulk reports run on a separate queue with concurrency derived from memory measurements.
- [ ] Reporting DB alias with its own `statement_timeout`; connection budget documented.
- [ ] Artifact reuse key includes tenant, key, version, params hash (with scope), format and freshness.
- [ ] Per-user/per-tenant caps and throttles return 429/503 with clear messages.
- [ ] Benchmarks on production-sized data stored with the change; regression guard in CI or nightly.

## 12. Related references and official documentation

- In this skill: [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) ·
  [background-report-generation.md](background-report-generation.md) ·
  [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) ·
  [excel-report-generation.md](excel-report-generation.md) ·
  [pdf-report-generation.md](pdf-report-generation.md) ·
  [charts-and-data-visualization.md](charts-and-data-visualization.md) ·
  [reporting-observability-and-operations.md](reporting-observability-and-operations.md) ·
  [report-generation-troubleshooting.md](report-generation-troubleshooting.md)
- django-performance: [file-and-data-processing-performance](../../django-performance/references/file-and-data-processing-performance.md) ·
  [memory-and-resource-management](../../django-performance/references/memory-and-resource-management.md) ·
  [background-task-performance](../../django-performance/references/background-task-performance.md) ·
  [concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md) ·
  [database-performance](../../django-performance/references/database-performance.md) ·
  [caching-engineering](../../django-performance/references/caching-engineering.md) ·
  [performance-architecture-decisions](../../django-performance/references/performance-architecture-decisions.md) ·
  [performance-testing-and-benchmarking](../../django-performance/references/performance-testing-and-benchmarking.md)
- Other skills: [django-orm — query-optimization](../../django-orm/references/query-optimization.md) ·
  [django-data-processing — large-dataset-processing](../../django-data-processing/references/large-dataset-processing.md) ·
  [django-data-processing — data-processing-performance](../../django-data-processing/references/data-processing-performance.md) ·
  [postgresql-engineering](../../postgresql-engineering/SKILL.md)
- Official documentation:
  <https://docs.djangoproject.com/en/5.2/topics/db/optimization/> ·
  <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator> ·
  <https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects> ·
  <https://docs.djangoproject.com/en/5.2/howto/outputting-csv/> ·
  <https://openpyxl.readthedocs.io/en/stable/optimized.html> ·
  <https://xlsxwriter.readthedocs.io/working_with_memory.html> ·
  <https://doc.courtbouillon.org/weasyprint/stable/> ·
  <https://docs.celeryq.dev/en/stable/userguide/workers.html> ·
  <https://www.postgresql.org/docs/16/runtime-config-client.html> ·
  <https://docs.python.org/3/library/tracemalloc.html>
