# Filtering, Search, and Ordering

> **Scope.** Owns: DRF's filter-backend architecture, `SearchFilter`, `OrderingFilter`, the
> third-party `DjangoFilterBackend`/`FilterSet` (django-filter), custom backends and filters,
> query-parameter design and validation, related-field/range/date filters, allow-listed ordering,
> and how filtering interacts with scoping and performance.
> Does not cover: pagination ([pagination.md](pagination.md)), access scoping design
> ([permissions-and-access-control.md](permissions-and-access-control.md)), lookup semantics and
> indexing ([django-orm queryset-api](../../django-orm/references/queryset-api.md),
> [django-orm database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md)),
> or full-text search engines.

## 1. Purpose and scope

List endpoints become useful through filters and ordering and become slow and leaky through
unbounded ones. This reference covers implementing filters that are explicit, validated, scoped
and indexable.

## 2. Core concepts

| Backend | Package | Query params | Configure with |
|---|---|---|---|
| `rest_framework.filters.SearchFilter` | DRF (built-in) | `?search=` (`SEARCH_PARAM`) | `search_fields` |
| `rest_framework.filters.OrderingFilter` | DRF (built-in) | `?ordering=` (`ORDERING_PARAM`) | `ordering_fields`, `ordering` |
| `django_filters.rest_framework.DjangoFilterBackend` | **django-filter** (third-party: `pip install django-filter`, add `"django_filters"` to `INSTALLED_APPS`) | Field-specific (`?status=open`) | `filterset_fields` or `filterset_class` |
| Custom `BaseFilterBackend` | Your code | Anything | `filter_queryset(request, queryset, view)` |

**`search_fields` prefixes:** `^` istartswith, `=` iexact, `$` iregex, `@` full-text search
(PostgreSQL only); no prefix = `icontains`. Multiple terms (whitespace/comma separated) must all
match (AND); each term may match any field (OR).

## 3. Internal mechanics

- `GenericAPIView.filter_queryset(queryset)` applies each class in `filter_backends`
  (default `DEFAULT_FILTER_BACKENDS`) in order. It is called by `ListModelMixin.list()` **and by
  `get_object()`** — filters also affect detail lookups (`?status=closed` on a detail URL can
  404).
- Backends receive the already-scoped queryset from `get_queryset()`; scoping must not be
  implemented as an optional filter.
- `OrderingFilter`: validates requested fields against `ordering_fields`. If `ordering_fields`
  is **not set**, the allowed fields default to the serializer's readable fields that map to
  model fields — usually wider than intended. `ordering_fields = "__all__"` allows every model
  field. Invalid fields are silently ignored (no error). `ordering` sets the default.
- `SearchFilter` builds `Q` objects across `search_fields`; when a search field crosses a
  to-many relation it applies `distinct()` to avoid duplicates (**VERIFY** exact condition in your
  release).
- `DjangoFilterBackend` builds a `FilterSet` from `filterset_class`/`filterset_fields`, validates
  query params with a Django form, and on invalid input raises a `ValidationError` (400) by
  default. Unknown query params are ignored.

## 4. Engineering decisions and trade-offs

| Need | Choose |
|---|---|
| Exact/enum/range/date filters on known fields | django-filter `FilterSet` (explicit, validated, documented) |
| Simple text search over a few columns of small/medium tables | `SearchFilter` with `^`/`=` prefixes where possible |
| Search over large tables or relevance ranking | PostgreSQL full-text (`@` or a custom backend with `SearchVector` + GIN index) or a search engine |
| Client-controlled sort | `OrderingFilter` with explicit `ordering_fields` |
| Filter not expressible as field lookups | `django_filters.CharFilter(method=...)` or a custom backend |
| Avoid a dependency for one filter | Read `request.query_params` in `get_queryset()`, validate with a serializer |

**Query parameter design.** Use field names clients understand (`created_after`, not
`created_at__gte`), consistent types (ISO 8601 dates), comma-separated multi-values
(`?status=open,paid`) or repeated keys — pick one convention API-wide. Every filter is part of
the public contract; document it.

## 5. Django/DRF implementation patterns

**FilterSet with range, date, related and multi-value filters:**

```python
import django_filters

class OrderFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=Order.Status.choices)
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")
    total = django_filters.RangeFilter()                          # ?total_min=10&total_max=100
    customer = django_filters.NumberFilter(field_name="customer_id")
    product_sku = django_filters.CharFilter(field_name="lines__product__sku", distinct=True)
    has_notes = django_filters.BooleanFilter(method="filter_has_notes")

    class Meta:
        model = Order
        fields = []                                               # only the declared filters

    def filter_has_notes(self, queryset, name, value):
        return queryset.exclude(note="") if value else queryset.filter(note="")
```

**View configuration:**

```python
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = OrderFilter
    search_fields = ["=reference", "^customer__name"]
    ordering_fields = ["created_at", "total", "reference"]
    ordering = ["-created_at", "-id"]                             # deterministic default

    def get_queryset(self):
        return order_list(user=self.request.user)                # scoping is not a filter
```

