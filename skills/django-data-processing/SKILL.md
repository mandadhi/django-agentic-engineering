---
name: django-data-processing
description: >-
  Django data processing engineering specialist — processing architectures (batch, stream,
  near-real-time, ETL/ELT), data ingestion (APIs, files, databases, webhooks, cloud storage,
  CDC, checkpoints, watermarks), file processing (CSV, Excel, JSON/JSONL, XML, text, Parquet,
  Avro, ORC, ZIP, PDF), validation and cleansing, transformation and mapping, large-dataset and
  streaming processing, pipelines, background data jobs with Celery or management commands,
  multi-tenant import/export systems, bulk loading and upserts, reconciliation, reliability and
  recovery, processing security, performance, testing and observability. Load when building,
  reviewing or troubleshooting an import, export, ingestion job, ETL pipeline, data sync,
  reconciliation, bulk data operation or document-extraction pipeline in Django/Python.
project: django-agentic-engineering
abstract: >-
  Data is untrusted until validated, processed in bounded streams, loaded idempotently, and
  proven correct by reconciliation — not by a green job status. A concise workflow and navigation
  guide here; nineteen focused references under references/ loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, PostgreSQL, Celery, Redis, RabbitMQ, pandas, Polars, PyArrow, openpyxl, lxml, PyMuPDF]
domain: [Software Engineering, Data]
---

# Django Data Processing Engineering Specialist

Engineering judgement for systems that move and reshape data: get it in, prove it is valid,
transform it deterministically, persist it safely, prove the result matches the source, and
recover cleanly when anything fails. Baseline: **Python 3.12+, Django 5.2 LTS, DRF 3.16,
PostgreSQL, Celery 5.x**; Django 6.0 only where qualified "(Django ≥ 6.0)"; uncertain or
version-dependent claims are marked `VERIFY:`. Every API is labelled `[stdlib]`, `[Django]`,
`[DRF]` or `[third-party: package]` — check the lock file; **never assume pandas, Polars,
Celery, a broker or a distributed architecture is needed or installed.**

## Activate when

- Building an upload/import, bulk export, scheduled ingestion, ETL/ELT pipeline or data sync.
- Parsing or generating CSV, Excel, JSON/JSONL, XML, Parquet/Avro/ORC, ZIP or PDF data.
- Designing validation, rejected-record handling, field/code mapping or transformation logic.
- Processing datasets too large for memory or a single request; designing batch/chunk/parallel work.
- Writing a Celery task or management command that processes data; tracking job progress/status.
- Reconciling source and target (payroll, payments, inventory, attendance, customers).
- Troubleshooting duplicates, missing records, stuck jobs, OOM workers, slow loads or bad data.

## Data processing engineering principles

1. **Simplest architecture that meets the requirement.** Request → management command → single
   Celery queue → multi-stage pipeline → streaming platform; each step up needs a measured reason.
2. **Inputs are untrusted.** Bound size, sniff type, parse safely (no XXE, no zip bombs), validate
   before use, and neutralise formula injection on export.
3. **Stream, never load whole.** Generators, chunked readers and keyset iteration with explicit
   memory, row, byte and runtime limits.
4. **Validate at the boundary, constrain in the database.** App validation produces actionable
   errors; DB constraints are the final guard. Decide up front: reject the dataset or isolate bad records.
5. **Transformations are pure, versioned and testable.** Mapping definitions are data with a
   version recorded on every output for lineage.
6. **Every stage is idempotent and resumable.** Natural keys, upserts, run ids and checkpoints;
   delivery is at-least-once — exactly-once is achieved only as *effectively-once* through idempotent sinks.
7. **Transactions are sized deliberately.** All-or-nothing only when small enough; otherwise
   committed batches plus a recovery story.
8. **Success ≠ correctness.** Reconcile counts, totals and keys against the source; surface exceptions.
9. **Classify failures.** Retry transient ones with backoff; quarantine permanent/data errors;
   never retry validation errors.
10. **Observable and auditable.** Correlation ids, per-job counters, structured logs, alerts on
    stuck jobs, rejection spikes and watermark lag; who ran what, with which mapping version.

## Processing workflow

1. **Understand business data and requirements** — entities, owners, consumers, correctness rules
   → `data-processing-fundamentals.md`.
2. **Identify source and destination systems** — formats, access, system of record
   → `data-ingestion-engineering.md`.
