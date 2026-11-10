# Excel and Spreadsheet Report Generation

> **Scope:** Producing XLSX report files from Django with openpyxl or XlsxWriter — library selection, workbook and worksheet design, cell/number/date formatting, formulas and recalculation, native charts, conditional formatting, data validation, freeze panes, filters, merged cells, large-workbook generation, Excel limits and workbook validation. **Not here:** the `safe_cell()` helper and CSV/JSON/XML output ([csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)), chart data preparation and image charts ([charts-and-data-visualization.md](charts-and-data-visualization.md)), running generation in a task ([background-report-generation.md](background-report-generation.md)), serving the file ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)), reading/importing spreadsheets ([django-data-processing — file-processing-engineering](../../django-data-processing/references/file-processing-engineering.md)).

## 1. Purpose and scope

XLSX is the format business users ask for when they want to *work with* report data: filter, sort,
pivot, add their own formulas, paste into a model. That makes it different from PDF (a fixed,
presentation-oriented artefact) and CSV (a plain data interchange format). An XLSX report must
therefore get three things right at once:

1. **Typed values** — numbers as numbers, dates as dates, money with a currency number format —
   so the user's own formulas work.
2. **Safe content** — user-controlled strings must never become live formulas.
3. **Bounded resources** — a 300,000-row workbook must not exhaust a worker's memory.

Neither openpyxl nor XlsxWriter is part of Django or the Python standard library; both are
`[third-party]` packages. Use them only if the project already depends on one, or after an explicit
dependency decision. Django contributes only the response/storage plumbing.

## 2. Core concepts

| Concept | Meaning for reports |
|---|---|
| Workbook | The `.xlsx` file: a ZIP archive of XML parts (Office Open XML, ECMA-376). |
| Worksheet | One grid. Reports typically use a *Summary* sheet, one or more *Data* sheets and a *Parameters/About* sheet. |
| Cell value vs number format | The stored value (e.g. `Decimal("1234.5")`) is separate from the display format (`#,##0.00`). Formatting never changes the value. |
| Shared strings / inline strings | XLSX stores strings in a shared table or inline; libraries decide this — relevant only to file size and memory. |
| Formula | Stored as text (`=SUM(D2:D100)`) plus an optional *cached result*. The cached result is what non-recalculating viewers display. |
| Write-only / constant-memory mode | Library modes that flush rows to disk as they are written, trading random access for bounded memory. |
| Native chart | A chart object stored in the workbook, bound to cell ranges; Excel re-renders it. Distinct from a PNG image inserted on a sheet. |

### Library capabilities (they are not interchangeable)

| Capability | openpyxl `[third-party: openpyxl]` | XlsxWriter `[third-party: XlsxWriter]` |
|---|---|---|
| Create new workbook | Yes | Yes |
| Read existing workbook | Yes (`load_workbook`, `read_only=True` mode) | **No** |
| Modify an existing file / fill a template workbook | Yes (normal mode only) | **No** — writes new files only |
| Low-memory writing | `Workbook(write_only=True)` + `WriteOnlyCell` | `{"constant_memory": True}` — rows must be written in order |
| Random-access writes in low-memory mode | No (append rows only) | No (row-by-row; earlier rows flushed) |
| Native charts | Yes (`openpyxl.chart`) | Yes (`workbook.add_chart`), broader chart formatting options |
| Conditional formatting | Yes | Yes |
| Data validation | Yes | Yes |
| Rich strings (mixed fonts in a cell) | Limited `[VERIFY: openpyxl CellRichText support and version, openpyxl ≥ 3.1]` | Yes (`write_rich_string`) |
| Formula evaluation | **No** (never computes results) | **No** (stores formulas; optional supplied result value) |
| Interprets strings starting `=` as formulas on generic write | openpyxl treats a string value beginning with `=` as a formula `[VERIFY: openpyxl cell data-type inference for "=" strings]` | `write()` does by default (`strings_to_formulas=True`) |
| Timezone-aware datetimes | Rejected — strip tz first `[VERIFY: exact openpyxl behaviour/exception]` | `remove_timezone` workbook option `[VERIFY: default False]` |
| Output to in-memory buffer | `wb.save(fileobj)` | `Workbook(buffer, {"in_memory": True})` or file path |

Rules of thumb: **template-filling or read-back → openpyxl; heavily formatted or very large
write-once reports → XlsxWriter; whichever the project already has → that one.**

## 3. Internal mechanics

### How the libraries build the file

