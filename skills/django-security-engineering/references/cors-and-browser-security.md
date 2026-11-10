# CORS and Browser Security

> Scope: the same-origin policy and what it does and does not protect; CORS mechanics and
> django-cors-headers (third-party); Content Security Policy via django-csp (third-party, Django
> 5.2) or the built-in CSP support (Django ≥ 6.0); clickjacking protection (`X-Frame-Options`,
> `frame-ancestors`); `nosniff`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`,
> `Permissions-Policy`, COEP/CORP; secure frontend–backend topologies.
> Not covered here: HTTPS/HSTS/proxy settings ([secure-django-configuration.md](./secure-django-configuration.md));
> cookie flags and `SameSite` ([session-and-cookie-security.md](./session-and-cookie-security.md));
> CSRF ([csrf-protection.md](./csrf-protection.md)); escaping and sanitisation
> ([cross-site-scripting.md](./cross-site-scripting.md)); middleware internals
> ([django-core middleware](../../django-core/references/middleware.md)); frontend build and asset
> pipelines ([django-frontend](../../django-frontend/SKILL.md)).

## 1. Purpose and scope

Browser security controls are instructions the server sends to the browser: "only let these
origins read my responses", "only run scripts from here", "never render me in a frame". They are
**browser-side controls** — enforced by a cooperating user agent, ignored by `curl`, scripts,
mobile apps and attackers' own tools. They limit what a *malicious page in a victim's browser* can
do; they never replace server-side authentication, authorisation, CSRF or input validation.

This reference explains which headers Django emits by default, which require a third-party
package or custom middleware, how to configure them without breaking the frontend, and how to
validate them on real production responses.

## 2. Core concepts

**Origin** = scheme + host + port. `https://app.example.com` and `https://api.example.com` are
different origins; they are the same *site* (registrable domain `example.com`) — relevant for
`SameSite` cookies, not for CORS.

**Same-origin policy (SOP)**: a page may *send* many cross-origin requests (forms, `<img>`,
`<script>`, `fetch` with simple methods) but may not *read* cross-origin responses unless the
target opts in. SOP therefore stops data theft by reading, but **not** state-changing requests —
that gap is what CSRF protection covers.

**CORS** (Fetch standard) is a controlled *relaxation* of SOP. The server says which origins may
read responses, with which methods/headers, and whether credentials (cookies, HTTP auth) are
included. CORS never makes a server more secure; a misconfigured CORS policy only makes it less.

**CSP** restricts the sources from which a document may load scripts, styles, frames, images and
connections, and where it may be framed (`frame-ancestors`) or submit forms (`form-action`). Its
main security value is limiting the impact of XSS.

**Framing controls**: `X-Frame-Options` (legacy, `DENY`/`SAMEORIGIN`) and CSP `frame-ancestors`
(modern, supports origin lists; takes precedence in browsers that support it).

**Other headers**:

| Header | Purpose | Django 5.2 |
|---|---|---|
| `X-Content-Type-Options: nosniff` | Stop MIME sniffing scripts/styles | Built-in, `SECURE_CONTENT_TYPE_NOSNIFF=True` default (Django ≥ 3.0) |
| `Referrer-Policy` | Limit URL leakage in `Referer` | Built-in, `SECURE_REFERRER_POLICY="same-origin"` default (Django ≥ 3.1) |
| `Cross-Origin-Opener-Policy` | Isolate browsing context from cross-origin popups/openers | Built-in, `SECURE_CROSS_ORIGIN_OPENER_POLICY="same-origin"` default (Django ≥ 4.0) |
| `X-Frame-Options` | Clickjacking | Built-in, `XFrameOptionsMiddleware`, `DENY` default (Django ≥ 3.0) |
| `Content-Security-Policy` | XSS impact reduction, framing, form targets | django-csp (third-party) on 5.2; built-in on Django ≥ 6.0 |
| `Permissions-Policy` | Disable powerful features (camera, geolocation…) | Not built-in — custom middleware or proxy |
| `Cross-Origin-Embedder-Policy` / `Cross-Origin-Resource-Policy` | Cross-origin isolation, resource embedding | Not built-in |
| `X-XSS-Protection` | Legacy XSS auditor | Do not use; `SECURE_BROWSER_XSS_FILTER` was removed (Django 4.0 — `VERIFY`) |

## 3. Internal mechanics

**CORS request flow**

