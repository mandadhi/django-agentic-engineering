# Report Templates and Reusability

> **Scope:** How to make report output reusable across formats and reports: a format-agnostic
> intermediate `ReportDocument` with a `Renderer` protocol, shared components and branding, template
> storage and versioning (code vs database), safe use of the Django template engine for HTML/PDF,
> localisation of numbers, dates and currency, and theming. **Not here:** library-specific rendering
> details — see [pdf-report-generation.md](pdf-report-generation.md),
> [excel-report-generation.md](excel-report-generation.md),
> [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md),
> [docx-and-document-generation.md](docx-and-document-generation.md); report definitions and the
> `ReportSpec` registry ([reporting-architecture-and-design.md](reporting-architecture-and-design.md)).

## 1. Purpose and scope

Once a system has more than a handful of reports, duplication appears: each report re-implements
its header, its money formatting, its "data as of" footer, and its CSV, XLSX and PDF variants drift
apart (a column added to the PDF but not the CSV, a different rounding rule in each). This
reference gives the structures that prevent that drift:

- one **data → document model → renderer** pipeline, so a report's content is defined once and
  rendered to several formats;
- **components** (header, KPI block, totals table, disclaimer) shared by all reports;
- **versioned templates**, so a historical report can be explained and reproduced;
- one **presentation-formatting** layer for locale, currency and time zone.

It does not mandate an abstraction for every project: a system with three reports and one format
each should not build a rendering framework (see §4).

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Report data | Typed result of the selector (`ReportDataset`): rows, aggregates, `data_as_of` — no formatting. |
| `ReportDocument` | Format-agnostic intermediate representation: metadata + ordered blocks (heading, paragraph, KPI list, table, chart, page break). |
| Block | One structural element. Tables carry **typed** columns (text, integer, money, percent, date, datetime). |
| Renderer | Converts a `ReportDocument` to bytes in one format (`csv`, `xlsx`, `pdf`, `docx`). |
| Component | Reusable function producing blocks (or template partials) shared by many reports. |
| Template | Format-specific layout asset: Django HTML template for PDF, `.docx` template, `.xlsx` template workbook. |
| Template version | Immutable identifier of the layout used for a run; part of reproducibility. |
| Presentation formatter | Locale/currency/time-zone aware conversion of typed values to display strings for text formats. |
| Theme | Brand tokens (colours, fonts, logo, legal footer) per tenant or product, consumed by all renderers. |

## 3. Internal mechanics

```text
ReportSpec.selector(tenant, user, params)
        │  typed rows + aggregates (Decimal, aware datetime, int)
        ▼
build_document(data, params, theme, locale)  ── components ──┐
        │  ReportDocument (blocks, typed columns)            │
        ▼                                                    │
RENDERERS[fmt].render(document, out)                         │
   csv  : typed → canonical serialisation (no locale)        │
   xlsx : typed → native cell types + number_format          │
   pdf  : typed → formatted strings → Django template → engine
   docx : typed → formatted strings → template styles ◄──────┘
        ▼
ReportArtifact (bytes, sha256) + run's report_version + template version
```

Key rule: **values stay typed until the renderer.** CSV and XLSX must receive `Decimal`/`date`
values (machine-readable, native cells); PDF and DOCX format them for humans. Formatting early
(turning `Decimal("1234.50")` into `"1.234,50 €"` in the selector) breaks spreadsheets, sorting and
tests.

## 4. Engineering decisions and trade-offs

### 4.1 When to introduce the intermediate model

| Situation | Recommendation |
|---|---|
| 1–5 reports, one format each | No IR. A selector + one rendering function per report; share only formatting helpers. |
| Same report in 2+ formats, or 10+ reports | Introduce `ReportDocument` + renderers. |
| Pixel-designed documents (invoice, payslip) | Bypass the generic IR: dedicated Django template rendered by the PDF engine, sharing only theme and formatters. |
| Large row-streaming exports (CSV of 5M rows) | Bypass the IR for rows: stream directly from `.iterator()` ([csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)); the IR would materialise rows. |

The IR is for **structure and consistency**, not for every byte. Its table blocks should accept an
iterator so streaming renderers are not forced to buffer — but PDF/DOCX renderers will buffer anyway.

### 4.2 Template storage: code vs database

| Factor | Templates in the repository | Templates in the database / storage |
|---|---|---|
| Change control | PR review, CI render tests, deploy | Admin UI; needs its own approval workflow |
| Versioning | Git history + explicit version constant | Must model `ReportTemplate(key, version, content, approved_by, approved_at)` and keep old versions immutable |
| Security | Trusted, reviewed | **Server-side template injection risk**; restricted engine required |
| Per-tenant customisation | Limited (theme tokens) | Possible |
| Rollback | Redeploy | Activate previous version |

