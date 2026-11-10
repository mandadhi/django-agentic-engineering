# Columnar, Archive and Document Files: Parquet, Avro, ORC, ZIP, PDF

> **Scope:** Structure and safe, memory-bounded processing of Parquet, Avro and ORC (streaming
> reads/writes, schema handling and evolution, pushdown), ZIP archives (member iteration and
> limits) and PDF (text/table extraction, encrypted and scanned documents, structured extraction
> with confidence). **Not here:** CSV/Excel/JSON/XML/plain text and fixed-width →
> [file-processing-engineering.md](file-processing-engineering.md); batch sizing and parallelism →
> [large-dataset-processing.md](large-dataset-processing.md); ZIP bombs/path traversal/malicious
> documents in depth → [data-processing-security.md](data-processing-security.md); the end-to-end
> document extraction architecture →
> [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md).

## 1. Purpose and scope

These formats appear at the edges of Django systems: data-lake exports (Parquet), event/CDC
streams landed to files (Avro), Hadoop/Hive-era warehouses (ORC), partners bundling many files
(ZIP), and humans sending documents (PDF). They share one property that text formats lack —
**random access and internal structure** — which you exploit for speed (read only the columns
and row groups you need) and must respect for safety (archives and PDFs are containers that can
hide large or hostile content). This reference covers how to read and write them in bounded
memory and how to turn them into `(locator, record)` streams compatible with the parser
registry in [file-processing-engineering.md](file-processing-engineering.md#51-format-detection-and-parser-registry).

## 2. Core concepts

| Format | Shape | Schema | Typical use in Django systems |
|---|---|---|---|
| Parquet | Columnar; file → row groups → column chunks → pages; footer metadata with per-chunk statistics | Embedded (Thrift footer; Arrow schema often stored as key-value metadata) | Analytical exports, data-lake hand-off, large staging snapshots |
| Avro | Row-oriented object container file: header (schema JSON + codec + sync marker), then blocks of records | Embedded writer schema; reader schema resolution rules | Event streams, CDC dumps, Kafka-adjacent integrations |
| ORC | Columnar; file → stripes (index, data, footer) → streams; file footer with stripe stats | Embedded | Legacy Hive/Hadoop exports |
| ZIP | Local file headers + central directory at the end | None | Bundles of CSVs/PDFs, XLSX (itself a ZIP) |
| PDF | Object graph: pages, content streams, fonts, xref table, optional encryption | None (presentation format) | Invoices, licences, remittance advices, lab reports |

Key vocabulary:
- **Projection (column) pushdown**: read only requested columns — free in columnar formats.
- **Predicate pushdown**: skip row groups/stripes whose min/max statistics cannot match a filter.
  Coarse-grained: it skips whole row groups, not individual rows; rows still need filtering.
- **Schema evolution**: adding/removing/renaming/promoting fields across file versions.
- **Extraction confidence**: for PDFs, a per-field score that decides automatic acceptance vs
  human review.

## 3. Internal mechanics

### 3.1 Parquet
A reader first reads the **footer** (last bytes: metadata length + `PAR1` magic), giving the
schema, row-group offsets and column-chunk statistics (min, max, null count). Each column chunk
is compressed (Snappy, Zstd, Gzip) and dictionary/RLE encoded. Reading 3 of 80 columns touches
only those chunks — I/O drops proportionally. Memory is dominated by **one row group × selected
columns** after decompression; a writer that produced 2 GB row groups forces readers to hold
them, so row-group size is a producer decision with consumer consequences (128 MB–1 GB is a
common target; VERIFY your engine's defaults). Logical types matter: `DECIMAL(p,s)` maps to
`decimal128` and Python `Decimal`; `TIMESTAMP(isAdjustedToUTC=true)` is UTC-instant, `false`
is a naive local datetime.

### 3.2 Avro
The container header holds the writer's schema as JSON. Records are binary-encoded **without
field names** — decoding is impossible without the schema, and field order is defined by it.
A *reader schema* may differ: fields are matched by name (and aliases); added fields need
defaults; type promotions (int→long→float→double, string↔bytes) are allowed. Blocks are
separated by a 16-byte sync marker, which allows splitting and some corruption recovery.
`fastavro.reader` [third-party: fastavro] iterates records lazily, one block in memory at a time.

### 3.3 ORC
Stripes (default ~64 MB, VERIFY) each contain index streams (row-index min/max per 10k rows),
data streams per column and a stripe footer. `pyarrow.orc.ORCFile` [third-party: pyarrow] exposes
`nstripes` and `read_stripe(i, columns=...)`, giving natural bounded batches.

### 3.4 ZIP
The authoritative member list is the **central directory** at the end of the file.
`zipfile.ZipFile.infolist()` [stdlib] reads it without decompressing anything, exposing
`filename`, `file_size` (declared uncompressed), `compress_size`, `flag_bits` (bit 0 = encrypted)
and `external_attr` (Unix mode, which can mark symlinks). Declared sizes are attacker-controlled
— enforce limits while reading too, not only from metadata. `zf.open(info)` returns a streaming
decompressor; `extractall` writes to disk with whatever names the archive chose.

### 3.5 PDF
A PDF is an object graph; text is drawn by content-stream operators placing glyphs at
coordinates. "Text extraction" reconstructs reading order from glyph positions — columns,
tables and rotated text are heuristics. Scanned PDFs contain only images: no text layer exists,
so extraction returns empty strings and OCR is required. Encrypted PDFs may open without a
password (owner-password only, permission flags) or require a user password.

## 4. Engineering decisions and trade-offs

### 4.1 Columnar readers

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Stream one large Parquet file row-wise into Django | `pq.ParquetFile.iter_batches(batch_size, columns)` | Need filters across many files → `pyarrow.dataset` |
| Directory/partitioned dataset (Hive `date=2026-11-01/`) with filters | `pyarrow.dataset` with `filter=` | Simple single file → `ParquetFile` |
| Analytical transform/aggregation, then load summary | Polars `scan_parquet` lazy + filter/select | Per-row business validation with rich errors |
| Avro container files | `fastavro.reader` (+ `reader_schema` for evolution) | Schema registry wire format (Kafka) → integration skill |
| ORC | `pyarrow.orc.ORCFile.read_stripe` | Producer can emit Parquet instead |
| Produce Parquet export | `pq.ParquetWriter` incremental batches | Tiny exports for spreadsheet users → CSV/XLSX |

### 4.2 Archives and documents

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Partner sends ZIP of CSVs | Iterate `infolist()`, stream allowed members via `zf.open` | `extractall` to shared disk |
| Nested ZIPs | Allow depth ≤ 1, apply limits cumulatively | Unbounded recursion |
| Born-digital PDF text | PyMuPDF (fast; **AGPL-3.0 or commercial** licence) | Licence unacceptable → pypdf [third-party: pypdf] (BSD) or pdfplumber [third-party: pdfplumber] (MIT) |
| Tables in born-digital PDF | pdfplumber `extract_tables` or PyMuPDF `find_tables` | Layout varies per vendor → template per vendor or ML extraction service |
| Scanned PDF | OCR (Tesseract via pytesseract [third-party: pytesseract], or a cloud OCR service) | High-stakes fields → human review always |
| Encrypted PDF | Fail with clear code unless a password is part of the contract | Brute-forcing / stripping protection |

## 5. Django/Python implementation patterns

### 5.1 Parquet: footer inspection and streaming batches

```python
import pyarrow as pa                     # [third-party: pyarrow]
import pyarrow.parquet as pq
from collections.abc import Iterator
from typing import Any, BinaryIO
from parsing.registry import FileFormatError   # see file-processing-engineering.md §5.1

REQUIRED = {"employee_id": pa.string(), "gross_pay": pa.decimal128(12, 2)}

def inspect_parquet(src: BinaryIO) -> pq.ParquetFile:
    pf = pq.ParquetFile(src)
    schema = pf.schema_arrow
    for name, typ in REQUIRED.items():
        if name not in schema.names:
            raise FileFormatError("missing_columns", f"Parquet lacks column {name!r}")
        if not schema.field(name).type.equals(typ):
            raise FileFormatError("schema_mismatch", f"{name}: {schema.field(name).type} != {typ}")
    md = pf.metadata
    if md.num_rows > 50_000_000:
        raise FileFormatError("too_many_records", f"{md.num_rows} rows")
    return pf

def iter_parquet(src: BinaryIO, *, batch_size: int = 10_000) -> Iterator[tuple[str, dict[str, Any]]]:
    pf = inspect_parquet(src)
    offset = 0
    for batch in pf.iter_batches(batch_size=batch_size, columns=list(REQUIRED)):
        for i, row in enumerate(batch.to_pylist()):      # list[dict]; Decimal preserved
            yield f"row {offset + i}", row
        offset += batch.num_rows
```

`iter_batches` still decompresses whole column chunks of a row group internally; `batch_size`
bounds the Python objects you create, not the Arrow buffers (VERIFY: memory profile with your
files' row-group sizes). Row-group statistics are available via
`pf.metadata.row_group(i).column(j).statistics` (`min`, `max`, `null_count`; may be `None` if the
writer did not record them).

### 5.2 Partitioned datasets with pushdown

```python
import datetime as dt
import pyarrow.dataset as ds

def iter_ledger_rows(root: str, since: dt.date) -> Iterator[pa.RecordBatch]:
    dataset = ds.dataset(root, format="parquet", partitioning="hive")   # date=YYYY-MM-DD/...
    yield from dataset.to_batches(
        columns=["txn_id", "account", "amount", "booked_at"],
        filter=(ds.field("date") >= since) & (ds.field("status") == "POSTED"),
        batch_size=50_000,
    )
```

The partition filter prunes directories before any file is opened; the `status` filter uses
row-group statistics where available, then filters rows. Remote roots need a filesystem object
(`pyarrow.fs.S3FileSystem`, or fsspec-compatible) — credentials are an integration concern, see
[../../django-integrations/references/file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md).

Polars equivalent for analytical work (lazy; pushdown applied by the optimiser):

```python
import polars as pl                     # [third-party: polars]

summary = (
    pl.scan_parquet("s3://lake/payroll/*.parquet")        # (VERIFY: storage_options for your setup)
      .filter(pl.col("pay_date") >= pl.date(2026, 10, 1))
      .group_by("cost_centre")
      .agg(pl.col("gross_pay").sum())
      .collect()          # streaming engine flag differs by version (VERIFY: engine="streaming")
)
```

### 5.3 Streaming Parquet writes

```python
def write_parquet_export(rows: Iterator[dict[str, Any]], dst: BinaryIO, schema: pa.Schema,
                         *, chunk: int = 50_000) -> int:
    written = 0
    with pq.ParquetWriter(dst, schema, compression="zstd") as writer:
        buf: list[dict[str, Any]] = []
        for row in rows:
            buf.append(row)
            if len(buf) >= chunk:
                writer.write_table(pa.Table.from_pylist(buf, schema=schema))  # one row group per call
                written += len(buf); buf.clear()
        if buf:
            writer.write_table(pa.Table.from_pylist(buf, schema=schema)); written += len(buf)
    return written
```

Pass an explicit schema: inference from the first batch makes a column `null`-typed if the first
50k values are `None`, and the next batch then fails. Very small chunks create many tiny row
groups that hurt readers — aim for row groups of at least tens of MB. Write to a temp object
and rename/commit only on success so consumers never see a half-written file (footer missing =
unreadable file).

### 5.4 Avro read with schema evolution, and streaming write

```python
import fastavro                          # [third-party: fastavro]

READER_SCHEMA = fastavro.parse_schema({
    "type": "record", "name": "Customer", "namespace": "crm.v3",
    "fields": [
        {"name": "customer_id", "type": "string"},
        {"name": "email", "type": ["null", "string"], "default": None},
        {"name": "segment", "type": "string", "default": "UNKNOWN"},   # added in v3
    ],
})

def iter_avro(src: BinaryIO) -> Iterator[tuple[str, dict[str, Any]]]:
    try:
        reader = fastavro.reader(src, reader_schema=READER_SCHEMA)
    except (ValueError, fastavro.read.SchemaResolutionError) as exc:  # (VERIFY: exception names)
        raise FileFormatError("avro_schema", str(exc)) from exc
    for idx, record in enumerate(reader):
        yield f"record {idx}", record

def write_avro(records: Iterator[dict[str, Any]], dst: BinaryIO) -> None:
    fastavro.writer(dst, READER_SCHEMA, records, codec="deflate")   # consumes the iterator lazily
```

`reader.writer_schema` exposes the embedded schema — log its fingerprint on the ingestion batch
so lineage records which producer schema was read. Incompatible evolution (renamed field without
alias, removed field without default in the reader) fails at reader construction: a **format
error**, never a row error. Wider policy on schema evolution across pipeline runs:
[etl-and-elt-pipelines.md](etl-and-elt-pipelines.md).

### 5.5 ORC by stripe

```python
import pyarrow.orc as orc

def iter_orc(path: str, columns: list[str]) -> Iterator[pa.RecordBatch]:
    f = orc.ORCFile(path)
    for i in range(f.nstripes):
        yield from f.read_stripe(i, columns=columns).to_batches()   # (VERIFY: return type per version)
```

### 5.6 ZIP: safe member iteration, no `extractall`

```python
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 1_000
    max_member_bytes: int = 500 * 1024**2
    max_total_bytes: int = 2 * 1024**3
    max_ratio: float = 100.0
    allowed_suffixes: frozenset[str] = frozenset({".csv", ".xlsx", ".pdf", ".jsonl"})

def iter_zip_members(fh: BinaryIO, limits: ZipLimits) -> Iterator[tuple[str, BinaryIO]]:
    try:
        zf = zipfile.ZipFile(fh)
    except zipfile.BadZipFile as exc:
        raise FileFormatError("zip_invalid", "Not a readable ZIP archive.") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > limits.max_entries:
            raise FileFormatError("zip_too_many_entries", f"{len(infos)} entries")
        if sum(i.file_size for i in infos) > limits.max_total_bytes:
            raise FileFormatError("zip_too_large", "Declared uncompressed size exceeds limit")
        seen: set[str] = set()
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or info.filename in seen:
                raise FileFormatError("zip_bad_path", f"Unsafe member name {info.filename!r}")
            seen.add(info.filename)
            if info.flag_bits & 0x1:
                raise FileFormatError("zip_encrypted", f"{info.filename} is encrypted")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise FileFormatError("zip_symlink", f"{info.filename} is a symlink")
            if name.suffix.lower() not in limits.allowed_suffixes or name.name.startswith("."):
                continue                                     # __MACOSX/, .DS_Store, unknowns
            if info.compress_size and info.file_size / info.compress_size > limits.max_ratio:
                raise FileFormatError("zip_ratio", f"{info.filename} compression ratio too high")
            with zf.open(info) as raw:
                yield info.filename, BoundedReader(raw, limits.max_member_bytes)
```

`BoundedReader` is a small `io.RawIOBase` wrapper that counts bytes returned by `read()` and
raises `FileFormatError("zip_member_too_large")` past the cap — declared `file_size` can lie, so
the cap must be enforced on actual decompressed bytes. Members are consumed sequentially and must
be fully processed before the next is opened. **Nested archives**: an XLSX member is itself a
ZIP; openpyxl will open it — apply ZIP limits to it too (cumulative budget), and reject `.zip`
members unless the contract allows depth 1. Some consumers (openpyxl) need a seekable stream:
copy the bounded member to a `tempfile.SpooledTemporaryFile` [stdlib] first.

### 5.7 PDF text, tables, encryption, scanned pages

```python
import pymupdf                           # [third-party: pymupdf] AGPL-3.0 or commercial licence

MAX_PAGES = 50

@dataclass(frozen=True, slots=True)
class PageText:
    page: int
    text: str
    has_text_layer: bool
    image_count: int

def extract_pdf_pages(data: bytes) -> Iterator[PageText]:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:                          # pymupdf raises its own FileDataError
        raise FileFormatError("pdf_invalid", "Not a readable PDF.") from exc
    with doc:
        if doc.needs_pass:                            # user password required
            raise FileFormatError("pdf_encrypted", "PDF is password protected.")
        if doc.page_count > MAX_PAGES:
            raise FileFormatError("pdf_too_many_pages", f"{doc.page_count} pages")
        for page in doc:
            text = page.get_text("text")
            yield PageText(page.number + 1, text, bool(text.strip()), len(page.get_images()))

def extract_tables(data: bytes, page_no: int) -> list[list[list[str | None]]]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        tabs = doc[page_no - 1].find_tables()         # (VERIFY: available in your PyMuPDF version)
        return [t.extract() for t in tabs.tables]
```

`doc.is_encrypted` is true even for owner-password-only files that open fine; `needs_pass` is the
"cannot read without a password" check. A page with no text layer but with images is a scanned
page → route to OCR. Licence-safe alternatives: pypdf `PdfReader(stream).pages[i].extract_text()`
with `reader.is_encrypted`; pdfplumber `pdfplumber.open(fp)` → `page.extract_tables()`
(VERIFY: exact signatures for your versions). Parse PDFs in a subprocess or a worker with a
memory/time limit — malformed PDFs can make native parsers hang or crash.

### 5.8 Structured extraction with confidence and human review

```python
from decimal import Decimal, InvalidOperation
import re

@dataclass(frozen=True, slots=True)
class ExtractedField:
    name: str
    value: str | None
    confidence: float           # 0..1, from method + validation outcome
    page: int
    method: str                 # "text_regex" | "table" | "ocr"

INVOICE_TOTAL = re.compile(r"Total\s+due[:\s]+(?:EUR|€)?\s*([\d.,]+)", re.I)

def extract_invoice_total(pages: list[PageText]) -> ExtractedField:
    for p in pages:
        if m := INVOICE_TOTAL.search(p.text):
            raw = m.group(1).replace(".", "").replace(",", ".")      # vendor uses 1.234,56
            try:
                Decimal(raw)
                conf = 0.95 if p.has_text_layer else 0.6
            except InvalidOperation:
                conf = 0.2
            return ExtractedField("total", raw, conf, p.page, "text_regex" if p.has_text_layer else "ocr")
    return ExtractedField("total", None, 0.0, 0, "text_regex")

def needs_review(fields: list[ExtractedField], threshold: float = 0.9) -> bool:
    return any(f.value is None or f.confidence < threshold for f in fields)
```

Confidence combines extraction method (text layer > table > OCR) with validation outcome
(parses as Decimal, line items sum to total, VAT number checksum). Below threshold → a review
task with the page image and extracted values side by side; reviewer corrections are stored as
the accepted values *and* as training/feedback data. Validation rules themselves live in
[data-validation-and-cleansing.md](data-validation-and-cleansing.md).

## 6. Production use cases

- **Payroll data-lake hand-off (Parquet).** Payroll provider drops monthly Parquet per legal
  entity; `inspect_parquet` enforces `decimal128(12,2)` for pay columns (float columns are a
  contract breach → format error); `iter_batches(columns=...)` feeds row-wise reconciliation.
- **CRM change events (Avro).** Nightly Avro files from the CRM; reader schema v3 adds
  `segment` with default; writer-schema fingerprint stored on `IngestionBatch` for lineage.
- **Inventory snapshot from a legacy Hive warehouse (ORC).** Stripe-by-stripe read, convert to
  Parquet once, then all downstream jobs read Parquet.
- **Hospital doctor credentialing bundle (ZIP of PDFs + CSV manifest).** Manifest CSV parsed
  first; each PDF licence certificate extracted, licence number regex + issuing-board code
  validated; low confidence → credentialing officer review queue.
- **Supplier invoices (PDF, mixed born-digital and scanned).** Per-vendor extraction templates;
  scanned pages routed to OCR; totals cross-checked against line items; never auto-post below
  confidence threshold.
- **Bulk export for a customer's analysts.** `ParquetWriter` streams 40M order rows to object
  storage, Zstd compression, explicit schema with `decimal128` amounts and UTC timestamps.

## 7. Advantages and disadvantages

| Format / tool | Advantages | Disadvantages |
|---|---|---|
| Parquet | Column/predicate pushdown, compression, typed Decimal/timestamps | Not human-readable; row-wise appends impossible; row-group sizing matters |
| Avro | Embedded schema, evolution rules, splittable | Row-oriented (no column pruning); needs schema discipline |
| ORC | Good compression, stripe stats | Smaller Python ecosystem; prefer Parquet for new work |
| ZIP | Universal bundling | Bombs, traversal, lying metadata; sequential processing |
| PyMuPDF | Fast, rich (text, tables, images) | AGPL licence; native code attack surface |
| pypdf / pdfplumber | Permissive licences, pure Python (pypdf) | Slower; weaker layout handling |
| OCR | Unlocks scanned documents | Error-prone; needs review; CPU heavy |

## 8. Security and performance considerations

**Security** (depth in [data-processing-security.md](data-processing-security.md)):
- ZIP: cap entries, total and per-member decompressed bytes (enforced on actual bytes), ratio,
  nesting depth; reject absolute/`..` names, symlinks, encrypted members; never `extractall`
  untrusted archives (if you must extract a tar, use `extractall(filter="data")`, Python ≥ 3.12).
- PDF: parse in an isolated worker with time and memory limits; cap pages and bytes; never
  execute embedded JavaScript/attachments; do not render untrusted PDFs in the main web process.
- Columnar files: footer metadata can declare absurd sizes — check `num_rows`, `num_row_groups`
  and schema width before reading data.
- Extracted PII (licence numbers, bank details from invoices) follows the redaction and
  retention policy; raw PDFs live in private storage with access-controlled downloads.
- Licence compliance: AGPL for PyMuPDF applies to network-served use — get a legal decision
  before adopting it (VERIFY: with your legal team).

**Performance** (depth in [data-processing-performance.md](data-processing-performance.md)):
- Select columns always; filters on partition keys first, statistics second.
- `batch.to_pylist()` creates Python objects per cell — the dominant cost for row-wise loading;
  keep work in Arrow/Polars when the destination is analytical or `COPY`.
- Remote Parquet reads issue range requests: a footer read plus one request per column chunk;
  many small files are slower than fewer large ones.
- PDF extraction is CPU-bound — scale with processes, not threads or async.

## 9. Common mistakes and anti-patterns

- `pq.read_table()` on a 20 GB file → OOM → `iter_batches` with `columns`.
- Expecting predicate pushdown to filter rows → wrong counts → still apply the filter to returned rows (dataset API does; manual statistics checks do not).
- Letting writers infer schema from the first batch → `null` column type, later batch fails → explicit `pa.Schema`.
- Writing tiny row groups (1k rows) → slow downstream reads → ≥ tens of MB per row group.
- Avro field renamed without alias → reader fails on all historic files → aliases + defaults; compatibility check in CI.
- `zipfile.extractall()` on uploads → path traversal, disk exhaustion → iterate and stream members.
- Trusting `ZipInfo.file_size` → bomb passes checks → count real decompressed bytes.
- Treating `is_encrypted` as unreadable → rejects valid owner-protected PDFs → use `needs_pass`.
- Empty text from a PDF treated as "no data" → scanned invoices silently skipped → detect missing text layer, route to OCR/review.
- Auto-posting OCR amounts → financial errors → confidence threshold + cross-field checks + human review.
- Adopting PyMuPDF without a licence review → AGPL obligations → decide early; pypdf/pdfplumber alternatives.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `ArrowInvalid: Parquet magic bytes not found` | Truncated/half-written file | Writer must finalise footer; write-then-rename |
| Memory spikes despite small `batch_size` | Huge row groups | Inspect `metadata.row_group(i).total_byte_size`; ask producer to reduce |
| Filter does not speed anything up | No statistics, or column unsorted | Check `statistics` is not `None`; sort/partition by filter key |
| Amounts arrive as float | Producer wrote `double` | Schema check rejects; contract requires `DECIMAL` |
| Avro `SchemaResolutionError` | Incompatible reader/writer schemas | Compare `reader.writer_schema`; add default/alias |
| ZIP member count = 0 after filtering | `__MACOSX/` resource forks, wrong suffix | Log skipped names; review allowed suffixes |
| PDF text in wrong order | Multi-column layout | `get_text("blocks")`/`"words"` with coordinates; per-vendor template |
| PDF text empty | Scanned document | Check images on page; OCR path |

Validation checklist:
- [ ] Footer/header schema validated before reading data; numeric money types are Decimal.
- [ ] Columns projected; partition/predicate filters applied.
- [ ] Writers use explicit schemas, sensible row-group size, write-then-commit.
- [ ] ZIP limits enforced on real bytes; no `extractall`; nested depth bounded.
- [ ] PDF page/byte caps, `needs_pass` check, scanned-page detection, confidence-based review.
- [ ] Licence decision recorded for PDF libraries.

## 11. Related references and skills

- [file-processing-engineering.md](file-processing-engineering.md) — text/spreadsheet formats, parser registry.
- [large-dataset-processing.md](large-dataset-processing.md) — batch sizing, PyArrow/Polars streaming.
- [data-processing-security.md](data-processing-security.md) — bombs, traversal, malicious documents.
- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — validating extracted fields.
- [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md) — schema evolution across runs.
- [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md) — document extraction pipeline.
- [../../django-integrations/references/file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md).
- [../../django-security-engineering/references/file-upload-security.md](../../django-security-engineering/references/file-upload-security.md).
- [../../django-performance/references/memory-and-resource-management.md](../../django-performance/references/memory-and-resource-management.md).

## 12. Official documentation and external references

- https://arrow.apache.org/docs/python/parquet.html
- https://arrow.apache.org/docs/python/dataset.html
- https://arrow.apache.org/docs/python/orc.html
- https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html
- https://docs.pola.rs/api/python/stable/reference/api/polars.scan_parquet.html
- https://fastavro.readthedocs.io/en/latest/reader.html
- https://fastavro.readthedocs.io/en/latest/writer.html
- https://docs.python.org/3/library/zipfile.html
- https://docs.python.org/3/library/tarfile.html#extraction-filters
- https://docs.python.org/3/library/tempfile.html
- https://pymupdf.readthedocs.io/en/latest/
- https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
