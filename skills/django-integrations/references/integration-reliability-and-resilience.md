# Integration Reliability and Resilience

**Scope:** Canonical owner of how integrations behave under failure: failure classification, timeouts as
budgets, retry policy, backoff and jitter, retry budgets, idempotency keys, duplicate prevention, circuit
breakers, bulkheads, outbound rate and concurrency limiting, fallback and graceful degradation, and partial
failure. HTTP client configuration lives in [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md);
architectural choice of distributed styles lives in
[django-architecture distributed-systems](../../django-architecture/references/distributed-systems.md).

## 1. Purpose and scope

Provide the decision rules and Django implementations that keep a slow or failing external system from
corrupting data, duplicating side effects, or taking the Django application down with it.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Optional libraries:
tenacity `[third-party: tenacity]`, pybreaker `[third-party: pybreaker]`, redis-py `[third-party: redis]`.

Applies to every transport: HTTP, SOAP, broker publishes, cloud SDK calls, SMTP, SFTP.

## 2. Core concepts

### Failure classification

| Class | Examples | Action |
|---|---|---|
| Transient, request not sent | DNS failure, connect refused, connect timeout, pool timeout | Retry (any method) |
| Transient, server-declared | 429, 503, 502, 504, 408, broker "server busy" | Retry after delay / `Retry-After` |
| Ambiguous outcome | Read timeout, connection reset after send, 500 on a non-idempotent call | Retry **only** if idempotent or keyed; else reconcile |
| Permanent | 400, 404, 422, validation/contract errors, business rejection (card declined) | Do not retry; surface/record |
| Authentication | 401/403, expired/revoked credentials | Refresh once if token-based; then permanent + alert |
| Local saturation | Our pool exhausted, breaker open, rate limiter denies | Fail fast or queue; do not hammer |

These map to `apps/integrations/errors.py`: `TransientIntegrationError`, `RateLimitedError`,
`AmbiguousOutcomeError`, `PermanentIntegrationError`, `AuthenticationError`, `ContractViolationError`.

### Fundamental truths

- **A timeout does not prove the remote operation failed.** It proves you stopped waiting.
- **Retries do not give exactly-once execution.** They give at-least-once *attempts*. Exactly-once *effect*
  needs idempotency on the receiving side (idempotency key, natural key, conditional write).
- **Retries multiply load.** 3 layers × 3 attempts = 27 calls to a struggling dependency.
- **A 2xx is not business success**; a business rejection is not retryable.

### Idempotency

An operation is idempotent if applying it N ≥ 1 times has the same effect as once. HTTP defines GET, HEAD,
PUT, DELETE, OPTIONS as idempotent (RFC 9110 §9.2.2) — but that is a *contract*, and some APIs violate it.
POST and PATCH are not idempotent unless the provider supports an **idempotency key** (de facto header
`Idempotency-Key`; IETF draft `VERIFY` status) or the operation carries a natural unique key.

## 3. Internal mechanics

### Backoff and jitter

```text
exponential:   delay_n = min(cap, base * 2**n)
full jitter:   delay_n = random.uniform(0, min(cap, base * 2**n))         # best spread (AWS analysis)
equal jitter:  d = min(cap, base * 2**n); delay_n = d/2 + random.uniform(0, d/2)
decorrelated:  delay_n = min(cap, random.uniform(base, delay_{n-1} * 3))
```

Without jitter, clients that failed together retry together (thundering herd). Honour `Retry-After` when
present, capped by the remaining deadline.

### Retry budget

Cap retries as a **ratio** of normal traffic rather than per call: e.g. retries may add at most 10 % to the
request rate per provider over a sliding window. When the budget is exhausted, fail fast. This prevents retry
storms that per-call limits cannot (each call is "only" retrying 3 times).

### Circuit breaker states

```text
CLOSED --(failure rate ≥ threshold within window)--> OPEN
OPEN   --(cool-down elapsed)-------------------------> HALF_OPEN
HALF_OPEN --(probe succeeds)--> CLOSED ; --(probe fails)--> OPEN
```

In Django, state must be **shared across processes** (Gunicorn workers, Celery workers), so in-memory
breakers (pybreaker default storage) only protect one process. Use Redis/cache-backed state.

### Deadline propagation

Each hop subtracts elapsed time from the remaining budget and passes it on. A per-attempt timeout is
`min(configured_timeout, remaining_budget)`. When remaining < minimum useful attempt time, stop.

### Bulkhead

