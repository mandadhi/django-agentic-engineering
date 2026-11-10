# Production Performance Troubleshooting

**Scope:** Symptom-driven runbooks for performance incidents in production Django systems — slow
requests, database bottlenecks, CPU, memory, worker and connection-pool exhaustion, cache and queue
failures, intermittent latency, traffic spikes and regressions. Each runbook goes signals →
hypotheses → confirming measurement → mitigation → owning reference; tool usage lives in
[profiling-and-observability.md](profiling-and-observability.md).

## 1. Purpose and scope

Use during or after an incident, when an alert fires on latency/saturation, or when a deploy is
suspected of a regression. The goal is to **stabilise first, then find the root cause, then fix
durably** — with evidence at each step.

Out of scope: general incident process and postmortems (organisation process; an
`incident-response` skill may exist), designing capacity ([scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)),
deep PostgreSQL internals ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 2. Core concepts

- **Mitigation ≠ fix.** Rolling back, scaling out, disabling a feature flag or shedding load buys time;
  the root-cause fix comes after.
- **Start from the user-facing signal** (RED at the load balancer), then descend: which route →
  which component (trace) → which resource (USE) → which code/query (profile/`pg_stat_statements`).
- **Change correlation.** Most regressions follow a change: deploy, config, migration, data growth,
  traffic mix, dependency upgrade, infrastructure event. Build the timeline first.
- **Wait vs service time.** If LB latency ≫ in-app latency, requests are queuing before Django
  (workers or upstream); if in-app latency rose, the code path or its dependencies slowed.
- **Saturation spreads.** A slow DB holds web workers longer → worker pool saturates → LB queues →
  retries from clients → more load. Find the *first* saturated resource in the timeline.

## 3. Internal mechanics

### How a single slow dependency takes down a Django tier

1. A query that took 20 ms now takes 2 s (lock wait, plan flip, cache miss storm).
2. By Little's Law, in-flight requests on that route rise 100× at the same arrival rate.
3. Sync workers (one request each) fill up; new requests queue in the Gunicorn backlog/LB.
4. Connection pool or `max_connections` is reached; other routes now wait for connections too.
5. Health checks time out → orchestrator restarts "unhealthy" pods → fewer workers, cold caches → worse.
6. Clients and upstream services retry → arrival rate increases (retry amplification).

Timeouts at every hop, bounded pools, and load shedding break this chain
([concurrency-and-worker-management.md](concurrency-and-worker-management.md),
[scalability-and-capacity-planning.md](scalability-and-capacity-planning.md)).

### Signals to have before an incident

| Layer | Signals |
|---|---|
| Edge/LB | Request rate, status codes, latency histogram per route, upstream connect/queue time |
| Django | Route RED metrics, in-app time split (DB/cache/external), worker busy count, request timeouts |
| Process | CPU, RSS per worker, restarts, GC pauses (if exported) |
| PostgreSQL | Active connections by state, `pg_stat_statements`, lock waits, replication lag, CPU/IO |
| Redis | Hit ratio, latency, evictions, memory, connected clients |
| Queue | Depth per queue, oldest message age, task runtime, retries, worker count |

## 4. Engineering decisions and trade-offs

| Mitigation | Use when | Risk |
|---|---|---|
| Roll back deploy | Strong correlation with a release | Migrations may not be reversible; data written by new code |
| Feature-flag off | Change is flag-gated | Partial functionality loss |
| Scale web out | CPU-bound saturation, DB has headroom | Multiplies DB connections and load if DB is the constraint — can worsen the incident |
| Scale DB up | DB CPU/IO saturated, no quick query fix | Slow, costly, may require failover |
| Kill a query / backend | Runaway query or lock holder | Rolls back its transaction; may be a legitimate job |
| Shed load / rate-limit | Overload beyond capacity | Rejects some users deliberately |
| Serve stale cache | Recompute is the bottleneck | Staleness visible to users |
| Pause consumers / batch jobs | Background work competes with web for DB | Backlog grows; SLAs on async work |

Prefer reversible, low-blast-radius mitigations first. Record every mitigation with a timestamp —
it changes the signals you are reading.

## 5. Django implementation patterns

Each runbook: **Signals → Hypotheses → Confirm → Mitigate → Fix (owner)**.

### Runbook 1 — Slow requests on one route

- **Signals:** p95/p99 up on one route; others normal.
- **Hypotheses:** new N+1; plan change or missing index; external call slower; payload grew; cache
  key change causing misses.
- **Confirm:** trace of slow requests (span breakdown); query count/DB time per request (timing
  middleware); `pg_stat_statements` top by `total_exec_time` filtered to that query shape; diff of
  the route's code since last good deploy.
- **Mitigate:** roll back or flag off; temporarily cache the response if safe; cap page size.
- **Fix:** [orm-performance-optimization.md](orm-performance-optimization.md),
  [database-performance.md](database-performance.md), [api-performance.md](api-performance.md).

