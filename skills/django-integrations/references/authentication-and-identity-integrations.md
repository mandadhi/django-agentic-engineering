# Authentication and Identity Integrations

**Scope:** How a Django application authenticates *to* external systems and verifies identities *from* them: API keys, Basic, bearer tokens, OAuth 2.0 grants, OpenID Connect, JWT/JWKS validation, Microsoft Entra ID patterns, mTLS and service-to-service credentials. End-user login, password handling, sessions and DRF authentication classes are owned by [django-security-engineering](../../django-security-engineering/references/authentication-security.md) and [django-rest-framework](../../django-rest-framework/references/authentication.md); secret storage mechanics by [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md).

## 1. Purpose and scope

Every integration has two identity questions: *who am I to the provider* (outbound credentials) and *who is calling me / who issued this token* (inbound verification). Getting either wrong produces the worst integration incidents: leaked long-lived credentials, tokens accepted from the wrong issuer, refresh-token loss that silently breaks a customer connection, or a token-refresh stampede that gets the client rate-limited by the identity provider.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Libraries: `httpx` [third-party: httpx], `PyJWT` [third-party: PyJWT] with `cryptography` [third-party: cryptography], `authlib` [third-party: Authlib] optional, `msal` [third-party: msal] and `azure-identity` [third-party: azure-identity] for Entra ID.

Covered here: mechanism selection, token acquisition and caching, refresh and rotation, JWT validation, IdP integration patterns, mTLS, service-to-service auth. Not covered: building your own authorization server (use a product), DRF token auth classes, SAML internals (mentioned only as a legacy alternative).

## 2. Core concepts

### OAuth 2.0 is authorization; OpenID Connect is authentication

| | OAuth 2.0 (RFC 6749) | OpenID Connect Core 1.0 |
|---|---|---|
| Question answered | "May this client access this resource (on behalf of someone)?" | "Who is the user, and how/when did they authenticate?" |
| Primary artifact | Access token (opaque or JWT) for the **resource server** | ID token (always JWT) for the **client** |
| Audience | API (resource server) | Your client application (`aud` = your client_id) |
| Must you parse it? | Clients: no — treat access tokens as opaque. Resource servers: yes | Yes — the client must validate the ID token |
| Scope marker | Arbitrary scopes | `openid` scope required; `profile`, `email` optional |

Rules that follow:
- Never use an access token as proof of user identity in your client ("login with OAuth" without OIDC is the classic confused-deputy hole).
- Never send an ID token to an API as a bearer credential; APIs accept access tokens issued for them.
- A client should not inspect access-token claims; formats change without notice (Entra ID explicitly warns about this `VERIFY`).

### Credential types

| Mechanism | Identity of | Lifetime | Revocation | Typical use |
|---|---|---|---|---|
| API key (header) | Application/account | Long | Manual rotation | Simple SaaS APIs, internal tools |
| HTTP Basic (RFC 7617) | Account | Long | Password change | Legacy, SOAP, SFTP gateways |
| Static bearer token | Account/app | Long | Manual | PATs, legacy "API tokens" |
| OAuth client credentials | Application | Short (minutes–hours) | Expiry + secret rotation | Service-to-service |
| OAuth authorization code (+PKCE) | User via client | Access short, refresh long | Refresh revocation | Acting on behalf of a user (CRM, mailbox, drive) |
| Signed JWT client assertion (RFC 7523) | Application | Seconds–minutes | Key rotation | Certificate-based client auth, Entra ID |
| mTLS (RFC 8705 for OAuth) | Application/host | Certificate validity | CRL/OCSP, rotation | Banking, B2B, internal mesh |
| Managed/workload identity | Compute resource | Short, platform-issued | Platform | Cloud resources, no stored secret |

### Grant selection

