# Performance Engineering Fundamentals

**Scope:** The theory every other reference in this skill relies on: latency, throughput, concurrency,
capacity, percentiles, queueing (Little's Law), bottleneck identification, performance budgets and
SLOs. Framework-specific tactics live in the other references; this one gives the vocabulary and the
arithmetic used to justify them.

## 1. Purpose and scope

Use this reference when you need to:

- Frame a vague complaint ("the app is slow") as a measurable problem.
- Decide which metric matters (latency, throughput, saturation, error rate) for a workload.
- Translate a target (e.g. "p95 < 300 ms at 400 req/s") into worker, connection and queue sizes.
- Decide whether an optimisation is worth doing at all.

Out of scope: tool usage ([profiling-and-observability.md](profiling-and-observability.md)), worker
sizing details ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)),
capacity planning at the fleet level ([scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)).

## 2. Core concepts

| Term | Definition | Typical Django unit |
|---|---|---|
| **Latency** | Time from request arrival to last byte sent, as seen by a defined observer (client, load balancer, app) | ms per request |
| **Service time** | Time a worker actually spends processing (excludes queueing) | ms of worker occupancy |
| **Wait (queue) time** | Time a request waits for a free worker/connection | ms |
| **Throughput** | Completed work per unit time | req/s, tasks/s, rows/s |
| **Concurrency** | Requests in progress at one instant (in service + waiting) | count |
| **Capacity** | Maximum sustainable throughput while meeting the latency objective | req/s at SLO |
| **Utilisation** | Fraction of time a resource is busy | % CPU, % workers busy |
| **Saturation** | Work that cannot be served immediately (queue length) | backlog depth |

**Latency ≠ 1/throughput.** A service with 40 workers each taking 200 ms has 200 req/s throughput
and 200 ms latency. Adding workers raises throughput without changing per-request latency; making
the code faster improves both. Always state which one a change targets.

### Percentiles and why averages lie

Response-time distributions are right-skewed (long tail). The mean is pulled by outliers yet hides
them: 95 requests at 50 ms + 5 at 2 000 ms gives a mean of 147 ms — a value no user experienced.

| Percentile | Meaning | Use |
|---|---|---|
| p50 | Median; typical experience | Regression detection on the common path |
| p90/p95 | Most users' worst normal experience | SLO targets for interactive endpoints |
| p99/p99.9 | Tail; GC pauses, lock waits, cold caches, retries | Capacity and fan-out risk |
| max | Single worst sample; noisy | Debugging only |

**Tail amplification:** a page that issues *N* independent backend calls is slow if *any* call is
slow. With each call having a 1 % chance of exceeding its p99, P(page hits at least one p99) =
1 − 0.99^N; for N = 20 that is ~18 %. Fan-out turns backend p99 into frontend p80.

### Aggregating percentiles correctly

- **Never average percentiles** across hosts or time windows; p95 of the fleet is not the mean of
  per-host p95s.
- **Histograms** (fixed buckets, e.g. Prometheus `_bucket`, OpenTelemetry explicit-bucket or
  exponential histograms) are mergeable: sum bucket counts, then compute the quantile. Accuracy is
  bounded by bucket width — choose buckets around your SLO threshold (e.g. 100, 200, 300, 500 ms).
- **Summaries** (client-side quantiles) are precise per instance but cannot be merged.
- For SLOs prefer "fraction of requests under threshold" (a counter ratio) over a quantile: it
  aggregates exactly.

### CPU-bound vs I/O-bound

| Workload | Signature | Lever |
|---|---|---|
| CPU-bound (serialisation, template rendering, pure-Python loops, image work) | Worker CPU ≈ 100 % while busy; wall time ≈ CPU time | Less work, better algorithms, more processes (GIL), C-accelerated libraries |
| I/O-bound (DB, cache, HTTP calls, storage) | Low CPU, wall time ≫ CPU time | Fewer round trips, batching, caching, concurrency (threads/async) |
| Lock/contention-bound | Low CPU, latency rises with concurrency, not with load per request | Shorter critical sections, fewer hot rows, different isolation |

Quick test: compare `time.process_time()` with `time.perf_counter()` for a request (see
[profiling-and-observability.md](profiling-and-observability.md)). If CPU/wall < 0.3, you are
waiting, not computing.

## 3. Internal mechanics

### Little's Law

For any stable system: **L = λ × W** — average concurrency (L) equals arrival rate (λ) times average
time in system (W). It holds regardless of distribution, which makes it the most useful sizing tool
available.

**Worked example (illustrative):** an API receives λ = 300 req/s at peak; measured mean service time
W = 120 ms.

- Required in-flight capacity: L = 300 × 0.12 = **36 concurrent requests**.
- With sync Gunicorn workers (1 request each), you need ≥ 36 worker slots *plus headroom* —
  running at 100 % utilisation means unbounded queueing (see below). Targeting ~70 % utilisation:
  36 / 0.7 ≈ 52 slots.
- Each slot that touches the DB may hold a connection: up to ~52 DB connections from this tier,
  which must fit within PostgreSQL `max_connections` together with Celery, cron and admin access.
- If an optimisation cuts W to 60 ms, L halves to 18 — the same hardware now serves twice the load
  or the same load with fewer connections.

Little's Law applies at every layer: DB connections (L = queries/s × query time), Celery queues
(backlog = arrival rate × wait), and connection pools.

### Utilisation and the queueing knee

For a single-server queue with random arrivals (M/M/1), mean time in system is
W = S / (1 − ρ), where S is service time and ρ is utilisation.

| ρ | W as multiple of S |
|---|---|
| 0.50 | 2× |
| 0.70 | 3.3× |
| 0.80 | 5× |
| 0.90 | 10× |
| 0.95 | 20× |

Multi-server pools (M/M/c) degrade later and more abruptly, but the shape is the same: latency is
flat until a knee, then explodes. Consequences:

- A system "only" at 85 % CPU can already have poor p99.
- Small load increases near the knee cause large latency jumps — typical of traffic spikes.
- Reducing variance (fewer pathological slow requests) matters as much as reducing the mean: a few
  10-second exports occupying workers starve fast requests (head-of-line blocking).

### Amdahl's Law

If a fraction *p* of request time is improved by factor *s*, overall speedup = 1 / ((1 − p) + p/s).
Making serialisation (10 % of request time) 5× faster gives only 1.087× overall. Optimise the
largest measured component first.

### USE and RED methods

- **USE** (per resource — CPU, memory, disk, network, DB, pool): **U**tilisation, **S**aturation,
  **E**rrors. Finds infrastructure bottlenecks.
- **RED** (per service/endpoint): **R**ate, **E**rrors, **D**uration (as histogram). Describes user
  experience.

Use RED to detect *that* something is slow and which endpoint; use USE to find *which resource* is
the constraint.

## 4. Engineering decisions and trade-offs

| Decision | Choose A when… | Choose B when… |
|---|---|---|
| Optimise latency vs throughput | Users wait synchronously (checkout, search) | Batch/ingest work where completion rate matters |
| Scale out vs make faster | Code is already efficient and the resource is saturated | Profiling shows waste (N+1, repeated work) — scaling multiplies waste and DB load |
| Mean vs percentile target | Cost/throughput planning | User-facing SLOs |
| Tight vs loose budget | Hot, high-traffic endpoints | Rare admin/reporting pages |
| Fix now vs defer | SLO burn or clear cost driver | Speculative; no measurement shows impact |

Trade-offs every optimisation carries: complexity, staleness (caching), consistency (async/eventual),
operational surface (new infrastructure), and cost. An optimisation that improves p50 but worsens
p99 (e.g. caching with stampedes) can be a net regression for the SLO.

## 5. Django implementation patterns

### Defining a performance budget per endpoint

```python
# perf_budgets.py — single source of truth used by tests and dashboards (illustrative)
BUDGETS = {
    "api:order-list": {"p95_ms": 250, "max_queries": 6, "max_payload_kb": 200},
    "api:order-detail": {"p95_ms": 120, "max_queries": 4, "max_payload_kb": 40},
    "web:dashboard": {"p95_ms": 400, "max_queries": 12, "max_payload_kb": 300},
}
```

Query budgets are enforced in tests ([django-testing](../../django-testing/SKILL.md),
`performance-and-reliability-testing`); latency budgets are enforced by load tests
([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)) and SLO alerts.

### SLO and error budget

```text
SLI:  proportion of GET /api/orders/ requests (excluding 4xx) served in < 300 ms, measured at the LB
SLO:  99.0 % over a rolling 28 days
Error budget: 1 % of requests ≈ 2 016 slow-or-failed requests per 201 600 (illustrative volume)
Alert: burn rate > 14.4 over 1 h AND > 6 over 6 h  (multi-window burn-rate alerting)
```

Measure the SLI at the load balancer or ingress, not only in Django: app timing misses time spent
queued in the worker backlog.

### Measuring a baseline before changing code

```python
import statistics, time
from django.test import Client

def sample_latency(path: str, n: int = 200) -> dict:
    client = Client()
    for _ in range(20):          # warm-up: import caches, connection setup, template cache
        client.get(path)
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        client.get(path)
        samples.append((time.perf_counter() - t0) * 1000)
    q = statistics.quantiles(samples, n=100)
    return {"p50": q[49], "p95": q[94], "p99": q[98], "mean": statistics.fmean(samples)}
```

This is a single-client micro-baseline (service time, no queueing). It is useful for before/after
comparisons of one code path, never for capacity claims — use a load tool for that.

## 6. Production use cases

