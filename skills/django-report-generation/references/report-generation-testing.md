# Report Generation Testing

> **Scope:** How to prove a reporting subsystem correct: calculation unit tests, integration tests, validating the *contents* of CSV/XLSX/PDF/DOCX/chart outputs, download endpoint and header tests, authorisation and tenant-isolation tests, background task, retry and failure tests, schedule tests across time zones and DST, deterministic fixtures, snapshot and visual regression testing, and large-dataset and memory tests. **Not here:** general test strategy, pytest/pytest-django mechanics, factories and mocking tools → [django-testing](../../django-testing/SKILL.md); what controls must exist → [reporting-security-and-compliance.md](reporting-security-and-compliance.md); performance budgets → [report-generation-performance.md](report-generation-performance.md).

## 1. Purpose and scope

A green HTTP 200 proves almost nothing about a report. The defects that matter — wrong totals, a
missing day at a DST boundary, another tenant's rows, a formula executed in Excel, a PDF that
opens blank, a worker that retries and emails twice — are only caught by tests that **parse the
generated file and assert on its contents**, and by tests that exercise failure paths. This
reference gives report-specific test patterns; general principles (levels, isolation, factories)
come from [django-testing — testing-fundamentals](../../django-testing/references/testing-fundamentals.md).

## 2. Core concepts

| Layer | What it proves | Speed | Tooling |
|---|---|---|---|
| Calculation unit | Selector/aggregation produces correct numbers for hand-computed fixtures | Fast (DB) | `[pytest-django]` `django_db`, factories |
| Renderer unit | Given rows, the file has the right structure, types and escaping | Fast, no DB | Parse output with format library |
| Endpoint integration | Auth, scoping, headers, streaming, status codes | Medium | Django test `Client` / DRF `APIClient` |
| Task integration | State transitions, artifact creation, retries, idempotency | Medium | Call the task function directly; fake storage |
| Schedule tests | Next-run computation and dispatch across time zones/DST | Fast | Clock control `[third-party: time-machine]` or `[third-party: freezegun]` |
| Snapshot / golden file | Output unchanged unless intended | Medium | Normalised comparison |
| Visual regression | Layout unchanged | Slow | Rasterise PDF `[external tool]` + image diff |
| Large data / memory | Bounded memory, within time budget | Slow (nightly) | `tracemalloc` `[stdlib]`, container metrics |

Test oracle principle: expected values come from **independently computed fixtures**
(hand-written numbers), never from re-running the same aggregation logic in the test.

## 3. Internal mechanics

### Reading responses

- `HttpResponse`: `response.content` (bytes).
- `StreamingHttpResponse` / `FileResponse` `[Django]`: there is no `.content`; consume
  `b"".join(response.streaming_content)`. Consuming the stream is also what executes the
  generator — a test that does not consume it never runs the export code.
- Async streaming responses (Django ≥ 4.2) yield from an async iterator; under the sync test
  client Django consumes them with a warning [VERIFY: behaviour of sync Client with async
  streaming_content in Django 5.2] — use `AsyncClient` for async views
  ([django-testing — async-testing](../../django-testing/references/async-testing.md)).

### Parsing outputs

| Format | Parse with | Assert on |
|---|---|---|
| CSV | `csv.reader(io.StringIO(body.decode("utf-8-sig")))` `[stdlib]` | Header row, column count per row, typed value formats, escaping, BOM presence |
| JSON / JSONL | `json.loads` per line `[stdlib]` | Schema (`[third-party: jsonschema]`), key order if deterministic output is promised |
| XML | `xml.etree.ElementTree.fromstring` `[stdlib]`; XSD with `[third-party: lxml]` | Structure, namespaces, schema validity |
| XLSX | `openpyxl.load_workbook(io.BytesIO(body))` `[third-party: openpyxl]` | Sheet names, cell values and types, `number_format`, freeze panes, formulas as strings |
| PDF | `[third-party: pypdf]` `PdfReader` (page count, metadata, text); `[third-party: pdfplumber]` for tables/positions | Page count, presence of key text, metadata, no unescaped markup |
| DOCX | `docx.Document(io.BytesIO(body))` `[third-party: python-docx]` | Paragraph/table text, styles, section headers |
| PNG/SVG charts | Image header bytes / `[third-party: Pillow]` size; SVG via XML parser | Non-empty, dimensions, title/labels in SVG text |

