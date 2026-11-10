# Middleware

> Scope: middleware architecture, ordering, the hook API, writing sync/async-capable middleware, state, exception handling, testing. Not covered here: the full handler flow ([request-response-lifecycle.md](./request-response-lifecycle.md)), session/auth semantics ([authentication-sessions.md](./authentication-sessions.md)), cache middleware ([caching.md](./caching.md)), security header policy ([django-security-engineering](../../django-security-engineering/SKILL.md)), async internals ([asynchronous-django.md](./asynchronous-django.md)).

## Purpose and overview

Middleware is the place for **cross-cutting, per-request concerns that apply to (almost) every URL**: security headers, sessions, authentication, request IDs, locale, tenant selection. It is the wrong place for anything specific to a handful of endpoints — that belongs in a decorator, view mixin or service. Check the installed version (`python -m django --version`); this file assumes Django 5.2 LTS.

## How it works internally

- `settings.MIDDLEWARE` is a list of dotted paths. At process start, `BaseHandler.load_middleware()` instantiates each class **once**, passing the next inner callable as `get_response`. The result is an onion: the first entry is the outermost layer.
- Request phase runs **top → bottom**; response phase runs **bottom → top**.
- Each layer's `get_response` is wrapped by `convert_exception_to_response`, so exceptions from inner layers arrive as **responses**, not exceptions.
- Optional hooks collected at load time:
  - `process_view(request, view_func, view_args, view_kwargs)` — after URL resolution, before the view, in **list order**. Return `None` to continue or a response to short-circuit the view.
  - `process_exception(request, exception)` — only for exceptions raised by the view (or its template rendering), in **reverse order**. First non-`None` response wins.
  - `process_template_response(request, response)` — for responses with `render()`, in **reverse order**; must return a response.
- A layer that returns without calling `get_response` short-circuits everything inside it, but **outer** layers still run their response phase.
- Raising `django.core.exceptions.MiddlewareNotUsed` in `__init__` removes the layer (logged at DEBUG when `DEBUG=True`).
- Sync/async: each middleware declares `sync_capable` (default `True`) and `async_capable` (default `False`). Django adapts each layer to the mode of the handler; mismatches are bridged with `sync_to_async`/`async_to_sync`, which costs a thread hop per mismatched layer.

## Core concepts and APIs

Recommended order for a typical server-rendered app (absorbs the project's previous guidance):

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",          # first: HTTPS redirect, HSTS, headers
    "whitenoise.middleware.WhiteNoiseMiddleware",             # only if WhiteNoise serves static files
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",              # APPEND_SLASH, DISALLOWED_USER_AGENTS
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "config.middleware.RequestIdMiddleware",                  # after auth if it logs the user
]
```

Ordering rules and why:

| Rule | Why |
|---|---|
| `SecurityMiddleware` first | redirects HTTP→HTTPS before any other work; adds headers to every response |
| `SessionMiddleware` before auth and messages | they read `request.session` |
| `AuthenticationMiddleware` after sessions | lazily resolves `request.user` from the session |
| `CsrfViewMiddleware` before anything assuming CSRF protection | it uses `process_view` to check the token |
| `LocaleMiddleware` after sessions, before `CommonMiddleware` | needs session; Common's slash redirect must use activated language |
| `GZipMiddleware` (if used) high, before anything reading/altering body | compresses the final body; see BREACH notes in django-security-engineering |
| `UpdateCacheMiddleware` first / `FetchFromCacheMiddleware` last | see [caching.md](./caching.md) |
| Custom middleware needing `request.user` | below `AuthenticationMiddleware` |

`LoginRequiredMiddleware` (added in **Django 5.1**) makes every view require login unless marked `@login_not_required`; place it after `AuthenticationMiddleware`. Django **6.0** adds built-in Content Security Policy middleware — check the 6.0 release notes before relying on it; on 5.2 use a header in custom middleware or a package.

Decorators in `django.utils.decorators`: `sync_only_middleware`, `async_only_middleware`, `sync_and_async_middleware` for function-style middleware.

## Implementation examples

Request ID middleware (class style, sync) — pairs with `request_id_var` in `config/logging.py`:

```python
# config/middleware.py
import re
import uuid
from config.logging import request_id_var

_VALID_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")

class RequestIdMiddleware:
    header = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response       # once per process: no per-request state here

    def __call__(self, request):
        inbound = request.META.get(self.header, "")
        rid = inbound if _VALID_ID.match(inbound) else uuid.uuid4().hex   # never trust raw input
        request.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)        # no leakage between requests on the same thread
        response["X-Request-ID"] = rid
        return response
```

Sync **and** async capable version — avoids thread hops under ASGI:

```python
from asgiref.sync import iscoroutinefunction, markcoroutinefunction

class RequestIdMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)        # tell Django this instance is awaitable

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        rid, token = self._start(request)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response["X-Request-ID"] = rid
        return response

    async def __acall__(self, request):
        rid, token = self._start(request)
        try:
            response = await self.get_response(request)
        finally:
            request_id_var.reset(token)
        response["X-Request-ID"] = rid
        return response

    def _start(self, request):
        inbound = request.META.get(self.header, "")
        rid = inbound if _VALID_ID.match(inbound) else uuid.uuid4().hex
        request.request_id = rid
        return rid, request_id_var.set(rid)
```

`markcoroutinefunction` requires asgiref ≥ 3.6 (shipped with supported Django versions).

Mapping a domain exception globally with `process_exception`:

```python
from django.http import JsonResponse
from apps.billing.exceptions import PaymentProviderUnavailable

class DomainErrorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, PaymentProviderUnavailable):
            return JsonResponse({"error": "payment_unavailable"}, status=503)
        return None                            # let Django's normal 500 handling run
