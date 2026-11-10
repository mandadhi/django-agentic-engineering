# Production Architectures: Sync, Reconciliation and Documents

> **Scope:** Four reusable reference architectures — (4) financial reconciliation system,
> (5) bulk export service, (6) external system synchronisation, (7) document extraction and
> processing pipeline — each from a minimum viable variant to a scaled one, with requirements,
> components, data flow, failure handling, persistence, security, scaling, monitoring and
> trade-offs. **Not here:** reconciliation mechanics →
> [data-reconciliation-engineering.md](data-reconciliation-engineering.md); export models/UX →
> [data-import-and-export-systems.md](data-import-and-export-systems.md); API clients, webhooks and
> sync transport → [django-integrations](../../django-integrations/SKILL.md); PDF/archive parsing →
> [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md); architectures
> 1–3 and 8 → [production-architectures-imports-and-pipelines.md](production-architectures-imports-and-pipelines.md).

## 1. Purpose and scope

These blueprints cover workloads where correctness is judged against **another system or a
human**: a bank statement, a downstream spreadsheet user, a CRM, a scanned invoice. Each is given
as a **minimum viable (MV)** variant (Django + PostgreSQL + management command or one Celery
queue) and a **scaled** variant with explicit escalation triggers. Numbering matches the
skill-wide catalogue.

## 2. Core concepts

| Concept | Meaning here | Owner |
|---|---|---|
| Match rule | Deterministic predicate pairing source and target records (exact ref, amount ± tolerance, date window) | [data-reconciliation-engineering.md](data-reconciliation-engineering.md) |
| Exception | An unmatched/mismatched item needing human or automated resolution (`ReconciliationException`) | same |
| Snapshot consistency | An export reflects one point in time, not a moving target | [data-import-and-export-systems.md](data-import-and-export-systems.md) |
| External ID mapping | `(system, external_id) ↔ local_id` cross-reference; the backbone of sync | [data-synchronization-and-reconciliation.md](../../django-integrations/references/data-synchronization-and-reconciliation.md) |
| System of record (SoR) | Per entity **and per field**, which system wins | this file, §5.3 |
| Echo / loop | A change we pushed comes back as an inbound change and is re-pushed | this file, §5.3 |
| Confidence score | Extractor's estimate that a field value is correct; drives auto-accept vs review | this file, §5.4 |
| Human-in-the-loop (HITL) queue | Work queue of low-confidence extractions or exceptions for people | this file |

## 3. Internal mechanics

### 3.1 Shared skeleton

All four reuse the control loop of the companion file (job row → `on_commit` enqueue → atomic
claim → staged processing → terminal state) and add one of two outer loops:

```text
 (a) Compare loop  (4, 6):  load A ─► load B ─► match ─► classify differences ─► act / queue
 (b) Produce loop  (5, 7):  select input ─► transform ─► write artefact/records ─► verify ─► deliver
```

### 3.2 Determinism and human queues

Matching (4), conflict resolution (6) and confidence thresholds (7) must be **deterministic and
versioned**; store `rule_version` on every run and decision so auditors can explain any result
months later. Exceptions and review items move `OPEN → IN_REVIEW → RESOLVED | ESCALATED | DISMISSED`, with
assignee, reason code and audit trail. Use `select_for_update(skip_locked=True)` [Django] to hand
out items so two reviewers never work on the same one.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Daily bank-to-ledger matching, < 100k lines | (4) MV: nightly command, SQL matching | Escalate when intraday matching or multiple banks/currencies need parallel runs |
| Users need large data extracts | (5) async export to object storage + signed link | Streaming in-request beyond ~30 s or ~100k rows |
| Small, fast exports (< 10k rows) | `StreamingHttpResponse` [Django] | Holding whole files in memory |
| Two systems share customers/orders | (6) one-way sync with clear SoR first | Bidirectional only when both sides genuinely edit the same entity |
| Field-level bidirectional edits | (6) per-field SoR + version checks | Last-writer-wins on timestamps from different clocks |
| Digital PDFs with consistent layout | (7) text extraction + templates, no OCR | Adding OCR/ML before measuring digital-text accuracy |
| Scans, photos, varied layouts | (7) OCR or managed document-AI service + review queue | Fully automatic posting without review for financial data |

## 5. Django/Python implementation patterns

### 5.1 Architecture 4 — Financial reconciliation system

