---
name: django-performance
description: >-
  Django performance engineering specialist — performance fundamentals (latency, throughput,
  percentiles, Little's Law, SLOs), request/application cost, profiling and observability,
  ORM and database performance, caching engineering, DRF API performance, concurrency and worker
  sizing, async Django, memory and resource management, background-task throughput, file and
  bulk-data processing, HTTP/static delivery, scalability and capacity planning, load testing and
  benchmarking, production performance troubleshooting and performance architecture decisions.
  Load when a page, endpoint, task or command is slow or resource-hungry, when sizing workers or
  connection pools, when adding caching, async or background processing, when planning capacity
  or load tests, or when investigating a production latency, CPU, memory or saturation incident.
project: django-agentic-engineering
abstract: >-
  Measure, locate the bottleneck, apply the least complex effective fix, and prove it with the
  same measurement. A concise investigation workflow and navigation guide here; seventeen focused
  references under references/ loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, PostgreSQL, Redis, Celery, Gunicorn, Uvicorn]
domain: [Software Engineering, Performance]
---

# Django Performance Engineering Specialist

Diagnose and improve the performance of real Django systems: find where time and resources go,
fix the dominant cost, and validate the result with numbers. Baseline: **Django 5.2 LTS, DRF
3.16, PostgreSQL, Redis**; behaviour tied to a version is qualified "(Django ≥ X.Y)", Django 6.0
only where explicitly stated, and uncertain claims are marked `VERIFY`. Third-party tools are
labelled `[third-party: package]` — never assume one is installed. Check the project's lock file,
database backend and deployment model (WSGI/ASGI, server, container limits) before recommending
anything; optimisations do not transfer identically across backends or environments.

## Activate when

- A page, endpoint, task, command or report is slow, or its latency/query count grows with data.
- p95/p99 latency, error rate, CPU, memory, DB connections or queue depth breach a target.
- Sizing Gunicorn/Uvicorn workers, threads, Celery concurrency or connection pools.
- Introducing caching, async views, background processing, read replicas or precomputation.
- Planning capacity, designing load tests or benchmarks, or gating performance regressions.
- Investigating a production incident involving latency, saturation, exhaustion or memory growth.

## Performance engineering principles

1. **Measure before optimising.** No change without a baseline number for the metric it targets.
2. **Percentiles, not averages.** Report p50/p95/p99 with throughput and error rate together.
3. **Find the bottleneck first.** Optimising a non-bottleneck changes nothing the user sees.
4. **Least complex effective fix.** Query shape before indexes, indexes before caching, caching
   before architecture changes — unless measurement says otherwise.
5. **Every optimisation has a cost.** Caches need invalidation, indexes slow writes, async adds
   boundaries, tasks add queue latency and eventual consistency. Name the cost.
6. **Query count is not query cost.** Fewer, heavier queries can be slower; check plans and time.
7. **Throughput ≠ latency.** More workers can raise throughput while worsening tail latency.
8. **Profilers explain one process; monitoring and tracing explain the system.** Use both.
9. **Validate under representative load and data volume**, then guard with a regression check.

### Required justification for any recommendation

State: (1) the bottleneck it addresses, (2) how that bottleneck is measured, (3) why the change
should help, (4) the trade-offs and when it would make things worse, (5) how the result is validated.

## Investigation workflow

1. **Understand the workload and expectations** — user journey, SLO/budget, traffic shape, data
   volume, versions, backend, deployment model → `performance-engineering-fundamentals.md`.
2. **Identify the metrics** — latency percentiles, throughput, error rate, saturation (CPU,
   memory, connections, queue depth).
3. **Establish a measurable baseline** — production telemetry or a reproducible benchmark with
   realistic data → `performance-testing-and-benchmarking.md`.
4. **Locate the bottleneck** — traces → spans → profiles/queries → `profiling-and-observability.md`.
5. **Determine the root cause** — load the owning reference from the table below.
6. **Select the least complex effective optimisation** using the decision guidelines.
7. **Implement** following the owning reference's Django patterns.
8. **Benchmark and validate** — same measurement, same conditions, before vs after.
9. **Evaluate regressions, cost and operational impact** — write paths, memory, consistency,
   infrastructure cost, on-call burden.
10. **Document** the result, the remaining limits and any `VERIFY` items; add a regression guard
    (query-count test, benchmark threshold, alert).

Inspect only the code on the hot path (view/serializer, selector/QuerySet, middleware, task) and
the relevant settings blocks. Load one to three references per step — never all.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [performance-engineering-fundamentals.md](references/performance-engineering-fundamentals.md) | Latency/throughput/concurrency/capacity, percentiles, CPU- vs I/O-bound, Little's Law, queueing, USE/RED, bottlenecks, budgets and SLOs |
| [django-application-performance.md](references/django-application-performance.md) | Request lifecycle cost centres, middleware, URL resolution, views, templates, context processors, startup, request-scoped computation |
| [profiling-and-observability.md](references/profiling-and-observability.md) | cProfile, pyinstrument, py-spy, Debug Toolbar, Silk, OpenTelemetry, tracing, request/query timing, flame graphs, production-safe profiling |
| [orm-performance-optimization.md](references/orm-performance-optimization.md) | N+1 detection, `select_related`/`prefetch_related` cost, column trimming, query count vs cost, aggregation cost, bulk ops, `iterator()`, round trips |
| [database-performance.md](references/database-performance.md) | Index selection and overhead, EXPLAIN/ANALYZE, `pg_stat_statements`, slow queries, connection management and pooling, locks, transaction duration, write amplification |
| [caching-engineering.md](references/caching-engineering.md) | Cache patterns, Django cache framework, Redis, key design, invalidation, TTLs, stampedes, warming, negative/fragment/per-view caching, consistency |
| [api-performance.md](references/api-performance.md) | DRF serialization cost, nested serializers, pagination cost, filtering/ordering, payload size, API caching, throttling cost, bulk and streaming endpoints |
| [concurrency-and-worker-management.md](references/concurrency-and-worker-management.md) | Processes vs threads, GIL, WSGI/ASGI worker models, worker and pool sizing, connection limits, thread safety, backpressure |
| [async-django-performance.md](references/async-django-performance.md) | Async views, sync/async boundaries, thread-sensitive execution, async ORM limits, concurrent I/O, event-loop blocking, when async helps |
| [memory-and-resource-management.md](references/memory-and-resource-management.md) | CPython memory, tracemalloc/memray, leaks, large QuerySets, upload memory, resource cleanup, worker recycling, GC |
| [background-task-performance.md](references/background-task-performance.md) | Offloading decisions, Celery throughput, worker pools, prefetch/acks, batching, retry amplification, idempotency, queue latency and monitoring |
| [file-and-data-processing-performance.md](references/file-and-data-processing-performance.md) | Uploads/downloads, streaming exports, chunked processing, bulk ingestion/COPY, CSV/Excel, images, documents |
| [http-and-static-delivery-performance.md](references/http-and-static-delivery-performance.md) | Static files, CDN, compression, Cache-Control, ETags/conditional requests, keep-alive, HTTP/2/3, payload, SSR |
| [scalability-and-capacity-planning.md](references/scalability-and-capacity-planning.md) | Horizontal/vertical scaling, statelessness, replicas, cache/queue scaling, load balancing, capacity estimation, degradation, load shedding |
| [performance-testing-and-benchmarking.md](references/performance-testing-and-benchmarking.md) | Load/stress/spike/soak tests, benchmark design, workload models, warm-up, percentile analysis, Locust, k6, regression benchmarks |
| [production-performance-troubleshooting.md](references/production-performance-troubleshooting.md) | Symptom runbooks: slow requests, DB, CPU, memory, worker/pool exhaustion, cache failure, queue buildup, intermittent latency, regressions |
| [performance-architecture-decisions.md](references/performance-architecture-decisions.md) | Sync vs async execution, cache vs DB, denormalization, precomputation, materialized views, CQRS/read models, event-driven, cost vs performance |

## Task-based loading

| Task | Load |
|---|---|
| Set an SLO / latency budget, interpret percentiles | performance-engineering-fundamentals.md |
| "This endpoint is slow" (unknown cause) | profiling-and-observability.md (+ production-performance-troubleshooting.md) |
| Query count grows with rows / N+1 | orm-performance-optimization.md |
| One slow query, choosing an index | database-performance.md (+ orm-performance-optimization.md) |
| Slow DRF list endpoint | api-performance.md, orm-performance-optimization.md |
| Slow HTML page / templates / middleware overhead | django-application-performance.md (+ profiling-and-observability.md) |
| Adding or fixing a cache | caching-engineering.md (+ http-and-static-delivery-performance.md for HTTP caching) |
| Sizing workers, threads or connection pools | concurrency-and-worker-management.md, database-performance.md |
| "Should this view be async?" | async-django-performance.md (+ concurrency-and-worker-management.md) |
| Worker memory grows / OOM kills | memory-and-resource-management.md (+ production-performance-troubleshooting.md) |
| Move slow work off the request; Celery queue backlog | background-task-performance.md |
| Large import/export, CSV/Excel, image or PDF generation | file-and-data-processing-performance.md (+ background-task-performance.md) |
| Static assets, CDN, compression, cache headers | http-and-static-delivery-performance.md |
| Plan for 10× traffic, read replicas, load shedding | scalability-and-capacity-planning.md (+ performance-architecture-decisions.md) |
| Write a load test or CI performance gate | performance-testing-and-benchmarking.md |
| Production incident: latency, CPU, pool exhaustion, queue buildup | production-performance-troubleshooting.md (+ the owner it points to) |
| Precompute, denormalize, materialized view, read model | performance-architecture-decisions.md (+ database-performance.md) |
| Review a change for performance risk | orm-performance-optimization.md, api-performance.md, then the owner of each finding |

