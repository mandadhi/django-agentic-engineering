# Payment and Financial Integrations

**Scope:** Owns the integration architecture between a Django application and payment gateways or financial data providers: payment state machines, authorise/capture/refund workflows, idempotency tied to internal records, webhook-driven state, monetary precision, audit trails, ambiguous-outcome recovery, settlement-file ingestion and money reconciliation. It does not own generic webhook mechanics ([webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md)), generic retry/idempotency theory ([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)), generic data reconciliation ([data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)) or compliance certification advice.

## 1. Purpose and scope

Payment integrations are where integration mistakes cost real money: a retried POST charges a customer twice, a timeout is treated as a failure and the order is cancelled although the money was taken, a float rounds a refund by a cent, a webhook arrives before the API response and the state machine rejects it. This reference gives an agent a defensible design for card/wallet/bank-transfer payments through a third-party gateway.

It focuses on **architecture with a generic gateway ("AcmePay")** — not on private bank APIs, card networks or any specific provider's SDK. Provider-specific behaviour (Stripe PaymentIntents, Adyen, PayPal, etc.) is called out as such.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x.

Not legal or compliance advice: PCI DSS scope, PSD2/SCA, and local financial regulation must be confirmed with the organisation's compliance owner and QSA. Statements here about scope reduction are architectural tendencies, not certifications.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Authorisation | Issuer reserves funds; no money moves yet. Authorisations expire (often ~7 days for cards, varies by network/provider `VERIFY`). |
| Capture | Requests settlement of an authorised amount (full or partial, sometimes multiple partial captures — provider-specific). |
| Sale / auto-capture | Authorise and capture in one operation. |
| Void / cancel | Release an authorisation before capture. |
| Refund | Return captured funds (full/partial; multiple partials up to captured amount). Asynchronous — can fail later. |
| Chargeback / dispute | Customer's bank reverses a payment; arrives via webhook days to months later. |
| Settlement | Provider pays out net funds (minus fees, refunds, disputes) to your bank in batches; described by settlement/payout reports. |
| Payment method token | Opaque reference to card/bank details stored at the provider; your system never sees the PAN. |
| Idempotency key | Client-generated key making a retried create/capture/refund request return the original result instead of executing again (provider support and key retention window vary `VERIFY`). |
| External reference | Provider's ID for the payment/refund (`pay_123`); plus *your* reference sent to the provider (merchant reference) so either side can find the other. |
| Minor units | Integer amounts in the currency's smallest unit (cents). Exponent varies: JPY 0, USD 2, KWD/BHD 3 (ISO 4217). |
| Reconciliation | Proving internal ledger = provider records = bank settlement, and resolving differences. |

Key distinctions:

- **Gateway 2xx ≠ money received.** A successful "create payment" may return `requires_action` (3-D Secure), `pending` (bank transfer), or `authorised` (not captured).
- **Timeout ≠ failure.** The charge may have succeeded. Outcome is *unknown* until confirmed by query, webhook, or reconciliation.
- **Retries ≠ exactly-once.** Only idempotency keys honoured by the provider (or a query-before-retry protocol) prevent duplicate charges.
- **Webhooks are the authoritative async channel** for most gateways, but can be delayed, duplicated and reordered.

## 3. Internal mechanics

### Typical card payment lifecycle

```text
 create(intent) ─► PENDING ──authorise──► AUTHORISED ──capture──► CAPTURED ──settle──► SETTLED
                    │  │                      │                      │
                    │  └─requires_action─► ACTION_REQUIRED (3DS)      ├─refund──► (Refund objects)
                    │                         │                      └─dispute─► DISPUTED
                    └──decline──► FAILED ◄────┘ (expire/void) ──► CANCELLED
          timeout anywhere ─► UNKNOWN (resolve by query/webhook/reconciliation)
```

Refunds and disputes are *separate entities* with their own lifecycles, linked to the payment; modelling them as payment statuses loses partial-refund history.

