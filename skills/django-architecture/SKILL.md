---
name: django-architecture
description: >-
  Django architecture specialist — analysing requirements, choosing architectural styles,
  modular-monolith and app boundaries, DDD applied pragmatically, layering, service layer and
  transaction boundaries, dependency management, design patterns, domain events, event-driven
  and distributed concerns, scalability, ADRs and architecture anti-patterns. Load when designing
  a feature or system, deciding where code belongs, reviewing coupling, planning a refactor, or
  evaluating a proposal to split, scale or add abstraction.
project: django-agentic-engineering
abstract: >-
  Modular monolith by default; domain-shaped apps with explicit public interfaces; thin entry
  points; services and selectors only where they earn their keep; every abstraction justified by
  a real second use. Fifteen focused references loaded on demand.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering]
---

# Django Architecture Specialist

Architectural judgement for Django systems: analyse requirements, pick the simplest structure
that meets them, make trade-offs explicit, and improve existing codebases incrementally.
Framework mechanics live in [django-core](../django-core/SKILL.md); this skill decides *where
things go and why*.

## Activate when

- Designing a new feature, app, bounded context or integration.
- Deciding whether logic belongs in a model, QuerySet, service, selector, form or view.
- Reviewing cross-app imports, circular dependencies, fat views/models or a "god app".
- Someone proposes microservices, an event bus, repositories, a DI container or a rewrite.
- Planning for scale, async processing, cross-service consistency, or recording a decision.

## Architectural workflow

1. **Frame the problem** — functional need, quality attributes (latency, throughput,
   consistency, auditability), team size, deadlines, the Django/DRF versions in use.
2. **Map the domain** — which existing apps own the data involved; which boundary does the
   change cross?
3. **Inspect only what's affected** — the owning apps' `models.py`, `services.py`/`selectors.py`,
   `api.py` and their imports. Don't sweep the repo.
4. **Generate 2–3 options**, including "do the simplest thing in the existing structure".
5. **Evaluate trade-offs** against the quality attributes, cost of change and reversibility.
6. **Decide and record** — an ADR for anything hard to reverse or cross-cutting.
7. **Implement incrementally** — boundaries first, then behaviour; keep the system shippable.
8. **Validate** with the checklist below and explain the decision to the reader.

## Decision guidelines

- **Default to a modular monolith**: one deployable, one database, apps with public interfaces.
  Extract a service only for a *measured* need — independent scaling, separate team cadence,
  compliance isolation — and only once the in-process boundary is already clean.
- **Apps follow the domain**, not technical layers. An app owns its models, migrations and
  invariants; others use its public interface.
- **Dependencies point one way** (platform → domain → feature → interface). Cycles mean a
  concept is in the wrong place.
- **Thin entry points.** Views, serializers, forms, commands and tasks translate I/O and delegate.
- **Add layers when they pay**: a service when logic spans models, needs a transaction, causes
  side effects or has several callers; a selector when reads need scoping and loading strategy.
- **The ORM is the data-access layer.** Repositories, Protocols and injected dependencies are for
  real seams — external gateways, multiple implementations, hard-to-test I/O.
- **Prefer explicit calls over implicit reactions**; publish events only when a producer must not
  know its consumers, and dispatch them after commit.
- **Consistency before distribution**: keep invariants inside one transaction; accept eventual
  consistency only deliberately, with idempotency and an outbox.
- **Measure before scaling**; scale the cheapest layer first.

## Reference navigation

Load only what the task needs; each reference stands alone.

| Task involves… | Load |
|---|---|
| Principles, coupling/cohesion, SOLID/DRY/KISS/YAGNI in Django terms | [architecture-fundamentals.md](references/architecture-fundamentals.md) |
| Choosing a style: monolith, layered, hexagonal, clean, microservices, EDA | [architectural-styles.md](references/architectural-styles.md) |
| Module boundaries, dependency direction, data ownership in one deployable | [modular-monolith.md](references/modular-monolith.md) |
| App sizing, public interfaces, cross-app refs, circular imports | [django-application-boundaries.md](references/django-application-boundaries.md) |
| Bounded contexts, entities, value objects, aggregates | [domain-driven-design.md](references/domain-driven-design.md) |
| Presentation/application/domain/infrastructure mapping | [layered-architecture.md](references/layered-architecture.md) |
| Where business logic goes; services, selectors, transactions, DTOs | [service-layer.md](references/service-layer.md) |
| Dependency inversion, gateways, settings-driven implementations | [dependency-management.md](references/dependency-management.md) |
| Factory, Strategy, Adapter, Observer, Command, Repository, UoW… | [design-patterns.md](references/design-patterns.md) |
| In-process domain events, `on_commit`, events vs signals | [domain-events.md](references/domain-events.md) |
| Brokers, delivery guarantees, outbox, retries, DLQ, ordering | [event-driven-architecture.md](references/event-driven-architecture.md) |
| Calls between services, timeouts, idempotency, sagas | [distributed-systems.md](references/distributed-systems.md) |
| Stateless design, DB scaling, replicas, background work | [scalability-patterns.md](references/scalability-patterns.md) |
| Writing or reviewing an ADR | [architecture-decision-records.md](references/architecture-decision-records.md) |
| Diagnosing and remediating structural problems | [architecture-anti-patterns.md](references/architecture-anti-patterns.md) |

Typical pairings: new feature → `django-application-boundaries` + `service-layer`; "should we
split this out?" → `architectural-styles` + `modular-monolith` + `architecture-decision-records`;
cross-app side effects → `domain-events` (+ `event-driven-architecture` if they leave the process);
refactoring a messy codebase → `architecture-anti-patterns`.

## Handoffs

| Topic | Skill |
|---|---|
| Settings, middleware, URLs, signals and command mechanics | [django-core](../django-core/SKILL.md) |
| Models, QuerySets, constraints, migrations, transactions in depth | [django-orm](../django-orm/SKILL.md) |
| API design, serializers, exception handlers | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Authorisation design, threat modelling | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test strategy for services, fakes, boundaries | [django-testing](../django-testing/SKILL.md) |
| Profiling, query optimisation, load testing | [django-performance](../django-performance/SKILL.md) |
| Celery, brokers, third-party APIs | [django-integrations](../django-integrations/SKILL.md) |
| Database scaling, replication, partitioning | [postgresql-engineering](../postgresql-engineering/SKILL.md) |

## Validation checklist

- [ ] New code lives in the app that owns the data it changes; no new upward imports or cycles.
- [ ] Cross-app writes go through the owning app's public interface.
- [ ] Entry points only translate and delegate; services don't take `request` or return responses.
- [ ] Transaction boundary is explicit; side effects run after commit and are idempotent.
- [ ] Each new abstraction has a named, current reason (second implementation, test seam, I/O).
- [ ] Authorisation is enforced in scoped queries, not only at the view.
- [ ] Hard-to-reverse decisions have an ADR with alternatives and consequences.
- [ ] The design can be tested without HTTP and without external services.
