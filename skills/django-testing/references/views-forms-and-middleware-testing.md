# Views, Forms and Middleware Testing

> **Scope:** How to test Django's HTML-facing request layer — function- and class-based views,
> forms and ModelForms, redirects, templates and context, authentication/permission behaviour at
> the view, middleware, file and streaming responses, sessions and cookies — with the test
> `Client` and `RequestFactory`. DRF endpoints are in
> [django-rest-framework-testing.md](django-rest-framework-testing.md); attack-focused tests
> (CSRF, IDOR, session fixation) are in [security-testing.md](security-testing.md).
>
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · **Depends on:** **[Django]**
> `django.test` (`Client`, `RequestFactory`, `SimpleTestCase`/`TestCase` assertions),
> **[pytest-django]** `client`, `admin_client`, `rf`, `settings` fixtures,
> **[third-party: factory_boy]** for data (optional).

## 1. Purpose and scope

The view layer translates HTTP into calls to services and back. Its tests answer a narrow set of
questions:

- Does the URL resolve to the view, accept the right methods, and reject the wrong ones?
- Who may call it — anonymous, logged-in, staff, object owner — and what does everyone else get
  (302 to login, 403, 404)?
- Does valid input produce the intended **side effect** (DB change, email, redirect) and invalid
  input re-render with field errors and **no** side effect?
- Does the response carry the right template, context, headers, cookies and file content?
- Does middleware add, reject or transform what it is supposed to, in the right order?

Business rules themselves are tested at the service level
([service-and-architecture-testing.md](service-and-architecture-testing.md)); view tests prove
the wiring and the HTTP contract, not every rule permutation.

Out of scope: request-lifecycle internals ([django-core request lifecycle](../../django-core/references/request-response-lifecycle.md)),
form framework mechanics ([django-core forms](../../django-core/references/forms-validation.md)),
middleware design ([django-core middleware](../../django-core/references/middleware.md)).

## 2. Core concepts

| Tool | Provenance | Runs URL resolution | Runs middleware | Runs view | Typical use |
|---|---|---|---|---|---|
| `Client` / `client` fixture | **[Django]** / **[pytest-django]** | Yes | Yes (full stack) | Yes | Default for view tests: integration through the real stack |
| `AsyncClient` / `async_client` | **[Django]** (≥ 3.1) / **[pytest-django]** | Yes | Yes (ASGI handler) | Yes | Async views — see [async-testing.md](async-testing.md) |
| `RequestFactory` / `rf` fixture | **[Django]** / **[pytest-django]** | No | No | Only if you call it | Unit-testing a view function, a CBV method, a decorator or a middleware class |
| `admin_client` | **[pytest-django]** | Yes | Yes | Yes | Quick superuser access; prefer explicit roles for permission tests |

Key response attributes (Client responses only, unless noted):

- `response.status_code`, `response.headers`, `response.content` / `response.text`
  (`.text` is Django ≥ 5.0 — `VERIFY`; otherwise `content.decode()`).
- `response.context` and `response.templates` — populated because the test runner instruments
  template rendering (`setup_test_environment()`); **not** available on responses produced by
  calling a view with `RequestFactory` unless the response is a `TemplateResponse` you render.
- `response.redirect_chain` — only with `follow=True`.
- `response.wsgi_request` — the `HttpRequest` actually processed, useful for inspecting
  `request.user` or `request.session` after middleware ran.
- `response.resolver_match` — which view/URL name handled the request.

Assertions on `SimpleTestCase` **[Django]** work with pytest too: either inherit from
`django.test.TestCase`, or call them via `pytest_django.asserts`
(`from pytest_django.asserts import assertContains, assertRedirects, assertTemplateUsed`)
**[pytest-django]**.

## 3. Internal mechanics

### What the test Client actually does

1. Builds a WSGI environ (`SERVER_NAME="testserver"`, path, method, encoded body, headers).
2. Passes it to `ClientHandler`, a `BaseHandler` subclass that loads the **full middleware
   stack** from `settings.MIDDLEWARE`.
3. By default sets `request._dont_enforce_csrf_checks = True`, so CSRF is *not* enforced unless
   you construct `Client(enforce_csrf_checks=True)`.
4. Connects to the `template_rendered` signal to capture templates and contexts, and to
   `request_started`/`request_finished` (DB connections are *not* closed between requests within
   a test).
5. Stores cookies from responses in `client.cookies` and sends them on subsequent requests —
   so sessions persist across calls on the same client instance.
6. Re-raises exceptions raised by the view unless `raise_request_exception=False` (Django ≥ 3.0),
   in which case you get the handler's 500 response and `response.exc_info`.

`setup_test_environment()` (done by the Django runner and by pytest-django) also forces
`ALLOWED_HOSTS` to include `testserver`, switches `EMAIL_BACKEND` to locmem and sets
`DEBUG=False` regardless of settings (unless `--debug-mode`).

### What RequestFactory does not do

`RequestFactory` returns a bare `WSGIRequest`. There is **no middleware**, so:

- `request.user` is unset — set it (`request.user = user` or `AnonymousUser()`).
- `request.session` is unset — attach `SessionStore()` or run `SessionMiddleware` manually.
- `request._messages` is unset — attach `FallbackStorage(request)` if the view uses messages.
- No URL resolution — `request.resolver_match` is `None`; pass URL kwargs to the view yourself.
- CSRF is not checked (unless you invoke the middleware).

That is the point: RequestFactory isolates the unit under test. It is also the trap: a view that
works under `rf` may be unreachable, mis-routed, or blocked by middleware in reality.

### Form validation flow (what you are asserting on)

`form.is_valid()` → `full_clean()` → per-field `clean()` (to_python → validate → validators) →
`clean_<field>()` → `clean()` → for ModelForms `_post_clean()` → `instance.full_clean(exclude=…)`
including model `validate_constraints()` (Django ≥ 4.1). Errors are stored in `form.errors`
(messages) and `form.errors.as_data()` (`ValidationError` objects with `.code`). Testing codes
is robust to copy/translation changes; testing messages is not.

## 4. Engineering decisions and trade-offs

### Client vs RequestFactory

| Choose | When | Avoid when |
|---|---|---|
| `Client` | Default. Proves routing, middleware, auth decorators, templates, redirects, cookies together | You need to isolate a single method with awkward preconditions |
| `RequestFactory` | Unit-testing a decorator, a middleware class, a CBV hook (`get_queryset`, `get_context_data`), or a view whose stack is expensive | You would have to hand-build user, session, messages and tenant — that's a sign the Client is the right tool |

Rule of thumb: **one Client test per view behaviour that a user can observe**; RequestFactory only
for logic that is hard to reach through HTTP or for reusable components (mixins, decorators,
middleware) tested once in isolation.

### How much to test in the view

| Concern | Test at the view? |
|---|---|
| Access control (anonymous/forbidden/owner) | **Yes**, every view |
| Happy path: status/redirect + DB effect | **Yes** |
| One invalid submission → re-render with errors, no DB change | **Yes** |
| Every validation rule permutation | **No** — test the form or service directly |
| Template markup details | Sparingly: key content and escaping, not layout |
| Exact context dict | Only keys the template contract depends on |

### `follow=True` or not

`follow=True` exercises the post-redirect page but hides *where* you were redirected and doubles
the request count. Prefer asserting the redirect with `assertRedirects(..., fetch_redirect_response=False)`
and, separately, testing the target view. Use `follow=True` when the post-redirect message or
page state is the behaviour under test.

### Login in tests

| Method | Cost | Proves |
|---|---|---|
| `client.force_login(user)` **[Django]** | Cheap: writes a session directly, skips backends and password hashing | Nothing about the login process |
| `client.login(username=..., password=...)` **[Django]** | Runs `authenticate()` and hashing | Backend configuration works |
| POST to the login URL | Full stack incl. CSRF, form, rate limiting | The real login flow |

Use `force_login` everywhere except the dedicated login-flow tests in
[security-testing.md](security-testing.md).

## 5. Django implementation patterns

### Fixtures

```python
# conftest.py
import pytest
from apps.accounts.tests.factories import UserFactory

@pytest.fixture
def user(db):
    return UserFactory()

@pytest.fixture
def logged_in_client(client, user):
    client.force_login(user)
    return client
```

Factories and fixture scoping: [fixtures-and-test-data.md](fixtures-and-test-data.md).

### Access-control behaviour of a protected view

```python
import pytest
from django.urls import reverse
from pytest_django.asserts import assertRedirects

pytestmark = pytest.mark.django_db

def test_anonymous_is_redirected_to_login(client, settings):
    url = reverse("invoices:list")
    response = client.get(url)
    assertRedirects(response, f"{reverse(settings.LOGIN_URL)}?next={url}",
                    fetch_redirect_response=False)

def test_user_without_permission_gets_403(logged_in_client):
    # @permission_required(..., raise_exception=True) or PermissionRequiredMixin for an
    # authenticated user → 403; without raise_exception it would redirect to login (302).
    response = logged_in_client.get(reverse("invoices:export"))
    assert response.status_code == 403

def test_other_users_invoice_is_404(logged_in_client):
    other = InvoiceFactory()                       # belongs to someone else
    response = logged_in_client.get(reverse("invoices:detail", args=[other.pk]))
    assert response.status_code == 404
```

Behaviour notes **[Django]**:

- `login_required` / `LoginRequiredMixin` → 302 to `LOGIN_URL?next=…` for anonymous users.
- `PermissionRequiredMixin` → 403 for authenticated users lacking the permission, redirect for
  anonymous ones (unless `raise_exception=True`, then 403 for both).
- `LoginRequiredMiddleware` (Django ≥ 5.1) makes every view require login except those marked
  `@login_not_required`. If the project uses it, add a test that the public views (login,
  password reset, health check) are still reachable anonymously and that a sample of others
  redirect — the failure mode is accidentally locking out the login page or opening a view.

### Form submission: success and failure

```python
from pytest_django.asserts import assertFormError, assertRedirects, assertTemplateUsed

def test_create_invoice_success(logged_in_client, user):
    customer = CustomerFactory(owner=user)
    response = logged_in_client.post(
        reverse("invoices:create"),
        {"customer": customer.pk, "number": "INV-1", "amount": "120.00"},
    )
    invoice = Invoice.objects.get(number="INV-1")
    assertRedirects(response, reverse("invoices:detail", args=[invoice.pk]),
                    fetch_redirect_response=False)
    assert invoice.owner == user                    # server-side ownership, not from POST

def test_create_invoice_invalid_amount_rerenders(logged_in_client, user):
    customer = CustomerFactory(owner=user)
    response = logged_in_client.post(
        reverse("invoices:create"),
        {"customer": customer.pk, "number": "INV-1", "amount": "-5"},
    )
    assert response.status_code == 200
    assertTemplateUsed(response, "invoices/invoice_form.html")
    assertFormError(response.context["form"], "amount", "Ensure this value is greater than or equal to 0.")
    assert not Invoice.objects.exists()
```

`assertFormError(form, field, errors)` takes the **form instance** (Django ≥ 4.1); the old
`assertFormError(response, "form", field, errors)` signature was deprecated in 4.1 and removed in
5.0. Likewise `assertFormSetError(formset, form_index, field, errors)`. Use `field=None` for
non-field errors.

### Form unit tests — assert codes, not messages

```python
from apps.invoices.forms import InvoiceForm

@pytest.mark.django_db
def test_due_date_before_issue_date_is_rejected():
    form = InvoiceForm(data={"issue_date": "2026-03-10", "due_date": "2026-03-01", "amount": "10"})
    assert not form.is_valid()
    codes = [e.code for e in form.errors.as_data()["__all__"]]
    assert "due_before_issue" in codes

def test_form_rejects_unknown_customer_choice(user):
    form = InvoiceForm(data={...}, user=user)       # queryset scoped to user's customers
    assert not form.is_valid()
    assert form.has_error("customer", code="invalid_choice")
```

`form.has_error(field, code=...)` **[Django]** is the cleanest code assertion. For ModelForms,
test that (a) excluded/server-owned fields cannot be set via data (mass assignment), (b) model
constraints surface as form errors (Django ≥ 4.1 `validate_constraints`), and (c) `save(commit=False)`
callers set required server fields. Pure `forms.Form` tests need no database — use `SimpleTestCase`
or omit `django_db`.

### Templates and content

```python
from pytest_django.asserts import assertContains, assertNotContains, assertInHTML

def test_dashboard_lists_only_open_invoices(logged_in_client, user):
    InvoiceFactory(owner=user, number="OPEN-1", status="open")
    InvoiceFactory(owner=user, number="PAID-1", status="paid")
    response = logged_in_client.get(reverse("invoices:dashboard"))
    assertContains(response, "OPEN-1", count=1)
    assertNotContains(response, "PAID-1")
    assertInHTML('<span class="badge">1 open</span>', response.content.decode())
    assert list(response.context["invoices"]) == [...]
```

- `assertContains(..., html=True)` compares semantically (attribute order, whitespace ignored).
- `assertContains` also asserts `status_code` (default 200) — pass `status_code=` when needed.
- Assert on visible text that is part of the contract; don't snapshot whole pages.
- Turn on `string_if_invalid` (or pytest-django's `--fail-on-template-vars` **[pytest-django]**)
  in a test settings profile to catch misspelled context variables.

### Unit-testing a CBV hook with RequestFactory

```python
from django.contrib.auth.models import AnonymousUser
from apps.invoices.views import InvoiceListView

def test_get_queryset_scopes_to_user(rf, user):
    mine = InvoiceFactory(owner=user)
    InvoiceFactory()
    request = rf.get("/ignored/")
    request.user = user
    view = InvoiceListView()
    view.setup(request)                      # sets view.request/args/kwargs (Django ≥ 2.2)
    assert list(view.get_queryset()) == [mine]
```

Use this when the hook's logic has many branches; otherwise a single Client test covers it.
To call the whole view: `response = InvoiceListView.as_view()(request)`; for a
`TemplateResponse`, call `response.render()` before inspecting `content`, and read
`response.context_data` instead of `response.context`.

### RequestFactory with session and messages

```python
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore

def build_request(rf, user, path="/", method="post", data=None):
    request = getattr(rf, method)(path, data or {})
    request.user = user
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request
```

If you need more than this helper, switch to the Client.

### Messages

```python
from django.contrib.messages import SUCCESS, Message
from django.contrib.messages.test import MessagesTestMixin   # Django ≥ 5.0

class TestArchive(MessagesTestMixin, TestCase):
    def test_archive_flashes_success(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("invoices:archive", args=[self.inv.pk]), follow=True)
        self.assertMessages(response, [Message(SUCCESS, "Invoice archived.")])
```

In pytest without the mixin: `list(get_messages(response.wsgi_request))`.

### Request headers and query strings

```python
response = client.get(
    reverse("reports:list"),
    {"status": "open"},                                  # query string for GET
    headers={"accept-language": "de", "x-request-id": "t-1"},   # Django ≥ 4.2
)
```

Before 4.2, headers were passed as WSGI environ kwargs (`HTTP_ACCEPT_LANGUAGE="de"`), which still
works. Django ≥ 5.1 adds a `query_params` argument to all client methods so POST/PUT requests can
carry a query string without hand-building the URL — `VERIFY` against the installed version.

### Middleware: unit and integration

```python
# apps/core/middleware.py
class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        request.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
```

```python
# unit: stub get_response, no stack
from django.http import HttpResponse

def test_request_id_is_propagated(rf):
    seen = {}
    def get_response(request):
        seen["id"] = request.request_id
        return HttpResponse()
    mw = RequestIDMiddleware(get_response)
    response = mw(rf.get("/", headers={"x-request-id": "abc"}))
    assert seen["id"] == "abc" and response["X-Request-ID"] == "abc"

def test_request_id_generated_when_missing(rf):
    response = RequestIDMiddleware(lambda r: HttpResponse())(rf.get("/"))
    assert len(response["X-Request-ID"]) == 32

# integration: proves it is installed and ordered correctly
@pytest.mark.django_db
def test_request_id_present_on_real_responses(client):
    assert "X-Request-ID" in client.get(reverse("health")).headers
```

Patterns:

- **Short-circuiting middleware** (tenant resolution, maintenance mode, IP allow-list): assert
  the view is *not* called on rejection — a `get_response` stub that fails the test if invoked.
- **Ordering dependencies**: if middleware reads `request.user`, an integration test proves it
  sits after `AuthenticationMiddleware`. A one-line test that asserts relative positions in
  `settings.MIDDLEWARE` is cheap insurance for order-critical pairs.
- **`process_exception`**: use a test-only URLconf (`@pytest.mark.urls("tests.urls_boom")`
  **[pytest-django]** or `@override_settings(ROOT_URLCONF=...)` **[Django]**) with a view that
  raises, and `Client(raise_request_exception=False)` to see the handled response.
- **Async-capable middleware**: test both paths if the class declares `async_capable`
  — see [async-testing.md](async-testing.md).
- Toggle middleware per test with `modify_settings(MIDDLEWARE={"remove": [...]})` **[Django]**
  or the `settings` fixture **[pytest-django]**; never mutate `settings.MIDDLEWARE` in place.

### File and streaming responses

```python
def test_invoice_pdf_download(logged_in_client, user):
    invoice = InvoiceFactory(owner=user, number="INV-7")
    response = logged_in_client.get(reverse("invoices:pdf", args=[invoice.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'attachment; filename="INV-7.pdf"'
    body = b"".join(response.streaming_content)       # FileResponse is streaming
    assert body.startswith(b"%PDF-")

def test_csv_export_streams_rows(logged_in_client, user):
    InvoiceFactory.create_batch(3, owner=user)
    response = logged_in_client.get(reverse("invoices:export_csv"))
    assert response.streaming
    lines = b"".join(response.streaming_content).decode().splitlines()
    assert len(lines) == 4                               # header + 3 rows
```

`response.content` raises on streaming responses — use `streaming_content`. Close file handles
(`response.close()`) when a test opens many, or Windows may hold locks on temporary files.
Store uploaded/generated files in `tmp_path` or `InMemoryStorage` (Django ≥ 4.2) so tests never
write to the real media root — see [integration-and-background-task-testing.md](integration-and-background-task-testing.md).

### Sessions and cookies

```python
def test_cart_survives_requests(client):
    session = client.session            # property returns a new SessionStore each access
    session["cart"] = [1, 2]
    session.save()                      # required; then the session cookie is set on client
    response = client.get(reverse("cart:detail"))
    assertContains(response, "2 items")

def test_preference_cookie_flags(client):
    response = client.post(reverse("prefs:theme"), {"theme": "dark"})
    cookie = response.cookies["theme"]
    assert cookie.value == "dark"
    assert cookie["samesite"] == "Lax" and cookie["httponly"]
```

Assign `client.session` to a variable before modifying it — `client.session["x"] = 1` mutates a
throwaway object. Session security (rotation, fixation, expiry) is in
[security-testing.md](security-testing.md).

### Error handlers

```python
@pytest.mark.urls("tests.urls_errors")             # defines handler404/handler500 + a raising view
def test_custom_500_page(client):
    client.raise_request_exception = False
    response = client.get("/boom/")
    assert response.status_code == 500
    assertTemplateUsed(response, "500.html")
    assert b"Traceback" not in response.content
```

`DEBUG` is `False` under the test runner, so custom 404/500 handlers are active.

### Internationalisation (brief)

Send `headers={"accept-language": "de"}` (with `LocaleMiddleware`) or use `translation.override("de")`
**[Django]** for form/unit tests. Assert translated strings only in a few i18n smoke tests;
elsewhere assert codes or language-independent markers.

## 6. Production use cases

- **Order checkout form**: Client tests for anonymous redirect, owner success (redirect + order
  row + `mail.outbox`), one invalid submission with no DB change; form unit tests for each
  cross-field rule by error code.
- **Admin-like back office with permissions**: parametrised matrix over roles × views asserting
  302/403/200 ([security-testing.md](security-testing.md) owns the matrix pattern).
- **Tenant middleware**: unit tests for host/header parsing and the reject path; one
  integration test that a request on tenant A's host never sees tenant B's data.
- **Report download**: content-type, filename quoting with non-ASCII names, streaming body,
  and a query-count assertion for the generator ([performance-and-reliability-testing.md](performance-and-reliability-testing.md)).
- **Migration to `LoginRequiredMiddleware`**: an inventory test that iterates URL patterns and
  asserts anonymous access returns a redirect except for an explicit allow-list.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Client integration tests | Real routing/middleware/templates; catches wiring bugs; refactor-safe | Slower; failures can be far from the cause; needs DB for auth |
| RequestFactory unit tests | Fast, precise, no middleware noise | Manual request setup; can pass while the real stack fails |
| Form unit tests | Fast, exhaustive rule coverage, no HTTP | Do not prove the view uses the form correctly |
| Template content assertions | Catch missing/escaped content | Brittle if they assert layout or full HTML |
| `follow=True` | Tests the user-visible end state | Hides redirect target; extra queries |

## 8. Security and performance considerations

- The Client **does not enforce CSRF by default**. A suite that never uses
  `Client(enforce_csrf_checks=True)` cannot catch a broken CSRF setup — keep a few CSRF tests
  ([security-testing.md](security-testing.md)).
- Assert that server-owned fields (`owner`, `status`, `price`) are ignored when posted.
- Assert escaping of user-provided content in at least one template test
  (`assertContains(response, "&lt;script&gt;")`), and that error pages leak no stack traces.
- `force_login` is cheap; `login()` with the default PBKDF2 hasher is slow — set
  `PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]` in test settings.
- `SimpleTestCase` view tests (no DB) are fastest for static or cache-only views; anything
  touching sessions with the DB backend needs DB access.
- Avoid rendering heavy templates in tests that only assert redirects/DB effects; for
  rendering-agnostic logic, prefer service tests.

## 9. Common mistakes and anti-patterns

- **Testing only `status_code == 200`** — also assert the DB effect, redirect target or key content.
- **Asserting full HTML** or long substrings of layout — breaks on cosmetic changes.
- **Asserting validation messages** instead of error codes — breaks on copy edits/translations.
- **`client.session["k"] = v` without saving** — the change is lost silently.
- **RequestFactory for everything** — hand-built requests drift from what middleware produces.
- **`admin_client` for permission tests** — superusers bypass permission checks; use explicit roles.
- **Old `assertFormError(response, "form", ...)`** — fails on Django ≥ 5.0.
- **Reading `response.content` on `FileResponse`** — raises; use `streaming_content`.
- **Mutating `settings.MIDDLEWARE` directly** — leaks into other tests; use `modify_settings` or
  the `settings` fixture.
- **Relying on `follow=True` for access-control tests** — the final 200 of the login page looks
  like success.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `response.context` is `None` | Response not from Client, or template not rendered via Django engine (e.g. Jinja2 without instrumentation) | Use Client; for Jinja2, assert on content |
| `AttributeError: 'WSGIRequest' has no attribute 'user'/'session'` | RequestFactory without middleware | Set attributes manually or use Client |
| `MessageFailure: You cannot add messages without installing MessageMiddleware` | RequestFactory request lacks `_messages` | Attach `FallbackStorage(request)` |
| 400 Bad Request on every request | `ALLOWED_HOSTS` mismatch when using a custom `SERVER_NAME`/`HTTP_HOST` | Add host to test settings or use default `testserver` |
| Redirect to login despite `force_login` | Different client instance, custom auth backend not in `AUTHENTICATION_BACKENDS`, or inactive user | Pass `backend=` to `force_login`; check `is_active` |
| `assertRedirects` fails fetching target | Target view needs extra state | `fetch_redirect_response=False` and test the target separately |
| Test passes alone, fails in suite | Leaked `settings`, cache, translation or URLconf state | See [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |
| Exception instead of 500 response | Client re-raises by default | `Client(raise_request_exception=False)` |

Validation checklist for a view change: anonymous / forbidden / not-owner / owner cases; one
invalid submission with no side effect; redirect target; template or key content; headers and
cookies where they are part of the contract.

## 11. Related references and skills

- [django-rest-framework-testing.md](django-rest-framework-testing.md) — API views.
- [security-testing.md](security-testing.md) — CSRF, login flows, IDOR, permission matrices.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — users, factories, fixture scope.
- [async-testing.md](async-testing.md) — `AsyncClient`, async views and middleware.
- [django-testing-framework.md](django-testing-framework.md) — test classes and `override_settings`.
- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — `client`, `rf`, `settings`, `urls` marker.
- django-core: [urls-views.md](../../django-core/references/urls-views.md),
  [forms-validation.md](../../django-core/references/forms-validation.md),
  [middleware.md](../../django-core/references/middleware.md),
  [authentication-sessions.md](../../django-core/references/authentication-sessions.md),
  [file-handling.md](../../django-core/references/file-handling.md).
- [django-frontend](../../django-frontend/SKILL.md) — template and JavaScript concerns.

## 12. Official documentation and external references

- Django testing tools (Client, RequestFactory, assertions): <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Advanced testing topics (RequestFactory, testing middleware): <https://docs.djangoproject.com/en/5.2/topics/testing/advanced/>
- Django forms API (`errors.as_data`, `has_error`): <https://docs.djangoproject.com/en/5.2/ref/forms/api/>
- Messages framework testing (`MessagesTestMixin`): <https://docs.djangoproject.com/en/5.2/ref/contrib/messages/#testing>
- `LoginRequiredMiddleware`: <https://docs.djangoproject.com/en/5.2/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware>
- Request/response objects (`FileResponse`, `StreamingHttpResponse`): <https://docs.djangoproject.com/en/5.2/ref/request-response/>
- pytest-django helpers and assertions: <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- Django 4.1 release notes (`assertFormError` signature): <https://docs.djangoproject.com/en/5.2/releases/4.1/>
- Django 5.1 release notes (`query_params`, `LoginRequiredMiddleware`): <https://docs.djangoproject.com/en/5.2/releases/5.1/>