3. **Quantify volume, frequency, latency** — rows/bytes per run, peak, SLA → fundamentals (architecture selection).
4. **Define schemas and data contracts** — required fields, types, keys, error codes
   → `data-validation-and-cleansing.md`.
5. **Design ingestion and validation stages** → ingestion, file references, validation.
6. **Design transformation and mapping** → `data-transformation-and-mapping.md`.
7. **Select a persistence strategy** — staging, upsert, COPY, transaction size
   → `database-loading-and-persistence.md`.
8. **Define batching, concurrency and resource limits** → `large-dataset-processing.md`,
   `data-processing-performance.md`.
9. **Design idempotency and recovery** → `processing-reliability-and-recovery.md`,
   `django-background-processing.md`.
10. **Add reconciliation and auditability** → `data-reconciliation-engineering.md`.
11. **Implement monitoring and error reporting** → `data-processing-observability.md`.
12. **Validate correctness, reliability and performance** → `data-processing-testing.md` and the checklist below.

Inspect only the affected models, services, tasks, parsers and the relevant settings block.
Load **one to three references per task** — never all.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [data-processing-fundamentals.md](references/data-processing-fundamentals.md) | Lifecycle, batch/stream/real-time, sync vs async, ETL vs ELT concepts, lineage/provenance, quality dimensions, delivery guarantees and exactly-once limits, architecture selection |
| [data-ingestion-engineering.md](references/data-ingestion-engineering.md) | API/file/DB/webhook/cloud-storage ingestion, pull vs push, full vs incremental, CDC, connectors, pagination, rate limits, checkpoints, watermarks, ingest dedupe, raw landing, ingestion audit |
| [file-processing-engineering.md](references/file-processing-engineering.md) | CSV, XLS/XLSX, JSON, JSONL, XML, plain text: structure, encoding, header detection, streaming parsers, format errors, library choice |
| [columnar-archive-and-document-files.md](references/columnar-archive-and-document-files.md) | Parquet, Avro, ORC, ZIP archives, PDF: structure, streaming read/write, schema, extraction, limits |
| [data-validation-and-cleansing.md](references/data-validation-and-cleansing.md) | Validation layers, nulls/missing values, normalisation, cleansing, quality scoring, validation reports, rejected records, reject-dataset vs isolate-records policy |
| [data-transformation-and-mapping.md](references/data-transformation-and-mapping.md) | Field/schema/code/enum mapping, types, dates/timezones, Decimal/currency, units, nested JSON, flattening, enrichment, derived fields, versioned mapping definitions |
| [large-dataset-processing.md](references/large-dataset-processing.md) | Generators, chunking, streaming I/O, `iterator()`/bulk limits, keyset iteration, pandas chunks, Polars lazy, PyArrow batches, multiprocessing/threads/async, backpressure, limits |
| [etl-and-elt-pipelines.md](references/etl-and-elt-pipelines.md) | Stage contracts, orchestration, dependencies, scheduling, pipeline state, incremental/full/delta, watermarks, schema evolution, pipeline recovery, end-to-end example |
| [django-background-processing.md](references/django-background-processing.md) | Management commands vs Celery, data-task design, decomposition, retries, time limits, routing, periodic jobs, progress, cancellation, `ProcessingJob` status |
| [data-import-and-export-systems.md](references/data-import-and-export-systems.md) | Multi-tenant import/export architecture: upload, preview, column mapping, confirm, background import, exports, download links, history, error reports, resume, reprocess |
| [database-loading-and-persistence.md](references/database-loading-and-persistence.md) | Staging tables, bulk insert/update, UPSERT/MERGE/COPY, transaction boundaries, idempotent loading, FK resolution, rollback, staging→production promotion |
| [data-reconciliation-engineering.md](references/data-reconciliation-engineering.md) | Count/field/aggregate/financial reconciliation, missing/duplicate/mismatch detection, tolerances, hashing, exceptions, reprocessing |
| [processing-reliability-and-recovery.md](references/processing-reliability-and-recovery.md) | Failure isolation, error taxonomy, poison records, data DLQ, checkpoint resume, compensation, duplicate prevention, recovery runbooks |
| [data-processing-security.md](references/data-processing-security.md) | File type/MIME/size checks, zip bombs, path traversal, XXE, malicious documents, CSV injection, temp files, protected downloads, masking, retention, tenant isolation |
| [data-processing-performance.md](references/data-processing-performance.md) | Throughput/latency/memory/CPU/IO, batch and chunk sizing, concurrency, bottlenecks, profiling, benchmarking, capacity planning |
| [data-processing-testing.md](references/data-processing-testing.md) | Transformation, parser, import/export, malformed/large-file, partial-failure, retry, idempotency, reconciliation and property-based tests; fixtures |
| [data-processing-observability.md](references/data-processing-observability.md) | Job/pipeline metrics, structured logs, correlation ids, audit records, dashboards, alerts, runbooks |
| [production-architectures-imports-and-pipelines.md](references/production-architectures-imports-and-pipelines.md) | Blueprints: large CSV/Excel import, multi-source ingestion, ETL platform, scheduled processing platform |
| [production-architectures-sync-reconciliation-and-documents.md](references/production-architectures-sync-reconciliation-and-documents.md) | Blueprints: financial reconciliation, bulk export service, external system sync, document extraction pipeline |

