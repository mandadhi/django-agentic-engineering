# Webhooks and Inbound Integrations

**Scope:** Receiving events pushed by external systems into Django (webhook endpoint design, signature and timestamp verification, replay and duplicate handling, persistence in a `WebhookEvent` inbox, asynchronous processing, ordering, failure and replay) and sending webhooks to your own customers (subscriptions, signing, delivery, retries, disabling). Broker-delivered events are owned by [messaging-and-event-driven-integrations](messaging-and-event-driven-integrations.md); the generic Inbox pattern by [integration-architecture-patterns](integration-architecture-patterns.md); DRF view mechanics by [django-rest-framework](../../django-rest-framework/SKILL.md).

## 1. Purpose and scope

A webhook is an HTTP callback: the provider decides when to call you, retries on its own schedule, may deliver duplicates and out of order, and expects a fast 2xx. The receiving endpoint is a public, unauthenticated-by-session URL that can trigger money movement or account changes — so it must be authenticated by signature, idempotent, and decoupled from processing.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Provider SDK verifiers (e.g. `stripe` [third-party: stripe], `svix` [third-party: svix]) are optional.

The canonical flow is **verify → persist → acknowledge → process**:

```text
Provider ──POST──▶ [edge: size limit, rate limit] ──▶ Django view
   verify signature on raw body + timestamp ──fail──▶ 400/401
   INSERT WebhookEvent (unique provider,event_id) ON CONFLICT → duplicate → 200
   transaction.on_commit(enqueue process task) ──▶ 200 (fast)
Worker: lock event → idempotent handler → mark processed | retry | failed (replayable)
```

## 2. Core concepts

- **Event id**: provider-assigned unique id per event (`evt_...`, `X-GitHub-Delivery`); the dedupe key. If absent, derive a key from a stable hash of (type, object id, object version) — never from receipt time.
- **At-least-once delivery**: providers retry until a 2xx (or until they give up after hours/days). Duplicates are normal, not exceptional.
- **No ordering guarantee**: `invoice.paid` can arrive before `invoice.created`. Most providers state this explicitly (Stripe does `VERIFY`).
- **Thin vs fat events**: thin events carry an id and type ("something changed, fetch it"); fat events carry the object snapshot. Thin events are order-safe (you fetch current state); fat events must be version-checked.
- **Acknowledgement semantics**: 2xx = "I have durably accepted it", not "I have processed it". Returning 2xx before persisting loses events on crash; returning 5xx after persisting causes harmless duplicates.
- **Delivery window and timeout**: providers wait a few seconds (commonly 5–30 s, provider-specific `VERIFY`) — slow endpoints are treated as failures and may be auto-disabled.
- **Verification**: HMAC over raw body (most common), asymmetric signatures, mTLS, or a shared token in a header (weakest). Signature must be computed over the exact raw bytes.
- **Replay attack**: an attacker re-sends a captured valid request. Mitigated by a signed timestamp within a tolerance window plus event-id dedupe.

### Provider differences (examples — verify against current docs)

| Provider | Signature | Timestamp/replay | Notes |
|---|---|---|---|
| Stripe | `Stripe-Signature: t=...,v1=...`; HMAC-SHA256 over `"{t}.{raw_body}"` | Yes, default tolerance 300 s in SDK `VERIFY` | Multiple `v1` values during secret rotation; use `stripe.Webhook.construct_event` |
| GitHub | `X-Hub-Signature-256: sha256=<hex>` HMAC of raw body | No timestamp; dedupe on `X-GitHub-Delivery` | Redeliveries reuse the delivery GUID `VERIFY` |
| Standard Webhooks / Svix | `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64>`; HMAC over `"{id}.{ts}.{body}"` | Yes | Secret is `whsec_` + base64 key; used by several SaaS providers |
| Microsoft Graph change notifications | `clientState` echo you set at subscription; optional encrypted resource data; validation handshake | Subscriptions expire and must be renewed | On subscription creation Graph POSTs `?validationToken=...`; respond 200 `text/plain` with the token within ~10 s `VERIFY` |
| Twilio | `X-Twilio-Signature`: HMAC-SHA1 over full URL + sorted form params | No | URL must match exactly what Twilio called (scheme/host behind proxies) |
| Azure Event Grid (webhook) | Validation handshake `SubscriptionValidationEvent`; auth via Entra token or query key | — | Return `validationResponse` code `VERIFY` |
| Slack | `X-Slack-Signature: v0=...` over `v0:{ts}:{body}` | Yes, `X-Slack-Request-Timestamp` | URL verification challenge on setup |

