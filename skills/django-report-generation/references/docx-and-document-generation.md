# DOCX and Document Generation

> **Scope:** Producing editable Word (`.docx`) reports from Django with python-docx (and optionally
> docxtpl): templates, reusable components, headers/footers, tables, styles, images, sections, page
> breaks, metadata, template versioning, large documents, validation and limitations, plus when DOCX
> is the right format at all. **Not here:** the cross-format renderer abstraction and template storage
> policy ([report-templates-and-reusability.md](report-templates-and-reusability.md)), PDF engines
> ([pdf-report-generation.md](pdf-report-generation.md)), reading/extracting uploaded documents
> ([django-data-processing — columnar-archive-and-document-files](../../django-data-processing/references/columnar-archive-and-document-files.md)).

## 1. Purpose and scope

DOCX is the format for **documents a human will edit after generation**: contract drafts,
board-pack narratives, audit working papers, HR letters, proposals pre-filled from data. It is a
poor format for final, tamper-evident output (use PDF) and for tabular data people will re-analyse
(use XLSX/CSV).

Django has no DOCX support `[Django]`. python-docx `[third-party: python-docx]` creates and edits
Office Open XML word-processing packages; docxtpl `[third-party: docxtpl]` adds Jinja2 templating on
top of python-docx. Neither renders a document visually — no page layout engine, no PDF output.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| OOXML package | A ZIP of XML parts (`word/document.xml`, `styles.xml`, headers, footers, media, `docProps/core.xml`). |
| Document / Section | A document contains sections; each section has page size, orientation, margins and its own header/footer. |
| Paragraph / Run | Paragraph = block; runs = character-formatted spans inside it. |
| Style | Named paragraph, character or table style defined in `styles.xml`; python-docx can only apply styles that exist in the template (it ships a default template). |
| Table | Grid of cells; can use a table style from the template. |
| Field | Dynamic value computed by Word on open/print (PAGE, NUMPAGES, TOC). python-docx has no high-level API for fields. |
| Core properties | Title, author, subject, keywords, created/modified, revision (`doc.core_properties`). |
| Template document | A `.docx` (or `.dotx`-derived `.docx`) with styles, headers, logos and placeholders that code fills. |

## 3. Internal mechanics

- `Document(path_or_stream)` loads the whole package and parses its XML into an in-memory tree
  (lxml). Everything you add grows that tree; `save(stream)` serialises and zips it at the end.
  **There is no streaming write** — memory scales with document size.
- `Document()` with no argument uses python-docx's built-in default template; corporate styles
  come from loading your own template file instead.
- Pagination does not exist in the file: Word (or LibreOffice) lays out pages when opened. So
  "which page is this table on", "page X of Y", TOC page numbers and widow control are all decided
  by the viewer, not by your code.
- Fields (PAGE, NUMPAGES, TOC) are XML instructions with cached results; viewers update them
  on open/print — some only after user confirmation [VERIFY: Word `updateFields` setting behaviour].
- docxtpl renders Jinja2 tags placed in the template's text runs, then saves through python-docx.
  Tags split across runs by Word's editor are a classic failure (see §11).

## 4. Engineering decisions and trade-offs

### 4.1 DOCX vs PDF vs XLSX

| Need | DOCX | PDF | XLSX |
|---|---|---|---|
| Recipient edits the narrative | **Best** | Poor | Poor |
| Fixed, legally final layout | Poor (renders differently per viewer) | **Best** | Poor |
| Recipient re-analyses numbers | Poor | Poor | **Best** |
| Archival / evidence (hash, PDF/A) | Weak | **Best** | Weak |
| Precise page numbering at generation time | No (viewer computes) | Yes | n/a |
| Mail-merge-style letters | **Good** (templates) | Good | n/a |
| Large tabular data (10k+ rows) | Poor (slow to open, huge) | Acceptable | **Best** |

Common hybrid: generate DOCX for editing **and** a PDF of the approved version — the PDF, not the
DOCX, is the record.

### 4.2 Programmatic python-docx vs template (docxtpl)

| Factor | Programmatic python-docx | docxtpl template |
|---|---|---|
| Who changes layout | Developers | Business/design users edit the `.docx` |
| Dynamic structure (variable sections, nested loops) | Full control | Jinja loops/conditions; fine for moderate complexity |
| Risk | Verbose code | Template drift, broken tags, untested edits |
| Testing | Code-level | Must version and test the template file itself |

Default: **template file for branding and static layout + programmatic python-docx (or docxtpl) for
data-driven content.** Keep templates in the repository under version control unless business
users must edit them — then see storage options in
[report-templates-and-reusability.md](report-templates-and-reusability.md).

### 4.3 Converting DOCX to PDF

python-docx cannot. Options: LibreOffice headless (`soffice --headless --convert-to pdf`)
`[external tool]`, a document-conversion service `[external service]`, or Microsoft Graph conversion
`[external service]` [VERIFY: Graph format conversion endpoint]. LibreOffice rendering differs from
Word (fonts, spacing). If PDF is the real deliverable, generate PDF directly with an HTML/PDF engine.

