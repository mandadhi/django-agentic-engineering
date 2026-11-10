# File and Data Processing Performance

**Scope:** Performance of moving and transforming large data in Django — uploads, downloads,
streamed exports, bulk ingestion, CSV/Excel, images and generated documents — with bounded memory,
short worker occupancy and efficient database writes. Pipeline and ETL design belongs to
[django-data-processing](../../django-data-processing/SKILL.md); memory theory to
[memory-and-resource-management.md](memory-and-resource-management.md).

## 1. Purpose and scope

File and data workloads fail differently from ordinary requests: they are long, memory-hungry and
I/O-heavy, and one of them can pin a web worker for minutes or OOM-kill it. This reference helps
an agent:

- Keep memory proportional to chunk size, not file size.
- Keep web workers free by streaming, offloading to the proxy/object storage, or moving work to
  tasks.
- Choose the right bulk write path (`bulk_create`, `COPY`, staging tables) for the volume.
- Measure the right things: peak RSS, worker occupancy time, rows/s, DB write time.

Out of scope: storage backend configuration ([django-core](../../django-core/SKILL.md) file
handling), upload validation and security policy ([django-security-engineering](../../django-security-engineering/SKILL.md)),
report layout and PDF libraries ([django-report-generation](../../django-report-generation/SKILL.md)).

Third-party tools named here: **[third-party: openpyxl]**, **[third-party: Pillow]**,
**[third-party: psycopg]** (psycopg 3), **[third-party: django-storages]**, **[third-party: boto3]**.

## 2. Core concepts

| Concept | Performance meaning |
|---|---|
| Worker occupancy | Seconds a WSGI/ASGI worker slot is held; a 2-minute download on slow clients blocks a sync worker for 2 minutes |
| Peak memory | Max RSS during processing; drives OOM kills and container sizing |
| Streaming | Producing/consuming data incrementally; memory ≈ chunk size |
| Offload | Let nginx/CDN/object storage move bytes instead of Python |
| Write amplification | Each inserted row costs index updates, WAL, constraint checks, triggers |
| Batch / transaction size | Rows per statement and per commit; too small = round-trip bound, too large = lock/WAL/memory bound |
| Slow clients | Mobile users uploading/downloading at low bandwidth multiply occupancy |

Rule of thumb: Python should touch bytes only when it must transform them. Moving bytes is the
job of the reverse proxy and object storage.

## 3. Internal mechanics

### Upload handling

Django parses multipart bodies through `FILE_UPLOAD_HANDLERS`:

- `MemoryFileUploadHandler` keeps files ≤ `FILE_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) in
  memory.
- `TemporaryFileUploadHandler` streams larger files to `FILE_UPLOAD_TEMP_DIR`.
- `DATA_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) caps non-file request body size;
  `DATA_UPLOAD_MAX_NUMBER_FIELDS` / `DATA_UPLOAD_MAX_NUMBER_FILES` (Django ≥ 3.2.18/4.1.7
  security releases, VERIFY) bound field/file counts.

The whole body is still received **by the worker** before the view runs (unless the proxy
buffers it first — nginx `proxy_request_buffering on` is the default and protects sync workers
from slow uploads). Raising `FILE_UPLOAD_MAX_MEMORY_SIZE` trades disk I/O for RAM per concurrent
upload — concurrency × size can exhaust memory.

### Response types

| Response | Behaviour |
|---|---|
| `HttpResponse(content)` | Entire body materialised in memory |
| `StreamingHttpResponse(iterator)` | Body produced chunk by chunk; no `Content-Length`; middleware must not read `.content` |
| `FileResponse(file_obj)` | Streams a file object in blocks (`block_size`, default 4096 bytes in `FileResponse`, VERIFY); uses `wsgi.file_wrapper` (e.g. sendfile) when the server provides one |

Under ASGI, `StreamingHttpResponse` with a **sync** iterator is consumed in a thread with
buffering; Django ≥ 4.2 supports async iterators for ASGI streaming and warns when a sync
iterator is used (VERIFY warning wording/version).

### QuerySet iteration

