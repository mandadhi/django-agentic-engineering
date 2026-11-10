# Large Dataset Processing

> **Scope:** Processing datasets that do not fit comfortably in memory or in one request: generators,
> chunking, streaming reads/writes, `itertools.batched`, `QuerySet.iterator()` limits, keyset
> iteration, incremental and partitioned processing, pandas/Polars/PyArrow streaming,
> multiprocessing/threading/async selection, backpressure, temporary storage and processing limits.
> **Not here:** format-specific parsing ([file-processing-engineering.md](file-processing-engineering.md),
> [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md)), load/UPSERT/COPY
> mechanics ([database-loading-and-persistence.md](database-loading-and-persistence.md)), measuring and
> sizing ([data-processing-performance.md](data-processing-performance.md)), Celery task design
> ([django-background-processing.md](django-background-processing.md)), ORM bulk internals
> ([django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md)).

## 1. Purpose and scope

"Large" means any dataset where the naive approach — `list(reader)`, `pd.read_csv(path)`,
`list(Model.objects.all())` — either exceeds the worker's memory budget, exceeds a request/task time
limit, or holds a database resource (lock, snapshot, connection) longer than the system tolerates.
In practice that threshold is far lower than people expect: a 500 MB CSV parsed into Python dicts
commonly occupies several GB (VERIFY: the expansion factor depends on column count and types —
measure with `tracemalloc`, see [data-processing-performance.md](data-processing-performance.md)).

The governing rule of this reference: **memory must be bounded by the chunk size, not by the dataset
size.** Every pattern below exists to keep peak resident memory roughly constant as input grows from
10 k to 100 M rows. Never load an entire large dataset into memory, even "temporarily".

This file covers:

- Streaming reads and writes, generator pipelines, `itertools.batched`.
- Batch and chunk processing; consequences of bulk DB operations (not their mechanics).
- Iterating large tables: `QuerySet.iterator()` vs keyset pagination.
- Incremental and partitioned processing (tenant, date, hash range).
- Parallelism: processes, threads, asyncio — and when none of them help.
- Backpressure, resource management, temporary storage, processing limits.

## 2. Core concepts

| Concept | Meaning | Bound it by |
|---|---|---|
| Streaming read | Consume input incrementally from a file handle / cursor / HTTP body | Buffer size, chunk size |
| Chunk | A unit of work read and transformed together (e.g. 10 000 rows) | Memory budget per worker |
| Batch | A unit of work persisted together (one `bulk_create`, one transaction) | Lock time, WAL, retry cost |
| Generator pipeline | Chain of lazy iterators: `parse → validate → transform → batch → load` | One item / one batch in flight |
| Keyset iteration | `WHERE pk > :last ORDER BY pk LIMIT n` loops | Index on the key |
| Incremental processing | Only rows changed since a watermark | Watermark correctness (see [etl-and-elt-pipelines.md](etl-and-elt-pipelines.md)) |
| Partitioned processing | Split the dataset into independent slices (tenant/date/hash) | Partition skew |
| Backpressure | A slow consumer slows the producer instead of buffering unboundedly | Bounded queues |
| Processing limit | Hard caps: rows, bytes, runtime, error count | Configuration per job type |

Chunk and batch are often the same number but need not be: you may parse in chunks of 50 000 rows
(cheap, in memory) and commit in batches of 2 000 (bounded lock duration). Keep them separate
parameters.

## 3. Internal mechanics

### 3.1 Generators and the iterator protocol

A generator function [stdlib] returns an object whose `__next__` runs the body until the next
`yield`, then suspends with its frame (locals, instruction pointer) preserved. Chaining generators
means only one item travels through the whole pipeline at a time; the cost is per-item Python call
overhead (~tens of ns per `next`, VERIFY: on your interpreter), which is negligible compared with
parsing and DB I/O.

`itertools.batched(iterable, n)` [stdlib] (Python ≥ 3.12) pulls `n` items and yields them as a
**tuple**; the last tuple may be shorter. It never materialises more than one batch.

### 3.2 What `QuerySet.iterator()` actually does

- Normal iteration fills `QuerySet._result_cache` with every model instance — memory grows with the
  table. `iterator(chunk_size=N)` [Django] bypasses the cache.
- On PostgreSQL it uses a **server-side cursor** (named cursor) and fetches `chunk_size` rows per
  round trip. With PgBouncer in transaction-pooling mode you must set
  `DISABLE_SERVER_SIDE_CURSORS = True`, in which case the driver fetches the **whole result set**
  client-side and only Django's object construction is chunked (VERIFY: confirm driver buffering for
  your psycopg version).
- `prefetch_related` is honoured only when `chunk_size` is given (Django ≥ 4.1), one prefetch per
  chunk.
- The cursor lives inside a transaction (explicit, or implicit for the cursor's lifetime under
  autocommit — VERIFY: exact semantics for your backend/version). A long iteration therefore pins an
  MVCC snapshot: VACUUM cannot remove dead tuples newer than it, and connection poolers keep a
  connection busy for the whole run.
- Writing to the same table while iterating it is safe on PostgreSQL (snapshot isolation) but you
  will not see your own writes in the iteration.

### 3.3 Keyset pagination

`Model.objects.filter(pk__gt=last_pk).order_by("pk")[:n]` compiles to an index range scan that
starts exactly where the previous page ended. Each page is a short, independent query: no long
snapshot, works through PgBouncer, resumable from a stored `last_pk`. Contrast with `OFFSET`,
which must scan and discard all skipped rows — page 10 000 costs 10 000× page 1.

### 3.4 Processes, threads, and the GIL

CPython's GIL lets one thread execute bytecode at a time (free-threaded builds exist from
Python 3.13 as an experimental option — VERIFY: status before relying on it). Threads therefore speed up
**waiting** (network, disk, DB) but not pure-Python CPU work. C extensions that release the GIL
(pyarrow, Polars, many pandas operations, `hashlib` on large buffers) can use threads in parallel.
Processes get separate interpreters and true parallelism, paid for by pickling arguments/results
across the process boundary and by per-process memory.

`fork` copies the parent's open sockets. A forked child that reuses the parent's PostgreSQL
connection corrupts the protocol stream for both — hence `django.db.connections.close_all()` before
creating the pool, and each child opening its own connection lazily.

### 3.5 asyncio

An event loop interleaves coroutines only at `await` points. It improves throughput only when the
work is dominated by concurrent I/O waits (many HTTP calls, many object-storage GETs). CPU-bound
parsing inside a coroutine blocks the whole loop. Django's async ORM API still executes sync
database drivers in a thread pool (VERIFY: per Django version), so async ORM loops are not faster
than sync ones. See [django-core asynchronous Django](../../django-core/references/asynchronous-django.md).

## 4. Engineering decisions and trade-offs

### 4.1 When to use each tool — limitations and trade-offs

| Tool | Use when | Limitations / trade-offs |
|---|---|---|
| Python generators [stdlib] | Default for any row-oriented pipeline; CSV/JSONL/XML record streams | Single-pass (cannot rewind; re-open the source); exceptions surface lazily at consumption time; per-row Python overhead caps throughput at roughly 10⁴–10⁵ rows/s/core (VERIFY: by benchmark); hidden `list()` calls defeat them |
| `QuerySet.iterator(chunk_size=…)` [Django] | Read-only scan of a large table in one short-to-medium job | Holds a server-side cursor and snapshot for the whole scan (blocks vacuum, ties up a pooled connection); not resumable; incompatible with PgBouncer transaction pooling unless server-side cursors are disabled (then whole result buffered client-side); `prefetch_related` only with `chunk_size`; no result cache, so re-iteration re-queries |
| Keyset pagination | Long jobs, resumable jobs, PgBouncer, concurrent writers | Needs a unique, indexed, monotonic sort key; rows inserted behind the cursor are missed (use a watermark); composite keys need tuple comparison |
| `bulk_create()` [Django] | Inserting thousands+ new rows per batch | No `save()`, no `pre_save`/`post_save` signals, no `full_clean()`; no M2M; not for multi-table-inheritance children; `ignore_conflicts=True` leaves PKs unset; `update_conflicts` fails if the same key appears twice in one batch (PostgreSQL); one huge call = one huge statement + long transaction — always pass `batch_size` |
| `bulk_update()` [Django] | Updating a modest number of known objects with per-row different values | No `save()`/signals; cannot update PK; generates `CASE WHEN pk=… THEN …` per field — SQL size and planning cost grow with batch size, keep batches modest (hundreds to low thousands); duplicates in the list: only first applied (VERIFY: per Django version); for very large updates load to staging and do a set-based `UPDATE … FROM` instead |
| pandas chunking [third-party: pandas] | Vectorised transforms on CSV/JSONL where column operations dominate | `read_csv(chunksize=N)` yields DataFrames but `read_excel` cannot chunk; numerics default to float (money corruption — use `dtype=str` then `Decimal`); dtype inference can differ between chunks; each chunk still costs several × raw size; adds a heavy dependency |
| Polars lazy execution [third-party: polars] | Columnar filter/aggregate/join over files larger than RAM, then write a file | Streaming engine does not support every operation (falls back to in-memory — VERIFY: per version); API for streaming collect changed (`collect(streaming=True)` vs `collect(engine="streaming")` — VERIFY: for your Polars version); results must still be fed into Django in batches; not row-by-row business validation friendly |
| PyArrow streaming [third-party: pyarrow] | Parquet/Arrow/CSV record batches, columnar handoff, zero-copy to Polars | `iter_batches` row-group granularity — a giant row group is still loaded whole; conversion `to_pylist()` per batch reintroduces Python overhead; schema strictness surprises on dirty CSV |
| multiprocessing (`ProcessPoolExecutor`) [stdlib] | CPU-bound per-chunk work (parsing, hashing, heavy transforms) on one machine | Pickling cost for arguments/results; memory × workers; must close DB connections before fork; each worker opens a DB connection (count against `max_connections`); errors and timeouts harder to manage; inside Celery prefork workers, nested pools are usually wrong — scale Celery workers instead |

### 4.2 Choosing an iteration strategy

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Scan 2 M rows once, finishes in < 5 min, direct DB connection | `iterator(chunk_size=2000)` | PgBouncer transaction pooling; job may run hours |
| Nightly job over 80 M rows that must resume after crash | Keyset pagination + checkpoint `last_pk` | Sort key not unique/indexed — add one |
| Only changed rows since last run | Incremental by `updated_at` watermark + pk tiebreak | Clock skew / late commits — use overlap window |
| 400 tenants, one big table | Partition by tenant, one task per tenant | Heavy skew (one tenant = 60 %) → sub-partition by hash/pk range |
| CPU-heavy transform per file | Process pool or more Celery workers | Work is DB-bound — more processes only add contention |
| 5 000 HTTP enrichments | Threads or asyncio with semaphore | Provider rate limit — throttle, see [data-ingestion-engineering.md](data-ingestion-engineering.md) |

### 4.3 Single big job vs many small jobs

Partitioned small jobs give retry granularity, parallelism and progress visibility; they cost
orchestration, more DB connections and the need for a final aggregation/reconciliation step.
Prefer partitions sized to finish in minutes, not hours (task time limits, deploy restarts).

## 5. Django/Python implementation patterns

### 5.1 Generator pipeline with `itertools.batched`

```python
import csv
import io
from collections.abc import Iterable, Iterator
from itertools import batched  # [stdlib] Python >= 3.12
from typing import IO

def read_rows(binary: IO[bytes]) -> Iterator[tuple[int, dict[str, str]]]:
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")  # [stdlib]
    for line_no, row in enumerate(csv.DictReader(text), start=2):
        yield line_no, row

def transform(rows: Iterable[tuple[int, dict[str, str]]]) -> Iterator[RowResult]:
    for line_no, raw in rows:
        try:
            yield RowResult.ok(line_no, to_domain(raw))      # pure function, no I/O
        except DataError as exc:                               # permanent, row-level
            yield RowResult.rejected(line_no, raw, str(exc))

def run(binary: IO[bytes], loader: "BatchLoader", *, batch_size: int = 2000) -> Stats:
    stats = Stats()
    for batch in batched(transform(read_rows(binary)), batch_size):
        good = [r.value for r in batch if r.ok]
        bad = [r for r in batch if not r.ok]
        loader.load(good)                  # one transaction per batch
        loader.reject(bad)                 # RejectedRecord rows, see validation reference
        stats.add(len(good), len(bad))
    return stats
```

Error strategy: row-level `DataError` is permanent → reject and continue; `OperationalError`
from the loader is transient → let it propagate so the task retries from the last checkpoint
(see [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md)).

### 5.2 Keyset iteration helper (resumable)

```python
from collections.abc import Iterator
from django.db.models import Model, QuerySet

def iter_keyset[M: Model](qs: QuerySet[M], *, page: int = 2000,
                          start_after: int | None = None) -> Iterator[list[M]]:
    """Yield pages ordered by pk. Each page is an independent short query."""
    last = start_after
    base = qs.order_by("pk")
    while True:
        chunk = list(base.filter(pk__gt=last)[:page] if last is not None else base[:page])
        if not chunk:
            return
        yield chunk
        last = chunk[-1].pk

# usage: checkpoint after every page so a crash resumes, not restarts
for rows in iter_keyset(Invoice.objects.filter(tenant_id=t).only("pk", "total", "status"),
                        start_after=job.checkpoint_pk):
    process(rows)
    ProcessingJob.objects.filter(pk=job.pk).update(checkpoint_pk=rows[-1].pk)
```

Use `.only()` / `.values_list()` to shrink rows; model instances cost far more memory than tuples.

### 5.3 Partitioning by hash range

```python
def hash_partitions(n: int) -> list[tuple[int, int]]:
    return [(i, n) for i in range(n)]

# SQL-side: WHERE mod(abs(hashtext(external_id)), %(n)s) = %(i)s   -- PostgreSQL hashtext is
# internal/undocumented (VERIFY: stability across PG versions) — prefer pk ranges: split [min_pk, max_pk] into n ranges.
def pk_ranges(lo: int, hi: int, parts: int) -> list[tuple[int, int]]:
    step = max(1, (hi - lo + parts) // parts)
    return [(s, min(s + step - 1, hi)) for s in range(lo, hi + 1, step)]
```

PK ranges are cheap, index-friendly and deterministic; with sparse PKs some ranges are empty —
acceptable. Fan out one task per range ([django-background-processing.md](django-background-processing.md)).

### 5.4 Multiprocessing CPU-bound chunks safely

```python
from concurrent.futures import ProcessPoolExecutor, as_completed  # [stdlib]
from django.db import connections  # [Django]

def parse_chunk(path: str, start: int, end: int) -> list[tuple]:
    # child: pure CPU work on a byte range; return compact tuples, not model instances
    ...

def parallel_parse(path: str, ranges: list[tuple[int, int]], workers: int = 4) -> Iterator[list[tuple]]:
    connections.close_all()                          # never share sockets across fork
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(parse_chunk, path, s, e) for s, e in ranges]
        for fut in as_completed(futures):
            yield fut.result()                       # parent does DB writes, in batches
```

Pass **paths and offsets**, not data, to children; return compact tuples. Pickling a 50 MB list
of dicts each way can cost more than the parse itself. Keep DB writes in the parent (or give each
child its own connection and count them against `max_connections`).

### 5.5 Bounded concurrency for I/O (threads or asyncio)

```python
import asyncio

async def fetch_all(keys: list[str], client, limit: int = 16) -> list[bytes]:
    sem = asyncio.Semaphore(limit)                   # [stdlib] caps in-flight requests
    async def one(k: str) -> bytes:
        async with sem:
            return await client.get_object(k)       # client API illustrative
    return await asyncio.gather(*(one(k) for k in keys))
```

For 100 k keys, do not create 100 k coroutines at once — feed keys through a bounded
`asyncio.Queue(maxsize=…)` with N consumer tasks so memory stays bounded. The thread equivalent is
`ThreadPoolExecutor(max_workers=N)` with submissions throttled by a `queue.Queue(maxsize=…)`.

### 5.6 Backpressure with a bounded queue

```python
import queue, threading

def producer_consumer(source: Iterator[tuple], sink, *, maxsize: int = 8) -> None:
    q: queue.Queue[tuple | None] = queue.Queue(maxsize=maxsize)   # bounded => producer blocks
    def produce() -> None:
        try:
            for batch in batched(source, 2000):
                q.put(batch)          # blocks when the loader falls behind
        finally:
            q.put(None)
    t = threading.Thread(target=produce, daemon=True)
    t.start()
    while (batch := q.get()) is not None:
        sink.load(batch)
    t.join()
```

Queue-level backpressure in Celery is different: the broker queue is effectively unbounded, so
the producer must check depth or use chained/scheduled fan-out instead of enqueuing 1 M tasks at once
(see [data-processing-performance.md](data-processing-performance.md#54-queue-saturation)).

### 5.7 Streaming writes and temporary storage

```python
import tempfile, csv, io
from django.core.files import File
from django.core.files.storage import default_storage

def write_export(rows: Iterator[tuple], name: str) -> str:
    # spool in RAM up to 8 MB, then roll over to disk automatically
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as tmp:  # [stdlib]
        text = io.TextIOWrapper(tmp, encoding="utf-8", newline="", write_through=True)
        w = csv.writer(text)
        for r in rows:
            w.writerow(r)
        text.flush(); text.detach()          # keep tmp open after wrapper is gone
        tmp.seek(0)
        return default_storage.save(name, File(tmp))   # streamed upload by the backend
```

Use `NamedTemporaryFile(dir=settings.FILE_UPLOAD_TEMP_DIR, delete=True)` when a library needs a
real path (e.g. some Excel or Parquet writers). Always use context managers so files are removed
on exception; workers that are SIGKILLed leak temp files — run a periodic sweeper on the temp dir.
Temp-file permissions and secure deletion: [data-processing-security.md](data-processing-security.md).

### 5.8 Processing limits

```python
from dataclasses import dataclass
import time

@dataclass(frozen=True)
class Limits:
    max_rows: int = 5_000_000
    max_bytes: int = 2 * 1024**3
    max_seconds: int = 3 * 3600
    max_error_rows: int = 10_000          # stop early: a 100 % bad file is a mapping problem

class LimitExceeded(Exception): ...       # permanent: fail the job, do not retry

def enforce(limits: Limits, *, rows: int, errors: int, started: float) -> None:
    if rows > limits.max_rows: raise LimitExceeded(f"row limit {limits.max_rows}")
    if errors > limits.max_error_rows: raise LimitExceeded("too many invalid rows")
    if time.monotonic() - started > limits.max_seconds: raise LimitExceeded("runtime limit")
```

Check byte limits **before** parsing (object metadata / `Content-Length`) and enforce row/runtime
limits inside the loop. Limits are per job type and per tenant plan.

## 6. Production use cases

- **Payroll (12 M timesheet rows/month):** keyset-iterate `TimeEntry` per tenant/pay period, compute
  gross pay with `Decimal` in a generator, write `PayslipLine` in 2 000-row batches; checkpoint pk so
  a deploy restart resumes rather than double-pays.
- **Product catalog feed (3 GB CSV from supplier):** stream from object storage, Polars lazy scan to
  dedupe and filter to changed SKUs, `sink_parquet` to temp, then PyArrow `iter_batches` into the
  loader. Python only touches changed rows.
- **Hospital doctor roster sync:** small (40 k rows) but nightly; plain generators + `bulk_update`
  of changed fields only — no pandas needed.
- **Inventory snapshot per warehouse:** partition by warehouse id, one Celery task each; skewed
  central warehouse sub-partitioned by pk range.
- **Customer enrichment via external API:** 200 k records, asyncio with `Semaphore(20)` respecting
  provider rate limits; results written back in batches through a bounded queue.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Generators + batching | Constant memory, no dependencies, easy to test per stage | Python-speed per row; single pass |
| Columnar engines (Polars/PyArrow) | 10–100× faster set operations, larger-than-RAM | Extra dependency, harder row-level error reporting |
| Partitioned tasks | Parallel, retry per slice, progress visible | Orchestration, skew, final reconciliation needed |
| Multiprocessing | True CPU parallelism | Memory × N, pickling, fork/DB pitfalls |
| asyncio/threads | Cheap concurrency for I/O | No CPU gain; harder debugging; must bound concurrency |

## 8. Security and performance considerations

**Security**
- Processing limits (bytes, rows, runtime, decompressed size) are a DoS control, not just hygiene;
  enforce them before and during parsing ([data-processing-security.md](data-processing-security.md)).
- Temp files hold sensitive data: create with `tempfile` (mode 0600 on POSIX), in a dedicated
  directory, deleted in `finally`/context managers; never use predictable names.
- Partitioned tasks must carry and enforce `tenant_id`; never derive tenant from file content.
- Child processes inherit environment and secrets — do not pass credentials as task arguments.

**Performance**
- Chunk size dominates memory; batch size dominates DB lock time and round trips. Find both
  empirically: [data-processing-performance.md](data-processing-performance.md).
- Fetch only needed columns (`.only()`, `values_list()`, `usecols=`, `columns=`).
- Bulk ops consequences: one `bulk_create` of 1 M objects builds a gigantic statement and
  transaction — always `batch_size`; index maintenance cost scales with rows, see
  [database-loading-and-persistence.md](database-loading-and-persistence.md).
- Disable `DEBUG` in workers: `connection.queries` accumulates every SQL string and leaks memory
  in long jobs (call `django.db.reset_queries()` if DEBUG must be on).

## 9. Common mistakes and anti-patterns

- `list(Model.objects.all())` / `pd.read_csv(big)` "just to count" → OOM kill → stream and count, or `COUNT(*)`.
- `for obj in qs:` on 10 M rows → result cache holds everything → `iterator(chunk_size=…)` or keyset.
- `OFFSET` pagination over a big table → quadratic runtime → keyset pagination.
- `iterator()` behind PgBouncer transaction pooling → cursor errors or full client buffering → `DISABLE_SERVER_SIDE_CURSORS` + keyset.
- Holding one `transaction.atomic()` around a 3-hour loop → bloat, lock waits, nothing committed on crash → commit per batch with checkpoints.
- Forking with open DB connections → "server closed the connection unexpectedly"/corrupted results → `connections.close_all()` before the pool.
- Process pool inside a Celery prefork worker → N×M processes, connection exhaustion → scale Celery concurrency instead.
- `asyncio.gather` over 1 M coroutines → memory blowup and provider bans → semaphore + bounded queue.
- Enqueuing a task per row → broker saturation, huge overhead → task per chunk/partition.
- pandas default float for money → rounding errors → `dtype=str`, convert to `Decimal`.
- Assuming `bulk_create` fires signals (search index, audit) → silent drift → call the side effect explicitly per batch.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Worker OOM-killed mid-file | Hidden materialisation (`list()`, `read_excel`, result cache, DEBUG query log) | `tracemalloc` snapshot diff; grep for `list(`/`.all()` loops |
| Job slows down page after page | OFFSET pagination, or growing in-memory dedupe set | `EXPLAIN` the page query; bound/persist dedupe state |
| Table bloat / vacuum lag during job | Long-lived snapshot (`iterator()`, long transaction) | `pg_stat_activity.xact_start`; switch to keyset + short transactions |
| `cursor "_django_curs_…" does not exist` | Server-side cursor through transaction pooler | `DISABLE_SERVER_SIDE_CURSORS = True` |
| Parallel run slower than serial | DB-bound work, lock contention, pickling cost | Profile parent vs child time; reduce workers |
| Temp disk fills | Leaked temp files after SIGKILL | Sweeper job; check `FILE_UPLOAD_TEMP_DIR` usage |
| Rows missing in incremental run | Watermark without overlap / late commits | Overlap window + idempotent load |

Validation checklist:
- [ ] Peak RSS measured at 1×, 10× input size is roughly flat.
- [ ] No `list()`/`read_excel`/unbounded set in the hot path.
- [ ] Chunk size and batch size are configurable and separately set.
- [ ] Job resumes from checkpoint after `kill -9` without duplicates.
- [ ] Limits (rows, bytes, runtime, errors) configured and tested.
- [ ] DB connections closed before fork; worker connection count ≤ budget.

## 11. Related references and skills

- [file-processing-engineering.md](file-processing-engineering.md) — streaming CSV/Excel/JSON/XML parsing.
- [columnar-archive-and-document-files.md](columnar-archive-and-document-files.md) — Parquet/Avro/ORC/ZIP/PDF.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — batch commits, UPSERT, COPY.
- [data-processing-performance.md](data-processing-performance.md) — sizing, profiling, capacity planning.
- [django-background-processing.md](django-background-processing.md) — fan-out, checkpoints, time limits.
- [processing-reliability-and-recovery.md](processing-reliability-and-recovery.md) — resume and error taxonomy.
- [django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md),
  [queryset internals](../../django-orm/references/queryset-internals.md).
- [django-performance memory management](../../django-performance/references/memory-and-resource-management.md),
  [concurrency and workers](../../django-performance/references/concurrency-and-worker-management.md).
- [django-core asynchronous Django](../../django-core/references/asynchronous-django.md).

## 12. Official documentation and external references

- https://docs.python.org/3/library/itertools.html#itertools.batched
- https://docs.python.org/3/library/concurrent.futures.html
- https://docs.python.org/3/library/asyncio-sync.html#asyncio.Semaphore
- https://docs.python.org/3/library/tempfile.html
- https://docs.python.org/3/library/queue.html
- https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator
- https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-update
- https://docs.djangoproject.com/en/5.2/ref/databases/#transaction-pooling-server-side-cursors
- https://pandas.pydata.org/docs/user_guide/io.html#iterating-through-files-chunk-by-chunk
- https://docs.pola.rs/user-guide/concepts/lazy-api/
- https://arrow.apache.org/docs/python/parquet.html
- https://www.postgresql.org/docs/current/queries-limit.html