## 5. Implementation patterns

### 5.1 Building from a versioned template

```python
# reports/docx/payroll_summary.py  [third-party: python-docx]
from io import BytesIO
from pathlib import Path
from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.shared import Inches, Pt

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_VERSION = 3                      # bump on any layout/style change; recorded on the run

def build_payroll_summary(payroll_run, department_rows, *, chart_png: bytes | None) -> bytes:
    doc = Document(TEMPLATE_DIR / f"payroll_summary_v{TEMPLATE_VERSION}.docx")

    props = doc.core_properties
    props.title = f"Payroll summary {payroll_run.period_start:%Y-%m}"
    props.subject = "Payroll"
    props.author = "Payroll reporting"     # not an individual's name/PII
    props.keywords = f"payroll_run={payroll_run.pk};template=v{TEMPLATE_VERSION}"

    doc.add_heading(props.title, level=1)
    doc.add_paragraph(
        f"Period {payroll_run.period_start:%d %b %Y} – {payroll_run.period_end:%d %b %Y}. "
        f"Status: {payroll_run.get_status_display()}."
    )

    table = doc.add_table(rows=1, cols=4, style="Report Table")   # style must exist in template
    for cell, title in zip(table.rows[0].cells, ("Department", "Employees", "Gross", "Net")):
        cell.text = title
    for row in department_rows:                                   # small, aggregated list
        cells = table.add_row().cells
        cells[0].text = row["department"]
        cells[1].text = str(row["employees"])
        cells[2].text = f"{row['gross']:,.2f}"
        cells[3].text = f"{row['net']:,.2f}"

    if chart_png:
        doc.add_page_break()
        doc.add_picture(BytesIO(chart_png), width=Inches(6))

    appendix = doc.add_section(WD_SECTION.NEW_PAGE)
    appendix.orientation = WD_ORIENT.LANDSCAPE
    appendix.page_width, appendix.page_height = appendix.page_height, appendix.page_width
    doc.add_heading("Appendix", level=2)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()
```

Notes:
- `cell.text = ...` and `add_paragraph(text)` insert text literally — no markup parsing, so user
  strings cannot inject formatting. They can still contain control characters invalid in XML;
  strip them (see §8).
- Changing orientation requires swapping width and height explicitly; setting `orientation` alone
  does not rotate the page.
- Apply number formatting yourself (Word has no cell number formats); format `Decimal`s at the
  presentation boundary with the locale rules in [report-templates-and-reusability.md](report-templates-and-reusability.md).

### 5.2 Headers and footers per section

```python
section = doc.sections[0]
section.different_first_page_header_footer = True
header = section.header
header.is_linked_to_previous = False
header.paragraphs[0].text = f"{tenant.name} — Confidential"
footer = section.footer
footer.paragraphs[0].text = f"Generated {run.finished_at:%Y-%m-%d %H:%M} UTC · {run.report_key} v{run.report_version}"
```

Page numbers ("Page X of Y") need PAGE/NUMPAGES fields. python-docx has no API for fields; either
put the fields in the template's footer (recommended — Word authors them, code never touches
them) or insert field XML through `docx.oxml` low-level elements [VERIFY: oxml element construction
for `w:fldSimple`/`w:fldChar` in the installed python-docx version].

### 5.3 Reusable components

Keep small functions that take a `Document` and data, add one block, and return nothing:

```python
def add_kpi_table(doc, kpis: list[tuple[str, str]], *, style: str = "KPI Table") -> None:
    table = doc.add_table(rows=0, cols=2, style=style)
    for label, value in kpis:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, value

def add_disclaimer(doc, text: str) -> None:
    doc.add_paragraph(text, style="Disclaimer")
```

Components depend only on style names; the template provides the styles. A missing style raises
`KeyError` at generation — catch that in tests against every template version.

### 5.4 Template filling with docxtpl (optional)

```python
from docxtpl import DocxTemplate          # [third-party: docxtpl]

tpl = DocxTemplate(TEMPLATE_DIR / "offer_letter_v2.docx")
tpl.render({"employee": {"full_name": employee.full_name, "department": employee.department},
            "salary": f"{salary:,.2f}", "start_date": start_date.isoformat()},
           autoescape=True)           # [VERIFY: autoescape parameter in installed docxtpl]
tpl.save(out)
```

- Pass a plain dict of already formatted, primitive values — not model instances (avoids lazy
  queries and exposing attributes to template authors).
- docxtpl uses Jinja2 `[third-party: Jinja2]`, not the Django template engine. Enable escaping of
  XML special characters; unescaped `&` or `<` produces a corrupt document.
- A business-edited template is code: validate it in CI by rendering with fixture data.

### 5.5 Large documents

python-docx holds the whole DOM in memory and `add_row()` on large tables is slow. Guidance:
- Keep DOCX tables to hundreds, not tens of thousands, of rows; put bulk data in an XLSX companion.
- Generate in a background task with a time limit
  ([background-report-generation.md](background-report-generation.md)).
- Write to `SpooledTemporaryFile` and save to private storage; don't hold multiple copies of `bytes`.

