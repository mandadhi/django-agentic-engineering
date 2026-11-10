---
name: api-specification
description: >-
  Output template for specifying or reporting a Django REST Framework API change: endpoints with
  authentication and permissions, request and response contracts, errors, pagination and
  filtering, the access matrix, compatibility, files changed and verification.
kind: output-template
agents: [django-api-agent]
commands: [django-api]
project: django-agentic-engineering
abstract: >-
  Standard structure for a DRF API proposal or change report in which every endpoint has an
  explicit access decision and every contract change has a stated compatibility impact.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, APIs]
---

# API Specification Template

## Purpose

Define the contract and the access rules of a set of API endpoints so they can be implemented,
tested and reviewed against one source. The same structure serves as the proposal before
implementation and the report after it.

## When to use

- Adding or changing DRF endpoints, serializers, permissions, pagination, filtering or throttling.
- Proposing an API change for approval, or reporting one that was implemented.
- Not for reference documentation of an existing API for readers; use
  [technical documentation](technical-documentation.template.md).

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | The API change being specified | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Resources and endpoints covered | task |
| `{{prepared_by}}` | Agent or person producing the specification | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `proposed`, `implemented`, `partial` or `blocked` | producer |
| `{{sources}}` | Requirements, model specification, existing API modules | inputs |
| `{{api_conventions}}` | Existing URL style, versioning scheme, error format, auth classes | project settings and code |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Context | required | Existing API conventions the change follows | `file:line` |
| Endpoints | required | One row per method and path with auth, permission, request, response and errors | URLconf and view |
| Payloads | optional | Field-level request and response contracts, read-only and write-only fields | serializer source |
| Pagination, filtering and ordering | required | Paginator and maximum page size, allowed filter and ordering fields | view or settings |
| Access matrix | required | Expected outcome per endpoint for anonymous, other user, owner, privileged | tests |
| Throttling | optional | Throttle scopes and rates, required for authentication endpoints | settings or view |
| Compatibility | required | Breaking changes and the migration path for clients | change classification |
| Files changed | required | Files created or edited; `none` for a proposal | diff |
| Verification | required | Tests and schema validation commands with results | command output |
| Open questions | required | Unclear ownership, scoping or behaviour | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions).
- Every endpoint states an explicit permission; a public endpoint states why (`API-M1`).
- Classify each contract change with the
  [API design checklist](../references/django-api-design-checklist.md#compatibility-classification).
- Example payloads use obviously fake values; never real tokens, emails or identifiers.

## Output structure

````markdown
# API Change — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Context
Conventions: {{api_conventions}}

## Endpoints
| Method | Path | Auth | Permission | Request | Response | Errors |
|---|---|---|---|---|---|---|

## Payloads
| Endpoint | Field | Type | Direction (read/write/both) | Required | Validation |
|---|---|---|---|---|---|

## Pagination, filtering and ordering
| Endpoint | Pagination (max page size) | Filter fields | Ordering fields |
|---|---|---|---|

## Access matrix
| Endpoint | Anonymous | Other user | Owner | Privileged |
|---|---|---|---|---|

## Throttling
| Endpoint or scope | Rate | Reason |
|---|---|---|

## Compatibility
| Change | Classification (additive/breaking) | Client impact | Migration path |
|---|---|---|---|

## Files changed

## Verification
| Command | Result |
|---|---|

## Open questions
````

## Completion checklist

- [ ] Every endpoint has authentication, an explicit permission and a scoped queryset.
- [ ] The access matrix has an expected status for every endpoint and role.
- [ ] Every list endpoint states pagination and its maximum page size.
- [ ] Every contract change is classified, and breaking changes have a migration path.
- [ ] Verification lists real command results, or `not-run` with the reason.