Rule: when the provider ships an official verifier, use it; when you implement one, copy the documented byte layout exactly and test with a captured real request.

## 3. Internal mechanics

### Why the raw body matters

`request.body` is the bytes Django read from the socket. DRF's `request.data`, `json.loads` + `json.dumps`, or middleware that rewrites bodies produce different bytes. Accessing `request.POST` or DRF `request.data` first can also make `request.body` unavailable (`RawPostDataException`) for some content types — read `request.body` first. Proxies that decompress, re-encode charset, or normalise line endings break signatures; configure them to pass bodies through.

For form-encoded providers (Twilio), the signature covers the parsed parameters plus the *public* URL; behind a TLS-terminating proxy `request.build_absolute_uri()` must reflect the public scheme/host (`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST` configured deliberately).

### Dedupe via database constraint

A `UniqueConstraint(provider, event_id)` plus `INSERT ... ON CONFLICT DO NOTHING` (Django: `bulk_create(..., ignore_conflicts=True)` or catching `IntegrityError` in a savepoint) is race-free across concurrent deliveries; a `filter().exists()` check is not.

### Processing state machine

```text
RECEIVED ──claim──▶ PROCESSING ──ok──▶ PROCESSED
    ▲                   │ transient error (attempts < max) ──▶ RECEIVED (retry scheduled)
    │                   │ permanent error / max attempts ───▶ FAILED (alert; replayable)
    └─────── replay ────┘ unknown type ──▶ IGNORED
```

A stuck `PROCESSING` (worker killed) is recovered by a sweeper that resets rows whose `locked_at` is older than the task time limit.

## 4. Engineering decisions and trade-offs

| Decision | Options | Recommendation |
|---|---|---|
| Process inline vs async | Inline in the request / persist + task | Persist + task; inline only for trivial, idempotent, sub-100 ms work with no external calls |
| Queue mechanism | Celery task / DB polling worker / broker | Celery `on_commit` when Celery exists; a DB-polling management command (`SELECT ... FOR UPDATE SKIP LOCKED`) is enough without Celery — the `WebhookEvent` table is the queue |
| Store full payload | Full / minimal / none | Full (redacted if needed) with retention: enables replay and debugging. Do not store card data or secrets |
| Thin vs fat handling | Trust payload / re-fetch | Re-fetch for state changes where ordering matters and API cost is acceptable; else version-check |
| Response on unknown type | 2xx / 4xx | 2xx and mark `IGNORED` — 4xx triggers pointless retries |
| Response on handler bug | n/a — not in request path | Async processing means provider never sees your bugs |
| One endpoint per provider vs generic | `/webhooks/<provider>/` | Per provider route, shared inbox model and processing framework |
| Ordering | Accept out-of-order / serialize per entity | Version/timestamp compare per entity; serialize per entity only when unavoidable |

## 5. Django implementation patterns

### Inbox model

```python
# apps/integrations/models.py
from django.db import models
from django.db.models.functions import Now

class WebhookEvent(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received"; PROCESSING = "processing"; PROCESSED = "processed"
        FAILED = "failed"; IGNORED = "ignored"

    provider = models.CharField(max_length=32)
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=128)
    payload = models.JSONField()
    headers = models.JSONField(default=dict)          # allow-listed headers only, no auth headers
    status = models.CharField(max_length=12, choices=Status, default=Status.RECEIVED)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    received_at = models.DateTimeField(db_default=Now())
    locked_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], name="webhook_event_uniq")]
        indexes = [models.Index(fields=["status", "received_at"], name="webhook_status_idx")]
```

### Generic HMAC verifier with timestamp and secret rotation