`QuerySet.iterator(chunk_size=N)` uses server-side cursors on PostgreSQL so rows are fetched in
chunks without populating the result cache. Server-side cursors are incompatible with
transaction-pooling PgBouncer unless `DISABLE_SERVER_SIDE_CURSORS = True` (then the driver
fetches everything client-side — memory benefit lost). See
[orm-performance-optimization.md](orm-performance-optimization.md).

### Bulk writes

- `bulk_create(objs, batch_size=...)` builds multi-row `INSERT`s; skips `save()` and signals;
  returns PKs on PostgreSQL. `update_conflicts=True` (Django ≥ 4.1) performs upserts.
- PostgreSQL `COPY FROM STDIN` streams rows in the COPY protocol — typically several times faster
  than multi-row INSERT for large loads because parse/plan and per-statement overhead disappear
  (magnitude illustrative and workload-dependent).
- Each index on the target table adds work per row; large loads into heavily indexed tables are
  index-bound, not Python-bound.

### Images

Pillow decodes the full image into memory: an 8000×8000 RGBA image ≈ 256 MB decoded regardless of
a 5 MB JPEG file. `Image.MAX_IMAGE_PIXELS` (default ~89.5 MP) raises `DecompressionBombWarning`,
and an error above 2× that. `Image.draft()` lets the JPEG decoder downscale during decode,
cutting memory and time for thumbnails.

## 4. Engineering decisions and trade-offs

### Upload path

| Option | When | Trade-off |
|---|---|---|
| Through Django (default handlers) | Small files (< a few MB), need immediate validation | Worker occupancy + temp disk; proxy must buffer |
| Direct-to-object-storage (presigned PUT/POST) | Large files, mobile clients, high volume | More moving parts: client flow, callback/verification task, orphaned objects cleanup |
| Chunked/resumable upload protocol | Very large files on unreliable networks | Complexity; third-party protocol (e.g. tus) |

### Download path

| Option | When | Trade-off |
|---|---|---|
| `FileResponse` from Django | Small private files, low volume | Worker held for full transfer |
| `X-Accel-Redirect` (nginx) / `X-Sendfile` (Apache) | Private files on disk/volume behind your proxy; Django authorises, proxy streams | Proxy config coupling; internal locations must not be public |
| Signed/presigned URL redirect | Files in S3/GCS; CDN delivery | Link is a bearer token until expiry; caching and revocation semantics |

### Export path

| Size | Approach |
|---|---|
| Small (< ~10k rows, fast query) | Synchronous `HttpResponse` is fine |
| Medium | `StreamingHttpResponse` with `iterator()` — no worker memory spike, but worker held for duration and errors mid-stream produce truncated files |
| Large or slow query | Background task writes to object storage; user gets a link/notification |

Streaming makes memory flat but **not** shorter: the request is as long as the query plus
transfer. Behind proxies with read timeouts (often 60 s) long streams are cut. Prefer tasks when
duration can exceed proxy/client timeouts.

### Ingestion path

| Volume (illustrative) | Approach |
|---|---|
| ≤ 10k rows, needs model validation | `full_clean()` + `bulk_create(batch_size=1000)` |
| 10k–1M rows | Validate in Python in chunks, `bulk_create` per chunk in short transactions |
| ≥ 1M rows or recurring feeds | `COPY` into an unlogged/temp staging table, validate and merge with set-based SQL (`INSERT ... SELECT ... ON CONFLICT`) |

Deferring index cost (drop and recreate indexes, or load into a fresh table and swap) helps only
for very large initial loads and requires exclusive control of the table — on a live table it
breaks reads and constraints. Unlogged staging tables are fast but not crash-safe and not
replicated.

### Transaction sizing

One giant transaction: simplest atomicity, but long-held locks, WAL bursts, replication lag and
a full rollback on row 999,999. Many tiny transactions: commit overhead dominates. Commit per
chunk (e.g. 1–10k rows, measure) and track progress so the job is resumable.

## 5. Django implementation patterns

### Streamed CSV export (Django docs Echo pattern)

```python
import csv
from django.http import StreamingHttpResponse

class Echo:
    """Pseudo-buffer: csv.writer calls write(), we return the line instead of storing it."""
    def write(self, value):
        return value

def export_orders_csv(request):
    rows = (
        Order.objects.for_user(request.user)
        .order_by("pk")
        .values_list("reference", "status", "total", "created_at")
        .iterator(chunk_size=2000)
    )
    writer = csv.writer(Echo())
    header = ["reference", "status", "total", "created_at"]

    def generate():
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow(row)

    response = StreamingHttpResponse(generate(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders.csv"'
    return response
```

