# Pagination

> **Scope.** Owns: DRF's pagination architecture, `PageNumberPagination`,
> `LimitOffsetPagination`, `CursorPagination`, custom paginators and response envelopes, page-size
> limits, ordering/stability requirements, pagination in `APIView`s, and performance on large
> datasets. Does not cover: filtering/ordering backends ([filtering-search-and-ordering.md](filtering-search-and-ordering.md)),
> SQL-level pagination and keyset internals ([django-orm query-optimization](../../django-orm/references/query-optimization.md)),
> or Django's `Paginator` outside DRF ([django-core urls-views](../../django-core/references/urls-views.md)).

## 1. Purpose and scope

Every collection endpoint needs a bound. This reference explains DRF's paginators, how to choose
one, and how to keep pages correct and cheap.

## 2. Core concepts

| Class | Params | Response keys | Count query | Random access |
|---|---|---|---|---|
| `PageNumberPagination` | `page`, optional `page_size_query_param` | `count`, `next`, `previous`, `results` | Yes | Yes (page N) |
| `LimitOffsetPagination` | `limit`, `offset` | `count`, `next`, `previous`, `results` | Yes | Yes (offset) |
| `CursorPagination` | `cursor` (opaque), optional `page_size_query_param` | `next`, `previous`, `results` | No | No |

Key attributes:

- `PageNumberPagination`: `page_size` (defaults to `PAGE_SIZE`), `page_query_param`,
  `page_size_query_param` (default `None` = clients cannot change size), `max_page_size`,
  `last_page_strings` (`("last",)`).
- `LimitOffsetPagination`: `default_limit` (defaults to `PAGE_SIZE`), `max_limit`.
- `CursorPagination`: `page_size`, `ordering` (default `"-created"` — must be overridden if the
  model has no `created` field), `cursor_query_param`, `page_size_query_param`, `max_page_size`.

## 3. Internal mechanics