1. *Simple request* (GET/HEAD/POST, only CORS-safelisted headers, content type
   `text/plain`, `multipart/form-data` or `application/x-www-form-urlencoded`): the browser sends it
   immediately with `Origin`, **the server executes it**, and the browser decides afterwards whether
   JS may read the response. Side effects happen regardless of CORS.
2. *Preflighted request* (other methods, `Content-Type: application/json`, custom headers such as
   `Authorization` or `X-CSRFToken`): browser first sends `OPTIONS` with
   `Access-Control-Request-Method/-Headers`; only if the response allows it is the real request
   sent. Preflight caching via `Access-Control-Max-Age`.
3. *Credentials*: with `fetch(..., {credentials: "include"})` the response must carry
   `Access-Control-Allow-Credentials: true` **and** an explicit origin — browsers reject
   `Access-Control-Allow-Origin: *` with credentials.
4. *Caching*: if the allowed origin varies per request, the response must include `Vary: Origin`
   or a shared cache/CDN may serve origin A's CORS headers to origin B.

**django-cors-headers** (third-party, `corsheaders`): `CorsMiddleware` inspects `Origin`, answers
preflights itself (short-circuiting the view), and adds `Access-Control-*` headers when the origin
matches `CORS_ALLOWED_ORIGINS`/`CORS_ALLOWED_ORIGIN_REGEXES` and the path matches
`CORS_URLS_REGEX`. It must run before any middleware that can generate a response
(`CommonMiddleware`, `WhiteNoise`), otherwise those responses lack CORS headers. It patches
`Vary: Origin` (`VERIFY` for the installed version).

**Django `SecurityMiddleware`** sets `nosniff`, `Referrer-Policy` and COOP (plus HTTPS redirect and
HSTS, owned by [secure-django-configuration.md](./secure-django-configuration.md)) in
`process_response`, only if the header is not already present.

**`XFrameOptionsMiddleware`** sets `X-Frame-Options` from `X_FRAME_OPTIONS` unless the response
already has it or the view set `response.xframe_options_exempt = True` (via
`@xframe_options_exempt`). `@xframe_options_sameorigin` / `@xframe_options_deny` set per view.

**CSP enforcement**: the browser parses the policy, blocks violating loads/executions and, if a
`report-to`/`report-uri` directive is present, POSTs a violation report. With a **nonce**, each
response carries a fresh random value in `script-src 'nonce-…'` and matching `nonce="…"` attributes;
an injected `<script>` lacks it and is blocked. `'strict-dynamic'` lets nonce-approved scripts load
further scripts, so allow-lists of hosts become unnecessary in CSP Level 3 browsers.

**django-csp ≥ 4.0** (third-party) changed its configuration from many `CSP_*` settings to a
`CONTENT_SECURITY_POLICY` / `CONTENT_SECURITY_POLICY_REPORT_ONLY` dict with a `DIRECTIVES` key and
constants from `csp.constants` (`VERIFY` against the installed version; 3.x settings are
incompatible). The nonce is generated lazily: it is only added to the header if something read
`request.csp_nonce` during rendering.

**Django ≥ 6.0 built-in CSP**: `django.middleware.csp.ContentSecurityPolicyMiddleware`, settings
`SECURE_CSP` and `SECURE_CSP_REPORT_ONLY` (dicts of directives), constants in
`django.utils.csp.CSP` (`CSP.SELF`, `CSP.NONCE`, …), a `csp` context processor exposing
`csp_nonce` to templates, and per-view override decorators in `django.views.decorators.csp`
(`VERIFY` exact names when upgrading). Not available on 5.2.

## 4. Engineering decisions and trade-offs

**Topology first, CORS second.** The safest CORS policy is none:

| Topology | CORS | CSRF/cookies | Notes |
|---|---|---|---|
| SPA served from the same origin as the API (reverse proxy routes `/api/` to Django) | Not needed | Session + CSRF works unchanged | Preferred for first-party browser apps |
| SPA on `app.example.com`, API on `api.example.com` | Allow-list one origin, credentials if cookies | Same-site, so `SameSite=Lax` cookies flow; add origin to `CSRF_TRUSTED_ORIGINS` | Common; keep the list exact |
| SPA on an unrelated domain | Allow-list + credentials, or token auth without cookies | `SameSite=None; Secure` needed for cookies — weaker | Prefer bearer tokens; consider BFF |
| Public read-only API, no credentials | `*` acceptable for those paths only | No cookies | Scope with `CORS_URLS_REGEX` |
| Server-to-server / mobile clients | Irrelevant (no browser) | Token auth | Don't add CORS "to make it work" |

**CSP strategy**

