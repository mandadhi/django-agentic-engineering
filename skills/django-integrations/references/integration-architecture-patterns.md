# Integration Architecture Patterns

**Scope:** The structural patterns used to build integrations in Django — Adapter, Gateway, Facade,
Anti-Corruption Layer, Ports and Adapters, Transactional Outbox (canonical implementation), Inbox, Saga, Process
Manager, orchestration vs choreography, compensation, Strangler Fig, API composition and the limits of
distributed transactions. Each pattern states the problem it solves and when its complexity is justified.
Generic GoF pattern theory and the choice of architectural style belong to
[django-architecture](../../django-architecture/references/design-patterns.md).

## 1. Purpose and scope

Use this reference when deciding *how to structure* integration code so that provider churn, partial failure
and cross-system consistency are handled deliberately. Broker mechanics are in
[messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md); Celery mechanics in
[celery-and-background-integrations.md](celery-and-background-integrations.md); idempotency keys and retries
in [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. The Outbox relay below
requires a database that supports `SELECT … FOR UPDATE SKIP LOCKED` (PostgreSQL, MySQL 8+, Oracle; not SQLite).

## 2. Core concepts

| Pattern | Problem it solves | Justified when | Not justified when |
|---|---|---|---|
| **Adapter** | Provider interface does not match the interface your code expects | Always, at least thinly, for any external system | — |
| **Gateway** | Callers need one object encapsulating access to an external resource | Any provider used from more than one place | Single one-off call in a management command |
| **Facade** | A complex subsystem (SDK, several APIs) needs a simple entry point | SDK with many operations, multi-step flows | Provider already exposes one simple call |
| **Anti-Corruption Layer (ACL)** | External model/semantics would corrupt your domain model | Legacy/ERP/partner models differ substantially from yours | Provider model already matches the domain |
| **Ports and Adapters (hexagonal)** | Domain must be testable and provider-swappable | Core domain with ≥ 1 external dependency you want to fake or swap | CRUD apps without meaningful domain logic |
| **Transactional Outbox** | Dual write: DB commit and message publish/remote call cannot be atomic | Lost or phantom messages are unacceptable | Effects that may be lost (best-effort notifications) or that can be re-derived |
| **Inbox** | Consumer receives duplicates; must process each message once in effect | At-least-once inputs (brokers, webhooks) with non-idempotent handlers | Handlers that are naturally idempotent (upserts by key) |
| **Saga** | Business transaction spans systems without a shared transaction | Multi-step flows where each step commits independently and failure must be compensated | All steps live in one database — use one transaction |
| **Process Manager** | Saga coordination needs durable state, timeouts, branching | Long-running flows (minutes–days), human steps, many branches | Linear 2-step flow |
| **Orchestration** | Central coordinator drives steps | Complex flows, need visibility of state | Few independent reactions |
| **Choreography** | Services react to each other's events | Few steps, independent teams, loose coupling | Flow logic you must understand from one place |
| **Strangler Fig** | Replace a legacy system incrementally | Big-bang rewrite is too risky | Small system replaceable in one release |
| **API Composition** | A read needs data from several systems | Query-time join of small datasets with acceptable latency | High-volume reads → build a read model via sync |

## 3. Internal mechanics

### 3.1 Adapter, Gateway, Facade, ACL in one stack

```text
domain service ──▶ PaymentGateway (port, Protocol)          ← Ports and Adapters
                      ▲
                      │ implements
                AcmePayGateway (gateway.py)                  ← Gateway + ACL (translation of models,
                      │                                         statuses, errors, money units)
                      ▼
                AcmePayClient (client.py)                    ← Adapter/Facade over HTTP or SDK
                      ▼
                httpx.Client / vendor SDK
```

The ACL is the *translation* responsibility (vocabulary, invariants, error mapping); Gateway/Adapter are the
*structural* shape. In Django code one module (`gateway.py`) usually plays all three roles; keep the
transport (`client.py`) separate so it can be reused and mocked at the transport level.

### 3.2 Why the outbox works

The business row and the `OutboxMessage` row are written in **one local transaction**. Either both commit or
neither does. A separate relay reads committed outbox rows and publishes them, marking them published. The
relay can crash after publishing and before marking → the message is published **again**. The outbox therefore
gives **at-least-once** publication; consumers must deduplicate (Inbox) using the message id.

```text
tx: INSERT order; INSERT outbox(id, topic, payload) ; COMMIT
relay loop: SELECT … FOR UPDATE SKIP LOCKED LIMIT n → publish → UPDATE published_at → COMMIT
consumer: INSERT inbox(message_id) ON CONFLICT DO NOTHING → if inserted: handle → COMMIT
```

`SKIP LOCKED` lets several relay workers run concurrently without blocking or double-claiming the same rows.

### 3.3 Why the inbox works

The consumer inserts the message id into a table with a unique constraint **in the same transaction** as the
handler's DB effects. A duplicate either fails the unique insert (skip) or waits for the first transaction and
then sees the row. This gives effectively-once **DB effects**; remote side effects inside the handler still
need idempotency keys.

### 3.4 Saga mechanics

A saga is a sequence of local transactions `T1 … Tn`, each with a compensating action `C1 … Cn-1`. If `Tk`
fails permanently, run `Ck-1 … C1`. Compensations are **semantic undo** (refund, cancel, release), not
rollback: the intermediate state was visible, and some actions (an email sent) cannot be compensated —
order steps so that irreversible actions run last ("pivot transaction").

## 4. Engineering decisions and trade-offs

### 4.1 Outbox vs `on_commit` dispatch vs direct call

| Option | Guarantee | Failure gap | Use when |
|---|---|---|---|
| Direct call in request | None if called before commit; lost if process dies after | Remote effect without local commit, or vice versa | Reads, validations |
| `transaction.on_commit(task.delay)` | Task enqueued only after commit | Process crash or broker outage between commit and enqueue loses the effect | Most side effects; loss is rare and detectable/reconcilable |
| Outbox + relay | At-least-once publish of every committed intent | None (duplicates instead of loss) | Money, compliance, cross-system state, event streams others rely on |
| CDC (Debezium on WAL) | At-least-once from the DB log | Operational complexity | Many tables, high volume, platform team available |

### 4.2 Orchestration vs choreography

- **Orchestration** (a process manager in Django drives steps): explicit state, easy to answer "where is order
  42?", easy timeouts; risk of a god-orchestrator that knows every service's internals.
- **Choreography** (each service reacts to events): loose coupling, no central bottleneck; flow logic is
  implicit, cycles and "event storms" are hard to debug. Prefer choreography for 2–3 independent reactions,
  orchestration once a flow has compensation, timeouts or more than ~4 steps.

### 4.3 Distributed transaction limitations

Two-phase commit (XA) requires every participant to support a prepare phase and a transaction coordinator.
SaaS APIs, brokers like Kafka (outside its own transactions), and email/SMS gateways do not participate. Django
has no XA coordinator; PostgreSQL's `PREPARE TRANSACTION` exists but is disabled by default
(`max_prepared_transactions = 0`; managed services may differ `VERIFY`) and orphaned prepared transactions hold locks and block vacuum. Avoid
distributed transactions unless every participant is a database you control and a justified requirement
exists; use outbox + idempotency + sagas + reconciliation instead.

### 4.4 Strangler Fig routing options

| Routing point | Pros | Cons |
|---|---|---|
| Reverse proxy / API gateway (path-based) | No code changes in legacy | Coarse-grained |
| Django facade calling legacy for unmigrated features | Fine-grained, feature flags | Django becomes temporarily coupled to legacy |
| Event interception (legacy DB CDC → new system) | Gradual data migration | Two writers risk; needs clear SoR per entity during migration |

## 5. Django implementation patterns

### 5.1 Gateway with ACL translation

```python
# apps/billing/integrations/acmepay/gateway.py
from decimal import Decimal

from apps.billing.ports import ChargeResult
from apps.integrations.errors import ContractViolationError, PermanentIntegrationError

from .client import AcmePayClient
from .schemas import AcmePayCharge

_STATUS_MAP = {"succeeded": "authorised", "requires_capture": "authorised",
               "failed": "declined", "processing": "pending"}


class AcmePayGateway:
    def __init__(self, client: AcmePayClient | None = None) -> None:
        self._client = client or AcmePayClient.default()

    def authorise(self, *, amount: Decimal, currency: str, reference: str,
                  idempotency_key: str) -> ChargeResult:
        raw = self._client.create_charge(
            amount_minor=int(amount * 100),          # ACL: provider uses minor units (currency-dependent!)
            currency=currency.lower(),
            metadata={"reference": reference},
            idempotency_key=idempotency_key,
        )
        charge = AcmePayCharge.model_validate(raw)   # pydantic [third-party: pydantic]
        try:
            status = _STATUS_MAP[charge.status]
        except KeyError as exc:                       # unknown enum value: fail loudly, don't guess
            raise ContractViolationError(provider="acmepay", operation="authorise",
                                         detail=f"unknown status {charge.status!r}") from exc
        return ChargeResult(external_id=charge.id, status=status)
```

Minor-unit conversion must use the currency's exponent (JPY has 0, BHD has 3); see
[payment-and-financial-integrations.md](payment-and-financial-integrations.md).

### 5.2 Transactional Outbox (canonical)

```python
# apps/integrations/models.py
import uuid

from django.db import models
from django.db.models.functions import Now


class OutboxMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)   # = message id for consumers
    topic = models.CharField(max_length=128)
    key = models.CharField(max_length=128, blank=True)        # partition/ordering key, e.g. order id
    payload = models.JSONField()
    headers = models.JSONField(default=dict, blank=True)      # correlation id, schema version, traceparent
    created_at = models.DateTimeField(db_default=Now())
    published_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"], name="outbox_pending_idx",
                         condition=models.Q(published_at__isnull=True)),
        ]
```

```python
# apps/orders/services.py — write intent in the same transaction as the business change
from django.db import transaction

from apps.integrations.models import OutboxMessage


def place_order(*, customer_id: int, lines: list[dict], correlation_id: str) -> Order:
    with transaction.atomic():
        order = Order.objects.create(customer_id=customer_id, status=Order.Status.PLACED)
        OrderLine.objects.bulk_create([OrderLine(order=order, **line) for line in lines])
        OutboxMessage.objects.create(
            topic="orders.order_placed.v1",
            key=str(order.pk),
            payload={"order_id": order.pk, "customer_id": customer_id,
                     "total": str(order.total), "currency": order.currency},
            headers={"correlation_id": correlation_id, "schema": "order_placed/1"},
        )
    return order
```

```python
# apps/integrations/outbox.py — relay (run from a management command loop or a periodic Celery task)
import logging
from collections.abc import Callable

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import OutboxMessage

logger = logging.getLogger(__name__)
Publisher = Callable[[OutboxMessage], None]   # raises on failure; must be safe to call twice


def relay_batch(publish: Publisher, *, batch_size: int = 100, max_attempts: int = 20) -> int:
    published = 0
    with transaction.atomic():
        batch = list(
            OutboxMessage.objects
            .select_for_update(skip_locked=True)
            .filter(published_at__isnull=True, attempts__lt=max_attempts)
            .order_by("created_at")[:batch_size]
        )
        for message in batch:
            try:
                publish(message)
            except Exception as exc:                      # noqa: BLE001 — record and continue
                OutboxMessage.objects.filter(pk=message.pk).update(
                    attempts=F("attempts") + 1, last_error=repr(exc)[:2000])
                logger.warning("outbox.publish_failed", extra={"message_id": str(message.pk),
                                                               "topic": message.topic})
                if message.key:
                    break      # preserve per-key order: stop instead of publishing later messages
                continue
            OutboxMessage.objects.filter(pk=message.pk).update(published_at=timezone.now())
            published += 1
    return published
```

Design notes:

- The lock is held while publishing; keep batches small and the publisher's timeout short (≤ a few seconds),
  otherwise use a two-step claim (`claimed_until` timestamp) and publish outside the transaction.
- **Ordering:** `skip_locked` plus several relay workers breaks global order. If consumers need per-key order,
  run one relay per partition of keys (e.g. `hash(key) % n`) or a single relay, and stop the batch on a failure
  for a keyed message (as above, simplified — a stricter version skips only later messages with the same key).
- Rows exceeding `max_attempts` are the outbox's dead letters: alert on them, expose a replay command.
- Prune published rows on a schedule (`DELETE … WHERE published_at < now() - interval '7 days'` in batches);
  large outbox tables slow the relay query. Partial index above keeps the pending scan cheap.
- Publisher implementations: broker producer
  ([messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md)), HTTP call with
  `Idempotency-Key = str(message.id)`, or `celery_app.send_task`.
- Polling interval trades latency for DB load; with PostgreSQL, `LISTEN/NOTIFY` (sent `on_commit`) can wake the
  relay early — the poll stays as the safety net.

### 5.3 Inbox (consumer-side deduplication)

```python
# apps/integrations/models.py
class InboxMessage(models.Model):
    source = models.CharField(max_length=64)            # "orders-topic", "partner_crm"
    message_id = models.CharField(max_length=128)
    received_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "message_id"], name="inbox_source_msg_uniq")]
```

```python
# apps/integrations/inbox.py
from collections.abc import Callable

from django.db import IntegrityError, transaction

from .models import InboxMessage


def handle_once(*, source: str, message_id: str, handler: Callable[[], None]) -> bool:
    """Run handler's DB effects at most once per (source, message_id). Returns False for duplicates."""
    try:
        with transaction.atomic():
            InboxMessage.objects.create(source=source, message_id=message_id)
            handler()                      # same transaction: rollback removes the inbox row too
    except IntegrityError:
        # Inner savepoint would be needed if handler itself may raise IntegrityError for other reasons.
        if InboxMessage.objects.filter(source=source, message_id=message_id).exists():
            return False
        raise
    return True
```

Acknowledge the broker message **after** `handle_once` returns (whether True or False). Webhooks use the
same idea with `WebhookEvent` — see [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md).
Retention: keep inbox rows longer than the maximum redelivery/replay window of the source.

### 5.4 Saga orchestrated by a process manager

```python
# apps/fulfilment/models.py
class FulfilmentSaga(models.Model):
    class State(models.TextChoices):
        STARTED = "started"; STOCK_RESERVED = "stock_reserved"; PAID = "paid"
        SHIPPED = "shipped"; COMPENSATING = "compensating"; FAILED = "failed"; COMPLETED = "completed"

    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT)
    state = models.CharField(max_length=20, choices=State, default=State.STARTED)
    reservation_id = models.CharField(max_length=64, blank=True)
    payment_id = models.CharField(max_length=64, blank=True)
    deadline_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=0)       # optimistic concurrency
    updated_at = models.DateTimeField(auto_now=True)
```

```python
# apps/fulfilment/process.py
from django.db import transaction

from apps.integrations.errors import PermanentIntegrationError, TransientIntegrationError

# Each step function returns (next_state, fields_to_store) and uses idempotency key f"saga:{id}:{step}".
STEPS = {
    FulfilmentSaga.State.STARTED: reserve_stock,
    FulfilmentSaga.State.STOCK_RESERVED: take_payment,
    FulfilmentSaga.State.PAID: request_shipment,
}


def advance(saga_id: int) -> None:
    """Idempotent step executor; called by a task after each transition or by a timeout sweeper."""
    saga = FulfilmentSaga.objects.get(pk=saga_id)
    step = STEPS.get(saga.state)
    if step is None:
        return
    try:
        result_state, fields = step(saga)
    except TransientIntegrationError:
        raise                                                # task retry with backoff
    except PermanentIntegrationError:
        _transition(saga, FulfilmentSaga.State.COMPENSATING)
        compensate(saga.pk)
        return
    _transition(saga, result_state, **fields)
    transaction.on_commit(lambda: advance_task.delay(saga.pk))


def _transition(saga: FulfilmentSaga, state: str, **fields) -> None:
    updated = (FulfilmentSaga.objects
               .filter(pk=saga.pk, version=saga.version)
               .update(state=state, version=saga.version + 1, **fields))
    if not updated:
        raise ConcurrentTransition(saga.pk)                  # another worker moved it; re-read and stop


def compensate(saga_id: int) -> None:
    saga = FulfilmentSaga.objects.get(pk=saga_id)
    if saga.payment_id:
        payment_gateway().refund(payment_id=saga.payment_id, idempotency_key=f"saga:{saga.pk}:refund")
    if saga.reservation_id:
        inventory_gateway().release(reservation_id=saga.reservation_id,
                                    idempotency_key=f"saga:{saga.pk}:release")
    _transition(saga, FulfilmentSaga.State.FAILED)
```

Rules: every step and every compensation is idempotent; the saga row is the single source of truth for
progress; a periodic sweeper picks sagas past `deadline_at` and either retries or compensates; compensations
that themselves fail permanently go to a human queue (admin action + alert). For workflow engines (Temporal
[third-party: temporalio], AWS Step Functions, Azure Durable Functions / Logic Apps) consider them once
you have many long-running flows — they provide durable timers and history at the cost of a new runtime.

### 5.5 Choreography with domain events

Publish facts via the outbox (`orders.order_placed.v1`); each consumer app handles them through the inbox and
publishes its own facts (`payments.payment_captured.v1`). In-process reactions inside the monolith should use
explicit service calls or in-process domain events dispatched `on_commit`
([domain-events.md](../../django-architecture/references/domain-events.md)), not a broker round-trip.

### 5.6 Strangler Fig with a Django facade

```python
# apps/customers/selectors.py
from django.conf import settings

from .integrations.legacy_erp.gateway import LegacyErpCustomerGateway


def get_customer(customer_id: str) -> CustomerDTO:
    if customer_id in settings.CUSTOMERS_MIGRATED_PREFIXES or flag_enabled("customers.native_read"):
        return CustomerDTO.from_model(Customer.objects.get(external_ref=customer_id))
    return LegacyErpCustomerGateway().fetch(customer_id)    # ACL maps legacy SOAP shapes
```

Migrate one capability at a time: (1) facade in front of legacy, (2) shadow-read and compare (log
mismatches), (3) switch reads, (4) move writes with the new system as SoR and sync back to legacy if others
still read it, (5) remove the legacy path. See [soap-and-legacy-integrations.md](soap-and-legacy-integrations.md).

### 5.7 API composition

```python
import asyncio

import httpx


async def customer_overview(customer_id: str) -> dict:
    async with asyncio.timeout(2.0):                      # total budget for the composed read
        profile, invoices, tickets = await asyncio.gather(
            crm.get_profile(customer_id),
            billing.list_invoices(customer_id),
            support.list_tickets(customer_id),
            return_exceptions=True,
        )
    return {
        "profile": None if isinstance(profile, Exception) else profile,
        "invoices": [] if isinstance(invoices, Exception) else invoices,
        "tickets_unavailable": isinstance(tickets, Exception),   # partial result, explicit to the caller
    }
```

Composition needs async clients (or threads), a total deadline, and an explicit partial-result contract.
When the same composed view is read often, replace composition with a synchronised local read model
([data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)).

## 6. Production use cases

| Use case | Patterns |
|---|---|
| Checkout → payment → stock → shipment across three providers | Orchestrated saga + idempotency keys + reconciliation |
| Publishing order events to warehouse and analytics | Outbox → broker; consumers use Inbox |
| Replacing a SOAP ERP over 18 months | Strangler Fig + ACL + data sync with per-entity SoR |
| Customer 360 page across CRM, billing, support | API composition with budget and partial results; read model later |
| Swapping SMS provider after an outage | Ports and Adapters with a failover adapter |
| Bank settlement import | Gateway over SFTP + ACL mapping file records to domain ledger entries |

Failure scenario: the relay published `order_placed` but crashed before marking it; on restart it publishes
again — the warehouse consumer's inbox drops the duplicate. Without the inbox the warehouse ships twice.

## 7. Advantages and disadvantages

| Pattern | Advantages | Disadvantages |
|---|---|---|
| Ports and Adapters / ACL | Testable, provider-swappable, domain protected | Extra layers, mapping code to maintain |
| Outbox | No lost messages; atomic with business data | Relay to operate, duplicates, table growth, latency of polling |
| Inbox | Effectively-once DB effects | Storage, retention decisions; does not cover remote side effects |
| Saga / process manager | Consistency without 2PC, visible state | Compensation design is hard; intermediate states visible to users |
| Choreography | Loose coupling | Hidden flow, hard debugging |
| Strangler Fig | Incremental, low-risk | Long dual-running period, two SoRs risk |
| API composition | Fresh data, no sync infrastructure | Latency = slowest dependency; availability multiplies |

## 8. Security and performance considerations

- Outbox payloads and inbox rows may contain PII: minimise payload content (ids + needed fields), encrypt or
  avoid sensitive fields, apply retention and pruning. Headers must never carry credentials.
- Saga compensations that move money require audit records (who/why/when) — see
  [payment-and-financial-integrations.md](payment-and-financial-integrations.md).
- Relay performance: partial index on pending rows, small batches, short publish timeouts, batched deletes.
  Long-held `FOR UPDATE` locks plus large tables cause bloat; monitor table size and relay lag
  ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).
- API composition fans out N requests per page view: cap concurrency and cache per-dependency results
  ([caching-engineering.md](../../django-performance/references/caching-engineering.md)).

## 9. Common mistakes and anti-patterns

- **"Outbox" that publishes inside the transaction** — defeats the purpose; publish only from the relay.
- **Assuming the outbox gives exactly-once** — it gives at-least-once; consumers must dedupe.
- **Multiple relay workers with ordering assumptions** — `skip_locked` reorders.
- **Compensation as `DELETE`** — compensations are business actions recorded for audit, not row deletions.
- **Irreversible step first** (send confirmation email, then charge card).
- **Choreography loops** — service A reacts to B's event that was caused by A's event, forever.
- **Leaky ACL** — provider enums or ids used in domain conditionals (`if charge.status == "requires_capture"`).
- **Abstraction for abstraction's sake** — ports and factories for a single CSV export used once a quarter.
- **XA/2PC across Django and SaaS** or "best-effort 2PC" with manual prepare/commit HTTP calls.
- **Strangler without a SoR plan** — both systems accept writes for the same entity.

## 10. Troubleshooting and validation

| Symptom | Check |
|---|---|
| Outbox lag growing | `SELECT count(*), min(created_at) FROM integrations_outboxmessage WHERE published_at IS NULL`; relay running? publish errors in `last_error`? lock waits? |
| Same event processed twice | Inbox constraint present? Handler and inbox insert in the same transaction? Message id stable across redeliveries (use outbox `id`, not broker delivery tag)? |
| Events out of order | Multiple relays / partitions; key chosen correctly; consumer tolerates reordering via version checks |
| Sagas stuck in intermediate state | Sweeper running? Deadlines set? `ConcurrentTransition` noise? Count sagas per state older than SLA |
| Compensation failed | Human queue / alert; idempotency key reused on retry |

Validation checklist:

- [ ] Every cross-system effect uses one of: idempotent direct call, `on_commit` task, or outbox — choice justified.
- [ ] Outbox rows written in the same `atomic()` block as the business change; relay is the only publisher.
- [ ] Consumers dedupe with an inbox or naturally idempotent writes.
- [ ] Saga steps and compensations are idempotent with deterministic keys; irreversible steps last.
- [ ] Dead-lettered outbox rows and stuck sagas are alerted and replayable.
- [ ] Tests cover relay crash after publish, duplicate delivery and compensation paths
  ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [integration-engineering-fundamentals.md](integration-engineering-fundamentals.md) — when these patterns apply
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — publishers/consumers for the outbox and inbox
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — running relays, sagas and sweepers
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — idempotency keys, retries
- [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md) — read models, reconciliation
- django-architecture: [design-patterns.md](../../django-architecture/references/design-patterns.md),
  [event-driven-architecture.md](../../django-architecture/references/event-driven-architecture.md),
  [distributed-systems.md](../../django-architecture/references/distributed-systems.md),
  [domain-events.md](../../django-architecture/references/domain-events.md),
  [dependency-management.md](../../django-architecture/references/dependency-management.md)
- Transactions and locking: [django-orm](../../django-orm/SKILL.md),
  [postgresql-engineering](../../postgresql-engineering/SKILL.md)

## 12. Official documentation and external references

- Django `select_for_update(skip_locked=True)`: <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update>
- Django transactions / `on_commit`: <https://docs.djangoproject.com/en/5.2/topics/db/transactions/>
- PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`: <https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
- PostgreSQL `PREPARE TRANSACTION`: <https://www.postgresql.org/docs/current/sql-prepare-transaction.html>
- PostgreSQL `LISTEN`/`NOTIFY`: <https://www.postgresql.org/docs/current/sql-notify.html>
- Transactional outbox (microservices.io): <https://microservices.io/patterns/data/transactional-outbox.html>
- Saga pattern (microservices.io): <https://microservices.io/patterns/data/saga.html>
- Azure Architecture Center — Saga, Strangler Fig, Anti-corruption Layer, Compensating Transaction:
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/saga>,
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/strangler-fig>,
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer>,
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/compensating-transaction>
- Enterprise Integration Patterns — Process Manager: <https://www.enterpriseintegrationpatterns.com/patterns/messaging/ProcessManager.html>
- Debezium (CDC) [third-party]: <https://debezium.io/documentation/>
- Martin Fowler, "StranglerFigApplication": <https://martinfowler.com/bliki/StranglerFigApplication.html>
