# Data Reconciliation Engineering

> **Scope:** Proving that the data a pipeline delivered matches its source(s): source-to-target,
> count, field-level, aggregate and financial reconciliation; missing/duplicate/mismatch detection;
> tolerances; hashing and checksums; reports, status, exception management (`ReconciliationRun`,
> `ReconciliationException`) and reprocessing of mismatches. **Not here:** generic system-to-system
> sync patterns ([django-integrations data-synchronization-and-reconciliation.md](../../django-integrations/references/data-synchronization-and-reconciliation.md)),
> gateway/settlement API specifics ([payment-and-financial-integrations.md](../../django-integrations/references/payment-and-financial-integrations.md)),
> rejected-record handling ([data-validation-and-cleansing.md](data-validation-and-cleansing.md)),
> loading/UPSERT ([database-loading-and-persistence.md](database-loading-and-persistence.md)),
> reference architecture for a full financial reconciliation system
> ([production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md)).

## 1. Purpose and scope

Reconciliation is an **independent, after-the-fact check** that two (or more) representations of
the same business facts agree. Ingestion, validation and loading report on *what they did*;
reconciliation reports on *what is true*. They are not the same thing.

### Why reconcile when every step reported success

| Failure mode | Why the pipeline still says "SUCCEEDED" |
|---|---|
| Silent drop | A parser skipped a malformed line, `ignore_conflicts=True` swallowed a row, a `WHERE` filter excluded a status nobody anticipated |
| Over-eager filter | A transformation dropped `status="REVERSED"` rows that finance still needs |
| Timezone shift | A 23:30 local transaction stored as next-day UTC; daily totals differ per day, but the month sums agree |
| Duplicate delivery | At-least-once task delivery or a re-sent file loaded twice under a different batch id |
| Source restatement | The source corrected yesterday's data after you ingested it; your copy is now stale but internally consistent |
| Truncation / precision | Float conversion, `max_length` truncation, currency minor-unit confusion (cents vs units) |
| Partial page | An API returned 200 with a short page during an incident; pagination ended early |
| Mapping bug | A new `MappingDefinition` version maps `gross` into `net`; every row "validates" |

Reconciliation catches these because it recomputes facts from the **source of truth** and compares
them to the **target**, using a path that shares as little code as possible with the load path.

This reference covers: designing reconciliation checks, implementing them efficiently in
Django/PostgreSQL, recording results, and managing the exceptions they raise until resolution.

## 2. Core concepts

| Concept | Definition |
|---|---|
| Source of truth | The system whose value wins in a dispute (bank file for cash, HRIS for headcount, WMS for on-hand stock). Must be decided per field, not per system. |
| Record-count reconciliation | `count(source) == count(target)` per scope (day, batch, tenant). Cheap, catches gross loss, blind to offsetting errors. |
| Aggregate / control-total reconciliation | Sums/counts grouped by business keys (day, cost centre, currency). Control totals are totals the *source* declares (file trailer, settlement report header). |
| Field-level reconciliation | Compare each matched record field by field. Expensive; definitive. |
| Key matching | Pair records via a business key (employee number + period, payment reference, SKU + warehouse). Matching quality bounds reconciliation quality. |
| Missing in target | Key exists in source, not in target (dropped). |
| Missing in source | Key exists in target, not in source (phantom, stale, or loaded from elsewhere). |
| Mismatch | Key in both, compared fields differ beyond tolerance. |
| Duplicate | Key appears more than once on a side where it should be unique. |
| Tolerance | Accepted difference: absolute (±0.01), relative (±0.1 %), or temporal (±1 day settlement lag). |
| Row hash | Digest of a canonical serialisation of compared fields; equal hash ⇒ equal fields (modulo collisions, negligible for SHA-256). |
| Bucket hash | Hash over the sorted row hashes of a key range/bucket; lets you find *which* bucket differs without comparing every row. |
| Exception | A persisted, assignable discrepancy with lifecycle OPEN → INVESTIGATING → RESOLVED/ACCEPTED. |

Reconciliation is **multi-level**: run cheap checks first (counts, control totals), and only drill
into field-level comparison where aggregates disagree or where the domain requires it (payroll and
payments always require it).

## 3. Internal mechanics

### 3.1 Matching algorithms

| Approach | How it works | Memory | When |
|---|---|---|---|
| SQL anti-join | Both sides in the same DB (source landed in staging): `LEFT JOIN … WHERE t.key IS NULL` | DB-side | Default when both sides can be landed in PostgreSQL |
| Set difference on key chunks | Partition key space (hash(key) % N or key ranges); per partition load key sets into Python `set`s and diff | O(partition) | Source is an API/file you cannot load to DB |
| Sorted merge | Both streams sorted by key; walk them in lockstep like a merge join | O(1) | Huge files already sorted, or sortable externally |
| Hash-bucket descent | Compare bucket hashes; recurse only into differing buckets | O(buckets) | Two databases/services, expensive transfer, few differences expected |

Sorted merge in detail: advance the pointer with the smaller key; equal keys ⇒ compare fields;
source-only ⇒ MISSING_IN_TARGET; target-only ⇒ MISSING_IN_SOURCE; consecutive equal keys on one
side ⇒ DUPLICATE. It requires **identical collation and normalisation of keys on both sides**
(PostgreSQL `ORDER BY` collation vs Python string ordering differ for non-ASCII — sort with
`COLLATE "C"` or sort on normalised bytes).

### 3.2 Canonical serialisation for hashing

Two equal records must serialise to identical bytes. Normalise before hashing:

- Fixed field order (explicit list, not dict iteration order of whatever produced the row).
- `Decimal` → quantised string (`"1200.50"`, never `1200.5` vs `1200.50`); never float.
- Datetimes → UTC ISO-8601 with fixed precision; dates → `YYYY-MM-DD`.
- Strings → Unicode NFC, stripped per business rule; case-fold only if the business key is
  case-insensitive.
- `None` → an explicit sentinel distinct from empty string.
- Field separator that cannot occur in values (use JSON encoding of a list, which escapes).

### 3.3 Where the comparison runs

Comparisons should run off a **snapshot**: either source extract stored with its checksum plus a
target query under `REPEATABLE READ`, or a target "as of" timestamp. Comparing a moving target
produces phantom mismatches that disappear on re-run, which trains people to ignore exceptions.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Both datasets can be in PostgreSQL | Land source in staging, SQL anti-joins + `GROUP BY` aggregates | Escalate to bucket hashing if source landing is too costly (> tens of GB/day) |
| Source is a file with trailer control totals | Validate trailer count/sum before load; reconcile again after load | Never trust trailer alone — it is produced by the same system that may be wrong |
| Payroll / payments | Full field-level, every run, zero tolerance on amounts (or ±0.01 rounding by rule) | Sampling — regulators and auditors expect completeness |
| Inventory quantities | Field-level on quantity with temporal tolerance (in-flight movements) | Zero-tolerance snapshot compare during business hours (false positives) |
| CRM ↔ app customers | Hash-based change detection, field-level only on differing hashes | Comparing free-text fields exactly (formatting noise) |
| Very large, few diffs expected | Bucket hashes, descend into differing buckets | Many diffs (bucket descent degenerates to full compare — just compare) |
| Daily ops dashboards | Count + aggregate per day | Using only totals for money (offsetting errors cancel out) |

### Tolerance types

| Tolerance | Formula | Use |
|---|---|---|
| Absolute | `abs(a - b) <= tol` | Currency rounding (±0.01) |
| Relative | `abs(a - b) <= tol * max(abs(a), abs(b))` | FX-converted totals, sensor readings |
| Temporal | match within ±N days/minutes | Settlement lag, device clock drift |
| Rounding-aware | compare after `quantize` with the rule both sides use | Tax/gross-to-net differences |

Always apply tolerance to **Decimal** values. Relative tolerance around zero must fall back to
absolute (relative of 0 is 0).

### Hash vs field compare

| | Row hash | Field-by-field |
|---|---|---|
| Transfer cost | 32 bytes/row | full rows |
| Explains *what* differs | No | Yes |
| Sensitive to normalisation bugs | Very (one stray space ⇒ all mismatched) | Less |
| Best use | Detecting *which* rows differ | Explaining *how* they differ |

