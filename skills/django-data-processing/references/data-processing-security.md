# Data Processing Security

> **Scope:** Security controls specific to processing files and datasets: upload handling for
> imports, type/MIME sniffing, size limits, archive and ZIP-bomb defences, path traversal,
> malicious documents, XXE, CSV/Excel formula injection, sensitive-data handling, temp files,
> access-controlled downloads, masking/redaction, retention, secure deletion, tenant isolation in
> tasks and processing permissions. **Not here:** general upload, authZ, crypto and logging
> principles ([django-security-engineering](../../django-security-engineering/SKILL.md):
> [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md),
> [multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md),
> [secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md));
> format parsing details ([file-processing-engineering.md](file-processing-engineering.md),
> [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md)).

## 1. Purpose and scope

Data-processing systems accept files from customers, partners and integrations, parse them with
complex libraries, hold large volumes of personal and financial data in staging, and emit exports
that are opened in spreadsheet software. Each of these is an attack or leak surface:

| Surface | Threat |
|---|---|
| Upload endpoint | Oversized files (DoS), disguised executables, storage exhaustion |
| Parser | XXE, billion laughs, ZIP bombs, decompression memory blow-up, library CVEs |
| Archive extraction | Path traversal ("zip slip"), symlinks, overwrite of app files |
| Document content | Macros (xlsm), PDF JavaScript, embedded files |
| Export | CSV/formula injection executing in a victim's spreadsheet |
| Staging / temp / error reports | PII copies with no retention or access control |
| Background tasks | Cross-tenant data mixing when tenant context is lost |
| Downloads | Guessable or long-lived URLs to sensitive exports |

The goal: **treat every input file as hostile, every intermediate copy as sensitive, and every
output as something that will be opened by a privileged person.**

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Content sniffing | Determining type from magic bytes / structure, not filename or client `Content-Type` |
| Allow-list | Accept only listed (extension, sniffed type) pairs per import type |
| Decompression ratio | Uncompressed ÷ compressed size; very high ratios indicate bombs |
| Zip slip | Archive member names like `../../settings.py` or `/etc/x` escaping the target directory |
| XXE | XML external entity resolving local files or URLs during parsing |
| Billion laughs | Nested entity expansion consuming exponential memory |
| Formula injection | Cell starting with `=`, `+`, `-`, `@`, tab or CR interpreted as formula on open |
| Data classification | PUBLIC / INTERNAL / CONFIDENTIAL / RESTRICTED (PII, bank, health) driving controls |
| Masking | Irreversible or partial display transform (`****1234`) for viewers |
| Redaction | Removing values from logs, errors, reports |
| Crypto-shredding | Deleting the encryption key so ciphertext copies (backups, versions) become unreadable |

## 3. Internal mechanics

### 3.1 Django upload path
Django streams request bodies through upload handlers: files ≤ `FILE_UPLOAD_MAX_MEMORY_SIZE`
stay in memory (`InMemoryUploadedFile`), larger ones spill to a temp file in
`FILE_UPLOAD_TEMP_DIR` (`TemporaryUploadedFile`) [Django]. `DATA_UPLOAD_MAX_MEMORY_SIZE` limits the
non-file body and **does not limit file size**; `DATA_UPLOAD_MAX_NUMBER_FILES` limits file count.
Django has no built-in max file size — enforce it at the proxy (`client_max_body_size` in nginx,
equivalent in load balancers) and in your form/serializer, and per job before parsing.

### 3.2 Why extension/Content-Type checks fail
Both are attacker-controlled. XLSX, DOCX and JAR are all ZIP containers (`PK\x03\x04`); CSV has no
magic number at all. Correct validation is layered: magic bytes → container structure (does the
ZIP contain `[Content_Types].xml` and `xl/workbook.xml`?) → successful bounded parse.

### 3.3 Archive bombs
A 42 KB ZIP can expand to petabytes via nested archives; a single DEFLATE member can reach ratios
~1000:1, and overlapping-entry constructions bypass naive per-entry checks. Declared
`file_size` in the ZIP central directory can lie — so enforce limits on **bytes actually read**,
not only declared sizes. XLSX parsing decompresses XML parts, so it is subject to the same risk.

