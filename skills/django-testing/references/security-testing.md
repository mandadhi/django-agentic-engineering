# Security Testing

> **Scope:** How to prove Django security controls with automated tests — login/logout and
> password-reset flows, session rotation and expiry, permission boundaries, CSRF enforcement,
> the CORS boundary, upload validation, object-level authorisation (IDOR), multi-tenant
> isolation, configuration and header checks, and regression tests for fixed vulnerabilities.
> Which controls must exist and how to implement them is owned by
> [django-security-engineering](../../django-security-engineering/SKILL.md); its
> [security-testing-and-incident-response.md](../../django-security-engineering/references/security-testing-and-incident-response.md)
> owns SAST/DAST programmes and incident response.
>
> **Baseline:** Django 5.2 LTS · DRF 3.16 · pytest ≥ 8 · pytest-django 4.x · **Depends on:**
> **[Django]** test `Client`, `django.core.mail`, `django.contrib.auth` tokens, `check --deploy`;
> **[DRF]** `APIClient`; **[pytest-django]** fixtures; optional **[third-party: django-cors-headers]**,
> **[third-party: django-axes]**, **[third-party: time-machine / freezegun]**.

## 1. Purpose and scope

Security tests differ from functional tests in one way: they assert that **something does not
happen** — the attacker's request is denied, the foreign object is invisible, the token stops
working, the dangerous file is rejected. Functional suites almost never cover these because the
happy path passes without them.

This reference provides reusable test patterns for each control class. It does not decide which
controls a system needs (threat modelling, [django-security-engineering](../../django-security-engineering/SKILL.md))
and does not cover penetration testing, DAST or dependency scanning beyond pointing at their
owners.

## 2. Core concepts

- **Failing-case test**: for every control, a test that performs the attack and asserts denial.
  The control is "tested" only when removing it makes a test fail.
- **Actor matrix**: anonymous, authenticated-no-role, each role, owner, same-tenant non-owner,
  other-tenant user, staff/superuser. Most authorisation bugs are a missing row.
- **Boundary inventory**: the set of entry points (URL patterns, API actions, admin, tasks,
  commands, webhooks) that tests enumerate *programmatically*, so new routes are covered by
  default rather than by memory.
- **Existence hiding**: out-of-scope objects return **404**, not 403, so IDs cannot be probed.
- **Regression test**: a test named after the ticket/advisory, reproducing the exploit, kept
  forever.
- **Mutation check of a control**: temporarily removing the permission class / scoping filter
  and confirming tests fail — the strongest evidence a suite protects the control
  ([coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) covers mutation tools).

## 3. Internal mechanics

Mechanics that change what a test proves:

- **CSRF is off in the test client** by default (`_dont_enforce_csrf_checks`). Only
  `Client(enforce_csrf_checks=True)` / `APIClient(enforce_csrf_checks=True)` exercises
  `CsrfViewMiddleware`. DRF's `SessionAuthentication` enforces CSRF only for session-authenticated
  requests; token-authenticated requests are not CSRF-checked.
- **Session rotation**: `django.contrib.auth.login()` calls `request.session.cycle_key()`
  (rotating the session key and so preventing fixation); `logout()` calls `session.flush()`.
  `force_login()` writes a new session directly and does not prove rotation — use real `login`
  via POST to test it.
- **Password-reset tokens** (`PasswordResetTokenGenerator`) are HMACs over user pk, password
  hash, `last_login`, timestamp and email (email included since Django 3.1); they are invalidated
  by password change or login and expire after `PASSWORD_RESET_TIMEOUT` seconds.
- **`update_session_auth_hash`** keeps the current session alive after a password change; all
  other sessions are invalidated because the session's auth hash no longer matches
  (`SessionAuthenticationMiddleware` behaviour is built into `get_user()` since Django 1.10).
- **CORS headers** are added by **[third-party: django-cors-headers]** middleware; the browser,
  not Django, enforces them. A request from a disallowed origin still reaches the view and still
  executes — CORS tests assert headers only, never access control.
- **`check --deploy`** runs the `security.W0xx` system checks against the *active* settings
  module — run it with production settings, not test settings.

Control internals: [CSRF](../../django-security-engineering/references/csrf-protection.md),
[sessions](../../django-security-engineering/references/session-and-cookie-security.md),
[authentication](../../django-security-engineering/references/authentication-security.md).

## 4. Engineering decisions and trade-offs

