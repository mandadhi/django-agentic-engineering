# Caching

> Scope: Django's cache framework — `CACHES`, backends, the low-level API, per-view and
> template-fragment caching, cache middleware, HTTP cache headers, keys, invalidation and
> consistency. Not covered here: query-level optimisation ([django-orm](../../django-orm/SKILL.md)),
> profiling and load testing ([django-performance](../../django-performance/SKILL.md)), cached
> session backend details ([authentication-sessions.md](./authentication-sessions.md)), Redis
> operations ([django-integrations](../../django-integrations/SKILL.md)).

## Purpose and overview

A cache trades **freshness for latency and load**. Django gives you one API over several
backends and four granularities:

| Granularity | Tool | Cache key derived from |
|---|---|---|
| Whole site | `UpdateCacheMiddleware` + `FetchFromCacheMiddleware` | URL + `Vary` headers |
| One view | `@cache_page` | URL + `Vary` headers |
| Part of a template | `{% cache %}` | fragment name + vary-on args |
| Arbitrary values | `cache.get/set` | your explicit key |

Most real projects get the best ratio from low-level caching of expensive computations, with
per-view caching reserved for anonymous, public pages. Check the version
(`python -m django --version`): the built-in Redis backend needs Django ≥ 4.0; async cache
methods (`aget`, `aset`, …) exist from 4.0.

## How it works internally

- `django.core.cache.caches` is a per-thread (per-async-context) registry; `caches["default"]`
  and the `cache` shortcut lazily instantiate the backend from `CACHES`.
- Every key is transformed by `KEY_FUNCTION` (default `"{KEY_PREFIX}:{VERSION}:{key}"`) before
  reaching the store. `KEY_PREFIX` isolates environments sharing one server; `VERSION` lets you
  invalidate everything at once by bumping it.
- `TIMEOUT` (default 300 s) applies when `set()` is given no timeout; `None` means never expire,
  `0` means don't cache.
- Values are pickled by most backends — any picklable object works, but class changes between
  deploys can break unpickling of old entries.
- `cache_page` and the cache middleware use `django.utils.cache.learn_cache_key` /
  `get_cache_key`: the response's `Vary` header list is stored, and the final key includes the
  values of those request headers. Responses with `Cache-Control: private`, `no-cache`, or that
  set cookies are not stored by the middleware.
- Async methods on built-in backends currently delegate to the sync implementation via
  `sync_to_async`; they are API-compatible, not natively async I/O (verify for your version).

## Core concepts and APIs

```python
from django.core.cache import cache, caches

cache.get(key, default=None)
cache.set(key, value, timeout=300)
cache.add(key, value, timeout)            # only if absent → returns bool; useful for locks
cache.get_or_set(key, callable_or_value, timeout)
cache.get_many(keys) / cache.set_many(mapping, timeout)
cache.delete(key) / cache.delete_many(keys)
cache.incr(key, delta=1) / cache.decr(key)  # atomic on Redis/Memcached; not on LocMem across processes
cache.touch(key, timeout)                 # extend TTL
cache.clear()                             # wipes the WHOLE store, not just your prefix on some backends
# async (Django ≥ 4.0): await cache.aget(...), cache.aset(...), cache.aget_or_set(...)
```

View/HTTP helpers: `django.views.decorators.cache.cache_page`, `never_cache`, `cache_control`;
`django.views.decorators.vary.vary_on_headers`, `vary_on_cookie`;
`django.utils.cache.patch_cache_control`, `patch_vary_headers`.

Template: `{% load cache %}{% cache 600 order_sidebar request.user.pk %}…{% endcache %}`;
invalidate with `django.core.cache.utils.make_template_fragment_key("order_sidebar", [user_pk])`.

## Implementation examples

```python
# config/settings.py
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",   # Django ≥ 4.0, needs redis-py
        "LOCATION": os.environ.get("REDIS_URL", "redis://localhost:6379/1"),
        "KEY_PREFIX": os.environ.get("DJANGO_CACHE_PREFIX", "shop"),
        "TIMEOUT": env_int("DJANGO_CACHE_TIMEOUT", 300),
    }
}
```

Low-level caching with explicit, versioned keys and invalidation on write:

```python
# apps/orders/selectors.py
from django.core.cache import cache

def _summary_key(customer_id: int) -> str:
    return f"orders:summary:v2:{customer_id}"

def order_summary(customer_id: int) -> dict:
    return cache.get_or_set(
        _summary_key(customer_id),
        lambda: _compute_summary(customer_id),   # only runs on miss
        timeout=600,
    )

# apps/orders/services.py
from django.db import transaction

def place_order(*, customer, items) -> Order:
    with transaction.atomic():
        order = Order.objects.create(customer=customer)
        ...
        transaction.on_commit(lambda: cache.delete(_summary_key(customer.pk)))
    return order
```

Why `on_commit`: deleting before commit lets a concurrent reader repopulate the cache with the
old (pre-commit) data, which then lives for the full TTL.

Per-view caching for a public page:

```python
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

@cache_page(60 * 15)
@vary_on_headers("Accept-Language")
def catalogue(request): ...

# Class-based / URLconf form:
path("catalogue/", cache_page(900)(CatalogueView.as_view()), name="catalogue"),
```

Site-wide cache middleware — order matters because response middleware runs bottom-to-top:

```python
MIDDLEWARE = [
    "django.middleware.cache.UpdateCacheMiddleware",    # FIRST: sees final response last
    "django.middleware.security.SecurityMiddleware",
    # ... session, common, csrf, auth ...
    "django.middleware.cache.FetchFromCacheMiddleware", # LAST: short-circuits before the view
]
CACHE_MIDDLEWARE_SECONDS = 300
CACHE_MIDDLEWARE_KEY_PREFIX = "site"
```