- **openpyxl normal mode** builds an in-memory object model: every cell is a Python object. Memory
  grows roughly linearly with populated cells and is large per cell (often cited as tens of times
  the file size `[VERIFY: openpyxl performance docs memory estimate]`). Fine for summaries and
  thousands of rows; dangerous for hundreds of thousands.
- **openpyxl write-only mode** (`Workbook(write_only=True)`) streams each appended row to a
  temporary file; the sheet cannot be read back or edited, cells cannot be addressed by coordinate,
  and sheet-level settings (column widths, freeze panes, auto filter) must be set **before** rows
  are appended `[VERIFY: openpyxl optimized.html — ordering of freeze_panes/column_dimensions in write-only]`.
- **XlsxWriter** always writes new files. In default mode it keeps worksheet data in memory until
  `close()`; with `constant_memory` each row is flushed when a later row is started, so writing a
  cell into an already-flushed row is silently ignored `[VERIFY: XlsxWriter working_with_memory docs]`.
  Strings in constant-memory mode are written inline rather than via the shared-strings table, which
  enlarges the file slightly.

### Dates and time zones

Excel stores dates as serial numbers (days since an epoch) with **no time-zone concept**. A report
must decide the presentation zone (normally `tenant.timezone`), convert aware UTC datetimes to it,
then drop `tzinfo`. Record the zone in a header row or the *About* sheet so readers know what
"09:00" means.

```python
from zoneinfo import ZoneInfo  # [stdlib]

def excel_datetime(value, tz_name: str):
    """Aware UTC datetime -> naive local datetime for Excel."""
    if value is None:
        return None
    return value.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
```

### Formulas and recalculation

Neither library calculates formulas. The XML stores the formula and a cached result:

- XlsxWriter writes a cached result of `0` unless you pass `value=` to `write_formula()`; Excel
  recalculates on open, but **file previewers, mobile viewers, some web viewers, and any program
  reading the file with openpyxl `data_only=True` see the cached value** `[VERIFY: XlsxWriter "Formula Results" docs]`.
- openpyxl writes no cached value; reading it back with `data_only=True` returns `None` until the
  file has been opened and saved by Excel.

Consequence: **write computed values for anything that matters** (totals, tax, net pay) and use
formulas only as a convenience for the user's subsequent edits — or write both (value in the
report, formula in an explicitly labelled "check" column).

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Choose by |
|---|---|---|---|
| Library | openpyxl | XlsxWriter | Read/modify or template workbook → openpyxl. Write-once, heavy formatting, rich strings, large outputs → XlsxWriter. Existing dependency wins ties. |
| Totals | Computed in DB, written as values | Excel formulas | Values for reproducibility and non-Excel viewers; formulas when users will edit inputs. Never let a formula be the only source of an audited figure. |
| Big data volume | One XLSX with many sheets | CSV (or zipped CSVs) | Above roughly 200–500k rows XLSX generation is slow and the file is unwieldy; offer CSV. Hard limit 1,048,576 rows/sheet. |
| Formatting | Rich per-cell styles | Column-level formats, few named styles | Each distinct style combination costs file size and (openpyxl) memory; formats per column scale. |
| Charts | Native Excel chart | Embedded PNG | Native when the user will filter/extend data; PNG only for fixed, branded visuals ([charts-and-data-visualization.md](charts-and-data-visualization.md)). |
| Layout | Merged-cell "pretty" layout | Flat table + separate summary sheet | Flat tables for data sheets (merged cells break sort/filter/copy); merges only on a cover/summary sheet. |
| Template | Designer-built `.xlsx` filled by openpyxl | Fully code-generated | Template when non-engineers own the look; code when the structure is data-driven. Templates require openpyxl normal mode (memory-bound). |

## 5. Implementation patterns

### 5.1 Data sheet with openpyxl write-only mode

```python
from openpyxl import Workbook                  # [third-party: openpyxl]
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

from reports.formatting import safe_cell

MONEY = '#,##0.00'
DATE = 'yyyy-mm-dd'

def write_invoice_sheet(wb: Workbook, rows, *, tz_name: str) -> None:
    ws = wb.create_sheet("Invoices")
    ws.freeze_panes = "A2"                      # set before appending rows
    for col, width in zip("ABCDE", (14, 32, 12, 12, 14)):
        ws.column_dimensions[col].width = width

    header = []
    for title in ("Number", "Customer", "Issued", "Status", "Total"):
        c = WriteOnlyCell(ws, value=title)
        c.font = Font(bold=True)
        header.append(c)
    ws.append(header)

    for number, customer, issued_at, status, total in rows:   # rows from .iterator()
        issued = WriteOnlyCell(ws, value=excel_datetime(issued_at, tz_name))
        issued.number_format = DATE
        amount = WriteOnlyCell(ws, value=total)                 # Decimal stays numeric
        amount.number_format = MONEY
        ws.append([safe_cell(number), safe_cell(customer), issued, status, amount])
```