Default: **repository templates + per-tenant theme tokens** (logo, colours, legal footer text) stored
as data. Database-stored layout templates only when business users must change layout without
deploys, and then only for trusted administrators, with versioning, approval, and a restricted engine.

### 4.3 Template versioning policy

- A template version is **immutable** once used for a delivered artifact.
- `ReportRun.report_version` captures output semantics (columns, calculations); record the template
  version alongside it (e.g. in artifact metadata or an extension field on the run).
- Bump the template version for any change a recipient could notice (layout, wording, columns,
  rounding display). Cosmetic-only changes still bump: a regulator comparing two copies will notice.
- Re-delivering an old report means **serving the stored artifact**, not re-rendering with the
  current template. Re-rendering historical data with a new template must be an explicit,
  audited action producing a new run.

### 4.4 Localisation source

| Need | Use |
|---|---|
| Dates and numbers in the project's configured languages | Django l10n `[Django]`: `formats.number_format`, `formats.date_format`, `translation.override` |
| Currency symbol/placement per locale, CLDR-correct formats | Babel `[third-party: Babel]` `format_currency`, `format_decimal` |
| Machine formats (CSV/JSON/XML) | No localisation: ISO 8601, `.` decimal separator, ISO 4217 code in its own column |
| XLSX | Native types + `number_format`; Excel applies the viewer's locale to separators |

## 5. Implementation patterns

### 5.1 Intermediate document model

```python
# reports/document.py
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Literal, Protocol

ColumnType = Literal["text", "int", "money", "percent", "date", "datetime"]

@dataclass(frozen=True)
class Column:
    key: str
    label: str
    type: ColumnType
    currency_field: str | None = None      # row key holding ISO 4217 code for money

@dataclass(frozen=True)
class TableBlock:
    columns: tuple[Column, ...]
    rows: Iterable[dict]                   # may be an iterator — renderers must not re-iterate
    totals: dict | None = None

@dataclass(frozen=True)
class HeadingBlock:
    text: str
    level: int = 1

@dataclass(frozen=True)
class KpiBlock:
    items: tuple[tuple[str, object, ColumnType], ...]

@dataclass(frozen=True)
class ChartBlock:
    png: bytes                             # produced by the charts module
    alt_text: str

Block = HeadingBlock | KpiBlock | TableBlock | ChartBlock

@dataclass
class ReportDocument:
    title: str
    report_key: str
    report_version: int
    template_version: int
    data_as_of: datetime
    timezone: str
    locale: str
    blocks: list[Block] = field(default_factory=list)

class Renderer(Protocol):
    content_type: str
    extension: str
    def render(self, document: ReportDocument, out: BinaryIO) -> None: ...
```

Renderers are registered by format and looked up from `ReportSpec.formats`:

```python
# reports/renderers/__init__.py
RENDERERS: dict[str, Renderer] = {
    "csv": CsvRenderer(),     # uses safe_cell, ISO dates, plain Decimals
    "xlsx": XlsxRenderer(),   # native types + number formats
    "pdf": PdfRenderer(template="reports/pdf/generic_report.html"),
}

def render(document: ReportDocument, fmt: str, out) -> Renderer:
    renderer = RENDERERS[fmt]
    renderer.render(document, out)
    return renderer
```

A renderer that cannot represent a block must fail loudly (`ChartBlock` in CSV → skip only if the
spec says so, otherwise raise). Silent omission is how formats drift.

### 5.2 Building the document with shared components

```python
# sales/report_documents.py
from reports.components import header_blocks, totals_row
from reports.document import Column, KpiBlock, ReportDocument, TableBlock

COLUMNS = (
    Column("region", "Region", "text"),
    Column("orders", "Orders", "int"),
    Column("revenue", "Revenue", "money", currency_field="currency"),
)

def build_weekly_sales_document(data, *, params, tenant, theme) -> ReportDocument:
    doc = ReportDocument(
        title=f"Weekly sales {params['week_start']:%Y-%m-%d}",
        report_key="sales.weekly_revenue", report_version=4, template_version=2,
        data_as_of=data.data_as_of, timezone=tenant.timezone, locale=theme.locale,
    )
    doc.blocks += header_blocks(doc, theme)
    doc.blocks.append(KpiBlock((("Orders", data.totals["orders"], "int"),
                                ("Revenue", data.totals["revenue"], "money"))))
    doc.blocks.append(TableBlock(COLUMNS, data.rows, totals=totals_row(data)))
    return doc
```

Components (`header_blocks`, `totals_row`, `disclaimer_block`) live in one module, take data and
theme, and return blocks. Totals come from the selector's SQL aggregate, never re-summed from
formatted strings in the renderer.

### 5.3 Django templates for HTML/PDF safely

