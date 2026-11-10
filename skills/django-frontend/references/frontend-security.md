# Frontend Security

> **Scope:** Owns browser-side security for Django frontends: XSS (including DOM XSS sinks), safe HTML in templates and JS, CSP from the page author's view (nonces, HTMX/Alpine implications, rollout), cookies and tokens from the browser's view, CSRF in fetch/HTMX/SPA clients, CORS vs CSRF vs auth, clickjacking, URL handling and open redirects in UI, upload UI, client-side secrets and storage, dependencies, third-party scripts/SRI, and data exposure. Backend enforcement (middleware settings, session engine, authorization, upload validation) belongs to [django-security-engineering](../../django-security-engineering/SKILL.md) — this file summarizes and links.

## 1. Purpose and scope

The browser is an untrusted, user-controlled runtime. Frontend security has two jobs:

1. **Don't create vulnerabilities in the browser** — DOM XSS, token leakage, open redirects, leaking data in HTML/JS, unsafe third-party code.
2. **Integrate correctly with server controls** — send CSRF tokens, respect cookie flags, ship a CSP the page actually complies with.

State plainly and design accordingly:

- **Hidden UI is not a security boundary.** A removed button does not stop a `curl` request.
- **Frontend validation is not a security boundary.** It is UX; the Django form/serializer re-validates everything.
- **Anything sent to the browser is public to that user** — HTML source, `json_script` data, JS bundles, source maps, `VITE_*` variables, API responses.

## 2. Core concepts

### Threat map

| Threat | Browser-side cause | Primary defense | Backend reference |
|---|---|---|---|
| Stored/reflected XSS | `|safe`, `mark_safe`, unescaped attributes | Autoescape, `format_html`, CSP | [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md) |
| DOM XSS | Untrusted data into DOM/JS sinks | `textContent`, DOM APIs, Trusted Types | same |
| CSRF | Cookie-authenticated unsafe request from another site | CSRF token + SameSite | [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md) |
| Session theft | Script-readable tokens | HttpOnly cookies, CSP | [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md) |
| Clickjacking | Page framed by attacker | `X-Frame-Options`/`frame-ancestors` | [cors-and-browser-security.md](../../django-security-engineering/references/cors-and-browser-security.md) |
| Open redirect | Unvalidated `next=` | `url_has_allowed_host_and_scheme` | [authentication-security.md](../../django-security-engineering/references/authentication-security.md) |
| Supply chain | Compromised npm/CDN script | Lockfiles, audit, SRI, self-hosting | [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md) |
| Data exposure | Over-rendering, source maps, caching | Minimal context, `Cache-Control` | [secure-django-configuration.md](../../django-security-engineering/references/secure-django-configuration.md) |

### CORS ≠ CSRF ≠ authentication

| Mechanism | Answers | Enforced by |
|---|---|---|
| Authentication | Who is this? | Server (session/token) |
| Authorization | May they do this? | Server, per request |
| CSRF protection | Did *our* page intend this cookie-authenticated request? | Server checks token/origin |
| CORS | May script on origin X **read** responses from origin Y? | Browser, based on server headers |

CORS never blocks a simple cross-site form POST from being *sent*; it only controls reading the response. Adding `django-cors-headers` (third-party) does not provide CSRF protection, and a permissive CORS policy with `credentials` can create one.

### Escaping contexts

Django autoescaping is HTML-body/quoted-attribute escaping. It is **not** safe for: unquoted attributes, `<script>` bodies, `on*=` handlers, `style=`, `href`/`src` URL schemes (`javascript:`), or JSON in JS. Use `json_script` for data into JS and validate URL schemes server-side.

## 3. Internal mechanics

### DOM XSS sinks and safe alternatives

