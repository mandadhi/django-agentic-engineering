# Service layer: services and selectors

> Scope: where business logic goes, application vs domain services, selectors, transaction
> boundaries, composition, signatures, domain exceptions, DTOs, anaemic vs bloated services,
> testing. Not covered here: layer theory ([layered-architecture.md](./layered-architecture.md)),
> aggregates ([domain-driven-design.md](./domain-driven-design.md)), injecting gateways
> ([dependency-management.md](./dependency-management.md)), event dispatch
> ([domain-events.md](./domain-events.md)), query optimisation ([django-orm](../../django-orm/SKILL.md)).

## Purpose

A service layer gives each **use case** one named, testable function that every entry point
(view, API, admin, command, task) calls. It owns the transaction and side effects. It is added
**when it earns its keep** — not as a mandatory wrapper around every `objects.create()`.

## Technical concepts

**Where does this code go?**

| Code | Home |
|---|---|
| Reusable filter (`active`, `for_user`, `due_before`) | `QuerySet` method, exposed via `as_manager()` / `Manager.from_queryset` |
| Rule about one row's own fields | DB constraint, then `Model.clean()` / validators |
| Derived value of one row | model property / `GeneratedField` (Django ≥ 5.0) / annotation |
| State query on one instance (`is_overdue`) | model method |
| Read use case (list for a screen, a report) | selector |
| Write touching > 1 model, needing a transaction or side effects | service |
| Parsing/validating request input shape | serializer or form |
| HTTP, CLI or task concerns | view / command / task — then call the service |

If it fits a QuerySet or model method, it doesn't need a service.

| Kind | Example | Lives in |
|---|---|---|
| Application service | `place_order`, `cancel_order` — orchestrate a use case, own the transaction | `services.py` |
| Domain service | `price_order_line` — pure business calculation across entities, no transaction, no I/O | `pricing.py` / `domain.py` |
| Selector | `order_list`, `order_detail` — scoped, optimised reads | `selectors.py` |

## Internal mechanics

**Transaction boundary.** The service that *owns the use case* opens `transaction.atomic()`.
Services it calls join that transaction (nested `atomic` becomes a savepoint). Side effects that
leave the database run in `transaction.on_commit`, so they never fire for rolled-back data.

```mermaid
sequenceDiagram
    participant V as View/Task
    participant S as orders.place_order
    participant I as inventory.reserve_stock
    participant DB
    V->>S: place_order(customer, lines)
    S->>DB: BEGIN; INSERT order
    S->>I: reserve_stock(product_id, qty)
    I->>DB: SELECT ... FOR UPDATE; UPDATE stock
    S->>DB: INSERT lines; COMMIT
    DB-->>S: committed
    S-->>V: Order
    Note over S,DB: on_commit → send_order_confirmation.delay(order.pk)
```

**Locking.** `select_for_update()` inside `atomic()` locks the rows read, preventing lost updates
between check and write. Lock the aggregate root; lock in a consistent order (by pk) when locking
several rows to avoid deadlocks. Behaviour depends on the backend — PostgreSQL supports it fully;
SQLite ignores it ([django-orm](../../django-orm/SKILL.md)).

**Composition.** Services call other apps' public services, not their models' internals. Private
helpers start with `_`. Keep orchestration flat: one service calling three others is fine; five
levels of service calling service is a sign the use case is mis-cut.

## Decision guidance

Add a service when **at least one** holds:

- the write spans multiple models or apps;
- it needs a transaction or row lock;
- it has side effects (email, task, HTTP, event);
- more than one entry point performs it.

Add a selector when the read encodes **access scope** or a **loading strategy** reused by more
than one caller, or is complex enough to deserve its own test. A detail view doing
`get_object_or_404(Order.objects.for_user(user), pk=pk)` needs no selector.

Functions vs classes: prefer **module-level functions** with keyword-only args. Use a class
only when it holds real collaborators (e.g., an injected gateway) or configuration — never a
bag of `@staticmethod`s.

## Django implementation examples

**Selectors** return QuerySets (lazy, composable) or simple data objects:

```python
# apps/orders/querysets.py
class OrderQuerySet(models.QuerySet):
    def for_user(self, user):
        return self if user.is_staff else self.filter(customer__user=user)

    def open(self):
        return self.exclude(status__in=[Order.Status.CANCELLED, Order.Status.FULFILLED])

# apps/orders/selectors.py
def order_list(*, user, search: str | None = None) -> QuerySet[Order]:
    qs = (Order.objects.for_user(user).open()
          .select_related("customer")
          .prefetch_related("lines__product")
          .order_by("-created_at", "-pk"))
    if search:
        qs = qs.filter(reference__icontains=search)
    return qs
```

**Write services:**

```python
# apps/orders/services.py
@transaction.atomic
def place_order(*, customer: Customer, lines: list[LineInput]) -> Order:
    order = Order.objects.create(customer=customer, status=Order.Status.PENDING)
    for line in sorted(lines, key=lambda l: l.product_id):        # consistent lock order
        reserve_stock(product_id=line.product_id, quantity=line.quantity)  # inventory public API
        OrderLine.objects.create(order=order, product_id=line.product_id, quantity=line.quantity)
    transaction.on_commit(lambda: send_order_confirmation.delay(order.pk))
    return order

@transaction.atomic
def cancel_order(*, order_id: int, actor: User, reason: str) -> Order:
    order = Order.objects.select_for_update().get(pk=order_id)   # lock the root
    if order.status == Order.Status.FULFILLED:
        raise OrderNotCancellable(order_id)
    order.status = Order.Status.CANCELLED
    order.cancel_reason = reason
    order.save(update_fields=["status", "cancel_reason", "updated_at"])
    for line in order.lines.all():
        release_stock(product_id=line.product_id, quantity=line.quantity)
    AuditEntry.objects.create(actor=actor, action="order.cancel", object_id=order.pk)
    transaction.on_commit(lambda: notify_cancellation.delay(order.pk))
    return order
```

Note: when `update_fields` is used, `auto_now` fields are only written if listed — hence
`"updated_at"`.

**Signature rules**

- Keyword-only arguments; accept ids or model instances — never `request`, serializers or forms.
- One public function per use case.
- Return the changed object, not a dict, unless the caller needs a DTO.
- External calls (HTTP, email, tasks) after commit via `on_commit`, or inside a task.

**Domain exceptions** — one base per app; entry points map them:

```python
# apps/orders/exceptions.py
class OrderError(Exception):
    code = "order_error"

class OutOfStock(OrderError):
    code = "out_of_stock"

class OrderNotCancellable(OrderError):
    code = "order_not_cancellable"
```

A single DRF exception handler can map `OrderError` subclasses to `409`/`422` with their `code`
([django-rest-framework](../../django-rest-framework/SKILL.md)); HTML views map them to form
errors or messages. Services never raise DRF/HTTP exceptions.

**DTOs** — frozen dataclasses or `NamedTuple` for non-model inputs/outputs (a parsed CSV row, a
report line). Don't add Pydantic just for this when serializers/forms already validate the
boundary.

```python
@dataclass(frozen=True, slots=True)
class LineInput:
    product_id: int
    quantity: int
```

**Thin entry point:**

```python
# apps/orders/api/views.py
class PlaceOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = PlaceOrderInput(data=request.data)
        data.is_valid(raise_exception=True)
        order = place_order(customer=request.user.customer,
                            lines=[LineInput(**l) for l in data.validated_data["lines"]])
        return Response(OrderOutput(order).data, status=status.HTTP_201_CREATED)
```

## Production use cases

- The same `cancel_order` used by the customer UI, the support admin action and a nightly
  `expire_unpaid_orders` command — one audit trail, one rule.
- Celery task `def retry_payment(invoice_id): billing.services.collect_payment(invoice_id=...)`
  — the task stays a three-line adapter.
- Selectors as the single place to fix an N+1 found in production.

## Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| One implementation per use case across all entry points | Extra module per app |
| Explicit transaction and side-effect boundary | Trivial pass-through services add noise |
| Fast tests without HTTP | Risk of "god service" files |
| Clear review surface for business rules | Teams may drift into anaemic models |

**Models stay lean but not anaemic.** Fine on a model: fields, `Meta.constraints`, `clean()`,
`__str__`, `get_absolute_url`, simple properties and state queries (`is_overdue`,
`ensure_editable`). Not on a model: sending email, calling APIs, touching other aggregates,
anything needing `request`. **Not bloated services either**: a 2,000-line `services.py` signals
the app is too big or use cases are mis-cut — split by use case (`services/placement.py`,
`services/cancellation.py`) or split the app.

## Security and performance considerations

- Authorise at the entry point **and** scope in selectors (`for_user`) so new callers inherit
  protection — [django-security-engineering](../../django-security-engineering/SKILL.md).
- Services trust their arguments' *types*, not their *authorisation*; when a service is callable
  from several contexts, pass `actor` and check object-level permission inside.
- Selectors own `select_related`/`prefetch_related` so each read is optimised once —
  [django-performance](../../django-performance/SKILL.md).
- Keep transactions short: no HTTP calls or file processing inside `atomic()`.

## Common mistakes

- A service per CRUD operation that only wraps `objects.create()`.
- `@staticmethod`-only service classes; services taking `request`.
- Business rules in serializer `create()`/`save()`, `Model.save()` overrides or signal handlers.
- Calling `.delay()` inside the transaction — the worker may run before commit and not find the row.
- Selectors returning lists (evaluated) when callers need to paginate/filter further.
- Catching broad `Exception` in services and returning `None`.

## Troubleshooting and evaluation

- **Task can't find the object** → enqueued before commit; move to `on_commit`.
- **Double-processing / lost update** → missing `select_for_update` on read-then-write.
- **Deadlocks** → inconsistent lock order across services; sort by pk.
- **Behaviour differs between API and admin** → logic lives in a serializer or form; move to service.

**Testing**

- Services: test each outcome and each domain exception, asserting DB state and that
  `on_commit` callbacks were scheduled (`TestCase.captureOnCommitCallbacks(execute=True)`
  or pytest-django's `django_capture_on_commit_callbacks`).
- Selectors: assert scoping (user A never sees user B's rows) and query counts
  (`assertNumQueries` / `django_assert_num_queries`).
- Views: one thin test per outcome. See [django-testing](../../django-testing/SKILL.md).

## Related knowledge

[layered-architecture.md](./layered-architecture.md) ·
[domain-driven-design.md](./domain-driven-design.md) ·
[dependency-management.md](./dependency-management.md) ·
[domain-events.md](./domain-events.md) ·
[signals-management-commands.md](../../django-core/references/signals-management-commands.md) ·
[django-orm](../../django-orm/SKILL.md)

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/db/transactions/
- https://docs.djangoproject.com/en/stable/topics/db/transactions/#performing-actions-after-commit
- https://docs.djangoproject.com/en/stable/ref/models/querysets/#select-for-update
- https://docs.djangoproject.com/en/stable/topics/db/managers/
- https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TestCase.captureOnCommitCallbacks
- https://martinfowler.com/eaaCatalog/serviceLayer.html
