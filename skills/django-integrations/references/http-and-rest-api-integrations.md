# HTTP and REST API Integrations

**Scope:** How a Django application consumes external HTTP/REST APIs: client choice and configuration,
connection pooling, timeouts, status handling, pagination, rate-limit headers, response validation, error
normalisation, streaming, large payloads, API versioning and wrapping provider SDKs. Retry policy, circuit
breakers and idempotency keys are owned by
[integration-reliability-and-resilience.md](integration-reliability-and-resilience.md); building DRF endpoints
is owned by [django-rest-framework](../../django-rest-framework/SKILL.md).

## 1. Purpose and scope

Give an agent everything needed to build a **third-party API client** that is the only place in the codebase
that knows a provider's URLs, auth, payload shapes and error formats, and that returns domain objects and
raises the shared exception taxonomy.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. HTTP clients:
HTTPX `[third-party: httpx]` (default for new code) and Requests `[third-party: requests]` (where already
used). Validation examples use Pydantic v2 `[third-party: pydantic]` or stdlib dataclasses.

In scope: outbound request/response mechanics. Out of scope: authentication flows
([authentication-and-identity-integrations.md](authentication-and-identity-integrations.md)), inbound webhooks
([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md)), SSRF depth
([django-security-engineering ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md)).

## 2. Core concepts

| Concept | Meaning for an integration |
|---|---|
| Client (session) | Long-lived object holding a connection pool, default headers, auth and timeouts. Reuse it. |
| Connection pool | Keep-alive TCP/TLS connections per host; avoids handshake cost (often 50–200 ms per new TLS connection over WAN). |
| Connect timeout | Max time to establish TCP + TLS. Short (1–3 s): failure means the request was **not** sent. |
| Read timeout | Max time between bytes received. Expiry means the request **may have been processed**. |
| Write timeout | Max time to send a chunk of the request body (httpx only). |
| Pool timeout | Max wait for a free connection from the pool (httpx only). Expiry signals local saturation, not remote failure. |
| Total deadline | Wall-clock budget for the whole operation including retries; neither httpx nor requests enforce it per request. |
| Status class | 2xx success, 3xx redirect, 4xx caller error (except 408/409/425/429), 5xx server error. |
| Contract | The documented request/response shape, versioned by the provider. Validate what you depend on. |
| Error normalisation | Mapping transport errors, status codes and provider error bodies into `apps/integrations/errors.py`. |

**A 2xx does not mean business success.** Many APIs return `200` with `{"status": "FAILED"}` or a partial
batch result, or `202 Accepted` meaning "queued, outcome later". Always interpret the body.

**A timeout does not mean the remote operation failed.** A read timeout on `POST /charges` leaves the outcome
unknown; raise `AmbiguousOutcomeError` and reconcile.

## 3. Internal mechanics

### httpx

- `httpx.Client` / `httpx.AsyncClient` own an `HTTPTransport` backed by `httpcore` with a pool governed by
  `httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)` (defaults `VERIFY`).
- `httpx.Timeout(10.0, connect=3.0)` sets read/write/pool to 10 s and connect to 3 s. Default when unset is 5 s
  for all four (`VERIFY` for your installed version). `timeout=None` disables — never do this.
- `HTTPTransport(retries=N)` retries **only connection establishment failures** (`ConnectError`,
  `ConnectTimeout`), never on status codes or read timeouts.
- Redirects are **not** followed by default (`follow_redirects=False`), unlike requests.
- Event hooks (`event_hooks={"request": [...], "response": [...]}`) run on every request: use for logging,
  metrics, correlation-ID injection.
- HTTP/2 requires `httpx[http2]` and `http2=True`; multiplexes over one connection per host.
- `response.raise_for_status()` raises `httpx.HTTPStatusError` for 4xx/5xx. Exception hierarchy:
  `httpx.HTTPError` → `TransportError` (`TimeoutException` → `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`,
  `PoolTimeout`; `NetworkError` → `ConnectError`, `ReadError`; `ProtocolError`) and `HTTPStatusError`.

### requests

- `requests.Session` holds a `HTTPAdapter` per URL prefix; each adapter owns a urllib3 `PoolManager`
  (`pool_connections=10`, `pool_maxsize=10`, `VERIFY`).
