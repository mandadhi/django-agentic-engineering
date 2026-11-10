# Data Import and Export Systems

> **Scope:** A reusable, multi-tenant import/export architecture: upload → preview → column mapping
> → validation preview → confirm → background import; export requests, generation, streaming,
> download links; history, job tracking, partial failures, error reports, resumable imports and
> reprocessing. Defines `ImportJob`, `ExportJob`, `ColumnMapping` and the importer registry.
> **Not here:** format parsing ([file-processing-engineering.md](file-processing-engineering.md)),
> row validation rules ([data-validation-and-cleansing.md](data-validation-and-cleansing.md)), load
> mechanics ([database-loading-and-persistence.md](database-loading-and-persistence.md)), task
> design ([django-background-processing.md](django-background-processing.md)), upload hardening
> ([data-processing-security.md](data-processing-security.md)), document/report rendering and
> formatting ([django-report-generation](../../django-report-generation/SKILL.md)).

## 1. Purpose and scope

Most Django products grow a dozen ad-hoc "upload a spreadsheet" and "download as CSV" features,
each with its own bugs: whole-file parsing in the request, no tenant filter in the export query,
no error report, no way to resume. This reference designs **one** framework that every entity type
plugs into, so that security, tenancy, progress, history and error handling are implemented once.

The system owns the workflow around the data, not the data rules:

| Concern | Owned here | Delegated to |
|---|---|---|
| Workflow states, preview, mapping UX, confirmation | Yes | — |
| Parsing CSV/XLSX | Calls into | [file-processing-engineering.md](file-processing-engineering.md) |
| Row validation | Calls the importer's validator | [data-validation-and-cleansing.md](data-validation-and-cleansing.md) |
| Persisting rows | Calls the importer's loader | [database-loading-and-persistence.md](database-loading-and-persistence.md) |
| Export file layout/branding (PDF, styled XLSX) | No | [django-report-generation](../../django-report-generation/SKILL.md) |
| Direct-to-storage uploads, presigned URLs | Uses | [django-integrations object storage](../../django-integrations/references/file-transfer-and-object-storage.md) |

## 2. Core concepts

- **Import type / importer:** a registered entity ("customers", "employees", "products") with a
  schema, header aliases, row validator, mapper and loader.
- **`ImportJob`:** one uploaded file's lifecycle, tenant-scoped, with counters, checkpoint and
  error report.
- **`ColumnMapping`:** a saved mapping from source headers to importer fields, per tenant and
  import type, reusable for the next upload from the same source.
- **Preview:** first N rows (e.g. 20) parsed from the head of the file — never the whole file in a
  request.
- **Validation preview:** the preview rows run through the validator so the user sees errors before
  committing.
- **Confirmation:** explicit user action that enqueues the background import with the chosen
  mapping and options (mode: create-only / upsert; error policy).
- **`ExportJob`:** a request to produce a file from a tenant-filtered query; generated in the
  background for large outputs, delivered by a short-lived, access-checked link.
- **Error report:** downloadable CSV of rejected rows with line number, field, message and original
  values — formula-injection-safe.

## 3. Internal mechanics

### 3.1 Import state machine

```text
UPLOADED ──preview──> MAPPING ──validate preview──> AWAITING_CONFIRMATION
   │                                                     │ confirm (on_commit enqueue)
   └──reject (format/size)──> FAILED                     v
                                QUEUED ──> RUNNING ──> SUCCEEDED | PARTIAL | FAILED
                                              │  ^
                                              v  │ resume from checkpoint
                                           (worker crash / retry)
CANCELLED reachable from any non-terminal state.
```

States map onto `ProcessingJob.Status` (PENDING/QUEUED/RUNNING/SUCCEEDED/PARTIAL/FAILED/CANCELLED)
from [django-background-processing.md](django-background-processing.md) plus a `stage` field for
the pre-confirmation steps. Transitions use conditional updates
(`filter(pk=…, status=expected).update(status=new)`) so two workers or a double-click cannot both
start the job.

### 3.2 Preview without reading the whole file