### Idempotency at the provider

A provider that supports idempotency typically stores `(merchant, key) → first response` for a retention window and returns the stored response on replay; replaying the same key with a *different* body returns an error. Keys expire (e.g. 24 h at some providers `VERIFY`), so a retry days later is *not* protected — after the window, query by merchant reference instead.

### Webhook ordering

The provider may emit `payment.captured` before your API call returns, or deliver `payment.refunded` before `payment.captured` on retry. Your state machine must accept events in any order: apply transitions only if they move forward, and treat "already there" as success.

### Settlement files

Providers expose settlement/payout reports (CSV, JSON API, or SFTP drops; legacy banks may use fixed-width or ISO 20022 camt.053 statements). Each line references a provider transaction ID, gross, fee, net, currency, type (charge/refund/dispute/adjustment), and payout batch.

## 4. Engineering decisions and trade-offs

| Decision | Options | Prefer | Why / trade-off |
|---|---|---|---|
| Card data handling | Hosted payment page / hosted fields / client-side tokenisation vs server-side PAN | Hosted page or hosted fields/tokenisation | PAN never touches your servers → typically much smaller PCI DSS scope (SAQ type determined by your QSA). Less UI control. |
| Capture mode | Auto-capture vs separate capture | Auto-capture for digital goods; auth then capture on shipment for physical goods | Separate capture adds expiry handling and a second failure point. |
| Source of truth for state | Synchronous API response vs webhooks vs both | Both, through one idempotent state-transition function | API gives fast UX; webhooks catch async and out-of-band changes (disputes, bank transfers). |
| Money type | `float` / `Decimal` / integer minor units | `Decimal` in domain + `DecimalField`; integer minor units on the wire when provider expects them | Float cannot represent 0.10 exactly. Convert at the adapter boundary with currency exponent. |
| Ledger | Status fields only vs double-entry ledger | Status + append-only event/audit log; a ledger when you hold balances, split payments or need accounting-grade reports | A ledger is real work; required for marketplaces/wallets. |
| Synchronous charge in request | Inline vs background | Inline for interactive checkout (user waits, short timeout, ambiguous → pending); background for off-session charges (subscriptions, retries) | Inline UX needs answer now; background allows retries with backoff and reconciliation. |
| Multiple providers | Single vs routing/failover | Single unless volume/regions justify | Failover on ambiguous outcome risks double charge; tokens are usually not portable between providers. |
| Distributed transaction | 2PC between DB and gateway | Never possible; use state machine + idempotency + reconciliation | Gateways don't participate in your transaction. |

## 5. Django implementation patterns

### Money

```python
# apps/payments/money.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

CURRENCY_EXPONENT = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}   # source from ISO 4217 data


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount must be Decimal")
        exp = CURRENCY_EXPONENT[self.currency]
        if self.amount != self.amount.quantize(Decimal(1).scaleb(-exp)):
            raise ValueError(f"{self.amount} has too many decimals for {self.currency}")

    def to_minor(self) -> int:
        return int(self.amount.scaleb(CURRENCY_EXPONENT[self.currency]))

    @classmethod
    def from_minor(cls, value: int, currency: str) -> "Money":
        return cls(Decimal(value).scaleb(-CURRENCY_EXPONENT[currency]), currency)


def allocate(total: Money, ratios: list[int]) -> list[Money]:
    """Split without losing cents: remainder goes to the first parts."""
    exp = CURRENCY_EXPONENT[total.currency]
    minor = total.to_minor()
    parts = [minor * r // sum(ratios) for r in ratios]
    for i in range(minor - sum(parts)):
        parts[i] += 1
    return [Money.from_minor(p, total.currency) for p in parts]
```

Parse provider JSON amounts with `Decimal(str(value))` or `json.loads(..., parse_float=Decimal)` — never through `float`. Use an explicit rounding mode (`ROUND_HALF_EVEN` or what your finance team specifies) for tax/fee calculations. `django-money` `[third-party: django-money]` exists; many teams prefer a plain `DecimalField` + `currency` `CharField` pair for transparency.

