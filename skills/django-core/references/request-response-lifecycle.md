# Request–response lifecycle

> Scope: the end-to-end path of one HTTP request through Django: handler internals, middleware chain construction, URL resolution, view execution, exception conversion, response delivery and debugging. Not covered here: writing middleware ([middleware.md](./middleware.md)), routing and views ([urls-views.md](./urls-views.md)), WSGI vs ASGI servers ([asgi-wsgi.md](./asgi-wsgi.md)), async internals ([asynchronous-django.md](./asynchronous-django.md)), logging config ([logging-exceptions.md](./logging-exceptions.md)), transactions ([django-orm](../../django-orm/SKILL.md)).

## Purpose and overview

Most "mysterious" Django bugs — a header that never appears, an exception that is never logged, a 500 that middleware does not see, a session that is not saved — are lifecycle bugs. Knowing which component runs when, and what it receives, turns them into a five-minute diagnosis.

The flow, simplified:

```text
server (gunicorn / uvicorn)
  └─ WSGIHandler / ASGIHandler          set script prefix, send request_started, build HttpRequest
       └─ BaseHandler.get_response      set urlconf, run the middleware chain
            └─ middleware 1 → 2 → … → n (request phase, top → bottom)
                 └─ _get_response       resolve URL, process_view hooks, call view,
                                         process_exception hooks, process_template_response, render
            ← middleware n → … → 1      (response phase, bottom → top)
  ← response returned to the server, streamed, then closed → request_finished
```

## How it works internally

All in `django/core/handlers/` (verify against the installed version; the structure has been stable since Django 2.0/3.1).

1. **Startup.** The entry point (`get_wsgi_application()` / `get_asgi_application()`) calls `django.setup()` (populates the app registry, configures logging) and instantiates the handler. The handler calls `BaseHandler.load_middleware()` **once per process**.
2. **Chain construction.** `load_middleware()` iterates `settings.MIDDLEWARE` **in reverse**. It starts with `_get_response` (or `_get_response_async`) as the innermost callable, wraps it with `convert_exception_to_response`, then instantiates each middleware with the current handler as `get_response` and wraps the result again. It also collects `process_view`, `process_template_response` and `process_exception` hooks. A middleware raising `MiddlewareNotUsed` in `__init__` is skipped. Sync/async adaptation between layers happens here (`adapt_method_mode`).
3. **Request arrival.** `WSGIHandler.__call__` sets the script prefix, sends `request_started`, builds a `WSGIRequest` from the environ and calls `get_response()`. `ASGIHandler` first reads the whole body into a spooled temp file, then builds an `ASGIRequest`.
4. **`get_response()`** sets the active URLconf (`ROOT_URLCONF`) and calls the outermost middleware. After the chain returns, it logs any response with status ≥ 400 via `log_response()` to the `django.request` logger (unless already logged).
5. **`_get_response()`** — the innermost layer:
   - `resolve_request()` uses `request.urlconf` if a middleware set it, otherwise the default, and stores `request.resolver_match`. No match → `Resolver404` (a subclass of `Http404`).
   - Runs each `process_view(request, view_func, args, kwargs)` in middleware order; the first non-`None` return short-circuits the view.
   - Wraps the view for `ATOMIC_REQUESTS` if enabled, then calls it.
   - If the view raised, calls `process_exception` hooks **in reverse order**; the first returned response wins; if none returns, the exception is re-raised.
   - Fails loudly if the view returned `None` ("didn't return an HttpResponse object").
   - If the response has `render()` (a `TemplateResponse`), runs `process_template_response` hooks (reverse order) and then renders.
6. **Exception conversion.** `convert_exception_to_response` wraps *every* layer, so an exception raised anywhere becomes a response at the nearest boundary, via `response_for_exception()`:

| Exception | Result | Logged to |
|---|---|---|
| `Http404` | `handler404` (DEBUG: technical 404 page) | `django.request` (WARNING) |
| `PermissionDenied` | `handler403` | `django.security` / `django.request` |
| `BadRequest`, `MultiPartParserError` | `handler400` | `django.request` |
| `SuspiciousOperation` subclasses (e.g. `DisallowedHost`) | `handler400` | `django.security.<ClassName>` |
| anything else | `got_request_exception` signal, then `handler500` (DEBUG: technical 500) | `django.request` (ERROR, with traceback) |

   `DEBUG_PROPAGATE_EXCEPTIONS = True` re-raises instead of returning a 500 — useful only when an outer layer must see the raw exception.
