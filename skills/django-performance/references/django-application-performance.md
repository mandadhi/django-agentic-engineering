# Django Application Performance

**Scope:** Where time goes inside a Django process between "worker picks up the request" and "bytes
handed back to the server": middleware, URL resolution, views, templates, context processors,
non-DRF serialisation, startup and per-request computation. Lifecycle mechanics are owned by
django-core; this reference covers their *cost* and how to reduce it.

## 1. Purpose and scope

Use when profiling shows significant **non-database Python time** in requests, when a page is slow
despite few queries, when worker boot is slow, or when reviewing middleware, context processors or
template-heavy views.

Mechanics (what each lifecycle stage does) → [django-core request-response-lifecycle](../../django-core/references/request-response-lifecycle.md)
and [django-core middleware](../../django-core/references/middleware.md). ORM cost →
[orm-performance-optimization.md](orm-performance-optimization.md). DRF serialisers →
[api-performance.md](api-performance.md). Async/sync adaptation cost →
[async-django-performance.md](async-django-performance.md).

## 2. Core concepts

A request's service time decomposes into cost centres:

| Stage | Typical cost (illustrative, warm process) | Becomes significant when… |
|---|---|---|
| WSGI/ASGI handler + request object | < 0.2 ms | Never on its own |
| Middleware stack (each layer, both directions) | 0.01–0.5 ms per layer | A layer does I/O (DB, cache, HTTP) or heavy work per request |
| URL resolution | 0.05–0.5 ms | Thousands of patterns, deep `include()` nesting, regex-heavy or i18n patterns |
| View logic | Varies | Python loops over large data, repeated computation |
| DB/cache/HTTP I/O | 1–1 000+ ms | Almost always the largest — see other references |
| Template rendering | 1–200 ms | Large loops, many `{% include %}`, heavy filters, lazy queries in templates |
| Context processors | 0.01–50 ms each | A processor queries the DB or calls services on every render |
| Response serialisation | 0.5–100 ms | Large JSON payloads with the stdlib encoder |
| Response middleware (GZip etc.) | 1–20 ms for large bodies | Compressing large responses in Python |

The goal is not to shave microseconds from cheap stages; it is to find the stage that **scales with
data or traffic** and stop it doing unnecessary work.

## 3. Internal mechanics

### Middleware

- The middleware chain is built **once per process** at handler load (`BaseHandler.load_middleware`);
  each request then traverses nested callables. Per-layer overhead is a few function calls — cheap
  unless the layer does work.
- Middleware that reads `request.user` triggers the lazy user lookup: session load (cache/DB read)
  plus a `User` query. `AuthenticationMiddleware` sets `request.user` lazily, so placing a custom
  middleware that touches `request.user` on every request (including static/health endpoints)
  forces that cost on all of them.
- `SessionMiddleware` loads the session lazily but **saves it on response** when modified, or on
  every request when `SESSION_SAVE_EVERY_REQUEST = True` — a DB write per request with the DB
  session backend.
- Under ASGI, a sync-only middleware forces a thread hop via `sync_to_async` for every request; under
  WSGI an async-only middleware forces `async_to_sync`. See
  [async-django-performance.md](async-django-performance.md).
- `GZipMiddleware` compresses in-process (CPU on the worker). Usually better done at the reverse
  proxy/CDN ([http-and-static-delivery-performance.md](http-and-static-delivery-performance.md)).

### URL resolution

`URLResolver` checks patterns in order, recursing into `include()`s; results of populating
reverse lookups are cached per resolver. `path()` converters compile to regexes. For typical apps
(hundreds of patterns) resolution is sub-millisecond. Put high-traffic routes early only if
measurement shows resolution matters (rare); never restructure URLs for speed without a profile.

### Template rendering

- Templates are compiled to node trees. The **cached template loader** keeps compiled templates in
  memory per process. Since Django 4.1 it is enabled by default whenever `loaders` is not set
  explicitly — including in development, where the autoreloader resets it — whereas before 4.1 it
  was enabled only when `DEBUG = False`. `VERIFY` the exact condition against the 4.1 release notes
  for projects that set `OPTIONS["loaders"]` manually: an explicit list without
  `django.template.loaders.cached.Loader` disables caching.