- **Client credentials**: no user; the app itself is the principal. Default for back-end integrations.
- **Authorization code + PKCE (RFC 7636)**: user delegates access; use PKCE even for confidential clients (OAuth 2.1 draft and RFC 9700 BCP recommend it `VERIFY` for final RFC status of OAuth 2.1).
- **Device code (RFC 8628)**: CLI/TV, rarely relevant to Django.
- **Refresh token**: obtains new access tokens without user interaction.
- **Implicit and Resource Owner Password Credentials**: deprecated by RFC 9700; do not implement.
- **Token exchange (RFC 8693) / On-Behalf-Of (Entra)**: a middle-tier API calls a downstream API as the user.

## 3. Internal mechanics

### Client credentials flow

```text
Django worker ──POST /token (grant_type=client_credentials, client_id, secret|assertion, scope)──▶ IdP
              ◀── {access_token, token_type: Bearer, expires_in: 3599} ──
Django worker ──GET /resource, Authorization: Bearer <token>──▶ Resource server
              ◀── 200 | 401 (expired/invalid → refresh once, retry once)
```

### Authorization code + PKCE

1. Generate `code_verifier` (43–128 chars, high entropy), `code_challenge = BASE64URL(SHA256(verifier))`, `state`, and for OIDC a `nonce`. Store verifier/state/nonce server-side (session).
2. Redirect to `/authorize?response_type=code&client_id&redirect_uri&scope&state&code_challenge&code_challenge_method=S256`.
3. Callback: compare `state` (CSRF), exchange `code` + `code_verifier` at `/token`.
4. OIDC: validate ID token (`iss`, `aud`, `exp`, `iat`, `nonce`, signature). Persist the refresh token encrypted.

### JWT validation pipeline (resource-server side)

1. Parse header without trusting it; take `kid` and `alg`.
2. Reject `alg` not in an explicit allow-list (never `none`, never accept HS* when you expect RS*/ES* — algorithm confusion).
3. Find the key by `kid` in the cached JWKS; on miss, refetch JWKS once (rate-limited) to handle key rotation.
4. Verify signature, then `exp`/`nbf` (with small leeway), `iss` exact match, `aud` contains your identifier.
5. Authorize on claims (`scp`/`scope` for delegated, `roles` for app permissions, `tid` for tenant).

### Discovery

OIDC providers publish `/.well-known/openid-configuration` (OIDC Discovery 1.0); plain OAuth servers may publish RFC 8414 metadata. Cache it (hours); it contains `issuer`, `jwks_uri`, `token_endpoint`. Pin the expected `issuer` in settings — discovery documents are trusted inputs only when fetched over TLS from a configured URL.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Avoid / when acceptable |
|---|---|---|
| App-to-app auth to a cloud service | Managed/workload identity (no secret) | Client secret — only when the platform cannot issue identity |
| Client authentication at IdP | Certificate/JWT assertion (`private_key_jwt`) | Client secret — acceptable with secret manager + rotation |
| Token cache location | Process memory + Redis shared cache with lock | DB per request; fetching a token per call |
| Validate inbound JWT | Local validation with cached JWKS | Introspection (RFC 7662) per request — use only when revocation must be immediate, cache results briefly |
| User-delegated access storage | Encrypted refresh token per (user, provider) row | Storing access tokens long-term; storing in session cookies |
| Library | `authlib`/`msal` for flows; PyJWT for validation | Hand-rolled OAuth flows beyond simple client credentials |
| B2B high assurance | mTLS + OAuth (certificate-bound tokens, RFC 8705) | IP allow-lists as sole control |

Trade-off: JWT access tokens validate locally (fast, no IdP dependency per request) but cannot be revoked before `exp`; keep lifetimes short (5–60 min). Opaque tokens + introspection give revocation but add latency and an availability dependency.

## 5. Django implementation patterns

### Settings

```python
# settings.py — values come from env / secret manager, never literals
ACME_OAUTH = {
    "TOKEN_URL": env("ACME_TOKEN_URL"),
    "CLIENT_ID": env("ACME_CLIENT_ID"),
    "CLIENT_SECRET": env("ACME_CLIENT_SECRET"),   # injected at runtime
    "SCOPE": "orders.read orders.write",
}
```

### Client-credentials token provider with shared cache and stampede protection

