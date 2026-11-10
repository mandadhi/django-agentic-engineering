# Authentication and sessions

> Scope: `django.contrib.auth` architecture, backends, custom user models, login/logout, passwords, permissions and groups, sessions and their backends, auth middleware and decorators. Not covered here: token/JWT/API auth ([django-rest-framework](../../django-rest-framework/SKILL.md)); deep hardening, MFA, brute-force protection, CSP ([django-security-engineering](../../django-security-engineering/SKILL.md)); middleware mechanics ([middleware.md](./middleware.md)); production cookie settings checklist ([production-configuration.md](./production-configuration.md)).

## Purpose and overview

Django separates three concerns that agents often blur:

- **Authentication** — who is this? (`authenticate()` via backends)
- **Session** — remember that across requests (`SessionMiddleware`, `login()`)
- **Authorisation** — may they do this? (`has_perm()`, object scoping in queries)

Getting the **user model** right on day one matters more than anything else here: swapping `AUTH_USER_MODEL` after the first migration is a painful, manual data migration.

Assumes Django 5.2 LTS; async APIs and `LoginRequiredMiddleware` are version-dependent — check `python -m django --version`.

## How it works internally

**Login flow.**

1. View calls `authenticate(request, username=..., password=...)`.
2. Django iterates `AUTHENTICATION_BACKENDS` in order; each backend's `authenticate()` returns a user, `None` (try next), or raises `PermissionDenied` (stop immediately).
3. `login(request, user)` cycles the session key (prevents session fixation), stores `_auth_user_id`, `_auth_user_backend` and `_auth_user_hash` (an HMAC of the password hash) in the session, rotates the CSRF token, and sends `user_logged_in`.

**Per-request flow.**

1. `SessionMiddleware` reads the session cookie and attaches a lazy `request.session`.
2. `AuthenticationMiddleware` sets `request.user` as a `SimpleLazyObject` (and `request.auser()` coroutine since 5.0). Nothing is loaded until accessed.
3. On access, `get_user()` reads the user id and backend path from the session, calls that backend's `get_user(id)`, then verifies `_auth_user_hash` against the current password hash. A mismatch (password changed elsewhere) flushes the session — that's how "log out other sessions on password change" works.
4. Unauthenticated → `AnonymousUser`.

**Sessions.** Data is serialised with `JSONSerializer` (the pickle serializer was removed in 5.0) and stored by `SESSION_ENGINE`. The session is saved at response time only if modified (or `SESSION_SAVE_EVERY_REQUEST = True`).

**Permissions.** `user.has_perm("orders.change_order")` asks each backend; `ModelBackend` checks user and group permissions (cached on the user instance for the request). Superusers pass every check; inactive users fail every check. `ModelBackend` returns **no** object-level permissions — `has_perm(perm, obj)` returns `False` unless another backend implements it.

## Core concepts and APIs

| API | Notes |
|---|---|
| `authenticate`, `login`, `logout` | Async variants `aauthenticate`, `alogin`, `alogout` since 5.0 |
| `get_user_model()` / `settings.AUTH_USER_MODEL` | Use the function at runtime, the setting string in FKs |
| `AbstractUser` / `AbstractBaseUser` + `PermissionsMixin` | Extend vs. build from scratch |
| `BaseUserManager` | `create_user`, `create_superuser`, `normalize_email` |
| `login_required`, `permission_required`, `user_passes_test` | FBV decorators |
| `LoginRequiredMixin`, `PermissionRequiredMixin`, `UserPassesTestMixin` | CBV mixins (put leftmost) |
| `LoginRequiredMiddleware` + `@login_not_required` | Default-deny, added in 5.1 |
| `update_session_auth_hash(request, user)` | Keep current session after password change |
| `PASSWORD_HASHERS`, `AUTH_PASSWORD_VALIDATORS` | Hashing algorithm and policy |
| `Permission`, `Group`, `Meta.permissions` | Authorisation data |
| `request.session` | Dict-like; `set_expiry`, `cycle_key`, `flush` |
| Built-in views: `LoginView`, `LogoutView`, `PasswordChangeView`, `PasswordResetView`, ... | `LogoutView` accepts POST only since 5.0 |

## Implementation examples

**Custom user model — do this at project start.**

```python
# apps/accounts/models.py
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models

class User(AbstractUser):
    email = models.EmailField(unique=True)
    username = None                         # email-only login
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = EmailUserManager()            # subclass of UserManager using email

# config/settings.py
AUTH_USER_MODEL = "accounts.User"
```

