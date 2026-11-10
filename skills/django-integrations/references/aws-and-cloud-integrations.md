# AWS and General Cloud Integrations

**Scope:** How Django applications integrate with AWS services through boto3 (S3, SQS, SNS, EventBridge, Secrets Manager, Lambda), plus the provider-neutral cloud concepts every cloud integration needs: workload identity, credential management, private networking, SDK retry/timeout configuration and the difference between SDK behaviour and service guarantees. Azure specifics live in [azure-integrations.md](azure-integrations.md); broker theory in [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md); storage abstractions, presigned URLs and multipart uploads in [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md).

## 1. Purpose and scope

Baseline: Python 3.12+, Django 5.2 LTS, PostgreSQL, Redis, Celery 5.x where present, [third-party: boto3] / botocore (current 1.3x line `VERIFY`).

This reference answers:

- Which credential source a Django process on AWS should use, and why static keys are a last resort.
- How to configure boto3 clients (retry mode, timeouts, connection pool) and share them safely.
- How to publish and consume with SQS/SNS/EventBridge without losing or duplicating business effects.
- How to read secrets from Secrets Manager without turning it into a hot-path dependency.
- When Lambda belongs in the integration and how it talks to Django.
- Cross-cloud concepts (Section 2.2) reused by the Azure reference.

Not every application needs direct cloud access. A Django app that only stores files can use django-storages and never import boto3 elsewhere; an app that calls one AWS-hosted partner API over HTTPS needs no SDK at all. Each new service dependency adds IAM surface, failure modes and test seams — add it deliberately.

## 2. Core concepts

### 2.1 AWS building blocks

| Service | Role in integrations | Delivery/consistency notes |
|---|---|---|
| **S3** | Object storage, file exchange, data lake drop zone | Strong read-after-write consistency for PUT/DELETE since Dec 2020; event notifications are at-least-once and may arrive out of order |
| **SQS Standard** | Work queue | At-least-once, best-effort ordering, nearly unlimited throughput |
| **SQS FIFO** | Ordered work queue per `MessageGroupId` | Exactly-once *send* deduplication within a 5-minute window; throughput limits per queue/group `VERIFY` |
| **SNS** | Pub/sub fan-out to SQS, Lambda, HTTPS, email/SMS | At-least-once to subscribers; filter policies on attributes or body |
| **EventBridge** | Event bus with content-based routing rules, partner/SaaS events, scheduler | At-least-once to targets; retries up to 24 h then DLQ if configured `VERIFY` |
| **Secrets Manager** | Versioned secrets with rotation Lambdas | Staging labels `AWSCURRENT`/`AWSPENDING`/`AWSPREVIOUS` |
| **SSM Parameter Store** | Cheaper config/secrets (SecureString) | Lower throughput limits on standard tier `VERIFY` |
| **Lambda** | Event-driven glue | Async invocations retried twice by default `VERIFY`; at-least-once |

### 2.2 General cloud integration concepts (provider-neutral)

| Concept | Meaning | AWS | Azure |
|---|---|---|---|
| **Workload identity** | The running workload gets short-lived credentials from the platform; no long-lived secret | IAM role for EC2/ECS task role, EKS IRSA or EKS Pod Identity | Managed identity, AKS workload identity |
| **Workload identity federation** | An external OIDC issuer (GitHub Actions, GKE, on-prem) exchanges its token for cloud credentials | `AssumeRoleWithWebIdentity` | Federated identity credentials on an app registration |
| **Data-plane vs control-plane permissions** | Reading a queue vs creating it | IAM actions `sqs:ReceiveMessage` vs `sqs:CreateQueue` | Data roles vs management roles |
| **Private connectivity** | Traffic to the service never traverses the public internet | VPC gateway endpoints (S3, DynamoDB), interface endpoints (PrivateLink) | Private endpoints + private DNS zones |
| **SDK-based vs REST-based integration** | SDK handles signing, retries, pagination; raw REST gives control and fewer deps | boto3 vs SigV4-signed HTTP | azure-* SDKs vs REST |
| **SDK behaviour vs service guarantee** | SDK retries/timeouts are client-side choices; the service's delivery, consistency and durability are contractual properties | e.g. boto3 retrying `SendMessage` can create a duplicate SQS message; SQS Standard's at-least-once is the service guarantee | Same distinction for Service Bus sends |

Rule: design correctness around the **service guarantee** (at-least-once → idempotent consumer), and tune the **SDK behaviour** for latency and cost.

## 3. Internal mechanics