```python
# reports/renderers/pdf.py
from django.template.loader import render_to_string     # [Django]
from django.utils import translation

class PdfRenderer:
    content_type, extension = "application/pdf", "pdf"

    def __init__(self, template: str):
        self.template = template

    def render(self, document, out):
        with translation.override(document.locale):
            html = render_to_string(self.template, {
                "doc": document,
                "fmt": PresentationFormatter(document.locale, document.timezone),
            })
        html_to_pdf(html, out)            # engine + restricted fetcher: pdf-report-generation.md
```

Template rules:

- Autoescaping stays on `[Django]`; no `|safe`/`mark_safe` on data.
- Shared partials: `{% include "reports/pdf/_header.html" %}`,
  `{% include "reports/pdf/_table.html" with block=block %}`; a base template
  `reports/pdf/base.html` holds `@page` CSS and the running footer with `report_key`, versions and
  `data_as_of`.
- Pass prepared data only; templates must not trigger queries (no `invoice.lines.all` in a loop
  without prefetch). Test with `django_assert_num_queries` / `assertNumQueries`.
- `string_if_invalid` helps catch missing variables during development; Django documents it as a
  debugging aid, not production behaviour `[Django]`.

### 5.4 Restricted engine for database-stored templates (only if unavoidable)

```python
from django.template import Context, Engine   # [Django]

RESTRICTED_ENGINE = Engine(
    dirs=[], app_dirs=False,
    libraries={},                               # no {% load %} of custom tag libraries
    builtins=["django.template.defaulttags", "django.template.defaultfilters"],
    autoescape=True,
)

def render_db_template(source: str, context: dict) -> str:
    return RESTRICTED_ENGINE.from_string(source).render(Context(context))
```

Residual risks even with this engine: built-in tags such as `{% include %}`/`{% extends %}` and
`{% debug %}` remain available [VERIFY: whether a custom `builtins` list can exclude individual
default tags; it likely requires a custom tag library], and any callable without arguments on context
objects can be invoked. Therefore: context = plain dicts of primitives, templates authored only by
trusted admins, every version reviewed and stored immutably, and render-tested before activation.
See [django-security-engineering — injection-prevention](../../django-security-engineering/references/injection-prevention.md).

### 5.5 Presentation formatting

```python
# reports/presentation.py
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo                              # [stdlib]

from babel.numbers import format_currency                  # [third-party: Babel] — only if installed
from django.utils import formats, timezone, translation    # [Django]


class PresentationFormatter:
    def __init__(self, locale: str, tz: str):
        self.locale, self.tz = locale, ZoneInfo(tz)

    def money(self, value: Decimal, currency: str) -> str:
        q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)   # business rounding rule
        return format_currency(q, currency, locale=self.locale.replace("-", "_"))

    def number(self, value) -> str:
        with translation.override(self.locale):
            return formats.number_format(value, decimal_pos=0, use_l10n=True, force_grouping=True)

    def datetime(self, value) -> str:
        local = timezone.localtime(value, self.tz)
        with translation.override(self.locale):
            return formats.date_format(local, "DATETIME_FORMAT", use_l10n=True)
```

- Currency **amount and code come from the data**, never from the viewer's locale; a locale only
  decides symbol placement and separators.
- Rounding for display must match how totals were computed; state the rule in the report footer
  when it matters (financial statements).
- JPY and other zero-decimal currencies: quantize per currency exponent [VERIFY: use Babel's
  currency digits data or an ISO 4217 table].
- Time zone: the report's time zone (tenant), not the server's, and not the requesting user's
  unless the spec says so — and print it.

### 5.6 Theming

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Theme:
    locale: str
    primary_colour: str          # "#1F4E79"
    font_family: str             # must be installed/registered for PDF engines
    logo_static_path: str        # "reports/logos/acme.png" (static, allow-listed)
    legal_footer: str            # plain text, escaped by templates
```

Theme tokens are data (per tenant), validated on save (hex colour regex, allow-listed fonts, logo
uploaded through the hardened upload path and stored privately). PDF templates consume them as CSS
variables in an inline `<style>` block (escaped); XLSX renderers map `primary_colour` to header
fills; DOCX uses template styles (per-tenant templates only if branding requires it).

## 6. Production use cases

| Use case | Pattern |
|---|---|
| Weekly sales report as XLSX for analysts and PDF for managers | One `ReportDocument`; two renderers; identical totals by construction |
| White-label SaaS reports | Repository templates + per-tenant `Theme`; no per-tenant layout code |
| Invoice and payslip | Dedicated templates (not generic IR), shared theme and formatter, template version stored with the artifact |
| Regulator asks "why does the March copy differ?" | Stored artifact + `report_version` + template version explain it |
| Customer success edits a welcome-pack layout monthly | DB-stored template, restricted engine, approval workflow, render preview with fixture data |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| IR + renderers | Formats stay consistent; new formats cheap; renderers testable in isolation | Abstraction cost; lowest-common-denominator layouts; may tempt buffering of rows |
| Per-report bespoke rendering | Maximum layout control; simple for few reports | Duplication; formats drift |
| Repository templates | Reviewed, tested, versioned for free | Changes need deploys |
| DB-stored templates | Business agility | Injection risk, version management, untested changes reach production |
| Babel for formatting | CLDR-correct currency/number output | Extra dependency; must be installed in workers |

## 8. Security considerations

- **Server-side template injection**: never render user-supplied template source with the default
  engine; see §5.4. User *data* is fine with autoescaping; user *templates* are code.
- **Escaping per output**: HTML → Django autoescape; ReportLab `Paragraph` → `xml.sax.saxutils.escape`;
  CSV/XLSX → `safe_cell` for formula injection ([csv-and-structured-report-generation.md](csv-and-structured-report-generation.md));
  DOCX → strip XML-invalid characters. A shared renderer must apply the right escaping for its format.
- **Theme assets**: logos are uploads — validate type and size, store privately, reference via the
  PDF engine's allow-listed fetcher, never as arbitrary URLs (SSRF).
- **Context minimisation**: templates receive only what they display; no `request`, `user`
  objects or settings, which leak data through `{{ }}` mistakes or admin-authored templates.
- **Classification footer**: components should print the report's classification
  (`ReportSpec.classification`) on every page/sheet for confidential/restricted reports.

## 9. Performance considerations

- The IR adds negligible cost for summary documents; for large tables keep `TableBlock.rows` an
  iterator and let streaming renderers consume it once.
- Format with one formatter instance per render, not per cell; avoid entering `translation.override`
  per cell.
- Babel formatting per cell is slower than `str(Decimal)`; machine formats skip it entirely.
- Template loading: rely on the cached template loader in production [VERIFY: cached loader default
  behaviour in Django 5.2 when `DEBUG=False`]; DB-stored templates should cache compiled `Template`
  objects keyed by `(key, version)`.

## 10. Common mistakes and anti-patterns

- Formatting values to strings in the selector, then writing strings into XLSX (text cells, no sums).
- Re-summing totals in each renderer — the PDF and CSV disagree after a rounding change.
- A "generic" renderer that silently drops blocks it can't render.
- Rendering old runs with the current template and calling it the original.
- Admin-editable templates rendered by the project's default engine with model instances in context.
- Server locale or `settings.TIME_ZONE` leaking into report output instead of the report's locale/time zone.
- Building an IR framework for three reports.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| PDF and XLSX totals differ | Different rounding paths or totals recomputed | Totals sourced from one aggregate; renderer tests compare outputs |
| Wrong decimal separator in CSV | Locale formatting applied to machine format | CSV renderer must bypass `PresentationFormatter` |
| Dates off by one day | Formatting in UTC/server tz | `localtime(value, report_tz)` in formatter |
| Template change broke one tenant | Tenant theme font not installed or logo missing | Render-test every active theme in CI |
| `TemplateSyntaxError` in production from DB template | Unvalidated template saved | Compile + render-preview on save; reject before activation |

Validation practices:

- **Renderer contract test**: one fixture `ReportDocument` rendered by every registered renderer;
  assert each output contains the same headers and totals ([report-generation-testing.md](report-generation-testing.md)).
- **Template matrix**: every active `(template_version, theme)` renders in CI.
- **Query budget**: rendering performs zero queries.

## 12. Related references and official documentation

- [reporting-architecture-and-design.md](reporting-architecture-and-design.md) — `ReportSpec`, `ReportRun`, versioning fields
- [pdf-report-generation.md](pdf-report-generation.md) · [excel-report-generation.md](excel-report-generation.md) · [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) · [docx-and-document-generation.md](docx-and-document-generation.md) — format renderers
- [charts-and-data-visualization.md](charts-and-data-visualization.md) — `ChartBlock` content
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — typed data, time zones, Decimal
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — classification, PII
- [report-generation-testing.md](report-generation-testing.md) — renderer and template tests
- [django-security-engineering — injection-prevention](../../django-security-engineering/references/injection-prevention.md)
- [django-security-engineering — cross-site-scripting](../../django-security-engineering/references/cross-site-scripting.md)
- [django-architecture — design-patterns](../../django-architecture/references/design-patterns.md) — Strategy/Adapter for renderers
- <https://docs.djangoproject.com/en/5.2/ref/templates/api/>
- <https://docs.djangoproject.com/en/5.2/topics/i18n/formatting/>
- <https://docs.djangoproject.com/en/5.2/topics/i18n/timezones/>
- <https://docs.python.org/3/library/decimal.html>
- <https://babel.pocoo.org/en/latest/numbers.html>