### Models with state machine and constraints

```python
# apps/payments/models.py
import uuid
from django.db import models
from django.db.models import Q, F
from django.db.models.functions import Now


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        ACTION_REQUIRED = "action_required"
        UNKNOWN = "unknown"              # outcome ambiguous — must be resolved
        AUTHORISED = "authorised"
        CAPTURED = "captured"
        FAILED = "failed"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    currency = models.CharField(max_length=3)
    captured_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    provider = models.CharField(max_length=30, default="acmepay")
    provider_payment_id = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)   # sent to provider
    failure_code = models.CharField(max_length=60, blank=True)
    version = models.PositiveIntegerField(default=0)                  # optimistic concurrency
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(condition=Q(captured_amount__lte=F("amount")),
                                   name="payment_capture_le_amount"),
            models.CheckConstraint(condition=Q(refunded_amount__lte=F("captured_amount")),
                                   name="payment_refund_le_captured"),
            models.UniqueConstraint(fields=["provider", "provider_payment_id"],
                                    condition=~Q(provider_payment_id=""),
                                    name="payment_provider_ref_uniq"),
            # At most one live (non-terminal) payment per order prevents double-checkout races.
            models.UniqueConstraint(fields=["order"],
                                    condition=Q(status__in=["pending", "action_required", "unknown",
                                                            "authorised", "captured"]),
                                    name="payment_one_active_per_order"),
        ]


class PaymentEvent(models.Model):
    """Append-only audit trail: every transition, with its cause."""
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="events")
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    source = models.CharField(max_length=20)       # api_response | webhook | reconciliation | operator
    source_ref = models.CharField(max_length=120, blank=True)   # webhook event id, report line, user id
    data = models.JSONField(default=dict)          # redacted provider fields
    created_at = models.DateTimeField(db_default=Now())


class Refund(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; UNKNOWN = "unknown"; SUCCEEDED = "succeeded"; FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    provider_refund_id = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    reason = models.CharField(max_length=60)
    requested_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(db_default=Now())
```

`CheckConstraint(condition=...)` is the Django ≥ 5.1 spelling (`check=` deprecated). `decimal_places=3` accommodates three-decimal currencies; validate per-currency precision in `Money`. Use `on_delete=PROTECT` everywhere — financial records are never cascade-deleted. Restrict UPDATE/DELETE on `PaymentEvent` at the DB role level if audit integrity matters ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

### Transition function — the single write path

```python
# apps/payments/state.py
from django.db import transaction
from .models import Payment, PaymentEvent

S = Payment.Status
ALLOWED: dict[str, set[str]] = {
    S.PENDING: {S.ACTION_REQUIRED, S.AUTHORISED, S.CAPTURED, S.FAILED, S.UNKNOWN, S.CANCELLED},
    S.UNKNOWN: {S.ACTION_REQUIRED, S.AUTHORISED, S.CAPTURED, S.FAILED, S.CANCELLED},
    S.ACTION_REQUIRED: {S.AUTHORISED, S.CAPTURED, S.FAILED, S.CANCELLED, S.UNKNOWN},
    S.AUTHORISED: {S.CAPTURED, S.CANCELLED},
    S.CAPTURED: set(),          # refunds/disputes are separate entities
    S.FAILED: set(),
    S.CANCELLED: set(),
}


class IllegalTransition(Exception): ...


def transition(payment_id, to: str, *, source: str, source_ref: str = "", data: dict | None = None,
               provider_payment_id: str = "", captured_amount=None) -> Payment:
    with transaction.atomic():
        p = Payment.objects.select_for_update().get(pk=payment_id)
        if p.status == to:
            return p                                   # idempotent: duplicate webhook / replay
        if to not in ALLOWED[p.status]:
            # Out-of-order event that is "behind" current state (e.g. authorised after captured):
            if _is_behind(p.status, to):
                return p
            raise IllegalTransition(f"{p.status} -> {to}")
        PaymentEvent.objects.create(payment=p, from_status=p.status, to_status=to,
                                    source=source, source_ref=source_ref, data=data or {})
        p.status = to
        if provider_payment_id and not p.provider_payment_id:
            p.provider_payment_id = provider_payment_id
        if captured_amount is not None:
            p.captured_amount = captured_amount
        p.version += 1
        p.save()
        if to == S.CAPTURED:
            transaction.on_commit(lambda: fulfil_order.delay(str(p.order_id)))
        return p
```

