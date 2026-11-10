# File Transfer and Object Storage

**Scope:** How Django applications exchange files with external systems: object storage (S3-compatible, Azure Blob) behind Django's storage API, presigned/SAS URLs, multipart and chunked transfers, streaming downloads, checksums and integrity, SFTP/FTPS, file-drop integrations, large-file processing, lifecycle management and external document systems. Django's file/storage framework mechanics belong to [file-handling.md](../../django-core/references/file-handling.md); upload validation and malicious-content defences to [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md); throughput tuning of file pipelines to [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md); cloud credential setup to [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md) and [azure-integrations.md](azure-integrations.md).

## 1. Purpose and scope

Baseline: Python 3.12+, Django 5.2 LTS (`STORAGES` setting, Django ≥ 4.2), PostgreSQL for metadata, Celery 5.x where background processing is used.

Files cross integration boundaries in four ways:

1. **User ↔ object storage** — uploads/downloads, ideally direct with short-lived signed URLs.
2. **Django ↔ object storage** — the app reads/writes generated documents, exports, attachments.
3. **System ↔ system file exchange** — partners and legacy systems push/pull files over SFTP/FTPS or into a bucket ("file drop").
4. **Django ↔ document management systems** — SharePoint/OneDrive (Microsoft Graph), Box, Google Drive, ECM platforms via their APIs.

Each needs the same engineering answers: who owns the file, how integrity is proven, how a transfer is resumed or made idempotent, how access is limited in time and scope, and how lifecycle (retention, deletion) is enforced.

Third-party packages referenced: [third-party: django-storages], [third-party: boto3], [third-party: azure-storage-blob], [third-party: paramiko], [third-party: asyncssh], [third-party: msgraph-sdk]. None assumed installed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Object storage** | Flat key→object namespace with metadata; whole-object PUT (or multipart); no in-place edits, no rename (copy+delete), no directory semantics. |
| **Storage abstraction** | Django `Storage` API (`save`, `open`, `url`, `delete`, `exists`) — lets models and services stay backend-agnostic. |
| **Presigned URL / SAS** | Time-limited URL whose query string carries a signature authorising one operation on one object. Bearer credential: whoever holds it can use it. |
| **Multipart upload (S3) / block upload (Azure)** | Large object uploaded in independently retried parts, then committed atomically. Uncommitted parts cost storage until aborted. |
| **Chunked transfer** | Application-level splitting (resumable uploads, `tus` protocol, HTTP range requests) — distinct from HTTP chunked transfer-encoding. |
| **Checksum** | Hash proving content integrity end to end (MD5, SHA-256, CRC32C/CRC64). |
| **ETag** | Opaque version identifier; equals MD5 only for single-part, non-KMS-encrypted S3 PUTs — never treat as a general content hash. |
| **Manifest / done-file** | A sidecar file declaring a batch is complete (file list, sizes, checksums, record counts). |
| **Claim-check** | Store a large payload in storage and send only a reference through a message broker. |
| **Lifecycle rule** | Storage-side policy that transitions or deletes objects by age/prefix/tag. |

## 3. Internal mechanics

### 3.1 Django storages

`STORAGES` (Django ≥ 4.2) maps aliases (`"default"`, `"staticfiles"`, custom) to backends. `FileField` uses `default` unless given `storage=` (a callable is allowed, evaluated lazily). `FieldFile.save()` calls `Storage.save()` → `get_available_name()` (may append random suffix if the name exists and overwriting is off) → `_save()`. For S3, `_save()` performs a PUT or multipart upload via boto3's transfer manager; `url()` returns either a public URL or a presigned URL (when `querystring_auth=True`, the default in django-storages' S3 backend `VERIFY`).

`exists()` on object storage is a network call (HEAD); avoid it in loops. `size`, `url` and `open` likewise hit the network.

### 3.2 Presigned URLs

- **S3:** SigV4 signature over method, bucket, key, selected headers and expiry. Max expiry 7 days with IAM user credentials; with temporary (role) credentials the URL also expires when the session token does `VERIFY`. A presigned POST policy can enforce `content-length-range`, `Content-Type` prefix and key prefix — a presigned PUT cannot enforce size.
- **Azure:** SAS token with permissions, start/expiry, optional IP range and protocol. User delegation SAS (Entra-signed) preferred over account-key SAS; see [azure-integrations.md](azure-integrations.md).
- Signing is **local computation** — generating a URL does not check that the object exists or that the identity has permission. Errors surface only when the client uses it.