openpyxl reads formulas as strings and does not compute them; to assert on the *value* of a
formula cell, test the formula text (or compute expected values separately). PDF text extraction
is approximate (spacing, ordering, ligatures) — assert on stable tokens, not whole paragraphs.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Avoid / escalate | Why |
|---|---|---|---|
| Where to test numbers | Selector/aggregation tests against the real DB with hand-computed fixtures | Asserting totals by parsing a PDF | Cheaper, precise failure messages; renderer tests then only check placement |
| Renderer tests | Feed in-memory rows; parse output | Full DB → endpoint → file for every case | Separates calculation bugs from formatting bugs |
| Task tests | Call the task function directly with fake storage | `task_always_eager` everywhere | Eager mode hides serialization, `on_commit`, retry semantics [VERIFY: Celery eager-mode caveats] |
| Clock | `time-machine` / `freezegun` or an injected clock | Real `now()` | Month-end, DST and "last week" logic break on real dates |
| Golden files | Normalised snapshots for structure-heavy outputs (CSV, JSON, XML) | Byte-for-byte PDF snapshots | PDFs embed timestamps, IDs, font subsets |
| Visual regression | Only for customer-facing templated documents (invoices, statements) | Every report | Slow, brittle across font/library versions |
| Large-data tests | Nightly/perf job with production-shaped data | PR-blocking test suite | Minutes of runtime |

## 5. Implementation patterns

### Calculation tests with hand-computed fixtures

```python
import pytest
from datetime import date
from decimal import Decimal

@pytest.mark.django_db
def test_monthly_revenue_excludes_void_and_respects_tenant_timezone(tenant_factory, invoice_factory, user):
    tenant = tenant_factory(timezone="America/New_York")
    other = tenant_factory()
    # 2026-10-31 23:30 local = 2026-11-01 03:30 UTC → belongs to October locally
    invoice_factory(tenant=tenant, status="paid", total=Decimal("100.00"),
                    issued_at=aware("2026-10-31 23:30", tenant.timezone))
    invoice_factory(tenant=tenant, status="void", total=Decimal("999.00"),
                    issued_at=aware("2026-10-15 12:00", tenant.timezone))
    invoice_factory(tenant=other, status="paid", total=Decimal("50.00"),
                    issued_at=aware("2026-10-15 12:00", other.timezone))

    rows = monthly_revenue(tenant=tenant, user=user, year=2026)

    october = next(r for r in rows if r["month"] == date(2026, 10, 1))
    assert october["revenue"] == Decimal("100.00")
    assert october["invoices"] == 1
```

Cases to always include: empty period (zero rows → zeros, not `None`), boundary instants at
`start` and `end` (half-open), DST-transition days, refunds/negative amounts, multiple currencies
if supported, rounding at `.005`, rows with nulls, and another tenant's data that must not appear.
Factories: [django-testing — fixtures-and-test-data](../../django-testing/references/fixtures-and-test-data.md).

### CSV content and formula-injection test

```python
import csv
import io
from decimal import Decimal

def test_orders_csv_escapes_formulas_and_has_contract(client_logged_in, order_factory):
    order_factory(customer__name='=HYPERLINK("http://x")', total=Decimal("12.50"))
    response = client_logged_in.get("/reports/orders.csv")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert 'attachment; filename="orders' in response["Content-Disposition"]
    body = b"".join(response.streaming_content).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["Reference", "Customer", "Total", "Created"]
    assert all(len(r) == 4 for r in rows)
    assert rows[1][1].startswith("'=")          # neutralised
    assert rows[1][2] == "12.50"                 # decimal serialization contract
```

