# Caching Engineering

**Scope:** Deciding whether, where and how to cache in a Django application — patterns, key
design, invalidation, TTLs, stampede control, Redis operation and measurement. Framework API
mechanics live in [django-core caching](../../django-core/references/caching.md); HTTP caching
headers and ETags are owned by [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md).

## 1. Purpose and scope

A cache trades **freshness and consistency** for **latency and backend load**. This reference
helps an agent answer, with numbers:

- Is the bottleneck a repeated, expensive, mostly-read computation that a cache can absorb?
- Which pattern (cache-aside, read-through, write-through, write-behind) fits the consistency need?
- What is the key, TTL, invalidation trigger and failure behaviour?
- How will hit ratio, latency and correctness be validated after rollout?

Out of scope: ORM fixes that remove the need for a cache ([orm-performance-optimization.md](orm-performance-optimization.md)),
CDN/browser caching ([http-and-static-delivery-performance.md](http-and-static-delivery-performance.md)),
DRF throttle storage ([django-rest-framework throttling](../../django-rest-framework/references/throttling-and-rate-limiting.md)).

## 2. Core concepts

### When caching helps

Expected saving per request (illustrative model):

```text
saving = hit_ratio × (cost_miss_path) − (cost_cache_lookup) − (1 − hit_ratio) × (cost_cache_write)
```

- `cost_cache_lookup` for Redis on the same AZ is typically 0.3–1 ms RTT plus (de)serialisation.
  Pickling a 500 KB object graph can cost more than the query it replaces.
- A cache is worth it when the miss path is ≥ 10× the lookup cost **and** the hit ratio is high
  (≥ 80% for latency goals; lower can still pay off for offloading a saturated database).
- Caching makes things **worse** when: the hit ratio is low (high-cardinality keys, per-user
  data viewed once); values are large (network + pickle dominate); the cache is near
  `maxmemory` (evictions destroy hit ratio for everyone); or invalidation bugs serve wrong data.

### Vocabulary

| Term | Meaning |
|---|---|
| Hit ratio | hits / (hits + misses), measured per key family, not globally |
| TTL | Upper bound on staleness when invalidation fails |
| Invalidation | Removing/superseding an entry when its source changes |
| Stampede (dog-pile) | Many concurrent misses recomputing the same expensive value |
| Negative caching | Caching "not found"/empty results to avoid repeated misses |
| Warming | Populating entries before traffic needs them |
| Read-your-writes | A user sees their own update immediately, despite caching |

### Patterns

| Pattern | Flow | Consistency | Fit in Django |
|---|---|---|---|
| Cache-aside (lazy) | App reads cache; on miss reads DB and sets cache | Stale until TTL/invalidation | Default; `cache.get_or_set` |
| Read-through | Cache layer loads on miss itself | Same as cache-aside | A wrapper/selector that hides loading; Django has no native loader |
| Write-through | Write DB, then update cache synchronously | Fresher; write latency + failure coupling | Small, hot, frequently read objects (config, feature flags) |
| Write-behind (write-back) | Write cache; flush to DB asynchronously | Risk of data loss on cache failure | Rarely appropriate; counters/analytics only, never money/inventory |

## 3. Internal mechanics

### Django cache framework

- `django.core.cache.backends.redis.RedisCache` is built in (Django ≥ 4.0), using `redis-py`
  [third-party: redis]. `django-redis` [third-party: django-redis] is an alternative adding
  compression, connection-pool options and raw client access.
- Values are pickled by default; keys are built as `KEY_PREFIX:VERSION:key` via `KEY_FUNCTION`.
- `caches["alias"]` returns a per-thread backend instance (connection state is per thread);
  async methods (`aget`, `aset`, …) exist since Django 4.0 but for most backends wrap sync calls
  via `sync_to_async` (VERIFY for RedisCache in 5.2 — no native async client is used).
- `LocMemCache` is **per process**: with 8 Gunicorn workers there are 8 independent caches,
  so invalidating in one worker leaves 7 stale copies and memory is multiplied by 8.
- `DatabaseCache` and `FileBasedCache` add DB/disk I/O — rarely a win on hot paths.
- `cache_page`/`UpdateCacheMiddleware` cache the full response keyed by URL plus headers listed
  in `Vary`; `{% cache %}` caches rendered fragments with `vary_on` arguments.

### Redis behaviour that matters

- Memory bound by `maxmemory`; when full, `maxmemory-policy` decides: `allkeys-lru`/`allkeys-lfu`
  suit a pure cache; `volatile-*` only evicts keys with a TTL; `noeviction` makes writes fail.
- Single-threaded command execution: one `KEYS *` or a huge `DEL` blocks every client.
  Use `SCAN`/`UNLINK`, never `cache.delete_pattern` on large keyspaces in request paths.
- `INFO stats` exposes `keyspace_hits`, `keyspace_misses`, `evicted_keys`, `expired_keys`.

## 4. Engineering decisions and trade-offs

| Decision | Choose A when | Choose B when |
|---|---|---|
| Cache vs fix the query | Query is already optimal and still too costly at the needed rate | N+1, missing index or over-fetching is the real cause — fix that first |
| Local vs distributed | Immutable per-process data (parsed config, compiled regex) — `functools.lru_cache` | Anything that must be consistent across workers/hosts — Redis |
| Low-level vs per-view | Personalised pages with a shared expensive part | Anonymous, identical-for-everyone responses |
| TTL-only vs explicit invalidation | Bounded staleness is acceptable (stats, listings) | Users must see changes immediately (their own profile, prices) |
| Fail-open vs fail-closed | Cache is an accelerator — fall through to DB | Cache holds state (rate limits, locks) whose absence is unsafe |
| Two-tier (local + Redis) | Extreme read rate on tiny, rarely changing values | Anything changing — two tiers double the invalidation problem |

**TTL selection.** Derive TTL from the business staleness tolerance, not habit. Add jitter
(±10–20%) so keys written together do not expire together. Very long TTLs need reliable
invalidation; very short TTLs (< expected inter-arrival time of requests for that key) give a
near-zero hit ratio.

## 5. Django implementation patterns

### Configuration: separate cache from broker

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ["REDIS_CACHE_URL"],          # dedicated instance/db, allkeys-lru
        "TIMEOUT": 300,
        "KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "app"),
        "VERSION": 1,                                       # bump to invalidate globally on format change
        "OPTIONS": {
            "socket_connect_timeout": 0.2,                  # passed to redis-py pool; VERIFY option names
            "socket_timeout": 0.2,
        },
    }
}
# Celery broker uses a different Redis (noeviction) — an evicting cache must never hold tasks.
```

### Cache-aside with key discipline

```python
import hashlib
from django.core.cache import cache

def cache_key(*parts: object) -> str:
    return "v2:" + ":".join(str(p) for p in parts)

def search_key(tenant_id: int, term: str, locale: str) -> str:
    digest = hashlib.sha256(term.encode()).hexdigest()[:32]    # bound length, no raw input
    return cache_key("search", tenant_id, locale, digest)

def product_summary(tenant_id: int, product_id: int) -> dict:
    return cache.get_or_set(
        cache_key("product-summary", tenant_id, product_id),
        lambda: _compute_summary(tenant_id, product_id),       # returns plain dict, not model instances
        timeout=600 + random.randint(0, 120),
    )
```

Cache **plain data** (dicts, tuples, ids), not model instances or QuerySets: pickled instances
couple the cache to model code and break on deploys that change fields.

### Generational (versioned) invalidation

```python
def tenant_generation(tenant_id: int) -> int:
    key = f"gen:tenant:{tenant_id}"
    gen = cache.get(key)
    if gen is None:
        cache.add(key, 1, timeout=None)
        gen = cache.get(key, 1)
    return gen

def catalog_key(tenant_id: int, page: int) -> str:
    return cache_key("catalog", tenant_id, tenant_generation(tenant_id), page)

def bump_tenant_generation(tenant_id: int) -> None:
    transaction.on_commit(lambda: cache.incr(f"gen:tenant:{tenant_id}"))  # old keys age out via TTL
```

Cost: one extra round trip per read (use `get_many` to fetch generation and value keys together
when possible). Benefit: O(1) invalidation of an unbounded key family.

### Explicit delete after commit

```python
def update_price(product: Product, price: Decimal) -> None:
    with transaction.atomic():
        Product.objects.filter(pk=product.pk).update(price=price)
        transaction.on_commit(lambda: cache.delete(cache_key("product-summary", product.tenant_id, product.pk)))
