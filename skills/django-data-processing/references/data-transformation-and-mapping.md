# Data Transformation and Mapping

> **Scope:** Turning validated source records into target-shaped records: field and schema
> mapping, type conversion, dates/timezones (DST, Excel serials), Decimal/currency/FX, units,
> code/enum/reference mapping, nested JSON and flattening, aggregation, enrichment, derived and
> conditional fields, composable transformation pipelines and versioned, declarative mapping
> configuration (`MappingDefinition`). **Not here:** parsing → [file-processing-engineering.md](file-processing-engineering.md);
> validation rules and `RejectedRecord` → [data-validation-and-cleansing.md](data-validation-and-cleansing.md);
> user-facing column mapping UI (`ColumnMapping`) → [data-import-and-export-systems.md](data-import-and-export-systems.md);
> pipeline stages/state → [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md);
> FK resolution at load time → [database-loading-and-persistence.md](database-loading-and-persistence.md);
> lineage concepts → [data-processing-fundamentals.md](data-processing-fundamentals.md).

## 1. Purpose and scope

Transformation is where silent data corruption happens: a float sneaks into money, a naive
timestamp is stored as UTC, an unmapped code becomes `None`. The goals of this reference are:

- **Deterministic**: same input + same mapping version → same output, byte for byte.
- **Explainable**: every output record records which mapping version produced it (lineage).
- **Fail-loud**: unmapped codes, ambiguous dates and lossy conversions raise coded errors that
  flow into the rejected-record path, never defaults.
- **Data-driven where it varies**: per-tenant/per-source differences live in versioned mapping
  configuration; genuinely complex business logic stays in reviewed Python.

## 2. Core concepts

| Concept | Definition | Example |
|---|---|---|
| Field mapping | source field → target field (rename, select) | `EmpNo` → `employee_number` |
| Schema mapping | source shape → target shape (split/merge/nest/flatten) | `{name: {first, last}}` → `first_name`, `last_name` |
| Type conversion | string/loose types → strict target types | `"1.234,50"` → `Decimal("1234.50")` |
| Code mapping | source code → canonical code via lookup table | `"CARD"` → `CARDIOLOGY` |
| Enrichment | add data from reference sources | cost centre → legal entity |
| Derived field | computed from other fields | `annual_salary = monthly × 12` |
| Conditional transform | branch on data | contractors skip pension deduction |
| Aggregation | many records → one | transactions → daily account totals |
| Mapping definition | versioned, declarative spec of the above | `hris-to-payroll v7` |

Row-wise vs vectorised: business mappings with per-record errors are clearest as pure Python
functions over dicts; analytical/bulk transforms (unit conversion across 50M rows, aggregations)
belong in pandas/Polars/SQL. Choose per step, not per project.

## 3. Internal mechanics

### 3.1 Transformation as function composition

A transform is `Callable[[Record, Context], Record]` that either returns a new record or raises
`TransformError(code, field, message)`. A pipeline is an ordered list of transforms; the runner
applies them, catches `TransformError`, and routes the record to rejection with stage
`transform`. Pure functions (no I/O, no global state; reference data passed in via `Context`)
make transforms trivially testable and safely re-runnable — a core requirement for idempotent
reprocessing.

### 3.2 Where precision is lost

| Conversion | Loss mechanism | Rule |
|---|---|---|
| str → float → Decimal | binary float rounding (`0.1 + 0.2`) | Parse money straight from `str` to `Decimal` |
| Excel float → Decimal | value already float in cell | `Decimal(str(v))`, then `quantize` to contract scale |
| Decimal quantize twice | double rounding | Round once, at the defined business step |
| naive datetime → aware | wrong zone assumed | Source timezone is mapping config, never server TZ |
| local time in DST gap/overlap | nonexistent/ambiguous instants | Detect and reject or apply documented rule |
| int codes `007` → int | leading zeros lost | Codes are strings |
| units | float factors, mixed systems | `Decimal` factors from a table |

