---
name: architecture-decision-record
description: >-
  Output template for one architecture decision record (ADR) in a Django project: context,
  decision drivers, options with trade-offs, the decision, consequences and how compliance is
  verified.
kind: output-template
agents: [django-architecture-agent]
commands: [django-design]
project: django-agentic-engineering
abstract: >-
  Standard structure for recording one hard-to-reverse Django design decision with the options
  considered, so later readers know why it was made and when to revisit it.
sdlc_phase: Architecture/Planning
tech_stack: [Django, Markdown]
domain: [Software Engineering, Documentation]
---

# Architecture Decision Record Template

## Purpose

Capture one significant decision, the alternatives that were considered and the consequences that
were accepted, so the decision can be understood, checked and revisited without archaeology.

## When to use

- A decision is expensive to reverse: app boundaries, data ownership, the API style, background
  processing, multi-tenancy, a new infrastructure dependency, a layering rule.
- A reviewer would reasonably ask "why was it done this way?".
- Not for reversible, local implementation choices; a code comment or the change description is enough.
- Superseding an earlier decision: write a new record and set the old one to `superseded`.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{adr_number}}` | Sequential number in the project's ADR log | existing ADR directory, else `0001` |
| `{{subject}}` | Short decision title in the imperative ("Use a service layer for orders") | decision |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Apps or components affected | design |
| `{{prepared_by}}` | Agent or person proposing the decision | producer |
| `{{date}}` | Date proposed or decided (ISO 8601) | producer |
| `{{document_status}}` | `proposed`, `accepted`, `rejected`, `deprecated` or `superseded by ADR-<n>` | decision process |
| `{{sources}}` | Requirements, design document, related ADRs | inputs |
| `{{decision_statement}}` | One or two sentences stating what was decided | decision |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Context | required | The problem, the forces and constraints, the current state | requirement ids, `file:line` |
| Decision drivers | required | The quality attributes and constraints the options are judged against | listed in Context and requirements |
| Options considered | required | At least two options, including the status quo when one exists | table |
| Decision | required | The chosen option and the reason it wins on the drivers | — |
| Consequences | required | Positive, negative and neutral effects, including new work created | — |
| Compliance | optional | How conformance is checked: a test, an import rule, a review checklist item | named check |
| Revisit when | optional | Conditions that would reopen the decision | measurable trigger |
| Related decisions | optional | Links to records this one depends on, amends or supersedes | resolvable links |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). One decision per record.
- Options are compared on the stated drivers, not on preference. Unknown costs are marked `unknown`.
- An accepted record is not rewritten; a change of mind is a new record (`DOC-M6`).

## Output structure

````markdown
# ADR-{{adr_number}}: {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Context

## Decision drivers
| Driver | Why it matters here |
|---|---|

## Options considered
| Option | Pros | Cons | Fit with drivers |
|---|---|---|---|

## Decision
{{decision_statement}}

## Consequences
| Kind (positive/negative/neutral) | Consequence | Follow-up work |
|---|---|---|

## Compliance

## Revisit when

## Related decisions
````

## Completion checklist

- [ ] The record states exactly one decision, with its status.
- [ ] At least two options are compared on the listed drivers.
- [ ] Negative consequences and the follow-up work they create are stated.
- [ ] Every requirement or code reference is traceable to its source.
