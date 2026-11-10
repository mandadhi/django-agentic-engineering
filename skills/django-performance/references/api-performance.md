# API Performance

**Scope:** Measuring and reducing the response time and resource cost of Django REST Framework endpoints: serialization overhead, nested-serializer query cost, pagination, filtering and ordering cost, payload size, API caching, throttling cost, bulk and streaming endpoints. DRF component mechanics are owned by [django-rest-framework](../../django-rest-framework/SKILL.md); this reference decides which lever to pull and how to prove it worked.

## 1. Purpose and scope

A DRF list endpoint's time is typically split across: database queries, model instantiation, serializer `to_representation`, rendering to JSON, and transfer. Teams routinely fix the first and discover the remaining 60% of the time is serialization. This reference covers where API time goes and the Django/DRF-level fixes for each part.

Covered: serializer cost model, light/read-only serializers, `values()`-based endpoints, `SerializerMethodField` cost, nested N+1, pagination cost (page-number/limit-offset vs cursor), filter/ordering cost, payload size, compression and ETags (pointer), API caching (pointer), throttling overhead, bulk endpoints, expensive-operation offloading, streaming exports, renderer choice.

Not covered: ORM loading-strategy detail ([orm-performance-optimization.md](orm-performance-optimization.md)), cache patterns ([caching-engineering.md](caching-engineering.md)), HTTP headers/compression/CDN ([http-and-static-delivery-performance.md](http-and-static-delivery-performance.md)), DRF API mechanics ([django-rest-framework](../../django-rest-framework/SKILL.md)).

Baseline: Django 5.2 LTS, DRF 3.16.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Per-row serialization cost | For each instance, each field does attribute access + `to_representation`. Cost ≈ rows × fields × per-field cost. Illustrative: 20–60 µs per row for a 15-field `ModelSerializer`; 1 000 rows ≈ 20–60 ms of pure Python. |
| Fixed per-request cost | Auth, permissions, throttles, content negotiation, middleware — paid even for empty responses. |
| Query cost vs Python cost | Measure both; DB time from query capture, Python time from a profiler. |
| Payload size | Bytes rendered and transferred; drives render time, compression CPU, and client-side parse time. |
| Pagination cost | `COUNT(*)` for totals plus `OFFSET` scan cost that grows with page depth. |
| Endpoint cost amplification | One request that triggers unbounded work (huge page sizes, deep nesting, full-text search, exports). |

Distinguish **latency** (p95 of one endpoint) from **throughput** (requests/s the workers sustain). Serialization CPU directly reduces throughput on sync workers because it holds the GIL ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)).

## 3. Internal mechanics

Relevant cost points in DRF (mechanics in [../../django-rest-framework/references/request-response-lifecycle.md](../../django-rest-framework/references/request-response-lifecycle.md) and [../../django-rest-framework/references/serializers-and-model-serializers.md](../../django-rest-framework/references/serializers-and-model-serializers.md)):

- **Field construction**: a `ModelSerializer` builds its fields by introspecting the model (`get_fields()` → `build_field()`) when `.fields` is first accessed on a serializer *instance*. With `many=True`, a `ListSerializer` wraps a **single child** instance, so field construction happens once per request, not per row. Instantiating a serializer per row manually (`[S(o).data for o in qs]`) repeats it N times.
- **`to_representation` loop**: for each row, iterates readable fields, calls `field.get_attribute(instance)` (dotted `source` lookups traverse relations → possible lazy queries) and `field.to_representation(value)`. Nested serializers recurse.
- **`SerializerMethodField`**: one Python call per row; any query inside is an N+1.
- **Relational fields**: `PrimaryKeyRelatedField` on a forward FK reads `obj.<fk>_id` without a query (DRF optimises the pk-only case — `VERIFY` for `source` with dotted paths); `StringRelatedField`/`SlugRelatedField`/nested serializers load the related object.
- **Pagination**: `PageNumberPagination` uses Django's `Paginator` → `COUNT(*)` + `LIMIT/OFFSET`. `LimitOffsetPagination` also counts. `CursorPagination` uses a keyset `WHERE (ordering) > cursor LIMIT n+1`, no count.
- **Rendering**: `JSONRenderer` uses the stdlib `json` module with DRF's encoder; cost scales with payload size. The browsable API renderer is far more expensive (renders forms, may run extra queries) — never in production defaults.
- **Throttling**: `SimpleRateThrottle` reads and writes a history list in the cache on **every** throttled request (one get + one set per throttle class), so latency of the cache backend is added to every call.

