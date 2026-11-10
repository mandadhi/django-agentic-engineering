# DRF Testing and Debugging

> **Scope.** Owns: DRF's test utilities (`APIRequestFactory`, `APIClient`, `APITestCase` and
> friends, `RequestsClient`, `force_authenticate`), testing serializers, validation, views,
> ViewSets, routers, permissions, pagination, filtering, uploads and error bodies, query-count
> tests for endpoints, and debugging failed API requests.
> Does not cover: general test strategy, factories/fixtures, CI and coverage policy
> ([django-testing](../../django-testing/SKILL.md)), load and performance testing
> ([django-performance](../../django-performance/SKILL.md)).

## 1. Purpose and scope

API behaviour is defined by status codes, bodies, headers and side effects per caller. This
reference shows how to test each layer with DRF's tools and how to debug a failing request
quickly.

## 2. Core concepts

| Tool (`rest_framework.test`) | Use |
|---|---|
| `APIRequestFactory` | Build a request and call a view directly (unit-level, no URL routing/middleware) |
| `force_authenticate(request, user=None, token=None)` | Attach credentials to a factory request |
| `APIClient` | Full Django test client path (URLs, middleware, DRF) with `format=`, `credentials()`, `force_authenticate()` |
| `APITestCase` / `APITransactionTestCase` / `APISimpleTestCase` / `APILiveServerTestCase` | Django test cases with `self.client` as `APIClient` |
| `URLPatternsTestCase` | Per-test-class `urlpatterns` |
| `RequestsClient` | `requests`-style client against the WSGI app (needs the `requests` package) |

pytest users: `pytest-django` with an `APIClient` fixture — third-party, conventions in
[django-testing](../../django-testing/SKILL.md).

**Test levels for APIs.**

| Level | Target | Tools | Use for |
|---|---|---|---|
| Unit | Serializer, permission class, filter, paginator | Direct instantiation, `APIRequestFactory` | Validation rules, edge cases, permission logic |
| Integration (API) | Endpoint through URLs/middleware/DRF | `APIClient` | Status/body/headers per role; query counts; contract |
| Service | Business operation without HTTP | Plain `TestCase` | Workflows (owned by django-testing/architecture) |
| End-to-end | Real server, real auth flow | `APILiveServerTestCase`, `RequestsClient`, external tools | Critical journeys, auth integration |

Most API tests should be `APIClient` integration tests; push complex rule permutations down to
serializer/permission unit tests.

## 3. Internal mechanics

- `APIClient` extends Django's test client; `format="json"` (or `TEST_REQUEST_DEFAULT_FORMAT`)
  encodes the body with the matching renderer and sets `Content-Type`.
- `force_authenticate()` bypasses authentication classes: `request.user` is the given user and
  `request.auth` the given token. Use it for permission tests, not for testing authentication.
- `APIClient` performs **no CSRF checks** by default — `SessionAuthentication` CSRF failures
  only appear with `APIClient(enforce_csrf_checks=True)`.
- `client.login()` uses Django sessions; `client.credentials(HTTP_AUTHORIZATION=...)` sets
  headers for subsequent requests; `client.credentials()` clears them; `force_authenticate(user=None)`
  clears forced auth.
- Responses from `APIClient` expose `response.data` (pre-render Python data) and rendered
  `response.content`/`response.json()`. Views called via `APIRequestFactory` return unrendered
  responses — call `response.render()` before reading `content`.
- `assertNumQueries` / `CaptureQueriesContext` come from Django and count queries on the default
  connection.

## 4. Engineering decisions and trade-offs

- **`response.data` vs `response.json()`.** `data` is convenient but may contain `ErrorDetail`,
  `Decimal`, `datetime`; `json()` asserts the real wire format — prefer it for contract
  assertions.
- **`force_authenticate` vs real credentials.** Force for permission matrices; real credentials in
  one test per authentication class.
- **Snapshot/contract testing.** Schema diffs in CI catch contract drift cheaply
  ([api-versioning-and-documentation.md](api-versioning-and-documentation.md)); full-body snapshots
  are brittle — assert keys and key values.
- **Query-count assertions** pin the N+1 behaviour of list endpoints; assert with two different
  dataset sizes to prove the count is constant.

## 5. Django/DRF implementation patterns

**Endpoint tests per role:**

```python
class OrderApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.owner = UserFactory(organization=cls.org)
        cls.stranger = UserFactory()                       # other organization
        cls.order = OrderFactory(customer__user=cls.owner, customer__organization=cls.org)
        cls.url = reverse("v1:order-detail", kwargs={"pk": cls.order.pk})

    def test_anonymous_is_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)  # 403 if session first

    def test_other_tenant_gets_404(self):
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_retrieve(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["reference"], self.order.reference)
```

**Create with validation error and error-shape assertion:**

```python
def test_create_rejects_excessive_quantity(self):
    self.client.force_authenticate(self.owner)
    response = self.client.post(reverse("v1:order-list"),
                                {"lines": [{"product_id": self.product.pk, "quantity": 5000}]},
                                format="json")
    self.assertEqual(response.status_code, 400)
    body = response.json()
    self.assertEqual(body["code"], "invalid")
    self.assertIn("quantity", body["errors"]["lines"][0])
```

**Real token authentication path:**

```python
def test_token_auth(self):
    token = Token.objects.create(user=self.owner)          # built-in authtoken
    self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    self.assertEqual(self.client.get(self.url).status_code, 200)
```

**Serializer unit test:**

```python
def test_booking_rejects_inverted_range():
    request = APIRequestFactory().post("/")
    request.user = UserFactory.build()
    s = BookingInputSerializer(data={"room_id": 1, "starts_at": "2026-05-02T10:00Z",
                                     "ends_at": "2026-05-02T09:00Z"},
                               context={"request": request})
    assert not s.is_valid()
    assert s.errors["ends_at"][0].code == "invalid_range"   # field errors may short-circuit first
```

**Permission class unit test:**

```python
def test_only_admins_may_modify():
    request = APIRequestFactory().patch("/")
    request.user = member
    assert IsProjectAdminOrReadOnly().has_object_permission(request, view=None, obj=project) is False
```

(Factory requests are Django `HttpRequest`s; wrap with `Request(request)` if the permission reads
DRF-only attributes such as `request.auth`/`request.data`.)

**ViewSet via factory (no URLs):**

```python
view = OrderViewSet.as_view({"get": "list"})
request = APIRequestFactory().get("/orders/", {"status": "open"})
force_authenticate(request, user=owner)
response = view(request)
response.render()
```

**Router test:** assert names resolve — `reverse("v1:order-cancel", kwargs={"pk": 1})` — and
that unsupported methods return 405.

**Pagination, filtering, ordering:**

```python
def test_list_is_paginated_and_ordered(self):
    OrderFactory.create_batch(60, customer__user=self.owner)
    self.client.force_authenticate(self.owner)
    body = self.client.get(reverse("v1:order-list"), {"page_size": 500, "ordering": "-created_at"}).json()
    self.assertEqual(len(body["results"]), 200)          # max_page_size
    created = [r["created_at"] for r in body["results"]]
    self.assertEqual(created, sorted(created, reverse=True))
```

**Query count:**

```python
def test_list_query_count_is_constant(self):
    OrderFactory.create_batch(5, customer__user=self.owner)
    self.client.force_authenticate(self.owner)
    with self.assertNumQueries(3):     # COUNT + page SELECT (+ select_related) + prefetch lines — document each
        self.client.get(reverse("v1:order-list"))
```

**File upload:**

```python
def test_upload(self):
    self.client.force_authenticate(self.owner)
    f = SimpleUploadedFile("a.pdf", b"%PDF-1.4 ...", content_type="application/pdf")
    response = self.client.post(reverse("v1:document-upload"), {"file": f, "title": "A"},
                                format="multipart")
    self.assertEqual(response.status_code, 201)
```

**Throttling:** clear the cache in `setUp` and patch rates for the test.

## 6. Production use cases

- **Permission matrix** per endpoint: anonymous, stranger, member, admin, other tenant — the
  highest-value API tests in multi-tenant systems.
- **Contract regression**: schema diff + response-shape tests for the error envelope.
- **Performance regression**: `assertNumQueries` on every list endpoint.
- **Auth integration**: one end-to-end test per authentication class (session with CSRF
  enforced, token, JWT via the third-party package's own views).

## 7. Advantages and disadvantages

| Tool | Advantages | Disadvantages |
|---|---|---|
| `APIRequestFactory` | Fast, isolates the view | Skips URLs, middleware; manual rendering |
| `APIClient` | Realistic request path | Slower; CSRF off by default |
| `force_authenticate` | Simple permission tests | Doesn't exercise authentication |
| `RequestsClient` | Same client code as external callers | Extra dependency; session handling differs |

## 8. Security and performance considerations

- Always test the negative paths (401/403/404) — a missing permission is a silent bug.
- Test with two tenants; single-tenant fixtures cannot detect scoping bugs.
- Run at least one test with `enforce_csrf_checks=True` if session authentication is used.
- Query-count tests protect production latency; keep counts explicit and commented.

## 9. Common mistakes and anti-patterns

- Posting dicts without `format="json"` (sent as multipart; nested data breaks).
- Asserting only `status_code`.
- Using `force_authenticate` everywhere and never testing real credentials.
- Asserting on full error message strings instead of codes.
- Tests sharing throttle/cache state.
- Using `response.data` to assert JSON types (Decimal vs string).

## 10. Troubleshooting and validation

**Debugging a failing request — in order:**

1. Status and body: `print(response.status_code, response.json())` (or `response.data`).
2. 401/403: which authenticator ran? (`request.successful_authenticator`), first class's
   challenge header, permission `message`, CSRF?
3. 404: scoped `get_queryset()`, filter backends in `get_object()`, URL kwarg name.
4. 400: serializer `errors` — field vs `non_field_errors`; `partial` flag.
5. 415/406: `Content-Type` vs parsers, `Accept` vs renderers.
6. 500: traceback from the test runner; exception handler returned `None`.
7. Queries: `CaptureQueriesContext(connection)` and print `ctx.captured_queries`.
8. Routes: print `router.urls`; `python manage.py show_urls` (third-party django-extensions).

| Symptom | Cause |
|---|---|
| `response.data` is `None` | View returned a non-DRF response |
| `ContentNotRenderedError` | Reading `.content` of a factory-called view without `render()` |
| Works in tests, CSRF fails in browser | `APIClient` skips CSRF by default |
| Query count differs between runs | Cached content types/permissions or session lookups; assert after a warm-up request |

## 11. Related references and skills

- [request-response-lifecycle.md](request-response-lifecycle.md) — where each failure originates
- [permissions-and-access-control.md](permissions-and-access-control.md)
- [exception-handling-and-error-responses.md](exception-handling-and-error-responses.md)
- [django-testing](../../django-testing/SKILL.md) — strategy, factories, CI
- [django-orm query-optimization](../../django-orm/references/query-optimization.md)

## 12. Official documentation and external references

- Testing: <https://www.django-rest-framework.org/api-guide/testing/>
- Django testing tools: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- `assertNumQueries`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#django.test.TransactionTestCase.assertNumQueries>
- Third-party: pytest-django <https://pytest-django.readthedocs.io/>, factory_boy <https://factoryboy.readthedocs.io/>