```python
# apps/integrations/auth.py
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from django.core.cache import cache
from django_redis import get_redis_connection  # [third-party: django-redis]; or redis-py directly

from apps.integrations.errors import AuthenticationError, TransientIntegrationError

EARLY_REFRESH_SECONDS = 120  # refresh before expiry; covers clock skew and in-flight requests


@dataclass(frozen=True)
class CachedToken:
    value: str
    expires_at: float


class ClientCredentialsProvider:
    def __init__(self, *, name: str, token_url: str, client_id: str, client_secret: str,
                 scope: str, http: httpx.Client) -> None:
        self.name, self.token_url, self.scope = name, token_url, scope
        self._auth = (client_id, client_secret)
        self._http = http
        self._local: CachedToken | None = None
        self._cache_key = f"oauth:token:{name}"

    def token(self) -> str:
        now = time.time()
        if self._local and self._local.expires_at - EARLY_REFRESH_SECONDS > now:
            return self._local.value
        cached: CachedToken | None = cache.get(self._cache_key)
        if cached and cached.expires_at - EARLY_REFRESH_SECONDS > now:
            self._local = cached
            return cached.value
        # One refresher across all workers; others wait briefly then re-read the cache.
        lock = get_redis_connection("default").lock(f"{self._cache_key}:lock", timeout=15, blocking_timeout=10)
        with lock:
            cached = cache.get(self._cache_key)
            if cached and cached.expires_at - EARLY_REFRESH_SECONDS > time.time():
                self._local = cached
                return cached.value
            fresh = self._fetch()
            cache.set(self._cache_key, fresh, timeout=int(fresh.expires_at - time.time()))
            self._local = fresh
            return fresh.value

    def invalidate(self) -> None:
        self._local = None
        cache.delete(self._cache_key)

    def _fetch(self) -> CachedToken:
        try:
            resp = self._http.post(self.token_url, auth=self._auth,
                                   data={"grant_type": "client_credentials", "scope": self.scope})
        except httpx.TransportError as exc:
            raise TransientIntegrationError(provider=self.name, operation="token") from exc
        if resp.status_code in (400, 401):
            # invalid_client / invalid_scope: configuration or rotated secret — page someone, don't retry
            raise AuthenticationError(provider=self.name, operation="token", status_code=resp.status_code)
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientIntegrationError(provider=self.name, operation="token", status_code=resp.status_code)
        body = resp.json()
        return CachedToken(body["access_token"], time.time() + int(body["expires_in"]))
```

Notes: the cache stores a bearer token — use a Redis instance not shared with untrusted tenants, with TLS and auth; set the cache TTL to the token lifetime. If Redis is unavailable, fall back to per-process fetch (degraded, more IdP load) rather than failing every call.

### Plugging the token into httpx (refresh-on-401 once)

```python
class OAuthBearer(httpx.Auth):
    def __init__(self, provider: ClientCredentialsProvider) -> None:
        self.provider = provider

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.provider.token()}"
        response = yield request
        if response.status_code == 401:            # token revoked/rotated early
            self.provider.invalidate()
            request.headers["Authorization"] = f"Bearer {self.provider.token()}"
            yield request                           # exactly one retry; a second 401 is real
```

`httpx.Auth` re-sends the same request; this is safe only because a 401 means the provider did not process it. Do not combine with a generic retry layer that also retries 401.

### Authorization code + PKCE (user-delegated connection)

