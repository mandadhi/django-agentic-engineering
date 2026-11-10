# Production configuration

> Scope: Django-specific production settings — `DEBUG`, `ALLOWED_HOSTS`, `SECRET_KEY`, HTTPS and proxies, secure cookies, CSRF settings, `SecurityMiddleware` settings, static/media serving, database connections and pooling, logging switches, health checks, deployment/system checks and a readiness checklist. Not covered here: settings mechanics and env vars ([settings-configuration.md](./settings-configuration.md)), middleware order ([middleware.md](./middleware.md)), server choice and workers ([asgi-wsgi.md](./asgi-wsgi.md)), `LOGGING` dict ([logging-exceptions.md](./logging-exceptions.md)), storage backends ([file-handling.md](./file-handling.md)), `CACHES` ([caching.md](./caching.md)), CSP/permissions/deeper hardening ([django-security-engineering](../../django-security-engineering/SKILL.md)), Postgres tuning ([postgresql-engineering](../../postgresql-engineering/SKILL.md)), Docker/cloud deployment (out of scope).

## Purpose and overview

Django's defaults favour local development. Production needs a known set of switches flipped and verified on every deploy. This reference lists those switches, why each exists, and how to prove they're set (`check --deploy` plus a health endpoint).

## How it works internally

- `DEBUG=True` makes Django render the technical 500 page (settings, local variables, SQL), record every SQL query in `connection.queries` (memory growth), and serve no custom 404/500 templates. With `DEBUG=False`, `ALLOWED_HOSTS` is enforced and `handler404/500` templates are used.
- `request.get_host()` validates the `Host` header (or `X-Forwarded-Host` if `USE_X_FORWARDED_HOST=True`) against `ALLOWED_HOSTS`; failure raises `DisallowedHost` → 400 and a `django.security.DisallowedHost` log record.
- `request.is_secure()` is true if the server says the scheme is https, or if `SECURE_PROXY_SSL_HEADER` matches. `SecurityMiddleware` uses it for `SECURE_SSL_REDIRECT` and HSTS; CSRF uses it for strict Referer/Origin checks; secure cookies depend on it indirectly (browsers drop `Secure` cookies over http).
- `SecurityMiddleware` adds headers on the response path: HSTS (only on https responses), `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`. `XFrameOptionsMiddleware` adds `X-Frame-Options`.
- DB connections: Django opens a connection per thread lazily, and at request end (`request_finished`) closes it if older than `CONN_MAX_AGE` (0 = close every request; `None` = never). `CONN_HEALTH_CHECKS` (≥ 4.1) pings a reused connection before the first query of a request. Django ≥ 5.1 supports psycopg 3 connection pools via `OPTIONS["pool"]`.
- `check --deploy` runs the `security`/deployment-tagged checks (`security.W004` HSTS missing, `W008` no SSL redirect, `W012` session cookie not secure, `W016` CSRF cookie not secure, `W018` DEBUG on, `W020` ALLOWED_HOSTS empty, `W009` weak SECRET_KEY, ...) against the **currently loaded** settings.

## Core concepts and APIs

| Setting | Production value | Why |
|---|---|---|
| `DEBUG` | `False` | Info leak, memory growth |
| `ALLOWED_HOSTS` | Explicit hostnames | Host-header poisoning (password-reset links, cache keys) |
| `SECRET_KEY` | ≥ 50 random chars from secret store | Signs sessions, CSRF, password-reset tokens, `signing` |
| `SECURE_PROXY_SSL_HEADER` | `("HTTP_X_FORWARDED_PROTO", "https")` **only** behind a proxy that always sets/overwrites it | Otherwise clients can spoof https |
| `SECURE_SSL_REDIRECT` | `True` (or redirect at the proxy) | Force https |
| `SECURE_HSTS_SECONDS` | Start `3600`, raise to `31536000` | Browser refuses http for the domain |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` / `_PRELOAD` | Only when every subdomain serves https | Hard to undo |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `True` | Cookies never sent over http |
| `SESSION_COOKIE_HTTPONLY` | `True` (default) | JS can't read session id |
| `SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_SAMESITE` | `"Lax"` (default) | CSRF mitigation |
| `CSRF_TRUSTED_ORIGINS` | `["https://app.example.com"]` | Needed for cross-subdomain/unsafe-origin POSTs; scheme required since Django 4.0 |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `True` (default) | MIME sniffing |
| `SECURE_REFERRER_POLICY` | `"same-origin"` (default) or stricter | Leaking URLs to third parties |
| `X_FRAME_OPTIONS` | `"DENY"` (default) | Clickjacking |
| `CONN_MAX_AGE` / `CONN_HEALTH_CHECKS` / `OPTIONS["pool"]` | See below | Connection overhead |
| `ADMINS`, `SERVER_EMAIL` | Set only if email error reports are wanted | Error reporting ([logging-exceptions.md](./logging-exceptions.md)) |

Django 6.0 adds built-in Content-Security-Policy support; on 5.2 CSP needs a third-party package — see [django-security-engineering](../../django-security-engineering/SKILL.md) and check `python -m django --version` before recommending either.

## Implementation examples

### Production settings block

```python
# config/settings.py (or settings/prod.py) — values come from env, safe defaults
DEBUG = env_bool("DJANGO_DEBUG")                       # default False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")       # "app.example.com,api.example.com"
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

