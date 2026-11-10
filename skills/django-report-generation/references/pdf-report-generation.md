# PDF Report Generation

> **Scope:** Choosing and using a PDF engine for Django reports — ReportLab, WeasyPrint, xhtml2pdf
> and headless Chromium — including layout, paged media, headers/footers, page numbering, tables,
> fonts, images, metadata, encryption, PDF/A and accessibility limits, rendering cost and output
> validation. **Not here:** SSRF threat analysis ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)),
> chart creation ([charts-and-data-visualization.md](charts-and-data-visualization.md)), the shared
> renderer abstraction and template versioning ([report-templates-and-reusability.md](report-templates-and-reusability.md)),
> task orchestration ([background-report-generation.md](background-report-generation.md)), download
> responses ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)).

## 1. Purpose and scope

PDF is the format for documents whose **layout is part of the contract**: invoices, payslips,
statements, signed-off regulatory submissions, anything printed or archived. It is the wrong format
for data people will re-analyse (use XLSX/CSV — see [excel-report-generation.md](excel-report-generation.md),
[csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)).

Django has **no PDF engine** `[Django]`. The official "Outputting PDFs" how-to only shows wiring a
third-party library (ReportLab) into an `HttpResponse`/`FileResponse`. Every engine below is
third-party or an external service; each brings system dependencies, a CPU cost and a distinct set
of layout capabilities.

This reference answers: which engine, how to build a correct multi-page document with it, how to
keep it safe and bounded, and how to prove the output is right.

## 2. Core concepts

| Concept | Meaning for reports |
|---|---|
| Programmatic PDF | Python code places flowables/drawing primitives (ReportLab). Layout logic lives in code. |
| HTML-to-PDF | A Django template renders HTML + print CSS; an engine lays it out (WeasyPrint, xhtml2pdf, Chromium). |
| CSS Paged Media | CSS spec for pages: `@page` size/margins, margin boxes (`@top-center`, `@bottom-right`), `counter(page)`, `counter(pages)`, `page-break-*`/`break-*`. Engine support varies. |
| Flowable | ReportLab platypus unit (Paragraph, Table, Image, Spacer, PageBreak) that the frame splits across pages. |
| Repeated table header | Header row re-drawn on every page a table spans (`repeatRows` in ReportLab; `thead` as `table-header-group` in CSS). |
| Page X of Y | Total page count is only known after layout — requires a two-pass or deferred-render approach. |
| Font embedding | The PDF must contain the glyphs used; standard 14 PDF fonts cover Latin-1-ish text only. |
| Document metadata | Info dictionary / XMP: title, author, subject, keywords, creation date. |
| PDF/A | ISO archival profile (embedded fonts, no encryption, colour profiles, no external content). |
| PDF/UA / tagged PDF | Accessibility profile: structure tree, reading order, alt text, language. |
| Encryption & permissions | User/owner passwords and permission flags (print, copy). Flags are advisory to compliant viewers. |

## 3. Internal mechanics

### 3.1 Engine pipelines

```text
ReportLab:   data -> flowables (Paragraph/Table/Image) -> DocTemplate frames -> page callbacks -> PDF bytes
WeasyPrint:  data -> Django template -> HTML string -> parse HTML+CSS -> fetch resources (url_fetcher)
             -> layout (boxes, pagination) -> Pango text shaping -> PDF bytes
xhtml2pdf:   data -> Django template -> HTML -> CSS 2.1 subset -> ReportLab flowables -> PDF bytes
Chromium:    data -> HTML(+JS) -> headless browser (Playwright / external service) -> print-to-PDF
```

### 3.2 Library comparison

