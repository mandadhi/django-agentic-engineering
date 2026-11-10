---
name: django-model-specification
description: >-
  Output template for specifying or reporting Django model changes: entities, fields,
  relationships with on_delete, constraints, indexes with the queries they serve, sensitive data,
  migrations, deployment notes and verification.
kind: output-template
agents: [django-model-agent]
commands: [django-model]
project: django-agentic-engineering
abstract: >-
  Standard structure for a Django data-model proposal or change report in which every invariant
  maps to a constraint and every index to a query.
sdlc_phase: Development
tech_stack: [Python, Django, PostgreSQL]
domain: [Software Engineering, Data]
---

# Django Model Specification Template

## Purpose

Specify the data layer of a change precisely enough that the models, constraints, indexes and
migrations can be implemented and reviewed against it. The same structure serves as the proposal
before implementation and the report after it.

## When to use

- Adding or changing models, fields, relationships, managers, constraints or indexes.
- Proposing a model change for approval before any file is edited.
- Not for API payload shapes; use the [API specification](api-specification.template.md).
- When the migration itself is risky (large tables, backfills, renames), add a
  [migration plan](migration-plan.template.md).

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | The model change being specified | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Apps and models affected | design or task |
| `{{prepared_by}}` | Agent or person producing the specification | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `proposed`, `implemented`, `partial` or `blocked` | producer |
| `{{sources}}` | Requirements, architecture design, existing model files | inputs |
| `{{database_backend}}` | Database engine and version, or `unknown` | settings and lock files |
| `{{conventions}}` | Key type, naming, abstract base models, timestamp fields in use | existing models |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Context | required | Backend, existing conventions, the requirement behind the change | `file:line` |
| Changes | required | One row per model change, with the reason | requirement or decision id |
| Fields | required | Field type, options, nullability, default for each new or changed field | model source |
| Relationships | optional | Cardinality, `on_delete`, `related_name` for each relation | model source |
| Constraints | required | One row per invariant and the constraint that enforces it | constraint name in `Meta` |
| Indexes | required | Each index and the query it serves | query location |
| Sensitive data | optional | Fields holding personal, credential or financial data and their protection | field and control |
| Migrations | required | Each migration file, its operations, reversibility and risk | `sqlmigrate` output |
| Deployment notes | required | Sequencing, backfills, rollback | migration plan when risky |
| Verification | required | Commands run and their results | command output |
| Open questions | required | Missing rules, ambiguous cardinality or ownership | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions).
- Every stated invariant appears under Constraints, or is listed with the reason it cannot be
  expressed in the database (`MOD-M1`). Every index names its query (`PRF-M8`).
- Migration risk follows the [migration safety reference](../references/django-migration-safety.md).
- In a proposal, Migrations lists planned operations and Verification lists the commands that will run.

## Output structure

````markdown
# Model Change — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Context
- Database backend: {{database_backend}}
- Conventions: {{conventions}}

## Changes
| App.Model | Change | Reason |
|---|---|---|

## Fields
| App.Model.field | Type and options | Null / blank | Default | Notes |
|---|---|---|---|---|

## Relationships
| From | To | Cardinality | on_delete | related_name |
|---|---|---|---|---|

## Constraints
| Name | Rule enforced | Type |
|---|---|---|

## Indexes
| Name | Columns | Serves |
|---|---|---|

## Sensitive data
| Field | Data class | Protection |
|---|---|---|

## Migrations
| File | Operations | Reversible | Risk (lock/rewrite/backfill) |
|---|---|---|---|

## Deployment notes

## Verification
| Command | Result |
|---|---|

## Open questions
````

## Completion checklist

- [ ] Every requested change appears under Changes with a reason.
- [ ] Every relation states `on_delete`; every index names the query it serves.
- [ ] Every invariant maps to a constraint or to a stated reason why not.
- [ ] Every migration has a reversibility and risk assessment.
- [ ] Verification lists real command results, or `not-run` with the reason.