```python
# apps/integrations/webhooks/verify.py
import hashlib, hmac, time

class InvalidSignature(Exception): ...

def verify_hmac_sha256(*, body: bytes, signature_hex: str, timestamp: str,
                       secrets: list[str], tolerance: int = 300) -> None:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise InvalidSignature("bad timestamp") from exc
    if abs(time.time() - ts) > tolerance:
        raise InvalidSignature("timestamp outside tolerance")       # replay window
    signed = f"{ts}.".encode() + body
    for secret in secrets:                                          # [current, previous] during rotation
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature_hex):
            return
    raise InvalidSignature("signature mismatch")
```

### Receiver view

```python
# apps/payments/integrations/acmepay/webhook_views.py
import json
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.integrations.models import WebhookEvent
from apps.integrations.tasks import process_webhook_event
from apps.integrations.webhooks.verify import InvalidSignature, verify_hmac_sha256

KEEP_HEADERS = {"x-acme-event-type", "x-acme-delivery-attempt"}

@csrf_exempt            # authenticated by signature; endpoint uses no cookies/session
@require_POST
def acmepay_webhook(request):
    body = request.body                                             # raw bytes, read first
    try:
        verify_hmac_sha256(body=body, signature_hex=request.headers.get("X-Acme-Signature", ""),
                           timestamp=request.headers.get("X-Acme-Timestamp", ""),
                           secrets=settings.ACMEPAY_WEBHOOK_SECRETS)
    except InvalidSignature as exc:
        logger.warning("webhook.rejected", extra={"provider": "acmepay", "reason": str(exc)})
        return HttpResponse(status=400)
    try:
        event = json.loads(body)
        event_id, event_type = event["id"], event["type"]
    except (ValueError, KeyError):
        return HttpResponse(status=400)                             # signed but malformed: alert, don't retry
    try:
        with transaction.atomic():
            row = WebhookEvent.objects.create(
                provider="acmepay", event_id=event_id, event_type=event_type, payload=event,
                headers={k: v for k, v in request.headers.items() if k.lower() in KEEP_HEADERS})
            transaction.on_commit(lambda: process_webhook_event.delay(row.pk))
    except IntegrityError:
        pass                                                        # duplicate delivery: already stored
    return HttpResponse(status=200)
```

Notes:
- Exclude the URL from `LoginRequiredMiddleware` (Django ≥ 5.1: `@login_not_required`).
- Limit body size at the proxy and via `DATA_UPLOAD_MAX_MEMORY_SIZE`; rate-limit the route.
- If the DB is down, return 5xx — the provider will retry; that is the correct backpressure.
- Without Celery, drop the `on_commit` line and let a polling worker pick up `RECEIVED` rows.
- A DRF `APIView` works too, but disable parsers before verification (read `request.body`), use `authentication_classes = []`, `permission_classes = [AllowAny]`.

### Microsoft Graph validation handshake (enterprise example)

```python
@csrf_exempt
@require_POST
def graph_notifications(request):
    token = request.GET.get("validationToken")
    if token is not None:                       # subscription creation/renewal handshake
        return HttpResponse(token, content_type="text/plain", status=200)
    payload = json.loads(request.body)
    for n in payload.get("value", []):
        if not hmac.compare_digest(n.get("clientState", ""), settings.GRAPH_CLIENT_STATE):
            return HttpResponse(status=400)
    # store each notification keyed by (subscriptionId, resource, changeType, resourceData.id + version) ...
    return HttpResponse(status=202)
```

Graph subscriptions expire (max lifetime varies by resource `VERIFY`); schedule a renewal task well before `expirationDateTime`. Notifications are thin — fetch the changed message/file via Graph API with app credentials ([authentication-and-identity-integrations](authentication-and-identity-integrations.md)). Escaping: echo the token as plain text only; never render it into HTML.

### Processing task with claim, idempotent handler, failure recording

```python
# apps/integrations/tasks.py
from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.integrations.errors import PermanentIntegrationError, TransientIntegrationError
from apps.integrations.models import WebhookEvent
from apps.integrations.webhooks.registry import HANDLERS      # {(provider, type): callable}

MAX_ATTEMPTS = 8

@shared_task(bind=True, acks_late=True, max_retries=MAX_ATTEMPTS)
def process_webhook_event(self, event_pk: int) -> None:
    with transaction.atomic():
        event = (WebhookEvent.objects.select_for_update(skip_locked=True)
                 .filter(pk=event_pk, status=WebhookEvent.Status.RECEIVED).first())
        if event is None:
            return                                   # processed, ignored, or claimed by another worker
        event.status, event.locked_at = WebhookEvent.Status.PROCESSING, timezone.now()
        event.attempts = F("attempts") + 1
        event.save(update_fields=["status", "locked_at", "attempts"])

    handler = HANDLERS.get((event.provider, event.event_type))
    try:
        if handler is None:
            outcome = WebhookEvent.Status.IGNORED
        else:
            handler(event)                           # idempotent domain service, own transaction
            outcome = WebhookEvent.Status.PROCESSED
    except TransientIntegrationError as exc:
        _mark(event_pk, WebhookEvent.Status.RECEIVED, exc)
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries * 30, 3600))
    except Exception as exc:                         # permanent or bug: park for replay
        _mark(event_pk, WebhookEvent.Status.FAILED, exc)
        raise
    _mark(event_pk, outcome, None)

def _mark(pk: int, status: str, exc: Exception | None) -> None:
    WebhookEvent.objects.filter(pk=pk).update(
        status=status, last_error=repr(exc)[:2000] if exc else "",
        processed_at=timezone.now() if status in ("processed", "ignored") else None, locked_at=None)
```

When `self.retry` is exhausted Celery raises `MaxRetriesExceededError`; catch it or use a `on_failure` hook to set `FAILED`. Detailed retry policy: [integration-reliability-and-resilience](integration-reliability-and-resilience.md); task mechanics: [celery-and-background-integrations](celery-and-background-integrations.md).

### Idempotent, order-tolerant handler (payment example)

```python
# apps/payments/services/webhooks.py
def handle_payment_succeeded(event: WebhookEvent) -> None:
    obj = event.payload["data"]["object"]
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(external_id=obj["id"])
        if payment.provider_updated_at and payment.provider_updated_at >= obj["updated_at"]:
            return                                    # stale or duplicate event
        if payment.status in {Payment.Status.SUCCEEDED, Payment.Status.REFUNDED}:
            return                                    # terminal; never regress
        payment.mark_succeeded(provider_updated_at=obj["updated_at"])
        transaction.on_commit(lambda: send_receipt.delay(payment.pk))
```

Guarding transitions with a state machine rather than "set status = payload status" prevents a late `payment.processing` from overwriting `succeeded`. Payment specifics: [payment-and-financial-integrations](payment-and-financial-integrations.md).

### Notification-provider example (delivery status callbacks)

Email/SMS providers post `delivered`, `bounced`, `complained` events, often batched (one POST, many events). Store each event individually keyed by the provider's event id or `(message_id, event, timestamp)`; on `bounced`/`complained`, suppress the address. See [email-sms-and-notification-integrations](email-sms-and-notification-integrations.md).

### Sweeper and replay

```python
# apps/integrations/management/commands/replay_webhooks.py
class Command(BaseCommand):
    help = "Re-queue FAILED (or stuck PROCESSING) webhook events"

    def add_arguments(self, parser):
        parser.add_argument("--provider"); parser.add_argument("--type")
        parser.add_argument("--since", type=datetime.fromisoformat)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = WebhookEvent.objects.filter(status=WebhookEvent.Status.FAILED)
        # filters...
        ids = list(qs.values_list("pk", flat=True)[:1000])
        if not opts["dry_run"]:
            WebhookEvent.objects.filter(pk__in=ids).update(status=WebhookEvent.Status.RECEIVED, last_error="")
            for pk in ids:
                process_webhook_event.delay(pk)
        self.stdout.write(f"{len(ids)} events re-queued")
```

