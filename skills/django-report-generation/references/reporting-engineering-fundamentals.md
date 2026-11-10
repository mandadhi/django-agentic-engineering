# Reporting Engineering Fundamentals

> **Scope:** What a reporting system is, the kinds of reports and their delivery models, the report
> lifecycle, definitions/parameters/metadata/ownership/versioning as concepts, requirements gathering,
> accuracy and consistency, SLAs, and high-level architecture selection. **Not here:** concrete models
> and state machines → [reporting-architecture-and-design.md](reporting-architecture-and-design.md);
> query mechanics → [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md);
> worked blueprints → [production-reporting-architectures.md](production-reporting-architectures.md).

## 1. Purpose and scope

A report is a **claim about the business**, delivered as a file or a view: "revenue for October was
€1,204,330.17", "these 412 employees were paid", "this invoice is owed". When a report is wrong, people
make wrong decisions, pay the wrong amounts or file the wrong regulatory return. Reporting engineering
is therefore primarily a **correctness and trust** discipline, and only secondarily a formatting one.

This reference gives an engineering agent the vocabulary and decision framework to use *before*
choosing a library or writing a query:

- classify the report (what kind, who consumes it, what happens if it is wrong);
- decide how it is produced (on demand, scheduled, pre-generated; sync or async);
- write down the requirements that make "correct" testable;
- decide whether the task is "generate one report" or "build a reporting platform".

Use it when a new report is requested, when an existing report is disputed, or when reporting needs
are growing beyond ad-hoc views.

## 2. Core concepts

### 2.1 What constitutes a reporting system

| Component | Responsibility |
|---|---|
| Report definition | What the report is: key, version, parameters, data source, output formats, permission, classification |
| Parameter handling | Validate, normalise and authorise the inputs (period, tenant, filters) |
| Data acquisition | Scoped, consistent queries that compute the numbers — ideally in the database |
| Rendering | Turn a dataset into CSV/XLSX/PDF/DOCX/JSON, deterministically |
| Execution | Where and when generation runs: request, worker, scheduler |
| Artifact storage | Keep generated files privately with metadata, checksum and expiry |
| Delivery | Download endpoint, signed link, email, SFTP, webhook |
| Governance | Access control, audit, retention, ownership, versioning |
| Operations | Metrics, alerts, cleanup, runbooks |

A single CSV export view touches four of these. A reporting platform deliberately owns all nine.

### 2.2 Report categories

| Category | Purpose | Typical examples | Error tolerance | Freshness |
|---|---|---|---|---|
| Operational | Run today's work | Open orders, stock below reorder level, unpaid invoices due this week | Low tolerance for missing rows | Minutes to real time |
| Analytical | Understand trends | Revenue by region per month, cohort retention, product mix | Small lag acceptable; definitions must be stable | Hours to a day |
| Financial | Account for money | Invoice, customer statement, monthly P&L, AR aging, payroll register | Zero tolerance; must reconcile to the ledger | Period close |
| Regulatory / compliance | Prove something to an authority | Tax returns, payroll filings, audit extracts, data-subject access exports | Zero tolerance; format often prescribed; reproducible years later | Fixed deadlines |

The category drives most downstream decisions: financial and regulatory reports need snapshots,
versioning, reproducibility and audit; operational reports need freshness and speed.

### 2.3 Transactional vs analytical reporting

- **Transactional (OLTP) reporting** reads the live, normalised tables the application writes:
  "orders placed today", an invoice PDF. Small, selective, index-backed queries; current state.
- **Analytical (OLAP-style) reporting** aggregates large ranges: "revenue per product per month for
  three years". Scans, group-bys and windows; competes with the transactional workload for I/O and
  locks if run on the primary.

