---
name: django-integrations
description: >-
  Django integrations engineering specialist — integration architecture and boundaries, outbound
  HTTP/REST clients (httpx, requests, provider SDKs), SOAP and legacy systems, OAuth 2.0 / OIDC /
  JWT / mTLS and identity providers (Microsoft Entra ID), inbound and outbound webhooks, timeouts,
  retries, idempotency, circuit breakers and rate limits, message brokers (RabbitMQ, Kafka, Azure
  Service Bus, SQS), Celery-based integration workflows, Azure and AWS service integrations, object
  storage and file transfer (S3, Blob, SFTP), email/SMS/push providers, payment gateways and
  reconciliation, data synchronisation, integration security, observability and contract testing.
  Load when a Django system calls, is called by, exchanges files or messages with, or synchronises
  data with any external system, or when such an integration fails in production.
project: django-agentic-engineering
abstract: >-
  Every external system sits behind one adapter with an explicit contract, a timeout, a classified
  failure model, idempotent side effects and its own telemetry. A concise design workflow and
  navigation guide here; eighteen focused references under references/ loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, httpx, requests, Celery, PostgreSQL, Redis, RabbitMQ, Kafka, Azure, AWS]
domain: [Software Engineering, Integrations]
---

# Django Integrations Engineering Specialist

Engineering judgement for connecting Django systems to the outside world: choose the communication
pattern, define the contract and ownership, make every failure mode explicit, and prove it under
failure. Baseline: **Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x**;
Django 6.0 only where qualified "(Django ≥ 6.0)", provider behaviour qualified "(provider-specific)",
uncertain claims marked `VERIFY`. Third-party packages are labelled `[third-party: package]` — check
the lock file; never assume one is installed, and never assume Celery, Redis or a broker is required.

## Activate when

- Calling an external API or SDK (payments, CRM, ERP, identity, email/SMS, storage, partner APIs).
- Receiving webhooks or callbacks, or sending webhooks to customers.
- Publishing to or consuming from a queue, topic or event stream; running integration work in tasks.
- Integrating with Azure or AWS services, SOAP/legacy systems, SFTP drops or object storage.
- Synchronising or reconciling data with a system of record; designing an anti-corruption layer.
- Reviewing integration code for timeouts, retries, idempotency, secrets, SSRF or logging hygiene.
- Troubleshooting duplicates, lost events, provider outages, token failures, queue backlogs or drift.

## Integration engineering principles

1. **One adapter per external system.** Only the adapter knows URLs, auth, wire formats and SDK
   types; domain code depends on a port and receives domain objects and normalised errors.
2. **Contracts are explicit and validated** at the boundary, in both directions; versioned changes.
3. **Every call has a deadline.** Connect/read/pool timeouts always; total time fits the caller's budget.
4. **Classify every failure**: transient, permanent, rate-limited, auth, contract, **ambiguous**. A
   timeout does not prove the remote operation failed; a 2xx does not prove business success.
5. **Retry only what is safe**: idempotent operations or idempotency keys; bounded, backed off,
   jittered, budgeted. Retries and broker guarantees never give business-level exactly-once —
   idempotent consumers and handlers do.
6. **No network I/O inside `transaction.atomic()`.** Commit, then dispatch (`on_commit`, outbox);
   avoid distributed transactions — use outbox/inbox, sagas and reconciliation.
7. **Inbound is untrusted**: verify signatures on the raw body, bound sizes, dedupe, persist, then process.
8. **Least privilege, no secrets in code or logs**; prefer managed/workload identity to static keys.
9. **Observable by default**: correlation IDs, per-provider latency/error/retry metrics, DLQ and lag alerts.
10. **Simplest mechanism that meets the requirement** — a synchronous call before a task, a task
    before a broker, a broker before an event platform. Justify each step up in an ADR.

## Integration design workflow

1. **Requirement & external system** — business outcome, SLA, volume, provider limits and sandbox
   → `integration-engineering-fundamentals.md`.
2. **Ownership & boundaries** — system of record, owning Django app, anti-corruption layer
   → fundamentals, `integration-architecture-patterns.md`.
3. **Communication pattern** — sync request/response, async task, webhook, message/event, file, sync job.
4. **Contract** — schema, versioning, identifiers, error model, ordering and duplicate semantics.
5. **Authentication** → `authentication-and-identity-integrations.md`.
6. **Failure behaviour** — timeouts, retries, idempotency, breaker, fallback, reconciliation
   → `integration-reliability-and-resilience.md`.
