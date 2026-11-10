---
name: django-api-design-checklist
description: >-
  Shared checklist for designing and reviewing Django REST Framework endpoints: resource and URL
  design, contracts, access control, collections, errors, throttling and documentation, plus a
  classification of breaking and additive contract changes.
kind: shared-reference
agents: [django-api-agent, django-code-review-agent]
project: django-agentic-engineering
abstract: >-
  Endpoint-by-endpoint DRF design and review checklist with the code location to inspect and a
  compatibility classification for contract changes, mapped to API rule ids and DRF skill references.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, APIs]
---

# Django API Design Checklist

## Purpose

Make API proposals, implementations and reviews check the same things for every endpoint, and
classify every contract change the same way, so breaking changes are never discovered by clients.

## Scope

HTTP APIs built with Django REST Framework. Plain Django JSON views can use the same checklist,
reading "serializer" as the code that validates and shapes the payload.

## How to use

- Before implementation, answer every row for each new endpoint in the
  [API specification](../templates/api-specification.template.md).
- In review, check the rows that the changed files touch, and record each gap with its rule id
  as a finding in the [code review report](../templates/code-review-report.template.md) findings table.
- Follow the project's existing conventions where they exist; a new convention needs a decision record.

## Per-endpoint checklist

| Area | Question | Where to look | Rule | Skill reference |
|---|---|---|---|---|
| Resource and URL | Is the resource a noun, nested at most where ownership requires, consistent with existing routes? | router registrations, `urls.py` | `API-R1` | [routers and URL design](../skills/django-rest-framework/references/routers-and-url-design.md) |
| Methods and status | Does each method have its HTTP meaning and the matching success code? | view or ViewSet actions | `API-R3` | [views and ViewSets](../skills/django-rest-framework/references/views-and-viewsets.md) |
| Authentication | Which authentication classes apply, by default or on the view? | `REST_FRAMEWORK`, view attributes | `API-M1` | [authentication](../skills/django-rest-framework/references/authentication.md) |
| Permission | Is a permission declared for every action, including custom `@action`s? | `permission_classes`, `get_permissions()` | `API-M1`, `SEC-M4` | [permissions](../skills/django-rest-framework/references/permissions-and-access-control.md) |
| Scoping | Does `get_queryset()` restrict rows to the caller before lookup? | `get_queryset()` | `API-M2` | [permissions](../skills/django-rest-framework/references/permissions-and-access-control.md) |
| Fields | Are fields listed explicitly, with read-only and write-only fields marked? | serializer `Meta.fields` | `API-M3` | [serializers](../skills/django-rest-framework/references/serializers-and-model-serializers.md) |
| Validation | Are field, object and uniqueness rules validated with clear messages? | `validate_*`, `validate()`, validators | `API-R4` | [fields and validation](../skills/django-rest-framework/references/fields-and-validation.md) |
| Nested writes | Are nested and related writes explicit and transactional? | `create()`/`update()` | `COD-M5` | [nested serializers](../skills/django-rest-framework/references/nested-and-relational-serializers.md) |
| Pagination | Is every list paginated with a maximum page size? | `pagination_class`, settings | `API-M4` | [pagination](../skills/django-rest-framework/references/pagination.md) |
| Filtering and ordering | Are filter and ordering fields explicit allow-lists? | `filterset_fields`, `ordering_fields` | `API-M9` | [filtering](../skills/django-rest-framework/references/filtering-search-and-ordering.md) |
| Query cost | Does serializing a page run a fixed number of queries? | `get_queryset()` prefetching, serializer sources | `API-M8`, `PRF-M1` | [API performance](../skills/django-performance/references/api-performance.md) |
| Errors | Do errors use the project's single format and avoid leaking internals? | exception handler, raised exceptions | `API-M5` | [error responses](../skills/django-rest-framework/references/exception-handling-and-error-responses.md) |
| Throttling | Are authentication and expensive endpoints throttled? | `throttle_classes`, settings | `API-M7` | [throttling](../skills/django-rest-framework/references/throttling-and-rate-limiting.md) |
| Schema and docs | Is the endpoint in the generated schema and documented? | schema generation config | `API-R2`, `DOC-M5` | [versioning and documentation](../skills/django-rest-framework/references/api-versioning-and-documentation.md) |
| Tests | Do tests cover the contract, validation and the access matrix? | API test module | `TST-M5` | [DRF testing](../skills/django-testing/references/django-rest-framework-testing.md) |

## Compatibility classification

Classify each change to a published contract. A breaking change needs a version or deprecation
path (`API-M6`).

| Change | Classification |
|---|---|
| New endpoint, new optional request field, new response field | additive |
| New enum value in a response | additive for tolerant clients; treat as breaking if clients validate strictly |
| Removing or renaming an endpoint, field or enum value | breaking |
| Making an optional request field required, or narrowing accepted values | breaking |
| Changing a field's type, format, nullability or meaning | breaking |
| Changing default ordering, page size or pagination style | breaking for clients that depend on it; state it |
| Changing authentication or permissions so that a client loses access | breaking |
| Changing error status codes or the error body shape | breaking |
| Fixing behaviour that contradicted the documentation | breaking for clients relying on it; announce it |

## Related skills and rules

- Skills: [django-rest-framework](../skills/django-rest-framework/SKILL.md),
  [django-security-engineering](../skills/django-security-engineering/SKILL.md)
  ([API security](../skills/django-security-engineering/references/api-security.md)),
  [django-orm](../skills/django-orm/SKILL.md).
- Rules: [django-api-standards](../rules/django-api-standards/SKILL.md),
  [django-security-standards](../rules/django-security-standards/SKILL.md),
  [django-testing-standards](../rules/django-testing-standards/SKILL.md),
  [django-documentation-standards](../rules/django-documentation-standards/SKILL.md).