## Task-based loading

| Task | Load |
|---|---|
| Choose an architecture (request vs command vs Celery vs pipeline vs stream) | data-processing-fundamentals.md (+ a production-architectures reference) |
| Build a CSV/Excel upload-and-import feature | data-import-and-export-systems.md, file-processing-engineering.md, data-validation-and-cleansing.md |
| Pull data from a paginated external API on a schedule | data-ingestion-engineering.md, etl-and-elt-pipelines.md |
| Parse XML/JSON/Parquet/ZIP/PDF input | file-processing-engineering.md or columnar-archive-and-document-files.md (+ data-processing-security.md) |
| Define validation rules and rejected-row reporting | data-validation-and-cleansing.md |
| Map fields/codes between systems (HRIS → payroll, ERP → CRM) | data-transformation-and-mapping.md |
| Process millions of rows without OOM | large-dataset-processing.md, data-processing-performance.md |
| Fast, idempotent bulk load / upsert | database-loading-and-persistence.md (+ django-orm bulk-operations-and-batching) |
| Write or review a Celery data task / management command | django-background-processing.md, processing-reliability-and-recovery.md |
| Large export with download link | data-import-and-export-systems.md, large-dataset-processing.md |
| Payroll/payment/inventory reconciliation | data-reconciliation-engineering.md |
| Job failed halfway / duplicates / poison rows / replay | processing-reliability-and-recovery.md (+ data-processing-observability.md) |
| Security review of an upload or processing pipeline | data-processing-security.md (+ django-security-engineering) |
| Slow pipeline or sizing workers/batches | data-processing-performance.md, large-dataset-processing.md |
| Test an importer, transformer or pipeline | data-processing-testing.md |
| Dashboards, alerts, structured logs for jobs | data-processing-observability.md |
| Design a full platform (ETL, multi-source, scheduled, sync, documents) | the matching production-architectures reference, then its linked owners |

## Skill pairings and handoffs

This skill owns **data ingestion, validation, transformation, pipelines, imports/exports,
loading strategy, reconciliation and processing reliability**; the paired skill owns the
mechanism or general practice.

| Topic | Skill |
|---|---|
| Settings, storage API, forms, signals, management command mechanics, logging config | [django-core](../django-core/SKILL.md) |
| App boundaries, service layer, events, ADRs, whether to distribute | [django-architecture](../django-architecture/SKILL.md) |
| QuerySet, bulk-operation and transaction mechanics, constraints, migrations | [django-orm](../django-orm/SKILL.md) |
| General profiling, worker sizing, application performance | [django-performance](../django-performance/SKILL.md) |
| External API clients, Celery/broker infrastructure, webhooks, object storage transfer, generic sync | [django-integrations](../django-integrations/SKILL.md) |
| General Django security, upload hardening principles, tenancy model, secrets | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test framework mechanics, factories, mocking, task-test mechanics | [django-testing](../django-testing/SKILL.md) |
| Import/export API endpoints and serializers | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Rendering/formatting downloadable reports and documents | [django-report-generation](../django-report-generation/SKILL.md) |
| Query plans, COPY/WAL internals, vacuum, partitioning | [postgresql-engineering](../postgresql-engineering/SKILL.md) |

Examples: QuerySet optimisation → django-orm; designing an external API client → django-integrations,
but transforming its large datasets → here; Celery broker setup → django-integrations, but
designing a reliable chunked import task → here; file-processing security specifics → here,
with general principles in django-security-engineering.