if env_bool("DJANGO_BEHIND_TLS_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=not DEBUG)
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0 if DEBUG else 3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_SUBDOMAINS")
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
```

Bad:

```python
DEBUG = True
ALLOWED_HOSTS = ["*"]                                  # accepts any Host header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # with no proxy stripping client headers
SECURE_HSTS_SECONDS = 31536000; SECURE_HSTS_PRELOAD = True     # on day one, before every subdomain has TLS
```

### Database connections

```python
DATABASES["default"].update({
    "CONN_MAX_AGE": env_int("DJANGO_CONN_MAX_AGE", 60),   # persistent connections (WSGI)
    "CONN_HEALTH_CHECKS": True,
    "OPTIONS": {"connect_timeout": 5},
})
```

Pooling alternative (Django ≥ 5.1, PostgreSQL with psycopg 3 and `psycopg[pool]` installed):

```python
DATABASES["default"]["CONN_MAX_AGE"] = 0                  # pooling requires CONN_MAX_AGE = 0
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 2, "max_size": 10, "timeout": 10}}
```

| Situation | Choice |
|---|---|
| WSGI, threads/processes, DB nearby | `CONN_MAX_AGE=60` + health checks |
| ASGI (connections are per-event-loop-thread and don't persist well) | `CONN_MAX_AGE=0` + server-side pooler (PgBouncer) or psycopg pool — verify behaviour in [asgi-wsgi.md](./asgi-wsgi.md) |
| PgBouncer in transaction mode | `CONN_MAX_AGE=0` or short, `DISABLE_SERVER_SIDE_CURSORS=True` |

Size: `workers × threads × max_size` must stay under the database's `max_connections`. Details → [postgresql-engineering](../../postgresql-engineering/SKILL.md).

### Static and media

- Run `python manage.py collectstatic --noinput` at **build** time into `STATIC_ROOT`.
- Use `ManifestStaticFilesStorage` (hashed filenames → far-future caching) via `STORAGES["staticfiles"]` (Django ≥ 4.2).
- Serve static from a CDN/reverse proxy, or WhiteNoise if already used. `django.contrib.staticfiles` serving (`runserver`) and `static()` URL helpers only work with `DEBUG=True` — by design.
- Media (user uploads) is **never** served by Django in production and should live on object storage or a separate domain; see [file-handling.md](./file-handling.md).

```python
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},  # or a cloud backend
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
```

### Health checks

Two endpoints, kept out of auth, sessions and heavy middleware:

```python
# config/health.py
from django.db import connection
from django.http import JsonResponse

def liveness(request):
    return JsonResponse({"status": "ok"})              # process is up; no dependencies

def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "db_unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

