# API Security

> Scope: security posture of Django REST Framework APIs — a secure `REST_FRAMEWORK` baseline,
> the OWASP API Security Top 10 (2023) mapped to DRF controls, choosing and hardening session,
> token and JWT authentication, object- and function-level authorisation pitfalls, mass
> assignment and excessive data exposure, throttling limits, pagination/filter/bulk abuse,
> error handling without disclosure, API inventory, and inbound webhook verification.
> Not covered here: DRF mechanics — auth classes
> ([authentication.md](../../django-rest-framework/references/authentication.md)), permission
> classes ([permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md)),
> throttles ([throttling-and-rate-limiting.md](../../django-rest-framework/references/throttling-and-rate-limiting.md)),
> paginators ([pagination.md](../../django-rest-framework/references/pagination.md)), exception
> handlers ([exception-handling-and-error-responses.md](../../django-rest-framework/references/exception-handling-and-error-responses.md));
> authorisation design ([authorization-and-access-control.md](./authorization-and-access-control.md));
> tenant isolation ([multi-tenant-security.md](./multi-tenant-security.md)); CSRF/CORS for SPAs
> ([csrf-protection.md](./csrf-protection.md), [cors-and-browser-security.md](./cors-and-browser-security.md));
> SSRF in outbound calls ([ssrf-prevention.md](./ssrf-prevention.md)); signing keys and token
> storage ([secrets-and-cryptography.md](./secrets-and-cryptography.md)).

## 1. Purpose and scope

APIs remove the friction that HTML views put in front of attackers: every object is addressable
by id, every field is visible in JSON, every request is scriptable at high rate. Most API
breaches are not exotic — they are **missing object-level checks, writable fields that should be
server-owned, and unbounded queries**. This reference tells an agent which DRF defaults are safe,
which are not, and which controls the application must add.

Baseline: Django 5.2 LTS, DRF 3.16. Third-party packages are named as such. Code examples were
not executed.

## 2. Core concepts

- **Framework protection vs developer responsibility.**

  | DRF provides | The application must |
  |---|---|
  | Authentication classes, `request.user`/`request.auth` | Choose classes, token lifetime, revocation |
  | Permission hooks (`has_permission`, `has_object_permission`) | Write the policy; scope every queryset |
  | Serializer field declarations, `read_only` | List fields explicitly; mark server-owned fields read-only |
  | Throttle classes backed by the cache | Pick rates/scopes; add login/OTP-specific limits; edge limits |
  | Pagination classes | Enable globally, cap page size |
  | `SessionAuthentication` CSRF enforcement | Keep it; send tokens from SPA |
  | Exception handler returning JSON | Ensure 5xx and 404/403 bodies leak nothing |

- **OWASP API Security Top 10 (2023) → DRF controls.**

  | Risk | Primary DRF/Django control |
  |---|---|
  | API1 Broken Object Level Authorization (BOLA/IDOR) | Scoped `get_queryset()`; object permissions via `get_object()`; 404 out of scope |
  | API2 Broken Authentication | Hardened auth classes, short-lived/hashed tokens, throttled login, MFA |
  | API3 Broken Object Property Level Authorization | Explicit `fields`, `read_only_fields`, separate input/output serializers |
  | API4 Unrestricted Resource Consumption | Pagination caps, throttles, `DATA_UPLOAD_MAX_*`, bounded list inputs, query timeouts |
  | API5 Broken Function Level Authorization | Per-action permissions (`get_permissions()`), admin endpoints separated |
  | API6 Unrestricted Access to Sensitive Business Flows | Business-specific throttles, idempotency keys, anti-automation, step-up auth |
  | API7 Server Side Request Forgery | URL allow-lists, private-IP blocking ([ssrf-prevention.md](./ssrf-prevention.md)) |
  | API8 Security Misconfiguration | Secure `REST_FRAMEWORK` baseline, `check --deploy`, no browsable API in prod, CORS allow-list |
  | API9 Improper Inventory Management | Generated OpenAPI schema, versioning, retiring old versions, no shadow endpoints |
  | API10 Unsafe Consumption of APIs | Validate third-party responses, timeouts, TLS verification, treat as untrusted input |

- **401 vs 403 vs 404.** 401: no/invalid credentials (with `WWW-Authenticate`). 403: known
  caller, action not allowed. 404: object not in the caller's scope — prefer it over 403 for
  objects so ids cannot be enumerated.