### Runbook 2 — Database bottleneck (all routes slow)

- **Signals:** DB CPU or IO high, many `active` backends, rising query latency for unrelated routes.
- **Hypotheses:** one expensive query dominating; lock contention; autovacuum/bloat; a batch job or
  migration running; replica lag sending reads to primary; connection storm.

```sql
-- What is running now, longest first
SELECT pid, now() - query_start AS runtime, state, wait_event_type, wait_event,
       left(query, 120) AS query
FROM pg_stat_activity
WHERE state <> 'idle' AND backend_type = 'client backend'
ORDER BY runtime DESC LIMIT 20;

-- Who blocks whom
SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
       left(blocking.query, 120) AS blocking_query, now() - blocking.xact_start AS blocking_xact_age
FROM pg_stat_activity blocked
JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
JOIN pg_stat_activity blocking ON blocking.pid = b.pid;

-- Top cost since last reset (requires pg_stat_statements extension)
SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time, 1) AS mean_ms,
       rows, left(query, 120) AS query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 15;
```

- **Mitigate:** cancel runaway queries (`pg_cancel_backend(pid)`; `pg_terminate_backend` as last
  resort); pause the batch job; set `statement_timeout` for the web role if absent.
- **Fix:** [database-performance.md](database-performance.md) (indexes, plans, locks, transaction
  duration), [postgresql-engineering](../../postgresql-engineering/SKILL.md) (vacuum, configuration).

### Runbook 3 — High CPU on web workers

- **Signals:** container CPU near limit, throughput flat, latency rising; CPU throttling metrics
  (cgroup `throttled_time`) increasing.
- **Hypotheses:** hot code path (regex, serialisation, template loops); traffic mix shifted to an
  expensive route; GZip in-process; password hashing on a login flood; retry loops.
- **Confirm:** `py-spy record` on one worker for 30 s; correlate CPU with per-route rate; check CPU
  throttling vs node CPU (limits too low look like slow code).
- **Mitigate:** scale out (if DB has headroom); rate-limit the hot route; move compression to proxy.
- **Fix:** [django-application-performance.md](django-application-performance.md),
  [api-performance.md](api-performance.md), [concurrency-and-worker-management.md](concurrency-and-worker-management.md).

### Runbook 4 — Memory growth

- **Signals:** RSS per worker grows over hours; OOM kills; restarts correlate with uptime.
- **Hypotheses:** large QuerySet materialisation on some route (step increases); unbounded
  module-level caches/`lru_cache` (steady growth); `DEBUG=True` storing queries; leaking references
  in long-lived objects; fragmentation (RSS stays high after peak).
- **Confirm:** RSS vs time per worker (steps vs slope); tracemalloc snapshots diff; memray on a
  staging replay; check `settings.DEBUG`.
- **Mitigate:** worker recycling (`max_requests` + jitter), raise memory limit temporarily.
- **Fix:** [memory-and-resource-management.md](memory-and-resource-management.md),
  [file-and-data-processing-performance.md](file-and-data-processing-performance.md).

### Runbook 5 — Worker exhaustion

- **Signals:** LB upstream-queue/connect time up, 502/503/504s, Gunicorn `WORKER TIMEOUT` logs, all
  workers busy; in-app latency may look *normal* for requests that do get served.
- **Hypotheses:** slow dependency without timeouts; long-running requests (exports, uploads) on sync
  workers; too few workers for arrival rate × service time; slow clients on sync workers without a
  buffering proxy.
- **Confirm:** `py-spy dump` across workers — where are they all waiting?; Little's Law with
  current rate and service time; check per-route concurrency.
- **Mitigate:** add timeouts/circuit breaker for the failing dependency; shed or route expensive
  endpoints to a separate pool; scale out.
- **Fix:** [concurrency-and-worker-management.md](concurrency-and-worker-management.md),
  [background-task-performance.md](background-task-performance.md) (move long work off-request).

### Runbook 6 — Connection pool exhaustion

- **Signals:** `OperationalError: too many clients` / `remaining connection slots are reserved`;
  with Django's psycopg pool (Django ≥ 5.1) `PoolTimeout`; PgBouncer `cl_waiting` > 0.
- **Hypotheses:** workers × threads × processes exceed `max_connections`; long transactions holding
  connections (`idle in transaction`); leaked connections in threads/scripts; new deploy doubling
  pods during rollout.

```sql
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;
SELECT pid, now() - xact_start AS xact_age, left(query, 100)
FROM pg_stat_activity WHERE state = 'idle in transaction' ORDER BY xact_age DESC;
```

- **Mitigate:** set `idle_in_transaction_session_timeout`; reduce worker count temporarily; slow the
  rollout surge.