`rows` is a `values_list(...).iterator(chunk_size=2000)` from the report selector — see
[reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md).
Save to a temporary file or `SpooledTemporaryFile` `[stdlib]`, not a growing `BytesIO`, for large
outputs.

`Decimal` values are written as numbers; Excel holds IEEE-754 doubles, so precision beyond ~15
significant digits is lost in the file even though your calculation was exact. Report figures
should already be quantized to the currency's minor unit before writing.

### 5.2 Formatted summary with XlsxWriter

```python
import xlsxwriter                               # [third-party: XlsxWriter]

def build_sales_summary(path: str, regions, *, currency_format: str) -> None:
    wb = xlsxwriter.Workbook(path, {
        "strings_to_formulas": False,   # user text never becomes a formula
        "strings_to_urls": False,       # avoid auto-hyperlinks (and per-sheet URL limits)
        "remove_timezone": False,       # we convert explicitly; aware datetimes should fail loudly
    })
    bold = wb.add_format({"bold": True, "bottom": 1})
    money = wb.add_format({"num_format": currency_format})   # e.g. '[$€-x-euro2] #,##0.00'
    ws = wb.add_worksheet("By region")
    ws.write_row(0, 0, ["Region", "Orders", "Revenue"], bold)
    for r, row in enumerate(regions, start=1):
        ws.write_string(r, 0, row["region"])
        ws.write_number(r, 1, row["orders"])
        ws.write_number(r, 2, float(row["revenue"]), money)   # see note below
    last = len(regions)
    ws.write_string(last + 1, 0, "Total", bold)
    ws.write_formula(last + 1, 2, f"=SUM(C2:C{last + 1})", money,
                     value=float(sum(r["revenue"] for r in regions)))  # cached result
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, last, 2)
    wb.close()
```

Notes:

- `write_string`/`write_number` are explicit; generic `write()` type-sniffs and, by default,
  turns `"=..."` strings into formulas. Disabling `strings_to_formulas` is defence in depth; keep
  `safe_cell()` too, because a user who receives the file and re-saves it as CSV re-exposes the text.
- XlsxWriter's `write_number` expects a numeric type; converting `Decimal` → `float` is a
  presentation step after exact computation `[VERIFY: whether XlsxWriter accepts Decimal directly]`.
- Supplying `value=` keeps non-recalculating viewers correct.

### 5.3 Number, date and currency formats

| Data | Format string | Note |
|---|---|---|
| Money (generic) | `#,##0.00` | Separators are rendered by the viewer's locale; the format code always uses `,` and `.`. |
| Money with symbol | `"€"#,##0.00` or locale-tagged `[$€-x-euro2] #,##0.00` | One currency per column; multi-currency reports need a currency column, not mixed symbols. |
| Negative in red/parentheses | `#,##0.00;[Red](#,##0.00)` | Common in financial statements. |
| Percentage | `0.0%` | Store 0.125, not 12.5. |
| Date | `yyyy-mm-dd` | Unambiguous across locales. |
| Datetime | `yyyy-mm-dd hh:mm` | Plus a header stating the zone. |
| Integer IDs / codes | Text (`@`) or written as string | Prevents loss of leading zeros (`00123`) and scientific notation for long numbers (> 15 digits, e.g. IBANs, card tokens). |

### 5.4 Conditional formatting, data validation, filters

```python
# XlsxWriter: highlight overdue invoices and restrict a "Reviewed" column to Yes/No
ws.conditional_format(1, 5, last, 5, {
    "type": "cell", "criteria": ">", "value": 30,
    "format": wb.add_format({"bg_color": "#FFC7CE"}),
})
ws.data_validation(1, 6, last, 6, {"validate": "list", "source": ["Yes", "No"]})
ws.autofilter(0, 0, last, 6)
```

openpyxl equivalents live in `openpyxl.formatting.rule` (e.g. `CellIsRule`) and
`openpyxl.worksheet.datavalidation.DataValidation`; `ws.auto_filter.ref = "A1:G500"` sets the
filter range (not available in write-only mode in the same way `[VERIFY: openpyxl write-only
auto_filter support]`). Conditional formats should never be the only carrier of meaning — add a
status column too (accessibility, and the formatting is lost when exported to CSV).