## Production patterns

- **Shared backend** (Redis or Memcached) in any multi-process deployment. `LocMemCache` is
  per-process: each gunicorn worker has its own copy, so invalidation only reaches one worker.
- **Separate aliases** for different lifetimes/eviction policies: `default` for computed values,
  `sessions` if using the cached session backend, so a `clear()` or eviction doesn't log everyone out.
- **Stampede protection**: on a hot key expiring, many workers recompute at once. Mitigate with
  (a) `cache.add(lock_key, 1, 30)` so one worker recomputes while others serve stale or wait,
  (b) jittered TTLs, or (c) background refresh before expiry.
- **Versioned keys** (`:v2:`) when the cached shape changes, instead of `clear()` on deploy.
- **Fail-open**: a cache outage should degrade to slow, not to 500s. Wrap non-critical cache calls
  or rely on backend options (Redis client timeouts); never store the only copy of data in cache.
- **HTTP caching** (`Cache-Control`, `ETag` via `ConditionalGetMiddleware` or `@condition`) is
  often cheaper than server-side caching — the CDN/browser does the work.

## Decision guidance

| Situation | Strategy | Trade-off |
|---|---|---|
| Anonymous, identical pages | `cache_page` or CDN | Must not vary by user; stale for TTL |
| Logged-in pages with one slow block | Fragment cache keyed by user/object | More keys, finer invalidation |
| Expensive aggregate used in many places | Low-level `get_or_set` + invalidate on write | You own key design and invalidation |
| Data that must be exact (balances, stock) | Don't cache, or cache with write-through + short TTL | Consistency over speed |
| Rate limits / counters | `add` + `incr` on Redis | Needs atomic backend |
| Dev / tests | `LocMemCache` or `DummyCache` | No cross-process sharing |

Backends: Redis (`backends.redis.RedisCache`, ≥ 4.0) — rich, persistent-capable, atomic ops;
Memcached (`PyMemcacheCache` ≥ 3.2, `PyLibMCCache`) — simple, fast, volatile;
`DatabaseCache` (run `createcachetable`) — no extra infra but adds DB load; `FileBasedCache` —
single host only; `LocMemCache` — per process; `DummyCache` — no-op. The old
`MemcachedCache` (python-memcached) was removed in 4.1.

## Security considerations

- **Never page-cache responses that depend on the user** without `Vary: Cookie` (or don't cache
  them). Caching a logged-in page under a URL-only key leaks one user's data to the next.
  `SessionMiddleware` adds `Vary: Cookie` when the session is accessed — don't strip it.
- CSRF tokens embedded in cached HTML will be shared/stale; avoid caching forms or fetch the token
  separately.
- Don't put secrets or PII in cache keys (keys appear in monitoring tools); hash identifiers if needed.
- Pickle deserialisation means anyone who can write to your cache can execute code — keep the
  cache server private and authenticated.
- Memcached keys > 250 chars or with spaces raise warnings/errors; hash long user-derived keys.

## Performance considerations

- A cache hit still costs a network round trip; batch with `get_many` instead of looping `get`.
- Cache the **result** (dict/list of primitives), not querysets or model instances with lazy
  relations — those either re-query or pickle large graphs.
- Size values: Memcached's default item limit is 1 MB; huge Redis values block the server.
- Measure hit ratio before and after; a cache with a low hit ratio is pure overhead.
  Profiling methodology → [django-performance](../../django-performance/SKILL.md).

## Testing

- Use `LocMemCache` in test settings and call `cache.clear()` in `setUp` (or a base class) —
  locmem persists across tests in the same process.
- Test invalidation: populate, perform the write via the service, assert the key is gone after
  commit (`TestCase.captureOnCommitCallbacks(execute=True)`, Django ≥ 3.2).
- For per-view caching, assert the second request doesn't hit the view (mock or count queries
  with `assertNumQueries(0)`).
- `@override_settings(CACHES=...)` to switch to `DummyCache` when a test must bypass caching.

## Common mistakes and anti-patterns

- Bad: `if cache.get(k) is None:` when `None` is a legitimate cached value → permanent misses.
  Good: use a sentinel default `cache.get(k, MISSING)`.
- Invalidating inside the transaction instead of `on_commit` (stale repopulation race).
- `cache.clear()` in application code on a shared server — wipes other apps' and sessions' data.
- Cache middleware placed in the wrong order, or `FetchFromCacheMiddleware` before
  `SessionMiddleware`/`AuthenticationMiddleware` dependencies it relies on for `Vary`.
- Infinite TTLs with no invalidation path; "we'll restart Redis" is not a strategy.
- Caching to hide an N+1 query instead of fixing it ([django-orm](../../django-orm/SKILL.md)).

## Troubleshooting

| Symptom | Check |
|---|---|
| Users see each other's pages | Per-view/site cache on authenticated pages; missing `Vary: Cookie` |
| Changes not visible after save | Invalidation missing, wrong key, or done before commit |
| Works on one worker only | `LocMemCache` in a multi-process deployment |
| Nothing is cached by middleware | Response sets cookies, has `Cache-Control: private`, non-GET/HEAD, or non-200 |
| Errors after deploy on cache read | Pickled class changed; bump key version |
| `ImproperlyConfigured` / import error | Backend client library not installed (`redis`, `pymemcache`) |

Inspect keys with `cache.make_key("orders:summary:v2:42")` to see the real stored key.

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/cache/
- https://docs.djangoproject.com/en/stable/ref/settings/#caches
- https://docs.djangoproject.com/en/stable/ref/middleware/#module-django.middleware.cache
- https://docs.djangoproject.com/en/stable/ref/utils/#module-django.utils.cache
- https://docs.djangoproject.com/en/stable/topics/conditional-view-processing/
