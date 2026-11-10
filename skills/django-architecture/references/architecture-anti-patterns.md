# Architecture anti-patterns

> Scope: recognising and remediating fat views, fat models, god objects, circular dependencies, unnecessary abstractions, premature microservices, distributed monoliths, overengineering, shared mutable state and excessive coupling in Django codebases. Not covered here: the target designs themselves, which are linked per anti-pattern; query-level problems ([django-orm](../../django-orm/SKILL.md), [django-performance](../../django-performance/SKILL.md)).

## Purpose

A review aid: each entry gives the **Django symptom** you can spot in code, why it hurts, a short
bad example, and remediation steps that can be applied incrementally. Remediate when the
anti-pattern causes measurable pain (bugs, change friction, slow tests, incidents), not on
sight. Refactors without that pain are themselves overengineering.

## Technical concepts

Most anti-patterns are failures of one of three properties:
- **Cohesion**: things that change together live together (fat models, god objects violate it).
- **Coupling direction**: dependencies point from volatile to stable code (cycles and excessive coupling violate it).
- **Proportionality**: complexity matches the problem (overengineering and premature microservices violate it).

See [architecture-fundamentals.md](./architecture-fundamentals.md) for the principles.

## Internal mechanics

Why these hurt specifically in Django:
- Views, serializers, forms, admin and management commands are all **entry points**. Logic placed in one is unavailable to the others, so it gets copied.
- `Model.save()` and signals run for every write path, including admin, fixtures and shell, but *not* for `bulk_create`/`update()`. Logic there is both too broad and leaky.
- App import order is resolved by the app registry; cross-app imports at module level create import-time cycles that surface as `AppRegistryNotReady` or `ImportError` only in some entry points.
- Module-level state lives per worker process (and per thread under some servers), so it diverges across gunicorn workers.

## Decision guidance

| Anti-pattern | Severity signal | First remediation |
|---|---|---|
| Fat views | Same rule copied in view + serializer + command | Extract a service |
| Fat models | `save()` sends email / calls APIs | Move side effects to service + `on_commit` |
| God object | App/class edited by every feature, >15 models | Split by subdomain |
| Circular deps | Function-local imports to "fix" import errors | Fix the dependency direction |
| Unnecessary abstraction | Interface with one implementation, no fake | Inline it |
| Premature microservices | Services share a DB or deploy together | Re-merge into modules |
| Distributed monolith | Synchronous call chains across services | Async events / merge |
| Overengineering | More layers than use cases | Delete layers without a second use |
| Shared mutable state | Module-level dicts mutated at runtime | Move state to DB/cache/request |
| Excessive coupling | Apps reach into each other's models to write | Public interfaces per app |

## Django implementation examples

### Fat views
**Symptom:** a view with 100+ lines: validation, pricing, stock checks, emails, all inline.

```python
def checkout(request):
    cart = request.session["cart"]
    for item in cart:
        product = Product.objects.get(pk=item["id"])
        if product.stock < item["qty"]:
            return JsonResponse({"error": "out of stock"}, status=409)
        product.stock -= item["qty"]; product.save()
    order = Order.objects.create(user=request.user, ...)
    send_mail("Order placed", ..., [request.user.email])      # before commit, inside request
    return JsonResponse({"id": order.pk})
```

**Hurts:** untestable without HTTP; race on stock; email sent even if later steps fail; logic unavailable to API/commands.
**Remediate:** extract `place_order` into `apps/orders/services.py` (transaction, `select_for_update` via `inventory.reserve_stock`, `OutOfStock`), dispatch email after commit, leave the view to parse → call → map errors. See [service-layer.md](./service-layer.md).

### Fat models
**Symptom:** `save()`/`delete()` overrides with side effects; models importing other apps' services; 1,000-line `models.py`.

```python
class Order(models.Model):
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        PaymentGateway().charge(self.total)       # runs in admin edits, fixtures, migrations…
        Invoice.objects.create(order=self)
```

**Remediate:** keep on the model only its own invariants, constraints, derived properties and state queries (`is_overdue`). Move multi-model workflows to services and external calls behind adapters. Note `save()` side effects are skipped by `bulk_create`/`update()`, which is another reason not to rely on them. See [domain-driven-design.md](./domain-driven-design.md) for where behaviour belongs.

### God objects
**Symptom:** a `core` app with 40 models; a `utils.py` imported everywhere; an `OrderManager` class with 60 methods.
**Remediate:** map responsibilities to subdomains, split the app along them (move models with `SeparateDatabaseAndState` migrations or `db_table` to avoid data moves), and leave a thin compatibility re-export during migration. See [django-application-boundaries.md](./django-application-boundaries.md).

### Circular dependencies
**Symptom:** `orders` imports `billing` and `billing` imports `orders`; function-local imports; `ImportError: cannot import name ... (most likely due to a circular import)`.
**Remediate (in order):** string model references (`"orders.Order"`); `TYPE_CHECKING`-only imports for hints; move the shared concept to a lower tier; replace the upward call with a domain event ([domain-events.md](./domain-events.md)). Enforce with an import-linter contract if the team adopts it. Detail: [django-application-boundaries.md](./django-application-boundaries.md).

### Unnecessary abstractions
**Symptom:** `IOrderRepository` + `DjangoOrderRepository` + `OrderService` + `OrderServiceFactory`, each with one implementation and no test fake.