# config/urls.py
urlpatterns += [path("healthz/", liveness), path("readyz/", readiness)]
```

Why split: liveness failing restarts the process; a DB outage should take the instance out of rotation (readiness), not restart-loop every pod. Note probes hit with the pod IP/internal hostname as `Host` — add it to `ALLOWED_HOSTS` or have the probe send a valid Host header; otherwise they get 400.

### Logging switches

- Logs to stdout, structured, `INFO` root; `django.security.*` at `WARNING` or above and alerted on (DisallowedHost spikes, SuspiciousOperation).
- `django.request` logs 5xx with tracebacks — ensure it reaches your error tracker. Setup → [logging-exceptions.md](./logging-exceptions.md).

## Production patterns

- **Same artifact, different env**: production values come from env vars; the image is identical across stages.
- **Deploy pipeline order**: build → `collectstatic` → `check --deploy` → migrations (backward-compatible first) → roll out → readiness gates traffic.
- **Run `check --deploy` with production env** in CI and as a pre-start step; `--fail-level WARNING` makes warnings block. Silence a check only with a justified `SILENCED_SYSTEM_CHECKS` entry (e.g. `security.W008` when the proxy does the redirect).
- **Never `runserver`** in production; use gunicorn/uvicorn/daphne ([asgi-wsgi.md](./asgi-wsgi.md)).
- **Custom error templates** `404.html`, `500.html` (500 rendered without context — keep it static).

## Decision guidance

| Question | Recommendation |
|---|---|
| Redirect to https in Django or proxy? | Proxy if it terminates TLS for everything; else `SECURE_SSL_REDIRECT`. Not both with conflicting rules. |
| HSTS preload? | Only after months of stable https on all subdomains |
| Persistent connections or pool? | WSGI: `CONN_MAX_AGE`; many workers/ASGI: pool (psycopg ≥ 5.1 or PgBouncer) |
| Static via WhiteNoise or CDN? | CDN/proxy at scale; WhiteNoise is fine for simple deployments |
| Email error reports (`ADMINS`)? | Prefer an error tracker; emails don't dedupe |

## Security considerations

- `SECRET_KEY` compromise lets attackers forge sessions and reset tokens — rotate via `SECRET_KEY_FALLBACKS` ([settings-configuration.md](./settings-configuration.md)).
- `SECURE_PROXY_SSL_HEADER` and `USE_X_FORWARDED_HOST` trust client-controllable headers — enable only when the proxy strips/overwrites them.
- Admin: non-obvious URL, restricted by network or SSO, 2FA — see [django-security-engineering](../../django-security-engineering/SKILL.md).
- Health endpoints expose nothing sensitive (no versions, hostnames, error text).

## Performance considerations

- `DEBUG=False` alone removes query recording and enables the cached template loader.
- Connection reuse saves a TCP+TLS+auth handshake per request (often several ms).
- Hashed static files + long cache headers remove repeat downloads.
- Readiness checks must be cheap (`SELECT 1`); don't run migrations checks or external API calls on every probe.
- Profiling and capacity → [django-performance](../../django-performance/SKILL.md).

## Testing

- CI job: `DJANGO_SETTINGS_MODULE=<prod settings> python manage.py check --deploy --fail-level WARNING` with dummy-but-valid env values.
- Unit-test health views with the test client (simulate DB failure by patching the cursor).
- Smoke test after deploy: `curl -I https://host/` shows `Strict-Transport-Security`, `X-Content-Type-Options`, secure cookies; an http request redirects.
- Security header tests in depth → [django-security-engineering](../../django-security-engineering/SKILL.md).

## Common mistakes and anti-patterns

- `DEBUG` defaulting to `True` or toggled via a truthy string bug.
- `ALLOWED_HOSTS = ["*"]` to "fix" health-check 400s.
- Redirect loops: `SECURE_SSL_REDIRECT=True` behind a TLS-terminating proxy without `SECURE_PROXY_SSL_HEADER`.
- Serving media from the app server or putting uploads in `STATIC_ROOT`.
- `CONN_MAX_AGE=None` with a DB that kills idle connections and no health checks → intermittent "connection already closed".
- `CONN_MAX_AGE > 0` together with `OPTIONS["pool"]` → `ImproperlyConfigured`.
- Running migrations from every replica at startup simultaneously.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 400 Bad Request, `DisallowedHost` in logs | Host not in `ALLOWED_HOSTS` (often probes or new domain) | Add the host; fix probe Host header |
| Infinite redirect to https | Proxy terminates TLS; Django sees http | Set `SECURE_PROXY_SSL_HEADER` (proxy must set the header) |
| CSRF 403 "Origin checking failed" | Origin not trusted, or https not detected | `CSRF_TRUSTED_ORIGINS` with scheme; fix proxy header |
| Login works then session lost | `SESSION_COOKIE_SECURE=True` on http, or cookie domain mismatch | Serve over https; check `SESSION_COOKIE_DOMAIN` |
| Static 404 with `DEBUG=False` | Expected — Django doesn't serve static | `collectstatic` + proxy/CDN/WhiteNoise |
| `ValueError: Missing staticfiles manifest entry` | Referenced file not collected | Run `collectstatic`; fix path |
| `too many connections` | workers × pool exceeds DB limit | Lower pool/`CONN_MAX_AGE`, add PgBouncer |
| `connection already closed` intermittently | Stale persistent connection | `CONN_HEALTH_CHECKS=True`; lower `CONN_MAX_AGE` |

## Official documentation

- https://docs.djangoproject.com/en/stable/howto/deployment/checklist/
- https://docs.djangoproject.com/en/stable/ref/settings/#std-setting-SECURE_PROXY_SSL_HEADER
- https://docs.djangoproject.com/en/stable/ref/middleware/#module-django.middleware.security
- https://docs.djangoproject.com/en/stable/ref/csrf/#settings
- https://docs.djangoproject.com/en/stable/ref/databases/#persistent-connections
- https://docs.djangoproject.com/en/stable/ref/databases/#connection-pool
- https://docs.djangoproject.com/en/stable/howto/static-files/deployment/
- https://docs.djangoproject.com/en/stable/ref/checks/#security
- https://docs.djangoproject.com/en/stable/topics/security/
