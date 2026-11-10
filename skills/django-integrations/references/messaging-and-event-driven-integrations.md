# Messaging and Event-Driven Integrations

**Scope:** How Django applications publish to and consume from message brokers (RabbitMQ, Apache Kafka,
Azure Service Bus, Redis) — delivery semantics, acknowledgement, ordering, dead-lettering, envelopes, schema
evolution, replay and backpressure. It does not decide *whether* a system should be event-driven
([django-architecture](../../django-architecture/references/event-driven-architecture.md)), does not cover
Celery as a task queue ([celery-and-background-integrations.md](celery-and-background-integrations.md)), and
does not implement the outbox/inbox tables ([integration-architecture-patterns.md](integration-architecture-patterns.md)).

## 1. Purpose and scope

Use this reference when a Django service must exchange messages with other systems through a broker: emitting
domain events to other teams, consuming partner event streams, integrating with an enterprise bus, or
replacing polling with push. It explains what each broker actually guarantees, what the application must add
on top, and how to run producers and consumers inside a Django codebase.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Broker client libraries are
third-party: [third-party: pika], [third-party: aio-pika], [third-party: kombu], [third-party: confluent-kafka],
[third-party: azure-servicebus], [third-party: redis]. Broker behaviour quoted here is version- and
configuration-dependent; confirm against the broker version actually deployed.

Out of scope: broker cluster operations (sizing, replication tuning), Celery internals, and the architectural
choice between orchestration and choreography (see
[integration-architecture-patterns.md](integration-architecture-patterns.md)).

## 2. Core concepts

### Queue versus topic

| Model | Delivery | Typical broker | Use for |
|---|---|---|---|
| Queue (point-to-point) | Each message to **one** competing consumer | RabbitMQ queue, SQS, Service Bus queue | Work distribution, commands |
| Topic / publish-subscribe | Each message to **every** subscription; competing consumers within a subscription | RabbitMQ fanout/topic exchange, Service Bus topic, SNS, Kafka topic + consumer groups | Events with several independent consumers |
| Log (partitioned, retained) | Consumers read by offset; messages retained independent of consumption | Kafka, Event Hubs, Redis Streams | Replayable event streams, high throughput, ordering per key |

A **command** asks one owner to do something ("CapturePayment"); an **event** states a fact that already
happened ("PaymentCaptured"). Commands go to a queue owned by the receiver; events go to a topic owned by the
producer. Mixing these (publishing commands to a topic "for anyone") creates hidden coupling.

### Delivery semantics

| Semantic | Mechanism | Failure mode |
|---|---|---|
| At-most-once | Ack before processing (auto-ack) | Crash after ack loses the message |
| At-least-once | Ack after processing | Crash before ack redelivers → **duplicates** |
| "Exactly-once" | Broker-internal transactions (Kafka EOS) or dedupe windows (Service Bus duplicate detection) | Only covers the broker boundary, never your DB + external side effects |

**Broker capability ≠ application guarantee.** Kafka's idempotent producer and transactions give
exactly-once *within Kafka* (read-process-write between topics). Service Bus duplicate detection drops
re-sent messages with the same `MessageId` within a window. Neither makes your Django handler — which writes
PostgreSQL and maybe calls an HTTP API — run exactly once. Business-level exactly-once *effect* is achieved
only by **at-least-once delivery + idempotent consumer** (inbox dedupe or naturally idempotent writes).

### Acknowledgement, visibility and lock

- **RabbitMQ:** consumer acks/nacks per delivery tag; unacked messages are redelivered when the channel closes.
  `basic.qos(prefetch_count)` bounds in-flight messages per consumer. A consumer ack timeout exists
  (default 30 minutes in recent versions `VERIFY`).
- **SQS:** a received message becomes invisible for the *visibility timeout*; if not deleted in time it
  reappears. Long handlers must extend visibility.
- **Service Bus (peek-lock):** message locked for a lock duration; complete/abandon/dead-letter explicitly;
  lock renewal needed for long processing.
- **Kafka:** no per-message ack; consumers commit **offsets** per partition. Committing offset N means
  "everything before N is done". A slow message blocks progress of its partition.

### Ordering

Ordering is only ever guaranteed within a scope: a Kafka partition (choose the key), a Service Bus session,
an SQS FIFO message group, a single RabbitMQ queue with one consumer (and no requeue). Competing consumers,
retries and DLQ redrive all break global order. Design consumers to be **order-tolerant** (compare versions or
timestamps) rather than relying on order across scopes.

### Dead-letter queues and poison messages

A **poison message** fails deterministically (bad schema, missing reference, bug). Without a cap it is
redelivered forever, blocking a partition or burning CPU. A **dead-letter queue (DLQ)** receives messages that
exceeded max delivery count, expired, or were explicitly rejected. A DLQ is useless without monitoring,
an owner and a redrive procedure.

### Event versioning and schema evolution

Consumers deploy independently of producers, so every change must be compatible across the deploy window:

- **Backward compatible** (new consumer reads old messages): add optional fields with defaults.
- **Forward compatible** (old consumer reads new messages): consumers ignore unknown fields.
- **Breaking**: rename/remove/retype a field, change meaning → new event type or major version
  (`order.placed.v2`), published in parallel until consumers migrate.

Schema registries ([third-party: Confluent Schema Registry], Azure Schema Registry) enforce compatibility
rules for Avro/Protobuf/JSON Schema at publish time.

### Replay and backpressure

- **Replay**: logs (Kafka, Event Hubs, Redis Streams) can re-read from an offset within retention; queues
  cannot — once acked, gone. If replay matters on a queue broker, persist events yourself (outbox table
  retention, event archive).
- **Backpressure**: consumers pull at their own pace (prefetch/`max.poll.records`); producers face broker
  limits (RabbitMQ flow control/memory alarms, Service Bus throttling, Kafka quotas). Unbounded in-memory
  buffering in the producer converts backpressure into an OOM.

## 3. Internal mechanics

### RabbitMQ (AMQP 0-9-1)

Producers publish to an **exchange** with a routing key; **bindings** route to queues (direct, topic, fanout,
headers). Durability needs three things: durable exchange, durable queue (or quorum queue), and persistent
messages (`delivery_mode=2`). **Publisher confirms** tell the producer the broker accepted the message; without
them a network blip silently drops messages. **Quorum queues** (RabbitMQ ≥ 3.8) replicate via Raft and support
a `delivery-limit` that dead-letters poison messages. Dead-lettering is configured with `x-dead-letter-exchange`.

### Apache Kafka

A topic has partitions; each partition is an ordered, replicated log. The producer chooses the partition from
the key hash. A **consumer group** assigns each partition to one consumer; adding consumers beyond the
partition count adds no throughput. Rebalances pause consumption and can cause duplicate processing of
uncommitted messages. Producer durability: `acks=all`, `enable.idempotence=true` (default true in recent
librdkafka/Kafka clients `VERIFY`), `min.insync.replicas` ≥ 2 on the topic. Kafka has no built-in DLQ — the
application publishes failed records to a `<topic>.dlq` topic.

### Azure Service Bus (concept level)

Namespaces contain queues and topics/subscriptions. Peek-lock delivery, max delivery count → built-in DLQ
subqueue, **sessions** for ordered FIFO per session id, scheduled messages, duplicate detection window,
subscription filters. SDK specifics (`ServiceBusClient`, `AutoLockRenewer`, managed identity) live in
[azure-integrations.md](azure-integrations.md).

### Redis as a broker — limitations

| Feature | Pub/Sub | Lists (`LPUSH`/`BRPOP`) | Streams (`XADD`/`XREADGROUP`) |
|---|---|---|---|
| Persistence | None — offline subscribers miss messages | In memory, persistence per RDB/AOF config | Same as lists |
| Ack | No | No (use `BLMOVE` reliable-queue pattern) | Yes (`XACK`, pending entries list) |
| Consumer groups | No | No | Yes |
| Replay | No | No | Yes, within `MAXLEN` trimming |
| DLQ | No | Manual | Manual (`XPENDING` + `XCLAIM`/`XAUTOCLAIM` + delivery count) |

Redis is acceptable for low-stakes, internal, short-lived messaging where Redis is already operated with
persistence and failover. Data loss on failover (async replication) and memory-bound retention make it a poor
choice for business-critical or cross-team events.

### Message lifecycle in a Django consumer

```text
broker ──deliver──► consumer process (management command)
                      │ 1. parse envelope, validate schema
                      │ 2. BEGIN; INSERT InboxMessage(source, message_id) ON CONFLICT DO NOTHING
                      │    └─ conflict → duplicate: COMMIT, ack, done
                      │ 3. call domain service (same transaction)
                      │ 4. COMMIT (side effects queued via on_commit)
                      │ 5. ack / commit offset
                      └ failure: transient → nack/requeue with delay; permanent → DLQ; then ack
```

Ack happens **after** the database commit. Crash between commit and ack → redelivery → inbox dedupe absorbs it.

## 4. Engineering decisions and trade-offs

### Do you need a broker at all?

| Situation | Prefer |
|---|---|
| Background work inside one Django system | Celery or a DB-backed queue — not an enterprise broker |
| Notify one known external system | Direct HTTP call or webhook from a task, with retries |
| Several independent consumers, owned by other teams | Topic on a managed broker |
| High-volume stream, replay, ordering per key | Kafka / Event Hubs |
| Enterprise Azure estate, sessions, scheduled delivery | Azure Service Bus |
| Already-operated RabbitMQ, flexible routing | RabbitMQ (quorum queues) |
| Fire-and-forget cache invalidation between web processes | Redis Pub/Sub (loss acceptable) |

A broker adds an always-on dependency, schema governance, DLQ operations and on-call surface. Justify it with
fan-out, decoupled availability or throughput — not fashion. Record the choice in an ADR
([architecture-decision-records](../../django-architecture/references/architecture-decision-records.md)).

### Broker comparison

| Concern | RabbitMQ | Kafka | Azure Service Bus | Redis Streams |
|---|---|---|---|---|
| Model | Queues + exchanges | Partitioned log | Queues + topics | Log in memory |
| Retention after consume | No | Yes (time/size) | No | Until trimmed |
| Ordering | Per queue (single consumer) | Per partition | Per session | Per stream |
| DLQ | Built-in (DLX) | Application-built | Built-in | Application-built |
| Delayed delivery | Plugin / TTL+DLX | No (application) | Scheduled messages | No |
| Ops burden | Medium (self-hosted) | High self-hosted; lower managed | Low (PaaS) | Low if Redis exists |

### Publishing: transactional consistency

Publishing directly after `save()` has two failure modes: DB commits but publish fails (event lost), or
publish succeeds and DB rolls back (phantom event). Options:

1. **`transaction.on_commit(publish)`** — removes phantom events; still loses events if the process dies
   between commit and publish. Acceptable for non-critical notifications.
2. **Transactional outbox** — write `OutboxMessage` in the same transaction; a relay publishes and marks
   `published_at`. At-least-once, no loss. Required for business-critical events. Implementation:
   [integration-architecture-patterns.md](integration-architecture-patterns.md).
3. **CDC on the outbox table** (Debezium) — relay without polling; more infrastructure.

### Message size and payload strategy

Event-carried state transfer (full snapshot in the event) reduces callbacks but grows payloads and leaks
data to all subscribers. Thin events ("order 123 changed, version 7") force consumers to call back,
coupling availability. For large data use the **claim-check** pattern: store the payload in object storage,
publish a reference ([file-transfer-and-object-storage.md](file-transfer-and-object-storage.md)). Broker size
limits are low (Service Bus Standard 256 KB `VERIFY`, Kafka default ~1 MB `VERIFY`, SQS 256 KB `VERIFY`).

### Consumer hosting

| Option | Fits | Watch |
|---|---|---|
| Management command per consumer, run as its own deployment | Most Django consumers | Graceful shutdown, health, DB connection hygiene |
| Celery worker consuming a non-Celery queue (kombu `ConsumerStep`) | Teams already running only Celery | Mixing task and message semantics |
| Serverless trigger (Azure Functions, Lambda) calling a Django API | Spiky volumes | Cold starts, auth between function and API |
| Async consumer (aio-pika, `aiokafka`) | Very high I/O concurrency | Django ORM is sync-first; use `sync_to_async` carefully |

## 5. Django implementation patterns

### Envelope

Every message carries metadata separate from the business payload:

```python
# apps/integrations/messaging/envelope.py
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Envelope:
    type: str                      # "orders.order_placed"
    version: int                   # schema major version
    source: str                    # "django.orders"
    data: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))   # dedupe key
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    subject: str | None = None     # aggregate id, also the partition/session key
    correlation_id: str | None = None
    causation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

This mirrors CloudEvents attributes (`id`, `source`, `type`, `time`, `subject`); adopting the CloudEvents spec
([third-party: cloudevents]) helps when crossing organisational boundaries.

### Producer port and adapters

```python
# apps/integrations/messaging/ports.py
from typing import Protocol

from .envelope import Envelope


class EventPublisher(Protocol):
    def publish(self, topic: str, envelope: Envelope, *, key: str | None = None) -> None: ...
```

```python
# apps/integrations/messaging/kafka.py   [third-party: confluent-kafka]
import json
import logging

from confluent_kafka import KafkaException, Producer
from django.conf import settings

from apps.integrations.errors import TransientIntegrationError
from .envelope import Envelope

logger = logging.getLogger(__name__)


class KafkaPublisher:
    def __init__(self) -> None:
        self._producer = Producer({
            "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
            "acks": "all",
            "enable.idempotence": True,
            "delivery.timeout.ms": 30_000,
            "security.protocol": "SASL_SSL",
            **settings.KAFKA_AUTH,          # from env / secret store, never literals
        })

    def publish(self, topic: str, envelope: Envelope, *, key: str | None = None) -> None:
        errors: list[Exception] = []

        def on_delivery(err, msg) -> None:
            if err is not None:
                errors.append(KafkaException(err))

        self._producer.produce(
            topic,
            key=(key or envelope.subject or "").encode(),
            value=json.dumps(envelope.to_dict()).encode(),
            headers={"ce_type": envelope.type, "correlation_id": envelope.correlation_id or ""},
            on_delivery=on_delivery,
        )
        remaining = self._producer.flush(10)   # synchronous confirm for outbox relay semantics
        if remaining or errors:
            raise TransientIntegrationError(
                provider="kafka", operation=f"publish:{topic}", status_code=None, request_id=envelope.id
            )
```

Flushing per message is slow; an outbox relay should produce a batch, then flush once and mark only the
confirmed rows as published.

```python
# apps/integrations/messaging/rabbitmq.py   [third-party: pika]
import json

import pika
from django.conf import settings

from .envelope import Envelope


class RabbitPublisher:
    def __init__(self) -> None:
        params = pika.URLParameters(settings.RABBITMQ_URL)   # amqps:// in production
        self._conn = pika.BlockingConnection(params)
        self._channel = self._conn.channel()
        self._channel.confirm_delivery()                       # publisher confirms

    def publish(self, topic: str, envelope: Envelope, *, key: str | None = None) -> None:
        # topic = exchange name; key = routing key, e.g. "orders.order_placed.v1"
        self._channel.basic_publish(
            exchange=topic,
            routing_key=key or f"{envelope.type}.v{envelope.version}",
            body=json.dumps(envelope.to_dict()).encode(),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.DeliveryMode.Persistent,
                message_id=envelope.id,
                correlation_id=envelope.correlation_id,
            ),
            mandatory=True,     # unroutable → UnroutableError instead of silent drop
        )
```

`pika.BlockingConnection` is not thread-safe; one connection per process/thread. Do not open it at import time
in web workers.

### Consumer as a management command with graceful shutdown

```python
# apps/orders/management/commands/consume_partner_events.py
import json
import logging
import signal

import time

from confluent_kafka import Consumer, KafkaError, TopicPartition
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.integrations.errors import PermanentIntegrationError, TransientIntegrationError
from apps.orders.integrations.partner.handlers import handle_partner_event
from apps.integrations.messaging.dlq import send_to_dlq

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume partner order events from Kafka."

    def handle(self, *args, **options) -> None:
        self._running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        consumer = Consumer({
            "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "orders-partner-events",
            "enable.auto.commit": False,          # commit only after DB commit
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": 300_000,
            **settings.KAFKA_AUTH,
        })
        consumer.subscribe(["partner.orders.v1"])
        try:
            while self._running:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() != KafkaError._PARTITION_EOF:
                        logger.error("kafka.consume_error", extra={"error": str(msg.error())})
                    continue
                close_old_connections()           # long-lived process: honour CONN_MAX_AGE / dead conns
                try:
                    handle_partner_event(json.loads(msg.value()))
                except PermanentIntegrationError:
                    send_to_dlq("partner.orders.v1.dlq", msg)
                except TransientIntegrationError:
                    # Do not commit; seek back so the record is retried after a pause.
                    consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
                    self._pause_briefly()
                    continue
                consumer.commit(message=msg, asynchronous=False)
        finally:
            consumer.close()                      # leaves group cleanly → faster rebalance

    def _stop(self, *_):
        self._running = False

    def _pause_briefly(self) -> None:
        time.sleep(2)   # simple; for long outages pause() the partition while a breaker is open
```

Key points: `close_old_connections()` per message because the request/response cycle that normally recycles DB
connections does not exist here; SIGTERM sets a flag so the in-flight message finishes before exit (container
orchestrators send SIGTERM then SIGKILL after a grace period — keep processing time below it).

### Idempotent handler with inbox dedupe

```python
# apps/orders/integrations/partner/handlers.py
from django.db import IntegrityError, transaction

from apps.integrations.errors import PermanentIntegrationError
from apps.integrations.models import InboxMessage
from apps.orders.services import apply_partner_status

from .schemas import PartnerOrderEvent   # pydantic model of the wire contract


def handle_partner_event(raw: dict) -> None:
    try:
        event = PartnerOrderEvent.model_validate(raw)
    except ValueError as exc:
        raise PermanentIntegrationError(
            provider="partner", operation="orders.event", status_code=None, request_id=raw.get("id")
        ) from exc

    with transaction.atomic():
        try:
            with transaction.atomic():           # savepoint so the duplicate doesn't poison the outer tx
                InboxMessage.objects.create(source="partner.orders", message_id=event.id)
        except IntegrityError:
            return                               # duplicate delivery — already applied
        apply_partner_status(
            external_order_id=event.subject,
            status=event.data.status,
            source_version=event.data.version,   # order-tolerance: ignore older versions
        )
```

Full inbox semantics (retention, pruning, `ON CONFLICT`) are in
[integration-architecture-patterns.md](integration-architecture-patterns.md).

### Order tolerance by version

```python
def apply_partner_status(*, external_order_id: str, status: str, source_version: int) -> None:
    updated = (
        Order.objects
        .filter(external_ref=external_order_id, partner_version__lt=source_version)
        .update(partner_status=status, partner_version=source_version)
    )
    # updated == 0 → older or duplicate event; safe to ignore
```

### Schema evolution in consumers

```python
from pydantic import BaseModel, ConfigDict   # [third-party: pydantic]


class PartnerOrderData(BaseModel):
    model_config = ConfigDict(extra="ignore")   # forward compatible: ignore unknown fields
    status: str
    version: int
    channel: str = "web"                         # field added later: default keeps old messages valid
```

Dispatch on `(type, version)`; keep handlers for the previous major version until the producer confirms no
old messages remain (including in DLQs and retained logs).

### Backpressure controls

- RabbitMQ: `channel.basic_qos(prefetch_count=10)` — bounded unacked messages per consumer.
- Kafka: `max.poll.records`/batch size, pause/resume partitions while a downstream breaker is open.
- Service Bus: `max_message_count`/prefetch on the receiver.
- When the downstream (DB, partner API) is degraded, **stop pulling** (pause) instead of pulling and failing —
  failed messages burn delivery counts and end in the DLQ for a transient reason.

### DLQ redrive command

Provide `manage.py redrive_dlq --source partner.orders.v1.dlq --limit 100 --dry-run`. It re-publishes to the
main topic preserving the original `id` (so the inbox still dedupes) and records who redrove what.

## 6. Production use cases

1. **Order events to a warehouse system (Service Bus topic).** Orders app writes `OutboxMessage` in the
   order transaction; relay publishes with `MessageId = envelope.id` and `SessionId = order_id` so per-order
   events are ordered. The WMS subscription has a SQL filter on `type`; max delivery count 10 → DLQ alert.
2. **Partner event stream (Kafka).** Partner publishes order updates keyed by order id. Django consumer
   group with 6 replicas on a 12-partition topic; inbox dedupe; version comparison for order tolerance;
   offsets committed after DB commit. Lag alert at 5 minutes of backlog.
3. **Legacy integration over RabbitMQ.** ERP posts invoice commands to a quorum queue; consumer applies them
   and publishes `invoice.applied` back. `delivery-limit=5` dead-letters malformed XML payloads.
4. **Cache invalidation (Redis Pub/Sub).** Web processes subscribe to `config.changed`; message loss is
   acceptable because caches also have a TTL.
5. **Replay after a consumer bug.** A bug mis-mapped currency for 3 hours. Fix deployed; consumer group
   offsets reset to the bug start time (Kafka `--reset-offsets --to-datetime`); idempotent handlers make
   reprocessing of correct records harmless. Only possible because retention > 3 hours and handlers are idempotent.

Failure scenarios to design for: broker unavailable at publish (outbox absorbs), consumer crash mid-batch
(redelivery + dedupe), rebalance storm from slow handlers exceeding `max.poll.interval.ms`, DLQ silently
filling, a producer shipping a breaking schema change.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Temporal decoupling: producer works while consumers are down | Eventual consistency; harder to reason about current state |
| Fan-out to many consumers without producer changes | Duplicates and reordering must be handled in every consumer |
| Load levelling: queues absorb spikes | Another critical dependency to operate, secure and monitor |
| Replay (log brokers) enables recovery and new consumers | Schema governance across teams is real ongoing work |
| Natural audit trail of events | Debugging spans multiple processes; needs tracing |

## 8. Security and performance considerations

### Security
- TLS to the broker (`amqps://`, `SASL_SSL`, Service Bus is TLS-only); no plaintext in production.
- Least-privilege credentials per producer/consumer: send-only vs listen-only SAS/ACLs/RBAC; separate
  identities per service. Prefer managed identity on Azure ([azure-integrations.md](azure-integrations.md)).
- Consumers treat messages as **untrusted input**: validate schema, size and types; never `pickle` or
  `yaml.load` message bodies ([integration-security.md](integration-security.md)).
- Events broadcast data to every subscriber: minimise PII, prefer IDs over personal data, encrypt sensitive
  fields if needed. DLQs retain payloads — apply the same retention and access controls.
- Cross-tenant: include tenant id in the envelope and enforce it in handlers.

### Performance
- Batch publishes from the outbox relay; flush/confirm per batch, not per message.
- Partition count bounds consumer parallelism (Kafka); session count bounds Service Bus ordered parallelism.
- Keep handlers short; offload slow external calls to tasks or separate stages so `max.poll.interval.ms` and
  lock durations are not exceeded.
- Inbox table grows with every message: index `(source, message_id)` unique, prune by age
  ([postgresql-engineering](../../postgresql-engineering/SKILL.md) for partitioning large inboxes).
- Worker and concurrency sizing: [concurrency-and-worker-management](../../django-performance/references/concurrency-and-worker-management.md).

## 9. Common mistakes and anti-patterns

- Assuming "exactly-once" broker settings make the Django handler exactly-once.
- Auto-ack / `enable.auto.commit=True` with processing afterwards → silent message loss on crash.
- Publishing inside `transaction.atomic()` before commit → phantom events on rollback.
- Publishing after commit without an outbox for business-critical events → lost events on crash.
- Infinite requeue of poison messages (`basic_nack(requeue=True)` forever) blocking the queue.
- No owner, alert or redrive tool for the DLQ.
- Relying on global ordering with competing consumers.
- Breaking schema changes in place; consumers rejecting unknown fields.
- Using Redis Pub/Sub for events that must not be lost.
- Huge payloads in messages instead of claim-check references.
- Opening a broker connection per message or at Django import time in web workers.
- Long-lived consumer without `close_old_connections()` → "server closed the connection unexpectedly".

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Duplicate side effects | At-least-once redelivery without dedupe | Inbox rows, handler idempotency, ack after commit |
| Messages "lost" | Auto-ack, no publisher confirms, Redis Pub/Sub, unroutable messages | Producer config, `mandatory=True`, broker metrics for unroutable |
| Kafka lag growing, repeated rebalances | Handler slower than `max.poll.interval.ms` | Consumer logs for rebalance, processing time per message |
| SQS/Service Bus message processed twice | Visibility/lock expired during processing | Handler duration vs timeout; add lock renewal |
| DLQ filling | Poison messages or downstream outage | DLQ reason headers (`x-death`, `DeadLetterReason`), error categories |
| Events out of order | Multiple partitions/consumers, retries | Partition key choice, version checks |
| Consumer stuck after DB failover | Stale DB connection in long-lived process | `close_old_connections()` per message |

Validation checklist:
- [ ] Producer durability: confirms/`acks=all`, persistent messages, TLS.
- [ ] Critical events go through an outbox; non-critical use `on_commit`.
- [ ] Ack/commit only after DB commit; handlers idempotent with inbox or natural keys.
- [ ] Max delivery count + DLQ configured, monitored, with a redrive command.
- [ ] Ordering scope explicitly chosen (key/session/group) and handlers tolerate reorder.
- [ ] Envelope has id, type, version, source, time, correlation id.
- [ ] Schema compatibility rules documented; consumers ignore unknown fields.
- [ ] Graceful shutdown tested with SIGTERM; lag/queue depth alerts exist
  ([integration-observability-and-operations.md](integration-observability-and-operations.md)).
- [ ] Broker tests via containers or emulators ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox, inbox, saga, choreography.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — task queues vs messaging.
- [azure-integrations.md](azure-integrations.md) — Service Bus and Event Hubs SDK usage.
- [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md) — SQS, SNS, EventBridge.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retries, idempotency keys, failure classification.
- [integration-observability-and-operations.md](integration-observability-and-operations.md) — lag, DLQ metrics, tracing across messages.
- [integration-testing-and-contracts.md](integration-testing-and-contracts.md) — broker tests, schema contracts.
- django-architecture: [event-driven-architecture](../../django-architecture/references/event-driven-architecture.md),
  [domain-events](../../django-architecture/references/domain-events.md),
  [distributed-systems](../../django-architecture/references/distributed-systems.md).
- django-core: [signals-management-commands](../../django-core/references/signals-management-commands.md).
- django-performance: [background-task-performance](../../django-performance/references/background-task-performance.md).

## 12. Official documentation and external references

- RabbitMQ reliability guide: https://www.rabbitmq.com/docs/reliability
- RabbitMQ consumer acknowledgements and publisher confirms: https://www.rabbitmq.com/docs/confirms
- RabbitMQ quorum queues: https://www.rabbitmq.com/docs/quorum-queues
- RabbitMQ dead letter exchanges: https://www.rabbitmq.com/docs/dlx
- Apache Kafka documentation (design, semantics): https://kafka.apache.org/documentation/
- confluent-kafka-python: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html
- Azure Service Bus overview: https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview
- Service Bus dead-letter queues: https://learn.microsoft.com/azure/service-bus-messaging/service-bus-dead-letter-queues
- Service Bus message sessions: https://learn.microsoft.com/azure/service-bus-messaging/message-sessions
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
- Redis Pub/Sub: https://redis.io/docs/latest/develop/interact/pubsub/
- Amazon SQS visibility timeout: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- CloudEvents specification: https://cloudevents.io/
- pika documentation: https://pika.readthedocs.io/
- Django management commands: https://docs.djangoproject.com/en/5.2/howto/custom-management-commands/
- Django transactions (`on_commit`): https://docs.djangoproject.com/en/5.2/topics/db/transactions/
