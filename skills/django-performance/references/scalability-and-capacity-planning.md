# Scalability and Capacity Planning

**Scope:** How a Django system grows to handle more load — scaling axes, statelessness,
database/cache/queue scaling limits, load balancing, capacity estimation, resource limits,
graceful degradation and load shedding. Architectural patterns for scale are owned by
[django-architecture scalability-patterns](../../django-architecture/references/scalability-patterns.md);
worker sizing math by [concurrency-and-worker-management.md](concurrency-and-worker-management.md).

## 1. Purpose and scope

Scaling is about **which resource saturates first** and what it costs to move that limit.
This reference helps an agent:

- Estimate capacity needs from traffic and service-time measurements.
- Identify which tier (workers, database, cache, queue) bounds throughput.
- Make the app horizontally scalable (stateless) before adding instances.
- Plan read replicas, cache clusters and queue scaling with their consistency costs.
- Design graceful degradation and load shedding so overload fails partially, not totally.

Queueing theory (Little's Law, utilisation vs latency): [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md).
PostgreSQL replication and partitioning internals: [postgresql-engineering](../../postgresql-engineering/SKILL.md).

## 2. Core concepts

| Concept | Meaning for Django |
|---|---|
| Vertical scaling | Bigger instance; simple, bounded by largest machine and cost curve |
| Horizontal scaling | More app instances behind a load balancer; requires stateless workers |
| Scaling bottleneck | The first saturated resource — often DB connections or write throughput, not app CPU |
| Headroom | Spare capacity at peak (commonly target ≤ 60–70% utilisation; illustrative) |
| Elasticity | Autoscaling speed vs traffic ramp speed |
| Degradation | Serving reduced functionality instead of errors |
| Load shedding | Rejecting excess work early and cheaply (429/503) |

App servers scale roughly linearly until a **shared** resource saturates: the primary
database, a Redis instance, a downstream API's rate limit, or the queue broker.

## 3. Internal mechanics

### Stateless application tier

Requirements before adding a second instance:

- Sessions in database, cache (`django.contrib.sessions.backends.cache`/`cached_db`) or signed
  cookies — never in local files.
- Uploaded/generated files in shared object storage (`STORAGES["default"]`), not local disk.
- No correctness-relevant `LocMemCache` state (rate limits, locks, invalidated data) — each
  process has its own.
- Scheduled jobs run once (Celery beat single instance or a lock), not once per web instance.
- Migrations run as a release step, not on every container start.

### Database boundary

- Each app process holds connections: `instances × workers × threads` (persistent connections
  with `CONN_MAX_AGE`) or a pool per process (`OPTIONS["pool"]`, Django ≥ 5.1 with psycopg 3
  [third-party: psycopg[pool]]). Doubling instances doubles connections; PostgreSQL
  `max_connections` and per-connection memory become the ceiling — hence PgBouncer
  [third-party: PgBouncer] in transaction mode (VERIFY server-side cursor and prepared
  statement compatibility: `DISABLE_SERVER_SIDE_CURSORS = True` is required with transaction pooling).
- Reads scale via replicas and caches; **writes do not scale horizontally** on a single
  primary — they scale by reducing write volume, batching, partitioning or sharding.

### Read replicas

```python
class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        return "replica"
    def db_for_write(self, model, **hints):
        return "default"
    def allow_relation(self, obj1, obj2, **hints):
        return True
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "default"

DATABASE_ROUTERS = ["config.routers.PrimaryReplicaRouter"]
```

Replication is asynchronous by default: a user who just wrote may read stale data from the
replica (read-your-writes violation). Mitigations: pin the user to primary for N seconds after
a write (cookie/session flag), route reads inside a transaction to primary, use
`.using("default")` for post-write reads, monitor lag (`pg_stat_replication.replay_lag`).

### Cache and queue tiers

- Redis is single-threaded per shard; one hot key saturates one core regardless of cluster
  size. Redis Cluster shards by key hash slot — multi-key operations need hash tags.
- Celery throughput scales with workers until the broker, the DB or a downstream API saturates
  ([background-task-performance.md](background-task-performance.md)).

### Load balancing

| Algorithm | Behaviour | Fit |
|---|---|---|
| Round robin | Equal distribution | Uniform, short requests |
| Least connections / least outstanding requests | Avoids busy instances | Mixed request durations (typical Django) |
| Consistent hashing | Sticky by key | Local caches; hurts balance |

Health checks: a **liveness** endpoint that does not touch the DB (restart only dead processes)
and a **readiness** endpoint that checks critical dependencies cheaply with timeouts. A
readiness check that queries a struggling DB makes every instance "unhealthy" at once and
turns a slowdown into a full outage.

## 4. Engineering decisions and trade-offs

| Decision | Prefer A when | Prefer B when |
|---|---|---|
| Vertical vs horizontal | DB tier; early stage; single-tenant workloads | Stateless app tier; need for redundancy and rolling deploys |
| Replicas vs cache | Many distinct queries, moderate staleness OK | Few hot keys, high read rate |
| Autoscale on CPU vs request latency/queue depth | CPU-bound workers | I/O-bound workers (CPU stays low while queueing grows) |
| Shed at proxy vs app | Cheap, protects workers, coarse | Needs identity/tenant/priority awareness |
| Degrade vs fail | Non-critical features (recommendations, counts) | Correctness-critical paths (payments) — fail fast, clearly |

Adding app instances when the database is the bottleneck **reduces** throughput: more
connections and contention on the saturated resource.

## 5. Django implementation patterns

### Capacity estimation (illustrative worked example)

```text
Peak traffic:          600 req/s  (measured p99-minute from last quarter, × 1.5 growth)
Mean service time:     80 ms      (APM, includes DB wait)
Concurrency needed:    600 × 0.08 = 48 in-flight requests   (Little's Law)
Target utilisation:    60%  → 48 / 0.6 = 80 worker slots
Per instance:          gthread, 4 workers × 4 threads = 16 slots  (sized per concurrency ref)
Instances:             80 / 16 = 5 → 6 for N+1 redundancy
DB connections:        6 × 16 = 96 at worst (+ Celery, + admin) vs max_connections, or pool/PgBouncer
DB load check:         600 req/s × 6 queries/req = 3,600 QPS — validate against DB benchmark
```

Validate the estimate with a load test at 1.0× and 1.5× peak
([performance-testing-and-benchmarking.md](performance-testing-and-benchmarking.md)); the
model ignores tail latency and contention, which grow non-linearly above ~70% utilisation.

### Container resource limits vs workers

- Worker count derived from `os.cpu_count()` sees **host** CPUs, not the container's CPU quota;
  set worker counts explicitly from configuration.
- Memory limit ≥ `workers × peak RSS per worker` + headroom; otherwise the OOM killer removes
  workers mid-request. Use `max_requests` + jitter for slow leaks
  ([memory-and-resource-management.md](memory-and-resource-management.md)).
- CPU throttling (CFS quota) causes latency spikes invisible in average CPU; watch
  `container_cpu_cfs_throttled_periods_total`.

### Graceful degradation

```python
def recommendations_for(user):
    if not flags.is_enabled("recommendations"):          # kill switch flipped under load
        return []
    try:
        return cache.get(f"recs:{user.pk}") or recs_client.fetch(user.pk, timeout=0.3)
    except (RecsTimeout, RecsUnavailable):
        metrics.incr("recs.degraded")
        return []                                          # page still renders
```

Patterns: feature kill switches, serving stale cache entries, timeouts plus fallbacks for
non-critical dependencies, circuit breakers on downstream APIs, and disabling expensive
`COUNT(*)` totals under load.

### Load shedding and rate limiting

- At the proxy: nginx `limit_req`/`limit_conn`, ALB/WAF rules — cheapest, protects workers.
- At the app: DRF throttles ([throttling-and-rate-limiting](../../django-rest-framework/references/throttling-and-rate-limiting.md))
  for per-user/tenant fairness; each check costs a cache round trip.
- Return `429 Too Many Requests` (client over quota) or `503 Service Unavailable` (server
  overloaded) with `Retry-After`; clients must back off with jitter.
- Shed by priority: health checks and checkout before exports and search suggestions.
- Bound queues: a request queued longer than the client timeout is wasted work — keep
  proxy/upstream timeouts aligned (client < LB < Gunicorn `timeout`).

## 6. Production use cases

- **Flash sale (10× spike in 2 minutes):** autoscaling took 4 minutes; pre-scaling on schedule,
  cached product pages and a waiting-room queue kept checkout available.
- **Replica rollout:** reports moved to a replica cut primary CPU by 40% (illustrative), but
  "order not found" after checkout appeared — fixed by primary pinning after writes.
- **Connection ceiling:** scaling web from 10 to 30 pods hit `FATAL: too many connections`;
  PgBouncer in transaction mode + smaller per-process pools resolved it.
- **Thundering herd after outage:** all clients retried immediately on recovery; exponential
  backoff with jitter and `Retry-After` smoothed the restart.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Horizontal app scaling | Redundancy, rolling deploys, near-linear for app CPU | Multiplies DB connections; needs statelessness |
| Vertical DB scaling | No code changes | Cost curve, hard ceiling, restart for resize |
| Read replicas | Offload reads, isolate analytics | Replication lag, routing complexity |
| Load shedding | Protects core paths | Rejected users; needs client cooperation |
| Degradation | Partial availability | Feature complexity, needs testing |

## 8. Security and performance considerations

- Rate limiting is also abuse protection; key by authenticated identity where possible, by IP
  only with correct `X-Forwarded-For` handling (trust only your proxies).
- Expensive unauthenticated endpoints (search, exports, password reset) are DoS amplifiers —
  shed or throttle them first.
- Replicas must enforce the same access controls; analytics users on replicas should be
  read-only roles.
- Autoscaling without a maximum is a cost-amplification risk under attack.

## 9. Common mistakes and anti-patterns

- Scaling app instances to fix a database bottleneck.
- Readiness checks that depend on the database or downstream APIs without timeouts.
- `workers = 2 × cpu_count() + 1` computed from host CPUs inside a CPU-limited container.
- Local-memory sessions, files or rate limits in a multi-instance deployment.
- Reading from replicas immediately after writes.
- Unbounded retries and queues; client timeouts shorter than server timeouts (work continues
  for disconnected clients).
- Capacity planning from averages instead of peak-minute traffic and p95/p99 latency.

## 10. Troubleshooting and validation

| Signal | Indicates |
|---|---|
| Latency rises, app CPU low, DB CPU/IO high | DB bound — scale DB / reduce queries, not app |
| Latency rises, app CPU high | App bound — profile, then scale out |
| Request queue time grows (proxy `upstream_connect_time`, `X-Request-Start` header delta) | Worker saturation |
| `too many connections` / pool timeouts | Connection ceiling — pooling, fewer slots |
| Replica `replay_lag` > seconds | Stale reads, check write bursts and replica size |
| CPU throttled periods high | Container CPU limit too low for worker count |

Validation steps: load test to find the knee of the latency curve; record the saturating
resource; repeat after each change; run a failure drill (kill an instance, a replica, Redis) to
confirm degradation paths. See [production-performance-troubleshooting.md](production-performance-troubleshooting.md).

## 11. Related references and skills

- [django-architecture scalability-patterns](../../django-architecture/references/scalability-patterns.md) — architectural scale patterns.
- [concurrency-and-worker-management.md](concurrency-and-worker-management.md) — worker and pool sizing.
- [database-performance.md](database-performance.md) — connection pooling, query load.
- [caching-engineering.md](caching-engineering.md) — cache-backed degradation.
- [background-task-performance.md](background-task-performance.md) — queue scaling.
- [performance-engineering-fundamentals.md](performance-engineering-fundamentals.md) — Little's Law, utilisation.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — replication, partitioning.
- [django-rest-framework](../../django-rest-framework/SKILL.md) — throttling mechanics.

## 12. Official documentation and external references

- Django multiple databases and routers: https://docs.djangoproject.com/en/5.2/topics/db/multi-db/
- Django database connection management / pooling: https://docs.djangoproject.com/en/5.2/ref/databases/#connection-management
- Django PostgreSQL connection pool (Django ≥ 5.1): https://docs.djangoproject.com/en/5.2/ref/databases/#connection-pool
- Django transaction pooling note: https://docs.djangoproject.com/en/5.2/ref/databases/#transaction-pooling-server-side-cursors
- PostgreSQL replication monitoring: https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-REPLICATION-VIEW
- PostgreSQL `max_connections`: https://www.postgresql.org/docs/current/runtime-config-connection.html
- Redis Cluster specification: https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/
- Gunicorn design and worker sizing: https://docs.gunicorn.org/en/stable/design.html
- RFC 9110 `Retry-After`: https://www.rfc-editor.org/rfc/rfc9110#field.retry-after
- Google SRE book, "Handling Overload": https://sre.google/sre-book/handling-overload/
- PgBouncer [third-party]: https://www.pgbouncer.org/config.html
