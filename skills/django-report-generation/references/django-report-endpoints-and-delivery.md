# Django Report Endpoints and Delivery

> **Scope:** The HTTP surface of a reporting subsystem — download views, `FileResponse` and
> `StreamingHttpResponse`, content types, `Content-Disposition` and filenames, permission-checked
> lookup, DRF endpoints for requesting runs and reading their metadata, signed and expiring
> download URLs, web-server offload (`X-Accel-Redirect`), HTTP caching, Range requests, error
> semantics and the `ReportDownload` audit record. **Not here:** generating the bytes (format
> references), the background job ([background-report-generation.md](background-report-generation.md)),
> threat analysis and retention policy ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)),
> general DRF mechanics ([django-rest-framework](../../django-rest-framework/SKILL.md)).

## 1. Purpose and scope

A report endpoint is a **data-access boundary that happens to return a file**. It must answer
three questions on every request: *who* is asking, *may they see this run's data*, and *how are
the bytes delivered without tying up an application worker longer than necessary*.

This reference covers:

- Synchronous "generate and return" views for small reports.
- Streaming responses for row-oriented exports (CSV, JSONL).
- Serving stored artifacts produced by background runs.
- API endpoints to request a run, poll status and obtain a download link.
- Signed, expiring URLs — app-signed (`django.core.signing`) and storage-presigned (S3/Azure).
- Response headers: `Content-Type`, `Content-Disposition`, `Cache-Control`, `X-Content-Type-Options`.
- Download auditing.

It assumes the canonical `ReportRun` / `ReportArtifact` / `ReportDownload` models defined in
[reporting-architecture-and-design.md](reporting-architecture-and-design.md).

## 2. Core concepts

### Delivery modes

| Mode | When | Mechanism | Worker held for |
|---|---|---|---|
| Synchronous render | Small, bounded output (< ~`ReportSpec.max_sync_rows`, < ~2 s) | `HttpResponse` / `FileResponse(BytesIO)` | Whole generation |
| Streaming render | Row-oriented formats (CSV, JSONL), moderate size | `StreamingHttpResponse(generator)` | Whole stream duration |
| Stored artifact via app | Artifact exists in private storage, moderate traffic | `FileResponse(storage.open(...))` | Transfer duration |
| Stored artifact via web server | Large files, high traffic, same host/volume | `X-Accel-Redirect` / `X-Sendfile` | Milliseconds |
| Stored artifact via presigned URL | Object storage (S3, Azure Blob) | Redirect to time-limited URL | Milliseconds |

### Response classes [Django]

- `HttpResponse` — whole body in memory; fine for small outputs.
- `StreamingHttpResponse` — iterates a generator; no `Content-Length`, cannot be cached by
  middleware that needs the body; accepts async iterators (Django ≥ 4.2).
- `FileResponse` — subclass of `StreamingHttpResponse` optimised for file-like objects; sets
  `Content-Type` (guessed from filename if not given), `Content-Length` when the size is
  knowable, and `Content-Disposition` when `as_attachment=True` or `filename=` is passed. Uses
  `wsgi.file_wrapper` when available.

### Attachment vs inline

- `attachment` — browser saves the file. Default for every report format that is not meant to be
  viewed in the browser, and **always** for CSV/XLSX/DOCX.
- `inline` — browser renders it (PDF preview). Only for formats the browser renders safely and
  only from a response that also sends `X-Content-Type-Options: nosniff`. Never inline HTML
  produced from user data on the application origin.

### Content types

| Format | Content-Type |
|---|---|
| CSV | `text/csv; charset=utf-8` |
| JSON | `application/json` |
| JSON Lines | `application/x-ndjson` (no IANA registration; widely used) [VERIFY: confirm consumer expectations; `application/jsonl` is also seen] |
| XML | `application/xml` |
| PDF | `application/pdf` |
| XLSX | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |
| ZIP bundle | `application/zip` |
| PNG chart | `image/png` |

Store the content type on `ReportArtifact.content_type` at generation time; never derive it from
a user-supplied filename at download time.

## 3. Internal mechanics

### How `FileResponse` builds headers [Django]

