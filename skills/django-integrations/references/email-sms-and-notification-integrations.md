# Email, SMS and Notification Integrations

**Scope:** Owns how Django sends transactional email, SMS and push notifications through external providers: the provider abstraction, idempotent notification records, templates, delivery/bounce/complaint tracking, suppression, retries and failover. It does not own webhook signature mechanics ([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md)), retry theory ([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)) or Django's email API basics ([django-core](../../django-core/SKILL.md)).

## 1. Purpose and scope

Notifications look trivial (`send_mail(...)`) and fail in production in predictable ways: a slow SMTP server blocks a request worker, a retried task sends the same password-reset email three times, a hard-bounced address keeps being mailed until the provider suspends the account, an SMS goes to a number that replied STOP. This reference gives an agent the engineering model to avoid those failures.

Covered:

- Channels: email (SMTP and HTTP APIs), SMS gateways, mobile/web push (concept level).
- A `NotificationProvider` port with per-channel adapters and optional failover.
- Notification records as the unit of idempotency, auditing and delivery tracking.
- Template ownership and rendering, localisation, and preview.
- Delivery status, bounce, complaint and opt-out events from provider webhooks.
- Suppression lists and consent.
- Queueing, retry, rate limits and provider failover.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Provider names (SES, SendGrid, Postmark, Mailgun, Twilio, Vonage, FCM, APNs) appear only as provider-specific examples; generic examples use "NotifyHub".

## 2. Core concepts