| Decision | Recommendation | Trade-off |
|---|---|---|
| Hand-written per-view auth tests vs URL-inventory tests | Both: inventory test guarantees every route denies anonymous by default; per-view matrices prove role logic | Inventory needs an allow-list and URL kwargs generation |
| 404 vs 403 for out-of-scope objects | Assert 404 (existence hiding) unless the product explicitly accepts 403 | Must be consistent across HTML and API |
| `force_login` vs real login in security tests | Real login for session/rotation/lockout tests; `force_login` elsewhere | Real login is slower and couples to the form |
| Tenant tests per endpoint vs generic fixture pair | Generic `tenant_a`/`tenant_b` fixture pair + parametrised routes | Requires a consistent tenant model |
| Security tests in the main suite vs separate job | Main suite (they are cheap); DAST/scanners in separate jobs | — |
| Testing third-party controls (axes, cors-headers) | Test *your configuration* of them (thresholds, origins), not their internals | — |

## 5. Django implementation patterns

### Actor and tenant fixtures

```python
# conftest.py
import pytest

@pytest.fixture
def tenant_a(db):
    return TenantFactory(slug="a")

@pytest.fixture
def tenant_b(db):
    return TenantFactory(slug="b")

@pytest.fixture
def actors(tenant_a, tenant_b):
    return {
        "anonymous": None,
        "member": UserFactory(tenant=tenant_a, role="member"),
        "manager": UserFactory(tenant=tenant_a, role="manager"),
        "other_tenant": UserFactory(tenant=tenant_b, role="manager"),
        "staff": UserFactory(tenant=tenant_a, is_staff=True),
        "tenant": tenant_a,
    }
```

### Login, logout and session rotation

```python
pytestmark = pytest.mark.django_db

def test_login_rotates_session_key(client):
    user = UserFactory(password="S3cure-pass!")
    client.get(reverse("login"))                      # establish an anonymous session
    session = client.session
    session["probe"] = 1
    session.save()
    before = client.session.session_key
    client.post(reverse("login"), {"username": user.username, "password": "S3cure-pass!"})
    assert client.session.session_key != before        # fixation prevented
    assert int(client.session["_auth_user_id"]) == user.pk

def test_logout_flushes_session_and_requires_post(client):
    user = UserFactory()
    client.force_login(user)
    assert client.get(reverse("logout")).status_code == 405   # GET logout removed in Django 5.0
    client.post(reverse("logout"))
    assert "_auth_user_id" not in client.session

def test_wrong_password_and_unknown_user_look_identical(client):
    UserFactory(username="alice", password="right-pass-1")
    r1 = client.post(reverse("login"), {"username": "alice", "password": "wrong"})
    r2 = client.post(reverse("login"), {"username": "nobody", "password": "wrong"})
    assert r1.status_code == r2.status_code == 200
    assert r1.context["form"].errors == r2.context["form"].errors
```

`LogoutView` accepts only POST since Django 5.0 (GET deprecated in 4.1). Adjust if the project
uses a custom logout view.

### Session expiry and invalidation

```python
def test_password_change_invalidates_other_sessions(client):   # from django.test import Client
    user = UserFactory(password="old-pass-123")
    other_device = Client()
    other_device.force_login(user)
    client.force_login(user)
    client.post(reverse("password_change"),
                {"old_password": "old-pass-123", "new_password1": "N3w-pass-xyz!", "new_password2": "N3w-pass-xyz!"})
    assert other_device.get(reverse("dashboard")).status_code == 302   # logged out
    assert client.get(reverse("dashboard")).status_code == 200          # current session kept

def test_idle_session_expires(client, settings, time_machine):   # third-party: time-machine
    settings.SESSION_COOKIE_AGE = 900
    settings.SESSION_SAVE_EVERY_REQUEST = True
    client.force_login(UserFactory())
    time_machine.move_to(timezone.now() + timedelta(minutes=16))
    assert client.get(reverse("dashboard")).status_code == 302
```

Cookie flags are a configuration check: assert `response.cookies["sessionid"]["secure"]`,
`["httponly"]`, `["samesite"]` under production-like settings
(`override_settings(SESSION_COOKIE_SECURE=True, ...)` or a settings-module test).

### Password reset, end to end

