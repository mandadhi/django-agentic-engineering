# Throttling and Rate Limiting

> **Scope.** Owns: DRF's throttling architecture, `AnonRateThrottle`, `UserRateThrottle`,
> `ScopedRateThrottle`, custom throttles, rate and scope configuration, cache dependency,
> client identification behind proxies, concurrency and distributed-deployment limitations, and
> how application throttling relates to infrastructure rate limiting.
> Does not cover: Django's cache configuration ([django-core caching](../../django-core/references/caching.md)),
> denial-of-service defence and abuse/threat modelling ([django-security-engineering](../../django-security-engineering/SKILL.md)),
> or capacity planning ([django-performance](../../django-performance/SKILL.md)).

## 1. Purpose and scope

DRF throttles shape *fair use* per client or per endpoint and give clients a `429` with
`Retry-After`. **They are not a complete security or DoS defence**: they run inside the
application after the request has been accepted, parsed for auth and checked for permissions.

## 2. Core concepts

| Class | Identifies by | Scope key | Applies to |
|---|---|---|---|
| `AnonRateThrottle` | Client IP (`get_ident`) | `anon` | Unauthenticated requests only |
| `UserRateThrottle` | User PK (IP if anonymous) | `user` | All requests |
| `ScopedRateThrottle` | User PK or IP + view's `throttle_scope` | value of `throttle_scope` | Only views that define `throttle_scope` |
| `BaseThrottle` | — | — | Base for custom logic (`allow_request`, `wait`) |
| `SimpleRateThrottle` | `get_cache_key()` | `scope` | Base for custom cache-backed rate throttles |

Rates: `"<number>/<period>"` with period `s`, `m`, `h`, `d` (only the first letter is
significant: `"100/min"` = `"100/m"`). Configured in `DEFAULT_THROTTLE_RATES` by scope.

**Application vs infrastructure rate limiting.**

| Layer | Examples | Strengths | Limits |
|---|---|---|---|
| Edge/CDN/WAF | Cloud WAF rules, CDN rate rules | Absorbs volume before it reaches you | Coarse identity (IP, path) |
| Reverse proxy / gateway | nginx `limit_req`, API gateway quotas | Cheap, before Python | Little knowledge of users/plans |
| DRF throttles | Per user, per scope, per plan | Knows the authenticated user and endpoint | Runs late; approximate; cache-dependent |

Use them together: edge/proxy for volume, DRF for per-user/per-endpoint policy.

## 3. Internal mechanics

- `check_throttles()` runs in `APIView.initial()` **after** authentication and permission checks.
  Requests rejected earlier (invalid credentials → 401, forbidden → 403) are not counted.
- All throttles from `get_throttles()` are evaluated; if any refuse, `Throttled` (429) is raised
  with the maximum `wait()` as `Retry-After`.
- `SimpleRateThrottle.allow_request()`:
  1. `key = get_cache_key(request, view)` — `None` means "don't throttle this request".
  2. `history = cache.get(key, [])` — a list of request timestamps.
  3. Drop timestamps older than the window; if `len(history) >= num_requests` → refuse.
  4. Otherwise insert `now` and `cache.set(key, history, duration)`.
- This is a read-modify-write on the cache with **no locking**: concurrent requests can exceed the
  limit. It is a sliding-log approximation, documented by DRF as not a strict guarantee.
- The cache is `django.core.cache.cache` (default alias) unless the throttle's `cache` attribute
  is overridden. `LocMemCache` is per process — with N workers, effective limits are ~N× the
  configured rate. Use a shared cache (Redis/Memcached) in production.
- `get_ident()` uses `REMOTE_ADDR`, or `X-Forwarded-For` when `NUM_PROXIES` is set (it takes
  the address `NUM_PROXIES` from the right). If `NUM_PROXIES` is `None` and `X-Forwarded-For` is
  present, DRF uses the whole header value — **VERIFY** exact behaviour for your release; set
  `NUM_PROXIES` explicitly.

## 4. Engineering decisions and trade-offs

| Need | Approach |
|---|---|
| General fair-use for anonymous and authenticated traffic | `AnonRateThrottle` + `UserRateThrottle` defaults |
| Stricter limit on login, password reset, OTP, signup | `ScopedRateThrottle` with `throttle_scope = "login"`, low rate, plus infrastructure limits |
| Expensive endpoints (exports, search) | Scoped throttle per endpoint |
| Per-plan quotas (free vs paid) | Custom `SimpleRateThrottle` whose `get_rate()`/`allow_request` reads the plan |
| Strict billing quotas | Not DRF throttles — a durable counter in the DB/billing system |
| Concurrent-request caps | Not supported by DRF throttles — gateway or semaphore in cache |
| Brute force on credentials | Throttle the login view by IP *and* username + lockout policy + edge limits |

## 5. Django/DRF implementation patterns

**Settings:**

