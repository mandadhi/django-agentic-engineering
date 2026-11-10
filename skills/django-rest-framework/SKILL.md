---
name: django-rest-framework
description: >-
  Django REST Framework engineering specialist — DRF architecture and request lifecycle,
  serializers and validation, relational and nested serializers, views/ViewSets and routers,
  authentication, permissions and queryset scoping, throttling, filtering/search/ordering,
  pagination, parsers/renderers and uploads, error responses, versioning and OpenAPI, and API
  testing. Load when designing, building, reviewing or debugging a DRF endpoint, serializer,
  permission, API setting or API contract.
project: django-agentic-engineering
abstract: >-
  Thin, scoped, paginated, documented APIs: serializers validate and represent, services act,
  querysets and permissions enforce access, one error format, explicit contracts. A concise
  workflow and navigation guide here; sixteen focused references loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, OpenAPI]
domain: [Software Engineering, APIs]
---

# Django REST Framework Engineering Specialist

How to design, implement, secure, document, test and maintain REST APIs with DRF. Baseline:
**Django 5.2 LTS + DRF 3.16**; behaviour tied to a DRF release is qualified "(DRF ≥ 3.x)" and
uncertain claims are marked `VERIFY`. Check the project's lock file before recommending a
version-specific feature. Third-party packages (django-filter, drf-spectacular, simplejwt, knox,
drf-nested-routers) are always named as such — never assume they are installed.

## Activate when

- Adding or changing an endpoint, serializer, ViewSet, router, permission, throttle, filter,
  paginator, parser/renderer or exception handler.
- Changing `REST_FRAMEWORK` settings, authentication, versioning or the OpenAPI schema.
- Reviewing an API for data leaks (fields, objects, tenants), N+1 queries or contract breaks.
- Debugging unexpected 400/401/403/404/406/415/429/500 responses from a DRF view.

## Engineering philosophy

1. **Use DRF's built-ins first**; add packages or custom base classes only for a demonstrated need.
2. **Views orchestrate HTTP**; services own business operations; selectors own scoped reads.
3. **Serializers validate and represent** — explicit fields, no hidden side effects.
4. **Authentication identifies; it never authorises.** Scope querysets *and* check permissions.
5. **Resources and HTTP semantics** over RPC endpoints; predictable statuses and one error format.
6. **Validate all untrusted input** — bodies, query params, related IDs, files — with bounds.
7. **Optimise from measured queries**, in the view's queryset, not in serializers.
8. **Everything visible is a contract**: documented, tested, versioned deliberately.

## API engineering workflow

1. **Frame** — resource(s), clients, auth model, tenancy, data volume, compatibility constraints,
   Django/DRF versions and installed API packages.
2. **Load references** from the tables below — one to three, never all.
3. **Inspect only** the affected app's `api.py`/`views.py`, `serializers.py`, `urls.py`,
   selectors/services and the `REST_FRAMEWORK` settings block.
4. **Design the contract** — URLs and methods, request/response shapes, status and error codes,
   pagination, filters, permissions per action.
5. **Implement** — scoped `get_queryset()`, input/output serializers, thin view calling a service,
   permissions, then filters/pagination/throttles.
6. **Verify** — API tests per role (incl. other tenant), validation and error bodies, query counts,
   schema diff.
7. **Explain** trade-offs, third-party dependencies, and anything marked `VERIFY`.

## Reference navigation

| Reference | Owns |
|---|---|
| [drf-fundamentals.md](references/drf-fundamentals.md) | DRF architecture, REST/HTTP semantics, component responsibilities, settings baseline, DRF vs plain Django |
| [serializers-and-model-serializers.md](references/serializers-and-model-serializers.md) | Serializer lifecycle, `ModelSerializer`, `save`/`create`/`update`, `many`/`partial`, context, input vs output |
| [fields-and-validation.md](references/fields-and-validation.md) | Field options, `validate_<field>`/`validate`, validators, ordering, error structure, serializer vs model vs DB validation |
| [nested-and-relational-serializers.md](references/nested-and-relational-serializers.md) | Relational fields, nested read/write, M2M, related-ID authorisation, relationship query cost |
| [views-and-viewsets.md](references/views-and-viewsets.md) | `APIView`, generic views, mixins, ViewSets, `@action`, `get_*`/`perform_*` hooks, choosing a view type |
| [routers-and-url-design.md](references/routers-and-url-design.md) | Resource URL design, routers, basenames, action URLs, nested URLs, avoiding RPC sprawl |
| [request-response-lifecycle.md](references/request-response-lifecycle.md) | `Request`/`Response`, dispatch order: negotiation → versioning → auth → permissions → throttles → handler → exceptions → rendering |
| [authentication.md](references/authentication.md) | Built-in auth classes, custom auth, ordering, 401 vs 403, token lifecycle, third-party JWT/OAuth boundaries |
| [permissions-and-access-control.md](references/permissions-and-access-control.md) | Permission classes, object permissions, composition, queryset scoping, tenant/ownership/role patterns |
| [throttling-and-rate-limiting.md](references/throttling-and-rate-limiting.md) | Throttle classes, rates/scopes, cache dependency, proxies, limits of app-level throttling |
| [filtering-search-and-ordering.md](references/filtering-search-and-ordering.md) | `SearchFilter`, `OrderingFilter`, django-filter (third-party), query-param validation, filter performance |
| [pagination.md](references/pagination.md) | Page-number, limit/offset, cursor, custom envelopes, stable ordering, large datasets |
| [parsers-renderers-and-content-negotiation.md](references/parsers-renderers-and-content-negotiation.md) | Parsers, uploads, renderers, `Accept`/`Content-Type`, format suffixes, file responses |
| [exception-handling-and-error-responses.md](references/exception-handling-and-error-responses.md) | DRF exceptions, custom handler, consistent error body, domain-error mapping, 5xx and disclosure |
| [api-versioning-and-documentation.md](references/api-versioning-and-documentation.md) | Versioning schemes, compatibility, deprecation, OpenAPI (built-in vs drf-spectacular), schema CI |
| [drf-testing-and-debugging.md](references/drf-testing-and-debugging.md) | `APIClient`/`APIRequestFactory`, permission matrices, query counts, uploads, debugging failed requests |

