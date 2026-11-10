# Views and ViewSets

> **Scope.** Owns: `@api_view`, `APIView`, `GenericAPIView`, the model mixins, concrete generic
> views, `ViewSet`/`GenericViewSet`/`ModelViewSet`/`ReadOnlyModelViewSet`, custom ViewSets,
> `@action`, the `get_*`/`perform_*` hooks, per-action configuration, and how to choose between
> them. Does not cover: URL generation for ViewSets ([routers-and-url-design.md](routers-and-url-design.md)),
> the dispatch sequence ([request-response-lifecycle.md](request-response-lifecycle.md)),
> permission class design ([permissions-and-access-control.md](permissions-and-access-control.md)),
> or Django's own CBVs ([django-core urls-views](../../django-core/references/urls-views.md)).

## 1. Purpose and scope

Views translate HTTP into calls on serializers, selectors and services and translate results
back into responses. This reference covers DRF's view hierarchy, which hooks to override, and
how to keep views thin.

## 2. Core concepts

**Hierarchy.**

```
APIView                      # request wrapping, auth, permissions, throttles, negotiation, exceptions
└── GenericAPIView           # queryset, serializer_class, lookup, filtering, pagination helpers
    ├── + mixins → concrete generic views (ListCreateAPIView, ...)
    └── GenericViewSet       # = ViewSetMixin + GenericAPIView; actions instead of get/post
        ├── ReadOnlyModelViewSet   = List + Retrieve mixins
        └── ModelViewSet           = Create + Retrieve + Update + Destroy + List mixins
ViewSet                      # = ViewSetMixin + APIView (no queryset helpers)
```

**Mixins** (`rest_framework.mixins`) provide actions, not HTTP handlers:

| Mixin | Action(s) | Hook | Success status |
|---|---|---|---|
| `ListModelMixin` | `list` | — (uses `filter_queryset`, `paginate_queryset`) | 200 |
| `CreateModelMixin` | `create` | `perform_create(serializer)` | 201 (+ `Location` if `url` field via `get_success_headers`) |
| `RetrieveModelMixin` | `retrieve` | — (uses `get_object`) | 200 |
| `UpdateModelMixin` | `update`, `partial_update` | `perform_update(serializer)` | 200 |
| `DestroyModelMixin` | `destroy` | `perform_destroy(instance)` | 204 |

**Concrete generic views** (`rest_framework.generics`): `CreateAPIView`, `ListAPIView`,
`RetrieveAPIView`, `DestroyAPIView`, `UpdateAPIView`, `ListCreateAPIView`,
`RetrieveUpdateAPIView`, `RetrieveDestroyAPIView`, `RetrieveUpdateDestroyAPIView` — each maps
HTTP methods to the matching mixin actions.

**`GenericAPIView` attributes and hooks.**

| Attribute | Hook | Notes |
|---|---|---|
| `queryset` | `get_queryset()` | Override for per-request scoping; the attribute is re-cloned with `.all()` |
| `serializer_class` | `get_serializer_class()`, `get_serializer()`, `get_serializer_context()` | Per-action serializers |
| `lookup_field` (`"pk"`), `lookup_url_kwarg` | `get_object()` | `get_object()` filters, 404s, then checks object permissions |
| `filter_backends` | `filter_queryset()` | Applied to list **and** `get_object()` |
| `pagination_class` | `paginate_queryset()`, `get_paginated_response()` | |
| `permission_classes` etc. | `get_permissions()`, `get_authenticators()`, `get_throttles()` | Instantiate per request |

## 3. Internal mechanics

- `ViewSet.as_view({"get": "list", "post": "create"})` (called by routers) binds HTTP methods to
  action methods and sets `self.action` (`"list"`, `"create"`, `"retrieve"`, `"update"`,
  `"partial_update"`, `"destroy"`, or a custom action name). For unmapped methods such as
  `OPTIONS`, `self.action` may be `None` or `"metadata"` — **VERIFY** for your release; handle
  both in `get_permissions()`/`get_serializer_class()`.
