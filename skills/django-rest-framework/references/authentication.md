# Authentication

> **Scope.** Owns: DRF's authentication architecture, the built-in classes
> (`SessionAuthentication`, `BasicAuthentication`, `TokenAuthentication`,
> `RemoteUserAuthentication`), custom authentication classes, ordering, `request.user`/
> `request.auth`, anonymous users, failures and `WWW-Authenticate` challenges, token lifecycle,
> and how external identity providers fit architecturally.
> Does not cover: what an identified caller may do ([permissions-and-access-control.md](permissions-and-access-control.md)),
> Django's user model/backends/sessions ([django-core authentication-sessions](../../django-core/references/authentication-sessions.md)),
> or deep security design — password policy, token storage, OAuth/OIDC hardening, CSRF/CORS
> policy ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 1. Purpose and scope

Authentication answers **"who is calling?"** It never answers "may they do this?". This
reference explains how DRF identifies callers, what is built in, what requires third-party
packages, and the operational lifecycle of credentials.

## 2. Core concepts

**Built into DRF** (`rest_framework.authentication`):

| Class | Credential | Typical client | Notes |
|---|---|---|---|
| `SessionAuthentication` | Django session cookie | Same-site browser SPA/pages | Enforces CSRF on unsafe methods for authenticated sessions; no `WWW-Authenticate` header |
| `BasicAuthentication` | `Authorization: Basic base64(user:pass)` | Testing, simple internal tools | Sends the password every request; HTTPS only; challenge `Basic realm="api"` |
| `TokenAuthentication` | `Authorization: Token <key>` | Simple machine/mobile clients | Requires `rest_framework.authtoken` app + migration; one token per user; key stored **unhashed**; no expiry |
| `RemoteUserAuthentication` | `REMOTE_USER` set by the web server | SSO terminated at a proxy | Trusts the upstream server; see Django's remote user docs |

**Not built into DRF** — third-party packages, install and evaluate separately:

| Need | Commonly used packages |
|---|---|
| JWT access/refresh tokens | `djangorestframework-simplejwt` |
| Hashed, expiring, multiple tokens per user | `django-rest-knox` |
| OAuth2 provider | `django-oauth-toolkit` |
| Login/registration endpoints | `dj-rest-auth`, `djoser` |
| OIDC relying party / SSO | `mozilla-django-oidc`, `django-allauth` |

**Results.** `request.user` is a user instance or, when no authenticator succeeds,
`UNAUTHENTICATED_USER` (default `django.contrib.auth.models.AnonymousUser`). `request.auth` is
authenticator-specific (the `Token` instance, a validated JWT payload, or `None`).

## 3. Internal mechanics

- Each class implements `authenticate(self, request)` returning:
  - `None` — "not my credential type", try the next class;
  - `(user, auth)` — success, stop;
  - raise `AuthenticationFailed` — credentials were presented and are invalid, stop.
- Classes run in `authentication_classes` order (default `DEFAULT_AUTHENTICATION_CLASSES`), when
  `request.user` is first accessed — `APIView.initial()` forces this via
  `perform_authentication()`.
- `authenticate_header(self, request)` returns the `WWW-Authenticate` value. When the response is
  `NotAuthenticated`/`AuthenticationFailed`, DRF uses the **first** class's header: if present →
  401 with the header; if absent → 403.
- `SessionAuthentication` reads `request._request.user` (set by Django's
  `AuthenticationMiddleware`) and, if active, runs CSRF validation; failure → 403
  "CSRF Failed: …".
- `TokenAuthentication` looks up `Token.objects.select_related("user").get(key=key)` (via
  `model`), rejects inactive users. The keyword can be changed by subclassing (`keyword = "Bearer"`).

## 4. Engineering decisions and trade-offs

| Client | Recommended |
|---|---|
| First-party SPA on the same site | `SessionAuthentication` + CSRF token handling |
| Mobile app / first-party native client | Short-lived access tokens + refresh (JWT via simplejwt, or knox), or an OAuth2/OIDC flow |
| Third-party developers | OAuth2 (django-oauth-toolkit) or per-client API keys with scopes |
| Internal service-to-service | mTLS/network identity at infrastructure layer, or signed service tokens |
| Partner webhooks into your API | Signature verification of the payload, not user authentication ([django-integrations](../../django-integrations/SKILL.md)) |

**Opaque tokens vs JWT.** Opaque tokens (DB lookup per request) are revocable instantly and
simple; JWTs avoid the lookup but cannot be revoked before expiry without a deny-list, so keep
access lifetimes short. Neither is "more secure" by default.

**Ordering.** Put the class most clients use first, and choose the first class deliberately
because it determines 401 vs 403 for unauthenticated responses. A common order is token/JWT
first, session second.

## 5. Django/DRF implementation patterns

**Settings and per-view override:**

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "config.api.auth.BearerTokenAuthentication",          # machine clients
        "rest_framework.authentication.SessionAuthentication",  # first-party browser
    ],
}

class PartnerFeedView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated, HasScope.for_scope("feed:read")]
```

**Built-in token with `Bearer` keyword:**

```python
from rest_framework.authentication import TokenAuthentication

class BearerTokenAuthentication(TokenAuthentication):
    keyword = "Bearer"
