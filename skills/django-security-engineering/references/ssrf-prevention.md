# SSRF Prevention

> Scope: server-side request forgery mechanics; features that fetch user-controlled URLs;
> URL, scheme and IP validation; DNS rebinding and time-of-check/time-of-use; redirect handling;
> timeouts and response caps; egress proxies and network policy; open-redirect prevention for
> `next=` style parameters.
> Not covered here: HTTP client configuration, retries and webhook delivery reliability
> ([django-integrations](../../django-integrations/SKILL.md)); webhook signature verification
> ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); "upload from URL" file
> validation after download ([file-upload-security.md](./file-upload-security.md)); CORS, which
> is a browser control and unrelated to SSRF ([cors-and-browser-security.md](./cors-and-browser-security.md)).

## 1. Purpose and scope

SSRF happens when an attacker makes your server send a request to a destination of their
choosing. The server sits inside your network, has cloud credentials, and is trusted by internal
services — so the attacker reaches what they cannot reach directly: cloud metadata endpoints,
admin interfaces on localhost, databases and caches with HTTP-ish protocols, internal APIs
without authentication.

Django has **no SSRF protection**: it does not make outbound requests on your behalf, and
`URLValidator` only checks syntax. Every feature that fetches a user-supplied URL is an
application responsibility. This reference also covers open redirects, the browser-side cousin
(the server tells the *browser* to go somewhere attacker-chosen), because both are fixed by the
same discipline: never follow an untrusted destination without an allow-list.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Basic SSRF | Response is returned to the attacker (URL preview, import-from-URL) |
| Blind SSRF | Response is not returned, but the request still happens (webhooks, pingbacks) — still exploitable for internal state changes and port scanning via timing |
| Metadata endpoint | `169.254.169.254` (AWS, GCP, Azure, others), `fd00:ec2::254` (AWS IPv6), `metadata.google.internal`; yields credentials on many platforms |
| IMDSv2 | AWS metadata requiring a PUT-acquired session token; blocks most simple GET-only SSRF — enable it, but do not rely on it alone |
| DNS rebinding | A hostname resolves to a public IP at validation time and a private IP at connection time |
| TOCTOU | Validating one thing (a resolved IP) and connecting to another (a new resolution) |
| Redirect pivot | An allowed URL redirects to an internal one; client libraries follow redirects by default |
| Egress control | Network-level restriction of which destinations the application may contact |
| Open redirect | `redirect(request.GET["next"])` sends users to an attacker site with your domain's credibility |

Features that are usually SSRF-prone: outgoing webhooks to customer URLs, link previews/unfurling,
avatar or "import file from URL", RSS/iCal importers, PDF/HTML renderers that load `<img>`/`<link>`
resources from user HTML, OAuth/OIDC discovery with user-provided issuers, SAML metadata URLs,
XML parsers resolving external entities ([injection-prevention.md](./injection-prevention.md)).

## 3. Internal mechanics

Why naive validation fails:

1. **Parser differentials.** `urllib.parse`, `requests`/`urllib3`, and `httpx` can disagree on
   URLs such as `http://allowed.com@evil.com/`, `http://evil.com\@allowed.com`, or ones with
   unusual whitespace. Validate using the same parsed structure you connect with, and reject
   userinfo (`user:pass@`) outright.
2. **Alternative IP encodings.** `http://2130706433/`, `http://0x7f000001/`, `http://0177.0.0.1/`,
   `http://127.1/`, `http://[::ffff:127.0.0.1]/` all reach loopback on many stacks. String
   blocklists ("contains 127.0.0.1") are useless; resolve and compare `ipaddress` objects.
3. **DNS.** A hostname check without resolution permits `internal.attacker.com → 10.0.0.5`.
   A resolution check followed by a second resolution inside the HTTP client permits rebinding
   (attacker DNS with TTL 0 alternates answers).
4. **Redirects.** `requests` follows up to 30 redirects by default; `httpx` does not follow by
   default (`follow_redirects=False`). Each hop is a new destination needing validation.
5. **Schemes.** `file://`, `gopher://`, `dict://`, `ftp://` are supported by some libraries
   (curl-based ones notably). `requests` supports only HTTP(S) without extra adapters, but a
   scheme allow-list is still required because libraries and renderers vary.
