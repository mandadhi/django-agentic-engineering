# Routers and URL Design

> **Scope.** Owns: REST resource and URL design, collection/detail/relationship endpoints,
> HTTP method mapping, explicit URL patterns for DRF views, `SimpleRouter`/`DefaultRouter`,
> registration and basenames, generated URL names, custom action URLs, nested resource URLs, and
> avoiding RPC-style endpoint sprawl. Does not cover: ViewSet behaviour
> ([views-and-viewsets.md](views-and-viewsets.md)), URL-based versioning
> ([api-versioning-and-documentation.md](api-versioning-and-documentation.md)), format suffix
> negotiation ([parsers-renderers-and-content-negotiation.md](parsers-renderers-and-content-negotiation.md)),
> or Django URLconf fundamentals ([django-core urls-views](../../django-core/references/urls-views.md)).

## 1. Purpose and scope

URLs are the most visible and least changeable part of an API. This reference covers designing
them around resources and generating them consistently with DRF routers.

## 2. Core concepts

**Resource-oriented URLs.**

| URL | `GET` | `POST` | `PUT`/`PATCH` | `DELETE` |
|---|---|---|---|---|
| `/orders/` (collection) | list | create | — | — |
| `/orders/{id}/` (detail) | retrieve | — | update / partial update | destroy |
| `/orders/{id}/lines/` (sub-collection) | list lines | add line | — | — |
| `/orders/{id}/lines/{line_id}/` | retrieve line | — | update line | remove line |
| `/orders/{id}/cancel/` (state transition) | — | perform | — | — |

Conventions: plural nouns, lowercase, hyphens for multi-word segments (`/purchase-orders/`),
trailing slash consistent with the project (DRF routers default to `trailing_slash=True`), IDs
(or stable slugs) in the path, filters in the query string.

**Routers.**

| Router | Generates |
|---|---|
| `SimpleRouter` | List/detail routes and `@action` routes |
| `DefaultRouter` | Same, plus an API root view and optional format-suffix patterns (`.json`) |

Generated URL names: `{basename}-list`, `{basename}-detail`, and `{basename}-{url_name}` for
actions (`url_name` defaults to the method name with underscores replaced by dashes).

## 3. Internal mechanics

- `router.register(prefix, viewset, basename=None)`. `basename` defaults to
  `queryset.model._meta.model_name`; **it is required** when the ViewSet has no `queryset`
  attribute (only `get_queryset()`).
- The router inspects ViewSet methods to build routes: `list`/`create` → `^{prefix}/$`;
  `retrieve`/`update`/`partial_update`/`destroy` → `^{prefix}/{lookup}/$`; `@action(detail=False)`
  → `^{prefix}/{url_path}/$`; `@action(detail=True)` → `^{prefix}/{lookup}/{url_path}/$`.
- The lookup segment uses `lookup_field`/`lookup_url_kwarg` and, for regex routes,
  `lookup_value_regex` (default `[^/.]+`). `SimpleRouter(use_regex_path=False)` uses Django path
  converters with `lookup_value_converter` (DRF ≥ 3.15 — **VERIFY** exact release).
- Only methods with an implementation are routed; a ViewSet without `destroy` gets no `DELETE`.
- `DefaultRouter` adds format suffix patterns by default; disable with
  `include_format_suffixes=False` if you don't use them.

## 4. Engineering decisions and trade-offs

| Decision | Guidance |
|---|---|
| Router vs explicit `path()` | Router for ViewSets (consistency, schema); explicit `path()` for `APIView`s and one-off endpoints |
| `DefaultRouter` vs `SimpleRouter` | `SimpleRouter` when you don't want the root view or format suffixes in production |
| Nested URLs vs filters | `/orders/{id}/lines/` when lines only exist within an order; `/lines/?order=…` when lines are queried across orders |
| Nesting depth | At most one level of nesting; deeper paths become brittle |
| Commands | Prefer modelling state (`PATCH status`) when a simple field change; an action (`POST /cancel/`) when the transition has rules/side effects |
| IDs vs slugs vs UUIDs in URLs | Sequential IDs leak volume and invite enumeration; UUIDs or slugs for public APIs — scoping still required |

**Avoiding RPC proliferation.** Smells: verbs in paths (`/getOrders`, `/createOrder`),
`/orders/{id}/update-address/` when `PATCH /orders/{id}/` would do, actions that operate on a
different resource. Each action should be a noun-backed state transition of its own resource.

## 5. Django/DRF implementation patterns

**Project URLs with a versioned API prefix:**

```python
# config/urls.py
urlpatterns = [
    path("api/v1/", include(("config.api_v1", "api-v1"), namespace="v1")),
]

# config/api_v1.py
router = SimpleRouter()
router.register("orders", OrderViewSet, basename="order")
router.register("products", ProductViewSet, basename="product")

urlpatterns = [
    path("reports/revenue/", RevenueReportView.as_view(), name="revenue-report"),
    *router.urls,
]
```