### 3.1 Credential provider chain (botocore)

Order (abridged; `VERIFY` against current docs): explicit parameters → environment variables (`AWS_ACCESS_KEY_ID`…) → web identity token file (`AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` — this is how IRSA works) → shared config/credentials files and SSO profiles → container credentials endpoint (`AWS_CONTAINER_CREDENTIALS_FULL_URI`/`RELATIVE_URI` — ECS task roles and EKS Pod Identity) → EC2 instance metadata (IMDSv2).

Refreshable credentials are refreshed automatically before expiry by botocore. A `boto3.Session` caches credentials; creating sessions per request throws that away.

**IRSA vs EKS Pod Identity:** IRSA annotates a Kubernetes service account with a role ARN; the pod receives a projected OIDC token that boto3 exchanges via STS. Pod Identity (newer) uses an agent and the container credentials endpoint, with role associations managed via EKS API rather than OIDC trust policies. Both give per-pod least privilege; prefer Pod Identity for new EKS clusters if your SDK versions support it `VERIFY` (minimum botocore version applies).

### 3.2 boto3 retry and timeout configuration

```python
from botocore.config import Config

AWS_CONFIG = Config(
    region_name="eu-west-1",
    retries={"mode": "standard", "max_attempts": 4},  # total attempts including the first
    connect_timeout=3,
    read_timeout=10,
    max_pool_connections=20,  # default 10; raise for threaded workers sharing a client
    tcp_keepalive=True,
)
```

Retry modes:

| Mode | Behaviour |
|---|---|
| `legacy` | Historic default; 5 attempts; limited set of retryable errors `VERIFY` |
| `standard` | 3 attempts default; exponential backoff with jitter; retries throttling, transient and some timeout errors; retry quota (token bucket) to stop retry storms |
| `adaptive` | `standard` + client-side rate limiting that slows the client when throttled. Experimental per docs `VERIFY`; useful for batch jobs, not latency-sensitive web requests |

Set via `Config(retries=...)` or `AWS_RETRY_MODE`/`AWS_MAX_ATTEMPTS` env vars. The default `read_timeout` is 60 s — too long for a web request.

### 3.3 Client thread safety

- **Clients** (`boto3.client(...)`) are thread-safe for concurrent calls (with caveats on mutating events/handlers) — share one per process per service/region.
- **Sessions** and **resources** (`boto3.resource`) are **not** thread-safe. Create a session per thread if you need resources; prefer clients in shared code.
- Create clients after fork in prefork servers; the `urllib3` pool must not be shared across processes.
- Client creation is expensive (loads JSON service models — tens of ms and MBs of memory); never per request.

### 3.4 SQS mechanics

- `ReceiveMessage` with `WaitTimeSeconds` up to 20 (long polling) — reduces empty receives and cost.
- Received messages become invisible for the **visibility timeout** (queue default 30 s, max 12 h `VERIFY`); delete with the `ReceiptHandle` after successful processing; extend with `ChangeMessageVisibility` for long work.
- `ApproximateReceiveCount` and a **redrive policy** (`maxReceiveCount`) move poison messages to a DLQ. DLQ redrive back to source is supported via API/console.
- Message size up to 256 KiB; larger payloads → store in S3 and send a pointer (claim-check) `VERIFY` (AWS has announced larger limits in some contexts).
- FIFO: `MessageGroupId` orders within a group; `MessageDeduplicationId` (or content-based dedup) suppresses duplicate *sends* within 5 minutes. A consumer crash after side effects but before delete still causes reprocessing — FIFO does not give exactly-once processing.

### 3.5 SNS → SQS fan-out

SNS topic publishes to multiple SQS queues (one per consuming service). Use **raw message delivery** to avoid the SNS JSON envelope, or parse the envelope consistently. Subscription filter policies route by message attributes (or body with `FilterPolicyScope=MessageBody`). SQS queue policy must allow the topic to send.

### 3.6 EventBridge

`PutEvents` writes up to 10 entries per call (256 KB total `VERIFY`); the response can report **partial failure** (`FailedEntryCount > 0`) with HTTP 200 — you must inspect per-entry results. Rules match on event pattern and deliver to targets with retry policy and optional DLQ. EventBridge Scheduler replaces cron-like CloudWatch Events rules.

### 3.7 Secrets Manager