1. If `filename` is passed (or derivable from the file object's `name`) and
   `as_attachment=True`, it emits `Content-Disposition: attachment; filename="..."`. Non-ASCII
   names are emitted with the RFC 5987 `filename*=utf-8''...` form.
   [VERIFY: exact header form produced by `django.utils.http.content_disposition_header` (Django ≥ 4.2) for non-ASCII and quote-containing names]
2. `Content-Length` is set from the file size when the object exposes it (`os.fstat`, `getbuffer`,
   or `size`), otherwise omitted.
3. The file object is closed when the response is closed — opening storage files without a
   `with` block is correct here because the response owns the handle.

### Streaming and the server stack

```text
generator → Django StreamingHttpResponse → WSGI/ASGI server → reverse proxy → client
```

- Each `yield` becomes a chunk only if every hop forwards it. Nginx buffers proxied responses by
  default; send `X-Accel-Buffering: no` or disable `proxy_buffering` for that location.
- Gunicorn sync workers enforce a worker timeout (default 30 s [VERIFY: Gunicorn `timeout` default and whether it applies to in-progress streaming responses for sync vs gthread workers])
  — a slow stream can be killed mid-file, producing a **truncated download that looks complete**
  to the user unless the format has a trailer they check.
- Middleware that reads `response.content` (e.g. some compression or debug middleware) breaks or
  buffers streaming responses. `GZipMiddleware` handles streaming responses but compresses
  per-chunk. [VERIFY: GZipMiddleware streaming behaviour in Django 5.2]
- Database connections stay open for the life of a generator iterating a QuerySet; under ASGI the
  sync iterator runs in a thread. See [django-core — asynchronous-django](../../django-core/references/asynchronous-django.md).

### Signed URLs

Two different mechanisms, often confused:

| Mechanism | Who verifies | Revocable early | Owned by |
|---|---|---|---|
| App-signed token (`TimestampSigner`) in a Django URL | Your Django view on every hit | Yes — view can re-check run status/permissions | [Django] `django.core.signing` |
| Storage presigned URL (S3 SigV4, Azure SAS) | The storage provider | No (until expiry), unless keys/policy rotated | [third-party: django-storages] + [external service] |

An app-signed URL is a **capability token plus a re-check**; a presigned URL is a **pure
capability** — anyone holding it within the window can download. Keep presigned lifetimes short
(minutes) and issue them only from a permission-checked view.

### Range requests

Django's `FileResponse` does not implement `Range`/`206 Partial Content`.
[VERIFY: confirm no Range support in Django 5.2 `FileResponse`] Resumable downloads of large
artifacts therefore require the web server (`X-Accel-Redirect` — nginx honours Range on internal
locations) or object storage (S3/Azure honour Range on presigned URLs).

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Escalate when | Trade-off |
|---|---|---|---|
| Sync vs async generation | Sync only below `ReportSpec.max_sync_rows` and a ~2 s budget | Larger, PDF/XLSX with many rows → background run | Sync is simpler but holds a worker and risks timeouts |
| Streaming vs stored artifact | Stream CSV/JSONL when the user wants it now and it is moderate | Repeat downloads, audit, retries, huge size → stored artifact | Streams are not resumable, not reproducible, and fail silently mid-transfer |
| Serving stored files | `FileResponse` from private storage through a checked view | High traffic / large files → X-Accel-Redirect or presigned URL | App-served is simplest and fully audited; offload frees workers |
| Link type | App-signed URL that re-checks permission | Object storage at scale → presigned redirect issued per click | Presigned URLs cannot be revoked within their window |
| Link lifetime | Short (minutes) for presigned; hours/days for app-signed links in emails, with re-check | — | Long-lived presigned links leak via forwarding, logs, browser history |
| Status codes | 404 for runs outside caller scope; 410 for expired artifacts; 409 for "not ready" | — | 403 leaks existence; 200 with error body breaks clients |
| Filename source | Server-generated from spec key, scope, period, timestamp | — | User-provided names invite traversal and header injection |

## 5. Implementation patterns

### Scoped lookup — the single choke point

```python
# reports/selectors.py
def visible_runs(*, user, tenant) -> QuerySet[ReportRun]:
    qs = ReportRun.objects.filter(tenant=tenant).select_related("artifact")
    if user.has_perm("reports.view_all_tenant_reports"):
        return qs
    return qs.filter(requested_by=user)          # plus schedule-recipient rules if applicable


def get_visible_run(*, user, tenant, run_id) -> ReportRun:
    run = get_object_or_404(visible_runs(user=user, tenant=tenant), pk=run_id)
    spec = get_spec(run.report_key)
    if not user.has_perm(spec.permission):       # report-level permission re-checked at download
        raise Http404
    return run
```

Every endpoint — HTML view, DRF view, signed-link view — goes through `get_visible_run`. A run
belonging to another user or tenant is indistinguishable from a non-existent one.

### Synchronous small report [Django]

```python
@login_required
@require_GET
def invoice_pdf(request, invoice_id):
    invoice = get_object_or_404(invoices_for(user=request.user, tenant=request.tenant), pk=invoice_id)
    pdf_bytes = render_invoice_pdf(invoice)                     # see pdf-report-generation.md
    response = FileResponse(io.BytesIO(pdf_bytes), as_attachment=True,
                            filename=f"invoice-{invoice.number}.pdf",
                            content_type="application/pdf")
    patch_cache_control(response, private=True, no_store=True)
    return response
```

`invoice.number` is server-generated; still pass it through `get_valid_filename` if any part
could contain user text.

### Streaming CSV [Django] + [stdlib]

```python
def export_orders_csv(request):
    form = OrdersExportForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    rows = orders_export_rows(user=request.user, tenant=request.tenant, **form.cleaned_data)
    response = StreamingHttpResponse(stream_csv(rows), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True, filename=report_filename("orders", request.tenant, form.cleaned_data, "csv"))
    response["X-Accel-Buffering"] = "no"
    patch_cache_control(response, private=True, no_store=True)
    return response
```

`stream_csv` (pseudo-buffer writer, `safe_cell`) lives in
[csv-and-structured-report-generation.md](csv-and-structured-report-generation.md). Validate
parameters **before** creating the response — once streaming starts, the status code is already
sent and errors can only truncate the body.

### Server-generated filenames [Django]

```python
from django.utils.text import get_valid_filename, slugify

def report_filename(key: str, tenant, params: dict, ext: str, *, at=None) -> str:
    at = at or timezone.now()
    period = params.get("period_label", "")
    parts = [slugify(key.replace(".", "-")), slugify(tenant.slug), slugify(period),
             at.strftime("%Y%m%dT%H%MZ")]
    return get_valid_filename("-".join(p for p in parts if p) + f".{ext}")
```

- `ReportArtifact.filename` is computed once at generation and stored; downloads reuse it.
- `ReportArtifact.storage_name` is an opaque path (`reports/<tenant_id>/<run_id>.<ext>`) — never
  derived from the display filename.

### Serving a stored artifact [Django]

```python
@login_required
@require_GET
def download_run(request, run_id):
    run = get_visible_run(user=request.user, tenant=request.tenant, run_id=run_id)
    if run.status == ReportRun.Status.EXPIRED or not hasattr(run, "artifact") \
            or run.artifact.deleted_at or run.artifact.expires_at <= timezone.now():
        return HttpResponse(status=410)                          # Gone
    if run.status != ReportRun.Status.SUCCEEDED:
        return HttpResponse(status=409)                          # not ready / failed
    artifact = run.artifact
    record_download(artifact=artifact, request=request, via="view")
    response = FileResponse(report_storage.open(artifact.storage_name, "rb"),
                            as_attachment=True, filename=artifact.filename,
                            content_type=artifact.content_type)
    patch_cache_control(response, private=True, no_store=True)
    return response
```

### Offloading to nginx with `X-Accel-Redirect` [external tool]

```python
response = HttpResponse(content_type=artifact.content_type)
response["X-Accel-Redirect"] = f"/_protected_reports/{artifact.storage_name}"
response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=artifact.filename)
patch_cache_control(response, private=True, no_store=True)
return response
```

```nginx
location /_protected_reports/ {
    internal;                       # unreachable directly from clients
    alias /srv/report-artifacts/;
}
```

Django did the authorisation and audit; nginx streams the bytes and handles Range. `storage_name`
must be server-generated — it is concatenated into a path.

### Presigned redirect for object storage [third-party: django-storages] [external service]

```python
def download_run_presigned(request, run_id):
    run = get_visible_run(user=request.user, tenant=request.tenant, run_id=run_id)
    artifact = ensure_downloadable(run)                    # raises Gone/Conflict as above
    record_download(artifact=artifact, request=request, via="signed_url")
    url = report_storage.url(artifact.storage_name, expire=120,
                             parameters={"ResponseContentDisposition":
                                 content_disposition_header(as_attachment=True, filename=artifact.filename)})
    return HttpResponseRedirect(url)
```

[VERIFY: `S3Storage.url()` signature (`parameters`, `expire`) and support for `ResponseContentDisposition` in the installed django-storages version; Azure backend uses a different mechanism (`expiration_secs`, SAS)]

Record the download **when the URL is issued** — the app never sees the actual transfer.

### App-signed link for emails [Django]

```python
signer = TimestampSigner(salt="reports.download.v1")

def issue_download_url(*, run, user) -> str:
    token = signer.sign_object({"run": str(run.pk), "user": user.pk})
    return settings.SITE_URL + reverse("reports:signed-download", args=[token])

def signed_download(request, token):
    try:
        data = signer.unsign_object(token, max_age=timedelta(days=3))
    except signing.SignatureExpired:
        return HttpResponse(status=410)
    except signing.BadSignature:
        raise Http404
    if not request.user.is_authenticated or request.user.pk != data["user"]:
        return redirect_to_login(request.get_full_path())
    run = get_visible_run(user=request.user, tenant=request.tenant, run_id=data["run"])
    ...  # same as download_run
```

[VERIFY: `TimestampSigner.unsign_object(..., max_age=...)` accepts a `timedelta` in Django 5.2]
The token binds run *and* recipient; the view still requires login and re-runs the permission
check, so a forwarded email does not grant access. If the business requires login-free links,
that is a security decision documented in
[reporting-security-and-compliance.md](reporting-security-and-compliance.md).

### DRF endpoints [DRF]

Keep the API thin: it requests runs and exposes metadata; it does not generate files.

```python
class ReportRunViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                       mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = ReportRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return visible_runs(user=self.request.user, tenant=self.request.tenant)

    def create(self, request):
        s = ReportRunRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        run = request_report(user=request.user, tenant=request.tenant, **s.validated_data)
        headers = {"Location": reverse("api:report-run-detail", args=[run.pk], request=request)}
        return Response(ReportRunSerializer(run).data, status=status.HTTP_202_ACCEPTED, headers=headers)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        run = self.get_object()
        return Response({"url": issue_short_lived_url(run=run, user=request.user), "expires_in": 120})
```

- `202 Accepted` + `Location` for async; the response body includes `status`, `requested_at`.
- `ReportRunSerializer` is **read-only** and lists explicit fields: `id`, `report_key`,
  `format`, `status`, `requested_at`, `finished_at`, `data_as_of`, `row_count`, `error_code`,
  `error_message`, `size_bytes`, `expires_at`. Never expose `storage_name`, `celery_task_id`,
  raw exception text or `params_hash`.
- Return a link rather than file bytes from the API. If file bytes must come from DRF, return a
  Django `FileResponse` from the action — DRF renderers are for serialisable data. See
  [django-rest-framework — parsers-renderers-and-content-negotiation](../../django-rest-framework/references/parsers-renderers-and-content-negotiation.md).
- Throttle `create` per user/tenant ([throttling-and-rate-limiting](../../django-rest-framework/references/throttling-and-rate-limiting.md)).

### Download audit [Django]

```python
# reports/models.py (continued) — canonical audit model
class ReportDownload(models.Model):
    class Via(models.TextChoices):
        VIEW = "view"; SIGNED_URL = "signed_url"

    artifact = models.ForeignKey("reports.ReportArtifact", on_delete=models.PROTECT, related_name="downloads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    downloaded_at = models.DateTimeField(db_default=Now())
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)
    via = models.CharField(max_length=12, choices=Via)

    class Meta:
        indexes = [models.Index(fields=["artifact", "-downloaded_at"], name="reportdownload_artifact_recent")]
```

`PROTECT` keeps audit rows when artifacts are soft-deleted; the application never updates or
deletes these rows ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)).