```python
import re
from django.core import mail

def test_password_reset_flow(client):
    user = UserFactory(email="alice@example.test")
    client.post(reverse("password_reset"), {"email": "alice@example.test"})
    assert len(mail.outbox) == 1
    link = re.search(r"https?://[^/]+(/reset/\S+/)", mail.outbox[0].body).group(1)

    r = client.get(link, follow=True)                    # token swapped for session-held token
    set_password_url = r.redirect_chain[-1][0]
    client.post(set_password_url, {"new_password1": "N3w-pass-xyz!", "new_password2": "N3w-pass-xyz!"})
    user.refresh_from_db()
    assert user.check_password("N3w-pass-xyz!")

    # token is single-use: password hash changed
    r = client.get(link, follow=True)
    assert not r.context["validlink"]

def test_reset_does_not_reveal_account_existence(client):
    r_known = client.post(reverse("password_reset"), {"email": UserFactory().email})
    r_unknown = client.post(reverse("password_reset"), {"email": "ghost@example.test"})
    assert r_known.status_code == r_unknown.status_code
    assert r_known["Location"] == r_unknown["Location"]
```

Also test: expired token (advance time past `PASSWORD_RESET_TIMEOUT`), inactive users receive no
email, and the reset email's link uses the configured domain rather than the request `Host`
header (host-header poisoning; requires `ALLOWED_HOSTS` and a fixed `domain_override` or Sites
configuration — see [django-security-engineering authentication](../../django-security-engineering/references/authentication-security.md)).

Password validators: one test that `validate_password("password")` raises with the expected
codes under the project's `AUTH_PASSWORD_VALIDATORS`, and that the signup/change form surfaces it.

### Brute force and rate limits

```python
def test_login_locks_after_failures(client, settings):
    # third-party: django-axes — assert the project's configured threshold, not axes internals
    user = UserFactory(password="right-pass-1")
    for _ in range(settings.AXES_FAILURE_LIMIT):
        client.post(reverse("login"), {"username": user.username, "password": "bad"})
    r = client.post(reverse("login"), {"username": user.username, "password": "right-pass-1"})
    assert r.status_code in {403, 429}
```

DRF throttle test mechanics (cache, rate patching): [django-rest-framework-testing.md](django-rest-framework-testing.md).

### Route inventory: default-deny for every URL

```python
from django.urls import get_resolver, URLPattern, URLResolver

PUBLIC = {"login", "password_reset", "password_reset_done", "health", "api:schema"}

def iter_named_patterns(resolver=None, ns=""):
    resolver = resolver or get_resolver()
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            new_ns = f"{ns}{p.namespace}:" if p.namespace else ns
            yield from iter_named_patterns(p, new_ns)
        elif isinstance(p, URLPattern) and p.name:
            yield f"{ns}{p.name}", p

@pytest.mark.django_db
@pytest.mark.parametrize("name, pattern", list(iter_named_patterns()), ids=lambda v: str(v))
def test_route_denies_anonymous(client, name, pattern):
    if name in PUBLIC or name.startswith("admin:"):
        pytest.skip("public or covered by admin tests")
    kwargs = {k: 1 if "pk" in k or k.endswith("id") else "x" for k in pattern.pattern.regex.groupindex}
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code in {302, 401, 403, 404, 405}
```

Parametrising at collection time touches the URLconf at import — acceptable here because
`get_resolver()` needs no DB. Keep `PUBLIC` reviewed; a new public route must be added
deliberately. The same idea inventories `csrf_exempt` views:

```python
def test_csrf_exempt_views_are_allow_listed():
    exempt = {name for name, p in iter_named_patterns()
              if getattr(p.callback, "csrf_exempt", False)}
    assert exempt <= {"webhooks:stripe", "webhooks:github"}    # each must verify signatures
```

DRF `APIView.as_view()` marks views `csrf_exempt` because CSRF is enforced inside
`SessionAuthentication` — filter DRF views out (check `hasattr(p.callback, "cls")`) or include
them in a separate assertion.

### CSRF enforcement

```python
def test_html_form_rejects_post_without_token(db):
    client = Client(enforce_csrf_checks=True)
    client.force_login(UserFactory())
    assert client.post(reverse("profile:update"), {"name": "x"}).status_code == 403

def test_html_form_accepts_post_with_token(db):
    client = Client(enforce_csrf_checks=True)
    client.force_login(UserFactory())
    client.get(reverse("profile:update"))                       # sets csrftoken cookie
    token = client.cookies["csrftoken"].value
    r = client.post(reverse("profile:update"), {"name": "x", "csrfmiddlewaretoken": token})
    assert r.status_code == 302

def test_session_api_write_requires_csrf(db):
    api = APIClient(enforce_csrf_checks=True)
    api.force_login(UserFactory())
    assert api.post(reverse("api:note-list"), {"text": "x"}, format="json").status_code == 403
```

