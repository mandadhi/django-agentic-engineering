# Integration Engineering Fundamentals

**Scope:** How to reason about connecting a Django system to anything it does not own — coupling, communication
style, contracts, data ownership, boundaries, failure domains and the integration decision record. It does not
cover specific protocols or pattern implementations (see the owning references in section 11) nor general
architectural-style selection, which belongs to
[django-architecture](../../django-architecture/references/architectural-styles.md).

## 1. Purpose and scope

An integration is any code path where correctness depends on a system the team does not deploy, version or
operate together with the Django application: a payment provider, an identity provider, a partner REST or SOAP
API, a message broker, an object store, an SFTP drop, another internal service owned by a different team.

This reference gives the vocabulary and the decision framework every other reference in this skill assumes.
Read it before designing a new integration, when reviewing an integration proposal, or when an existing
integration keeps failing in ways that suggest the wrong communication style was chosen.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Nothing here requires Celery,
Redis or a broker — those are options selected by the decisions below, not defaults.

## 2. Core concepts

### 2.1 Integration engineering principles

1. **The remote system is unreliable, slow and changes without asking.** Design for timeouts, partial
   failure, duplicate delivery, out-of-order delivery and contract drift from day one.
2. **One owner per fact.** Every piece of data has exactly one system of record; everyone else holds a copy
   with a known staleness.
3. **Translate at the boundary.** External models never leak into domain code; an adapter (anti-corruption
   layer) converts wire shapes to domain types and external errors to the shared taxonomy in
   `apps/integrations/errors.py`.
4. **Make every effect idempotent or reconcilable.** If an operation cannot be made idempotent, it must be
   detectable and correctable after the fact.
5. **Explicit contracts, explicit versions.** A contract is schema + semantics + error model + SLA + auth.
6. **Observe every boundary crossing.** Latency, outcome, provider request id and correlation id on every call.
7. **Prefer the simplest communication style that meets the requirement.** Synchronous HTTP inside a request
   is fine when the user needs the answer now and the provider is fast; queues are for decoupling time and
   load, not for fashion.

### 2.2 Topologies

| Topology | Shape | Fits | Breaks down when |
|---|---|---|---|
| Point-to-point | Each pair of systems talks directly | 1–5 integrations, each with distinct semantics | N systems × M consumers → N·M contracts; changes ripple |
| Hub-and-spoke (ESB, iPaaS, Azure Logic Apps, API gateway) | Central hub routes/transforms | Many systems with shared canonical data, central governance | Hub becomes a bottleneck and a team dependency; business logic leaks into the hub |
| Event bus / broker (pub-sub) | Producers publish facts; consumers subscribe | Many consumers of the same fact, temporal decoupling | Consumers need answers (request-response), strict global ordering, small teams with little ops capacity |

A Django modular monolith usually ends up with **point-to-point outward adapters** (one per provider) plus,
optionally, **one broker** for facts other systems consume. A hub is normally imposed by the enterprise, not
chosen by the app team; treat it as one more external system with its own contract.

### 2.3 Communication styles

| Axis | Option A | Option B |
|---|---|---|
| Timing | **Synchronous** — caller waits for the result | **Asynchronous** — caller continues; result arrives later or never matters |
| Interaction | **Request-response** — caller asks, callee answers | **Event-driven** — producer states a fact; does not know consumers |
| Initiation | **Push** (webhook, message) | **Pull** (polling, file pickup, incremental sync) |

These axes are independent: an HTTP call can be dispatched asynchronously from a Celery task; a message
can implement request-response with a reply queue; a webhook (push) usually needs an asynchronous processor.

Key facts:

- **Synchronous coupling is temporal coupling.** The caller's availability becomes the product of its own and
  the callee's: two services at 99.9 % each give at most ~99.8 % for the combined path.
- **Asynchronous does not mean reliable.** A message is only as durable as the broker configuration, the
  producer's acknowledgement handling and the consumer's idempotency.
- **A successful HTTP response does not guarantee successful business processing.** `202 Accepted` means
  queued; even `200 OK` may carry a business-level failure in the body (common in SOAP and legacy APIs).