Isolate resources per dependency so one slow provider cannot exhaust shared capacity: separate Celery queues
and workers per provider, separate HTTP pools, semaphores bounding concurrent in-flight calls.

## 4. Engineering decisions and trade-offs

### Retry policy (canonical table)

| Condition | GET/PUT/DELETE | POST/PATCH with idempotency key | POST/PATCH without key |
|---|---|---|---|
| Connect error / connect timeout / pool timeout | Retry | Retry | Retry (not sent) |
| Read timeout / reset after send | Retry | Retry (same key) | **No** — `AmbiguousOutcomeError`, reconcile |
| 429 | Retry after `Retry-After` | Retry after `Retry-After` | Retry after `Retry-After` (provider rejected before processing — `VERIFY` per provider) |
| 502/503/504 | Retry | Retry | Usually **no**; 503 before processing may be safe (provider-specific) |
| 500 | Retry (limited) | Retry (limited) | **No** — reconcile |
| 408/425 | Retry | Retry | Provider-specific |
| 401 | Refresh token once, then fail | same | same |
| Other 4xx / validation / business rejection | No | No | No |

### Where to retry

| Context | Strategy |
|---|---|
| Web request | 0–1 quick retries inside a tight deadline (≤ ~1–2 s total); else fail fast and degrade or enqueue |
| Celery task | Task-level retry (`autoretry_for`, `retry_backoff`, `retry_jitter`, `max_retries`); don't `sleep` in-process across long waits |
| Broker consumer | Nack/redeliver with delay or DLQ after N attempts (broker-specific) |
| Batch sync | Per-record retry + failed-record table; never fail the whole batch for one row |

**Retry at one layer only.** If the SDK retries, the task should not also retry the same error class, or
configure the SDK to 0 retries and own the policy.

### When retries are dangerous

- Non-idempotent operations without a key: payments, order submission, sending email/SMS, ticket creation.
- Operations whose ambiguous outcome is costly to duplicate and cheap to look up — **look up first**.
- When the dependency is overloaded (retries deepen the outage) — breaker and budget must cap them.
- Long retry chains inside a DB transaction (locks held) or a web request (worker starvation).
- Retrying after a business rejection (declined card) — may trigger fraud rules.

### Circuit breaker vs retry vs rate limiter

| Mechanism | Protects | Use when |
|---|---|---|
| Retry + backoff | Single call from transient blips | Always, bounded |
| Retry budget | Dependency from retry storms | High-volume providers |
| Circuit breaker | Our workers from waiting on a dead dependency; dependency from load | Sync paths, high volume, real outages observed |
| Rate limiter | Provider quota; our account from suspension | Provider documents quotas |
| Concurrency limit / bulkhead | Our capacity from one slow provider | Several providers share workers |

Breakers add complexity and false trips at low volume; a breaker on a provider called 10 times/hour is
noise. Prefer task queues + backoff there.

### Fallback strategies

| Fallback | Example | Risk |
|---|---|---|
| Cached / last-known value | FX rates, catalogue | Staleness — show age, cap max age |
| Default value | "Delivery estimate unavailable" | Must be clearly non-authoritative |
| Queue for later | Accept order, charge asynchronously | UX must handle pending state |
| Alternate provider | SMS provider B | Duplicate sends if A actually succeeded |
| Fail closed | Fraud check down → hold the order | Revenue impact; but safe |

Never fall back to **fail open** on security or money decisions (auth, fraud, payment status).

## 5. Django implementation patterns

### Idempotency keys (outbound)

```python
# apps/orders/services.py
import uuid
from django.db import models, transaction


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; SUBMITTED = "submitted"; SUCCEEDED = "succeeded"
        FAILED = "failed"; UNKNOWN = "unknown"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    external_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
```

Rules:
- Generate the key **once, persisted before the first attempt**, and reuse it on every retry. Deriving it
  from a stable record (`f"payment:{payment.pk}"`) also works.
- A new logical operation (user retries checkout with different amount) gets a **new** key; reusing a key
  with a different body is rejected by most providers (409/422, provider-specific).
- Provider key retention is finite (e.g. 24 h at Stripe `VERIFY`); retries beyond retention lose protection.
- If the provider has no idempotency support: record `SUBMITTED` before the call, and on `AmbiguousOutcomeError`
  mark `UNKNOWN` and **query by your reference** (`metadata.order_ref`, `client_reference_id`) before retrying.

### Inbound idempotency (duplicate request prevention)

