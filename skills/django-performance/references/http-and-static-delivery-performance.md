# HTTP and Static Delivery Performance

**Scope:** Performance of bytes on the wire between Django and the client — static and media
delivery, CDNs, compression, Cache-Control, ETags and conditional requests, connection reuse,
HTTP/2 and HTTP/3, payload size and server-rendered page delivery. Application-level caching
belongs to [caching-engineering.md](caching-engineering.md).

## 1. Purpose and scope

Much perceived latency is not Django's: it is DNS, TLS, round trips, transfer size and
re-downloading unchanged assets. This reference helps an agent:

- Keep static and media files off Python workers entirely.
- Set cache headers that let browsers and CDNs avoid requests or transfers.
- Use conditional requests so unchanged resources cost a 304, not a full render.
- Choose where compression and protocol upgrades happen (proxy/CDN, not Gunicorn).
- Measure TTFB, transfer size and cache hit ratio at the edge.

Template and view rendering cost: [django-application-performance.md](django-application-performance.md).
Frontend build tooling: [django-frontend](../../django-frontend/SKILL.md).

## 2. Core concepts

| Concept | Performance effect |
|---|---|
| TTFB | Server think time + network; Django owns the think-time part |
| Transfer time | Size ÷ bandwidth, dominated by payload on mobile networks |
| Round trips | Each new TCP+TLS connection costs 1–3 RTTs before the first byte |
| Freshness (`max-age`, `s-maxage`) | Cache serves without contacting origin |
| Validation (`ETag`, `Last-Modified`) | Cache asks origin; 304 avoids body transfer, not the request |
| Cache key | URL + headers in `Vary`; extra `Vary` dimensions fragment the cache |
| Immutable assets | Content-hashed filenames can be cached for a year |

Freshness avoids the request; validation avoids the body. Both avoid work only if the origin
can decide cheaply — an ETag computed by rendering the full response saves bandwidth, not CPU.

## 3. Internal mechanics

### Static files

- `collectstatic` copies assets into `STATIC_ROOT`. With
  `STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"`
  (the `STORAGES` setting exists since Django 4.2), filenames get content hashes
  (`app.3f2a1c.css`) and `{% static %}` resolves through `staticfiles.json`.
- `django.contrib.staticfiles` serving via `runserver` is development-only; in production
  static must be served by a proxy, CDN or WhiteNoise [third-party: whitenoise].
- WhiteNoise serves from the Python process but sets far-future headers for hashed files and
  can serve precompressed `.gz`/`.br` files (`CompressedManifestStaticFilesStorage`); behind a
  CDN the origin hit rate is tiny, so the Python cost is negligible.

### Compression

- `django.middleware.gzip.GZipMiddleware` compresses in the worker: CPU per response, and it
  must run early in `MIDDLEWARE` (outermost relevant). Compressing responses that mix secrets
  (CSRF tokens) with attacker-reflected input enables BREACH; Django masks CSRF tokens, but
  other secrets in bodies remain exposed — see Django's GZip warning.
- Proxies/CDNs (nginx `gzip`/`brotli` module, CloudFront, Fastly) compress outside the GIL
  and can cache compressed variants (`Vary: Accept-Encoding`).

### Conditional requests

```text
Client: GET /api/catalog         → 200, ETag: "v42"
Client: GET /api/catalog         If-None-Match: "v42"
Server: computes current ETag "v42" cheaply → 304 Not Modified (no body)
```

- `ConditionalGetMiddleware` adds an ETag computed from the **already rendered** body (if the
  response has none and is not streaming) and turns a matching request into a 304 — it saves
  bandwidth only; the view, queries and template have already run.
- `@condition(etag_func=..., last_modified_func=...)`, `@etag`, `@last_modified` run the cheap
  function **before** the view; on match the view is never executed — saves CPU and DB.

### Connections and protocols

- Gunicorn sync workers handle one request per worker; `keepalive` (default 2 s) matters only
  when Gunicorn faces clients directly — which it should not. Run it behind nginx/ALB that
  keeps upstream and downstream connections pooled.
- HTTP/2 multiplexing and HTTP/3 (QUIC) are terminated at the proxy/CDN; Gunicorn speaks
  HTTP/1.1. Uvicorn/Daphne/Hypercorn HTTP/2 support varies by server (Hypercorn supports h2;
  VERIFY current Uvicorn status) but edge termination is still the norm.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Trade-off |