### 5.5 Native charts

```python
# XlsxWriter: chart bound to the summary table written in 5.2
chart = wb.add_chart({"type": "column"})
chart.add_series({
    "name": "Revenue",
    "categories": ["By region", 1, 0, last, 0],
    "values": ["By region", 1, 2, last, 2],
})
chart.set_title({"name": "Revenue by region"})
ws.insert_chart("E2", chart)
```

openpyxl: `from openpyxl.chart import BarChart, Reference`; build `Reference(ws, min_col=..., min_row=..., max_row=...)`, `chart.add_data(ref, titles_from_data=True)`, `chart.set_categories(...)`, `ws.add_chart(chart, "E2")`. Chart ranges must point to cells that exist; charts over 100k-row ranges are slow to open — chart the summary sheet, not the data sheet.

### 5.6 Workbook structure for auditable reports

1. **Summary** — headline figures, written as values.
2. **Data** — flat table, one header row, frozen, auto-filtered, no merged cells.
3. **About** — report key and version, parameters, tenant, `data_as_of`, generated-at, time zone,
   currency, rounding rule, run ID. This is what makes a spreadsheet reproducible when it is
   emailed around out of context.

Also set document properties (openpyxl `wb.properties.title/creator`; XlsxWriter
`wb.set_properties({...})`) — but never put PII or internal hostnames there.

## 6. Production use cases

| Use case | Library and mode | Notes |
|---|---|---|
| Monthly payroll register (5k employees) | XlsxWriter default mode | Payslip totals as values; `Department` subtotals as values; a "check" column with formulas; About sheet with `PayrollRun` period. |
| Accounts receivable ageing | openpyxl normal mode filling a finance-designed template | Template owned by finance; code fills named ranges; generated in a task. |
| Order export (400k rows) | XlsxWriter `constant_memory` or openpyxl write-only; offer CSV alongside | Single flat sheet; no conditional formatting over the full range. |
| Weekly sales pack | XlsxWriter | Summary sheet + native charts + region sheets. |
| Inventory valuation | Either | Item codes written as text; quantities as numbers; valuation total as value. |

## 7. Advantages and disadvantages

| | Advantages | Disadvantages |
|---|---|---|
| XLSX format | Typed values; users can analyse; multiple sheets; native charts | Row limit; heavy to generate; formula-injection surface; viewer differences (recalculation, fonts) |
| openpyxl | Read and write; template filling; write-only mode | High memory in normal mode; slower writes; fewer formatting features |
| XlsxWriter | Fast; rich formatting and charts; constant-memory mode; extensive docs | Cannot read/modify; constant-memory forbids out-of-order writes; generic `write()` type sniffing surprises |

## 8. Security considerations

- **Formula injection.** Any user- or external-system-controlled string (customer name, order
  reference, notes) beginning with `=`, `+`, `-`, `@`, TAB or CR can execute as a formula (DDE,
  `HYPERLINK` exfiltration). Pass every such string through `safe_cell()` (owned by
  [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)) **and** use
  type-explicit writes. Never build formula strings by interpolating user text.
- **Hyperlinks.** Do not auto-create links from data (`strings_to_urls=False`); if links are
  required, allow-list schemes (`https`) and hosts.
- **Hidden data.** Hidden sheets/columns and document properties are not access control — anything
  in the file is readable. Generate per-audience files instead of hiding columns.
- **Macros.** Never generate `.xlsm` for reports; recipients' security policies rightly block them.
- **Template files** are code-adjacent: store them in the repository or a controlled storage
  location, not user-uploaded.