7. **Implement the adapter/client** — the reference for the transport (table below).
8. **Observability** → `integration-observability-and-operations.md`.
9. **Tests** — mocked transport, contract tests, failure simulation → `integration-testing-and-contracts.md`.
10. **Validate** security, reliability and recovery with the checklist below → `integration-security.md`.

Every recommendation states its impact on **security, reliability, data consistency,
observability, operational complexity, performance and cost**. Inspect only the adapter, its
callers, the related models/tasks and the relevant settings block. Load one to three references
per step — never all.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [integration-engineering-fundamentals.md](references/integration-engineering-fundamentals.md) | Principles, point-to-point vs hub, sync vs async, request/response vs events, coupling, contracts, data ownership, boundaries, lifecycle, failure domains, decision records |
| [integration-architecture-patterns.md](references/integration-architecture-patterns.md) | Adapter, gateway, facade, ACL, ports & adapters, outbox, inbox, saga, process manager, orchestration vs choreography, strangler, API composition, compensation, 2PC limits |
| [http-and-rest-api-integrations.md](references/http-and-rest-api-integrations.md) | httpx/requests clients, pooling, timeout types, status handling, pagination, rate-limit headers, response validation, error normalisation, streaming, large payloads, API versions, SDK wrapping |
| [integration-reliability-and-resilience.md](references/integration-reliability-and-resilience.md) | Failure classification, retry policy, backoff/jitter, retry budgets, deadlines, idempotency keys, circuit breakers, bulkheads, rate/concurrency limits, fallback, partial failure |
| [soap-and-legacy-integrations.md](references/soap-and-legacy-integrations.md) | SOAP/WSDL, namespaces, schema validation, WS-Security, zeep, safe XML parsing, REST-to-SOAP adapters, legacy modernisation |
| [authentication-and-identity-integrations.md](references/authentication-and-identity-integrations.md) | API keys, Basic, bearer, OAuth 2.0 flows, OIDC, token caching/refresh/rotation, JWT/JWKS validation, Entra ID, service-to-service auth, mTLS |
| [webhooks-and-inbound-integrations.md](references/webhooks-and-inbound-integrations.md) | Receiver design, signatures/HMAC, timestamps/replay, dedupe, persistence, async processing, ordering, DLQ/replay, outbound webhook delivery, provider differences |
| [messaging-and-event-driven-integrations.md](references/messaging-and-event-driven-integrations.md) | Queues vs topics, RabbitMQ, Kafka, Service Bus, Redis limits, acks, visibility, consumer groups, ordering, delivery semantics, DLQ/poison, schema evolution, replay, backpressure |
| [celery-and-background-integrations.md](references/celery-and-background-integrations.md) | When to use Celery, on_commit dispatch, routing, retries, task idempotency, scheduling, results, worker failure, serialization, canvas workflows, long-running integrations, monitoring |
| [azure-integrations.md](references/azure-integrations.md) | azure-identity, Service Bus, Blob/Storage, Event Hubs, Functions, Key Vault, Entra ID, API Management, Logic Apps, Azure Monitor |
| [aws-and-cloud-integrations.md](references/aws-and-cloud-integrations.md) | boto3 configuration, S3, SQS, SNS, EventBridge, Secrets Manager, Lambda; managed/workload identity, private endpoints, SDK vs service guarantees |
| [file-transfer-and-object-storage.md](references/file-transfer-and-object-storage.md) | Storage abstraction, presigned/SAS URLs, multipart/chunked transfer, streaming, checksums, SFTP/FTPS, file-drop integrations, lifecycle, document systems |
| [email-sms-and-notification-integrations.md](references/email-sms-and-notification-integrations.md) | SMTP and email APIs, SMS, push, provider abstraction/failover, templates, delivery and bounce tracking, notification idempotency |
| [payment-and-financial-integrations.md](references/payment-and-financial-integrations.md) | Payment state machines, authorise/capture/refund, payment webhooks, duplicate prevention, settlement files, reconciliation, monetary precision, auditability |
| [data-synchronization-and-reconciliation.md](references/data-synchronization-and-reconciliation.md) | Source of truth, mapping/transformation, full vs incremental sync, CDC, checkpoints, conflicts, duplicates, external IDs, reconciliation, failed-record recovery |
| [integration-security.md](references/integration-security.md) | TLS/mTLS, credential protection and rotation, token storage, request signing, SSRF, XXE, unsafe deserialisation, input validation, redaction/PII, audit, least privilege, dependencies |
| [integration-observability-and-operations.md](references/integration-observability-and-operations.md) | Structured logs, correlation IDs, OpenTelemetry, latency/error/retry metrics, queue depth and lag, DLQ monitoring, health checks, alerting, dashboards, runbooks |
| [integration-testing-and-contracts.md](references/integration-testing-and-contracts.md) | MockTransport, respx/responses, fakes, contract validation, Pact, webhook/auth tests, sandboxes, failure/timeout/retry simulation, broker and cloud SDK stubbing |

## Task-based loading

| Task | Load |
|---|---|
| Design a new integration / choose sync vs async | integration-engineering-fundamentals.md, integration-architecture-patterns.md |
| Build a client for a third-party REST API | http-and-rest-api-integrations.md, integration-reliability-and-resilience.md |
| Add OAuth client credentials / call a token-protected API | authentication-and-identity-integrations.md (+ http-and-rest-api-integrations.md) |
| Validate JWTs from an IdP / Entra ID sign-in or app roles | authentication-and-identity-integrations.md (+ azure-integrations.md) |
| Receive provider webhooks | webhooks-and-inbound-integrations.md, integration-security.md |
| Send webhooks to customers | webhooks-and-inbound-integrations.md, integration-reliability-and-resilience.md |
| Retry storms, duplicate side effects, provider outage handling | integration-reliability-and-resilience.md (+ integration-observability-and-operations.md) |
| Publish domain events reliably to a broker | integration-architecture-patterns.md (outbox), messaging-and-event-driven-integrations.md |
| Consume from RabbitMQ/Kafka/Service Bus/SQS | messaging-and-event-driven-integrations.md (+ azure- or aws-and-cloud-integrations.md) |
| Move integration calls to Celery | celery-and-background-integrations.md, integration-reliability-and-resilience.md |
| Multi-step cross-system workflow with compensation | integration-architecture-patterns.md, celery-and-background-integrations.md |
| SOAP/WSDL or legacy ERP integration | soap-and-legacy-integrations.md (+ integration-security.md for XML) |
| Azure Service Bus, Blob, Key Vault, managed identity | azure-integrations.md |
| S3, SQS, SNS, EventBridge, Secrets Manager, IRSA | aws-and-cloud-integrations.md |
| Direct-to-storage uploads, large files, SFTP drops | file-transfer-and-object-storage.md (+ cloud reference) |
| Email/SMS/push sending, bounces, provider failover | email-sms-and-notification-integrations.md, webhooks-and-inbound-integrations.md |
| Payment gateway, refunds, settlement reconciliation | payment-and-financial-integrations.md, webhooks-and-inbound-integrations.md |
| Sync master data with a CRM/ERP | data-synchronization-and-reconciliation.md (+ http- or messaging reference) |
| Security review of an integration | integration-security.md (+ owner of the transport) |
| Dashboards, alerts, tracing, runbooks for integrations | integration-observability-and-operations.md |
| Test an integration without the real provider | integration-testing-and-contracts.md |
| "Provider says it sent it, we never saw it" / drift between systems | integration-observability-and-operations.md, data-synchronization-and-reconciliation.md |

## Skill pairings and handoffs

This skill owns **communication with external systems, its reliability, messaging, cloud services
and synchronisation**; the paired skill owns the mechanism or the general practice.

| Topic | Skill |
|---|---|
| Settings, logging config, async/ASGI mechanics, storage API, signals and commands | [django-core](../django-core/SKILL.md) |
| Architectural style, app boundaries, service layer, domain events, whether to distribute | [django-architecture](../django-architecture/SKILL.md) |
| Building DRF endpoints (including the API others call), serializers, DRF auth/throttles | [django-rest-framework](../django-rest-framework/SKILL.md) |
| General application security: SSRF depth, secrets/crypto, injection, auditing, supply chain | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test framework mechanics, mocking tools, task/transaction test mechanics, CI | [django-testing](../django-testing/SKILL.md) |
| Worker/pool sizing, profiling, async performance, queue throughput | [django-performance](../django-performance/SKILL.md) |
| Transactions, `select_for_update`, constraints, bulk operations | [django-orm](../django-orm/SKILL.md) |
| Import/export and ETL pipelines over files | [django-data-processing](../django-data-processing/SKILL.md) |
| Locking, logical replication/CDC internals, partitioning of event tables | [postgresql-engineering](../postgresql-engineering/SKILL.md) |

