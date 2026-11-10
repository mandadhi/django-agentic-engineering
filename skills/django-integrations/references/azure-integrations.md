# Azure Integrations

**Scope:** How Django applications use Microsoft Azure services through the Azure SDK for Python: identity and credentials, Service Bus, Blob Storage, Event Hubs, Key Vault, Functions, Logic Apps, API Management and Azure Monitor. Broker theory lives in [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md), OAuth/OIDC protocol details for Microsoft Entra ID in [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md), object-storage abstractions in [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md), and cross-cloud concepts (managed/workload identity, private endpoints, SDK vs service guarantees) in [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md).

## 1. Purpose and scope

This reference tells an agent how to wire a Django 5.2 LTS / Python 3.12+ application (PostgreSQL, Redis, Celery 5.x where present) to Azure services safely: which SDK package, which credential, which client lifetime, which retry and timeout settings, and which failure modes to design for.

Covered:

- `azure-identity` [third-party: azure-identity] credential chains and managed identity.
- Azure Service Bus [third-party: azure-servicebus]: senders, receivers, peek-lock, lock renewal, sessions, dead-lettering.
- Azure Blob Storage [third-party: azure-storage-blob]: clients, uploads/downloads, user delegation SAS.
- Azure Event Hubs [third-party: azure-eventhub, azure-eventhub-checkpointstoreblob]: producers, consumers, checkpoints.
- Azure Key Vault [third-party: azure-keyvault-secrets]: secret retrieval and caching.
- Azure Functions, Logic Apps and API Management as integration boundaries (not as Django code).
- Azure Monitor via [third-party: azure-monitor-opentelemetry].
- SDK retry policy and timeout configuration.

Not covered: Azure infrastructure provisioning (Bicep/Terraform), AKS cluster operations, Azure SQL (PostgreSQL is the baseline — Azure Database for PostgreSQL Flexible Server is just PostgreSQL; see [postgresql-engineering](../../postgresql-engineering/SKILL.md)).

Not every Django app needs direct Azure access. Many integrate only through HTTP APIs fronted by API Management, or through a single storage account. Add an SDK dependency only for a service the app actually talks to.

## 2. Core concepts

| Concept | Meaning for a Django integration |
|---|---|
| **Microsoft Entra ID** (formerly Azure AD) | The identity provider issuing tokens to your app (as a workload) and to your users. |
| **Managed identity** | An Entra identity bound to an Azure compute resource (App Service, Container Apps, AKS via workload identity, VM). No secret in your config. System-assigned (lifecycle tied to resource) or user-assigned (independent, reusable). |
| **Workload identity (AKS)** | Kubernetes service account federated to an Entra app/user-assigned identity via OIDC token exchange. |
| **RBAC data-plane roles** | E.g. "Storage Blob Data Contributor", "Azure Service Bus Data Sender", "Key Vault Secrets User". Management-plane roles (Owner, Contributor) do **not** automatically grant data access. |
| **Connection string / SAS / account key** | Shared-secret auth. Works everywhere, rotates badly, grants broad rights. Avoid in production when Entra auth is supported. |
| **Namespace / account / vault** | The service endpoint: `<ns>.servicebus.windows.net`, `<acct>.blob.core.windows.net`, `<vault>.vault.azure.net`. |
| **Peek-lock vs receive-and-delete** | Service Bus settlement modes: at-least-once vs at-most-once. |
| **Partition / consumer group / checkpoint** | Event Hubs streaming model, similar to Kafka. |

Service Bus vs Event Hubs vs Storage Queues:

| Need | Choose |
|---|---|
| Business commands/events, per-message settlement, DLQ, sessions (ordered per key), scheduled messages, transactions within the namespace | **Service Bus** |
| High-throughput telemetry/event streams, replay by offset, many independent readers | **Event Hubs** (Kafka-compatible endpoint available on Standard+ `VERIFY`) |
| Very simple, cheap queue, no ordering, no DLQ semantics beyond poison handling | Storage Queues |
| Fan-out of resource events (blob created, etc.) | Event Grid (delivers to webhook, Service Bus, Function) |