`GetSecretValue` returns `SecretString`/`SecretBinary` for a version stage. Rotation via Lambda progresses `createSecret → setSecret → testSecret → finishSecret`; during rotation both `AWSCURRENT` and `AWSPENDING` may be valid. Calls are billed and rate limited — cache in-process. AWS provides a caching library [third-party: aws-secretsmanager-caching] and the **AWS Parameters and Secrets Lambda Extension** for Lambda.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Alternative | When the alternative wins |
|---|---|---|---|
| Credentials | Workload identity (task role / IRSA / Pod Identity / instance profile) | Static IAM user keys | On-prem or third-party hosts without federation — then rotate keys ≤ 90 days and scope tightly; better still, use IAM Roles Anywhere `VERIFY` |
| CI/CD access | OIDC federation (e.g. GitHub Actions → `AssumeRoleWithWebIdentity`) | Long-lived keys in CI secrets | Never, if federation is available |
| Queue | SQS Standard + idempotent consumer | SQS FIFO | True per-key ordering requirement and throughput fits FIFO limits |
| Fan-out | SNS → SQS per consumer | Multiple producers writing multiple queues | One consumer only — skip SNS |
| Cross-account / SaaS routing, content-based rules | EventBridge | SNS filter policies | Simple fan-out with attribute filters, lower latency |
| Background work inside Django | Celery (Redis/RabbitMQ broker) or Celery with SQS broker | Raw SQS consumer command | Messages produced/consumed by non-Celery systems — use raw SQS with a schema, not Celery's message format |
| Secrets | Injected at deploy (ECS `secrets` from Secrets Manager/SSM, EKS Secrets Store CSI) | Runtime `GetSecretValue` with cache | Rotation must apply without restart |
| Lambda vs Django worker | Django worker for logic that needs the ORM/domain | Lambda | Event glue, S3-triggered transforms, scale-to-zero, isolated runtimes |
| SDK vs REST | boto3 | Signed raw HTTP (`botocore.auth.SigV4Auth`) | Minimal dependency footprint, async-only stack (or use [third-party: aioboto3]) |

Cost: SQS charges per request (64 KB chunks), so long polling and batching (`SendMessageBatch`, `DeleteMessageBatch`, 10 per call) cut cost; NAT gateway data processing charges are a classic hidden cost — VPC endpoints for S3/SQS avoid them. Pricing `VERIFY`.

## 5. Django implementation patterns

### 5.1 Process-scoped clients

```python
# apps/integrations/aws.py
from functools import cache

import boto3
from botocore.config import Config
from django.conf import settings

_BASE = Config(
    region_name=settings.AWS_REGION,
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=2,
    read_timeout=5,
    max_pool_connections=settings.AWS_MAX_POOL_CONNECTIONS,
)


@cache
def aws_client(service: str, **overrides):
    # one boto3 Session per process; clients are thread-safe and reused
    cfg = _BASE.merge(Config(**overrides)) if overrides else _BASE
    return boto3.session.Session().client(service, config=cfg)


def s3():
    return aws_client("s3", read_timeout=60)  # data transfers need longer reads


def sqs():
    return aws_client("sqs", read_timeout=25)  # > WaitTimeSeconds (20) for long polling
```

`@cache` with kwargs works because they are hashable scalars. Credentials come from the provider chain — **no** `aws_access_key_id=` arguments in code.

### 5.2 Publishing via the outbox to SNS/SQS

The outbox model/relay is owned by [integration-architecture-patterns.md](integration-architecture-patterns.md); the AWS publisher must handle partial batch failures:

```python
# apps/integrations/publishers/sns.py
import json

from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from apps.integrations.aws import aws_client
from apps.integrations.errors import AmbiguousOutcomeError, PermanentIntegrationError, TransientIntegrationError
from apps.integrations.models import OutboxMessage


def publish(messages: list[OutboxMessage], topic_arn: str) -> list[OutboxMessage]:
    """Returns messages confirmed published. Others stay in the outbox for the next relay pass."""
    entries = [
        {
            "Id": str(i),
            "Message": json.dumps(m.payload),
            "MessageAttributes": {"type": {"DataType": "String", "StringValue": m.headers["type"]}},
            # FIFO topics only: "MessageGroupId": m.key, "MessageDeduplicationId": str(m.id),
        }
        for i, m in enumerate(messages[:10])
    ]
    try:
        resp = aws_client("sns").publish_batch(TopicArn=topic_arn, PublishBatchRequestEntries=entries)
    except ReadTimeoutError as exc:
        raise AmbiguousOutcomeError(provider="sns", operation="publish_batch") from exc
    except ConnectTimeoutError as exc:
        raise TransientIntegrationError(provider="sns", operation="publish_batch") from exc
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"AuthorizationError", "InvalidParameter", "NotFound"}:
            raise PermanentIntegrationError(provider="sns", operation="publish_batch") from exc
        raise TransientIntegrationError(provider="sns", operation="publish_batch") from exc
    ok = {int(s["Id"]) for s in resp.get("Successful", [])}
    # resp["Failed"] entries carry SenderFault: True → permanent, False → retry
    return [messages[i] for i in sorted(ok)]
```