- Pagination is enabled globally by `DEFAULT_PAGINATION_CLASS` **and** a page size (`PAGE_SIZE`
  or the class's `page_size`). DRF's default is no pagination.
- Only generic views/ViewSets paginate automatically: `ListModelMixin.list()` calls
  `paginate_queryset()`, serializes the page and returns `get_paginated_response(data)`. `APIView`
  must call the paginator explicitly.
- `PageNumberPagination` wraps Django's `Paginator`: `COUNT(*)` on the filtered queryset, then
  `LIMIT/OFFSET`. Invalid page → `NotFound` (404) with "Invalid page.".
- `LimitOffsetPagination` also counts, then slices `[offset:offset + limit]`.
- `CursorPagination` encodes position (last ordering value + offset for ties + direction) in a
  base64 cursor and filters `WHERE ordering_field > value` (keyset). It requires a fixed ordering
  of an unchanging, ideally unique, indexed field; it uses `OrderingFilter`'s ordering if that
  filter is present (so client-chosen ordering must be restricted to suitable fields).
- Django emits `UnorderedObjectListWarning` when paginating an unordered queryset; pages may
  overlap or skip rows.

## 4. Engineering decisions and trade-offs

| Situation | Choose |
|---|---|
| Admin tables needing "page 7 of 40" and total counts, moderate table size | `PageNumberPagination` |
| Clients that think in offsets (some data grids, sync tools) | `LimitOffsetPagination` |
| Large or append-heavy collections, feeds, infinite scroll, sync/export | `CursorPagination` |
| Strong consistency while rows are inserted between requests | `CursorPagination` (no duplicates/skips) |
| Very large tables where `COUNT(*)` dominates | Cursor, or a custom page-number paginator without exact count |
| Bulk export of everything | Not pagination — an async export job ([django-data-processing](../../django-data-processing/SKILL.md)) |

Offset-based pagination degrades linearly with depth (`OFFSET 1000000` still scans the skipped
rows) and shifts under concurrent inserts/deletes.

## 5. Django/DRF implementation patterns

**Project paginators:**

```python
# config/api/pagination.py
class DefaultPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200

class FeedPagination(CursorPagination):
    page_size = 100
    ordering = ("-created_at", "-id")      # unique tie-breaker; index (created_at, id)
    page_size_query_param = "page_size"
    max_page_size = 500
```

```python
REST_FRAMEWORK = {"DEFAULT_PAGINATION_CLASS": "config.api.pagination.DefaultPagination", "PAGE_SIZE": 50}

class ActivityViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = FeedPagination
```

**Custom envelope** (e.g. `meta` + `data`), keeping DRF's mechanics:

```python
class EnvelopePagination(DefaultPagination):
    def get_paginated_response(self, data):
        return Response({
            "data": data,
            "meta": {"count": self.page.paginator.count, "page": self.page.number,
                     "page_size": self.get_page_size(self.request)},
            "links": {"next": self.get_next_link(), "previous": self.get_previous_link()},
        })

    def get_paginated_response_schema(self, schema):     # keep generated docs accurate
        return {"type": "object", "properties": {"data": schema, "meta": {"type": "object"},
                                                 "links": {"type": "object"}}}
```

**Pagination in an `APIView`:**

```python
class SearchView(APIView):
    def get(self, request):
        qs = search_products(organization=request.user.organization, q=request.query_params.get("q", ""))
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(ProductOutputSerializer(page, many=True).data)
```

**Cheaper counts for large tables** (approximate or capped — document the semantics):

```python
class CappedCountPaginator(django.core.paginator.Paginator):
    @cached_property
    def count(self):
        return self.object_list[:10_001].count()            # "10000+" semantics

class LargeTablePagination(DefaultPagination):
    django_paginator_class = CappedCountPaginator
```

**Disable pagination for a known-small, bounded collection:** `pagination_class = None` — and
make the bound explicit in the queryset.

## 6. Production use cases

- **Orders admin**: page-number with `max_page_size=200`, default ordering `("-created_at", "-id")`.
- **Activity feed / mobile timeline**: cursor pagination on `(created_at, id)`.
- **Partner sync**: cursor pagination ordered by `(updated_at, id)` so clients resume from the
  last cursor (note: rows updated during sync move; document the guarantee).
- **Filtered lists**: filters applied first, then pagination; `count` reflects the filtered set.

## 7. Advantages and disadvantages

| Class | Advantages | Disadvantages |
|---|---|---|
| Page number | Familiar, random access, totals | `COUNT(*)` per request; deep pages slow; unstable under writes |
| Limit/offset | Flexible window sizes | Same costs as page number; clients can request huge offsets |
| Cursor | Constant-time pages with an index; stable under inserts | No totals, no jumping to page N; ordering constraints; opaque cursors |

## 8. Security and performance considerations

- Always set `max_page_size`/`max_limit` when clients can choose size — otherwise
  `?page_size=1000000` is an exfiltration and DoS vector.
- Ordering must be deterministic: add a unique tie-breaker (`id`).
- Index the ordering columns (and filter + ordering composites); verify with `explain()`.
- `COUNT(*)` on large filtered PostgreSQL tables is expensive — measure; consider capped counts or
  cursors.
- Prefetching applies per page: `prefetch_related` runs one extra query per relation per page,
  which is the desired behaviour.

## 9. Common mistakes and anti-patterns

- No global pagination class → unbounded lists.
- `page_size_query_param` without `max_page_size`.
- Unordered querysets (`UnorderedObjectListWarning`), or ordering by a non-unique field only.
- `CursorPagination` with the default `ordering = "-created"` on a model without `created`.
- Letting `OrderingFilter` choose arbitrary orderings with cursor pagination.
- Building custom envelopes without updating the schema.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| Duplicate/missing items across pages | Non-deterministic ordering; concurrent writes with offset pagination |
| Response not paginated | No page size; `APIView` without explicit paginator; `pagination_class = None` |
| `FieldError: Cannot resolve keyword 'created'` | Cursor pagination default ordering |
| 404 "Invalid page." | Page out of range (expected behaviour; clients should follow `next`) |
| Slow first request on a large table | `COUNT(*)` |

Tests: assert envelope keys, `max_page_size` capping, deterministic order across two pages, and
query count per page ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [filtering-search-and-ordering.md](filtering-search-and-ordering.md)
- [views-and-viewsets.md](views-and-viewsets.md)
- [api-versioning-and-documentation.md](api-versioning-and-documentation.md) — documenting envelopes
- [django-orm query-optimization](../../django-orm/references/query-optimization.md)
- [django-performance](../../django-performance/SKILL.md), [django-data-processing](../../django-data-processing/SKILL.md)

## 12. Official documentation and external references

- Pagination: <https://www.django-rest-framework.org/api-guide/pagination/>
- Django pagination: <https://docs.djangoproject.com/en/5.2/topics/pagination/>
- Cursor pagination background (DRF docs reference): "Building cursors for the Disqus API" by David Cramer — linked from the DRF pagination guide