- **An HTTP timeout does not prove the remote operation failed.** It proves only that the answer did not
  arrive in time — see `AmbiguousOutcomeError` in
  [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md).

### 2.4 Coupling dimensions

| Dimension | Tight | Loose |
|---|---|---|
| Temporal | Both must be up at the same time | Broker/outbox buffers |
| Format | Shares the provider's schema inside the domain | Adapter maps to own types |
| Location | Hard-coded hostnames | Configuration/service discovery |
| Semantic | Consumer relies on provider's internal states/codes | Consumer relies on documented, versioned facts |
| Behavioural | Caller orchestrates callee's internal steps | Caller requests an outcome |

Loose coupling has costs: eventual consistency, more infrastructure, harder debugging. Choose per dimension,
not globally.

### 2.5 Integration contracts

A contract is more than an OpenAPI or WSDL file. Record:

- **Syntax:** schema (OpenAPI, WSDL/XSD, JSON Schema, Avro/Protobuf), encodings, size limits.
- **Semantics:** what each field means, units, currency, time zones, nullability, enum evolution rules.
- **Behaviour:** idempotency guarantees, ordering, pagination consistency, eventual-consistency windows.
- **Error model:** status codes, error body, which errors are retryable.
- **Non-functional:** rate limits, latency SLO, availability, maintenance windows, support contacts.
- **Security:** auth mechanism, scopes, IP ranges, certificate rotation schedule.
- **Versioning and deprecation policy.**

### 2.6 Data ownership and system boundaries

- **System of record (SoR):** the only system allowed to decide a fact (e.g. the PSP decides whether money
  moved; the HR system decides employment status; Django decides order lines).
- **Copies** are caches with a staleness budget and a refresh mechanism (event, webhook, poll, sync).
- **External identifiers** are stored explicitly (`ExternalReference` with unique `(system, external_id)`) —
  never overloaded into internal primary keys.

### 2.7 Integration lifecycle

1. Discovery (requirement, owner, SLA, sandbox access, contract) →
2. Design (style, contract, auth, failure behaviour; integration decision record) →
3. Build (adapter, tests with fakes and contract tests) →
4. Certify (provider sandbox, security review, load/limits check) →
5. Operate (dashboards, alerts, runbook, credential rotation) →
6. Evolve (version upgrades, deprecations) →
7. Retire (stop traffic, revoke credentials, delete data per retention).

Most incidents happen in steps 5–6, so plan them during step 2.

### 2.8 Failure domains

A failure domain is the set of things that fail together. A synchronous call from a web request puts the
provider in the request's failure domain: provider latency becomes your latency, provider outage becomes your
error rate, provider rate limits throttle your users. Techniques that **separate** failure domains: async
dispatch, bulkheads (dedicated queues/worker pools/connection pools), circuit breakers, caches with stale
reads, and fallbacks. Techniques that **join** them: synchronous chains, shared connection pools, shared
worker queues, shared credentials across environments.

## 3. Internal mechanics

### 3.1 What actually happens on a boundary crossing

```text
domain service ──calls──▶ port (Protocol) ──implemented by──▶ gateway (translate)
                                                                │
                                                                ▼
                                                    client (auth, transport, retries)
                                                                │  DNS → TCP → TLS → HTTP
                                                                ▼
                                                    provider edge (WAF, gateway, LB)
                                                                │
                                                                ▼
                                                    provider application → its DB / its downstreams
```

Every arrow is a place where the request can be lost **before** or **after** the remote side applied it. The
two cases are indistinguishable to the caller on timeout or connection reset after the request was written.

### 3.2 Three-outcome model

Every external effect yields one of: **succeeded**, **failed (definitely not applied)**, **unknown**. Code that
models only two outcomes will double-charge, double-send or silently drop work. Handle unknown via idempotency
keys (provider deduplicates), a status lookup (`GET /payments?reference=...`), or reconciliation
([data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)).

### 3.3 Consistency across a boundary

There is no shared transaction between PostgreSQL and a remote API. The options are:

- **Commit locally, then call** (after `on_commit`), with retry + idempotency; record the outcome in a second
  transaction.
- **Record intent in the same transaction** (outbox), relay later — see
  [integration-architecture-patterns.md](integration-architecture-patterns.md).
- **Call first, then commit** — only when the remote call is idempotent/side-effect-free (reads, validations),
  because a local rollback after a successful remote effect leaves an orphan.

## 4. Engineering decisions and trade-offs

### 4.1 Synchronous or asynchronous?

| Question | If yes → |
|---|---|
| Does the user need the remote result to continue (address validation, card authorisation)? | Synchronous, tight timeout, clear fallback |
| Is p99 provider latency > ~1 s or availability < your SLO? | Asynchronous, or synchronous with fallback |
| Can the effect be delayed seconds/minutes without business harm? | Asynchronous (task or outbox) |
| Do several independent systems need to know the same fact? | Publish an event (broker or webhooks) |
| Is the volume bursty relative to the provider's rate limit? | Asynchronous with a rate-limited worker queue |
| Is the operation long-running (minutes+)? | Async job + status polling or callback |

Asynchronous options, in increasing complexity: `transaction.on_commit` + thread/`asyncio` (not durable — lost on
process exit), database-backed job table polled by a management command, Celery, a broker with independent
consumers. Pick the least complex one that gives the durability you need; see
[celery-and-background-integrations.md](celery-and-background-integrations.md).

### 4.2 Direct call, SDK, or hub?

| Option | Use when | Cost |
|---|---|---|
| Plain HTTP client (httpx) | Small API surface, stable REST contract | You own auth, retries, pagination |
| Vendor SDK | Complex auth/signing (AWS SigV4, Azure AD), many operations | Dependency weight, hidden retries/timeouts — configure explicitly |
| Enterprise hub / APIM / iPaaS | Mandated by the organisation, shared canonical model | Extra hop, another team's change process |

### 4.3 Where integration code lives (modular monolith)

- Provider adapters live in the **domain app that owns the use case** (`apps/billing/integrations/acmepay/`),
  not a giant `apps/integrations/providers/` grab bag; the shared `apps/integrations/` app holds only
  cross-cutting plumbing (client factory, errors, resilience, inbox/outbox models).
- If two domain apps need the same provider for different purposes, give each its own port; share the
  low-level client only.
- When a boundary inside the monolith may become a network boundary later, treat it already as one: public
  interface functions, DTOs, no cross-app ORM joins — see
  [modular-monolith.md](../../django-architecture/references/modular-monolith.md).

### 4.4 Integration decision record

For every non-trivial integration, write a short ADR
([format](../../django-architecture/references/architecture-decision-records.md)) answering:

```text
System / owner / SoR for each fact exchanged
Communication style (sync/async, push/pull) and why
Contract source and version; change notification channel
Auth mechanism and credential owner/rotation
Timeouts, retry policy, idempotency strategy, unknown-outcome handling
Rate limits and expected volume (peak / average)
Failure behaviour: fallback, user impact, degradation
Observability: metrics, alerts, runbook link
Data classification (PII, financial) and retention
Exit plan: how to swap or retire the provider
```

### 4.5 Evaluate every recommendation against

Security · reliability · data consistency · observability · operational complexity · performance · cost. A
design that improves one dimension by sacrificing another must say so explicitly.

## 5. Django implementation patterns

### 5.1 Layout

```text
apps/
  integrations/              # shared plumbing only
    http.py                  # build_client(...)
    errors.py                # IntegrationError taxonomy
    resilience.py            # retry_call, CircuitBreaker, RateLimiter
    models.py                # WebhookEvent, InboxMessage, OutboxMessage, SyncCheckpoint, ExternalReference
  billing/
    ports.py                 # PaymentGateway Protocol (domain-facing)
    services.py              # use cases; depend on the port only
    integrations/acmepay/
      client.py              # transport + auth
      schemas.py             # wire contract
      gateway.py             # implements PaymentGateway, maps to domain types/errors
```