**Validating ad-hoc query params without django-filter:**

```python
class ReportQuerySerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()
    group_by = serializers.ChoiceField(choices=["day", "week", "month"], default="day")

    def validate(self, attrs):
        if attrs["end"] < attrs["start"] or (attrs["end"] - attrs["start"]).days > 366:
            raise serializers.ValidationError("Range must be 0–366 days.", code="invalid_range")
        return attrs

params = ReportQuerySerializer(data=request.query_params)
params.is_valid(raise_exception=True)
```

**Custom backend** (e.g. a `?mine=true` convenience filter shared by many views):

```python
class MineFilterBackend(filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.query_params.get("mine") == "true":
            return queryset.filter(created_by=request.user)
        return queryset
```

Implement `get_schema_operation_parameters()` on custom backends so schema generators document
them.

**Related-filter tenant scoping** — `ModelChoiceFilter` querysets must be scoped:

```python
def projects_for_request(request):
    return Project.objects.for_organization(request.user.organization) if request else Project.objects.none()

class TaskFilter(django_filters.FilterSet):
    project = django_filters.ModelChoiceFilter(queryset=projects_for_request)
```

## 6. Production use cases

- **Admin order table**: status multi-select, date range, customer, ordering by
  `created_at`/`total`, search by exact reference.
- **Catalogue search**: PostgreSQL full-text via `@` prefix or a custom backend with ranked
  `SearchVector`, backed by a GIN index.
- **Reporting endpoint**: query-param serializer bounding date ranges.
- **Multi-tenant SaaS**: all filters operate on the tenant-scoped queryset; related filters use
  scoped querysets so filter validation cannot probe other tenants' IDs.

## 7. Advantages and disadvantages

| Option | Advantages | Disadvantages |
|---|---|---|
| django-filter | Declarative, validated, schema-aware, rich lookups | Third-party dependency; `filterset_fields` dicts can expose too much |
| `SearchFilter` | Built-in, trivial setup | `icontains` → sequential scans; joins across relations; no ranking |
| `OrderingFilter` | Built-in, allow-listing | Silent ignore of invalid fields; unindexed sorts are slow |
| Custom backend | Exactly the semantics needed | You document and test it |

## 8. Security and performance considerations

- **Allow-list ordering fields.** Ordering by an unlisted field can reveal hidden data via sort
  order (e.g. ordering by a private score).
- Filtering by related fields (`lines__product__sku`) can probe data from related models — expose
  only intended paths.
- `icontains` and leading-wildcard searches cannot use B-tree indexes; on large tables use
  trigram (`pg_trgm`) or full-text indexes, or restrict to `^`/`=` prefixes.
- Index the filter and ordering combinations that real clients use; verify with `explain()`
  ([django-orm query-optimization](../../django-orm/references/query-optimization.md)).
- To-many filters/search can duplicate rows; `distinct()` adds cost — check the SQL.
- `$` regex search exposes the database to expensive patterns; avoid on public APIs.

## 9. Common mistakes and anti-patterns

- Implementing tenant scoping as a filter param (`?organization=3`).
- `ordering_fields` unset, or `"__all__"`, on public endpoints.
- `filterset_fields = "__all__"`-style exposure of every model field.
- No default `ordering` → unstable pagination ([pagination.md](pagination.md)).
- Assuming `DjangoFilterBackend` is part of DRF (it ships with django-filter).
- Search over many `icontains` fields on million-row tables.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| Filter param has no effect | Backend not in `filter_backends`; param name mismatch; unknown params ignored |
| `?ordering=x` ignored | Field not in `ordering_fields` (silently dropped) |
| Duplicate rows | To-many filter/search without `distinct` |
| 404 on detail with query params | Filters apply in `get_object()` |
| `ModuleNotFoundError: django_filters` | django-filter not installed |
| Slow list with search | Unindexed `icontains`; inspect SQL via `connection.queries` or `explain()` |

Tests: one per filter (match and non-match), invalid value → 400, ordering applied, other
tenant's IDs not reachable ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [pagination.md](pagination.md) — ordering requirements, filtered pagination
- [permissions-and-access-control.md](permissions-and-access-control.md) — scoping
- [views-and-viewsets.md](views-and-viewsets.md) — `filter_queryset()`, `get_object()`
- [django-orm queryset-api](../../django-orm/references/queryset-api.md),
  [django-orm query-optimization](../../django-orm/references/query-optimization.md)
- [django-performance](../../django-performance/SKILL.md), [postgresql-engineering](../../postgresql-engineering/SKILL.md) — indexes for search

## 12. Official documentation and external references

- DRF filtering: <https://www.django-rest-framework.org/api-guide/filtering/>
- django-filter (third-party): <https://django-filter.readthedocs.io/en/stable/guide/rest_framework.html>
- Django full-text search: <https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/search/>