Common pairings: CSV import → import/export + file-processing + validation (with django-security-engineering
file-upload-security); nightly ERP sync → ingestion + transformation + reconciliation (with
django-integrations data-synchronization-and-reconciliation); OOM in worker → large-dataset +
performance (with django-performance memory-and-resource-management).

## Architecture decision guidelines

| Decision | Prefer | Escalate when | Because |
|---|---|---|---|
| Where processing runs | In-request only for small, bounded, fast work (< ~1–2 s, few thousand rows) | Larger/slower → management command (ops-run) or Celery task (user-triggered) | Requests have timeouts and hold workers |
| Scheduler | Platform cron + management command, or Celery beat if Celery exists | Dependencies/backfills/SLAs across many jobs → orchestrator (Airflow/Dagster) | Orchestrators are infrastructure to run |
| Batch vs stream | Batch (scheduled or triggered) | Business needs sub-minute freshness → micro-batch, then streaming | Streaming adds ordering, state and ops cost |
| ETL vs ELT | ETL in Python for business-rule-heavy, row-level mapping | Set-based transforms on large data already in PostgreSQL → ELT (staging + SQL) | Move logic to where data and expertise are |
| Tooling | stdlib `csv`/generators + ORM | Analytical/columnar work → Polars/PyArrow (pandas if already used) | Dependencies and memory cost |
| Error policy | Isolate invalid records with a report; fail on structural errors or error rate > threshold | Regulated/financial all-or-nothing → validate fully, then load atomically | Users fix rows, not files; finance needs completeness |
| Transaction size | Committed batches (500–5 000 rows) + idempotent re-run | Small dataset with strict atomicity → one transaction or staging + set-based swap | Long transactions lock, bloat and are hard to retry |
| Loading | `bulk_create(update_conflicts=…)` per batch | Millions of rows → staging table via COPY + `INSERT … SELECT`/`MERGE` | Fewer round trips, set-based integrity |
| Parallelism | One worker, measure first | Proven CPU-bound → processes; I/O-bound → threads/async with bounded concurrency | Parallelism moves the bottleneck to the DB |
| Delivery guarantee | At-least-once + idempotent sink | — exactly-once is never assumed from Celery or brokers | Redelivery always happens eventually |
| Correctness proof | Control totals and counts per run | Financial/regulated → field-level/hash reconciliation with exception workflow | Successful execution can still lose data |

Record hard-to-reverse choices (orchestrator, staging model, error policy, retention) in an ADR.

## Production validation checklist

- [ ] Volume, frequency, latency and correctness requirements are written down; architecture is the simplest that meets them.
- [ ] Inputs bounded (size, rows, entries, pages, runtime); type sniffed from bytes; parsers are XXE/zip-bomb safe.
- [ ] Data contract defined; validation produces coded, located errors; dataset-vs-record rejection policy explicit.
- [ ] No whole-file or whole-table loads into memory; streaming/chunked I/O with measured batch sizes.
- [ ] Mapping definitions versioned; version and source recorded on outputs (lineage).
- [ ] Loads are idempotent (natural keys/upserts/run ids); intra-batch duplicates removed; FKs resolved per batch.
- [ ] Transaction boundaries justified; bulk operations account for skipped `save()`, signals and `full_clean()`.
- [ ] Tasks take ids, enqueue on commit, are idempotent, time-limited, retry only transient errors, and checkpoint long work.
- [ ] Job status, counters and error reports visible to users; stuck/cancelled jobs handled.
- [ ] Reconciliation (counts, totals, keys) runs after load; exceptions are tracked to resolution.
- [ ] Tenant scoping in every query, task, storage path and download; downloads are signed/short-lived and permission-checked.
- [ ] Sensitive data masked in logs and error reports; uploads, staging and reports purged per retention policy.
- [ ] Metrics, structured logs with correlation ids, and alerts for failures, rejection spikes, lag and queue age.
- [ ] Tests cover malformed files, boundaries, duplicates, partial failure, retry/idempotency and reconciliation.
- [ ] Third-party packages labelled and present in the lock file; `VERIFY:` items checked or disclosed.
- Docs: <https://docs.djangoproject.com/en/5.2/> · <https://docs.python.org/3/library/csv.html> ·
  <https://docs.celeryq.dev/en/stable/>