| Aspect | ReportLab `[third-party: reportlab]` | WeasyPrint `[third-party: weasyprint]` | xhtml2pdf `[third-party: xhtml2pdf]` | Headless Chromium `[external tool]` / `[external service]` |
|---|---|---|---|---|
| Authoring model | Python API (platypus, canvas) | HTML + CSS templates | HTML + limited CSS | HTML + CSS + JavaScript |
| CSS support | n/a | Strong CSS Paged Media; flex/grid partial [VERIFY: grid/flex support level in the installed WeasyPrint version's changelog] | CSS 2.1 subset, no flex/grid | Full browser CSS; paged-media margin boxes limited [VERIFY: Chromium margin-box support] |
| JavaScript | n/a | None | None | Yes (client-side chart libs work) |
| Page X of Y | Two-pass canvas subclass | `counter(page)` / `counter(pages)` in margin boxes | `<pdf:pagenumber>` / `<pdf:pagecount>` tags [VERIFY: xhtml2pdf tag names] | Header/footer templates with `pageNumber`/`totalPages` classes |
| System deps | Pure Python + C accel (wheels) | Pango, HarfBuzz, fontconfig (system libraries) | ReportLab and its deps | A Chromium binary or a separate rendering service |
| Unicode / complex scripts | TTF registration; no complex shaping (Arabic/Indic) in open-source [VERIFY: shaping support in current ReportLab] | Pango/HarfBuzz shaping, RTL | Weak | Full |
| Speed | Fastest for tabular data | Moderate; slow for very long tables | Moderate | Slow startup; heavy memory per page/browser |
| Encryption | Built in (`reportlab.lib.pdfencrypt`) [VERIFY: parameter names] | Not native — post-process | Via ReportLab [VERIFY: xhtml2pdf encryption option name] | Not native — post-process |
| Tagged PDF / PDF/UA | Not produced by open-source [VERIFY: ReportLab open-source tagged-PDF support] | `pdf_variant="pdf/ua-1"` [VERIFY: version added and completeness] | No | Chromium can emit tagged PDF [VERIFY: flag and Playwright option] |
| PDF/A | Limited/commercial [VERIFY: PDF/A support in open-source vs ReportLab PLUS] | `pdf_variant="pdf/a-3b"` etc. [VERIFY: supported variants in installed version] | No | No (post-process with an external converter) |
| Licence | BSD (open-source); commercial ReportLab PLUS adds features | BSD | Apache 2.0 [VERIFY: xhtml2pdf licence on PyPI] | Chromium BSD; services vary |

## 4. Engineering decisions and trade-offs

### 4.1 Choosing an engine

| Situation | Choose | Why / cost |
|---|---|---|
| Designers own the look; invoices, statements, letters | WeasyPrint | Templates are reviewable HTML/CSS; needs Pango in the image |
| Very long tabular reports (10k+ rows), labels, precise coordinates | ReportLab | Fast, low memory per row, no system libs; layout in Python is verbose |
| Legacy project already on xhtml2pdf | Keep, don't expand | CSS gaps grow with requirements; migrate to WeasyPrint when layout fights you |
| Layout depends on JavaScript (client chart libs, SPA components) | Headless Chromium as an isolated service | Heavy, slow, large attack surface; prefer rendering charts server-side instead |
| Archival PDF/A mandated | WeasyPrint `pdf_variant` or external converter, then validate with veraPDF `[external tool]` | Claims of conformance must be validated, never assumed |
| Accessible PDF mandated (PDF/UA) | Evaluate WeasyPrint `pdf/ua-1` or Chromium tagged output; provide an accessible HTML alternative | No engine here guarantees full PDF/UA; manual audit required |

**Default for Django projects:** WeasyPrint for document-style reports; ReportLab when row volume
or precision dominates. Add exactly one engine per project unless a measured need justifies two.

### 4.2 HTML-to-PDF vs programmatic

| Factor | HTML-to-PDF | Programmatic |
|---|---|---|
| Who edits layout | Front-end/design + devs | Python devs only |
| Reuse of web templates | High (but print CSS differs from screen CSS) | None |
| Determinism | Depends on fonts/fontconfig present in the image | High |
| Long tables | Slower; whole document laid out in memory | Fast; flowables stream into frames |
| Security surface | Resource fetching (SSRF), HTML injection | Markup injection in `Paragraph` only |

### 4.3 Synchronous vs background

Render in the request only when the document is small and bounded (one invoice, a few pages,
< ~1–2 s at p95). Multi-page statements, batch invoices and anything with charts belong in a task
that stores an artifact — see [background-report-generation.md](background-report-generation.md).

## 5. Implementation patterns

### 5.1 WeasyPrint invoice with restricted resource loading

```python
# billing/pdf.py
from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from weasyprint import HTML, default_url_fetcher   # [third-party: weasyprint]

STATIC_PREFIX = "static:"   # templates reference e.g. src="static:billing/logo.png"

def local_only_url_fetcher(url, *args, **kwargs):
    # [VERIFY: url_fetcher signature/return type and the newer URLFetcher class API in the
    #  installed WeasyPrint release notes; the function form is documented for older versions]
    if url.startswith(STATIC_PREFIX):
        path = finders.find(url.removeprefix(STATIC_PREFIX))
        if path is None:
            raise ValueError(f"Unknown static asset: {url}")
        return default_url_fetcher(f"file://{path}", *args, **kwargs)
    if url.startswith("data:"):
        return default_url_fetcher(url, *args, **kwargs)
    raise ValueError(f"Blocked resource: {url}")       # no http(s), no arbitrary file://

def render_invoice_pdf(invoice) -> bytes:
    html = render_to_string("billing/invoice_pdf.html", {
        "invoice": invoice,
        "lines": invoice.lines.select_related("product").order_by("id"),
    })
    return HTML(string=html, url_fetcher=local_only_url_fetcher).write_pdf()
```

- `render_to_string` autoescapes `[Django]`; never pass user text through `|safe`.
- The fetcher is an allow-list, not a block-list. Threat analysis (redirects, DNS rebinding,
  `file://` reads): [reporting-security-and-compliance.md](reporting-security-and-compliance.md) and
  [django-security-engineering — ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md).
- Using `finders.find` works when static files are on local disk; with a CDN/remote `STATICFILES_STORAGE`,
  ship report assets inside the image or embed as `data:` URIs.

### 5.2 Print CSS: page size, margin boxes, page X of Y, repeated headers

```css
/* billing/static/billing/invoice_pdf.css */
@page {
  size: A4;
  margin: 20mm 15mm 22mm;
  @top-left     { content: element(doc-header); }
  @bottom-left  { content: "Invoice " string(invoice-number); font-size: 8pt; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; }
}
@page :first { @top-left { content: none; } }

header.running   { position: running(doc-header); }
h1.invoice-number { string-set: invoice-number content(); }

table.lines { width: 100%; border-collapse: collapse; }
table.lines thead { display: table-header-group; }   /* repeat on every page */
table.lines tfoot { display: table-footer-group; }
table.lines tr    { break-inside: avoid; }
td.amount         { text-align: right; font-variant-numeric: tabular-nums; }
.totals           { break-inside: avoid; break-before: avoid; }
```

`position: running()` / `element()` and `string-set` are CSS GCPM features supported by WeasyPrint
[VERIFY: running elements support in the installed version]; xhtml2pdf and Chromium do not support
them the same way.

### 5.3 ReportLab statement with repeated header and page X of Y

```python
# billing/pdf_reportlab.py  [third-party: reportlab]
from xml.sax.saxutils import escape                  # [stdlib]
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

class NumberedCanvas(canvas.Canvas):
    """Defer page output so the total page count is known (two-pass pattern)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()
    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8)
            self.drawRightString(195 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()

def build_customer_statement(fileobj, customer, invoices) -> None:
    styles = getSampleStyleSheet()
    rows = [["Invoice", "Issued", "Total"]]
    rows += [[inv.number, inv.issued_at.date().isoformat(), f"{inv.total:,.2f}"] for inv in invoices]
    table = Table(rows, repeatRows=1, colWidths=[60 * mm, 50 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
    ]))
    doc = SimpleDocTemplate(fileobj, pagesize=A4, title=f"Statement {customer.pk}",
                            author="Billing", subject="Customer statement")
    doc.build([Paragraph(escape(customer.name), styles["Title"]), table],
              canvasmaker=NumberedCanvas)
```

- `Paragraph` parses a mini-markup; unescaped `<` or `&` in a customer name breaks the build or
  injects formatting — always `escape()` user text. Plain `Table` cell strings are not parsed.
- The `NumberedCanvas` pattern keeps page states in memory; for thousands of pages, prefer
  "Page N" only, or a second render pass with the known total.
- Pass the invoice iterator already formatted; do not query inside flowable construction.

### 5.4 Fonts and Unicode

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont("NotoSans", "/app/fonts/NotoSans-Regular.ttf"))  # once, at import
```

- ReportLab's built-in standard fonts do not cover most non-Latin scripts; register TTF fonts and
  reference them in styles. Complex-script shaping is limited [VERIFY: shaping support in current ReportLab release notes].
- WeasyPrint uses fontconfig: install fonts in the image or declare `@font-face` with `src: url("static:fonts/...")`
  through the restricted fetcher. Never load web fonts from a CDN at render time.
- Pin font files in the repo or image; a different fallback font changes line breaks and page count,
  which breaks determinism and visual tests.

### 5.5 Images and charts

- Render charts to SVG or PNG first ([charts-and-data-visualization.md](charts-and-data-visualization.md)),
  then embed: WeasyPrint `<img src="data:image/svg+xml;base64,...">` or `static:`; ReportLab
  `platypus.Image(BytesIO(png), width=..., height=...)`. SVG in ReportLab needs svglib
  `[third-party: svglib]`.
- Downscale logos and photos to the printed size at ~150–300 DPI; a 5 MB logo on every page bloats
  output (engines may dedupe identical images [VERIFY: per engine]).

### 5.6 Metadata

- ReportLab: `SimpleDocTemplate(title=..., author=..., subject=..., keywords=...)`.
- WeasyPrint: from HTML — `<title>`, `<meta name="author">`, `<meta name="description">`,
  `<meta name="keywords">`, `<html lang="en">` [VERIFY: supported meta names].
- Record `report_key`, `report_version` and `data_as_of` in the visible footer and, where useful,
  keywords — this makes a printed copy traceable to a `ReportRun`.
- Do not put PII in metadata fields; they are indexed by desktop search tools.

### 5.7 Encryption and permissions (advisory)

```python
from io import BytesIO                        # [stdlib]
from pypdf import PdfReader, PdfWriter        # [third-party: pypdf]

