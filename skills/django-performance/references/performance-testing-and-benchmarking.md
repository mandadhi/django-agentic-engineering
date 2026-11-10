# Performance Testing and Benchmarking

**Scope:** Designing, running and interpreting load, stress, spike, soak and regression
benchmarks for Django applications so that performance claims are measured, reproducible and
comparable. Test-suite mechanics (pytest fixtures, query-count assertions, CI wiring) belong to
[django-testing](../../django-testing/SKILL.md), specifically
[performance-and-reliability-testing.md](../../django-testing/references/performance-and-reliability-testing.md).

## 1. Purpose and scope

A benchmark is an experiment: it answers one question under controlled conditions. Most bad
performance decisions come from benchmarks that measured the wrong thing — an empty database, a
warm cache, a closed-loop generator hiding queueing, or a laptop. This reference helps an agent:

- Pick the test type that answers the question.
- Model the workload so the result transfers to production.
- Measure latency percentiles and throughput correctly, avoiding coordinated omission.
- Separate deterministic CI gates (query counts) from noisy wall-clock benchmarks.
- Write Locust and k6 scripts and report results others can reproduce.

Tools: **[third-party: locust]**, **[third-party: k6]** (Grafana k6, Go binary, JavaScript
scripts), **[third-party: pytest-benchmark]**, **[third-party: hdrhistogram]** (optional).

## 2. Core concepts

### Test types

| Type | Question | Shape | Typical duration (illustrative) |
|---|---|---|---|
| Load | Do we meet the SLO at expected peak? | Ramp to target rate, hold | 15–60 min hold |
| Stress | Where and how do we fail beyond peak? | Step up until errors/SLO breach | Until failure |
| Breakpoint | What is maximum sustainable throughput? | Slow continuous ramp | Until knee in latency curve |
| Spike | Do we survive sudden bursts (campaign, cron alignment)? | Jump ×5–10 in seconds, drop | Minutes |
| Soak | Do leaks, connection churn, cache growth appear over time? | Steady moderate load | Hours |
| Micro/regression benchmark | Did this function/endpoint get slower? | Repeated isolated calls | Seconds–minutes |

### Workload model

- **Closed model**: N virtual users each send a request, wait for the response, think, repeat.
  Throughput falls automatically when the system slows → it under-reports latency during
  saturation.
- **Open model**: requests arrive at a fixed rate regardless of responses (like real internet
  traffic). Latency and queueing show honestly; required for SLO validation.
- **Coordinated omission**: the generator stops sending while waiting for slow responses, so
  slow periods produce *fewer* samples and percentiles look better than reality. Use
  arrival-rate executors (k6 `constant-arrival-rate`) or correct for it.

### Metrics

- Latency percentiles (p50/p95/p99, max) per endpoint, not a global average — see
  [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md).
- Throughput: successful requests/s (errors excluded — fast 500s inflate throughput).
- Error rate by status class and by cause (timeouts vs 5xx vs 429).
- Saturation: CPU, memory, worker occupancy, DB connections, DB CPU/IO, queue depth, cache hit
  rate. A latency number without saturation context can't be explained.

Percentiles must be computed from raw samples or mergeable histograms (HDR histograms); never
average percentiles across workers or time windows.

## 3. Internal mechanics

### Load generators

- **Locust**: Python; each user is a greenlet (gevent). Default user model is closed (wait_time
  between tasks). CPU of the generator becomes the bottleneck at high rates — distribute with
  `--master`/`--worker`. `constant_throughput(n)` / `constant_pacing(s)` give per-user pacing
  but remain closed-loop when responses are slower than the pacing (VERIFY exact semantics).