Consumers must tolerate duplicates: an ambiguous timeout + relay retry publishes twice.

### 5.3 SQS consumer as a management command

```python
# apps/billing/management/commands/consume_invoices.py
import json
import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction

from apps.billing.services import record_invoice_event
from apps.integrations.aws import sqs
from apps.integrations.errors import PermanentIntegrationError
from apps.integrations.models import InboxMessage

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    def handle(self, *args, **opts):
        running = True

        def stop(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        url = settings.SQS_INVOICE_QUEUE_URL
        while running:
            resp = sqs().receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=20,
                VisibilityTimeout=120, MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
            for msg in resp.get("Messages", []):
                close_old_connections()
                try:
                    body = json.loads(msg["Body"])
                    with transaction.atomic():
                        _, created = InboxMessage.objects.get_or_create(source="sqs:invoices", message_id=body["event_id"])
                        if created:
                            record_invoice_event(body)
                    sqs().delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
                except PermanentIntegrationError:
                    # leave it: redrive policy moves it to the DLQ after maxReceiveCount
                    # (or send to DLQ explicitly and delete, to avoid wasting receives)
                    logger.warning("sqs.permanent_failure", extra={"message_id": msg["MessageId"]})
                except Exception:
                    # transient: message reappears after the visibility timeout
                    logger.exception("sqs.handler_failed", extra={"message_id": msg["MessageId"]})
```

Notes:

- Deduplicate on a **business event id** inside the payload, not the SQS `MessageId` — a duplicate send produces a new `MessageId`.
- Delete only after commit. Visibility timeout ≥ worst-case processing time × safety factor; extend with `change_message_visibility` for long tasks.
- Stop cleanly on SIGTERM: finish the current batch, don't receive more (ECS/Kubernetes send SIGTERM before SIGKILL).
- For Celery-native work, Celery's SQS transport ([third-party: kombu] with `boto3`/`pycurl` deps `VERIFY`) exists; it has no result backend on SQS and requires `visibility_timeout` ≥ longest ETA/countdown — see [celery-and-background-integrations.md](celery-and-background-integrations.md).
### 5.4 EventBridge publishing with partial-failure handling

```python
def put_events(entries: list[dict]) -> list[dict]:
    resp = aws_client("events").put_events(Entries=entries)  # ≤ 10 entries
    if resp["FailedEntryCount"]:
        failed = [e for e, r in zip(entries, resp["Entries"]) if "ErrorCode" in r]
        return failed  # caller re-queues these; HTTP 200 did not mean all succeeded
    return []
```

"A successful HTTP request does not guarantee successful business processing" applies literally here.

### 5.5 Secrets Manager with in-process cache

```python
# apps/integrations/secrets_aws.py
import json
import time
from threading import Lock

from apps.integrations.aws import aws_client

_cache: dict[str, tuple[float, dict]] = {}
_lock = Lock()
TTL = 300


def get_secret_json(secret_id: str) -> dict:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(secret_id)
        if hit and now - hit[0] < TTL:
            return hit[1]
    value = json.loads(aws_client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"])
    with _lock:
        _cache[secret_id] = (now, value)
    return value


def invalidate(secret_id: str) -> None:
    with _lock:
        _cache.pop(secret_id, None)
```

On a downstream 401 after rotation: `invalidate()`, refetch, retry once. Database passwords rotated by Secrets Manager need connection re-creation — Django's `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` (Django ≥ 4.1) help, but a password change still requires the settings value to update; prefer IAM database authentication or a pooler that handles it ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

### 5.6 Lambda integration patterns

| Pattern | Flow | Notes |
|---|---|---|
| Lambda calls Django | S3 event → Lambda → signed request to Django internal API | Django stays the owner of invariants; authenticate with IAM-signed requests via API Gateway IAM auth or an OIDC/JWT token |
| Django invokes Lambda | `lambda.invoke(InvocationType="Event")` from a Celery task | Async invoke returns 202 only — outcome is unknown; have Lambda report back (callback, queue) |
| Lambda consumes SQS | SQS event source mapping → Lambda | Use `ReportBatchItemFailures` to avoid reprocessing successful records in a batch |
| Django deployed on Lambda | (Mangum/Zappa-style adapters) | Out of scope; cold starts, connection limits to PostgreSQL (use RDS Proxy) |

