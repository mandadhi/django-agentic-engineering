# File handling

> Scope: `FileField`/`ImageField`, uploads, `UploadedFile`, upload handlers and limits, the
> `STORAGES["default"]` backend, custom and cloud storage, file lifecycle and cleanup.
> Not covered here: static files and their serving ([production-configuration.md](./production-configuration.md)),
> form field validation in general ([forms-validation.md](./forms-validation.md)), DRF parsers
> ([django-rest-framework](../../django-rest-framework/SKILL.md)), malware scanning and deep upload
> hardening ([django-security-engineering](../../django-security-engineering/SKILL.md)), vendor SDK setup
> ([django-integrations](../../django-integrations/SKILL.md)).

## Purpose and overview

Django separates three concerns that are often conflated:

| Concern | Object | Responsibility |
|---|---|---|
| Incoming bytes | `UploadedFile` (in `request.FILES`) | Holds an upload in memory or a temp file |
| Persistence | `Storage` backend | Saves, opens, deletes and builds URLs for named files |
| Reference | `FileField` / `FieldFile` | Stores only the *name* (a string path) in the database column |

The database never holds file content. Everything that goes wrong in production — orphaned
files, path traversal, unbounded memory, public URLs for private documents — comes from
misunderstanding which of these three layers owns the decision.

Check the installed version (`python -m django --version`): the `STORAGES` setting and the
`storages` registry exist from Django 4.2; the old `DEFAULT_FILE_STORAGE` / `STATICFILES_STORAGE`
settings were removed in 5.1.

## How it works internally

1. The request body is streamed through `FILE_UPLOAD_HANDLERS` (default:
   `MemoryFileUploadHandler`, then `TemporaryFileUploadHandler`). Uploads at or below
   `FILE_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) become `InMemoryUploadedFile`; larger ones are
   streamed to a `TemporaryUploadedFile` in `FILE_UPLOAD_TEMP_DIR` (system temp by default).
2. Parsing is lazy: it happens on first access to `request.POST` or `request.FILES`.
3. A `ModelForm` (or your code) assigns the `UploadedFile` to a `FileField`. On `instance.save()`,
   `FieldFile.save()` is called via `pre_save`: the field computes a name with `upload_to`, the
   storage's `generate_filename()` → `get_valid_name()` sanitises it, `get_available_name()`
   avoids collisions (appending a random suffix), and `Storage.save()` writes the bytes.
4. Only the returned name (relative to the storage root) is written to the column
   (`varchar(100)` by default — raise `max_length` for deep `upload_to` paths).
5. Accessing `instance.document.url` asks the storage for a URL; `.open()` / `.read()` asks it for
   bytes. Nothing touches storage until you call those.

`DATA_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) caps the **non-file** part of the body;
file content is excluded from it. `DATA_UPLOAD_MAX_NUMBER_FIELDS` (1000) and
`DATA_UPLOAD_MAX_NUMBER_FILES` (100, added in the 3.2.18/4.0.10/4.1.7 security releases) cap
counts. **There is no built-in per-file size limit** — you must enforce it yourself and at the
reverse proxy.

## Core concepts and APIs

- `models.FileField(upload_to=..., storage=..., max_length=...)`; `upload_to` can be a
  `strftime` string or a callable `(instance, filename) -> str`. `storage` may be a callable
  (evaluated at runtime, so different environments can choose different backends without new
  migrations).
- `models.ImageField` — requires Pillow; validates the file opens as an image and can populate
  `width_field` / `height_field`.
- `FieldFile`: `.name`, `.url`, `.size`, `.open()`, `.save(name, content, save=True)`,
  `.delete(save=True)`.
- `django.core.files.File`, `ContentFile(bytes_or_str, name=...)` — wrap content you generate
  (e.g. a PDF) so storage can save it.
- `UploadedFile`: `.name` (client-supplied, untrusted), `.size`, `.content_type` (client-supplied,
  untrusted), `.chunks()`, `.multiple_chunks()`.
- Storage API: `save`, `open`, `delete`, `exists`, `url`, `size`, `listdir`,
  `get_available_name`, `generate_filename`. Access via `default_storage` or
  `from django.core.files.storage import storages; storages["default"]` (4.2+).
- Validators: `FileExtensionValidator`, `validate_image_file_extension`.
- Response helpers: `FileResponse` (streams a file-like object; sets `Content-Disposition`
  with `as_attachment=True, filename=...`).

## Implementation examples

```python
# config/settings.py
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "media"          # never inside STATIC_ROOT or the code tree you serve
MEDIA_URL = "/media/"
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440  # in-memory threshold, not a limit
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440
```

```python
# apps/billing/models.py
import uuid
from django.core.validators import FileExtensionValidator
from django.db import models

def invoice_upload_to(instance: "Invoice", filename: str) -> str:
    # Ignore the client name entirely: no traversal, no PII, no collisions.
    return f"invoices/{instance.issued_on:%Y/%m}/{uuid.uuid4().hex}.pdf"

class Invoice(models.Model):
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT)
    issued_on = models.DateField()
    pdf = models.FileField(
        upload_to=invoice_upload_to,
        max_length=255,
        validators=[FileExtensionValidator(["pdf"])],
    )
```

Size and content validation — check bytes, not the extension or `content_type` alone:

```python
# apps/billing/validators.py
from django.core.exceptions import ValidationError

MAX_PDF_BYTES = 10 * 1024 * 1024

def validate_pdf(upload) -> None:
    if upload.size > MAX_PDF_BYTES:
        raise ValidationError("PDF must be 10 MB or smaller.", code="file_too_large")
    head = upload.read(5)
    upload.seek(0)                      # leave the file readable for storage.save()
    if head != b"%PDF-":
        raise ValidationError("File is not a PDF.", code="invalid_pdf")
```

Generating a file server-side:

```python
# apps/billing/services.py
from django.core.files.base import ContentFile
from django.db import transaction

def attach_invoice_pdf(invoice: Invoice, pdf_bytes: bytes) -> None:
    old_name = invoice.pdf.name
    invoice.pdf.save("invoice.pdf", ContentFile(pdf_bytes), save=True)  # name passes through upload_to
    if old_name:
        storage = invoice.pdf.storage
        transaction.on_commit(lambda: storage.delete(old_name))         # delete only if DB commit succeeds
```

Serving a private file through an authorised view instead of a public URL:

```python
# apps/billing/views.py
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404

@login_required
def download_invoice(request, pk: int):
    invoice = get_object_or_404(Invoice.objects.filter(order__customer=request.user), pk=pk)
    return FileResponse(invoice.pdf.open("rb"), as_attachment=True, filename=f"invoice-{pk}.pdf")
```

## Production patterns

- **Object storage, not local disk,** whenever more than one app instance runs: local
  `MEDIA_ROOT` is per-container and vanishes on redeploy. The common choice is the third-party
  `django-storages` package (S3, GCS, Azure) configured via `STORAGES["default"]`; only add it if
  the project doesn't already use an equivalent.
- **Private vs public buckets.** Public assets (avatars) can use plain URLs + CDN. Private
  documents (invoices) use a private bucket with short-lived **signed URLs** generated by the
  storage backend, or a Django view that authorises then streams/redirects. Never rely on an
  unguessable path as the only access control.
- **Multiple storages:** add aliases (`STORAGES["private"]`) and select with
  `storage=lambda: storages["private"]` on the field.
- **Direct-to-bucket uploads** for large files: the browser uploads with a pre-signed POST/PUT,
  then Django receives only the key and verifies the object exists. This keeps large bodies off
  your workers.
- **Custom storage**: subclass `Storage` (or an existing backend) and decorate with
  `@deconstructible` so migrations can serialise it; implement at least `_open`, `_save`,
  `delete`, `exists`, `url`.
- **Reverse proxy limit** (`client_max_body_size` in nginx or equivalent) is your first
  defence against huge bodies; Django's checks run after the bytes arrive.

## Decision guidance

| Need | Choose |
|---|---|
| Single-server, small app | `FileSystemStorage` + web server serving `MEDIA_URL` (never Django in prod) |
| Multiple instances / containers | Object storage via a storage backend |
| Private per-user files | Private storage + authorised view or signed URLs with short expiry |
| Uploads > ~50 MB | Direct-to-storage pre-signed upload; Django records the key |
| Tests | `InMemoryStorage` (Django ≥ 4.2) or a temp `MEDIA_ROOT` |
| Generated files (PDF, CSV) | `ContentFile` + `FieldFile.save()`; generate in a background task if slow |

## Security considerations

- Treat `UploadedFile.name` and `.content_type` as attacker-controlled. Rename files; check magic
  bytes or parse with a real library (Pillow for images).
- Never serve user uploads from the same origin with inferred content types if HTML/SVG could be
  uploaded — stored XSS. Serve from a separate domain or force `Content-Disposition: attachment`
  and `X-Content-Type-Options: nosniff`.
- `ImageField` verification does not make an image safe to process; decompression bombs exist
  (Pillow has `MAX_IMAGE_PIXELS`). Re-encode images if you transform them.
- Never build storage paths by concatenating user input; storages reject absolute paths and
  `..`, but `upload_to` callables that ignore the client name remove the risk entirely.
- Deep hardening (AV scanning, quarantine buckets) → [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- Uploads above `FILE_UPLOAD_MAX_MEMORY_SIZE` go to disk; ensure the temp dir has space and is
  not a slow network mount.
- Iterate `upload.chunks()` when copying manually; never `upload.read()` a large file into memory.
- `FileResponse` streams; under WSGI the server may use `wsgi.file_wrapper` (sendfile). For
  object storage, redirect to a signed URL rather than proxying bytes through Python.
- `instance.file.size` / `.url` may make a network call on remote storages — avoid in list loops.

## Testing

```python
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class InvoiceUploadTests(TestCase):
    def test_rejects_non_pdf(self):
        fake = SimpleUploadedFile("x.pdf", b"not a pdf", content_type="application/pdf")
        with self.assertRaises(ValidationError):
            validate_pdf(fake)
```

- Post multipart data with `self.client.post(url, {"pdf": fake})`.
- If you must use disk, point `MEDIA_ROOT` at `tempfile.mkdtemp()` and remove it in
  `tearDownClass`; never write into the real media directory.
- Test cleanup paths (old file deleted after replacement, nothing deleted on rollback).
- Broader strategy → [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- Assuming deleting a model instance deletes its file — **it doesn't** (by design since 1.3,
  because the transaction may roll back and files may be shared). Clean up explicitly.
- Bad: deleting files in a `post_delete` signal synchronously → file gone even if the surrounding
  transaction rolls back. Good: `transaction.on_commit(lambda: storage.delete(name))`, or a
  periodic orphan-sweeper command.
- Trusting `FILE_UPLOAD_MAX_MEMORY_SIZE` as an upload limit; it is only the memory/disk threshold.
- Keeping `max_length=100` with long date-based paths → `DataError` on save in PostgreSQL.
- Serving `MEDIA_URL` with `django.views.static.serve` in production.
- Storing the absolute URL in the database; store the name and let storage build the URL.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `request.FILES` empty | Form lacks `enctype="multipart/form-data"`, or field name mismatch |
| `RequestDataTooBig` | Non-file POST data exceeds `DATA_UPLOAD_MAX_MEMORY_SIZE` |
| `TooManyFilesSent` | More files than `DATA_UPLOAD_MAX_NUMBER_FILES` |
| 413 before Django logs anything | Reverse proxy body limit |
| `SuspiciousFileOperation` | Generated name escapes storage root or is absolute |
| URL 404 in dev | `MEDIA_URL` not added to urlpatterns via `static()` (DEBUG only) |
| Files missing after deploy | Local `MEDIA_ROOT` on ephemeral container disk |
| Name gets random suffix | `get_available_name` avoiding a collision — expected |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/files/
- https://docs.djangoproject.com/en/stable/topics/http/file-uploads/
- https://docs.djangoproject.com/en/stable/ref/files/storage/
- https://docs.djangoproject.com/en/stable/ref/files/uploads/
- https://docs.djangoproject.com/en/stable/howto/custom-file-storage/
- https://docs.djangoproject.com/en/stable/ref/models/fields/#filefield
- https://docs.djangoproject.com/en/stable/ref/settings/#storages