```

Bad vs good — catching view errors:

```python
# BAD: never triggers; inner layer already converted the exception to a 500 response
def __call__(self, request):
    try:
        return self.get_response(request)
    except PaymentProviderUnavailable:
        return JsonResponse(..., status=503)

# GOOD: use process_exception (above), or handle it in the view/service boundary
```

Conditional disabling at startup:

```python
from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed

class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        if not settings.MAINTENANCE_MODE:
            raise MiddlewareNotUsed
        self.get_response = get_response
    ...
```

Note this is evaluated once per process: toggling requires a restart. For runtime toggles, read a cached flag inside `__call__`.

## Production patterns

- **O(1), no I/O by default.** If a middleware must hit the DB or cache, do it lazily (`SimpleLazyObject`, like `request.user`) so requests that never use it pay nothing.
- **Exempt cheap paths** (health checks, static) early in `__call__` when the middleware is expensive.
- **State on `request`, not on `self`.** The instance is shared across all threads/tasks; instance attributes mutated per request are race conditions.
- **Use `ContextVar` for log context** and always `reset()` in `finally`.
- **Make custom middleware async-capable** when the project runs under ASGI; one sync-only layer in the middle forces the rest of the chain inward to run in a thread.
- **Keep third-party middleware audited**: each one runs on every request and sees every request body/header.

## Decision guidance

| Concern | Middleware | Decorator / mixin | View/service |
|---|---|---|---|
| Applies to nearly all URLs | ✅ | ❌ repetitive | ❌ |
| Applies to a few endpoints | ❌ hidden, global cost | ✅ explicit at the view | ✅ |
| Needs resolved view/kwargs | `process_view` | ✅ naturally has them | ✅ |
| Business rule | ❌ | ❌ | ✅ |
| Response header for everything | ✅ | ❌ | ❌ |
| Auth policy "default deny" | `LoginRequiredMiddleware` (≥ 5.1) | `@login_required` per view | — |

Prefer the decorator when the concern is about *the view*; prefer middleware when it is about *the request/transport*.

## Security considerations

- Validate any inbound header you trust (request IDs, `X-Forwarded-*`); only accept them from a trusted proxy. Use `SECURE_PROXY_SSL_HEADER` / `USE_X_FORWARDED_HOST` rather than parsing headers yourself (see [production-configuration.md](./production-configuration.md)).
- Do not log request bodies, cookies or `Authorization` headers from middleware.
- Moving security middleware below a middleware that short-circuits (e.g. a custom cache or maintenance page) means those responses skip security headers.
- A middleware that returns a response before `CsrfViewMiddleware.process_view` runs bypasses CSRF checks for that path — make sure it is read-only.

## Performance considerations

- Each sync↔async boundary costs a thread switch; measure with and without when running ASGI.
- Avoid `request.body` access in middleware: it forces reading the full body for every request and breaks streaming uploads (`RawPostDataException` if `request.POST`/`FILES` was consumed first).
- `GZipMiddleware` costs CPU per response; usually the reverse proxy/CDN should compress.
- Profiling guidance: [django-performance](../../django-performance/SKILL.md).

## Testing

```python
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from config.middleware import RequestIdMiddleware

class RequestIdMiddlewareTests(SimpleTestCase):
    def test_generates_id_for_invalid_inbound(self):
        mw = RequestIdMiddleware(lambda request: HttpResponse())
        request = RequestFactory().get("/", HTTP_X_REQUEST_ID="<script>")
        response = mw(request)
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_preserves_valid_inbound(self):
        mw = RequestIdMiddleware(lambda request: HttpResponse())
        response = mw(RequestFactory().get("/", HTTP_X_REQUEST_ID="abc-12345"))
        self.assertEqual(response["X-Request-ID"], "abc-12345")

    async def test_async_path(self):
        async def get_response(request):
            return HttpResponse()
        mw = RequestIdMiddleware(get_response)
        response = await mw(RequestFactory().get("/"))
        self.assertIn("X-Request-ID", response.headers)
```

- Unit-test with a fake `get_response`; integration-test ordering with `self.client` and `@override_settings(MIDDLEWARE=[...])`.
- Test `process_exception` by calling it directly with an exception instance.
- More patterns: [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- Old-style `MiddlewareMixin`/`process_request` written for new code — fine for compatibility, but plain classes are clearer.
- Per-request data stored on `self`.
- Querying the database in every request for a feature used by one page.
- `try/except` around `get_response` expecting view exceptions.
- Reading `request.user` above `AuthenticationMiddleware`.
- Forgetting `return` of the response → `None` propagates and crashes outer layers.
- Sync-only custom middleware in an ASGI deployment without measuring the cost.

## Troubleshooting

| Symptom | Check |
|---|---|
| `AttributeError: 'WSGIRequest' object has no attribute 'user'` | middleware or view runs before/without `AuthenticationMiddleware` |
| Middleware never runs | not in `MIDDLEWARE`, raised `MiddlewareNotUsed`, or an earlier layer short-circuited |
| `process_view` not called | the path did not resolve (404) or an earlier `process_view` returned a response |
| `process_exception` not called | exception came from middleware, not the view; or another hook returned first |
| ASGI slower than WSGI | sync-only middleware layers; check `async_capable` on each |
| `'coroutine' object has no attribute ...` | async `get_response` called without `await` / missing `markcoroutinefunction` |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/http/middleware/
- https://docs.djangoproject.com/en/stable/ref/middleware/
- https://docs.djangoproject.com/en/stable/ref/middleware/#middleware-ordering
- https://docs.djangoproject.com/en/stable/topics/http/middleware/#asynchronous-support
- https://docs.djangoproject.com/en/stable/topics/async/