Never let Lambda write directly to Django-owned tables.

### 5.7 Async stacks

boto3 is synchronous. In async Django views, either call via `sync_to_async(..., thread_sensitive=False)` with a bounded thread pool, or use [third-party: aioboto3] / [third-party: aiobotocore]. Do not block the event loop ([async-django-performance.md](../../django-performance/references/async-django-performance.md)).

## 6. Production use cases

1. **Order fulfilment fan-out.** Django outbox → SNS `order-events` → SQS `warehouse`, `invoicing`, `analytics`, each with its own DLQ and consumer; filter policies so analytics receives everything and warehouse only `order.paid`.
2. **Partner file drop.** Partner writes to a cross-account S3 prefix; S3 event → SQS → Django consumer validates manifest and imports idempotently ([file-transfer-and-object-storage.md](file-transfer-and-object-storage.md)).
3. **SaaS events via EventBridge partner bus.** Zendesk/Datadog-style partner events matched by rule → SQS → Django.
4. **Nightly reconciliation.** EventBridge Scheduler triggers an ECS task running `manage.py reconcile_payments` — no Celery beat required.
5. **Credential hygiene migration.** Replace IAM user keys in `.env` with ECS task roles; CI uses GitHub OIDC; old keys deactivated after CloudTrail shows no use.

## 7. Advantages and disadvantages

| Option | Advantages | Disadvantages |
|---|---|---|
| Workload identity | No long-lived secrets; per-workload least privilege; CloudTrail attribution | Platform-specific setup; local dev needs SSO profile |
| boto3 | Signing, retries, pagination, waiters built in | Heavy import/memory; sync only; generated API hard to type (use [third-party: boto3-stubs]) |
| SQS | Simple, durable, cheap, scales | No fan-out; Standard reorders/duplicates; 256 KiB limit; visibility tuning |
| SNS | Easy fan-out, filtering | No persistence beyond retries; needs SQS behind it for durability |
| EventBridge | Content routing, SaaS integrations, archive & replay | Higher latency than SNS; 256 KB events; partial failures |
| Secrets Manager runtime reads | Rotation without restart | Cost, latency, new runtime dependency |
| Lambda | Scale-to-zero glue | Separate deployment, observability gap, at-least-once with retries |

## 8. Security and performance considerations

Security:

- IAM policies scoped to resource ARNs and actions (`sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility` on one queue), not `sqs:*` on `*`.
- Require IMDSv2 on EC2 (hop limit 1 for containers) to blunt SSRF credential theft; SSRF from Django to `169.254.169.254` is the classic cloud credential leak ([ssrf-prevention.md](../../django-security-engineering/references/ssrf-prevention.md)).
- S3: Block Public Access on; bucket policies require TLS (`aws:SecureTransport`); SSE-KMS where key-level access control/audit is needed; restrict to VPC endpoint via `aws:SourceVpce` when appropriate.
- SQS/SNS: SSE enabled; queue policies restrict `aws:SourceArn`.
- Never log full boto3 debug output in production — it can include signed headers and payloads.
- Treat queue payloads as untrusted; validate schemas; never unpickle.
- Private networking: VPC gateway endpoint for S3 (free) and interface endpoints for SQS/SNS/Secrets Manager/STS; enable private DNS. Without the STS endpoint, IRSA token exchange still goes over NAT `VERIFY` regional STS endpoint behaviour.

Performance:

- Reuse clients; raise `max_pool_connections` to match thread concurrency (otherwise "Connection pool is full" warnings and serialised calls).
- Batch APIs (SQS 10/batch, SNS `PublishBatch` 10, EventBridge 10).
- Long polling (`WaitTimeSeconds=20`); `read_timeout` must exceed it.
- `adaptive` retry mode for bulk jobs hitting throttles; `standard` with small `max_attempts` inside web requests.
- Large S3 transfers: `TransferConfig` (multipart threshold/concurrency) — [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md).

## 9. Common mistakes and anti-patterns