`IllegalTransition` from a webhook means your model and the provider disagree — alert, do not silently drop.

### Gateway port and AcmePay adapter

```python
# apps/payments/integrations/acmepay/gateway.py
from dataclasses import dataclass
from typing import Protocol
import httpx
from django.conf import settings

from apps.integrations.http import build_client
from apps.integrations.errors import (
    AmbiguousOutcomeError, PermanentIntegrationError, RateLimitedError, TransientIntegrationError,
)
from apps.payments.money import Money


@dataclass(frozen=True)
class GatewayResult:
    provider_payment_id: str
    status: str                 # mapped to our vocabulary
    decline_code: str = ""


class PaymentGateway(Protocol):
    def create_payment(self, *, money: Money, method_token: str, reference: str,
                       idempotency_key: str, capture: bool) -> GatewayResult: ...
    def capture(self, *, provider_payment_id: str, money: Money, idempotency_key: str) -> GatewayResult: ...
    def refund(self, *, provider_payment_id: str, money: Money, idempotency_key: str) -> str: ...
    def find_by_reference(self, reference: str) -> GatewayResult | None: ...


_STATUS = {"succeeded": "captured", "authorized": "authorised", "requires_action": "action_required",
           "declined": "failed", "canceled": "cancelled", "processing": "pending"}

_client = build_client("acmepay", base_url=settings.ACMEPAY_BASE_URL,
                       timeout=httpx.Timeout(15.0, connect=3.0),
                       auth=httpx.BasicAuth(settings.ACMEPAY_SECRET_KEY, ""))


class AcmePayGateway:
    def create_payment(self, *, money, method_token, reference, idempotency_key, capture):
        body = {"amount": money.to_minor(), "currency": money.currency.lower(),
                "payment_method": method_token, "merchant_reference": reference,
                "capture": capture}
        data = self._post("/v1/payments", body, idempotency_key)
        return GatewayResult(data["id"], _STATUS[data["status"]], data.get("decline_code", ""))

    def _post(self, path: str, body: dict, idempotency_key: str) -> dict:
        try:
            r = _client.post(path, json=body, headers={"Idempotency-Key": idempotency_key})
        except httpx.ConnectError as exc:                 # nothing reached the provider
            raise TransientIntegrationError(provider="acmepay", operation=path) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            raise AmbiguousOutcomeError(provider="acmepay", operation=path) from exc
        if r.status_code == 429:
            raise RateLimitedError(provider="acmepay", operation=path,
                                   retry_after=float(r.headers.get("Retry-After", "2")))
        if r.status_code == 402:                          # declined: business result, not an error
            return r.json()
        if r.status_code == 409:                          # idempotency key reused with different body
            raise PermanentIntegrationError(provider="acmepay", operation=path, status_code=409)
        if r.status_code >= 500:
            # Many gateways may have processed the request before erroring; with an idempotency
            # key, retrying is safe. Without one, treat as ambiguous.
            raise TransientIntegrationError(provider="acmepay", operation=path, status_code=r.status_code)
        if r.status_code >= 400:
            raise PermanentIntegrationError(provider="acmepay", operation=path, status_code=r.status_code)
        return r.json()
```

### Idempotency keys tied to internal records

- Payment create: `idempotency_key = f"payment:{payment.id}:create"` — generated when the `Payment` row is created, stored, and reused for every retry.
- Capture: `f"payment:{payment.id}:capture:{n}"` where *n* is the capture sequence for partial captures.
- Refund: `f"refund:{refund.id}"` — the `Refund` row is created first (in the same transaction as the business decision), then the API call is made.
- Never derive keys from timestamps or random values generated at call time; a retry would generate a new key and a new charge.
- Also send `merchant_reference = str(payment.id)` so you can *query* by reference after the provider's idempotency window has expired.

### Checkout service with ambiguous-outcome handling

```python
# apps/payments/services.py
def start_checkout(*, order, method_token: str, gateway: PaymentGateway) -> Payment:
    with transaction.atomic():
        payment_id = uuid.uuid4()
        payment = Payment.objects.create(
            id=payment_id, order=order, amount=order.total, currency=order.currency,
            idempotency_key=f"payment:{payment_id}:create",
        )
    # Commit BEFORE calling the gateway: the row is our durable record of the attempt.
    exp = CURRENCY_EXPONENT[payment.currency]
    money = Money(payment.amount.quantize(Decimal(1).scaleb(-exp)), payment.currency)
    try:
        result = gateway.create_payment(money=money, method_token=method_token,
                                        reference=str(payment.id),
                                        idempotency_key=payment.idempotency_key, capture=True)
    except AmbiguousOutcomeError:
        transition(payment.id, Payment.Status.UNKNOWN, source="api_response")
        transaction.on_commit(lambda: resolve_unknown_payment.apply_async((str(payment.id),), countdown=30))
        return payment                       # UI: "We're confirming your payment…"
    except TransientIntegrationError:
        transition(payment.id, Payment.Status.FAILED, source="api_response",
                   data={"reason": "provider_unavailable"})
        raise
    return transition(payment.id, result.status, source="api_response",
                      provider_payment_id=result.provider_payment_id,
                      captured_amount=payment.amount if result.status == "captured" else None)
```

`ConnectError` is safe to mark failed (request never reached the provider). A read timeout is not — the user may have been charged.

```python
# apps/payments/tasks.py
@shared_task(bind=True, max_retries=10)
def resolve_unknown_payment(self, payment_id: str) -> None:
    payment = Payment.objects.get(pk=payment_id)
    if payment.status != Payment.Status.UNKNOWN:
        return                                          # a webhook already resolved it
    found = get_gateway().find_by_reference(str(payment.id))
    if found is None:
        if payment.created_at < timezone.now() - timedelta(hours=1):
            transition(payment.id, Payment.Status.FAILED, source="reconciliation",
                       data={"reason": "not_found_at_provider"})
            return
        raise self.retry(countdown=min(30 * 2 ** self.request.retries, 1800))
    transition(payment.id, found.status, source="reconciliation",
               provider_payment_id=found.provider_payment_id)
```

### Webhooks drive the same state machine

```python
# apps/payments/webhook_handlers.py — called by the generic WebhookEvent processor
def handle_acmepay_event(event: WebhookEvent) -> None:
    obj = event.payload["data"]["object"]
    payment = Payment.objects.filter(provider_payment_id=obj["id"]).first() or \
              Payment.objects.filter(pk=obj.get("merchant_reference")).first()
    if payment is None:
        raise UnknownPaymentReference(obj["id"])        # retry later; event may precede our commit
    # Re-fetch from the provider for high-value transitions rather than trusting payload fields.
    transition(payment.id, _STATUS[obj["status"]], source="webhook", source_ref=event.event_id,
               provider_payment_id=obj["id"])
```

Signature verification, the `WebhookEvent` inbox and dedupe are in [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md). Disputes create a `Dispute` entity and notify operations; they never mutate the original `Payment.captured_amount`.

### Refund workflow

1. Validate in a transaction: `select_for_update()` the payment, check `refunded_amount + amount <= captured_amount`, create `Refund(PENDING)`, increment a *reserved* refunded amount (or compute from non-failed refunds).
2. `on_commit` → task calls `gateway.refund(..., idempotency_key=f"refund:{refund.id}")`.
3. Provider response or webhook moves `Refund` to `SUCCEEDED`/`FAILED`; a failed refund releases the reservation and alerts.
4. Refund permissions are an authorisation concern: restrict who can refund and how much; audit `requested_by` ([django-security-engineering](../../django-security-engineering/references/authorization-and-access-control.md)).

### Reconciliation and settlement ingestion

```python
# apps/payments/reconciliation.py
import csv
from decimal import Decimal

class SettlementLine(models.Model):
    report_id = models.CharField(max_length=100)
    line_no = models.PositiveIntegerField()
    provider_txn_id = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)                 # charge|refund|dispute|fee|adjustment
    gross = models.DecimalField(max_digits=14, decimal_places=3)
    fee = models.DecimalField(max_digits=14, decimal_places=3)
    net = models.DecimalField(max_digits=14, decimal_places=3)
    currency = models.CharField(max_length=3)
    payout_id = models.CharField(max_length=100)
    match_status = models.CharField(max_length=20, default="unmatched")
    class Meta:
        constraints = [models.UniqueConstraint(fields=["report_id", "line_no"], name="settle_line_uniq")]


def ingest_settlement(report_id: str, rows: Iterable[dict]) -> None:
    lines = [SettlementLine(report_id=report_id, line_no=i, provider_txn_id=r["txn_id"],
                            kind=r["type"], gross=Decimal(r["gross"]), fee=Decimal(r["fee"]),
                            net=Decimal(r["net"]), currency=r["currency"], payout_id=r["payout_id"])
             for i, r in enumerate(rows, start=1)]
    SettlementLine.objects.bulk_create(lines, ignore_conflicts=True)   # re-ingesting is idempotent
```

Matching rules (run as a scheduled job, results to a `ReconciliationIssue` table, not just logs):

| Case | Meaning | Action |
|---|---|---|
| Internal CAPTURED, settlement line found, amounts equal | Matched | Mark matched |
| Internal CAPTURED, no line after N settlement cycles | Missing settlement | Investigate; query provider |
| Line found, no internal payment | Orphan (charge made outside your flow, or lost response) | Link via merchant reference or escalate |
| Amount/currency mismatch | Partial capture, FX, bug | Escalate; never auto-correct money |
| Internal FAILED/UNKNOWN, line exists | Customer was charged | Transition to CAPTURED via reconciliation + fulfil or refund per policy |
| Sum of lines ≠ payout total | Report incomplete or adjustments | Block close of the period |

Also reconcile payout totals against bank statements if finance requires it — usually a finance-system responsibility rather than the Django app's. File transport (SFTP, object storage) is in [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md); generic matching strategy in [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md).

## 6. Production use cases

1. **Double-click checkout.** Two requests create two `Payment` rows → `payment_one_active_per_order` partial unique constraint makes the second fail; return the existing payment instead.
2. **Gateway read timeout at peak.** 0.5 % of charges time out; previously marked failed → customers charged without orders. Now `UNKNOWN` → resolver task and webhook converge to `CAPTURED`; order fulfilled.
3. **Webhook before response.** `payment.succeeded` arrives while the request thread is still waiting; the handler can't find `provider_payment_id` yet → falls back to `merchant_reference` (our UUID, committed before the call) → transition succeeds; the later API response is a no-op transition.
4. **Partial shipment.** Authorise full basket; capture per shipment with sequential capture keys; void the remaining authorisation when the order closes; handle authorisation expiry with a re-authorisation flow (provider-specific).
5. **Subscription renewal.** Off-session charge in a Celery task with backoff on transient errors and dunning schedule on declines; idempotency key `renewal:{subscription}:{period}` guarantees one charge per period even if the scheduler fires twice.
6. **Month-end close.** Settlement ingestion + matching produces zero unmatched items or a signed-off exception list; finance exports from the reconciled tables.

## 7. Advantages and disadvantages

| Design element | Advantages | Disadvantages |
|---|---|---|
| Explicit state machine + transition function | One write path; invalid states impossible; auditable | Must evolve with provider statuses; more ceremony than a status field |
| DB constraints on amounts/uniqueness | Defends against application bugs and races | Constraint changes need migrations; errors must be mapped to user-facing messages |
| `UNKNOWN` state + resolver | No false failures, no double charges | UX must handle "confirming"; extra task infrastructure |
| Webhooks + API responses | Fast UX and eventual completeness | Two paths racing; requires idempotent transitions |
| Hosted fields / tokenisation | PAN never on your servers; typically reduced PCI scope | Less UI control; dependence on provider JS |
| Settlement reconciliation | Detects money discrepancies the app can't see otherwise | Report formats change; needs operator workflow |
| Append-only audit table | Forensics, disputes, compliance evidence | Storage growth; PII redaction responsibilities |

## 8. Security and performance considerations

**Security**

- Never handle raw card numbers unless the organisation has deliberately accepted that PCI DSS scope. Never log PAN, CVV, full bank account numbers or provider secret keys. Redact provider payloads before storing them in `PaymentEvent.data` ([integration-security.md](integration-security.md)).
- Never trust client-submitted amounts: compute from the server-side order. Never trust webhook payload amounts for fulfilment of high-value orders — re-fetch from the provider API.
- Separate keys per environment; restricted/limited-scope keys where the provider supports them; rotate on staff departure ([secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md)).
- Refund and manual-transition endpoints need strong authorisation, MFA for operators, and dual control for large amounts (business decision).
- Webhook endpoints: signature verification on raw body, replay window, no CSRF/session auth.
- Rate-limit checkout attempts per user/card fingerprint to deter card-testing fraud; providers also offer fraud tools (provider-specific).
- Audit records: who, what, when, source; immutable at the DB permission level where feasible ([security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md)).

**Performance**

- Keep the gateway call out of DB transactions — `select_for_update` locks held across a 15 s timeout serialise checkouts.
- Interactive checkout: request timeout budget must exceed gateway read timeout plus overhead, or the proxy kills the request mid-charge — set gateway read timeout (e.g. 15–30 s) below the proxy timeout and treat the gap as ambiguous.
- Settlement ingestion: stream files, `bulk_create` in batches, index `provider_txn_id` and `provider_payment_id` ([file-and-data-processing-performance](../../django-performance/references/file-and-data-processing-performance.md)).
- Route payment tasks to a dedicated queue so a notification backlog cannot delay capture or resolution.

## 9. Common mistakes and anti-patterns

- `float` for amounts, or `Decimal(0.1)` (constructed from float).
- Assuming every currency has two decimals.
- Retrying a create-payment POST without an idempotency key.
- Generating a fresh idempotency key per attempt.
- Marking a payment failed on read timeout.
- Calling the gateway inside `transaction.atomic()` — rollback leaves an orphaned charge with no internal record; commit the attempt record first.
- Status updated directly (`payment.status = "captured"; save()`) from views, webhooks and admin — bypasses the transition rules and audit.
- Rejecting out-of-order webhooks with 4xx so the provider retries forever, or acknowledging and dropping them silently.
- Modelling refunds as a status of the payment; losing partial refund history.
- Cascade-deleting payments with users or orders.
- Fulfilment triggered from the client-side redirect ("success URL") instead of server-confirmed state.
- No reconciliation job: discrepancies discovered by customers or auditors.
- Logging full provider responses including payment-method details.
- Failing over to another gateway after an ambiguous outcome.
- Claiming "PCI compliant" because a hosted page is used — scope reduction is not compliance.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Customer charged twice | Missing/regenerated idempotency key; two active payments per order | Provider logs by merchant reference; idempotency key on rows; partial unique constraint |
| Charged but order not fulfilled | Timeout treated as failure; webhook handler failing; `on_commit` not firing (task dispatched before commit) | Payments in `UNKNOWN`/`FAILED` with provider records; `WebhookEvent` status; resolver task metrics |
| `IllegalTransition` alerts | Provider added a status; out-of-order events not tolerated | Map new status; add "behind current state" handling |
| Refund over captured amount | Concurrent refunds without lock | `select_for_update` + check constraint `refunded <= captured` |
| Reconciliation mismatches by 1 minor unit | Float conversion or rounding mode | Trace conversion; use `Decimal(str(...))`, explicit rounding |
| 409 from provider | Same idempotency key with different body (amount changed between retries) | Never mutate the amount of an attempted payment; create a new attempt |
| Authorisations expiring | Capture delayed beyond network window | Monitor age of `AUTHORISED`; capture or re-authorise earlier |