- **Fix:** connection budget and pooling — [database-performance.md](database-performance.md),
  [concurrency-and-worker-management.md](concurrency-and-worker-management.md).

### Runbook 7 — Cache failure or hit-ratio collapse

- **Signals:** DB load jumps while traffic is flat; Redis latency/timeouts; evictions spike; hit
  ratio drops after deploy.
- **Hypotheses:** Redis down/failing over; `maxmemory` reached with eviction; key prefix/version
  bumped (deploy cleared effective cache); stampede on a hot key after expiry; cache client timeouts
  set too high (requests wait on a dead cache).
- **Confirm:** `redis-cli INFO stats` (`keyspace_hits/misses`, `evicted_keys`), `INFO memory`,
  `SLOWLOG GET`; app cache-call latency spans; deploy diff for key changes.
- **Mitigate:** make cache failure non-fatal (short socket timeouts, fall through to DB with load
  protection); restore capacity; serve stale.
- **Fix:** [caching-engineering.md](caching-engineering.md).

### Runbook 8 — Queue buildup

- **Signals:** queue depth and oldest-message age rising; user-visible delays (emails, exports).
- **Hypotheses:** arrival > throughput (new producer, traffic spike); tasks slowed (DB, external API);
  retry storm; workers stuck on long tasks with prefetch hoarding; a poison message failing repeatedly.
- **Confirm:** rate in vs rate out per queue; task runtime histogram; retry counts; `celery inspect
  active` / `reserved`.
- **Mitigate:** scale consumers *only if* the downstream (DB/API) has headroom; pause low-priority
  producers; purge or park poison messages (with care — data loss).
- **Fix:** [background-task-performance.md](background-task-performance.md),
  [django-integrations](../../django-integrations/SKILL.md).

### Runbook 9 — Intermittent latency spikes

| Pattern | Suspect | Confirm |
|---|---|---|
| Spikes every N minutes | Cron/beat job, cache TTL alignment, autovacuum, backups | Overlay job schedule; TTL jitter; `pg_stat_progress_vacuum` |
| Spikes on some pods only | Noisy neighbour, CPU throttling, bad node | Per-pod latency; cgroup throttling; node metrics |
| p99 only, random | GC pauses, lock waits, cold workers after recycling | GC callbacks/timings; `log_lock_waits`; latency by worker age |
| After deploy for minutes | Cold caches, cold workers, connection storms | Hit ratio and connection count during rollout |
| Spikes with specific tenants | Data skew (large tenant), plan choice for skewed values | Latency by tenant (sampled logs), `EXPLAIN` with that tenant's parameters |

Fix owners: [memory-and-resource-management.md](memory-and-resource-management.md) (GC),
[database-performance.md](database-performance.md) (locks, plans),
[caching-engineering.md](caching-engineering.md) (TTL jitter, warming).

### Runbook 10 — Traffic spike

- **Signals:** request rate several × normal; saturation across tiers.
- **Hypotheses:** legitimate (campaign, launch), bot/scraper, retry storm from a client bug, attack.
- **Confirm:** rate by route, client, user-agent, ASN; ratio of new vs returning sessions; retries.
- **Mitigate:** CDN caching for anonymous pages, rate limits per client, load shedding of
  non-critical routes, graceful degradation (disable recommendations, serve cached), autoscaling
  within DB connection budget.
- **Fix:** [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md),
  [http-and-static-delivery-performance.md](http-and-static-delivery-performance.md).

### Runbook 11 — Performance regression analysis

1. **Pin the onset** to a deploy or change using route-level histograms (not averages).
2. **Diff** the changes: code, dependency lock file, settings, migrations, infrastructure.
3. **Reproduce** with a benchmark of the affected route on old vs new build, same data
   ([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)).
4. **Bisect** if the diff is large: `git bisect run` with a benchmark script whose exit code
   encodes "regressed" (threshold well above run-to-run noise).

```bash
git bisect start BAD_SHA GOOD_SHA
git bisect run python scripts/bench_route.py --route api:order-list --max-p95-ms 220 --runs 5
```

5. **Add a guard**: query-count assertion or benchmark threshold in CI so it cannot recur
   ([django-testing](../../django-testing/SKILL.md)).

## 6. Production use cases

- **Black Friday 504s.** Timeline: 10:02 traffic 3×; 10:04 DB connections at max; 10:05 web workers
  all busy. Root: a "stock level" query lost index use for a hot SKU (parameter skew) and ran 1.8 s
  under load. Mitigation: cached stock levels for 5 s (acceptable staleness agreed with product);
  fix: partial index + query rewrite; guard: `EXPLAIN` check in a migration review.
- **Gradual 3-week latency creep.** No deploy correlation; table grew 4×; offset pagination on
  `/api/events/` scanning deeper pages. Fix: cursor pagination ([api-performance.md](api-performance.md)).