### XLSX content test

```python
import io
from decimal import Decimal
from openpyxl import load_workbook   # [third-party: openpyxl]

def test_payroll_xlsx_types_and_formats(payroll_run_with_two_payslips):
    buf = io.BytesIO()
    build_payroll_xlsx(payroll_rows(payroll_run_with_two_payslips), buf)
    wb = load_workbook(io.BytesIO(buf.getvalue()))
    ws = wb["Payroll"]
    assert ws["A1"].value == "Employee #"
    assert ws.freeze_panes == "A2"
    assert isinstance(ws["E2"].value, (int, float, Decimal))   # numeric, not text
    assert ws["E2"].number_format == "#,##0.00"
    assert ws.max_row == 1 + 2 + 1                             # header + rows + total
    assert ws[f"E{ws.max_row}"].value == "=SUM(E2:E3)"          # formula text, not computed
```

Write-only workbooks can only be read back after saving; read with normal mode in tests (fine for
small fixtures). See [excel-report-generation.md](excel-report-generation.md).

### PDF content test

```python
import io
from pypdf import PdfReader   # [third-party: pypdf]

def test_invoice_pdf_contains_totals_and_metadata(invoice_with_lines):
    pdf = render_invoice_pdf(invoice_with_lines, base_url="file:///app/static/")
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert invoice_with_lines.number in text
    assert "1,250.00" in text
    assert reader.metadata.title == f"Invoice {invoice_with_lines.number}"
```

For multi-page tables, assert the header text appears on every page (repeated headers) and the
last page carries the total. For escaping, put `<b>` or `&` in a customer name and assert it
appears literally.

### DOCX re-open test

```python
import io
from docx import Document   # [third-party: python-docx]

def test_employment_letter_docx(employee):
    buf = io.BytesIO()
    build_employment_letter(employee, buf)
    doc = Document(io.BytesIO(buf.getvalue()))
    texts = [p.text for p in doc.paragraphs]
    assert any(employee.full_name in t for t in texts)
    assert doc.core_properties.title == "Employment letter"
    assert doc.tables[0].cell(0, 0).text == "Department"
```

### Chart tests

Test the **data preparation** function (series, labels, buckets) as plain Python — that is where
bugs live. For the rendered chart: assert PNG signature and dimensions, or parse SVG and check
title/axis label text. Avoid pixel assertions except in visual regression jobs.

### Endpoint, authorisation and tenant matrix

```python
@pytest.mark.django_db
@pytest.mark.parametrize("actor, expected", [
    ("anonymous", 302),        # or 401 for API
    ("same_tenant_other_user", 404),
    ("other_tenant_admin", 404),
    ("requester", 200),
    ("recipient", 200),
])
def test_download_access_matrix(actor, expected, make_actor, succeeded_run, client):
    user = make_actor(actor, run=succeeded_run)
    if user:
        client.force_login(user)
    response = client.get(f"/reports/runs/{succeeded_run.id}/download/")
    assert response.status_code == expected
    if expected == 200:
        assert response["Cache-Control"].startswith("private")
        assert response["X-Content-Type-Options"] == "nosniff"
```

Also test: params with another tenant's `customer` id → validation error; `tenant_id` in the
request body has no effect; expired signed URL → rejected; signed URL for artifact A cannot fetch
artifact B; permission revoked between request and generation → run fails with
`permission_revoked`; expired artifact → 404/410. Patterns:
[django-testing — security-testing](../../django-testing/references/security-testing.md),
[django-testing — views-forms-and-middleware-testing](../../django-testing/references/views-forms-and-middleware-testing.md),
[django-testing — django-rest-framework-testing](../../django-testing/references/django-rest-framework-testing.md).

### Task tests with fake storage