6. **Proxies from the environment.** `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` env vars change where
   connections go (`requests` honours them via `trust_env=True`).

## 4. Engineering decisions and trade-offs

| Approach | Strength | Cost | Use when |
|---|---|---|---|
| Host allow-list (exact hostnames) | Strongest at app level | Limits product features | Integrations with known partners, OIDC issuers you configure |
| Egress proxy that enforces IP policy (e.g. Smokescreen — third-party) | Central, covers all code and libraries, resolves and pins internally | Infra to run; app must route through it | Any product feature that fetches arbitrary customer URLs |
| Network policy (security groups, Kubernetes `NetworkPolicy`, separate VPC for fetchers) | Cannot be bypassed by application bugs | Coarse; ops ownership | Always, as the last layer |
| In-app resolve-validate-pin | Works without infra | Easy to get subtly wrong; per-library code | When no egress proxy exists yet |
| Isolated fetcher service with no credentials | Limits blast radius | Another service | High-volume previews/renderers |

Recommended layering for arbitrary-URL features: scheme/port allow-list and IP validation in
the app **plus** an egress proxy or dedicated network segment, **plus** IMDSv2 (hop limit 1) on
cloud hosts. For fixed integrations: a hostname allow-list in settings.

## 5. Django implementation patterns

### 5.1 Validate a URL and its resolved addresses

```python
# integrations/netsafety.py
import ipaddress
import socket
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

ALLOWED_SCHEMES = {"https"}          # add "http" only if the feature truly needs it
ALLOWED_PORTS = {443}                # {80, 443} with http


def _is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global excludes private, loopback, link-local (incl. 169.254.169.254), shared CGNAT,
    # reserved and documentation ranges. Multicast is checked explicitly.
    return not ip.is_global or ip.is_multicast


def resolve_public_url(url: str) -> tuple[str, int, list[str]]:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValidationError("URL scheme not allowed.", code="ssrf_scheme")
    if parts.username or parts.password:
        raise ValidationError("Credentials in URL not allowed.", code="ssrf_userinfo")
    host = parts.hostname
    if not host:
        raise ValidationError("URL has no host.", code="ssrf_host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ValidationError("Port not allowed.", code="ssrf_port")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationError("Host does not resolve.", code="ssrf_dns") from exc
    addresses = sorted({info[4][0] for info in infos})
    for addr in addresses:
        if _is_forbidden(ipaddress.ip_address(addr.split("%")[0])):
            raise ValidationError("Destination not allowed.", code="ssrf_private")
    return host, port, addresses
```