|---|---|---|---|
| Static serving | CDN in front of WhiteNoise/object storage | nginx from disk | CDN: global latency, cost per GB; nginx: simple, single region |
| Compression | Edge/proxy | `GZipMiddleware` | Edge: no worker CPU, cacheable variants; middleware: works anywhere, costs CPU, BREACH care |
| Freshness vs validation | Long `max-age` + hashed URLs | Short `max-age` + ETag | Freshness avoids requests but needs URL versioning; validation always round-trips |
| Shared vs private caching | `public, s-maxage` | `private, no-store` | Personal data must never be stored by shared caches |
| ETag source | Version/updated_at aggregate (cheap) | Body hash | Body hash requires rendering — no CPU saving |
| Media delivery | Object storage + signed URLs | Django `FileResponse` | Workers stay free vs simpler access control |

## 5. Django implementation patterns

### Static configuration

```python
STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage"},       # [third-party: django-storages]
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",                    # directly after SecurityMiddleware
    # ...
]
STATIC_URL = os.environ.get("STATIC_URL", "/static/")                # CDN URL in production
```

Hashed files get `Cache-Control: max-age=315360000, public, immutable` from WhiteNoise;
unhashed files get a short max-age (`WHITENOISE_MAX_AGE`, default 60 s; VERIFY).

### Cheap conditional views

```python
from django.db.models import Max
from django.views.decorators.http import condition

def catalog_etag(request, tenant_id):
    agg = Product.objects.filter(tenant_id=tenant_id).aggregate(m=Max("updated_at"))
    return f'"{tenant_id}-{agg["m"].timestamp() if agg["m"] else 0}"'

@condition(etag_func=catalog_etag)
def catalog(request, tenant_id): ...            # skipped entirely on If-None-Match match
```

The aggregate must be index-backed (`(tenant_id, updated_at)`); otherwise the "cheap" check is
itself a full scan. Deleted rows don't change `Max(updated_at)` — use a per-tenant version
counter if deletes matter.

### Cache-Control helpers

```python
from django.views.decorators.cache import cache_control, never_cache
from django.utils.cache import patch_cache_control, patch_vary_headers

@cache_control(public=True, max_age=60, s_maxage=300, stale_while_revalidate=30)
def public_landing(request): ...

@never_cache                                    # no-cache, no-store, must-revalidate, private
def account_statement(request): ...
```

`stale-while-revalidate` and `stale-if-error` (RFC 5861) let CDNs serve stale content during
revalidation or origin errors — powerful graceful degradation, supported unevenly across CDNs
(VERIFY for your provider).

### Media offload

```python
def download_invoice(request, pk):
    invoice = get_object_or_404(Invoice.objects.for_user(request.user), pk=pk)
    url = default_storage.url(invoice.pdf.name)          # S3 presigned URL, short expiry
    return redirect(url)                                 # worker freed in ms, not seconds
```

If files must stream through Django, use `FileResponse` (streams in blocks) and/or
`X-Accel-Redirect`/`X-Sendfile` so the proxy sends the bytes
([file-and-data-processing-performance.md](file-and-data-processing-performance.md)).

### Payload optimisation

- Paginate and trim fields ([api-performance.md](api-performance.md)).
- Responsive images and modern formats generated off-request.
- Avoid inlining large JSON blobs into HTML; avoid base64 images in API responses.

### Server-side rendering

- TTFB is the full view + template render; `StreamingHttpResponse` with a generator lets the
  browser start on `<head>` while the body renders, but breaks middleware that needs the body
  (GZip computes on the fly, ETag middleware skips streaming responses, `Content-Length` unknown).
- Cache expensive, shared fragments rather than whole personalised pages
  ([caching-engineering.md](caching-engineering.md)).

## 6. Production use cases

- **Asset-heavy app on mobile:** moving static to a CDN with immutable hashed files cut repeat
  page loads from ~45 requests to ~3 (illustrative); validate with browser DevTools "transferred"
  column and CDN hit ratio > 95%.
- **Catalog API polled by clients every 30 s:** `@condition` with a version counter turned
  80% of requests into 304s skipped before the view; DB QPS dropped proportionally.
- **Report downloads tying up workers:** 50 MB PDFs streamed through sync workers exhausted the
  pool under 20 concurrent downloads; presigned URLs freed workers entirely.

Failure scenarios:

- **Missing manifest entry:** `ManifestStaticFilesStorage` raises `ValueError` for a referenced
  file not collected → 500s after deploy. Run `collectstatic` in the build; test templates.
- **CDN caching personal pages:** `Cache-Control` absent on authenticated pages and CDN
  configured to cache HTML by default. Fix: explicit `private`/`no-store`, `Vary: Cookie`.