For HTTPS-only origin checks (`CSRF_TRUSTED_ORIGINS`), send `secure=True` and an `Origin`
header in the test and assert acceptance/rejection — `VERIFY` exact header requirements against
the installed Django version, as origin checking rules changed in Django 4.0.

### CORS boundary

```python
# third-party: django-cors-headers, configured with CORS_ALLOWED_ORIGINS = ["https://app.example.test"]
def test_cors_allows_configured_origin(client):
    r = client.get(reverse("api:ping"), headers={"origin": "https://app.example.test"})
    assert r["Access-Control-Allow-Origin"] == "https://app.example.test"

def test_cors_omits_header_for_unknown_origin(client):
    r = client.get(reverse("api:ping"), headers={"origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers

def test_cors_preflight(client):
    r = client.options(reverse("api:ping"), headers={
        "origin": "https://app.example.test",
        "access-control-request-method": "POST",
    })
    assert r.status_code == 200 and "POST" in r["Access-Control-Allow-Methods"]
```

These tests prove configuration only. **CORS is not authorisation**: the disallowed-origin
request above still executed the view. Authorisation tests remain mandatory
([CORS and browser security](../../django-security-engineering/references/cors-and-browser-security.md)).

### Upload validation

```python
from django.core.files.uploadedfile import SimpleUploadedFile

@pytest.fixture
def private_storage(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path

@pytest.mark.parametrize("name, content, ctype, code", [
    ("cv.pdf", b"MZ\x90\x00 executable", "application/pdf", "invalid_content"),   # spoofed type
    ("cv.exe", b"%PDF-1.7", "application/pdf", "invalid_extension"),
    ("big.pdf", b"%PDF-1.7" + b"0" * (11 * 1024 * 1024), "application/pdf", "file_too_large"),
])
def test_rejects_dangerous_uploads(make_client, user, private_storage, name, content, ctype, code):
    upload = SimpleUploadedFile(name, content, content_type=ctype)
    r = make_client(user).post(reverse("api:document-list"), {"file": upload}, format="multipart")
    assert r.status_code == 400
    assert r.data["file"][0].code == code
    assert not any(private_storage.rglob("*"))

def test_filename_is_server_generated(make_client, user, private_storage):
    upload = SimpleUploadedFile("../../etc/passwd.pdf", b"%PDF-1.7", content_type="application/pdf")
    r = make_client(user).post(reverse("api:document-list"), {"file": upload}, format="multipart")
    stored = Document.objects.get(pk=r.json()["id"]).file.name
    assert ".." not in stored and "passwd" not in stored
```

Also test that downloads go through a permission-checked view (other user → 404) and that the
stored file is not reachable at a public media URL. Controls:
[file-upload security](../../django-security-engineering/references/file-upload-security.md).

### Object-level authorisation (IDOR) across every route

```python
OBJECT_ROUTES = [
    ("get",    "api:invoice-detail"),
    ("patch",  "api:invoice-detail"),
    ("delete", "api:invoice-detail"),
    ("post",   "api:invoice-send"),       # @action routes are frequently forgotten
    ("get",    "invoices:pdf"),           # HTML/file routes too
]

@pytest.mark.parametrize("method, route", OBJECT_ROUTES)
def test_other_tenant_cannot_touch_invoice(make_client, actors, tenant_b, method, route):
    foreign = InvoiceFactory(tenant=tenant_b, status="draft")
    client = make_client(actors["manager"])           # tenant A manager
    r = getattr(client, method)(reverse(route, args=[foreign.pk]))
    assert r.status_code == 404
    foreign.refresh_from_db()
    assert foreign.status == "draft"                  # unchanged
```

Always assert **no state change** after a denied write — some bugs return 404 *after* writing.
Also test **references in payloads**: creating an object that points at a foreign-tenant FK
(`{"customer": foreign_customer.pk}`) must be rejected.

### Multi-tenant isolation beyond HTTP

- **Lists/search/exports/aggregates**: tenant A sees counts and totals of A only.
- **Tasks**: call the task with tenant B's object id under tenant A's context and assert refusal,
  or assert the task re-derives tenant from the object.
- **Caches**: render as tenant A, then tenant B, assert B never sees A's cached fragment (cache
  keys include tenant).
