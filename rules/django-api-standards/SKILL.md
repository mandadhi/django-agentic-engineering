---
name: django-api-standards
description: >-
  Mandatory Django REST Framework engineering rule: default-deny permissions, caller-scoped
  querysets, explicit serializer fields, pagination, consistent error responses, throttling of
  authentication endpoints, contract compatibility and query efficiency. Apply when building,
  changing or reviewing DRF endpoints.
project: django-agentic-engineering
abstract: >-
  Enforceable DRF API standards for secure, consistent and backward-compatible endpoints in any
  Django project.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, APIs]
---

# Rule: Django API Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Every endpoint must be safe by default, predictable for its clients, and stable once published.

## Scope

Applies to DRF settings, serializers, views/ViewSets, routers, permissions, authentication,
pagination, filtering and API error handling. Plain Django JSON views follow the same intent.

## Mandatory requirements

- **API-M1** `DEFAULT_PERMISSION_CLASSES` is not `AllowAny`. Every endpoint declares or
  inherits a permission. A public endpoint sets `AllowAny` explicitly, with a comment that gives the reason.
- **API-M2** `get_queryset()` limits results to what the caller may see, before any object
  lookup. Objects outside the caller's scope return 404.
- **API-M3** Serializers list their `fields` explicitly. They never use `"__all__"`, and never
  use `exclude` on models that hold sensitive data. Server-owned fields (owner, status,
  timestamps, computed values) are read-only.
- **API-M4** List endpoints are paginated, with a maximum page size.
- **API-M5** Errors use DRF exception handling in the project's consistent format. Responses
  never contain stack traces, SQL or internal configuration.
- **API-M6** A published contract MUST NOT break without a version or deprecation path.
  Breaking means removing or renaming fields, changing types or meaning, or tightening validation.
- **API-M7** Login, token, password-reset and similar authentication endpoints are throttled.
- **API-M8** List serialization performs no per-object queries. Relations used by the
  serializer are loaded with `select_related`/`prefetch_related` on the queryset.
- **API-M9** `ordering_fields` and filter fields are explicit allow-lists, never `"__all__"`.

## Recommendations

- **API-R1** Follow the project's existing URL, naming and versioning style. Use ViewSets with
  routers where the project already does.
- **API-R2** Generate and validate an OpenAPI schema (for example drf-spectacular) in CI.
- **API-R3** Use status codes with their HTTP meaning (201 on create, 204 on delete, 409 on conflicts).
- **API-R4** Keep serializers focused on representation and validation. Delegate side effects
  to the service layer.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `fields = "__all__"` | Explicit field list | API-M3 |
| `Model.objects.all()` as the viewset queryset for user-owned data | Caller-scoped `get_queryset()` | API-M2 |
| Access checks only in the frontend | Server-side permission classes | API-M1 |
| `SerializerMethodField` that queries per row | Annotate or prefetch on the queryset | API-M8 |
| Unpaginated list endpoint | Pagination with a maximum page size | API-M4 |
| Renaming a response field in place | New version, or a deprecation period | API-M6 |

## Security and maintainability

Scoped querysets and explicit fields prevent IDOR and mass assignment, two of the most common
API flaws. Contract stability protects clients you cannot see.

## Verification

Grep for `__all__`, `AllowAny` and `queryset =` in API modules. Read `REST_FRAMEWORK` in
settings. Look for tests covering the access matrix and query counts on list endpoints.

## Related skills

[django-rest-framework](../../skills/django-rest-framework/SKILL.md) (permissions, serializers, pagination, errors, versioning) ·
[django-security-engineering](../../skills/django-security-engineering/SKILL.md) (API security) ·
[django-orm](../../skills/django-orm/SKILL.md) (query optimisation).