- **Bearer credentials vs ambient credentials.** Tokens in an `Authorization` header are not
  sent automatically by browsers (no CSRF), but are readable by any script on the page (XSS
  exfiltration). Session cookies are sent automatically (CSRF needed) but can be `HttpOnly`.

## 3. Internal mechanics

- **Dispatch order** (DRF `APIView.initial`): content negotiation → versioning →
  `perform_authentication` (lazy; triggered here) → `check_permissions` → `check_throttles` →
  handler. Throttles run **after** permissions, so unauthenticated floods of a protected endpoint
  are rejected by permissions before throttling counts them. (VERIFY against DRF 3.16 source.)
- **Object permissions run only inside `get_object()`.** `GenericAPIView.get_object()` filters
  `get_queryset()` → `filter_queryset()` → `get_object_or_404()` → `check_object_permissions()`.
  `has_object_permission` is **never** called for `list`, for custom `@action`s that load
  objects themselves, or for an overridden `retrieve()` that does `Model.objects.get(pk=...)`.
- **`SessionAuthentication.enforce_csrf()`** runs only when a session user is authenticated;
  anonymous requests are not CSRF-checked by DRF. `APIView.as_view()` wraps the view in
  `csrf_exempt` because DRF performs its own check for session auth.
- **`TokenAuthentication`** (`rest_framework.authtoken`, built-in to DRF) looks up
  `Token.objects.select_related("user").get(key=key)`: the key is stored **in plaintext**, one
  token per user, **no expiry** by default. A DB read leak equals full account takeover.
- **Throttles** compute a cache key (`user.pk` or client IP) and keep a list of timestamps in the
  cache. Read-modify-write is not atomic — concurrent requests can exceed the rate; with
  `LocMemCache` limits are per process. Client IP comes from `REMOTE_ADDR`, or from
  `X-Forwarded-For` honouring `NUM_PROXIES`; with `NUM_PROXIES` unset DRF uses the whole XFF
  header value, which a client can forge (VERIFY exact behaviour in DRF 3.16 `get_ident`).
- **Serializer writes.** `ModelSerializer` builds writable fields for every model field in
  `fields` that is not `editable=False`/auto; `fields = "__all__"` therefore includes `owner`,
  `is_staff`, `status`, `price`. `validated_data` only contains declared, writable fields — the
  defence is the field list.
- **Related fields** (`PrimaryKeyRelatedField`, `SlugRelatedField`) validate an id against their
  `queryset`. An unscoped `queryset=Project.objects.all()` lets a caller attach their object to
  another user's project — BOLA through a foreign key.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Browser SPA on same site | Session + CSRF vs JWT in JS | Session cookies (`HttpOnly`, `SameSite=Lax`) + CSRF header. JWT in `localStorage` is exfiltratable by any XSS. |
| Mobile / third-party clients | DRF authtoken, django-rest-knox (third-party), simplejwt (third-party), OAuth2 (django-oauth-toolkit, third-party) | Prefer hashed, expiring, revocable tokens (knox, OAuth2 access tokens). authtoken only for low-risk internal tools, ideally with a custom hashed model. |
| JWT | Stateless access + refresh | Accept only with: pinned algorithm, short access lifetime (5–15 min), refresh rotation + blacklist, `aud`/`iss` validation, dedicated signing key. Revocation needs state anyway. |
| Service-to-service | mTLS, OAuth2 client credentials, HMAC-signed requests | Do not reuse user tokens; scope per service. |
| Rate limiting | DRF throttles, django-ratelimit (third-party), edge/WAF/API gateway | Edge for volume and DoS; DRF scoped throttles for business flows (login, OTP, password reset, exports). |
| Pagination | Page-number, limit/offset, cursor | Cursor for large/public datasets (no deep-offset scans, no total count leak); always a `max_page_size`. |
| Error detail | Verbose validation errors vs generic | Field-level validation errors are fine; never include stack traces, SQL, internal ids or existence hints for auth/object lookups. |
| Browsable API | On in dev; off or staff-only in prod | It renders forms that reveal fields and enables session-auth exploration; disable via `DEFAULT_RENDERER_CLASSES` in prod. |

## 5. Django implementation patterns

### Secure `REST_FRAMEWORK` baseline

```python
# settings/production.py
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",   # browser clients (CSRF enforced)
        "knox.auth.TokenAuthentication",                           # third-party: django-rest-knox
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],  # no browsable API
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],        # add Multipart per view
    "DEFAULT_PAGINATION_CLASS": "core.api.pagination.BoundedPageNumberPagination",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min", "user": "600/min",
        "login": "10/min", "otp": "5/min", "password_reset": "5/hour", "export": "10/hour",
    },
    "NUM_PROXIES": 1,          # exactly the number of trusted proxies that append to XFF
    "EXCEPTION_HANDLER": "core.api.exceptions.security_exception_handler",
    "UNAUTHENTICATED_USER": None,  # request.user is None for anonymous; forces explicit checks (optional)
}
```

`UNAUTHENTICATED_USER = None` is a trade-off: it breaks code that calls
`request.user.is_authenticated` on anonymous requests; keep the default `AnonymousUser` unless
the team wants that strictness.

```python
# core/api/pagination.py
from rest_framework.pagination import PageNumberPagination

class BoundedPageNumberPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200          # without this, ?page_size=10000000 is honoured
```

### BOLA: scope the queryset, let `get_object()` do the rest

```python
class InvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated, IsInvoiceEditorOrReadOnly]

    def get_queryset(self):
        # Scoping is the primary control: out-of-scope ids become 404 for every action.
        return Invoice.objects.for_account(self.request.user.account).select_related("customer")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        invoice = self.get_object()            # runs object permissions; never Invoice.objects.get(pk=pk)
        services.void_invoice(invoice=invoice, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
```

### BFLA: per-action permissions

```python
def get_permissions(self):
    if self.action in {"destroy", "void"}:
        return [IsAuthenticated(), HasAccountRole("billing_admin")]
    return super().get_permissions()
```

Every `@action` inherits the ViewSet's `permission_classes` unless it passes its own — review each.

### BOPLA: mass assignment and excessive exposure

```python
class InvoiceWriteSerializer(serializers.ModelSerializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.none())
    created_by = serializers.HiddenField(default=serializers.CurrentUserDefault())

    class Meta:
        model = Invoice
        fields = ["customer", "due_date", "lines", "created_by"]   # never "__all__"
        # status, total, account, paid_at are server-owned: not listed at all

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context["request"]
        # Related ids are authorised too: only the caller's account customers are valid.
        self.fields["customer"].queryset = Customer.objects.for_account(request.user.account)


class InvoiceReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ["id", "number", "customer", "status", "total", "due_date"]   # no internal notes, cost price
```

Set `account` in `perform_create(serializer.save(account=...))` or in the service, never from
input. For user objects, `is_staff`, `is_superuser`, `groups`, `user_permissions`, `password`
must never be writable through a profile endpoint.

### Bounded inputs (API4)

```python
class BulkTagSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(min_value=1),
                                min_length=1, max_length=100)
    tag = serializers.CharField(max_length=50)
```

Also bound: `DATA_UPLOAD_MAX_MEMORY_SIZE`, `DATA_UPLOAD_MAX_NUMBER_FIELDS` (Django settings;
note DRF parses JSON bodies itself — the memory limit applies to `request.body` reads, VERIFY
coverage for streamed parsers), search term length, filter list length, and export row counts.

### Filtering and ordering allow-lists

```python
class CustomerViewSet(viewsets.ReadOnlyModelViewSet):
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "=reference"]             # never email/phone/notes unless intended
    ordering_fields = ["name", "created_at"]           # explicit; default "__all__"-like behaviour leaks
    ordering = ["-created_at", "id"]                   # deterministic, indexed
```

Without `ordering_fields`, `OrderingFilter` allows ordering by any serializer field, and
ordering by a hidden field can act as an oracle (binary-search a secret by sort position).
Filtering by related lookups (django-filter, third-party) must not traverse into fields the
caller cannot read (`?owner__email__startswith=a`).

### Error handling without disclosure

```python
# core/api/exceptions.py
import logging
from rest_framework.views import exception_handler

security_log = logging.getLogger("security.api")

def security_exception_handler(exc, context):
    response = exception_handler(exc, context)
    request = context.get("request")
    if response is not None and response.status_code in (401, 403):
        security_log.warning(
            "authz_fail",
            extra={"event": "authz_fail", "status": response.status_code,
                   "path": request.path if request else None,
                   "user_id": getattr(getattr(request, "user", None), "pk", None),
                   "view": type(context.get("view")).__name__},
        )
    # Unhandled exceptions return None → Django's 500 handler (DEBUG=False shows no traceback).
    return response
```