## 6. Production use cases

| Use case | Approach |
|---|---|
| HR offer/contract letters | docxtpl template per legal version; output DOCX for edit + PDF of signed version |
| Monthly payroll narrative for finance | python-docx with template v-N; aggregated tables only; detailed payslip data in XLSX |
| Audit working papers | DOCX with data tables pre-filled; stored per `ReportRun`, immutable once issued |
| Board pack | DOCX sections assembled from components; charts from Matplotlib PNG |
| Customer proposal | Template owned by sales ops, reviewed via CI render tests |

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Recipients can edit and comment | No control of final pagination; renders differently in Word/LibreOffice/Google Docs |
| Templates editable by non-developers | Templates drift; tags break when edited in Word |
| python-docx is pure Python, no system libs | No PDF export, no fields API, whole document in memory |
| Corporate styles reused via template | Large tables are slow to build and open |

## 8. Security considerations

- **Not a security boundary**: DOCX can be edited trivially; never use it as the system of record.
- **XML-invalid characters**: control characters (e.g. `\x00`–`\x08`) in user text raise or corrupt
  the file; strip them before writing.
- **Template trust**: a template supplied by users can contain macros (only in `.docm`), external
  links, remote images or tracking pixels (`INCLUDEPICTURE` fields, external relationships). Accept
  templates only from trusted administrators; validate the package — reject macro-enabled types and
  external relationships [VERIFY: how to inspect `part.rels` for `TargetMode="External"`].
- **Jinja in docxtpl**: template authors can run Jinja expressions on the context — pass a minimal
  dict of primitives, never ORM objects, `request` or settings.
- **Metadata**: core properties and "last modified by" may reveal names; set them explicitly.
  Python-docx keeps whatever the template had (including the template author) unless overwritten.
- Store and deliver like any report artifact (private storage, authorised download) —
  [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Cost is dominated by XML tree manipulation; time grows with number of runs/cells. Aggregate in
  SQL and add only summary tables.
- Loading a template with many embedded images/fonts costs each run; keep templates lean.
- Images: downscale before `add_picture`; DOCX stores images at original resolution.
- DOCX→PDF via LibreOffice is slow (process start) and not thread-safe in one profile; run a
  dedicated converter worker/service with concurrency 1 per instance [VERIFY: LibreOffice concurrent
  profile behaviour].

## 10. Common mistakes and anti-patterns

- Choosing DOCX for a final financial statement that must not change.
- Using `Document()` (default template) and then applying corporate style names that don't exist.
- Building 50k-row tables in Word.
- Passing model instances into docxtpl contexts.
- Templates stored only in a production database with no history or tests.
- Expecting page numbers or TOC page numbers to be correct without fields being updated by a viewer.
- Re-generating historical documents with the current template version instead of the stored artifact.

## 11. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError: "no style with name 'Report Table'"` | Style missing from template | Add style to template; test all template versions |
| Word says the file is corrupt | Invalid XML chars, unescaped `&`/`<` in docxtpl | Strip control chars; enable escaping |
| docxtpl tag rendered literally | Word split `{{ name }}` across runs | Retype tag in one go / paste as plain text; inspect `document.xml` |
| Landscape section still portrait | Width/height not swapped | Swap `page_width`/`page_height` |
| Page numbers show stale values | Field results cached | Update fields on open; or produce PDF for fixed numbering |
| PDF conversion looks different | LibreOffice fonts/metrics differ | Install the same fonts in the converter image |

Validate content by reopening the bytes:

```python
from io import BytesIO
from docx import Document

def assert_payroll_docx(data: bytes, payroll_run) -> None:
    doc = Document(BytesIO(data))
    assert doc.core_properties.title.startswith("Payroll summary")
    texts = [p.text for p in doc.paragraphs]
    assert any(f"{payroll_run.period_start:%d %b %Y}" in t for t in texts)
    header = [c.text for c in doc.tables[0].rows[0].cells]
    assert header == ["Department", "Employees", "Gross", "Net"]
```

More test patterns: [report-generation-testing.md](report-generation-testing.md).

## 12. Related references and official documentation

- [report-templates-and-reusability.md](report-templates-and-reusability.md) — renderer abstraction, template versioning and storage, localisation
- [pdf-report-generation.md](pdf-report-generation.md) — final-layout documents
- [excel-report-generation.md](excel-report-generation.md) — tabular companions to narrative documents
- [charts-and-data-visualization.md](charts-and-data-visualization.md) — chart images for DOCX
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — artifact protection, PII
- [report-generation-testing.md](report-generation-testing.md) — DOCX output tests
- [django-security-engineering — file-upload-security](../../django-security-engineering/references/file-upload-security.md) — accepting uploaded templates
- [django-data-processing — columnar-archive-and-document-files](../../django-data-processing/references/columnar-archive-and-document-files.md) — reading documents
- <https://python-docx.readthedocs.io/>
- <https://docxtpl.readthedocs.io/>
- <https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html>
- <https://learn.microsoft.com/en-us/office/open-xml/word/overview>