## 3. Internal mechanics

### 3.1 Credential resolution

`DefaultAzureCredential` tries a chain: environment variables (service principal), workload identity, managed identity, then developer credentials (Azure CLI, Azure Developer CLI, PowerShell, VS Code…) — exact order and members vary by `azure-identity` version `VERIFY`. Consequences:

- Convenient locally, but in production a misconfiguration silently falls through to the next credential, adding latency (IMDS probe timeouts) and making failures confusing.
- Each credential caches tokens in memory and refreshes before expiry. A credential object should be **long-lived** (module or process scope), not created per request.
- You can exclude chain members (`exclude_*_credential=True` kwargs) and, in recent `azure-identity` releases, restrict the chain with the `AZURE_TOKEN_CREDENTIALS` environment variable `VERIFY` (feature and variable name are version-dependent).

Production recommendation: construct an explicit credential.

```python
# apps/integrations/azure_credentials.py
from functools import cache

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential, WorkloadIdentityCredential
from django.conf import settings


@cache
def azure_credential():
    mode = settings.AZURE_CREDENTIAL_MODE  # "managed", "workload", "dev"
    if mode == "managed":
        # client_id needed only for a user-assigned identity
        return ManagedIdentityCredential(client_id=settings.AZURE_MANAGED_IDENTITY_CLIENT_ID or None)
    if mode == "workload":
        return WorkloadIdentityCredential()  # reads AZURE_* env injected by AKS webhook
    return DefaultAzureCredential()  # local development only
```

### 3.2 SDK pipeline and retries

Azure SDK clients share `azure-core`'s HTTP pipeline: retry policy, logging policy, user agent, transport (requests by default for sync, aiohttp for async `VERIFY`). Default retry: exponential, `retry_total=10`-ish with backoff for transient status codes (408, 429, 500, 502, 503, 504) `VERIFY`. Per-client kwargs override it:

```python
BlobServiceClient(
    account_url=..., credential=...,
    retry_total=3, retry_backoff_factor=0.8, retry_backoff_max=10,
    connection_timeout=5, read_timeout=30,
)
```

Service Bus and Event Hubs use AMQP, not HTTP; they have their own `retry_total`, `retry_backoff_factor`, `retry_mode` and `idle_timeout` options on the client constructor.

The SDK retry is transport-level. It knows nothing about your business idempotency: a retried blob upload overwrites the same name (idempotent); a retried Service Bus send can produce a duplicate message (enable duplicate detection on the entity or dedupe on consume). See [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

### 3.3 Service Bus peek-lock

1. Receiver fetches message; the broker locks it for `LockDuration` (entity setting, default 60 s, max 5 min `VERIFY`).
2. Consumer processes and calls `complete_message` → removed.
3. If the lock expires or `abandon_message` is called → redelivered, `delivery_count` increments.
4. When `delivery_count` exceeds `MaxDeliveryCount` (default 10 `VERIFY`) → moved to the `$deadletterqueue` sub-queue.
5. `dead_letter_message(reason=..., error_description=...)` moves it explicitly.
6. `AutoLockRenewer` extends locks for long handlers, up to a configured maximum.

Sessions: messages with the same `session_id` are delivered in order to one receiver holding the session lock — this is how Service Bus provides per-key FIFO. Requires a session-enabled entity.

Duplicate detection: entity-level window keyed by `message_id`; must be enabled at creation `VERIFY` (cannot be toggled later on some tiers).

### 3.4 Blob Storage

- Block blobs: large uploads are split into blocks (`stage_block` / `commit_block_list`); `upload_blob` handles chunking via `max_single_put_size`, `max_block_size` and `max_concurrency`.
- Conditional requests with ETags (`if_match`, `if_none_match="*"`) give optimistic concurrency and create-only semantics.
- `content_settings.content_md5` / service-computed MD5 for integrity; see [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md).
- SAS types: account SAS and service SAS (signed with the account key) vs **user delegation SAS** (signed with a key obtained via Entra auth; max validity 7 days `VERIFY`; revocable by revoking the delegation key). Prefer user delegation SAS.

### 3.5 Event Hubs

Events are appended to partitions and retained for a configured period (1 day default on Standard, longer on Premium/Dedicated `VERIFY`). Consumers in a consumer group track their position via **checkpoints**, stored externally (Blob Storage checkpoint store). `EventHubConsumerClient` with a checkpoint store load-balances partitions across instances. Checkpointing is your responsibility: checkpoint after durable processing; on restart you resume from the last checkpoint and reprocess anything after it (at-least-once).

### 3.6 Key Vault

Secrets are versioned. `SecretClient.get_secret(name)` returns the current version. Each call is an HTTPS request subject to vault throttling (on the order of thousands of transactions per 10 s per vault `VERIFY`). Rotation creates a new version; consumers must re-read.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Alternative | When the alternative wins |
|---|---|---|---|
| Auth to Azure services | Managed/workload identity + data-plane RBAC | Connection string / account key | Local emulators (Azurite, Service Bus emulator), legacy tiers, or providers without Entra support |
| Credential construction | Explicit `ManagedIdentityCredential` / `WorkloadIdentityCredential` in prod | `DefaultAzureCredential` | Local development and CI with `az login` |
| Secrets delivery | Platform injects Key Vault references as env vars (App Service / Container Apps Key Vault references, AKS Secrets Store CSI driver) | App calls Key Vault at runtime | Secrets that rotate frequently and must be picked up without restart |
| Messaging | Service Bus queues/topics for business messages | Event Hubs | Streaming volume, replay, analytics fan-out |
| Event producer reliability | Transactional outbox → Service Bus | Send from request | Never send inside `transaction.atomic`; direct send acceptable only for fire-and-forget non-critical events |
| Consumer host | Dedicated worker process (management command) | Azure Function consuming the queue and calling Django API | Bursty load, separate scaling, team owns Functions |
| File uploads from browsers | User delegation SAS, direct to Blob | Proxy through Django | Small files, need to scan/transform synchronously |
| API exposure to partners | Azure API Management in front of Django | Direct | Internal-only APIs |
| Workflow across SaaS connectors | Logic Apps calling Django APIs/webhooks | Django + Celery code | Long-running human-in-loop, many SaaS connectors, low-code ownership |

Cost notes: Service Bus Premium is billed per messaging unit, Standard per operation; Event Hubs per throughput unit/processing unit; Key Vault per 10k operations; Monitor by ingested GB. Unbounded log export to Application Insights is a common surprise bill — sample traces. All pricing `VERIFY`.

## 5. Django implementation patterns

### 5.1 Settings

```python
# config/settings/base.py
import os

AZURE_CREDENTIAL_MODE = os.environ.get("AZURE_CREDENTIAL_MODE", "dev")
AZURE_MANAGED_IDENTITY_CLIENT_ID = os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID", "")
AZURE_SERVICEBUS_NAMESPACE = os.environ.get("AZURE_SERVICEBUS_NAMESPACE", "")  # e.g. myns.servicebus.windows.net
AZURE_STORAGE_ACCOUNT_URL = os.environ.get("AZURE_STORAGE_ACCOUNT_URL", "")    # https://acct.blob.core.windows.net
AZURE_KEY_VAULT_URL = os.environ.get("AZURE_KEY_VAULT_URL", "")
```

No connection strings or keys in settings modules; if a connection string is unavoidable, it comes from the environment and is never logged. See [settings-configuration.md](../../django-core/references/settings-configuration.md).

### 5.2 Process-scoped clients

Azure SDK sync clients are generally safe to share across threads for independent operations `VERIFY` (per-SDK guidance differs; Service Bus senders/receivers are **not** thread-safe — create one per thread or guard with a lock). Create them lazily per process, after fork (Gunicorn/Celery prefork), not at import time.

```python
# apps/integrations/azure_clients.py
from functools import cache

from azure.storage.blob import BlobServiceClient
from django.conf import settings

from .azure_credentials import azure_credential


@cache
def blob_service() -> BlobServiceClient:
    return BlobServiceClient(
        account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
        credential=azure_credential(),
        retry_total=3,
        connection_timeout=5,
        read_timeout=60,
    )
```

`functools.cache` per process is fine because Gunicorn/Celery workers import after fork when `preload_app` is off; with `--preload`, make sure no client is created in the master.

### 5.3 Publishing to Service Bus via the outbox

The outbox model and relay are owned by [integration-architecture-patterns.md](integration-architecture-patterns.md). The Azure-specific part is the publisher:

```python
# apps/integrations/publishers/servicebus.py
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.servicebus.exceptions import ServiceBusError, OperationTimeoutError
from django.conf import settings

from apps.integrations.azure_credentials import azure_credential
from apps.integrations.errors import AmbiguousOutcomeError, TransientIntegrationError
from apps.integrations.models import OutboxMessage


def publish_batch(messages: list[OutboxMessage]) -> None:
    with ServiceBusClient(
        fully_qualified_namespace=settings.AZURE_SERVICEBUS_NAMESPACE,
        credential=azure_credential(),
        retry_total=3,
    ) as client:
        by_topic: dict[str, list[OutboxMessage]] = {}
        for m in messages:
            by_topic.setdefault(m.topic, []).append(m)
        for topic, items in by_topic.items():
            with client.get_topic_sender(topic_name=topic) as sender:
                batch = sender.create_message_batch()
                for m in items:
                    msg = ServiceBusMessage(
                        body=m.payload_json(),
                        message_id=str(m.id),          # enables broker duplicate detection
                        session_id=m.key or None,       # per-key ordering if entity is session-enabled
                        content_type="application/json",
                        application_properties={"type": m.headers.get("type", ""), "schema_version": m.headers.get("v", "1")},
                    )
                    try:
                        batch.add_message(msg)
                    except ValueError:  # batch full
                        sender.send_messages(batch)
                        batch = sender.create_message_batch()
                        batch.add_message(msg)
                try:
                    sender.send_messages(batch)
                except OperationTimeoutError as exc:
                    # The broker may have accepted the batch. Outbox relay re-sends; message_id + duplicate
                    # detection (or consumer-side inbox) absorbs the duplicate.
                    raise AmbiguousOutcomeError(provider="servicebus", operation="send") from exc
                except ServiceBusError as exc:
                    raise TransientIntegrationError(provider="servicebus", operation="send") from exc
```

A relay that creates a `ServiceBusClient` per batch pays an AMQP connection handshake each time; for high volume keep a long-lived client in the relay process and recreate it on connection errors.

### 5.4 Consuming from Service Bus in a Django worker

Run consumers as a management command (one process per entity or per partition of work), not inside web workers.

```python
# apps/orders/management/commands/consume_order_events.py
import logging
import signal

from azure.servicebus import AutoLockRenewer, ServiceBusClient
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction

from apps.integrations.azure_credentials import azure_credential
from apps.integrations.errors import PermanentIntegrationError
from apps.integrations.models import InboxMessage
from apps.orders.services import apply_order_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume order events from Service Bus subscription"

    def handle(self, *args, **opts):
        stop = False

        def _stop(*_):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGTERM, _stop)
        renewer = AutoLockRenewer(max_lock_renewal_duration=300)
        with ServiceBusClient(settings.AZURE_SERVICEBUS_NAMESPACE, azure_credential()) as client:
            with client.get_subscription_receiver(
                topic_name="orders", subscription_name="billing",
                max_wait_time=5, prefetch_count=20, auto_lock_renewer=renewer,
            ) as receiver:
                while not stop:
                    for msg in receiver.receive_messages(max_message_count=20, max_wait_time=5):
                        close_old_connections()
                        try:
                            with transaction.atomic():
                                _, created = InboxMessage.objects.get_or_create(
                                    source="servicebus:orders/billing", message_id=msg.message_id,
                                )
                                if created:
                                    apply_order_event(str(msg))
                            receiver.complete_message(msg)
                        except PermanentIntegrationError as exc:
                            receiver.dead_letter_message(msg, reason="permanent", error_description=str(exc)[:1000])
                        except Exception:
                            logger.exception("servicebus.handler_failed", extra={"message_id": msg.message_id,
                                                                                 "delivery_count": msg.delivery_count})
                            receiver.abandon_message(msg)  # redelivered; DLQ after MaxDeliveryCount
        renewer.close()
```

Key points:

- Settle **after** the DB commit. If the process dies between commit and `complete_message`, the message is redelivered and the `InboxMessage` row makes it a no-op.
- `prefetch_count` × handler time must stay below `LockDuration`, or prefetched messages lose their locks before processing.
- `close_old_connections()` because long-running commands do not get Django's request-cycle connection cleanup.
- Poison messages: permanent failures go to DLQ immediately; transient ones are abandoned and retried. A separate DLQ inspector command lists, re-submits or purges.

### 5.5 Blob user delegation SAS for direct browser upload

```python
# apps/documents/integrations/azure_blob/client.py
from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobSasPermissions, generate_blob_sas

from apps.integrations.azure_clients import blob_service


def upload_sas_url(container: str, blob_name: str, *, minutes: int = 10) -> str:
    svc = blob_service()
    now = datetime.now(timezone.utc)
    key = svc.get_user_delegation_key(key_start_time=now - timedelta(minutes=5), key_expiry_time=now + timedelta(hours=1))
    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=container,
        blob_name=blob_name,              # server-generated name, never user-supplied
        user_delegation_key=key,
        permission=BlobSasPermissions(create=True, write=True),
        expiry=now + timedelta(minutes=minutes),
        start=now - timedelta(minutes=5),  # clock skew tolerance
        content_type="application/pdf",    # pins response header; does not validate upload content
    )
    return f"{svc.url}{container}/{blob_name}?{sas}"
```

Cache the user delegation key (e.g. Redis, 50 minutes) — requesting one per URL adds a round trip and counts against throttling. The identity needs "Storage Blob Delegator" (or a data role that includes `generateUserDelegationKey`) `VERIFY`. After upload, confirm via an Event Grid `BlobCreated` event or a client callback, then verify size/type server-side (see [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)).

### 5.6 Event Hubs consumer with checkpoint store

```python
from azure.eventhub import EventHubConsumerClient
from azure.eventhub.extensions.checkpointstoreblob import BlobCheckpointStore

checkpoint_store = BlobCheckpointStore(blob_account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
                                       container_name="eh-checkpoints", credential=azure_credential())
consumer = EventHubConsumerClient(
    fully_qualified_namespace="myhub-ns.servicebus.windows.net",
    eventhub_name="telemetry",
    consumer_group="django-ingest",
    credential=azure_credential(),
    checkpoint_store=checkpoint_store,
)


def on_event_batch(partition_context, events):
    if not events:
        return
    close_old_connections()
    rows = [map_event(e) for e in events]
    TelemetryPoint.objects.bulk_create(rows, ignore_conflicts=True)  # natural-key unique constraint → idempotent
    partition_context.update_checkpoint(events[-1])  # checkpoint after durable write


with consumer:
    consumer.receive_batch(on_event_batch=on_event_batch, max_batch_size=500, starting_position="-1")
```

Checkpoint per batch, not per event — each checkpoint is a blob write.

### 5.7 Key Vault with caching

Prefer platform-injected secrets. When the app must read at runtime (rotation without restart):

```python
# apps/integrations/secrets.py
import time
from threading import Lock

from azure.keyvault.secrets import SecretClient
from django.conf import settings

from .azure_credentials import azure_credential

_client: SecretClient | None = None
_cache: dict[str, tuple[float, str]] = {}
_lock = Lock()
TTL = 300


def get_secret(name: str) -> str:
    global _client
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < TTL:
            return hit[1]
        if _client is None:
            _client = SecretClient(vault_url=settings.AZURE_KEY_VAULT_URL, credential=azure_credential())
    value = _client.get_secret(name).value
    with _lock:
        _cache[name] = (now, value)
    return value
```

In-process caching only; never put secret values in Redis or Django's cache. On an auth failure from the downstream (e.g. 401 using a rotated API key), invalidate the cache entry and retry once. Rotation procedure: add new version → both old and new accepted downstream → consumers pick up new within TTL → disable old. See [secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md).

### 5.8 Functions, Logic Apps and API Management as boundaries

- **Azure Functions**: good for event-driven glue that should scale independently of Django (blob-triggered thumbnailing, queue-triggered fan-out). Integrate by having the Function call a Django internal API (authenticated with Entra tokens — validate `aud`/`roles` in Django; see [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md)) or by writing to a queue Django consumes. Do not let a Function write directly into Django's database tables — it bypasses invariants and migrations.
- **Logic Apps**: low-code orchestration across SaaS connectors. Treat as an external client: Django exposes a versioned API or receives a webhook from it; Logic Apps retry policies are configured per action (default exponential, 4 retries `VERIFY`) — your endpoint must be idempotent.
- **API Management (APIM)**: gateway in front of Django for partner APIs — subscription keys, OAuth validation (`validate-jwt` policy), rate limiting, transformation, versioning. Django still authorises: APIM is a perimeter, not your authorisation layer. Restrict Django ingress to APIM (private endpoint / VNet integration, or a shared secret header verified in middleware as defence in depth). Forwarded headers: configure `SECURE_PROXY_SSL_HEADER` and `USE_X_FORWARDED_HOST` only when the proxy chain overwrites them.

### 5.9 Azure Monitor / Application Insights

```python
# config/otel.py — call once at process start (gunicorn post_fork, celery worker_process_init)
from azure.monitor.opentelemetry import configure_azure_monitor


def init_telemetry():
    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],  # not a secret per se, but keep in env
        # sampling and instrumentation options vary by version VERIFY
    )
```

The distro auto-instruments Django, requests, urllib3, psycopg2 (and others) via OpenTelemetry `VERIFY` exact list. Initialise **after** fork in prefork servers, or the exporter thread is lost. Correlation and metrics design: [integration-observability-and-operations.md](integration-observability-and-operations.md).

## 6. Production use cases

1. **Order events to ERP via Service Bus topic.** Django writes orders + outbox in one transaction; a relay publishes to `orders` topic with `message_id = outbox.id`; ERP adapter subscribes. Duplicate detection window 10 min covers relay retries; consumer inbox covers anything beyond.
2. **Document upload portal.** Browser gets a 10-minute user delegation SAS for a server-named blob; Event Grid `BlobCreated` → Service Bus queue → Django consumer runs malware scan status check and marks document `available`.
3. **IoT/telemetry ingestion.** Devices → Event Hubs; Django ingestion workers (one per partition set) batch-insert into PostgreSQL with checkpoints in Blob; dashboards read aggregates.
4. **Partner API.** APIM fronts `/partner/v1/*`, validates partner OAuth tokens from Entra, applies quotas; Django validates the forwarded token again and enforces object-level access.
5. **Secret rotation without redeploy.** Payment-provider API key stored in Key Vault; app reads with 5-minute cache; Event Grid `SecretNewVersionCreated` triggers cache invalidation via a small management endpoint or simply waits for TTL.

## 7. Advantages and disadvantages

| Aspect | Advantage | Disadvantage |
|---|---|---|
| Managed identity | No secrets to leak or rotate; RBAC auditable | Only on Azure compute; local dev needs a different credential; role assignment propagation delays (minutes) `VERIFY` |
| Service Bus | Rich semantics (DLQ, sessions, scheduling, duplicate detection) | AMQP clients are stateful and not thread-safe; Premium cost; lock-duration tuning |
| Event Hubs | Throughput, replay, Kafka endpoint | No per-message ack/DLQ; checkpoint management is yours |
| Blob + SAS | Offloads bandwidth from Django | SAS leakage = access until expiry; harder to enforce validation before upload |
| Key Vault runtime reads | Rotation without restart | Latency, throttling, new failure dependency at startup |
| APIM | Central policies, developer portal | Another hop and policy language to test; can mask Django errors |
| Azure Monitor distro | One-line instrumentation | Vendor-shaped config; cost scales with volume |

## 8. Security and performance considerations

Security:

- Disable shared-key access on storage accounts (`allowSharedKeyAccess=false`) when everything uses Entra auth; disable SAS based on account keys.
- Least-privilege data roles scoped to the entity (queue/topic/container/vault), not the subscription.
- Private endpoints + disabled public network access for Service Bus Premium, Storage, Key Vault; Django runs in an integrated VNet. Remember DNS: private endpoint requires private DNS zones (`privatelink.blob.core.windows.net` etc.), a classic cause of "works in one environment" failures.
- Never log SAS URLs (query string is the credential), connection strings, or Key Vault values. Strip query strings in log filters.
- Validate Entra-issued tokens for internal APIs (`iss`, `aud`, `tid`, `roles`), never just "is signed".
- Service Bus message bodies: treat as untrusted input — validate with schemas; never `pickle`.

Performance:

- Reuse credentials and clients; token acquisition with managed identity hits IMDS/identity endpoint — cached, but cold start adds latency.
- Service Bus: batch sends; `prefetch_count` for receivers; sessions limit parallelism to number of active sessions.
- Blob: tune `max_concurrency` for large uploads; stream downloads with `download_blob().chunks()` instead of `readall()`.
- Event Hubs: partition count caps consumer parallelism and is hard to change (fixed on Standard `VERIFY`).
- Worker sizing and memory: [concurrency-and-worker-management.md](../../django-performance/references/concurrency-and-worker-management.md).

## 9. Common mistakes and anti-patterns

- `DefaultAzureCredential` in production without restriction → slow IMDS probes and the wrong identity picked up.
- Creating `DefaultAzureCredential()` per request — loses the token cache, hammers the identity endpoint.
- Sending to Service Bus inside `transaction.atomic()` — message escapes even if the transaction rolls back.
- Completing a message before the DB commit — crash = lost message.
- Lock duration shorter than worst-case handler time without `AutoLockRenewer` → duplicate processing via redelivery.
- Assuming Service Bus duplicate detection = exactly-once processing. It deduplicates sends within a window, not handler side effects.
- Using a management-plane "Contributor" role and wondering why data access returns 403.
- Account-key SAS with long expiry embedded in emails.
- Checkpointing Event Hubs before writes are durable → data loss on crash.
- Letting Azure Functions write directly into Django tables.
- Treating APIM as the only authorization layer.
- Initialising the Azure Monitor exporter in the Gunicorn master with `--preload`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `ClientAuthenticationError`/`CredentialUnavailableError` | Credential chain fell through; MI not assigned; wrong client_id for user-assigned MI | Enable `azure.identity` logging at INFO; confirm identity on the resource; `AZURE_CLIENT_ID` |
| 403 `AuthorizationPermissionMismatch` on blobs | Missing data-plane role or not yet propagated | Role assignment scope; wait/retry; test with `az storage blob list --auth-mode login` |
| Name resolution or connection timeout only in prod | Private endpoint DNS not linked to VNet | `nslookup <acct>.blob.core.windows.net` from the pod/app resolves to private IP |
| Messages repeatedly redelivered, `delivery_count` rising | Lock lost (handler too slow, prefetch too high) or handler raising | Lock duration vs p99 handler time; logs for abandon reasons |
| DLQ growing | Poison messages or MaxDeliveryCount too low | Inspect `dead_letter_reason`; alert on DLQ count |
| Event Hubs consumer reprocessing from start | Checkpoint store container wrong/unwritable; different consumer group | Checkpoint blobs updated? |
| Key Vault 429 | Reading per request without cache | Add cache; consider platform references |
| Missing traces in App Insights | Exporter initialised before fork; sampling | Init in `post_fork`/`worker_process_init`; check sampling ratio |

Validation checklist:

- [ ] Production uses an explicit managed/workload identity; no account keys or connection strings in env unless justified.
- [ ] Each identity has only the data-plane roles it needs, scoped to entities.
- [ ] SDK clients are process-scoped, created after fork; Service Bus senders/receivers not shared across threads.
- [ ] Retry totals and timeouts set explicitly per client and fit the caller's deadline.
- [ ] Publishing goes through the outbox; consuming uses an inbox; settlement after commit.
- [ ] DLQ monitored and a replay/inspect command exists.
- [ ] Private endpoint DNS verified from the runtime network.
- [ ] Tests use Azurite / Service Bus emulator or stubs ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md) — general cloud identity, networking, SDK-vs-service guarantees.
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — delivery semantics, DLQ, ordering.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox/inbox implementation.
- [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) — Entra ID protocol flows, token validation.
- [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md) — storage abstraction, checksums, large files.
- [integration-observability-and-operations.md](integration-observability-and-operations.md) — tracing, metrics, alerts.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retry policy and idempotency.
- [secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md) — secret handling and rotation.
- [distributed-systems.md](../../django-architecture/references/distributed-systems.md) — whether to distribute at all.
- [concurrency-and-worker-management.md](../../django-performance/references/concurrency-and-worker-management.md) — worker sizing.

