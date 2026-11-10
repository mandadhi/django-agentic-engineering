# CSV and Structured Report Generation

> **Scope:** Generating CSV, JSON, JSON Lines and XML report outputs from Django — the `csv` module, streaming CSV responses, encoding and BOM, dialects and delimiters, quoting, newlines, serialization of dates and decimals, deterministic serialization, schema validation, and the canonical `safe_cell()` formula-injection helper used by every spreadsheet-bound export. **Not here:** XLSX ([excel-report-generation.md](excel-report-generation.md)), download headers and storage ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)), query design ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)), *parsing* incoming CSV/JSON/XML ([django-data-processing — file-processing-engineering](../../django-data-processing/references/file-processing-engineering.md)).

## 1. Purpose and scope

Structured formats are the workhorses of reporting: CSV for "give me the data", JSON/JSONL for
system consumers and APIs, XML for regulators, banks and legacy systems that publish an XSD. Their
engineering problems are not rendering but **contract fidelity**:

- every consumer must parse the same bytes the same way (encoding, delimiter, quoting, newlines);
- values must round-trip without loss (`Decimal`, dates, nulls, leading zeros);
- outputs must be reproducible for the same parameters and data snapshot;
- spreadsheet-bound outputs must not carry executable formulas.

These formats are also the only ones that can be **streamed end to end** with bounded memory, which
makes them the default for large exports.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Dialect | Delimiter, quote char, escaping, line terminator, quoting policy. `[stdlib]` `csv` defaults to the `excel` dialect: `,` delimiter, `"` quote, `\r\n` terminator, `QUOTE_MINIMAL`. |
| RFC 4180 | Informational spec for CSV: CRLF line breaks, `"` quoting, doubled quotes inside quoted fields. Many consumers deviate. |
| BOM | `EF BB BF` at file start. Signals UTF-8 to Excel on Windows; confuses some parsers that don't expect it. |
| Column contract | Ordered column names, types, formats, nullability and meaning — published and versioned with the report definition. |
| JSON Lines (JSONL/NDJSON) | One JSON document per line; streamable and appendable, unlike a single JSON array. |
| Canonical serialization | A byte-stable representation: sorted keys, fixed separators, fixed number/date formats, stable row order. Enables hashing and diffing of reports. |
| Formula injection | A cell value interpreted as a formula by spreadsheet software when the CSV is opened (OWASP "CSV Injection"). |

## 3. Internal mechanics

### The `csv` writer `[stdlib]`

`csv.writer(fileobj)` calls `fileobj.write(str)` once per `writerow()` and returns that call's
return value. That detail enables streaming: give it a pseudo-buffer whose `write()` returns the
line instead of storing it.

- Files must be opened with `newline=""`; otherwise on Windows the `\r\n` terminator becomes
  `\r\r\n`. The writer emits the dialect's `lineterminator` itself.
- Embedded newlines inside a field are legal in quoted CSV, but many line-based tools break on them.
  Decide per contract: keep (and quote) or replace with a space.
- Quoting constants: `QUOTE_MINIMAL` (default), `QUOTE_ALL`, `QUOTE_NONNUMERIC`, `QUOTE_NONE`,
  and **(Python ≥ 3.12)** `QUOTE_STRINGS` (quote all strings, never `None`) and `QUOTE_NOTNULL`
  (quote everything except `None`). With these two, `None` is written as an empty unquoted field,
  letting consumers distinguish null (`,,`) from empty string (`,"",`).
- `QUOTE_NONNUMERIC` quotes everything that is not an `int`/`float`; `Decimal` is not a float, so
  it is quoted `[VERIFY: csv QUOTE_NONNUMERIC handling of Decimal]` — another reason to format
  numbers explicitly.

### Streaming through Django

`StreamingHttpResponse` `[Django]` iterates a generator and sends each chunk; the response has no
`Content-Length`, and middleware that reads `response.content` cannot be used on it. The database
side streams through `QuerySet.iterator(chunk_size=...)` `[Django]`, which on PostgreSQL uses a
server-side cursor (unless `DISABLE_SERVER_SIDE_CURSORS` is set, e.g. under transaction-pooling
PgBouncer, in which case rows are still fetched in chunks client-side but the whole result is
materialised by the driver `[VERIFY: Django docs — iterator() with server-side cursors disabled]`).
Django ≥ 4.2 also accepts an **async** iterator for `StreamingHttpResponse` under ASGI.