- Rendering cost scales with node count × loop iterations. `{% include %}` inside a loop re-resolves
  and renders the sub-template every iteration; custom tags/filters with heavy Python run per call.
- Variable resolution tries dict lookup → attribute → list index, and **calls callables**. A model
  method or related manager referenced in a template executes (and may query) on every access.
- Jinja2 (supported backend) renders faster for loop-heavy templates — `VERIFY` gains per workload;
  migrating is rarely justified unless templates are the measured bottleneck.

### Context processors

Every `render()` with a request (i.e. `RequestContext`) runs **all** configured context processors,
whether or not the template uses their output. Built-ins are mostly lazy (`auth` wraps permissions
in `PermWrapper`, which queries only when `perms` is accessed). A custom processor that executes
queries (e.g. "unread notifications count") adds that cost to every page, including error pages.

### Serialisation (non-DRF)

`JsonResponse` uses `json.dumps` with `DjangoJSONEncoder` — pure-Python `default()` hooks for
dates/decimals/UUIDs. For multi-megabyte payloads encoding can be tens of ms.
[third-party: orjson] encodes natively and returns bytes; pass them to `HttpResponse` with
`content_type="application/json"`.

### Startup and worker initialisation

Worker boot imports settings, all `INSTALLED_APPS`, models, and runs every `AppConfig.ready()`.
URLconf, templates and some caches populate lazily on the **first request**, which is why the first
request to a fresh worker is slower ("cold worker"). With Gunicorn `--preload`, the app is imported
in the master before forking, so workers share read-only memory pages (copy-on-write) and boot
faster — but DB connections or threads opened during import are shared across forks and break.

## 4. Engineering decisions and trade-offs

| Decision | Do it when… | It makes things worse when… |
|---|---|---|
| Remove/short-circuit a middleware | Profiling shows it on every request with I/O | It provides security headers/CSRF — never remove for speed |
| Move a context processor's data into the specific views | Only some pages use it | Many templates need it — use a lazy object instead |
| Cache rendered fragments | Fragment is expensive and shared | Fragment is per-user and cheap — cache overhead and key cardinality exceed savings |
| Switch to orjson | JSON encoding > ~10 % of request time on large payloads | Payloads are small; adds a dependency and custom-type handling |
| Gunicorn `--preload` | Large import time, memory pressure across many workers | Code opens connections/threads at import; you need per-worker reload |
| Jinja2 | Template rendering dominates after removing queries | Team relies on DTL ecosystem/tags; cost of migration > gain |
| Precompute in `ready()` | Small, static, process-wide data | It hits the DB (runs at import, before migrations, in every management command) |

## 5. Django implementation patterns

### Lazy, scoped middleware work

```python
# Bottleneck: middleware loading tenant + feature flags (2 cache reads + 1 query) on every request,
# including /healthz and static. Measure: per-middleware span or query count on /healthz.
from django.utils.functional import SimpleLazyObject

SKIP_PREFIXES = ("/healthz", "/static/", "/metrics")

class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SKIP_PREFIXES):
            return self.get_response(request)
        # Lazy: resolved only if a view/template actually reads request.tenant
        request.tenant = SimpleLazyObject(lambda: resolve_tenant(request.get_host()))
        return self.get_response(request)
```

Validate: query count on `/healthz` drops to 0; p50 of endpoints that never use the tenant drops by
the measured lookup cost. Trade-off: laziness moves the cost to first access, which can hide it in
templates — keep the span around `resolve_tenant` so it remains visible in traces.

### Lazy context processor

```python
from django.utils.functional import SimpleLazyObject

def notifications(request):
    def _count():
        if not request.user.is_authenticated:
            return 0
        return Notification.objects.filter(user=request.user, read=False).count()
    return {"unread_count": SimpleLazyObject(_count)}
```

Templates that never render `{{ unread_count }}` no longer pay for the query. Note that a
`SimpleLazyObject` wrapping an `int` is evaluated on first use in the template; for heavy values
consider caching per user with a short TTL ([caching-engineering.md](caching-engineering.md)).