## 12. Official documentation and external references

- Azure SDK for Python: https://learn.microsoft.com/azure/developer/python/sdk/azure-sdk-overview
- Azure Identity client library: https://learn.microsoft.com/python/api/overview/azure/identity-readme
- Credential chains / DefaultAzureCredential: https://learn.microsoft.com/azure/developer/python/sdk/authentication/credential-chains
- Managed identities overview: https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/overview
- AKS workload identity: https://learn.microsoft.com/azure/aks/workload-identity-overview
- Service Bus client library for Python: https://learn.microsoft.com/python/api/overview/azure/servicebus-readme
- Service Bus message transfers, locks and settlement: https://learn.microsoft.com/azure/service-bus-messaging/message-transfers-locks-settlement
- Service Bus dead-letter queues: https://learn.microsoft.com/azure/service-bus-messaging/service-bus-dead-letter-queues
- Service Bus duplicate detection: https://learn.microsoft.com/azure/service-bus-messaging/duplicate-detection
- Service Bus message sessions: https://learn.microsoft.com/azure/service-bus-messaging/message-sessions
- Service Bus quotas: https://learn.microsoft.com/azure/service-bus-messaging/service-bus-quotas
- Blob Storage client library for Python: https://learn.microsoft.com/python/api/overview/azure/storage-blob-readme
- User delegation SAS: https://learn.microsoft.com/rest/api/storageservices/create-user-delegation-sas
- Event Hubs client library for Python: https://learn.microsoft.com/python/api/overview/azure/eventhub-readme
- Event Hubs features (partitions, consumer groups, checkpointing): https://learn.microsoft.com/azure/event-hubs/event-hubs-features
- Key Vault secrets client library: https://learn.microsoft.com/python/api/overview/azure/keyvault-secrets-readme
- Key Vault service limits: https://learn.microsoft.com/azure/key-vault/general/service-limits
- Azure Functions overview: https://learn.microsoft.com/azure/azure-functions/functions-overview
- Logic Apps overview: https://learn.microsoft.com/azure/logic-apps/logic-apps-overview
- API Management policies (validate-jwt): https://learn.microsoft.com/azure/api-management/validate-jwt-policy
- Azure Monitor OpenTelemetry distro (Python): https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python
- Private endpoint DNS: https://learn.microsoft.com/azure/private-link/private-endpoint-dns
- Azure SDK retry/azure-core configuration: https://learn.microsoft.com/python/api/azure-core/azure.core.pipeline.policies.retrypolicy `VERIFY`
