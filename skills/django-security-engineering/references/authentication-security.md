# Authentication Security

> Scope: Django's authentication architecture from a security perspective — backend chain,
> `authenticate()`/`login()`, password hashing and upgrades, password validators and NIST
> alignment, custom backend security, account enumeration, password reset tokens, brute-force
> and credential-stuffing defences, MFA and step-up authentication, `LoginRequiredMiddleware`,
> logout, SSO/OIDC considerations and async authentication.
> Not covered here: session storage, rotation and cookie flags
> ([session-and-cookie-security.md](./session-and-cookie-security.md)); what an authenticated user
> may do ([authorization-and-access-control.md](./authorization-and-access-control.md)); API
> tokens and JWT ([api-security.md](./api-security.md)); signing and key management
> ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); auth event logging
> ([security-logging-and-auditing.md](./security-logging-and-auditing.md)); framework mechanics of
> users, backends and `AUTH_USER_MODEL`
> ([django-core authentication-sessions](../../django-core/references/authentication-sessions.md)).

## 1. Purpose and scope

Authentication answers one question: *is this request made by the principal it claims to be?*
Django ships a solid core — salted, iterated password hashing, timing-equalised login, signed
reset tokens, session rotation on login — but leaves the hardest operational problems to the
application: rate limiting, account lockout policy, MFA, breached-password screening, enumeration
resistance in custom flows, and safe SSO integration. This reference separates the two and gives
patterns for the parts the developer owns.

Code targets Django 5.2 LTS; version-specific behaviour is qualified. Examples were not executed.

## 2. Core concepts

| Concept | Meaning in Django |
|---|---|
| Credential | Something presented to prove identity: password, OTP, WebAuthn assertion, IdP assertion, token |
| Backend | Class with `authenticate(request, **credentials)` and `get_user(user_id)`; listed in `AUTHENTICATION_BACKENDS` |
| Authentication | `django.contrib.auth.authenticate()` tries each backend in order and returns a user or `None` |
| Login | `django.contrib.auth.login()` binds the user to the session (rotates the session key, stores user id, backend path and a session auth hash) |
| Password hasher | Class in `PASSWORD_HASHERS`; the first entry hashes new passwords, all entries can verify |
| Password validator | Class in `AUTH_PASSWORD_VALIDATORS`; run by `validate_password()` — only where your code or Django's forms call it |
| Assurance level | NIST SP 800-63B AAL1 (single factor), AAL2 (two factors), AAL3 (hardware-bound, phishing-resistant) |
| Enumeration | Learning whether an account exists from responses, timing or side channels |
| Step-up | Requiring fresh or stronger authentication before a sensitive operation |

**Framework provides:** hashing and verification, hash upgrade on login, constant-time hash
comparison, a dummy hash for unknown users in `ModelBackend`, signed time-limited reset tokens,
session key rotation on login, session invalidation on password change (via the session auth
hash), POST-only logout (Django ≥ 5.0), `LoginRequiredMiddleware` (Django ≥ 5.1).

**Developer owns:** rate limiting and lockout, MFA, breached-password checks, validator use in
custom forms/APIs, enumeration-safe custom flows, secure email/link generation, SSO protocol
validation, audit logging, and recovery processes.

## 3. Internal mechanics

### Backend chain

`authenticate(request, **credentials)` iterates `AUTHENTICATION_BACKENDS`. For each backend it
inspects the signature; a backend whose signature does not accept the credentials is skipped. A
backend returns a user, returns `None` (try the next), or raises `PermissionDenied` (stop
immediately and fail). On failure of all backends the `user_login_failed` signal fires with
sanitised credentials (keys matching `api|token|key|secret|password|signature` are masked by
`cleanse_setting` logic — VERIFY exact masking for custom keys). On success the backend path is
annotated on the user (`user.backend`).

`login(request, user)` then:

1. Cycles the session key (`request.session.cycle_key()`) if the session already belonged to
   another user or was anonymous — prevents session fixation; flushes if a different user was
   logged in.