### JSON `[stdlib]` and Django's encoder

`json.dumps` rejects `Decimal`, `datetime`, `date` and `UUID`. `DjangoJSONEncoder` `[Django]`
serialises `Decimal` as a **string**, datetimes as ISO 8601 (with microseconds truncated to
milliseconds `[VERIFY: DjangoJSONEncoder datetime precision]`), dates as `YYYY-MM-DD`, UUIDs as
strings. Strings for money are deliberate: JSON numbers are commonly parsed as binary floats by
consumers.

### XML `[stdlib]`

- `xml.etree.ElementTree` builds a tree in memory — fine for documents up to tens of MB.
- `xml.sax.saxutils.XMLGenerator` writes events (`startElement`, `characters`, `endElement`)
  directly to a stream and escapes text — the streaming option.
- The standard library does **not** validate against XSD. Use `[third-party: lxml]`
  (`lxml.etree.XMLSchema`) or `[third-party: xmlschema]`.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Format | CSV / JSONL / JSON / XML | Humans with Excel → CSV (or XLSX). Machine consumers → JSONL (streamable) or JSON (small). Regulator/bank with XSD → XML. |
| Encoding | UTF-8 / UTF-8 with BOM / legacy code page | UTF-8 always. Add BOM only for "open in Excel" exports; omit for machine feeds. Never cp1252 unless a contract demands it — and then fail on unencodable characters rather than replace silently. |
| Delimiter | `,` / `;` / TAB | Comma by contract. Excel in locales with decimal comma expects `;` when opening by double-click `[VERIFY: Excel list-separator behaviour]` — prefer XLSX for those users rather than locale-specific CSV variants. |
| Number format | `1234.50` / `1.234,50` | Always machine format: `.` decimal separator, no thousands separator, fixed scale. Locale formatting belongs in XLSX/PDF. |
| Datetime format | ISO 8601 with offset / local naive | ISO 8601 with offset for machine feeds (`2026-10-31T23:00:00+00:00`); for human CSV, local time **plus** a documented zone column or header. |
| Null representation | empty / `NULL` / `\N` | Empty field (plus `QUOTE_STRINGS`/`QUOTE_NOTNULL` to distinguish `""`) unless a loader needs `\N`. Document it. |
| Formula injection | Escape / don't escape | Escape (`safe_cell`) for anything a human may open in a spreadsheet. For machine feeds offer a separate, explicitly named "raw" format with no escaping and restricted permission. |
| JSON shape | One array / JSONL / envelope with metadata | JSONL for large; small JSON may use an envelope `{"meta": {...}, "rows": [...]}`. |
| Validation | None / schema check in tests / schema check on every run | Contracted external deliveries (regulatory XML, partner JSON): validate every artifact before delivery. Internal CSV: contract tests. |

## 5. Implementation patterns

### 5.1 Canonical `safe_cell()` (owned here)

```python
# reports/formatting.py
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")

def safe_cell(value):
    """Neutralise spreadsheet formula injection for user-controlled strings.

    Only strings are changed; numbers, dates and None are returned untouched so they stay typed.
    The value is prefixed with a single quote, which spreadsheets treat as "text".
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
```

Trade-offs to state in the column contract:

- The output is **altered** (`'-5% discount`). Non-spreadsheet consumers see the apostrophe. That is
  why machine feeds get a separate, unescaped format.
- Apply it to strings only. Never stringify numbers first: `-12.50` as a number is safe; as the
  string `"-12.50"` it would be prefixed and become text.
- OWASP also suggests quoting every field; with the `csv` module `QUOTE_ALL` does that, but quoting
  alone does not stop Excel evaluating formulas `[VERIFY: OWASP CSV Injection current guidance]`.
- Lookalike characters (full-width `＝`) are handled inconsistently by spreadsheet apps — consider
  normalising with `unicodedata.normalize("NFKC", value)` before the check if the data source is
  untrusted `[VERIFY: spreadsheet handling of full-width prefixes]`.