The file is stored first (direct-to-storage or via Django upload to `default_storage`). Preview
opens it with `default_storage.open(name, "rb")` [Django], wraps the stream in the streaming
parser and pulls `N + 1` rows with `itertools.islice` [stdlib]. For XLSX, openpyxl read-only mode
still has to open the ZIP container and shared-strings part; cap file size before preview
(VERIFY: memory for shared strings on very large workbooks). The request never iterates past N.

### 3.3 Header detection

Normalise each source header (strip, casefold, collapse whitespace/punctuation) and match against
the importer's field aliases: exact alias → saved `ColumnMapping` → fuzzy match with
`difflib.get_close_matches(h, aliases, n=1, cutoff=0.85)` [stdlib]. Fuzzy matches are only
*suggestions* shown to the user; they are never applied silently to required money/identifier
fields.

### 3.4 Exports: streaming vs background

`StreamingHttpResponse` [Django] sends chunks as a generator yields them; memory stays bounded,
but the request occupies a web worker for the duration, proxies may time out (VERIFY: your
load balancer idle timeout), and an error mid-stream cannot change the already-sent 200 status — the
client receives a truncated file. Therefore: stream only small/medium exports (seconds); generate
large exports in a background task to object storage and deliver a signed URL.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Import < 1 000 rows, interactive admin | Synchronous validate + load in request, still via the registry | File size not capped; lock-heavy loads |
| Import of any size from end users | Store → preview → confirm → background job | — |
| Files > ~100 MB or slow networks | Direct-to-storage presigned upload | Tenant/content checks skipped — still validate server-side after upload |
| Export < ~50 k rows, simple CSV | `StreamingHttpResponse` with iterator | Exceeds proxy timeout; needs XLSX styling |
| Export large or XLSX | Background `ExportJob` → storage → signed link | Real-time requirement → paginated API instead |
| Errors in some rows | Import valid rows (PARTIAL) + error report | Business needs all-or-nothing (payroll) → validate-all-first, then load |
| Same supplier sends monthly file | Saved `ColumnMapping` per tenant + source | Headers change silently → re-confirm when signature differs |

**Error policy per import type** (set in the registry, overridable per job within limits):
`reject_file` (any invalid row fails the job — financial data), `skip_invalid` (PARTIAL —
customer lists), `threshold` (fail if invalid rows > X % — catches wrong-file uploads).

## 5. Django/Python implementation patterns

### 5.1 Models

```python
import uuid
from django.conf import settings
from django.db import models
from django.db.models.functions import Now

class ImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; QUEUED = "queued"; RUNNING = "running"; SUCCEEDED = "succeeded"
        PARTIAL = "partial"; FAILED = "failed"; CANCELLED = "cancelled"
    class Stage(models.TextChoices):
        UPLOADED = "uploaded"; MAPPING = "mapping"; AWAITING_CONFIRMATION = "awaiting_confirmation"
        PROCESSING = "processing"; DONE = "done"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    import_type = models.CharField(max_length=50)            # registry key
    file = models.FileField(upload_to="imports/%Y/%m/")      # storage key; never trust original name
    original_name = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64, db_index=True)
    source_headers = models.JSONField(default=list)          # headers as found in the file
    mapping = models.JSONField(default=dict)                 # snapshot used for this run
    options = models.JSONField(default=dict)                 # mode, error_policy
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    stage = models.CharField(max_length=24, choices=Stage, default=Stage.UPLOADED)
    total_rows = models.PositiveIntegerField(null=True)
    processed_rows = models.PositiveIntegerField(default=0)
    created_rows = models.PositiveIntegerField(default=0)
    updated_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    checkpoint_batch = models.PositiveIntegerField(default=0)  # resume point
    error_report = models.FileField(upload_to="imports/errors/%Y/%m/", blank=True)
    failure_reason = models.TextField(blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)  # reprocessing
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "import_type", "-created_at"])]
```

```python
class ColumnMapping(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    import_type = models.CharField(max_length=50)
    name = models.CharField(max_length=100)                  # e.g. "Supplier ACME monthly"
    header_signature = models.CharField(max_length=64)       # sha256 of normalised header list
    mapping = models.JSONField()                             # {"source header": "field_name" | None}
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "import_type", "name"],
                                               name="uniq_mapping_name_per_tenant_type")]

class ExportJob(models.Model):
    Status = ImportJob.Status
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    export_type = models.CharField(max_length=50)
    format = models.CharField(max_length=10, choices=[("csv", "CSV"), ("xlsx", "XLSX")])
    filters = models.JSONField(default=dict)                  # validated against exporter schema
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    row_count = models.PositiveIntegerField(null=True)
    file = models.FileField(upload_to="exports/%Y/%m/", blank=True)
    expires_at = models.DateTimeField(null=True)
    download_count = models.PositiveIntegerField(default=0)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True)
```

`mapping` on `ImportJob` is a **snapshot**: editing the saved `ColumnMapping` later must not change
what a historical job meant. Expose `public_id` in URLs, not sequential PKs.

### 5.2 Importer registry

```python
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable, Mapping

@dataclass(frozen=True)
class FieldSpec:
    name: str
    required: bool
    aliases: tuple[str, ...] = ()

@dataclass(frozen=True)
class Importer:
    key: str
    label: str
    fields: tuple[FieldSpec, ...]
    validate_row: Callable[[Mapping[str, str]], "RowResult"]   # pure; returns cleaned or errors
    map_row: Callable[["Cleaned", int], object]               # cleaned -> model instance (tenant_id)
    load_batch: Callable[[list[object], "ImportJob"], "LoadResult"]
    permission: str                                           # e.g. "crm.import_customer"
    error_policy: str = "skip_invalid"
    max_rows: int = 500_000
    max_bytes: int = 50 * 1024 * 1024

_REGISTRY: dict[str, Importer] = {}

def register(importer: Importer) -> Importer:
    if importer.key in _REGISTRY:
        raise ValueError(f"duplicate importer {importer.key}")
    _REGISTRY[importer.key] = importer
    return importer

def get_importer(key: str) -> Importer:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise PermissionError("unknown import type") from None   # never import by dotted path
```

Register importers in each app's `AppConfig.ready()`. The registry is the allow-list: the import
type in a request is only ever a key into it — never a model name or import path. Exporters use the
same pattern (`Exporter(key, permission, base_queryset(tenant, user), columns, filters_schema)`).

### 5.3 Upload, preview and mapping (service layer)

```python
from itertools import islice
from django.core.files.storage import default_storage

PREVIEW_ROWS = 20

def create_import(*, tenant, user, import_type: str, upload) -> ImportJob:
    imp = get_importer(import_type)
    if not user.has_perm(imp.permission):                      # per-import-type permission
        raise PermissionDenied
    if upload.size > imp.max_bytes:
        raise ImportRejected("file too large")
    digest = sha256_of(upload)                                 # hashlib.file_digest (Python >= 3.11)
    return ImportJob.objects.create(tenant=tenant, import_type=import_type, file=upload,
                                    original_name=upload.name[:255], file_sha256=digest,
                                    created_by=user)

def build_preview(job: ImportJob) -> Preview:
    imp = get_importer(job.import_type)
    with default_storage.open(job.file.name, "rb") as fh:
        headers, rows = open_tabular(fh, job.original_name)   # streaming parser, see file-processing
        sample = list(islice(rows, PREVIEW_ROWS))             # bounded: never the whole file
    suggested = suggest_mapping(job.tenant_id, imp, headers)  # saved mapping > alias > fuzzy
    checked = [imp.validate_row(apply_mapping(r, suggested)) for r in sample]
    return Preview(headers=headers, mapping=suggested, rows=sample, results=checked)
```

Warn when the same `file_sha256` was imported for this tenant recently ("this file was already
imported on …") — a cheap guard against double-processing.

### 5.4 Confirmation and background import

```python
from django.db import transaction

def confirm_import(job: ImportJob, *, user, mapping: dict[str, str | None], options: dict) -> None:
    imp = get_importer(job.import_type)
    missing = {f.name for f in imp.fields if f.required} - {v for v in mapping.values() if v}
    if missing:
        raise ImportRejected(f"unmapped required fields: {sorted(missing)}")
    if set(mapping.values()) - {f.name for f in imp.fields} - {None}:
        raise ImportRejected("mapping targets unknown fields")   # cannot map onto tenant/status
    updated = ImportJob.objects.filter(
        pk=job.pk, tenant=job.tenant, stage=ImportJob.Stage.AWAITING_CONFIRMATION
    ).update(mapping=mapping, options=sanitize_options(imp, options),
             stage=ImportJob.Stage.PROCESSING, status=ImportJob.Status.QUEUED)
    if updated != 1:
        raise ImportRejected("job is not awaiting confirmation")  # double-submit safe
    transaction.on_commit(lambda: run_import.delay(job.pk))
```

### 5.5 Import worker loop with checkpoints

```python
def execute_import(job_id: int) -> None:
    job = ImportJob.objects.select_related("tenant").get(pk=job_id)
    imp = get_importer(job.import_type)
    errors = ErrorReportWriter(job)                             # spools rejected rows to temp file
    with default_storage.open(job.file.name, "rb") as fh:
        _, rows = open_tabular(fh, job.original_name)
        for n, batch in enumerate(batched(enumerate(rows, start=2), 1000)):
            if n < job.checkpoint_batch:
                continue                                        # resume: already committed
            good = []
            for line, raw in batch:
                res = imp.validate_row(apply_mapping(raw, job.mapping))
                if res.ok:
                    good.append(imp.map_row(res.cleaned, job.tenant_id))
                else:
                    errors.add(line, raw, res.errors)
            with transaction.atomic():
                result = imp.load_batch(good, job)
                ImportJob.objects.filter(pk=job.pk).update(
                    checkpoint_batch=n + 1, processed_rows=F("processed_rows") + len(batch),
                    created_rows=F("created_rows") + result.created,
                    updated_rows=F("updated_rows") + result.updated)
            if cancelled(job.pk):
                return finish(job, ImportJob.Status.CANCELLED, errors)
    finish(job, ImportJob.Status.PARTIAL if errors.count else ImportJob.Status.SUCCEEDED, errors)
```

Error classification: parse/format errors → FAILED immediately (permanent); row errors → error
report (permanent, row-level); `OperationalError`/storage timeouts → raise for task retry
(transient), which resumes at `checkpoint_batch`. Error rows from batches before a crash must not be
lost: persist them as `RejectedRecord` rows per batch (see
[data-validation-and-cleansing.md](data-validation-and-cleansing.md)) and build the CSV from those at
the end, rather than relying on an in-memory/temp writer surviving a restart.

### 5.6 Error report CSV (formula-injection-safe)

```python
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

def safe_cell(v: object) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(DANGEROUS) else s

def write_error_report(job: ImportJob, out) -> None:
    w = csv.writer(out)
    w.writerow(["line", "field", "message", *job.source_headers])
    for rec in RejectedRecord.objects.filter(tenant=job.tenant, import_job=job).order_by("line").iterator(chunk_size=2000):
        w.writerow([rec.line, rec.field, safe_cell(rec.message),
                    *(safe_cell(rec.raw.get(h)) for h in job.source_headers)])
```

The same `safe_cell` applies to every user-originated value in exports. Escape values in HTML
previews (Django autoescape) — never `|safe`.

### 5.7 Streaming CSV export (small/medium)

```python
from django.http import StreamingHttpResponse

class Echo:
    def write(self, value: str) -> str:
        return value

def export_customers_csv(request) -> StreamingHttpResponse:
    exp = get_exporter("customers")
    if not request.user.has_perm(exp.permission):
        raise PermissionDenied
    qs = exp.base_queryset(request.tenant, request.user).values_list(*exp.columns)
    writer = csv.writer(Echo())
    def rows():
        yield writer.writerow(exp.headers)
        for r in qs.iterator(chunk_size=2000):
            yield writer.writerow([safe_cell(v) for v in r])
    resp = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="customers.csv"'
    return resp
```

### 5.8 Background export and temporary download links

Large exports: a task iterates the exporter queryset with keyset pagination
([large-dataset-processing.md](large-dataset-processing.md)), writes CSV via a spooled temp file or
XLSX via `openpyxl.Workbook(write_only=True)` [third-party: openpyxl] with `ws.append(row)`, saves
to private storage, sets `expires_at`, and notifies the user. Download goes through an
access-checked view:

```python
def download_export(request, public_id: uuid.UUID):
    job = get_object_or_404(ExportJob, public_id=public_id, tenant=request.tenant,
                            status=ExportJob.Status.SUCCEEDED)
    if job.requested_by_id != request.user.pk and not request.user.has_perm("exports.view_all"):
        raise Http404
    if job.expires_at and job.expires_at < timezone.now():
        raise Http404
    ExportJob.objects.filter(pk=job.pk).update(download_count=F("download_count") + 1)
    return redirect(job.file.url)   # S3Storage/AzureStorage signed URL, short expiry (VERIFY: signed-URL expiry setting names for your django-storages backend)
```

The signed URL itself is a bearer token: keep its expiry short (minutes), generate it only after the
permission check, and never email it — email a link to the view. A periodic task deletes expired
export files and keeps the `ExportJob` row as history.

## 6. Production use cases

- **HR employee onboarding (employees importer):** HR uploads the monthly XLSX from a payroll
  provider; saved `ColumnMapping` "Provider X" auto-applies; 3 % rows rejected for unknown
  department codes; error report downloaded, corrected, and reprocessed as a child job.
- **Product catalog (products importer, 400 k rows):** direct-to-storage upload, threshold policy
  (fail if > 20 % invalid — catches wrong supplier file), resumable after a deploy at batch 212.
- **Customer list for CRM (customers importer):** upsert by `(tenant, email)`; preview flags 40
  duplicate emails within the file before confirmation.
- **Payments export for finance (2 M rows/month):** background CSV to S3, link valid 15 minutes,
  every download audited; formula-safe cells because finance opens it in Excel.
- **Attendance export for a hospital ward manager:** small, streamed CSV restricted by the
  exporter's `base_queryset` to the manager's wards.

**Reprocessing:** a "Re-run rejected rows" action builds a new `ImportJob(parent=job)` from the
corrected error-report CSV (same importer, same mapping snapshot). Because loaders are idempotent
by natural key, re-running the whole original file is also safe.

## 7. Advantages and disadvantages

| Design choice | Advantages | Disadvantages |
|---|---|---|
| Central registry | Security/tenancy/progress implemented once; consistent UX | Up-front framework cost; importers must fit the contract |
| Preview + confirm | Catches wrong files and mappings before writes | Extra step for power users (offer "auto-confirm with saved mapping") |
| Saved `ColumnMapping` | Fast repeat imports | Stale mappings when source changes — compare header signature |
| Background imports | No request timeouts, resumable | Needs worker infra, polling/notifications |
| Streamed exports | Simple, no storage | Ties up web worker; truncated file on mid-stream error |
| Background exports + signed URL | Scales, retryable, auditable | Storage cost, link expiry UX |

## 8. Security and performance considerations

**Security**
- Tenant scoping everywhere: every `ImportJob`/`ExportJob`/`ColumnMapping`/`RejectedRecord` query
  filters by `request.tenant`; exporters build querysets from a `base_queryset(tenant, user)` hook
  that also applies row-level permissions ([multi-tenant security](../../django-security-engineering/references/multi-tenant-security.md)).
- Per-import-type permissions checked at upload, confirm and download — not just in the UI.
- Mapping targets come only from the importer's field allow-list; files can never set `tenant`,
  ownership, status or price-override fields.
- Upload limits, content sniffing, ZIP-bomb protection for XLSX, malware scanning hook:
  [data-processing-security.md](data-processing-security.md) and
  [file upload security](../../django-security-engineering/references/file-upload-security.md).
- Export cells neutralised against formula injection (OWASP CSV Injection).
- Private storage only; signed URLs short-lived and issued after an access check; downloads audited.

**Performance**
- Preview reads ≤ N rows; counting total rows happens in the worker, not the request.
- Batch size and checkpoints per [data-processing-performance.md](data-processing-performance.md).
- Progress updates via `F()` counters once per batch — not per row (write amplification).
- Exports: `values_list()` instead of model instances; index on export filter columns.

## 9. Common mistakes and anti-patterns

- Parsing the whole file in the upload request for "row count" → timeouts, OOM → count in the worker.
- Import type taken as a model path from the request → arbitrary model writes → registry allow-list.
- Export queryset `Model.objects.filter(**request.GET)` → cross-tenant leakage, filter injection → validated filters on a tenant-scoped base queryset.
- Enqueuing the task before the job row commits → worker gets `DoesNotExist` → `transaction.on_commit`.
- Mutating the saved `ColumnMapping` used by past jobs → history becomes unexplainable → snapshot mapping on the job.
- Error rows kept only in memory → lost on retry → persist per batch.
- Raw cell values in error report → formula execution in Excel → `safe_cell`.
- Long-lived public S3 URLs in emails → data exfiltration → access-checked view + short expiry.
- Streaming a 5 M-row export through gunicorn → worker starvation, proxy timeout → background export.
- Fuzzy-matched headers auto-applied to amount fields → wrong data loaded → suggestions require user confirmation.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Job stuck in QUEUED | Task enqueued before commit or worker down | Check `on_commit`; broker queue depth |
| Job restarts from row 1 after crash | Checkpoint not committed with batch | Update checkpoint inside the batch transaction |
| Duplicates after retry | Loader not idempotent | Natural-key UPSERT ([database-loading-and-persistence.md](database-loading-and-persistence.md)) |
| Export file truncated | Mid-stream exception / proxy timeout | Move to background export; check proxy timeout |
| Users see other tenants' export | Base queryset bypassed | Tests asserting tenant filter on every exporter |
| Mapping not auto-applied | Header signature changed (extra column, renamed) | Show diff; ask user to update mapping |
| Excel shows formulas/`#NAME?` in report | No formula neutralisation | `safe_cell` on all user values |

Validation checklist:
- [ ] Every importer/exporter registered with a permission; unknown keys rejected.
- [ ] Preview bounded to N rows; file size capped before preview.
- [ ] Confirm is idempotent (double click → one job).
- [ ] Kill worker mid-import → resumes at checkpoint, counters correct, no duplicates.
- [ ] Error report opens safely in Excel; line numbers match source.
- [ ] Download links expire; access check enforced; downloads audited.
- [ ] Tenant-isolation tests for every exporter ([data-processing-testing.md](data-processing-testing.md)).

## 11. Related references and skills

- [file-processing-engineering.md](file-processing-engineering.md) — streaming CSV/XLSX parsers used by preview and worker.
- [data-validation-and-cleansing.md](data-validation-and-cleansing.md) — `RejectedRecord`, row validators.
- [data-transformation-and-mapping.md](data-transformation-and-mapping.md) — `MappingDefinition` for system-to-system mappings.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — loaders, idempotency, rollback by job.
- [django-background-processing.md](django-background-processing.md) — `ProcessingJob`, retries, cancellation.
- [large-dataset-processing.md](large-dataset-processing.md) — keyset iteration for large exports.
- [data-processing-security.md](data-processing-security.md) — upload hardening, retention.
- [production-architectures-imports-and-pipelines.md](production-architectures-imports-and-pipelines.md) — large CSV/Excel import architecture.
- [production-architectures-sync-reconciliation-and-documents.md](production-architectures-sync-reconciliation-and-documents.md) — bulk export service.
- [django-report-generation](../../django-report-generation/SKILL.md) — rendering/formatting export documents.
- [django-integrations object storage](../../django-integrations/references/file-transfer-and-object-storage.md) — presigned uploads/downloads.
- [django-security-engineering authorization](../../django-security-engineering/references/authorization-and-access-control.md), [multi-tenant security](../../django-security-engineering/references/multi-tenant-security.md).
- [django-architecture service layer](../../django-architecture/references/service-layer.md).

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- https://docs.djangoproject.com/en/5.2/ref/files/storage/
- https://docs.djangoproject.com/en/5.2/howto/outputting-csv/#streaming-large-csv-files
- https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects
- https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- https://docs.python.org/3/library/csv.html
- https://docs.python.org/3/library/difflib.html#difflib.get_close_matches
- https://openpyxl.readthedocs.io/en/stable/optimized.html
- https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html
- https://django-storages.readthedocs.io/en/latest/backends/azure.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
- https://owasp.org/www-community/attacks/CSV_Injection