### 3.3 Timezones in Django
With `USE_TZ=True` [Django] Django stores aware datetimes in UTC. `zoneinfo.ZoneInfo` [stdlib]
attaches zones; a wall-clock time inside a DST overlap is ambiguous (`fold=0` earlier, `fold=1`
later) and one inside a gap does not exist — zoneinfo does not raise for either, it silently
picks an offset. Dates (no time) must stay `date`; converting a birth date or pay date to a
midnight UTC datetime shifts it by a day for users west of UTC.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Per-row business mapping with user-facing errors | Pure Python transform pipeline | > tens of millions rows and logic is simple → vectorise |
| Bulk numeric/unit conversion, aggregation | Polars/pandas expressions or SQL | Need per-row error codes → row-wise |
| Mappings differ per tenant/source and change often | Declarative `MappingDefinition` (JSON) + registry of named transforms | Logic needs loops/conditions beyond simple rules → code |
| Complex, stable business rule (payroll tax) | Reviewed Python code, versioned with app releases | Encoding it in JSON "rules engine" |
| Code mapping table maintained by business users | DB table `CodeMapping(source_system, code_set, source_code, target_code, valid_from)` | Hard-coded dicts in code |
| Unmapped code encountered | Reject record with `map.unmapped_code` (or quarantine) | Defaulting to `OTHER` silently |
| FX conversion | Dated rate table, rate recorded on output | "Current" rate at processing time |
| Ambiguous date formats (`03/04/2026`) | Explicit format per source in mapping | Auto-detection / `dateutil` fuzzy parse |

## 5. Django/Python implementation patterns

### 5.1 Composable pipeline

```python
# transform/pipeline.py
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

type Record = dict[str, Any]

class TransformError(Exception):
    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message); self.code, self.field = code, field

@dataclass(frozen=True)
class Context:
    tenant_id: int
    mapping_version: int
    source_tz: str
    reference: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # preloaded per batch

type Transform = Callable[[Record, Context], Record]

def run_pipeline(rec: Record, ctx: Context, steps: list[Transform]) -> Record:
    for step in steps:
        rec = step(rec, ctx)                 # each returns a NEW dict; inputs never mutated
    return rec | {"_mapping_version": ctx.mapping_version}

def transform_batch(rows, ctx: Context, steps: list[Transform]):
    ok: list[tuple[str, Record]] = []
    failed: list[tuple[str, TransformError]] = []
    for locator, rec in rows:
        try:
            ok.append((locator, run_pipeline(rec, ctx, steps)))
        except TransformError as exc:        # data error: permanent for this record
            failed.append((locator, exc))
        # any other exception = bug or system error: let it propagate and fail the job
    return ok, failed
```

Only `TransformError` is a record-level failure. A `KeyError` or `TypeError` is a code bug —
swallowing it would reject 2M records as "bad data". Classification of transient vs permanent
errors: [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md).

### 5.2 Type conversion helpers

```python
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP   # [stdlib]
import datetime as dt

def to_decimal(raw: object, field: str, *, decimal_sep: str = ".", group_sep: str = ",") -> Decimal:
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, float):               # Excel/JSON floats: go through repr string
        raw = repr(raw)
    s = str(raw).strip().replace(" ", "").replace(" ", "")
    if group_sep:
        s = s.replace(group_sep, "")
    s = s.replace(decimal_sep, ".")
    if s.startswith("(") and s.endswith(")"):  # accounting negative: (123.45)
        s = "-" + s[1:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise TransformError("type.decimal", field, "not a decimal number") from None
    if not d.is_finite():
        raise TransformError("type.decimal", field, "non-finite value")
    return d

def to_date(raw: object, field: str, fmt: str) -> dt.date:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.datetime.strptime(str(raw).strip(), fmt).date()   # explicit format per source
    except ValueError:
        raise TransformError("type.date", field, f"expected format {fmt}") from None
```