Common pairings: payment provider → payment + webhooks + reliability (with django-security-engineering);
event publishing → architecture-patterns + messaging (with django-architecture event-driven
architecture); slow provider dragging the site → reliability + celery (with django-performance
concurrency); flaky integration tests → testing-and-contracts (with django-testing mocking).

## Integration decision guidelines

| Decision | Prefer | Escalate when | Because |
|---|---|---|---|
| Sync vs async | Sync call within the request if fast, bounded and needed for the response | User needn't wait, provider slow/unreliable, fan-out → task or message | Async adds queues, eventual consistency and ops cost |
| Background mechanism | `on_commit` + Celery task (or Django ≥ 6.0 Tasks with a production backend) | Cross-service consumers, replay, ordering → broker | A broker is infrastructure you must run and monitor |
| Event publication | Transactional outbox + relay | — never dual-write DB and broker | Dual writes lose or invent events |
| Inbound duplicates | Unique constraint on external event/message ID (inbox) | High volume → TTL'd dedupe store plus idempotent handlers | Every provider and broker redelivers |
| Retries | Bounded exponential backoff + jitter on transient errors, idempotent ops only | Sustained failure → circuit breaker, DLQ, alert | Unbounded retries amplify outages |
| Non-idempotent call timed out | Mark ambiguous, query provider/reconcile | — | Blind retry duplicates side effects |
| Auth to a cloud service | Managed/workload identity | Static keys only where unsupported, rotated and vaulted | No long-lived secret to leak |
| Auth to a partner API | OAuth client credentials with cached tokens; mTLS for high assurance | — | Scoped, expiring, revocable |
| Webhook trust | HMAC/asymmetric signature on raw body + timestamp window | IP allow-list only as an extra layer | IPs change; signatures prove origin and integrity |
| Large files | Direct-to-storage presigned/SAS URLs, streaming | — | Keeps bytes off app workers |
| Data sync | Provider events/CDC + periodic reconciliation | Full re-sync for recovery | Events drift; reconciliation finds it |
| Cross-system consistency | Saga with compensation + reconciliation | — avoid 2PC/XA | Distributed transactions rarely exist across SaaS APIs |
| SDK vs raw HTTP | Official SDK wrapped in an adapter, retries/timeouts configured explicitly | Raw HTTP when the SDK is heavy, sync-only or opaque | SDK defaults are not your SLA |

Record hard-to-reverse choices (broker, event schema, identity model, system of record) in an ADR.

## Validation checklist

- [ ] The external system has one adapter; domain code imports no HTTP/SDK types.
- [ ] System of record, data ownership and external identifiers are documented and stored.
- [ ] Contract is validated at the boundary; unknown fields tolerated, breaking changes detected.
- [ ] Every outbound call sets connect/read/pool timeouts that fit the caller's deadline.
- [ ] Failures are classified; only transient errors on idempotent/keyed operations are retried, bounded with jitter.
- [ ] Timeouts on non-idempotent operations are treated as ambiguous and reconciled.
- [ ] No network I/O inside `transaction.atomic()`; dispatch uses `on_commit` or an outbox.
- [ ] Inbound webhooks/messages: verified (raw body), size-bounded, deduplicated, persisted, processed asynchronously, replayable.
- [ ] Consumers and handlers are idempotent and tolerate reordering; poison messages reach a DLQ that is alerted on.
- [ ] Credentials come from a secret store or managed identity, are least-privilege, rotatable and never logged.
- [ ] User-influenced outbound URLs are SSRF-protected; XML parsing is XXE-safe; no unsafe deserialisation.
- [ ] Logs carry correlation and external IDs with sensitive data redacted; per-provider latency/error/retry metrics, queue depth/lag and DLQ alerts exist.
- [ ] Tests cover success, timeout, 429/5xx, 4xx, malformed payload, auth failure, bad signature and duplicate delivery; contracts are tested.
- [ ] A runbook covers provider outage, credential rotation, DLQ replay and reconciliation.
- [ ] Version- and provider-specific claims are qualified; `VERIFY` items were checked or disclosed.