2. Stores `_auth_user_id`, `_auth_user_backend`, `_auth_user_hash` (HMAC of the password hash).
3. Rotates the CSRF token (`rotate_token`).
4. Sends `user_logged_in` (which by default updates `last_login`).

On each request `AuthenticationMiddleware` lazily loads the user via the stored backend's
`get_user()` and verifies `_auth_user_hash` against `user.get_session_auth_hash()`; a mismatch
(password changed elsewhere) flushes the session. `SECRET_KEY_FALLBACKS` (Django ≥ 4.1) is
consulted so key rotation does not log everyone out.

### `ModelBackend` hardening already present

- **Timing equalisation:** when the username does not exist, `ModelBackend.authenticate` runs the
  default hasher on the supplied password (`UserModel().set_password(password)`) so the response
  time is similar to a wrong-password case.
- **Inactive users:** `user_can_authenticate()` rejects `is_active=False`. `AllowAllUsersModelBackend`
  removes this check — use it only if you deliberately show "account disabled".
- **Constant-time comparison:** hashers compare with `hmac.compare_digest` via `constant_time_compare`.

### Hashing and upgrade

A stored password is `algorithm$iterations$salt$hash`. `check_password(raw, encoded, setter)`
identifies the hasher by prefix, verifies, and if the hasher is not the preferred one or its work
factor is outdated (`must_update`) calls `setter` to re-hash with the current preferred hasher.
`User.check_password()` passes such a setter, so hashes upgrade transparently on successful login.
PBKDF2 iteration defaults increase each feature release (1,000,000 in Django 5.2 — VERIFY against
the 5.2 release notes); dormant accounts keep old work factors until they log in.

### Password reset tokens

`PasswordResetTokenGenerator.make_token(user)` produces `<base36 timestamp>-<HMAC>` where the HMAC
(keyed by `SECRET_KEY`, verified also against `SECRET_KEY_FALLBACKS`) covers the user's pk,
password hash, `last_login` timestamp, the token timestamp and the user's email field value
(email inclusion since Django 3.1 — VERIFY). Consequences: a token becomes invalid once the
password changes, the user logs in again, or the email changes; it expires after
`PASSWORD_RESET_TIMEOUT` seconds (default 259200 = 3 days). There is no server-side token store,
so you cannot revoke a single token except by changing one of those inputs or rotating the key.

`PasswordResetView` → `PasswordResetForm.get_users(email)` returns only active users with a usable
password and a case-insensitive email match; the view shows the same "done" page whether or not
an email was sent. `PasswordResetConfirmView` moves the token from the URL into the session and
redirects to a token-free URL (`set-password`) so the token does not leak via `Referer`.

### Access gating