7. **Response phase.** Each middleware's code after `get_response()` runs, innermost first.
8. **Delivery and teardown.** The server iterates the response body. When it calls `response.close()`, Django fires `request_finished`; handlers on that signal close or recycle DB connections (`close_old_connections`, honouring `CONN_MAX_AGE`) and reset per-request state. For streaming responses this happens *after* the stream ends.

Key consequence of step 6: **an outer middleware's `try/except` around `get_response()` never sees view exceptions** — they were already converted to a 500 response by the wrapper around the inner layer. Use `process_exception` or the `got_request_exception` signal instead (see [middleware.md](./middleware.md)).

## Core concepts and APIs

| API | Role |
|---|---|
| `django.core.handlers.base.BaseHandler` | builds and runs the middleware chain |
| `django.core.handlers.exception.convert_exception_to_response` | per-layer exception → response conversion |
| `request.resolver_match` | `ResolverMatch` (view_name, url_name, namespaces, route, kwargs); set after resolution |
| `request.urlconf` | per-request URLconf override (set in middleware, e.g. per-tenant) |
| `django.core.signals.request_started / request_finished / got_request_exception` | lifecycle hooks |
| `TemplateResponse` | rendering deferred until after `process_template_response` |
| `StreamingHttpResponse`, `FileResponse` | body generated during delivery, after middleware |
| `ATOMIC_REQUESTS` (per database) | wrap each view in a transaction |

Transactions: by default each query autocommits. `ATOMIC_REQUESTS` wraps **only the view**, not middleware or streaming generation — details in [django-orm](../../django-orm/SKILL.md).

## Implementation examples

Per-request URLconf (multi-tenant hosts) — resolution reads `request.urlconf`:

```python
# config/middleware.py
class TenantUrlconfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.get_host().startswith("partners."):
            request.urlconf = "config.urls_partners"
        return self.get_response(request)
```

Observing failures without middleware — `got_request_exception` fires only for unhandled (→500) exceptions:

```python
# apps/core/signals.py — connected in AppConfig.ready()
import logging
from django.core.signals import got_request_exception
from django.dispatch import receiver

logger = logging.getLogger(__name__)

@receiver(got_request_exception)
def count_unhandled(sender, request, **kwargs):
    route = getattr(getattr(request, "resolver_match", None), "route", "unresolved")
    logger.info("unhandled_exception", extra={"route": route})
```

Deferred rendering lets middleware alter context before templates render:

```python
from django.template.response import TemplateResponse

def order_list(request):
    return TemplateResponse(request, "orders/list.html", {"orders": []})
# a middleware's process_template_response can now set response.context_data["banner"] = ...
```

## Production patterns

- **Keep the request path predictable.** Everything in `MIDDLEWARE` runs for static-looking URLs, health checks and 404s too. Put cheap, universal concerns there; endpoint-specific concerns in decorators or views.
- **Use `resolver_match.route` (not the raw path) as a metrics/log label** — it has bounded cardinality (`orders/<int:pk>/`).
- **Treat `request_finished` as best-effort cleanup**; it does not fire if the client disconnects before the server closes the response in some server configurations, and for streaming it fires late. Do not rely on it for business work.
- **Streaming responses outlive the view.** The DB connection, transaction and contextvars of the view are gone or different by the time the generator runs. Materialise data first or open what you need inside the generator.
- **Never put business side effects in the response phase** of middleware: the client may already have received headers for streaming responses, and failures there produce confusing 500s.

## Decision guidance

| Need | Hook at |
|---|---|
| Run on every request before routing | middleware `__call__` before `get_response` |
| Need the resolved view / kwargs | `process_view` or `request.resolver_match` inside the view |
| Convert a domain exception to a response | `process_exception` (global) or `try/except` in the view (local) |
| Observe every unhandled error | `got_request_exception` or logging (`django.request`) |
| Modify context before render | return `TemplateResponse` + `process_template_response` |
| Cleanup after response sent | `request_finished` signal (best-effort); avoid private response internals |
| Customise 404/500 pages | `handler404` / `handler500` ([urls-views.md](./urls-views.md)) |

## Security considerations

- `ALLOWED_HOSTS` is enforced lazily by `request.get_host()` (raising `DisallowedHost`, a `SuspiciousOperation` → 400). Code that reads `request.META["HTTP_HOST"]` directly bypasses validation — always use `get_host()` / `build_absolute_uri()`.
- `DEBUG=True` turns `Http404` and 500s into technical pages that leak settings, paths and local variables. See [production-configuration.md](./production-configuration.md).
- The ASGI handler buffers the request body before the view runs; body-size limits (`DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`) and proxy limits protect memory — see [file-handling.md](./file-handling.md).
- `SuspiciousOperation` logs under `django.security.*`; route those loggers to alerting.

