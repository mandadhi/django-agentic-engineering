# File Upload Security

> Scope: how Django receives uploads; size, count and memory limits; type and content
> validation; MIME spoofing; filenames and path traversal; storage permissions; private media
> and safe serving; malware scanning; image and document processing risks; upload-specific
> testing.
> Not covered here: storage backend and `FileField` mechanics
> ([django-core file-handling](../../django-core/references/file-handling.md)); DRF parsers and
> multipart handling ([django-rest-framework parsers](../../django-rest-framework/references/parsers-renderers-and-content-negotiation.md));
> API-wide input limits ([api-security.md](./api-security.md)); CSP and `nosniff` header
> configuration ([cors-and-browser-security.md](./cors-and-browser-security.md)); fetching
> files from user-supplied URLs ([ssrf-prevention.md](./ssrf-prevention.md)); large import
> pipelines ([django-data-processing](../../django-data-processing/SKILL.md)).

## 1. Purpose and scope

An upload endpoint lets an untrusted party write bytes onto your infrastructure and, usually,
have them served back to other users. The attacks follow from that: exhaust disk or memory,
write outside the intended directory, upload content the browser executes on your origin
(stored XSS via HTML/SVG), exploit parsers (images, PDFs, archives, Office documents), deliver
malware to other users, or read other users' private files through predictable URLs.

Django gives you safe plumbing (streamed parsing, filename sanitisation, storage path checks).
It does **not** decide what types are acceptable, inspect content, scan for malware, enforce
per-file size limits, or authorise downloads — those are application responsibilities.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Upload handler | Object that receives multipart chunks: `MemoryFileUploadHandler` (small files) then `TemporaryFileUploadHandler` (disk) by default |
| Buffering threshold | `FILE_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB default) — above it a file is streamed to a temp file; it is **not** a size limit |
| Declared type | `UploadedFile.content_type` — copied from the client's multipart header; attacker-controlled |
| Detected type | Type determined from file bytes (magic numbers, full parse) |
| Polyglot | A file valid in two formats at once (e.g. GIF + JavaScript, JPEG + HTML) |
| Private media | Files that require authorisation to read; must not be reachable by a guessable public URL |
| Serving origin | The scheme/host the file is served from; content served from the app origin can read its cookies and DOM |
| Quarantine | Storage area where files wait until scanning/validation completes |

Framework vs developer responsibility:

| Django does | You must |
|---|---|
| Streams multipart bodies, spills to temp files | Cap request size at the proxy and per-file size in validation |
| Limits non-file POST data (`DATA_UPLOAD_MAX_MEMORY_SIZE`) and field/file counts | Choose allowed types and verify content |
| Strips directory components from client filenames; storage rejects traversal | Generate your own filenames; never build paths from user input |
| `ImageField` checks the file opens as an image (Pillow) | Re-encode images, cap pixel counts, treat Pillow as a parser attack surface |
| Provides `FileResponse`, storage APIs | Authorise downloads, set `Content-Disposition`, serve from a separate origin |

## 3. Internal mechanics

**Request parsing.** `request.FILES` is populated lazily by `MultiPartParser` when `request.POST`
or `request.FILES` is first accessed. Each handler in `FILE_UPLOAD_HANDLERS` receives chunks;
`MemoryFileUploadHandler` keeps a file in memory only if the request's content length is below
`FILE_UPLOAD_MAX_MEMORY_SIZE`, otherwise `TemporaryFileUploadHandler` writes it to
`FILE_UPLOAD_TEMP_DIR` (system temp by default). Result: `InMemoryUploadedFile` or
`TemporaryUploadedFile`.

**Limits Django enforces:**

| Setting | Default | Effect |
|---|---|---|
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | 2.5 MB | Max size of request body **excluding file upload data**; raises `RequestDataTooBig` → 400 |
| `DATA_UPLOAD_MAX_NUMBER_FIELDS` | 1000 | Max GET/POST parameters; `TooManyFieldsSent` → 400 |
| `DATA_UPLOAD_MAX_NUMBER_FILES` | 100 | Max files per multipart request; `TooManyFilesSent` → 400 (added in the Feb 2023 security releases, present in Django 4.2+) |
| `FILE_UPLOAD_MAX_MEMORY_SIZE` | 2.5 MB | In-memory vs temp-file threshold only |

Nothing above bounds the size of an individual uploaded file. A 20 GB upload will be streamed to
temp disk unless the proxy or a custom upload handler stops it.

**Filename handling.** The parser reduces the client filename to its base name. On save,
`Storage.generate_filename()` → `get_valid_name()` (`django.utils.text.get_valid_filename`:
strips unsafe characters, spaces → underscores) and `get_available_name()` (appends a random
suffix on collision). `FileSystemStorage` resolves paths with `safe_join` and raises
`SuspiciousFileOperation` if the result escapes `location`. Several CVEs have been fixed in
this area (e.g. path traversal via crafted names in `get_available_name()` for derived storages,
CVE-2024-39330, fixed in 4.2.14 / 5.0.7 — `VERIFY` exact versions) — staying on the latest patch
release matters.

**Permissions.** `FILE_UPLOAD_PERMISSIONS` defaults to `0o644` (Django ≥ 3.0);
`FILE_UPLOAD_DIRECTORY_PERMISSIONS` defaults to `None` (process umask).

**Image validation.** `forms.ImageField.to_python()` opens the file with Pillow and calls
`verify()`, then sets `content_type` from Pillow's detected format. `verify()` checks structural
integrity of a limited set of formats; it does not decode the full image and does not prove the
file is harmless — polyglots pass. `models.ImageField` adds `validate_image_file_extension`.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Where to cap size | Proxy (`client_max_body_size`), custom upload handler, form validator | All three: proxy for capacity, validator for per-field business limits; a handler only if you must abort mid-stream |
| Type check depth | Extension → magic bytes → full parse/re-encode | Extension allow-list always; magic bytes for documents; full decode + re-encode for images |
| Storage visibility | Public bucket/CDN, private bucket + signed URLs, private + proxying view | Default private; public only for content intended for anonymous viewing after validation |
| Serving origin | App origin, separate "user content" domain, object store domain | Never the app origin for user content; a separate registrable domain is strongest |
| Scanning | Synchronous, async with quarantine, none | Async with quarantine for files shared with other users; synchronous only for small files and low latency budgets |
| Processing location | Web worker, background worker, isolated sandbox | Parse untrusted documents outside the web process, ideally in a sandboxed container with no network |
| Original vs derived | Serve original, serve re-encoded derivative | Serve derivatives for images; keep originals private if needed for audit |

## 5. Django implementation patterns

### 5.1 Model with server-generated names and private storage

```python
# documents/models.py
import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models

from .validators import validate_file_size, validate_pdf_content

ALLOWED_DOC_EXTENSIONS = ["pdf"]


def document_upload_to(instance: "Document", filename: str) -> str:
    # Never use the client's name or path: only a normalised, allow-listed extension survives.
    ext = PurePosixPath(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_DOC_EXTENSIONS:
        ext = "bin"
    return f"documents/{instance.tenant_id}/{uuid.uuid4().hex}.{ext}"


class Document(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    file = models.FileField(
        upload_to=document_upload_to,
        # Default storage is private (see 5.4); pass storage=<callable> for a dedicated backend.
        validators=[
            FileExtensionValidator(ALLOWED_DOC_EXTENSIONS),
            validate_file_size,
            validate_pdf_content,
        ],
    )
    original_name = models.CharField(max_length=255)  # display only, never a path
    status = models.CharField(max_length=16, default="pending")  # pending|clean|rejected
    created_at = models.DateTimeField(auto_now_add=True)
```

Store the client's original filename separately (truncated, for display) and escape it when
rendering. Model-field validators run in `full_clean()` — ModelForms and DRF `ModelSerializer`
run them; direct `Model.objects.create()` does not.

### 5.2 Size and content validators

```python
# documents/validators.py
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


def validate_file_size(f) -> None:
    if f.size > MAX_DOCUMENT_BYTES:
        raise ValidationError(_("File too large (max 10 MB)."), code="file_too_large")


def validate_pdf_content(f) -> None:
    pos = f.tell()
    try:
        f.seek(0)
        head = f.read(1024)
    finally:
        f.seek(pos)
    if not head.startswith(PDF_MAGIC):
        raise ValidationError(_("File is not a PDF."), code="invalid_file_type")
```

For many formats, `python-magic` (third-party, wraps libmagic) or `filetype` (third-party,
pure Python) provide detection. Magic bytes prove the start of the file, not the absence of
other content — combine with re-encoding or isolated parsing.

**Never** trust `f.content_type`; it is whatever the client sent.

### 5.3 Images: decode, bound, re-encode

```python
# media/images.py
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000  # Pillow default ~89M; lower it for your use case
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def sanitise_image(uploaded) -> ContentFile:
    uploaded.seek(0)
    try:
        with Image.open(uploaded) as img:
            if img.format not in ALLOWED_FORMATS:
                raise ValidationError("Unsupported image format.", code="invalid_image")
            img = ImageOps.exif_transpose(img)  # apply orientation before metadata is dropped
            img.thumbnail((4096, 4096))
            out = BytesIO()
            # Re-encoding drops EXIF (GPS, device), embedded payloads and trailing data.
            img.convert("RGB").save(out, format="JPEG", quality=85)
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValidationError("Invalid image.", code="invalid_image") from exc
    return ContentFile(out.getvalue())
```

Pillow raises `DecompressionBombWarning` above `MAX_IMAGE_PIXELS` and `DecompressionBombError`
above twice that; consider promoting the warning to an error with
`warnings.simplefilter("error", Image.DecompressionBombWarning)`. Keep Pillow patched — image
codecs are a recurring CVE source. Do not accept SVG as an "image" unless you sanitise it as
XML/HTML or rasterise it server-side; SVG can carry `<script>`.

### 5.4 Private storage and authorised downloads

```python
# settings/production.py (Django >= 4.2 STORAGES)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",  # django-storages (third-party)
        "OPTIONS": {
            "bucket_name": env("PRIVATE_MEDIA_BUCKET"),
            "default_acl": "private",
            "querystring_auth": True,       # presigned URLs
            "querystring_expire": 300,      # seconds
            "file_overwrite": False,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
```

With S3, setting `custom_domain` typically disables query-string signing — do not combine a
public CDN domain with private files without CloudFront signed URLs (`VERIFY` against your
django-storages version).

Authorised download view (any storage):

```python
# documents/views.py
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect

from .models import Document
from .selectors import documents_visible_to


@login_required
def download_document(request, pk):
    doc = get_object_or_404(documents_visible_to(request.user), pk=pk)  # 404, not 403
    if doc.status != "clean":
        raise Http404
    if hasattr(doc.file.storage, "bucket"):  # object storage: short-lived presigned URL
        return redirect(doc.file.url)
    response = FileResponse(
        doc.file.open("rb"),
        as_attachment=True,
        filename=doc.original_name,  # FileResponse encodes it in Content-Disposition safely
        content_type="application/pdf",  # server-chosen, never the client's
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
```

For large files on local disk, let the web server stream after Django authorises:

```python
response = HttpResponse()
response["X-Accel-Redirect"] = f"/protected/{doc.file.name}"  # nginx `internal;` location
response["Content-Type"] = "application/pdf"
response["Content-Disposition"] = "attachment"
return response
```

The nginx location must be `internal` so clients cannot request `/protected/...` directly.
Validate that `doc.file.name` came from your database, not the request.

### 5.5 Quarantine and asynchronous scanning

```python
# documents/services.py
from django.db import transaction

from .tasks import scan_document


def accept_upload(*, user, tenant, uploaded) -> Document:
    doc = Document(tenant=tenant, owner=user, file=uploaded,
                   original_name=uploaded.name[:255], status="pending")
    doc.full_clean()
    doc.save()
    transaction.on_commit(lambda: scan_document.delay(doc.pk))
    return doc
```

The task streams the file to a scanner (ClamAV `clamd` via a client library — third-party —
or a cloud malware-scanning service), sets `status="clean"` or `"rejected"`, and deletes or
moves rejected files. Downloads and processing check `status`. See
[django-integrations](../../django-integrations/SKILL.md) for task reliability.

### 5.6 Archives and documents

```python
import zipfile
from pathlib import Path

MAX_MEMBERS, MAX_TOTAL = 1000, 200 * 1024 * 1024


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_TOTAL:
            raise ValueError("Archive too large")  # declared sizes; also cap while streaming
        for m in members:
            target = (dest / m.filename).resolve()
            if not target.is_relative_to(dest):  # zip slip
                raise ValueError("Unsafe path in archive")
        zf.extractall(dest)
```

Declared sizes in the central directory can lie; for hostile input also count bytes while
reading each member. Prefer processing members in memory without extracting to disk at all.

Document converters (LibreOffice, Ghostscript, ImageMagick, wkhtmltopdf) have large attack
surfaces: run them in a separate container with no network, a read-only filesystem, CPU/memory
and time limits; restrict ImageMagick coders with `policy.xml` (disable `MSL`, `URL`, `PS`,
`EPS`, `PDF` unless needed). Office files may contain macros — never open them server-side with
macro execution, and warn users when serving them.

## 6. Production use cases

- **Avatars:** extension + Pillow decode, pixel cap, re-encode to WebP/JPEG at fixed sizes,
  serve derivatives from a cookieless CDN domain; originals discarded.
- **Invoices / KYC documents:** private bucket, tenant-prefixed keys, async scan with quarantine,
  presigned URLs ≤ 5 minutes, audit log of every download
  ([security-logging-and-auditing.md](./security-logging-and-auditing.md)).
- **CSV/XLSX imports:** size and row caps, parse in a worker, formula-injection-safe exports
  (prefix cells beginning with `=`, `+`, `-`, `@` with `'` when re-exporting) — pipeline design
  in [django-data-processing](../../django-data-processing/SKILL.md).
- **User-generated attachments in messaging:** always `Content-Disposition: attachment`, separate
  content domain, per-file authorisation.

## 7. Advantages and disadvantages

| Control | Advantage | Disadvantage |
|---|---|---|
| Server-generated names | Kills traversal, overwrite and name-based XSS | Need a separate display-name column |
| Re-encoding images | Removes polyglots, metadata, trailing payloads | CPU cost; may lose quality/animation/colour profiles |
| Separate content domain | Uploaded HTML/SVG cannot touch app cookies or DOM | DNS/TLS/CDN setup; CORS if JS fetches files |
| Presigned URLs | Offloads bandwidth; time-limited | URL is a bearer credential until expiry; can be shared/leaked in logs |
| Proxying view / X-Accel-Redirect | Per-request authorisation and audit | Web tier involvement; config must keep internal path internal |
| Async scanning | No latency on upload | Files exist in a pending state; UI and access checks must handle it |

## 8. Security and performance considerations

- Set the proxy body limit (`client_max_body_size`, ALB/CloudFront limits) to the largest
  legitimate upload; this is the only control that stops bytes before Django reads them.
- Temp files land in `FILE_UPLOAD_TEMP_DIR`; put it on a volume with quota so floods cannot fill
  the root disk, and make sure it is not web-served.
- Never place `MEDIA_ROOT` inside `STATIC_ROOT` or a directory the web server executes
  (PHP/CGI handlers) — and never serve media with `django.views.static.serve` in production.
- Uploaded HTML, SVG, XML and PDF can execute script in browsers. Serve with
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, a restrictive CSP on the
  content domain (`default-src 'none'; sandbox`), and a server-chosen `Content-Type`.
- `FileExtensionValidator` checks the name only; on its own it is a usability filter, not a
  security control.
- Object keys should not be guessable *and* must still be authorised — unguessability is
  defence in depth, not access control.
- Image and document parsing are CPU- and memory-bound: run them in workers with resource limits
  to keep a malicious upload from starving web processes.
- Delete files when their record is deleted (Django does not delete files on model delete) and
  apply retention to quarantine areas.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| `if f.content_type == "image/png"` as the type check | Trivially spoofed | Detect from content; re-encode |
| `upload_to=lambda i, name: f"uploads/{name}"` kept with user name | Name collisions, odd characters, information leak | UUID names; store display name separately |
| `open(os.path.join(MEDIA_ROOT, request.GET["path"]))` | Path traversal / arbitrary file read | Look up the file by DB id through a scoped queryset |
| Media served from the app domain with inline disposition | Stored XSS with session access | Separate domain, attachment, nosniff |
| Relying on `FILE_UPLOAD_MAX_MEMORY_SIZE` as a size limit | Unlimited uploads to temp disk | Proxy limit + size validator |
| Public bucket for "private" files with random names | Anyone with a leaked URL reads it forever | Private ACL + short presigned URLs |
| Processing PDFs/Office docs in the web process | RCE or DoS in the main app | Sandboxed worker |
| `zipfile.extractall()` on untrusted archives without checks | Zip slip, zip bombs | Validate members and sizes; avoid disk extraction |
| Scanning only on download, or not at all, for shared files | Malware distribution | Scan before files become visible to others |

## 10. Troubleshooting and validation

- **413 from proxy vs 400 from Django:** 413 = proxy body limit; 400 with `RequestDataTooBig`,
  `TooManyFieldsSent` or `TooManyFilesSent` in logs = Django `DATA_UPLOAD_*` limits.
- **`SuspiciousFileOperation`:** a generated name escaped the storage root or contained path
  components — inspect `upload_to`; never "fix" by catching it.
- **Validators not running:** you saved with `objects.create()` or `save()` without
  `full_clean()`; validate in the form/serializer or service.
- **Presigned URL returns 403:** clock skew, expired URL, or bucket policy denies; signed URLs
  absent entirely usually means `custom_domain` is set.

Tests (see [security-testing-and-incident-response.md](./security-testing-and-incident-response.md)):

```python
from django.core.files.uploadedfile import SimpleUploadedFile


def test_rejects_html_disguised_as_pdf(client, user):
    client.force_login(user)
    evil = SimpleUploadedFile("invoice.pdf", b"<html><script>alert(1)</script>",
                              content_type="application/pdf")
    resp = client.post("/documents/upload/", {"file": evil})
    assert resp.status_code == 400 or "file" in resp.context["form"].errors


def test_other_users_document_is_404(client, user, other_users_document):
    client.force_login(user)
    assert client.get(f"/documents/{other_users_document.pk}/download/").status_code == 404
```

Also test: oversize file, too many files, traversal-like names (`../../x.pdf`, `x.pdf\x00.html`),
SVG with script, decompression-bomb PNG, pending/rejected documents not downloadable, response
headers (`Content-Disposition`, `nosniff`, `Content-Type`).

## 11. Related references and skills

- [cross-site-scripting.md](./cross-site-scripting.md) — stored XSS via uploaded HTML/SVG and filenames.
- [cors-and-browser-security.md](./cors-and-browser-security.md) — `nosniff`, CSP for content domains.
- [authorization-and-access-control.md](./authorization-and-access-control.md) — download authorisation, IDOR.
- [multi-tenant-security.md](./multi-tenant-security.md) — tenant-prefixed storage and isolation.
- [ssrf-prevention.md](./ssrf-prevention.md) — "upload from URL" features.
- [api-security.md](./api-security.md) — upload endpoints in DRF.
- [django-core file-handling](../../django-core/references/file-handling.md) · [DRF parsers](../../django-rest-framework/references/parsers-renderers-and-content-negotiation.md) · [django-integrations](../../django-integrations/SKILL.md) · [django-data-processing](../../django-data-processing/SKILL.md)

## 12. Official documentation and external references

- File uploads: https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- Upload settings (`DATA_UPLOAD_*`, `FILE_UPLOAD_*`): https://docs.djangoproject.com/en/5.2/ref/settings/#file-uploads
- File storage API: https://docs.djangoproject.com/en/5.2/ref/files/storage/
- Security — user-uploaded content: https://docs.djangoproject.com/en/5.2/topics/security/#user-uploaded-content
- `FileResponse`: https://docs.djangoproject.com/en/5.2/ref/request-response/#fileresponse-objects
- Django security releases archive: https://docs.djangoproject.com/en/dev/releases/security/
- OWASP File Upload Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- Pillow decompression bomb protection: https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.open
- django-storages (third-party): https://django-storages.readthedocs.io/
- nginx `X-Accel-Redirect`: https://nginx.org/en/docs/http/ngx_http_core_module.html#internal
- ImageMagick security policy: https://imagemagick.org/script/security-policy.php