### 5.2 Streaming CSV download

```python
import csv
from django.http import StreamingHttpResponse

from reports.formatting import safe_cell

class Echo:
    """Pseudo-buffer: csv.writer calls write(); we hand the line back to the generator."""
    def write(self, value: str) -> str:
        return value

def stream_orders_csv(queryset, *, tz, filename: str) -> StreamingHttpResponse:
    rows = queryset.values_list(
        "reference", "customer__name", "status", "created_at", "total"
    ).order_by("created_at", "pk").iterator(chunk_size=2000)

    writer = csv.writer(Echo(), quoting=csv.QUOTE_STRINGS)   # Python ≥ 3.12

    def generate():
        yield "﻿"                                         # BOM: Excel-targeted export only
        yield writer.writerow(["Reference", "Customer", "Status", "Created (local)", "Total"])
        for ref, customer, status, created_at, total in rows:
            yield writer.writerow([
                safe_cell(ref), safe_cell(customer), status,
                created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                f"{total:.2f}",                               # Decimal, fixed scale, no float
            ])

    response = StreamingHttpResponse(generate(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
```

- `queryset` must already be the **scoped** selector result (`reports.selectors` / the report
  spec's selector); this function never decides who sees what.
- `order_by(..., "pk")` gives deterministic order (ties broken by PK).
- Use `django.utils.http.content_disposition_header` (Django ≥ 4.2) or `FileResponse` for non-ASCII
  filenames — see [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).
- Behind nginx, disable proxy buffering for the location or send `X-Accel-Buffering: no`; check the
  app server's worker timeout (Gunicorn sync default 30 s `[VERIFY: Gunicorn --timeout default for sync workers]`) — a slow stream is killed
  mid-file and the user receives a truncated CSV with HTTP 200. For exports that can exceed the
  timeout, generate in a task ([background-report-generation.md](background-report-generation.md)).
- Errors after the first chunk cannot change the status code. Optionally end every complete file
  with a trailer row (`# rows=N`) only if the contract allows it; otherwise rely on the task-based
  path for exports whose completeness must be verifiable.

### 5.3 Writing CSV to a file in a task

```python
import csv
import io
import tempfile

from reports.formatting import safe_cell

def write_csv_artifact(rows, header) -> tuple[tempfile.SpooledTemporaryFile, int]:
    tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")  # [stdlib]
    text = io.TextIOWrapper(tmp, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(text)
    writer.writerow(header)
    count = 0
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
        count += 1
    text.detach()            # keep tmp open after the wrapper goes away
    tmp.seek(0)
    return tmp, count
```

Compute `sha256` and `size_bytes` while uploading and store them on
`ReportArtifact`; the row count goes to `ReportRun.row_count`.

### 5.4 Value serialization rules

| Type | CSV | JSON |
|---|---|---|
| `Decimal` money | `f"{v:.2f}"` after quantizing with the documented rounding mode | string `"1234.50"` (or integer minor units `123450` by contract) |
| `date` | `YYYY-MM-DD` | `"YYYY-MM-DD"` |
| aware `datetime` | ISO 8601 with offset, or local + zone column | ISO 8601 with offset |
| `None` | empty field | `null` |
| `bool` | `true`/`false` (contract), not Python `True` | `true`/`false` |
| choices | stored code **and** label columns if humans read it | code (labels are presentation) |
| IDs with leading zeros | string (CSV is untyped, but Excel will strip zeros on open — another reason for XLSX for humans) | string |

### 5.5 Deterministic JSON and JSONL

```python
import json
from django.core.serializers.json import DjangoJSONEncoder

def canonical_json(obj) -> bytes:
    return json.dumps(
        obj, cls=DjangoJSONEncoder, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")

def jsonl_lines(rows):
    for row in rows:                 # dicts from .values().iterator()
        yield canonical_json(row) + b"\n"
```

Same snapshot + same parameters + same code version → identical bytes → identical `sha256`. That
enables duplicate-artifact detection and simple regression tests. Determinism also requires a total
order on rows and no "generated at" timestamp inside the payload (put it in artifact metadata or a
separate `meta` object excluded from the hash).

### 5.6 Streaming XML for a regulatory submission

```python
from xml.sax.saxutils import XMLGenerator       # [stdlib]

def write_payments_xml(stream, payments, *, period: str) -> None:
    gen = XMLGenerator(stream, encoding="utf-8", short_empty_elements=True)
    gen.startDocument()
    gen.startElement("PaymentReport", {"period": period, "version": "2"})
    for p in payments:                           # .iterator()
        gen.startElement("Payment", {"id": str(p.pk)})
        for tag, value in (("Invoice", p.invoice.number),
                           ("Amount", f"{p.amount:.2f}"),
                           ("ReceivedAt", p.received_at.isoformat())):
            gen.startElement(tag, {}); gen.characters(value); gen.endElement(tag)
        gen.endElement("Payment")
    gen.endElement("PaymentReport")
    gen.endDocument()
```

`p.invoice.number` implies `select_related("invoice")` on the queryset — otherwise N+1.
`stream` must be a binary file object. `XMLGenerator` escapes `&`, `<`, `>` in text and attribute
values; characters illegal in XML 1.0 (most C0 control characters) are **not** removed — strip them
before writing or the document is invalid.

### 5.7 Schema validation before delivery

```python
from lxml import etree                           # [third-party: lxml]

_SCHEMA = etree.XMLSchema(etree.parse("reports/schemas/payment_report_v2.xsd"))

def validate_payment_xml(path: str) -> None:
    doc = etree.parse(path, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    _SCHEMA.assertValid(doc)                     # raises DocumentInvalid with line info
```

For JSON contracts, `[third-party: jsonschema]` (`jsonschema.validate(instance, schema)` or a
pre-built validator class for repeated use). Store schemas in the repository, versioned with the
`ReportSpec.version`; a schema change is a report version change.

## 6. Production use cases

| Use case | Format | Key choices |
|---|---|---|
| Ad-hoc order export from an admin screen | CSV, streamed | BOM, `safe_cell`, local times with zone in header, `.iterator()`. |
| Nightly finance feed to a data warehouse | JSONL or CSV without BOM, no escaping ("raw" format), gzip | Canonical serialization, sha256 manifest, schema/contract tests, restricted permission. |
| Bank payment or tax-authority submission | XML validated against the published XSD | Streaming `XMLGenerator`, validation gate before delivery, artifact retained per policy. |
| Partner API bulk statement | JSON with envelope (`meta`, `rows`) | Money as strings, ISO dates, versioned schema. |
| Payroll export to an external payroll provider | Provider-specified CSV dialect | Exact dialect from contract (delimiter, encoding, date format), contract test with provider sample. |

## 7. Advantages and disadvantages

| Format | Advantages | Disadvantages |
|---|---|---|
| CSV | Universal; truly streamable; tiny overhead; easy diff | Untyped; dialect ambiguity; Excel mangles values (zeros, dates, long numbers); injection surface |
| JSON | Typed-ish, nested; universal in APIs | Single document not streamable for consumers; numbers ambiguous for money |
| JSONL | Streamable both ways; appendable; resumable | Less familiar; no top-level metadata unless a header line is agreed |
| XML | Strong schemas (XSD); mandated by many regulators | Verbose; XXE risks when parsed (consumers); stdlib can't validate |

## 8. Security considerations

- **Formula injection** — `safe_cell()` on every user/external string in human-facing CSV; see
  [reporting-security-and-compliance.md](reporting-security-and-compliance.md) for the threat model.
- **Data minimisation** — exports are the cheapest way to exfiltrate a whole table. Columns come
  from the report definition (allow-list), never from a client-supplied field list without
  validation against that allow-list.
- **Raw (unescaped) machine formats** carry the injection risk to whoever opens them; restrict them
  to service accounts / specific permissions and name them clearly.
- **XML** — you generate, you don't parse, but when you validate with lxml disable entity
  resolution and network access (as above). Never build XML with string concatenation.
- **Filenames** — never interpolate raw user input into `Content-Disposition`; see
  [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).
- **Logs** — never log rows or the payload; log row counts and hashes.

## 9. Performance considerations

- The hot path is usually ORM instance creation: use `values_list()`/`values()` with `iterator()`.
- `csv.writer` cost is small; per-row Python formatting (`strftime`, `astimezone`) dominates — do
  time-zone conversion with a precomputed `ZoneInfo` object; consider formatting in SQL for very
  large feeds.
- Yield reasonably sized chunks: one `yield` per row is acceptable; batching ~100 rows into one
  string reduces per-chunk overhead in the WSGI server `[VERIFY: measurable benefit depends on server]`.
- Compress large machine feeds (`gzip` `[stdlib]`) in the task, not in the request.
- A streaming response holds a web worker and a DB connection (and an open server-side cursor
  inside a transaction) for its full duration — budget concurrency accordingly
  ([report-generation-performance.md](report-generation-performance.md)).

## 10. Common mistakes and anti-patterns

- Building `rows = list(queryset)` before writing, defeating streaming.
- Opening CSV files without `newline=""` (blank lines between rows on Windows).
- Writing `str(Decimal)` without fixing scale (`1E+2`, `100.0000`) or converting to `float`.
- Locale-formatted numbers (`1.234,50`) in a machine CSV.
- Naive datetimes with no zone stated; mixing UTC and local in one file.
- Adding a BOM to feeds consumed by programs that don't strip it (first header becomes `﻿id`).
- Escaping numbers (stringify then `safe_cell`) — negatives become text.
- Unordered querysets — two runs over identical data produce different files.
- Generating XML via f-strings; forgetting to strip control characters.
- Using `json.dumps(default=str)` for everything — silently stringifies unexpected types.
- Streaming exports that silently truncate on worker timeout.

## 11. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| `Ã©` instead of `é` in Excel | UTF-8 without BOM opened as legacy code page | BOM for Excel-targeted CSV, or deliver XLSX. |
| First column named `﻿reference` in consumer | BOM sent to a machine consumer | Remove BOM from machine feeds. |
| Blank line after every row | File opened without `newline=""` | Fix the open call. |
| Download ends early, HTTP 200 | Worker timeout / proxy timeout during stream | Move to task-generated artifact; raise timeouts only for a measured need. |
| Leading zeros / long numbers mangled | Excel type inference on open | Document "import as text" or offer XLSX with text cells. |
| Values starting with `'` in machine import | `safe_cell` applied to a machine feed | Use the raw format for machines. |
| XSD validation fails on special chars | Control characters / wrong encoding declaration | Strip illegal chars; keep `encoding="utf-8"` consistent. |

Validation in tests: parse the produced bytes with `csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))`
and assert header, row count, typed parsing of each column, and injection escaping; for JSON assert
`json.loads` + schema; for XML assert XSD validity. For streaming responses, consume
`b"".join(response.streaming_content)`. Details: [report-generation-testing.md](report-generation-testing.md).

## 12. Related references and official documentation

- [excel-report-generation.md](excel-report-generation.md) — XLSX alternative for human consumers
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) — headers, filenames, streaming responses
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — iteration, ordering, Decimal, time zones
- [report-generation-performance.md](report-generation-performance.md) — streaming vs buffering
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — formula injection threat model, data minimisation
- [report-generation-testing.md](report-generation-testing.md) — file-content validation
- [django-data-processing — data-import-and-export-systems](../../django-data-processing/references/data-import-and-export-systems.md)
- [django-data-processing — file-processing-engineering](../../django-data-processing/references/file-processing-engineering.md)
- [django-performance — file-and-data-processing-performance](../../django-performance/references/file-and-data-processing-performance.md)
- <https://docs.python.org/3/library/csv.html>
- <https://docs.python.org/3/library/json.html>
- <https://docs.python.org/3/library/xml.sax.utils.html>
- <https://docs.djangoproject.com/en/5.2/howto/outputting-csv/>
- <https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects>
- <https://docs.djangoproject.com/en/5.2/topics/serialization/#djangojsonencoder>
- <https://www.rfc-editor.org/rfc/rfc4180>
- <https://jsonlines.org/>
- <https://lxml.de/validation.html>
- <https://python-jsonschema.readthedocs.io/>
- <https://owasp.org/www-community/attacks/CSV_Injection>