For your own APIs that external systems call: store `(client_id, idempotency_key) → response hash/status`
with a unique constraint, return the stored response for repeats, reject key reuse with a different body.
Webhooks and broker messages use `WebhookEvent` / `InboxMessage`
([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md),
[integration-architecture-patterns.md](integration-architecture-patterns.md)).

### Bounded retry helper (`apps/integrations/resilience.py`)

```python
import random
import time
from collections.abc import Callable
from typing import TypeVar

from apps.integrations.errors import RateLimitedError, TransientIntegrationError

T = TypeVar("T")


def retry_call(
    fn: Callable[[float], T],          # receives per-attempt timeout
    *,
    attempts: int = 3,
    base: float = 0.2,
    cap: float = 5.0,
    deadline_s: float = 10.0,
    min_attempt_s: float = 0.3,
) -> T:
    """Retry only TransientIntegrationError, with full jitter, inside a total deadline."""
    end = time.monotonic() + deadline_s
    for attempt in range(attempts):
        remaining = end - time.monotonic()
        if remaining < min_attempt_s:
            break
        try:
            return fn(remaining)
        except TransientIntegrationError as exc:
            if attempt == attempts - 1:
                raise
            delay = random.uniform(0, min(cap, base * 2 ** attempt))
            if isinstance(exc, RateLimitedError) and exc.retry_after is not None:
                delay = exc.retry_after
            if time.monotonic() + delay + min_attempt_s > end:
                raise                                     # not enough budget left to try again
            time.sleep(delay)
    raise TransientIntegrationError("retry deadline exhausted")
```

`AmbiguousOutcomeError` and `PermanentIntegrationError` are deliberately **not** retried. tenacity expresses
the same policy declaratively (`retry_if_exception_type`, `wait_random_exponential`, `stop_after_delay`).

### Celery task retries

```python
from celery import shared_task
from apps.integrations.errors import AmbiguousOutcomeError, RateLimitedError, TransientIntegrationError


@shared_task(bind=True, autoretry_for=(TransientIntegrationError,), retry_backoff=2, retry_backoff_max=600,
             retry_jitter=True, max_retries=8, acks_late=True)
def submit_payment(self, payment_id: int) -> None:
    payment = Payment.objects.get(pk=payment_id)
    if payment.external_id or payment.status in {Payment.Status.SUCCEEDED, Payment.Status.FAILED}:
        return                                                    # idempotent re-run
    try:
        charge = gateway().create_charge(amount=payment.amount, currency=payment.currency,
                                         source=payment.source, idempotency_key=str(payment.idempotency_key))
    except RateLimitedError as exc:
        raise self.retry(exc=exc, countdown=exc.retry_after or 30)
    except AmbiguousOutcomeError:
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.UNKNOWN)
        reconcile_payment.apply_async((payment.pk,), countdown=60)
        return
    Payment.objects.filter(pk=payment.pk).update(external_id=charge.external_id, status=charge.status)
```

Celery task mechanics (acks, `on_commit` dispatch): [celery-and-background-integrations.md](celery-and-background-integrations.md).

### Shared circuit breaker (Redis)

```python
import time
from django.core.cache import caches

from apps.integrations.errors import TransientIntegrationError


class CircuitOpenError(TransientIntegrationError):
    """Raised without calling the provider."""


class CircuitBreaker:
    def __init__(self, name: str, *, failure_threshold: int = 10, window_s: int = 60, cooldown_s: int = 30):
        self.cache = caches["default"]                   # must be shared (Redis), not LocMem
        self.k_fail, self.k_open, self.k_probe = (f"cb:{name}:{s}" for s in ("fail", "open", "probe"))
        self.threshold, self.window, self.cooldown = failure_threshold, window_s, cooldown_s

    def before_call(self) -> None:
        if self.cache.get(self.k_open):
            # one probe per cooldown period; cache.add is atomic on Redis
            if not self.cache.add(self.k_probe, 1, timeout=self.cooldown):
                raise CircuitOpenError("circuit open")

    def on_success(self) -> None:
        self.cache.delete_many([self.k_fail, self.k_open, self.k_probe])

    def on_failure(self) -> None:
        self.cache.add(self.k_fail, 0, timeout=self.window)     # start window
        if self.cache.incr(self.k_fail) >= self.threshold:
            self.cache.set(self.k_open, time.time(), timeout=None)


def guarded(breaker: CircuitBreaker, fn, *args, **kwargs):
    breaker.before_call()
    try:
        result = fn(*args, **kwargs)
    except TransientIntegrationError:
        breaker.on_failure()
        raise
    breaker.on_success()
    return result
```