Bottleneck: peak memory from `list(queryset)` + full CSV string (illustrative: 500k rows → 1.2
GB RSS → ~60 MB flat). `values_list` avoids model instantiation. Validate with peak RSS and
time-to-first-byte; ensure GZip or other middleware doesn't buffer the stream. Under ASGI,
prefer an async generator (Django ≥ 4.2) wrapping async iteration.

### Authorised download offloaded to nginx

```python
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

def download_invoice(request, pk):
    invoice = get_object_or_404(Invoice.objects.for_user(request.user), pk=pk)
    response = HttpResponse()
    response["X-Accel-Redirect"] = f"/protected/invoices/{invoice.file.name}"
    response["Content-Type"] = "application/pdf"
    response["Content-Disposition"] = f'attachment; filename="invoice-{invoice.number}.pdf"'
    return response
```

```nginx
location /protected/ {
    internal;                       # not reachable directly by clients
    alias /srv/media/;
}
```

Django's occupancy drops to the authorisation query; nginx streams with sendfile. For S3 via
[third-party: django-storages], redirect to `invoice.file.url` with signed querystring auth and a
short expiry instead.

### Presigned direct upload

```python
import boto3  # [third-party: boto3]

def create_upload(request):
    key = f"uploads/{request.user.pk}/{uuid.uuid4()}"
    post = boto3.client("s3").generate_presigned_post(
        Bucket=settings.UPLOAD_BUCKET,
        Key=key,
        Conditions=[["content-length-range", 1, 200 * 1024 * 1024]],
        ExpiresIn=600,
    )
    PendingUpload.objects.create(user=request.user, key=key)
    return JsonResponse(post)
# Client uploads to S3; then calls /uploads/complete, which enqueues a verification/processing task.
```

### Chunked CSV ingestion with bulk_create

```python
import csv, io
from itertools import islice
from django.db import transaction

def import_products(file_obj, *, batch_size: int = 2000) -> int:
    text = io.TextIOWrapper(file_obj, encoding="utf-8", newline="")
    reader = csv.DictReader(text)
    total = 0
    while True:
        chunk = list(islice(reader, batch_size))
        if not chunk:
            return total
        objs = [Product(sku=r["sku"], name=r["name"], price=r["price"]) for r in chunk]
        with transaction.atomic():
            Product.objects.bulk_create(
                objs,
                batch_size=batch_size,
                update_conflicts=True,
                unique_fields=["sku"],
                update_fields=["name", "price"],
            )
        total += len(objs)
```

Memory bounded by `batch_size`; each chunk commits independently (record a checkpoint if the
job must resume). Bulk mechanics and bypassed hooks:
[django-orm bulk-operations-and-batching](../../django-orm/references/bulk-operations-and-batching.md).

### COPY into staging, then merge (PostgreSQL, psycopg 3)

```python
from django.db import connection, transaction

def load_prices(rows_iter) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE price_stage (sku text, price numeric) ON COMMIT DROP")
        raw = cursor.cursor                               # underlying psycopg 3 cursor (VERIFY wrapper attribute)
        with raw.copy("COPY price_stage (sku, price) FROM STDIN") as copy:
            for sku, price in rows_iter:
                copy.write_row((sku, price))
        cursor.execute("""
            UPDATE catalog_product p SET price = s.price
            FROM price_stage s WHERE p.sku = s.sku AND p.price IS DISTINCT FROM s.price
        """)
```

`IS DISTINCT FROM` skips no-op updates — avoiding dead tuples and index churn (write
amplification). Requires psycopg 3 (Django ≥ 4.2 supports it); with psycopg2 use `copy_expert`.
Bypasses model validation entirely — validate in the staging table with SQL.

### Excel read/write with openpyxl modes

```python
from openpyxl import load_workbook, Workbook  # [third-party: openpyxl]

def iter_rows(path):
    wb = load_workbook(path, read_only=True, data_only=True)   # lazy, low memory
    try:
        for row in wb.active.iter_rows(min_row=2, values_only=True):
            yield row
    finally:
        wb.close()                                             # read_only keeps the file open

def write_report(path, rows):
    wb = Workbook(write_only=True)                             # rows streamed to disk
    ws = wb.create_sheet()
    for row in rows:
        ws.append(row)
    wb.save(path)
```

Normal mode loads every cell as an object — tens of times the file size in RAM (VERIFY
openpyxl's documented ratio). XLSX generation is CPU-heavy; do large ones in tasks.

### Thumbnails in a task

```python
from PIL import Image  # [third-party: Pillow]

@shared_task(ignore_result=True, soft_time_limit=60, time_limit=90)
def make_thumbnail(photo_id: int) -> None:
    photo = Photo.objects.get(pk=photo_id)
    with photo.original.open("rb") as fh, Image.open(fh) as im:
        if im.width * im.height > 50_000_000:
            raise ValueError("image too large")              # explicit cap below Pillow's bomb limit
        im.draft("RGB", (800, 800))                          # JPEG decode-time downscale
        im.thumbnail((400, 400))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    photo.thumbnail.save(f"{photo.pk}.jpg", ContentFile(buf.getvalue()), save=True)
```

Generate PDFs/documents the same way: task → object storage → link, never inside the request for
non-trivial documents ([django-report-generation](../../django-report-generation/SKILL.md)).

## 6. Production use cases

- **Export OOM**: "Download all orders" built a list of 2M model instances; workers OOM-killed
  under concurrent use. Fix: streamed `values_list().iterator()` for ≤ 100k rows, task + S3 link
  above. Validate: peak RSS flat, no OOM events in container logs.
- **Slow-client download exhaustion**: 8 sync workers each serving a 300 MB file to mobile users
  → site unavailable. Fix: `X-Accel-Redirect` or signed URLs; worker occupancy from minutes to ms.
- **Upload timeouts**: 1 GB video uploads through Django timed out at the proxy. Fix: presigned
  direct upload + processing task.
- **Nightly import overrunning**: 3M-row feed with `save()` per row took 4 h; `bulk_create`
  batches → 25 min; COPY + merge → 4 min (illustrative). Also removed no-op updates that
  generated vacuum pressure.
- **Replication lag**: single-transaction 20M-row update produced a WAL burst and replica lag of
  minutes; read-replica users saw stale data. Fix: chunked commits with throttling.
- **Thumbnail decompression bomb**: crafted PNG expanded to tens of GB. Fix: pixel cap, task time
  limit, and dimension check before full decode.

## 7. Advantages and disadvantages

| Technique | Advantage | Disadvantage |
|---|---|---|
| Streaming responses | Flat memory, early first byte | Worker still occupied; mid-stream errors truncate; no Content-Length |
| Proxy offload / signed URLs | Near-zero worker cost, CDN-friendly | Infra coupling; bearer-URL exposure window |
| Presigned uploads | Workers never see bytes | Async verification flow, orphan cleanup |
| `bulk_create` | 10–100× fewer round trips than `save()` loops (illustrative) | Skips `save()`/signals/validation |
| COPY + staging | Fastest bulk path on PostgreSQL | Backend-specific, raw SQL, bypasses ORM validation |
| Background processing | Bounded request latency, retryable | Eventual completion UX, queue capacity planning |

## 8. Security and performance considerations

- Bound everything: request body size, file size, rows per import, pixels per image, pages per
  document. Unbounded inputs are memory and CPU DoS vectors.
- Authorise **before** issuing `X-Accel-Redirect`/signed URLs; keep signed URL expiry short and
  never cache them in shared caches.
- Never trust client `Content-Type` or extension for processing decisions; detect type, and
  process untrusted images/documents in isolated workers with limits.
- CSV exports opened in spreadsheets can execute formulas (CSV injection) — escape leading
  `=`, `+`, `-`, `@` ([django-security-engineering](../../django-security-engineering/SKILL.md)).
- Temp files from `TemporaryFileUploadHandler` live on local disk — size the volume for
  concurrency × max upload size.
- Staging tables and COPY run with the app's DB role; scope permissions accordingly.

## 9. Common mistakes and anti-patterns

- `file.read()` on an upload or `response.content` on a download — loads everything.
- `list(qs)` / `qs.all()` without `iterator()` for exports; `iterator()` behind transaction-mode
  PgBouncer without understanding `DISABLE_SERVER_SIDE_CURSORS`.
- `StreamingHttpResponse` that's buffered anyway by middleware or a proxy (`proxy_buffering`) and
  then times out.
- `save()` per row in imports; or one `bulk_create` of 5M objects built in memory first.
- Updating rows that didn't change; re-importing full feeds instead of diffs.
- Generating PDFs/XLSX/thumbnails synchronously in the request.
- Raising `FILE_UPLOAD_MAX_MEMORY_SIZE` to "speed up" uploads.
- Dropping indexes on a live table to speed a load.

## 10. Troubleshooting and validation

| Symptom | Measure | Typical cause |
|---|---|---|
| Worker OOM during export/import | Peak RSS per request (memray/tracemalloc — [memory-and-resource-management.md](memory-and-resource-management.md)) | Materialised QuerySet, whole-file reads, openpyxl normal mode |
| All workers busy, low CPU | Worker occupancy by endpoint, request duration histogram | Slow clients on downloads/uploads through Django |
| Import slow, Python CPU low | DB time per batch, `pg_stat_statements` for the INSERT | Index/constraint/trigger cost, tiny batches, network round trips |
| Import slow, Python CPU high | Profile ([profiling-and-observability.md](profiling-and-observability.md)) | Per-row validation, model instantiation, date parsing |
| Truncated downloads | Proxy and client timeout logs | Stream longer than proxy read timeout |
| Replica lag during jobs | Replica lag metric vs job timeline | Huge transactions, WAL bursts |

Validation recipe: run the job against a production-sized dataset, record rows/s, peak RSS,
total DB time and WAL volume (`pg_stat_wal`, PostgreSQL ≥ 14) before and after; for endpoints,
record p95 duration and worker occupancy under concurrent load
([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).

## 11. Related references and skills

- [memory-and-resource-management.md](memory-and-resource-management.md) — memory profiling, leaks, worker recycling.
- [background-task-performance.md](background-task-performance.md) — queues, time limits and retries for offloaded processing.
- [orm-performance-optimization.md](orm-performance-optimization.md) — `iterator()`, projections.
- [database-performance.md](database-performance.md) — write amplification, index cost, WAL, locks.
- [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md) — CDN, proxy offload, compression.
- [django-data-processing](../../django-data-processing/SKILL.md) — import/export and ETL pipeline design.
- [django-report-generation](../../django-report-generation/SKILL.md) — PDF/XLSX document generation.
- [django-orm bulk-operations-and-batching](../../django-orm/references/bulk-operations-and-batching.md) — bulk API semantics.
- [django-core](../../django-core/SKILL.md) — file handling and storage backends.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — COPY, unlogged tables, WAL tuning.

## 12. Official documentation and external references

- Django 5.2 — File uploads: https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- Django 5.2 — Upload settings: https://docs.djangoproject.com/en/5.2/ref/settings/#file-upload-max-memory-size
- Django 5.2 — Outputting CSV (streaming): https://docs.djangoproject.com/en/5.2/howto/outputting-csv/
- Django 5.2 — Request/response objects (`StreamingHttpResponse`, `FileResponse`): https://docs.djangoproject.com/en/5.2/ref/request-response/
- Django 5.2 — `QuerySet.iterator()`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator
- Django 5.2 — `bulk_create()`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- PostgreSQL — COPY: https://www.postgresql.org/docs/current/sql-copy.html
- PostgreSQL — Populating a database: https://www.postgresql.org/docs/current/populate.html
- psycopg 3 — COPY: https://www.psycopg.org/psycopg3/docs/basic/copy.html
- openpyxl — Optimised modes: https://openpyxl.readthedocs.io/en/stable/optimized.html
- Pillow — `Image.draft`, `thumbnail`, `MAX_IMAGE_PIXELS`: https://pillow.readthedocs.io/en/stable/reference/Image.html
- nginx — X-Accel: https://nginx.org/en/docs/http/ngx_http_core_module.html#internal
- AWS S3 presigned POST: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html
