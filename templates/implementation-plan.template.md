---
name: implementation-plan
description: >-
  Output template for an ordered Django implementation plan: objective, prerequisites, work items
  with owning agent, inputs, acceptance criteria and dependencies, verification, rollout, rollback
  and open questions.
kind: output-template
agents: [django-architecture-agent]
commands: [django-design]
project: django-agentic-engineering
abstract: >-
  Standard structure for turning an agreed Django design into dependency-ordered, verifiable work
  items, each owned by one agent.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, AI Agents]
---

# Implementation Plan Template

## Purpose

Break agreed work into small, ordered items that each have one owner, clear inputs and acceptance
criteria someone else can check, so implementation can proceed without re-deciding the design.

## When to use

- After an architecture design or decision is accepted and the work must be sequenced.
- A change spans several apps or several agents (model, API, tests, documentation).
- Not for making design decisions; those belong in the
  [architecture design](architecture-design.template.md). The plan links to it.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | Feature or change being planned | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | What the plan delivers and excludes | design |
| `{{prepared_by}}` | Agent or person producing the plan | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `draft`, `approved`, `in-progress` or `done` | producer |
| `{{sources}}` | Architecture design, ADRs, requirements | inputs |
| `{{objective}}` | What is true when the plan is complete | design |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Objective | required | The outcome and how completion is recognised | acceptance criteria |
| Prerequisites | required | Decisions, access, data or dependencies needed before work starts | — |
| Work items | required | Ordered items with owner, inputs, acceptance criteria, dependencies | each item checkable |
| Requirement traceability | optional | Each requirement mapped to the items that deliver it | no unmapped requirement |
| Verification | required | Tests and checks proving each item and the whole | named commands |
| Rollout | optional | Deployment order, feature flags, migration sequencing | migration plan if risky |
| Rollback | optional | How to undo each deployable step | — |
| Risks and open questions | required | What could block or change the plan | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions).
- Each work item has exactly one owning agent and is small enough to review as one change.
- Items are ordered so that no item depends on a later one; migrations precede code that needs
  them, and contract steps follow the code that stops using old structures.
- Risky schema changes reference a [migration plan](migration-plan.template.md).

## Output structure

````markdown
# Implementation Plan — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Objective
{{objective}}

## Prerequisites
| Prerequisite | Status | Owner |
|---|---|---|

## Work items
| # | Work item | Agent | Inputs | Depends on | Acceptance criteria |
|---|---|---|---|---|---|

## Requirement traceability
| Requirement | Work items |
|---|---|

## Verification
| Work item | Check | Command or test |
|---|---|---|

## Rollout

## Rollback

## Risks and open questions
````

## Completion checklist

- [ ] Every work item has one owning agent, inputs and checkable acceptance criteria.
- [ ] Items are ordered by dependency with no forward references.
- [ ] Every requirement in scope maps to at least one work item.
- [ ] Verification names the test or command for each item.