- **No default timeout**: `requests.get(url)` can block forever. Pass `timeout=(connect, read)` on every call
  (or subclass the adapter to inject one).
- Retries come from `urllib3.util.Retry` mounted on the adapter (`allowed_methods`, `status_forcelist`,
  `backoff_factor`, `respect_retry_after_header`). Default `allowed_methods` excludes POST and PATCH.
- Follows redirects by default (`allow_redirects=True`), including across hosts — relevant for SSRF.
- `Session` is not documented as thread-safe `VERIFY`; one session per thread or per worker process is the
  conservative choice. `httpx.Client` is documented as thread-safe for concurrent use.

### Process model implications in Django

- WSGI worker (Gunicorn sync/gthread): a module-level client is created once per process after fork. Do not
  create the client at import time in a module imported by the Gunicorn master with `--preload` and then use
  it across fork — sockets inherited across fork are corrupt. Lazily create on first use.
- Celery prefork workers: same rule; create lazily in the child (or on `worker_process_init`).
- ASGI: `AsyncClient` must be used in the event loop that created it. Create it in an async context (lifespan
  handler or lazily per loop); never share an `AsyncClient` across loops (e.g. `async_to_sync` creates new loops).

## 4. Engineering decisions and trade-offs

### Client library

| Situation | Choose | Why |
|---|---|---|
| New code, sync or async | httpx | One API for both, explicit timeouts, MockTransport for tests, HTTP/2 option |
| Project already standardised on requests | requests | Don't add a second HTTP stack; mount urllib3 Retry |
| Provider ships a maintained official SDK | SDK, wrapped | Auth, pagination, signing and retries handled; wrap behind a gateway |
| SDK is unmaintained, heavy or hides timeouts | Thin httpx client | Control over timeouts, pooling, errors |
| Very high concurrency fan-out in async views | `httpx.AsyncClient` with a semaphore | Bounded concurrency without threads |

### Timeout sizing

Derive from the caller's deadline, not from the provider's p50:

```text
caller budget (e.g. web request behind a 30 s proxy, target p99 2 s)
  → per-integration budget  (≤ 1.5 s total for this call, including retries)
  → per-attempt timeout     (connect 1 s, read ≈ provider p99 × 1.5, capped by remaining budget)
```

Use background tasks when the provider's p99 exceeds what a user should wait. See deadline propagation in
[integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

### Validation strictness

| Approach | Use when | Cost |
|---|---|---|
| Pydantic model, `extra="ignore"` | Default: validate fields you use, tolerate additions | Dependency; small CPU cost |
| Pydantic `extra="forbid"` | Contract tests only — detects drift early | Breaks production on harmless additions |
| Dataclass + manual parsing | No Pydantic in project, small payloads | Hand-written coercion |
| No validation (`dict` passthrough) | Never for domain-relevant data | `KeyError` deep in business code |

**Tolerant reader:** accept unknown fields and unknown enum values (map to `UNKNOWN`), reject missing required
fields and type mismatches you depend on.

### Sync vs async client

Async helps only when one request performs several independent outbound calls concurrently, or in an
ASGI-native service with many concurrent slow upstreams. A sync view calling one API gains nothing from async.
Detail: [django-performance async](../../django-performance/references/async-django-performance.md).

## 5. Django implementation patterns

### Client factory (`apps/integrations/http.py`)

```python
# apps/integrations/http.py
import logging
import time
from functools import cache

import httpx
from django.conf import settings

logger = logging.getLogger("integrations.http")


def _log_request(request: httpx.Request) -> None:
    request.extensions["start"] = time.monotonic()


def _log_response(response: httpx.Response) -> None:
    started = response.request.extensions.get("start", time.monotonic())
    logger.info(
        "http.call",
        extra={
            "method": response.request.method,
            "host": response.request.url.host,
            "path": response.request.url.path,      # never the query string: may carry tokens
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "provider_request_id": response.headers.get("x-request-id"),
        },
    )


def build_client(
    name: str,
    *,
    base_url: str,
    timeout: httpx.Timeout,
    auth: httpx.Auth | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        auth=auth,
        headers={"User-Agent": f"{settings.SERVICE_NAME}/{settings.RELEASE} ({name})", **(headers or {})},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        transport=httpx.HTTPTransport(retries=1),  # connection-establishment retries only
        follow_redirects=False,
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )
```

### Provider client + gateway (`apps/billing/integrations/acmepay/`)

```python
# client.py — transport, auth, error normalisation. Knows HTTP, nothing about domain.
from functools import cache

import httpx
from django.conf import settings

from apps.integrations.errors import (
    AmbiguousOutcomeError, AuthenticationError, ContractViolationError,
    PermanentIntegrationError, RateLimitedError, TransientIntegrationError,
)
from apps.integrations.http import build_client

PROVIDER = "acmepay"


@cache  # one client per process; created lazily after fork
def get_client() -> httpx.Client:
    return build_client(
        PROVIDER,
        base_url=settings.ACMEPAY_BASE_URL,
        timeout=httpx.Timeout(8.0, connect=2.0),
        headers={"Authorization": f"Bearer {settings.ACMEPAY_API_KEY}", "AcmePay-Version": "2025-01-01"},
    )


def request(method: str, path: str, *, operation: str, idempotent: bool, **kwargs) -> httpx.Response:
    ctx = {"provider": PROVIDER, "operation": operation}
    try:
        response = get_client().request(method, path, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise TransientIntegrationError(str(exc), **ctx) from exc        # not sent
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
        if idempotent:
            raise TransientIntegrationError(str(exc), **ctx) from exc
        raise AmbiguousOutcomeError(str(exc), **ctx) from exc            # may have been applied
    return _check(response, ctx)


def _check(response: httpx.Response, ctx: dict) -> httpx.Response:
    status = response.status_code
    ctx |= {"status_code": status, "request_id": response.headers.get("x-request-id")}
    if status < 400:
        return response
    if status in (401, 403):
        raise AuthenticationError(_provider_message(response), **ctx)
    if status == 429:
        raise RateLimitedError(_provider_message(response), retry_after=parse_retry_after(response), **ctx)
    if status in (408, 425) or status >= 500:
        raise TransientIntegrationError(_provider_message(response), **ctx)
    raise PermanentIntegrationError(_provider_message(response), **ctx)


def _provider_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("error", {}).get("code", "unknown"))   # code only; messages may echo PII
```

```python
# schemas.py — the provider's wire contract (only fields we use)
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ChargeStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):           # tolerant reader for new enum values
        return cls.UNKNOWN


class ChargeOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    status: ChargeStatus
    amount_minor: int                    # integer minor units on the wire
    currency: str
```

```python
# gateway.py — implements the domain port; translates wire ↔ domain
from decimal import Decimal

from pydantic import ValidationError

from apps.billing.domain import Charge, PaymentGateway  # Protocol + domain dataclass
from apps.integrations.errors import ContractViolationError

from . import client
from .schemas import ChargeOut


class AcmePayGateway(PaymentGateway):
    def create_charge(self, *, amount: Decimal, currency: str, source: str, idempotency_key: str) -> Charge:
        response = client.request(
            "POST", "/v1/charges", operation="create_charge", idempotent=True,  # keyed → safe to retry
            json={"amount_minor": int(amount * 100), "currency": currency, "source": source},
            headers={"Idempotency-Key": idempotency_key},
        )
        try:
            wire = ChargeOut.model_validate_json(response.content)
        except ValidationError as exc:
            raise ContractViolationError(str(exc), provider=client.PROVIDER, operation="create_charge") from exc
        return Charge(external_id=wire.id, status=wire.status.value,
                      amount=Decimal(wire.amount_minor) / 100, currency=wire.currency)
```

Domain code receives the gateway through a settings-driven factory or explicit parameter
([django-architecture dependency-management](../../django-architecture/references/dependency-management.md)); it
never imports `httpx`. Minor-unit exponents vary by currency — see
[payment-and-financial-integrations.md](payment-and-financial-integrations.md).

### Status handling reference

| Status | Meaning | Normalised to |
|---|---|---|
| 200/201/204 | Success — still check body for business status | return |
| 202 | Accepted, async processing; poll or await webhook | return + track job id |
| 3xx | Redirect not followed by default; usually misconfigured base URL | `PermanentIntegrationError` |
| 400/422 | Invalid request — bug or bad input | `PermanentIntegrationError` |
| 401/403 | Bad/expired credentials or missing scope | `AuthenticationError` (refresh token once if OAuth, then fail) |
| 404 | Missing resource — may be legitimate (lookup) | caller decides; often `None` |
| 409 | Conflict / idempotency-key reuse with different body (provider-specific) | `PermanentIntegrationError`, inspect |
| 408/425/429 | Timeout / too early / rate limited | `TransientIntegrationError` / `RateLimitedError` |
| 500/502/503/504 | Server or gateway failure | `TransientIntegrationError` |

### Retry-After and rate-limit headers

```python
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_retry_after(response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:                                           # HTTP-date form (RFC 9110 §10.2.3)
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
```

Proactive throttling headers vary: `X-RateLimit-Limit/Remaining/Reset` (de facto, provider-specific semantics:
reset may be epoch seconds or seconds-from-now), or the IETF `RateLimit`/`RateLimit-Policy` fields (draft, not
yet an RFC `VERIFY`). Feed `Remaining` into the shared limiter rather than reacting only to 429.

### Pagination

| Style | Request | Pitfall |
|---|---|---|
| Offset/limit | `?offset=200&limit=100` | Inserts/deletes during iteration skip or duplicate rows |
| Page number | `?page=3` | Same as offset |
| Cursor/token | `?cursor=abc` from previous response | Cursor expiry; must not be constructed client-side |
| Keyset | `?since_id=123` / `?updated_after=ts` | Ties on timestamp → use (timestamp, id) |
| Link header | `Link: <...>; rel="next"` (RFC 8288) | Follow the URL as given — but validate host |

```python
from collections.abc import Iterator


def iter_customers(*, updated_after: str, page_size: int = 100, max_pages: int = 1_000) -> Iterator[dict]:
    params = {"updated_after": updated_after, "limit": page_size}
    for _ in range(max_pages):                     # hard stop against cursor loops
        data = client.request("GET", "/v1/customers", operation="list_customers",
                              idempotent=True, params=params).json()
        yield from data["items"]
        cursor = data.get("next_cursor")
        if not cursor:
            return
        params = {"cursor": cursor, "limit": page_size}
    raise PermanentIntegrationError("pagination exceeded max_pages", provider="partnercrm",
                                    operation="list_customers")
```

Persist the cursor/watermark in `SyncCheckpoint` after each committed page for resumability
([data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)).

### Streaming responses and large payloads

```python
import hashlib
from django.core.files.storage import storages


def download_report(report_id: str, dest_name: str, *, max_bytes: int = 500 * 1024 * 1024) -> str:
    digest, total = hashlib.sha256(), 0
    with client.get_client().stream("GET", f"/v1/reports/{report_id}/file") as response:
        if response.status_code >= 400:
            response.read()                             # error bodies are small; needed to parse error code
        client._check(response, {"provider": "acmepay", "operation": "download_report"})
        with storages["private"].open(dest_name, "wb") as fh:   # VERIFY: write-mode open on remote backends
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise PermanentIntegrationError("payload too large", provider="acmepay",
                                                    operation="download_report")
                digest.update(chunk)
                fh.write(chunk)
    return digest.hexdigest()
```

- Never call `.json()` / `.content` on unbounded responses; stream and cap bytes.
- For large JSON arrays, prefer NDJSON or paginated endpoints; streaming JSON parsers (`ijson`
  `[third-party: ijson]`) are a fallback.
- Upload large bodies from a file object or generator (`content=iter_chunks()`), not a fully loaded `bytes`.
- Long downloads belong in tasks, not request/response cycles. Storage details:
  [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md).

### Async client

```python
import asyncio
import httpx

_SEM = asyncio.Semaphore(10)


async def fetch_quotes(skus: list[str]) -> dict[str, dict]:
    async with httpx.AsyncClient(base_url=settings.QUOTES_URL, timeout=httpx.Timeout(3.0, connect=1.0)) as ac:
        async def one(sku: str) -> tuple[str, dict]:
            async with _SEM:
                r = await ac.get(f"/quotes/{sku}")
                r.raise_for_status()
                return sku, r.json()
        results = await asyncio.gather(*(one(s) for s in skus), return_exceptions=True)
    return {sku: data for res in results if not isinstance(res, BaseException) for sku, data in [res]}
```

A per-call `AsyncClient` is acceptable for a fan-out inside one request; for sustained traffic keep one per
event loop. Never call a sync client from async code (blocks the loop) — use `sync_to_async` only as a stopgap.

### requests equivalent

```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

session = requests.Session()
session.mount("https://", HTTPAdapter(
    pool_connections=4, pool_maxsize=20,
    max_retries=Retry(total=3, connect=3, read=0, status=3, backoff_factor=0.5, backoff_jitter=0.3,
                      status_forcelist=(429, 502, 503, 504), allowed_methods={"GET", "PUT", "DELETE"},
                      respect_retry_after_header=True, raise_on_status=False),
))
response = session.get(url, timeout=(2.0, 8.0))   # always pass timeout
```

`backoff_jitter` requires urllib3 ≥ 2.0 `VERIFY`. `read=0` avoids replaying requests whose response was lost.

### API versioning (consumer side)

- Pin the provider version explicitly (header such as `AcmePay-Version`, URL `/v2/`, or account setting) —
  never rely on "latest". Provider-specific.
- Keep the version in one constant in `client.py`; upgrade via a branch that re-runs contract tests.
- Watch `Deprecation` (RFC 9745 `VERIFY`) and `Sunset` (RFC 8594) headers; log and alert on them.
- Upgrades: dual-read during migration (gateway supports old + new schema), feature-flag the switch.

### Wrapping provider SDKs

- Instantiate once per process, configure timeout and retries explicitly (SDK defaults vary: some retry
  POSTs, some have no timeout).
- Catch the SDK's exception hierarchy at the gateway boundary and translate into the shared taxonomy.
- Do not let SDK model objects escape into domain code or into `JSONField`s.
- Pin SDK versions; SDK upgrades can silently change API versions.

## 6. Production use cases

- **Exchange-rate lookup in a checkout view**: GET with 1 s total budget, cached 10 min in Redis, stale cache
  as fallback; failure degrades to last-known rate with a banner.
- **CRM contact sync**: nightly cursor-paginated GET in a Celery task, checkpoint per page, contract-validated
  rows, invalid rows sent to a failed-record table instead of aborting the run.
- **Shipping label creation**: POST with idempotency key from the shipment row, 202 + polling job id, label PDF
  streamed to private storage.
- **Address validation in a form**: sync call with 800 ms timeout; on timeout, accept the address and queue
  async re-validation rather than block the user.
- **Partner catalogue import (2 GB)**: streamed download, SHA-256 verified against the manifest, processed in
  chunks ([django-data-processing](../../django-data-processing/SKILL.md)).

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Thin httpx client + gateway | Full control of timeouts, errors, logging; easy MockTransport tests | You maintain pagination, auth refresh, signing |
| Official SDK wrapped | Less code, provider-maintained auth/signing | Opaque retries/timeouts, heavy deps, upgrade churn |
| Generated client (OpenAPI) | Typed, quick to start | Generated code often lacks timeouts/errors policy; regenerate churn |
| Synchronous call in request | Simple, immediate result | Couples user latency/availability to provider |
| Background call | Isolates user from provider failures | Eventual consistency, status UX needed |

## 8. Security and performance considerations

**Security**
- TLS verification always on (`verify=True` default); custom CA bundle via `verify=ssl_context`, never
  `verify=False`. Details in [integration-security.md](integration-security.md).
- Do not follow redirects to arbitrary hosts; when the URL is user- or tenant-supplied apply SSRF controls
  ([django-security-engineering ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md)).
- Credentials in headers set by the client/auth object, never in URLs (query strings land in proxy logs).
- Log path, status, duration and provider request id — never `Authorization`, bodies with PII, or full URLs
  with query strings.
- Cap response sizes; hostile or broken upstreams can return gigabytes.

**Performance**
- One pooled client per process; per-call `httpx.Client()` costs a TLS handshake each time.
- Size `max_connections` to worker concurrency; a pool smaller than thread count produces `PoolTimeout`
  under load (local saturation, not provider failure).
- Request compression (`Accept-Encoding: gzip` is default); HTTP/2 for many small concurrent calls to one host.
- Cache stable reference data with TTL ([django-performance caching](../../django-performance/references/caching-engineering.md)).

## 9. Common mistakes and anti-patterns

- `requests.get(url)` without `timeout=` — hangs a worker forever.
- `timeout=30` copied everywhere, larger than the web request's own deadline.
- Creating a client per call; or creating one at import time and sharing it across `fork()`.
- `raise_for_status()` then `response.json()["data"]["id"]` in a view — unhandled `KeyError`/`HTTPStatusError`
  becomes a 500 with no context.
- Treating 200 as success without reading a business status field.
- Treating a read timeout on a POST as failure and re-POSTing without an idempotency key.
- Returning provider dicts/SDK objects to views, serializers or templates.
- Logging `response.text` — provider error bodies often echo request data, including PII and card fragments.
- `extra="forbid"` in production models — a provider adding a field takes the integration down.
- Building next-page URLs manually instead of using the returned cursor/`Link`.
- Unbounded pagination loops (provider bug returns the same cursor forever).
- Async view calling `requests` — blocks the event loop.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `PoolTimeout` under load | Pool smaller than concurrency, or slow upstream holding connections | Pool metrics, `Limits`, worker threads |
| `ConnectTimeout` spikes | DNS, egress firewall/NAT exhaustion, provider outage | DNS timing, SNAT port usage, status page |
| `ReadTimeout` on some endpoints only | Heavy operation, timeout below provider p99 | Provider latency histogram per operation |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Corporate proxy CA, outdated certifi, missing intermediate | `openssl s_client -connect host:443 -showcerts` |
| Works locally, hangs in prod | Missing egress rule / proxy env vars (`HTTPS_PROXY`), no timeout | `trust_env`, network policy |
| Intermittent `RemoteProtocolError` | Server closed idle keep-alive connection | Lower `keepalive_expiry` below server idle timeout |
| Duplicate records at provider | Retried non-idempotent POST | Idempotency keys, retry config |
| Missing records in sync | Offset pagination during writes | Switch to cursor/keyset |
| `ContractViolationError` after a quiet deploy | Provider changed contract/version | Pinned version header, provider changelog |

Validation steps: unit tests with `httpx.MockTransport` covering every row of the status table, timeouts and
malformed bodies ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)); check that every
client has explicit timeouts (`grep -rn "requests\.\(get\|post\)" | grep -v timeout`); confirm per-provider
latency/error metrics exist ([integration-observability-and-operations.md](integration-observability-and-operations.md)).

