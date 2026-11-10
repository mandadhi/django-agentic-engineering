# Memory and Resource Management

**Scope:** How Django processes use memory, file handles and connections; how to profile memory,
find leaks and unbounded growth, process large data without loading it, and keep long-running
web and task workers within their limits. Detailed file/ETL processing patterns live in
[file-and-data-processing-performance.md](file-and-data-processing-performance.md).

## 1. Purpose and scope

Use this reference when:

- Worker RSS grows steadily until the container is OOM-killed (exit code 137).
- A management command or Celery task loading a large table consumes gigabytes.
- Uploads or downloads spike memory.
- "Too many open files" or connection leaks appear in long-running processes.
- Deciding on worker recycling or GC tuning.

CPU profiling tools are in [profiling-and-observability.md](profiling-and-observability.md); this
reference owns **memory** profiling tools.

## 2. Core concepts

- **RSS vs Python heap.** RSS (what the kernel and container limits see) includes interpreter,
  C extensions, thread stacks, freed-but-retained memory and shared pages. `tracemalloc` sees only
  Python allocations. A process can have a small live heap and a large RSS.
- **Leak vs growth vs spike.**
  - *Leak*: references retained indefinitely (RSS climbs with request count, never plateaus).
  - *High-water growth*: fragmentation — RSS rises to the largest working set and stays (plateaus).
  - *Spike*: one request/task allocates a lot (large QuerySet, file in memory) — may OOM instantly.
- **Memory per worker × workers = budget.** Every extra Gunicorn process or Celery child multiplies
  base RSS (illustrative: 120–300 MB per Django worker after imports).
- **Copy-on-write after fork.** Pre-forked workers share parent pages until written; CPython's
  refcount updates touch object headers, so sharing erodes over time.

## 3. Internal mechanics

### CPython memory

- **Reference counting** frees most objects immediately when the last reference drops.
- **Cyclic GC** (generations 0/1/2, thresholds via `gc.get_threshold()`) collects reference cycles.
  Django objects form cycles easily (model instance ↔ related cache, exceptions ↔ tracebacks ↔
  frames). Python 3.13/3.14 change GC internals (incremental collection work) — `VERIFY` behaviour
  for your version before tuning.
- **pymalloc** serves small objects (≤ 512 bytes) from 256 KiB arenas (size per version — `VERIFY`);
  an arena is returned to the OS only when entirely empty. Long-lived objects scattered across
  arenas pin them → RSS rarely shrinks after a spike.
- Large allocations go to the system allocator (glibc malloc); glibc's per-thread arenas can
  inflate RSS with many threads (`MALLOC_ARENA_MAX` is a known mitigation — measure first).

### Django-specific retention

| Source | Mechanism |
|---|---|
| `DEBUG=True` | Every query appended to `connection.queries` (capped at 9000 entries per connection — `VERIFY` constant), reset only per request; long scripts with DEBUG grow without bound |
| QuerySet result cache | Iterating a QuerySet stores all instances in `_result_cache`; keeping the QuerySet alive keeps all rows |
| `prefetch_related` | Prefetched objects held on each instance |
| Model instance caches | `_state.fields_cache`, `_prefetched_objects_cache` keep related objects alive |
| Signal receivers | `weak=True` by default; `weak=False` or bound closures capturing large objects keep them alive |
| Module-level dicts / unbounded `functools.lru_cache(maxsize=None)` / `@cache` | Grow with distinct keys forever |
| `LocMemCache` | Per-process, bounded by `MAX_ENTRIES` (default 300) — large values still cost |
| Logging handlers buffering records | `MemoryHandler`, in-memory test handlers left in prod |

### Database cursors

- Default psycopg cursors are client-side: the whole result set is transferred into client memory
  before the first row is returned.
- `QuerySet.iterator()` on PostgreSQL uses **server-side cursors** (named cursors) and fetches
  `chunk_size` rows at a time (default 2000), and skips the result cache.
- Server-side cursors need a transaction-scoped connection; behind **PgBouncer in transaction
  pooling mode** they break unless `DISABLE_SERVER_SIDE_CURSORS = True` is set for that database —
  which reverts to fetching the full result into memory per chunked fetch from the driver
  (`VERIFY` exact semantics), so batch by keyset instead.
- `iterator()` with `prefetch_related` requires `chunk_size` (Django ≥ 4.1; prefetches per chunk).

### Uploads

Django's upload handlers keep files up to `FILE_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) in
memory (`MemoryFileUploadHandler`) and stream larger ones to temp files
(`TemporaryFileUploadHandler`). `DATA_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) caps non-file
request bodies; `request.body` reads the whole body into memory.

## 4. Engineering decisions and trade-offs

| Technique | Bottleneck addressed | Measure with | Trade-offs / when it hurts |
|---|---|---|---|
| `iterator(chunk_size=…)` | Full result set + result cache in memory | tracemalloc peak, RSS during job | No result cache (re-iteration re-queries); server-side cursor holds a transaction/snapshot open — long cursors block vacuum; PgBouncer caveat |
| Keyset batching (`pk__gt=last`) | Same, plus long-open cursors | Peak memory, transaction duration | More queries; requires an indexed, monotonic key |
| `values()`/`values_list()` | Model instance overhead (~1 KB+ per instance, illustrative) | tracemalloc by line | Lose model methods; dict per row still costs |
| `only()`/`defer()` | Wide rows (JSON/text blobs) | Peak memory, query bytes | Accessing a deferred field triggers a query per row |
| Streaming responses / `FileResponse` | Building whole payload in memory | RSS during download | Holds a worker for the transfer duration — offload to object storage/CDN for large files |
| Worker recycling (`max_requests`) | Slow leaks/fragmentation | RSS vs request count | Masks leaks; restart cost; synchronized restarts without jitter |
| `gc.freeze()` before fork | Copy-on-write erosion from GC touching shared objects | Shared vs private memory (`smem`, `/proc/<pid>/smaps_rollup`) | Frozen objects never collected; benefit varies |
| Raising GC thresholds / `gc.disable()` | GC pause CPU time | GC time via `gc.callbacks`, CPU profile | Unbounded cycle growth → memory blow-up; only with measurement |

## 5. Django implementation patterns

### Processing a large table in bounded memory

```python
# BAD: loads every row and every instance; keeps them in the result cache
for invoice in Invoice.objects.filter(status="open"):
    process(invoice)

# GOOD (direct PostgreSQL connection): server-side cursor, bounded chunks, no result cache
qs = (Invoice.objects.filter(status="open")
      .only("id", "total", "customer_id")
      .order_by("pk"))
for invoice in qs.iterator(chunk_size=2000):
    process(invoice)

# GOOD (works behind PgBouncer transaction mode; short transactions; restartable)
def iter_batches(qs, size=2000):
    last_pk = 0
    while True:
        batch = list(qs.filter(pk__gt=last_pk).order_by("pk")[:size])
        if not batch:
            return
        yield batch
        last_pk = batch[-1].pk
```

### Long-running commands and threads: connections and DEBUG

```python
from django.db import close_old_connections, reset_queries

class Command(BaseCommand):
    def handle(self, *args, **opts):
        for batch in iter_batches(Event.objects.filter(processed=False)):
            close_old_connections()   # drop connections past CONN_MAX_AGE or broken; Django only does this per request
            handle_batch(batch)
            reset_queries()           # no-op unless DEBUG=True, where connection.queries grows otherwise
```

Threads you start yourself (and `ThreadPoolExecutor` workers) get their own DB connections that
Django never closes; call `connection.close()` (or `close_old_connections()`) in a `finally`.

### Memory profiling with tracemalloc (stdlib)

```python
import tracemalloc

tracemalloc.start(25)                          # frames per traceback
snap1 = tracemalloc.take_snapshot()
run_suspect_code()                             # e.g. 500 requests via the test client
snap2 = tracemalloc.take_snapshot()

for stat in snap2.compare_to(snap1, "traceback")[:10]:
    print(stat)                                # growth by allocation site
current, peak = tracemalloc.get_traced_memory()
```

tracemalloc slows allocation noticeably (often 2×+, illustrative) — use in dev/staging or
short controlled windows. It does not see C-extension allocations outside Python's allocator.

### memray for peaks and native allocations [third-party: memray]

```bash
memray run -o export.bin manage.py export_orders --month 2026-10
memray flamegraph export.bin          # where the peak was allocated
memray run --native ...               # include C allocations (psycopg, lxml, Pillow)
memray attach <pid>                   # Linux: attach to a running process (staging)
```

Other tools: `objgraph` [third-party: objgraph] for "what keeps this alive"
(`objgraph.show_growth()`, `show_backrefs`), `guppy3` [third-party: guppy3] heap summaries.
Production: track RSS per worker in metrics; take heap snapshots on a single canary, not fleet-wide.

### Measuring per-request memory growth

```python
import resource, logging
log = logging.getLogger("perf.memory")

class MaxRSSMiddleware:   # staging diagnostic; ru_maxrss is KiB on Linux, bytes on macOS
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        response = self.get_response(request)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if after > before:
            log.info("maxrss +%d KiB on %s", after - before, request.resolver_match.view_name if request.resolver_match else request.path)
        return response
```

Peak RSS only moves on new highs — it identifies which endpoints set the high-water mark.

### Uploads and downloads

```python
# settings.py
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440      # default; lower to stream to disk sooner
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440      # bound non-file bodies; raise only per need
FILE_UPLOAD_TEMP_DIR = "/tmp/uploads"        # ensure disk space / emptyDir sized accordingly

# Download: stream from storage in chunks instead of reading into memory
from django.http import FileResponse
def download(request, pk):
    doc = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    return FileResponse(doc.file.open("rb"), as_attachment=True, filename=doc.filename)
```

Prefer pre-signed object-storage URLs for large files so the worker is not involved at all.
Chunked CSV/Excel generation and ingestion patterns: [file-and-data-processing-performance.md](file-and-data-processing-performance.md).

### Bounded in-process caches

```python
from functools import lru_cache

@lru_cache(maxsize=512)       # never maxsize=None for keys derived from user input
def tax_rules(region: str, year: int) -> TaxRules: ...
```

Monitor `tax_rules.cache_info()` in staging to choose `maxsize`.

### Worker recycling and GC around fork

```python
# gunicorn.conf.py
max_requests = 2000            # illustrative; set from the measured RSS growth slope
max_requests_jitter = 200      # stagger restarts across workers
preload_app = True             # share imported code pages; incompatible with per-worker init that needs fork safety

def when_ready(server):
    import gc
    gc.freeze()                # Python ≥ 3.7: move current objects to a permanent generation to reduce CoW
```

`preload_app` imports Django in the master; DB connections or threads created at import time
would be shared across forks — ensure none are opened before fork.

```python
# Celery settings — recycle children that leak or fragment
CELERY_WORKER_MAX_TASKS_PER_CHILD = 500
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 400_000   # KiB; child replaced after the current task exceeds it
```

Recycling is a containment measure. Record that a leak exists and keep investigating.

## 6. Production use cases

1. **Monthly export OOM.** A Celery task built a list of 3M dicts for CSV (≈ 4 GB RSS,
   illustrative) and was killed at the 2 GiB limit. Fix: `values_list().iterator(chunk_size=5000)`
   streamed to a temp file and uploaded to object storage; peak RSS 180 MB. Validated with memray
   peak comparison and a full-size staging run.
2. **Slow leak in web workers.** RSS grew ~2 MB per 1,000 requests without plateau. tracemalloc
   diff pointed to a module-level dict caching per-user permission sets. Fix: move to Redis with
   TTL; `max_requests` kept as a guard.
3. **PgBouncer migration broke exports.** After moving to transaction pooling, `iterator()` raised
   cursor errors. Set `DISABLE_SERVER_SIDE_CURSORS` and switched exports to keyset batches.
4. **Upload spikes.** Mobile clients uploaded 40 MB images; a custom view read `request.body`,
   holding the whole body plus a decoded copy. Fix: multipart upload with temp-file handler and
   direct-to-storage uploads for large files.
5. **DEBUG in a cron container.** A nightly command ran with `DEBUG=True` from a mis-set env var;
   `connection.queries` grew to millions of strings. Fix: settings validation at startup.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Streaming/iterator processing | Bounded memory regardless of data size | Longer-held DB snapshots; harder retries mid-stream |
| Worker recycling | Simple, reliable containment | Hides root cause; restart latency; cold caches |
| tracemalloc | Stdlib, precise allocation sites | Overhead; Python-only allocations |
| memray | Native allocations, peak flame graphs | Third-party; Linux/macOS only; overhead |
| GC tuning | Can reduce pause CPU | Easy to cause memory blow-ups; version-specific |

## 8. Security and performance considerations

- Unbounded uploads and request bodies are a memory-exhaustion DoS vector — limit at the proxy and
  with Django's settings; validate file size before processing.
- Heap snapshots and memory dumps contain secrets and personal data (tokens, PII strings) — treat
  as sensitive artefacts, keep out of public tickets.
- Temp files from uploads may contain sensitive data; ensure cleanup and private temp directories.
- Unbounded caches keyed by user input are both a leak and an attack surface.
- Decompression/image bombs: bound pixel counts (Pillow `Image.MAX_IMAGE_PIXELS`) and archive
  expansion sizes.

## 9. Common mistakes and anti-patterns

- `list(qs)` / `len(qs)` / iterating twice on million-row QuerySets.
- Using `iterator()` behind PgBouncer transaction mode without `DISABLE_SERVER_SIDE_CURSORS`.
- Holding a server-side cursor open for an hour while calling external APIs per row.
- `DEBUG=True` in workers, commands or load tests.
- `lru_cache(maxsize=None)` or module dicts keyed by user/tenant/request data.
- Reading whole files (`f.read()`, `request.body`) to process or return them.
- Treating `max_requests` as the fix for a leak.
- Measuring only Python heap and concluding there is no leak while RSS grows (native leak or
  fragmentation).
- Setting container memory limit = average RSS × workers with no headroom for spikes.
- Threads/commands that never close their DB connections.

## 10. Troubleshooting and validation

| Symptom | Diagnosis steps |
|---|---|
| OOM kills (exit 137, `OOMKilled`) | Correlate with endpoint/task logs at the time; check which process (master vs worker vs child); memray/tracemalloc the suspect path in staging with production-size data |
| RSS climbs, never plateaus | Leak: tracemalloc snapshot diff after N repeated requests; objgraph `show_growth()`; check DEBUG and module-level caches |
| RSS rises to a level and stays | Fragmentation/high-water: find the peak allocator (memray); reduce peak; recycling acceptable |
| Memory spikes on specific endpoint | Query count/rows fetched; payload size; upload handling |
| `too many open files` | `ls /proc/<pid>/fd | wc -l`; unclosed files, sockets, HTTP clients created per call |
| Connections grow in long-running process | `pg_stat_activity` by `application_name`/`backend_start`; missing `close_old_connections()` |

Validation of a memory fix:

1. Reproduce with production-like data volume (row counts and blob sizes matter).
2. Record peak RSS and tracemalloc/memray peak before and after.
3. Soak test (hours) and plot RSS vs requests/tasks — should plateau
   ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).
4. Confirm no regression in duration (iterator/keyset add queries) and DB transaction duration.
5. Set alerts on RSS percentage of container limit and OOM events.

## 11. Related references and skills

- [file-and-data-processing-performance.md](file-and-data-processing-performance.md) — streaming files, CSV/Excel, bulk ingestion.
- [orm-performance-optimization.md](orm-performance-optimization.md) — `iterator()`, `only()`, `values()` from the query-cost angle.
- [database-performance.md](database-performance.md) — PgBouncer, connection lifecycle, long transactions.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — worker counts vs memory budget.
- [background-task-performance.md](background-task-performance.md) — Celery task memory, chunking.
- [profiling-and-observability.md](profiling-and-observability.md) — CPU profiling, production metrics.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — memory-growth incidents.
- [django-orm](../../django-orm/SKILL.md) — QuerySet internals and result caching.
- [django-core](../../django-core/SKILL.md) — file uploads and storage mechanics.
- [django-data-processing](../../django-data-processing/SKILL.md) — import/export pipelines.

## 12. Official documentation and external references

- Django `iterator()`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator
- Server-side cursors / PgBouncer: https://docs.djangoproject.com/en/5.2/ref/databases/#transaction-pooling-server-side-cursors
- File uploads and handlers: https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- Upload settings: https://docs.djangoproject.com/en/5.2/ref/settings/#file-upload-max-memory-size
- `close_old_connections` / connection management: https://docs.djangoproject.com/en/5.2/ref/databases/#connection-management
- Python `tracemalloc`: https://docs.python.org/3/library/tracemalloc.html
- Python `gc`: https://docs.python.org/3/library/gc.html
- Python memory management: https://docs.python.org/3/c-api/memory.html
- memray [third-party]: https://bloomberg.github.io/memray/
- objgraph [third-party]: https://mg.pov.lt/objgraph/
- Gunicorn `max_requests`, `preload_app`: https://docs.gunicorn.org/en/stable/settings.html
- Celery worker settings: https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker
- psycopg server-side cursors: https://www.psycopg.org/psycopg3/docs/advanced/cursors.html