This is a count-in-window breaker; production breakers often use failure **rate** with a minimum request
volume to avoid tripping on 3 failures out of 3 calls at night. Count only transient/timeout errors —
4xx are caller bugs, not provider health.

### Distributed rate limiter (Redis token bucket)

```python
import time
from django_redis import get_redis_connection   # [third-party: django-redis]; or redis.Redis.from_url

from apps.integrations.errors import RateLimitedError

_LUA = """
local key, rate, burst, now, cost = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local s = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(s[1]) or burst
local ts = tonumber(s[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = tokens >= cost
if allowed then tokens = tokens - cost end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 1)
if allowed then return 0 end
return tostring((cost - tokens) / rate)
"""


class RateLimiter:
    def __init__(self, name: str, *, rate_per_s: float, burst: int):
        self.redis = get_redis_connection("default")
        self.key, self.rate, self.burst = f"rl:{name}", rate_per_s, burst
        self.script = self.redis.register_script(_LUA)

    def acquire(self, *, cost: int = 1, max_wait_s: float = 5.0) -> None:
        end = time.monotonic() + max_wait_s
        while True:
            wait = float(self.script(keys=[self.key], args=[self.rate, self.burst, time.time(), cost]))
            if wait == 0:
                return
            if time.monotonic() + wait > end:
                raise RateLimitedError("local rate limit", retry_after=wait, provider=self.key)
            time.sleep(wait)
```

Notes: uses server-side Lua for atomicity; clock is the caller's `time.time()` (skew between hosts shifts
refill slightly — use `redis.call('TIME')` inside the script to avoid it). In Celery, prefer raising and
`self.retry(countdown=wait)` over sleeping. Celery's per-task `rate_limit` is **per worker**, not global
`VERIFY`, so it does not enforce a provider-wide quota.

### Concurrency limit / bulkhead

- Celery: route each provider to its own queue (`task_routes`) and run a worker with fixed `--concurrency`.
- Threads: `threading.BoundedSemaphore(n)` around calls in a gthread worker (per process only).
- Cross-process: Redis semaphore (sorted set of lease tokens with expiry) — only when quotas demand it.

### Partial failure

For batch/multi-step operations:
- Record per-item outcome (`succeeded`, `failed`, `unknown`) instead of all-or-nothing.
- Provider bulk endpoints may return `200` with per-item errors — parse them.
- Multi-provider workflows (charge + ship + notify) need compensation or a saga, not distributed transactions
  ([integration-architecture-patterns.md](integration-architecture-patterns.md)).

## 6. Production use cases

- **Payment provider brown-out**: p99 rises to 20 s; per-attempt timeout 8 s with task retries and jitter;
  breaker opens at 50 % failure over 60 s with ≥ 20 calls; checkout switches to "payment pending" and the queue
  drains when the breaker closes.
- **CRM quota 100 req/10 s per account**: token bucket rate 9/s burst 20 shared by all workers; 429 still
  handled with `Retry-After`.
- **Email provider outage**: notifications queue grows; failover to secondary only for messages with no
  ambiguous attempt at primary ([email-sms-and-notification-integrations.md](email-sms-and-notification-integrations.md)).
- **Order submission to ERP without idempotency**: timeout → `UNKNOWN`; reconciliation job queries ERP by
  our order number before resubmitting.

## 7. Advantages and disadvantages

| Mechanism | Advantages | Disadvantages |
|---|---|---|
| Retries with jitter | Hide transient faults cheaply | Amplify load; duplicate side effects if misapplied |
| Idempotency keys | Safe retries for writes | Requires provider support and key persistence |
| Circuit breaker | Fast failure, protects workers and provider | Tuning; false trips at low volume; shared state needed |
| Rate limiter | Prevents quota bans | Redis dependency; adds latency |
| Bulkheads | Contain blast radius | More queues/workers to operate |
| Fallbacks | Availability | Stale or approximate data; correctness risk |

## 8. Security and performance considerations

- Breakers and limiters keyed by tenant/account where quotas are per account; never let one tenant exhaust
  shared quota (noisy neighbour).
- Retry logs must not include request bodies; log key, attempt, error class.
- Idempotency keys are not secrets but must not be guessable if they also authorise replay of responses on
  **your** API — scope stored responses to the authenticated client.