## Performance considerations

- Middleware cost is multiplied by every request; one DB query per request in middleware is often the largest single overhead in small apps.
- Mixing sync middleware into an ASGI stack forces `sync_to_async` thread hops per layer; see [middleware.md](./middleware.md) and [asynchronous-django.md](./asynchronous-django.md).
- URL resolution is linear over patterns at each level but cached per URLconf; deep `re_path` lists with expensive regexes show up at high RPS.
- `CONN_MAX_AGE=0` means connect/disconnect per request (handled at `request_started`/`request_finished`); persistent connections are covered in [production-configuration.md](./production-configuration.md).
- Profiling methodology belongs to [django-performance](../../django-performance/SKILL.md).

## Testing

- `django.test.Client` runs the **full** handler: middleware, URL resolution, exception conversion. Use it to assert lifecycle behaviour (status, headers added by middleware, handler404 output).
- `RequestFactory` builds a bare request and calls the view directly — **no middleware**, so set `request.user`/`request.session` yourself. Use it for unit-testing view logic.
- Client re-raises unhandled view exceptions by default (`raise_request_exception=True`, Django ≥ 3.0) so tests fail with the real traceback; pass `Client(raise_request_exception=False)` to assert the 500 response instead.
- Assert logging with `self.assertLogs("django.request", level="ERROR")`.

```python
from django.test import Client, TestCase

class LifecycleTests(TestCase):
    def test_unknown_url_returns_404_with_request_id(self):
        response = self.client.get("/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("X-Request-ID", response.headers)   # response phase still runs

    def test_crash_returns_500(self):
        client = Client(raise_request_exception=False)
        with self.assertLogs("django.request", level="ERROR"):
            response = client.get("/orders/boom/")
        self.assertEqual(response.status_code, 500)
```

Broader test infrastructure: [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- **Wrapping `get_response()` in `try/except` to catch view errors** — they never arrive as exceptions. Use `process_exception`.
- **Reading `request.user` in middleware placed above `AuthenticationMiddleware`** → `AttributeError`.
- **Returning `None` from a view branch** (forgot `return`) → `ValueError: … didn't return an HttpResponse object`.
- **Assuming `ATOMIC_REQUESTS` covers middleware writes** — it does not.
- **Doing work in a streaming generator that needs request-scoped state** that was reset after the view returned.
- **Swallowing exceptions in a view and returning 200** — breaks error logging, `got_request_exception` and monitoring.

## Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| Header added by middleware missing on errors | middleware raised itself, or response was replaced by a later layer; check order |
| 500 with no traceback in logs | `LOGGING` disabled `django.request`, or `disable_existing_loggers=True`; see [logging-exceptions.md](./logging-exceptions.md) |
| `Bad Request (400)` with no detail | `DisallowedHost` → check `ALLOWED_HOSTS` and `django.security.DisallowedHost` logs |
| Wrong view handling a URL | inspect `request.resolver_match`; run `resolve("/path/")` in `manage.py shell`; earlier pattern shadowing |
| Session/messages not saved | response phase skipped for that middleware (short-circuit above it) or exception in its response hook |
| DB "connection already closed" in streaming | connection closed at `request_finished` vs generator timing |

Debugging workflow:

1. Reproduce with `self.client` in a test; it exercises the same chain.
2. Print the effective stack: `from django.conf import settings; settings.MIDDLEWARE`.
3. `python manage.py shell -c "from django.urls import resolve; print(resolve('/orders/1/'))"`.
4. Temporarily set the `django.request` and `django.db.backends` loggers to DEBUG locally.
5. Set a breakpoint in `django/core/handlers/base.py:_get_response` to watch resolution and hooks.

## Official documentation

- https://docs.djangoproject.com/en/stable/ref/request-response/
- https://docs.djangoproject.com/en/stable/topics/http/middleware/
- https://docs.djangoproject.com/en/stable/topics/http/urls/#how-django-processes-a-request
- https://docs.djangoproject.com/en/stable/ref/signals/#module-django.core.signals
- https://docs.djangoproject.com/en/stable/ref/views/#error-views
- https://docs.djangoproject.com/en/stable/ref/template-response/
- https://docs.djangoproject.com/en/stable/topics/testing/tools/