```python
def record_download(*, artifact, request, via: str) -> None:
    ReportDownload.objects.create(
        artifact=artifact, user=request.user, via=via,
        ip=client_ip(request),                       # trusted-proxy aware helper
        user_agent=request.headers.get("User-Agent", "")[:256])
```

Write the audit row before returning the response; if the audit insert fails, fail the download
for `restricted` classifications rather than serve an unaudited file.

## 6. Production use cases

| Use case | Endpoint design |
|---|---|
| Invoice PDF from the customer portal | Synchronous view, scoped by customer/tenant, `attachment`, `no-store` |
| "Export orders" button, up to 50k rows | Streaming CSV with validated filters, `X-Accel-Buffering: no` |
| Monthly payroll register (XLSX, restricted) | `POST` → 202; poll; download via app view with audit; no presigned URL |
| Weekly sales report emailed to managers | App-signed link in email → login → permission re-check → presigned redirect |
| Bulk export of 5 M ledger entries | Background run, artifact in S3, presigned URL valid 2 minutes, Range supported by S3 |
| Partner API pulls daily report | DRF `GET /reports/runs/?report_key=...` + `download` action returning a short-lived URL |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Sync `FileResponse` | Simple, immediate, testable | Holds worker; bounded by request timeout |
| `StreamingHttpResponse` | Constant memory, starts fast | No retry/resume, truncation invisible, proxy buffering pitfalls, holds DB connection |
| App-served stored artifact | Full authz and audit per byte-stream | App worker busy during transfer |
| `X-Accel-Redirect` | Worker freed, Range support, authz retained | Requires shared filesystem with nginx; ops coupling |
| Presigned URL | Scales infinitely, Range support | Bearer capability until expiry; audit only at issuance |
| App-signed email link | Revocable via re-check, bound to recipient | Requires login (friction) |