```python
from django.core.files.storage import InMemoryStorage   # [Django] (Django ≥ 4.2)

@pytest.mark.django_db
def test_generate_report_creates_artifact_once(monkeypatch, pending_run):
    storage = InMemoryStorage()
    monkeypatch.setattr("reports.tasks.report_storage", storage)

    generate_report(str(pending_run.id))          # call the function, not .delay()
    generate_report(str(pending_run.id))          # redelivery

    pending_run.refresh_from_db()
    assert pending_run.status == ReportRun.Status.SUCCEEDED
    assert ReportArtifact.objects.filter(run=pending_run).count() == 1
    assert storage.exists(pending_run.artifact.storage_name)
```

Further task cases: claim race (second worker sees status not `PENDING` → no-op); transient
storage error → retry scheduled with backoff, attempt incremented; permanent error (validation,
permission) → `FAILED` without retry; soft time limit → `FAILED` with `timeout` code and temp
file removed; `cancel_requested=True` between chunks → `CANCELLED`, no artifact; reaper moves
stale `RUNNING` (old `heartbeat_at`) back to `PENDING` or `FAILED`; enqueue happens only after
commit (`django_capture_on_commit_callbacks` `[pytest-django]`). Mechanics:
[django-testing — integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md),
[django-testing — database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md).

### Schedule tests across DST

```python
from datetime import time
import time_machine   # [third-party: time-machine]

@pytest.mark.parametrize("now_utc, expected_next_utc", [
    # Daily 07:00 Europe/Berlin across the 2026-10-25 DST end (CEST→CET)
    ("2026-10-24T05:30:00+00:00", "2026-10-25T06:00:00+00:00"),
    ("2026-10-25T06:30:00+00:00", "2026-10-26T06:00:00+00:00"),
])
def test_daily_schedule_next_run_respects_dst(now_utc, expected_next_utc, schedule_factory):
    schedule = schedule_factory(frequency="daily", run_at_local=time(7, 0), timezone="Europe/Berlin")
    with time_machine.travel(now_utc, tick=False):
        assert compute_next_run(schedule).isoformat() == expected_next_utc
```

[VERIFY: the 2026 EU DST end date (last Sunday of October = 2026-10-25) when finalising fixtures.]
Also test: monthly on day 31 in a 30-day month (policy: last day vs skip), a local time that
doesn't exist (spring-forward gap) or occurs twice (fall-back), dispatcher idempotency (running
the dispatcher twice in the same minute creates one run), delivery dedupe by `idempotency_key`.
The `compute_next_run` contract is defined in
[scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md).

### Deterministic fixtures and snapshots

- Fixed seeds for any random/Faker data; fixed clock; explicit time zone on every datetime.
- `ORDER BY` with a unique tiebreaker in every report query, otherwise row order is
  nondeterministic and snapshot tests flake.
- Normalise before comparing snapshots: strip PDF `CreationDate`/`ModDate`/`/ID`, XLSX
  `docProps/core.xml` timestamps, generated-at footers; or make generation deterministic by
  injecting fixed metadata (see the format references).
- Snapshot plugins (`[third-party: syrupy]`) help, but review snapshot diffs like code — an
  auto-updated snapshot of a wrong total is a regression locked in.

### Visual regression

Rasterise PDF pages (`pdftoppm` from Poppler `[external tool]`, or `[third-party: pdf2image]`
wrapping it) at fixed DPI and compare with a tolerance against approved baselines. Pin fonts and
renderer versions in the CI image; otherwise every library upgrade becomes a visual diff. Reserve
for externally visible documents; run outside the PR-blocking suite if slow.
[django-testing — browser-and-end-to-end-testing](../../django-testing/references/browser-and-end-to-end-testing.md)
covers browser-based visual testing for HTML.

### Large-dataset and memory tests

```python
import tracemalloc   # [stdlib]

@pytest.mark.slow
@pytest.mark.django_db
def test_orders_csv_memory_is_bounded(bulk_orders_200k):
    tracemalloc.start()
    for _ in stream_orders_csv(tenant=bulk_orders_200k.tenant):
        pass
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 50 * 1024 * 1024
```

`tracemalloc` sees Python allocations only; WeasyPrint/Pango or Cairo memory needs process RSS
measurement in a perf job. Generate bulk fixtures with `bulk_create` or SQL `generate_series`,
not factories row by row ([django-testing — performance-and-reliability-testing](../../django-testing/references/performance-and-reliability-testing.md)).
Also add query-count assertions on report selectors to catch N+1
(`django_assert_num_queries` `[pytest-django]`).

## 6. Production use cases

| Report | Must-have tests |
|---|---|
| Invoice PDF | Line totals and tax sum to invoice total; currency formatting; escaping of customer text; page break with repeated header; tenant matrix on download |
| Monthly payroll register | Payslip totals reconcile to PayrollRun totals; masked columns per role; only approved runs reportable; permission revoked → fails |
| Weekly sales XLSX | Week boundaries in tenant tz; numeric cell types; formulas text; formula injection in product names |
| Daily ops CSV (streamed) | Header contract; row count = fixture count; BOM policy; consumption of stream |
| Scheduled financial statement | Next-run across DST/month-end; one run per period; one delivery per recipient on retry |
| Bulk ledger export | Memory bound; time budget; cancellation mid-export; artifact checksum stored |

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Parse-and-assert file contents | Catches real defects | More test code per format |
| Hand-computed fixtures | Independent oracle | Effort to build; must cover edge cases deliberately |
| Golden/snapshot files | Broad regression net cheaply | Brittle, can enshrine wrong output |
| Visual regression | Catches layout breaks | Slow, environment-sensitive |
| Direct task invocation | Fast, deterministic | Doesn't exercise broker/serialization — add a few real-worker smoke tests |
| Memory tests | Prevent OOM regressions | Slow; Python-only visibility via tracemalloc |

## 8. Security considerations

- Every report endpoint gets the full actor matrix (anonymous, other user, other tenant, revoked
  role, recipient, admin) — missing/foreign must return identical 404.
- Test with hostile inputs: formula prefixes, `<script>`/`<img src=http://169.254.169.254>` in text
  rendered to PDF (assert no outbound fetch with a fetcher spy), path-traversal filenames, CRLF in
  filenames.
- Assert logs contain no PII (capture logs, scan for fixture names/emails).
- Never use real customer data or production exports as fixtures; synthetic data only.
- Signed URL tests: expiry, tampering, cross-artifact reuse, cross-user reuse for bound tokens.

## 9. Performance considerations

- Keep format-parsing tests on small fixtures; put large-data tests behind a marker run nightly.
- Use `InMemoryStorage` and skip chart rendering in unit tests where only data is asserted.
- WeasyPrint rendering costs hundreds of ms per document — share rendered output across asserts
  via fixtures with appropriate scope.
- Bulk fixtures via `bulk_create`/SQL; reuse DB between runs (`--reuse-db` `[pytest-django]`).
- Track suite time of report tests; slow ones are a defect ([django-testing — performance-and-reliability-testing](../../django-testing/references/performance-and-reliability-testing.md)).

## 10. Common mistakes and anti-patterns

- `assert response.status_code == 200` as the only assertion of an export test.
- Not consuming `streaming_content`, so the generator never runs.
- Expected totals computed by calling the same selector or a copy of its logic.
- Fixtures created "now" with real clock; tests failing at month-end or on DST days.
- Using SQLite in tests for reports that rely on PostgreSQL date truncation, time zones or window
  functions — results differ.
- Byte-for-byte PDF/XLSX comparisons.
- `task_always_eager=True` globally and believing retries/`on_commit` are tested.
- Testing authorisation only for the happy-path user.
- No test for the empty report.

## 11. Troubleshooting and validation

| Symptom in tests | Cause | Fix |
|---|---|---|
| Totals off by one day of data | Fixture datetimes naive or in UTC instead of tenant tz | Build fixtures with aware local times |
| Snapshot test fails every run | Timestamps/IDs in output or unstable ordering | Normalise metadata; add unique tiebreaker ordering |
| XLSX test sees `None` for formula cells | Reading with `data_only=True` on a file never opened in Excel | Assert formula text |
| PDF text assertions flaky across machines | Font fallback differences, extraction spacing | Pin fonts in CI image; assert tokens |
| Task test passes, production duplicates | Test never simulated redelivery | Call task twice; test claim |
| Schedule test passes locally, fails in CI | Local TZ leaking; system tz differs | Set `TIME_ZONE`, use aware datetimes, freeze clock |
| Memory test unstable | GC timing, shared fixtures | Measure peak over a larger run; compare to a generous budget |

Validation checklist:

- [ ] Calculations tested against hand-computed fixtures incl. empty, boundary, DST, rounding, nulls, refunds.
- [ ] Each format has a parse-and-assert test (structure, types, escaping, metadata).
- [ ] Endpoint tests consume streams and assert `Content-Type`, `Content-Disposition`, cache headers.
- [ ] Actor/tenant matrix on request, status and download endpoints; foreign → 404.
- [ ] Task tests: success, redelivery, claim race, transient retry, permanent failure, timeout, cancellation, stale reaper, enqueue on commit.
- [ ] Schedule tests across DST and month-end; dispatcher and delivery idempotency.
- [ ] Deterministic ordering and fixed clocks; snapshots normalised.
- [ ] Nightly large-data and memory tests with budgets; query-count assertions on selectors.
- [ ] Third-party test packages are installed and labelled; `[VERIFY: ...]` items resolved.

## 12. Related references and official documentation

- In this skill: [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) ·
  [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) ·
  [excel-report-generation.md](excel-report-generation.md) ·
  [pdf-report-generation.md](pdf-report-generation.md) ·
  [docx-and-document-generation.md](docx-and-document-generation.md) ·
  [charts-and-data-visualization.md](charts-and-data-visualization.md) ·
  [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) ·
  [background-report-generation.md](background-report-generation.md) ·
  [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) ·
  [reporting-security-and-compliance.md](reporting-security-and-compliance.md) ·
  [report-generation-performance.md](report-generation-performance.md)
- django-testing: [testing-fundamentals](../../django-testing/references/testing-fundamentals.md) ·
  [pytest-and-pytest-django](../../django-testing/references/pytest-and-pytest-django.md) ·
  [fixtures-and-test-data](../../django-testing/references/fixtures-and-test-data.md) ·
  [mocking-and-dependency-isolation](../../django-testing/references/mocking-and-dependency-isolation.md) ·
  [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md) ·
  [security-testing](../../django-testing/references/security-testing.md) ·
  [database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md) ·
  [performance-and-reliability-testing](../../django-testing/references/performance-and-reliability-testing.md) ·
  [test-maintenance-and-troubleshooting](../../django-testing/references/test-maintenance-and-troubleshooting.md)
- Other skills: [django-data-processing — data-processing-testing](../../django-data-processing/references/data-processing-testing.md) ·
  [django-security-engineering — security-testing-and-incident-response](../../django-security-engineering/references/security-testing-and-incident-response.md)
- Official documentation:
  <https://docs.djangoproject.com/en/5.2/topics/testing/> ·
  <https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects> ·
  <https://docs.djangoproject.com/en/5.2/ref/files/storage/#the-inmemorystorage-class> ·
  <https://pytest-django.readthedocs.io/> ·
  <https://docs.python.org/3/library/csv.html> ·
  <https://docs.python.org/3/library/tracemalloc.html> ·
  <https://openpyxl.readthedocs.io/> ·
  <https://pypdf.readthedocs.io/> ·
  <https://python-docx.readthedocs.io/> ·
  <https://docs.celeryq.dev/en/stable/userguide/testing.html>