## 11. Related references and skills

- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retry policy,
  idempotency keys, breakers, rate limiting.
- [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) — `httpx.Auth`
  implementations, OAuth token caching.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — gateway/adapter/ACL patterns.
- [integration-testing-and-contracts.md](integration-testing-and-contracts.md) — MockTransport, respx, Pact.
- [integration-security.md](integration-security.md) — TLS, SSRF summary, redaction.
- [django-performance async](../../django-performance/references/async-django-performance.md),
  [concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md).
- [django-core asynchronous-django](../../django-core/references/asynchronous-django.md),
  [settings-configuration](../../django-core/references/settings-configuration.md).
- [django-rest-framework api-versioning-and-documentation](../../django-rest-framework/references/api-versioning-and-documentation.md)
  (versioning your own API; this file covers consuming others').

## 12. Official documentation and external references

- HTTPX: <https://www.python-httpx.org/advanced/clients/>, <https://www.python-httpx.org/advanced/timeouts/>,
  <https://www.python-httpx.org/advanced/resource-limits/>, <https://www.python-httpx.org/advanced/transports/>,
  <https://www.python-httpx.org/advanced/event-hooks/>, <https://www.python-httpx.org/async/>,
  <https://www.python-httpx.org/exceptions/>
- Requests: <https://requests.readthedocs.io/en/latest/user/advanced/#timeouts>,
  <https://requests.readthedocs.io/en/latest/user/advanced/#transport-adapters>
- urllib3 Retry: <https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Retry>
- Pydantic: <https://docs.pydantic.dev/latest/concepts/models/>
- RFC 9110 HTTP Semantics (status codes, Retry-After): <https://www.rfc-editor.org/rfc/rfc9110>
- RFC 6585 (429 Too Many Requests): <https://www.rfc-editor.org/rfc/rfc6585>
- RFC 8288 Web Linking: <https://www.rfc-editor.org/rfc/rfc8288>
- RFC 8594 Sunset header: <https://www.rfc-editor.org/rfc/rfc8594>
- RFC 9457 Problem Details: <https://www.rfc-editor.org/rfc/rfc9457>
- IETF RateLimit header fields draft: <https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/> `VERIFY` status
- Django storages API: <https://docs.djangoproject.com/en/5.2/ref/files/storage/>
