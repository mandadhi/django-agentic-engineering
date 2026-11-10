# CSRF Protection

> Scope: cross-site request forgery mechanics, how `CsrfViewMiddleware` decides (Origin, Referer,
> token), token masking, the CSRF decorators, cookie vs session storage, AJAX and SPA integration,
> `CSRF_TRUSTED_ORIGINS` and subdomains, DRF's session-only enforcement, login CSRF, safe
> exemptions (signed webhooks), failure views and testing.
> Not covered here: CORS and cross-origin reads ([cors-and-browser-security.md](./cors-and-browser-security.md));
> session cookie flags and `SameSite` semantics ([session-and-cookie-security.md](./session-and-cookie-security.md));
> token-authenticated APIs ([api-security.md](./api-security.md)); proxy/HTTPS detection
> ([secure-django-configuration.md](./secure-django-configuration.md)); webhook signature design
> ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); middleware mechanics
> ([django-core middleware.md](../../django-core/references/middleware.md)).

## 1. Purpose and scope

Browsers attach cookies to requests a *different* site triggers. If the application
authenticates with cookies (sessions), any site the user visits can submit forms or fire
requests that the server cannot distinguish from intentional ones. CSRF protection proves that
an unsafe request originated from the application's own pages. Django enables this by default
for every view through `CsrfViewMiddleware`; developer responsibilities are: keep unsafe
operations off safe methods, include the token in forms and JavaScript requests, configure
trusted origins for the real topology, and never exempt cookie-authenticated views.

Baseline: Django 5.2 LTS + DRF 3.16. Examples were not executed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Ambient credential | Credential the browser attaches automatically (cookies, HTTP Basic, client certs) — the precondition for CSRF |
| Safe methods | `GET`, `HEAD`, `OPTIONS`, `TRACE` — never checked, so they must not change state |
| CSRF secret | 32-char random value stored in the `csrftoken` cookie (or the session) |
| Masked token | 64-char per-response encoding of the secret (`{% csrf_token %}`, `get_token()`), mitigating BREACH |
| Origin check | Compares the `Origin` header with the request host and `CSRF_TRUSTED_ORIGINS` |
| Referer check | Fallback for HTTPS requests without `Origin` |
| Login CSRF | Forcing a victim to log in to the *attacker's* account, so their actions are recorded there |

Not in scope of CSRF: token- or header-authenticated APIs (`Authorization: Bearer …`) — the
browser does not attach those automatically, so CSRF does not apply (XSS still does).

## 3. Internal mechanics

`CsrfViewMiddleware.process_view` in Django 5.2, simplified:

1. View marked `csrf_exempt` → accept. Request method safe → accept (token still available).
2. Test-client flag `_dont_enforce_csrf_checks` → accept (see section 10).
3. **Origin**: if `HTTP_ORIGIN` present, it must equal `request.scheme + "://" + request.get_host()`
   or match `CSRF_TRUSTED_ORIGINS` (entries include the scheme since Django 4.0; wildcard
   subdomains like `https://*.example.com` allowed) — else reject "Origin checking failed".
4. **Referer** (only if no `Origin` and `request.is_secure()`): must be present, HTTPS, and match
   the host, `CSRF_COOKIE_DOMAIN` (or `SESSION_COOKIE_DOMAIN` with `CSRF_USE_SESSIONS`), or a
   trusted origin — protects against HTTP MITM injecting forms into HTTPS sites.
5. **Token**: secret read from the cookie (`CSRF_COOKIE_NAME`) or session (`CSRF_USE_SESSIONS`);
   submitted token read from `POST["csrfmiddlewaretoken"]`, else from the header named by
   `CSRF_HEADER_NAME` (default `HTTP_X_CSRFTOKEN`, i.e. `X-CSRFToken`). The token is unmasked
   and compared in constant time. Missing/incorrect → reject.
6. Rejection calls `CSRF_FAILURE_VIEW` (default `django.views.csrf.csrf_failure`, 403) and logs on
   `django.security.csrf`.

Other mechanics:

- `get_token(request)` creates the secret if needed and flags the response to set the cookie;
  `{% csrf_token %}` calls it. `process_response` sets the cookie and patches `Vary: Cookie`.