- **Nightly 02:00 p99 spikes.** Celery beat triggers a full-table report on the primary. Fix: run on
  a replica with `statement_timeout`, chunked ([background-task-performance.md](background-task-performance.md)).

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Runbook-driven triage | Fast, consistent, less guesswork under stress | Can anchor on the wrong runbook; revisit hypotheses |
| Rollback-first | Fastest recovery for deploy regressions | Loses the live evidence; capture profiles/traces first if safe |
| Live profiling (py-spy) | Direct evidence of hot code | Needs privileges; small overhead |
| Killing queries | Immediate relief | Can abort legitimate work, roll back large transactions slowly |

## 8. Security and performance considerations

- During incidents, do not enable Debug Toolbar/Silk or `DEBUG=True` in production to "see more".
- Ad hoc SQL on the primary during an incident can worsen load — prefer `pg_stat_*` views and
  replicas for investigation queries.
- Traffic spikes may be attacks: involve security when patterns look adversarial
  ([django-security-engineering](../../django-security-engineering/SKILL.md)); rate limits are both a performance and a
  security control.
- Incident artifacts (profiles, query samples, logs) can contain PII — store and share accordingly.

## 9. Common mistakes and anti-patterns

- Scaling the web tier to fix a database bottleneck.
- Restarting everything first and losing the evidence (capture `py-spy dump`, `pg_stat_activity`).
- Reading averages; missing that only p99 or one tenant is affected.
- Chasing the loudest symptom (web CPU) instead of the first saturated resource.
- No timeouts on outbound calls, so one dependency outage exhausts all workers.
- Increasing `max_connections` as the response to pool exhaustion instead of finding long
  transactions or reducing per-process connections.
- Declaring the incident fixed without verifying the user-facing SLI recovered.
- Skipping the regression guard after the fix.

## 10. Troubleshooting and validation

Triage order (first 15 minutes):

1. Confirm user impact on the SLI (LB route latency/errors).
2. Build the change timeline (deploys, flags, jobs, traffic).
3. Identify the first saturated resource (USE across web, DB, cache, queue).
4. Choose the lowest-risk mitigation; record it.
5. Capture evidence (traces, `py-spy dump`, `pg_stat_activity`, Redis `INFO`) before restarts.
6. Root cause with the matching runbook; durable fix with the owning reference.

Post-incident validation:

- [ ] SLI back within objective for a full traffic cycle.
- [ ] Root cause demonstrated (reproduced or evidenced), not inferred.
- [ ] Fix measured before/after under representative load.
- [ ] Regression guard added (test, alert, benchmark, timeout, limit).
- [ ] Missing signals that slowed diagnosis added to dashboards.

## 11. Related references and skills

- [profiling-and-observability.md](profiling-and-observability.md) — tools used in every runbook.
- [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md) — Little's Law, queueing knee.
- [database-performance.md](database-performance.md), [orm-performance-optimization.md](orm-performance-optimization.md) — DB root causes.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — workers, pools, timeouts.
- [memory-and-resource-management.md](memory-and-resource-management.md) — memory growth.
- [caching-engineering.md](caching-engineering.md), [background-task-performance.md](background-task-performance.md).
- [scalability-and-capacity-planning.md](scalability-and-capacity-planning.md) — load shedding, degradation.
- [performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md) — regression benchmarks.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md), [django-integrations](../../django-integrations/SKILL.md),
  [django-testing](../../django-testing/SKILL.md).

## 12. Official documentation and external references

- PostgreSQL — `pg_stat_activity` and statistics views: https://www.postgresql.org/docs/current/monitoring-stats.html
- PostgreSQL — `pg_stat_statements`: https://www.postgresql.org/docs/current/pgstatstatements.html
- PostgreSQL — Server signalling functions (`pg_cancel_backend`): https://www.postgresql.org/docs/current/functions-admin.html
- PostgreSQL — Client connection defaults (`statement_timeout`, `idle_in_transaction_session_timeout`): https://www.postgresql.org/docs/current/runtime-config-client.html
- PostgreSQL — Lock monitoring (`log_lock_waits`): https://www.postgresql.org/docs/current/runtime-config-logging.html
- Django — Database connection pooling (Django ≥ 5.1): https://docs.djangoproject.com/en/5.2/ref/databases/#connection-pool
- Gunicorn — Settings (`timeout`, `max_requests`, `max_requests_jitter`): https://docs.gunicorn.org/en/stable/settings.html
- Redis — `INFO`: https://redis.io/docs/latest/commands/info/
- Redis — Eviction policies: https://redis.io/docs/latest/develop/reference/eviction/
- Celery — Monitoring and management (`inspect`): https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- Git — `git bisect run`: https://git-scm.com/docs/git-bisect
- Google SRE Book — Addressing cascading failures: https://sre.google/sre-book/addressing-cascading-failures/