#### Requirements

- Match bank statement lines (CSV, MT940, ISO 20022 camt.053 XML) and payment-provider
  settlement reports against internal payments/ledger entries, daily, per account and currency.
- Exact money arithmetic (`decimal.Decimal` [stdlib]); configurable tolerances (fees, FX).
- One-to-one, one-to-many (batch payouts) and many-to-one (split payments) matches.
- Exceptions queue with four-eyes approval for manual matches; immutable audit; period close.

#### Components

- Statement ingestion (Architecture 2 pattern; format parsing per
  [file-processing-engineering.md](file-processing-engineering.md), XML via lxml/defusedxml).
- `ReconciliationRun`, `ReconciliationException`, plus `Match` (links source line ids ↔ target
  entry ids, rule, rule_version, matched_by).
- Matcher: ordered rule passes in SQL (exact reference → amount+date window → aggregate).
- Exception workbench (Django admin or custom views) with permission-checked actions.
- Provider APIs per [payment-and-financial-integrations.md](../../django-integrations/references/payment-and-financial-integrations.md).

#### Data flow

```text
 bank SFTP / API ──► StatementFile (sha256, account, period) ──► parse ──► StatementLine (staged)
 ledger / payments ─────────────────────────────────────────────► candidate entries (window)
                                   │
                    ReconciliationRun(account, date, rule_version)
                                   ▼
   pass 1: exact end-to-end reference          ─► Match(rule="ref")
   pass 2: amount == and |date Δ| ≤ 2 days      ─► Match(rule="amt_date")      unique candidate only
   pass 3: Σ(entries by payout_id) == line      ─► Match(rule="aggregate")
   pass 4: amount within tolerance (fees)       ─► Match(rule="tolerance", delta recorded)
                                   ▼
   leftovers ─► ReconciliationException(type=MISSING_IN_LEDGER | MISSING_IN_BANK |
                                         AMOUNT_MISMATCH | DUPLICATE)
                                   ▼
   control totals: opening + Σ lines == closing balance?  ── no ──► run FAILED (statement incomplete)
                                   ▼
   reviewers resolve ─► manual Match (maker) ─► approve (checker) ─► period close lock
```

#### Processing stages

1. Ingest + verify statement integrity (balance continuity with previous statement). 2. Select
candidate window. 3. Rule passes, most specific first; each pass only considers unmatched items
and only accepts **unique** candidates (ambiguous → exception, never a guess). 4. Raise
exceptions. 5. Report and notify. 6. Human resolution. 7. Close period (no further changes).

#### Failure handling

- Missing/duplicate statement (gap in sequence or balances) → permanent for that run; do not
  match partial data.
- Re-running a day: delete unapproved automatic matches for `(account, date)` and recompute;
  approved manual matches are preserved and excluded from candidates.
- Ledger changes after reconciliation → reopen affected matches via event, not silent overwrite.
- Detail: [data-reconciliation-engineering.md](data-reconciliation-engineering.md),
  [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

#### Persistence strategy

Statement lines immutable (corrections are new lines). `Match` rows reference both sides with a
unique constraint preventing an item from being matched twice (partial unique index on active
matches). Amounts as `DecimalField(max_digits=18, decimal_places=2)` (or per-currency minor units
as integers). All runs, rules and manual actions retained for audit periods required by
regulation (VERIFY: retention period for your jurisdiction).

#### Security

Maker-checker for manual matches and write-offs (different users, enforced in the service);
object-level permissions per account/entity
([authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md));
tamper-evident audit log; bank credentials/keys in a secrets manager; statement files encrypted at
rest and never emailed.

#### Scaling

MV: one nightly command per account set; SQL joins on `(account, amount, value_date)` with
indexes handle millions of lines. Scaled: parallel runs per account/currency (independent), an
intraday trigger on statement arrival, and partitioned statement tables by month.

#### Monitoring

Auto-match rate per rule and account, open exceptions by age and value, unreconciled balance,
statement arrival lateness, time-to-close. Alert on match-rate drops (usually upstream format
change).

#### Trade-offs

- **Simpler alternative:** export both sides to a spreadsheet for a finance user — acceptable
  only for very low volume; no audit trail.
- Fuzzy/ML matching raises auto-match rate but must stay **suggestions** that a human confirms in
  regulated contexts.
- Escalate to a dedicated reconciliation product when many entities, intercompany and FX
  revaluation rules dominate.

```python
# Pass 2 as a set-based query: unique amount+date candidates only.
from datetime import timedelta
from recon.models import StatementLine, LedgerEntry, Match

def match_amount_date(run, window: timedelta = timedelta(days=2)) -> int:
    created = 0
    lines = StatementLine.objects.filter(run=run, match__isnull=True)
    for line in lines.iterator(chunk_size=2_000):
        candidates = LedgerEntry.objects.filter(
            account=run.account, amount=line.amount, currency=line.currency,
            value_date__range=(line.value_date - window, line.value_date + window),
            match__isnull=True,
        )[:2]
        found = list(candidates)
        if len(found) == 1:                      # ambiguous (2) → left for exceptions
            Match.objects.create(run=run, line=line, entry=found[0],
                                 rule="amt_date", rule_version=run.rule_version)
            created += 1
    return created
```

Per-line queries are acceptable for the MV at tens of thousands of lines; at scale rewrite as a
single SQL statement with a window function counting candidates per line.

### 5.2 Architecture 5 — Bulk export service

#### Requirements

- Users/tenants request exports (CSV/XLSX/JSONL/Parquet) of up to tens of millions of rows with
  filters; consistent snapshot; download link valid for a limited time; history of exports.
- Exports must not degrade OLTP latency; formula injection neutralised; access-controlled.

#### Components

- `ExportJob` (filters, format, requested_by, status, file key, row count, checksum, expires_at)
  ([data-import-and-export-systems.md](data-import-and-export-systems.md)).
- Writer: `csv` [stdlib], openpyxl `Workbook(write_only=True)` [third-party: openpyxl],
  `pq.ParquetWriter` [third-party: pyarrow]. Rendering/formatting of report-style files →
  [django-report-generation](../../django-report-generation/SKILL.md).
- Object storage + short-lived signed URL, or an authenticated Django view returning
  `FileResponse` [Django] after a permission check.
- Optional read replica for export queries.

#### Data flow

```text
 user ─► POST /exports {filters, format} ─► ExportJob(PENDING) ─► on_commit enqueue ("exports" queue)
                                                     │
 worker: claim ─► open read-only snapshot (REPEATABLE READ txn or as_of timestamp filter)
           │
           ▼  keyset pages (pk > last, ORDER BY pk, LIMIT 5000) on replica
         serialise ─► neutralise formula cells ─► temp file ─► exports/<tenant>/<job>.csv.gz
           ▼                                                  (sha256, row_count)
 ExportJob(SUCCEEDED, expires_at=now+7d) ─► notify ─► GET /exports/<id>/download
                                                      (authz re-checked) ─► signed URL (5–15 min)
 retention task ─► delete expired files + mark EXPIRED
```

#### Processing stages

1. Validate request (filters whitelisted, row estimate vs limit). 2. Snapshot query. 3. Stream
serialise. 4. Upload. 5. Verify (row count, checksum). 6. Deliver link. 7. Expire.

#### Failure handling

- Worker crash → restart from scratch (exports are cheap to regenerate and must be consistent;
  resuming a half-written file across a different snapshot breaks consistency).
- Partial upload → abort multipart upload; never mark SUCCEEDED before upload completes.
- Over-limit request → reject at request time with guidance (narrow filters / scheduled delivery).
- Transient storage/DB errors → task retries with backoff.

#### Persistence strategy

Files in object storage with lifecycle rules as a backstop to the retention task; `ExportJob`
retained longer than the file for audit (who exported what, when, with which filters). Snapshot:
either one long `REPEATABLE READ` transaction (simple; holds back vacuum on large exports) or an
`as_of` timestamp filter on append-only/versioned data (VERIFY: acceptable transaction duration
with your DBA).

#### Security

Authorisation checked at request **and** download time; tenant-prefixed keys; signed URLs short
and never logged in full; `Content-Disposition: attachment`; formula injection neutralisation for
CSV/XLSX (OWASP); column-level masking for roles without PII rights; audit every export —
bulk export is a primary data-exfiltration path
([data-processing-security.md](data-processing-security.md)).

#### Scaling

< 10k rows: synchronous `StreamingHttpResponse` with a generator, no job. 10k–5M: MV async job,
one `exports` queue, keyset pagination. > 5M or many concurrent: read replica, per-tenant caps,
`COPY … TO STDOUT` for CSV, Parquet for machine consumers.

#### Monitoring

Export duration and size distribution, queue wait, failures by format, downloads per export
(unusual patterns → security review), storage used by unexpired exports.

#### Trade-offs

- **Simpler alternative:** synchronous streaming — no job table, but ties up a web worker and has
  no retry. **Escalate** to async when exports exceed ~30 s or users need history/re-download.
- XLSX is user-friendly but slow and memory-heavy even write-only; offer CSV for big data.
- Email attachments are an anti-pattern for anything sensitive or large — send links.

```python
import csv, gzip, io, tempfile
from django.core.files import File
from django.db import transaction
from exports.models import ExportJob
from exports.safety import neutralise_cell     # prefixes ' to = + - @ \t \r per policy

def generate_csv_export(job: ExportJob, page_size: int = 5_000) -> None:
    qs = job.build_queryset().order_by("pk").values_list(*job.columns, "pk")
    with tempfile.TemporaryFile() as tmp:
        with gzip.GzipFile(fileobj=tmp, mode="wb") as gz, \
             io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(job.headers)
            rows, last_pk = 0, 0
            with transaction.atomic():           # snapshot: REPEATABLE READ on this connection
                while page := list(qs.filter(pk__gt=last_pk)[:page_size]):
                    for *values, pk in page:
                        writer.writerow([neutralise_cell(v) for v in values])
                    rows, last_pk = rows + len(page), page[-1][-1]
        tmp.seek(0)
        job.file.save(f"{job.pk}.csv.gz", File(tmp), save=False)
    job.mark_succeeded(row_count=rows)
```

Keyset pages only form one consistent snapshot if the transaction runs at REPEATABLE READ — e.g.
a dedicated `exports` DB alias with `OPTIONS["isolation_level"]` set (VERIFY: psycopg 3
isolation option in Django 5.2 settings), or an `as_of` filter.

### 5.3 Architecture 6 — External system synchronisation

#### Requirements

- Keep entities (customers, products, orders, employees) consistent between Django app, ERP and
  CRM; one-way or bidirectional; near-real-time (minutes) plus nightly full reconciliation.
- External IDs maintained; no duplicates on retries; no infinite echo loops; explicit conflict
  policy; auditable change history.

#### Components

- `ExternalRef(system, entity_type, external_id, local_id, last_synced_version, last_synced_hash)`
  — unique on `(system, entity_type, external_id)` and `(system, entity_type, local_id)`.
- Outbound: transactional outbox table written in the same transaction as the local change;
  relay task pushes to the external API
  ([event-driven-architecture.md](../../django-architecture/references/event-driven-architecture.md)).
- Inbound: webhooks or polling with watermarks (Architecture 2 connectors).
- Conflict resolver with per-field SoR policy.
- Nightly full-compare reconciler (Architecture 4 compare loop, without money semantics).
- Transport, auth, rate limits → [http-and-rest-api-integrations.md](../../django-integrations/references/http-and-rest-api-integrations.md),
  [data-synchronization-and-reconciliation.md](../../django-integrations/references/data-synchronization-and-reconciliation.md).

#### Data flow

```text
            local edit (Django)                                  ERP / CRM edit
                  │ same txn                                           │
                  ▼                                                    ▼
   OutboxEvent(entity, version, changed_fields, origin="local")   webhook / poll (watermark)
                  │ relay task (ordered per entity key)                │
                  ▼                                                    ▼
   push via API (idempotency key = event id) ──► ERP/CRM      InboundChange(raw, ext version)
                  │                                                    │
   ExternalRef.last_synced_version/hash updated                         ▼
                                                     echo check: hash == last_synced_hash? → drop
                                                                       │
                                                     resolve conflicts per field SoR policy
                                                                       ▼
                                                     apply locally (origin="erp") — no outbox
                                                       event for fields owned by ERP
 nightly: full compare local vs remote ─► drift report / auto-repair per policy
```

#### Processing stages

1. Capture change (outbox or inbound). 2. Map to target schema (mapping version). 3. Echo
suppression (origin tag + content hash). 4. Conflict resolution. 5. Apply with idempotency key.
6. Update `ExternalRef`. 7. Periodic full reconciliation.

#### Conflict policy (decide per field, document in an ADR)

| Policy | Use when | Risk |
|---|---|---|
| Single SoR per entity | One system owns lifecycle (ERP owns invoices) | Users editing the non-owner see changes reverted |
| SoR per field | CRM owns contact details, ERP owns credit limit | Requires field-level change tracking |
| Version/ETag compare-and-set | Target API supports conditional updates | Retry/merge logic on 409/412 |
| Last-writer-wins by timestamp | Low-value fields only | Clock skew, silent data loss |
| Manual queue | High-value conflicts (bank details) | Human latency |

#### Failure handling

- Transient API errors → retry with backoff, preserving per-entity order (a later event must not
  overtake an earlier one for the same key — route by key or check versions on apply).
- Permanent (validation rejected by ERP) → park event in a failed-sync table with payload and
  error; alert owner; do not block other entities.
- External create succeeded but response lost → idempotency key or lookup by our reference
  stored on the external record before creating again.
- Drift found nightly → auto-repair only for fields with clear SoR; otherwise exception queue.

#### Persistence strategy

Outbox rows deleted/archived after acknowledgement; `ExternalRef` is permanent; inbound raw
changes retained for replay window; audit table of applied changes `(entity, field, old, new,
origin, rule_version)`.

#### Security

Per-system service credentials with least scope; webhook signature verification; never let
inbound data change authorisation-relevant fields (roles, tenant) without explicit policy;
PII minimisation — sync only fields the target needs.

#### Scaling

MV: outbox relay as a periodic task (every 30–60 s) on one queue, polling inbound every few
minutes. Scaled: per-system queues, concurrency bounded by API rate limits, ordered partitions by
entity key, CDC from the database instead of outbox polling when write volume is high (VERIFY:
operational cost of logical replication tooling).

#### Monitoring

Outbox backlog and oldest event age, sync lag per system, conflict counts by field, failed-sync
count, nightly drift count. Alert on backlog age, not size.

#### Trade-offs

- **Simpler alternative:** one-way nightly full sync with external IDs — covers many ERP→CRM
  needs with no conflict handling.
- Bidirectional sync multiplies failure modes; adopt only with per-field SoR documented.
- Escalate to a message broker/integration platform when > 3 systems need the same events
  ([messaging-and-event-driven-integrations.md](../../django-integrations/references/messaging-and-event-driven-integrations.md)).

```python
import hashlib, json
from django.db import transaction
from sync.models import ExternalRef, InboundChange
from sync.policy import FIELD_OWNER          # {"email": "crm", "credit_limit": "erp", ...}
from customers.models import Customer

def content_hash(fields: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()

def apply_inbound(change: InboundChange) -> None:
    with transaction.atomic():
        ref = ExternalRef.objects.select_for_update().get(
            system=change.system, entity_type="customer", external_id=change.external_id)
        if content_hash(change.fields) == ref.last_synced_hash:
            change.mark_skipped("echo"); return
        if change.external_version <= ref.last_synced_version:
            change.mark_skipped("stale"); return
        customer = Customer.objects.select_for_update().get(pk=ref.local_id)
        owned = {f: v for f, v in change.fields.items() if FIELD_OWNER.get(f) == change.system}
        for field, value in owned.items():
            setattr(customer, field, value)
        customer.save(update_fields=[*owned, "updated_at"])   # origin tagged; no outbox for these
        ref.last_synced_version, ref.last_synced_hash = change.external_version, content_hash(change.fields)
        ref.save(update_fields=["last_synced_version", "last_synced_hash"])
        change.mark_applied(fields=list(owned))
```

### 5.4 Architecture 7 — Document extraction and processing pipeline

#### Requirements

- Ingest supplier invoices, delivery notes, doctor credential certificates, expense receipts as
  PDF (digital or scanned) and images; extract structured fields (supplier, number, date,
  totals, line items); validate; post to the business system.
- Per-field confidence; auto-accept above threshold; human review queue otherwise; reviewers'
  corrections captured for quality measurement.
- Original document retained, immutable and linked to the resulting record.

#### Components

- Intake: upload, email-in, SFTP; `Document` (sha256, tenant, source, status, page_count).
- Sanitiser/inspector: type sniffing, page/size limits, encryption check
  ([columnar-archive-and-document-files.md](columnar-archive-and-document-files.md)).
- Text extractor for digital PDFs: pypdf [third-party: pypdf] (BSD), pdfplumber
  [third-party: pdfplumber] (MIT), or PyMuPDF [third-party: pymupdf] — **AGPL-3.0 or commercial
  licence**; using it in a network service may oblige you to release source unless licensed
  commercially (VERIFY: licence obligations with legal).
- OCR (optional): Tesseract via a wrapper, or managed services such as AWS Textract or Azure AI
  Document Intelligence ([aws-and-cloud-integrations.md](../../django-integrations/references/aws-and-cloud-integrations.md),
  [azure-integrations.md](../../django-integrations/references/azure-integrations.md))
  (VERIFY: current service names, pricing and data-residency terms).
- Field extractor (templates/regex per supplier, layout rules, or a model) emitting
  `ExtractedField(name, value, confidence, page, bbox, method)`.
- Validator (business rules: Σ lines == total, VAT math, supplier exists, duplicate invoice
  number) — [data-validation-and-cleansing.md](data-validation-and-cleansing.md).
- Review queue UI showing the page image beside fields; `ReviewTask` state machine.
- Queues: `docs_extract` (CPU, prefork), `docs_ocr` (slow/external), `docs_post`.

#### Data flow

```text
 upload/email/SFTP ─► Document(RECEIVED, sha256) ─► dedupe by (tenant, sha256)
        ▼
 inspect: type sniff, size/pages ≤ limit, encrypted? ── fail ──► REJECTED (reason)
        ▼
 classify: has text layer? ── yes ──► text extraction (pypdf/pdfplumber/PyMuPDF)
        │                   └─ no ──► [optional] OCR (local or managed) ─► text + word boxes
        ▼
 document type classification (invoice / receipt / certificate)
        ▼
 field extraction ─► ExtractedField(value, confidence, page, bbox, method, extractor_version)
        ▼
 business validation ─► rule failures lower confidence / force review
        ▼
 route: all required fields ≥ threshold AND validation passed ── yes ──► AUTO_ACCEPTED
        │                                                       no ──► ReviewTask(OPEN)
        ▼                                                                 │ reviewer corrects
 structured record (e.g. SupplierInvoice, DRAFT) ◄────────────────────────┘ (corrections stored)
        ▼
 post to ERP (Architecture 6 outbox) ─► Document(POSTED) ; metrics: auto-rate, field accuracy
```

#### Processing stages

Inspect → extract text (OCR if needed) → classify → extract fields → validate → score → route →
review → create record → post. Each stage persists its output on the `Document` so a failure in
OCR does not repeat inspection, and a new extractor version can be replayed over stored text.

#### Failure handling

- Corrupt/encrypted/oversized → permanent REJECTED with user-visible reason.
- OCR/document-AI service timeout or throttling → transient, retry with backoff on `docs_ocr`.
- Extraction exceptions on one document never affect others (one task per document).
- Low confidence is not a failure — it is a routing outcome.
- Duplicate invoice (same supplier + number) → review task, never auto-post.

#### Persistence strategy

Original file immutable in object storage (WORM/object lock for regulated records;
VERIFY: provider feature); extracted text and fields in PostgreSQL (`JSONField` for raw extractor output,
typed columns for accepted values); reviewer corrections stored as `(field, predicted, corrected,
reviewer)` to compute accuracy and tune thresholds.

#### Security

Documents are untrusted input: parse in a worker with resource limits (time, memory, pages),
ideally an isolated container with no network for local parsers; never render embedded
JavaScript/links; keep parsing libraries patched. Sending documents to a managed OCR service is a
data transfer — check contracts, region and retention. Review UI enforces tenant and role
scoping; certificates and medical documents are special-category data → strict access and
retention ([data-processing-security.md](data-processing-security.md)).

#### Scaling

| Stage | Bottleneck | Response |
|---|---|---|
| Text extraction | CPU | Prefork workers on `docs_extract`, `max_tasks_per_child` to contain leaks |
| OCR | CPU (local) or quota/latency (managed) | Separate queue; batch API where offered; cap concurrency to quota |
| Review | Humans | Raise auto-accept rate via per-supplier templates; prioritise by due date/value |

#### Monitoring

Auto-accept rate, per-field accuracy (from corrections), review queue size and age, OCR cost per
document, rejection reasons, end-to-end time from receipt to posting.

#### Trade-offs

- **Simpler alternative:** manual data entry UI with the PDF beside the form — often right for
  < 50 documents/day.
- Templates per supplier are precise but brittle; models generalise but need confidence
  calibration and review.
- Escalate to OCR/document-AI only after measuring how many documents lack a text layer; never
  remove the review queue for financial postings.

```python
from decimal import Decimal
from docs.models import Document, ReviewTask

REQUIRED = ("supplier_id", "invoice_number", "invoice_date", "total_gross")
THRESHOLD = {"total_gross": 0.98, "invoice_number": 0.95, "default": 0.90}

def route(doc: Document) -> str:
    fields = {f.name: f for f in doc.extracted_fields.filter(extractor_version=doc.extractor_version)}
    low = [n for n in REQUIRED
           if n not in fields or fields[n].confidence < THRESHOLD.get(n, THRESHOLD["default"])]
    lines_total = sum((Decimal(f.value) for f in doc.line_amount_fields()), Decimal("0"))
    rule_failures = []
    if "total_net" in fields and lines_total != Decimal(fields["total_net"].value):
        rule_failures.append("lines_do_not_sum")
    if doc.is_possible_duplicate():
        rule_failures.append("duplicate_invoice_number")
    if low or rule_failures:
        ReviewTask.objects.get_or_create(document=doc, defaults={
            "reasons": {"low_confidence": low, "rules": rule_failures}})
        return "REVIEW"
    doc.accept_automatically()
    return "AUTO_ACCEPTED"
```

## 6. Production use cases

| Business scenario | Architecture | Variant | Key decision |
|---|---|---|---|
| E-commerce: daily Stripe/PSP payouts vs orders vs bank | 4 | MV | Aggregate pass on payout ID; fee tolerance |
| Payroll: net pay file vs bank debit confirmation | 4 | MV | Zero tolerance; maker-checker for exceptions |
| Hospital: monthly staff roster export for 30 sites (XLSX) | 5 | MV | Write-only XLSX; role-based column masking |
| SaaS tenant "export all my data" (GDPR portability) | 5 | Scaled | JSONL/ZIP, replica, audited, 7-day expiry |
| ERP → CRM customers and credit limits; CRM → ERP contact details | 6 | Bidirectional, per-field SoR | Outbox + echo suppression |
| Product catalog master in Django → marketplace channels | 6 | One-way | Idempotent pushes, nightly drift check |
| Accounts payable invoice capture | 7 | Scaled | Per-supplier templates + review for totals |
| Doctor credential certificates (scans) for onboarding | 7 | MV + OCR | Always human review; strict retention |
| Employee expense receipts (photos) | 7 | Managed OCR | Duplicate detection; policy validation |

## 7. Advantages and disadvantages

| Architecture | Advantages | Disadvantages |
|---|---|---|
| 4 Reconciliation | Detects loss/duplication early; audit-ready | Rule tuning effort; exception backlog needs staffing |
| 5 Bulk export | Self-service data access; no web timeouts | Exfiltration risk; storage and snapshot costs |
| 6 Sync | Consistent data across systems; external IDs enable replay | Bidirectional complexity; ordering and echo bugs are subtle |
| 7 Document pipeline | Removes manual keying; traceable to source document | OCR cost/accuracy; licences (PyMuPDF AGPL); humans still needed |

## 8. Security and performance considerations

**Security**
- Financial/document data: maker-checker, immutable originals, tamper-evident audit logs
  ([security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)).
- Exports: authorise at request and download, short signed-URL expiry, tenant-scoped keys,
  formula neutralisation, role-based column masking.
- Sync: least-privilege per-system credentials ([secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md)),
  verified webhooks; inbound data never escalates privileges or crosses tenants
  ([multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)).
- Documents: untrusted parsers in isolated, resource-limited workers; PyMuPDF licence review;
  contractual/residency review before sending documents to managed OCR.

**Performance**
- Reconciliation: set-based SQL, indexes on match keys; per-line ORM only at MV volumes.
- Exports: keyset pagination or `COPY … TO STDOUT`, streamed upload, replica for big reads; long
  snapshot transactions delay vacuum.
- Sync: bounded by external rate limits — use batch APIs; keep per-entity ordering without
  serialising everything.
- Documents: extraction on prefork queues, OCR isolated, extracted text cached so field
  re-extraction skips OCR.
- Details: [data-processing-performance.md](data-processing-performance.md),
  [background-task-performance.md](../../django-performance/references/background-task-performance.md).

## 9. Common mistakes and anti-patterns

- Fuzzy auto-matching → wrong matches hide discrepancies → auto-accept only unique deterministic matches.
- Floats for amounts → off-by-a-cent mismatches → `Decimal` or integer minor units.
- Reconciling an incomplete statement → false exceptions → verify balance continuity first.
- Exports via `list(queryset)` → OOM → keyset pagination/streaming.
- Long-lived public download links → data leak → short signed URLs after re-authorisation.
- Last-writer-wins on wall clocks → silent overwrites → per-field SoR and version checks.
- No echo suppression → update loops, API quota exhaustion → origin tags + content hash.
- External creates without idempotency → duplicates after timeouts → idempotency key/reference lookup.
- OCR on every PDF → cost and latency for no gain → check for a text layer first.
- Auto-posting unvalidated extractions → wrong payments → totals math, duplicate checks, thresholds.
- PyMuPDF without licence review → AGPL obligations → pypdf/pdfplumber or a commercial licence.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Auto-match rate dropped overnight | Bank changed reference format | Inspect new statement lines; update parser/rule version |
| Item matched twice | Missing unique constraint on active matches | Partial unique index; recompute run |
| Export rows inconsistent (totals don't add up) | Pages read across a moving dataset | Single snapshot transaction or `as_of` filter |
| Export task killed / redelivered mid-way | Broker visibility timeout < export duration | Raise timeout; split exports; restart-from-scratch semantics |
| Customer flips between two values repeatedly | Echo loop / conflicting SoR | Origin tags, content hash, per-field ownership |
| Duplicate records in CRM | Lost create response retried | Idempotency key / external reference lookup |
| Review queue keeps growing | Thresholds too strict or new supplier layout | Per-field accuracy from corrections; add template |
| Extraction worker memory climbs | Parser leaks on malformed PDFs | `max_tasks_per_child`, page limits, isolate |

**Validation checklist**
- [ ] MV variant first; escalation triggers recorded in an ADR.
- [ ] Rules/policies versioned and stored with every decision.
- [ ] Money as `Decimal`; tolerances explicit; ambiguous matches never auto-accepted.
- [ ] Exports snapshot-consistent, streamed, neutralised, access-controlled, expiring, audited.
- [ ] Sync: `ExternalRef` uniqueness, outbox in same transaction, echo suppression, per-field SoR,
  nightly drift check.
- [ ] Documents: originals immutable, limits enforced, licences reviewed, review queue for
  low-confidence and all financial postings below threshold.
- [ ] Tests for reruns, duplicates and partial failure per [data-processing-testing.md](data-processing-testing.md).

## 11. Related references and skills

- In this skill (beyond those linked above): [django-background-processing.md](django-background-processing.md),
  [database-loading-and-persistence.md](database-loading-and-persistence.md),
  [data-processing-observability.md](data-processing-observability.md).
- [django-integrations](../../django-integrations/SKILL.md):
  [file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md),
  [integration-reliability-and-resilience.md](../../django-integrations/references/integration-reliability-and-resilience.md).
- [django-architecture: domain-events.md](../../django-architecture/references/domain-events.md),
  [django-orm: transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md).

## 12. Official documentation and external references

- Django `StreamingHttpResponse` / `FileResponse`: https://docs.djangoproject.com/en/5.2/ref/request-response/
- Django `select_for_update`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update
- Python `decimal`: https://docs.python.org/3/library/decimal.html
- Python `csv`: https://docs.python.org/3/library/csv.html
- PostgreSQL transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL COPY: https://www.postgresql.org/docs/current/sql-copy.html
- openpyxl write-only mode: https://openpyxl.readthedocs.io/en/stable/optimized.html
- PyMuPDF (licence and API): https://pymupdf.readthedocs.io/en/latest/
- django-storages: https://django-storages.readthedocs.io/en/latest/
- AWS Textract: https://docs.aws.amazon.com/textract/
- Azure AI Document Intelligence: https://learn.microsoft.com/azure/ai-services/document-intelligence/
- S3 presigned URLs: https://docs.aws.amazon.com/AmazonS3/latest/userguide/ShareObjectPreSignedURL.html
- OWASP CSV Injection: https://owasp.org/www-community/attacks/CSV_Injection