- Since Django 4.1 the cookie holds the **unmasked** secret (transitional `CSRF_COOKIE_MASKED`
  removed in 5.0); masked tokens differ per response, so pages can embed them safely under
  compression.
- `rotate_token(request)` is called by `login()`, defeating fixation of a pre-login token.
- Decorators (`django.views.decorators.csrf`): `csrf_exempt` (skip), `csrf_protect` (enforce on a
  view when the middleware is absent/bypassed), `requires_csrf_token` (make the token available
  without enforcing — e.g. custom 403/404 templates), `ensure_csrf_cookie` (force cookie set on a
  `GET`, for JavaScript-only pages without a form).
- **DRF**: `APIView.as_view()` wraps the view in `csrf_exempt`; `SessionAuthentication` then
  calls `enforce_csrf()` **only when a session user is authenticated**. Unauthenticated unsafe
  requests and other authentication classes (Token, JWT) are not CSRF-checked.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Secret storage | Cookie (default) vs `CSRF_USE_SESSIONS = True` | Cookie works for JS clients reading it; sessions avoid an extra cookie but JS must get the token from the DOM/endpoint |
| `CSRF_COOKIE_HTTPONLY` | `False` (default) vs `True` | Little security value (XSS can read the token from the DOM); `True` breaks the standard JS pattern |
| SPA auth | Session cookie + CSRF vs bearer token | Same-site SPA: session + CSRF (HttpOnly session, revocable). Third-party/mobile: tokens, no CSRF |
| Cross-subdomain SPA | `CSRF_TRUSTED_ORIGINS` + shared cookie domain vs token endpoint | Prefer a `GET /api/csrf/` endpoint returning `get_token()` over a parent-domain cookie |
| Webhooks | `csrf_exempt` + signature | The only routine reason to exempt; authenticity comes from HMAC, not cookies |
| `SameSite` as CSRF defence | Rely on it vs keep tokens | Keep tokens; SameSite is browser-dependent, doesn't cover same-site subdomains or Lax top-level GETs |

## 5. Django implementation patterns

### Forms (default)

```django
<form method="post" action="{% url 'billing:cancel' subscription.pk %}">
  {% csrf_token %}
  <button type="submit">Cancel subscription</button>
</form>
```

Never put `{% csrf_token %}` in a form whose `action` points to an external site — it leaks the token.

### Fetch / AJAX from Django-rendered pages

```django
{# base.html — token available even if no form on the page #}
<meta name="csrf-token" content="{{ csrf_token }}">
```

```javascript
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

async function postJSON(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    body: JSON.stringify(payload),
  });
  if (response.status === 403) throw new Error("CSRF or permission failure");
  return response.json();
}
```

Reading from the DOM works with `CSRF_USE_SESSIONS` and `CSRF_COOKIE_HTTPONLY`; reading the
`csrftoken` cookie also works with defaults. For pages that render no token, add
`@ensure_csrf_cookie` to the view.

### SPA on a different subdomain

```python
# settings.py — SPA at https://app.example.com, Django API at https://api.example.com
CSRF_TRUSTED_ORIGINS = ["https://app.example.com"]
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
# CORS with credentials for app.example.com is required too — see cors-and-browser-security.md
```

```python
# api/views.py
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.http import require_GET


@require_GET
def csrf_token_view(request):
    return JsonResponse({"csrfToken": get_token(request)})
```

The SPA calls this with `credentials: "include"`, then sends the value as `X-CSRFToken`. This
avoids `CSRF_COOKIE_DOMAIN = ".example.com"`, which exposes the cookie to every subdomain.

### DRF login endpoint (unauthenticated → not checked by DRF)

```python
from django.contrib.auth import login
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import LoginSerializer


@method_decorator(csrf_protect, name="dispatch")
class SessionLoginView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []  # no session auth before login
    throttle_scope = "login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        login(request, serializer.validated_data["user"])
        return Response(status=status.HTTP_204_NO_CONTENT)
```