- **Vary explosion:** `Vary: User-Agent` destroyed CDN hit ratio.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Static/media off workers frees capacity for dynamic requests | CDN configuration is another deployable with its own failure modes |
| Hashed immutable assets: near-zero repeat cost | Requires a build step and correct manifest |
| Conditional views skip view work entirely | ETag logic must track every input to the response |
| Edge compression and HTTP/2/3 without app changes | Purging and invalidating CDNs is slow/eventually consistent |

## 8. Security and performance considerations

- Authenticated/personal responses: `Cache-Control: private` or `no-store`; never rely on a
  CDN default. `Vary: Cookie`/`Authorization` where shared caches could store responses.
- BREACH: avoid compressing responses that combine secrets with reflected input, or disable
  compression for those views.
- Signed URLs: short expiry, scoped to one object; they are bearer tokens if leaked.
- `SECURE_*` headers and HSTS add bytes but enable HTTP/2 over TLS — negligible cost.
- Large `Set-Cookie` or session payloads add bytes to every request.

## 9. Common mistakes and anti-patterns

- Serving static files through Django without WhiteNoise or a proxy (or via `runserver`).
- Long `max-age` on unhashed filenames — users stuck on old assets.
- ETags computed by rendering the body and expecting CPU savings.
- `GZipMiddleware` plus edge compression — double work or broken `Content-Encoding`.
- Streaming large files through sync workers.
- Assuming HTTP/2 push/multiplexing at Gunicorn level.
- Overly broad `Vary` headers.

## 10. Troubleshooting and validation

```bash
curl -sI https://example.com/static/app.3f2a1c.css          # check Cache-Control, ETag, Age, X-Cache
curl -s -o /dev/null -w "ttfb=%{time_starttransfer} total=%{time_total} size=%{size_download}\n" \
     -H "Accept-Encoding: br,gzip" https://example.com/api/catalog/
curl -sI -H 'If-None-Match: "42-1730700000.0"' https://example.com/api/catalog/   # expect 304
```

- Edge: CDN hit ratio, origin request rate, bytes served.
- Browser: Lighthouse/WebPageTest for TTFB, LCP and transfer size (lab), RUM for field data.
- Server: share of 304s per endpoint, worker time saved (APM spans absent on 304 paths).
- Regression: assert headers in tests (`response.headers["Cache-Control"]`) via
  [django-testing](../../django-testing/SKILL.md).

| Symptom | Check |
|---|---|
| Assets re-downloaded every visit | Hashed names? `immutable`/`max-age` present? |
| Users see stale JS after deploy | Unhashed URL with long max-age; CDN not purged |
| 304 rate zero | ETag differs per request (timestamps/random in body), proxy strips `If-None-Match` |
| High TTFB but fast view span | Network/TLS, proxy buffering, queueing before worker |

## 11. Related references and skills

- [caching-engineering.md](caching-engineering.md) — application caching patterns.
- [django-application-performance.md](django-application-performance.md) — template/middleware cost.
- [file-and-data-processing-performance.md](file-and-data-processing-performance.md) — streaming large files.
- [api-performance.md](api-performance.md) — payload size, compression for APIs.
- [django-core production configuration](../../django-core/references/production-configuration.md) and
  [file handling](../../django-core/references/file-handling.md).
- [django-frontend](../../django-frontend/SKILL.md) — asset bundling.
- [django-security-engineering](../../django-security-engineering/SKILL.md) — header and caching exposure.

## 12. Official documentation and external references

- Django static files deployment: https://docs.djangoproject.com/en/5.2/howto/static-files/deployment/
- `ManifestStaticFilesStorage`: https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/#manifeststaticfilesstorage
- Conditional view processing: https://docs.djangoproject.com/en/5.2/topics/conditional-view-processing/
- GZip middleware (BREACH note): https://docs.djangoproject.com/en/5.2/ref/middleware/#module-django.middleware.gzip
- Cache-control decorators: https://docs.djangoproject.com/en/5.2/topics/cache/#controlling-cache-using-other-headers
- RFC 9111 HTTP Caching: https://www.rfc-editor.org/rfc/rfc9111
- RFC 9110 HTTP Semantics (conditional requests): https://www.rfc-editor.org/rfc/rfc9110#name-conditional-requests
- RFC 5861 stale-while-revalidate: https://www.rfc-editor.org/rfc/rfc5861
- MDN HTTP caching: https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching
- WhiteNoise [third-party]: https://whitenoise.readthedocs.io/
- Gunicorn settings (`keepalive`): https://docs.gunicorn.org/en/stable/settings.html#keepalive