- `get_object()`: `queryset = self.filter_queryset(self.get_queryset())` →
  `get_object_or_404(queryset, **{lookup_field: kwargs[lookup_url_kwarg]})` →
  `self.check_object_permissions(request, obj)`.
- `ListModelMixin.list`: filter → paginate → serialize `many=True` → paginated or plain response.
- `UpdateModelMixin.update` clears `instance._prefetched_objects_cache` after saving so the
  response reflects updated relations.
- `@action(detail=..., methods=[...], url_path=..., url_name=..., **initkwargs)` marks a method
  as a routable action; extra kwargs such as `permission_classes` or `serializer_class` override
  view attributes for that action only. `@my_action.mapping.delete` adds another HTTP method to
  the same URL.

## 4. Engineering decisions and trade-offs

| Need | Choose |
|---|---|
| Standard CRUD (or a subset) on one resource | `GenericViewSet` + only the mixins you need, with a router |
| Full CRUD with no special cases | `ModelViewSet` |
| Read-only collection + detail | `ReadOnlyModelViewSet` or `ListAPIView` + `RetrieveAPIView` |
| One or two endpoints with explicit URLs | Concrete generic views |
| Non-CRUD operation, aggregated report, custom flow | `APIView` (or `@api_view`) |
| State transition on a resource (`cancel`, `approve`) | `@action(detail=True, methods=["post"])` |
| A collection-level operation (`bulk-archive`, `export`) | `@action(detail=False)` — sparingly |

**When custom actions signal a design problem.** A handful of state-transition actions is
healthy. Many actions on one ViewSet (`add_item`, `remove_item`, `set_address`, `update_note`)
usually means a missing sub-resource (`/orders/{id}/lines/`) or that `PATCH` would suffice.
Actions that do not touch the ViewSet's resource belong to another resource's ViewSet.

**`ModelViewSet` vs explicit mixins.** `ModelViewSet` exposes `PUT`, `PATCH` and `DELETE`
whether you want them or not; compose mixins so the API surface is deliberate.

**`perform_*` hooks vs overriding the action.** Use `perform_create(serializer)` to inject
server-owned fields (`serializer.save(owner=...)`). When create needs a different input and
output serializer or a service with its own return value, override `create()` instead of
contorting `perform_create`.

## 5. Django/DRF implementation patterns

**Composed ViewSet with per-action serializers and permissions:**

```python
class OrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                   mixins.CreateModelMixin, viewsets.GenericViewSet):
    filterset_class = OrderFilter                       # django-filter (third-party)
    ordering_fields = ["created_at", "total"]

    def get_queryset(self):
        return order_list(user=self.request.user)       # scoped + prefetched selector

    def get_serializer_class(self):
        if self.action == "list":
            return OrderOutputSerializer
        if self.action == "create":
            return OrderCreateInputSerializer
        return OrderDetailOutputSerializer

    def get_permissions(self):
        if self.action == "cancel":
            return [IsAuthenticated(), CanCancelOrder()]
        return [IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order = place_order(customer=request.user.customer, **payload.validated_data)
        return Response(OrderDetailOutputSerializer(order, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()                       # scoped 404 + object permissions
        payload = CancelOrderInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order = cancel_order(order=order, actor=request.user, **payload.validated_data)
        return Response(OrderDetailOutputSerializer(order, context=self.get_serializer_context()).data)
```

**`perform_create` for a simple single-model resource:**

```python
class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer

    def get_queryset(self):
        return Product.objects.filter(organization=self.request.user.organization)

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.organization)

    def perform_destroy(self, instance):
        archive_product(product=instance, actor=self.request.user)   # soft delete via service
```

**`APIView` for a non-CRUD endpoint:**

```python
class RevenueReportView(APIView):
    permission_classes = [IsAuthenticated, HasReportingAccess]

    def get(self, request):
        params = RevenueReportQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        report = revenue_report(organization=request.user.organization, **params.validated_data)
        return Response(RevenueReportSerializer(report).data)
```