- **k6**: Go runtime executing JS per VU; `scenarios` choose executors: `ramping-vus`
  (closed), `constant-arrival-rate` / `ramping-arrival-rate` (open — allocates VUs to sustain the
  rate, reports `dropped_iterations` when it can't). Thresholds turn the test into pass/fail.

### What changes results between runs

- Data volume and distribution (query plans differ on 1k vs 10M rows; PostgreSQL planner
  statistics).
- Cache state: Redis cache, PostgreSQL shared buffers, OS page cache, CPU caches, Python import
  and template caches.
- JIT/warm-up: first requests per worker pay lazy imports, URL resolver population, template
  compilation, DB connection establishment, TLS handshakes.
- Noisy neighbours, CPU frequency scaling, container CPU throttling (CFS quota).
- Generator location: same host steals CPU; across regions adds network latency.

### Database state

`pg_stat_statements` accumulates since last reset; reset before each run so per-run query stats
are attributable: `SELECT pg_stat_statements_reset();`. Autovacuum, checkpoints and bloat from
the previous run change the next run — restore a snapshot or run `VACUUM ANALYZE` consistently.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Environment | Production-like staging vs production (shadow/off-peak) vs laptop | Staging with same instance types and data scale for decisions; laptop only for relative micro-benchmarks |
| Tool | Locust vs k6 | Locust when the team prefers Python and needs complex session logic; k6 for open-model arrival rates, thresholds and CI-friendly single binary |
| Model | Closed vs open | Open for SLO/capacity; closed acceptable for "N concurrent users of an internal tool" |
| Data | Synthetic factories vs anonymised production snapshot | Snapshot preserves skew (hot tenants, long tails); synthetic needs deliberate skew |
| Cache | Cold vs warm | Test both: warm for steady state, cold for deploy/cache-flush recovery |
| CI gating | Query counts vs wall-clock | Query counts are deterministic → hard gate; wall-clock is noisy → dedicated runner, statistical comparison, soft alert |

When benchmarks mislead:

- Optimising p50 on a closed model while production p99 is dominated by queueing.
- Testing one endpoint in isolation when the real bottleneck is a shared resource (DB connections)
  saturated by the mix.
- Comparing runs with different data, versions or cache states.

## 5. Django implementation patterns

### Benchmark plan (write before running)

```text
Question:     Can /api/orders/ meet p95 < 300 ms at 200 req/s with the projected Q4 mix?
Workload:     70% list (page 1–5), 20% detail, 10% create; 5% tenants generate 50% of traffic
Data:         anonymised snapshot 2026-10-01 (12M orders, 400k customers)
Environment:  staging, 4 × app (same instance type as prod), db.r6g.xlarge, Redis 7
Versions:     git SHA, Django 5.2.x, DRF 3.16.x, gunicorn config, PostgreSQL 16
Warm-up:      5 min at 50 req/s, discarded
Measure:      15 min at 200 req/s (open model), p50/p95/p99, errors, DB CPU, connections
Pass:         p95 < 300 ms, p99 < 800 ms, errors < 0.1%, no dropped iterations
```

### Locust script

```python
# loadtests/locustfile.py  [third-party: locust]
import random
from locust import HttpUser, task, between

class ApiUser(HttpUser):
    wait_time = between(1, 3)                     # think time; closed model

    def on_start(self):
        resp = self.client.post("/api/auth/token/", json={
            "username": f"load{random.randint(1, 5000)}", "password": "load-test-only"})
        self.client.headers["Authorization"] = f"Bearer {resp.json()['access']}"

    @task(7)
    def list_orders(self):
        page = random.randint(1, 5)
        self.client.get(f"/api/orders/?page={page}", name="/api/orders/?page=[n]")

    @task(2)
    def order_detail(self):
        self.client.get(f"/api/orders/{random.choice(ORDER_IDS)}/", name="/api/orders/[id]/")

    @task(1)
    def create_order(self):
        self.client.post("/api/orders/", json=make_order_payload(), name="/api/orders/ POST")
```

```bash
locust -f loadtests/locustfile.py --host https://staging.example.com \
  --headless -u 400 -r 20 --run-time 20m --csv results/run-$(git rev-parse --short HEAD) \
  --html results/report.html
```

`name=` groups URLs with IDs so percentiles are per route, not per unique URL. Load-test users,
tokens and data must exist only in the test environment.

### k6 open-model script with thresholds

```javascript
// loadtests/orders.js  [third-party: k6]
import http from "k6/http";
import { check } from "k6";

export const options = {
  scenarios: {
    warmup: {
      executor: "constant-arrival-rate", rate: 50, timeUnit: "1s",
      duration: "5m", preAllocatedVUs: 50, maxVUs: 200, tags: { phase: "warmup" },
    },
    steady: {
      executor: "constant-arrival-rate", rate: 200, timeUnit: "1s",
      duration: "15m", startTime: "5m", preAllocatedVUs: 200, maxVUs: 1000,
      tags: { phase: "steady" },
    },
  },
  thresholds: {
    "http_req_duration{phase:steady,route:list}": ["p(95)<300", "p(99)<800"],
    "http_req_failed{phase:steady}": ["rate<0.001"],
    "dropped_iterations": ["count==0"],
  },
};

const TOKEN = __ENV.TOKEN;

export default function () {
  const page = Math.floor(Math.random() * 5) + 1;
  const res = http.get(`${__ENV.BASE_URL}/api/orders/?page=${page}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
    tags: { route: "list" },
  });
  check(res, { "status 200": (r) => r.status === 200 });
}
```

```bash
k6 run -e BASE_URL=https://staging.example.com -e TOKEN=$LOAD_TOKEN \
  --summary-export results/k6-summary.json loadtests/orders.js