## 8. Security considerations

- **IDOR**: every lookup through `visible_runs`; UUID primary keys reduce guessing but are **not**
  authorisation. Return 404 for out-of-scope runs.
- **Report-level permission** re-checked at download — permissions may have been revoked after
  the run was requested.
- **Headers**: `Cache-Control: private, no-store` on every report response; Django's
  `SecurityMiddleware` sends `X-Content-Type-Options: nosniff` when
  `SECURE_CONTENT_TYPE_NOSNIFF=True` (default) [VERIFY: default value in Django 5.2].
- **Filenames**: server-generated; `get_valid_filename`; never place user strings raw into
  `Content-Disposition` (header injection, quotes, CR/LF).
- **Storage paths**: `storage_name` never contains user input; prevents path traversal through
  `X-Accel-Redirect` or storage APIs.
- **Presigned URLs**: short expiry, issued per click, private bucket, no public ACL, avoid logging
  the full URL (the signature is a credential).
- **Referer leakage**: a page displaying a signed URL should set `Referrer-Policy` so the URL is
  not leaked to third-party resources.
- **Inline rendering**: never inline HTML reports from the app origin.
- Threat model and policy: [reporting-security-and-compliance.md](reporting-security-and-compliance.md),
  [django-security-engineering — authorization-and-access-control](../../django-security-engineering/references/authorization-and-access-control.md).