## Task-based loading

| Task | Load |
|---|---|
| Building a CRUD API | serializers-and-model-serializers.md, views-and-viewsets.md (+ routers-and-url-design.md) |
| Designing a new API surface / URL structure | routers-and-url-design.md, drf-fundamentals.md |
| Implementing custom validation | fields-and-validation.md |
| Handling nested resources or related IDs | nested-and-relational-serializers.md (+ routers-and-url-design.md for sub-resources) |
| Designing authentication | authentication.md, permissions-and-access-control.md |
| Multi-tenant or ownership access control | permissions-and-access-control.md, nested-and-relational-serializers.md |
| Protecting login/expensive endpoints | throttling-and-rate-limiting.md, authentication.md |
| Implementing filtering, search or sorting | filtering-search-and-ordering.md (+ pagination.md) |
| Optimising API pagination or slow lists | pagination.md, django-orm query-optimization |
| Fixing N+1 queries in serializers | nested-and-relational-serializers.md, django-orm query-optimization |
| Implementing file uploads or downloads | parsers-renderers-and-content-negotiation.md, fields-and-validation.md |
| Standardising error responses | exception-handling-and-error-responses.md |
| Introducing a breaking change / new version | api-versioning-and-documentation.md |
| Documenting an API (OpenAPI) | api-versioning-and-documentation.md |
| Writing API tests | drf-testing-and-debugging.md (+ permissions-and-access-control.md) |
| Debugging API behaviour (401/403/404/406/415/429) | request-response-lifecycle.md, drf-testing-and-debugging.md |
| Reviewing an API pull request | permissions-and-access-control.md, serializers-and-model-serializers.md, then owners of each finding |

## Handoffs

| Topic | Skill |
|---|---|
| Django settings, middleware, URLconf, sessions, file storage, logging | [django-core](../django-core/SKILL.md) |
| Services, selectors, app boundaries, transaction ownership | [django-architecture](../django-architecture/SKILL.md) |
| Models, relationships, constraints, QuerySet optimisation, transactions | [django-orm](../django-orm/SKILL.md) |
| Authentication/authorisation design, CSRF/CORS, secrets, threat modelling | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test strategy, factories, fixtures, CI | [django-testing](../django-testing/SKILL.md) |
| Profiling, caching strategy, load testing | [django-performance](../django-performance/SKILL.md) |
| Celery/background jobs, external APIs, webhooks, identity providers | [django-integrations](../django-integrations/SKILL.md) |
| Imports/exports, ETL, large-file processing | [django-data-processing](../django-data-processing/SKILL.md) |

Organisation-wide API contract standards may also exist as plugin skills
(`api-design-standards`, `api-versioning-policy`); follow them when present.

## Validation checklist

- [ ] `DEFAULT_PERMISSION_CLASSES`, pagination and throttles are set; any `AllowAny` is explicit.
- [ ] Every `get_queryset()` is scoped to the caller/tenant; related-field querysets are scoped too.
- [ ] Serializers list explicit `fields`; server-owned fields are read-only and set server-side.
- [ ] Inputs are bounded (lengths, ranges, list sizes, page sizes, file sizes).
- [ ] Views delegate multi-model or side-effecting work to services; no I/O in serializers.
- [ ] Each `@action` has deliberate permissions; unused HTTP methods are not exposed.
- [ ] Ordering is deterministic and allow-listed; list endpoints are paginated.
- [ ] Errors use the project's single error format with stable codes; 5xx leak nothing.
- [ ] Tests cover anonymous, unauthorised, other-tenant and authorised callers, validation errors,
      and query counts for lists.
- [ ] Schema regenerated and diffed; breaking changes follow the versioning policy.
- [ ] Third-party packages used are installed, version-compatible and named as third-party.
- Docs: <https://www.django-rest-framework.org/> (check release notes for the installed version).