`LoginRequiredMiddleware` (Django ≥ 5.1) redirects unauthenticated requests to `LOGIN_URL` for every
view not decorated with `login_not_required`; Django's `LoginView` and related auth views are
already marked. It applies to Django views only — DRF views authenticate in `APIView.initial`,
so API authentication must be configured in DRF ([api-security.md](./api-security.md)). VERIFY
how the middleware treats DRF views in your setup before relying on it for APIs.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Hasher | PBKDF2 (built-in default), Argon2id (`Argon2PasswordHasher`, requires third-party `argon2-cffi`), scrypt (`ScryptPasswordHasher`, built-in, Django ≥ 4.0), bcrypt (third-party `bcrypt`) | Argon2id if you can add the dependency (OWASP preferred); PBKDF2 at Django's defaults is acceptable. Keep old hashers listed after the preferred one so upgrades happen. |
| Password policy | Complexity rules vs length + blocklist | Follow NIST 800-63B: minimum length (≥ 8; current NIST revision raises single-factor minimum to 15 — VERIFY), allow long passphrases (≥ 64 chars), no composition rules, no periodic forced rotation, screen against breached/common lists. |
| Breached-password check | `CommonPasswordValidator` (built-in, ~20k list) vs HIBP k-anonymity API (custom/third-party) | Add an online check for high-value apps; fail open or closed deliberately and time-box the call. |
| Throttling | Per IP, per account, per (account, IP), global | Per-account limits stop targeted guessing but enable lockout DoS; per-IP limits are defeated by botnets. Combine: progressive delay per account, hard limit per IP, CAPTCHA/MFA challenge as escalation rather than hard lockout. |
| Brute-force tooling | django-axes (third-party), DRF throttles, edge/WAF | django-axes for HTML login; DRF `ScopedRateThrottle` on API auth endpoints; edge rate limits for volume. |
| MFA | TOTP (django-otp, django-allauth), WebAuthn/passkeys (django-allauth MFA, py_webauthn, django-otp-webauthn — third-party), SMS | Mandatory for staff/superusers; offer to all users; prefer WebAuthn (phishing-resistant); avoid SMS for high-value accounts (SIM swap). |
| Session vs token | Session cookie (browser), token (non-browser) | See [session-and-cookie-security.md](./session-and-cookie-security.md) and [api-security.md](./api-security.md). |
| Enterprise identity | Local passwords vs OIDC/SAML SSO | SSO centralises MFA and offboarding; keep a break-glass local admin with MFA. |
| Reveal "account locked/disabled"? | Generic vs specific message | Generic by default; specific only through the out-of-band channel (email to the owner). |

## 5. Django implementation patterns

### Settings baseline

```python
# settings/base.py
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",   # needs argon2-cffi (third-party)
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "accounts.validators.BreachedPasswordValidator"},  # custom, below
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",  # Django >= 5.1: default-deny
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

PASSWORD_RESET_TIMEOUT = 60 * 60  # 1 hour; default is 3 days
LOGIN_URL = "accounts:login"
```

```python
# settings/test.py — fast hashing only in tests
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
```

### Validators must be called explicitly in custom flows

`UserCreationForm`, `SetPasswordForm` and `PasswordChangeForm` call `validate_password`. Custom
signup forms, DRF serializers and management commands do not unless you call it.

```python
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128,
                                     style={"input_type": "password"})

    def validate(self, attrs):
        candidate = User(email=attrs["email"])  # for UserAttributeSimilarityValidator
        validate_password(attrs["password"], user=candidate)  # raises django ValidationError
        return attrs
```

DRF converts Django's `ValidationError` raised inside `validate()` — wrap it as
`serializers.ValidationError({"password": list(e.messages)})` to keep a field-level error shape.

### Breached-password validator (custom, HIBP k-anonymity)

```python
# accounts/validators.py
import hashlib

import httpx  # third-party HTTP client; any client with timeouts works
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class BreachedPasswordValidator:
    """Reject passwords in the Have I Been Pwned corpus. Sends only a 5-char SHA-1 prefix."""

    def __init__(self, timeout=2.0, fail_closed=False):
        self.timeout = timeout
        self.fail_closed = fail_closed

    def validate(self, password, user=None):
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 (API format)
        prefix, suffix = digest[:5], digest[5:]
        try:
            resp = httpx.get(f"https://api.pwnedpasswords.com/range/{prefix}",
                             headers={"Add-Padding": "true"}, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.HTTPError:
            if self.fail_closed:
                raise ValidationError(_("Password could not be checked. Try again."),
                                      code="password_check_unavailable")
            return
        for line in resp.text.splitlines():
            hash_suffix, _, count = line.partition(":")
            if hash_suffix == suffix and int(count) > 0:
                raise ValidationError(_("This password has appeared in a data breach."),
                                      code="password_breached")

    def get_help_text(self):
        return _("Your password must not appear in known data breaches.")
```

### Secure custom backend

