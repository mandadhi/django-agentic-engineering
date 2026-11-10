# Exception Handling and Error Responses

> **Scope.** Owns: DRF's exception classes, how DRF converts exceptions into responses, `Http404`
> and Django `PermissionDenied` integration, custom exception handlers, a consistent error body,
> field-level error formatting, domain-exception mapping, unexpected 500s from APIs, correlation
> IDs in error responses, and error disclosure.
> Does not cover: validation rules themselves ([fields-and-validation.md](fields-and-validation.md)),
> Django logging configuration and generic exception flow
> ([django-core logging-exceptions](../../django-core/references/logging-exceptions.md)), or
> incident response.

## 1. Purpose and scope

Clients integrate against errors as much as successes. This reference defines how to produce a
single, predictable error format and how to separate expected client errors from server failures.

## 2. Core concepts

**`APIException` hierarchy** (`rest_framework.exceptions`):

| Exception | Status | Default code |
|---|---|---|
| `ValidationError` | 400 | `invalid` |
| `ParseError` | 400 | `parse_error` |
| `AuthenticationFailed` | 401 (or 403 without challenge) | `authentication_failed` |
| `NotAuthenticated` | 401 (or 403 without challenge) | `not_authenticated` |
| `PermissionDenied` | 403 | `permission_denied` |
| `NotFound` | 404 | `not_found` |
| `MethodNotAllowed` | 405 | `method_not_allowed` |
| `NotAcceptable` | 406 | `not_acceptable` |
| `UnsupportedMediaType` | 415 | `unsupported_media_type` |
| `Throttled` | 429 (+ `Retry-After`) | `throttled` |

Each has `status_code`, `default_detail`, `default_code`; `detail` is an `ErrorDetail` (string
with `.code`) or nested list/dict of them. `exc.get_codes()` / `exc.get_full_details()` expose
codes.

**Expected vs unexpected errors.**

| Kind | Examples | Status | Log level | Body |
|---|---|---|---|---|
| Expected client error | Validation, auth, permission, not found, conflict, throttled | 4xx | info/none | Specific, actionable |
| Expected domain rejection | "Order already shipped", "Insufficient stock" | 409/422/400 | info | Specific code + message |
| Unexpected server error | Bug, DB down, upstream timeout | 5xx | error (with traceback) | Generic message + correlation ID only |

## 3. Internal mechanics

- `APIView.handle_exception(exc)`:
  1. For `NotAuthenticated`/`AuthenticationFailed`: set `WWW-Authenticate` from the first
     authenticator, or change status to 403.
  2. Call `get_exception_handler()` → `EXCEPTION_HANDLER` (default
     `rest_framework.views.exception_handler`).
  3. If the handler returns `None`, call `raise_uncaught_exception()` → Django handles it (500,
     `handler500`, error logging, debug page if `DEBUG`).
- Default `exception_handler(exc, context)`:
  - Converts `Http404` → `NotFound`, Django `PermissionDenied` → DRF `PermissionDenied`.
  - For `APIException`: body `{"detail": ...}` for non-dict/list details, or the dict/list
    itself (validation errors); sets `WWW-Authenticate`/`Retry-After` headers; calls
    `set_rollback()` (marks the atomic block for rollback when `ATOMIC_REQUESTS` is on).
  - Returns `None` for everything else — including Django's `ValidationError` raised outside
    serializer validation and `IntegrityError`.
- `context` contains `view`, `args`, `kwargs`, `request`.
- For non-DRF 500s, `rest_framework.exceptions.server_error` and `bad_request` are generic JSON
  views you can assign to `handler500`/`handler400` in the root URLconf.

## 4. Engineering decisions and trade-offs

**Error body format.** Choose one and apply it everywhere via the exception handler:

- **DRF default** — `{"detail": "..."}` or field dict. Zero effort; inconsistent shapes between
  validation and other errors.
- **RFC 9457 Problem Details** (`application/problem+json`) — standard fields `type`, `title`,
  `status`, `detail`, `instance`, plus extensions (`errors`, `code`, `request_id`). Recommended
  for public APIs.
- **Custom envelope** — `{"error": {"code", "message", "fields", "request_id"}}`. Fine if
  documented and consistent.

**Domain exceptions.** Services raise domain exceptions (`OrderAlreadyShipped`) that know nothing
of HTTP. The exception handler maps them to statuses. This keeps services reusable from tasks and
commands ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).

**409 vs 422 vs 400.** 400 for malformed/invalid input; 409 for conflicts with current state
(duplicate, already processed, version mismatch); 422 is acceptable for semantically invalid but
well-formed input — pick a convention and document it.

## 5. Django/DRF implementation patterns

**Custom API exceptions:**

```python
class Conflict(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"
```

**Problem-details handler with domain mapping and correlation ID:**