### 3.4 XML entity expansion
The stdlib `xml.*` parsers are documented as not secure against maliciously constructed data;
`defusedxml` [third-party: defusedxml] wraps them and forbids DTDs/entities. lxml can be configured
with `resolve_entities=False, no_network=True` and without `huge_tree`; default entity behaviour
changed across lxml 5.x (VERIFY: defaults of your lxml version). openpyxl uses defusedxml if it is
installed (VERIFY: openpyxl docs for current behaviour).

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Customer CSV/XLSX import | Allow-list types, sniff, size cap, parse in worker with limits | Accepting xlsm/xlsb unless business needs macros-bearing files (it doesn't for data) |
| Partner ZIP drops | Inspect `infolist()`, bounded `zf.open()` reads, no extraction to disk | `extractall()` on untrusted archives |
| XML feeds | defusedxml or hardened lxml `iterparse` | `xml.etree` / `minidom` on untrusted input |
| PDF documents | Parse in isolated worker (container, memory/CPU limits, no network) | Parsing in web process; rendering with JS enabled |
| Malware scanning | AV/sandbox service for files later downloaded by others | Treating AV as sufficient (parsers still need limits) |
| CSV exports | Neutralise formula prefixes by default | Disabling neutralisation globally because one consumer complained |
| Large PII exports | Async generation + short-lived signed URL + audit | Emailing files as attachments |
| Temp files | `tempfile` in a private, non-web dir, deleted in `finally` | Writing under `MEDIA_ROOT` or static paths |

### Magic-byte libraries

| Option | Notes |
|---|---|
| Manual signature check | Zero dependencies; enough for small allow-lists (ZIP, PDF, Parquet `PAR1`, gzip) |
| `filetype` [third-party: filetype] | Pure Python, reads first bytes (VERIFY: supported types list) |
| `python-magic` [third-party: python-magic] | Wraps libmagic; needs system library (VERIFY: Windows installation requirements) |

## 5. Django/Python implementation patterns

### 5.1 Layered file validation for imports

```python
import zipfile                                     # [stdlib]
from django.core.exceptions import ValidationError # [Django]

ALLOWED = {
    ".csv": "text",
    ".xlsx": "xlsx",
    ".parquet": "parquet",
}
MAX_BYTES = {"text": 200 * 2**20, "xlsx": 50 * 2**20, "parquet": 500 * 2**20}

def sniff(fh, ext: str) -> str:
    kind = ALLOWED.get(ext.lower())
    if kind is None:
        raise ValidationError("File type not allowed.")
    head = fh.read(8); fh.seek(0)
    if kind == "xlsx":
        if not head.startswith(b"PK\x03\x04"):
            raise ValidationError("Not an XLSX file.")
        with zipfile.ZipFile(fh) as zf:
            names = set(zf.namelist())
            if "xl/workbook.xml" not in names:
                raise ValidationError("Not an XLSX workbook.")
            if "xl/vbaProject.bin" in names:
                raise ValidationError("Macro-enabled workbooks are not accepted.")
        fh.seek(0)
    elif kind == "parquet" and head[:4] != b"PAR1":
        raise ValidationError("Not a Parquet file.")
    elif kind == "text" and b"\x00" in fh.read(64 * 1024):
        raise ValidationError("Binary content in a text file.")
    fh.seek(0)
    return kind

def validate_upload(upload) -> str:
    ext = "." + upload.name.rsplit(".", 1)[-1] if "." in upload.name else ""
    kind = sniff(upload.file, ext)
    if upload.size > MAX_BYTES[kind]:
        raise ValidationError("File too large.")
    return kind
```

Store the upload under a server-generated key (`imports/<tenant>/<uuid>`), never the client
filename; keep the original name only as metadata (sanitised for display). Per-tenant quotas and
per-job row limits are a second size control: a 50 MB CSV can still hold 5 M rows.

### 5.2 Safe archive member iteration (no extraction)

```python
import zipfile
from pathlib import PurePosixPath

MAX_ENTRIES, MAX_TOTAL, MAX_RATIO, CHUNK = 1000, 2 * 2**30, 100, 1 << 20

def iter_members(path: str, allowed_suffixes: set[str]):
    total = 0
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError("Too many entries")
        for info in infos:
            p = PurePosixPath(info.filename)
            if info.is_dir() or p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                continue  # or reject the whole archive — safer for partner feeds
            if p.suffix.lower() not in allowed_suffixes:
                continue
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise ValueError(f"Suspicious compression ratio: {info.filename}")
            def bounded(info=info):
                nonlocal total
                with zf.open(info) as src:
                    while chunk := src.read(CHUNK):
                        total += len(chunk)
                        if total > MAX_TOTAL:
                            raise ValueError("Archive expands beyond limit")
                        yield chunk
            yield info.filename, bounded()
```

Counting bytes actually read defends against lying headers. Nested archives: do not recurse
automatically; if needed, apply the same limits with a depth cap of 1. For tar:
`extractall(filter="data")` (Python ≥ 3.12) blocks absolute paths, links outside, device files.
Format details → [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md).

### 5.3 XML

```python
from defusedxml.ElementTree import iterparse      # [third-party: defusedxml]
# or, hardened lxml:
from lxml import etree                            # [third-party: lxml]

def iter_records(src):
    for _ev, elem in etree.iterparse(src, events=("end",), tag="Record",
                                     resolve_entities=False, no_network=True,
                                     load_dtd=False, huge_tree=False):
        yield {c.tag: c.text for c in elem}
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
```

(VERIFY: `iterparse` keyword support for `load_dtd`/`no_network` in your lxml version.) Reject
documents containing `<!DOCTYPE` if your schema never needs one — the simplest robust rule.

### 5.4 Malicious documents
- **xlsm/xlsb/xls with macros:** reject for data import (see 5.1). openpyxl does not execute
  macros, but the file will be stored and possibly downloaded by staff.
- **PDF:** may contain JavaScript, launch actions, embedded files, malformed xref tables that hang
  parsers. Extract text in an isolated worker (dedicated queue, container with memory/CPU limits,
  no outbound network, read-only FS except a temp dir), with page-count and time limits; never
  re-serve the original to other users without AV scanning. PyMuPDF is AGPL-3.0
  [third-party: pymupdf] — licence review required; pypdf/pdfplumber are permissive alternatives.
- Apply `soft_time_limit`/`time_limit` on parser tasks (signal-based; VERIFY: unsupported on Windows and some pools)
  and OS-level limits (cgroups/container memory) as the real backstop.

### 5.5 CSV/Excel formula injection

```python
from decimal import Decimal   # [stdlib]

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

def neutralise(value: object) -> object:
    """OWASP CSV-injection mitigation for exported cells."""
    if isinstance(value, str) and value.startswith(_DANGEROUS):
        return "'" + value
    return value

def test_neutralise() -> None:
    assert neutralise("=HYPERLINK(\"http://x\",\"y\")") == "'=HYPERLINK(\"http://x\",\"y\")"
    assert neutralise("@SUM(A1)") == "'@SUM(A1)"
    assert neutralise("-12.50") == "'-12.50"   # string! export numbers as numbers, not str
    assert neutralise(Decimal("-12.50")) == Decimal("-12.50")
    assert neutralise("Alice") == "Alice"
```

Neutralising `-` breaks negative numbers serialised as strings, so keep numeric columns typed
(Decimal/int) up to the writer. OWASP also recommends quoting every field (`csv.QUOTE_ALL`)
(VERIFY: current OWASP guidance wording). For XLSX written with openpyxl, a string starting with `=`
may be stored as a formula — set the cell `data_type` to string or neutralise (VERIFY: openpyxl
cell-type inference). **On import**, values like `=1+1` are data: store as text, never evaluate,
and neutralise again on any re-export.

### 5.6 Temporary files

```python
import os, tempfile                               # [stdlib]
from django.conf import settings
from django.core.files.storage import default_storage   # [Django]

def with_temp_copy(storage_key: str, process) -> None:
    # PROCESSING_TMP: private dir (0700), on a volume with quota; not under MEDIA_ROOT/STATIC_ROOT
    fd, path = tempfile.mkstemp(dir=settings.PROCESSING_TMP, suffix=".bin")  # 0600 on POSIX
    try:
        with os.fdopen(fd, "wb") as out, default_storage.open(storage_key, "rb") as src:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                out.write(chunk)
        process(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
```

A scheduled sweeper deletes files older than N hours in `PROCESSING_TMP` (workers killed by OOM
skip `finally`). On Windows, `mkstemp` permissions follow ACLs, not mode bits (VERIFY: effective ACLs on your host).

### 5.7 Access-controlled downloads

```python
from django.http import FileResponse, Http404, HttpResponseRedirect  # [Django]
from django.contrib.auth.decorators import login_required

@login_required
def download_export(request, job_id):
    job = ExportJob.objects.filter(pk=job_id, tenant=request.tenant).first()   # tenant check
    if job is None or job.status != "SUCCEEDED":
        raise Http404
    if not request.user.has_perm("exports.download_exportjob", job):          # object-level
        raise Http404
    AuditEvent.objects.create(action="export_download", actor=request.user, object_ref=str(job.pk))
    # Option A: short-lived signed URL (S3/Azure) — 5 min expiry
    url = job.file.storage.url(job.file.name)   # django-storages: expiry via settings (VERIFY: setting names)
    return HttpResponseRedirect(url)
    # Option B: stream through Django: FileResponse(job.file.open("rb"), as_attachment=True)
```

Return 404 (not 403) for other tenants' jobs to avoid enumeration. Signed URLs are bearer tokens:
keep expiry short, ensure the bucket is private, set `Content-Disposition: attachment`. Object
storage specifics → [file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md).

### 5.8 Redaction and masking

```python
import logging, re

_PATTERNS = [(re.compile(r"\b\d{2}-?\d{2}-?\d{2}\b"), "<sort-code>"),
             (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "<iban>"),
             (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>")]

def redact(text: str) -> str:
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text

def mask_account(acct: str) -> str:
    return "****" + acct[-4:] if len(acct) > 4 else "****"

class RedactingFilter(logging.Filter):            # [stdlib]
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage()); record.args = ()
        return True
```

Regex redaction is a safety net, not the control. The control is **field-level policy**: the
column spec classifies fields (`classification="RESTRICTED"`), and `RejectedRecord.payload`,
`ProcessingFailure.error_message`, reconciliation `field_diffs` and error-report exports keep only
the fields needed to fix the row, masking RESTRICTED values. DB driver errors echo values
(`Key (email)=(x@y.com) already exists`) — redact before persisting or logging.

### 5.9 Tenant isolation in tasks

```python
@shared_task(bind=True, acks_late=True)           # [third-party: celery]
def import_rows(self, tenant_id: int, job_id: str) -> None:
    job = ImportJob.objects.select_related("tenant").get(pk=job_id, tenant_id=tenant_id)
    with tenant_context(job.tenant):              # your helper: sets contextvar for managers/logging
        ...  # every query filters tenant_id; storage keys prefixed f"tenants/{tenant_id}/"
```

Pass IDs, not objects or querysets; re-fetch with **both** `pk` and `tenant_id` so a forged or
mixed-up job id cannot cross tenants. Contextvar-based tenant state must be reset per task (workers
reuse threads/processes). Never cache lookups keyed only by code (`"SKU-1"`) — include tenant.

## 6. Production use cases

- **Payroll file import (HR admin uploads XLSX):** xlsx allow-list, macro rejection, 20 MB cap,
  parse in `imports` queue with memory limit; salary and bank columns RESTRICTED → masked in
  preview, excluded from error report except masked last 4; permission `payroll.import_payroll`
  plus approval by a second user (`payroll.approve_import`) before promotion.
- **Hospital doctor roster CSV from agency:** contains registration numbers and phone numbers;
  retention 30 days for raw upload, 90 days for rejected rows; downloads of error reports only by
  the uploading tenant's roster admins.
- **Partner ZIP of invoices (PDF + XML):** archive inspected without extraction, XML through
  defusedxml, PDFs in isolated extractor with 60 s limit and 200-page cap.
- **Customer export to CSV for marketing:** formula neutralisation on, email masked unless user
  has `customers.export_pii`; link valid 10 min; every download audited.
- **Attendance biometric data:** biometric templates never enter the processing pipeline — only
  punch events with device id and badge id (data minimisation).

### Processing permissions matrix (example)

| Action | Permission | Notes |
|---|---|---|
| Upload/import entity X | `<app>.import_<entity>` | Per entity type; tenant admin grants |
| Approve/promote import | `<app>.approve_import` | Must differ from uploader for financial data |
| View error report | `<app>.view_importjob` + same tenant | Masked by classification |
| Download raw upload | `<app>.download_source` | Rare; audited |
| Replay/compensate | `<app>.reprocess_job` | Ops/engineering only, audited |

## 7. Advantages and disadvantages

| Control | Advantage | Cost / limitation |
|---|---|---|
| Content sniffing | Stops disguised files cheaply | CSV unsniffable — rely on parse limits |
| No-extraction archive reading | Removes zip-slip class entirely | Some libraries need a real path (temp file needed) |
| Isolated parser workers | Contains parser exploits and resource exhaustion | Extra infrastructure, queue routing |
| Formula neutralisation | Protects spreadsheet users | Alters string values; consumers may object |
| Signed short-lived URLs | Offloads bandwidth, no long-lived links | Bearer token; logged by proxies; revocation hard |
| Crypto-shredding | Deletes data in backups/versions | Requires per-tenant/per-dataset keys from the start |

## 8. Security and performance considerations

**Security**
- Layer limits: proxy body size → Django view/serializer → per-job row/byte limits → OS/container
  limits on workers.
- Keep parser libraries patched; they are the most exposed code you run.
- **Retention:** scheduled purge for raw uploads, staging rows, `RejectedRecord`, error-report
  files, exports, temp dirs — per classification and contract. Implement as a periodic task that
  deletes by `created_at < cutoff` in batches and records counts in an audit event; also configure
  object-storage lifecycle rules as a backstop.
- **Secure deletion:** on object storage with versioning, `delete()` creates a delete marker —
  prior versions remain until lifecycle expiry; backups retain data too (VERIFY: your bucket
  versioning/lifecycle and backup retention). For guaranteed erasure, encrypt sensitive datasets
  with per-tenant keys (KMS / Key Vault) and destroy the key (crypto-shredding). Overwriting files
  on SSD/cloud storage gives no guarantee.
- Never store uploads or exports under `MEDIA_ROOT` served publicly by the web server.
- Audit: who uploaded, approved, downloaded, reprocessed — see
  [security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md).

**Performance**
- Sniffing reads a few KB — negligible; ZIP central-directory inspection is O(entries).
- Byte-counting bounded reads cost little compared with parsing.
- Isolated workers add queue latency; route only risky formats (PDF, archives) there.
- Redaction regexes on every log line cost CPU at high log volume — prefer structured logging that
  never includes payload fields, and use the regex filter only as a backstop.
- Retention purges: delete in keyset batches to avoid long locks and WAL spikes
  ([large-dataset-processing.md](large-dataset-processing.md)).

## 9. Common mistakes and anti-patterns

- Trusting `upload.content_type` → executable accepted as CSV → sniff bytes + allow-list.
- Assuming `DATA_UPLOAD_MAX_MEMORY_SIZE` caps file size → multi-GB uploads → proxy + explicit size check.
- `zipfile.extractall()` on partner archives → zip slip / bomb → bounded `zf.open()` reads.
- `xml.etree.ElementTree` on untrusted XML → entity expansion DoS → defusedxml / hardened lxml.
- Exporting user-entered strings raw → formula injection in finance's Excel → neutralise.
- Logging full rows on validation failure → PII in log aggregation for years → log row number + field name.
- Temp files under `MEDIA_ROOT` → publicly downloadable staging data → private `PROCESSING_TMP`.
- Task takes `tenant_id` but queries `Job.objects.get(pk=...)` alone → cross-tenant processing → filter by both.
- Long-lived (7-day) signed URLs in emails → leaked links → minutes-long URLs behind an authenticated view.
- "Deleted" exports still present as object versions → retention violation → lifecycle rules / crypto-shredding.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Worker killed parsing small XLSX | Decompression bomb / huge shared strings | Check ZIP ratio before openpyxl; container memory limit |
| XML parse hangs/CPU 100 % | Entity expansion | Reject DOCTYPE; defusedxml |
| Exported CSV opens with formulas | Neutralisation bypassed in one exporter | Centralise writer; test |
| Users from tenant A see tenant B rows in error report | Task lost tenant context; unscoped query | Re-fetch with tenant_id; query audit |
| Uploaded files reachable by URL | Stored under media root | Move to private storage; rotate keys |
| PII in logs | Exceptions with values logged | Redaction filter; change log statements |
| Disk full on workers | Temp files not cleaned after crashes | Sweeper task; per-volume quota |

**Validation checklist**
- [ ] Allow-list + magic-byte check + size limits at proxy, view and job.
- [ ] Archive limits: entries, total bytes read, ratio, path checks; no `extractall`.
- [ ] XML parsed with defusedxml or hardened lxml; DOCTYPE rejected where possible.
- [ ] Macro-enabled documents rejected; PDFs parsed in isolated workers with limits.
- [ ] Formula neutralisation on all CSV/XLSX exports, with tests.
- [ ] Field classification drives masking in previews, error reports, DLQ and logs.
- [ ] Temp files private, cleaned in `finally` and by sweeper.
- [ ] Downloads: tenant + permission check, short-lived signed URL, audit event.
- [ ] Retention purge jobs scheduled and monitored; versioning/backups accounted for.
- [ ] Tasks receive tenant_id and re-scope every query and storage key.
- Security tests (bombs, traversal, injection fixtures) → [data-processing-testing.md](data-processing-testing.md).

## 11. Related references and skills

- [file-processing-engineering.md](file-processing-engineering.md) — CSV/XLSX/XML parsing details.
- [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md) — ZIP, PDF, Parquet specifics.
- [data-import-and-export-systems.md](data-import-and-export-systems.md) — upload/preview/confirm flow, export links.
- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — `RejectedRecord` content.
- [data-processing-observability.md](data-processing-observability.md) — safe structured logging.
- [django-security-engineering: file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)
- [django-security-engineering: multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)
- [django-security-engineering: injection-prevention.md](../../django-security-engineering/references/injection-prevention.md)
- [django-security-engineering: secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md)
- [django-security-engineering: authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)
- [django-core: file-handling.md](../../django-core/references/file-handling.md)
- [django-integrations: file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md)

## 12. Official documentation and external references

- Django file uploads: https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- Django upload settings: https://docs.djangoproject.com/en/5.2/ref/settings/#file-upload-max-memory-size
- Django security overview: https://docs.djangoproject.com/en/5.2/topics/security/
- Python XML vulnerabilities: https://docs.python.org/3/library/xml.html#xml-vulnerabilities
- Python `zipfile`: https://docs.python.org/3/library/zipfile.html
- Python `tarfile` extraction filters: https://docs.python.org/3/library/tarfile.html#extraction-filters
- Python `tempfile`: https://docs.python.org/3/library/tempfile.html
- lxml parsing and security: https://lxml.de/parsing.html
- OWASP CSV Injection: https://owasp.org/www-community/attacks/CSV_Injection
- OWASP File Upload Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- OWASP XXE Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html
- django-storages S3: https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html
- AWS S3 versioning: https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html