```

Deleting inside the transaction lets a concurrent reader repopulate the cache with the old
committed value before the commit — always invalidate in `on_commit`. `QuerySet.update()`,
`bulk_create` and `bulk_update` bypass `save()` and signals; invalidate explicitly there.

### Stampede protection

```python
def get_with_lock(key: str, compute, timeout: int = 300):
    value = cache.get(key)
    if value is not None:
        return value
    if cache.add(f"lock:{key}", "1", timeout=30):          # atomic SET NX: one recomputer
        try:
            value = compute()
            cache.set(key, value, timeout + random.randint(0, 60))
            cache.set(f"stale:{key}", value, timeout * 10)
        finally:
            cache.delete(f"lock:{key}")
        return value
    stale = cache.get(f"stale:{key}")
    return stale if stale is not None else compute()       # cold start fallback
```

Alternatives: **probabilistic early expiration** (store `(value, computed_at, compute_time)`
and recompute early with probability rising as expiry approaches — the "XFetch" approach), or
**refresh-ahead** from a scheduled task for a known small set of hot keys.

### Negative caching

```python
_MISSING = "__none__"

def get_profile(external_id: str):
    hit = cache.get(f"profile:{external_id}")
    if hit == _MISSING:
        return None
    if hit is not None:
        return hit
    profile = fetch_profile(external_id)
    cache.set(f"profile:{external_id}", profile or _MISSING, timeout=300 if profile else 30)
    return profile
```

Shorter TTL for negatives so newly created objects appear quickly; negative caching also
blunts enumeration traffic against expensive lookups.

### Fragment and per-view caching

```django
{% load cache %}
{% cache 600 sidebar request.user.tenant_id LANGUAGE_CODE %}
  {% for c in categories %}…{% endfor %}
{% endcache %}
```

```python
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

@cache_page(60)
@vary_on_headers("Accept-Language")
def public_pricing(request): ...
```

`cache_page` on a view that reads `request.user` adds `Vary: Cookie` (via session access),
which yields one entry per session — memory cost with almost no reuse. Cache the expensive
computation instead.

### Queryset results

Cache the **evaluated, projected** result (`list(qs.values("id", "name"))`), never a lazy
QuerySet. Results are only as fresh as their invalidation trigger.

### Fail-open wrapper

```python
logger = logging.getLogger(__name__)

def safe_get_or_set(key, compute, timeout):
    try:
        return cache.get_or_set(key, compute, timeout)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        logger.warning("cache_unavailable", extra={"key_family": key.split(":")[1]})
        return compute()                                   # DB must survive this load — test it
