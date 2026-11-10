# File uploads and imports

## Upload validation

```python
ALLOWED = {
    ".csv": {"text/csv", "text/plain", "application/csv"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip"},
}
MAX_BYTES = 20 * 1024 * 1024

class ImportUploadForm(forms.Form):
    kind = forms.ChoiceField(choices=ImportJob.Kind.choices)
    file = forms.FileField()

    def clean_file(self):
        f = self.cleaned_data["file"]
        ext = Path(f.name).suffix.lower()
        if ext not in ALLOWED:
            raise forms.ValidationError("Upload a .csv or .xlsx file.", code="extension")
        if f.size > MAX_BYTES:
            raise forms.ValidationError("File is larger than 20 MB.", code="too_large")
        head = f.read(2048); f.seek(0)
        if ext == ".xlsx" and not head.startswith(b"PK\x03\x04"):     # xlsx is a zip
            raise forms.ValidationError("Not a valid Excel file.", code="content")
        if ext == ".csv" and b"\x00" in head:                         # binary masquerading as CSV
            raise forms.ValidationError("Not a valid CSV file.", code="content")
        return f
```

- `python-magic`/libmagic gives better sniffing if the image already has it; don't add a native
  dependency just for two formats.
- Images: open with Pillow and `verify()`, re-encode to strip metadata, cap pixel dimensions
  (`Image.MAX_IMAGE_PIXELS`).
- Global limits: `DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE` (larger files
  spool to a temp file), plus the proxy's `client_max_body_size`.

## Storage

```python
def import_upload_path(instance, filename: str) -> str:
    return f"imports/{instance.created_by_id}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"

class ImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; RUNNING = "running"; SUCCEEDED = "succeeded"; FAILED = "failed"

    class Kind(models.TextChoices):
        CUSTOMERS = "customers"; PRODUCTS = "products"

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind)
    file = models.FileField(upload_to=import_upload_path)
    original_name = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    total_rows = models.PositiveIntegerField(default=0)
    created_rows = models.PositiveIntegerField(default=0)
    updated_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    error_report = models.FileField(upload_to="imports/errors/", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)
```

- Production storage: S3/GCS/Azure via `django-storages`, configured in `STORAGES["default"]`;
  private bucket, server-side encryption, signed URLs for download.
- Workers read the file through `default_storage.open(job.file.name)` — never assume a local path.
- Keep `original_name` for display only.

## Parsing CSV

```python
import csv, io

def iter_csv_rows(fileobj, *, required: set[str]):
    text = io.TextIOWrapper(fileobj, encoding="utf-8-sig", newline="")   # strips BOM
    reader = csv.DictReader(text)
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise ImportFormatError(f"Missing columns: {', '.join(sorted(missing))}")
    for line_no, row in enumerate(reader, start=2):                      # header is line 1
        yield line_no, {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
```

Decide the encoding contract (UTF-8 with or without BOM); on `UnicodeDecodeError` fail the job
with a clear message rather than guessing.

## Parsing Excel

```python
from openpyxl import load_workbook

def iter_xlsx_rows(fileobj, *, required: set[str]):
    wb = load_workbook(fileobj, read_only=True, data_only=True)   # data_only: values, not formulas
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip().lower() if h is not None else "" for h in next(rows, [])]
        missing = required - set(header)
        if missing:
            raise ImportFormatError(f"Missing columns: {', '.join(sorted(missing))}")
        for line_no, values in enumerate(rows, start=2):
            if all(v is None for v in values):
                continue
            yield line_no, dict(zip(header, values))
    finally:
        wb.close()
```

Excel cells arrive typed (`datetime`, `float`, `int`) — normalise (e.g. `Decimal(str(v))`) before
validation. Legacy `.xls` needs `xlrd`; ask users for `.xlsx` instead of adding it.

## Row validation and batching

```python
class CustomerRow(serializers.Serializer):
    email = serializers.EmailField()
    name = serializers.CharField(max_length=200)
    credit_limit = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)

BATCH = 1000

def process_import(job: ImportJob) -> None:
    ImportJob.objects.filter(pk=job.pk).update(status=ImportJob.Status.RUNNING)
    errors: list[tuple[int, dict]] = []
    batch: list[Customer] = []
    total = 0
    with default_storage.open(job.file.name, "rb") as fh:
        for line_no, raw in iter_csv_rows(fh, required={"email", "name"}):
            total += 1
            row = CustomerRow(data=raw)
            if not row.is_valid():
                errors.append((line_no, row.errors))
                continue
            batch.append(Customer(tenant_id=job.tenant_id, **row.validated_data))
            if len(batch) >= BATCH:
                _flush(batch); batch.clear()
        _flush(batch)
    _finish(job, total=total, errors=errors)

@transaction.atomic
def _flush(batch: list[Customer]) -> None:
    if batch:
        Customer.objects.bulk_create(batch, update_conflicts=True,
                                     unique_fields=["tenant", "email"],
                                     update_fields=["name", "credit_limit"])
```

- Deduplicate within the file before upserting (PostgreSQL rejects the same conflict key twice
  in one `INSERT ... ON CONFLICT`).
- Resolve foreign keys with one lookup per batch (`in_bulk` / dict by natural key), not per row.
- All-or-nothing imports: validate the whole file first (first pass), then write in one
  transaction (second pass) — only for files small enough to hold locks briefly.
- Update `job` counters every N batches so the UI can show progress.

## Error reports

Write rejected rows to a CSV (`line`, `field`, `message`) saved to `job.error_report`; show the
first 50 errors in the UI and link to the full file. Never echo raw cell values into HTML
without escaping.

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/http/file-uploads/
- https://docs.djangoproject.com/en/stable/ref/files/uploads/
- https://docs.djangoproject.com/en/stable/ref/files/storage/
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#bulk-create
- https://docs.python.org/3/library/csv.html
- https://openpyxl.readthedocs.io/en/stable/optimized.html
- https://django-storages.readthedocs.io/
