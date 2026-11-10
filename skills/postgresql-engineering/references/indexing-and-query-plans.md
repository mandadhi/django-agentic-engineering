# Indexing and query plans

## Index types

| Type | Use for | Django |
|---|---|---|
| B-tree | equality, range, `ORDER BY`, `LIKE 'prefix%'` (with `text_pattern_ops` or C collation) | `models.Index` (default) |
| Partial | skewed predicates (`status='pending'`, `deleted_at IS NULL`) | `Index(..., condition=Q(...))` |
| Covering | index-only scans for hot reads | `Index(..., include=[...])` |
| Expression | `lower(email)`, `date_trunc('day', ts)` | `Index(Lower("email"), name=...)` |
| GIN | `jsonb` containment, arrays, full-text `tsvector`, trigram | `GinIndex` (`contrib.postgres`) |
| GiST | ranges, exclusion constraints, geometry | `GistIndex` |
| BRIN | huge append-only tables ordered by time/id | `BrinIndex` |

## Column order in composite B-tree indexes

1. Columns compared with `=` (most selective or always-present first, e.g. `tenant_id`).
2. Then the range or sort column (`created_at DESC`).
3. A query can use a leading prefix of the index, not a middle column alone.

`WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT 50` → index
`(tenant_id, status, created_at DESC)`.

## Reading EXPLAIN

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) SELECT ...;
```

Look at, in order:

1. **Actual time & rows** of the top node — the real cost.
2. **Estimated vs actual rows** per node — a 10×+ gap means stale or insufficient statistics
   (`ANALYZE`, raise `default_statistics_target` for that column, or extended statistics for
   correlated columns: `CREATE STATISTICS ... (dependencies) ON a, b FROM t`).
3. **Seq Scan on a big table with a selective filter** → missing/unused index.
4. **Rows Removed by Filter** large → index doesn't cover the predicate.
5. **Sort with `external merge`** → `work_mem` too small or missing index for the order.
6. **Nested Loop with many loops** over a big inner side → join on unindexed FK or a bad estimate.
7. **Buffers: shared read** high → cold cache / I/O bound.

Run with real parameter values; generic plans for prepared statements can differ.

## Common rewrites

| Slow | Faster |
|---|---|
| `OFFSET 100000 LIMIT 50` | keyset: `WHERE (created_at, id) < (%s, %s) ORDER BY created_at DESC, id DESC LIMIT 50` (DRF `CursorPagination`) |
| `WHERE id IN (SELECT ...)` with huge lists from Python | `Exists(...)` / join in SQL |
| `COUNT(*)` for "has any" | `EXISTS` (`.exists()`) |
| `OR` across different columns | `UNION` of indexed queries, or separate indexes + bitmap OR |
| `icontains` on big text | trigram GIN index or full-text search |
| `DISTINCT` to fix join duplicates | `Exists` filter instead of the join |

## Full-text search

```python
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, SearchVectorField

class Article(models.Model):
    search_vector = models.GeneratedField(
        expression=SearchVector("title", weight="A", config="english")
                   + SearchVector("body", weight="B", config="english"),
        output_field=SearchVectorField(), db_persist=True)

    class Meta:
        indexes = [GinIndex(fields=["search_vector"], name="article_search_gin")]

query = SearchQuery(term, config="english", search_type="websearch")
Article.objects.filter(search_vector=query).annotate(rank=SearchRank("search_vector", query)).order_by("-rank")
```

Trigram similarity for fuzzy matching (`CREATE EXTENSION pg_trgm` via `TrigramExtension()` in a
migration; `GinIndex(OpClass(Upper("name"), name="gin_trgm_ops"), name=...)`).

## JSONB

- Filter with containment (`data__contains={"kind": "x"}`) — served by a GIN index.
- Frequently filtered keys deserve real columns (or a `GeneratedField` + B-tree index).

## Finding index problems

```sql
-- unused indexes (since stats reset)
SELECT relname, indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes WHERE idx_scan = 0 ORDER BY pg_relation_size(indexrelid) DESC;

-- top queries by total time (pg_stat_statements)
SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time, 1) AS mean_ms, query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```

Check that an index is unused on **all** replicas before dropping it.

## Official documentation

- https://www.postgresql.org/docs/current/indexes.html
- https://www.postgresql.org/docs/current/using-explain.html
- https://www.postgresql.org/docs/current/planner-stats.html
- https://www.postgresql.org/docs/current/textsearch.html
- https://www.postgresql.org/docs/current/pgtrgm.html
- https://www.postgresql.org/docs/current/pgstatstatements.html
- https://docs.djangoproject.com/en/stable/ref/contrib/postgres/indexes/
- https://docs.djangoproject.com/en/stable/ref/contrib/postgres/search/
