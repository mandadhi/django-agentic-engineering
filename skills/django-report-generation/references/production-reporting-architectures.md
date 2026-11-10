# Production Reporting Architectures

> **Scope:** Ten end-to-end reporting blueprints for Django systems — invoices, payroll, financial statements, sales and revenue, operational exports, regulatory reports, bulk exports, scheduled customer reports, multi-tenant SaaS reporting and historical/audit reports — plus a decision matrix for choosing execution mode, storage and delivery. **Not here:** the detailed mechanics each blueprint relies on; every blueprint links to the reference that owns them.

## 1. Purpose and scope

Each blueprint answers the same 13 questions so they can be compared: business requirements,
format, data acquisition, query strategy, generation strategy, sync/async, storage, delivery,
security, failure handling, performance, testing and monitoring. They use the canonical models
(`ReportSpec`, `ReportRun`, `ReportArtifact`, `ReportSchedule`, `ReportRecipient`,
`ReportDelivery`, `ReportDownload`) and domain apps (`billing`, `sales`, `payroll`, `inventory`,
`ledger`). They are starting points to adapt, not prescriptions: volumes, SLAs and legal
requirements of the real system decide.

## 2. Core concepts

Five variables decide most reporting architectures:

| Variable | Question | Pushes towards |
|---|---|---|
| **Cost** | How many rows, how much rendering CPU? | Large → async, streaming, pre-aggregation |
| **Freshness** | Must the data be live, or "as of" a cut-off? | Cut-off → snapshot / pre-generated |
| **Reproducibility** | Must the same report be reproducible later, exactly? | Yes → immutable artifact + snapshot + version |
| **Audience** | Internal user, customer, regulator, machine? | External → stricter auth, retention, formats |
| **Cadence** | Ad hoc, scheduled, event-triggered? | Scheduled/event → pre-generated + distribution |

Execution modes used below (compared in [reporting-architecture-and-design.md](reporting-architecture-and-design.md)):
**sync** (render in request), **stream** (`StreamingHttpResponse`), **async on-demand** (task →
artifact → notify), **scheduled** (dispatcher → task → artifact → deliveries), **pre-generated /
event-triggered** (artifact created when the business event happens).

## 3. Internal mechanics

Shared skeleton all blueprints instantiate:

```text
           ┌──────────── request (view/API) or schedule dispatcher or domain event ───────────┐
           ▼                                                                                   │
  request_report(): validate params (ReportSpec.params_form) → authorise → dedupe (params_hash)
           │ create ReportRun(pending) ── transaction.on_commit ──► generate_report(run_id)
           ▼                                                            │
  sync/stream path (small, cheap)                       claim → selector(snapshot) → render
  render + respond directly                                   → store ReportArtifact (private)
                                                              → succeeded → notify/deliver
                                                                            │
                                         download view / signed URL ◄──────┘  (ReportDownload audit)
                                         cleanup task deletes after expires_at
```

## 4. Engineering decisions and trade-offs

### Architecture decision matrix

| Requirement signal | Execution mode | Storage | Delivery |
|---|---|---|---|
| < ~5 000 rows, < ~2 s, interactive | Sync | None | Inline response |
| Large tabular, CSV acceptable, user waits | Stream | None | Streaming download |
| Large tabular, XLSX/PDF, or > ~10 s | Async on-demand | Private object storage, TTL | Notify + authenticated link |
| Same report for many users/periods, slow-changing data | Pre-aggregated read model + sync/async | Summary table / MV | Screen + export |
| Legally significant document (invoice, payslip, statement) | Event-triggered pre-generation | Immutable artifact, long retention | Portal download, email link |
| Fixed cadence, defined recipients | Scheduled | Private storage, retention per policy | Email link / SFTP / portal |
| Exact reproducibility required later | Snapshot data + immutable artifact + version | Artifact + snapshot tables | Re-download the stored artifact, never regenerate |
| Must be live to the minute | Sync on read replica / read model with short refresh | None or cache | Screen / small export |
| External audience (customers, regulators) | Async or pre-generated | Private storage, audited | Authenticated portal or signed short-lived URL |
| Machine consumer | Async or scheduled, JSON/CSV/XML with schema | Storage or push | API / SFTP / webhook |
| Millions of rows | Async, chunked, CSV/JSONL (XLSX only if under sheet limits) | Object storage, multipart | Signed URL |
| Multi-tenant, uneven tenant sizes | Async with per-tenant concurrency limits | Tenant-prefixed private storage | Tenant-scoped links |

### Decision flow

```text
Is the output a legally significant document tied to a business event?
 ├─ yes → pre-generate on event, immutable artifact, long retention (Blueprints 1, 2)
 └─ no
    Is it delivered on a fixed cadence to defined recipients?
     ├─ yes → scheduled + distribution (Blueprints 3, 8)
     └─ no (on demand)
        Expected cost under ~2 s and ~5k rows, every time?
         ├─ yes → sync response
         └─ no
            CSV/JSONL acceptable and no heavy rendering?
             ├─ yes, and proxy/timeouts allow it → stream
             └─ otherwise → async on-demand → artifact → notify
Then: Is the underlying query slow AND repeated? → add a read model / pre-aggregation.
      Must the result be reproducible later? → snapshot inputs + keep artifact + version.
```

## 5. Implementation patterns

### Blueprint 1 — Invoice generation

| Aspect | Design |
|---|---|
| Business requirements | Every issued `Invoice` has exactly one legally binding PDF; content never changes after issue; corrections via credit note, not edit. Customers download from a portal; finance can re-send. |
| Format | PDF (often PDF/A for archiving [VERIFY: whether WeasyPrint's pdf_variant supports the required PDF/A level in the installed version]); optional structured e-invoice XML where local law requires it. |
| Data acquisition | `Invoice` + `InvoiceLine` + `Customer` + tenant branding at issue time. |
| Query strategy | One query with `select_related("customer")` + `prefetch_related("lines__product")`; small. |
| Generation strategy | Event-triggered: the `issue_invoice()` service sets `status="issued"` and enqueues `generate_report` on commit. HTML template + WeasyPrint (layout by designers) or ReportLab. |
| Sync/async | Async (seconds), but fast; the portal shows "preparing" briefly. |
| Storage | Private storage, server-generated name, `sha256` recorded; retention per tax law [VERIFY: jurisdiction-specific invoice retention period]. Never regenerated in place. |
| Delivery | Portal download view (scoped by customer), email with a link to the portal. |
| Security | Customer users see only their customer's invoices; staff by tenant + permission; download audit; no external resource loading in the renderer. |
| Failure handling | Retry transient render/storage errors; the invoice stays `issued` even if the PDF fails — an alert and reaper ensure the PDF eventually exists. |
| Performance | Fonts and logos local; batch issuance (month-end) spreads over a dedicated queue. |
| Testing | Totals on the PDF equal `Invoice.total`; text extraction checks number, dates, amounts; immutability test (regeneration refused after issue). |
| Monitoring | Issued invoices without artifact after N minutes (DB gauge); render failures by `error_code`. |

### Blueprint 2 — Monthly payroll reports

| Aspect | Design |
|---|---|
| Business requirements | After `PayrollRun` approval: payroll register (finance), cost-centre summary, payslips per `Employee`. Numbers must match the approved run exactly. Highly confidential. |
| Format | Register: XLSX (finance analysis) + PDF (sign-off); payslips: PDF per employee. |
| Data acquisition | `Payslip`/`PayslipLine` for one `PayrollRun` — the approved run is itself the snapshot. |
| Query strategy | `values()` aggregation by department/cost centre; `iterator(chunk_size=…)` over payslips. |
| Generation strategy | Event-triggered on `PayrollRun` → `approved`: one register run plus a fan-out of payslip runs (chunked group of tasks), each idempotent on `(payroll_run, employee)`. |
| Sync/async | Async; deadline-driven (must finish before payment approval). |
| Storage | Private storage under a tenant/run prefix; encrypted at rest; retention per payroll/tax law [VERIFY: jurisdiction-specific payroll record retention]. |
| Delivery | Register to the finance group via authenticated links; payslips via the employee self-service portal; email contains no figures. |
| Security | Separate permission (e.g. `reports.view_payroll_reports`); employees see only their own payslip; no payroll data in logs; download audit mandatory. |
| Failure handling | Per-employee payslip failures isolated and retried; register not marked complete until reconciliation (sum of payslips = run totals) passes. |
| Performance | Thousands of small PDFs → bounded concurrency; reuse compiled templates and fonts per worker. |
| Testing | Reconciliation tests; per-employee access tests; payslip content tests with fixed fixtures. |
| Monitoring | Fan-out progress (done/total), deadline SLA alert, reconciliation failure alert. |

### Blueprint 3 — Financial statements

| Aspect | Design |
|---|---|
| Business requirements | Monthly/quarterly P&L, balance sheet, trial balance from `ledger`. Must reproduce exactly what was reported for a closed period, even after late adjustments. |
| Format | PDF for distribution; XLSX for accountants (values, not formulas, plus optional formula sheet). |
| Data acquisition | `LedgerEntry` aggregated by `Account` for the period; closed periods read from a period-close snapshot table. |
| Query strategy | `values("account").annotate(debit=Sum(..., filter=Q(...)), credit=...)`; REPEATABLE READ transaction so all statements share one snapshot; `Decimal` throughout. |
| Generation strategy | Scheduled after period close + on demand for open periods (watermarked "provisional"). `report_version` bumped when account mapping changes. |
| Sync/async | Async. |
| Storage | Immutable artifacts for closed periods; long retention [VERIFY: statutory retention for financial statements in the relevant jurisdiction]. |
| Delivery | Authenticated portal for finance and auditors; scheduled distribution to the CFO list. |
| Security | Restricted classification; MFA-protected access recommended; download audit; external auditors via time-boxed accounts, not shared links. |
| Failure handling | Fail closed: if the trial balance doesn't balance (debits ≠ credits), mark the run `failed` with `error_code="reconciliation_failed"` — never deliver an unbalanced statement. |
| Performance | Period-close snapshot or monthly balances table avoids scanning all entries; materialized view refreshed after close. |
| Testing | Balancing invariant; known-period fixtures with hand-computed results; rounding rules; reproducibility test (regenerate closed period → identical sha256 when output is deterministic). |
| Monitoring | Snapshot freshness, reconciliation failures, late statements vs close calendar. |

### Blueprint 4 — Sales and revenue reports

| Aspect | Design |
|---|---|
| Business requirements | Revenue by day/week/month, product, region, salesperson; dashboards plus exports; "same numbers on screen and in export". |
| Format | Screen (HTML/JSON API), XLSX and CSV exports, PDF summary with charts. |
| Data acquisition | `Order`/`OrderLine` (bookings) or `Invoice`/`Payment` (recognised revenue) — define which in the `ReportSpec`; never mix silently. |
| Query strategy | Daily summary table (`tenant, day, product, revenue, orders`) maintained incrementally; detail drill-down queries the base tables with indexes. |
| Generation strategy | One selector shared by screen and export; small ranges sync, large ranges async. |
| Sync/async | Sync for summaries; async for detail exports > `max_sync_rows`. |
| Storage | Short-lived artifacts (e.g. 7 days). |
| Delivery | Download in-app; weekly scheduled email link to managers. |
| Security | Row-level scoping (salesperson sees own region); tenant scoping; formula injection escaping on product/customer names. |
| Failure handling | Retries for transient DB errors; row limit with a clear error suggesting a narrower range. |
| Performance | Pre-aggregation is the main lever; charts rendered from aggregated rows only. |
| Testing | Summary table equals base-table aggregation for random days (reconciliation test); time-zone boundary tests. |
| Monitoring | Summary freshness, export queue wait, rows histogram. |

### Blueprint 5 — Operational dashboards and exports

| Aspect | Design |
|---|---|
| Business requirements | Near-real-time views (open orders, stock below threshold, overdue invoices) with "export what I see". |
| Format | Screen; CSV export of the current filter. |
| Data acquisition | Live tables on a read replica (accept replication lag, show it) or the primary for small tenants. |
| Query strategy | Indexed filters, allow-listed sort fields, keyset pagination on screen; the export uses the same filter object. |
| Generation strategy | Streaming CSV for exports; no artifact. |
| Sync/async | Sync/stream; fall back to async if the row count estimate exceeds the limit. |
| Storage | None (or short-lived artifact for the async fallback). |
| Delivery | Direct streaming download. |
| Security | Same scoped selector as the screen; export permission separate from view permission for sensitive lists; audit bulk exports of personal data. |
| Failure handling | Mid-stream failure leaves a truncated file — emit a trailer/row count or use async for critical exports. |
| Performance | `values_list` + `iterator`; proxy buffering disabled; statement timeout on the reporting alias. |
| Testing | Export rows equal screen query rows for the same filter; streaming content assembled in tests. |
| Monitoring | Export duration, aborted streams, replica lag. |

### Blueprint 6 — Regulatory and compliance reports

| Aspect | Design |
|---|---|
| Business requirements | Periodic submissions (tax, payroll filings, transaction reporting) in a prescribed format and schema, with evidence of what was submitted and when. Requirements come from law/regulators — confirm with compliance. |
| Format | Prescribed XML/CSV/XBRL/fixed-width, validated against the official schema; human-readable PDF companion. |
| Data acquisition | Frozen snapshot of the reporting period (snapshot tables populated at cut-off). |
| Query strategy | Deterministic ordering; explicit mapping from domain codes to regulatory codes, versioned. |
| Generation strategy | Scheduled with a manual approval step: generated → reviewed → submitted (a business workflow on top of `ReportRun`). |
| Sync/async | Async. |
| Storage | Immutable artifacts + submission receipts; retention per regulation [VERIFY: regulator-specific retention requirements]; WORM/object-lock storage where required. |
| Delivery | Regulator portal/API/SFTP via an integration adapter ([django-integrations](../../django-integrations/SKILL.md)); internal approval via portal. |
| Security | Restricted classification; segregation of duties (generator ≠ approver); full audit trail. |
| Failure handling | Schema validation failure blocks submission; submission retries idempotent with the regulator's reference; never resubmit without checking status (ambiguous outcomes). |
| Performance | Usually modest; correctness dominates. |
| Testing | Schema validation tests; golden-file tests per mapping version; code-mapping unit tests. |
| Monitoring | Deadline calendar alerts; validation failures; submission status. |

### Blueprint 7 — Large-scale bulk exports

| Aspect | Design |
|---|---|
| Business requirements | Customers/admins export millions of rows (all orders, all ledger entries) for BI or migration. |
| Format | CSV (gzip) or JSONL; Parquet if consumers want it ([django-data-processing — columnar-archive-and-document-files](../../django-data-processing/references/columnar-archive-and-document-files.md)). XLSX only under sheet limits. |
| Data acquisition | Keyset iteration by primary key over an indexed tenant filter, on a replica/reporting alias. |
| Query strategy | `values_list` + `iterator(chunk_size=…)` or keyset batches; no model instantiation; REPEATABLE READ or `data_as_of` cut-off for consistency. |
| Generation strategy | Async task writing to a temporary file then multipart upload, or directly streaming to storage; checkpoint by last key for resumability. |
| Sync/async | Async, dedicated `bulk` queue, per-tenant concurrency 1. |
| Storage | Object storage, short TTL (24–72 h), size quotas per tenant. |
| Delivery | Notify + signed short-lived URL issued from an authenticated view; Range supported by storage. |
| Security | Export permission, rate limits per tenant, audit of bulk personal-data exports, formula escaping if CSV may be opened in spreadsheets. |
| Failure handling | Resume from checkpoint; reaper for crashed runs; partial files never published. |
| Performance | Bounded memory (constant), compression, avoid long transactions on the primary. See [report-generation-performance.md](report-generation-performance.md). |
| Testing | Row count and checksum vs source; memory test at large volume; resume test. |
| Monitoring | Rows/s throughput, bytes, duration, storage consumption per tenant. |

### Blueprint 8 — Scheduled customer reports

| Aspect | Design |
|---|---|
| Business requirements | Customers subscribe to daily/weekly/monthly reports (e.g. weekly sales by store) delivered at a local time. |
| Format | User-selected among the `ReportSpec.formats`. |
| Data acquisition | Same selectors as on-demand reports, parameters stored on `ReportSchedule` with relative periods ("last week"). |
| Query strategy | Relative period resolved at run time in the schedule's time zone, then stored as absolute dates on `ReportRun.params`. |
| Generation strategy | Dispatcher task every minute claims due schedules (`next_run_at <= now`) atomically, creates runs, advances `next_run_at`. |
| Sync/async | Scheduled async. |
| Storage | Private, TTL aligned with link lifetime. |
| Delivery | `ReportDelivery` per `ReportRecipient`, email with link to an authenticated download; idempotency key per (run, recipient, channel). |
| Security | Recipients must be users with access at *delivery time*; external emails only via approved allow-list; unsubscribe handling. |
| Failure handling | Generation retry, then delivery retry independently; failed deliveries visible to the schedule owner. |
| Performance | Spread schedules (jitter) to avoid the 08:00 Monday spike; dedupe identical schedules across users into one run with many deliveries. |
| Testing | Next-run computation across DST/month ends; dispatcher idempotency; delivery dedupe. See [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md). |
| Monitoring | Overdue schedules, delivery failure rate, late delivery vs promised time. |

### Blueprint 9 — Multi-tenant SaaS reporting

| Aspect | Design |
|---|---|
| Business requirements | Every tenant gets the same report catalogue; tenants differ greatly in size; strict isolation; fair use of shared workers. |
| Format | All supported formats; tenant branding via templates ([report-templates-and-reusability.md](report-templates-and-reusability.md)). |
| Data acquisition | Tenant-scoped managers in every selector; tenant time zone and currency from `Tenant`. |
| Query strategy | Composite indexes leading with `tenant_id`; large tenants on pre-aggregated tables; optional PostgreSQL RLS as defence in depth ([django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md)). |
| Generation strategy | Async with per-tenant concurrency caps (count of running runs per tenant checked at claim); heavy tenants routed to a separate queue. |
| Sync/async | Mixed by size per `ReportSpec.max_sync_rows`. |
| Storage | Tenant-prefixed keys in private storage; per-tenant quotas and retention settings. |
| Delivery | Tenant-scoped download view; signed URLs bound to run id + user. |
| Security | Tenant id derived from the authenticated context, never from parameters; cross-tenant tests for every report; cache keys include tenant. |
| Failure handling | One tenant's failing report must not block others (queue isolation, per-tenant limits). |
| Performance | Noisy-neighbour control; capacity planned on the largest tenant, not the average. |
| Testing | Isolation matrix tests; per-tenant time zone tests; large-tenant volume test. |
| Monitoring | Queue wait by tenant tier; per-tenant usage from DB queries (not metric labels). |

### Blueprint 10 — Historical and audit reports

| Aspect | Design |
|---|---|
| Business requirements | "What did the system look like on date X?" — inventory valuation as of year end, customer balances at a date, who changed what. |
| Format | XLSX/CSV for analysis; PDF for formal audit evidence. |
| Data acquisition | Effective-dated/history tables (`StockMovement` ledger, audit log, history tables) rather than current state. |
| Query strategy | As-of queries: sum movements up to the cut-off (`StockMovement.objects.filter(moved_at__lt=as_of)`), or select history rows valid at `as_of`. See [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md). |
| Generation strategy | Async; `as_of` is a required parameter and recorded as `data_as_of`. |
| Sync/async | Async. |
| Storage | Artifacts for formally requested audits kept per retention policy; ad hoc ones short-lived. |
| Delivery | Authenticated portal for auditors with time-boxed access. |
| Security | Audit reports often expose more than normal screens (who did what) — separate permission; access itself audited. |
| Failure handling | If history is incomplete before a date (feature introduced later), fail with a clear error rather than reporting current state. |
| Performance | Movement ledgers grow large: periodic balance snapshots (e.g. monthly closing balances) + movements since snapshot. |
| Testing | As-of correctness tests with movements before/after cut-off; snapshot + delta equals full sum. |
| Monitoring | Snapshot job freshness; long-running as-of queries. |

### Supporting pattern: materialized-view-backed read model

Several blueprints (3, 4, 9) rely on a read model. A PostgreSQL materialized view mapped with an
unmanaged model and refreshed from a scheduled task:

```python
# reports/tasks.py
from celery import shared_task
from django.db import connection

@shared_task(ignore_result=True)
def refresh_daily_revenue() -> None:
    with connection.cursor() as cur:
        # CONCURRENTLY needs a unique index on the MV [PostgreSQL]
        # [VERIFY: lock level and requirements of REFRESH MATERIALIZED VIEW CONCURRENTLY in PostgreSQL 16]
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY reports_daily_revenue")
```

Record the refresh time and expose it as `data_as_of` on every report built from the view
(view definition: [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)).

## 6. Production use cases

| Organisation | Blueprint combination |
|---|---|
| B2B SaaS invoicing product | 1 (invoices) + 8 (customer scheduled statements) + 9 (multi-tenant) |
| HR/payroll platform | 2 (payroll) + 6 (statutory filings) + 10 (historical headcount) |
| E-commerce operator | 4 (sales) + 5 (operational) + 7 (bulk exports for BI) |
| Finance back office | 3 (statements) + 10 (audit) + 6 (regulatory) |

The subsystem is shared: one `reports` app, one registry, one set of models, many `ReportSpec`s.
Blueprints differ in configuration (execution mode, retention, permissions, formats), not in
separate infrastructure.

## 7. Advantages and disadvantages

| Mode | Advantages | Disadvantages |
|---|---|---|
| Sync | Simplest; immediate | Request timeouts; ties up web workers; unbounded risk |
| Stream | Constant memory, immediate start | CSV/JSONL only in practice; partial files on failure; proxy config |
| Async on-demand | Bounded, retryable, observable | Queue latency; more moving parts; notification UX |
| Scheduled | Predictable load, ready on arrival | Stale between runs; delivery complexity; DST/time zone logic |
| Pre-generated / event-triggered | Immutable, reproducible, instant download | Storage and retention cost; regeneration rules needed |
| Read model / pre-aggregation | Fast repeated queries | Freshness lag; extra pipeline; reconciliation needed |

## 8. Security considerations

- Every blueprint enforces access through the same scoped selector and permission per `ReportSpec`;
  delivery channels never bypass it (an email link leads to an authenticated view).
- Classification drives controls: `restricted` (payroll, financial statements, regulatory) requires
  encryption at rest, audit of every access, no attachments, short link lifetimes.
- Immutable artifacts for legal documents must also be protected from deletion before their
  retention ends (object lock / restricted delete permissions).
- Regulatory blueprints add segregation of duties and evidence of submission.
- Details: [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Size capacity on the peak (month-end, Monday morning, year-end), not the average.
- Separate queues — e.g. light (invoices, payslips), default (statements, medium exports) and bulk
  (multi-million rows) — so one class cannot starve another.
- Pre-aggregation and snapshots are the main levers for repeated heavy reports; streaming and
  write-only modes are the main levers for one-off large ones.
- Fan-out blueprints (payslips, invoices) need bounded concurrency to protect the database.
- Details: [report-generation-performance.md](report-generation-performance.md).

## 10. Common mistakes and anti-patterns

- Regenerating legal documents (invoices, payslips) on every download instead of storing the issued artifact.
- One generic "export" endpoint that synchronously builds any report of any size.
- Scheduled reports that email attachments containing payroll data.
- Treating a regulatory submission as "just another CSV" without schema validation and evidence.
- Separate selectors for screen and export, producing different numbers.
- Multi-tenant reporting without per-tenant concurrency limits — one large tenant blocks everyone.
- Historical reports computed from current state.
- Building a separate reporting microservice before the in-process `reports` app has clear
  boundaries ([django-architecture — modular-monolith](../../django-architecture/references/modular-monolith.md)).

## 11. Troubleshooting and validation

Checklist to validate a chosen architecture before implementation:

- [ ] Requirements recorded: volume (rows, bytes), latency/SLA, freshness, reproducibility, audience, cadence, retention, classification.
- [ ] Execution mode chosen from the decision matrix, with the reason recorded (ADR if hard to reverse — [django-architecture — architecture-decision-records](../../django-architecture/references/architecture-decision-records.md)).
- [ ] One selector per report, shared by every output channel.
- [ ] Snapshot/`data_as_of` strategy defined for multi-query and historical reports.
- [ ] Storage, retention and deletion defined per classification.
- [ ] Delivery never bypasses authorisation; links expire.
- [ ] Failure behaviour defined: retries, reconciliation gates, user-visible errors, reaper.
- [ ] Peak-load capacity estimated; queues separated.
- [ ] Tests planned for correctness, file content, access, and failure paths.
- [ ] Metrics and alerts defined for the SLA ([reporting-observability-and-operations.md](reporting-observability-and-operations.md)).

When an existing architecture misbehaves, start from
[report-generation-troubleshooting.md](report-generation-troubleshooting.md).

## 12. Related references and official documentation

- [reporting-engineering-fundamentals.md](reporting-engineering-fundamentals.md)
- [reporting-architecture-and-design.md](reporting-architecture-and-design.md)
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)
- [background-report-generation.md](background-report-generation.md)
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md)
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md)
- [report-generation-performance.md](report-generation-performance.md)
- [report-generation-testing.md](report-generation-testing.md)
- [reporting-observability-and-operations.md](reporting-observability-and-operations.md)
- [django-architecture — architecture-decision-records](../../django-architecture/references/architecture-decision-records.md)
- [django-performance — performance-architecture-decisions](../../django-performance/references/performance-architecture-decisions.md)
- [django-data-processing — production-architectures-sync-reconciliation-and-documents](../../django-data-processing/references/production-architectures-sync-reconciliation-and-documents.md)
- [django-integrations — file-transfer-and-object-storage](../../django-integrations/references/file-transfer-and-object-storage.md)
- [django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md)
- [postgresql-engineering](../../postgresql-engineering/SKILL.md)
- <https://docs.djangoproject.com/en/5.2/topics/db/aggregation/>
- <https://docs.djangoproject.com/en/5.2/ref/request-response/>
- <https://docs.celeryq.dev/en/stable/userguide/routing.html>
- <https://www.postgresql.org/docs/16/sql-refreshmaterializedview.html>
- <https://doc.courtbouillon.org/weasyprint/stable/>
