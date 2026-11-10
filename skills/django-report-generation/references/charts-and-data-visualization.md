# Charts and Data Visualization

> **Scope:** Charts inside generated reports — choosing static vs interactive charts per output format, server-side rendering with Matplotlib and Plotly, chart data preparation (aggregation, grouping, gap filling, time series), axis/legend/label formatting, embedding in PDF, XLSX, DOCX, email and HTML, accessibility, determinism, performance and memory cleanup. **Not here:** native Excel chart APIs in depth ([excel-report-generation.md](excel-report-generation.md)), PDF page layout ([pdf-report-generation.md](pdf-report-generation.md)), DOCX layout ([docx-and-document-generation.md](docx-and-document-generation.md)), report queries ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)), interactive front-end dashboards ([django-frontend](../../django-frontend/SKILL.md)).

## 1. Purpose and scope

A chart in a report is a **derived view of numbers that already appear (or could appear) in a
table**. Its engineering requirements follow from that:

- it must be generated from the same snapshot and the same aggregated figures as the tables
  next to it, never from a separate query that might disagree;
- it must render without a browser, display or user session — on a headless worker;
- it must work in the target container: a PDF page, an XLSX sheet, a DOCX body, an email client
  that runs no JavaScript;
- it must be readable by people who cannot distinguish colours or cannot see the image;
- it must not leak memory across thousands of renders in a long-lived worker.

Matplotlib and Plotly are `[third-party]` libraries; neither is part of Django. Use one charting
library per project where possible — two libraries means two styling systems and two dependency
footprints.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Static chart | A raster (PNG) or vector (SVG/PDF) image fixed at generation time. Works everywhere images work. |
| Interactive chart | HTML + JavaScript (e.g. Plotly.js) rendered in a browser: hover, zoom, toggle series. Works only where JS runs. |
| Native spreadsheet chart | A chart object inside XLSX bound to cell ranges; the spreadsheet application renders it. |
| Backend (Matplotlib) | The renderer. `Agg` is the non-interactive raster backend for servers; GUI backends need a display. |
| pyplot state machine | `matplotlib.pyplot` keeps global "current figure" state — not thread-safe and leaks figures if not closed. |
| Kaleido | `[third-party: kaleido]` engine Plotly uses to export static images; recent versions drive a locally installed Chrome/Chromium `[VERIFY: Kaleido v1 Chrome requirement and plotly version compatibility]`. |
| Chart dataset | The small, pre-aggregated series (dozens to a few thousand points) the chart draws. |

### Which chart for which format

| Output | Recommended | Why / caveat |
|---|---|---|
| PDF (WeasyPrint) | SVG (crisp, small) or PNG at 150–200 dpi | WeasyPrint renders SVG images `[VERIFY: WeasyPrint SVG feature coverage for Matplotlib SVG output]`; embed as data URI or local file — never remote URL. |
| PDF (ReportLab) | PNG via `platypus.Image`; vector via `[third-party: svglib]` → ReportLab drawing | ReportLab also has its own `reportlab.graphics` charts — native but a separate, more verbose API. |
| XLSX | Native Excel chart over a summary range | Users can re-filter and the chart stays live; PNG only for branded fixed visuals. |
| DOCX | PNG (`add_picture`) | python-docx has no chart API; SVG support in Word via python-docx is not available `[VERIFY: python-docx SVG support]`. |
| Email body | PNG (inline CID attachment or hosted, authenticated link) | Email clients run no JavaScript; many block remote images; SVG support is poor. |
| Web report page / dashboard | Plotly HTML (`to_html`) or a front-end chart library | Interactive; serve plotly.js from your static files, not a public CDN, if CSP or offline use matters. |
| CSV/JSON | No chart — export the chart dataset | Consumers chart it themselves. |

## 3. Internal mechanics

### Matplotlib on servers

- Select the non-interactive backend **before** pyplot is imported: environment variable
  `MPLBACKEND=Agg` in the worker image, or `matplotlib.use("Agg")` at process start. Modern
  Matplotlib falls back to Agg when no display is available `[VERIFY: Matplotlib headless backend fallback behaviour]`, but be explicit.
- The object-oriented API — `from matplotlib.figure import Figure` — creates figures that are **not
  registered with pyplot**, so they are garbage-collected normally and are safe to create
  concurrently in different threads (one figure per thread) `[VERIFY: Matplotlib thread-safety docs]`.
  This is the documented pattern for web applications.
- With pyplot, every `plt.figure()`/`plt.subplots()` stays referenced until `plt.close(fig)`;
  forgetting it is the classic worker memory leak (Matplotlib warns after 20 open figures).
- Font discovery builds a cache on first use (`~/.cache/matplotlib`, or `MPLCONFIGDIR`). In
  read-only containers set `MPLCONFIGDIR` to a writable path and bake fonts into the image to avoid
  a slow first render on every new container.