`getaddrinfo` normalises decimal/octal/short IPv4 forms the same way the OS will when
connecting, which is why validation runs on its output, not on the string. Reject if **any**
resolved address is forbidden. `is_global` semantics have been refined across Python releases
(e.g. 3.11–3.13 corrections for some IPv4-mapped and special-purpose ranges — `VERIFY` against
your Python version); the explicit `ipv4_mapped` unwrap keeps behaviour consistent. Add your own
internal public ranges (e.g. your VPC's public IPs, load balancers) to a deny-list.

### 5.2 Fetch while pinned to the validated IP (httpx)

Validation alone leaves a rebinding window because the client resolves again. Connect to the
validated IP while keeping the original hostname for TLS SNI, certificate verification and the
`Host` header:

```python
# integrations/safe_fetch.py
import httpx

from .netsafety import resolve_public_url

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)


class FetchError(Exception):
    pass


def safe_fetch(url: str, *, max_redirects: int = 3) -> bytes:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        for _ in range(max_redirects + 1):
            host, port, addresses = resolve_public_url(url)
            pinned = httpx.URL(url).copy_with(host=addresses[0])
            request = client.build_request(
                "GET", pinned,
                headers={"Host": host if port in (80, 443) else f"{host}:{port}"},
                extensions={"sni_hostname": host},  # VERIFY: httpx/httpcore SNI extension
            )
            response = client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    url = str(httpx.URL(url).join(location))  # re-validated on next loop
                    continue
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchError("Response too large")
                return bytes(body)
            finally:
                response.close()
    raise FetchError("Too many redirects")
```

Certificate verification against the hostname (not the IP) depends on the SNI extension being
honoured by the transport; test it against a real HTTPS host before relying on it (`VERIFY`).
With `requests`, equivalent pinning needs a custom `HTTPAdapter` passing `server_hostname` /
`assert_hostname` to urllib3 (`VERIFY` for your urllib3 version). If this feels fragile, that is
the argument for an egress proxy: send all user-URL fetches through it
(`httpx.Client(proxy=settings.EGRESS_PROXY_URL)`) and let it enforce the IP policy at connect time.

Third-party helpers exist (e.g. `advocate` for `requests`); check maintenance status before
adopting (`VERIFY`).

### 5.3 Fixed-integration allow-list

```python
# settings
OUTBOUND_ALLOWED_HOSTS = {"api.stripe.com", "hooks.slack.com"}

# integrations/clients.py
def ensure_allowed(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in settings.OUTBOUND_ALLOWED_HOSTS:
        raise ValidationError("Destination not allowed.", code="ssrf_host")
```

Compare exact hostnames; `endswith("stripe.com")` admits `evilstripe.com`.

### 5.4 Customer webhooks

- Validate the URL at registration **and** at every delivery (DNS changes after registration).
- Deliver from a worker through the egress proxy; never from the web request.
- Do not return response bodies or detailed connection errors to the customer — they turn blind
  SSRF into port scanning. Record "delivered / failed (status class)" only.
- Sign payloads so receivers can verify origin ([secrets-and-cryptography.md](./secrets-and-cryptography.md)).

### 5.5 Open redirects (`next=`)

```python
from django.conf import settings
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, fallback=None):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(candidate)
    return redirect(fallback or settings.LOGIN_REDIRECT_URL)
```

Django's `LoginView`/`LogoutView` already apply this check to `next`; custom views must do it
themselves. `request.get_host()` is safe only because `ALLOWED_HOSTS` validates it
([secure-django-configuration.md](./secure-django-configuration.md)). Prefer redirecting to named
URLs or an allow-listed set of paths over echoing arbitrary URLs.

### 5.6 HTML/PDF renderers

Renderers (WeasyPrint, headless Chromium, wkhtmltopdf) fetch `<img src>`, CSS `url()`, `<iframe>`
and even `file://` from user-influenced HTML. Configure a custom URL fetcher that only serves
local static assets (WeasyPrint `url_fetcher`), disable network access for the renderer
process/container, and never render raw user HTML without sanitising it
([cross-site-scripting.md](./cross-site-scripting.md)).

## 6. Production use cases

- **Link previews in chat:** isolated fetcher worker, egress proxy, HTML-only content types,
  1 MB cap, 5 s budget, cache results by URL hash, no cookies or auth headers forwarded.
- **"Import avatar from URL":** `safe_fetch` → pass bytes to the image pipeline in
  [file-upload-security.md](./file-upload-security.md); never store the URL as the avatar source.
- **Enterprise SSO with customer-supplied OIDC issuer:** admin-only configuration, HTTPS only,
  validated at save and fetch, metadata cached, issuer allow-listed per tenant.
- **Cloud hosts:** IMDSv2 required with hop limit 1; containers do not get node credentials;
  metadata endpoint blocked by network policy for workloads that do not need it.

## 7. Advantages and disadvantages

| Control | Advantage | Disadvantage |
|---|---|---|
| Hostname allow-list | Simple, strong | Not possible for arbitrary-URL features |
| Resolve + IP block | Covers arbitrary URLs | Rebinding unless pinned; easy to miss ranges and encodings |
| IP pinning | Closes TOCTOU | Library-specific; TLS hostname handling is delicate |
| Egress proxy | Uniform, auditable, library-agnostic | Operational dependency; must itself be hardened |
| Network policy | Bypass-proof for app bugs | Coarse; requires infra changes |
| Disabling redirects | Removes redirect pivot | Some legitimate URLs redirect; re-validate per hop instead |

## 8. Security and performance considerations

- Always set connect and read timeouts; slow-loris style responses otherwise pin workers.
- Stream and cap response size; never `response.content` on an untrusted URL without a cap.
- Strip credentials: never forward the user's cookies, `Authorization`, or internal service
  tokens to user-chosen URLs. Use a dedicated client with no default auth.
- `trust_env=False` so environment proxy settings do not silently change routing (unless the
  egress proxy is configured that way deliberately).
- Error messages must be generic; distinguishing "connection refused" from "timeout" enables
  internal port scanning.
- Rate-limit URL-fetch features per user; they are amplification and scanning tools otherwise.
- Validation does DNS lookups — cache negative results briefly, and keep it off hot request
  paths by doing fetches in workers.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| `URLValidator()` as SSRF protection | Accepts `http://169.254.169.254/` | Resolve and check IPs; allow-list hosts |
| String blocklist of `localhost`, `127.0.0.1` | Bypassed by encodings, IPv6, DNS | `ipaddress` checks on resolved addresses |
| Validate hostname, then `requests.get(url)` | DNS rebinding | Pin to the validated IP or use an egress proxy |
| `requests.get(url)` with default redirects | Allowed host redirects inward | Disable redirects; validate each hop |
| Returning fetch errors/body to the user | Port scanning, data exfiltration | Generic errors; return only parsed, needed fields |
| Fetching in the web request with no timeout | Worker exhaustion | Worker + timeouts + caps |
| `redirect(request.GET["next"])` | Open redirect / phishing | `url_has_allowed_host_and_scheme` |
| `host.endswith("example.com")` | `attackerexample.com` passes | Exact match or `== host or endswith("." + domain)` |

## 10. Troubleshooting and validation

- **Legitimate URL rejected:** log the resolved addresses (server-side only) — often an IPv6
  AAAA record in a reserved range or a CDN using shared address space.
- **TLS errors after pinning:** SNI/hostname verification not applied to the pinned connection;
  confirm the transport honours the hostname (`VERIFY` section 5.2) or move to an egress proxy.
- **Works locally, blocked in prod:** network policy or proxy doing its job; make the allow-list
  explicit rather than widening egress.

Test payloads (unit-test `resolve_public_url` with mocked `getaddrinfo`, and integration-test the
full fetch against a local listener):

```python
import pytest
from django.core.exceptions import ValidationError

from integrations.netsafety import resolve_public_url

BLOCKED = [
    "http://127.0.0.1/", "https://localhost/", "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/", "https://[::ffff:10.0.0.1]/", "https://2130706433/", "https://0x7f.1/",
    "file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "https://user:pw@example.com/",
    "https://example.com:6379/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocks_internal_destinations(url):
    with pytest.raises(ValidationError):
        resolve_public_url(url)
```

Also test: a public URL that 302-redirects to `http://127.0.0.1/` is refused; a hostname whose
mocked resolution returns one public and one private address is refused; oversize bodies abort;
`next=//evil.com` and `next=https://evil.com` fall back to `LOGIN_REDIRECT_URL`.

## 11. Related references and skills

- [threat-modeling.md](./threat-modeling.md) — outbound calls as an attack surface.
- [injection-prevention.md](./injection-prevention.md) — XXE as an SSRF vector.
- [file-upload-security.md](./file-upload-security.md) — validating fetched files.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — webhook signing, cloud credential exposure.
- [authentication-security.md](./authentication-security.md) — login redirects.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — SSRF regression tests.
- [django-integrations](../../django-integrations/SKILL.md) — HTTP clients, webhooks, identity providers.

## 12. Official documentation and external references

- `url_has_allowed_host_and_scheme`: https://docs.djangoproject.com/en/5.2/ref/utils/#django.utils.http.url_has_allowed_host_and_scheme (`VERIFY` anchor; function is documented in the security topic and release notes)
- Security — Host header validation: https://docs.djangoproject.com/en/5.2/topics/security/#host-header-validation
- `URLValidator`: https://docs.djangoproject.com/en/5.2/ref/validators/#urlvalidator
- OWASP SSRF Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- OWASP Unvalidated Redirects and Forwards Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Unvalidated_Redirects_and_Forwards_Cheat_Sheet.html
- Python `ipaddress`: https://docs.python.org/3/library/ipaddress.html
- httpx advanced usage (transports, extensions): https://www.python-httpx.org/advanced/
- AWS IMDSv2: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html
- Smokescreen egress proxy (third-party): https://github.com/stripe/smokescreen
- IANA special-purpose address registries: https://www.iana.org/assignments/iana-ipv4-special-registry/