Combine: hash to find candidates, field compare to describe them in the exception.

## 5. Django/Python implementation patterns

### 5.1 Models

```python
# reconciliation/models.py  [Django]
from django.conf import settings
from django.db import models


class ReconciliationRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING"
        BALANCED = "BALANCED"          # no exceptions above tolerance
        UNBALANCED = "UNBALANCED"      # exceptions raised
        FAILED = "FAILED"              # the check itself crashed — NOT the same as unbalanced

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    check_name = models.CharField(max_length=100)          # "payroll_vs_bank"
    check_version = models.CharField(max_length=20)        # bump when logic/tolerance changes
    scope = models.JSONField()                             # {"period": "2026-10", "entity": "UK01"}
    source_ref = models.CharField(max_length=255)          # file key / extract id
    source_checksum = models.CharField(max_length=64, blank=True)
    target_as_of = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    source_count = models.BigIntegerField(null=True)
    target_count = models.BigIntegerField(null=True)
    source_total = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    target_total = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    exception_counts = models.JSONField(default=dict)      # {"MISMATCH": 3, ...}
    pipeline_run_id = models.UUIDField(null=True)          # link to PipelineRun (etl)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        indexes = [models.Index(fields=["tenant", "check_name", "-started_at"])]
```

```python
class ReconciliationException(models.Model):
    class Type(models.TextChoices):
        MISSING_IN_TARGET = "MISSING_IN_TARGET"
        MISSING_IN_SOURCE = "MISSING_IN_SOURCE"
        MISMATCH = "MISMATCH"
        DUPLICATE = "DUPLICATE"

    class Severity(models.TextChoices):
        LOW = "LOW"; MEDIUM = "MEDIUM"; HIGH = "HIGH"; CRITICAL = "CRITICAL"

    class Resolution(models.TextChoices):
        OPEN = "OPEN"
        INVESTIGATING = "INVESTIGATING"
        FIXED_TARGET = "FIXED_TARGET"        # reprocessed / corrected in our system
        FIXED_SOURCE = "FIXED_SOURCE"        # source restated
        ACCEPTED = "ACCEPTED"                # known, justified difference (needs note + approver)
        AUTO_CLEARED = "AUTO_CLEARED"        # disappeared on a later run

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="exceptions")
    type = models.CharField(max_length=20, choices=Type.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    business_key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)          # sha256(check_name|key|type) — dedupes across runs
    field_diffs = models.JSONField(default=dict)           # {"net_pay": {"source": "2010.00", "target": "2001.00"}}
    amount_delta = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    resolution = models.CharField(max_length=15, choices=Resolution.choices, default=Resolution.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    first_seen_run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "resolution", "severity"]),
                   models.Index(fields=["fingerprint"])]
```

`fingerprint` lets a re-run update an existing open exception (`first_seen_run` preserved) instead
of creating a duplicate, and lets you AUTO_CLEAR exceptions not reproduced by a later run.
`field_diffs` may contain PII — see [data-processing-security.md](data-processing-security.md).

### 5.2 Canonical row hash

```python
import hashlib, json, unicodedata
from datetime import date, datetime, UTC
from decimal import Decimal, ROUND_HALF_EVEN   # [stdlib]

_NULL = "\x00NULL"

def _canon(v: object) -> object:
    match v:
        case None: return _NULL
        case Decimal(): return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
        case datetime(): return v.astimezone(UTC).isoformat(timespec="seconds")
        case date(): return v.isoformat()
        case str(): return unicodedata.normalize("NFC", v.strip())
        case bool() | int(): return v
        case float(): raise TypeError("float in reconciled data — convert to Decimal upstream")
        case _: return str(v)

def row_hash(row: dict[str, object], fields: tuple[str, ...]) -> str:
    payload = json.dumps([_canon(row[f]) for f in fields], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Quantising every Decimal to 2 places is only correct for currency fields; in real code drive the
scale per field from a spec (`{"hours": "0.01", "rate": "0.0001"}`).

### 5.3 Missing/duplicate detection in SQL (both sides in PostgreSQL)

```sql
-- staging_bank_line(run_id, payment_ref, amount) loaded from bank file; payroll_payment is target
-- Missing in target
SELECT s.payment_ref, s.amount
FROM staging_bank_line s
LEFT JOIN payroll_payment p ON p.payment_ref = s.payment_ref AND p.period = %(period)s
WHERE s.run_id = %(run_id)s AND p.id IS NULL;

-- Duplicates in target
SELECT payment_ref, count(*) FROM payroll_payment
WHERE period = %(period)s GROUP BY payment_ref HAVING count(*) > 1;

-- Aggregate by day (control totals); compare date in the business timezone, not UTC
SELECT (paid_at AT TIME ZONE 'Europe/London')::date AS d, currency, sum(amount), count(*)
FROM ledger_entry WHERE tenant_id = %(t)s AND paid_at >= %(start)s AND paid_at < %(end)s
GROUP BY 1, 2;
```

`NOT EXISTS` and `LEFT JOIN … IS NULL` normally plan as anti-joins in PostgreSQL (VERIFY:
`EXPLAIN` on your data); avoid `NOT IN (subquery)`
— it returns no rows if the subquery yields any NULL. Index the join key on both tables.

### 5.4 Sorted-merge reconciler for streams that cannot be landed

```python
from collections.abc import Iterator
from typing import NamedTuple

class Rec(NamedTuple):
    key: str
    h: str          # row_hash
    row: dict

def merge_reconcile(src: Iterator[Rec], tgt: Iterator[Rec]) -> Iterator[tuple[str, str, Rec | None, Rec | None]]:
    """Both iterators MUST be sorted by key with the same ordering. Yields (type, key, s, t)."""
    _END = Rec("￿" * 8, "", {})
    s, t = next(src, _END), next(tgt, _END)
    prev_s = prev_t = None
    while s is not _END or t is not _END:
        if s is not _END and s.key == prev_s:
            yield ("DUPLICATE", s.key, s, None); prev_s = s.key; s = next(src, _END); continue
        if t is not _END and t.key == prev_t:
            yield ("DUPLICATE", t.key, None, t); prev_t = t.key; t = next(tgt, _END); continue
        if t is _END or (s is not _END and s.key < t.key):
            yield ("MISSING_IN_TARGET", s.key, s, None); prev_s = s.key; s = next(src, _END)
        elif s is _END or t.key < s.key:
            yield ("MISSING_IN_SOURCE", t.key, None, t); prev_t = t.key; t = next(tgt, _END)
        else:
            if s.h != t.h:
                yield ("MISMATCH", s.key, s, t)
            prev_s, prev_t = s.key, t.key
            s, t = next(src, _END), next(tgt, _END)
```

The sentinel approach assumes keys sort below it; use an explicit `exhausted` flag in production.
The target stream is a keyset-paginated query ordered by key (see
[large-dataset-processing.md](large-dataset-processing.md)); the source stream must be sorted
identically — verify ordering in the loop and fail the run (status FAILED) if a key goes backwards.

### 5.5 Tolerance comparison and field diffs

```python
from decimal import Decimal

def within(a: Decimal, b: Decimal, *, abs_tol: Decimal = Decimal("0"), rel_tol: Decimal = Decimal("0")) -> bool:
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    scale = max(abs(a), abs(b))
    return scale != 0 and diff <= rel_tol * scale

def field_diffs(s: dict, t: dict, spec: dict[str, dict]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for field, rule in spec.items():
        a, b = s.get(field), t.get(field)
        if isinstance(a, Decimal) and isinstance(b, Decimal):
            if within(a, b, **rule):
                continue
        elif a == b:
            continue
        out[field] = {"source": str(a), "target": str(b)}
    return out
```

### 5.6 Recording exceptions idempotently

```python
from django.db import transaction
from django.utils import timezone

def record_exceptions(run: ReconciliationRun, found: list[ReconciliationException]) -> None:
    with transaction.atomic():
        open_qs = ReconciliationException.objects.filter(
            tenant=run.tenant, run__check_name=run.check_name,
            resolution__in=["OPEN", "INVESTIGATING"])
        existing = {e.fingerprint: e for e in open_qs.select_for_update()}
        seen: set[str] = set()
        new: list[ReconciliationException] = []
        for exc in found:
            seen.add(exc.fingerprint)
            if (cur := existing.get(exc.fingerprint)) is not None:
                cur.run, cur.field_diffs, cur.amount_delta = run, exc.field_diffs, exc.amount_delta
                cur.save(update_fields=["run", "field_diffs", "amount_delta"])
            else:
                exc.first_seen_run = run
                new.append(exc)
        ReconciliationException.objects.bulk_create(new, batch_size=1000)
        # Only auto-clear inside the SAME scope the run covered (filter by scope in real code).
        (open_qs.exclude(fingerprint__in=seen)
            .update(resolution="AUTO_CLEARED", resolved_at=timezone.now()))
```

Bulk semantics (no `save()`/signals) apply — see [database-loading-and-persistence.md](database-loading-and-persistence.md).
For very large `seen` sets, write fingerprints to a temp table and anti-join instead of `IN (...)`.

### 5.7 File checksum validation

```python
import hashlib  # [stdlib]
from django.core.files.storage import default_storage  # [Django]

def file_sha256(name: str) -> str:
    with default_storage.open(name, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()   # Python >= 3.11
```

Compare against the checksum the sender published (manifest file, API header, SFTP `.sha256`
sidecar). Store it on `ReconciliationRun.source_checksum` so a later re-run can prove it used the
same bytes. Object-store ETags are not reliable SHA/MD5 digests for multipart uploads
(VERIFY: provider ETag semantics for multipart/encrypted objects).

### 5.8 Reports, status and audit trail

A reconciliation report is generated from `ReconciliationRun` + its exceptions, never recomputed
separately. Minimum content:

```text
Check: payroll_vs_bank v3   Scope: entity=UK01 period=2026-10   Status: UNBALANCED
Source: bank/2026-10/UK01_payments.csv  sha256=9f2c…  Target as of: 2026-10-28T18:00:00Z
                 Source        Target        Delta
Count            4,812         4,811         -1
Net total GBP    9,843,120.55  9,841,110.55  -2,010.00
Exceptions: MISSING_IN_TARGET=1 (CRITICAL)  MISMATCH=0  DUPLICATE=0  MISSING_IN_SOURCE=0
Open from previous runs: 2   Auto-cleared this run: 1
```

Rendering to PDF/XLSX belongs to [django-report-generation](../../django-report-generation/SKILL.md);
exported reports need formula-injection neutralisation ([data-processing-security.md](data-processing-security.md)).

Audit trail: every resolution change writes an append-only event (exception id, actor, old → new
resolution, note, timestamp). A simple `ReconciliationExceptionEvent` model or a generic audit log
works; `django-simple-history`-style packages are an option [third-party] (VERIFY: package fits
your audit/retention requirements). Run rows themselves are immutable after `finished_at` — a
re-check is a new run, so the history shows how the discrepancy evolved.

## 6. Production use cases

### 6.1 Payroll: HRIS vs payroll engine vs bank file (three-way)
- **HRIS → payroll:** active headcount per entity/period; per employee: salary, cost centre,
  bank-account hash. MISSING_IN_TARGET = employee not paid (CRITICAL). MISSING_IN_SOURCE = paid
  leaver (CRITICAL — possible fraud).
- **Payroll → bank file:** count and sum of net pay per currency must equal bank file trailer *and*
  the summed lines; per employee net pay with zero tolerance. Hash bank account numbers; never put
  them in `field_diffs` in clear.
- Run before release of the bank file (blocking gate) and after bank confirmation (proof).

### 6.2 Payments: gateway settlement vs ledger
Settlement reports group charges, refunds, chargebacks and fees per payout. Reconcile:
`gross - fees - refunds == payout` per payout (control total), then per transaction: ledger entry
exists, amount and currency equal, status consistent. Temporal tolerance for settlement lag (a
charge on day D may settle D+2). Gateway API specifics →
[payment-and-financial-integrations.md](../../django-integrations/references/payment-and-financial-integrations.md).

### 6.3 Attendance: biometric devices vs HR system
Device punches are raw events (device clock may drift; devices go offline and batch-upload).
Reconcile per employee per *local* day: punch count, first-in/last-out with ±5 min temporal
tolerance, worked hours with ±0.05 h. Typical exceptions: device reported punches for a terminated
badge (MISSING_IN_SOURCE in HR), shift crossing midnight assigned to wrong day (timezone/day-boundary bug).

### 6.4 Inventory: WMS vs ERP
On-hand per SKU per warehouse. Exclude in-flight movements (`movement.created_at > snapshot_at -
lag`) from both sides or accept a temporal tolerance. Aggregate first (per warehouse total units,
total valuation) then drill into SKUs where the warehouse total disagrees. Persist a `snapshot_at`
on the run — comparing live tables during picking hours yields noise.

### 6.5 Customers: CRM vs application
Compute row hashes over normalised (email lower-case, phone E.164, name NFC) fields on both sides
nightly; field-diff only rows whose hashes differ. DUPLICATE on email in CRM is common and should be
routed to data stewards, not auto-merged.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Detects silent loss that logs and job status cannot | Additional compute and storage (staging copies of source) |
| Gives auditors evidence of completeness and accuracy | Matching rules are domain work; wrong keys produce noise |
| Turns incidents into tracked, assignable exceptions | False positives from timing/normalisation erode trust quickly |
| Detects source restatements and duplicate delivery | Requires an independent read path (more code to maintain) |
| Enables safe reprocessing (prove before/after) | Exception backlog needs ownership, or it becomes shelfware |

## 8. Security and performance considerations

**Security**
- Exceptions and reports contain business-sensitive values (salaries, card fingerprints). Store
  only what is needed in `field_diffs`; mask or hash identifiers (bank accounts → last 4 + hash).
  Masking/redaction rules → [data-processing-security.md](data-processing-security.md).
- Scope every query and exception by `tenant`; the reconciliation task must receive `tenant_id`
  and re-scope (never "all tenants" in one comparison set).
- `ACCEPTED` resolutions on financial checks require a different approver than the assignee
  (four-eyes); record both in the audit trail.
- Exceptions are append-mostly evidence: forbid deletion via permissions; resolution changes are
  audited (who, when, old → new, note).
- Checksums prove integrity only if the expected checksum comes from a separate trusted channel.

**Performance**
- Push comparison into PostgreSQL when both sides are there: anti-joins with indexes on the key
  beat Python sets by orders of magnitude.
- Python key sets: roughly 100 bytes per short string key including set overhead (VERIFY:
  measure with your key shape) — 10 M keys ≈ 1 GB+. Partition by
  `hash(key) % N` (deterministic hash, *not* Python `hash()` which is salted per process) or
  use sorted merge.
- Aggregate checks first; field-level only for failing groups unless the domain demands full.
- Bucket hashes: store per-row hashes in an indexed column at load time so reconciliation does not
  recompute them over the whole table.
- Run under `REPEATABLE READ` or against a snapshot table to avoid phantom mismatches; keep such
  transactions short (long snapshots delay vacuum) — see
  [transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md).

## 9. Common mistakes and anti-patterns

- Reconciling with the same code that loaded the data → shared bugs are invisible → use an
  independent read path (raw landed source, different query).
- Only totals for money → +100/−100 errors cancel → add per-record comparison.
- Comparing UTC dates against a business that closes days in local time → off-by-one-day
  mismatches at midnight → group by `AT TIME ZONE <business tz>`.
- Float arithmetic in tolerance checks → spurious 0.0000001 mismatches → Decimal throughout.
- `NOT IN (subquery)` with nullable keys → zero missing rows reported → `NOT EXISTS`.
- Marking run FAILED when exceptions exist → conflates "check crashed" with "data differs" →
  separate `UNBALANCED` and `FAILED`.
- New exception row every run for the same issue → thousands of duplicates → fingerprint + upsert.
- Auto-resolving by "re-running until clean" → masks flapping issues → AUTO_CLEARED is visible and
  reported; repeated flapping escalates severity.
- Storing full source rows (with PII) in exceptions forever → privacy liability → minimal diffs
  plus retention policy.
- Relative tolerance at zero values → division by zero or always-pass → absolute fallback.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Every row MISMATCH after a deploy | Canonicalisation changed (decimal scale, whitespace, timezone) | Diff canonical payloads for one key; bump `check_version`; never change hash spec silently |
| Mismatches vanish on re-run | Comparing live, moving data | Snapshot / `target_as_of`; temporal tolerance |
| Daily totals off, monthly totals equal | Day-boundary timezone shift | Group in business timezone on both sides |
| Count equal, sums differ | Duplicate + missing offsetting, or precision | Run duplicate and missing checks; inspect Decimal scale |
| Target count > source count | Duplicate delivery or re-sent file | DUPLICATE check on business key; check ingestion batch dedupe |
| Sorted merge reports massive missing on both sides | Different ordering/collation | Compare first 100 keys of each side; sort with `COLLATE "C"` or bytes |
| Run takes hours | Python-side set diff on full dataset | Land source, SQL anti-join; or partition |
| Exceptions not cleared after fix | Fingerprint includes run id or volatile data | Fingerprint = check + key + type only |

**Validation checklist**
- [ ] Source of truth defined per field; tolerances documented per field with business sign-off.
- [ ] Count, control-total, missing, duplicate and mismatch checks all implemented for the scope.
- [ ] Hash canonicalisation unit-tested (Decimal scale, None vs "", timezone, NFC).
- [ ] Run statuses distinguish BALANCED / UNBALANCED / FAILED.
- [ ] Exceptions deduplicated by fingerprint; auto-clear limited to the run's scope.
- [ ] Sensitive values masked in `field_diffs`; tenant scoping enforced.
- [ ] Reprocessing a mismatch triggers a follow-up run that proves resolution.
- Tests for these behaviours → [data-processing-testing.md](data-processing-testing.md).

### Reprocessing mismatches
1. Classify root cause from the exception: target bug (mapping/load) vs source restatement vs
   timing.
2. Target bug: fix code/mapping, reprocess only affected keys (pass key list to the reprocess
   job), idempotent UPSERT ([database-loading-and-persistence.md](database-loading-and-persistence.md)).
3. Source restatement: re-ingest the period (new `IngestionBatch`), then reload.
4. Re-run the same check with the same scope → exceptions become AUTO_CLEARED or FIXED_*;
   link the reprocess job id in `notes`. Procedures → [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

## 11. Related references and skills

- [data-processing-fundamentals.md](data-processing-fundamentals.md) — consistency and delivery semantics.
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — where the reconcile stage sits in a pipeline.
- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — `RejectedRecord` (rejected ≠ missing; reconcile must count rejects).
- [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) — reprocessing and recovery workflows.
- [data-processing-observability.md](data-processing-observability.md) — alerting on unbalanced runs.
- [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md) — financial reconciliation reference architecture.
- [django-integrations: data-synchronization-and-reconciliation.md](../../django-integrations/references/data-synchronization-and-reconciliation.md)
- [django-integrations: payment-and-financial-integrations.md](../../django-integrations/references/payment-and-financial-integrations.md)
- [django-orm: transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md)
- [django-security-engineering: security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)
- [../../postgresql-engineering/SKILL.md](../../postgresql-engineering/SKILL.md) — anti-join plans, indexes.

## 12. Official documentation and external references

- Python `hashlib` (`file_digest`): https://docs.python.org/3/library/hashlib.html
- Python `decimal`: https://docs.python.org/3/library/decimal.html
- Python `unicodedata.normalize`: https://docs.python.org/3/library/unicodedata.html
- Django aggregation: https://docs.djangoproject.com/en/5.2/topics/db/aggregation/
- Django `bulk_create`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- PostgreSQL subquery expressions (`EXISTS`, `NOT IN` NULL semantics): https://www.postgresql.org/docs/current/functions-subquery.html
- PostgreSQL transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL `AT TIME ZONE`: https://www.postgresql.org/docs/current/functions-datetime.html
- AWS S3 object integrity / checksums: https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html
