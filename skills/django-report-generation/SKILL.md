---
name: django-report-generation
description: >-
  Django report generation engineering specialist — reporting fundamentals and requirements,
  reporting subsystem architecture (report definitions, parameters, runs, artifacts, status),
  report data acquisition (DB-side aggregation, snapshots, read models, materialized views, time
  zones, Decimal precision, historical and reproducible reports), PDF (ReportLab, WeasyPrint,
  xhtml2pdf), Excel (openpyxl, XlsxWriter), CSV/JSON/XML, DOCX (python-docx), charts (Matplotlib,
  Plotly), report templates and renderers, download endpoints and signed links, background
  generation with Celery, scheduled reports and distribution, reporting security and compliance,
  performance, testing, observability, troubleshooting and production reporting architectures.
  Load when building, reviewing, scaling or troubleshooting an export, downloadable report,
  scheduled report, statement, invoice, payroll or financial report, or a reporting platform.
project: django-agentic-engineering
abstract: >-
  Reports are computed from a consistent, authorised, reproducible snapshot; rendered with bounded
  memory by the cheapest format and library that meets the requirement; generated off the request
  when they are slow; and delivered as private, expiring, audited artifacts. A concise workflow and
  navigation guide here; eighteen focused references under references/ loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, PostgreSQL, Celery, Redis, ReportLab, WeasyPrint, openpyxl, XlsxWriter, python-docx, Matplotlib, Plotly]
domain: [Software Engineering, Reporting]
---

# Django Report Generation Engineering Specialist

Engineering judgement for producing reports from Django systems: decide what the report must
prove, compute it correctly from a consistent and authorised data set, render it in the right
format, run it where it will not hurt the site, deliver it safely, and operate it in production.
Generating one file is a function; a **reporting platform** adds definitions, versioned
parameters, runs, artifacts, schedules, delivery, retention, audit and monitoring — decide which
one the requirement actually needs.

Baseline: **Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL 16+, Celery 5.x, Redis 7+**.
Version-dependent behaviour is qualified "(Django ≥ X.Y)"; uncertain claims are marked
`[VERIFY: …]`. Every API is labelled `[Django]`, `[stdlib]`, `[DRF]`, `[PostgreSQL]`,
`[third-party: package]`, `[external service]` or `[external tool]`. Rendering libraries, Celery
and object storage are **not** Django features — check the lock file and never assume one is
installed.

Reading uploaded files and import pipelines belong to
[django-data-processing](../django-data-processing/SKILL.md); this skill produces output.

## Activate when

- Adding a CSV/XLSX/PDF/DOCX/JSON/XML export, statement, invoice, payslip or downloadable report.
- Designing report definitions, parameters, run tracking, artifacts or a reporting subsystem.
- Writing aggregation queries whose results are published as figures (totals, balances, KPIs).
- A report is slow, times out, exhausts memory, or blocks web workers.
- Moving report generation to background tasks, scheduling reports, or emailing them.
- Reviewing download endpoints, signed links, tenant isolation or PII in reports.
- Totals disagree with the screen, dates are off by one, files are garbled, or jobs are stuck.

## Reporting engineering principles

1. **Correctness first.** A fast, pretty report with a wrong total is a defect with business
   consequences. Aggregate in the database, `Decimal` end to end, explicit rounding, half-open
   periods in the report's time zone.
2. **Reproducible by construction.** Every run records report key + version, normalised
   parameters (and hash), data-as-of time and the producing code/template version.
3. **One consistent snapshot.** Multi-query reports read under one snapshot (REPEATABLE READ,
   a frozen read model, or a dated pre-aggregate) and state what "as of" means.
4. **Security is a data-access boundary.** Authorise at request, at generation and at download;
   scope every selector by tenant and user; an identifier or URL is never authorisation.
5. **Separate generation from delivery.** Build an immutable artifact once; deliver it many
   times through channels that can fail and retry independently.
6. **Bounded resources.** Iterate in chunks, stream when the format allows it, buffer to temp
   files when it does not, and cap rows, size, runtime and concurrency.