A periodic task resets `PROCESSING` rows with `locked_at < now - task_time_limit`. The `FAILED` status *is* the dead-letter queue: alert on its growth, fix the handler, replay. For missed events (endpoint down beyond the provider's retry window), reconcile by listing events/objects from the provider API since the last checkpoint ([data-synchronization-and-reconciliation](data-synchronization-and-reconciliation.md)).

### Outbound webhooks (you notify customers)

```python
class WebhookSubscription(models.Model):
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    url = models.URLField(max_length=500)
    secret = EncryptedTextField()                         # shown once to the customer
    event_types = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

class WebhookDelivery(models.Model):
    subscription = models.ForeignKey(WebhookSubscription, on_delete=models.CASCADE)
    event_id = models.UUIDField()                         # stable across retries → consumer dedupe
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    attempt = models.PositiveSmallIntegerField(default=0)
    status_code = models.PositiveSmallIntegerField(null=True)
    duration_ms = models.PositiveIntegerField(null=True)
    next_attempt_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["subscription", "event_id"], name="uniq_delivery")]
```

Delivery rules:
- Create deliveries from an outbox in the same transaction as the domain change ([integration-architecture-patterns](integration-architecture-patterns.md)).
- Sign: `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64 HMAC(secret, id.ts.body)>` (Standard Webhooks layout) and document verification for consumers.
- Validate URL at subscription and at each delivery (https, public IP); `follow_redirects=False`; timeout ≤ 10 s; read at most a few KB of the response ([integration-security](integration-security.md)).
- Retry with exponential backoff and jitter over ~24–72 h; 2xx = success; 410 Gone = disable immediately; other 4xx/5xx/timeouts = retry.
- Auto-disable after sustained failure, notify the account owner, provide a "resend" UI and a delivery log.
- Per-subscription concurrency limits so one slow customer cannot starve deliveries to others (dedicated queue or rate limiter).
- Event ordering is not guaranteed to consumers; include `occurred_at` and resource version.

## 6. Production use cases

- **Payments**: `payment.succeeded` marks orders paid; `charge.refunded` and `dispute.created` update ledgers; missed events recovered by nightly reconciliation against the provider's list API.
- **Notifications**: SMS delivery receipts update message status; bounces populate a suppression list.
- **Enterprise**: Microsoft Graph notifications for new mail in a shared mailbox trigger document ingestion; Event Grid notifies of blob uploads; a CRM posts contact updates that feed data sync.
- **SaaS platform**: customers subscribe to `order.*` events; deliveries signed, logged, retried, auto-disabled after 3 days of failure.
- **Incident**: deploy broke the handler → events accumulate as `FAILED` while the endpoint still returns 200 → fix, replay; no provider-side data loss because acknowledgement was decoupled from processing.

## 7. Advantages and disadvantages

| Aspect | Advantages | Disadvantages |
|---|---|---|
| Webhooks vs polling | Near real-time, no wasted calls | Public endpoint, provider-controlled retries, missed events if down too long |
| Persist-then-process | Fast ack, replay, audit, handler bugs invisible to provider | Storage growth, extra moving parts, eventual consistency |
| Thin events + re-fetch | Ordering-safe, always current | Extra API calls, rate limits, provider API dependency |
| Fat events | No extra calls | Ordering and staleness handling required |
| Outbound webhooks | Simple for consumers | You own delivery infrastructure, SSRF risk, support burden |

## 8. Security and performance considerations

- Signature over raw body with `hmac.compare_digest`; timestamp tolerance ≤ 5 min; dedupe by event id defeats replays inside the window.
- No session/cookie auth and no CSRF on the endpoint; `csrf_exempt` is safe only because nothing cookie-based authorises it.
- Secrets per endpoint/provider, from the secret manager; support two during rotation.
- Unguessable path segments are not authentication; IP allow-lists are an additional layer only.
- Do not log full payloads at INFO; redact stored payloads where they contain sensitive data.
- Endpoint latency target: < 200 ms p99 (one INSERT + enqueue). Monitor; slow endpoints get disabled by providers.
- Bursts (provider backlog replays after an outage) can be 100× normal; the inbox absorbs them, the worker queue drains at a controlled rate.
- Inbox table growth: index on `(status, received_at)`; prune `PROCESSED` after a retention period; consider partitioning by month at high volume ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).
- Outbound: SSRF protections, per-subscription rate limits, response size caps.

## 9. Common mistakes and anti-patterns

- Verifying `json.dumps(request.data)` instead of `request.body`.
- Doing the business work in the request, then timing out → provider retries → duplicate processing.
- Returning 2xx before the event is durably stored.
- Dedupe with `exists()` then `create()` (race) instead of a unique constraint.
- Returning 4xx/5xx for unknown event types or handler bugs, causing retry storms and auto-disable.
- `if status == payload.status: save` without transition rules → regressions from out-of-order events.
- Trusting payload amounts/state for critical actions when the event is thin or could be stale — re-fetch.
- Single `FAILED` state with no alert or replay tooling (silent dead letters).
- Forgetting Graph/Event Grid validation handshakes, or renewing subscriptions too late.
- Outbound: following redirects, no timeout, sending to private IPs, unbounded retries, no per-customer isolation.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Action |
|---|---|---|
| All signatures fail after deploy | Secret not loaded / rotated; middleware altered body; proxy decompressing | Check secret version; compare `len(request.body)` with `Content-Length`; capture raw request in staging |
| Twilio-style signatures fail only in prod | URL mismatch (http vs https, host) behind proxy | Verify `SECURE_PROXY_SSL_HEADER`/host config; log computed URL (not secret) |
| Signatures fail intermittently | Clock skew beyond tolerance | NTP; log timestamp delta metric |
| Duplicate side effects | Handler not idempotent; dedupe key wrong | Unique constraint present? Handler guarded by state? |
| Provider dashboard shows timeouts | Inline processing, DB locks, slow enqueue | Profile endpoint; move work to task |
| `FAILED` growing | Handler bug or provider API failing | Inspect `last_error`; fix; `replay_webhooks` |
| Events missing entirely | Endpoint disabled by provider, DNS/WAF block, subscription expired | Provider delivery logs; reconcile via list API |

Validation (see [integration-testing-and-contracts](integration-testing-and-contracts.md) and [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md)):
- Valid signature → row stored, task scheduled (`captureOnCommitCallbacks`), 200.
- Invalid signature, stale timestamp, modified body, missing header → 400, nothing stored.
- Same event twice (sequentially and concurrently) → one row, one side effect.
- Out-of-order events → final state correct.
- Handler transient error → retried; permanent → `FAILED`; replay command reprocesses.
- Outbound: signature matches documented algorithm; private/redirect targets refused; disable after threshold.

## 11. Related references and skills

- [integration-security](integration-security.md) — verification, SSRF for outbound webhooks, redaction.
- [integration-reliability-and-resilience](integration-reliability-and-resilience.md) — retry/backoff, idempotency keys.
- [integration-architecture-patterns](integration-architecture-patterns.md) — Inbox and Outbox patterns.
- [celery-and-background-integrations](celery-and-background-integrations.md) — `on_commit` dispatch, task retries.
- [payment-and-financial-integrations](payment-and-financial-integrations.md), [email-sms-and-notification-integrations](email-sms-and-notification-integrations.md), [azure-integrations](azure-integrations.md) — domain-specific events.
- [integration-observability-and-operations](integration-observability-and-operations.md) — webhook backlog and failure alerts.
- django-security-engineering: [ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md), [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md).
- django-core: [signals-management-commands](../../django-core/references/signals-management-commands.md) (replay command mechanics).
- django-testing: [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md), [database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md).

## 12. Official documentation and external references

- Django CSRF exemption — https://docs.djangoproject.com/en/5.2/ref/csrf/#django.views.decorators.csrf.csrf_exempt
- Django `HttpRequest.body` — https://docs.djangoproject.com/en/5.2/ref/request-response/#django.http.HttpRequest.body
- Django `transaction.on_commit` — https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Django `login_not_required` — https://docs.djangoproject.com/en/5.2/topics/auth/default/#django.contrib.auth.decorators.login_not_required
- Python `hmac.compare_digest` — https://docs.python.org/3/library/hmac.html#hmac.compare_digest
- Standard Webhooks specification — https://www.standardwebhooks.com/ ; https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
- Stripe webhooks — https://docs.stripe.com/webhooks
- GitHub validating deliveries — https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- Microsoft Graph change notifications — https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks
- Azure Event Grid webhook validation — https://learn.microsoft.com/en-us/azure/event-grid/end-point-validation-cloud-events-schema `VERIFY` (CloudEvents vs Event Grid schema pages differ)
- Twilio webhook security — https://www.twilio.com/docs/usage/webhooks/webhooks-security
- Slack request verification — https://api.slack.com/authentication/verifying-requests-from-slack
- Celery tasks and retries — https://docs.celeryq.dev/en/stable/userguide/tasks.html
- PostgreSQL `INSERT ... ON CONFLICT` — https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT
