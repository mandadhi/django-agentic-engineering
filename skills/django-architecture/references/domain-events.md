# Domain events

> Scope: defining, raising and dispatching domain events inside a Django monolith, how events relate to transactions and `transaction.on_commit()`, consistency, idempotent handlers, and domain events vs Django signals. Not covered here: brokers, outbox and cross-process delivery ([event-driven-architecture.md](./event-driven-architecture.md)), sagas ([distributed-systems.md](./distributed-systems.md)), signal mechanics ([django-core signals](../../django-core/references/signals-management-commands.md)), transaction details ([django-orm](../../django-orm/SKILL.md)).

## Purpose

A domain event records that **something meaningful happened in the business**, e.g.
`OrderPlaced`, `InvoicePaid`, `StockDepleted`. It lets the owning app announce a fact without
importing the apps that care about it. That preserves dependency direction: `orders` (feature)
can announce to `notifications` or `reporting` without importing them, and a lower app
(`inventory`) can notify a higher one (`orders`) without an upward import.

Use domain events for **secondary reactions** (notify, project, audit, sync). Keep the primary
use case, the part that must succeed or fail together, as explicit calls in the service.

## Technical concepts

- **Event**: immutable, past-tense, carries identifiers and the minimal facts handlers need. No model instances: they are mutable, lazy and transaction-bound.
- **Publisher**: the service that performed the state change.
- **Handler**: a function subscribed to an event type; it lives in the reacting app.
- **Dispatch timing**: *immediate* (inside the transaction) or *after commit* (`on_commit`). Default to after commit.
- **In-process vs out-of-process**: in-process handlers run in the same worker; out-of-process means a task queue or broker (see [event-driven-architecture.md](./event-driven-architecture.md)).

## Internal mechanics

A minimal, dependency-free registry in the platform tier:

```python
# apps/platform/events.py
from collections import defaultdict
from typing import Callable, TypeVar
import logging
from django.db import transaction

logger = logging.getLogger(__name__)
E = TypeVar("E")
_handlers: dict[type, list[Callable]] = defaultdict(list)

def subscribe(event_type: type[E]):
    def register(fn: Callable[[E], None]):
        _handlers[event_type].append(fn)
        return fn
    return register

def publish(event) -> None:
    """Dispatch handlers after the current transaction commits (immediately if none is open)."""
    for handler in _handlers[type(event)]:
        transaction.on_commit(lambda h=handler: _run(h, event))

def _run(handler, event) -> None:
    try:
        handler(event)
    except Exception:
        logger.exception("event_handler_failed", extra={"event": type(event).__name__,
                                                         "handler": handler.__qualname__})
```

Why `on_commit` for every handler: if the transaction rolls back, the event never happened, so
handlers must not run. Outside `atomic()`, `on_commit` runs the callback immediately. Django ≥ 4.2
also offers `transaction.on_commit(func, robust=True)`, which logs and continues if a callback
raises; the wrapper above does the same explicitly and works on older versions.

```mermaid
sequenceDiagram
    participant V as View
    participant S as orders.place_order
    participant DB as Database
    participant R as platform.events
    participant H as notifications handler
    V->>S: place_order(...)
    S->>DB: BEGIN; INSERT order, lines
    S->>R: publish(OrderPlaced)
    R-->>R: on_commit(queue handler)
    S->>DB: COMMIT
    DB-->>R: commit hook fires
    R->>H: handle(OrderPlaced)
```

Handlers register at import time, so the reacting app imports its handlers module in
`AppConfig.ready()` (see [django-core models/app lifecycle](../../django-core/references/models-application-lifecycle.md)).

## Decision guidance

| Situation | Use |
|---|---|
| Step must succeed or the use case fails (reserve stock, create invoice lines) | Explicit service call inside the transaction |
| Reaction may fail independently (email, analytics, cache bust) | Domain event, after commit |
| Lower-tier app must notify a higher-tier app | Domain event (no upward import) |
| Reaction is slow or calls external APIs | Domain event whose handler enqueues a task |
| Another process/service must receive it reliably | Outbox + broker ([event-driven-architecture.md](./event-driven-architecture.md)) |
| Reacting to framework events (login, migrations) | Django signals |

### Domain events vs Django signals

| | Domain events (`apps.platform.events`) | Django model signals (`post_save`) |
|---|---|---|
| Meaning | Business fact (`OrderPlaced`) | Persistence fact (a row was saved) |
| Fired by | Explicit `publish()` in a service | Every `save()`, including admin, fixtures, shell |
| Missed by | Nothing that goes through the service | `bulk_create`, `update()`, raw SQL |
| Payload | Typed, frozen dataclass | `instance`, `created`, `**kwargs` |
| Transaction awareness | Built in (on_commit) | None: fires inside the transaction |

Signals remain right for framework hooks and for reusable apps that cannot know their callers.
For your own business flow, domain events are explicit, typed and transaction-aware.

## Django implementation examples

**Define events in the publishing app.**

```python
# apps/orders/events.py
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True, slots=True)
class OrderPlaced:
    order_id: int
    customer_id: int
    total: Decimal

@dataclass(frozen=True, slots=True)
class OrderCancelled:
    order_id: int
    reason: str
```

**Raise in the service, after the state change.**

```python
# apps/orders/services.py
from apps.platform.events import publish
from .events import OrderPlaced

@transaction.atomic
def place_order(*, customer: Customer, lines: list[LineInput]) -> Order:
    order = Order.objects.create(customer=customer, status=Order.Status.PENDING)
    for line in lines:
        reserve_stock(product_id=line.product_id, quantity=line.quantity)   # primary: explicit
        OrderLine.objects.create(order=order, product_id=line.product_id, quantity=line.quantity)
    order.recalculate_total()
    publish(OrderPlaced(order_id=order.pk, customer_id=customer.pk, total=order.total))  # secondary
    return order
```

**Handle in the reacting app; push slow work to a task.**

```python
# apps/notifications/handlers.py
from apps.platform.events import subscribe
from apps.orders.events import OrderPlaced
from .tasks import send_order_confirmation

@subscribe(OrderPlaced)
def queue_confirmation(event: OrderPlaced) -> None:
    send_order_confirmation.delay(order_id=event.order_id)

# apps/notifications/apps.py
class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    def ready(self):
        from . import handlers  # noqa: F401
```

**Idempotent handler.** Handlers may run twice (task retries, redelivery). Make the effect
idempotent with a unique constraint rather than a "check then insert" race:

```python
class SentNotification(models.Model):
    kind = models.CharField(max_length=50)
    order_id = models.BigIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "order_id"], name="uniq_notification")]

def send_confirmation_once(order_id: int) -> None:
    with transaction.atomic():
        _, created = SentNotification.objects.get_or_create(kind="order_confirmation", order_id=order_id)
        if not created:
            return
    send_email(...)   # at-least-once: a crash here means the email is not resent; choose deliberately
```

Order the marker and side effect according to which failure is worse: a duplicate email
(mark after sending) or a missing email (mark before sending).

**Bad: handler re-entering the publisher's transaction semantics.**

```python
@subscribe(OrderPlaced)
def charge(event):                       # payment is primary flow, not a reaction
    charge_invoice(order_id=event.order_id)
```

If payment failure must fail the order, it belongs in the service, not a fire-and-forget handler.

## Production use cases

- `OrderPlaced` → confirmation email task, analytics projection, loyalty points.
- `StockDepleted` (inventory, lower tier) → orders app pauses listings without inventory importing orders.
- `InvoicePaid` → fulfillment queued; audit log entry.
- Cache invalidation after a write: handler deletes keys post-commit, never pre-commit (avoids repopulating stale data).

## Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Decouples apps; preserves dependency direction | Control flow is less visible than direct calls |
| Rollback-safe when dispatched on commit | In-process handlers are lost if the process dies after commit |
| Typed payloads, easy to test | Handler failures are silent unless logged/alerted |
| Natural path to outbox/broker later | Event proliferation if every save becomes an event |

If a handler *must* run even after a crash, in-process `on_commit` is not enough: persist the
event in the outbox within the same transaction.

## Security and performance considerations

- Events carry ids, not PII-rich snapshots; handlers re-read what they need with proper scoping.
- `on_commit` handlers run synchronously in the request after commit: they add to response latency. Keep them to enqueueing or cheap work.
- Handler exceptions after commit cannot roll back the write; never let them surface as a 500 for a request whose data was saved (the wrapper catches and logs).
- In async views, `on_commit` callbacks still run synchronously in the thread that committed; keep them non-blocking.

## Common mistakes

- Publishing **before** the state change or outside `on_commit`: handlers see uncommitted or rolled-back data.
- Passing model instances in events; handlers then operate on stale or unsaved objects.
- Putting must-succeed steps in handlers.
- Handlers that publish further events in long chains, which become hidden workflows. Prefer a service or saga.
- Registering handlers in `models.py` (import order surprises) instead of `ready()`.
- Assuming exactly-once delivery.

## Troubleshooting and evaluation

| Symptom | Likely cause |
|---|---|
| Handler never runs in tests | `TestCase` wraps tests in a transaction; use `self.captureOnCommitCallbacks(execute=True)` (Django ≥ 3.2) or `TransactionTestCase` |
| Handler never runs in prod | Handlers module not imported in `ready()`; event class imported from two paths |
| Handler sees missing rows | Dispatched before commit, or the task reads from a lagging replica |
| Duplicate emails | Task retried without idempotency key |

```python
def test_place_order_publishes(self):
    with self.captureOnCommitCallbacks(execute=True) as callbacks:
        place_order(customer=self.customer, lines=[LineInput(product_id=self.p.pk, quantity=1)])
    assert len(callbacks) == 1
```

## Related knowledge

[event-driven-architecture.md](./event-driven-architecture.md) · [service-layer.md](./service-layer.md) ·
[design-patterns.md](./design-patterns.md) (Observer) · [domain-driven-design.md](./domain-driven-design.md) ·
[django-core signals & commands](../../django-core/references/signals-management-commands.md) ·
[django-testing](../../django-testing/SKILL.md)

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/db/transactions/#performing-actions-after-commit
- https://docs.djangoproject.com/en/stable/topics/signals/
- https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TestCase.captureOnCommitCallbacks
- https://martinfowler.com/eaaDev/DomainEvent.html