| Sink | Why dangerous | Safe alternative |
|---|---|---|
| `el.innerHTML = s`, `el.outerHTML = s` | Parses markup; `<img onerror>` executes | `el.textContent = s`; build nodes with `createElement`/`append`; `<template>` cloned with text filled via `textContent` |
| `el.insertAdjacentHTML(pos, s)` | Same as innerHTML | `el.insertAdjacentText` / `insertAdjacentElement` |
| `document.write(s)` | Parses markup, blocks parser | Don't use |
| `eval(s)`, `new Function(s)` | Executes code | `JSON.parse`, lookup tables |
| `setTimeout("code")`, `setInterval("code")` | String is eval'd | Pass a function |
| `a.href = s`, `iframe.src = s`, `form.action = s` | `javascript:` URLs execute | Parse with `new URL(s, location.origin)` and allow only `http:`/`https:` (and same-origin where intended) |
| `location = s`, `location.href = s`, `location.assign(s)`, `window.open(s)` | `javascript:` + open redirect | Same URL validation; prefer server-validated paths |
| `$(s)` / `$(el).html(s)` (jQuery) | HTML parsing | `.text()` |
| `dangerouslySetInnerHTML` (React) | Bypasses React escaping | Render as text; sanitize with a maintained sanitizer if HTML is unavoidable |
| `el.setAttribute("on…", s)`, `style` with `url()` | Handler/CSS injection | Event listeners; CSS classes |
| HTMX swap of attacker-controlled HTML | HTMX inserts verbatim | Escape server-side; `hx-disable` around user HTML |

```js
// static/js/safe-dom.js
export function safeHttpUrl(raw, { sameOrigin = false } = {}) {
  let url;
  try { url = new URL(raw, window.location.origin); } catch { return null; }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (sameOrigin && url.origin !== window.location.origin) return null;
  return url;
}

export function renderComment(list, { author, body }) {
  const li = document.createElement("li");
  const strong = document.createElement("strong");
  strong.textContent = author;           // text, never markup
  li.append(strong, ": ", body);         // append() with strings creates text nodes
  list.append(li);
}
```

Sources to treat as attacker-controlled: `location.*` (hash, search), `document.referrer`, `postMessage` data (check `event.origin`), `localStorage` (anyone with XSS once can plant payloads), API JSON fields, `window.name`.

### Trusted Types

Trusted Types make DOM sinks (`innerHTML`, `script.src`, `eval`…) reject plain strings when CSP contains `require-trusted-types-for 'script'`; only values produced by a named policy pass. This turns DOM XSS review into reviewing a few policies. Browser support: long Chromium-only; check current Baseline status before relying on it — elsewhere the header is ignored, so treat it as defense in depth [VERIFY: Trusted Types Baseline status / Firefox & Safari support on MDN]. Roll out with `Content-Security-Policy-Report-Only: require-trusted-types-for 'script'` first; HTMX and many libraries assign `innerHTML` and need a policy or will be reported.

### Content Security Policy

CSP is a response header that tells the browser which sources of script/style/frames/connections are allowed. A **nonce-based strict policy** is the modern baseline:

```text
Content-Security-Policy:
  default-src 'self';
  script-src 'self' 'nonce-<random-per-response>';
  style-src 'self';
  img-src 'self' data:;
  connect-src 'self';
  object-src 'none';
  base-uri 'self';
  form-action 'self';
  frame-ancestors 'none';
  report-to csp-endpoint
```

- The nonce must be unpredictable and **new on every response**; a cached page with a fixed nonce defeats it.
- Inline `<script>` without the nonce, `onclick=""` attributes and `javascript:` URLs are blocked.
- `'strict-dynamic'` lets nonce'd scripts load further scripts (useful for bundlers/third-party loaders); hosts in the allowlist are then ignored by supporting browsers.
- `'unsafe-inline'` in `script-src` largely nullifies XSS protection; `'unsafe-eval'` permits `eval`/`new Function`.

Framework implications:

| Feature | CSP requirement | Strict-CSP alternative |
|---|---|---|
| HTMX core requests/swaps | none special | — |
| `hx-on:*`, `hx-vals='js:…'`, `hx-trigger` `[filters]` | `'unsafe-eval'` | `htmx.config.allowEval = false`; listeners in modules |
| HTMX indicator styles | inline `<style>` → `style-src` | `includeIndicatorStyles: false` + own CSS |
| `<script>` tags inside HTMX swaps | nonce must match | `allowScriptTags: false`; init via `htmx:load` |
| Alpine.js standard build | `'unsafe-eval'` (expressions via `new Function`) | `@alpinejs/csp` build with `Alpine.data()` components [VERIFY: current CSP-build expression support] |
| Inline `style=""` attributes / JS `el.style` | `style=""` needs `'unsafe-inline'` in `style-src` (or `style-src-attr`); CSSOM `el.style.x =` is allowed | Classes, CSS custom properties set via CSSOM |
| React | none special (no eval) unless libraries need it | — |
| Vite dev server | HMR needs inline/eval/websocket | Dev-only relaxed policy; never ship it |

### Django CSP integration

- **Django 5.2**: use **django-csp (third-party)**. Version 4.x uses a dict setting `CONTENT_SECURITY_POLICY = {"DIRECTIVES": {...}}` with `csp.constants` (`SELF`, `NONCE`), middleware `csp.middleware.CSPMiddleware`, and exposes `request.csp_nonce` [VERIFY: django-csp 4.x setting/constant names and nonce context processor].
- **Django ≥ 6.0**: built-in CSP middleware and settings (`SECURE_CSP`, `SECURE_CSP_REPORT_ONLY`, nonce available to templates via a context processor) [VERIFY: exact Django 6.0 setting, middleware and context-processor names].

```python
# settings/base.py — Django 5.2 + django-csp 4.x (third-party)
from csp.constants import NONE, NONCE, SELF

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "csp.middleware.CSPMiddleware",
    # ...
]

CONTENT_SECURITY_POLICY_REPORT_ONLY = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF, NONCE],
        "style-src": [SELF],
        "img-src": [SELF, "data:"],
        "connect-src": [SELF],
        "object-src": [NONE],
        "base-uri": [SELF],
        "form-action": [SELF],
        "frame-ancestors": [NONE],
        "report-uri": "/csp-report/",
    }
}
```

```django
{# templates/base.html — nonce on the few inline scripts you keep #}
{{ dashboard_config|json_script:"dashboard-config" }}
<script nonce="{{ request.csp_nonce }}" type="module">
  import { initDashboard } from "{% static 'js/dashboard.js' %}";
  initDashboard(JSON.parse(document.getElementById("dashboard-config").textContent));
</script>
```

`json_script` emits `<script type="application/json">`, which is data, not executed — no nonce needed for it.

**Rollout**: (1) ship `Report-Only` with the target policy; (2) collect reports (endpoint with rate limiting; reports are noisy — extensions, injected ISP scripts); (3) remove inline handlers/eval usage; (4) enforce; (5) keep Report-Only for the *next* tightening step (e.g. Trusted Types).

### Cookies from the browser's view

| Attribute | Effect | Django setting |
|---|---|---|
| `HttpOnly` | Not readable by `document.cookie` — XSS cannot exfiltrate | `SESSION_COOKIE_HTTPONLY = True` (default) |
| `Secure` | Sent only over HTTPS | `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` |
| `SameSite=Lax` | Not sent on cross-site subrequests/POSTs; sent on top-level GET navigations | `SESSION_COOKIE_SAMESITE` (default `"Lax"`) |
| `SameSite=Strict` | Never cross-site (breaks inbound links into logged-in state) | — |
| `SameSite=None` | Cross-site allowed; requires `Secure` | Only for genuine cross-site embeds |
| `__Host-` prefix | Browser enforces `Secure`, `Path=/`, no `Domain` — prevents subdomain overwrite | `SESSION_COOKIE_NAME = "__Host-sessionid"`, `CSRF_COOKIE_NAME = "__Host-csrftoken"` |

