# Data Processing Performance

> **Scope:** Performance engineering for data pipelines and batch jobs: throughput, latency,
> memory, CPU, disk and network I/O; batch/chunk sizing; worker concurrency against database
> limits; database, serialisation, parsing and network bottlenecks; queue saturation and
> backpressure; profiling, benchmarking and capacity planning. **Not here:** general Django
> request/ORM performance ([django-performance](../../django-performance/SKILL.md)), streaming and
> parallelism patterns ([large-dataset-processing.md](large-dataset-processing.md)), load mechanics
> ([database-loading-and-persistence.md](database-loading-and-persistence.md)), metrics and dashboards
> ([data-processing-observability.md](data-processing-observability.md)), performance test design
> ([data-processing-testing.md](data-processing-testing.md)).

## 1. Purpose and scope

Pipeline performance questions are almost always one of three: *Will it finish inside the window?*
*Will it fit in the worker's memory?* *Will it hurt the database everyone else uses?* This
reference gives a measurement-first method to answer them, the main bottleneck classes in
Django data jobs, how to size chunks/batches/workers, and a worked capacity plan.

**Measure before optimising.** Data jobs have counter-intuitive bottlenecks: a "slow parser" is
often a per-row `SELECT` for an FK; a "slow database" is often one unindexed conflict target or a
`bulk_update` with a 10 000-row `CASE`. Optimising without a profile wastes days and adds
complexity (Polars, multiprocessing) that does not move the real constraint.

## 2. Core concepts

| Metric | Definition | Typical unit | Why it matters |
|---|---|---|---|
| Throughput | Records completed per unit time, end-to-end | rows/s | Determines whether the batch window is met |
| Latency | Time from record arrival to availability in target | s / min | Near-real-time SLAs; freshness |
| Peak memory | Maximum resident set size (RSS) of a worker | MB | OOM kills, worker density per node |
| CPU utilisation | Busy fraction of a core per worker | % | CPU-bound vs I/O-bound diagnosis |
| Disk I/O | Temp files, spooling, DB WAL writes | MB/s, IOPS | Hidden cost of temp storage and logging |
| Network I/O | Object storage reads, DB round trips, API calls | MB/s, RTT | Remote storage adds latency per request |
| Queue depth / age | Messages waiting and age of oldest | count, s | Saturation signal |
| Error/retry rate | Retries per 1 000 records | ratio | Retries consume capacity |

**Little's Law** for queues: `in-flight = arrival rate × time in system`. If tasks arrive at
50/s and each takes 4 s, you need ≥ 200 concurrent slots or the queue grows without bound.

**Amdahl's Law:** if 30 % of a job is a serial DB promotion, infinite parallel parsing caps
speed-up at ~3.3×. Find the serial part first.

## 3. Internal mechanics

### 3.1 Where time goes in a typical row pipeline

```text
object storage GET (network) → decode/parse (CPU, C or Python) → validate/transform (Python CPU)
→ FK/reference lookup (DB round trips) → serialise batch (CPU) → INSERT/UPSERT (DB CPU, index
maintenance, WAL fsync) → commit (fsync) → progress update (DB) → next batch
```

Each arrow is a candidate bottleneck. In pure Python row loops, validate/transform frequently
dominates (10–50 µs per row per field rule — VERIFY: by profiling your rules); on DB-heavy loads, index
maintenance and WAL dominate.

### 3.2 Database bottlenecks during load

- **Index maintenance:** every index on the target is updated per inserted row; 8 indexes ≈ 8
  B-tree insertions. GIN/trigram indexes are especially expensive.
- **Constraint checks:** unique checks are index probes; FK checks probe the parent table per row.
- **WAL:** every change writes WAL; `synchronous_commit` waits for flush at each commit — many tiny
  commits are fsync-bound, few huge commits are lock/bloat-bound.
- **Locks:** UPSERTs take row locks; parallel loaders touching overlapping keys in different
  orders deadlock. `bulk_update` on hot rows blocks OLTP traffic.
- **Connections:** each worker holds a connection; PostgreSQL per-connection memory and
  `max_connections` cap useful concurrency long before CPU does.

### 3.3 Serialisation overhead

| Format | Use in pipelines | Cost profile |
|---|---|---|
| JSON [stdlib `json`] | Celery task args, API payloads | Text encode/decode; Decimal/datetime need custom handling; large payloads slow and bloat the broker |
| pickle [stdlib] | multiprocessing args/results | Fast for Python objects, but unsafe for untrusted data; cost scales with object graph size |
| Arrow IPC [third-party: pyarrow] | Columnar handoff between processes/engines | Near zero-copy for columnar data; needs pyarrow on both sides |

Pass **references** (storage key, pk range, job id) between tasks/processes, not data. A Celery
message carrying 10 000 rows of JSON is a broker and memory problem.

### 3.4 Parsing overhead

`csv` [stdlib] is implemented in C for tokenising, but building a `dict` per row (`DictReader`) and
any per-field Python conversion runs at Python speed. pandas' C parser [third-party: pandas] and the
pyarrow/Polars multithreaded CSV readers [third-party: pyarrow] [third-party: polars] (C++/Rust)
tokenise and type-convert in native code, often an order of magnitude faster for wide numeric
files (VERIFY: benchmark with your data). The speed-up evaporates if you then iterate rows in Python
(`itertuples`, `to_pylist`) for business validation — choose the native engine only when the
transformation can be expressed as column operations.

### 3.5 Network overhead

Reading from S3/Azure Blob is per-request latency (tens of ms) plus bandwidth. Small buffered
reads through a file-like wrapper can turn into many ranged GETs (VERIFY: the buffer size of your
django-storages backend). Parquet readers issue range requests for footers and selected column
chunks — efficient when reading few columns, costly with many tiny row groups. Download-to-local-
temp then parse is often faster and more robust for large CSVs than streaming parse over the
network, at the cost of temp disk.

## 4. Engineering decisions and trade-offs

### 4.1 Batch and chunk sizing

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| ORM `bulk_create` / UPSERT | 1 000–5 000 rows per batch | Rows very wide (JSON blobs) → size by bytes, e.g. ≤ 4 MB per statement |
| `bulk_update` | 200–1 000 objects, few fields | Need > 100 k updates → staging + `UPDATE … FROM` |
| Read chunk (parse) | 10 000–100 000 rows, bounded by memory budget | Chunk RSS > 25 % of worker limit |
| COPY | Stream continuously; commit per 100 k–1 M rows into staging | Staging run must be atomic → one COPY per file |
| Celery task granularity | 30 s – 10 min of work per task | < 1 s per task (overhead dominates) or > visibility timeout |
| Progress updates | Once per batch or every N seconds | Per row |

### 4.2 Finding the chunk/batch size empirically

1. Fix a realistic sample (≥ 1 M rows, real width, real dirty-data ratio) and a production-like DB
   (same indexes, similar table size — empty tables lie).
2. Sweep batch sizes geometrically: 250, 500, 1 000, 2 000, 5 000, 10 000.
3. For each, record rows/s, peak RSS, p95 batch commit time, lock waits
   (`pg_stat_activity.wait_event_type = 'Lock'`), WAL generated (`pg_current_wal_lsn()` diff).
4. Throughput rises then plateaus; pick the **smallest size within ~10 % of the plateau** whose
   p95 commit time is within your lock budget (e.g. < 1 s on shared OLTP tables).
5. Re-run the sweep when schema (indexes) or row width changes materially.

### 4.3 Worker concurrency vs database capacity

```text
total_db_connections = Σ (web workers × threads) + Σ (celery workers × concurrency) + admin/cron + headroom
must be ≤ max_connections (or the pooler's server pool size)
```

| Work type | Concurrency guidance | Escalate when |
|---|---|---|
| CPU-bound parsing/transforms | processes ≈ cores per node | RSS × processes exceeds node memory |
| I/O-bound (API, object storage) | threads/async 8–64, bounded by provider limits | Rate-limit errors (429) appear |
| DB-bound loading | Small: 2–8 parallel loaders per DB, partitioned by key | Lock waits, rising commit latency, CPU on DB > 70 % |

Adding loaders past the DB's saturation point *reduces* throughput (lock contention, WAL
contention, context switching). The DB is usually the shared, unscalable resource; protect it with
a dedicated Celery queue whose total concurrency is capped
([concurrency and workers](../../django-performance/references/concurrency-and-worker-management.md)).

### 4.4 Optimisation ladder (cheapest first)

1. Remove per-row queries (batch FK lookups, prefetch reference data).
2. Fetch fewer columns; drop model instances for tuples.
3. Skip no-op writes (row hash, `IS DISTINCT FROM`).
4. Tune batch size; stop per-row progress writes.
5. Move set operations to SQL (staging + set-based promotion) or COPY.
6. Native parsers / columnar engines for column-wise transforms.
7. Parallelise by partition — only after 1–6, and only up to DB capacity.

## 5. Django/Python implementation patterns

### 5.1 Measure a stage: records/s and peak memory

```python
import time, tracemalloc, resource   # [stdlib]; resource is POSIX-only
from contextlib import contextmanager
from collections.abc import Iterator
import structlog                     # [third-party: structlog] — optional, any logger works

log = structlog.get_logger()

@contextmanager
def measured(stage: str, *, rows: list[int]) -> Iterator[None]:
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        _, peak_py = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss   # KB on Linux, bytes on macOS
        log.info("stage_done", stage=stage, rows=rows[0], seconds=round(elapsed, 3),
                 rows_per_s=round(rows[0] / elapsed, 1) if elapsed else None,
                 py_peak_mb=round(peak_py / 2**20, 1), max_rss_kb=rss_kb)
```

`tracemalloc` [stdlib] only sees Python-allocated memory and adds significant overhead (VERIFY:
often 2–4× slower); pandas/pyarrow native buffers are invisible to it — use RSS or memray for
those. Do not leave `tracemalloc` on in production loops; sample one run.

### 5.2 Profiling toolkit

| Tool | Origin | Use for | Notes |
|---|---|---|---|
| `cProfile` + `pstats` | [stdlib] | Deterministic call counts in a management command on a sample file | High overhead on tiny functions; distorts hot loops |
| `py-spy` | [third-party: py-spy] | Sampling profiler attached to a running worker (`py-spy top --pid`, `record -o flame.svg`) | Low overhead; needs ptrace permissions in containers (VERIFY: SYS_PTRACE capability in your runtime) |
| `tracemalloc` | [stdlib] | Python allocation snapshots, diff between batches to find leaks | Python heap only |
| `memray` | [third-party: memray] | Native + Python allocations, flame graphs, peak memory | Linux/macOS only (VERIFY: platform support for your version) |
| `EXPLAIN (ANALYZE, BUFFERS)` | PostgreSQL | Promotion/UPSERT/lookup queries | Run on production-sized data |
| `pg_stat_statements` | PostgreSQL extension | Which statements consume DB time during the job | Must be enabled |

```python
# management command: profile one representative file
import cProfile, pstats

def handle(self, *args, **opts) -> None:
    prof = cProfile.Profile()
    prof.enable()
    run_import_for_file(opts["path"], limit_rows=200_000)
    prof.disable()
    pstats.Stats(prof).sort_stats("cumulative").print_stats(30)
```

Read the profile top-down by cumulative time: the first frame that is *your* code and > 20 % of
the total is the target. Typical findings: `django.db.backends…execute` called once per row
(N+1), `Decimal(str(float))` conversions, regex compiled per row, `copy.deepcopy` of mapping config.

### 5.3 Benchmark harness

```python
from dataclasses import dataclass

@dataclass
class BenchResult:
    batch_size: int
    rows: int
    seconds: float
    peak_rss_mb: float
    p95_commit_ms: float

    @property
    def rows_per_s(self) -> float:
        return self.rows / self.seconds

def sweep(sample_key: str, sizes: tuple[int, ...] = (500, 1000, 2000, 5000, 10000)) -> list[BenchResult]:
    results = []
    for size in sizes:
        reset_target_tables()                 # same starting state each run; same indexes as prod
        warm_cache(sample_key)                # decide explicitly: warm or cold run, and report it
        results.append(run_once(sample_key, batch_size=size))
    return results
```

Benchmark rules: realistic volume (production-sized target table and indexes, not an empty test
DB); report warm vs cold separately (OS page cache and PostgreSQL shared buffers change results
dramatically); run ≥ 3 repetitions and report median and spread; record records/s **and** peak
RSS **and** DB-side metrics; same hardware class as production. Benchmarking methodology in depth:
[performance testing and benchmarking](../../django-performance/references/performance-testing-and-benchmarking.md).

### 5.4 Queue saturation

Saturation shows as growing queue depth and growing age of the oldest message while workers are
100 % busy. Fixes, in order: make tasks cheaper (section 4.4), cap fan-out (enqueue partitions in
waves rather than 1 M tasks at once), route heavy jobs to a dedicated queue so they cannot starve
interactive work, then add workers up to DB capacity. Producer-side backpressure for Celery:

```python
def enqueue_partitions(job_id: int, ranges: list[tuple[int, int]], *, max_outstanding: int = 32) -> None:
    """Called repeatedly by a coordinator task; enqueues only while outstanding work is bounded."""
    outstanding = PartitionRun.objects.filter(job_id=job_id, status__in=["queued", "running"]).count()
    for lo, hi in pending_ranges(job_id, ranges)[: max(0, max_outstanding - outstanding)]:
        pr = PartitionRun.objects.create(job_id=job_id, lo=lo, hi=hi, status="queued")
        transaction.on_commit(lambda pk=pr.pk: process_partition.delay(pk))
```

Broker-level controls (`worker_prefetch_multiplier=1`, queue length limits on RabbitMQ — VERIFY:
`x-max-length` overflow behaviour) belong in
[django-integrations Celery](../../django-integrations/references/celery-and-background-integrations.md).

## 6. Production use cases

### 6.1 Capacity plan: 50 M rows per night

Scenario: ERP→CRM order-line sync, 50 M rows, window 01:00–05:00 (4 h = 14 400 s), shared
PostgreSQL also serving early EU traffic after 04:00.

```text
Required throughput  = 50 000 000 / 14 400 s           ≈ 3 470 rows/s
Safety factor 2×     (retries, skew, growth 6 months)   ≈ 7 000 rows/s target
Measured single loader (staging COPY + UPSERT, batch 2 000, warm):  ~1 800 rows/s end-to-end
                     parse 9 000 rows/s | transform 4 500 rows/s | load 2 400 rows/s  (illustrative)
Bottleneck           = load (DB) then transform (Python)
Loaders needed       = 7 000 / 1 800                     ≈ 4 parallel partitions
DB check             : 4 loaders → DB CPU 55 %, p95 commit 300 ms, no lock waits  → OK
                       6 loaders → DB CPU 85 %, deadlocks on hot customers     → cap at 4
Connections          = 4 loaders + 1 coordinator + 2 headroom = 7 of the 20-connection batch pool
Memory               = 4 × 650 MB peak RSS = 2.6 GB on an 8 GB worker node     → OK
Network              = 50 M × ~220 B ≈ 11 GB from object storage ≈ 0.8 MB/s avg → negligible
Wall time            = 50 M / (4 × 1 800) ≈ 6 950 s ≈ 1 h 56 min  (+ promotion/reconcile ~25 min)
Finish by            ≈ 03:25 → 1.5 h buffer before 05:00
```

Re-plan triggers: row count +30 %, new index on the target, change of DB instance class, p95
nightly duration > 70 % of window. Numbers above are illustrative — replace each with your measured
values (VERIFY: replace all against your benchmark).

### 6.2 Other scenarios

- **Payroll calculation (CPU-bound):** profile showed 70 % in Decimal rule evaluation; precompiled
  rule tables + 4 processes per node gave 3.4× (Amdahl: serial posting step remained).
- **Product catalog import slowed 5× after a release:** `pg_stat_statements` revealed a new trigram
  GIN index on `name`; dropped from the load path by moving search indexing to an async refresh.
- **Hospital attendance feed latency:** 15-minute micro-batches met a 20-minute freshness SLA at a
  fraction of the cost of per-event processing.
- **Inventory export OOM:** tracemalloc diff showed `connection.queries` growth — `DEBUG=True` on
  the worker.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Measure-first profiling | Targets the real bottleneck | Needs realistic data and environment |
| Larger batches | Fewer round trips, less commit overhead | Longer locks, more memory, bigger retry unit |
| More workers | Higher throughput while not DB-bound | Connection pressure, contention, cost |
| Native parsers/columnar | Large CPU savings for column ops | Dependency weight; Python row loops negate gains |
| Background + queues | Smooths load, isolates from web | Queue saturation risk; needs monitoring |
| Capacity planning | Predictable windows | Must be re-done as volumes and schema change |

## 8. Security and performance considerations

**Security**
- Profiling artefacts (py-spy dumps, memray files, `cProfile` stats with arguments, sample data
  sets) may contain PII or secrets — store them like production data and delete after analysis.
- Benchmark with masked/synthetic data where possible
  ([data-processing-security.md](data-processing-security.md)); never copy production payroll or
  patient data to a laptop to benchmark.
- `py-spy`/ptrace capabilities in production containers widen the attack surface; grant them
  temporarily.
- Never use pickle for data crossing a trust boundary (broker messages from other systems); JSON or
  Arrow only.
- Processing limits (rows/bytes/runtime) double as DoS protection.

**Performance**
- Set an explicit memory budget per worker and alert at 80 % of it.
- Isolate batch workloads: separate Celery queue, capped concurrency, optionally a read replica for
  extraction (mind replication lag) and off-peak scheduling.
- Turn off `DEBUG` in workers; avoid per-row logging at INFO (log per batch).
- Re-run the batch-size sweep after schema or infrastructure changes.

## 9. Common mistakes and anti-patterns

- Optimising the parser without profiling → no gain, the bottleneck was per-row FK lookups → profile first.
- Benchmarking against an empty local DB → 10× optimistic numbers → production-sized tables and indexes.
- Reporting only warm-cache runs → nightly cold run misses the window → report warm and cold.
- Measuring rows/s without memory → fast but OOM at 3× volume → record peak RSS alongside.
- Adding workers until the DB falls over → throughput drops, OLTP impact → cap loaders by measured DB saturation.
- One Celery task per row → broker overhead dominates → tasks of 30 s–10 min.
- Passing row data through the broker → huge messages, memory → pass storage keys and ranges.
- Per-row progress `UPDATE` → doubles write load → per batch or time-based.
- Using pandas for row-by-row validation → slower than plain csv → native engines only for column ops.
- `tracemalloc` left enabled in production → large slowdown → sample runs only.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Throughput drops over the run | Index bloat, growing in-memory state, OFFSET paging | Per-batch timings; `n_dead_tup`; keyset paging |
| High CPU, low DB load | Python transform bound | cProfile/py-spy; vectorise or parallelise by process |
| Low CPU, low DB load | Waiting on network/storage | py-spy shows I/O waits; download to temp; concurrency for I/O |
| DB CPU high, lock waits | Too many loaders / overlapping keys | Reduce concurrency; partition by key; sort batches |
| RSS grows steadily | Leak: DEBUG query log, caches, unbounded sets | `tracemalloc` snapshot diff between batches |
| Queue age grows nightly | Arrival > capacity | Little's Law calc; cheaper tasks, more workers within DB budget |
| Commit p95 spikes | WAL/fsync pressure, checkpoints | DB metrics; batch size; schedule off-peak |

Validation checklist:
- [ ] Baseline recorded: rows/s, peak RSS, p95 batch time, DB CPU, for a realistic sample.
- [ ] Bottleneck identified by profile before any optimisation.
- [ ] Batch size chosen by sweep; documented with date and schema version.
- [ ] Total DB connections from all workers ≤ budget.
- [ ] Capacity plan shows ≥ 30–50 % headroom in the processing window.
- [ ] Performance regression test on a fixed sample in CI or nightly
  ([data-processing-testing.md](data-processing-testing.md)).

## 11. Related references and skills

- [large-dataset-processing.md](large-dataset-processing.md) — streaming, partitioning, parallelism patterns.
- [database-loading-and-persistence.md](database-loading-and-persistence.md) — COPY, UPSERT, no-op skipping.
- [django-background-processing.md](django-background-processing.md) — task granularity, queues, time limits.
- [data-processing-observability.md](data-processing-observability.md) — job metrics, alerts on duration and queue age.
- [data-processing-testing.md](data-processing-testing.md) — performance tests for pipelines.
- [django-performance](../../django-performance/SKILL.md):
  [file and data processing performance](../../django-performance/references/file-and-data-processing-performance.md),
  [background task performance](../../django-performance/references/background-task-performance.md),
  [memory and resource management](../../django-performance/references/memory-and-resource-management.md),
  [profiling and observability](../../django-performance/references/profiling-and-observability.md),
  [performance testing and benchmarking](../../django-performance/references/performance-testing-and-benchmarking.md),
  [concurrency and worker management](../../django-performance/references/concurrency-and-worker-management.md).
- [django-orm query optimisation](../../django-orm/references/query-optimization.md).
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — EXPLAIN, WAL, vacuum, COPY internals.

## 12. Official documentation and external references

- https://docs.python.org/3/library/profile.html
- https://docs.python.org/3/library/tracemalloc.html
- https://docs.python.org/3/library/time.html#time.perf_counter
- https://docs.python.org/3/library/resource.html
- https://docs.djangoproject.com/en/5.2/topics/db/optimization/
- https://docs.djangoproject.com/en/5.2/ref/databases/#persistent-connections
- https://docs.celeryq.dev/en/stable/userguide/optimizing.html
- https://www.postgresql.org/docs/current/populate.html
- https://www.postgresql.org/docs/current/using-explain.html
- https://www.postgresql.org/docs/current/pgstatstatements.html
- https://www.postgresql.org/docs/current/wal-configuration.html
- https://pandas.pydata.org/docs/user_guide/scale.html
- https://docs.pola.rs/user-guide/concepts/streaming/
- https://arrow.apache.org/docs/python/csv.html