## 9. Performance considerations

- Streaming holds a web worker **and** a DB connection for the full transfer; size the worker
  pool and `CONN_MAX_AGE` accordingly, or move large exports to background runs.
- `FileResponse` over object storage proxies every byte through Django — CPU, memory buffers
  and egress twice. Prefer presigned redirects above a size threshold (e.g. 20 MB).
- Do not compress already-compressed formats (XLSX, DOCX, PDF, ZIP); exclude them from
  compression middleware/proxy config.
- Metadata endpoints (`GET /runs/`) are paginated and use `select_related("artifact")`.
- Polling: return `Retry-After` on 202/409 responses to reduce polling pressure; or push
  notifications on completion.
- Performance depth: [report-generation-performance.md](report-generation-performance.md),
  [django-performance — http-and-static-delivery-performance](../../django-performance/references/http-and-static-delivery-performance.md).

## 10. Common mistakes and anti-patterns

- `ReportRun.objects.get(pk=run_id)` in the download view — unscoped lookup (IDOR).
- Public bucket or public `MEDIA_URL` for report files; permanent URLs in emails.
- Building a 200 MB XLSX inside the request and returning `HttpResponse(buffer.getvalue())`.
- Validating parameters inside the streaming generator — errors arrive after `200 OK`.
- Using `filename=request.GET["name"]`.
- Returning `403` for other tenants' runs (confirms existence).
- `Cache-Control: public` or no cache header on authenticated report responses behind a CDN.
- Logging the full presigned URL at INFO level.
- Returning file bytes through a DRF serializer as base64.
- Checking permissions only when the run is requested, never at download.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Download stalls then appears all at once | Proxy buffering | `X-Accel-Buffering: no`, nginx `proxy_buffering` |
| File truncated, no error | Worker timeout killed stream | Gunicorn timeout/logs; move to background run |
| Browser shows file inline / wrong name | Missing/incorrect `Content-Disposition` | Inspect headers with `curl -I` |
| Non-ASCII filename garbled | No `filename*` form | Use `FileResponse(filename=...)` / `content_disposition_header` |
| Presigned URL returns 403 | Clock skew, expired, wrong region/signature version | Storage provider logs; URL `X-Amz-Expires` |
| Excel says file is corrupt | HTML error page saved as `.xlsx` | Status code check in client; never 200 on errors |
| 410 for a fresh run | `expires_at` computed in naive/local time | Aware datetimes in UTC |