| Concept | Meaning | Why it matters |
|---|---|---|
| Transactional vs marketing | Transactional: triggered by a user action (receipt, reset). Marketing: bulk, consent-based. | Different legal basis, sending domains/IP pools, unsubscribe requirements. Keep them on separate streams. |
| Accepted vs delivered | Provider API returned 2xx = *accepted for delivery*. Delivery is a later, asynchronous event. | A 2xx is not business success. Track status from events. |
| Hard bounce | Permanent failure (mailbox doesn't exist). | Suppress the address immediately; repeated hard bounces damage sender reputation. |
| Soft bounce | Temporary failure (mailbox full, greylisting). | Provider usually retries; suppress only after repeated soft bounces. |
| Complaint | Recipient marked the message as spam (feedback loop). | Suppress for that stream; complaint rates above provider thresholds trigger suspension (`VERIFY` per provider). |
| Suppression list | Addresses/numbers you must not send to. | Exists at the provider *and* should exist locally so failover providers respect it. |
| Opt-out (SMS) | STOP/UNSUBSCRIBE keywords; carrier and regulatory rules. | Some gateways enforce opt-out automatically (provider-specific); your app must still record it. |
| E.164 | `+<country><number>`, max 15 digits. | Canonical phone format for storage and SMS APIs. |
| Push token | Device registration token (FCM) or device token (APNs). | Tokens expire/rotate; invalid-token responses must delete the token. |
| SPF / DKIM / DMARC | DNS-based sender authentication. | Without them, delivery to major mailbox providers degrades or fails. Configured outside Django. |

**Notification idempotency.** The business event ("order 812 shipped") must produce at most one notification per recipient per channel, even if the triggering task runs twice. The idempotency key belongs to *your* record, not the provider call.

## 3. Internal mechanics

### Django email stack

- `django.core.mail.send_mail` / `EmailMessage.send()` call the backend from `EMAIL_BACKEND`.
- `smtp.EmailBackend` opens an SMTP connection (`smtplib`), optionally STARTTLS (`EMAIL_USE_TLS`) or implicit TLS (`EMAIL_USE_SSL`, mutually exclusive), sends, closes. `EMAIL_TIMEOUT` defaults to `None` — **blocking forever** on a hung server. Always set it.
- `get_connection()` lets you reuse one connection for many messages (`connection.send_messages([...])`), avoiding a TLS handshake per email.
- `locmem.EmailBackend` stores messages in `django.core.mail.outbox` — what Django's test runner uses.
- `console`/`filebased` backends are for development only.
- `fail_silently=True` swallows errors — never use it in integration code; you lose the failure signal.

### HTTP email APIs

Providers expose REST APIs that accept JSON, return a provider message ID, and later emit events (delivered, bounced, opened, clicked, complained) via webhooks. `django-anymail` `[third-party: django-anymail]` implements Django `EmailBackend`s for many of them and normalises their webhook events into `anymail.signals.tracking` and inbound signals, which keeps the Django email API as the port for email-only needs.

### SMS

SMS APIs accept a destination (E.164), sender (short code, long number, alphanumeric sender ID — country-dependent), and body. Bodies are split into segments (GSM-7: 160 chars single, 153 per segment when concatenated; UCS-2 for non-GSM characters: 70/67) — cost and limits are per segment `VERIFY` per gateway. Delivery receipts (DLRs) arrive via webhook and are carrier-dependent: some carriers never return final status.

### Push

- FCM HTTP v1 API: OAuth 2.0 service-account token (not a static server key — the legacy API was retired `VERIFY`), per-message send; response errors such as `UNREGISTERED` mean delete the token.
- APNs: HTTP/2 with token-based (JWT, `.p8` key) or certificate auth; `410 Unregistered` means delete the device token.
- Payload size limits are small (≈4 KB) `VERIFY`. Treat push as best-effort; never the sole channel for critical information.

### Flow

```text
domain service ──(on_commit)──► Notification row (PENDING, unique key)
                                      │ Celery task send_notification(id)
                                      ▼
                         suppression check ─► SUPPRESSED (stop)
                                      │
                           provider.send(...)  ─► ACCEPTED (provider_message_id)
                                      │ webhook events
                                      ▼
                    DELIVERED | BOUNCED | COMPLAINED | FAILED  ─► update suppression
```

## 4. Engineering decisions and trade-offs

| Decision | Options | Prefer | Trade-off |
|---|---|---|---|
| Email transport | SMTP relay vs provider HTTP API | HTTP API via anymail (or provider SDK) for transactional at scale | SMTP is portable and provider-agnostic but gives less status detail and slower bulk; API couples to provider (anymail mitigates). |
| Send timing | Inline in request vs background task | Background task dispatched `on_commit` | Inline is simpler and surfaces errors to the user, but blocks a worker and loses the email on timeout. For low-volume admin tools inline with a short `EMAIL_TIMEOUT` is acceptable. |
| Is Celery required? | Celery, Django tasks (Django ≥ 6.0 `django.tasks` with a third-party backend `VERIFY`), DB-polling outbox worker | Whatever background runner the project already has | Do not add Celery only for email; a DB-backed outbox drained by a management command also works. |
| Abstraction | Django EmailBackend vs custom `NotificationProvider` port | EmailBackend for email-only; port when multiple channels, failover or per-tenant providers | A custom port adds code; EmailBackend cannot model SMS/push or provider-specific status. |
| Failover | Single provider vs active-passive vs active-active | Single provider + good alerting; active-passive for critical OTP/SMS | Failover doubles template/domain/suppression management; can send duplicates when the primary timed out after accepting. |
| Templates | Django templates in repo vs provider-hosted templates | Repo templates (versioned, reviewable, testable) | Provider templates let non-engineers edit but lose versioning and break silently on variable renames. |
| Tracking | Opens/clicks vs delivery only | Delivery/bounce/complaint always; opens/clicks only with consent and purpose | Open tracking is unreliable (privacy proxies prefetch images) and has privacy implications. |

**Failover and duplicates.** A timeout from provider A does not prove A did not send. Failing over to B after an *ambiguous* outcome can duplicate the message. For OTP codes a duplicate is acceptable; for "your payment was taken" it is confusing; for marketing it may breach consent. Fail over only on errors that prove non-acceptance (connection refused, 5xx before acceptance, explicit rejection) unless duplicates are acceptable for the notification type.

## 5. Django implementation patterns

### Settings

```python
# settings/base.py
import os

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")   # from secret store at runtime
EMAIL_USE_TLS = True
EMAIL_TIMEOUT = 10                     # seconds; default None blocks forever
DEFAULT_FROM_EMAIL = "Example <no-reply@mail.example.com>"
SERVER_EMAIL = "alerts@mail.example.com"

# With django-anymail [third-party: django-anymail]
# EMAIL_BACKEND = "anymail.backends.amazon_ses.EmailBackend"
# ANYMAIL = {"WEBHOOK_SECRET": os.environ["ANYMAIL_WEBHOOK_SECRET"]}
```

### Notification record

```python
# apps/notifications/models.py
from django.db import models
from django.db.models.functions import Now


class Notification(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "email"
        SMS = "sms"
        PUSH = "push"

    class Status(models.TextChoices):
        PENDING = "pending"
        SUPPRESSED = "suppressed"
        ACCEPTED = "accepted"          # provider accepted; not yet delivered
        DELIVERED = "delivered"
        BOUNCED = "bounced"
        COMPLAINED = "complained"
        FAILED = "failed"

    idempotency_key = models.CharField(max_length=200)    # e.g. "order-shipped:812:user:55:email"
    channel = models.CharField(max_length=10, choices=Channel)
    recipient = models.CharField(max_length=320)          # email or E.164
    template = models.CharField(max_length=100)
    context = models.JSONField(default=dict)              # minimal; no secrets
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    provider = models.CharField(max_length=40, blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="notification_idem_uniq"),
            models.UniqueConstraint(
                fields=["provider", "provider_message_id"],
                condition=~models.Q(provider_message_id=""),
                name="notification_provider_msg_uniq",
            ),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="notification_status_idx")]


class Suppression(models.Model):
    class Reason(models.TextChoices):
        HARD_BOUNCE = "hard_bounce"
        COMPLAINT = "complaint"
        OPT_OUT = "opt_out"
        MANUAL = "manual"

    channel = models.CharField(max_length=10, choices=Notification.Channel)
    recipient = models.CharField(max_length=320)          # normalised (lower-cased email, E.164)
    reason = models.CharField(max_length=20, choices=Reason)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["channel", "recipient"], name="suppression_uniq"),
        ]
```

Store a *minimal* context (IDs, not rendered PII) so you can re-render and so the table is not a PII archive; define a retention policy.

### Enqueue from a domain service

```python
# apps/notifications/services.py
from django.db import IntegrityError, transaction

from .models import Notification
from .tasks import send_notification


def notify(*, key: str, channel: str, recipient: str, template: str, context: dict) -> None:
    """Idempotent: the same key never creates a second notification."""
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                idempotency_key=key, channel=channel, recipient=normalise(channel, recipient),
                template=template, context=context,
            )
    except IntegrityError:
        return                                            # already requested
    transaction.on_commit(lambda: send_notification.delay(notification.pk))
```

Called inside the caller's transaction: the notification row commits with the business change, and the task only runs after commit. If the broker is down at commit time the row stays `PENDING`; a periodic sweeper re-enqueues old `PENDING` rows (a lightweight outbox — see [integration-architecture-patterns.md](integration-architecture-patterns.md)).

### Provider port and adapters

```python
# apps/notifications/providers/base.py
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    recipient: str
    subject: str | None
    text: str
    html: str | None
    reference: str          # our idempotency key; passed to provider as metadata/tag


@dataclass(frozen=True)
class SendResult:
    provider: str
    provider_message_id: str


class NotificationProvider(Protocol):
    name: str
    def send(self, message: OutboundMessage) -> SendResult: ...
```

```python
# apps/notifications/providers/django_email.py
from django.core.mail import EmailMultiAlternatives, get_connection

from apps.integrations.errors import TransientIntegrationError, PermanentIntegrationError
import smtplib, socket


class DjangoEmailProvider:
    name = "django-email"

    def send(self, message: OutboundMessage) -> SendResult:
        email = EmailMultiAlternatives(
            subject=message.subject or "", body=message.text, to=[message.recipient],
            headers={"X-Notification-Ref": message.reference},
        )
        if message.html:
            email.attach_alternative(message.html, "text/html")
        try:
            with get_connection() as connection:          # honours EMAIL_TIMEOUT
                email.connection = connection
                email.send(fail_silently=False)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentIntegrationError(provider=self.name, operation="send") from exc
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout) as exc:
            raise TransientIntegrationError(provider=self.name, operation="send") from exc
        # With anymail, email.anymail_status.message_id holds the provider ID.
        message_id = getattr(getattr(email, "anymail_status", None), "message_id", "") or ""
        return SendResult(provider=self.name, provider_message_id=message_id)
```

```python
# apps/notifications/providers/notifyhub_sms.py — generic HTTP SMS gateway
from apps.integrations.http import build_client
from apps.integrations.errors import (
    PermanentIntegrationError, RateLimitedError, TransientIntegrationError, AmbiguousOutcomeError,
)
import httpx
from django.conf import settings

_client = build_client(
    "notifyhub", base_url=settings.NOTIFYHUB_BASE_URL,
    timeout=httpx.Timeout(5.0, connect=2.0),
    auth=("api", settings.NOTIFYHUB_API_KEY),
)


class NotifyHubSmsProvider:
    name = "notifyhub"

    def send(self, message: OutboundMessage) -> SendResult:
        try:
            response = _client.post(
                "/v1/sms",
                json={"to": message.recipient, "body": message.text, "client_ref": message.reference},
                headers={"Idempotency-Key": message.reference},   # if the gateway supports it
            )
        except httpx.ConnectError as exc:
            raise TransientIntegrationError(provider=self.name, operation="sms.send") from exc
        except httpx.TimeoutException as exc:
            raise AmbiguousOutcomeError(provider=self.name, operation="sms.send") from exc
        if response.status_code == 429:
            raise RateLimitedError(provider=self.name, operation="sms.send",
                                   retry_after=float(response.headers.get("Retry-After", "5")))
        if response.status_code >= 500:
            raise TransientIntegrationError(provider=self.name, operation="sms.send",
                                            status_code=response.status_code)
        if response.status_code >= 400:
            raise PermanentIntegrationError(provider=self.name, operation="sms.send",
                                            status_code=response.status_code)
        return SendResult(provider=self.name, provider_message_id=response.json()["id"])
```

### Send task with suppression and controlled failover

```python
# apps/notifications/tasks.py
from celery import shared_task
from django.db import transaction

from apps.integrations.errors import (
    AmbiguousOutcomeError, PermanentIntegrationError, RateLimitedError, TransientIntegrationError,
)
from .models import Notification, Suppression
from .providers.registry import providers_for          # ordered [primary, secondary]
from .rendering import render


@shared_task(bind=True, max_retries=6, acks_late=True)
def send_notification(self, notification_id: int) -> None:
    with transaction.atomic():
        n = Notification.objects.select_for_update().get(pk=notification_id)
        if n.status != Notification.Status.PENDING:
            return                                         # already handled — idempotent
        if Suppression.objects.filter(channel=n.channel, recipient=n.recipient).exists():
            n.status = Notification.Status.SUPPRESSED
            n.save(update_fields=["status", "updated_at"])
            return
        n.attempts += 1
        n.save(update_fields=["attempts", "updated_at"])

    message = render(n)                                    # pure; no I/O
    chain = providers_for(n.channel)
    for index, provider in enumerate(chain):
        try:
            result = provider.send(message)
        except RateLimitedError as exc:
            raise self.retry(countdown=exc.retry_after)
        except AmbiguousOutcomeError:
            # Provider may have accepted. Do NOT fail over (duplicate risk); retry same provider
            # with the same reference/idempotency key.
            raise self.retry(countdown=30)
        except TransientIntegrationError as exc:
            if index + 1 < len(chain):
                continue                                   # proven non-acceptance → fail over
            raise self.retry(exc=exc, countdown=min(60 * 2 ** self.request.retries, 3600))
        except PermanentIntegrationError as exc:
            Notification.objects.filter(pk=n.pk).update(
                status=Notification.Status.FAILED, last_error=type(exc).__name__)
            return
        Notification.objects.filter(pk=n.pk, status=Notification.Status.PENDING).update(
            status=Notification.Status.ACCEPTED,
            provider=result.provider, provider_message_id=result.provider_message_id,
        )
        return
```

Note the provider call is outside the row lock transaction: never hold a DB lock or transaction open across network I/O.

### Templates

```text
templates/notifications/
  order_shipped/
    subject.txt
    body.txt
    body.html
    sms.txt
```

```python
# apps/notifications/rendering.py
from django.template.loader import render_to_string
from django.utils import translation


def render(n: Notification) -> OutboundMessage:
    ctx = build_context(n.template, n.context)            # loads fresh domain data by ID
    base = f"notifications/{n.template}"
    with translation.override(ctx.get("language", "en")):
        if n.channel == Notification.Channel.SMS:
            return OutboundMessage(n.channel, n.recipient, None,
                                   render_to_string(f"{base}/sms.txt", ctx).strip(), None,
                                   n.idempotency_key)
        return OutboundMessage(
            n.channel, n.recipient,
            render_to_string(f"{base}/subject.txt", ctx).strip().replace("\n", " "),
            render_to_string(f"{base}/body.txt", ctx),
            render_to_string(f"{base}/body.html", ctx),
            n.idempotency_key,
        )
```

Strip newlines from subjects (Django raises `BadHeaderError` on newlines in headers — header injection protection). Keep HTML email CSS inline; mail clients ignore `<style>` inconsistently.

### Delivery, bounce and complaint events

Receive provider events with the webhook inbox pattern ([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md)): verify signature over raw body, store `WebhookEvent`, process asynchronously. With anymail, connect to its normalised signal:

```python
# apps/notifications/receivers.py
from anymail.signals import tracking           # [third-party: django-anymail]
from django.dispatch import receiver

STATUS_MAP = {
    "delivered": Notification.Status.DELIVERED,
    "bounced": Notification.Status.BOUNCED,
    "complained": Notification.Status.COMPLAINED,
    "rejected": Notification.Status.FAILED,
}
TERMINAL = {Notification.Status.BOUNCED, Notification.Status.COMPLAINED, Notification.Status.FAILED}


@receiver(tracking)
def on_tracking(sender, event, esp_name, **kwargs) -> None:
    new_status = STATUS_MAP.get(event.event_type)
    if new_status is None:
        return                                     # opened/clicked/deferred: record only if needed
    updated = (Notification.objects
               .filter(provider_message_id=event.message_id)
               .exclude(status__in=TERMINAL)       # out-of-order: delivered after bounced
               .update(status=new_status))
    # Anymail normalises "bounced" as a permanent failure and "deferred" as temporary
    # (mapping from each ESP's raw events is anymail's — VERIFY per ESP in its docs).
    if new_status in {Notification.Status.BOUNCED, Notification.Status.COMPLAINED}:
        Suppression.objects.get_or_create(
            channel="email", recipient=event.recipient.lower(),
            defaults={"reason": "complaint" if event.event_type == "complained" else "hard_bounce"},
        )
```

Event semantics differ per provider (soft vs hard bounce classification, whether "delivered" exists); map them explicitly per provider and test the mapping with recorded payloads. Anymail runs signal receivers synchronously in the webhook request — keep them fast or enqueue a task.

### SMS opt-out

Handle inbound SMS (STOP, START, HELP) via the gateway's inbound webhook: normalise the keyword, insert/delete `Suppression(channel="sms", reason="opt_out")`, and keep an audit row with timestamp. Some gateways auto-handle STOP on long codes in some countries `VERIFY` — still mirror it locally so a failover gateway respects it.

### Phone normalisation

```python
import phonenumbers                       # [third-party: phonenumbers]

def to_e164(raw: str, default_region: str = "GB") -> str:
    number = phonenumbers.parse(raw, default_region)
    if not phonenumbers.is_valid_number(number):
        raise ValueError("invalid phone number")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
```

`django-phonenumber-field` `[third-party: django-phonenumber-field]` provides a model/form field built on the same library.

## 6. Production use cases

1. **Password reset email.** Inline-sending blocked workers during an SMTP outage. Fix: `EMAIL_TIMEOUT=10`, enqueue on commit, idempotency key `reset:{token_id}` so a double-click doesn't send two links. Reset links stay valid only per Django's token rules ([django-security-engineering](../../django-security-engineering/references/authentication-security.md)).
2. **Order shipped notification across channels.** One domain event → three `Notification` rows (email, SMS, push), each keyed `order-shipped:{order}:{user}:{channel}`. A retry of the event handler creates nothing new.
3. **OTP over SMS with failover.** Primary gateway returns connection errors in one country → fail over to secondary; ambiguous timeouts retry the primary because duplicate OTPs are harmless but resending a *different* code invalidates the first — reuse the same code within its validity window.
4. **Bounce storm after an import.** A CSV import created 20 000 users with stale addresses; hard bounces exceed the provider's threshold and the account is paused. Prevention: validate syntax at import, send a double-opt-in confirmation before any transactional stream, cap per-batch sends, alert on bounce rate.
5. **Per-tenant sending domains.** Multi-tenant SaaS sends from each customer's domain: a registry maps tenant → provider credentials and verified domain; credentials live in a secret store, referenced by name, never in the DB in plaintext.
6. **Enterprise relay.** A customer mandates their SMTP relay over TLS with client certificate: SMTP backend with `EMAIL_SSL_CERTFILE`/`EMAIL_SSL_KEYFILE` and a dedicated connection per tenant.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Django `send_mail` inline | Zero infrastructure; errors visible immediately | Blocks request; lost on failure; no idempotency or status |
| Django EmailBackend + anymail | Standard API; swap providers by setting; normalised webhooks | Email only; extra dependency; feature set is lowest common denominator unless using `esp_extra` |
| Custom `NotificationProvider` port | Multi-channel, failover, per-tenant routing, provider IDs captured | More code to own; must keep adapters and status mappings tested |
| Provider SDKs directly | Full feature access | SDK types leak into domain code unless wrapped; SDK default timeouts/retries vary |
| Provider-hosted templates | Non-engineers edit copy | Unversioned; variable drift breaks silently; harder to test |
| Active-passive failover | Survives provider outage | Duplicate risk, double configuration (DNS/DKIM, sender IDs), suppression sync |

## 8. Security and performance considerations

**Security**

- Credentials (SMTP password, API keys, FCM service account, APNs `.p8`) from a secret store; rotate; one per environment. See [integration-security.md](integration-security.md) and [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md).
- Header injection: never put user input in headers without Django's checks; Django rejects newlines in headers.
- HTML injection: templates auto-escape; never `|safe` user-supplied content in HTML email.
- Delivery/bounce webhooks are unauthenticated endpoints unless you verify signatures (anymail `WEBHOOK_SECRET` basic auth plus provider signature validation where supported) — otherwise attackers can suppress any address by posting fake bounces.
- Do not embed long-lived auth tokens in email links without expiry; use single-use, expiring tokens.
- PII: logs record notification ID, channel, template, provider ID — not addresses, phone numbers or bodies. OTP codes must never be logged.
- Enumeration: "we sent you an email if the account exists" — send asynchronously so response timing doesn't reveal account existence.
- Abuse: rate-limit user-triggerable sends (resend verification, SMS OTP) per user, per destination and per IP — SMS pumping fraud inflates costs ([throttling](../../django-rest-framework/references/throttling-and-rate-limiting.md)).

**Performance and cost**

- Reuse SMTP connections for batches (`get_connection()` + `send_messages`); per-message connections dominate latency.
- Route notifications to their own queue so a provider slowdown doesn't stall other tasks; cap concurrency to provider rate limits ([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)).
- Bulk/marketing sends: use provider batch endpoints, not one task per recipient beyond tens of thousands; throttle to the provider's per-second limits.
- SMS cost is per segment: keep bodies within one GSM-7 segment; a single emoji switches to UCS-2 and halves capacity.
- Index `provider_message_id` — every webhook event looks it up.

## 9. Common mistakes and anti-patterns

- `EMAIL_TIMEOUT` unset → a hung SMTP server freezes workers indefinitely.
- `send_mail` inside `transaction.atomic()` → email sent for a transaction that later rolls back; use `on_commit`.
- `fail_silently=True` in production code.
- Retrying the whole "create order and send email" task → duplicate emails; key notifications by business event.
- Treating provider 2xx as "delivered".
- Failing over to a second provider after a timeout and sending twice.
- Ignoring bounces/complaints until the provider suspends the account.
- Suppression only at the provider — a failover provider or a provider migration forgets it.
- Storing rendered bodies with PII indefinitely.
- Open tracking used as proof of reading.
- Hard-coding the `From` domain without SPF/DKIM/DMARC for it.
- Sending SMS to unnormalised numbers; country-specific sender rules ignored.
- Push tokens never cleaned → growing failures and wasted calls.
- Using the same sending stream for marketing and transactional mail; marketing complaints hurt password resets.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Requests hang when sending email | No `EMAIL_TIMEOUT`; inline send | Thread dump/py-spy shows `smtplib`; set timeout, move to task |
| `SMTPAuthenticationError` after deploy | Rotated credential, wrong env | Secret version; provider dashboard auth failures |
| Emails "sent" but not received | Spam-foldered; SPF/DKIM/DMARC fail; suppressed at provider | Raw headers (`Authentication-Results`), provider activity log by message ID |
| Duplicate emails | Missing idempotency key; failover after ambiguous timeout; `acks_late` redelivery with non-idempotent task | Count `Notification` rows per key; provider log by `client_ref` |
| Status stuck at `ACCEPTED` | Webhook not configured, failing signature, or provider doesn't send "delivered" for that channel | Webhook delivery logs at provider; `WebhookEvent` rows; signature errors in logs |
| SMS not delivered in one country | Sender ID not registered, carrier filtering, opt-out | Gateway DLR error codes; country registration requirements `VERIFY` |
| Push `UNREGISTERED`/`410` spikes | App reinstall/uninstall; stale tokens | Delete tokens on these errors; monitor token churn |

Validation checklist:

- [ ] `EMAIL_TIMEOUT` set; SMTP/HTTP client timeouts explicit.
- [ ] Sends dispatched via `on_commit`; never inside the business transaction.
- [ ] Unique idempotency key per business event × recipient × channel; tested with duplicate triggers.
- [ ] Suppression checked before every send and updated from bounces/complaints/opt-outs.
- [ ] Webhook signatures verified; event mapping tested with recorded payloads per provider.
- [ ] Failover only on proven non-acceptance (or documented acceptance of duplicates).
- [ ] No PII/OTP/tokens in logs; retention defined for `Notification`.
- [ ] Metrics: send rate, failure rate, bounce/complaint rate per provider ([integration-observability-and-operations.md](integration-observability-and-operations.md)).
- [ ] Tests use `locmem` backend / fake provider ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — receiving delivery/bounce events.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retry, rate limiting, failure classification, idempotency keys.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — task dispatch, `on_commit`, routing.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox for reliable enqueue.
- [integration-security.md](integration-security.md) — credentials, redaction.
- [integration-testing-and-contracts.md](integration-testing-and-contracts.md) — testing providers and webhook mappings.
- [django-core: logging-exceptions](../../django-core/references/logging-exceptions.md), [settings-configuration](../../django-core/references/settings-configuration.md).
- [django-security-engineering: authentication-security](../../django-security-engineering/references/authentication-security.md) (reset/OTP flows), [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).
- [django-testing: integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md).

## 12. Official documentation and external references

- Django — Sending email: https://docs.djangoproject.com/en/5.2/topics/email/
- Django — Email settings (`EMAIL_TIMEOUT` etc.): https://docs.djangoproject.com/en/5.2/ref/settings/#email-timeout
- Django — `transaction.on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit
- Python — `smtplib`: https://docs.python.org/3/library/smtplib.html
- django-anymail documentation: https://anymail.dev/en/stable/
- RFC 5321 (SMTP): https://www.rfc-editor.org/rfc/rfc5321
- RFC 7208 (SPF): https://www.rfc-editor.org/rfc/rfc7208 · RFC 6376 (DKIM): https://www.rfc-editor.org/rfc/rfc6376 · RFC 7489 (DMARC): https://www.rfc-editor.org/rfc/rfc7489
- RFC 8058 (one-click unsubscribe): https://www.rfc-editor.org/rfc/rfc8058
- ITU-T E.164: https://www.itu.int/rec/T-REC-E.164
- python-phonenumbers: https://github.com/daviddrysdale/python-phonenumbers
- Firebase Cloud Messaging HTTP v1: https://firebase.google.com/docs/cloud-messaging/send-message `VERIFY` (path changes occasionally)
- Apple — Sending notification requests to APNs: https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns
- Amazon SES — bounces and complaints: https://docs.aws.amazon.com/ses/latest/dg/send-email-concepts-deliverability.html `VERIFY`