```python
# accounts/backends.py
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    """Authenticate by case-insensitive email, preserving ModelBackend's protections."""

    def authenticate(self, request, email=None, password=None, **kwargs):
        if email is None or password is None:
            return None
        UserModel = get_user_model()
        try:
            user = UserModel._default_manager.get(email__iexact=email.strip())
        except UserModel.DoesNotExist:
            UserModel().set_password(password)  # equalise timing with the found-user path
            return None
        except UserModel.MultipleObjectsReturned:
            return None  # enforce a unique, case-insensitive constraint instead (django-orm)
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
```

Rules for any custom backend (LDAP, OTP, magic link, IdP):

- Return `None` for "not mine / not valid"; raise `PermissionDenied` only to stop the chain.
- Compare secrets with `django.utils.crypto.constant_time_compare`; store only hashes of tokens.
- Keep `user_can_authenticate()` (is_active) unless explicitly designed otherwise.
- Never log credentials; `user_login_failed` receives masked credentials — keep custom keys named
  so they are masked, and never log `request.POST`.
- Back the identifier with a DB uniqueness constraint (e.g. `UniqueConstraint(Lower("email"))`).

### Enumeration-safe flows

- **Login:** one message for unknown user, wrong password and inactive account
  ("Please enter a correct email and password"). `AuthenticationForm` already does this unless
  you use `AllowAllUsersModelBackend`, which surfaces "This account is inactive."
- **Password reset:** keep `PasswordResetView`'s identical response. Send email asynchronously
  (task queue) so the response time does not reveal whether a user exists.
- **Signup:** "If this address is available you'll receive an email" + send either a verification
  link or a "you already have an account" email. Do not return "email already registered".
- **APIs:** same status code and body; also check `/users/?email=` filters and username
  availability endpoints, which leak by design — rate-limit and require auth.

### Reset email link poisoning

`PasswordResetForm` builds the link domain from `request.get_host()` (or `domain_override` /
the Sites framework). With a permissive `ALLOWED_HOSTS` an attacker can send reset emails
containing their own domain. Keep `ALLOWED_HOSTS` explicit
([secure-django-configuration.md](./secure-django-configuration.md)) and pin the domain:

```python
from django.contrib.auth import views as auth_views

path("password-reset/", auth_views.PasswordResetView.as_view(
    domain_override="app.example.com",      # never derived from the Host header
    extra_email_context={"protocol": "https"},
    html_email_template_name="registration/password_reset_email.html",
), name="password_reset"),
```

### Brute-force protection

**django-axes (third-party):** tracks failures via `user_login_failed` and an authentication backend.

```python
INSTALLED_APPS += ["axes"]
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",     # must be first
    "accounts.backends.EmailBackend",
]
MIDDLEWARE += ["axes.middleware.AxesMiddleware"]
AXES_FAILURE_LIMIT = 10
AXES_COOLOFF_TIME = 1                         # hours (timedelta/callable also accepted)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]  # django-axes >= 6 — VERIFY syntax
AXES_RESET_ON_SUCCESS = True
# Behind a proxy, configure client-IP resolution (AXES_IPWARE_* settings) — VERIFY for your version
```

Locking by `(username, ip)` avoids letting anyone lock out a victim from anywhere, at the cost of
weaker protection against distributed guessing; add a separate per-username soft limit that
triggers CAPTCHA or MFA instead of lockout.

**DRF API login:** scoped throttles ([api-security.md](./api-security.md)).

```python
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"   # REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"] = "5/min"
```

Throttles key on client IP as derived from `REMOTE_ADDR`/`X-Forwarded-For` with `NUM_PROXIES` —
misconfigured proxies make every request look like the proxy (global lockout) or let clients
spoof IPs.

### MFA and step-up

```python
# accounts/decorators.py — step-up: require a recent strong authentication
import time
from functools import wraps

from django.shortcuts import redirect
from django.urls import reverse

STEP_UP_MAX_AGE = 300  # seconds


def mark_strong_auth(request):
    """Call after successful MFA verification."""
    request.session["strong_auth_at"] = int(time.time())


def require_recent_mfa(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        at = request.session.get("strong_auth_at", 0)
        if time.time() - at > STEP_UP_MAX_AGE:
            return redirect(f"{reverse('accounts:mfa-verify')}?next={request.get_full_path()}")
        return view(request, *args, **kwargs)
    return wrapper
```

Apply step-up to: changing email/password/MFA settings, viewing recovery codes, API key creation,
payouts, data exports, role changes. Validate `next` with `url_has_allowed_host_and_scheme`
([ssrf-prevention.md](./ssrf-prevention.md)).

MFA engineering rules:

- Library choice: django-otp (TOTP, static recovery tokens, plugin devices), django-allauth MFA
  (TOTP, recovery codes, WebAuthn), py_webauthn / dedicated passkey packages — all third-party.
- Admin: wrap the admin site with an OTP-requiring site (e.g. django-otp's `OTPAdminSite`) or a
  middleware that requires verified MFA for `is_staff` users.
- TOTP secrets are secrets: encrypt at rest ([secrets-and-cryptography.md](./secrets-and-cryptography.md)),
  enforce one-time use of each code within its window, throttle verification attempts.
- Recovery codes: generate with `secrets`, store hashed, single use, regenerate invalidates old.
- MFA reset by support is the weakest link — require identity verification and audit it.
- Rotate the session key after MFA succeeds (`request.session.cycle_key()`).

### Password change and session invalidation

```python
from django.contrib.auth import update_session_auth_hash


def change_password(request):
    form = PasswordChangeForm(user=request.user, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # keep this session; others are invalidated
        audit("password_changed", actor=user, request=request)
        notify_password_changed.delay(user.pk)     # out-of-band notice to the owner
        return redirect("accounts:security")
    return render(request, "accounts/password_change.html", {"form": form})
```

### Logout

Django ≥ 5.0 `LogoutView` accepts POST only (GET logout was deprecated in 4.1 and removed in
5.0), which prevents CSRF-triggered logout via `<img src=/logout/>`. Use a form with
`{% csrf_token %}`. `logout()` flushes the session; for "log out everywhere", delete other
sessions or bump a per-user session version ([session-and-cookie-security.md](./session-and-cookie-security.md)).

### `LoginRequiredMiddleware` (Django ≥ 5.1)

```python
from django.contrib.auth.decorators import login_not_required


@login_not_required
def healthz(request):
    return HttpResponse("ok")
```

Class-based views: decorate with `method_decorator(login_not_required, name="dispatch")` or set it
on the URL pattern's view (VERIFY recommended form for CBVs in 5.1+ docs). Audit the list of
`login_not_required` views in review.

### SSO / OIDC

Use a maintained library (mozilla-django-oidc, django-allauth, social-auth-app-django, authlib —
third-party) rather than hand-rolling. Verify: `state` (CSRF on the callback), `nonce` (replay),
PKCE for public clients, ID token signature, `iss`, `aud`, `exp`; exact redirect URI registration.
Map identities by the IdP's stable `sub` (+ `iss`), never by email alone unless the IdP asserts
`email_verified` and you trust it — otherwise account takeover by registering the victim's email at
a weaker IdP. Provisioning, deprovisioning and IdP integration details: [django-integrations](../../django-integrations/SKILL.md).

### Async authentication (Django ≥ 5.0)

`aauthenticate()`, `alogin()`, `alogout()` and `request.auser()` are the async counterparts.
Custom backends can implement `aauthenticate`; otherwise the sync method is wrapped with
`sync_to_async`. Async permission helpers on the user/backends (`ahas_perm`, etc.) were added in
Django 5.2 — VERIFY before use.

## 6. Production use cases

- **B2C web app:** Argon2, 12-char minimum + breached check, django-axes `(username, ip)`, edge
  rate limits, optional TOTP/passkeys, async reset emails, new-device login notifications.
- **Internal admin/back office:** SSO via OIDC with IdP-enforced MFA; local break-glass superuser
  with hardware key; `LoginRequiredMiddleware`; admin behind VPN or IP allow-list.
- **API for mobile:** password exchanged once for a scoped, hashed, revocable token; login endpoint
  throttled; refresh flows designed per [api-security.md](./api-security.md).
- **Migration from a legacy system:** wrap legacy hashes in a custom hasher (e.g. PBKDF2-wrapped
  SHA1 as Django's docs describe) and upgrade on next login; force reset for never-returning users.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Built-in `ModelBackend` + auth views | Audited, timing-equalised, upgradeable hashes, enumeration-safe reset | No rate limiting, no MFA, generic UX |
| Argon2id | Memory-hard; OWASP-preferred | Extra C dependency; memory cost per login under load |
| django-axes | Drop-in lockout, admin visibility | Lockout DoS if keyed on username only; DB/cache writes per failure |
| TOTP | Cheap, offline | Phishable; secret storage; clock drift |
| WebAuthn/passkeys | Phishing-resistant, no shared secret | More complex UX, recovery and library choice |
| SSO | Central MFA and offboarding | IdP becomes critical dependency; account-linking pitfalls |

## 8. Security and performance considerations

- Hashing is intentionally slow (tens to hundreds of ms). A login endpoint without rate limiting is
  a CPU-exhaustion vector; throttle before hashing where possible (edge, or check throttle first).
- Do not hash passwords in bulk on the request path (imports: background job; tests: MD5 hasher).
- Raising iterations in a release increases login latency; capacity-test after upgrades.
- Enumeration also leaks through timing of email sending, different redirects, and HTTP status
  codes — test the whole response, not just the message.
- Lockout counters stored in the database add a write per failed login; use cache-backed stores for
  high volume, but understand cache eviction resets counters.
- Never store passwords reversibly, in logs, analytics, or error trackers — configure
  `sensitive_post_parameters` on custom auth views (Django's auth views already use it).
- Password length upper bound (e.g. 128–4096 chars) prevents hashing DoS with huge inputs;
  Django's forms do not impose one by default on password fields — VERIFY.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| Custom signup/API serializer without `validate_password` | Weak passwords accepted | Call `validate_password(password, user)` |
| "No account with that email" on reset or signup | Account enumeration | Identical responses; out-of-band notification |
| Custom backend that returns early for unknown users | Timing-based enumeration | Run the hasher on the miss path |
| `user.password == request.POST["password"]` or `==` on tokens | Plaintext storage / timing leaks | `check_password`, `constant_time_compare`, store hashes |
| Using `AllowAllUsersModelBackend` without UI need | Disabled accounts reveal status | Default `ModelBackend` |
| Lockout keyed only on username | Attacker locks out any user | Key on (username, ip) + soft challenge |
| Throttles behind a proxy without `NUM_PROXIES`/IP config | Global lockout or bypass | Configure client IP resolution |
| MFA only on login page, not on admin or API | MFA bypass via other entry point | Enforce MFA at every interactive entry point |
| Recovery codes stored plaintext | Second factor leaks with DB | Hash recovery codes |
| GET logout link (pre-5.0 habits) / custom GET logout view | CSRF logout | POST form with CSRF token |
| Linking SSO accounts by unverified email | Account takeover | Link by `(iss, sub)`; require `email_verified` |
| `PASSWORD_RESET_TIMEOUT` left at 3 days for high-value apps | Long-lived reset links in mailboxes | Shorten to minutes–hours |
| Reset link domain from Host header with loose `ALLOWED_HOSTS` | Reset-link poisoning | Explicit hosts; `domain_override` |

## 10. Troubleshooting and validation

- **Users logged out after deploy:** `SECRET_KEY` rotated without `SECRET_KEY_FALLBACKS`, or a
  custom user model changed `get_session_auth_hash` inputs.
- **Hashes not upgrading:** login path bypasses `User.check_password` (custom backend calling
  `hashers.check_password` without `setter`).
- **Everyone locked out at once:** throttle/axes sees the proxy IP — inspect `REMOTE_ADDR` and
  forwarded headers in a debug log line.
- **Reset tokens "invalid" immediately:** user logged in (changing `last_login`) between issue and
  use, or email changed; or email clients prefetching the link — `PasswordResetConfirmView`'s
  session redirect handles prefetch for the GET; confirm the set-password step is POST.
- **`authenticate()` returns `None` with correct credentials:** backend signature does not accept
  the passed keyword (`username` vs `email`), user inactive, or an earlier backend raised
  `PermissionDenied`.

Validation tests (see [security-testing-and-incident-response.md](./security-testing-and-incident-response.md)):

```python
from django.test import TestCase, override_settings
from django.urls import reverse


class AuthSecurityTests(TestCase):
    def test_login_error_identical_for_unknown_and_wrong_password(self):
        UserFactory(email="a@example.com", password="correct-horse-battery")
        r1 = self.client.post(reverse("accounts:login"), {"username": "a@example.com", "password": "nope"})
        r2 = self.client.post(reverse("accounts:login"), {"username": "x@example.com", "password": "nope"})
        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.context["form"].non_field_errors(), r2.context["form"].non_field_errors())

    def test_password_reset_same_response_for_unknown_email(self):
        r = self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})
        self.assertRedirects(r, reverse("password_reset_done"))

    def test_logout_rejects_get(self):
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_session_invalidated_after_password_change_elsewhere(self):
        user = UserFactory()
        self.client.force_login(user)
        user.set_password("new-long-passphrase-123")
        user.save()
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
```

Also: `check --deploy`, a test that every URL without `login_not_required` redirects anonymous
users, and a test that the login endpoint returns 429 after N failures.

## 11. Related references and skills

- [session-and-cookie-security.md](./session-and-cookie-security.md) — session fixation, expiry, cookie flags.
- [authorization-and-access-control.md](./authorization-and-access-control.md) — what users may do after login.
- [api-security.md](./api-security.md) — tokens, JWT, API throttling.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — `SECRET_KEY` rotation, token hashing, TOTP secret encryption.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — login success/failure auditing.
- [csrf-protection.md](./csrf-protection.md) — login CSRF.
- [django-core authentication-sessions](../../django-core/references/authentication-sessions.md) — user model, backend and session mechanics.
- [django-rest-framework authentication](../../django-rest-framework/references/authentication.md) — DRF auth classes.
- [django-integrations](../../django-integrations/SKILL.md) — identity providers, email delivery.
- [django-testing](../../django-testing/SKILL.md) — factories and test layout.

## 12. Official documentation and external references

- Django authentication system: https://docs.djangoproject.com/en/5.2/topics/auth/
- Customising authentication: https://docs.djangoproject.com/en/5.2/topics/auth/customizing/
- Password management: https://docs.djangoproject.com/en/5.2/topics/auth/passwords/
- `django.contrib.auth` reference (signals, middleware, views): https://docs.djangoproject.com/en/5.2/ref/contrib/auth/
- `LoginRequiredMiddleware`: https://docs.djangoproject.com/en/5.2/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware
- Django security overview: https://docs.djangoproject.com/en/5.2/topics/security/
- Django 5.0 / 5.1 / 5.2 release notes: https://docs.djangoproject.com/en/5.2/releases/
- OWASP Authentication Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- OWASP Password Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP Forgot Password Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
- OWASP Multifactor Authentication Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html
- OWASP Credential Stuffing Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Credential_Stuffing_Prevention_Cheat_Sheet.html
- OWASP ASVS V2 (Authentication): https://owasp.org/www-project-application-security-verification-standard/
- NIST SP 800-63B: https://pages.nist.gov/800-63-4/sp800-63b.html
- Have I Been Pwned range API: https://haveibeenpwned.com/API/v3#PwnedPasswords
- django-axes: https://django-axes.readthedocs.io/ · django-otp: https://django-otp-official.readthedocs.io/ · django-allauth MFA: https://docs.allauth.org/