**Custom action URLs:**

```python
@action(detail=True, methods=["post"], url_path="cancel", url_name="cancel")
def cancel(self, request, pk=None): ...
# POST /api/v1/orders/{pk}/cancel/   name: order-cancel

@action(detail=False, methods=["get"], url_path="export")
def export(self, request): ...
# GET /api/v1/orders/export/          name: order-export
```

**Nested resources without a third-party package** (one level):

```python
order_lines = OrderLineViewSet.as_view({"get": "list", "post": "create"})
order_line = OrderLineViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})

urlpatterns += [
    path("orders/<int:order_pk>/lines/", order_lines, name="order-line-list"),
    path("orders/<int:order_pk>/lines/<int:pk>/", order_line, name="order-line-detail"),
]

class OrderLineViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                       mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = OrderLineSerializer

    def get_order(self):
        return get_object_or_404(order_list(user=self.request.user), pk=self.kwargs["order_pk"])

    def get_queryset(self):
        return self.get_order().lines.select_related("product")

    def perform_create(self, serializer):
        add_order_line(order=self.get_order(), **serializer.validated_data)
```

The parent is fetched through the user-scoped selector, so a foreign `order_pk` returns 404.
Third-party `drf-nested-routers` automates this; it is not part of DRF.

**Reversing:** use `rest_framework.reverse.reverse("order-detail", kwargs={"pk": 1}, request=request)`
— it returns absolute URLs when given `request` and respects the versioning scheme.

## 6. Production use cases

Well-designed URL structure for a commerce API:

```
GET    /api/v1/orders/?status=open&ordering=-created_at
POST   /api/v1/orders/
GET    /api/v1/orders/{id}/
PATCH  /api/v1/orders/{id}/                  # editable fields only
POST   /api/v1/orders/{id}/cancel/           # state transition with rules
GET    /api/v1/orders/{id}/lines/
POST   /api/v1/orders/{id}/lines/
DELETE /api/v1/orders/{id}/lines/{line_id}/
GET    /api/v1/customers/{id}/orders/        # relationship endpoint (read-only view of orders)
GET    /api/v1/reports/revenue/?from=2026-01-01&to=2026-03-31
```

## 7. Advantages and disadvantages

| Advantages of routers | Disadvantages |
|---|---|
| Uniform URLs and names across resources | URL structure is implicit; read `router.urls` to see it |
| Actions routed automatically | No built-in nesting |
| Schema generators understand ViewSet routes | `basename` mistakes produce confusing reverse errors |

## 8. Security and performance considerations

- URL design never replaces authorisation; nested URLs must scope the parent *and* the child.
- Sequential IDs enable enumeration — combine scoping with non-guessable public identifiers when
  exposure of counts matters.
- Don't put secrets, tokens or PII in paths or query strings (they end up in logs).
- `DefaultRouter`'s root view lists all registered endpoints; acceptable for internal APIs,
  consider `SimpleRouter` for public ones.

## 9. Common mistakes and anti-patterns

- Verbs in URLs and one action per field update.
- Missing `basename` for ViewSets that only define `get_queryset()`.
- Duplicate basenames across routers in the same namespace → wrong reverse results.
- Mixing trailing-slash and non-trailing-slash conventions (Django's `APPEND_SLASH` cannot
  redirect `POST`).
- Deeply nested URLs (`/orgs/{id}/projects/{id}/tasks/{id}/comments/`).

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| `NoReverseMatch` for `order-detail` | Wrong basename, namespace missing (`v1:order-detail`), or lookup kwarg name |
| Action URL 404 | `detail` flag wrong, `url_path` differs, method not in `methods` |
| `.json` suffix routes appear | `DefaultRouter` format suffixes |
| `POST` without trailing slash fails | `APPEND_SLASH` cannot redirect POST bodies |

Print routes: `python manage.py shell -c "from config.api_v1 import router; print(*router.urls, sep='\n')"`.
Assert `reverse()` names in tests.

## 11. Related references and skills

- [views-and-viewsets.md](views-and-viewsets.md) — actions and ViewSets
- [api-versioning-and-documentation.md](api-versioning-and-documentation.md) — URL versioning
- [permissions-and-access-control.md](permissions-and-access-control.md) — scoping nested resources
- [django-core urls-views](../../django-core/references/urls-views.md)

## 12. Official documentation and external references

- Routers: <https://www.django-rest-framework.org/api-guide/routers/>
- ViewSets — extra actions: <https://www.django-rest-framework.org/api-guide/viewsets/#marking-extra-actions-for-routing>
- Reverse: <https://www.django-rest-framework.org/api-guide/reverse/>
- Third-party: drf-nested-routers — <https://github.com/alanjds/drf-nested-routers>