Running analytical reports against the OLTP primary is fine at small scale and becomes the classic
cause of "the site is slow at 9 a.m. on Mondays". The escalation path is: indexes → read replica →
pre-aggregated tables / materialized views → a separate warehouse (outside this skill's scope).

### 2.4 Real-time vs scheduled, on-demand vs pre-generated

| Axis | Option | Meaning |
|---|---|---|
| Freshness | Real-time | Computed from current data at request time |
| | Scheduled | Computed at a fixed cadence (daily 06:00 tenant time) and delivered |
| Production | On-demand | Generated when a user asks, with their parameters |
| | Pre-generated | Generated ahead of demand (month-end statements for every customer) and served from storage |

These axes are independent: a scheduler can trigger an on-demand-style run; a pre-generated
artifact can be "real-time as of 02:00". Always state the **data-as-of** time on the output so the
reader knows which one they are looking at.

### 2.5 Report lifecycle

```text
 requirement ─► definition (v1) ─► request (params) ─► authorise ─► acquire data ─► render
      ▲                                                                              │
      │                                                                              ▼
  retire/version ◄── retain/expire ◄── audit ◄── deliver/download ◄── store artifact + metadata
```

Each arrow is a place where reports go wrong: an unversioned definition change, unauthorised
parameters, an inconsistent read, a non-deterministic render, a lost artifact, a link that never
expires.

### 2.6 Definitions, parameters, metadata, ownership, versioning

- **Definition** — the stable identity of a report (`sales.weekly_revenue`), what it computes, the
  permission required and the formats it supports. In this skill definitions are code
  (`ReportSpec`, see [reporting-architecture-and-design.md](reporting-architecture-and-design.md)).
- **Parameters** — user-variable inputs (period, region, currency, include_voided). Parameters are
  untrusted input: validated, normalised, bounded and authorised.
- **Metadata** — facts about a produced report: who requested it, definition version, normalised
  parameters, data-as-of, row count, generated-at, checksum, classification, expiry.
- **Ownership** — a named team that owns the definition's *business meaning* (finance owns "net
  revenue") and an engineering owner of the implementation. Without an owner, disputes about numbers
  have nowhere to go.
- **Versioning** — a definition's version changes whenever the **meaning of the output** changes
  (a new exclusion rule, a different rounding policy, a renamed column in a contracted CSV). Each
  generated report records the version it was produced with so old outputs remain explainable.

### 2.7 Accuracy and consistency

| Property | Definition | Typical failure |
|---|---|---|
| Accuracy | Each number equals the agreed business definition applied to the source data | Float arithmetic, wrong status filter, join fan-out double counting |
| Completeness | All in-scope records are included, none from outside scope | Off-by-one-day period boundary, missing tenant filter |
| Internal consistency | Totals equal the sum of their lines; sections agree | Header and lines read at different moments |
| Cross-report consistency | The same metric matches across reports | Two "revenue" definitions in two codebases |
| Reproducibility | Re-running with the same inputs and data-as-of gives the same output | Reading mutable current state for a historical period |
| Timeliness | Available by the time it is needed | Month-end batch finishes after the board meeting |

### 2.8 Generating a report vs building a reporting platform

**Generating a report** is a feature: one view or task, one query, one format, one consumer. It is the
right answer when there are few reports, small data and no regulatory weight. Cost: hours.

**A reporting platform** is a subsystem: a definition registry, a run model with statuses, generic
parameter validation, pluggable renderers, private artifact storage, delivery channels, schedules,
audit, retention and metrics shared by every report. Cost: weeks, plus ongoing operations.

Escalate from "report" to "platform" when two or more of these are true:

- More than a handful of reports share parameter handling, permissions or formats.
- Any report takes longer than a request timeout or needs to be emailed/scheduled.
- Reports are financial/regulatory and need audit, versioning and reproducibility.
- Multiple tenants must never see each other's outputs, and outputs are stored.
- Support repeatedly asks "who downloaded what, when, with which parameters?"

Do not build the platform speculatively for the first report — but do put the first report's query
in a selector and its rendering in a separate function, so the platform can absorb it later.

## 3. Internal mechanics

### 3.1 How a number is produced

```text
source rows ─► scope (tenant, permission) ─► filter (period, status) ─► join ─► aggregate
           ─► derive (ratios, running totals) ─► round/format ─► render
```

Rules for each stage:

1. **Scope first**, through the same scoped selector the UI uses. A report is just another read path.
2. **Filter with half-open ranges** in the business time zone.
3. **Join carefully** — each multi-valued join multiplies rows before aggregation.
4. **Aggregate in the database** with exact types (`Decimal` / PostgreSQL `numeric`).
5. **Derive from aggregates**, not averages of averages (ratio of sums, not mean of ratios).
6. **Round once, at presentation**, with a documented rounding mode; keep unrounded values for totals.

### 3.2 Where truth lives

A report should read from the **system of record** for each fact, or from a derived store whose
lineage to it is documented. If invoice totals are stored denormalised on `Invoice.total` *and*
derivable from `InvoiceLine`, the definition must say which one is authoritative and a reconciliation
check should prove they agree
([django-data-processing — data-reconciliation-engineering](../../django-data-processing/references/data-reconciliation-engineering.md)).

### 3.3 Time and reports

Reports are almost always about periods, and periods are defined in a **local business time zone**
(the tenant's), while Django stores aware datetimes in UTC (`USE_TZ = True` [Django]). "October" for a
tenant in `America/New_York` is `[2026-10-01T04:00Z, 2026-11-01T04:00Z)`. Mechanics are in
[reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md).

## 4. Engineering decisions and trade-offs

### 4.1 High-level architecture selection

| Situation | Recommended architecture | Why | Trade-off |
|---|---|---|---|
| < ~5k rows, < ~2 s, single format, interactive | Synchronous view returning `HttpResponse` [Django] | Simplest; immediate | Blocks a web worker; must stay small |
| Large CSV, simple rows, user waits for download | `StreamingHttpResponse` [Django] over `.iterator()` | Bounded memory, no task infra | Holds a worker for the whole stream; no retry; proxy/timeout tuning |
| XLSX/PDF/DOCX of any real size, or > ~10 s | Async: `ReportRun` + Celery [third-party: celery] task + stored artifact + notify | Survives timeouts, retryable, auditable | Needs broker, workers, storage, status UI |
| Same report for many recipients on a cadence | Scheduled: dispatcher + async generation + distribution | Predictable load, off-peak | Staleness; schedule/time-zone complexity |
| Same output requested by many users (statements, invoices) | Pre-generated artifacts | Generate once, serve many times; immutable record | Storage cost; regeneration rules when data changes |
| Dashboards over large fact tables viewed often | Pre-aggregated tables / materialized views [PostgreSQL] + fast query | Cheap reads | Refresh lag; extra schema to maintain |
| Regulatory submission | Async + snapshot + versioned definition + immutable artifact + audit | Reproducibility and evidence | Most moving parts; slowest to build |

The thresholds are starting points; measure on production-sized data
([report-generation-performance.md](report-generation-performance.md)).

### 4.2 Other foundational decisions

| Decision | Default | Escalate when | Because |
|---|---|---|---|
| Definitions in code vs database | Code (`ReportSpec` registry), reviewed and tested | Business users must compose reports → constrained DB-defined filters over a fixed dataset | Arbitrary user-built queries are a security and performance hazard |
| Read primary vs replica | Primary for small/transactional reports | Analytical load harms OLTP → replica, accept and display lag | Replica lag breaks read-your-writes |
| Live data vs snapshot | Live for operational | Financial/regulatory → snapshot or closed-period data | Reproducibility |
| Format | CSV for data exchange, XLSX for analysts, PDF for fixed documents | DOCX only when recipients must edit | Each format has different streaming and fidelity limits |
| Delivery | Download link from an authorised page | Email only links (not attachments) for sensitive data | Attachments escape access control forever |

## 5. Implementation patterns

### 5.1 Requirements template (fill before writing code)

```text
Report key / title        : billing.ar_aging  "Accounts receivable aging"
Business owner            : Finance operations      Engineering owner: Billing team
Category                  : Financial               Classification: confidential
Consumers                 : Finance staff (tenant), external auditor (export)
Question answered         : How much is owed, by customer, by days overdue bucket, as of date D?
Definition rules          : Invoices status in (issued); exclude void; partial payments reduce balance;
                            buckets 0-30, 31-60, 61-90, >90 by due_date relative to D (tenant tz)
Parameters                : as_of_date (required, <= today), customer (optional, must be visible)
Formats                   : xlsx, pdf, csv
Volume                    : up to 40k open invoices per tenant
Freshness / SLA           : on demand, ready < 2 min p95; month-end run by 07:00 local on day 1
Consistency               : single snapshot; totals must equal ledger AR control account
Reproducibility           : re-runnable for any past as_of_date (needs payment history, not current balance)
Retention                 : artifacts 30 days; month-end artifacts per finance retention policy
Acceptance tests          : hand-computed fixture incl. partial payment, void, tz edge at midnight, empty tenant
```

The month-end retention line is a placeholder: [VERIFY: retention periods for financial artifacts
are set by the organisation's finance/legal policy and jurisdiction — confirm before implementing].

### 5.2 Questions that expose hidden requirements

- "If two people run it for the same period a week apart, must they get the same numbers?" →
  snapshot / as-of design.
- "What do you compare this number to?" → reconciliation target and tolerance.
- "Which time zone defines a day?" → tenant/business time zone, not the server's.
- "What happens to an invoice voided after the period closed?" → historical vs current-state rules.
- "Who must *not* see this?" → permission, row-level scope, classification.
- "What is the largest tenant's volume, and next year's?" → execution mode.
- "Is the column layout a contract with another system?" → versioning, deterministic serialization.

### 5.3 Report SLAs

Express SLAs in measurable terms that the observability layer can alert on
([reporting-observability-and-operations.md](reporting-observability-and-operations.md)):

| SLA type | Example target |
|---|---|
| Interactive latency | Sync exports p95 < 2 s |
| Async completion | 95% of on-demand runs succeed within 5 min of request |
| Scheduled readiness | Daily ops report delivered by 07:00 tenant local time on 99% of days |
| Freshness | Dashboard data-as-of no older than 15 min |
| Correctness | Month-end financial totals reconcile to ledger with zero difference |
| Availability of artifacts | Download links valid for 7 days; artifacts retained per policy |

## 6. Production use cases

- **Invoice PDF** — transactional, financial, on-demand or pre-generated at issue time; immutable
  once issued; must equal stored totals.
- **Monthly payroll register** — financial/regulatory, scheduled after `PayrollRun` approval; reads
  approved payslips only; restricted classification.
- **Weekly sales by region** — analytical, scheduled Monday 06:00 tenant time; pre-aggregated
  daily table; XLSX to subscribers.
- **Open orders export** — operational, on-demand CSV, streamed; live data.
- **Tax/regulatory extract** — regulatory, prescribed format, versioned definition, snapshot of a
  closed period, immutable artifact retained per legal requirement.

See [production-reporting-architectures.md](production-reporting-architectures.md) for full designs.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Ad-hoc report views | Fast to ship; no infrastructure | Duplicated scoping/formatting; no audit, history or retries |
| Reporting platform | Consistent security, audit, formats, operations | Up-front cost; abstraction can over-generalise |
| Live data | Fresh; no refresh pipeline | Non-reproducible; load on OLTP |
| Snapshots / pre-aggregates | Fast, reproducible | Staleness; extra storage and refresh jobs |
| Code-defined definitions | Reviewed, tested, versioned with the app | Business users can't self-serve |

## 8. Security considerations

- A report is a **bulk read** — the most efficient way to exfiltrate data. Classify every
  definition (`internal` / `confidential` / `restricted`) at requirements time.
- Reports must reuse the same authorisation and tenant scoping as interactive screens; "export" is
  never a privilege escalation.
- Stored artifacts are copies of data outside the database's access controls; they need their own
  access checks, expiry and deletion.
- Compliance obligations (GDPR, SOX, PCI DSS, HIPAA, local payroll and tax law) are **legal
  requirements** to confirm with legal/compliance; technical controls support them but do not by
  themselves make a system compliant.

Details: [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Volume and format decide the execution mode more than code quality does: a 200k-row XLSX will not
  fit a 30-second request regardless of query tuning.
- Analytical reports on the OLTP primary compete with user traffic; plan read replicas or
  pre-aggregation when report load is measurable on the primary.
- Write performance budgets into requirements (rows, seconds, MB) so regressions are detectable.

Details: [report-generation-performance.md](report-generation-performance.md).

## 10. Common mistakes and anti-patterns

- Starting with the library ("we'll use WeasyPrint") before the definition and consumers are known.
- Two implementations of the same metric (dashboard and export) that drift apart.
- No business owner, so numerical disputes are resolved by whoever touches the code.
- Changing a report's rules in place without a version bump — last month's file can no longer be
  explained.
- Reporting "current state" for a historical period (today's customer region applied to last year's
  revenue) without saying so.
- Omitting the data-as-of timestamp and parameters from the output.
- Treating "the task finished" as "the report is correct".
- Building a full platform (schedules, multi-channel delivery) for a single CSV export.

## 11. Troubleshooting and validation

| Symptom | First question | Owner |
|---|---|---|
| "The numbers don't match finance's" | Which definition and data-as-of does each side use? | [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) |
| "Last month's report changed" | Is the definition versioned and the data snapshotted? | [reporting-architecture-and-design.md](reporting-architecture-and-design.md) |
| "Export times out for big tenants" | Is the execution mode right for the volume? | [report-generation-performance.md](report-generation-performance.md) |
| Any other runtime symptom | — | [report-generation-troubleshooting.md](report-generation-troubleshooting.md) |

Validation checklist for a new report:

- [ ] Category, classification, business and engineering owners recorded.
- [ ] Definition rules written precisely enough to hand-compute a fixture.
- [ ] Time zone, period semantics, rounding mode and currency handling stated.
- [ ] Consistency and reproducibility requirements stated (live vs snapshot).
- [ ] Volume today and projected; execution mode chosen from the selection table.
- [ ] SLA expressed as measurable targets.
- [ ] Retention and audit requirements confirmed with the data owner.

## 12. Related references and official documentation

- [reporting-architecture-and-design.md](reporting-architecture-and-design.md) — concrete subsystem design and models.
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — correct, consistent data.
- [production-reporting-architectures.md](production-reporting-architectures.md) — blueprints and decision matrix.
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — classification, access, compliance boundaries.
- [django-architecture — service-layer](../../django-architecture/references/service-layer.md) — selectors and services.
- [django-architecture — architecture-decision-records](../../django-architecture/references/architecture-decision-records.md) — recording the platform decision.
- [django-data-processing — data-processing-fundamentals](../../django-data-processing/references/data-processing-fundamentals.md) — batch vs stream concepts.
- [django-performance — performance-architecture-decisions](../../django-performance/references/performance-architecture-decisions.md) — precomputation and read models.
- <https://docs.djangoproject.com/en/5.2/topics/db/aggregation/>
- <https://docs.djangoproject.com/en/5.2/topics/i18n/timezones/>
- <https://docs.python.org/3/library/decimal.html>