### 3.3 Multipart / block uploads

S3: `CreateMultipartUpload` → `UploadPart` (5 MiB–5 GiB per part, max 10,000 parts, object max 5 TiB `VERIFY`) → `CompleteMultipartUpload` with the part list. Parts can be retried independently and uploaded in parallel. Incomplete uploads persist (and are billed) until `AbortMultipartUpload` or a lifecycle rule `AbortIncompleteMultipartUpload` cleans them.

Azure: `Put Block` (staged, uncommitted for up to 7 days `VERIFY`) → `Put Block List` commits. Max 50,000 blocks.

boto3's `upload_file`/`upload_fileobj` and Azure's `upload_blob` switch to multipart automatically above a threshold (`TransferConfig.multipart_threshold`, `max_single_put_size`).

### 3.4 Checksums and integrity

| Mechanism | Guarantees | Caveats |
|---|---|---|
| `Content-MD5` header on PUT | Service rejects the upload if the body doesn't match | MD5 is fine for corruption detection, not for adversarial integrity |
| S3 additional checksums (`ChecksumAlgorithm=SHA256/CRC32/CRC32C/CRC64NVME`) | Service verifies and stores; multipart gives checksum-of-checksums ("composite") unless full-object mode is selected `VERIFY` | Recent SDKs compute CRC checksums by default (botocore ≥ 1.36 "data integrity protections") which can break some S3-compatible stores `VERIFY` |
| S3 ETag | Changes when content changes | Multipart ETag = MD5 of part MD5s + `-N`; SSE-KMS ETags are not MD5 |
| Azure `Content-MD5` / `x-ms-content-crc64` | Per-request transactional integrity; blob-level `Content-MD5` stored when set | Not computed automatically for block blobs uploaded in blocks |
| Application SHA-256 stored in DB | End-to-end integrity independent of provider | You compute it (streaming); costs CPU on large files |

Rule: store your own SHA-256 (and size) in your metadata table for any file that crosses a trust boundary or is used as evidence; verify it on receipt and before processing.

### 3.5 SFTP vs FTPS

