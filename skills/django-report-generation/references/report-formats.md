# Report formats

> **Superseded (legacy, retained for reference).** Do not load for new work. Its content is now
> owned by [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md),
> [excel-report-generation.md](excel-report-generation.md),
> [pdf-report-generation.md](pdf-report-generation.md) and
> [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).

## Formula-injection safety (CSV and Excel)

```python
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

def safe_cell(value):
    """Prefix user-controlled strings that a spreadsheet would treat as a formula."""
    if isinstance(value, str) and value.startswith(_DANGEROUS):
        return "'" + value
    return value
```

Apply to every string that originates from users or external systems. Numbers and dates are
written as typed values, not strings.

## CSV

- Small: build with `csv.writer(response)` on an `HttpResponse(content_type="text/csv")`.
- Large: `StreamingHttpResponse` with a generator (see SKILL.md) — the proxy must not buffer
  (`X-Accel-Buffering: no` for nginx) and the worker timeout must allow the stream.
- Excel-friendly UTF-8: write a BOM (`"﻿"`) first if users open files directly in Excel.
- Dates as ISO 8601; decimals with `.`; document the column contract.

## Excel with openpyxl

```python
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

def build_orders_xlsx(rows, fileobj) -> None:
    wb = Workbook(write_only=True)                # streams rows; constant memory
    ws = wb.create_sheet("Orders")
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 18
    header = []
    for title in ("Reference", "Customer", "Total", "Created"):
        cell = WriteOnlyCell(ws, value=title)
        cell.font = Font(bold=True)
        header.append(cell)
    ws.append(header)
    for ref, customer, total, created in rows:
        total_cell = WriteOnlyCell(ws, value=total)
        total_cell.number_format = "#,##0.00"
        local = timezone.localtime(created, REPORT_TZ).replace(tzinfo=None)   # Excel has no tz
        ws.append([safe_cell(ref), safe_cell(customer), total_cell, local])
    wb.save(fileobj)
```

Notes:

- Excel has no time zones: convert to the report's time zone, then drop `tzinfo`.
- Write `Decimal` values directly; set `number_format` for currency/percentages.
- Write-only mode: set column widths and freeze panes **before** appending rows; no reading back.
- Limit: 1,048,576 rows per sheet — split or switch to CSV beyond that.
- For heavy formatting/charts on moderate data, `XlsxWriter` is an alternative — only if the
  project already uses it.

Serving:

```python
buffer = io.BytesIO()
build_orders_xlsx(rows, buffer)
return HttpResponse(buffer.getvalue(),
    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    headers={"Content-Disposition": 'attachment; filename="orders.xlsx"'})
```

For large workbooks generate in a task and store the file instead (see
[large-datasets.md](large-datasets.md)).

## PDF from HTML (WeasyPrint)

```python
from django.template.loader import render_to_string
from weasyprint import HTML

def render_invoice_pdf(invoice, *, base_url: str) -> bytes:
    html = render_to_string("billing/invoice_pdf.html", {"invoice": invoice})
    return HTML(string=html, base_url=base_url, url_fetcher=local_only_fetcher).write_pdf()
```

```css
/* invoice_pdf.css */
@page { size: A4; margin: 18mm 15mm; @bottom-right { content: "Page " counter(page) " of " counter(pages); } }
table { width: 100%; border-collapse: collapse; }
thead { display: table-header-group; }      /* repeat header on each page */
tr { page-break-inside: avoid; }
```

```python
from weasyprint import default_url_fetcher

def local_only_fetcher(url: str, *args, **kwargs):
    if not url.startswith(("file://", settings.STATIC_URL)):
        raise ValueError(f"Blocked resource: {url}")           # no network fetches (SSRF)
    return default_url_fetcher(url, *args, **kwargs)
```

- WeasyPrint needs Pango system libraries in the image — document it in the Dockerfile.
- Embed fonts locally for consistent output; avoid web fonts from CDNs.
- Rendering is CPU-heavy: multi-page PDFs belong in tasks.

## PDF programmatically (ReportLab)

Use for documents built from drawing primitives (labels, certificates, charts without HTML):

```python
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

def build_statement(fileobj, rows):
    doc = SimpleDocTemplate(fileobj, pagesize=A4)
    styles = getSampleStyleSheet()
    doc.build([Paragraph("Statement", styles["Title"]), Table([["Date", "Amount"], *rows], repeatRows=1)])
```

ReportLab `Paragraph` parses mini-markup — escape user strings with `xml.sax.saxutils.escape`.

## File naming

`<report>-<scope>-<period>-<generated-at>.<ext>`, e.g. `revenue-acme-2026-10-20261103T0912Z.xlsx`.
Sanitise any user-provided part (`django.utils.text.get_valid_filename`).

## Official documentation

- https://docs.djangoproject.com/en/stable/howto/outputting-csv/
- https://docs.djangoproject.com/en/stable/howto/outputting-pdf/
- https://docs.djangoproject.com/en/stable/ref/request-response/#streaminghttpresponse-objects
- https://openpyxl.readthedocs.io/en/stable/optimized.html
- https://doc.courtbouillon.org/weasyprint/stable/
- https://docs.reportlab.com/
- https://owasp.org/www-community/attacks/CSV_Injection