```python
# apps/crm/integrations/partnercrm/oauth_views.py
import base64, hashlib, secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect

from apps.crm.integrations.partnercrm.client import exchange_code
from apps.crm.models import ProviderConnection


@login_required
def connect(request):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    request.session["partnercrm_oauth"] = {"state": state, "verifier": verifier}
    params = {
        "response_type": "code", "client_id": settings.PARTNERCRM["CLIENT_ID"],
        "redirect_uri": settings.PARTNERCRM["REDIRECT_URI"], "scope": "contacts.read offline_access",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return redirect(f"{settings.PARTNERCRM['AUTHORIZE_URL']}?{urlencode(params)}")


@login_required
def callback(request):
    pending = request.session.pop("partnercrm_oauth", None)
    if not pending or not secrets.compare_digest(pending["state"], request.GET.get("state", "")):
        return HttpResponseBadRequest("invalid state")
    if "error" in request.GET:
        return redirect("integrations:failed")      # user denied consent; don't log the description verbatim
    tokens = exchange_code(code=request.GET["code"], verifier=pending["verifier"])
    ProviderConnection.objects.update_or_create(
        user=request.user, provider="partnercrm",
        defaults={"refresh_token": tokens.refresh_token, "scopes": tokens.scope,
                  "access_expires_at": tokens.expires_at},
    )
    return redirect("integrations:connected")
```

`redirect_uri` must be an exact, pre-registered URL; never build it from `request.get_host()` without `ALLOWED_HOSTS` discipline.

### Storing refresh tokens encrypted and handling rotation

```python
# apps/crm/models.py
from django.db import models
from apps.integrations.crypto import EncryptedTextField  # Fernet/KMS-backed field; see django-security-engineering

class ProviderConnection(models.Model):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE)
    provider = models.CharField(max_length=32)
    refresh_token = EncryptedTextField()
    scopes = models.CharField(max_length=500)
    access_expires_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=16, default="active")  # active | reauth_required | revoked
    version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "provider"], name="uniq_conn")]
```

Refresh-token rotation (RFC 9700 recommends it for public clients; many providers do it for all) means each refresh returns a **new** refresh token and invalidates the old one. Two workers refreshing concurrently → one wins, the other presents a revoked token → some IdPs then revoke the whole token family (reuse detection). Serialize refresh per connection:

```python
from django.db import transaction

def refresh_access_token(connection_id: int) -> str:
    with transaction.atomic():
        conn = ProviderConnection.objects.select_for_update().get(pk=connection_id)
        # Network call inside a short row lock is a deliberate exception: the lock IS the point.
        # Keep the token endpoint timeout small (≤ 5 s).
        tokens = partnercrm_refresh(conn.refresh_token)          # raises AuthenticationError on invalid_grant
        conn.refresh_token = tokens.refresh_token or conn.refresh_token
        conn.access_expires_at = tokens.expires_at
        conn.version += 1
        conn.save(update_fields=["refresh_token", "access_expires_at", "version"])
    return tokens.access_token
```

If the persisted write fails after the IdP rotated the token, the connection is lost — this is why the lock and the save must be adjacent. A Redis lock + separate save is an alternative that avoids holding a DB transaction during I/O. On `invalid_grant`, set `status="reauth_required"` and notify the user; do not retry.

### Inbound JWT validation (Django calls are authenticated by an IdP)

```python
# apps/integrations/jwt_validation.py
import jwt                                  # [third-party: PyJWT]
from jwt import PyJWKClient
from django.conf import settings

_jwks = PyJWKClient(settings.OIDC["JWKS_URI"], cache_keys=True, lifespan=3600, timeout=5)
ALLOWED_ALGS = ["RS256"]                    # explicit; never derived from the token header

def validate_access_token(raw: str) -> dict:
    signing_key = _jwks.get_signing_key_from_jwt(raw)       # refetches JWKS on unknown kid
    claims = jwt.decode(
        raw, signing_key.key, algorithms=ALLOWED_ALGS,
        audience=settings.OIDC["AUDIENCE"], issuer=settings.OIDC["ISSUER"],
        leeway=60, options={"require": ["exp", "iat", "iss", "aud"]},
    )
    return claims
```

`PyJWKClient` parameters (`cache_keys`, `lifespan`, `timeout`) vary across PyJWT 2.x releases (`VERIFY` against the installed version). Wire this into DRF as an authentication class per [DRF authentication](../../django-rest-framework/references/authentication.md); this module only owns verification.

### Microsoft Entra ID patterns