### Request-scoped memoisation

```python
from functools import cached_property

class OrderPresenter:
    def __init__(self, order):
        self.order = order

    @cached_property
    def totals(self):               # computed once per instance, i.e. once per request use
        return compute_totals(self.order)
```

```python
# Per-request cache without globals: attach to the request object
def get_permissions(request):
    if not hasattr(request, "_perm_cache"):
        request._perm_cache = load_permissions(request.user)
    return request._perm_cache
```

Avoid `functools.lru_cache`/`cache` on functions taking request-dependent arguments: it is
process-global, survives across requests (stale/leaked data between users), grows memory, and holds
references to model instances. Use it only for pure functions of small immutable inputs.

### Avoiding queries in templates

```django
{# Bad: order.customer and order.lines.count query per row #}
{% for order in orders %}{{ order.customer.name }} ({{ order.lines.count }}){% endfor %}
```

```python
# Good: do the work in the selector, pass plain data
orders = (Order.objects.select_related("customer")
          .annotate(line_count=Count("lines")).only("id", "customer__name"))
```

Detect with Debug Toolbar's template/SQL panels or [third-party: django-zen-queries]
(`queries_disabled()` around rendering) — details in
[orm-performance-optimization.md](orm-performance-optimization.md).

### Fast JSON for large non-DRF responses

```python
import orjson  # [third-party: orjson]
from django.http import HttpResponse

def export_points(request):
    rows = list(Point.objects.filter(series_id=request.GET["s"]).values_list("ts", "value"))
    body = orjson.dumps(rows)                     # bytes; datetimes serialised natively
    return HttpResponse(body, content_type="application/json")
```

Measure first: wrap `json.dumps` in a timer or read the profile. For truly large exports, stream
instead ([file-and-data-processing-performance.md](file-and-data-processing-performance.md)).

### Keeping `AppConfig.ready()` cheap

```python
class BillingConfig(AppConfig):
    name = "billing"

    def ready(self):
        from . import signals  # noqa: F401  — wiring only
        # No queries, no network, no large file loads here: runs in every worker AND every
        # manage.py command, and before migrations on a fresh database.
```

### Warming workers

Warm cold paths (URL resolver, templates) with a readiness probe that renders a representative,
cheap page, or by issuing a request after boot. Don't block startup on cache warming that depends
on the DB being healthy.

## 6. Production use cases

- **Dashboard p95 = 900 ms with 8 queries totalling 40 ms.** pyinstrument shows 600 ms in template
  rendering: an `{% include "row.html" %}` inside a 2 000-row loop plus a custom filter formatting
  currency via `babel` per cell. Fix: paginate to 100 rows, inline the row markup, precompute
  formatted values in the view. Result (illustrative): render 600 → 45 ms.
- **Every page does 2 extra queries.** A context processor loads the site menu from the DB. Fix:
  cache the menu (shared key, invalidate on admin save) — not per-user.
- **Workers take 25 s to boot, failing health checks during deploys.** Import profiling
  (`python -X importtime manage.py check`) shows a ML model loaded at import in an app module. Fix:
  lazy-load on first use in the dedicated worker type, or move the feature to a separate service.
- **Session table write-hot.** `SESSION_SAVE_EVERY_REQUEST=True` with the DB backend: one UPDATE per
  request. Fix: disable it, or use `cached_db`/cache sessions (with durability trade-offs —
  [django-security-engineering](../../django-security-engineering/SKILL.md) for session safety).

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Lazy objects | Cost only when used | Hides cost location; surprising evaluation inside templates |
| Request-scoped caching | No staleness across requests, no shared-state bugs | Only helps repeated work within a request |
| Fragment caching | Large render savings | Invalidation and key design ([caching-engineering.md](caching-engineering.md)) |
| Preload app | Faster boot, lower memory | Fork-safety hazards; reload requires full restart |
| Native JSON encoder | 3–10× faster encoding (illustrative, VERIFY per payload) | Dependency; different handling of Decimal/custom types |

## 8. Security and performance considerations

- Skipping middleware by path prefix must never skip security middleware (CSRF, security headers,
  auth) for paths that serve user content.