```

Thresholds tagged with `phase:steady` exclude warm-up from pass/fail. `dropped_iterations > 0`
means the generator couldn't sustain the rate — the result is invalid, not "fast". Spike and
breakpoint tests use `ramping-arrival-rate` with `stages`.

### Deterministic regression gates (CI)

Query-count budgets are the cheapest, most reliable performance regression test: they don't
depend on hardware. Their implementation (`django_assert_max_num_queries`,
`assertNumQueries`) is owned by
[django-testing performance-and-reliability-testing](../../django-testing/references/performance-and-reliability-testing.md).
From a performance standpoint, budget the endpoints that appear in the top of
`pg_stat_statements` and every list endpoint, and assert the count is independent of row count
(create 2 rows and 20 rows; the count must not change).

### Wall-clock micro-benchmarks

```python
# benchmarks/test_serializers.py  [third-party: pytest-benchmark]
import pytest

@pytest.mark.benchmark(group="order-serializer", min_rounds=50, warmup=True)
@pytest.mark.django_db
def test_order_list_serialization(benchmark, orders_200):
    data = benchmark(lambda: OrderListSerializer(orders_200, many=True).data)
    assert len(data) == 200
```

```bash
# On a dedicated, quiet runner — not shared CI workers
pytest benchmarks/ --benchmark-only --benchmark-autosave
pytest benchmarks/ --benchmark-only --benchmark-compare --benchmark-compare-fail=median:10%
```

Use median (robust to outliers) for comparison; fail only on a margin larger than observed
run-to-run noise (measure noise by running the unchanged baseline several times). Keep DB-free
micro-benchmarks separate from DB-backed ones — the latter measure the database as much as the
code.

### Database load testing

```sql
SELECT pg_stat_statements_reset();
-- run the load test --
SELECT calls, round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms, rows,
       left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Column names are PostgreSQL ≥ 13 (`total_exec_time`; older versions used `total_time`). Capture
alongside: connections (`pg_stat_activity`), lock waits, buffer hit ratio, replica lag. Plan
analysis: [database-performance.md](database-performance.md).

### Reproducibility checklist

- Pin and record: git SHA, lock file hash, Python/Django/DRF/PostgreSQL/Redis versions, server
  config (gunicorn/uvicorn args), settings flags (`DEBUG=False`, `CONN_MAX_AGE`, cache backend).
- Fixed dataset: snapshot ID or factory script with a fixed seed and documented skew.
- Same hardware class, no autoscaling during the run (or record scaling events), generator on a
  separate host in the same region.
- Same warm-up, duration and ordering; reset `pg_stat_statements` and restore DB snapshot between
  comparative runs.
- Store raw outputs (CSV/JSON summaries, histograms) with the report, not screenshots only.

### Reporting results

| Item | Example (illustrative) |
|---|---|
| Question & verdict | p95 < 300 ms at 200 req/s — **pass** (p95 241 ms) |
| Latency | p50 88 ms, p95 241 ms, p99 610 ms, max 2.1 s |
| Throughput | 199.6 req/s successful, 0.04% errors (all 504 from upstream X) |
| Saturation | App CPU 55%, DB CPU 70%, 92/120 connections in use |
| Bottleneck observed | DB CPU; top query = orders list count |
| Comparison | vs baseline SHA abc123: p95 −38%; same dataset and config |
| Limits | Cache warm; no background jobs running; single region |

## 6. Production use cases

- **Launch readiness**: open-model load test at 2× forecast peak revealed connection-pool
  exhaustion at 1.4× (p99 jumped from 400 ms to 9 s). Fix: pool sizing and PgBouncer
  ([concurrency-and-worker-management.md](concurrency-and-worker-management.md)). Re-test passed.
- **False confidence from closed model**: Locust with 100 users showed p95 180 ms; production
  at the same request rate had p95 1.2 s. The closed model throttled itself; k6
  `constant-arrival-rate` reproduced production.
- **Soak test leak**: 6-hour soak showed worker RSS +40 MB/hour; traced to an unbounded
  module-level dict cache ([memory-and-resource-management.md](memory-and-resource-management.md)).
- **Spike after cache flush**: cold-cache spike test showed a stampede on dashboard keys; fixed
  with locking/early refresh ([caching-engineering.md](caching-engineering.md)).
- **Regression caught in CI**: query-count test failed when a serializer added a nested field
  without prefetch — 3 queries became 203 on the 100-row fixture.
- **Benchmark noise**: pytest-benchmark on shared CI flagged ±25% swings on unchanged code;
  moved to a dedicated runner and a 10% median threshold.

## 7. Advantages and disadvantages

