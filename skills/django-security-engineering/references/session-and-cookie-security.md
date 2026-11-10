# Session and Cookie Security

> Scope: session engines and their security properties, session fixation and key rotation,
> session invalidation (logout, password change, "log out other sessions"), idle and absolute
> expiry, session serialisation, what may be stored in a session, cookie attributes (`Secure`,
> `HttpOnly`, `SameSite`, `Domain`, `Path`, `__Host-`/`__Secure-` prefixes), and signed cookies.
> Not covered here: login flow, passwords and MFA ([authentication-security.md](./authentication-security.md));
> CSRF cookie behaviour ([csrf-protection.md](./csrf-protection.md)); API tokens and JWT
> ([api-security.md](./api-security.md)); signing and key rotation internals
> ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); session framework mechanics
> ([django-core authentication-sessions.md](../../django-core/references/authentication-sessions.md));
> cache backend configuration ([django-core caching.md](../../django-core/references/caching.md)).

## 1. Purpose and scope

For browser applications the session cookie *is* the credential after login: whoever presents
it is the user. Session security therefore means (a) the identifier cannot be predicted, fixed,
stolen or replayed beyond its intended life, (b) it is invalidated whenever trust changes, and
(c) the cookie carrying it is scoped as narrowly as the browser allows. Django provides secure
identifier generation, fixation protection in `login()`, and auth-hash invalidation; lifetime
policy, cookie scope, server-side revocation and what goes into the session are developer
decisions.

Baseline: Django 5.2 LTS. Examples were not executed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Session key | Random 32-char identifier (from `get_random_string`, CSPRNG) stored in the cookie; data lives server-side (except `signed_cookies`) |
| Session fixation | Attacker plants a known session id before login and reuses it after the victim authenticates |
| Session hijacking | Attacker obtains a valid id (XSS, network, logs, malware) and replays it |
| Session auth hash | HMAC of the user's password hash stored as `_auth_user_hash`; mismatch logs the session out |
| Idle timeout | Session ends after N minutes of inactivity |
| Absolute timeout | Session ends N hours after login regardless of activity |
| Cookie scope | `Domain`/`Path` determine which hosts and paths receive the cookie |
| `SameSite` | Browser rule for sending cookies on cross-site requests (`Strict`, `Lax`, `None`) |

## 3. Internal mechanics

- **Engines** (`SESSION_ENGINE`):
  - `db` (default): row in `django_session`; revocable; needs `clearsessions` for cleanup.
  - `cache`: fast, but data is lost on eviction/restart and is only as private as the cache
    (shared Redis/memcached with no auth means anyone on the network can read or forge sessions).
  - `cached_db`: write-through to DB, read from cache; revocable and durable.
  - `signed_cookies`: data serialised, compressed optionally and signed with `SECRET_KEY` (salt
    `django.contrib.sessions.backends.signed_cookies`); **not encrypted** — the client can read
    it; no server-side revocation; a stolen cookie stays valid until expiry; size limited (~4 KB);
    a `SECRET_KEY` leak allows forging arbitrary session contents.
- **Serialisation**: `SESSION_SERIALIZER` defaults to `JSONSerializer`. `PickleSerializer` was
  deprecated in Django 4.1 and removed in 5.0 — it turned a `SECRET_KEY` leak (with
  `signed_cookies`) into remote code execution.
- **`login(request, user)`**: if the session already belongs to a different user, or has no
  auth hash, it calls `flush()`; otherwise `cycle_key()` — new key, same data — preventing
  fixation. It also stores `_auth_user_id`, `_auth_user_backend`, `_auth_user_hash` and calls
  `rotate_token()` for CSRF. `alogin()` is the async variant (Django ≥ 5.0).
- **`get_user()`** (used by `AuthenticationMiddleware`) recomputes the session auth hash and
  compares it with `constant_time_compare`; on mismatch it flushes the session. Changing the
  password therefore invalidates every other session. `SECRET_KEY_FALLBACKS` are consulted so
  rotation does not log everyone out (`VERIFY`: fallback verification for the session auth hash
  was added in a 4.1.x patch release).
- **`update_session_auth_hash(request, user)`** re-stamps the current session after a password
  change so the user who changed it stays logged in. `PasswordChangeView` calls it.
- **`logout(request)`** sends `user_logged_out`, calls `request.session.flush()` (deletes the
  server-side record, new empty key) and resets `request.user` to `AnonymousUser`.