```python
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "1000/hour",
        "login": "10/min",
        "export": "5/hour",
    },
    "NUM_PROXIES": 1,          # exactly the number of trusted proxies in front of Django
}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                      "LOCATION": env("REDIS_URL")}}
```

**Scoped throttle on a view or action:**

```python
class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]

    @action(detail=False, methods=["get"], throttle_scope="export")   # passed as initkwarg
    def export(self, request): ...
```

(`throttle_scope` as an `@action` kwarg overrides the attribute for that action; alternatively
set `throttle_scope` in `get_throttles()` logic — **VERIFY** your chosen approach in a test.)

**Login throttle keyed on submitted username and IP:**

```python
class LoginRateThrottle(SimpleRateThrottle):
    scope = "login"

    def get_cache_key(self, request, view):
        username = str(request.data.get("username", "")).lower()[:150]
        return self.cache_format % {"scope": self.scope,
                                    "ident": f"{self.get_ident(request)}:{username}"}
```

**Plan-based rate:**

```python
class PlanRateThrottle(UserRateThrottle):
    PLAN_RATES = {"free": "100/hour", "pro": "5000/hour"}

    def allow_request(self, request, view):
        if request.user.is_authenticated:
            self.rate = self.PLAN_RATES.get(request.user.organization.plan, "100/hour")
            self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)
```

**Dedicated throttle cache:**

```python
from django.core.cache import caches

class UserRateThrottle(throttling.UserRateThrottle):
    cache = caches["throttle"]
```

## 6. Production use cases

- **Public API**: edge limits per IP; DRF per-user hourly limits; scoped limits on search.
- **Auth endpoints**: low scoped rates on login/reset/OTP plus account lockout and alerting.
- **Expensive exports**: `5/hour` per user, with the export itself queued as a background job
  ([django-integrations](../../django-integrations/SKILL.md)).
- **Partner tiers**: plan-based throttle keyed on the API key's organization.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Per-user and per-endpoint policies with little code | Non-atomic; can be exceeded under concurrency |
| Standard 429 + `Retry-After` | Depends on a shared cache; LocMem breaks it in multi-process deployments |
| Easy plan-specific overrides | Runs after auth/permissions/parsing setup — no protection for cheap-to-send floods |
| | Stores a timestamp list per key (memory grows with rate) |

## 8. Security and performance considerations

- Not a DoS defence: the request has already consumed a worker. Put volume limits at the
  edge/proxy.
- Wrong `NUM_PROXIES` either throttles everyone as one IP (all traffic shares the proxy's address)
  or lets clients spoof `X-Forwarded-For` to dodge limits.
- Invalid-credential attempts are not throttled by DRF's default classes (authentication fails
  first); protect auth with dedicated mechanisms.
- High rates (e.g. `10000/hour`) store large timestamp lists per key; consider a fixed-window
  counter with atomic `cache.incr` for very high rates.
- Throttle cache outages: decide fail-open vs fail-closed explicitly in custom throttles.

## 9. Common mistakes and anti-patterns

- Using LocMem cache in production and believing limits are enforced.
- Relying on DRF throttling as the brute-force or DDoS control.
- `ScopedRateThrottle` in classes but no `throttle_scope` on the view (no effect) or scope missing
  from `DEFAULT_THROTTLE_RATES` (`ImproperlyConfigured`).
- Throttling health checks or internal service traffic accidentally.
- Using DRF throttles for billing-grade quotas.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| Limits never trigger | Per-process LocMem cache; `get_cache_key` returns `None`; scope not set |
| Everyone throttled together | `NUM_PROXIES` unset behind a proxy → all requests share the proxy IP |
| `ImproperlyConfigured: No default throttle rate set for 'x' scope` | Missing `DEFAULT_THROTTLE_RATES` entry |
| Throttling leaks between tests | Cache not cleared between tests (`cache.clear()` in `setUp`) |

Test: lower the rate via `override_settings` (note DRF caches throttle rates on the class —
**VERIFY**; patching `THROTTLE_RATES` on the class is a common workaround) and assert 429 and
`Retry-After` ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [authentication.md](authentication.md) — protecting credential endpoints
- [request-response-lifecycle.md](request-response-lifecycle.md) — where throttles run
- [exception-handling-and-error-responses.md](exception-handling-and-error-responses.md) — 429 body
- [django-core caching](../../django-core/references/caching.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md), [django-performance](../../django-performance/SKILL.md)

## 12. Official documentation and external references

- Throttling: <https://www.django-rest-framework.org/api-guide/throttling/>
- Settings — `NUM_PROXIES`: <https://www.django-rest-framework.org/api-guide/settings/>
- Django cache framework: <https://docs.djangoproject.com/en/5.2/topics/cache/>
- RFC 6585 §4 (429 Too Many Requests): <https://www.rfc-editor.org/rfc/rfc6585#section-4>