- *Nonce + `'strict-dynamic'`* — strongest, works with dynamic pages; requires every inline script
  to carry the nonce and disables full-page caching of HTML (nonce must be per response).
- *Hashes* — for static inline snippets; suits cached pages; brittle when the snippet changes.
- *Host allow-lists* (`script-src 'self' cdn.example.com`) — easy but often bypassable via JSONP or
  hosted libraries on allowed CDNs; acceptable as a first step.
- Never `'unsafe-inline'` for scripts (it neutralises CSP's XSS value; it is ignored when a nonce
  or hash is present in CSP2+ browsers). `'unsafe-eval'` only if a dependency truly requires it.
- Roll out with **report-only** first, collect violations, then enforce; keep report-only running
  for a stricter candidate policy.

**Framing**: `DENY` unless a feature needs embedding; then CSP `frame-ancestors` with exact origins
(XFO cannot express multiple origins). Set both for older browsers.

**Where to set headers**: Django for anything per-view or per-request (nonces, exemptions); the
proxy/CDN for static assets and media that never reach Django. Avoid setting the same header in
both places with different values — browsers combine multiple CSP headers (intersection), and
duplicated XFO values may be ignored.

## 5. Django implementation patterns

**Built-in headers (Django 5.2)** — defaults are already good; state them explicitly so a reviewer
sees intent:

```python
# settings/production.py
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",          # third-party; before CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",                    # third-party django-csp (Django 5.2)
    "config.middleware.PermissionsPolicyMiddleware",   # custom, below
]

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"  # or the stricter default "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
```

**CORS allow-list (django-cors-headers, third-party)**

```python
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")   # e.g. ["https://app.example.com"]
CORS_ALLOW_CREDENTIALS = True                              # only if the SPA uses session cookies
CORS_URLS_REGEX = r"^/api/.*$"                             # never expose HTML/admin paths
CORS_PREFLIGHT_MAX_AGE = 3600
# Do NOT set CORS_ALLOW_ALL_ORIGINS = True together with credentials.

# If regexes are unavoidable, anchor them and escape dots:
CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://[a-z0-9-]+\.preview\.example\.com$"]
# Wrong: r"https://.*example.com" matches https://evilexample.com and https://example.com.evil.io
```

Cross-origin session-authenticated SPAs also need the origin in `CSRF_TRUSTED_ORIGINS` and the CSRF
token header allowed (`X-CSRFToken` is in django-cors-headers' default `CORS_ALLOW_HEADERS` —
`VERIFY`); see [csrf-protection.md](./csrf-protection.md).

**CSP with django-csp 4.x (third-party, Django 5.2)** — `VERIFY` syntax against the installed version:

```python
from csp.constants import NONCE, NONE, SELF, STRICT_DYNAMIC

CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF, NONCE, STRICT_DYNAMIC],
        "style-src": [SELF, NONCE],
        "img-src": [SELF, "data:", "https://media.example-cdn.com"],
        "connect-src": [SELF, "https://api.example.com"],
        "object-src": [NONE],
        "base-uri": [NONE],
        "form-action": [SELF],
        "frame-ancestors": [NONE],
        "report-uri": "/csp-report/",
    },
}
# Rollout: start with CONTENT_SECURITY_POLICY_REPORT_ONLY = {...} and no enforcing policy.
```

```django
{# template: the nonce is per response; reading it is what adds it to the header #}
<script nonce="{{ request.csp_nonce }}">
  window.APP = JSON.parse(document.getElementById("app-config").textContent);
</script>
{{ app_config|json_script:"app-config" }}
```

**CSP on Django ≥ 6.0 (built-in)** — `VERIFY` names on upgrade:

```python
from django.utils.csp import CSP

MIDDLEWARE += ["django.middleware.csp.ContentSecurityPolicyMiddleware"]
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
}
# TEMPLATES[...]["OPTIONS"]["context_processors"] += ["django.template.context_processors.csp"]
# template: <script nonce="{{ csp_nonce }}">
```

**CSP violation report endpoint** — treat reports as untrusted, bounded, rate-limited input:

```python
import json
import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt  # browsers send reports without CSRF tokens
from django.views.decorators.http import require_POST

logger = logging.getLogger("security.csp")
MAX_REPORT_BYTES = 16 * 1024

@csrf_exempt  # justified: unauthenticated, no state change except a log line
@require_POST
def csp_report(request):
    if len(request.body) > MAX_REPORT_BYTES:
        return HttpResponse(status=413)
    try:
        report = json.loads(request.body).get("csp-report", {})
    except (ValueError, AttributeError):
        return HttpResponse(status=400)
    logger.warning(
        "csp_violation",
        extra={
            "blocked_uri": str(report.get("blocked-uri", ""))[:500],
            "directive": str(report.get("violated-directive", ""))[:200],
            "document_uri": str(report.get("document-uri", ""))[:500],
        },
    )
    return HttpResponse(status=204)
```