- **Cookie emission**: `SessionMiddleware.process_response` sets the cookie when the session was
  modified (or always, with `SESSION_SAVE_EVERY_REQUEST`), applying `SESSION_COOKIE_*` settings.
  `Vary: Cookie` is patched when the session was accessed.
- **Expiry**: `SESSION_COOKIE_AGE` (default 1 209 600 s = 2 weeks) sets both cookie max-age and
  server-side expiry date; `SESSION_EXPIRE_AT_BROWSER_CLOSE` makes it a browser-session cookie
  (browsers restoring tabs may keep it). `request.session.set_expiry()` overrides per session.
  Without `SESSION_SAVE_EVERY_REQUEST`, expiry is **absolute from last modification**, not idle.
- **Signed cookies (generic)**: `response.set_signed_cookie(key, value, salt=..., max_age=...)`
  and `request.get_signed_cookie(key, salt=..., max_age=...)` use `TimestampSigner`; integrity
  only, not confidentiality.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Engine | `db` / `cached_db` / `cache` / `signed_cookies` | `cached_db` for most production apps; `db` if no shared cache; avoid `signed_cookies` when revocation or confidentiality matters |
| Lifetime | Long (remember me) vs short | Short absolute lifetime for privileged users; "remember me" as an explicit, longer per-session expiry |
| Idle timeout | `SESSION_SAVE_EVERY_REQUEST` vs timestamp in session | Timestamp middleware avoids a DB write per request (write only every N minutes) |
| `SameSite` | `Lax` (default) / `Strict` / `None` | `Lax` for most apps; `Strict` for admin/back-office (breaks inbound links landing logged-in); `None` only for genuine cross-site embedding, requires `Secure` |
| Cookie domain | Host-only (default `None`) vs `.example.com` | Host-only unless subdomains must share login; parent-domain cookies are exposed to every subdomain |
| Name prefix | `sessionid` vs `__Host-sessionid` | `__Host-` when host-only, `Secure`, `Path=/` — browsers then reject subdomain/HTTP overwrites |

## 5. Django implementation patterns

### Baseline settings

```python
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "sessions"          # dedicated, authenticated cache; not shared with app data
SESSION_COOKIE_NAME = "__Host-sessionid"  # requires Secure, no Domain, Path=/
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True            # default True; never disable
SESSION_COOKIE_SAMESITE = "Lax"           # default "Lax"
SESSION_COOKIE_DOMAIN = None              # host-only
SESSION_COOKIE_PATH = "/"
SESSION_COOKIE_AGE = 60 * 60 * 12         # 12h absolute ceiling
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SERIALIZER = "django.contrib.sessions.serializers.JSONSerializer"
```

Changing `SESSION_COOKIE_NAME` logs everyone out once — schedule it.

### Idle and absolute timeouts

```python
# accounts/middleware.py
import time

from django.conf import settings
from django.contrib.auth import logout

IDLE_SECONDS = getattr(settings, "SESSION_IDLE_TIMEOUT", 30 * 60)
ABSOLUTE_SECONDS = getattr(settings, "SESSION_ABSOLUTE_TIMEOUT", 12 * 60 * 60)
TOUCH_INTERVAL = 60  # limit writes to once a minute


class SessionTimeoutMiddleware:
    """Place after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = int(time.time())
            session = request.session
            started = session.setdefault("auth_started_at", now)
            last_seen = session.get("last_seen_at", now)
            if now - last_seen > IDLE_SECONDS or now - started > ABSOLUTE_SECONDS:
                logout(request)
            elif now - last_seen >= TOUCH_INTERVAL:
                session["last_seen_at"] = now
        return self.get_response(request)
```

Set `auth_started_at` in a `user_logged_in` receiver so it is fresh after `cycle_key()`.

### Log out all other sessions

Built-in, via the auth hash: changing the password invalidates all sessions; the current one
survives through `update_session_auth_hash`. For a "sign out everywhere" button without a
password change, track session keys per user (db/cached_db engines):

```python
# accounts/models.py
from django.conf import settings
from django.db import models


class UserSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="tracked_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

```python
# accounts/services.py
from importlib import import_module

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import UserSession

SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


@receiver(user_logged_in)
def track_session(sender, request, user, **kwargs):
    UserSession.objects.get_or_create(user=user, session_key=request.session.session_key)


def revoke_other_sessions(*, user, keep_session_key: str | None) -> int:
    revoked = 0
    for tracked in UserSession.objects.filter(user=user).exclude(session_key=keep_session_key):
        SessionStore(session_key=tracked.session_key).delete()
        tracked.delete()
        revoked += 1
    return revoked