- **Files**: storage paths include tenant; signed URLs for A are rejected for B's user.
- **Admin**: staff of tenant A cannot list B's objects if admin is tenant-scoped.

Design: [multi-tenant security](../../django-security-engineering/references/multi-tenant-security.md),
[authorisation](../../django-security-engineering/references/authorization-and-access-control.md).

### Mass assignment and output exposure

```python
def test_cannot_escalate_role_via_profile_update(make_client, actors):
    member = actors["member"]
    make_client(member).patch(reverse("api:me"), {"role": "manager", "is_staff": True}, format="json")
    member.refresh_from_db()
    assert member.role == "member" and member.is_staff is False

def test_user_payload_excludes_sensitive_fields(make_client, actors):
    body = make_client(actors["member"]).get(reverse("api:me")).json()
    assert not {"password", "last_login_ip", "mfa_secret"} & body.keys()
```

### XSS escaping

```python
def test_comment_body_is_escaped(logged_in_client, user):
    CommentFactory(author=user, body="<script>alert(1)</script>")
    r = logged_in_client.get(reverse("comments:list"))
    assertContains(r, "&lt;script&gt;alert(1)&lt;/script&gt;")
    assertNotContains(r, "<script>alert(1)</script>")
```

Add this for every template path that renders user content with `|safe`, `mark_safe` or a
sanitiser ([cross-site scripting](../../django-security-engineering/references/cross-site-scripting.md)).

### Configuration and headers

```python
import os, subprocess, sys

@pytest.mark.slow
def test_production_settings_pass_check_deploy():
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING",
         "--settings", "config.settings.production"],
        capture_output=True, text=True,
        env={**os.environ, "DJANGO_SECRET_KEY": "x" * 50, "DATABASE_URL": "sqlite://"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Running in a subprocess avoids importing production settings into the test process. Header test
(with production-like middleware settings):

```python
@override_settings(SECURE_CONTENT_TYPE_NOSNIFF=True, SECURE_REFERRER_POLICY="same-origin",
                   X_FRAME_OPTIONS="DENY")
def test_security_headers_present(client, db):
    r = client.get(reverse("home"))
    assert r["X-Content-Type-Options"] == "nosniff"
    assert r["X-Frame-Options"] == "DENY"
    assert r["Referrer-Policy"] == "same-origin"
```

### Regression tests for vulnerabilities

```python
@pytest.mark.security
def test_sec_142_invoice_export_ignored_tenant_filter(make_client, actors, tenant_b):
    """SEC-142: /invoices/export?all=1 bypassed tenant scoping. Must never return foreign rows."""
    InvoiceFactory(tenant=tenant_b, number="B-1")
    r = make_client(actors["manager"]).get(reverse("invoices:export_csv"), {"all": "1"})
    assert b"B-1" not in b"".join(r.streaming_content)
```

Conventions: name and docstring reference the ticket/advisory; reproduce the original exploit
request exactly; never delete; register a `security` marker (`--strict-markers`) so the set can
be run on its own ([pytest-and-pytest-django.md](pytest-and-pytest-django.md)).

## 6. Production use cases

- **SaaS onboarding**: login rotation, reset flow, enumeration resistance, lockout threshold,
  password-change invalidating other sessions.
- **Multi-tenant API**: route inventory + IDOR matrix for every ViewSet and `@action`,
  payload-FK scoping, tenant-scoped exports, tasks and caches.
- **SPA on a separate origin**: CORS allow-list tests, session API CSRF tests, cookie
  `SameSite`/`Secure` assertions under production settings.
- **Document management**: upload type/size/content/filename tests, private download view tests.
- **Post-incident**: regression test per finding, plus an inventory test that would have caught
  the class of bug (e.g. every export route checked for tenant scoping).

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Route inventory tests | New routes covered automatically; catches forgotten decorators | Needs URL kwarg synthesis and an allow-list; coarse (no role logic) |
| Parametrised actor matrices | Readable spec; gaps visible | Setup heavy; must be maintained per resource |
| Real-flow auth tests | Prove rotation, CSRF, lockout | Slower; coupled to forms/templates |
| Configuration tests (`check --deploy`, headers) | Catch insecure settings before deploy | Need production-like settings; can't see proxy/CDN behaviour |
| Unit tests vs scanners | Precise, fast, run on every PR | Only test what you anticipated — complement with SAST/DAST |

## 8. Security and performance considerations

- Test secrets are fake and obviously so (`"x" * 50`); never load real credentials into the test
  environment, CI logs or fixtures.
- Use `.test`/`example` domains and synthetic personal data ([fixtures-and-test-data.md](fixtures-and-test-data.md)).
- Fast hashers (MD5) in test settings make real-login tests cheap; don't let that leak into
  production settings — a `check --deploy`-style test of `PASSWORD_HASHERS` in production
  settings guards it.
- Inventory tests parametrised over hundreds of routes are fast if they need no data; keep object
  creation in the IDOR matrix, not the inventory.
- A security test that is skipped is worse than none — fail CI on unexpected skips in the
  `security` marker set.

## 9. Common mistakes and anti-patterns

- Testing that the owner *can* access, never that others *cannot*.
- Asserting 403/404 on a denied write without checking the object is unchanged.
- Covering detail routes but not lists, exports, search, `@action`s, file downloads or admin.
- Using `admin_client`/superusers as "a logged-in user" — bypasses permission checks.
- Relying on `force_login` to "test login" — rotation and CSRF unexercised.
- No test ever runs with `enforce_csrf_checks=True`.
- Treating CORS tests as access-control tests.
- Asserting exact error messages that reveal more than they should, instead of asserting they are generic.
- Deleting old regression tests during refactors because "the code is gone".

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| CSRF test passes without a token | Client created without `enforce_csrf_checks=True`, or view is `csrf_exempt` | Construct the enforcing client; check inventory |
| Session key unchanged after login | Used `force_login`, or custom login bypassing `auth.login()` | POST to the real login view |
| Reset link regex finds nothing | Email is HTML-only or link absolute with different domain | Parse `alternatives`, set `domain_override`/Sites in test |
| IDOR test returns 403 instead of 404 | Permission check before queryset scoping | Decide policy; scope queryset first for existence hiding |
| CORS header missing for allowed origin | Middleware order (must be high, before `CommonMiddleware`) or origin mismatch (scheme/port) | Check `MIDDLEWARE` and exact origin string |
| `check --deploy` test fails in CI only | Missing env vars for production settings | Provide dummy values in the subprocess env |
| Inventory test errors on `reverse` | Converters needing specific kwarg shapes (slug, uuid) | Map converter type → sample value |

Validation: for each control introduced or changed, remove it locally and confirm a test fails;
confirm the `security` marker set runs in CI and is not silently skipped.

## 11. Related references and skills

- [django-rest-framework-testing.md](django-rest-framework-testing.md) — API matrices, auth classes, throttling.
- [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md) — Client mechanics, sessions, cookies.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — actor and tenant fixtures.
- [integration-and-background-task-testing.md](integration-and-background-task-testing.md) — webhook signature tests, task isolation.
- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — freezing time for expiry tests.
- [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) — mutation testing of controls, static analysis.
- django-security-engineering: [SKILL.md](../../django-security-engineering/SKILL.md),
  [security-testing-and-incident-response.md](../../django-security-engineering/references/security-testing-and-incident-response.md),
  [authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md),
  [multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md),
  [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md),
  [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md),
  [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md),
  [secure-django-configuration.md](../../django-security-engineering/references/secure-django-configuration.md).
- [django-core authentication-sessions.md](../../django-core/references/authentication-sessions.md).

## 12. Official documentation and external references

- Django security overview: <https://docs.djangoproject.com/en/5.2/topics/security/>
- CSRF and testing: <https://docs.djangoproject.com/en/5.2/ref/csrf/#testing-and-csrf-protection>
- Authentication views (login, logout, password reset): <https://docs.djangoproject.com/en/5.2/topics/auth/default/#module-django.contrib.auth.views>
- Session invalidation on password change: <https://docs.djangoproject.com/en/5.2/topics/auth/default/#session-invalidation-on-password-change>
- Deployment checklist / `check --deploy`: <https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/>
- Email testing (`mail.outbox`): <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#email-services>
- DRF testing (`enforce_csrf_checks`): <https://www.django-rest-framework.org/api-guide/testing/>
- django-cors-headers (third-party): <https://github.com/adamchainz/django-cors-headers>
- django-axes (third-party): <https://django-axes.readthedocs.io/>
- OWASP Web Security Testing Guide: <https://owasp.org/www-project-web-security-testing-guide/>
- OWASP ASVS: <https://owasp.org/www-project-application-security-verification-standard/>
