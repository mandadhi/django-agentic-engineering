---
name: code-review-report
description: >-
  Output template for a read-only Django code review: verdict, scope with the skills and rules
  applied per file, mechanical checks, severity-ranked findings with file:line and rule ids, and
  recommended follow-up.
kind: output-template
agents: [django-code-review-agent]
commands: [django-review]
project: django-agentic-engineering
abstract: >-
  Standard structure for a Django gate review whose verdict follows from its findings and whose
  every finding is located, explained and assigned to an owner.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, Git]
domain: [Software Engineering, Quality Assurance]
---

# Code Review Report Template

## Purpose

Record the outcome of reviewing a Django change before merge: whether it can merge, what must
change, where, why it matters, and who should fix it.

## When to use

- Reviewing a diff, branch, pull request or named set of files in a Django project.
- Not for a full security audit or profiling session; recommend a
  [security assessment](security-assessment.template.md) or
  [performance assessment](performance-assessment.template.md) instead.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | The change reviewed (branch, PR or paths) | task objective |
| `{{project_name}}` | Name of the Django project | repository |
| `{{scope}}` | Base and head refs, or the paths when not diff-scoped | task inputs |
| `{{prepared_by}}` | Agent or person producing the review | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `complete` or `partial` | producer |
| `{{sources}}` | PR description or acceptance criteria used | task inputs |
| `{{verdict}}` | `APPROVE`, `APPROVE-WITH-COMMENTS` or `CHANGES-REQUESTED` | findings and checks |
| `{{verdict_reason}}` | One-line reason for the verdict | findings |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Verdict | required | The verdict and its one-line reason | follows from Findings and Mechanical checks |
| Scope | required | Each changed file, its classification, and the skills and rules applied | `git diff --stat` |
| Mechanical checks | required | Each check with command and result, `not-run` with the reason | command output |
| Findings | required | Findings ordered by severity | `file:line` per finding |
| Not reviewed | optional | Files skipped and why (size, binary, out of scope) | — |
| Recommended follow-up agents | required | Agents or commands for fixes and deeper reviews | finding ids |
| Machine-readable summary | optional | The fenced JSON findings block defined by the producing agent | JSON parses |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Use the shared checklists that
  match each changed file ([security](../references/django-security-checklist.md),
  [API](../references/django-api-design-checklist.md),
  [performance](../references/django-performance-checklist.md),
  [migrations](../references/django-migration-safety.md),
  [tests](../references/django-testing-strategy.md)).
- A violated MUST is at least `medium`; a violated `SEC-M*` is at least `high`. Cite the id.
- Pre-existing issues appear only when the change makes them worse, labelled `pre-existing`.
- A check that did not run is `not-run`, never `passed`.

## Output structure

````markdown
# Code Review — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Verdict
**{{verdict}}** — {{verdict_reason}}

## Scope
| File | Classification | Skills applied | Rules applied |
|---|---|---|---|

## Mechanical checks
| Check | Command | Result |
|---|---|---|

## Findings
| Id | Severity | Dimension | Location | Issue | Why | Suggestion | Rule | Owner | Pre-existing |
|---|---|---|---|---|---|---|---|---|---|

## Not reviewed
| File | Reason |
|---|---|

## Recommended follow-up agents
| Finding ids | Agent | Command |
|---|---|---|

## Machine-readable summary
````

## Completion checklist

- [ ] Every changed file is in Scope or in Not reviewed with a reason.
- [ ] Every finding has a location, severity, dimension, explanation, suggestion and owner.
- [ ] Mechanical checks record real results or `not-run` with the reason.
- [ ] The verdict follows from the findings and checks.