`csrf_protect` on `dispatch` runs against the underlying Django `HttpRequest` before DRF's
`csrf_exempt` matters (`VERIFY` decorator interaction in the project's DRF version with a test).
Django's own `LoginView` is protected by the middleware already.

### Webhook exemption with signature verification

```python
import hashlib
import hmac
import time

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

MAX_SKEW_SECONDS = 300


@csrf_exempt  # no cookies involved; authenticity comes from the HMAC below
@require_POST
def payment_webhook(request):
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    signature = request.headers.get("X-Webhook-Signature", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
        return HttpResponseBadRequest()
    expected = hmac.new(
        settings.PAYMENT_WEBHOOK_SECRET.encode(),
        timestamp.encode() + b"." + request.body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return HttpResponseForbidden()
    enqueue_payment_event(request.body)  # idempotent processing keyed on event id
    return HttpResponse(status=204)
```

Use the provider's documented scheme; the shape above is illustrative.

### Custom failure view

```python
# settings.py
CSRF_FAILURE_VIEW = "core.views.csrf_failure"

# core/views.py
from django.shortcuts import render


def csrf_failure(request, reason=""):
    # Do not echo `reason` to users in production; it is logged by django.security.csrf.
    return render(request, "errors/csrf_failure.html", status=403)
```

## 6. Production use cases

- **Server-rendered app**: defaults plus `CSRF_COOKIE_SECURE = True`; nothing exempted.
- **HTMX**: send the token globally — `<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>`.
- **Same-origin SPA served by Django**: session auth + `X-CSRFToken`; DRF `SessionAuthentication`
  enforces CSRF for logged-in calls.
- **Mobile app + web app sharing an API**: web uses sessions (CSRF enforced), mobile uses
  token auth (no CSRF); configure `DEFAULT_AUTHENTICATION_CLASSES` so a token-auth request can
  never silently fall back to an ambient session without CSRF ([api-security.md](./api-security.md)).
- **TLS-terminating proxy**: Origin check compares with `request.scheme`; without a correct
  `SECURE_PROXY_SSL_HEADER`, `https://` origins mismatch the `http` scheme and every POST fails.

## 7. Advantages and disadvantages

| Mechanism | Advantages | Disadvantages |
|---|---|---|
| Synchroniser token (Django) | Robust, browser-independent, on by default | Requires token plumbing for JS clients; caching pages with tokens needs care |
| Origin/Referer checks | Cheap, no state | Some privacy tools strip `Referer`; legitimate cross-origin setups need trusted origins |
| `SameSite` cookies | Blocks most cross-site sends automatically | Same-site subdomains and Lax top-level GETs still pass; browser-dependent |
| Exemption + signature | Correct for server-to-server callbacks | Must be implemented correctly per provider |

## 8. Security and performance considerations

- **XSS defeats CSRF protection** — script on the origin can read tokens and send requests;
  fix XSS ([cross-site-scripting.md](./cross-site-scripting.md)).
- **Subdomains are same-site**: a compromised or user-content subdomain can set cookies for the
  parent domain (cookie tossing) and pass SameSite; keep `CSRF_COOKIE_DOMAIN` host-only, use
  wildcard trusted origins sparingly.
- **State changes on GET** are never protected; logout, "unsubscribe", approve links must be
  `POST` (or carry a single-use signed token).
- **Caching**: a cached page containing a token rendered for another visitor yields 403s, not a
  breach, because the secret is per-browser; still prefer not to full-page-cache forms. With
  `cache_page`, keep the `Vary: Cookie` Django adds.
- **BREACH**: masking protects the CSRF token, not other secrets reflected in compressed
  responses; avoid `GZipMiddleware` on pages mixing secrets and user-reflected input.
- `CSRF_COOKIE_AGE` default is ~1 year; it is not an authentication credential, but rotate via
  `rotate_token` on privilege changes (login does this).
- CORS does not protect against CSRF: simple `POST` (form-encoded) requests are sent
  cross-origin without preflight. Only JSON with custom headers triggers preflight.

## 9. Common mistakes and anti-patterns

- `@csrf_exempt` on a session-authenticated view "to make the SPA work".
- DRF API with `SessionAuthentication` and `AllowAny` endpoints that change state for anonymous
  users, assumed CSRF-protected (they are not).
- Mixing `SessionAuthentication` into a token API used by browsers: a request without the
  token header can authenticate by the ambient session.
- `CSRF_TRUSTED_ORIGINS = ["example.com"]` — missing scheme; invalid since Django 4.0
  (`4_0.E001` system check error).
- `CSRF_TRUSTED_ORIGINS = ["https://*.example.com"]` when user-controlled subdomains exist.
- Removing `CsrfViewMiddleware` and decorating "important" views with `csrf_protect` — new views
  are unprotected by default.
- State-changing `GET` endpoints (`/delete/<id>/`, `/logout/`).
- Exposing the failure `reason` or a debug CSRF page in production.
- Webhook signature compared with `==` (timing leak) or computed over re-serialised JSON instead
  of the raw `request.body`.

## 10. Troubleshooting and validation

| Symptom (403 reason) | Cause | Fix |
|---|---|---|
| "Origin checking failed - https://x does not match any trusted origins" | Cross-origin SPA, or proxy scheme/host mismatch | Add origin with scheme; fix `SECURE_PROXY_SSL_HEADER` / `Host` forwarding |
| "Referer checking failed - no Referer" | HTTPS request without `Origin` and `Referer` (privacy policy `no-referrer`) | Use `strict-origin-when-cross-origin`/`same-origin` referrer policy |
| "CSRF cookie not set" | Page never called `get_token`; cookie blocked; different domain | `ensure_csrf_cookie`, token endpoint, check `Secure` on HTTP dev |
| "CSRF token missing" / "incorrect" | JS not sending header; wrong header name; stale token after login rotation | Send `X-CSRFToken`; re-read token after login |
| DRF 403 "CSRF Failed" only when logged in | `SessionAuthentication` enforcing for session users | Send the token, or use token auth for non-browser clients |

Tests — Django's test client skips CSRF unless asked:

```python
from django.test import Client, TestCase
from rest_framework.test import APIClient


class CsrfEnforcementTests(TestCase):
    def setUp(self):
        self.user = UserFactory()

    def test_form_post_without_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post("/billing/subscriptions/1/cancel/")
        self.assertEqual(response.status_code, 403)

    def test_form_post_with_token_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get("/billing/")  # page renders {% csrf_token %} and sets the cookie
        token = client.cookies["csrftoken"].value
        response = client.post("/billing/subscriptions/1/cancel/", HTTP_X_CSRFTOKEN=token)
        self.assertNotEqual(response.status_code, 403)

    def test_session_api_requires_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post("/api/orders/", {"sku": "A1"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_untrusted_origin_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get("/billing/")
        token = client.cookies["csrftoken"].value
        response = client.post("/billing/subscriptions/1/cancel/",
                               HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)
```

Review check: `grep -rn "csrf_exempt" --include=*.py` — every hit needs a justification comment
and a non-cookie authenticity check.

## 11. Related references and skills

- [session-and-cookie-security.md](./session-and-cookie-security.md) — `SameSite`, cookie scope, `rotate_token` on login.
- [cors-and-browser-security.md](./cors-and-browser-security.md) — credentials with CORS, preflight, same-origin policy.
- [api-security.md](./api-security.md) — authentication-class selection and CSRF for DRF.
- [authentication-security.md](./authentication-security.md) — login/logout flows (login CSRF, POST logout).
- [secure-django-configuration.md](./secure-django-configuration.md) — proxy scheme/host trust.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — CSRF regression suite.
- [django-core middleware.md](../../django-core/references/middleware.md) ·
  [django-rest-framework authentication.md](../../django-rest-framework/references/authentication.md) ·
  [django-frontend](../../django-frontend/SKILL.md)

## 12. Official documentation and external references

- Cross Site Request Forgery protection (how it works, AJAX): https://docs.djangoproject.com/en/5.2/ref/csrf/
- How to use CSRF protection: https://docs.djangoproject.com/en/5.2/howto/csrf/
- CSRF settings: https://docs.djangoproject.com/en/5.2/ref/settings/#csrf-cookie-age
- Django 4.0 release notes (`CSRF_TRUSTED_ORIGINS` scheme, Origin checking): https://docs.djangoproject.com/en/5.2/releases/4.0/
- DRF — CSRF and `SessionAuthentication`: https://www.django-rest-framework.org/api-guide/authentication/#sessionauthentication
- DRF — Working with AJAX, CSRF & CORS: https://www.django-rest-framework.org/topics/ajax-csrf-cors/
- OWASP CSRF Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- RFC 6454 — The Web Origin Concept: https://www.rfc-editor.org/rfc/rfc6454
- BREACH attack: https://www.breachattack.com/