def encrypt_pdf(pdf_bytes: bytes, user_password: str, out) -> None:
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes)))  # [VERIFY: clone_from kwarg in installed pypdf]
    writer.encrypt(user_password=user_password, algorithm="AES-256")   # [VERIFY: algorithm names]
    writer.write(out)
```

- A **user password** encrypts content; **permission flags** (no print/copy) are honoured only by
  compliant viewers and are trivially removed — never treat them as access control.
- Emailing a password-protected PDF with the password in the same email is theatre. Prefer an
  authenticated download link ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)).
- Encryption is incompatible with PDF/A.

### 5.8 Temporary files and memory

```python
import tempfile                                            # [stdlib]
from django.core.files import File                         # [Django]

with tempfile.SpooledTemporaryFile(max_size=20 * 1024 * 1024) as tmp:
    build_customer_statement(tmp, customer, invoices)      # ReportLab writes to file-like
    tmp.seek(0)
    report_storage.save(storage_name, File(tmp))
```

- WeasyPrint's `write_pdf(target=fileobj)` writes to a file object, but the full layout tree is
  in memory first; memory scales with document size, not just output size.
- Spooled files roll to disk past `max_size`; the temp directory must be private and on a volume
  with space. Clean up via context managers, not `finally: os.remove` scattered in code.

## 6. Production use cases

| Use case | Engine | Pattern |
|---|---|---|
| Single invoice download | WeasyPrint | Sync render in view if < ~1 s; otherwise pre-generate on `issued` and store artifact |
| Monthly batch of 20k invoices | WeasyPrint or ReportLab | One task per invoice (or small batch), idempotent on `(invoice_id, template_version)` |
| Payslips | WeasyPrint | Generated when `PayrollRun` is approved, stored immutable, never re-rendered with a new template silently |
| 300-page customer statement | ReportLab | Background task, streaming rows from `.iterator()` into flowables |
| Regulatory filing | WeasyPrint with `pdf_variant` + veraPDF validation | Archive original bytes + sha256 in `ReportArtifact` |
| Dashboard snapshot with JS charts | Server-side charts → WeasyPrint; Chromium service only if unavoidable | Isolate Chromium in its own container with no network egress |

## 7. Advantages and disadvantages

| Engine | Advantages | Disadvantages |
|---|---|---|
| ReportLab | Fast, precise, no system libs, mature, encryption built in | Verbose layout code; designers can't edit; complex scripts limited |
| WeasyPrint | Real paged-media CSS, maintainable templates, good typography | System libs (Pango); slower on huge tables; no JS; fetcher must be locked down |
| xhtml2pdf | Simple, pure-Python install | Weak CSS; layout surprises; slower evolution |
| Headless Chromium | Pixel-matches the browser, runs JS | Heavy, slow, big attack surface, operational burden |

## 8. Security considerations

- **SSRF / local file read via resource loading**: HTML engines fetch `<img>`, `<link>`, `@import`,
  `url()`; a user-controlled string reaching HTML (or CSS) can point at internal services or
  `file:///etc/...`. Mitigate with autoescaping + allow-list fetcher (WeasyPrint `url_fetcher`,
  xhtml2pdf `link_callback`) + no network egress for render workers. Analysis lives in
  [reporting-security-and-compliance.md](reporting-security-and-compliance.md).