### 5.2 Port and adapter wiring

```python
# apps/billing/ports.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChargeResult:
    external_id: str
    status: str  # domain vocabulary: "authorised" | "declined" | "pending"


class PaymentGateway(Protocol):
    def authorise(self, *, amount: Decimal, currency: str, reference: str,
                  idempotency_key: str) -> ChargeResult: ...
```

```python
# apps/billing/gateways.py — resolve the implementation from settings (one seam for tests)
from functools import cache

from django.conf import settings
from django.utils.module_loading import import_string

from .ports import PaymentGateway


@cache
def payment_gateway() -> PaymentGateway:
    return import_string(settings.BILLING_PAYMENT_GATEWAY)()
```

Domain services call `payment_gateway().authorise(...)`; tests override `BILLING_PAYMENT_GATEWAY` with a fake
(call `payment_gateway.cache_clear()` in the fixture). See
[dependency-management.md](../../django-architecture/references/dependency-management.md).

### 5.3 Boundary rule for transactions

```python
from django.db import transaction


def place_order(*, cart_id: int) -> Order:
    with transaction.atomic():
        order = Order.objects.create_from_cart(cart_id)          # local invariants only
        transaction.on_commit(lambda: request_payment.delay(order.pk))
    return order
```

No HTTP call, SDK call or broker publish happens inside `atomic()`: it would hold row locks and a DB connection
for the duration of a remote call, and a rollback cannot undo the remote effect.

### 5.4 Store external identity explicitly

```python
class ExternalReference(models.Model):
    system = models.CharField(max_length=32)          # "acmepay", "partner_crm"
    external_id = models.CharField(max_length=128)
    content_type = models.ForeignKey("contenttypes.ContentType", on_delete=models.CASCADE)
    object_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["system", "external_id"], name="extref_system_extid_uniq"),
            models.UniqueConstraint(fields=["system", "content_type", "object_id"],
                                    name="extref_system_object_uniq"),
        ]
```

For hot paths prefer a typed column on the domain model (`Payment.acmepay_id` with a unique constraint); the
generic table suits many low-volume systems.

## 6. Production use cases

| Scenario | Style chosen | Why |
|---|---|---|
| Checkout card authorisation | Sync HTTP, 5–10 s total budget, idempotency key | User waits for the answer; provider supports keys |
| Order confirmation email | Async task after commit | User does not wait; retries are cheap |
| Payment status changes | Webhook inbox + periodic reconciliation poll | Push for latency, pull for completeness |
| CRM contact sync | Incremental pull with checkpoint, nightly full compare | CRM is SoR for contacts; webhooks unreliable |
| ERP invoice export | Outbox → SOAP adapter task | ERP slow/offline windows; must not lose invoices |
| Publish `order.placed` to analytics and warehouse | Outbox → broker topic | Multiple consumers, producer must not know them |
| Identity | OIDC with Entra ID | Standard protocol; Django does not own identities |

Production failure examples worth designing for: provider returns `200` with `{"status": "ERROR"}`; a webhook
arrives before the API call that created the object returns; a CSV drop arrives twice with different file
names; a provider silently adds an enum value; a certificate expires on a Saturday.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Synchronous request-response | Simple, immediate consistency for the caller, easy to debug | Temporal coupling, latency inheritance, cascading failures |
| Asynchronous messaging/tasks | Absorbs bursts and outages, separate failure domains | Eventual consistency, duplicate/ordering handling, more infrastructure |
| Point-to-point | Minimal infrastructure, tailored contracts | Contract sprawl at scale |
| Hub/broker | Central routing, many consumers | Shared dependency, governance overhead, hidden logic |
| Anti-corruption layer | Domain insulated from provider churn | More code, mapping maintenance |

## 8. Security and performance considerations

- Each integration is a **trust boundary**: add it to the threat model
  ([threat-modeling.md](../../django-security-engineering/references/threat-modeling.md)); classify the data that crosses it.
- Least privilege per integration: separate credentials per provider and per environment; scoped tokens.
- Inbound channels (webhooks, consumers, file drops) are untrusted input — verify origin, validate schema,
  bound size. Details in [integration-security.md](integration-security.md).