- Per-request caches attached to `request` are safe; module-level caches keyed by anything
  user-related can leak data across users under thread-based workers.
- Template fragment caches must vary on every permission-relevant dimension.
- `python -X importtime` and profiler output can reveal internal module structure — keep it out of
  public artifacts.

## 9. Common mistakes and anti-patterns

- Middleware that queries the DB or cache for every request, including health checks and static.
- Context processors that compute data most templates do not use.
- Calling model methods or related managers from templates inside loops.
- Explicit `TEMPLATES[...]["OPTIONS"]["loaders"]` without the cached loader in production.
- `lru_cache` on functions receiving `request`, `user` or model instances.
- Heavy work (model loading, remote config fetch, DB queries) in `AppConfig.ready()` or module import.
- `GZipMiddleware` on already-compressed or streaming responses, or when the proxy already compresses.
- Building huge Python lists/dicts to render, instead of paginating or streaming.
- Optimising URL resolution or middleware call overhead before checking DB time.

## 10. Troubleshooting and validation

| Symptom | Measurement | Likely cause |
|---|---|---|
| High Python time, few queries | pyinstrument/cProfile on the view | Template loops, serialisation, Python aggregation |
| Every endpoint slower by a constant | Per-middleware spans; compare `/healthz` latency | A middleware or context processor doing I/O |
| First request after deploy slow | Latency by worker age | Cold worker: lazy init, imports, template compile |
| Slow boot | `python -X importtime manage.py check 2> imports.log` | Heavy imports / `ready()` work |
| Template time grows with rows | Debug Toolbar templates panel; profile | Per-row includes, callables, missing pagination |

Validation:

- [ ] Profile before and after; the targeted stage's share of wall time decreased.
- [ ] `/healthz` and static paths execute zero queries.
- [ ] Production `TEMPLATES` config uses the cached loader (inspect settings or `engines["django"].engine.loaders`).
- [ ] Context processors and middleware are free of unconditional I/O, or that I/O is cached.
- [ ] Query counts of affected views asserted in tests ([django-testing](../../django-testing/SKILL.md)).

## 11. Related references and skills

- [profiling-and-observability.md](profiling-and-observability.md) — profiling a single request.
- [orm-performance-optimization.md](orm-performance-optimization.md) — queries from templates and views.
- [caching-engineering.md](caching-engineering.md) — fragment and per-view caching.
- [api-performance.md](api-performance.md) — DRF serialisation cost.
- [async-django-performance.md](async-django-performance.md) — sync/async middleware adaptation.
- [memory-and-resource-management.md](memory-and-resource-management.md) — preload and worker memory.
- [django-core](../../django-core/SKILL.md) — [request lifecycle](../../django-core/references/request-response-lifecycle.md),
  [middleware](../../django-core/references/middleware.md), [caching](../../django-core/references/caching.md).
- [django-frontend](../../django-frontend/SKILL.md) — template organisation.

## 12. Official documentation and external references

- Django — Performance and optimization: https://docs.djangoproject.com/en/5.2/topics/performance/
- Django — Middleware: https://docs.djangoproject.com/en/5.2/topics/http/middleware/
- Django — Templates API, loaders and caching: https://docs.djangoproject.com/en/5.2/ref/templates/api/#template-loaders
- Django 4.1 release notes (cached template loader in development): https://docs.djangoproject.com/en/5.2/releases/4.1/
- Django — Context processors: https://docs.djangoproject.com/en/5.2/ref/templates/api/#built-in-template-context-processors
- Django — Applications and `ready()`: https://docs.djangoproject.com/en/5.2/ref/applications/
- Django — URL dispatcher: https://docs.djangoproject.com/en/5.2/topics/http/urls/
- Python — `-X importtime`: https://docs.python.org/3/using/cmdline.html#cmdoption-X
- Python — `functools.cached_property`: https://docs.python.org/3/library/functools.html#functools.cached_property
- Gunicorn — `preload_app`: https://docs.gunicorn.org/en/stable/settings.html#preload-app
- orjson [third-party]: https://github.com/ijl/orjson