```

Fail-open is only safe if the database can absorb the full miss load; otherwise combine with
load shedding ([scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)).

## 6. Production use cases

| Case | Pattern | Key dimensions | Invalidation |
|---|---|---|---|
| Tenant dashboard aggregates (1.2 s SQL) | Cache-aside + lock + stale copy | tenant, period, role | TTL 5 min + generation bump on import |
| Product catalogue pages, read 500×/write 1× | Generational keys | tenant, generation, page, locale | `incr` generation on publish |
| Exchange rates from external API | Refresh-ahead task every 60 s | currency pair | TTL 5 min as safety net |
| Feature flags read on every request | Write-through + short local `lru_cache` (≤ 5 s) | flag name | Write updates Redis; local tier expires quickly |
| Public marketing page | `cache_page` or CDN | path, language | Short TTL / purge on deploy |

Failure scenarios seen in production:

- **Eviction storm:** cache and Celery broker shared one Redis with `allkeys-lru`; a large
  report cache evicted queued tasks. Fix: separate instances, `noeviction` for the broker.
- **Synchronized expiry:** 20k keys set by a nightly warm job with TTL 3600 expired together;
  DB CPU hit 100%. Fix: jitter + stale-while-revalidate.
- **Cross-tenant leak:** key omitted `tenant_id`; first tenant's dashboard served to others.
- **Deploy break:** cached pickled model instances unpickled into a model with a removed field.
  Fix: cache plain dicts and bump `VERSION` on format changes.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Sub-millisecond reads for expensive results | Staleness and invalidation bugs that are hard to reproduce |
| Offloads saturated databases and rate-limited APIs | Another stateful dependency to size, monitor and fail over |
| Absorbs read spikes | Cold-start and stampede risk after flushes or deploys |
| Cheap horizontal read scaling | Masks underlying inefficiency; DB must still survive cache loss |

## 8. Security and performance considerations

- Every dimension that changes the output — tenant, user or permission set, locale, feature
  flags, API version — must be in the key. Missing one is a data leak, not just a bug.
- Never store secrets or personal data in shared caches without need; set TTLs; respect deletion
  requests (GDPR erasure must also purge cached copies).
- Pickle deserialisation of attacker-controlled bytes is code execution: never point Django's
  cache at a Redis that untrusted parties can write to; require auth/TLS on Redis.
- Hash user-supplied key parts; unbounded keys enable memory exhaustion and key-injection
  (memcached forbids spaces/control characters; Django warns via `CacheKeyWarning`).
- Large values: measure serialised size (`len(pickle.dumps(v))`); > 100 KB values deserve
  compression or restructuring (illustrative threshold).

## 9. Common mistakes and anti-patterns

- Adding a cache before checking query count and plans; caching an N+1 result hides the N+1.
- `LocMemCache` in multi-worker production for data that must be consistent.
- `cache.get` → compute → `cache.set` on hot keys with no stampede protection.
- Invalidating inside the transaction instead of `on_commit`.
- Relying on `post_save` signals while bulk operations update the same rows.
- One global TTL for everything; no jitter.
- `delete_pattern("*")`/`KEYS` in request code; `cache.clear()` in a deploy hook on a shared Redis.
- Measuring a global hit ratio — a 95% overall ratio can hide a 10% ratio on the expensive family.
- Caching error responses or partial results under the success key.

## 10. Troubleshooting and validation

Measure before and after, per key family:

1. **Baseline:** p50/p95 latency and DB time of the target endpoint, DB QPS for the query family
   ([profiling-and-observability.md](profiling-and-observability.md)).
2. **Instrument** hits/misses in code (counter per key family) — Redis `INFO stats` gives only
   global `keyspace_hits/keyspace_misses`.
3. **Watch Redis:** `INFO stats` (`evicted_keys`, `expired_keys`), `INFO memory`
   (`used_memory`, `maxmemory`), `SLOWLOG GET`, `LATENCY DOCTOR`, `--bigkeys` scan offline.
4. **Load test** cold and warm cache, plus "Redis down" mode
   ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).
5. **Correctness test:** update source → assert next read reflects it (after `on_commit` using
   `django_capture_on_commit_callbacks(execute=True)`; see
   [django-testing](../../django-testing/SKILL.md)).

| Symptom | Likely cause | Check |
|---|---|---|
| Hit ratio low | Key too specific, TTL too short, evictions | Key cardinality, `evicted_keys` |
| Latency spikes every N minutes | Synchronized expiry / stampede | Miss rate timeline vs TTL |
| Stale data after edits | Invalidation in transaction, bulk update bypass, missing dimension | Trace write path |
| Redis CPU 100% | Large values, `KEYS`, hot key | `SLOWLOG`, `--hotkeys` (LFU policy required) |
| Errors after deploy | Pickled format changed | Bump `VERSION` |

## 11. Related references and skills

- [django-core caching](../../django-core/references/caching.md) — framework API and backends.
- [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md) — Cache-Control, ETags, CDN.
- [api-performance.md](api-performance.md) — caching API responses.
- [orm-performance-optimization.md](orm-performance-optimization.md) — fix queries before caching.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — cache scaling, degradation.
- [performance-architecture-decisions.md](performance-architecture-decisions.md) — cache vs database reads.
- [django-security-engineering](../../django-security-engineering/SKILL.md) — data exposure through caches.

## 12. Official documentation and external references

- Django cache framework: https://docs.djangoproject.com/en/5.2/topics/cache/
- Django `transaction.on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Redis eviction policies: https://redis.io/docs/latest/develop/reference/eviction/
- Redis `INFO`: https://redis.io/docs/latest/commands/info/
- Redis latency diagnosis: https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/
- django-redis [third-party]: https://github.com/jazzband/django-redis
- Vattani et al., "Optimal Probabilistic Cache Stampede Prevention" (VLDB 2015): https://www.vldb.org/pvldb/vol8/p886-vattani.pdf