Keep `DEBUG = False`; make the project's 500 handler return a JSON body with a correlation id
only.

### Inbound webhook verification

```python
import hashlib, hmac, time
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

MAX_SKEW = 300

def verify_webhook(request) -> None:
    ts = request.headers.get("X-Signature-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW:
        raise AuthenticationFailed("stale")
    expected = hmac.new(settings.WEBHOOK_SECRET.encode(), f"{ts}.".encode() + request.body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AuthenticationFailed("bad signature")
    # Replay: also store the provider's event id with a unique constraint and ignore duplicates.
```

Use the provider's documented scheme (Stripe, GitHub, etc. differ); read `request.body` before
any parser consumes the stream; webhook views are `AllowAny` + signature, CSRF does not apply to
non-session auth.

### JWT hardening (djangorestframework-simplejwt, third-party)

```python
SIMPLE_JWT = {
    "ALGORITHM": "HS256",                       # pinned; never accept "none" or client-chosen alg
    "SIGNING_KEY": env("JWT_SIGNING_KEY"),      # default is SECRET_KEY — separate it
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=10),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,           # needs rest_framework_simplejwt.token_blacklist app
    "AUDIENCE": "api.example.com",
    "ISSUER": "auth.example.com",
    "UPDATE_LAST_LOGIN": False,
}
```

For multi-service verification prefer an asymmetric algorithm (RS256/EdDSA) so verifiers hold
only the public key. Put no personal data in claims — JWTs are signed, not encrypted.

## 6. Production use cases

- **B2B SaaS API**: knox or OAuth2 tokens per integration, scoped to an account; account-scoped
  querysets; per-token throttles; audit of token creation/revocation.
- **SPA + API on the same registrable domain**: `SessionAuthentication` only, CSRF via
  `X-CSRFToken`, CORS not needed if same origin; no tokens in JS storage.
- **Public read API**: cursor pagination, anon throttles at edge and app, cached responses, no
  internal ids or emails in output.
- **Mobile app**: short-lived access token + rotating refresh token in platform secure storage;
  revoke on logout and password change.
- **Webhooks receiver**: signature + timestamp + idempotent event-id table; process async.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Scoped querysets | One control covers list, detail, actions, related lookups | Must be repeated in serializers' related querysets |
| Session auth for SPA | `HttpOnly`, server-side revocation | CSRF handling; same-site deployment needed |
| DRF authtoken | Built in, simple | Plaintext at rest, no expiry, one token per user |
| knox (third-party) | Hashed, expiring, per-device tokens | Extra dependency; DB hit per request |
| JWT (simplejwt, third-party) | No DB lookup for access tokens | Revocation needs blacklist; key management; claim misuse |
| DRF throttles | Easy per-scope rules | Cache-dependent, non-atomic, IP trust issues |

## 8. Security and performance considerations

- Throttles need a shared cache (Redis/Memcached) in multi-process deployments; `LocMemCache`
  multiplies the effective limit by worker count.
- Throttle by user for authenticated calls and by identifier (username/email, hashed) for login
  to stop distributed credential stuffing; IP-only limits are bypassed by botnets.
- Deep `OFFSET` pagination and unindexed filters are DoS vectors; cursor pagination and indexed,
  allow-listed filters cost less.
- Scoping and permission checks belong in the same query: filter, do not fetch-then-check (fewer
  rows, no timing oracle).
- Hashing tokens (SHA-256 of a high-entropy token is sufficient; no slow hash needed) costs
  microseconds.
- `count()` on huge tables for page-number pagination can be both slow and a data-size leak.
- Disable or authenticate the OpenAPI schema/docs endpoint if it exposes internal endpoints.

## 9. Common mistakes and anti-patterns

- `permission_classes = []` or `AllowAny` inherited by accident on a new ViewSet.
- `queryset = Model.objects.all()` with only `IsAuthenticated` (BOLA).
- Custom `retrieve`/`@action` that calls `Model.objects.get(pk=pk)`, skipping object permissions.
- Relying on `has_object_permission` to protect `list`.
- `fields = "__all__"` or `exclude = [...]` (new model fields become exposed/writable automatically).
- Unscoped `PrimaryKeyRelatedField(queryset=...all())` on write serializers.
- Returning the same serializer for owner and non-owner views (leaks private fields).
- `max_page_size` missing; `page_size_query_param` enabled without a cap.
- `NUM_PROXIES` unset behind a load balancer, or set too high (client controls the IP).
- JWT in `localStorage`; JWT signed with `SECRET_KEY` and never rotated; accepting tokens
  without `aud` check across services.