**Per-view framing exception** (e.g. an embeddable widget):

```python
from django.views.decorators.clickjacking import xframe_options_exempt

@xframe_options_exempt          # removes XFO; CSP frame-ancestors must now carry the allow-list
def embed_widget(request, token):
    response = render(request, "widgets/embed.html", {...})
    response["Content-Security-Policy"] = "frame-ancestors https://partner.example.org"
    return response
```

With django-csp, prefer its per-view decorators (`csp_update`/`csp_replace`) over hand-written
headers so the global policy is not lost (`VERIFY` decorator signatures in 4.x).

**Headers Django does not ship** — small middleware:

```python
# config/middleware.py
class PermissionsPolicyMiddleware:
    POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Permissions-Policy", self.POLICY)
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response
```

Add `Cross-Origin-Embedder-Policy: require-corp` only when cross-origin isolation is required
(e.g. `SharedArrayBuffer`); it breaks third-party embeds lacking CORP/CORS.

## 6. Production use cases

- **First-party SPA**: serve the SPA and `/api/` from one origin through nginx/CDN; no CORS, session
  auth with CSRF; CSP nonce or hashes on the SPA shell.
- **Partner dashboard embeds**: one view exempt from XFO with `frame-ancestors` listing partner
  origins from a DB/allow-list; everything else `DENY`.
- **Public open-data API**: `CORS_URLS_REGEX=r"^/api/public/"` with all origins and no credentials;
  private API paths stay origin-restricted.
- **Admin hardening**: `frame-ancestors 'none'`, strict `script-src`; Django admin templates work
  under a nonce-free `'self'` policy because they use external scripts (`VERIFY` for inline
  snippets in the installed version).
- **Third-party analytics/tag managers**: isolate in a dedicated policy review — tag managers are
  arbitrary-script loaders and defeat CSP if allowed broadly.

## 7. Advantages and disadvantages

| Control | Advantages | Disadvantages |
|---|---|---|
| CORS allow-list | Precise relaxation of SOP | Misconfiguration silently opens reads; no server protection |
| CSP nonce + strict-dynamic | Strong XSS mitigation, few hosts to maintain | Per-response nonce prevents HTML caching; every inline script needs updating |
| CSP host allow-list | Simple rollout | Bypassable via allowed CDNs/JSONP |
| `X-Frame-Options` | Universal support, built-in | Single origin only; superseded by `frame-ancestors` |
| Built-in headers (`nosniff`, Referrer, COOP) | Zero cost, on by default | COOP can break OAuth/payment popups that rely on `window.opener` |
| Custom header middleware | Fills gaps (Permissions-Policy, CORP) | Must be maintained and tested |

## 8. Security and performance considerations

- Browser controls are **defence in depth**. An endpoint must be safe for a request from `curl`
  with any `Origin`; CORS denial does not stop the request from executing.
- Reflecting the request `Origin` into `Access-Control-Allow-Origin` with credentials is equivalent
  to disabling SOP for authenticated data. `null` origin (sandboxed iframes, `file:`) must never be
  allowed.
- Subdomain wildcards trust every subdomain — including takeover-prone ones (dangling DNS to
  deprovisioned SaaS). Allow exact origins.
- CSP reports leak URLs (possibly with tokens) — strip query strings before storing, bound sizes,
  rate-limit the endpoint.
- Preflights add a round trip; `CORS_PREFLIGHT_MAX_AGE` reduces them (browsers cap the value).
- Nonce generation is cheap (`secrets`), but nonce-bearing pages must not be cached by a shared
  cache; set `Cache-Control: private`/`no-store` on them or use hashes for cacheable pages.
- `Referrer-Policy` prevents leaking password-reset tokens or IDs in URLs to third parties — keep
  sensitive tokens out of URLs anyway.

## 9. Common mistakes and anti-patterns

- `CORS_ALLOW_ALL_ORIGINS = True` plus `CORS_ALLOW_CREDENTIALS = True` (or a custom middleware
  echoing `Origin`).
- Unanchored or unescaped `CORS_ALLOWED_ORIGIN_REGEXES`.
- Adding CORS to "fix" a mobile app or server-to-server call — they don't use CORS.
- `CorsMiddleware` placed after `CommonMiddleware`/WhiteNoise, so redirects and errors lack headers
  and the frontend sees opaque failures.
- Believing CORS prevents CSRF — simple cross-origin POSTs still execute.
- CSP with `'unsafe-inline' 'unsafe-eval'` in `script-src` — security theatre.
- Static nonce (same value per deployment) or nonce in cached HTML.
- `@xframe_options_exempt` on a whole site or on login/payment pages.
- Setting `X-XSS-Protection: 1; mode=block` — legacy auditor introduced its own leaks; use CSP.
- Defining headers at both proxy and Django with conflicting values.
- Using a django-csp 3.x settings block (`CSP_DEFAULT_SRC = ...`) with django-csp 4.x installed
  (`VERIFY`: 4.x reports a configuration error/check).

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Browser "No 'Access-Control-Allow-Origin' header" | Origin not in allow-list; scheme/port mismatch; path outside `CORS_URLS_REGEX`; middleware order | `curl -i -H "Origin: https://app.example.com" https://api.example.com/api/x/` |
| Preflight 403/405 | View/proxy rejects `OPTIONS`; auth required on preflight | Preflight must be answered by `CorsMiddleware` before auth |
| Cookies not sent cross-origin | Missing `credentials: "include"`, `SameSite=Lax` on cross-site, no `Allow-Credentials` | See [session-and-cookie-security.md](./session-and-cookie-security.md) |
| CSP blocks own scripts | Inline script without nonce; nonce never read so not in header | View page source vs header nonce |
| Embed shows blank frame | XFO `DENY` or `frame-ancestors` | Response headers of the framed page |
| OAuth popup can't talk back | COOP `same-origin` | Use `same-origin-allow-popups` for that view |

Validation:

```python
from django.test import TestCase, override_settings

class BrowserHeaderTests(TestCase):
    def test_security_headers_present(self):
        r = self.client.get("/")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Referrer-Policy", r.headers)
        self.assertIn("frame-ancestors", r.headers.get("Content-Security-Policy", ""))

    def test_untrusted_origin_gets_no_cors_grant(self):
        r = self.client.get("/api/orders/", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_nonce_differs_per_response(self):
        h1 = self.client.get("/").headers["Content-Security-Policy"]
        h2 = self.client.get("/").headers["Content-Security-Policy"]
        self.assertNotEqual(h1, h2)
```

Also verify the **deployed** response (CDN/proxy may add or strip headers), check CSP with
Google's CSP Evaluator, and watch report-only violations for a release cycle before enforcing.

## 11. Related references and skills

- [csrf-protection.md](./csrf-protection.md) — cross-origin unsafe requests, `CSRF_TRUSTED_ORIGINS`.
- [cross-site-scripting.md](./cross-site-scripting.md) — the vulnerabilities CSP mitigates.
- [session-and-cookie-security.md](./session-and-cookie-security.md) — `SameSite`, cross-site cookies.
- [secure-django-configuration.md](./secure-django-configuration.md) — `SecurityMiddleware`, HTTPS, HSTS.
- [api-security.md](./api-security.md) — token auth for cross-origin clients.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — header regression tests, DAST.
- [django-core middleware](../../django-core/references/middleware.md) · [django-frontend](../../django-frontend/SKILL.md) · [django-rest-framework](../../django-rest-framework/SKILL.md)

## 12. Official documentation and external references

- Django clickjacking protection: https://docs.djangoproject.com/en/5.2/ref/clickjacking/
- Django `SecurityMiddleware`: https://docs.djangoproject.com/en/5.2/ref/middleware/#module-django.middleware.security
- Django security settings: https://docs.djangoproject.com/en/5.2/ref/settings/#secure-content-type-nosniff
- Django 6.0 CSP (built-in): https://docs.djangoproject.com/en/6.0/ref/csp/ (`VERIFY` path)
- django-csp (third-party): https://django-csp.readthedocs.io/
- django-cors-headers (third-party): https://github.com/adamchainz/django-cors-headers
- Fetch standard (CORS protocol): https://fetch.spec.whatwg.org/#http-cors-protocol
- W3C CSP Level 3: https://www.w3.org/TR/CSP3/
- MDN CORS: https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS
- MDN CSP: https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP
- OWASP Content Security Policy Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html
- OWASP Clickjacking Defense Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html
- OWASP HTTP Headers Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html
- Google CSP Evaluator: https://csp-evaluator.withgoogle.com/