### Plotly

- A Plotly figure is a JSON spec. `fig.to_html(full_html=False, include_plotlyjs=...)` emits a
  `<div>` plus a script; `include_plotlyjs=True` inlines the multi-megabyte plotly.js bundle,
  `"cdn"` references a public CDN, `False` assumes you load it yourself.
- `fig.to_image(format="png")` / `fig.write_image(...)` requires Kaleido; Kaleido launches a
  headless browser process per export (or reuses one), so static export is far heavier than
  Matplotlib and adds a browser to the worker image `[VERIFY: Kaleido process model]`.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Library | Matplotlib / Plotly / native format charts | Static reports (PDF, DOCX, email): Matplotlib. Interactive HTML: Plotly. XLSX: native chart. Avoid Plotly+Kaleido just to produce PNGs if Matplotlib can. |
| Raster vs vector | PNG / SVG | SVG for PDF/HTML (sharp at any zoom, small for line/bar charts). PNG for email/DOCX and for scatter plots with many thousands of points (SVG becomes huge). |
| Resolution | dpi 96 / 150 / 300 | 150 dpi is enough for screen and office printing; 300 for print-quality packs. File size grows with dpi². |
| Where data is computed | In SQL / in Python | Aggregate in SQL to the chart's granularity; Python only fills gaps and formats. |
| Chart vs table | Chart only / chart + table | Always keep the underlying numbers available (table, appendix or XLSX sheet) for accessibility and audit. |
| Render location | Inline in generation task / separate chart task | Inline for a few charts per report. Separate cached step when the same chart appears in many reports (e.g. one tenant KPI chart in every recipient's weekly pack). |

## 5. Implementation patterns

### 5.1 Chart dataset from the same aggregation as the table

```python
from datetime import date
from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import TruncMonth

def monthly_revenue_series(*, tenant, start: date, end: date, tz) -> list[tuple[date, Decimal]]:
    rows = (Invoice.objects
            .filter(tenant=tenant, status="paid",
                    issued_at__gte=start_dt(start, tz), issued_at__lt=start_dt(end, tz))
            .annotate(month=TruncMonth("issued_at", tzinfo=tz))
            .values("month")
            .annotate(revenue=Sum("total", default=Decimal("0")))
            .order_by("month"))
    by_month = {r["month"].date(): r["revenue"] for r in rows}
    return [(m, by_month.get(m, Decimal("0"))) for m in month_starts(start, end)]   # fill gaps
```

`start_dt` (local date → aware datetime) and `month_starts` are small helpers from the report's
time utilities ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)).
**Gap filling** matters: without it, a month with no sales disappears and a line chart draws a
misleading straight line across it. Use this same series for both the table and the chart.

### 5.2 Matplotlib with the OO API (no pyplot)

```python
import io
from matplotlib.figure import Figure                 # [third-party: matplotlib]
from matplotlib.ticker import FuncFormatter

def revenue_chart_png(series, *, currency: str, title: str) -> bytes:
    months = [m for m, _ in series]
    values = [float(v) for _, v in series]           # float for drawing only; tables use Decimal
    fig = Figure(figsize=(8, 3.5), dpi=150, layout="constrained")
    ax = fig.add_subplot()
    ax.bar(months, values, width=20, color="#1f77b4", label="Revenue")
    ax.set_title(title)
    ax.set_ylabel(f"Revenue ({currency})")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="upper left", frameon=False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", metadata={"Software": None})
    return buf.getvalue()
```

- `Decimal → float` is acceptable **only for drawing**; bar heights at pixel precision don't need
  exact decimals. Never feed chart floats back into totals.
- `layout="constrained"` (Matplotlib ≥ 3.6 `[VERIFY: version that added layout="constrained" to Figure]`) avoids clipped labels without
  `tight_layout` guesswork.
- No `plt.close` needed: the `Figure` is not tracked by pyplot and is freed when it goes out of scope.

### 5.3 Time-series axis formatting

```python
import matplotlib.dates as mdates

ax.xaxis.set_major_locator(mdates.MonthLocator())
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
```

- Plot **local period starts** (dates), not UTC timestamps, so a bar labelled "Oct 2026" contains
  October in the tenant's zone.
- Label units and currency on the axis; state the period and `data_as_of` in the caption.
- For long ranges use `mdates.AutoDateLocator()` + `ConciseDateFormatter` rather than one tick per
  point.
- Start bar-chart value axes at zero; truncated axes exaggerate differences.

### 5.4 Deterministic output

Reproducible charts let you hash artifacts and use image snapshot tests.

```python
fig.savefig(buf, format="png", metadata={"Software": None})                       # PNG
fig.savefig(buf, format="svg", metadata={"Date": None})                            # SVG
fig.savefig(buf, format="pdf", metadata={"CreationDate": None, "Producer": None})  # PDF
```