| Practice | Advantage | Disadvantage |
|---|---|---|
| Open-model load tests | Honest latency under queueing | Needs enough generator capacity; harder to reason about "users" |
| Closed-model tests | Simple, models fixed user populations | Coordinated omission, understates tail latency |
| Production-sized staging data | Realistic plans and cache behaviour | Cost, anonymisation effort, refresh cadence |
| Query-count CI gates | Deterministic, cheap, catches N+1 | Doesn't catch slow single queries or CPU regressions |
| Wall-clock CI benchmarks | Catches CPU regressions | Noisy; needs dedicated hardware and statistics |
| Testing in production (off-peak, shadow) | Most realistic | Risk to users, data pollution, needs kill switch |

## 8. Security and performance considerations

- Never point load tests at production without explicit approval, a kill switch, rate caps and
  coordination with third parties whose APIs you'll call (payment, email, SMS — stub them).
- Load-test credentials and data are test-only; don't commit tokens; anonymise snapshots
  ([django-security-engineering](../../django-security-engineering/SKILL.md)).
- WAFs, rate limiters and DDoS protection will block or skew a load test — allow-list the
  generator or test behind them deliberately, and state which.
- Throttling (DRF throttles) may cap throughput; decide whether the test measures the throttle or
  bypasses it.
- `DEBUG=True` stores every query in memory and changes timings — always benchmark with
  production settings.

## 9. Common mistakes and anti-patterns

- Benchmarking on an empty or tiny database; or against SQLite when production is PostgreSQL.
- Reporting averages or a single p95 for all endpoints.
- Including warm-up in the measured window; or never testing cold start.
- Generator on the same host as the app; generator CPU at 100%.
- Counting fast errors as throughput.
- Changing several variables between runs, then attributing the difference to one.
- Treating query-count reduction as proof of speed without measuring query time.
- Setting thresholds from one run with no noise measurement.
- Load-testing one endpoint when the SLO depends on the realistic mix.
- Running load tests with background jobs, cron and autoscaling uncontrolled and unrecorded.

## 10. Troubleshooting and validation

| Symptom | Check | Interpretation |
|---|---|---|
| Throughput plateaus, latency rises | App CPU, worker occupancy, DB CPU, connections | Saturated resource = the bottleneck; find which |
| Throughput plateaus, all servers idle | Generator CPU, `dropped_iterations`, network | Generator-limited; the result is invalid |
| High variance between identical runs | Noise across 3+ baseline runs; CPU throttling; cache state | Environment not controlled |
| p99 spikes periodically | Correlate with GC, checkpoints, autovacuum, cron, cache expiry | Background interference or synchronised TTLs |
| Errors only above a rate | Error type: 502/504 (proxy timeout), 503 (load shed), connection refused | Worker exhaustion, backlog overflow, pool limits |
| Staging fine, production slow | Data volume, plans (`EXPLAIN`), traffic mix, cache hit ratio | Unrepresentative workload |

Validation of an optimisation: same plan, same dataset, same config except the change; at least
three runs of baseline and candidate; compare percentiles and saturation; report the difference
with its noise range. Correlate generator metrics with server-side telemetry
([profiling-and-observability.md](profiling-and-observability.md)) — client-side latency includes
network and queueing that server spans may not show.

## 11. Related references and skills

- [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md) — percentiles, Little's Law, SLOs.
- [profiling-and-observability.md](profiling-and-observability.md) — server-side measurement during tests.
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — turning results into capacity estimates.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — worker and pool sizing validated by load tests.
- [database-performance.md](database-performance.md) — query plans and DB saturation.
- [production-performance-troubleshooting.md](production-performance-troubleshooting.md) — regression analysis in production.
- [django-testing performance-and-reliability-testing](../../django-testing/references/performance-and-reliability-testing.md) — query-count budgets, suite speed, CI mechanics.
- [django-testing](../../django-testing/SKILL.md) — CI pipelines and test fixtures.
- [django-rest-framework](../../django-rest-framework/SKILL.md) — API endpoints and throttling under test.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — `pg_stat_statements` and database tuning.

## 12. Official documentation and external references

- Locust documentation: https://docs.locust.io/en/stable/
- Locust — Running without the web UI: https://docs.locust.io/en/stable/quickstart.html
- k6 documentation: https://grafana.com/docs/k6/latest/
- k6 — Scenarios and executors: https://grafana.com/docs/k6/latest/using-k6/scenarios/
- k6 — Open and closed models: https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/
- k6 — Thresholds: https://grafana.com/docs/k6/latest/using-k6/thresholds/
- pytest-benchmark: https://pytest-benchmark.readthedocs.io/en/latest/
- PostgreSQL — pg_stat_statements: https://www.postgresql.org/docs/current/pgstatstatements.html
- Django 5.2 — Performance and optimization: https://docs.djangoproject.com/en/5.2/topics/performance/
- Django 5.2 — Testing tools (`assertNumQueries`): https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- HdrHistogram: https://hdrhistogram.github.io/HdrHistogram/
- Gil Tene, "How NOT to Measure Latency" (coordinated omission) — talk, widely cited; VERIFY current link.