```python
class OrderService:
    def __init__(self, repo: IOrderRepository): self.repo = repo
    def get(self, pk): return self.repo.get(pk)           # pure pass-through
```

**Remediate:** inline pass-throughs; use QuerySet methods for reads; keep an abstraction only where a second implementation exists (e.g. `PaymentGateway` with `FakeGateway`). See [design-patterns.md](./design-patterns.md) and [dependency-management.md](./dependency-management.md).

### Premature microservices
**Symptom:** a small team runs five Django services that deploy together, share one Postgres, and call each other synchronously for every request.
**Hurts:** network failure modes, distributed transactions, versioned APIs and five pipelines, with none of the benefits.
**Remediate:** merge back into a modular monolith with explicit app boundaries; extract later only for a measured scaling, ownership or compliance need. See [modular-monolith.md](./modular-monolith.md) and [architectural-styles.md](./architectural-styles.md).

### Distributed monolith
**Symptom:** services cannot deploy independently; a change requires coordinated releases; order creation synchronously calls inventory → pricing → billing, and any one outage fails checkout; services read each other's tables.
**Remediate:** give each service sole ownership of its data; replace synchronous chains with events/outbox where eventual consistency is acceptable; add timeouts and circuit breaking where calls remain; or merge services whose boundaries are wrong. See [distributed-systems.md](./distributed-systems.md) and [event-driven-architecture.md](./event-driven-architecture.md).

### Overengineering
**Symptom:** hexagonal ports/adapters, CQRS and event sourcing for a CRUD admin tool; a DI container; generic "engine" classes configured by YAML.
**Remediate:** match style to complexity: Django's defaults (models, forms, generic views) for CRUD; services where workflows appear; heavier patterns only around the complex core. Record the reason when you do adopt one ([architecture-decision-records.md](./architecture-decision-records.md)).

### Shared mutable state
**Symptom:** module-level caches/dicts mutated per request; class attributes on views used as per-request storage; `threading.local` in async code.

```python
_current_tenant = None                       # bad: shared across threads/requests in a worker
def set_tenant(t):
    global _current_tenant; _current_tenant = t
```

**Hurts:** cross-request data leaks (a security issue), worker-dependent behaviour, flaky tests.
**Remediate:** per-request data on `request` or in a `contextvars.ContextVar` set by middleware ([django-core middleware](../../django-core/references/middleware.md)); shared state in DB or cache ([django-core caching](../../django-core/references/caching.md)); immutable module-level constants only.

### Excessive coupling
**Symptom:** `billing` updates `Order.status` directly; templates traverse `order.customer.user.profile.company.plan`; serializers from one app imported by another.
**Remediate:** writes go through the owning app's service; expose curated `api.py`; pass ids/DTOs across boundaries; selectors own traversal and loading. See [modular-monolith.md](./modular-monolith.md).

## Production use cases

- A pre-scale audit: list entry points per use case and check every one calls the same service.
- Incident follow-up: a stale-data or duplicate-charge bug traced to `save()` side effects or signals → remediation plan above.
- Before splitting a service: confirm none of the "premature microservices" symptoms apply.

## Advantages and disadvantages

Remediation itself has costs: churn, migration risk, review load. Prefer **strangler-style**
refactors: introduce the service, route one entry point through it, then the rest, then delete
the old code, over big-bang rewrites.

## Security and performance considerations

- Shared mutable state and fat views are frequent sources of authorisation bugs (tenant leaks, unscoped queries).
- Fat models often hide N+1 queries in properties used in templates.
- Distributed monoliths multiply latency (sum of hops) and reduce availability (product of uptimes).
- Unnecessary repository layers that return lists defeat QuerySet laziness and pagination.

## Common mistakes

- Treating every large file as an anti-pattern; size alone is not the problem, mixed responsibilities are.
- "Fixing" fat views by moving the same code into `save()` or signals.
- Fixing cycles with function-local imports and calling it done.
- Rewriting into microservices to escape a messy monolith; the mess moves with you, plus a network.

## Troubleshooting and evaluation

Quick review heuristics:
- `grep -rn "def save" apps/` → each override does only own-row work?
- `grep -rn "post_save\|pre_save" apps/` → each receiver a framework/decoupling need, not core flow?
- Import graph (`import-linter`, `pydeps`) → no cycles, no upward edges?
- Count implementations per Protocol/ABC → ≥2 (including a fake), or inline it.
- Can each service be tested without `RequestFactory`? If not, HTTP leaked into it.

## Related knowledge

[architecture-fundamentals.md](./architecture-fundamentals.md) · [service-layer.md](./service-layer.md) ·
[django-application-boundaries.md](./django-application-boundaries.md) · [modular-monolith.md](./modular-monolith.md) ·
[design-patterns.md](./design-patterns.md) · [distributed-systems.md](./distributed-systems.md) ·
[django-core project structure](../../django-core/references/project-structure.md)

## Official documentation

- https://docs.djangoproject.com/en/stable/misc/design-philosophies/
- https://docs.djangoproject.com/en/stable/ref/models/instances/#saving-objects
- https://docs.djangoproject.com/en/stable/topics/signals/
- https://martinfowler.com/bliki/MonolithFirst.html
