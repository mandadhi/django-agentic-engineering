# Django REST Framework Testing

> **Scope:** Test strategy and patterns for DRF APIs — choosing between `APIClient`,
> `APIRequestFactory` and the `API*TestCase` classes, authentication and permission matrices,
> serializer validation tests, response structure, pagination/filtering, throttling, content
> negotiation, uploads and API contract testing. DRF test-tool mechanics and request debugging are
> owned by [django-rest-framework drf-testing-and-debugging](../../django-rest-framework/references/drf-testing-and-debugging.md);
> attack-focused cases by [security-testing.md](security-testing.md).
>
> **Baseline:** Django 5.2 LTS · DRF 3.16 · pytest ≥ 8 · pytest-django 4.x · **Depends on:**
> **[DRF]** `rest_framework.test`, **[Django]** test runner/DB isolation,
> **[pytest-django]** `django_db`, `settings`, `django_assert_max_num_queries`,
> optional **[third-party: drf-spectacular]**, **[third-party: schemathesis]**,
> **[third-party: pact-python]**, **[third-party: django-filter]**.

## 1. Purpose and scope

An API is a contract with clients you do not control. API tests must prove:

1. **Who** can call each action — anonymous, authenticated, role, owner, other tenant.
2. **What** they may send — validation, bounds, read-only fields, content types.
3. **What** they get back — status codes, stable response shape, error format, pagination.
4. **What happens** — DB state, events, tasks enqueued — and that nothing happens on failure.
5. **How much** it costs — bounded query count for list/detail endpoints.
6. **That the contract does not drift** — schema diff or contract tests in CI.

This reference decides what to test and how to structure it. Serializer/permission/throttle
*implementation* lives in [django-rest-framework](../../django-rest-framework/SKILL.md).

## 2. Core concepts

| Tool | Provenance | Use for |
|---|---|---|
| `APIClient` | **[DRF]** (subclass of Django `Client`) | Default: full stack (URLconf, middleware, DRF dispatch, renderers) |
| `APIRequestFactory` + `force_authenticate(request, user=...)` | **[DRF]** | Unit-testing a view/ViewSet action or a permission class without URL routing |
| `APISimpleTestCase` / `APITestCase` / `APITransactionTestCase` / `APILiveServerTestCase` | **[DRF]** | unittest-style classes whose `self.client` is an `APIClient`; mirror Django's DB semantics |
| `URLPatternsTestCase` | **[DRF]** | Per-class `urlpatterns` (testing a reusable API app without project URLs) |
| `RequestsClient` | **[DRF]** + **[third-party: requests]** | Exercise the API through the `requests` interface in-process (useful for SDK-style tests) |
| `serializer.is_valid()` / `.errors` | **[DRF]** | Unit tests of validation rules without HTTP |

`APIClient` methods accept `format="json"` (or `"multipart"`); the default comes from
`REST_FRAMEWORK["TEST_REQUEST_DEFAULT_FORMAT"]` (default `multipart`). Set it to `"json"` in test
settings for JSON APIs so tests don't silently send form-encoded bodies, which hide nested-data
bugs.

`response.data` is the **pre-render** Python data (may contain `ErrorDetail`, `Decimal`,
`datetime`); `response.json()` is what the client actually receives. Assert on `response.json()`
for contract tests and on `response.data` when you need error codes.

## 3. Internal mechanics

The DRF dispatch order determines which status you get and therefore what you must test
(details: [DRF request-response lifecycle](../../django-rest-framework/references/request-response-lifecycle.md)):

```text
content negotiation (406) → versioning → authentication (lazy; 401 on bad credentials)
→ permissions (401 vs 403) → throttles (429) → handler (parse body: 415/400; validate: 400;
get_object: 404/403) → exception handler → renderer
```

Testing consequences:

- **401 vs 403 for anonymous users** depends on the **first authentication class**: if it
  provides `authenticate_header()` (e.g. `TokenAuthentication`, `BasicAuthentication`, JWT
  classes) DRF returns 401 with `WWW-Authenticate`; if it does not (`SessionAuthentication`)
  DRF returns 403. Changing `DEFAULT_AUTHENTICATION_CLASSES` order changes your anonymous status
  code — tests should pin the expected one deliberately.
- **Permissions run before throttles** (DRF ≥ 3.x order is `perform_authentication`,
  `check_permissions`, `check_throttles`) — a throttled anonymous request to a protected view
  returns 401/403, not 429.
- **`force_authenticate`** sets `request._force_auth_user/_token`; the authentication classes are
  bypassed entirely, so it proves nothing about header parsing, token validity or CSRF.
  `SessionAuthentication` enforces CSRF only for session-authenticated unsafe requests, and only
  when the client is created with `enforce_csrf_checks=True`.
- **Object permissions** run only when the view calls `get_object()` (or `check_object_permissions`
  explicitly). List endpoints never call `has_object_permission` — scoping must come from
  `get_queryset()`. A test suite that only tests detail routes misses list leaks.
- **Throttle rates** — `SimpleRateThrottle.THROTTLE_RATES` is a class attribute bound to
  `api_settings.DEFAULT_THROTTLE_RATES` when the class is defined, and `get_rate()` reads it, so
  `override_settings(REST_FRAMEWORK=...)` may not change rates already captured — `VERIFY` against
  the installed DRF version; patching the attribute (or a `rate` on the throttle subclass) is
  reliable. Throttle history lives in the default cache, so tests must clear it.

## 4. Engineering decisions and trade-offs

### Choosing the tool

| Need | Choose | Why not the other |
|---|---|---|
| Endpoint behaviour, contract, permissions in context | `APIClient` | Factory skips URL routing, middleware and router-generated actions |
| A permission class with many branches | Direct unit test with `APIRequestFactory` + a stub view/object | Running each branch through HTTP multiplies setup |
| Validation rules | Serializer unit tests | HTTP adds noise; one HTTP test proves the serializer is wired in |
| Reusable API package | `URLPatternsTestCase` or `@pytest.mark.urls` | Avoids depending on the project URLconf |

### Authentication strategy

| Technique | Use for | Proves |
|---|---|---|
| `client.force_authenticate(user=user)` | Most endpoint tests | Authorisation logic only |
| `client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")` | ≥ 1 test per token/JWT auth class | Header parsing, token lookup, expiry, inactive users |
| `client.login(...)` / `force_login` + `enforce_csrf_checks=True` | Session-authenticated SPA endpoints | Session + CSRF behaviour |
| `client.force_authenticate(user=None)` / `client.credentials()` | Reset between steps | — |

Every authentication class in `DEFAULT_AUTHENTICATION_CLASSES` gets at least one **real-path**
test (valid, invalid, expired/revoked, inactive user) — otherwise a misconfigured setting ships
with a green suite.

### Matrix vs individual tests

Permission behaviour is a matrix (role × action × ownership). Parametrise it: the table *is* the
specification and gaps are visible in review. Keep business-outcome tests (DB effect, response
body) as separate named tests — a giant parametrised test that asserts everything is unreadable.

### Contract testing options

| Approach | Catches | Cost | Notes |
|---|---|---|---|
| Hand-written response-shape assertions | Missing/renamed keys in tested paths | Low | Only what you thought of |
| Schema generation + diff in CI (**[third-party: drf-spectacular]** or DRF's built-in generator) | Any undocumented change to the published schema | Low | Requires the schema to be accurate (serializer-driven) |
| Property-based schema fuzzing (**[third-party: schemathesis]**) | 500s, responses violating the schema, validation gaps | Medium; nondeterministic unless seeded | Run against a live server or the WSGI app; nightly or on API PRs |
| Consumer-driven contracts (**[third-party: pact-python]**) | Breaking changes for specific known consumers | High: broker, consumer cooperation | Justified for many independent consumer teams |

Default: schema diff on every PR + a seeded schemathesis run on API changes; Pact only when
consumers are separate teams with independent release cycles.

## 5. Django implementation patterns

### Settings and fixtures

```python
# config/settings/test.py
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}
```

```python
# conftest.py
import pytest
from rest_framework.test import APIClient

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def make_client(db):
    def _make(user=None, **kwargs):
        client = APIClient(**kwargs)
        if user is not None:
            client.force_authenticate(user=user)
        return client
    return _make
```

### Endpoint behaviour tests

```python
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

class TestProjectCreate:
    url = reverse("api:project-list")

    def test_creates_project_owned_by_caller(self, make_client):
        user = UserFactory()
        response = make_client(user).post(self.url, {"name": "Apollo", "owner": 999})
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Apollo"
        project = Project.objects.get(pk=body["id"])
        assert project.owner == user                    # read-only field ignored

    def test_rejects_blank_name_with_field_error(self, make_client):
        response = make_client(UserFactory()).post(self.url, {"name": ""})
        assert response.status_code == 400
        assert response.data["name"][0].code == "blank"
        assert not Project.objects.exists()
```

Assert the **error code** (`ErrorDetail.code`) rather than the English message. If the project
uses a custom exception handler with a uniform error envelope
([DRF error responses](../../django-rest-framework/references/exception-handling-and-error-responses.md)),
add one test per error class asserting the envelope shape, then assert codes elsewhere.

### Permission matrix

```python
ACTIONS = {
    "list":    ("get",    lambda p: reverse("api:project-list")),
    "retrieve":("get",    lambda p: reverse("api:project-detail", args=[p.pk])),
    "update":  ("patch",  lambda p: reverse("api:project-detail", args=[p.pk])),
    "destroy": ("delete", lambda p: reverse("api:project-detail", args=[p.pk])),
}

@pytest.mark.parametrize(
    "actor, action, expected",
    [
        ("anonymous", "list", 401), ("anonymous", "retrieve", 401),
        ("viewer", "list", 200), ("viewer", "retrieve", 200),
        ("viewer", "update", 403), ("viewer", "destroy", 403),
        ("editor", "update", 200), ("editor", "destroy", 403),
        ("admin", "destroy", 204),
        ("other_tenant", "retrieve", 404), ("other_tenant", "update", 404),
    ],
)
def test_project_permissions(make_client, actors, actor, action, expected):
    project = ProjectFactory(tenant=actors["tenant"])
    method, url = ACTIONS[action]
    client = make_client(actors[actor])               # actors["anonymous"] is None
    response = getattr(client, method)(url(project), {"name": "x"})
    assert response.status_code == expected
```

The `actors` fixture builds users per role in one tenant plus a user in another tenant.
Detailed IDOR and tenant-isolation patterns: [security-testing.md](security-testing.md). Permission
design: [DRF permissions](../../django-rest-framework/references/permissions-and-access-control.md).

### Unit-testing a permission class

```python
from rest_framework.test import APIRequestFactory, force_authenticate
from apps.projects.permissions import IsProjectEditor

def test_editor_permission_denies_viewer_on_unsafe_method():
    factory = APIRequestFactory()
    request = factory.patch("/irrelevant/")
    request.user = UserFactory.build(role="viewer")       # no DB needed
    project = ProjectFactory.build(tenant_id=request.user.tenant_id)
    assert IsProjectEditor().has_object_permission(request, view=None, obj=project) is False
```

When the permission inspects `request.user` via a DRF `Request`, wrap it:
`Request(request)` and set `drf_request.user`; or call the view
(`view = ProjectViewSet.as_view({"patch": "partial_update"})`, `force_authenticate(request, user)`,
`view(request, pk=...)`).

### Serializer unit tests

```python
from apps.projects.serializers import ProjectWriteSerializer

@pytest.mark.django_db
def test_end_date_must_follow_start_date():
    s = ProjectWriteSerializer(data={"name": "A", "start": "2026-05-01", "end": "2026-04-01"})
    assert not s.is_valid()
    assert s.errors["non_field_errors"][0].code == "end_before_start"

@pytest.mark.django_db
def test_partial_update_keeps_unsent_fields():
    project = ProjectFactory(name="A", description="keep me")
    s = ProjectWriteSerializer(project, data={"name": "B"}, partial=True)
    assert s.is_valid(), s.errors
    s.save()
    project.refresh_from_db()
    assert (project.name, project.description) == ("B", "keep me")

@pytest.mark.django_db
def test_related_id_outside_scope_is_rejected(rf_request_with_user):   # project fixture
    foreign = TeamFactory()                                # other tenant
    s = ProjectWriteSerializer(data={"name": "A", "team": foreign.pk},
                               context={"request": rf_request_with_user})
    assert not s.is_valid()
    assert s.errors["team"][0].code == "does_not_exist"
```

Serializer tests cover: each custom validator, cross-field rules, `partial=True`, read-only and
write-only fields, scoped related-field querysets (a classic IDOR vector), and representation
of tricky types (Decimal as string, timezone-aware datetimes). Validation mechanics:
[DRF fields and validation](../../django-rest-framework/references/fields-and-validation.md).

### Real authentication paths

```python
def test_token_auth_rejects_revoked_token(api_client):
    token = TokenFactory()
    token.delete()
    api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    response = api_client.get(reverse("api:project-list"))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Token")

def test_session_auth_unsafe_request_requires_csrf(db):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(UserFactory())
    response = client.post(reverse("api:project-list"), {"name": "A"})
    assert response.status_code == 403                      # CSRF Failed
```

For JWT (**[third-party: djangorestframework-simplejwt]**), add expired-token and
wrong-signature tests with frozen time ([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)).

### Pagination and filtering

```python
def test_list_is_paginated_and_stably_ordered(make_client, user):
    ProjectFactory.create_batch(25, owner=user)
    r1 = make_client(user).get(reverse("api:project-list"), {"page_size": 10})
    body = r1.json()
    assert set(body) >= {"count", "next", "previous", "results"}
    assert body["count"] == 25 and len(body["results"]) == 10
    r2 = make_client(user).get(body["next"])
    ids_1 = {p["id"] for p in body["results"]}
    ids_2 = {p["id"] for p in r2.json()["results"]}
    assert ids_1.isdisjoint(ids_2)                          # no duplicates across pages

def test_page_size_is_capped(make_client, user):
    ProjectFactory.create_batch(5, owner=user)
    r = make_client(user).get(reverse("api:project-list"), {"page_size": 100000})
    assert r.status_code == 200
    # assert len(results) <= max_page_size — the cap is the contract

@pytest.mark.parametrize("params", [{"ordering": "password"}, {"status": "nonsense"}, {"page": "abc"}])
def test_invalid_query_params_do_not_500(make_client, user, params):
    assert make_client(user).get(reverse("api:project-list"), params).status_code in {200, 400, 404}
```

- Create rows with **identical** sort keys to prove a tiebreaker (`-created_at, -id`) exists;
  otherwise pages overlap nondeterministically on PostgreSQL.
- Cursor pagination: assert you can walk all pages and collect exactly N unique ids.
- Filters (**[DRF]** `SearchFilter`/`OrderingFilter`, **[third-party: django-filter]**): one test
  per filter that it narrows results, plus allow-list tests that unknown ordering fields are
  ignored or rejected. Mechanics: [DRF pagination](../../django-rest-framework/references/pagination.md),
  [DRF filtering](../../django-rest-framework/references/filtering-search-and-ordering.md).
- Query budget for lists — create N and 2N rows and assert the same query count; tool details in
  [performance-and-reliability-testing.md](performance-and-reliability-testing.md).

### Throttling

```python
from django.core.cache import cache
from rest_framework.throttling import ScopedRateThrottle

@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()

def test_login_endpoint_throttles_after_five_attempts(api_client, monkeypatch):
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"login": "5/min"})
    url = reverse("api:login")
    for _ in range(5):
        api_client.post(url, {"username": "x", "password": "y"})
    response = api_client.post(url, {"username": "x", "password": "y"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers
```

- Test settings must use a **real cache backend** (locmem) — `DummyCache` disables throttling
  silently.
- Freeze or advance time to test window expiry instead of sleeping.
- Throttling keyed by IP depends on `REMOTE_ADDR`/`NUM_PROXIES`; set `REMOTE_ADDR` explicitly
  (`api_client.post(url, data, REMOTE_ADDR="203.0.113.5")`) when testing per-IP behaviour.
  Design: [DRF throttling](../../django-rest-framework/references/throttling-and-rate-limiting.md).

### Content negotiation and uploads

```python
def test_unsupported_accept_returns_406(make_client, user):
    r = make_client(user).get(reverse("api:project-list"), HTTP_ACCEPT="application/xml")
    assert r.status_code == 406

def test_unsupported_content_type_returns_415(make_client, user):
    r = make_client(user).post(reverse("api:project-list"), data="name=A",
                               content_type="text/plain")
    assert r.status_code == 415

def test_attachment_upload(make_client, user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    upload = SimpleUploadedFile("spec.pdf", b"%PDF-1.7\n...", content_type="application/pdf")
    r = make_client(user).post(reverse("api:attachment-list"), {"file": upload}, format="multipart")
    assert r.status_code == 201
```

Upload validation (size, type, content, filename) as security tests:
[security-testing.md](security-testing.md).

### Schema contract in CI

```bash
# third-party: drf-spectacular
python manage.py spectacular --file openapi.yaml --validate --fail-on-warn
git diff --exit-code openapi.yaml       # committed schema must match generated schema
```

```python
# third-party: schemathesis — seeded property-based run against the WSGI app (API shape: VERIFY for the installed major version)
import schemathesis
schema = schemathesis.openapi.from_path("openapi.yaml")
```

Run schemathesis with authentication set up and a fixed seed in CI; treat 500s and schema
violations as failures. Versioning policy: [DRF versioning and documentation](../../django-rest-framework/references/api-versioning-and-documentation.md).

### unittest style equivalent

```python
from rest_framework.test import APITestCase

class ProjectAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()

    def test_list_requires_auth(self):
        self.assertEqual(self.client.get(reverse("api:project-list")).status_code, 401)

    def test_list_returns_own_projects(self):
        ProjectFactory(owner=self.user)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(reverse("api:project-list")).json()["count"], 1)
```

## 6. Production use cases

- **Multi-tenant SaaS API**: parametrised matrix per ViewSet including `other_tenant → 404` for
  every detail action, list scoping tests, and related-ID scoping in serializers.
- **Public partner API with tokens**: real-path token tests (valid/revoked/expired/inactive),
  schema diff gating merges, schemathesis nightly, throttling per scope.
- **Mobile backend with session + JWT**: separate suites per auth class; CSRF tests for session
  writes; JWT expiry tests with frozen time.
- **Bulk import endpoint**: multipart upload, oversize rejection, partial-failure response
  format, idempotency on retry (see [integration-and-background-task-testing.md](integration-and-background-task-testing.md)).
- **Reporting list endpoint**: pagination stability, ordering allow-list, query-count budget.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| `APIClient` endpoint tests | Real contract, catches wiring/permission/routing bugs | Slower; broad failures |
| `APIRequestFactory` unit tests | Fast, targeted | Bypasses routers, middleware, `initial()` subtleties if misused |
| Serializer unit tests | Exhaustive validation coverage cheaply | Don't prove the view uses that serializer |
| `force_authenticate` | Simple, fast | Proves nothing about authentication |
| Parametrised matrices | Specification-like, gaps visible | Opaque failures if IDs are not readable — use `ids=` |
| Schema diff | Catches every contract change | Only as accurate as the schema |
| Schemathesis | Finds unexpected 500s and schema violations | Needs seeding, data setup, tuning |

## 8. Security and performance considerations

- Test list endpoints for scoping, not only detail endpoints — object permissions do not run on
  lists.
- Test that writable serializers ignore or reject server-owned fields (`owner`, `tenant`,
  `is_staff`, `price`) — mass assignment ([DRF/API security](../../django-security-engineering/references/api-security.md)).
- Assert error responses do not leak internals (no stack traces, SQL, or existence of foreign
  objects: 404 rather than 403 for out-of-scope objects).
- Keep at least one test with `enforce_csrf_checks=True` per session-authenticated write path.
- Query budgets on every list and nested-serializer endpoint catch N+1 regressions.
- Disable or bypass throttling in the general suite only by test settings that are *asserted*
  elsewhere — never leave the production throttle configuration untested.

## 9. Common mistakes and anti-patterns

- Testing only with `force_authenticate` — broken auth configuration ships green.
- Expecting 401 for anonymous but configuring `SessionAuthentication` first (returns 403), or vice versa.
- Asserting `response.data == {...full dict...}` — breaks on any added field; assert the keys
  that form the contract and use schema diff for completeness.
- Comparing `response.data` with JSON-native values (`Decimal("1.00") != "1.00"`); use
  `response.json()` for wire-format assertions.
- Sending multipart by default and missing nested JSON bugs.
- Not clearing the cache — throttle counts leak between tests and cause 429 flakes.
- Pagination tests with distinct timestamps only — they pass while real data with ties overlaps.
- Using `admin` users for "authenticated" tests — `IsAdminUser`/superuser bypasses hide bugs.
- Mocking the serializer or `get_queryset()` inside an endpoint test.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| 403 "CSRF Failed" unexpectedly | `enforce_csrf_checks=True` with session login and no token | Intended in CSRF tests; otherwise use `force_authenticate` |
| 401 vs 403 differs from expectation | First auth class lacks/has `authenticate_header` | Pin expectation; check `DEFAULT_AUTHENTICATION_CLASSES` order |
| 415 on POST | Default test format multipart vs parser classes JSON-only | `format="json"` or `TEST_REQUEST_DEFAULT_FORMAT` |
| Throttle test never hits 429 | `DummyCache`, rates captured at import, or permission denial first | locmem cache, patch `THROTTLE_RATES`, authenticate |
| Random 429s in suite | Shared cache across tests | autouse `cache.clear()` fixture |
| Pagination test flaky | Non-unique ordering | Add tiebreaker; create ties in test data |
| `response.json()` raises | Non-JSON renderer or empty 204 | Check `Content-Type`; assert 204 body is empty |

Request-level debugging (inspecting `response.wsgi_request`, renderer context, exception handler
output) is in [DRF testing and debugging](../../django-rest-framework/references/drf-testing-and-debugging.md).

Checklist per endpoint: anonymous, unauthorised role, other tenant (404), authorised success with
DB effect, one validation error with code, list pagination + ordering + query budget, real-auth
path covered somewhere, schema regenerated and diffed.

## 11. Related references and skills

- [security-testing.md](security-testing.md) — IDOR, tenant isolation, CSRF, auth flows.
- [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md) — Django `Client` mechanics.
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — query budgets.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — actor/tenant fixtures, factories.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — schema-diff and contract jobs.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — time and external calls.
- django-rest-framework: [drf-testing-and-debugging.md](../../django-rest-framework/references/drf-testing-and-debugging.md),
  [permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md),
  [authentication.md](../../django-rest-framework/references/authentication.md),
  [parsers-renderers-and-content-negotiation.md](../../django-rest-framework/references/parsers-renderers-and-content-negotiation.md).
- [django-security-engineering api-security.md](../../django-security-engineering/references/api-security.md).

## 12. Official documentation and external references

- DRF testing: <https://www.django-rest-framework.org/api-guide/testing/>
- DRF authentication (401 vs 403): <https://www.django-rest-framework.org/api-guide/authentication/#unauthorized-and-forbidden-responses>
- DRF permissions: <https://www.django-rest-framework.org/api-guide/permissions/>
- DRF throttling: <https://www.django-rest-framework.org/api-guide/throttling/>
- DRF exceptions and `ErrorDetail`: <https://www.django-rest-framework.org/api-guide/exceptions/>
- DRF content negotiation: <https://www.django-rest-framework.org/api-guide/content-negotiation/>
- Django testing tools: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- drf-spectacular (third-party): <https://drf-spectacular.readthedocs.io/>
- Schemathesis (third-party): <https://schemathesis.readthedocs.io/>
- Pact (third-party): <https://docs.pact.io/>