`to_decimal("1.234,50", "amount", decimal_sep=",", group_sep=".")` → `Decimal("1234.50")`. The
separators are mapping configuration per source/locale, not guessed. `repr(float)` gives the
shortest round-tripping string (`0.1` → `"0.1"`), which is what the spreadsheet displayed in most
cases; still quantise to the contract scale afterwards.

### 5.3 Dates, timezones, DST and Excel serial dates

```python
from zoneinfo import ZoneInfo                   # [stdlib]

UTC = dt.UTC                                     # Python >= 3.11

def localise(naive: dt.datetime, tz_name: str, field: str, *, ambiguous: str = "reject") -> dt.datetime:
    """Interpret a naive wall-clock time in tz_name; return aware UTC."""
    tz = ZoneInfo(tz_name)
    early, late = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if early.utcoffset() != late.utcoffset():
        roundtrip = early.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        if roundtrip != naive:
            raise TransformError("date.nonexistent_local_time", field, f"{naive} falls in a DST gap")
        match ambiguous:                         # DST overlap: the hour happens twice
            case "earliest": return early.astimezone(UTC)
            case "latest": return late.astimezone(UTC)
            case _: raise TransformError("date.ambiguous_local_time", field, f"{naive} is ambiguous")
    return early.astimezone(UTC)

_EXCEL_1900 = dt.datetime(1899, 12, 30)          # compensates Excel's fictitious 1900-02-29
_EXCEL_1904 = dt.datetime(1904, 1, 1)

def from_excel_serial(serial: float, field: str, *, epoch_1904: bool = False) -> dt.datetime:
    if epoch_1904:
        return _EXCEL_1904 + dt.timedelta(days=serial)
    if serial < 1 or 60 <= serial < 61:
        raise TransformError("date.excel_serial", field, f"invalid/ambiguous Excel serial {serial}")
    base = _EXCEL_1900 if serial >= 61 else dt.datetime(1899, 12, 31)
    return base + dt.timedelta(days=serial)
```

Notes:
- openpyxl already converts date-formatted cells to `datetime` using the workbook epoch; serial
  numbers reach you when the cell is unformatted or the data came via CSV/another tool. openpyxl
  exposes a helper (`openpyxl.utils.datetime.from_excel`) and the workbook epoch
  (VERIFY: names and signature for your openpyxl version).
- Payroll shifts that cross the autumn overlap (01:00–02:00 happens twice) need the clock-in
  device's UTC offset or sequence to disambiguate; if absent, reject or apply a documented rule
  (`earliest`) and flag a warning — never guess silently.
- Source timezone is per source (and sometimes per site: a hospital group spanning zones) — it is
  part of the mapping definition or reference data, not `settings.TIME_ZONE`.
- Ambiguous textual formats (`03/04/2026`) are only parseable with the declared format; reject
  two-digit years unless the contract defines the pivot.

### 5.4 Currency, minor units, rounding and FX

```python
MINOR_UNITS = {"EUR": 2, "USD": 2, "GBP": 2, "JPY": 0, "KWD": 3, "BHD": 3}   # load from ISO 4217 reference data

def quantum(currency: str) -> Decimal:
    try:
        return Decimal(1).scaleb(-MINOR_UNITS[currency])      # 2 -> Decimal("0.01")
    except KeyError:
        raise TransformError("map.unknown_currency", "currency", currency) from None

def to_money(amount: Decimal, currency: str, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    return amount.quantize(quantum(currency), rounding=rounding)

def to_minor_units(amount: Decimal, currency: str) -> int:
    q = to_money(amount, currency)
    if q != amount:
        raise TransformError("money.excess_precision", "amount", f"{amount} has too many decimals")
    return int(q.scaleb(MINOR_UNITS[currency]))               # 12.34 EUR -> 1234

@dataclass(frozen=True)
class FxRate:
    base: str; quote: str; rate: Decimal; rate_date: dt.date; source: str

def convert(amount: Decimal, ccy: str, target: str, on: dt.date,
            rates: Mapping[tuple[str, str, dt.date], FxRate]) -> tuple[Decimal, FxRate | None]:
    if ccy == target:
        return amount, None
    fx = rates.get((ccy, target, on))
    if fx is None:
        raise TransformError("fx.rate_missing", "amount", f"no {ccy}->{target} rate for {on}")
    return to_money(amount * fx.rate, target), fx              # round ONCE, after multiplication
```

