# Data Processing Testing

> **Scope:** How to test data-processing code specifically — transformations, parsers, schemas,
> imports/exports, large and malformed files, boundaries, duplicates, partial failure, retries,
> idempotency, reconciliation, background tasks, throughput, property-based tests and fixture
> design. **Not here:** general pytest/pytest-django mechanics, factories and DB test setup →
> [django-testing](../../django-testing/SKILL.md) (especially
> [fixtures-and-test-data.md](../../django-testing/references/fixtures-and-test-data.md),
> [database-and-transaction-testing.md](../../django-testing/references/database-and-transaction-testing.md),
> [integration-and-background-task-testing.md](../../django-testing/references/integration-and-background-task-testing.md));
> load benchmarking methodology → [django-performance](../../django-performance/references/performance-testing-and-benchmarking.md).

## 1. Purpose and scope

Data pipelines fail in ways ordinary request/response code does not: a BOM in byte 0, a quoted
newline in row 48 211, a batch that commits half its rows before the worker dies, a retry that
doubles a payroll run. Push logic into pure functions so the expensive layers stay thin:

| Layer | What it proves | Speed | DB |
|---|---|---|---|
| Pure unit (transform, validate, map) | Row-level logic is correct for every edge case | ms | No |
| Parser/format | Bytes → records is correct across encodings, dialects, corruption | ms | No |
| Service/integration | Parse → validate → load → job state with a real test DB | 100 ms–s | Yes |
| Task/orchestration | Retries, idempotency, partial failure, on-commit enqueue | s | Yes (often transactional) |
| Large-file / performance | Memory ceiling, throughput floor | s–min, `slow` marker | Optional |
| Property-based | Invariants hold for generated inputs | s | Prefer no |

## 2. Core concepts

- **Golden file** — small committed input + committed expected output; regenerate deliberately.
- **Minimal fixture** — smallest input triggering exactly one behaviour (one BOM row).
- **Generated fixture** — built at test time (`io.BytesIO`/`tmp_path`), never committed.
- **Invariant** — a property that must hold for all inputs: `parse(serialise(x)) == x`,
  `transform` never changes row count, `load(load(x)) == load(x)` (idempotency).
- **Observable outcome** — tests assert on durable state (rows, `ImportJob` status/counters,
  `RejectedRecord` rows, files in storage), not on log lines or internal calls.
- **Failure injection** — deterministically raising in batch N / the Nth storage or HTTP call.

## 3. Internal mechanics

### Why test transactions matter for pipelines

`TestCase` [Django] (and pytest-django's `db` fixture [pytest-django]) wraps each test in a
transaction that is rolled back. Consequences for data-processing tests:

- Batch-level `transaction.atomic()` [Django] blocks become **savepoints** — fine for asserting
  row counts after a simulated failure in batch N.
- `transaction.on_commit()` callbacks never fire unless captured: use
  `TestCase.captureOnCommitCallbacks(execute=True)` [Django] or the
  `django_capture_on_commit_callbacks` fixture [pytest-django].
- Code that opens a **second connection** (a separate worker, `COPY` on a raw psycopg connection,
  advisory locks across sessions, `select_for_update(skip_locked=True)` contention) needs real
  commits → `TransactionTestCase` / `transactional_db` [pytest-django]. These are slower (table
  truncation); use them only where needed.

### Celery in tests

- `my_task.apply(args=...)` [third-party: celery] runs the task synchronously in-process (eager
  path); calling the service function directly skips Celery entirely — prefer that for logic and
  keep one `.apply()` test for the wrapper (binding, retry config).
- `CELERY_TASK_ALWAYS_EAGER=True` (`task_always_eager`) makes `.delay()` run inline. Caveats: no
  serialisation round-trip (non-JSON args pass silently), no broker ack/visibility semantics, no
  time limits, retries behave differently from a real worker, and inline execution inside the
  caller's transaction hides `on_commit` ordering bugs. Set `task_eager_propagates=True` so
  exceptions surface (VERIFY: setting names for your Celery 5.x config namespace).

### Memory measurement

`tracemalloc` [stdlib] (`start()`, `get_traced_memory()` → `(current, peak)`, `reset_peak()`)
sees only Python-allocator memory, not C-extension buffers (pyarrow, lxml, numpy) — assert RSS
or treat as indicative there (VERIFY: allocator coverage for the specific library).

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Row-level transform/validation logic | Parametrised pure unit tests | Testing through the full import service for each edge case |
| Parser edge cases (BOM, encodings, dialects) | In-memory `io.BytesIO` minimal fixtures | Committing dozens of binary files nobody can read |
| Import correctness end-to-end | Service test with real test DB + golden file | Mocking the ORM (hides constraint, UPSERT and FK behaviour) |
| Code using `on_commit` enqueue | `captureOnCommitCallbacks(execute=True)` | `ALWAYS_EAGER` alone (callback never fires inside `TestCase`) |
| Multi-connection concurrency, COPY, locks | `transactional_db` / `TransactionTestCase` | Using it for every test (slow) |
| Object storage | `InMemoryStorage` (Django ≥ 4.2 — VERIFY: import path) or `FileSystemStorage(tmp_path)` | Real S3/Azure in unit tests; escalate to moto/Azurite for backend-specific behaviour |
| Large files | Generated on the fly, marked `slow` | Committed 500 MB fixtures |
| Throughput regressions | Coarse floor (records/sec) on a fixed CI runner, trend tracked | Tight thresholds on shared runners (flaky) |
| Broad input space (text, decimals, dates) | Hypothesis properties | Only hand-picked examples for round-trip code |
| PII in fixtures | Synthetic/factory data | Production extracts, even "anonymised" |

## 5. Django/Python implementation patterns

### 5.1 Table-driven transformation tests [pytest]

```python
from decimal import Decimal
import pytest
from payroll.transforms import parse_amount, TransformError

@pytest.mark.parametrize(("raw", "expected"), [
    ("1234.50", Decimal("1234.50")), ("1,234.50", Decimal("1234.50")), (" 12 ", Decimal("12.00")),
    ("-0.005", Decimal("-0.00")), ("0.015", Decimal("0.02")),   # ROUND_HALF_EVEN per business rule
])
def test_parse_amount_valid(raw: str, expected: Decimal) -> None:
    assert parse_amount(raw) == expected

@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "NaN", "Infinity", "1e400"])
def test_parse_amount_rejects(raw: str) -> None:
    with pytest.raises(TransformError):
        parse_amount(raw)
```

Always include an explicit test that money never passes through `float`
(`Decimal("0.1") + Decimal("0.2") == Decimal("0.3")` on the transformed output).

### 5.2 Parser tests with in-memory fixtures [stdlib] [pytest]

```python
import io
import pytest
from imports.parsers import iter_csv_records, ParseError

def _bytes(text: str, encoding: str = "utf-8") -> io.BytesIO:
    return io.BytesIO(text.encode(encoding))

def test_strips_utf8_bom() -> None:
    src = io.BytesIO(b"\xef\xbb\xbfemployee_id,name\r\nE1,Ana\r\n")
    rows = list(iter_csv_records(src))
    assert rows[0].keys() == {"employee_id", "name"}   # not "\ufeffemployee_id"

def test_quoted_newline_is_one_record() -> None:
    src = _bytes('id,note\r\n1,"line one\nline two"\r\n2,x\r\n')
    rows = list(iter_csv_records(src))
    assert [r["id"] for r in rows] == ["1", "2"]
    assert rows[0]["note"] == "line one\nline two"

@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_declared_encodings(encoding: str) -> None:
    src = _bytes("id,name\r\n1,Zoë\r\n", encoding)
    rows = list(iter_csv_records(src, encoding=encoding))
    assert rows[0]["name"] == "Zoë"

def test_undeclared_latin1_fails_loudly() -> None:
    src = _bytes("id,name\r\n1,Zoë\r\n", "latin-1")
    with pytest.raises(ParseError, match="encoding"):
        list(iter_csv_records(src, encoding="utf-8"))
```

Also cover: NUL bytes, header-only file, trailing blank lines, ragged rows, duplicate header
names, a field over `csv.field_size_limit()` [stdlib], `;` delimiter, CRLF vs LF.

### 5.3 Schema validation tests

Test the schema as data: one valid baseline row, then mutate one field per case so each test
proves one rule. Assert on the **error code and field**, not the message text.

```python
import pytest
from imports.validation import validate_row

BASE = {"employee_id": "E100", "hire_date": "2024-02-29", "salary": "52000.00", "dept": "FIN"}

@pytest.mark.parametrize(("field", "value", "code"), [
    ("employee_id", "", "required"), ("hire_date", "2023-02-29", "invalid_date"),
    ("salary", "-1", "out_of_range"), ("dept", "XXX", "unknown_reference"),
])
def test_rule_violations(field: str, value: str, code: str) -> None:
    errors = validate_row({**BASE, field: value}, known_depts={"FIN", "HR"})
    assert [(e.field, e.code) for e in errors] == [(field, code)]
```

Also assert `BASE` itself is valid, so a broken baseline cannot pass cases for the wrong reason.
Rules and codes: [data-validation-and-cleansing.md](data-validation-and-cleansing.md).

### 5.4 Import end-to-end with a real test DB [pytest-django]

```python
import pytest
from django.core.files.base import ContentFile
from imports.models import ImportJob
from imports.services import run_import
from hr.models import Employee

pytestmark = pytest.mark.django_db

def test_import_golden_file(tenant, golden) -> None:
    job = ImportJob.objects.create(tenant=tenant, kind="employees")
    job.source_file.save("employees.csv", ContentFile(golden("employees_small.csv")))
    run_import(job.pk)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.PARTIAL
    assert (job.rows_total, job.rows_loaded, job.rows_rejected) == (10, 8, 2)
    assert Employee.objects.filter(tenant=tenant).count() == 8
    rejected = list(job.rejected_records.values_list("row_number", "error_code"))
    assert rejected == [(4, "invalid_date"), (9, "duplicate_key")]
```

Assert tenant isolation in the same suite: an import for tenant A must not touch tenant B rows.

### 5.5 Export tests: parse the output back [stdlib]

```python
import csv, io
import pytest
from exports.services import write_customer_csv

DANGEROUS = ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx", "\rx"]

@pytest.mark.parametrize("value", DANGEROUS)
def test_formula_injection_neutralised(value: str) -> None:
    buf = io.StringIO(newline="")
    write_customer_csv(buf, [{"id": 1, "name": value}])
    rows = list(csv.DictReader(io.StringIO(buf.getvalue(), newline="")))
    assert rows[0]["name"].startswith("'")
    assert rows[0]["name"][1:] == value
```

Add a round-trip case for `Zoë "Z"\nSmith` (non-ASCII, quotes, newline). Treatment of real
negative numbers is a policy decision — test what
[data-processing-security.md](data-processing-security.md) defines. For XLSX exports, reload with
`openpyxl.load_workbook(buf, read_only=True)` [third-party: openpyxl] and compare cell values and
types (dates as dates, Decimals as numbers or strings per contract).

### 5.6 Large-file tests with a memory ceiling [stdlib] [pytest]

```python
import tracemalloc
from pathlib import Path
import pytest
from imports.parsers import iter_csv_records

def write_csv(path: Path, rows: int) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:   # streamed to disk, not RAM
        fh.write("id,name,amount\r\n")
        fh.writelines(f"{i},name-{i},{i % 1000}.25\r\n" for i in range(rows))
    return path

@pytest.mark.slow
def test_streaming_parse_memory_is_bounded(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "big.csv", 1_000_000)
    tracemalloc.start()
    try:
        with src.open("rb") as fh:
            count = sum(1 for _ in iter_csv_records(fh))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert count == 1_000_000
    assert peak < 20 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB — parser is buffering"
```

Register the marker (`markers = slow: long-running data tests`) in `pytest.ini`/`pyproject.toml`
and run `-m "not slow"` on every push, all tests nightly. A better invariant than an absolute
ceiling: peak at 1M rows ≈ peak at 100k rows (memory does not scale with input).

### 5.7 Boundary tests

```python
import pytest
from imports.loading import load_in_batches

BATCH = 500

@pytest.mark.django_db
@pytest.mark.parametrize("n", [0, 1, BATCH - 1, BATCH, BATCH + 1, 2 * BATCH + 1])
def test_batch_boundaries(n: int, tenant, make_rows, django_assert_max_num_queries) -> None:
    rows = make_rows(n)
    expected_batches = -(-n // BATCH)               # ceil
    with django_assert_max_num_queries(expected_batches * 2 + 2):
        result = load_in_batches(tenant, rows, batch_size=BATCH)
    assert result.loaded == n
```

Also test the configured limits exactly at and one past the limit: max rows, max file size, max
columns, max field length, max ZIP members. "At limit" must succeed; "limit + 1" must fail with a
clear, user-facing error **before** any rows are written.

### 5.8 Malformed-file tests

```python
import io, zipfile
import pytest
from imports.detect import open_tabular, UnsupportedFile, UnsafeArchive

def test_truncated_xlsx(golden) -> None:
    data = golden("customers.xlsx")
    with pytest.raises(UnsupportedFile):
        open_tabular(io.BytesIO(data[: len(data) // 2]), filename="customers.xlsx")

def test_extension_lies(golden) -> None:
    with pytest.raises(UnsupportedFile, match="content does not match"):
        open_tabular(io.BytesIO(golden("customers.xlsx")), filename="customers.csv")

def test_small_zip_bomb_rejected_by_ratio() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rows.csv", b"0" * 5_000_000)   # ~5 MB → a few KB compressed
    buf.seek(0)
    with pytest.raises(UnsafeArchive, match="ratio"):
        open_tabular(buf, filename="rows.zip", max_ratio=100)
```

Also: binary content (PNG header, NUL runs) named `.csv`, invalid XML, XML with a DOCTYPE/entity
declaration (must be rejected or not expanded — see
[data-processing-security.md](data-processing-security.md)), ZIP members with `../` or absolute
paths, encrypted PDF, empty file (0 bytes). Build bombs small-scale in-test; never commit one.

### 5.9 Duplicates, partial failure and retries

```python
import pytest
from unittest import mock
from imports.models import ImportJob
from imports import loading
from imports.services import run_import

@pytest.mark.django_db
def test_failure_in_batch_3_keeps_batches_1_2(tenant, job_with_rows) -> None:
    job = job_with_rows(tenant, rows=1_200, batch_size=500)   # batches: 500, 500, 200
    real = loading.load_batch
    effects = [real, real, loading.PermanentLoadError("constraint violated")]
    def flaky(*args, **kwargs):
        effect = effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect(*args, **kwargs)
    with mock.patch.object(loading, "load_batch", side_effect=flaky):
        run_import(job.pk)
    job.refresh_from_db()
    assert job.status == ImportJob.Status.PARTIAL
    assert job.rows_loaded == 1_000
    assert job.checkpoint == {"last_batch": 2}
    assert job.error_summary["code"] == "constraint violated"
```

Retry tests follow the same pattern with a **transient** error: fail batch 3 once, re-run
`run_import(job.pk)`, assert it resumes from the checkpoint (batches 1–2 not re-sent — count calls)
and ends `SUCCEEDED` with exactly 1 200 rows. Duplicate tests: same natural key twice **in one
batch** (must be deduped before `bulk_create(update_conflicts=True)` — PostgreSQL rejects
duplicate conflict keys within one statement), across batches, and already in the DB.

### 5.10 Idempotency and reconciliation

```python
import pytest
from hr.models import Employee
from imports.services import run_import

def business_state(tenant) -> list[tuple]:
    # Compare business columns only; audit columns (updated_at, job id) legitimately change.
    qs = Employee.objects.filter(tenant=tenant).order_by("employee_id")
    return list(qs.values_list("employee_id", "name", "salary", "dept_id"))

@pytest.mark.django_db
def test_import_is_idempotent(tenant, job_factory, golden) -> None:
    run_import(job_factory(tenant, golden("employees_small.csv")).pk)
    first = business_state(tenant)
    run_import(job_factory(tenant, golden("employees_small.csv")).pk)
    assert business_state(tenant) == first
    assert len(first) == 8
```

Also re-run the **same** job PK (simulated redelivery): it must be a no-op once `SUCCEEDED`.

Reconciliation tests seed source and target with known differences (one missing, one duplicate,
one amount off by 0.01, one within tolerance) and assert the exact `ReconciliationException`
set — see [data-reconciliation-engineering.md](data-reconciliation-engineering.md).

### 5.11 Storage doubles and background tasks [Django] [pytest-django]

```python
import pytest
from django.core.files.storage import InMemoryStorage   # Django ≥ 4.2
from imports import tasks
from imports.services import submit_import

@pytest.fixture
def mem_storage(monkeypatch) -> InMemoryStorage:
    storage = InMemoryStorage()
    monkeypatch.setattr("imports.services.import_storage", storage)
    return storage

@pytest.mark.django_db
def test_submit_enqueues_after_commit(tenant, user, mem_storage, django_capture_on_commit_callbacks, mocker) -> None:
    delay = mocker.patch.object(tasks.process_import, "delay")
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        job = submit_import(tenant, user, filename="a.csv", content=b"id\r\n1\r\n")
    assert len(callbacks) == 1
    delay.assert_called_once_with(job.pk)
    assert mem_storage.exists(job.source_file.name)
```

One wrapper test is enough: `tasks.process_import.apply(args=(job.pk,)).successful()`.

`mocker` comes from pytest-mock [third-party: pytest-mock]. For S3-specific behaviour (multipart,
presigned URLs, `ContentDisposition`), use moto [third-party: moto] (`mock_aws` in moto ≥ 5,
`mock_s3` earlier — VERIFY: decorator name for your moto version) or Azurite for Azure Blob. Patterns:
[mocking-and-dependency-isolation.md](../../django-testing/references/mocking-and-dependency-isolation.md).

### 5.12 Performance regression [pytest]

A `slow` test generates N rows (e.g. 200 000) in memory, times the transform with
`time.perf_counter()` [stdlib] and asserts `rows / elapsed > floor` with the measured rate in the
failure message. Set the floor at ~50 % of the measured baseline on a fixed CI runner, time only
the stage under test (not fixture generation), and record the rate as an artefact so trends are
visible. Real benchmarking:
[data-processing-performance.md](data-processing-performance.md).

### 5.13 Property-based tests [third-party: hypothesis]

```python
from decimal import Decimal
import io
from hypothesis import given, settings, strategies as st
from exports.serialise import write_rows
from imports.parsers import iter_csv_records
from imports.transforms import normalise_row

cell = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=40)
amount = st.decimals(min_value=Decimal("-1e9"), max_value=Decimal("1e9"), places=2,
                     allow_nan=False, allow_infinity=False)

@given(st.lists(st.tuples(cell, amount), max_size=50))
@settings(max_examples=300, deadline=None)
def test_csv_round_trip(rows: list[tuple[str, Decimal]]) -> None:
    buf = io.BytesIO()
    write_rows(buf, ["name", "amount"], rows, neutralise_formulas=False)
    buf.seek(0)
    back = [(r["name"], Decimal(r["amount"])) for r in iter_csv_records(buf)]
    assert back == rows

@given(st.dictionaries(st.sampled_from(["name", "email", "dept"]), cell))
def test_normalise_is_idempotent(row: dict[str, str]) -> None:
    once = normalise_row(row)
    assert normalise_row(once) == once
```

Notes: `\r` handling in round trips depends on your writer's `lineterminator` and quoting — if a
property fails on `\r`, that is a real finding, not a reason to filter it out. Keep property
tests DB-free; if a DB is required, use `hypothesis.extra.django.TestCase` because
function-scoped pytest fixtures are not reset between examples (VERIFY: current Hypothesis
health-check behaviour with pytest-django).

### 5.14 Fixture design

- Layout: `tests/fixtures/data/employees_small.csv` + `employees_small.expected.json` (golden
  pair), `edge/` for one-behaviour fixtures (`bom_utf8.csv`, `quoted_newline.csv`, `cp1252.csv`).
- Binary fixtures (xlsx, parquet, zip) built by a committed script; a `golden` fixture reads bytes
  by name; an `--update-golden` flag rewrites expected files for human-reviewed diffs.
- Factories (factory_boy [third-party: factory-boy]) + seeded `Faker` for synthetic DB state.
  Never production extracts — no real names, IBANs, national IDs or medical data, even "masked".

## 6. Production use cases

| Scenario | Highest-value tests |
|---|---|
| Payroll import (~40k rows/month) | Golden bank-CSV file; batch boundaries; re-upload does not double salaries; Σ net pay == header control total |
| Hospital doctor rosters (XLSX, 30 sites) | Merged cells, hidden sheets, formulas without cached values (`data_only=True` → `None`); night shifts across DST |
| Payments settlement file | Hypothesis: amount parsing never loses precision; failed batch leaves `ReconciliationRun` resumable |
| ERP→CRM customer sync | Transient 503 on call N then resume; idempotency on external ID; same customer twice in one page |
| Product catalog export (2M SKUs) | Constant memory while streaming (slow); formula injection on vendor-supplied names |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Pure unit + parametrise | Fast, precise failure messages, covers many edge cases | Misses DB/constraint behaviour |
| Real test DB service tests | Catches UPSERT, constraint, FK and tenant bugs | Slower; needs careful transaction mode choice |
| Golden / generated files | Diffs show regressions; no repo bloat for large data | Rot if regenerated unreviewed; CI time |
| Hypothesis | Finds encodings/edge cases humans miss; shrinks to minimal case | Slower; needs thought to state good properties |
| `ALWAYS_EAGER` | Simple wiring tests | Hides serialisation, ack and on_commit ordering bugs |

## 8. Security and performance considerations

**Security**
- Fixtures synthetic; a CI check greps `tests/fixtures/` for real email domains, IBAN/NI patterns.
- Security behaviours are tested as requirements: formula neutralisation, XXE rejection, ZIP
  ratio/count limits, archive path traversal, size limit before parsing, tenant isolation of
  imports/exports/download links (expired or foreign-tenant URL → 403/404), and error reports
  masking sensitive columns (e.g. `national_id`). No real malware or full-size bombs in the repo.

**Performance**
- Default suite fast: pure tests dominate; `slow` marker for > 1 s; `transactional_db` sparingly.
- Use `django_assert_num_queries` / `django_assert_max_num_queries` [pytest-django] to catch
  per-row queries (N+1 in reference resolution) — the most common import performance regression.
- Memory tests: assert scaling behaviour, not absolute bytes, when C extensions are involved.
- Run Hypothesis with a lower `max_examples` on push and a higher profile nightly
  (`settings.register_profile`).

## 9. Common mistakes and anti-patterns

- Mocking the ORM in import tests → UPSERT/constraint bugs ship → use a real test DB.
- Only testing the happy file → BOM/encoding/quoted-newline bugs in production → minimal edge
  fixtures per behaviour.
- `ALWAYS_EAGER` as the only task test → on_commit and serialisation bugs hidden → test the
  service directly, `captureOnCommitCallbacks` for enqueue, one `.apply()` test for the wrapper.
- Asserting on log/error text → brittle → assert error codes and durable state.
- Partial-failure tests asserting only "raises" → commit state unproven → assert rows,
  checkpoint and job status.
- Idempotency "tested" by running once → duplicates on retry → run twice and compare.
- Export tests comparing strings → quoting bugs missed → parse output back.
- Tight throughput thresholds on shared runners → flaky CI → generous floors plus trends.
- Production data or committed large binaries in fixtures → privacy incident, slow clones →
  synthetic, generated data.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `on_commit` task never runs in test | `TestCase` transaction never commits | Use `captureOnCommitCallbacks(execute=True)` |
| Test passes, prod import duplicates rows | Idempotency never exercised; dedupe missing within batch | Add run-twice and same-key-in-batch tests |
| Header key is `"\ufeffid"` | BOM not stripped | Open with `encoding="utf-8-sig"`; add BOM fixture |
| Memory test flaky | GC timing / C-extension allocations | Compare peaks at two sizes; `gc.collect()` before measuring |
| Hypothesis `FailedHealthCheck` with db fixture | Function-scoped fixture reused across examples | Make property DB-free or use `hypothesis.extra.django.TestCase` |
| Task test passes eagerly, fails on worker | Non-JSON args, model instances passed | Pass PKs; test serialisation explicitly |
| Batch boundary off-by-one only at N=batch_size+1 | Last partial batch dropped | Parametrise 0/1/B-1/B/B+1 |
| COPY/second-connection code sees no data | Data uncommitted in `TestCase` | Use `transactional_db` |

**Validation checklist**
- [ ] Every transform/validator has parametrised valid + invalid cases.
- [ ] Parser suite covers BOM, encodings, quoted newlines, ragged rows, NUL bytes, empty file.
- [ ] Import service tested end-to-end with a real DB, golden file, tenant isolation.
- [ ] Exports parsed back; formula injection cases covered.
- [ ] Partial failure, retry-resume, run-twice idempotency and in-batch duplicate tests exist.
- [ ] Limits tested at N and N+1; malformed/unsafe files rejected before writes.
- [ ] `slow` marker registered; large-file memory and throughput tests run nightly.
- [ ] Fixtures synthetic, reproducible, small.

## 11. Related references and skills

- Behaviour under test: [data-validation-and-cleansing.md](data-validation-and-cleansing.md),
  [file-processing-engineering.md](file-processing-engineering.md),
  [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md),
  [database-loading-and-persistence.md](database-loading-and-persistence.md),
  [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md),
  [django-background-processing.md](django-background-processing.md),
  [data-processing-security.md](data-processing-security.md),
  [data-processing-performance.md](data-processing-performance.md).
- [django-testing](../../django-testing/SKILL.md):
  [performance-and-reliability-testing.md](../../django-testing/references/performance-and-reliability-testing.md);
  [django-performance: performance-testing-and-benchmarking.md](../../django-performance/references/performance-testing-and-benchmarking.md).

## 12. Official documentation and external references

- Django testing tools: https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- Django `InMemoryStorage`: https://docs.djangoproject.com/en/5.2/ref/files/storage/
- Django transactions in tests: https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Python `csv`: https://docs.python.org/3/library/csv.html
- Python `tracemalloc`: https://docs.python.org/3/library/tracemalloc.html
- Python `zipfile`: https://docs.python.org/3/library/zipfile.html
- Celery testing: https://docs.celeryq.dev/en/stable/userguide/testing.html
- Hypothesis: https://hypothesis.readthedocs.io/en/latest/
- openpyxl: https://openpyxl.readthedocs.io/en/stable/
- OWASP CSV Injection: https://owasp.org/www-community/attacks/CSV_Injection