Also: set `matplotlib.rcParams["svg.hashsalt"]` to a fixed string so SVG element IDs are stable
`[VERIFY: svg.hashsalt rcParam]`; pin the Matplotlib version and bundle fonts in the image
(different FreeType or font versions change pixels); sort series and categories explicitly. The
`SOURCE_DATE_EPOCH` environment variable is honoured for some formats `[VERIFY: Matplotlib SOURCE_DATE_EPOCH support]`.

### 5.5 Embedding

```python
# WeasyPrint HTML template context: inline SVG/PNG as data URI (no network fetch)
import base64
context["revenue_chart"] = "data:image/png;base64," + base64.b64encode(png).decode()
```

```html
<figure>
  <img src="{{ revenue_chart }}" alt="Monthly revenue, Jan–Oct 2026; peak 182,400 EUR in August.">
  <figcaption>Revenue by month (paid invoices, Europe/Berlin, data as of 2026-11-01 06:00).</figcaption>
</figure>
```

- python-docx: `doc.add_picture(io.BytesIO(png), width=Inches(6))`.
- XlsxWriter image fallback: `ws.insert_image("H2", "chart.png", {"image_data": io.BytesIO(png)})`;
  prefer a native chart ([excel-report-generation.md](excel-report-generation.md)).
- Email: attach the PNG with a Content-ID and reference `cid:` in HTML; see
  [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md).

### 5.6 Plotly for a web report page

```python
import plotly.graph_objects as go                    # [third-party: plotly]

def revenue_chart_html(series) -> str:
    fig = go.Figure(go.Bar(x=[m.isoformat() for m, _ in series],
                           y=[float(v) for _, v in series], name="Revenue"))
    fig.update_layout(title="Revenue by month", yaxis_title="Revenue (EUR)")
    return fig.to_html(full_html=False, include_plotlyjs=False)   # plotly.js served from static
```

Rendered into a Django template with `{{ chart_html|safe }}` — acceptable only because the HTML is
generated by Plotly from data you control; titles and labels containing user input must still be
escaped by Plotly `[VERIFY: Plotly escaping of trace names/titles in to_html]`. Keep CSP in mind:
inline scripts need a nonce ([django-security-engineering — cors-and-browser-security](../../django-security-engineering/references/cors-and-browser-security.md)).

## 6. Production use cases

| Report | Chart | Approach |
|---|---|---|
| Weekly sales pack (PDF + email) | Revenue by region bar, 12-week trend line | Matplotlib PNG/SVG, rendered once per tenant per week, reused by all recipients. |
| Monthly financial statement (PDF) | Usually none, or a small cash-flow trend | Regulated statements favour tables; chart is supplementary and labelled. |
| Inventory dashboard export (XLSX) | Stock by warehouse | Native XLSX chart on the summary sheet. |
| Operational daily report (email) | Orders per hour sparkline | Small PNG inline; numbers also in the email body. |
| Customer portal analytics page | Interactive usage trends | Plotly HTML or front-end library over a JSON endpoint. |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Matplotlib (Agg) | Mature, headless, deterministic with care, PNG/SVG/PDF output, no browser | Default styling dated; pyplot global state pitfalls; first-run font cache cost |
| Plotly HTML | Interactive, good defaults, same spec for web and notebooks | Requires JS; large bundle; CSP considerations; not for email/PDF |
| Plotly static via Kaleido | One spec for web and static | Browser dependency in worker image; heavy and slower per image |
| Native XLSX charts | Live with data, editable by users | Rendering differs by spreadsheet app; limited styling |
| ReportLab graphics | Pure-Python, vector in PDF | Separate verbose API; PDF-only |

## 8. Security considerations

- **Charts leak data like tables do.** A chart image in a cached or shared artifact exposes
  aggregates (e.g. one-employee departments reveal salaries). Apply the same authorisation and
  minimum group-size rules as the tables
  ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)).
- **No remote resources.** Embed images as data URIs or local files; a PDF renderer fetching chart
  URLs is an SSRF surface ([pdf-report-generation.md](pdf-report-generation.md)).
- **User text in labels** (customer or product names) must be escaped where the output is HTML
  (Plotly) and must not be interpreted as markup: Matplotlib treats `$...$` as mathtext — escape `$`
  in user-controlled labels or disable via `usetex=False` and escaping `[VERIFY: Matplotlib mathtext escaping behaviour]`.
- **Public CDNs** for plotly.js add a third-party dependency and a privacy/integrity concern;
  self-host and use SRI if a CDN is unavoidable.
- **Hosted chart images for email** must be behind expiring signed URLs, not public buckets.

## 9. Performance considerations