```python
# config/api/errors.py
import logging
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import exceptions, status
from rest_framework.serializers import as_serializer_error   # undocumented helper — VERIFY
from rest_framework.views import exception_handler as drf_exception_handler
from orders.exceptions import DomainError        # base class: has .code and .http_status

logger = logging.getLogger("api.errors")

def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        exc = _domain_to_api(exc)
    elif isinstance(exc, DjangoValidationError):          # e.g. model full_clean() in a service
        exc = exceptions.ValidationError(detail=as_serializer_error(exc))

    response = drf_exception_handler(exc, context)
    request = context["request"]
    request_id = getattr(request, "request_id", None)    # set by request-ID middleware

    if response is None:                                  # unexpected → 500 via Django
        logger.exception("Unhandled API error", extra={"request_id": request_id})
        return None

    fields = response.data if isinstance(exc, exceptions.ValidationError) else None
    response.data = {
        "type": f"https://api.example.com/problems/{_code(exc)}",
        "title": response.status_text,
        "status": response.status_code,
        "detail": _detail(exc),
        "code": _code(exc),
        "errors": fields,
        "request_id": request_id,
    }
    response["Content-Type"] = "application/problem+json"
    return response

def _domain_to_api(exc):
    api_exc = exceptions.APIException(detail=str(exc), code=exc.code)
    api_exc.status_code = exc.http_status                # e.g. 409
    return api_exc

def _code(exc):
    codes = exc.get_codes()
    return codes if isinstance(codes, str) else "invalid"

def _detail(exc):
    return "Invalid input." if isinstance(exc, exceptions.ValidationError) else str(exc.detail)
```

`as_serializer_error` is a module-level helper in `rest_framework.serializers` that is not part of
the documented API — **VERIFY** it exists in your release, or convert `exc.message_dict` yourself.

Setting `Content-Type` directly can be overridden by the renderer's media type; if clients
require `application/problem+json`, add a renderer with that media type or set
`response.content_type` — **VERIFY** with an API test.

**Example responses:**

```json
HTTP/1.1 400 Bad Request
{"type": "https://api.example.com/problems/invalid", "title": "Bad Request", "status": 400,
 "detail": "Invalid input.", "code": "invalid",
 "errors": {"quantity": ["Ensure this value is less than or equal to 1000."]},
 "request_id": "01JC8Z..."}

HTTP/1.1 409 Conflict
{"type": "https://api.example.com/problems/order_already_shipped", "title": "Conflict",
 "status": 409, "detail": "Order R-1042 has already shipped.", "code": "order_already_shipped",
 "errors": null, "request_id": "01JC8Z..."}
```

**JSON for non-DRF errors:**

```python
# config/urls.py
handler500 = "rest_framework.exceptions.server_error"
handler400 = "rest_framework.exceptions.bad_request"
```

**Raising from views/services:** `raise NotFound("Order not found.")`, `raise
PermissionDenied(code="order_locked")`, or raise domain exceptions from services.

## 6. Production use cases

- Public API with RFC 9457 bodies, stable `code` values documented in the schema.
- Mobile client mapping `errors` field keys to form inputs.
- Support workflow: user reports `request_id`; logs and traces are searched by it.
- Concurrency conflicts (optimistic locking) surfaced as 409 with a retry hint.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| One handler formats every API error | A buggy handler breaks every error path — test it thoroughly |
| Domain exceptions stay HTTP-agnostic | Mapping table must be maintained |
| Codes allow client branching independent of wording | Changing the format is a breaking change |

## 8. Security and performance considerations

- Never return tracebacks, SQL, exception class names or internal IDs in 5xx bodies; `DEBUG=False`
  in production.
- Avoid user-enumeration via different messages ("unknown email" vs "wrong password").
- 404 vs 403 policy is an information-disclosure decision
  ([permissions-and-access-control.md](permissions-and-access-control.md)).
- Log 5xx with traceback and request ID; log 4xx at low level (high-volume 4xx can be an attack
  signal — metric, not log spam).
- Don't log request bodies containing secrets/PII ([django-core logging-exceptions](../../django-core/references/logging-exceptions.md)).

## 9. Common mistakes and anti-patterns

- `try/except` in every view building ad-hoc dicts.
- Returning 200 with `{"success": false}`.
- Converting all exceptions to 400 (hides server bugs, breaks retries and alerting).
- Catching `Exception` in the handler and returning a 500 body yourself without logging.
- `IntegrityError` reaching clients as 500 for predictable duplicates — map it in the service.
- Error messages as the only machine-readable signal.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| HTML 500 page for an API request | Handler returned `None` and no JSON `handler500` |
| Custom handler not called | `EXCEPTION_HANDLER` path wrong; exception raised in middleware (outside DRF) |
| Validation errors lose structure | Handler flattens `response.data` |
| `Retry-After`/`WWW-Authenticate` missing | Handler builds a new `Response` instead of modifying the default one |
| Partial writes after an error | No `atomic` in the service; `set_rollback` only affects `ATOMIC_REQUESTS` |

Tests: one per status family asserting the full body shape and codes
([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [fields-and-validation.md](fields-and-validation.md) — error structure from validation
- [request-response-lifecycle.md](request-response-lifecycle.md) — `handle_exception`
- [api-versioning-and-documentation.md](api-versioning-and-documentation.md) — documenting errors
- [django-core logging-exceptions](../../django-core/references/logging-exceptions.md)
- [django-architecture service-layer](../../django-architecture/references/service-layer.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md)

## 12. Official documentation and external references

- Exceptions: <https://www.django-rest-framework.org/api-guide/exceptions/>
- Status codes: <https://www.django-rest-framework.org/api-guide/status-codes/>
- RFC 9457 Problem Details: <https://www.rfc-editor.org/rfc/rfc9457>
- Django error views: <https://docs.djangoproject.com/en/5.2/ref/views/#error-views>
- Third-party (not part of DRF): drf-standardized-errors — <https://drf-standardized-errors.readthedocs.io/>