- Endpoints: v2.0 — `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, issuer `https://login.microsoftonline.com/{tenant}/v2.0`. Multi-tenant apps using `common`/`organizations` must validate `iss` per tenant (`tid` claim) against an allow-list — a fixed `issuer=` check does not work and skipping it accepts any tenant.
- App-only (client credentials) scope is `{resource-app-id-uri}/.default`; permissions come from **app roles** granted with admin consent and appear in the `roles` claim. Delegated permissions appear in `scp`.
- Access-token version is controlled by the resource app's manifest (`accessTokenAcceptedVersion` / `requestedAccessTokenVersion`), which changes `iss` and `aud` formats (`VERIFY` the exact manifest property name for the portal you use).
- Prefer certificate credentials or federated identity credentials (workload identity) over client secrets; secrets have a maximum lifetime (`VERIFY` current portal limit).
- `msal.ConfidentialClientApplication` caches tokens in memory per instance — keep one module-level instance per process; for shared caching serialize the MSAL token cache to Redis.
- Running in Azure: use `DefaultAzureCredential`/`ManagedIdentityCredential` instead of any secret — see [azure-integrations](azure-integrations.md).
- On-Behalf-Of: a Django API receiving a user token for itself exchanges it for a downstream token; never forward the inbound token to a different audience.

```python
import msal  # [third-party: msal]
from django.conf import settings

_app = msal.ConfidentialClientApplication(
    client_id=settings.ENTRA["CLIENT_ID"],
    authority=f"https://login.microsoftonline.com/{settings.ENTRA['TENANT_ID']}",
    client_credential={"private_key": settings.ENTRA["CERT_KEY_PEM"],
                       "thumbprint": settings.ENTRA["CERT_THUMBPRINT"]},
)

def graph_token() -> str:
    result = _app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise AuthenticationError(provider="entra", operation="token",
                                  request_id=result.get("correlation_id"))
    return result["access_token"]
```

### Mutual TLS with httpx

```python
import ssl
import httpx

ctx = ssl.create_default_context(cafile=settings.PARTNER_BANK["CA_BUNDLE"])   # pin partner CA if private
ctx.load_cert_chain(certfile=settings.PARTNER_BANK["CLIENT_CERT"], keyfile=settings.PARTNER_BANK["CLIENT_KEY"])
ctx.minimum_version = ssl.TLSVersion.TLSv1_2

bank_http = httpx.Client(base_url=settings.PARTNER_BANK["BASE_URL"], verify=ctx,
                         timeout=httpx.Timeout(10.0, connect=3.0))
```

The `cert=` argument on `httpx.Client` is deprecated in favour of an `SSLContext` in newer httpx (`VERIFY` for httpx ≥ 0.28). Key files are mounted from a secret store (Kubernetes secret / Key Vault CSI), readable only by the app user. Inbound mTLS is terminated at the load balancer or ingress; Django only sees forwarded headers (e.g. client-cert subject) — trust them only from the proxy, exactly like `SECURE_PROXY_SSL_HEADER`.

### API keys and Basic

```python
http = build_client("notifyhub", base_url=settings.NOTIFYHUB_URL, timeout=httpx.Timeout(5, connect=2),
                    auth=None)
http.headers["Authorization"] = f"Bearer {settings.NOTIFYHUB_API_KEY}"   # header, never query string
```

Query-string keys end up in proxy logs, browser history and APM traces; use them only when the provider offers nothing else, and redact the parameter in logging hooks.

### Service-to-service inside your estate

Order of preference: platform workload identity → OAuth client credentials with per-service clients and scopes → mTLS in a service mesh → shared HMAC key (only for two services owned by one team). Each calling service gets its own credential so audit and revocation are per caller.

## 6. Production use cases