Validation checklist:

- [ ] Every report endpoint uses `get_visible_run` (or equivalent scoped selector).
- [ ] Other-user and other-tenant tests return 404 ([report-generation-testing.md](report-generation-testing.md)).
- [ ] `Content-Type`, `Content-Disposition`, `Cache-Control: private, no-store` asserted in tests.
- [ ] Expired → 410; not ready → 409 (or 202 with status); failed → 409 with `error_code`.
- [ ] A `ReportDownload` row is written for each download or URL issuance.
- [ ] Streaming endpoints validate input before streaming and are excluded from buffering.

## 12. Related references and official documentation

- [reporting-architecture-and-design.md](reporting-architecture-and-design.md) — canonical models.
- [background-report-generation.md](background-report-generation.md) — producing the artifact.
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) — links in emails.
- [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) — streaming CSV writer.
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — threat model, retention.
- [report-generation-performance.md](report-generation-performance.md) — streaming vs buffering.
- [django-core — file-handling](../../django-core/references/file-handling.md)
- [django-rest-framework — views-and-viewsets](../../django-rest-framework/references/views-and-viewsets.md)
- [django-rest-framework — permissions-and-access-control](../../django-rest-framework/references/permissions-and-access-control.md)
- [django-security-engineering — secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md)
- [django-integrations — file-transfer-and-object-storage](../../django-integrations/references/file-transfer-and-object-storage.md)
- [django-data-processing — data-import-and-export-systems](../../django-data-processing/references/data-import-and-export-systems.md)
- <https://docs.djangoproject.com/en/5.2/ref/request-response/>
- <https://docs.djangoproject.com/en/5.2/howto/outputting-csv/>
- <https://docs.djangoproject.com/en/5.2/topics/signing/>
- <https://docs.djangoproject.com/en/5.2/ref/utils/#module-django.utils.http>
- <https://www.django-rest-framework.org/api-guide/viewsets/>
- <https://django-storages.readthedocs.io/>
- <https://nginx.org/en/docs/http/ngx_http_core_module.html#internal>
- <https://www.rfc-editor.org/rfc/rfc6266>