- Leaving `/api/v1/` live and unpatched after `/api/v2/` ships (API9).
- Exception handler that returns `str(exc)` for unexpected errors.
- Webhook signature compared with `==`, or computed over re-serialised JSON instead of raw body.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Other users' objects reachable by id | Unscoped `get_queryset()` or bypassed `get_object()` | Cross-user test per detail route and action |
| 403 "CSRF Failed" from SPA | Session auth without `X-CSRFToken` | [csrf-protection.md](./csrf-protection.md) |
| Throttle never triggers | LocMem cache per worker, or throttle not in view's `throttle_classes` | Inspect `view.get_throttles()`; cache backend |
| Everyone throttled together | All requests appear from proxy IP (`NUM_PROXIES` 0 / misconfigured) | Log `REMOTE_ADDR` and XFF in staging |
| 401 vs 403 confusion | First auth class has no `authenticate_header` | DRF authentication docs |
| Field silently ignored on write | Field read-only or not in `fields` — usually correct | Confirm intent; add test |

Validation steps:

- Permission matrix tests (anon, other user, other tenant, each role) for every route and
  `@action` — see [security-testing-and-incident-response.md](./security-testing-and-incident-response.md).
- Test that posting `{"is_staff": true, "account": <other>}` changes nothing.
- Diff the OpenAPI schema per release; review new endpoints and fields.
- Assert `?page_size=100000` returns at most `max_page_size` items.
- Grep: `"__all__"`, `AllowAny`, `objects.get(pk=`, `authentication_classes = []`.

## 11. Related references and skills

- [authorization-and-access-control.md](./authorization-and-access-control.md) — policy design, IDOR.
- [authentication-security.md](./authentication-security.md) — login abuse, MFA, credential handling.
- [multi-tenant-security.md](./multi-tenant-security.md) — tenant scoping of querysets and tokens.
- [csrf-protection.md](./csrf-protection.md) · [cors-and-browser-security.md](./cors-and-browser-security.md)
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — token generation/hashing, signing keys.
- [ssrf-prevention.md](./ssrf-prevention.md) · [security-logging-and-auditing.md](./security-logging-and-auditing.md)
- [django-rest-framework](../../django-rest-framework/SKILL.md):
  [authentication.md](../../django-rest-framework/references/authentication.md),
  [permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md),
  [throttling-and-rate-limiting.md](../../django-rest-framework/references/throttling-and-rate-limiting.md),
  [pagination.md](../../django-rest-framework/references/pagination.md),
  [filtering-search-and-ordering.md](../../django-rest-framework/references/filtering-search-and-ordering.md),
  [exception-handling-and-error-responses.md](../../django-rest-framework/references/exception-handling-and-error-responses.md)
- [django-integrations](../../django-integrations/SKILL.md) — webhook processing, identity providers.

## 12. Official documentation and external references

- DRF Authentication: https://www.django-rest-framework.org/api-guide/authentication/
- DRF Permissions: https://www.django-rest-framework.org/api-guide/permissions/
- DRF Throttling: https://www.django-rest-framework.org/api-guide/throttling/
- DRF Pagination: https://www.django-rest-framework.org/api-guide/pagination/
- DRF Filtering: https://www.django-rest-framework.org/api-guide/filtering/
- DRF Settings: https://www.django-rest-framework.org/api-guide/settings/
- Django security overview: https://docs.djangoproject.com/en/5.2/topics/security/
- OWASP API Security Top 10 (2023): https://owasp.org/API-Security/editions/2023/en/0x11-t10/
- OWASP REST Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html
- OWASP JSON Web Token Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html
- OWASP Mass Assignment Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Mass_Assignment_Cheat_Sheet.html
- RFC 8725 JWT Best Current Practices: https://www.rfc-editor.org/rfc/rfc8725
- RFC 6750 Bearer Token Usage: https://www.rfc-editor.org/rfc/rfc6750
- django-rest-knox (third-party): https://jazzband.github.io/django-rest-knox/
- djangorestframework-simplejwt (third-party): https://django-rest-framework-simplejwt.readthedocs.io/