- **HTML/markup injection**: user text rendered with `|safe` or into ReportLab `Paragraph` without
  `escape()` alters layout or injects links.
- **Chromium**: sandbox on, no `--no-sandbox` in shared hosts, no network, JS disabled unless needed,
  per-render timeouts.
- **Metadata leakage**: titles/keywords/authors and embedded file names may expose PII or internal names.
- **Permission flags are not security**; real control is who can download the artifact.
- Keep engines patched: font and image parsers in native libraries are a recurring CVE source
  ([django-security-engineering — dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md)).

## 9. Performance considerations

- PDF rendering is **CPU-bound** and usually **fully buffered**: you cannot stream a valid PDF
  page by page from these engines to the client (the cross-reference table is written at the end).
  Route to a dedicated queue with concurrency ≈ CPU cores ([report-generation-performance.md](report-generation-performance.md)).
- Fetch all data before rendering (one query for header, one for lines); no lazy relations in templates.
- WeasyPrint cost grows superlinearly with very long tables [VERIFY: measure on your data]; split
  into sections or use ReportLab beyond a few thousand rows.
- Reuse parsed CSS where the engine allows (`CSS(...)` object shared across renders) [VERIFY: WeasyPrint CSS object reuse across HTML renders].
- Limit images, embed subsetted fonts (default in most engines), avoid huge background images.
- Set per-task `soft_time_limit` and memory limits per worker; recycle workers (`max_tasks_per_child`).

## 10. Common mistakes and anti-patterns

- Rendering a 200-page PDF inside a web request.
- WeasyPrint with the default fetcher on templates containing user data (SSRF / file read).
- `|safe` on customer-supplied notes in an invoice template.
- Relying on system default fonts → output differs between laptops and containers; missing glyphs (□).
- Using screen CSS (flex grids, viewport units) and expecting print layout; ignoring `break-inside`.
- Page totals computed in the template by summing floats — compute `Decimal` totals in the selector/service.
- Treating PDF permission flags or a mailed password as access control.
- Re-rendering historical invoices with today's template and today's customer address instead of
  the stored artifact.
- Asserting only `status_code == 200` in tests.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Boxes/blank glyphs | Font lacks glyphs; fontconfig fallback | Register/install a font with coverage; pin fonts |
| Images missing in WeasyPrint | Fetcher blocked it or wrong path | Log blocked URLs; use `static:` scheme consistently |
| Header row not repeating | No `<thead>` (HTML) / `repeatRows` missing | Add thead / `repeatRows=1` |
| "Page 3 of 0" | Counter used outside margin box / single-pass canvas | Use `counter(pages)` in margin box / two-pass canvas |
| Rows split across pages | No `break-inside: avoid` | Add to `tr` and totals block |
| `OSError: cannot load library 'pango...'` | Missing system libs in image | Install per WeasyPrint install docs in Dockerfile |
| Worker OOM | Huge document laid out in memory | Split document, ReportLab, raise memory limit, recycle workers |

Validate generated files, not responses:

```python
from io import BytesIO
from pypdf import PdfReader                     # [third-party: pypdf]

def assert_invoice_pdf(pdf_bytes: bytes, invoice) -> None:
    assert pdf_bytes.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf_bytes))
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert invoice.number in text
    assert f"{invoice.total:,.2f}" in text
    assert reader.metadata.title == f"Invoice {invoice.number}"
```

- Text extraction order is engine-dependent; assert on presence, not positions.
- PDF/A: validate with veraPDF `[external tool]` in CI if conformance is claimed.
- Visual regression via rasterisation (`pdftoppm` `[external tool]`): see
  [report-generation-testing.md](report-generation-testing.md).

## 12. Related references and official documentation

- [report-templates-and-reusability.md](report-templates-and-reusability.md) — renderer interface, shared components, template versions
- [charts-and-data-visualization.md](charts-and-data-visualization.md) — chart images for PDFs
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — SSRF, PII, artifact protection
- [report-generation-performance.md](report-generation-performance.md) — buffering vs streaming, worker sizing
- [background-report-generation.md](background-report-generation.md) — task, storage, artifact lifecycle
- [docx-and-document-generation.md](docx-and-document-generation.md) — when DOCX beats PDF
- [report-generation-testing.md](report-generation-testing.md) — PDF content and visual tests
- [django-security-engineering — ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md)
- [django-security-engineering — cross-site-scripting](../../django-security-engineering/references/cross-site-scripting.md)
- [django-performance — file-and-data-processing-performance](../../django-performance/references/file-and-data-processing-performance.md)
- [django-data-processing — columnar-archive-and-document-files](../../django-data-processing/references/columnar-archive-and-document-files.md) (reading PDFs)
- <https://docs.djangoproject.com/en/5.2/howto/outputting-pdf/>
- <https://docs.reportlab.com/>
- <https://doc.courtbouillon.org/weasyprint/stable/>
- <https://xhtml2pdf.readthedocs.io/>
- <https://pypdf.readthedocs.io/>
- <https://www.w3.org/TR/css-page-3/>
- <https://verapdf.org/>