Even if you don't need extra fields today, `class User(AbstractUser): pass` keeps the door open. Keep profile-ish data that many features touch in separate models (`Profile`, `BillingAccount`) to avoid a giant user table.

For email login, normalise and compare case-insensitively (store lower-cased, or add a unique constraint on `Lower("email")`); otherwise `Bob@x.com` and `bob@x.com` become two accounts.

**Custom backend.**

```python
# apps/accounts/backends.py
from django.contrib.auth.backends import ModelBackend

class TenantModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, tenant=None, **kwargs):
        if tenant is None:
            return None                          # let another backend try
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user and user.tenant_id == tenant.id:
            return user
        return None

# settings
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.TenantModelBackend",
                           "django.contrib.auth.backends.ModelBackend"]
```

Subclassing `ModelBackend` keeps timing-attack mitigation (it runs the hasher for unknown users) and `user_can_authenticate()` (rejects inactive users). Backend dotted paths are stored in sessions — renaming a backend logs everyone out.

**Object-level authorisation — bad vs good.**

```python
# Bad: authenticated is not authorised; any user can view any order by ID.
@login_required
def order_detail(request, pk):
    order = get_object_or_404(Order, pk=pk)

# Good: scope the lookup to the user; 404 instead of leaking existence.
@login_required
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.for_user(request.user), pk=pk)
```

**Password change keeping the session.**

```python
if form.is_valid():
    user = form.save()
    update_session_auth_hash(request, user)    # otherwise this session is invalidated
```

**Default-deny site (5.1+).**

```python
MIDDLEWARE = [..., "django.contrib.auth.middleware.AuthenticationMiddleware",
              "django.contrib.auth.middleware.LoginRequiredMiddleware", ...]

from django.contrib.auth.decorators import login_not_required

@login_not_required
def healthz(request):
    return HttpResponse("ok")
```

Login views from `django.contrib.auth` are already exempt. On earlier versions, enforce with decorators/mixins or a custom middleware.

**Async view (5.0+).**

```python
async def dashboard(request):
    user = await request.auser()
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    ...
```

Accessing `request.user` attributes in async code triggers a sync DB lookup and raises `SynchronousOnlyOperation`. Async permission helpers (e.g. `ahas_perm`) and async manager methods were added later (5.2 per its release notes) — verify against the installed version.

## Production patterns

- **Session engine choice:**

| Engine | Pros | Cons |
|---|---|---|
| `db` (default) | Durable, simple | DB write on modify; needs `clearsessions` cron |
| `cache` | Fast | Lost on eviction/restart — users logged out |
| `cached_db` | Fast reads, durable | Writes still hit DB |
| `signed_cookies` | No server storage | Size limit (~4 KB), can't revoke server-side, data readable by client |

  `cached_db` with Redis is a common production choice. Schedule `manage.py clearsessions` for `db`/`cached_db`.
- Cookies: `SESSION_COOKIE_SECURE = True`, `SESSION_COOKIE_HTTPONLY = True` (default), `SESSION_COOKIE_SAMESITE = "Lax"` (default), explicit `SESSION_COOKIE_AGE`. Full list in [production-configuration.md](./production-configuration.md).
- Hashers: keep `PBKDF2PasswordHasher` default or put `Argon2PasswordHasher` first (needs `argon2-cffi` — a dependency decision). Django upgrades the stored hash on next login automatically, and raises PBKDF2 iteration counts each release.
- Enable `AUTH_PASSWORD_VALIDATORS` (similarity, minimum length, common, numeric).
- Use groups for roles, `Meta.permissions` for named capabilities (`("refund_order", "Can refund order")`), and check by permission, not group name.
- Log auth events via `user_logged_in`, `user_logged_out`, `user_login_failed` signals — never log passwords or raw credentials (`user_login_failed` already cleanses sensitive keys).

## Decision guidance

| Need | Choose |
|---|---|
| Username/password web login | `ModelBackend` + built-in views |
| Email login | Custom user with `USERNAME_FIELD = "email"` |
| SSO / external identity (LDAP, OIDC) | Additional backend (often a vetted package) before `ModelBackend`; `RemoteUserBackend` only behind a trusted proxy |
| API clients | DRF auth classes — see django-rest-framework |
| Most pages private | `LoginRequiredMiddleware` (≥ 5.1) |
| Few pages private | Decorators / mixins |
| Row-level access | Scoped querysets (preferred) or an object-permission backend |
| `AbstractUser` vs `AbstractBaseUser` | `AbstractUser` unless you must drop `username`/names entirely and accept rewriting managers/admin |

## Security considerations

- Never authenticate by comparing passwords yourself; use `check_password` / `authenticate`.
- `login()` cycles the session key; if you build custom login, call `request.session.cycle_key()` on privilege change.
- Use POST for logout (`LogoutView` requires it since 5.0) to prevent CSRF logouts.
- `RemoteUserMiddleware` trusts a header — safe only if the proxy strips client-supplied values.
- Signed-cookie sessions expose contents to the client (signed, not encrypted) and depend entirely on `SECRET_KEY`; rotating `SECRET_KEY` (use `SECRET_KEY_FALLBACKS`, 4.1+) invalidates sessions and password-reset tokens unless fallbacks are configured.
- Redirect targets after login: built-in views validate `next` with `url_has_allowed_host_and_scheme`; do the same in custom views.
- Rate limiting, lockout, MFA: [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- `request.user` costs one query per request (lazy, only when accessed). Avoid touching it in middleware for anonymous-heavy endpoints.
- `has_perm` loads all permissions once per user instance; checking in loops is fine, but each new user object reloads.
- `db` sessions mean a write when modified; `SESSION_SAVE_EVERY_REQUEST = True` turns every request into a write.
- PBKDF2/Argon2 are slow by design; login endpoints are CPU-heavy. Use `MD5PasswordHasher` **in tests only**.

## Testing

```python
def test_user_cannot_see_other_users_order(client, user, other_users_order):
    client.force_login(user)                       # skips backend + hasher: fast
    response = client.get(reverse("orders:detail", args=[other_users_order.pk]))
    assert response.status_code == 404

def test_login_with_mixed_case_email(client, user_factory):
    user_factory(email="bob@example.com", password="s3cret-pass!")
    assert client.login(username="BOB@example.com", password="s3cret-pass!")
```

- `force_login` for most tests; `client.login` when testing the backend.
- Test permission denial paths (403/404/redirect), not just success.
- Test custom backends by calling `authenticate(request=None, ...)` directly.
- Async: `AsyncClient` with `await client.aforce_login(user)` (async client login helpers added in 5.0).
- See [django-testing](../../django-testing/SKILL.md) for fixtures.

## Common mistakes and anti-patterns

- Starting with `auth.User` then needing a custom user mid-project.
- `ForeignKey(User)` or `from django.contrib.auth.models import User` instead of `settings.AUTH_USER_MODEL` / `get_user_model()`.
- Treating `login_required` as authorisation.
- Checking `user.groups.filter(name="admins").exists()` instead of permissions.
- `PermissionRequiredMixin` placed right of `View` in the MRO — never runs.
- Storing large objects or model instances in the session (JSON serialisation fails or bloats cookies).
- Forgetting `update_session_auth_hash` after password change, logging the user out.
- `cache` session backend with an evicting cache — random logouts.
- GET-based logout links.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Users logged out randomly | Cache eviction (`cache` engine), `SECRET_KEY` rotated, backend path renamed | `cached_db`, `SECRET_KEY_FALLBACKS`, keep backend paths stable |
| Logged out after password change | Missing `update_session_auth_hash` | Call it |
| `request.user` missing | `AuthenticationMiddleware` absent or before `SessionMiddleware` | Fix ordering |
| `SynchronousOnlyOperation` touching user in async view | Lazy sync load | `await request.auser()` (≥ 5.0) |
| `has_perm` False for superuser | User `is_active=False` | Activate or check the flag |
| Session cookie not set over HTTP locally | `SESSION_COOKIE_SECURE=True` in dev | Use env-specific setting |
| `ValueError: Dependency on app with no migrations: accounts` | Custom user app missing initial migration | `makemigrations accounts` before first `migrate` |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/auth/
- https://docs.djangoproject.com/en/stable/topics/auth/default/
- https://docs.djangoproject.com/en/stable/topics/auth/customizing/
- https://docs.djangoproject.com/en/stable/topics/auth/passwords/
- https://docs.djangoproject.com/en/stable/topics/http/sessions/
- https://docs.djangoproject.com/en/stable/ref/contrib/auth/
- https://docs.djangoproject.com/en/stable/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware
