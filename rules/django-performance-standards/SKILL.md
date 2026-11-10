---
name: django-performance-standards
description: >-
  Mandatory Django engineering rule for performance: no N+1 queries, bounded querysets,
  database-side aggregation, efficient bulk writes, offloading slow work, correctly keyed and
  invalidated caches, measured optimisation, and indexes for new access patterns. Apply when
  writing request-path code, queries, caching or batch work, and when optimising.
project: django-agentic-engineering
abstract: >-
  Enforceable performance standards that prevent the common Django scalability defects and
  require evidence for every optimisation.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Performance]
---

# Rule: Django Performance Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Prevent the predictable performance defects in Django code, and keep optimisation grounded in measurement.

## Scope

Applies to ORM usage, request-path work, caching, bulk processing and background offloading.
Schema-level index changes also follow `django-database-standards`.

## Mandatory requirements

- **PRF-M1** Code that iterates a queryset and reads relations loads them with
  `select_related()` or `prefetch_related()`. No query runs per row.
- **PRF-M2** Querysets on request paths are bounded, by pagination or slicing. Large batch
  jobs stream rows (`iterator()` with `chunk_size`, or keyed batches).
- **PRF-M3** Counting, existence checks, sums and filtering run in the database (`count()`,
  `exists()`, `aggregate()`, `annotate()`), not by loading rows into Python.
- **PRF-M4** Writes of many rows use `bulk_create()`, `bulk_update()` or `update()` with a
  batch size, and the change accounts for the skipped `save()` methods and signals.
- **PRF-M5** Work that can exceed the request's time budget (slow external calls, large
  exports, heavy computation) is not done synchronously in the request. It goes to the
  project's background mechanism.
- **PRF-M6** Cache keys include every input that changes the cached value: user, tenant,
  permissions, language and version. Every cache entry has a timeout and a defined
  invalidation path. Data scoped to one user is never stored under a shared key.
- **PRF-M7** An optimisation is justified by a measurement (query count, timing or query plan)
  taken before and after the change.
- **PRF-M8** A new filter or ordering on a large table comes with a reviewed index decision.
- **PRF-M9** Outbound network calls set explicit timeouts.

## Recommendations

- **PRF-R1** Use `only()`, `defer()` or `values()` for wide models when only a few columns are needed.
- **PRF-R2** Prefer cursor or keyset pagination for very large or fast-changing lists.
- **PRF-R3** Keep profiling tools (debug toolbar, silk) in development dependencies only.
- **PRF-R4** Lock in fixed hot paths with query-count tests.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `for o in qs: o.customer.name` without `select_related` | `select_related("customer")` | PRF-M1 |
| `len(Model.objects.all())` | `.count()` | PRF-M3 |
| `if qs:` just to test existence | `.exists()` | PRF-M3 |
| `save()` in a loop over thousands of rows | `bulk_update()` / `update()` | PRF-M4 |
| Generating a large export inside the view | Background task plus a download link | PRF-M5 |
| `cache.set("dashboard", data)` holding per-user data | Key that includes the user/tenant scope | PRF-M6 |
| `requests.get(url)` with no timeout | `timeout=` set explicitly | PRF-M9 |

## Security and maintainability

Badly keyed caches leak data between users. Unbounded queries are a denial-of-service vector.
Measured changes avoid complexity that has no benefit.

## Verification

Query-count assertions in tests. `CaptureQueriesContext` on the changed paths. `EXPLAIN` for new
heavy queries. Grep for `.all()` in loops, `len(` on querysets, `cache.set(` keys and `requests.` calls.

## Related skills

[django-performance](../../skills/django-performance/SKILL.md) (method, caching, API and background performance) ·
[django-orm](../../skills/django-orm/SKILL.md) (query optimisation, bulk operations) ·
[postgresql-engineering](../../skills/postgresql-engineering/SKILL.md) (indexes and query plans).