- **Order export to ERP** via client credentials: token cached in Redis, 300 Celery workers share one token, refresh lock prevents 300 concurrent token calls when it expires.
- **"Connect your CRM" feature**: authorization code + PKCE per user; refresh tokens encrypted; nightly task refreshes connections nearing provider idle-expiry; `reauth_required` surfaced in UI.
- **Internal API accepting Entra ID tokens** from other company services: validate `iss` = company tenant, `aud` = API app ID URI, and require an app role (`roles` contains `Orders.Read`).
- **Bank partner** requiring mTLS plus signed JWT client assertion; certificates rotated quarterly with overlap.
- **Microsoft Graph** mailbox reading using application permissions limited by an application access policy (`VERIFY` current Exchange RBAC for Applications replacement).

## 7. Advantages and disadvantages

| Mechanism | Advantages | Disadvantages |
|---|---|---|
| API key | Trivial, no IdP dependency | Long-lived, coarse scope, rotation painful |
| Client credentials | Short-lived, scoped, central revocation | IdP availability dependency; secret still needed unless assertion/MI |
| Auth code + PKCE | User consent, least privilege per user | Refresh-token lifecycle, re-auth UX, per-user storage |
| Local JWT validation | Fast, no per-request IdP call | No instant revocation; key-rotation handling required |
| Introspection | Real-time revocation | Latency, IdP coupling |
| mTLS | Strong, phishing-proof, binds tokens | Certificate operations, proxy termination complexity |
| Managed identity | No secret to leak or rotate | Platform-specific; local dev needs a different credential chain |

## 8. Security and performance considerations

- Least privilege: separate client registrations per environment and per integration; minimal scopes; never reuse production credentials in staging.
- Token endpoints are rate-limited by IdPs; one token per process per lifetime, not per request. A token fetch per call multiplies latency by 2 and can trip IdP throttling (Entra returns 429 with `Retry-After` `VERIFY`).
- Clock skew: allow 30–120 s leeway; NTP-sync hosts. Early refresh avoids sending tokens that expire in flight.
- Never log `Authorization`, `client_secret`, `code`, `refresh_token`, `id_token`; scrub in httpx event hooks and Sentry `before_send`.
- Cached tokens in Redis are credentials: TLS to Redis, auth, no shared tenant cache, short TTL.
- Validate redirect URIs exactly; open redirects in callbacks leak codes ([ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md) covers open redirects).
- Reject `alg=none`, HS/RS confusion, and `jku`/`x5u` header URLs (never fetch keys from token-supplied URLs).
- Refresh tokens: encrypt at rest, delete on disconnect, revoke at the provider (RFC 7009) when the user disconnects.
- Rotation: support two valid secrets/certs during overlap; automate expiry alerts 30 days ahead.

## 9. Common mistakes and anti-patterns

- Using OAuth access tokens (or `/userinfo` with any token) as "login" without OIDC ID-token validation.
- Decoding JWTs with `options={"verify_signature": False}` "just to read the claims" and then trusting them.
- `algorithms=jwt.get_unverified_header(t)["alg"]` — attacker chooses the algorithm.
- Not checking `aud` → accepting tokens meant for another API in the same tenant.
- Fetching a new client-credentials token for every request; or caching it past `expires_in`.
- Concurrent refresh of rotating refresh tokens → token family revoked → every user must reconnect.
- Retrying `invalid_client`/`invalid_grant` with backoff — they are permanent until a human acts.
- Client secrets in source, `.env` committed, CI logs, or Celery task arguments (task args are persisted in the broker and result backend).
- Storing access tokens in the session cookie or local storage for server-side integrations.
- Treating Entra `common` endpoint tokens with a single fixed issuer string (fails) or with no issuer check (accepts all tenants).

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| 401 after deploy, all calls | Secret/cert rotated or wrong env | Token endpoint response `error`; secret version in secret manager |
| Intermittent 401 near expiry | No early refresh; clock skew | Compare `exp` with host time; add early refresh |
| `InvalidAudienceError` | Token requested with wrong scope/resource | Decode (unverified, locally) and compare `aud` with settings |
| `InvalidIssuerError` with Entra | v1 vs v2 token version mismatch | Resource app manifest token version |
| Unknown `kid` errors after IdP rotation | JWKS cached without refetch-on-miss | JWKS client refresh behaviour; outbound access to `jwks_uri` |
| `invalid_grant` on refresh | Rotated token reused, consent revoked, idle expiry | Refresh concurrency; provider audit log |
| Token endpoint 429 | Per-request token fetch, cache down | Token fetch metric per minute |
| mTLS handshake failure | Wrong chain, expired cert, SNI, proxy terminating TLS | `openssl s_client -connect host:443 -cert c.pem -key k.pem` |

