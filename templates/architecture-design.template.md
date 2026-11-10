---
name: architecture-design
description: >-
  Output template for a Django architecture design: requirements and quality attributes, current
  state, decisions, target app structure, dependency and security boundaries, component diagram,
  implementation plan, risks and open questions.
kind: output-template
agents: [django-architecture-agent]
commands: [django-design]
project: django-agentic-engineering
abstract: >-
  Standard structure for a Django architecture design that other agents can implement without
  guessing, with each decision traceable to a requirement and each boundary checkable.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework, Mermaid]
domain: [Software Engineering, AI Agents]
---

# Architecture Design Template

## Purpose

Describe the structure a Django feature, refactoring or new system will have: which app owns
what, how layers and apps may depend on each other, where security, transaction and integration
boundaries sit, and the order in which to build it.

## When to use

- A feature or refactoring needs new apps, moved responsibilities or new cross-app interactions.
- Several agents will implement parts of one change and need a shared target.
- Not for a single decision with no structural effect; use the
  [architecture decision record](architecture-decision-record.template.md) alone.
- Not for sequencing already-agreed work; use the [implementation plan](implementation-plan.template.md).

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | Feature, refactoring or system being designed | task objective |
| `{{project_name}}` | Name of the Django project | project profile or settings package |
| `{{scope}}` | What the design covers and excludes | task objective and constraints |
| `{{prepared_by}}` | Agent or person producing the design | producer |
| `{{date}}` | Date of the design (ISO 8601) | producer |
| `{{document_status}}` | `draft`, `proposed`, `accepted` or `blocked` | producer |
| `{{sources}}` | Requirements documents, project profile, revision | task inputs and context |
| `{{design_summary}}` | Two to four sentences: the shape of the solution | design |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Summary | required | The shape of the solution in brief | the sections below |
| Context and requirements | required | Functional requirements, quality attributes, constraints, assumptions | requirement ids or task text |
| Current state | required | Relevant apps, layering and conventions the design must respect | `file:line` or project profile |
| Decisions | required | One entry per significant decision, in ADR form | each ADR lists options considered |
| Target structure | required | One row per app: what it owns, its public interface, its dependencies | table |
| Dependency and boundary rules | required | Allowed import and call directions, stated so they can be checked | a rule a reviewer or linter can test |
| Security boundaries | required | Where authentication, authorisation, validation and tenancy are enforced | named module or layer |
| Transaction and integration boundaries | optional | Atomic units, on-commit side effects, outbound adapters, background work | named function or task |
| Component diagram | required | Mermaid diagram of apps and external systems | matches Target structure |
| Implementation plan | required | Ordered work items with owning agent and acceptance criteria | each requirement maps to an item |
| Risks and open questions | required | Unresolved requirements, risks, assumptions to confirm | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions).
- Use only entities and rules the requirements state. A missing rule is an open question, never an assumption.
- Write each decision with the [architecture decision record](architecture-decision-record.template.md)
  structure; long plans may move to a separate [implementation plan](implementation-plan.template.md).
- Name configuration by setting or variable name only.

## Output structure

````markdown
# Architecture Design — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Summary
{{design_summary}}

## Context and requirements
| Id | Requirement or quality attribute | Kind (functional/quality/constraint/assumption) | Source |
|---|---|---|---|

## Current state
| App or module | Responsibility today | Convention to respect | Evidence |
|---|---|---|---|

## Decisions
<!-- One ADR per decision: Context · Options · Decision · Consequences -->

## Target structure
| App | Owns (models/use cases) | Public interface | Depends on |
|---|---|---|---|

## Dependency and boundary rules
| Rule | Applies to | How to check |
|---|---|---|

## Security boundaries
| Boundary | Control | Enforced in |
|---|---|---|

## Transaction and integration boundaries
| Operation | Atomic unit | After-commit effects | External system |
|---|---|---|---|

## Component diagram
```mermaid
flowchart LR
```

## Implementation plan
| # | Work item | Agent | Depends on | Acceptance criteria |
|---|---|---|---|---|

## Risks and open questions
| # | Risk or question | Impact | Owner |
|---|---|---|---|
````

## Completion checklist

- [ ] Every requirement maps to an owning app and at least one work item.
- [ ] Every significant decision lists the alternatives considered and its consequences.
- [ ] Dependency and security boundaries are stated as checkable rules.
- [ ] The implementation plan is ordered by dependency and names an agent per item.
- [ ] Assumptions and missing requirements appear under Risks and open questions.