```

**Custom API-key authentication with hashed keys:**

```python
class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Api-Key"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith(f"{self.keyword} "):
            return None                                       # not our scheme
        raw_key = header[len(self.keyword) + 1:].strip()
        key = ApiKey.objects.select_related("owner").filter(
            prefix=raw_key[:8], revoked_at__isnull=True
        ).first()
        if key is None or not key.matches(raw_key) or not key.owner.is_active:
            raise exceptions.AuthenticationFailed("Invalid API key.")
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())   # consider sampling
        return (key.owner, key)                                # request.auth = ApiKey (scopes)

    def authenticate_header(self, request):
        return self.keyword                                    # enables 401 + WWW-Authenticate
```

`key.matches()` should use a constant-time comparison of hashes (e.g. `hmac.compare_digest`).

**Login endpoint for the built-in token** (`rest_framework.authtoken.views.obtain_auth_token`)
— throttle it:

```python
class ObtainTokenView(ObtainAuthToken):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"
```

**External identity provider (architecture).** Validate the IdP-issued token (signature,
issuer, audience, expiry) in an authentication class or a vetted package; map the subject to a
local user (just-in-time provisioning in a service); put IdP scopes/claims on `request.auth` for
permission classes. Key discovery and caching details belong to
[django-security-engineering](../../django-security-engineering/SKILL.md) and [django-integrations](../../django-integrations/SKILL.md).

## 6. Production use cases

- **SPA + mobile**: session for the web app, JWT (simplejwt) for mobile; both resolve to the same
  user; permissions identical.
- **Partner API keys**: per-partner hashed keys with scopes on `request.auth`, rotation by issuing
  a second key before revoking the first.
- **Token rotation**: refresh-token rotation and deny-listing (simplejwt's blacklist app) on
  logout or compromise.
- **Revocation**: opaque tokens deleted/flagged on password change, user deactivation and
  admin action.

## 7. Advantages and disadvantages

| Class | Advantages | Disadvantages |
|---|---|---|
| Session | Built-in, HttpOnly cookies, instant logout | CSRF required; same-site only; not for third parties |
| Basic | Trivial | Password on every request; no revocation granularity |
| Built-in Token | Built-in, simple | Unhashed storage, no expiry, one token per user |
| JWT (third-party) | No DB lookup; standard claims | Revocation needs a deny-list; key management; payload visible |
| Custom API key | Exactly your model (scopes, hashing, expiry) | You own its correctness |

## 8. Security and performance considerations

- Authentication ≠ authorisation; `IsAuthenticated` alone grants every authenticated user
  everything the view exposes.
- HTTPS only for any credential-bearing scheme.
- Never accept tokens in query strings (logged by proxies and servers).
- The built-in `Token.key` is stored in plaintext — a DB leak leaks working credentials; prefer
  hashed keys for high-value APIs. Treat the built-in token as long-lived and rotate manually.
- DRF throttles run after authentication; brute-force of credentials must be limited elsewhere
  (scoped throttle on the login view, infrastructure rate limits, lockout policy).
- Per-request DB lookups for opaque tokens are usually cheap (indexed key); avoid writes per
  request (`last_used_at`) on hot paths or batch them.
- Don't disable CSRF by removing `SessionAuthentication` checks; if cross-site cookies are needed,
  design CORS/CSRF with [django-security-engineering](../../django-security-engineering/SKILL.md).

## 9. Common mistakes and anti-patterns

- Assuming DRF includes JWT support (it does not).
- Raising `AuthenticationFailed` when the header is simply absent or for another scheme — return
  `None` so other authenticators can try.
- Custom authenticators without `authenticate_header()` → clients get 403 instead of 401.
- Putting `SessionAuthentication` first for a mobile/token API (403s and CSRF failures).
- Long-lived JWT access tokens with no revocation strategy.
- Storing API keys in plaintext.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| 403 "Authentication credentials were not provided." | First authenticator has no challenge header (session) — expected 401 not possible |
| 403 "CSRF Failed" | Session-authenticated unsafe request without CSRF token/header |
| 401 "Invalid token." | Wrong keyword (`Token` vs `Bearer`), revoked token, inactive user |
| `request.user` is `AnonymousUser` with a valid header | Authenticator not in `authentication_classes` for that view; proxy strips `Authorization` (e.g. Apache `WSGIPassAuthorization Off`) |
| `no such table: authtoken_token` | `rest_framework.authtoken` not installed/migrated |

Tests: `APIClient.credentials(HTTP_AUTHORIZATION="Bearer …")` for the real path;
`force_authenticate()` for permission tests ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [permissions-and-access-control.md](permissions-and-access-control.md)
- [throttling-and-rate-limiting.md](throttling-and-rate-limiting.md) — login throttles
- [request-response-lifecycle.md](request-response-lifecycle.md) — when authentication runs
- [django-core authentication-sessions](../../django-core/references/authentication-sessions.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md) — credential and token security
- [django-integrations](../../django-integrations/SKILL.md) — IdPs and webhooks

## 12. Official documentation and external references

- Authentication: <https://www.django-rest-framework.org/api-guide/authentication/>
- Django authentication: <https://docs.djangoproject.com/en/5.2/topics/auth/>
- Django `REMOTE_USER`: <https://docs.djangoproject.com/en/5.2/howto/auth-remote-user/>
- Third-party: simplejwt <https://django-rest-framework-simplejwt.readthedocs.io/>,
  knox <https://jazzband.github.io/django-rest-knox/>,
  django-oauth-toolkit <https://django-oauth-toolkit.readthedocs.io/>
- RFC 9110 §11 (HTTP authentication): <https://www.rfc-editor.org/rfc/rfc9110#section-11>