## 4. Engineering decisions and trade-offs

### Serializer strategy

| Situation | Choice | Trade-off |
|---|---|---|
| Typical list/detail, <100 rows/page | `ModelSerializer` + correct `get_queryset()` prefetching | Fine; optimise queries first |
| Hot read-only list, large pages, profiler shows serializer time dominant | Read-only `Serializer` with explicit fields, or `values()` + dict serializer | Separate read/write serializers to maintain |
| Internal/export endpoint, thousands of rows | `values()` / `values_list()` and stream rows | No model methods; contract must be explicit |
| Computed fields | Annotate in SQL, read the annotation in `SerializerMethodField` or a plain field | Annotation must exist on every queryset that uses the serializer |

**When it makes things worse:** hand-rolled serializers that drift from the write serializer's validation or field names (contract breakage); `values()` endpoints that silently drop permission-relevant fields or scoping applied in model methods.

### Pagination

| Paginator | Cost | Use when |
|---|---|---|
| `PageNumberPagination` | `COUNT(*)` + `OFFSET k` (scans k rows) | Small/medium tables, UI needs page numbers and totals |
| `LimitOffsetPagination` | Same as above | Clients need arbitrary offsets; same depth cost |
| `CursorPagination` | Keyset seek, no count; O(page size) with index | Large or append-heavy tables, infinite scroll, sync APIs; needs a unique, immutable, indexed ordering |

Illustrative: on a 20 M-row table, `COUNT(*)` with a tenant filter took 1.4 s and `OFFSET 200000` 900 ms; cursor pages stayed ~8 ms. Cursor pagination cannot jump to page N and cannot show totals — a product decision, not only a technical one. Details → [../../django-rest-framework/references/pagination.md](../../django-rest-framework/references/pagination.md).

Mitigations when totals are required: cap counted rows (`count` over `[:10001]` → "10 000+"), cache counts per filter set with a short TTL, or PostgreSQL estimates (`reltuples`) for unfiltered totals.

### Filtering and ordering

- Every allowed filter and ordering is a query plan. Allow-list `ordering_fields`; back each common filter+ordering combination with a composite index ([database-performance.md](database-performance.md)).
- `SearchFilter` with `icontains` across several fields becomes `ILIKE '%term%'` OR-chains → sequential scans. For large tables use PostgreSQL full-text search or trigram indexes (GIN, `pg_trgm`), or a search service.
- django-filter [third-party: django-filter] filters that span to-many relations can add JOINs and duplicate rows → `distinct()` cost.
- Details → [../../django-rest-framework/references/filtering-search-and-ordering.md](../../django-rest-framework/references/filtering-search-and-ordering.md).

### Payload size

- Return only fields clients use; separate list (summary) and detail serializers.
- Sparse fieldsets (`?fields=id,name`) reduce payload but multiply cache variants and complicate query optimisation; implement only with an allow-list and matching `only()`.
- Avoid embedding unbounded nested collections; expose them as sub-resources with their own pagination.
- Compression (gzip/brotli) trades CPU for bytes; usually done at the proxy/CDN → [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md).

### Caching API responses

- Prefer caching the expensive computed part (aggregates, external lookups) over caching whole per-user responses.
- Conditional requests (`ETag`/`Last-Modified` → 304) save rendering and transfer but **not** the cost of computing the validator unless it is cheap (e.g. `max(updated_at)` query).
- Key every cached response on tenant, user/role, query params, `Accept`, version. Patterns, invalidation and stampede control → [caching-engineering.md](caching-engineering.md).

### Throttling

- Protects expensive endpoints from abuse; costs one or more cache round trips per request (≈0.2–1 ms each on same-AZ Redis, illustrative).
- Throttle state in locmem is per-process and meaningless with multiple workers; use the shared Redis cache.
- Prefer coarse limits at the edge (proxy/API gateway) for volumetric protection; use DRF throttles for per-user business limits. → [../../django-rest-framework/references/throttling-and-rate-limiting.md](../../django-rest-framework/references/throttling-and-rate-limiting.md).