Validation checklist:

- [ ] All amounts `Decimal`/`DecimalField`; currency exponent respected; minor-unit conversion tested per currency.
- [ ] Every outbound mutating call carries a stored idempotency key derived from an internal record.
- [ ] Ambiguous outcomes (timeouts after send) produce `UNKNOWN`, never `FAILED`; a resolver exists.
- [ ] All state changes go through one transition function with an audit row.
- [ ] DB constraints: positive amounts, capture ≤ amount, refunds ≤ captured, unique provider refs, one active payment per order.
- [ ] Webhooks verified, deduplicated, order-tolerant; high-value transitions re-fetched.
- [ ] No gateway call inside a transaction; fulfilment dispatched `on_commit`.
- [ ] Reconciliation job runs; issues land in a table with an operator workflow and alerting.
- [ ] No PAN/CVV/secret keys in code, logs, or audit payloads.
- [ ] Tests: duplicate submit, timeout → unknown → resolved, webhook before response, out-of-order events, partial refunds, rounding ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — signature verification, inbox, replay.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — idempotency keys (canonical), failure classification, retry budgets.
- [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md) — generic matching, checkpoints, failed-record recovery.
- [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md) — settlement files over SFTP/object storage.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — saga/compensation, outbox, gateway port.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — `on_commit` dispatch, task routing.
- [integration-security.md](integration-security.md) — redaction, credential handling.
- [django-architecture: distributed-systems](../../django-architecture/references/distributed-systems.md), [service-layer](../../django-architecture/references/service-layer.md).
- [django-orm](../../django-orm/SKILL.md) — constraints, `select_for_update`, transactions.
- [django-security-engineering: authorization-and-access-control](../../django-security-engineering/references/authorization-and-access-control.md), [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).
- [django-testing: database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md).

## 12. Official documentation and external references

- Python — `decimal`: https://docs.python.org/3/library/decimal.html
- Django — `DecimalField`: https://docs.djangoproject.com/en/5.2/ref/models/fields/#decimalfield
- Django — Constraints (`CheckConstraint`, `UniqueConstraint`): https://docs.djangoproject.com/en/5.2/ref/models/constraints/
- Django — `select_for_update`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update
- Django — Transactions and `on_commit`: https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- ISO 4217 currency codes: https://www.iso.org/iso-4217-currency-codes.html
- PCI Security Standards Council — document library: https://www.pcisecuritystandards.org/document_library/
- IETF draft — The Idempotency-Key HTTP Header Field: https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ `VERIFY` (draft status)
- Stripe — Idempotent requests (provider-specific example): https://docs.stripe.com/api/idempotent_requests
- Stripe — Webhooks best practices (provider-specific example): https://docs.stripe.com/webhooks
- ISO 20022 (bank statement messages, e.g. camt.053): https://www.iso20022.org/
- PostgreSQL — Constraints: https://www.postgresql.org/docs/current/ddl-constraints.html