The CSRF cookie is intentionally readable (`CSRF_COOKIE_HTTPONLY` default False) so JS can copy it into a header; making it HttpOnly gives little benefit — then read the token from a template/meta tag instead. Details: [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md).

### Why JWTs in localStorage are risky

`localStorage`/`sessionStorage` are readable by any script on the origin. One XSS (or one compromised dependency) can exfiltrate a long-lived bearer token and use it from anywhere, outliving the session. HttpOnly session cookies cannot be read by script (XSS can still act *as* the user while the page is open, but can't steal the credential). For same-origin Django frontends prefer **session cookies + CSRF**; for SPAs see [django-react-integration.md](django-react-integration.md) and [authentication.md](../../django-rest-framework/references/authentication.md).

## 4. Engineering decisions and trade-offs

| Decision | Prefer A when… | Prefer B when… |
|---|---|---|
| Session cookie vs bearer token in JS | **Session cookie**: same-site browser app (Django templates, HTMX, same-origin SPA) | **Token**: non-browser clients, third-party API consumers (keep tokens out of JS storage in browsers where possible) |
| Nonce CSP vs hash CSP | **Nonce**: server-rendered pages (Django renders every response) | **Hash**: static HTML served from CDN with a few fixed inline scripts |
| Self-host vs CDN for libraries | **Self-host** (default): single origin, simpler CSP, no third-party outage/compromise | **CDN**: only with pinned version + SRI + `crossorigin` |
| Alpine standard vs CSP build | **Standard**: no strict CSP yet | **CSP build**: strict CSP without `'unsafe-eval'`, accept `Alpine.data()` components |
| `X-Frame-Options` vs `frame-ancestors` | **XFO**: Django default, legacy coverage | **`frame-ancestors`**: need an allowlist of partner origins; send both (CSP wins where supported) |
| Sanitize HTML vs render text | **Text**: almost always | **Sanitize** (server-side, allowlist sanitizer): rich text is a product requirement |

## 5. Django/frontend implementation patterns

### 5.1 Safe data into templates and JS

```django
{# Good: autoescaped text #}
<h2>{{ project.name }}</h2>
<a href="{% url 'projects:detail' project.pk %}">Open</a>

{# Good: data for JS #}
{{ chart_data|json_script:"chart-data" }}

{# BAD: script context, autoescape is the wrong escaping here #}
<script>const name = "{{ project.name }}";</script>

{# BAD: user-controlled URL; autoescape does not block javascript: #}
<a href="{{ profile.website }}">Website</a>
```

Validate stored URLs server-side (`URLValidator(schemes=["http", "https"])`) and render with `rel="noopener noreferrer"` for external links (`noopener` is the default for `target="_blank"` in modern browsers, `noreferrer` also strips the Referer). Build HTML in Python only with `format_html`/`format_html_join`. Template escaping details: [django-template-engineering.md](django-template-engineering.md).

### 5.2 CSRF in fetch, HTMX and SPA

```js
// static/js/http.js — same-origin fetch with session cookie + CSRF
function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content
    ?? document.cookie.split("; ").find((c) => c.startsWith("csrftoken="))?.split("=")[1];
}

export async function postJSON(url, body, { signal } = {}) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
```

```django
<meta name="csrf-token" content="{{ csrf_token }}">
```

If you renamed the cookie (`__Host-csrftoken`), update the reader. HTMX: `hx-headers` on `<body>` or `htmx:configRequest` ([htmx-engineering.md](htmx-engineering.md)). Cross-origin SPA: `CSRF_TRUSTED_ORIGINS` with scheme (Django ≥ 4.0), CORS with credentials only for exact origins ([django-react-integration.md](django-react-integration.md)). Never use `@csrf_exempt` to "fix" a 403.

### 5.3 Open redirects and `next=`

```python
# accounts/views.py
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, fallback="/"):
    candidate = request.POST.get("next") or request.GET.get("next")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return fallback
```

`LoginView` already validates `next`. Client-side, never do `location.href = new URLSearchParams(location.search).get("next")` — use `safeHttpUrl(raw, { sameOrigin: true })` (§3).

### 5.4 Clickjacking

Django's `XFrameOptionsMiddleware` sends `X-Frame-Options: DENY` by default (Django ≥ 3.0). Add CSP `frame-ancestors 'none'` (or explicit partner origins). Embeddable widgets get a dedicated view with `@xframe_options_exempt` **and** a narrow `frame-ancestors` allowlist, and must not perform sensitive actions in one click.

### 5.5 Upload UI

- `accept="image/png,image/jpeg"` and client-side size checks are UX only; the server validates type (content sniffing), size and filename ([file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)).
- Preview local files with `URL.createObjectURL(file)` into `<img>` (revoke after use); never inject file names via `innerHTML`.
- Serve user uploads from a separate domain or with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`; never render user-uploaded SVG/HTML inline on the app origin.
- UI patterns: [django-forms-and-validation.md](django-forms-and-validation.md).

### 5.6 Client-side secrets and storage

- Everything bundled is public: `import.meta.env.VITE_*`, `process.env.*` replaced at build, config objects in `json_script`. Only publishable keys (e.g. a payment provider's publishable key, a public analytics ID) may appear. Secrets stay in Django settings ([secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md)).
- `localStorage`: no tokens, no PII; it persists, is shared by all scripts on the origin and survives logout unless you clear it. Clear app storage on logout (`Clear-Site-Data: "storage"` response header on logout or explicit `localStorage.removeItem`).
- HTMX history cache stores page snapshots in `localStorage` — disable for sensitive pages ([htmx-engineering.md](htmx-engineering.md)).
- Alpine `$persist` writes to `localStorage` — same rules.

### 5.7 Dependencies and third-party scripts

```bash
npm ci                       # install exactly from package-lock.json
npm audit --omit=dev         # production dependency advisories
npm outdated
```

- Commit lockfiles; use `npm ci` in CI; review new transitive deps; enable automated update PRs.
- Pin exact CDN versions; never `@latest` or unversioned URLs.
- Third-party scripts run with full page privileges (read DOM, cookies without HttpOnly, keystrokes). Minimize them, load them only on pages that need them, isolate in sandboxed iframes where possible.
- Supply-chain process: [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md).

```html
<script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js"
        integrity="sha384-REPLACE_WITH_PUBLISHED_HASH"
        crossorigin="anonymous" defer></script>
```

SRI: the browser hashes the downloaded file and refuses to run it if it doesn't match `integrity`. Cross-origin SRI requires CORS (`crossorigin="anonymous"` + the CDN sending `Access-Control-Allow-Origin`). SRI can't be used for scripts that change content (e.g. vendor "latest" loaders) — that is itself a risk signal. Generate: `openssl dgst -sha384 -binary file.js | openssl base64 -A`.

### 5.8 Sensitive data exposure

- **Page source**: only put in context what the template needs; don't serialize whole model instances (`model_to_dict(user)` leaks password hashes, flags). Hidden inputs are visible.
- **`json_script`**: escapes for safe embedding but the data is fully visible; send minimal, permission-filtered fields.
- **Source maps**: public source maps expose original source and comments; publish them only to your error tracker or behind auth ([static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)).
- **Autocomplete**: `autocomplete="current-password"`/`"new-password"`/`"one-time-code"` help password managers (good); use `autocomplete="off"` only where browsers honor it and it matters (e.g. admin search of other users' data). Never disable paste on password fields.
- **Caching of authenticated pages**: send `Cache-Control: private, no-store` (or at least `private`) for pages with personal data so shared caches/CDNs and back-button on shared machines don't expose them — `@never_cache` sets no-cache/no-store directives [VERIFY: exact directives @never_cache emits in 5.2]. `no-store` may exclude pages from the back/forward cache (performance trade-off). Details: [caching-engineering.md](../../django-performance/references/caching-engineering.md).
- **Error pages**: `DEBUG = False` in production; client-side error messages must not echo server stack traces.
- **Referrer leakage**: `SECURE_REFERRER_POLICY = "same-origin"` (Django default `"same-origin"` since 3.1) keeps tokens in URLs from leaking; never put secrets in URLs anyway.

## 6. Production use cases

- **Multi-tenant dashboard**: session cookie `__Host-` prefixed, strict nonce CSP, HTMX with `allowEval: false`, Alpine CSP build, all fragment views tenant-scoped ([multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)).
- **Rich-text comments**: server-side allowlist sanitization, rendered inside `hx-disable`, CSP blocks residual inline script.
- **Embeddable status widget**: dedicated read-only view, `frame-ancestors` allowlist, no session-dependent actions.
- **Same-origin React island**: session auth + CSRF header from meta tag; no tokens in storage; `connect-src 'self'`.
- **Marketing pages with analytics**: third-party tags confined to public pages, not loaded on authenticated app pages.

## 7. Advantages and disadvantages

| Control | Advantage | Disadvantage |
|---|---|---|
| Autoescaping | On by default, covers most XSS | Context-blind (JS, URLs, CSS) |
| Strict nonce CSP | Blocks most injected script | Requires removing inline handlers/eval; reporting noise |
| Trusted Types | Centralizes DOM XSS review | Partial browser support; library friction |
| HttpOnly session cookies | Credential theft via XSS impossible | Needs CSRF protection |
| SRI | Detects CDN tampering | Breaks on any CDN file change; needs CORS |
| Self-hosting | One origin, simple CSP | You own updates |

## 8. Security and performance considerations

### Security

- Defense in depth: escape output **and** CSP **and** HttpOnly cookies **and** server-side authorization. Any single layer will eventually fail.
- Server re-checks every request: permissions, tenant scope, validation ([authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- Security headers baseline (HSTS, nosniff, Referrer-Policy, COOP) via `SecurityMiddleware` ([secure-django-configuration.md](../../django-security-engineering/references/secure-django-configuration.md)).
- Log and monitor CSP reports and client errors without logging tokens or PII ([security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)).

### Performance

- Self-hosting removes extra DNS/TLS connections to CDNs (often faster on HTTP/2+).
- A nonce requires per-response HTML — compatible with Django rendering, but full-page CDN caching of HTML must not cache nonces; use hashes or edge-generated nonces for cached HTML.
- `no-store` on authenticated pages can disable bfcache; use `private` where acceptable ([frontend-performance.md](frontend-performance.md)).
- Fewer third-party scripts improves both security and INP/LCP.

## 9. Common mistakes and anti-patterns

1. **`{{ value|safe }}` on user content** → remove `|safe`; sanitize server-side if HTML is required.
2. **`el.innerHTML = response.message`** → `textContent`.
3. **`<script>var x = "{{ x }}";</script>`** → `json_script`.
4. **`href="{{ user_url }}"`** without scheme validation → validate `http(s)` on save; reject `javascript:`.
5. **JWT in `localStorage` for a same-origin app** → session cookie + CSRF.
6. **Secret API key in `VITE_*`** → move to Django; proxy the call server-side.
7. **`@csrf_exempt` to fix 403 from fetch** → send `X-CSRFToken`, fix `CSRF_TRUSTED_ORIGINS`.
8. **Believing CORS protects against CSRF** → CSRF tokens + SameSite; CORS controls reads.
9. **`'unsafe-inline'` added to get CSP "working"** → nonces; move handlers to modules.
10. **Enforcing CSP directly in production** → Report-Only first.
11. **Same nonce in cached HTML** → never cache nonce'd HTML in shared caches.
12. **Hiding the Delete button for non-admins as the only control** → permission check in the view.
13. **`location.href = params.get("next")`** → same-origin validation.
14. **CDN script without SRI or with `@latest`** → pin + `integrity` + `crossorigin`, or self-host.
15. **Public source maps for an internal admin app** → upload privately to the error tracker.
16. **Authenticated pages cacheable by shared caches** → `Cache-Control: private` / `@never_cache`.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Console: "Refused to execute inline script" | Missing/incorrect nonce, or inline handler | Compare nonce attribute vs header; search templates for `on*=` |
| "Refused to evaluate a string as JavaScript" | Alpine standard build, `hx-on`, `setTimeout(string)` | Switch to CSP builds / modules |
| CSRF 403 on fetch | No header, renamed cookie, missing trusted origin | Request headers; `CSRF_TRUSTED_ORIGINS` scheme; cookie name |
| Login works, then cookie missing | `Secure` cookie on HTTP dev, `__Host-` with `Domain` | DevTools Application → Cookies |
| SRI failure ("integrity … failed") | File changed or wrong hash, missing CORS | Recompute hash; check `crossorigin` and ACAO header |
| Page framed unexpectedly / widget won't frame | XFO/`frame-ancestors` mismatch | Response headers on the framed URL |
| Trusted Types violation reports | Library assigns `innerHTML` | Report sample; add policy or replace usage |

Validation:

- Grep templates/JS for `|safe`, `mark_safe`, `innerHTML`, `insertAdjacentHTML`, `outerHTML`, `document.write`, `eval(`, `new Function`, `dangerouslySetInnerHTML`, `on[a-z]+=`, `javascript:`.
- `python manage.py check --deploy` for header/cookie settings.
- Browser: DevTools Console (CSP), Application (cookies/storage contents), Network (headers), page source for leaked data.
- Automated: XSS payload tests through forms in Playwright (assert no `dialog` event), CSP header assertions, CSRF 403 tests ([security-testing.md](../../django-testing/references/security-testing.md), [frontend-testing.md](frontend-testing.md)).
- External: Mozilla Observatory-style header scanners; OWASP ZAP baseline ([security-testing-and-incident-response.md](../../django-security-engineering/references/security-testing-and-incident-response.md)).

## 11. Related references and skills

- [htmx-engineering.md](htmx-engineering.md) — HTMX CSRF, config, `hx-disable`, history cache
- [alpine-js-engineering.md](alpine-js-engineering.md) — CSP build
- [javascript-engineering.md](javascript-engineering.md) — DOM APIs, fetch, storage, `json_script`
- [django-template-engineering.md](django-template-engineering.md) — escaping, safe strings
- [django-react-integration.md](django-react-integration.md) — SPA auth, CORS, cookies, env vars
- [django-forms-and-validation.md](django-forms-and-validation.md) — upload UI, client vs server validation
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — source maps, lockfiles, CI build
- [frontend-performance.md](frontend-performance.md), [frontend-testing.md](frontend-testing.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md): [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md), [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md), [cors-and-browser-security.md](../../django-security-engineering/references/cors-and-browser-security.md), [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md), [authentication-security.md](../../django-security-engineering/references/authentication-security.md), [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md), [secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md), [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md), [secure-django-configuration.md](../../django-security-engineering/references/secure-django-configuration.md), [threat-modeling.md](../../django-security-engineering/references/threat-modeling.md)
- [security-testing.md](../../django-testing/references/security-testing.md)

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/topics/security/
- https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- https://docs.djangoproject.com/en/5.2/ref/utils/#django.utils.html.format_html
- https://docs.djangoproject.com/en/5.2/howto/csrf/
- https://docs.djangoproject.com/en/5.2/ref/clickjacking/
- https://docs.djangoproject.com/en/5.2/ref/settings/#session-cookie-samesite
- https://docs.djangoproject.com/en/5.2/ref/middleware/#module-django.middleware.security
- https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP
- https://developer.mozilla.org/en-US/docs/Web/API/Trusted_Types_API
- https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Clear-Site-Data
- https://web.dev/articles/strict-csp
- https://web.dev/articles/trusted-types
- https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Unvalidated_Redirects_and_Forwards_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html
- https://django-csp.readthedocs.io/ (third-party package docs)