7. **Idempotent, recoverable execution.** Runs are claimed atomically, deduplicated, retried
   only on transient failure, reaped when stuck, and never produce a second divergent artifact.
8. **Simplest execution mode that meets the SLA.** Synchronous → streaming → background →
   scheduled → pre-generated/pre-aggregated; each step up needs a measured reason.
9. **Prove the file, not the status code.** Tests open the generated file and assert content.
10. **Observable and auditable.** Correlation IDs, lifecycle metrics, download and delivery audit,
    retention enforced by jobs that are themselves monitored.

## Report engineering workflow

1. **Clarify the requirement** — audience, decision it supports, report type, figures and their
   definitions, accuracy/reproducibility needs, SLA, volume, frequency
   → `reporting-engineering-fundamentals.md`.
2. **Define the report** — `ReportSpec` (key, version, parameters, formats, permission,
   classification, retention) and the run/artifact model → `reporting-architecture-and-design.md`.
3. **Design the data acquisition** — selector, aggregation, snapshot, time zones, precision,
   read model or pre-aggregate → `reporting-data-acquisition-and-preparation.md`.
4. **Choose format and library** → the format reference (PDF, Excel, CSV/structured, DOCX,
   charts) and `report-templates-and-reusability.md` for shared layout.
5. **Choose the execution mode** — sync, streaming, background, scheduled, pre-generated
   (selection guide below) → `background-report-generation.md`, `report-generation-performance.md`.
6. **Design delivery** — download endpoint, signed URL, email/scheduled distribution
   → `django-report-endpoints-and-delivery.md`, `scheduled-reports-and-distribution.md`.
7. **Secure it** — authorisation points, tenant isolation, PII, injection, SSRF, retention
   → `reporting-security-and-compliance.md`.
8. **Test the content** → `report-generation-testing.md`.
9. **Instrument and operate** → `reporting-observability-and-operations.md`;
   failures → `report-generation-troubleshooting.md`.
10. **Validate** with the production checklist below; record hard-to-reverse choices in an ADR.

Inspect only the report's selector/service, its models, the renderer, the task and the relevant
settings block. Load **one to three references per step** — never all. For a whole-system design,
start with `production-reporting-architectures.md` and follow its links.

## Reporting architecture selection

| Signal | Execution mode | Typical delivery |
|---|---|---|
| Small, bounded output (≈ < 5 000 rows, < ~2 s), interactive | Synchronous request | `HttpResponse` / `FileResponse` |
| Large flat tabular export (CSV/JSONL), user waits for download | Streaming response from `.iterator()` | `StreamingHttpResponse` (proxy buffering off, worker timeout fits) |
| XLSX/PDF/DOCX with many rows/pages, or > ~10 s, or must survive disconnects | Background task → stored artifact | Notification + permission-checked or signed, expiring link |
| Recurring at known times (daily/weekly/monthly) | Scheduled dispatcher → background runs | Subscriptions, email link, delivery records |
| Same report requested by many users, data changes slowly | Pre-generated artifact or pre-aggregated table/materialized view | Reuse artifact keyed by params hash + data-as-of |
| Legally binding / reproducible document (invoice, statement, payslip) | Generate once at the business event, store immutably | Download of the stored original, never regenerate silently |

