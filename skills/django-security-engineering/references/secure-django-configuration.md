# Secure Django Configuration

> Scope: production security configuration — `DEBUG`, `ALLOWED_HOSTS` and Host-header attacks,
> forwarded headers and reverse-proxy trust, HTTPS redirect, HSTS, `SecurityMiddleware` and
> middleware order, environment-based settings with fail-fast loading, request-size limits,
> admin hardening, error-report disclosure, and `manage.py check --deploy`.
> Not covered here: cookie and session flags ([session-and-cookie-security.md](./session-and-cookie-security.md));
> CSRF settings ([csrf-protection.md](./csrf-protection.md)); CSP, framing, `nosniff`, Referrer and
> COOP headers ([cors-and-browser-security.md](./cors-and-browser-security.md)); secret storage and
> `SECRET_KEY` rotation ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); upload
> handling ([file-upload-security.md](./file-upload-security.md)); logging redaction
> ([security-logging-and-auditing.md](./security-logging-and-auditing.md)); settings mechanics
> ([django-core settings-configuration.md](../../django-core/references/settings-configuration.md)).

## 1. Purpose and scope

Most Django security features are present by default but several are *configuration-dependent*:
they only protect a deployment whose settings match its real topology (TLS termination point,
proxy chain, public hostnames). A secure codebase deployed with `DEBUG = True`, a wildcard
`ALLOWED_HOSTS`, or a trusted-but-spoofable `X-Forwarded-Proto` is insecure. This reference
defines the production baseline, why each setting exists, and how to verify it against the real
deployment rather than against `settings.py` alone.

Baseline: Django 5.2 LTS. Examples were not executed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Framework default | Protection on without action (e.g. `XFrameOptionsMiddleware` in the startproject template, Host validation when `DEBUG=False`) |
| Opt-in setting | Off by default because it depends on deployment (HSTS, SSL redirect, secure cookies) |
| Topology-dependent setting | Correct only if it matches the proxy chain (`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`) |
| Fail-fast configuration | Missing required secrets or hosts stop startup instead of falling back to insecure defaults |
| Deployment check | `manage.py check --deploy` — static checks of settings, not of the live response |

**Framework vs developer responsibility.** Django validates the `Host` header against
`ALLOWED_HOSTS`, emits HSTS/redirects when configured, and warns via `check --deploy`. The
developer must: choose the values, make the proxy overwrite (not append to) forwarded headers,
keep `DEBUG` off, provide secrets at runtime, and confirm headers on real responses.

## 3. Internal mechanics

- **`DEBUG = True`** renders technical 500 pages (traceback, local variables, settings filtered by
  `SafeExceptionReporterFilter` name heuristics only), technical 404 pages listing URL patterns,
  records every SQL statement in `connection.queries` (unbounded memory in long-running workers),
  and relaxes host validation. `runserver` static serving also depends on it.
- **Host validation**: `HttpRequest.get_host()` reads `HTTP_X_FORWARDED_HOST` (only if
  `USE_X_FORWARDED_HOST = True`), else `HTTP_HOST`, else `SERVER_NAME`+port, and raises
  `DisallowedHost` (→ 400, logged on `django.security.DisallowedHost`) when not in
  `ALLOWED_HOSTS`. With `DEBUG=True` and empty `ALLOWED_HOSTS`, Django allows
  `['.localhost', '127.0.0.1', '[::1]']`. Validation is lazy — it fires when code calls
  `get_host()` (CommonMiddleware, CSRF, `build_absolute_uri`, redirects), which is effectively every request with the default middleware.
- **`request.is_secure()`** returns `True` if the WSGI/ASGI scheme is `https`, or if the header
  named in `SECURE_PROXY_SSL_HEADER` equals the configured value. CSRF Referer/Origin checks,
  `SECURE_SSL_REDIRECT`, HSTS emission and `build_absolute_uri()` all depend on it.
- **`SecurityMiddleware`** (`django.middleware.security`) on request: if `SECURE_SSL_REDIRECT`
  and not `is_secure()` and path not matched by `SECURE_REDIRECT_EXEMPT` regexes → 301 to
  `https://` (`SECURE_SSL_HOST` if set). On response: adds `Strict-Transport-Security` **only on
  secure requests** when `SECURE_HSTS_SECONDS > 0`, plus `X-Content-Type-Options`,
  `Referrer-Policy` and `Cross-Origin-Opener-Policy` per their settings (owned by
  [cors-and-browser-security.md](./cors-and-browser-security.md)).
- **Request limits**: `DATA_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB, excludes file data),
  `DATA_UPLOAD_MAX_NUMBER_FIELDS` (default 1000) and `DATA_UPLOAD_MAX_NUMBER_FILES` (default
  100) raise `RequestDataTooBig` / `TooManyFieldsSent` / `TooManyFilesSent` → 400
  (`SuspiciousOperation` subclasses). `DATA_UPLOAD_MAX_NUMBER_FILES` arrived in the February 2023
  security releases (3.2.18 / 4.0.10 / 4.1.7, CVE-2023-24580) — `VERIFY` exact versions for
  older projects. Limits apply when `request.POST`/`FILES`/`body` are accessed.
- **System checks**: `check --deploy` runs checks tagged `deploy` (security, and some others);
  it inspects settings only — it cannot see the proxy, TLS, or what headers actually reach clients.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Guidance |
|---|---|---|---|
| HTTP→HTTPS redirect | At proxy/load balancer | `SECURE_SSL_REDIRECT = True` | Prefer the edge (cheaper, covers static/media); keep Django's redirect as a second layer unless health checks or the proxy loop |
| HSTS location | Edge config | `SECURE_HSTS_*` | Either, but one owner; Django only emits on requests it sees as secure |
| Host source | `HTTP_HOST` | `X-Forwarded-Host` | Use `USE_X_FORWARDED_HOST` only when the proxy rewrites `Host` and sets XFH itself |
| Scheme detection | WSGI/ASGI scheme from server (`--forwarded-allow-ips` in gunicorn/uvicorn) | `SECURE_PROXY_SSL_HEADER` | Either is fine if the trusted proxy overwrites the header; never both trusting different headers |
| Settings layout | Single module + env vars | `base.py`/`production.py` split | Split when environments diverge materially; production must not inherit dev defaults |
| Admin exposure | Public with MFA | Internal network / VPN only | Restrict by network where possible; MFA regardless |
| HSTS preload | Submit | Don't | Preload only when *every* subdomain is HTTPS permanently — removal takes months |

## 5. Django implementation patterns

### Fail-fast environment loading

```python
# config/settings/base.py
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Required environment variable {name} is not set")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


DEBUG = False  # never derived from an env var defaulting to True
```

Production never falls back to a dev secret or `DEBUG = True`: the default must be the *safe*
value. Secret sourcing (vaults, mounted files, rotation) is owned by
[secrets-and-cryptography.md](./secrets-and-cryptography.md).

### Production settings baseline

```python
# config/settings/production.py
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env, env_list

DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
SECRET_KEY_FALLBACKS = env_list("DJANGO_SECRET_KEY_FALLBACKS")  # Django ≥ 4.1

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # e.g. "app.example.com,api.example.com"
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production")

# TLS terminates at a proxy that OVERWRITES X-Forwarded-Proto on every request.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]  # internal plain-HTTP probe only

# HSTS: raise in stages (300 → 86400 → 31536000) once HTTPS is verified everywhere.
SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False  # True only after a deliberate preload decision

# Cookies — see session-and-cookie-security.md / csrf-protection.md
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")  # scheme required

# Headers — see cors-and-browser-security.md
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Request size limits — tune per endpoint needs, not globally upward
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
DATA_UPLOAD_MAX_NUMBER_FILES = 20

ADMINS = []  # prefer an error tracker with scrubbing over AdminEmailHandler
```

### Middleware order (security-relevant)

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",       # first: redirect before work
    # "whitenoise.middleware.WhiteNoiseMiddleware",        # third-party, directly after
    "django.contrib.sessions.middleware.SessionMiddleware",
    # "corsheaders.middleware.CorsMiddleware",             # third-party, before CommonMiddleware
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",  # Django ≥ 5.1, default-deny views
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
```

Ordering mechanics are owned by
[django-core middleware.md](../../django-core/references/middleware.md); default-deny login is
owned by [authentication-security.md](./authentication-security.md).

### Host-header-safe absolute URLs

Password-reset and invitation emails built from `request.get_host()` are only safe because
`ALLOWED_HOSTS` is strict. For outbound links, prefer a configured canonical origin:

```python
from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm

form = PasswordResetForm(data={"email": email})
if form.is_valid():
    form.save(
        domain_override=settings.SITE_DOMAIN,  # e.g. "app.example.com"
        use_https=True,
        request=None,
    )
```

### Admin hardening

```python
# urls.py — non-default path reduces noise; it is not access control.
urlpatterns = [path(env("DJANGO_ADMIN_PATH", "admin/"), admin.site.urls)]
```

- Require MFA for staff (third-party: `django-otp` `OTPAdminSite`, or django-allauth MFA) —
  [authentication-security.md](./authentication-security.md).
- Restrict `/admin/` to VPN/office ranges at the proxy (`allow`/`deny` rules), not in Django
  middleware reading spoofable `X-Forwarded-For`.
- Grant model permissions via groups; few superusers; audit admin actions
  (`LogEntry` is built-in but mutable; see [security-logging-and-auditing.md](./security-logging-and-auditing.md)).
- Do not register sensitive models (tokens, secrets) or expose them as editable fields.

## 6. Production use cases

- **Container behind a cloud load balancer**: LB terminates TLS and sets `X-Forwarded-Proto`;
  configure `SECURE_PROXY_SSL_HEADER`, ensure the app port is not reachable except from the LB
  (otherwise anyone can send the header directly).
- **Nginx → gunicorn**: `proxy_set_header X-Forwarded-Proto $scheme;` (overwrite) and
  `proxy_set_header Host $host;`; keep `USE_X_FORWARDED_HOST = False`.
- **Multi-domain SaaS with custom domains**: `ALLOWED_HOSTS` cannot be static; validate in a
  custom middleware against the tenant-domain table *before* `CommonMiddleware`, or set
  `ALLOWED_HOSTS = ["*"]` **only** together with such a middleware that rejects unknown hosts —
  document it as a deliberate exception ([multi-tenant-security.md](./multi-tenant-security.md)).
- **Staging**: same security settings as production, different secrets and hosts; `DEBUG=False`.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Django-side HTTPS/HSTS | Versioned with code, testable, `check --deploy` aware | Depends on correct `is_secure()`; misses static served by CDN |
| Edge-side HTTPS/HSTS | Covers everything, cheaper | Invisible to Django checks; config drift between environments |
| Strict `ALLOWED_HOSTS` | Blocks Host poisoning centrally | Must be updated per new domain; 400s on misconfigured probes |
| Fail-fast settings | Misconfiguration found at deploy | Requires all environments to provide variables explicitly |

## 8. Security and performance considerations

- **`SECURE_PROXY_SSL_HEADER` is a trust decision**: if any client can reach Django without
  passing the proxy, or the proxy appends rather than replaces the header, attackers make plain
  HTTP requests look secure — defeating redirects and Secure-cookie assumptions.
- **`USE_X_FORWARDED_HOST = True`** without a proxy that sets it lets clients choose the host
  used in reset emails, redirects and cache keys (web cache poisoning).
- **HSTS** with a long max-age is effectively irreversible for returning users; a broken
  certificate becomes an outage with no click-through. Stage rollout; `includeSubDomains`
  affects every subdomain, including internal tools on plain HTTP.
- **`DEBUG=True` in production** leaks source paths, settings, SQL, and installed apps, and
  `connection.queries` grows without bound — a memory leak under load.
- **Request limits** are DoS controls; raising `DATA_UPLOAD_MAX_MEMORY_SIZE` globally to accept
  one large JSON endpoint widens every endpoint. Raise per view by reading `request.body` only
  after an explicit size check, or stream.
- Error emails (`AdminEmailHandler`) include request metadata and POST data filtered only by
  `sensitive_post_parameters`; prefer a scrubbing error tracker.

## 9. Common mistakes and anti-patterns

- `DEBUG = os.environ.get("DEBUG", True)` — any string is truthy, and the default is insecure.
- `ALLOWED_HOSTS = ["*"]` "because the load balancer health check failed" — add the health-check
  host or exempt the probe at the proxy instead.
- Setting `SECURE_PROXY_SSL_HEADER` when there is no proxy, or when the proxy forwards the
  client's header unchanged.
- `SECURE_SSL_REDIRECT = True` behind a proxy without `SECURE_PROXY_SSL_HEADER` → infinite
  redirect loop; "fixing" it by disabling the redirect rather than configuring trust.
- Enabling `SECURE_HSTS_PRELOAD` / a one-year max-age on the first HTTPS deploy.
- Restricting admin by `request.META["REMOTE_ADDR"]` behind a proxy (always the proxy IP) or by
  raw `X-Forwarded-For` (client-controlled).
- Production settings importing `local.py` or `*` from a dev module that sets `DEBUG = True`.
- Suppressing `check --deploy` warnings without recording why.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| 400 Bad Request, `DisallowedHost` in logs | Host not in `ALLOWED_HOSTS` (often health checks, IP access) | Add the real hostname; exempt probes at the proxy |
| Redirect loop after enabling `SECURE_SSL_REDIRECT` | `is_secure()` false behind TLS-terminating proxy | Configure `SECURE_PROXY_SSL_HEADER` and proxy overwrite |
| No `Strict-Transport-Security` header | Request not seen as secure, or `SECURE_HSTS_SECONDS = 0` | Fix scheme detection; check on an HTTPS response |
| CSRF "Origin checking failed" after proxy change | Scheme/host mismatch | See [csrf-protection.md](./csrf-protection.md) |
| Reset emails contain attacker domain | Host from spoofable header | Strict `ALLOWED_HOSTS`, `USE_X_FORWARDED_HOST=False`, `domain_override` |

Validation steps:

```bash
python manage.py check --deploy --settings=config.settings.production --fail-level WARNING
curl -sI http://app.example.com/            # expect 301 to https
curl -sI https://app.example.com/ | grep -iE 'strict-transport|x-content-type|referrer|cross-origin|x-frame'
curl -sI -H 'Host: evil.example' https://app.example.com/    # expect 400
curl -sI -H 'X-Forwarded-Proto: https' http://<app-internal-ip>:8000/  # must be unreachable
```

Common `check --deploy` IDs (`VERIFY` against the installed version's check list):
`security.W001` SecurityMiddleware missing, `W002` XFrameOptionsMiddleware missing, `W003` CSRF
middleware missing, `W004` HSTS not set, `W005` HSTS without subdomains, `W006` nosniff off,
`W008` SSL redirect off, `W009` weak `SECRET_KEY`, `W012` session cookie not Secure, `W016` CSRF
cookie not Secure, `W018` `DEBUG=True`, `W019` `X_FRAME_OPTIONS` not `DENY`, `W020`
`ALLOWED_HOSTS` empty, `W021` HSTS preload off, `W022` no referrer policy, `W025` weak
`SECRET_KEY_FALLBACKS` entry.

Test settings that must hold (see [security-testing-and-incident-response.md](./security-testing-and-incident-response.md)):

```python
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings


class DeployCheckTests(SimpleTestCase):
    def test_production_settings_pass_deploy_checks(self):
        # Run in CI with DJANGO_SETTINGS_MODULE=config.settings.production
        call_command("check", "--deploy", "--fail-level", "WARNING")

    @override_settings(ALLOWED_HOSTS=["app.example.com"])
    def test_unknown_host_rejected(self):
        response = self.client.get("/", HTTP_HOST="evil.example")
        self.assertEqual(response.status_code, 400)
```

## 11. Related references and skills

- [session-and-cookie-security.md](./session-and-cookie-security.md) — cookie flags and session settings.
- [csrf-protection.md](./csrf-protection.md) — `CSRF_TRUSTED_ORIGINS`, proxy-related CSRF failures.
- [cors-and-browser-security.md](./cors-and-browser-security.md) — security headers set by `SecurityMiddleware`.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — `SECRET_KEY` sourcing and rotation.
- [authentication-security.md](./authentication-security.md) — `LoginRequiredMiddleware`, staff MFA.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — `django.security.*` loggers, error-report redaction.
- [django-core production-configuration.md](../../django-core/references/production-configuration.md) ·
  [django-core settings-configuration.md](../../django-core/references/settings-configuration.md) ·
  [django-core middleware.md](../../django-core/references/middleware.md)

## 12. Official documentation and external references

- Deployment checklist: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
- Security in Django: https://docs.djangoproject.com/en/5.2/topics/security/
- Security middleware: https://docs.djangoproject.com/en/5.2/ref/middleware/#module-django.middleware.security
- Settings reference (`ALLOWED_HOSTS`, `SECURE_*`, `DATA_UPLOAD_*`): https://docs.djangoproject.com/en/5.2/ref/settings/
- System check framework (security checks): https://docs.djangoproject.com/en/5.2/ref/checks/#security
- Django security releases archive: https://docs.djangoproject.com/en/dev/releases/security/
- RFC 6797 — HTTP Strict Transport Security: https://www.rfc-editor.org/rfc/rfc6797
- RFC 7239 — Forwarded HTTP Extension: https://www.rfc-editor.org/rfc/rfc7239
- HSTS preload list: https://hstspreload.org/
- OWASP HTTP Strict Transport Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Strict_Transport_Security_Cheat_Sheet.html
- OWASP Django Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Django_Security_Cheat_Sheet.html