```

The session key may be `None` before the first save; ensure `request.session.save()` has run
(`login()` cycles the key, so read it after `login()`). Not possible with `signed_cookies`.

### Session contents

```python
# Good: identifiers and small flags
request.session["checkout_cart_id"] = cart.pk
request.session["mfa_verified_at"] = int(time.time())

# Bad: secrets, PII, authorisation decisions the client could influence, large blobs
request.session["card_number"] = ...           # never
request.session["is_admin"] = True             # derive from the user on each request
```

Authorisation flags cached in a session go stale when roles change; re-derive or invalidate.

### Signed cookies for non-session state

```python
response.set_signed_cookie(
    "__Host-ui_prefs", "compact", salt="ui-prefs", max_age=60 * 60 * 24 * 30,
    secure=True, httponly=True, samesite="Lax",
)
value = request.get_signed_cookie("__Host-ui_prefs", default=None, salt="ui-prefs",
                                  max_age=60 * 60 * 24 * 30)
```

Always use a distinct `salt` per purpose and a `max_age`; signed is not encrypted.

## 6. Production use cases

- **Back-office app**: `SameSite=Strict`, 8h absolute + 15 min idle timeout, re-authentication
  (`mfa_verified_at` freshness check) before sensitive actions.
- **Consumer app with "remember me"**: default short session; on opt-in call
  `request.session.set_expiry(60 * 60 * 24 * 30)` after `login()`.
- **SSO across subdomains** (`app.` and `account.`): prefer a central IdP (OIDC) over a
  parent-domain session cookie; if a shared cookie is unavoidable, every subdomain becomes part
  of the trust boundary — no user-content subdomains.
- **Incident response**: mass logout by deleting `django_session` rows (and flushing the session
  cache) or rotating `SECRET_KEY` without fallbacks — see
  [security-testing-and-incident-response.md](./security-testing-and-incident-response.md).

## 7. Advantages and disadvantages

| Engine / choice | Advantages | Disadvantages |
|---|---|---|
| `db` | Durable, revocable, auditable | DB read per request; table growth without `clearsessions` |
| `cached_db` | Fast reads, durable, revocable | Cache must be secured; write still hits DB |
| `cache` | Fastest | Sessions vanish on eviction; cache compromise = session compromise |
| `signed_cookies` | No server state | No revocation, readable by client, size limits, key-leak forging |
| `SameSite=Strict` | Strongest cross-site protection | Logged-out appearance when arriving from external links |

## 8. Security and performance considerations

- `HttpOnly` stops script *reading* the cookie but not XSS acting as the user in the page — XSS
  prevention is still required ([cross-site-scripting.md](./cross-site-scripting.md)).
- `SameSite=Lax` still sends the cookie on top-level `GET` navigations from other sites; any
  state-changing `GET` is exploitable. Lax is defence in depth, not a CSRF replacement
  ([csrf-protection.md](./csrf-protection.md)). "Same-site" means same registrable domain, so
  sibling subdomains (`evil.example.com`) are *same-site*.
- `SameSite=None` requires `Secure`; browsers increasingly partition or block third-party
  cookies (CHIPS / tracking prevention) — `VERIFY` browser behaviour before relying on it.
- Parent-domain cookies can be **overwritten** (cookie tossing) by any subdomain, including over
  HTTP when `Secure` is absent; `__Host-` prevents this.
- Cache-backed sessions: require authentication/TLS on Redis, use a separate database/alias,
  and never expose the cache to tenants or untrusted services.
- `SESSION_SAVE_EVERY_REQUEST = True` writes on every request — measurable DB/cache load;
  prefer throttled touch as above.
- Run `clearsessions` on a schedule for `db`/`cached_db`; expired rows are not auto-removed.
- Session keys must never appear in URLs, logs or analytics.

## 9. Common mistakes and anti-patterns

- `SESSION_COOKIE_SECURE = False` in production "because the proxy handles TLS".
- Using `signed_cookies` and expecting logout or password change to revoke stolen cookies.
- Storing roles, prices, or tenant ids in the session as the source of truth.
- Custom login code that sets `request.session["_auth_user_id"]` instead of calling `login()`
  — loses fixation protection and auth-hash stamping.
- Logout implemented as `del request.session["user_id"]` or via `GET` link (logout CSRF);
  `LogoutView` requires `POST` (Django ≥ 5.0 removed GET logout).
- `SESSION_COOKIE_DOMAIN = ".example.com"` while user-generated content lives on a subdomain.
- Very long `SESSION_COOKIE_AGE` with no idle timeout for privileged accounts.
- Forgetting `update_session_auth_hash` after a custom self-service password change (the user
  is logged out). It only re-stamps when `request.user == user`, so admin resets of another
  account correctly invalidate that account's sessions.
- Resetting a compromised account's password via `QuerySet.update(password=...)` and assuming
  sessions die — they do (the hash changes), but tokens (DRF, JWT, API keys) do not; revoke them
  separately ([api-security.md](./api-security.md)).

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Users logged out on every deploy | Cache-only sessions on a flushed cache, or `SECRET_KEY` rotated without fallback | `cached_db`; use `SECRET_KEY_FALLBACKS` |
| Logged out after changing password | Custom flow missing `update_session_auth_hash` | Call it after `set_password()`/`save()` |
| Session lost on cross-site POST return (payment/SSO callback) | `SameSite=Strict`/`Lax` blocks cookie on cross-site POST | Use GET-redirect landing page or a dedicated callback with its own state token; avoid `None` globally |
| `__Host-` cookie not stored | `Domain` set, not `Secure`, or path not `/` | Meet all three constraints |
| `django_session` huge | No `clearsessions` | Schedule it |

```python
from django.conf import settings
from django.test import TestCase, override_settings