- `ROUND_HALF_EVEN` (banker's) avoids statistical drift in aggregates; many invoicing and tax
  rules mandate `ROUND_HALF_UP` — rounding mode is a business rule per mapping, record it.
- Use the rate **for the transaction/booking date** from a dated table (with a documented
  fallback such as "last business day before"), and store `rate`, `rate_date`, `source` on the
  output so reconciliation can reproduce the number.
- Integer minor units are safe for transport and for integrations (payment APIs); Django
  `DecimalField(max_digits=…, decimal_places=…)` is fine for storage — pick one per system.
- Allocation (splitting 100.00 across 3 cost centres) must distribute the remainder cent
  deterministically (largest remainder to first line) so totals reconcile.

### 5.5 Units, code and enum mapping

```python
UNIT_FACTORS: dict[tuple[str, str], Decimal] = {
    ("LB", "KG"): Decimal("0.45359237"), ("G", "KG"): Decimal("0.001"), ("KG", "KG"): Decimal(1),
}

def convert_unit(value: Decimal, unit: str, target: str) -> Decimal:
    factor = UNIT_FACTORS.get((unit.upper(), target))
    if factor is None:
        raise TransformError("map.unknown_unit", "unit", f"{unit}->{target}")
    return value * factor

def map_code(code_set: str, raw: str, ctx: Context, field: str) -> str:
    key = raw.strip().upper()
    target = ctx.reference.get(code_set, {}).get(key)
    if target is None:
        raise TransformError("map.unmapped_code", field, f"{code_set}: {raw!r} has no mapping")
    return target
```

Map to a Django `TextChoices` [Django] value as the final step (`Specialty(target)` raises
`ValueError` for an unknown value — convert it to a `TransformError`). Code tables carry
`valid_from/valid_to`: a source code retired last year may still appear in historic records,
so select the mapping valid on the record's business date, not today.

### 5.6 Nested JSON, flattening, aggregation, enrichment

```python
def flatten(obj: Mapping[str, Any], prefix: str = "", sep: str = ".") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}{sep}{k}" if prefix else k
        if isinstance(v, Mapping):
            out |= flatten(v, key, sep)
        else:
            out[key] = v                      # lists are kept: explode them explicitly, not here
    return out

def explode_lines(order: Record) -> list[Record]:
    """ERP order with nested lines -> one record per line (denormalised header fields)."""
    header = {"order_no": order["orderNo"], "customer": order["customer"]["id"]}
    return [header | {"line_no": i, "sku": ln["sku"], "qty": to_decimal(ln["qty"], "qty")}
            for i, ln in enumerate(order.get("lines", []), start=1)]
```

Enrichment via per-batch caches — load reference data once per batch into `Context.reference`
(`{"cost_centre": {"CC100": {"legal_entity": "DE01"}}}`), not per record. For reference data
that changes mid-job, pin a snapshot at job start so all records see the same version. External
lookups (geocoding, VAT validation) are I/O — batch them, cache results with TTL, and treat
failures as transient (retry) rather than record errors; client design lives in
[../../django-integrations/references/http-and-rest-api-integrations.md](../../django-integrations/references/http-and-rest-api-integrations.md).

Aggregation: row-wise Python `defaultdict(Decimal)` is fine up to a few million records;
beyond that aggregate in SQL on a staging table or in Polars. Always aggregate *after* converting
to a common currency/unit and *before* rounding display values.

### 5.7 Vectorised transforms (analytical/bulk only)

```python
import polars as pl                                    # [third-party: polars]

def normalise_catalog_prices(lf: pl.LazyFrame, fx: pl.DataFrame) -> pl.LazyFrame:
    return (
        lf.with_columns(pl.col("price").cast(pl.Decimal(12, 2)))        # (VERIFY: Decimal support level in your Polars version)
          .join(fx.lazy(), on=["currency", "price_date"], how="left")
          .with_columns((pl.col("price") * pl.col("rate")).round(2).alias("price_eur"))
          .with_columns(pl.when(pl.col("rate").is_null()).then(pl.lit("fx.rate_missing"))
                          .otherwise(None).alias("_error"))
    )
```

Vectorised code must still produce per-row error codes (the `_error` column) that are split out
into rejected records. Check the rounding mode of `round()` in your engine — it may not be
`ROUND_HALF_EVEN` (VERIFY: Polars/pandas rounding semantics for Decimal vs float). For money
rounded under a legal rule, convert row-wise with `Decimal` or round in SQL with explicit rules.

### 5.8 `MappingDefinition`: declarative, versioned, immutable once used

```python
import hashlib, json
from django.db import models
from django.db.models.functions import Now

class MappingDefinition(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"; ACTIVE = "active"; RETIRED = "retired"

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    name = models.CharField(max_length=80)                 # "hris-to-payroll"
    version = models.PositiveIntegerField()
    source_system = models.CharField(max_length=40)
    target_schema = models.CharField(max_length=80)        # "payroll.EmployeeInput@3"
    spec = models.JSONField()                              # field rules, see example below
    spec_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status, default=Status.DRAFT)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True) # exclusive
    first_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name", "version"], name="uniq_mapping_version")]

    def save(self, *args, **kwargs) -> None:
        canonical = json.dumps(self.spec, sort_keys=True, separators=(",", ":")).encode()
        self.spec_sha256 = hashlib.sha256(canonical).hexdigest()      # always derived, never trusted
        if self.pk and self.first_used_at:
            frozen = MappingDefinition.objects.filter(pk=self.pk).values_list("spec_sha256", flat=True).get()
            if frozen != self.spec_sha256:
                raise ValueError("MappingDefinition is immutable once used; create a new version.")
        super().save(*args, **kwargs)
```

```json
{
  "fields": [
    {"target": "employee_number", "source": "EmpID", "transforms": ["strip", "upper"], "required": true},
    {"target": "hire_date", "source": "StartDate", "transforms": [{"date": {"format": "%d.%m.%Y"}}]},
    {"target": "monthly_gross", "source": "Salary", "transforms": [{"decimal": {"decimal_sep": ",", "group_sep": "."}}, {"money": {"currency_field": "Currency", "rounding": "ROUND_HALF_UP"}}]},
    {"target": "pay_group", "source": "EmploymentType", "transforms": [{"code_map": {"code_set": "hris_emp_type"}}]}
  ],
  "source_timezone": "Europe/Berlin"
}
```

Mechanics: the spec references **named transforms from a code registry** (`strip`, `date`,
`code_map`) — never `eval` or user-provided expressions. Validate the spec with a JSON Schema /
pydantic model on save; compile it once per job into a `list[Transform]`. Selection: the active
version where `effective_from <= business_date < effective_to`. On first use, set
`first_used_at` (`update(...)` on the row) — from then on, edits require a new version, because
outputs reference it. The model-level `save()` guard does not catch `QuerySet.update()`; enforce
with a DB trigger or by revoking update permissions if the audit requirement is strict
(VERIFY: your audit requirements). Every output/staging row stores `mapping_definition_id`, so
reprocessing can reproduce or deliberately re-map with a newer version.

```python
REGISTRY: dict[str, Callable[..., Transform]] = {}       # "date" -> factory(field, **params)

def compile_spec(spec: dict) -> list[Transform]:
    steps: list[Transform] = []
    for rule in spec["fields"]:
        for t in rule.get("transforms", []):
            name, params = (t, {}) if isinstance(t, str) else next(iter(t.items()))
            factory = REGISTRY.get(name)
            if factory is None:
                raise ValueError(f"unknown transform {name!r}")   # config error: fail job, not rows
            steps.append(factory(source=rule["source"], target=rule["target"], **params))
    return steps
```

## 6. Production use cases

- **Employee data conversion (HRIS → payroll).** HRIS exports German-locale CSV; mapping v7
  converts `StartDate` (`%d.%m.%Y`), salary with decimal comma, `EmploymentType` codes
  (`FT`, `PT`, `WS`) to payroll pay groups; contractors (`conditional`) skip pension fields.
  Payroll period dates stay `date`; shift timestamps localised with Europe/Berlin and DST checks.
- **Financial transaction mapping (bank statement → ledger).** camt.053 entries map to journal
  lines: debit/credit indicator → sign, booking date → `date`, amount `Decimal` exact (no
  re-rounding), bank transaction code → ledger account via dated `CodeMapping`; foreign-currency
  entries converted with the booking-date FX rate stored on the line.
- **Product catalog synchronisation.** Supplier JSONL with nested variants exploded to SKU rows,
  weights converted LB→KG with `Decimal` factors, prices converted to EUR in Polars for 5M rows,
  category codes mapped; unmapped categories quarantine the product, not the feed.
- **Hospital doctor data import.** Licence numbers normalised (`ca 123456` → `CA-123456`),
  specialty free text/legacy codes mapped to the national taxonomy via a code table with
  validity dates, sites mapped to facility IDs; unmapped specialty → rejected with
  `map.unmapped_code` for the credentialing team to add a mapping.
- **Customer information synchronisation.** CRM ↔ billing: names split/merged, country names →
  ISO 3166 alpha-2, phone → E.164, consent flags mapped with explicit "unknown" state;
  last-modified timestamps normalised to UTC for conflict resolution.
- **ERP-to-CRM conversion.** ERP accounts (nested addresses, contacts) flattened into CRM
  account + contact records; payment terms codes mapped; `mapping_definition_id` stored so a
  later mapping fix can re-run only affected records.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Pure-function pipeline | Testable, deterministic, per-row errors | Slower than vectorised for bulk math |
| Declarative mapping spec | Per-tenant changes without deploys; auditable versions | Can grow into an untestable rules language; needs schema validation |
| Code-based mapping | Full expressiveness, code review | Deploy per change; per-tenant variation awkward |
| DB code tables | Business-maintained, dated | Needs governance; stale tables break imports |
| Vectorised (Polars/pandas) | Fast bulk transforms | Rounding/Decimal subtleties; row error tracking extra work |
| SQL in staging | Set-based, near the data | Logic split across languages; harder unit testing |

## 8. Security and performance considerations

**Security**
- Never `eval`/`exec` mapping expressions or let users upload Python/Jinja; specs reference
  registered transforms only, with validated parameters.
- Mapping definitions are configuration with financial impact: restrict who can create/activate
  versions (permissions per tenant), audit changes, require review for ACTIVE status
  ([../../django-security-engineering/references/authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- Tenant isolation: reference caches keyed by tenant; never share a `Context` across tenants.
- Derived fields can create new sensitive data (full name + DOB + licence); apply masking and
  retention rules to outputs and error payloads ([data-processing-security.md](data-processing-security.md)).

**Performance**
- Compile specs once per job; precompile regexes; preload reference data per batch into dicts.
- Avoid per-record ORM lookups in transforms (N+1) — resolve FKs in bulk at load
  ([database-loading-and-persistence.md](database-loading-and-persistence.md)).
- `Decimal` arithmetic is slower than float but rarely the bottleneck versus I/O and ORM; do not
  trade correctness for it. Profile first ([data-processing-performance.md](data-processing-performance.md)).
- Dict-copy per step (`rec | {...}`) costs allocations; for very long pipelines compose steps
  into one function per field, or vectorise.
- `ZoneInfo` instances are cached by key; still create them once per job for clarity.

## 9. Common mistakes and anti-patterns

- `float(amount)` anywhere in the money path → cent drift, reconciliation failures → `Decimal` from string.
- Quantising at every step → double rounding → round once at the defined business step.
- `datetime.strptime(...).replace(tzinfo=UTC)` for local timestamps → hours-off data → `localise()` with source zone.
- Storing dates as midnight UTC datetimes → off-by-one day in UI → keep `date`.
- Ignoring DST gaps/overlaps → shifts with negative or doubled duration → detect, reject or apply documented rule.
- Defaulting unmapped codes to `OTHER`/`None` → silent misclassification → `map.unmapped_code` rejection.
- Using today's FX rate for historic transactions → irreproducible amounts → dated rates stored on output.
- Editing a mapping in place after use → lineage broken, results not reproducible → immutable versions.
- Catching `Exception` in transforms → code bugs disguised as data errors → catch `TransformError` only.
- Pandas/Polars for row-wise business rules with user-facing errors → lost locators → row-wise Python.
- Mapping specs with embedded expressions → injection and untestable logic → named registry.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Totals differ by a few cents from source | Float conversion or double rounding | Trace a record; `Decimal` from string; single rounding |
| Times shifted by 1–2 hours | Wrong/missing source timezone, DST | Check mapping `source_timezone`; `localise` tests around transitions |
| Dates one day off | Date stored as datetime / TZ conversion | Use `DateField`; never convert pure dates |
| Excel dates in 1904 or off by 4 years | Workbook uses 1904 epoch | Check workbook epoch; pass `epoch_1904=True` |
| Many `map.unmapped_code` after a source upgrade | New codes in source system | Add mappings (new version/dated rows); re-process rejected records |
| Same input, different output on rerun | Reference data changed / mapping edited / "now" used | Snapshot reference data; immutable mapping; pass business date |
| FX `rate_missing` on weekends | No rates published | Documented fallback (previous business day) in rate lookup |

Validation checklist:
- [ ] Money: `Decimal` only, rounding mode and step documented, minor units from reference data.
- [ ] Dates: per-source timezone and format; DST gap/overlap handled; Excel epoch respected.
- [ ] Unmapped codes/units/currencies raise coded errors.
- [ ] Transforms pure; reference data snapshotted per job/batch.
- [ ] Mapping definitions versioned, validated on save, immutable once used, referenced by outputs.
- [ ] Tests cover DST transitions, leap days, rounding ties (`0.005`), locale separators
  ([data-processing-testing.md](data-processing-testing.md)).

## 11. Related references and skills

- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — validation layers, `RejectedRecord`.
- [file-processing-engineering.md](file-processing-engineering.md) — typed values from Excel/JSON.
- [data-import-and-export-systems.md](data-import-and-export-systems.md) — user column mapping (`ColumnMapping`).
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — where transform steps sit; schema evolution.
- [data-reconciliation-engineering.md](data-reconciliation-engineering.md) — proving transformed totals.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — FK resolution, upserts.
- [../../django-integrations/references/payment-and-financial-integrations.md](../../django-integrations/references/payment-and-financial-integrations.md).
- [../../django-integrations/references/data-synchronization-and-reconciliation.md](../../django-integrations/references/data-synchronization-and-reconciliation.md).
- [../../django-architecture/references/service-layer.md](../../django-architecture/references/service-layer.md).

## 12. Official documentation and external references

- https://docs.python.org/3/library/decimal.html
- https://docs.python.org/3/library/zoneinfo.html
- https://docs.python.org/3/library/datetime.html#datetime.datetime.fold
- https://docs.python.org/3/library/functools.html
- https://docs.djangoproject.com/en/5.2/topics/i18n/timezones/
- https://docs.djangoproject.com/en/5.2/ref/models/fields/#decimalfield
- https://docs.djangoproject.com/en/5.2/ref/models/fields/#enumeration-types
- https://docs.djangoproject.com/en/5.2/ref/models/fields/#jsonfield
- https://openpyxl.readthedocs.io/en/stable/datetime.html
- https://docs.pola.rs/api/python/stable/reference/expressions/index.html
- https://pandas.pydata.org/docs/user_guide/timeseries.html
- https://learn.microsoft.com/en-us/office/troubleshoot/excel/1900-and-1904-date-system