- **"Checkout is slow at lunchtime."** RED shows p95 rises from 180 ms to 2 s only between 12:00 and
  13:00 while p50 barely changes; USE shows web CPU 60 %, DB connections at the pool limit. This is
  queueing for connections, not slow code: Little's Law on the pool (L = 900 q/s × 25 ms ≈ 23 busy
  connections vs pool of 20) explains it.
- **Capacity sign-off for a launch.** Expected 3× traffic; current peak 200 req/s at 55 % worker
  utilisation. Linear projection gives 165 % — infeasible without either halving service time or
  adding capacity; the queueing table shows even 2× (110 %) is unsafe.
- **Optimisation triage.** Profiling shows 70 % DB wait, 20 % serialisation, 10 % other. Amdahl says
  fix DB first; a 2× faster serializer yields ≤ 10 %.

## 7. Advantages and disadvantages

| Practice | Advantage | Disadvantage |
|---|---|---|
| Percentile SLOs | Reflect user experience; catch tails | Need histograms; noisy at low volume |
| Little's Law sizing | Distribution-free, simple | Uses means; add headroom for variance |
| Strict budgets | Prevent gradual regressions | Need maintenance as features grow |
| Measure-first culture | Avoids wasted effort | Requires observability investment up front |

## 8. Security and performance considerations

- Slow endpoints are denial-of-service amplifiers: an unauthenticated request costing 2 s of CPU
  lets a small botnet saturate workers. Budget and rate-limit expensive paths
  ([scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)).
- Latency differences can leak information (timing side channels, e.g. user enumeration on login);
  do not "optimise" away constant-time comparisons or deliberate password hashing cost
  ([django-security-engineering](../../django-security-engineering/SKILL.md)).
- Metrics labels must not contain PII or unbounded values (user IDs, raw URLs) — cardinality
  explosions also hurt metric backend performance.

## 9. Common mistakes and anti-patterns

- Reporting averages, or averaging percentiles across hosts.
- Measuring latency only inside Django and missing queue time in front of the workers.
- Benchmarking a single request in a loop and calling it "load testing".
- Running systems at 90 %+ utilisation "for efficiency" and being surprised by tail latency.
- Optimising the code path that is easiest to change rather than the one that dominates time.
- Adding workers to fix a DB-bound endpoint — it increases DB contention and can lower throughput.
- Declaring success from one run; not accounting for variance (compare distributions, repeat runs).
- Treating a micro-optimisation (e.g. list comprehension vs loop) as meaningful next to a 40 ms query.

## 10. Troubleshooting and validation

1. State the observer and metric: "p95 at the LB for `GET /api/orders/`".
2. Confirm the distribution, not just one number (histogram or percentile table).
3. Split W into wait vs service time: compare LB/ingress latency with in-app timing. A large gap =
   queueing (workers, pool) — see [concurrency-and-worker-management.md](concurrency-and-worker-management.md).
4. Split service time into CPU vs I/O (process vs wall time; DB time from query timing).
5. Apply Amdahl: estimate maximum gain before implementing.
6. After the change, compare before/after distributions under the *same* load and data set; repeat
   at least three runs and report spread. A change smaller than run-to-run variance is not proven.

Validation checklist:

- [ ] Baseline recorded with percentiles, load level, data volume and version.
- [ ] Bottleneck resource identified with USE evidence.
- [ ] Expected gain estimated (Amdahl / Little's Law) before implementing.
- [ ] After-measurement uses identical conditions; improvement exceeds noise.
- [ ] Tail (p99) checked, not only p50.

## 11. Related references and skills

- [profiling-and-observability.md](profiling-and-observability.md) — how to obtain the numbers.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — applying Little's Law to workers and pools.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — fleet-level capacity.
- [performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md) — load-test methodology.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — symptom runbooks.
- [django-architecture](../../django-architecture/SKILL.md) — quality attributes in design decisions.

## 12. Official documentation and external references

- Django — Performance and optimization: https://docs.djangoproject.com/en/5.2/topics/performance/
- Python `time` (perf_counter, process_time): https://docs.python.org/3/library/time.html
- Python `statistics.quantiles`: https://docs.python.org/3/library/statistics.html
- OpenTelemetry metrics data model (histograms): https://opentelemetry.io/docs/specs/otel/metrics/data-model/
- Prometheus — Histograms and summaries: https://prometheus.io/docs/practices/histograms/
- Google SRE Book — Service Level Objectives: https://sre.google/sre-book/service-level-objectives/
- Google SRE Workbook — Alerting on SLOs (burn rate): https://sre.google/workbook/alerting-on-slos/
- Brendan Gregg — The USE Method: https://www.brendangregg.com/usemethod.html
- Dean & Barroso, "The Tail at Scale" (CACM 2013): https://research.google/pubs/the-tail-at-scale/
