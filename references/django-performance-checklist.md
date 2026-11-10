---
name: django-performance-checklist
description: >-
  Shared checklist for finding and verifying Django performance problems: symptoms mapped to likely
  causes, code patterns to search for, how to measure each on the test database, and the rule and
  skill reference behind each item.
kind: shared-reference
agents: [django-performance-agent, django-code-review-agent]
project: django-agentic-engineering
abstract: >-
  Symptom-to-cause table, search patterns for common Django performance anti-patterns and a
  measurement toolkit, mapped to performance rule ids and skill references.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Performance]
---

# Django Performance Checklist

## Purpose

Help agents find likely performance problems quickly and prove them with measurements, so that
assessments and reviews name the same causes and claim only measured gains.

## Scope

Django request paths, ORM usage, serializers, caching and background work, measured on the test
database. Infrastructure tuning (servers, database configuration) is out of scope except as a
recommendation for a human.

## How to use

1. Start from the symptom, or from the changed files in a review.
2. Search for the matching patterns; each hit is a candidate.
3. Measure the candidate with the toolkit before and after any change.
4. Record the result in the [performance assessment](../templates/performance-assessment.template.md)
   or as a finding in the [code review report](../templates/code-review-report.template.md).

## Symptom to likely cause

| Symptom | Likely causes | First measurement |
|---|---|---|
| Query count grows with the number of rows | Related objects read in a loop, serializer method fields querying, `__str__` touching relations | Query count at two page sizes |
| One slow query | Missing index, non-sargable filter, large `OFFSET`, unbounded sort | `QuerySet.explain()` on the test database |
| Slow page with few queries | Python work per row, template work, serialization of large payloads | Profiler on the view |
| Memory spikes | Whole querysets loaded, large exports in the request, file contents in memory | Peak memory of the path |
| Slow writes | Per-row `save()` in loops, signals on each save, long transactions | Query count and time of the write path |
| Latency tied to third parties | Outbound calls in the request without timeouts | Time spent in the client call |
| Slow admin pages | `list_display` on relations, missing `list_select_related`, `search_fields` on unindexed columns | Query count of the changelist |

## Patterns to search

| Pattern | Search hint | Rule | Skill reference |
|---|---|---|---|
| Relation access in loops without loading | `for .* in .*\.all\(\)` followed by `\.\w+\.\w+`; serializer `source="rel.field"` | `PRF-M1`, `API-M8` | [ORM optimisation](../skills/django-performance/references/orm-performance-optimization.md) |
| Unbounded querysets on request paths | `.all()` or `.filter(` returned without slicing or pagination | `PRF-M2` | [database performance](../skills/django-performance/references/database-performance.md) |
| Counting or summing in Python | `len(` on a queryset, `sum(` over a queryset, `if queryset:` | `PRF-M3` | [query optimisation](../skills/django-orm/references/query-optimization.md) |
| Per-row writes | `.save()` inside `for` loops, `create()` in loops | `PRF-M4` | [bulk operations](../skills/django-orm/references/bulk-operations-and-batching.md) |
| Slow work in the request | outbound HTTP, file generation, email sending in views | `PRF-M5`, `PRF-M9` | [background task performance](../skills/django-performance/references/background-task-performance.md) |
| Cache keys missing inputs | `cache.get(`/`cache.set(`, `cache_page` on per-user views | `PRF-M6` | [caching engineering](../skills/django-performance/references/caching-engineering.md) |
| New filter or ordering without an index decision | new `filter(`/`order_by(` fields on large tables | `PRF-M8` | [indexing and query plans](../skills/postgresql-engineering/references/indexing-and-query-plans.md) |
| Queries in `__str__` or listing properties | `def __str__` returning related attributes | `MOD-M8` | [ORM anti-patterns](../skills/django-orm/references/orm-anti-patterns.md) |

## Measurement toolkit

| Measure | Tool | Note |
|---|---|---|
| Query count in a test | `assertNumQueries` or `django_assert_num_queries` | Also locks in the fix as a regression test (`PRF-R4`) |
| Queries of a code path | `django.test.utils.CaptureQueriesContext` | Works without `DEBUG = True` |
| Query plan | `QuerySet.explain()` (options depend on the backend) | Run on the test database with representative volume |
| Time of a path | `time.perf_counter()` around the call, repeated runs | Report the median and the number of runs |
| Profile of a path | `cProfile` or a sampling profiler on the test client call | Only tools already in development dependencies (`PRF-R3`) |
| Memory | `tracemalloc` snapshots around the call | Compare peaks, not single allocations |

A gain is claimed only from a before and after measurement in the same environment (`PRF-M7`).
Without representative data volume, label conclusions `estimated`.

## Related skills and rules

- Skills: [django-performance](../skills/django-performance/SKILL.md)
  ([profiling and observability](../skills/django-performance/references/profiling-and-observability.md),
  [performance testing](../skills/django-performance/references/performance-testing-and-benchmarking.md)),
  [django-orm](../skills/django-orm/SKILL.md),
  [postgresql-engineering](../skills/postgresql-engineering/SKILL.md).
- Rules: [django-performance-standards](../rules/django-performance-standards/SKILL.md),
  [django-database-standards](../rules/django-database-standards/SKILL.md),
  [django-model-standards](../rules/django-model-standards/SKILL.md).