- Synchronous retries consume web workers: total budget × concurrency = worker-seconds lost during outages
  ([django-performance concurrency](../../django-performance/references/concurrency-and-worker-management.md)).
- Retry amplification shows up as queue growth — see
  [django-performance background-task-performance](../../django-performance/references/background-task-performance.md).
- Redis becomes a dependency of every guarded call; if Redis is down, fail **open** for breakers/limiters
  (call the provider) but log loudly — a breaker should not cause an outage.

## 9. Common mistakes and anti-patterns

- Retrying every exception (`except Exception: retry`) — retries bugs and business rejections.
- Retrying a POST after a read timeout with no idempotency key → duplicate charge/order/email.
- Generating a new idempotency key per attempt (defeats it).
- Nested retries: SDK retries × client retries × task retries.
- Fixed `time.sleep(5)` loops in views.
- `max_retries=None` / unbounded retries; no dead-letter state.
- In-process circuit breaker in a 32-worker deployment — each worker learns separately.
- Counting 4xx as breaker failures — a client bug opens the circuit for everyone.
- Fallback to "approved" when the fraud/payment check is unavailable.
- Calling external services inside `transaction.atomic()` — locks held during retries; remote effect without
  local commit on rollback.
- Treating `AmbiguousOutcomeError` as failure and telling the user "payment failed".

## 10. Troubleshooting and validation

| Symptom | Investigate |
|---|---|
| Provider reports duplicate operations | Was the key persisted before the first attempt? Same key reused on retry? Read-timeout retries without key? |
| Outage of one provider stalls all tasks | Shared queue/workers — add per-provider queues (bulkhead) |
| Retry storm after recovery | Missing jitter; retry budget; queue drained all at once — add rate limiter |
| Breaker flaps open/closed | Threshold too low for volume; probes counted wrong; 4xx counted as failures |
| 429 despite limiter | Limiter per process, not shared; other systems share the quota; clock skew |
| Records stuck in `UNKNOWN` | Reconciliation job missing or failing — alert on age of `UNKNOWN` rows |

Validation: test each row of the retry table with `httpx.MockTransport` (timeouts, 429 with `Retry-After`,
500 on POST without key → `AmbiguousOutcomeError`); assert attempts count and that the same idempotency key
is sent each time; fault-injection in staging (toxiproxy `[third-party: toxiproxy]`) for latency and resets
([integration-testing-and-contracts.md](integration-testing-and-contracts.md),
[django-testing integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md)).
Monitor retries, breaker state and limiter waits per provider
([integration-observability-and-operations.md](integration-observability-and-operations.md)).

## 11. Related references and skills

- [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md) — timeouts configuration, error normalisation.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — task retries, `on_commit`.
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — redelivery, DLQ, poison messages.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox, inbox, saga, compensation.
- [payment-and-financial-integrations.md](payment-and-financial-integrations.md) — ambiguous payment outcomes.
- [django-architecture distributed-systems](../../django-architecture/references/distributed-systems.md) — style-level reasoning.
- [django-performance background-task-performance](../../django-performance/references/background-task-performance.md),
  [scalability-and-capacity-planning](../../django-performance/references/scalability-and-capacity-planning.md).

## 12. Official documentation and external references

- RFC 9110 §9.2.2 Idempotent methods, §10.2.3 Retry-After: <https://www.rfc-editor.org/rfc/rfc9110>
- IETF Idempotency-Key header draft: <https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/> `VERIFY` status
- AWS Architecture Blog — Exponential Backoff and Jitter: <https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>
- AWS Builders' Library — Timeouts, retries and backoff with jitter: <https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/>
- Azure Architecture Center — Circuit Breaker pattern: <https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker>
- Azure Architecture Center — Retry pattern: <https://learn.microsoft.com/en-us/azure/architecture/patterns/retry>
- Azure Architecture Center — Bulkhead pattern: <https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead>
- Google SRE Book — Handling Overload / Addressing Cascading Failures: <https://sre.google/sre-book/addressing-cascading-failures/>
- Celery — automatic retry: <https://docs.celeryq.dev/en/stable/userguide/tasks.html#automatic-retry-for-known-exceptions>
- tenacity: <https://tenacity.readthedocs.io/>
- Redis — EVAL / scripting: <https://redis.io/docs/latest/develop/interact/programmability/eval-intro/>
- Django transactions — `on_commit`: <https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit>
- Stripe idempotent requests (provider-specific example): <https://docs.stripe.com/api/idempotent_requests>