class SessionSecurityTests(TestCase):
    def test_login_rotates_session_key(self):
        user = UserFactory(password="S3cure-pass-phrase")
        session = self.client.session          # pre-login session the attacker could know
        session["pre_login"] = True
        session.save()
        pre_key = session.session_key
        self.client.post("/accounts/login/", {"username": user.username,
                                              "password": "S3cure-pass-phrase"})
        self.assertNotEqual(self.client.session.session_key, pre_key)

    def test_password_change_invalidates_other_sessions(self):
        user = UserFactory(password="old-pass-phrase-1")
        other = self.client_class()
        other.force_login(user)
        user.set_password("new-pass-phrase-2")
        user.save()
        response = other.get("/account/")
        self.assertEqual(response.status_code, 302)  # redirected to login

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_session_cookie_flags(self):
        user = UserFactory(password="S3cure-pass-phrase")
        response = self.client.post("/accounts/login/", {"username": user.username,
                                                         "password": "S3cure-pass-phrase"})
        morsel = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(morsel["httponly"])
        self.assertTrue(morsel["secure"])
        self.assertEqual(morsel["samesite"], "Lax")
```

`UserFactory` is project test infrastructure ([django-testing](../../django-testing/SKILL.md)).
The test client sends plain HTTP; a `__Host-` cookie name is still set by Django but a real
browser would reject it without HTTPS. In the browser, inspect the
`Set-Cookie` header on the real HTTPS response for `Secure; HttpOnly; SameSite=Lax; Path=/`.

## 11. Related references and skills

- [authentication-security.md](./authentication-security.md) — login, logout, reset, MFA, re-authentication.
- [csrf-protection.md](./csrf-protection.md) — CSRF cookie, `rotate_token`, SameSite limits.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — signing, `SECRET_KEY_FALLBACKS`.
- [secure-django-configuration.md](./secure-django-configuration.md) — HTTPS prerequisites for `Secure` cookies.
- [api-security.md](./api-security.md) — token-based alternatives to sessions.
- [django-core authentication-sessions.md](../../django-core/references/authentication-sessions.md) ·
  [django-core caching.md](../../django-core/references/caching.md) ·
  [django-testing](../../django-testing/SKILL.md)

## 12. Official documentation and external references

- How to use sessions: https://docs.djangoproject.com/en/5.2/topics/http/sessions/
- Session settings: https://docs.djangoproject.com/en/5.2/ref/settings/#sessions
- `login()` / `logout()` / `update_session_auth_hash()`: https://docs.djangoproject.com/en/5.2/topics/auth/default/
- Signed cookies: https://docs.djangoproject.com/en/5.2/ref/request-response/#django.http.HttpRequest.get_signed_cookie
- Django 5.0 release notes (PickleSerializer removal, GET logout removal): https://docs.djangoproject.com/en/5.2/releases/5.0/
- RFC 6265 — HTTP State Management Mechanism: https://www.rfc-editor.org/rfc/rfc6265
- RFC 6265bis draft (SameSite, cookie prefixes): https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
- OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- OWASP ASVS V3 Session Management: https://owasp.org/www-project-application-security-verification-standard/
