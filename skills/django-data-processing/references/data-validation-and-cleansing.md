# Data Validation and Cleansing

> **Scope:** Validating and cleansing raw records after parsing: structural, schema, field, type,
> required, range, format, business-rule, referential and cross-record validation; null vs
> missing vs empty; duplicates; normalisation/standardisation; quality scoring; validation
> reports; the `RejectedRecord` model; and the reject-dataset vs isolate-records policy.
> **Not here:** parsing and format errors → [file-processing-engineering.md](file-processing-engineering.md);
> type conversion, dates, currency, code mapping → [data-transformation-and-mapping.md](data-transformation-and-mapping.md);
> quality dimensions as concepts → [data-processing-fundamentals.md](data-processing-fundamentals.md);
> duplicates at ingest → [data-ingestion-engineering.md](data-ingestion-engineering.md);
> DB constraints and upsert conflicts → [database-loading-and-persistence.md](database-loading-and-persistence.md);
> poison records/DLQ → [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

## 1. Purpose and scope

Validation decides **whether a record (and a dataset) may proceed**; cleansing decides **what
safe, deterministic corrections are applied** before that decision. Both must be explainable:
every rejection carries a stable error code, a locator and enough (redacted) context for a
human to fix the source. This reference defines the validation layers, the policy for partial
acceptance, the error catalogue and the `RejectedRecord` model that other references refer to.

## 2. Core concepts

### 2.1 Validation layers

| Layer | Question | Example | Typical failure scope |
|---|---|---|---|
| Structural | Is the dataset shaped as agreed? | Required columns present, no duplicate headers, row field count | Dataset (fatal) |
| Schema | Does each record match the declared schema? | JSON object has `amount` of type number | Record |
| Required field | Is a mandatory value present? | `employee_id` non-empty | Record |
| Type | Does the value parse into the target type? | `"12.5x"` not Decimal | Record |
| Format | Does it match a syntax? | Email, IBAN, ISO date, licence number pattern | Record |
| Range | Within bounds? | `0 <= hours <= 24`, date not in future | Record |
| Business rule | Domain invariant holds? | `net = gross - deductions`; termination date ≥ hire date | Record |
| Referential | Do references exist? | `cost_centre` exists for tenant; specialty code known | Record (or dataset if reference data missing) |
| Cross-record | Consistency across records? | Duplicate keys in file, one active contract per employee, batch totals match trailer | Records or dataset |

### 2.2 Null vs missing vs empty

| State | CSV | JSON | Meaning to decide |
|---|---|---|---|
| Missing | column absent | key absent | "Not provided" → keep existing value on update (PATCH semantics) |
| Null | n/a (no native null) | `null` | "Explicitly unknown/cleared" → set NULL if allowed |
| Empty string | `""` | `""` | Contract-defined: usually "cleared" for optional text, invalid for required |
| Sentinel | `N/A`, `-`, `NULL`, `0000-00-00` | same | Normalise to null via an explicit per-source list, never globally |

CSV cannot distinguish missing from empty for a present column. Write the rule into the import
contract (e.g. "empty cell = clear the field", or "empty cell = no change"); for upserts the
difference decides whether you overwrite production data with blanks.

### 2.3 Validation vs cleansing vs normalisation

- **Normalisation**: canonical *representation* without changing meaning — trim, Unicode NFKC,
  case-fold emails, E.164 phones, uppercase ISO codes.
- **Standardisation**: map variants to a controlled vocabulary — `"Cardio"`, `"cardiology"` →
  `CARDIOLOGY` (code mapping itself lives in [data-transformation-and-mapping.md](data-transformation-and-mapping.md)).
- **Cleansing**: deterministic, documented corrections — strip thousands separators, remove
  zero-width characters. Never "guess" corrections that change meaning (e.g. swapping day/month).
  Every correction that changes a value is recorded as a warning, not silently applied.

## 3. Internal mechanics

### 3.1 Validation pipeline order

```text
raw record ──► structural (header, field count) ──► normalise/cleanse ──► required/type/format
          ──► range ──► business rules ──► referential (batched lookups) ──► cross-record (set state)
          ──► ACCEPT (to transformation/load)  |  REJECT (RejectedRecord + report)
```

Cheap, local checks run first; checks that need I/O (referential) run per batch with preloaded
reference sets; cross-record checks keep bounded state (a set of seen keys, running totals).
Collect **all** errors for a record (not first-fail) so users fix a row once — except when an
earlier failure makes later checks meaningless (type failure → skip range check on that field).

### 3.2 Error shape

Every error is `(code, field | None, message, severity)`. `code` is stable and machine-readable
(`required`, `invalid_format.email`, `range.max`, `ref.unknown_cost_centre`, `dup.in_file`);
`message` is human text, translatable; `severity` is `error` (record rejected) or `warning`
(record accepted, flagged). Codes, not messages, drive policy, metrics and UI grouping.

### 3.3 App validation vs database constraints

Application validation produces good error messages and runs before any write; **the database
is the final guard** for invariants that must hold under concurrency (uniqueness, FKs, CHECK
constraints). Validation cannot see concurrent writers: two imports validating "email unique"
in parallel both pass. Keep `UniqueConstraint`/`CheckConstraint` in the model and map
`IntegrityError` during load back to a record-level error code
(see [database-loading-and-persistence.md](database-loading-and-persistence.md) and
[../../django-orm/references/database-constraints-and-indexes.md](../../django-orm/references/database-constraints-and-indexes.md)).
Remember `bulk_create()` skips `full_clean()` — model validation does not run on bulk loads.

## 4. Engineering decisions and trade-offs

### 4.1 Reject the dataset vs isolate invalid records

Rejecting the whole dataset protects consistency when records are interdependent (a bank
statement must balance; a payroll run must be complete). Isolating invalid records maximises
throughput when records are independent (catalog items, customer contacts). Make it an explicit
per-import-type policy, not an accident of code structure.

| Condition | Policy | Rationale |
|---|---|---|
| Format/structural error (encoding, missing column, unparsable document) | **Always fatal** | No record can be trusted |
| Control totals / trailer count mismatch | Fatal | File incomplete or tampered |
| Financial/payroll batches (records sum to a whole) | Fatal on any error (`max_error_rate = 0`) | Partial posting breaks balances |
| Independent master data (products, contacts) | Isolate; fatal if `error_rate > max_error_rate` (e.g. 5%) | Many errors signal a systemic problem (wrong template, wrong mapping) |
| Small files from humans (≤ 1k rows) | Validate all, show preview, user confirms | Cheap to fix and resubmit |
| Reference data missing (unknown cost centre for 80% rows) | Fatal + alert ops | Problem is on our side, not the data |
| Warnings only | Accept, report | — |

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    max_error_rate: float = 0.05          # fraction of data records
    max_errors_absolute: int = 10_000     # stop collecting early; file is clearly broken
    min_records: int = 1
    fatal_codes: frozenset[str] = frozenset({"ref.reference_data_missing", "control.total_mismatch"})
    all_or_nothing: bool = False

def dataset_verdict(total: int, rejected: int, codes_seen: set[str], p: AcceptancePolicy) -> str:
    if total < p.min_records:
        return "FAILED"
    if codes_seen & p.fatal_codes or (p.all_or_nothing and rejected):
        return "FAILED"
    if rejected / total > p.max_error_rate or rejected > p.max_errors_absolute:
        return "FAILED"
    return "PARTIAL" if rejected else "SUCCEEDED"
```

All-or-nothing with large files needs two passes (validate everything, then load) or a staging
table promoted in one transaction — see [database-loading-and-persistence.md](database-loading-and-persistence.md).
Isolate mode with an error-rate threshold needs either a validate-first pass or the ability to
roll back what was loaded (staging again) — otherwise you discover the 40% error rate after
loading 60% of the file.

### 4.2 Choosing a validation tool

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Rows mapping to Django models, reuse admin/forms rules | Django Forms or `django.core.validators` | Very high volume — form instantiation overhead per row |
| API-shaped records, DRF already in project | DRF `Serializer(data=row)` per row | Nested writes per row at millions of rows |
| Pure domain rules, max speed, no framework | dataclass + functions returning error lists | Rules duplicated elsewhere → centralise |
| Typed parsing + validation, JSON-heavy | pydantic v2 [third-party: pydantic] | Team does not use it elsewhere — another schema dialect to maintain |
| Partner-published JSON Schema | `jsonschema` [third-party: jsonschema] | Business rules — JSON Schema is structural only |
| DataFrame (analytical) checks | pandera [third-party: pandera] or Polars/pandas expressions | Need per-row error codes for users → row-wise tools |

## 5. Django/Python implementation patterns

### 5.1 Error catalogue

```python
# validation/codes.py
from dataclasses import dataclass
from enum import StrEnum

class Err(StrEnum):
    REQUIRED = "required"
    TYPE_DECIMAL = "type.decimal"
    TYPE_DATE = "type.date"
    FORMAT_EMAIL = "format.email"
    FORMAT_LICENCE = "format.licence_number"
    RANGE_MIN = "range.min"
    RANGE_MAX = "range.max"
    RULE_NET_PAY = "rule.net_pay_mismatch"
    RULE_DATE_ORDER = "rule.date_order"
    REF_UNKNOWN = "ref.unknown"
    REF_DATA_MISSING = "ref.reference_data_missing"
    DUP_IN_FILE = "dup.in_file"
    DB_UNIQUE = "db.unique_violation"

@dataclass(frozen=True, slots=True)
class FieldError:
    code: str
    field: str | None
    message: str
    severity: str = "error"     # "error" | "warning"
```

Rules for the catalogue: codes never change meaning once released (add new ones); dotted
namespaces group in reports; each code documents owner, severity, user-facing fix hint; tests
assert codes, not messages. Map external validator codes (Django, DRF, pydantic, jsonschema) to
catalogue codes in one adapter so reports are uniform.

### 5.2 Plain Python: dataclass + rule functions

```python
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
import datetime as dt, re

LICENCE_RE = re.compile(r"^[A-Z]{2}-\d{6}$")
type Rule = Callable[[dict[str, str]], list[FieldError]]

def required(*fields: str) -> Rule:
    def check(r: dict[str, str]) -> list[FieldError]:
        return [FieldError(Err.REQUIRED, f, f"{f} is required") for f in fields if not r.get(f)]
    return check

def decimal_in_range(field: str, lo: Decimal, hi: Decimal) -> Rule:
    def check(r: dict[str, str]) -> list[FieldError]:
        if not r.get(field):
            return []                                        # required() reports absence
        try:
            v = Decimal(r[field])
        except InvalidOperation:
            return [FieldError(Err.TYPE_DECIMAL, field, "not a number")]
        if not v.is_finite():
            return [FieldError(Err.TYPE_DECIMAL, field, "NaN/Infinity not allowed")]
        if v < lo: return [FieldError(Err.RANGE_MIN, field, f"must be >= {lo}")]
        if v > hi: return [FieldError(Err.RANGE_MAX, field, f"must be <= {hi}")]
        return []
    return check

def licence_format(r: dict[str, str]) -> list[FieldError]:
    v = r.get("licence_number", "")
    return [] if not v or LICENCE_RE.fullmatch(v) else [
        FieldError(Err.FORMAT_LICENCE, "licence_number", "expected e.g. CA-123456")]

DOCTOR_RULES: list[Rule] = [
    required("licence_number", "family_name", "specialty"),
    licence_format,
    decimal_in_range("fte", Decimal("0"), Decimal("1.0")),
]

def validate(record: dict[str, str], rules: list[Rule]) -> list[FieldError]:
    return [e for rule in rules for e in rule(record)]
```

### 5.3 Django validators and Forms

```python
from django import forms                                   # [Django]
from django.core.exceptions import ValidationError         # [Django]
from django.core.validators import EmailValidator, RegexValidator, MinValueValidator

class EmployeeRowForm(forms.Form):
    employee_id = forms.CharField(max_length=20, validators=[RegexValidator(r"^E\d{6}$", code="format.employee_id")])
    email = forms.EmailField(validators=[EmailValidator(code="format.email")])
    hire_date = forms.DateField(input_formats=["%Y-%m-%d"])
    termination_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    weekly_hours = forms.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(0)])

    def clean(self) -> dict:
        data = super().clean()
        hire, term = data.get("hire_date"), data.get("termination_date")
        if hire and term and term < hire:
            raise ValidationError("Termination before hire date.", code=Err.RULE_DATE_ORDER)
        return data

def form_errors(form: forms.Form) -> list[FieldError]:
    out: list[FieldError] = []
    for field, errs in form.errors.as_data().items():     # ValidationError instances with .code
        for e in errs:
            out.append(FieldError(e.code or "invalid", None if field == "__all__" else field,
                                  " ".join(e.messages)))
    return out
```

Reusing a form keeps import rules identical to the UI's rules. Cost: a Form instance per row
(fine for tens of thousands; measure before millions). Built-in field codes such as `required`,
`invalid`, `max_length` map naturally to the catalogue. More on forms:
[../../django-core/references/forms-validation.md](../../django-core/references/forms-validation.md).

### 5.4 DRF serializer per row, collecting `errors`

```python
from rest_framework import serializers                     # [DRF]

class CustomerRowSerializer(serializers.Serializer):
    external_id = serializers.CharField(max_length=40)
    email = serializers.EmailField()
    credit_limit = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)

def validate_rows(rows, ctx) -> tuple[list[dict], list[tuple[str, list[FieldError]]]]:
    valid, rejected = [], []
    for locator, raw in rows:
        s = CustomerRowSerializer(data=raw, context=ctx)    # one row; never many=True for imports
        if s.is_valid():
            valid.append(s.validated_data)
        else:
            errs = [FieldError(str(d.code), f, str(d)) for f, details in s.errors.items()
                    for d in details]                       # ErrorDetail has .code (flat fields only)
            rejected.append((locator, errs))
    return valid, rejected
```

`many=True` returns errors as a list aligned to input positions, which loses the file locator
and validates the whole list in memory; per-row instances keep locators and stream. Nested
serializers produce nested error dicts — flatten with a recursive helper.

### 5.5 pydantic v2 and jsonschema

```python
from pydantic import BaseModel, Field, ValidationError     # [third-party: pydantic]

class TxnIn(BaseModel):
    txn_id: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    booked_at: dt.date
    counterparty_iban: str | None = None

def validate_txn(raw: dict) -> tuple[TxnIn | None, list[FieldError]]:
    try:
        return TxnIn.model_validate(raw), []
    except ValidationError as exc:
        return None, [FieldError(f"pyd.{e['type']}", ".".join(map(str, e["loc"])), e["msg"])
                      for e in exc.errors(include_input=False)]   # (VERIFY: include_input kwarg in your version)

from jsonschema import Draft202012Validator               # [third-party: jsonschema]

def jsonschema_errors(validator: Draft202012Validator, obj: dict) -> list[FieldError]:
    return [FieldError(f"schema.{e.validator}", "/".join(map(str, e.absolute_path)) or None, e.message)
            for e in validator.iter_errors(obj)]
# Build once per job: Draft202012Validator.check_schema(schema); v = Draft202012Validator(schema)
```

pydantic's lax mode coerces `"12"` → `12`; use `strict=True` per field/model when coercion would
hide producer bugs. jsonschema `format` keywords are **not** enforced unless a format checker is
passed (VERIFY: `format_checker=Draft202012Validator.FORMAT_CHECKER` for your version). Both
error payloads can contain input values — never store them unredacted.

pandera (DataFrame-level, analytical paths): define a `DataFrameSchema` with column checks and
call `schema.validate(df, lazy=True)` to collect all failures into `SchemaErrors.failure_cases`
(VERIFY: import path `pandera.pandas` vs `pandera` and API for your version). Use it to gate a
bulk dataset, then map failure cases back to row indices for rejected-record entries.

### 5.6 Cross-record checks and duplicates

```python
import hashlib, json

class CrossRecordState:
    def __init__(self) -> None:
        self.seen_keys: dict[tuple, str] = {}               # natural key -> first locator
        self.seen_hashes: set[bytes] = set()

    def check(self, locator: str, rec: dict) -> list[FieldError]:
        errs: list[FieldError] = []
        key = (rec["tenant_id"], rec["employee_id"].upper())
        if (first := self.seen_keys.get(key)) is not None:
            errs.append(FieldError(Err.DUP_IN_FILE, "employee_id", f"duplicate of {first}"))
        else:
            self.seen_keys[key] = locator
        digest = hashlib.blake2b(json.dumps(rec, sort_keys=True, default=str).encode(), digest_size=16).digest()
        if digest in self.seen_hashes:
            errs.append(FieldError("dup.exact_row", None, "exact duplicate row", "warning"))
        self.seen_hashes.add(digest)
        return errs
```

Key-duplicate policy per contract: reject both, keep first, or keep last (last-wins suits
"latest state" feeds). Memory is O(unique keys) — for 50M keys use a staging table with a unique
index or a `GROUP BY ... HAVING count(*) > 1` query instead. Fuzzy duplicates (same person,
different spelling) are a matching problem: flag for review, never auto-merge.

### 5.7 Referential checks with batched reference sets

```python
def load_reference(tenant_id: int) -> dict[str, frozenset[str]]:
    return {
        "cost_centre": frozenset(CostCentre.objects.filter(tenant_id=tenant_id, active=True)
                                 .values_list("code", flat=True)),
        "specialty": frozenset(Specialty.objects.values_list("code", flat=True)),
    }
```

Load once per job (or per batch for huge sets), check with set membership; zero reference rows
for a tenant is a `ref.reference_data_missing` fatal error, not a million row errors.

### 5.8 `RejectedRecord` model (canonical definition)

```python
from django.db import models
from django.db.models.functions import Now

class RejectedRecord(models.Model):
    class Stage(models.TextChoices):
        PARSE = "parse"; VALIDATE = "validate"; TRANSFORM = "transform"; LOAD = "load"
    class Resolution(models.TextChoices):
        OPEN = "open"; FIXED_RESUBMITTED = "fixed"; IGNORED = "ignored"; REPROCESSED = "reprocessed"

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    job_type = models.CharField(max_length=40)              # "import" | "pipeline_step" | ...
    job_id = models.CharField(max_length=64)                # owning ImportJob / PipelineStepRun pk as text
    locator = models.CharField(max_length=255)              # "line 4812", "Sheet1!row 77", "/items/3"
    stage = models.CharField(max_length=12, choices=Stage)
    error_codes = models.JSONField(default=list)            # ["required", "format.email"]
    errors = models.JSONField(default=list)                 # [{"code","field","message"}] — no raw values
    raw_payload = models.JSONField(null=True)               # REDACTED copy of the raw record
    payload_sha256 = models.CharField(max_length=64)        # dedupe/reprocessing, from unredacted
    resolution = models.CharField(max_length=12, choices=Resolution, default=Resolution.OPEN)
    resolved_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        indexes = [models.Index(fields=["tenant", "job_id", "stage"]),
                   models.Index(fields=["tenant", "resolution", "created_at"])]
        constraints = [models.UniqueConstraint(fields=["job_id", "stage", "locator"],
                                               name="uniq_rejected_per_job_locator")]
```

Write rejected records with `bulk_create(batch, ignore_conflicts=True)` so job retries do not
duplicate them (the unique constraint makes the write idempotent). Generic `job_type`/`job_id`
avoids a `GenericForeignKey`; use a real FK if only one job model exists. Retention: purge or
anonymise `raw_payload` after the correction window (e.g. 30–90 days). Reprocessing flows live in
[processing-reliability-and-recovery.md](processing-reliability-and-recovery.md); the user-facing
error report download in [data-import-and-export-systems.md](data-import-and-export-systems.md).

### 5.9 Quality score and validation report

```python
from dataclasses import dataclass, field

@dataclass
class ValidationReport:
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    warnings: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    by_field: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)   # code -> first 5 locators

    def add(self, locator: str, errs: list[FieldError]) -> None:
        self.total += 1
        hard = [e for e in errs if e.severity == "error"]
        self.rejected += bool(hard); self.accepted += not hard
        self.warnings += len(errs) - len(hard)
        for e in errs:
            self.by_code[e.code] = self.by_code.get(e.code, 0) + 1
            if e.field: self.by_field[e.field] = self.by_field.get(e.field, 0) + 1
            if len(s := self.samples.setdefault(e.code, [])) < 5: s.append(locator)

    @property
    def validity(self) -> float:
        return self.accepted / self.total if self.total else 0.0
```

A quality score is a weighted combination of dimensions (validity, completeness of optional but
important fields, uniqueness, referential integrity) — weights per dataset, persisted per job so
trends are visible. Report top codes with counts, sample locators and a fix hint; full detail via
the rejected-record CSV.

## 6. Production use cases

- **Payroll (HRIS → payroll).** All-or-nothing: any error fails the run; checks include
  `net = gross − deductions` within 0.01, one active contract per employee per period, cost
  centre exists. Report goes to payroll officer before cut-off.
- **Bank payments/statement import.** Structural: trailer count and control sum must match
  (fatal). Records: IBAN checksum, Decimal amounts with ≤ 2 dp, booking date within statement
  period. Duplicate `txn_id` in file → fatal (indicates a resend merged with another).
- **Hospital doctor roster.** Isolate mode, `max_error_rate=0.02`; licence number format per
  issuing region, specialty code must exist, expired licence → warning plus compliance review.
- **Product catalog sync.** Isolate mode, 5% threshold; price > 0, GTIN check digit, category
  exists; rejected products keep their previous published version.
- **Customer contacts (CRM).** Normalise email (trim, lower-case domain), E.164 phone; fuzzy
  duplicates flagged for steward review; empty cell = "no change" by contract.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Reject whole dataset | Consistency, simple reasoning | One bad row blocks everything; frustrates users |
| Isolate invalid records | Throughput, partial progress | Downstream sees incomplete data; needs rejected-record handling |
| Plain Python rules | Fast, explicit, testable | You build messages/codes yourself |
| Django Forms/validators | Same rules as UI | Per-row overhead; form-shaped errors |
| DRF serializers | Same rules as API | Overhead; nested error flattening |
| pydantic | Fast typed parsing, rich errors | Extra dependency; lax coercion surprises |
| jsonschema | Standard, partner-shareable | Structural only; slower on big batches |
| DB constraints | Concurrency-safe final guard | Poor error messages; fail late |

## 8. Security and performance considerations

**Security**
- Rejected payloads and error messages are a PII/secrets leak path: redact by field policy
  (bank details, national IDs, salaries) before storing `raw_payload`; never include raw input in
  `errors` messages; scope all queries by tenant.
- Error reports are downloadable artefacts: access-controlled, expiring links, CSV formula
  injection neutralised on export ([data-processing-security.md](data-processing-security.md)).
- Regex validators on untrusted input: avoid catastrophic backtracking (nested quantifiers);
  cap field length before regex.
- Validation must not trust client-side preview results — revalidate in the worker.

**Performance**
- Precompile regexes and validators once per job; build schema validators once.
- Batch referential lookups (sets/dicts), never per-row queries (N+1).
- Cap collected errors (`max_errors_absolute`) and stop early when the verdict is already FAILED.
- Bulk-insert `RejectedRecord` in batches; store samples in the report, not millions of rows in
  memory.
- Cross-record state is O(keys): move to SQL on a staging table beyond a few million keys.

## 9. Common mistakes and anti-patterns

- First-error-only validation → users fix one error per upload cycle → collect all errors per record.
- Error messages as identifiers → UI/metrics break on wording change → stable codes.
- Treating empty string as null everywhere → required fields "pass", or optional fields wiped on upsert → per-contract rule.
- Global sentinel list (`"NA"` → null) → Namibia or surname "Na" erased → per-source, per-field sentinels.
- `Decimal("NaN")` accepted → aggregations become NaN → `is_finite()`.
- Relying on app-level unique checks → concurrent imports create duplicates → DB `UniqueConstraint`.
- Expecting `bulk_create` to run `full_clean()` → invalid rows persisted → validate explicitly.
- Isolate mode without an error-rate ceiling → wrong template "succeeds" with 90% rejected → `max_error_rate`.
- Storing raw rejected rows unredacted forever → compliance breach → redaction + retention.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Every row fails `required` on one field | Header alias mismatch | Check normalised header vs expected; alias map |
| Error rate spikes for one tenant | Reference data missing/inactive | `ref.unknown` concentrated on one code → fatal reference check |
| Duplicates in DB despite in-file check | Concurrent jobs or cross-file duplicates | DB unique constraint; upsert semantics |
| Rejected records duplicated after retry | Non-idempotent writes | Unique `(job_id, stage, locator)` + `ignore_conflicts` |
| Validation slow | Per-row DB queries | Preload reference sets; profile |

Validation checklist:
- [ ] Policy per import type (fatal codes, `max_error_rate`, all-or-nothing) documented.
- [ ] Error catalogue with stable codes; external codes mapped.
- [ ] Null/missing/empty semantics in the contract.
- [ ] DB constraints back every uniqueness/FK invariant.
- [ ] `RejectedRecord` writes idempotent, redacted, retained per policy.
- [ ] Report shows counts by code/field, samples, fix hints. Tests: [data-processing-testing.md](data-processing-testing.md).

## 11. Related references and skills

- [data-processing-fundamentals.md](data-processing-fundamentals.md) — quality dimensions.
- [file-processing-engineering.md](file-processing-engineering.md) — format vs row errors at parse time.
- [data-transformation-and-mapping.md](data-transformation-and-mapping.md) — conversion and code mapping.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — constraints, staging, IntegrityError mapping.
- [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) — reprocessing rejected records.
- [data-import-and-export-systems.md](data-import-and-export-systems.md) — validation preview and error reports.
- [../../django-core/references/forms-validation.md](../../django-core/references/forms-validation.md).
- [../../django-orm/references/database-constraints-and-indexes.md](../../django-orm/references/database-constraints-and-indexes.md).
- [../../django-rest-framework/SKILL.md](../../django-rest-framework/SKILL.md).

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/ref/validators/
- https://docs.djangoproject.com/en/5.2/ref/forms/validation/
- https://docs.djangoproject.com/en/5.2/ref/models/constraints/
- https://www.django-rest-framework.org/api-guide/serializers/#validation
- https://www.django-rest-framework.org/api-guide/exceptions/
- https://docs.pydantic.dev/latest/concepts/models/
- https://docs.pydantic.dev/latest/errors/validation_errors/
- https://docs.python.org/3/library/decimal.html
- https://docs.python.org/3/library/re.html
- https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
