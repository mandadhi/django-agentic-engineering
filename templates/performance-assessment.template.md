---
name: performance-assessment
description: >-
  Output template for a measured Django performance assessment: targets and method, baseline
  measurements, findings with evidence, changes with before and after values, recommendations
  for other owners, verification and limitations.
kind: output-template
agents: [django-performance-agent]
commands: [django-optimize]
project: django-agentic-engineering
abstract: >-
  Standard structure for Django performance work in which every claimed gain is a measured
  before-and-after pair and every estimate is labelled as one.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Performance]
---

# Performance Assessment Template

## Purpose

Report where a Django request path, task or query spends its time, what was changed and what the
change measurably achieved, separating measured results from estimates.

## When to use

- Investigating a slow endpoint, view, task, admin page or query.
- Reporting an optimisation, with its baseline and post-change measurements.
- Assessing a design for performance risks before it is built (findings only, no changes).
- Not for functional test results; use the [test plan](test-plan.template.md).

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | Path, task or query assessed | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Paths in scope and excluded | task |
| `{{prepared_by}}` | Agent or person producing the assessment | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `complete`, `partial` or `blocked` | producer |
| `{{sources}}` | Revision measured, reported symptoms | inputs |
| `{{environment}}` | Where measurements ran: database, data volume, settings | measurement setup |
| `{{performance_targets}}` | Stated targets (query count, latency, memory), or `none stated` | task or requirements |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Targets and method | required | Targets, environment, data volume, tools, how each path was measured | reproducible commands |
| Baseline | required | Measurements per path before any change | command output |
| Findings | required | Bottlenecks ranked by measured cost, tied to code | location and evidence |
| Changes | optional | Each applied change with before and after values and its regression test | test name |
| Recommendations | required | Changes not applied (migrations, caching, architecture) with owner | — |
| Verification | required | Commands run after the change and their results | command output |
| Limitations | required | Data volume, environment differences, estimates, noise | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Use the
  [performance checklist](../references/django-performance-checklist.md) to choose measurements.
- A gain is claimed only with a measured before and after on the same environment (`PRF-M7`).
  Static findings without measurement are labelled `estimated`.
- A change inside measurement noise is not reported as a gain.
- Measurements run against the test database only; never production or shared systems.

## Output structure

````markdown
# Performance Report — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Targets and method
- Targets: {{performance_targets}}
- Environment: {{environment}}

## Baseline
| Path | Queries | Duplicate queries | Time | Heaviest query plan | Command |
|---|---|---|---|---|---|

## Findings
| # | Location | Problem | Evidence (queries/time/plan) | Impact |
|---|---|---|---|---|

## Changes
| # | Change | Before | After | Test |
|---|---|---|---|---|

## Recommendations
| # | Recommendation | Expected effect (measured/estimated) | Owner agent |
|---|---|---|---|

## Verification
| Command | Result |
|---|---|

## Limitations
````

## Completion checklist

- [ ] Every path in scope has a baseline, or a stated reason why it could not be measured.
- [ ] Every applied change has before and after values and a regression test.
- [ ] Every estimate is labelled `estimated`; no gain within noise is claimed.
- [ ] Recommendations outside the producer's scope name an owner.