- Performance: synchronous integrations consume a web worker for the full remote latency; capacity is roughly
  `workers / remote_latency`. Size pools and decide async using
  [concurrency-and-worker-management.md](../../django-performance/references/concurrency-and-worker-management.md).
- Cost: per-call pricing, egress, broker throughput units and support plans belong in the decision record.

## 9. Common mistakes and anti-patterns

- **Provider types in the domain** (`stripe.PaymentIntent` passed into services or stored in JSON fields as the
  source of truth).
- **Two systems of record** for the same fact, each "fixing" the other in a sync loop.
- **Remote calls inside `transaction.atomic()`** or inside `save()`/signals.
- **Treating timeout as failure** and retrying a non-idempotent operation.
- **Treating HTTP 2xx as business success** without reading the body or the resulting state.
- **Async by default** — adding Celery and a broker for a call that takes 80 ms and has no burst problem.
- **Distributed transactions** (XA/2PC) across Django and SaaS APIs — unsupported by the providers and fragile.
- **No exit plan** — provider-specific logic spread across views, serializers, admin and templates.
- **Shared credentials** across environments or applications; production keys in staging.
- **Undocumented contract** — behaviour learned from production incidents.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Web latency spikes track a provider's latency | Sync call in request path, no timeout budget | Trace spans per provider; timeout config |
| Duplicated external effects | Retry without idempotency / timeout treated as failure | Provider request logs vs your idempotency keys |
| Local record exists, remote does not (or vice versa) | Call outside a consistent pattern; rollback after remote success | Outbox/`on_commit` usage; reconciliation report |
| Data flip-flops between systems | Unclear SoR, bidirectional sync without conflict policy | Ownership table in the ADR |
| Every incident needs a developer | No runbook, no replay tooling | Ops checklist below |

Design review checklist:

- [ ] SoR identified for every exchanged fact; external ids stored with unique constraints.
- [ ] Communication style justified; Celery/broker only where durability or decoupling is required.
- [ ] Contract captured (schema, semantics, errors, limits, versioning).
- [ ] Every remote effect is idempotent, or has an unknown-outcome path and reconciliation.
- [ ] No remote calls inside DB transactions.
- [ ] Failure behaviour and user impact defined; fallback or degradation chosen.
- [ ] Metrics, alerts and a runbook exist before go-live.
- [ ] Credentials scoped per environment with a rotation owner.

## 11. Related references and skills

- Patterns that implement these decisions: [integration-architecture-patterns.md](integration-architecture-patterns.md)
- Timeouts, retries, idempotency: [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)
- HTTP clients: [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md)
- Messaging: [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md)
- Sync and reconciliation: [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md)
- Architectural style selection and distributed-systems theory:
  [architectural-styles.md](../../django-architecture/references/architectural-styles.md),
  [distributed-systems.md](../../django-architecture/references/distributed-systems.md),
  [event-driven-architecture.md](../../django-architecture/references/event-driven-architecture.md)
- App boundaries: [django-application-boundaries.md](../../django-architecture/references/django-application-boundaries.md)
- Transactions in depth: [django-orm](../../django-orm/SKILL.md)

## 12. Official documentation and external references

- Django transactions and `on_commit`: <https://docs.djangoproject.com/en/5.2/topics/db/transactions/>
- RFC 9110 HTTP Semantics (method safety and idempotency): <https://www.rfc-editor.org/rfc/rfc9110>
- Enterprise Integration Patterns (Hohpe & Woolf): <https://www.enterpriseintegrationpatterns.com/>
- Microsoft Azure Architecture Center — cloud design patterns: <https://learn.microsoft.com/en-us/azure/architecture/patterns/>
- AWS Well-Architected Framework, Reliability pillar: <https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html>
- Martin Fowler, "Integration Database" / "Bounded Context": <https://martinfowler.com/bliki/BoundedContext.html>
- Python `typing.Protocol`: <https://docs.python.org/3/library/typing.html#typing.Protocol>