- Authorisation, tenant scoping and artifact storage are owned by
  [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Normal-mode openpyxl memory is the usual cause of OOM-killed workers on "Excel export"; switch to
  write-only or XlsxWriter `constant_memory` once outputs exceed tens of thousands of rows.
- **XLSX cannot be truly streamed to the HTTP client**: the ZIP container's central directory is
  written at the end, and both libraries finalise on save/close. Write to a temp file in a task, then
  serve or upload the finished file. See the streaming matrix in
  [report-generation-performance.md](report-generation-performance.md).
- Reuse format objects (XlsxWriter `add_format` once per style; openpyxl `NamedStyle`) — creating
  one per cell bloats files and can hit Excel's cell-style limit (~64,000 unique formats `[VERIFY: Excel specifications and limits]`).
- Avoid per-cell autofit loops over large sheets; set widths from header length or sample rows.
  (XlsxWriter offers `worksheet.autofit()` which simulates widths `[VERIFY: added in XlsxWriter 3.0.2]`;
  it is not exact and costs time on big sheets.)
- Conditional formats and data validation over whole columns are cheap to write but slow for Excel
  to open on large sheets; scope them to the populated range.
- Fetch rows with `values_list().iterator(chunk_size=...)` — ORM instance creation often costs more
  than the XLSX writing.

## 10. Common mistakes and anti-patterns

- `ws.append(row)` in openpyxl normal mode for 500k rows inside a web request.
- Writing money as strings (`"1,234.50"`) — breaks sums and sorting; or as `float` *before*
  calculating totals.
- Writing aware datetimes, or naive UTC without telling the reader.
- Formulas as the only source of totals — previewers show `0`, auditors can't reproduce.
- Using XlsxWriter `write()` with untrusted strings while `strings_to_formulas` is on.
- Merged header cells over data columns — sorting and filtering fail or misbehave.
- Product codes and IDs written as numbers — leading zeros lost, long IDs shown in scientific
  notation and truncated to 15 digits.
- Trying to "open and append" to an existing workbook with XlsxWriter (impossible) or loading a
  huge template in openpyxl normal mode for every run.
- Exceeding 1,048,576 rows silently by truncating — fail or split explicitly.
- Building a new format object for every cell.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Excel says "We found a problem with some content… repair?" | Invalid XML: control characters in strings, writes after close, invalid ranges in charts/validation, duplicate sheet names, sheet name > 31 chars or containing `[]:*?/\` | Strip control chars (except TAB/LF/CR) from text; validate sheet names; check chart ranges. |
| Totals show 0 in preview/mobile | Formula without cached result | Write values or supply `value=` (XlsxWriter). |
| Dates show as numbers like 45567 | Missing date number format | Set `number_format`/`num_format` on date cells. |
| `TypeError`/`ValueError` about tzinfo | Aware datetime | Convert to report zone and strip tz. |
| Leading zeros lost | Codes written as numbers | Write as strings. |
| Worker OOM | openpyxl normal mode / format per cell | Write-only / constant memory; shared formats. |
| Rows missing in constant-memory output | Out-of-order writes | Write strictly row by row. |

### Validating the generated workbook (tests and post-generation checks)

```python
from io import BytesIO

import pytest                        # [third-party: pytest]
from openpyxl import load_workbook   # [third-party: openpyxl] — also reads XlsxWriter output

def assert_invoice_workbook(content: bytes, *, expected_rows: int, expected_total) -> None:
    wb = load_workbook(BytesIO(content), read_only=True)
    assert wb.sheetnames[:3] == ["Summary", "Invoices", "About"]
    ws = wb["Invoices"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == expected_rows
    assert all(not (isinstance(v, str) and v[:1] in "=+-@") for r in rows for v in r)
    assert sum(r[4] for r in rows) == pytest.approx(float(expected_total))
```

Validate: sheet names, header row, row count, typed cell values (numbers are numbers), no
unescaped formula-leading strings, number formats on money/date columns, About-sheet metadata, and
that the file opens without error. Full test strategy:
[report-generation-testing.md](report-generation-testing.md). Opening in real Excel/LibreOffice
remains a manual or visual-regression step `[external tool]`.

## 12. Related references and official documentation

- [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) — `safe_cell()`, CSV alternative for large exports
- [charts-and-data-visualization.md](charts-and-data-visualization.md) — native vs image charts
- [report-templates-and-reusability.md](report-templates-and-reusability.md) — shared renderer interface and branding
- [report-generation-performance.md](report-generation-performance.md) — buffering vs streaming, memory budgets
- [background-report-generation.md](background-report-generation.md) — generating large workbooks in tasks
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — Decimal, time zones, iteration
- [django-data-processing — file-processing-engineering](../../django-data-processing/references/file-processing-engineering.md) — reading spreadsheets
- [django-performance — memory-and-resource-management](../../django-performance/references/memory-and-resource-management.md)
- [django-security-engineering — injection-prevention](../../django-security-engineering/references/injection-prevention.md)
- <https://openpyxl.readthedocs.io/en/stable/>
- <https://openpyxl.readthedocs.io/en/stable/optimized.html>
- <https://xlsxwriter.readthedocs.io/>
- <https://xlsxwriter.readthedocs.io/working_with_memory.html>
- <https://xlsxwriter.readthedocs.io/working_with_formulas.html>
- <https://support.microsoft.com/en-us/office/excel-specifications-and-limits-1672b34d-7043-467e-8e27-269d656771c3>
- <https://owasp.org/www-community/attacks/CSV_Injection>