- Static keys in `settings.py`, `.env` committed to git, or Docker images.
- `boto3.client(...)` created inside a view or task body for every call.
- Sharing a `boto3.resource`/`Session` across threads.
- Default 60 s `read_timeout` and legacy 5 retries in a web request → 5-minute worst case.
- Treating SQS FIFO deduplication as exactly-once processing.
- Deleting the SQS message before the DB transaction commits; or never deleting (visibility timeout loop).
- Visibility timeout shorter than processing time → concurrent duplicate processing.
- Ignoring `FailedEntryCount` / `Failed` in batch responses.
- Lambda async invoke treated as "done".
- Polling SQS with short polls in a tight loop (cost, CPU).
- Giving the web tier permissions that only the worker needs.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `NoCredentialsError` / `Unable to locate credentials` | Chain found nothing: missing role/annotation, wrong service account | `aws sts get-caller-identity` from the pod/task; env vars `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_CONTAINER_CREDENTIALS_FULL_URI` |
| `AccessDenied` | Policy missing action/resource; KMS key policy; bucket policy `aws:SourceVpce` | CloudTrail event, IAM policy simulator |
| `ExpiredToken` in long jobs | Static STS credentials passed explicitly instead of refreshable chain | Let botocore resolve credentials |
| Connect timeouts only in private subnets | No NAT/VPC endpoint; endpoint SG blocks 443; private DNS off | `nslookup sqs.<region>.amazonaws.com` resolves to VPC IP |
| Same SQS message processed twice concurrently | Visibility timeout < processing time | `ApproximateReceiveCount`, handler duration p99 |
| DLQ filling | Poison messages / schema change | Inspect DLQ, alarm on `ApproximateNumberOfMessagesVisible` > 0 |
| `ThrottlingException` / `SlowDown` (S3 503) | Burst above partition/API limits | Backoff, key prefix distribution, adaptive mode |
| High NAT bill | S3/SQS traffic via NAT | Add gateway/interface endpoints |

Validation checklist:

- [ ] No static AWS keys in code, images or repo; workload identity in all deployed environments.
- [ ] IAM policies scoped to specific ARNs; web and worker roles separated.
- [ ] Every boto3 client has explicit `connect_timeout`, `read_timeout`, retry mode and attempts.
- [ ] Clients are process-scoped and created after fork; no shared sessions/resources across threads.
- [ ] Consumers deduplicate on business ids and delete/ack after commit; DLQs configured and alarmed.
- [ ] Batch API responses checked for partial failures.
- [ ] Tests use [third-party: moto] or botocore `Stubber` — see [integration-testing-and-contracts.md](integration-testing-and-contracts.md).

## 11. Related references and skills

- [azure-integrations.md](azure-integrations.md) — Azure equivalents (managed identity, Service Bus, Blob, Key Vault).
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md) — delivery semantics, ordering, DLQ.
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox/inbox, claim-check, saga.
- [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md) — S3 uploads, presigned URLs, multipart.
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — Celery with SQS broker, scheduling.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — retry/idempotency policy.
- [integration-security.md](integration-security.md) — least privilege, credential protection.
- [ssrf-prevention.md](../../django-security-engineering/references/ssrf-prevention.md) — metadata endpoint protection.
- [secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md) — secret lifecycle.
- [scalability-and-capacity-planning.md](../../django-performance/references/scalability-and-capacity-planning.md) — scaling consumers.
- [distributed-systems.md](../../django-architecture/references/distributed-systems.md) — distribution trade-offs.

## 12. Official documentation and external references

- Boto3 documentation: https://boto3.amazonaws.com/v1/documentation/api/latest/index.html
- Boto3 credentials: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html
- Boto3 retries: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/retries.html
- Boto3 configuration (`Config`): https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html
- Boto3 clients, sessions and thread safety: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/clients.html
- S3 consistency model: https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel
- SQS visibility timeout: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- SQS FIFO exactly-once processing: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-exactly-once-processing.html
- SQS dead-letter queues: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- SNS fan-out to SQS: https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html
- SNS message filtering: https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html
- EventBridge PutEvents: https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutEvents.html
- EventBridge retry policy and DLQ: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-dlq.html
- Secrets Manager rotation: https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotating-secrets.html
- Secrets Manager caching (Python): https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_cache-python.html
- Lambda event source mapping / batch item failures: https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html
- Lambda asynchronous invocation: https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html
- EKS IAM roles for service accounts: https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html
- EKS Pod Identity: https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html
- IAM OIDC federation / AssumeRoleWithWebIdentity: https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html
- VPC endpoints (PrivateLink): https://docs.aws.amazon.com/vpc/latest/privatelink/what-is-privatelink.html
- IMDSv2: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html
- Celery SQS broker: https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/sqs.html
- moto: https://docs.getmoto.org/