**`@api_view` for a small command:**

```python
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_verification(request):
    send_verification_email(user=request.user)
    return Response(status=status.HTTP_202_ACCEPTED)
```

**Custom ViewSet base** — only when several ViewSets genuinely share configuration:

```python
class TenantScopedViewSet(viewsets.GenericViewSet):
    def get_queryset(self):
        return super().get_queryset().filter(organization=self.request.user.organization)
```

## 6. Production use cases

- **Admin table**: `ReadOnlyModelViewSet` with filtering, ordering, page-number pagination.
- **Order workflow**: create via service, state changes via `@action`s, no `PUT`/`DELETE`.
- **Reporting**: `APIView` with a query-parameter serializer and a selector.
- **Child collection**: separate `OrderLineViewSet` whose `get_queryset()` filters by the parent
  `order_pk` URL kwarg after scoping the parent to the user.

## 7. Advantages and disadvantages

| Option | Advantages | Disadvantages |
|---|---|---|
| `APIView` | Explicit, no hidden behaviour | Repeats lookup/pagination code |
| Generic views | Little code, explicit URL per view | Many small classes for one resource |
| ViewSets + router | One class per resource, consistent URLs, schema-friendly | `self.action` branching; implicit URL generation |
| `ModelViewSet` | Fastest CRUD | Exposes all methods; encourages logic in `perform_*` |

## 8. Security and performance considerations

- Always override `get_queryset()` to scope by user/tenant; a class-level
  `queryset = Model.objects.all()` with only permission classes is an IDOR risk.
- `get_object()` is the only place DRF calls `check_object_permissions` automatically; objects
  you fetch yourself must be checked or scoped explicitly.
- `@action` inherits the ViewSet's `permission_classes` unless overridden — review each action's
  authorisation.
- `get_permissions()` must cover every action, including `None`/`metadata`, with a safe default.
- Put `select_related`/`prefetch_related`/`annotate` in `get_queryset()` (or the selector),
  matched to the serializer used by the action.

## 9. Common mistakes and anti-patterns

- Overriding `list()`/`retrieve()` to change the queryset instead of `get_queryset()`.
- Evaluating `self.queryset` directly (cached across requests) instead of `get_queryset()`.
- Business workflows in `perform_create` (emails, payments, multi-model writes).
- `ModelViewSet` for resources that must not be deleted or replaced.
- Dozens of `@action`s turning a resource ViewSet into an RPC namespace.
- Calling `Model.objects.get(pk=pk)` inside an action instead of `self.get_object()`.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| `'NoneType' object is not callable` / missing serializer for `OPTIONS` | `get_serializer_class()` returns `None` for an unhandled `self.action` |
| 404 for an existing object | Scoped `get_queryset()` or filter backends exclude it (intended?) |
| Object permission never evaluated | Object fetched without `get_object()` |
| 405 Method Not Allowed | Mixin for that action not included; or action `methods` list |
| `AssertionError: ... should either include a queryset attribute` | No `queryset` and no `get_queryset()` override |

Validate: ViewSet tests per action and per role; schema shows only intended methods
([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [routers-and-url-design.md](routers-and-url-design.md)
- [request-response-lifecycle.md](request-response-lifecycle.md)
- [permissions-and-access-control.md](permissions-and-access-control.md)
- [serializers-and-model-serializers.md](serializers-and-model-serializers.md)
- [django-architecture service-layer](../../django-architecture/references/service-layer.md)
- [django-core urls-views](../../django-core/references/urls-views.md)

## 12. Official documentation and external references

- Views: <https://www.django-rest-framework.org/api-guide/views/>
- Generic views: <https://www.django-rest-framework.org/api-guide/generic-views/>
- ViewSets: <https://www.django-rest-framework.org/api-guide/viewsets/>
- Classy DRF (third-party class browser): <https://www.cdrf.co/>