### Bulk endpoints

- A bulk create endpoint (`many=True` → `bulk_create`) replaces N HTTP requests and N INSERTs with one of each.
- Bound the batch (e.g. ≤ 500 items, illustrative), validate per item, decide on partial-success semantics, and keep the transaction short.
- Above a few seconds of work, accept the payload and process asynchronously (`202 Accepted` + status resource) → [background-task-performance.md](background-task-performance.md).

## 5. Django implementation patterns

### Query-correct ViewSet

```python
from django.db.models import Count, Prefetch
from rest_framework import viewsets
from rest_framework.pagination import CursorPagination

class OrderCursorPagination(CursorPagination):
    ordering = ("-created_at", "-id")       # unique + indexed: (tenant_id, created_at, id)
    page_size = 50
    max_page_size = 200                       # CursorPagination ignores page_size_query_param unless set

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = OrderCursorPagination

    def get_queryset(self):
        qs = (Order.objects.filter(tenant=self.request.user.tenant)
              .select_related("customer")
              .annotate(line_count=Count("lines")))
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch("lines", queryset=OrderLine.objects.select_related("product")))
        return qs

    def get_serializer_class(self):
        return OrderDetailSerializer if self.action == "retrieve" else OrderListSerializer
```

Loading strategy varies per action; the list serializer only uses `customer` and the annotation.

### Lightweight read serializer

```python
from rest_framework import serializers

class OrderListSerializer(serializers.Serializer):          # no model introspection
    id = serializers.IntegerField(read_only=True)
    reference = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    line_count = serializers.IntegerField(read_only=True)  # from annotate(), no per-row query
```

### values()-based high-volume endpoint

```python
from rest_framework.response import Response
from rest_framework.views import APIView

class ProductOptionsView(APIView):
    def get(self, request):
        rows = list(Product.objects.filter(tenant=request.user.tenant, active=True)
                    .order_by("name").values("id", "name")[:1000])
        return Response(rows)                      # dicts render directly; no serializer loop
```

Use where the response is a fixed projection and no per-object permission logic exists.

### Streaming a large export outside DRF renderers

```python
import csv
from django.http import StreamingHttpResponse

class Echo:
    def write(self, value):
        return value

def export_orders(request):
    rows = (Order.objects.filter(tenant=request.user.tenant)
            .order_by("pk").values_list("reference", "status", "total", "created_at")
            .iterator(chunk_size=2000))
    writer = csv.writer(Echo())
    header = ("reference", "status", "total", "created_at")

    def generate():
        yield writer.writerow(header)
        for r in rows:
            yield writer.writerow(r)
    response = StreamingHttpResponse(generate(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders.csv"'
    return response
```

Notes: DRF renderers buffer the whole response, so large exports bypass them. A streaming response holds a worker (and, with `iterator()` on PostgreSQL, a server-side cursor and connection) for the duration; under WSGI this consumes a worker per download; under ASGI, sync iterators are consumed in a thread (Django ≥ 4.2 supports async iterators for `StreamingHttpResponse`). For exports beyond ~1 min, generate in a background task and serve from storage → [file-and-data-processing-performance.md](file-and-data-processing-performance.md).

### Faster JSON rendering (optional)

```python
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",          # browsable API only in DEBUG settings
    ],
}
```

[third-party: orjson] via [third-party: drf-orjson-renderer] can reduce rendering time for large payloads; benchmark first — for 50-row pages rendering is rarely the bottleneck, and type handling (Decimal, datetime) may differ from DRF's encoder (`VERIFY` output compatibility).

### Bounded bulk create

```python
class BulkLineCreateView(APIView):
    MAX_ITEMS = 500

    def post(self, request):
        if len(request.data) > self.MAX_ITEMS:
            return Response({"detail": "Too many items."}, status=413)
        serializer = LineInputSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        create_lines(order_id=..., items=serializer.validated_data)  # service: one atomic bulk_create
        return Response(status=201)
```

Nested-write mechanics → [../../django-rest-framework/references/nested-and-relational-serializers.md](../../django-rest-framework/references/nested-and-relational-serializers.md).

### Measuring endpoint cost split

```python
import time
from django.db import connection
from django.test.utils import CaptureQueriesContext

def measure(viewset_qs, serializer_cls):
    with CaptureQueriesContext(connection) as ctx:
        t0 = time.perf_counter(); objs = list(viewset_qs); t1 = time.perf_counter()
        data = serializer_cls(objs, many=True).data; t2 = time.perf_counter()
    db_ms = sum(float(q["time"]) for q in ctx.captured_queries) * 1000
    print(f"queries={len(ctx.captured_queries)} db={db_ms:.0f}ms "
          f"fetch={1000*(t1-t0):.0f}ms serialize={1000*(t2-t1):.0f}ms")
```

Queries appearing during the serialize phase are N+1s triggered by the serializer. Profile the serialize phase with pyinstrument for field-level hotspots ([profiling-and-observability.md](profiling-and-observability.md)).

## 6. Production use cases

- **Mobile feed endpoint p95 1.8 s:** queries fixed (7 total), but profiling showed 70% in nested serializers of 100 posts × 20 comments. Fix: comments moved to a sub-resource, feed returns `comment_count` annotation; payload 420 KB → 38 KB; p95 → 210 ms (illustrative).
- **Admin-style "page 5 000" scans:** a partner integration paged through 30 M rows with `?offset=`; each deep page took >3 s and the sync job ran for 20 h. Switched the sync API to `CursorPagination` ordered by `(updated_at, id)` with an index; full sync 40 min.
- **Ordering by an unindexed field:** `?ordering=customer__name` across a JOIN caused sorts of 4 M rows. Ordering allow-list restricted to indexed fields; customer-name ordering available only with a tenant filter.
- **Throttle latency:** two throttle classes with a remote Redis across regions added 6 ms per request. Moved cache to the same region; collapsed to one throttle scope.
- **Export endpoint exhausting workers:** synchronous CSV export of 2 M rows held 8 workers for minutes during month-end. Moved to a Celery task writing to object storage and emailing a signed URL.

## 7. Advantages and disadvantages

| Lever | Advantages | Disadvantages |
|---|---|---|
| Per-action querysets/serializers | Each action loads only what it needs | More code paths to test |
| Light read serializers / `values()` | Large Python-time savings on big pages | Duplicated field contracts; lose model behaviour |
| Cursor pagination | Constant cost at any depth, stable under inserts | No totals/page jumps; requires unique indexed ordering |
| Sparse fieldsets | Smaller payloads | Cache fragmentation; dynamic `only()` complexity |
| Throttling | Bounds abuse of expensive endpoints | Per-request cache round trips; shared-cache dependency |
| Bulk endpoints | Fewer requests and round trips | Partial-failure semantics; large transactions if unbounded |
| Streaming | Flat memory, early first byte | Holds workers/connections; errors after headers can't change status code |

## 8. Security and performance considerations

- Every performance shortcut must preserve **scoping**: `values()` endpoints and cached responses must filter by tenant/user exactly like the main queryset ([../../django-rest-framework/references/permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md)).
- Bound everything the client controls: `page_size` (`max_page_size`), bulk item counts, filter list lengths, search term length, nesting depth via `?expand=`.
- Expensive endpoints (search, export, reports) are DoS vectors — throttle them separately and/or move them to async jobs.
- Cache keys for API responses must include auth identity and permission-relevant attributes; never mark per-user responses `Cache-Control: public`.
- Disable the browsable API renderer in production: it is slower and can expose forms and related querysets.

## 9. Common mistakes and anti-patterns

- Prefetching in the serializer (`obj.items.all()` in `to_representation`) instead of `get_queryset()`.
- One `get_queryset()` that prefetches everything the detail view needs for the list action too.
- `SerializerMethodField` that issues a query or calls an external service per row.
- Serializing per row manually (`[S(o).data for o in qs]`) instead of `S(qs, many=True)`.
- `PageNumberPagination` on large, filtered tables with no plan for `COUNT(*)` cost.
- `CursorPagination` with a non-unique ordering (`created_at` only) → skipped or duplicated rows.
- Allowing `ordering_fields = "__all__"`.
- Unbounded `?page_size=` or nested `?expand=` parameters.
- Throttling with the locmem cache in multi-process deployments.
- Using DRF `Response` for multi-hundred-MB exports (buffered in memory).
- Optimising queries when the profiler says time is in serialization or rendering — or vice versa.

## 10. Troubleshooting and validation

**Diagnosis sequence for a slow endpoint**

1. Reproduce with a production-sized page, realistic relations and an authenticated user of the right role.
2. Split time: middleware/auth/throttle, DB (count, time), fetch/instantiate, serialize, render, transfer. APM spans or the `measure()` pattern above; Silk for per-request SQL in staging ([profiling-and-observability.md](profiling-and-observability.md)).
3. Fix the dominant part: DB → [orm-performance-optimization.md](orm-performance-optimization.md)/[database-performance.md](database-performance.md); serialize → light serializer/annotations; render/transfer → payload trimming/compression; pagination → cursor or capped counts.
4. Re-measure p50/p95 under concurrency with a load tool ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)); single-request timings hide GIL and worker contention.
5. Lock in with a query-budget test per list endpoint and, for hot endpoints, a regression benchmark.

**Validation checklist**

- [ ] Query count constant across page sizes 1, 20, 100.
- [ ] Serialize time measured and proportionate (rows × fields).
- [ ] Response size measured (uncompressed and compressed).
- [ ] Pagination cost checked at deep pages / large tenants; ordering unique and indexed.
- [ ] Each allowed filter/ordering combination has a plan reviewed with `explain()`.
- [ ] Client-controlled sizes bounded; expensive operations throttled or async.
- [ ] Cached/streamed/`values()` paths preserve the same scoping and permissions as the standard path (tests for other-tenant access).

## 11. Related references and skills

- [orm-performance-optimization.md](orm-performance-optimization.md) — loading strategies, annotations.
- [database-performance.md](database-performance.md) — indexes for filters and orderings.
- [caching-engineering.md](caching-engineering.md) — API response and computed-data caching.
- [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md) — compression, ETags, cache headers.
- [file-and-data-processing-performance.md](file-and-data-processing-performance.md) — large exports and uploads.
- [background-task-performance.md](background-task-performance.md) — async job endpoints.
- [django-rest-framework](../../django-rest-framework/SKILL.md): [pagination.md](../../django-rest-framework/references/pagination.md), [nested-and-relational-serializers.md](../../django-rest-framework/references/nested-and-relational-serializers.md), [filtering-search-and-ordering.md](../../django-rest-framework/references/filtering-search-and-ordering.md), [throttling-and-rate-limiting.md](../../django-rest-framework/references/throttling-and-rate-limiting.md), [serializers-and-model-serializers.md](../../django-rest-framework/references/serializers-and-model-serializers.md).
- [django-testing](../../django-testing/SKILL.md) — API query-count tests.

## 12. Official documentation and external references

- DRF serializers: https://www.django-rest-framework.org/api-guide/serializers/
- DRF serializer relations: https://www.django-rest-framework.org/api-guide/relations/
- DRF pagination (cursor pagination): https://www.django-rest-framework.org/api-guide/pagination/
- DRF filtering: https://www.django-rest-framework.org/api-guide/filtering/
- DRF throttling: https://www.django-rest-framework.org/api-guide/throttling/
- DRF renderers: https://www.django-rest-framework.org/api-guide/renderers/
- DRF release notes: https://www.django-rest-framework.org/community/release-notes/
- Django streaming large CSV files: https://docs.djangoproject.com/en/5.2/howto/outputting-csv/#streaming-large-csv-files
- `StreamingHttpResponse`: https://docs.djangoproject.com/en/5.2/ref/request-response/#streaminghttpresponse-objects
- Django `Paginator`: https://docs.djangoproject.com/en/5.2/topics/pagination/
- PostgreSQL full-text search / pg_trgm: https://www.postgresql.org/docs/current/textsearch.html · https://www.postgresql.org/docs/current/pgtrgm.html
- [third-party] django-filter: https://django-filter.readthedocs.io/ · orjson: https://github.com/ijl/orjson · drf-orjson-renderer: https://github.com/brianjbuck/drf_orjson_renderer