Thresholds are starting points — derive real ones from measured duration, memory and the
worker/proxy timeouts. Full matrix: `production-reporting-architectures.md`.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [reporting-engineering-fundamentals.md](references/reporting-engineering-fundamentals.md) | Report types, transactional vs analytical, real-time vs scheduled, on-demand vs pre-generated, lifecycle, definitions/parameters/metadata/ownership/versioning, requirements, accuracy, SLAs, report vs platform |
| [reporting-architecture-and-design.md](references/reporting-architecture-and-design.md) | Subsystem components, `ReportSpec` registry, canonical `ReportRun`/`ReportArtifact` models, parameter validation, filter/sort allow-lists, selector contract, status state machine, execution-mode comparison, multi-tenancy, generation vs delivery |
| [reporting-data-acquisition-and-preparation.md](references/reporting-data-acquisition-and-preparation.md) | Report queries, aggregation and fan-out, chunked/keyset iteration, snapshots and transactions, read models, materialized views, pre-aggregated tables, time zones, Decimal/currency, nulls, historical and reproducible reports |
| [pdf-report-generation.md](references/pdf-report-generation.md) | ReportLab, WeasyPrint, xhtml2pdf, headless browsers; layout, headers/footers, page numbering, tables, fonts/Unicode, images, metadata, encryption, PDF/A and accessibility limits, performance, validation |
| [excel-report-generation.md](references/excel-report-generation.md) | openpyxl vs XlsxWriter, workbook design, formats, dates, formulas and recalculation, XLSX formula injection, charts, conditional formatting, validation, large workbooks, limits |
| [csv-and-structured-report-generation.md](references/csv-and-structured-report-generation.md) | `csv` module, streaming CSV, encoding/BOM, quoting, newlines, serialization, JSON/JSONL/XML, schema validation, deterministic output, `safe_cell()` formula-injection helper |
| [docx-and-document-generation.md](references/docx-and-document-generation.md) | python-docx, document templates, styles, tables, images, sections, headers/footers, metadata, limits, DOCX vs PDF vs XLSX, conversion |
| [charts-and-data-visualization.md](references/charts-and-data-visualization.md) | Matplotlib, Plotly, static vs interactive, chart data preparation, embedding per format, axes/legends, accessibility, determinism, memory cleanup |
| [report-templates-and-reusability.md](references/report-templates-and-reusability.md) | Format-agnostic report document model and renderers, shared components and branding, template storage and versioning, Django templates for HTML/PDF, localisation |
| [django-report-endpoints-and-delivery.md](references/django-report-endpoints-and-delivery.md) | Report views, `FileResponse`, `StreamingHttpResponse`, content headers, filenames, DRF run/metadata endpoints, storage, signed/expiring URLs, X-Accel-Redirect, caching headers, Range, errors, download audit |
| [background-report-generation.md](references/background-report-generation.md) | Celery report tasks, task vs business status, claiming, idempotency, retries, timeouts, acks and crashes, progress, cancellation, dedupe, artifact creation and expiry, recovery, notification |
| [scheduled-reports-and-distribution.md](references/scheduled-reports-and-distribution.md) | Schedules, time-zone-aware next run, dispatcher, subscriptions and recipients, email distribution, delivery status, retries, dedupe, delivery audit, retention, payroll/ops/sales/financial examples |
| [reporting-security-and-compliance.md](references/reporting-security-and-compliance.md) | Report and row-level authorisation, tenant isolation, IDOR, PII/financial data, signed URL threats, SSRF, path traversal, temp files, cache leakage, encryption, retention, audit, technical controls vs legal compliance |
| [report-generation-performance.md](references/report-generation-performance.md) | Budgets, streaming vs buffering per format, memory, CPU rendering, queues and concurrency, DB connections, caching and artifact reuse, pre-aggregation, benchmarking, backpressure |
| [report-generation-testing.md](references/report-generation-testing.md) | Calculation tests, file-content validation per format, endpoint/header tests, authorisation and tenant tests, task/retry/schedule tests, fixtures, snapshot and visual regression, large-data and memory tests |
| [reporting-observability-and-operations.md](references/reporting-observability-and-operations.md) | Metrics, lifecycle logs, correlation IDs, audit trails, dashboards, alerts, cleanup operations, operational runbooks |
| [report-generation-troubleshooting.md](references/report-generation-troubleshooting.md) | Symptom → cause → fix catalogue: wrong figures, time-zone and rounding errors, OOM, timeouts, stuck/duplicate runs, broken files, missing fonts, download and delivery failures |
| [production-reporting-architectures.md](references/production-reporting-architectures.md) | Ten blueprints (invoices, payroll, financial statements, sales, dashboards/exports, regulatory, bulk exports, scheduled customer reports, multi-tenant SaaS, historical/audit) and the architecture decision matrix |

Legacy, superseded files retained for reference only — do not load for new work:
[references/report-formats.md](references/report-formats.md),
[references/large-datasets.md](references/large-datasets.md).

## Task-based loading

| Task | Load |
|---|---|
| Gather requirements / decide what kind of report this is | reporting-engineering-fundamentals.md |
| Design a reporting subsystem or report definition model | reporting-architecture-and-design.md (+ production-reporting-architectures.md) |
| Write or review the query behind a report; wrong totals | reporting-data-acquisition-and-preparation.md (+ report-generation-troubleshooting.md) |
| Month/period boundaries, time zones, currency rounding | reporting-data-acquisition-and-preparation.md |
| Generate an invoice or statement PDF | pdf-report-generation.md, report-templates-and-reusability.md |
| Excel export with formatting, formulas or charts | excel-report-generation.md (+ charts-and-data-visualization.md) |
| Large CSV export without loading into memory | csv-and-structured-report-generation.md, django-report-endpoints-and-delivery.md |
| JSON/XML report for a partner or regulator with a schema | csv-and-structured-report-generation.md |
| Editable Word document / contract / letter output | docx-and-document-generation.md, report-templates-and-reusability.md |
| Charts in PDF, Excel, email or dashboard | charts-and-data-visualization.md (+ the format reference) |
| Same report in several formats with shared branding | report-templates-and-reusability.md |
| Download endpoint, DRF report API, signed links | django-report-endpoints-and-delivery.md (+ reporting-security-and-compliance.md) |
| Move report generation to Celery; progress, cancel, retries | background-report-generation.md |
| Daily/weekly/monthly emailed reports, subscriptions | scheduled-reports-and-distribution.md, background-report-generation.md |
| Security review of reports, exports or downloads | reporting-security-and-compliance.md |
| Report is slow / OOM / blocks workers | report-generation-performance.md (+ report-generation-troubleshooting.md) |
| Test a report end to end | report-generation-testing.md |
| Metrics, alerts, dashboards, cleanup jobs | reporting-observability-and-operations.md |
| Production incident in reporting | report-generation-troubleshooting.md, reporting-observability-and-operations.md |
| Design a complete reporting solution | production-reporting-architectures.md, then the references it links |

## Skill pairings and handoffs

This skill owns **report-specific engineering**: definitions, figures, formats, rendering,
report execution, delivery, report security and report operations. The paired skill owns the
mechanism or general practice.

| Topic | Skill |
|---|---|
| Settings, storage API, request/response, templates, logging, management commands | [django-core](../django-core/SKILL.md) |
| App boundaries, service/selector layer, where the reporting app sits, ADRs | [django-architecture](../django-architecture/SKILL.md) |
| QuerySet, aggregation, expressions, transactions, indexes mechanics | [django-orm](../django-orm/SKILL.md) |
| Serializers, ViewSets, permissions classes, pagination, throttling | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Authorisation, IDOR, SSRF, multi-tenant, secrets, audit fundamentals | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test framework, factories, mocking, task and DB test mechanics | [django-testing](../django-testing/SKILL.md) |
| Profiling, worker sizing, caching, memory, load testing | [django-performance](../django-performance/SKILL.md) |
| Celery/broker infrastructure, object storage, email providers, external delivery | [django-integrations](../django-integrations/SKILL.md) |
| Imports, ETL, generic bulk export pipelines, reconciliation | [django-data-processing](../django-data-processing/SKILL.md) |
| Query plans, isolation levels, materialized view internals, partitioning | [postgresql-engineering](../postgresql-engineering/SKILL.md) |

Examples: a slow aggregation → write it here, tune the plan with django-orm/postgresql-engineering;
a reliable report task → here, Celery deployment → django-integrations; a generic data export
pipeline feeding another system → django-data-processing, a formatted business report → here.

## Reporting decision guidelines

| Decision | Prefer | Escalate when | Because |
|---|---|---|---|
| Where figures are computed | Database aggregation via a scoped selector | Repeated heavy aggregation → pre-aggregated table / materialized view | Python loops are slow and drift from SQL semantics |
| Consistency | One transaction under REPEATABLE READ or a dated read model | Cross-system figures → reconciled snapshot tables | Separate queries see different data |
| Money | `Decimal`, quantize at presentation with a documented rounding mode | Multi-currency → convert with dated rates stored on the run | Floats and implicit rounding break totals |
| PDF library | WeasyPrint (HTML/CSS templates) when the team owns HTML | Pixel-precise/programmatic layouts or no system libs → ReportLab; JS-rendered layouts → headless browser service | Template maintainability vs control vs runtime cost |
| Excel library | openpyxl when reading/modifying templates or already installed | Rich formatting/charts on new files with low memory → XlsxWriter | Different capabilities; neither streams over HTTP |
| Execution | Synchronous for small bounded output | Slow, large, binary formats, or must survive disconnect → background | Requests hold workers and hit proxy timeouts |
| Delivery | Permission-checked download view | High volume / large files → short-lived storage presigned URL | Re-authorisation vs offloading bytes |
| Email | Link to an authenticated download | Attachment only for small, low-sensitivity, recipient-approved reports | Mail is copied, forwarded and retained outside your control |
| Legally significant documents | Generate once, store immutable original with hash | — never silently regenerate | Later data or template changes alter the document |
| Reuse | Regenerate on demand | Same params + same data-as-of requested often → reuse artifact | Reuse needs a correct cache key and tenant scope |

Record hard-to-reverse choices (rendering stack, artifact retention, tenancy model, scheduler) in
an ADR ([django-architecture](../django-architecture/references/architecture-decision-records.md)).

## Production validation checklist

- [ ] Report purpose, audience, figure definitions, period semantics, time zone and rounding rule are written down.
- [ ] Figures computed in the database via a tenant- and user-scoped selector; fan-out checked; totals reconcile with the source screen or ledger.
- [ ] Multi-query reports read one consistent snapshot; the artifact states its data-as-of time.
- [ ] Runs record report key, version, normalised parameters, params hash, requester, correlation ID.
- [ ] Parameters validated with bounds (date range, row limits); filters and sort keys allow-listed; tenant never taken from client input.
- [ ] Execution mode justified by measured duration/size; long work runs in a time-limited background task on a dedicated queue.
- [ ] Tasks are enqueued on commit, claim runs atomically, are idempotent, retry only transient errors, and stuck runs are reaped.
- [ ] Memory bounded: chunked iteration; streaming where the format allows, temp-file buffering where it does not.
- [ ] CSV/XLSX cells neutralise formula injection; HTML-to-PDF rendering blocks external fetches; filenames are server-generated.
- [ ] Artifacts in private storage; downloads re-authorised or via short-lived, scoped signed URLs; `Cache-Control: private, no-store`.
- [ ] Cross-user and cross-tenant access to runs, artifacts and URLs returns 404 — proven by tests.
- [ ] Downloads and deliveries audited; PII and secrets absent from logs, filenames and email bodies.
- [ ] Retention and deletion enforced by a monitored cleanup job; legal requirements confirmed with compliance owners.
- [ ] Tests open generated files and assert content, headers, totals, time-zone edges and authorisation.
- [ ] Metrics for queue wait, duration, size, rows, failures, retries and delivery; alerts on failure rate, backlog and stuck runs.
- [ ] Third-party packages and external services labelled and present in the lock file/infrastructure; `[VERIFY: …]` items checked or disclosed.
- Docs: <https://docs.djangoproject.com/en/5.2/> · <https://docs.djangoproject.com/en/5.2/ref/request-response/> ·
  <https://docs.djangoproject.com/en/5.2/topics/db/optimization/> · <https://docs.python.org/3/library/csv.html>