- **SFTP** = SSH File Transfer Protocol over SSH (port 22). Single connection, key-based auth, host key verification. Not FTP.
- **FTPS** = FTP over TLS (explicit `AUTH TLS` on port 21, or implicit on 990). Separate control and data connections; passive ports through firewalls; TLS session reuse required by many servers (a known pain point with Python's `ftplib`).
- Plain FTP: credentials in cleartext — never across untrusted networks.

### 3.6 Streaming

`StreamingHttpResponse` iterates a generator; `FileResponse` streams a file-like object using `wsgi.file_wrapper` (sendfile) where available. Under ASGI, a sync iterator in `StreamingHttpResponse` is consumed in a thread with a warning; provide an async iterator (Django ≥ 4.2) `VERIFY` exact warning behaviour. Streaming from object storage through Django ties up a worker for the whole download — prefer a redirect to a presigned URL, or X-Accel-Redirect/X-Sendfile to the reverse proxy.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Alternative | When the alternative wins |
|---|---|---|---|
| Browser upload path | Direct-to-storage via presigned POST/SAS, then server-side confirmation | Upload through Django | Small files (< a few MB), need synchronous validation/transformation, or storage not reachable from clients |
| Download path | Redirect to short-lived presigned URL after authorisation check | Stream via Django / X-Accel-Redirect | Must hide storage hostnames, apply watermarking, or audit bytes served |
| Backend abstraction | Django `STORAGES` + django-storages | Direct boto3/azure SDK calls | Features the Storage API lacks: multipart control, tags, lifecycle, conditional writes, presigned POST policies — wrap those in an adapter, not scattered SDK calls |
| Partner bulk exchange | Bucket drop zone per partner (cross-account role / SAS) | SFTP | Partner can only do SFTP/FTPS (common in finance, healthcare, retail) — consider a managed SFTP front (AWS Transfer Family, Azure Blob SFTP) that lands files in object storage |
| Batch completeness signal | Manifest/done-file with checksums | Event per file | Single-file exchanges |
| Integrity | Own SHA-256 + provider checksum | Provider checksum only | Internal, non-evidentiary files |
| Large file processing | Stream in chunks from storage in a background job | Load into memory | Never for unbounded sizes |
| Retention | Storage lifecycle rules + DB metadata state | Cron job deleting files | Deletion depends on business state (legal hold, account closure) — then app-driven, logged deletion |

## 5. Django implementation patterns

### 5.1 Storage configuration

```python
# config/settings/base.py
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",  # django-storages ≥ 1.14 path VERIFY
        "OPTIONS": {
            "bucket_name": os.environ["MEDIA_BUCKET"],
            "region_name": os.environ.get("AWS_REGION", "eu-west-1"),
            "default_acl": None,            # rely on bucket policy; Object Ownership = bucket owner enforced
            "querystring_auth": True,       # private objects → presigned URLs
            "querystring_expire": 300,
            "file_overwrite": False,
            "object_parameters": {"ServerSideEncryption": "aws:kms"},
        },
    },
    "exports": {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
        "OPTIONS": {
            "account_name": os.environ["AZURE_STORAGE_ACCOUNT"],
            "azure_container": "exports",
            "token_credential": None,  # set in AppConfig.ready() from azure_credential(); VERIFY option name
            "expiration_secs": 300,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
```

Credentials come from the workload identity chain — no keys in `OPTIONS`.

```python
# apps/documents/models.py
import uuid

from django.core.files.storage import storages
from django.db import models


def document_path(instance: "Document", filename: str) -> str:
    # server-generated, unguessable, no user-controlled path segments
    return f"documents/{instance.tenant_id}/{uuid.uuid4()}"


def private_storage():
    return storages["default"]


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING_UPLOAD = "pending_upload"
        UPLOADED = "uploaded"
        AVAILABLE = "available"
        REJECTED = "rejected"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT)
    file = models.FileField(upload_to=document_path, storage=private_storage, max_length=255, blank=True)
    original_name = models.CharField(max_length=255)  # display only, never used as a key
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(null=True)
    sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING_UPLOAD)
    created_at = models.DateTimeField(auto_now_add=True)
```

### 5.2 Direct upload with presigned POST, then confirmation

```python
# apps/documents/integrations/s3/client.py
from apps.integrations.aws import s3
from django.conf import settings

MAX_BYTES = 50 * 1024 * 1024


def presigned_upload(key: str, content_type: str) -> dict:
    return s3().generate_presigned_post(
        Bucket=settings.MEDIA_BUCKET,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_BYTES],
            {"x-amz-checksum-algorithm": "SHA256"},  # VERIFY support in POST policies
        ],
        ExpiresIn=300,
    )


def head(key: str) -> dict:
    return s3().head_object(Bucket=settings.MEDIA_BUCKET, Key=key, ChecksumMode="ENABLED")
```

```python
# apps/documents/services.py
from django.db import transaction

from .integrations.s3 import client as s3_client
from .models import Document, document_path
from .tasks import verify_document


def start_upload(*, tenant, original_name: str, content_type: str) -> tuple[Document, dict]:
    if content_type not in {"application/pdf", "image/png", "image/jpeg"}:
        raise ValueError("unsupported type")
    doc = Document(tenant=tenant, original_name=original_name[:255], content_type=content_type)
    doc.file.name = document_path(doc, original_name)  # reserve the key, no bytes yet
    doc.save()
    return doc, s3_client.presigned_upload(doc.file.name, content_type)


def confirm_upload(*, document: Document) -> None:
    meta = s3_client.head(document.file.name)  # proves the object exists; client claims are not trusted
    with transaction.atomic():
        Document.objects.filter(pk=document.pk, status=Document.Status.PENDING_UPLOAD).update(
            status=Document.Status.UPLOADED, size=meta["ContentLength"],
        )
        transaction.on_commit(lambda: verify_document.delay(document.pk))  # sniff type, hash, scan
```

`verify_document` streams the object, computes SHA-256, checks magic bytes, optionally submits to a malware scanner, and moves the status to `available` or `rejected` (and deletes rejected objects). Event-driven alternative: S3 `ObjectCreated` → SQS → consumer, which removes the need for client confirmation but must map key → document.

Sweep `pending_upload` rows older than the URL expiry plus margin, and abort incomplete multipart uploads with a lifecycle rule.

### 5.3 Authorised downloads

```python
# apps/documents/views.py
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404


def download(request, pk):
    doc = get_object_or_404(Document.objects.for_user(request.user), pk=pk, status=Document.Status.AVAILABLE)
    url = doc.file.storage.url(
        doc.file.name,
        parameters={"ResponseContentDisposition": f'attachment; filename="{safe_ascii(doc.original_name)}"'},
        expire=60,
    )  # django-storages S3 supports parameters/expire VERIFY
    audit_log("document.download", user=request.user, document=doc)
    return HttpResponseRedirect(url)
```

Authorisation happens in Django; the URL only carries it for 60 seconds. Set `Cache-Control: no-store` if the redirect response could be cached by a proxy.

### 5.4 Streaming export generated by Django

```python
import csv

from django.http import StreamingHttpResponse


class Echo:
    def write(self, value):
        return value


def export_orders(request):
    writer = csv.writer(Echo())
    rows = Order.objects.for_user(request.user).values_list("id", "total", "created_at").iterator(chunk_size=2000)
    response = StreamingHttpResponse((writer.writerow(r) for r in rows), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders.csv"'
    return response
```

For exports beyond tens of seconds, generate in a background job into the `exports` storage and email/notify a presigned link instead — a proxy or load-balancer idle timeout will cut long streams. See [django-data-processing](../../django-data-processing/SKILL.md) for pipeline design.

### 5.5 Streaming download from storage with a running hash

```python
import hashlib

from django.core.files.storage import storages


def sha256_of(name: str, storage_alias: str = "default", chunk: int = 1024 * 1024) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with storages[storage_alias].open(name, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size
```

Never `read()` the whole object; memory per worker is finite (see [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md)).

### 5.6 Large server-side uploads with tuned transfer

```python
from boto3.s3.transfer import TransferConfig

TRANSFER = TransferConfig(multipart_threshold=16 * 1024**2, multipart_chunksize=16 * 1024**2, max_concurrency=4)

s3().upload_file(
    local_path, settings.ARCHIVE_BUCKET, key,
    ExtraArgs={"ChecksumAlgorithm": "SHA256", "ServerSideEncryption": "aws:kms"},
    Config=TRANSFER,
)
```

Use `If-None-Match: *` conditional writes (S3 supports since 2024 `VERIFY`; Azure via `if_none_match="*"`) when two workers might write the same key and only the first should win.

### 5.7 SFTP with pinned host keys

```python
# apps/partners/integrations/acme_sftp/client.py
import io
import posixpath

import paramiko
from django.conf import settings

from apps.integrations.errors import PermanentIntegrationError, TransientIntegrationError


def _connect() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.load_host_keys(settings.ACME_SFTP_KNOWN_HOSTS_FILE)   # pinned; provided via secret mount
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())   # never AutoAddPolicy in production
    pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(get_secret("acme-sftp-key")))
    try:
        ssh.connect(
            settings.ACME_SFTP_HOST, port=22, username=settings.ACME_SFTP_USER, pkey=pkey,
            timeout=10, banner_timeout=15, auth_timeout=15,
            allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException as exc:
        raise PermanentIntegrationError(provider="acme_sftp", operation="connect") from exc
    except (paramiko.SSHException, OSError) as exc:
        raise TransientIntegrationError(provider="acme_sftp", operation="connect") from exc
    return ssh


def upload_atomic(local_path: str, remote_dir: str, name: str) -> None:
    ssh = _connect()
    try:
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(60)
        tmp = posixpath.join(remote_dir, f".{name}.part")
        sftp.put(local_path, tmp, confirm=True)       # confirm=True stats size after upload
        sftp.posix_rename(tmp, posixpath.join(remote_dir, name))  # requires posix-rename extension VERIFY server support
    finally:
        ssh.close()
```

- Upload to a temporary name and rename, so the partner never picks up a half-written file. If the server lacks `posix-rename`, use `rename` (fails if target exists) or agree on a done-file.
- Host key fingerprint changes are a security event, not something to auto-accept.
- [third-party: asyncssh] is the async alternative with similar host-key (`known_hosts=`) controls.
- SFTP sessions are stateful and slow to establish; batch files per connection, and run transfers in background jobs, never in a request.

### 5.8 FTPS when SFTP is not available

```python
import ssl
from ftplib import FTP_TLS

ctx = ssl.create_default_context()  # verifies certificates and hostnames
with FTP_TLS(context=ctx, timeout=30) as ftp:
    ftp.connect(settings.LEGACY_FTPS_HOST, 21)
    ftp.login(settings.LEGACY_FTPS_USER, get_secret("legacy-ftps-password"))
    ftp.prot_p()                     # encrypt the data channel too — without it, file contents are cleartext
    with open(local_path, "rb") as fh:
        ftp.storbinary(f"STOR {name}.tmp", fh)
    ftp.rename(f"{name}.tmp", name)
```

Many FTPS servers require TLS session reuse on the data channel, which stdlib `ftplib` does not do by default; a small `FTP_TLS` subclass overriding `ntransfercmd` to pass `session=self.sock.session` is the common workaround `VERIFY` against the target server.

### 5.9 File-drop ingestion with manifests and idempotency

```python
# apps/partners/models.py
class InboundFile(models.Model):
    class Status(models.TextChoices):
        DISCOVERED = "discovered"; PROCESSING = "processing"; PROCESSED = "processed"; FAILED = "failed"; QUARANTINED = "quarantined"

    source = models.CharField(max_length=50)          # "acme_sftp", "s3://partner-drop/acme/"
    remote_name = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    batch_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.DISCOVERED)
    records_total = models.PositiveIntegerField(null=True)
    records_failed = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    discovered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "sha256"], name="inbound_file_content_uniq")]
```

Flow:

1. **Discover** — list the drop location; only consider batches whose manifest/done-file exists (`orders_20261104.csv` + `orders_20261104.manifest.json` with names, sizes, SHA-256, record counts).
2. **Copy into owned storage** (staging bucket/container) and compute SHA-256; compare with the manifest. Mismatch → `quarantined`, alert.
3. **Register** `InboundFile` — the unique `(source, sha256)` constraint makes re-delivery of the same content a no-op, regardless of filename. Decide explicitly whether the same name with different content is a correction (supersedes) or an error.
4. **Process** in a background job, streaming rows; upsert by the business key with per-record error capture so one bad row doesn't fail the batch (unless the contract says all-or-nothing). Record counts are reconciled against the manifest — see [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md).
5. **Acknowledge** to the partner (move to `processed/`, write an ack/reject file) only after commit.
6. **Archive** raw files immutably (object lock / immutable blob policy where required) for audit and replay.

### 5.10 External document systems (pattern level)

SharePoint/OneDrive via Microsoft Graph [third-party: msgraph-sdk] or plain HTTPS:

- Authenticate as an application with client credentials (certificate preferred) or workload identity federation; scope with `Sites.Selected` rather than tenant-wide `Sites.ReadWrite.All` ([authentication-and-identity-integrations.md](authentication-and-identity-integrations.md)).
- Uploads > 4 MB use an **upload session** (resumable, chunked ranges, typically multiples of 320 KiB `VERIFY`).
- Store the external `driveItem` id and `eTag`/`cTag` in `ExternalReference`; never rely on paths (users rename/move).
- Use delta queries for change sync with a stored delta link in `SyncCheckpoint`.
- Respect throttling (429 with `Retry-After`).

Box, Google Drive and ECM systems follow the same shape: app identity, resumable uploads, external ids, change cursors.

### 5.11 Lifecycle management

```json
{
  "Rules": [
    {"ID": "abort-incomplete-mpu", "Status": "Enabled", "Filter": {"Prefix": ""},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}},
    {"ID": "exports-expire", "Status": "Enabled", "Filter": {"Prefix": "exports/"},
     "Expiration": {"Days": 7}},
    {"ID": "inbound-archive", "Status": "Enabled", "Filter": {"Prefix": "inbound/archive/"},
     "Transitions": [{"Days": 30, "StorageClass": "GLACIER_IR"}]}
  ]
}
```

Lifecycle rules act asynchronously (up to ~a day delay `VERIFY`); the DB row must be updated in step (a nightly job marks rows whose objects expired, or reads the key before trusting it). For deletions driven by business state (GDPR erasure, legal hold), delete from the application with an audit log entry and handle versioned buckets (delete markers vs version deletion).

## 6. Production use cases

1. **Customer document vault** — presigned POST uploads, async verification and scanning, authorised 60-second download redirects, SHA-256 stored for evidence.
2. **Nightly partner order feed over SFTP** — Celery beat (or cron/Scheduler) pulls files with pinned host keys, verifies manifest checksums, imports idempotently, writes ack file.
3. **Bank statement/settlement files** — FTPS or SFTP with PGP-encrypted payloads; decrypt in memory-bounded streams; reconcile totals ([payment-and-financial-integrations.md](payment-and-financial-integrations.md)).
4. **Large report exports** — background job writes a 2 GB CSV via multipart upload to `exports/`, user gets a 24-hour link, lifecycle expires after 7 days.
5. **Contract archive in SharePoint** — on contract signature, Django uploads PDF via Graph upload session, stores `driveItem` id, syncs metadata changes via delta queries.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Direct-to-storage uploads | No worker time or bandwidth through Django; scales | Validation is after the fact; orphaned objects; CORS configuration on bucket |
| Upload through Django | Synchronous validation; simple | Workers blocked on slow clients; memory/temp disk; request size limits |
| Presigned download redirects | Offload bandwidth; CDN-friendly | URL is a bearer token; logs/referrers may capture it; revocation only by expiry/key rotation |
| Object storage via Storage API | Backend-agnostic, testable | Lowest-common-denominator features; hidden network calls (`exists`, `size`) |
| SFTP | Universally supported by enterprises; key auth | Stateful, slow, firewall/IP allow-listing, no native completeness signal |
| FTPS | Legacy compatibility | Firewall complexity, session reuse issues, weaker tooling |
| File-drop with manifests | Explicit completeness and integrity | Partner must implement manifests correctly |

## 8. Security and performance considerations

Security (details in [integration-security.md](integration-security.md) and [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)):

- Private buckets/containers; Block Public Access on; no public ACLs.
- Server-generated keys — never use client filenames as storage keys (path traversal, overwrite, enumeration).
- Short presigned expiries (minutes for uploads, ≤ minutes for downloads); POST policies to cap size and type.
- Strip presigned query strings from logs, error reports and analytics.
- Validate content after upload (magic bytes, size, scanning) before marking available; serve with `Content-Disposition: attachment` and correct `Content-Type` to avoid stored XSS from the storage origin.
- SFTP: pinned host keys, key-based auth, per-partner chroot users, IP allow-lists both ways.
- FTPS: verify certificates; `prot_p()` always.
- Encryption at rest (SSE-KMS / customer-managed keys where compliance requires) and in transit (TLS only bucket policy).
- Parsing partner files: CSV injection when re-exported to spreadsheets; XML → defused parsers ([soap-and-legacy-integrations.md](soap-and-legacy-integrations.md)); ZIP bombs and path traversal in archives.

Performance (see [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md)):

- Stream; constant memory regardless of file size.
- Parallel multipart parts for throughput; bound concurrency per worker.
- Avoid `storage.exists()`/`size` in loops; store metadata in PostgreSQL.
- Don't stream large files through gunicorn sync workers; redirect or offload to the proxy.
- Same-region storage and compute; VPC/private endpoints to avoid NAT costs.

## 9. Common mistakes and anti-patterns

- Trusting the client's claim that an upload finished — always HEAD/verify server-side.
- Using the S3 ETag as an MD5/content hash.
- Presigned PUT without size limits for untrusted users.
- Long-lived (days) presigned URLs emailed to users or embedded in HTML that gets cached.
- Reading whole files into memory (`.read()`, `readall()`).
- `paramiko.AutoAddPolicy()` in production.
- Processing a partner file while it is still being written (no rename/done-file protocol).
- Keying idempotency on filename only — partners resend the same name with corrected content, or the same content with a new name.
- No lifecycle rule for incomplete multipart uploads.
- Storage writes inside `transaction.atomic()` treated as rolled back on DB failure — storage is not transactional; clean up orphans.
- Deleting the DB row but not the object (or vice versa) without a reconciliation job.
- Calling SFTP or Graph in a web request.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `SignatureDoesNotMatch` on presigned URL | Client sent different headers (Content-Type) than signed; clock skew; wrong region endpoint | Compare signed vs sent headers; region-specific endpoint (`addressing_style`, `signature_version="s3v4"`) |
| 403 on presigned URL that worked before | Signing role credentials expired; KMS key permission missing | Expiry vs session token lifetime; KMS key policy |
| Browser upload blocked | Bucket CORS missing origin/method/headers | Bucket CORS config; preflight response |
| `XAmzContentSHA256Mismatch` / checksum errors against MinIO/other S3-compatible stores | New SDK default checksums unsupported | `request_checksum_calculation="when_required"` in botocore Config `VERIFY` |
| Storage bill growing without data growth | Incomplete multipart uploads, old versions | Lifecycle rules; `ListMultipartUploads` |
| SFTP `SSHException: Server ... not found in known_hosts` | Host key not pinned or rotated by partner | Confirm new fingerprint out-of-band, update secret |
| FTPS data transfer hangs/`425` | Passive ports blocked; TLS session reuse required | Firewall rules; session-reuse subclass |
| Duplicate import of partner file | Idempotency keyed on name; manifest absent | `InboundFile` unique on content hash |
| Download streams cut at ~60 s | Proxy/LB idle timeout | Redirect to presigned URL or background export |

Validation checklist:

- [ ] Storage configured via `STORAGES`; no credentials in settings; buckets/containers private.
- [ ] Keys server-generated; original names stored only as metadata.
- [ ] Uploads verified server-side (existence, size, type, hash, scan) before use.
- [ ] Presigned/SAS URLs short-lived, scoped to one object/operation, never logged.
- [ ] Large transfers stream; multipart thresholds and concurrency set; incomplete uploads auto-aborted.
- [ ] Partner exchanges use temp-name + rename or manifests; content-hash idempotency.
- [ ] SFTP host keys pinned; FTPS uses `prot_p()` and verified certificates.
- [ ] Lifecycle/retention rules exist and match DB state; deletion is audited.
- [ ] Tests stub storage (in-memory storage, [third-party: moto], Azurite) and simulate partial transfers ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md) — boto3 configuration, IAM, VPC endpoints.
- [azure-integrations.md](azure-integrations.md) — Blob clients, user delegation SAS, managed identity.
- [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md) — record-level import, reconciliation of counts.
- [integration-security.md](integration-security.md) — credential protection, redaction.
- [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) — Graph/app identities.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retries and idempotency.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — running transfers in tasks.
- [file-handling.md](../../django-core/references/file-handling.md) — Django File/Storage API mechanics.
- [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md) — upload validation, scanning, serving.
- [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md) — throughput and memory.
- [django-data-processing](../../django-data-processing/SKILL.md) — import/export pipelines.

## 12. Official documentation and external references

- Django file storage API: https://docs.djangoproject.com/en/5.2/ref/files/storage/
- Django `STORAGES` setting: https://docs.djangoproject.com/en/5.2/ref/settings/#storages
- Django `StreamingHttpResponse` / `FileResponse`: https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects
- django-storages documentation: https://django-storages.readthedocs.io/
- Amazon S3 presigned URLs: https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
- S3 POST policy: https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-HTTPPOSTConstructPolicy.html
- S3 multipart upload: https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html
- S3 checking object integrity: https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html
- S3 conditional writes: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- S3 lifecycle configuration: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html
- Boto3 S3 transfer configuration: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/customizations/s3.html
- Azure Blob client library (Python): https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme
- Azure block blobs (Put Block / Put Block List): https://learn.microsoft.com/rest/api/storageservices/understanding-block-blobs--append-blobs--and-page-blobs
- Azure Blob lifecycle management: https://learn.microsoft.com/azure/storage/blobs/lifecycle-management-overview
- Azure Blob SFTP support: https://learn.microsoft.com/azure/storage/blobs/secure-file-transfer-protocol-support
- AWS Transfer Family: https://docs.aws.amazon.com/transfer/latest/userguide/what-is-aws-transfer-family.html
- Python `ftplib.FTP_TLS`: https://docs.python.org/3/library/ftplib.html#ftplib.FTP_TLS
- Python `hashlib`: https://docs.python.org/3/library/hashlib.html
- Paramiko documentation: https://docs.paramiko.org/
- AsyncSSH documentation: https://asyncssh.readthedocs.io/
- SFTP protocol draft (filexfer): https://datatracker.ietf.org/doc/html/draft-ietf-secsh-filexfer-02
- FTP security extensions (FTPS), RFC 4217: https://www.rfc-editor.org/rfc/rfc4217
- Microsoft Graph upload sessions: https://learn.microsoft.com/graph/api/driveitem-createuploadsession
- Microsoft Graph delta query: https://learn.microsoft.com/graph/delta-query-overview
- tus resumable upload protocol: https://tus.io/protocols/resumable-upload