## Skill pairings and handoffs

This skill owns **diagnosis, optimisation, benchmarking and capacity**; the paired skill owns
**how the mechanism works**.

| Topic | Skill |
|---|---|
| Request lifecycle, middleware, cache framework, async and ASGI/WSGI mechanics | [django-core](../django-core/SKILL.md) |
| Architectural styles, service layer, events, scalability patterns, ADRs | [django-architecture](../django-architecture/SKILL.md) |
| QuerySet semantics, expressions, indexes/constraints API, transactions, bulk mechanics | [django-orm](../django-orm/SKILL.md) |
| Serializers, pagination, filters, throttling implementation | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Query-count tests, test-suite speed, CI automation | [django-testing](../django-testing/SKILL.md) |
| PostgreSQL internals, plan reading in depth, vacuum, partitioning, replication | [postgresql-engineering](../postgresql-engineering/SKILL.md) |
| Celery, brokers, external API clients, webhooks | [django-integrations](../django-integrations/SKILL.md) |
| ETL, import/export pipelines | [django-data-processing](../django-data-processing/SKILL.md) |
| Cache-key data leaks, DoS exposure of expensive endpoints | [django-security-engineering](../django-security-engineering/SKILL.md) |

Typical pairings: slow list API → api-performance + orm-performance-optimization (with
django-rest-framework pagination); pool exhaustion → concurrency-and-worker-management +
database-performance (with postgresql-engineering); performance regression gate →
performance-testing-and-benchmarking (with django-testing performance-and-reliability-testing).

## Optimisation decision guidelines

| Symptom (measured) | First consider | Escalate to | Watch for |
|---|---|---|---|
| Query count scales with rows | `select_related`/`prefetch_related`, annotations | Read model / denormalized field | JOIN fan-out, memory of large prefetches |
| One query dominates DB time | Rewrite query, trim columns, index backed by `EXPLAIN (ANALYZE, BUFFERS)` | Precompute / materialized view | Index write and storage cost; planner on other backends |
| Python time in serializers/templates | Lighter serializer, `values()`, fragment cache | Precomputation, payload redesign | Stale data, invalidation bugs |
| Expensive, repeated, shareable read | Low-level cache with explicit key, TTL and invalidation | HTTP/CDN caching | Stampedes, per-user leaks, low hit ratio |
| Slow work the user needn't wait for | Background task enqueued `on_commit` | Event-driven pipeline | Queue latency, retries, idempotency |
| Many concurrent outbound I/O waits | Async view with bounded concurrency (ASGI) | Dedicated async service | Sync boundaries, blocking calls in the loop |
| Requests queue at the server | Measure service time; fix slow paths; size workers from Little's Law | Horizontal scaling | DB connection limits, memory per worker |
| Memory grows per worker | Find the leak (tracemalloc/memray); stream/iterate | Worker recycling as mitigation | Masking leaks with `max_requests` |
| DB saturated by reads | Query/index fixes, caching | Read replicas with routing | Replication lag, read-your-writes |

## Validation checklist

- [ ] Baseline and after-change numbers recorded with the same method, data volume and load.
- [ ] Metrics reported as percentiles plus throughput and error rate, not averages alone.
- [ ] The bottleneck was identified by measurement (trace, profile, plan), not assumed.
- [ ] Each change states bottleneck, measurement, mechanism, trade-offs and validation.
- [ ] Query changes checked for both count and cost (plans/timings), on the production backend.
- [ ] New indexes justified against write/storage overhead; built without blocking writes.
- [ ] Caches have keys covering every varying dimension, a TTL, invalidation and stampede handling.
- [ ] Worker, thread and pool sizes derived from measurement and fit DB `max_connections` and memory limits.
- [ ] Async code has no blocking calls on the event loop and was benchmarked against sync.
- [ ] Background tasks are idempotent, bounded in retries, and their queue latency is monitored.
- [ ] A regression guard exists (query-count test, benchmark threshold or alert).
- [ ] Version-, backend- and third-party-dependent claims are qualified; `VERIFY` items listed.
- Docs: <https://docs.djangoproject.com/en/5.2/topics/performance/> ·
  <https://docs.djangoproject.com/en/5.2/topics/db/optimization/>