Validation checklist: token fetch count ≈ workers × (uptime / lifetime); tests cover expired, wrong-aud, wrong-iss, bad signature, unknown `kid`, `alg=none` tokens (see [integration-testing-and-contracts](integration-testing-and-contracts.md)); runbook for credential rotation exists.

## 11. Related references and skills

- [integration-security](integration-security.md) — credential protection, TLS, redaction across integrations.
- [azure-integrations](azure-integrations.md) — `DefaultAzureCredential`, managed identity, Key Vault.
- [aws-and-cloud-integrations](aws-and-cloud-integrations.md) — IAM roles, workload identity federation.
- [webhooks-and-inbound-integrations](webhooks-and-inbound-integrations.md) — inbound authentication by signature.
- [http-and-rest-api-integrations](http-and-rest-api-integrations.md) — client construction where auth plugs in.
- [integration-reliability-and-resilience](integration-reliability-and-resilience.md) — why auth errors are permanent, not retried.
- django-security-engineering: [authentication-security](../../django-security-engineering/references/authentication-security.md), [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md), [api-security](../../django-security-engineering/references/api-security.md).
- django-rest-framework: [authentication](../../django-rest-framework/references/authentication.md).
- django-testing: [mocking-and-dependency-isolation](../../django-testing/references/mocking-and-dependency-isolation.md).

## 12. Official documentation and external references

- RFC 6749 OAuth 2.0 — https://www.rfc-editor.org/rfc/rfc6749
- RFC 6750 Bearer Token Usage — https://www.rfc-editor.org/rfc/rfc6750
- RFC 7636 PKCE — https://www.rfc-editor.org/rfc/rfc7636
- RFC 9700 OAuth 2.0 Security Best Current Practice — https://www.rfc-editor.org/rfc/rfc9700
- RFC 7519 JWT — https://www.rfc-editor.org/rfc/rfc7519 ; RFC 7517 JWK — https://www.rfc-editor.org/rfc/rfc7517
- RFC 8725 JWT Best Current Practices — https://www.rfc-editor.org/rfc/rfc8725
- RFC 7523 JWT client assertions — https://www.rfc-editor.org/rfc/rfc7523
- RFC 7662 Token Introspection — https://www.rfc-editor.org/rfc/rfc7662 ; RFC 7009 Revocation — https://www.rfc-editor.org/rfc/rfc7009
- RFC 8705 OAuth mTLS — https://www.rfc-editor.org/rfc/rfc8705 ; RFC 8693 Token Exchange — https://www.rfc-editor.org/rfc/rfc8693
- RFC 8414 Authorization Server Metadata — https://www.rfc-editor.org/rfc/rfc8414 ; RFC 7617 Basic — https://www.rfc-editor.org/rfc/rfc7617
- OpenID Connect Core 1.0 — https://openid.net/specs/openid-connect-core-1_0.html
- OpenID Connect Discovery 1.0 — https://openid.net/specs/openid-connect-discovery-1_0.html
- Microsoft identity platform docs — https://learn.microsoft.com/en-us/entra/identity-platform/
- Entra access tokens — https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- Entra client credentials flow — https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
- MSAL for Python — https://learn.microsoft.com/en-us/entra/msal/python/
- PyJWT — https://pyjwt.readthedocs.io/en/stable/
- Authlib — https://docs.authlib.org/en/latest/
- HTTPX authentication — https://www.python-httpx.org/advanced/authentication/ ; SSL — https://www.python-httpx.org/advanced/ssl/
- Python `ssl` — https://docs.python.org/3/library/ssl.html ; `secrets` — https://docs.python.org/3/library/secrets.html