- Typical Matplotlib PNG renders take tens to hundreds of milliseconds and tens of MB transiently;
  Kaleido exports are typically much slower and heavier `[VERIFY: measure in your environment]`.
  Benchmark in the worker image before budgeting.
- Chart datasets must be small: aggregate in SQL (`Trunc*` + `Sum`), downsample long time series
  (e.g. daily → weekly) before plotting. Plotting 100k raw points is slow and unreadable.
- Render shared charts once and reuse the bytes (artifact or cache keyed by tenant, chart key,
  parameters hash and `data_as_of`). See [report-generation-performance.md](report-generation-performance.md).
- Memory: use the OO API; if pyplot is unavoidable, `plt.close(fig)` in `finally`; set Celery
  `worker_max_memory_per_child` / `max_tasks_per_child` as a safety net, not as the fix
  ([django-performance — memory-and-resource-management](../../django-performance/references/memory-and-resource-management.md)).
- Import Matplotlib lazily inside chart code so web processes that never render charts don't pay
  its import time and memory.

## 10. Common mistakes and anti-patterns

- A separate, slightly different query for the chart than for the table — totals disagree.
- Using `pyplot` in a web/Celery process without closing figures (memory growth, "More than 20
  figures" warnings), or sharing a pyplot figure across threads.
- Relying on an interactive backend (`TkAgg`) in a container — import errors or hangs.
- Plotly HTML in emails or PDFs (blank charts — no JS).
- Missing months/days not filled → misleading lines.
- Colour as the only differentiator; red/green pairs; unlabeled axes; no units/currency.
- Plotting UTC-bucketed data under local labels.
- Rendering the same chart per recipient instead of once per dataset.
- Using `float` totals from chart data in table figures.

## 11. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| Worker memory climbs per report | pyplot figures not closed | OO `Figure` API or `plt.close(fig)`. |
| `TclError`/display errors | GUI backend selected | `MPLBACKEND=Agg`. |
| First report after deploy is slow | Font cache build | Pre-build cache in image; `MPLCONFIGDIR` writable. |
| Squares instead of characters | Font lacks glyphs (CJK, Arabic) | Bundle a font with coverage and set `rcParams["font.family"]`. |
| Blank chart in PDF | Remote image blocked by url_fetcher, or Plotly HTML (JS) used | Data URI/local file; static image. |
| Blank chart in email | Remote image blocked by client | Inline CID attachment. |
| `write_image` fails in worker | Kaleido/Chrome missing | Install per Kaleido docs, or switch to Matplotlib. |
| Snapshot test flaky | Non-deterministic metadata, font/version drift | Strip metadata, pin versions, compare with tolerance. |

Validation: unit-test the **chart dataset** (gap filling, ordering, totals equal to the table) —
that is where correctness lives. For the image, assert it is a valid PNG/SVG of expected
dimensions and non-trivial size; use image-diff with tolerance (`[third-party: pytest-mpl]` or
pixel comparison) only for a few key charts. See
[report-generation-testing.md](report-generation-testing.md).

### Accessibility checklist

- [ ] Meaningful `alt` text summarising the insight (not "chart.png"); caption with period, scope,
      units and `data_as_of`.
- [ ] Underlying data available as a table or XLSX sheet.
- [ ] Colour-blind-safe palette (e.g. Okabe–Ito, Matplotlib `tab10`/`cividis`) and a second
      encoding (pattern, marker, direct labels) where series must be distinguished.
- [ ] Sufficient contrast, ≥ 9 pt text at final size, axis labels with units.
- [ ] PDF accessibility of images depends on the PDF engine's tagging support — see
      [pdf-report-generation.md](pdf-report-generation.md).

## 12. Related references and official documentation

- [excel-report-generation.md](excel-report-generation.md) — native XLSX charts
- [pdf-report-generation.md](pdf-report-generation.md) — embedding images, url_fetcher, PDF accessibility
- [docx-and-document-generation.md](docx-and-document-generation.md) — images in DOCX
- [report-templates-and-reusability.md](report-templates-and-reusability.md) — shared chart components and theming
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — aggregation, time zones, gap filling inputs
- [report-generation-performance.md](report-generation-performance.md) — chart cost and reuse
- [report-generation-testing.md](report-generation-testing.md) — chart tests and visual regression
- [django-performance — memory-and-resource-management](../../django-performance/references/memory-and-resource-management.md)
- [django-security-engineering — cors-and-browser-security](../../django-security-engineering/references/cors-and-browser-security.md)
- <https://matplotlib.org/stable/>
- <https://matplotlib.org/stable/gallery/user_interfaces/web_application_server_sgskip.html>
- <https://matplotlib.org/stable/users/explain/figure/backends.html>
- <https://plotly.com/python/>
- <https://plotly.com/python/static-image-export/>
- <https://plotly.com/python/interactive-html-export/>
- <https://www.w3.org/WAI/tutorials/images/complex/>
