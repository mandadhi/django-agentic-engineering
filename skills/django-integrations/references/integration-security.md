# Integration Security

**Scope:** The security controls specific to Django's communication with external systems — a consolidated integration threat model, outbound TLS and mTLS, credential protection, SSRF at integration points, XML and deserialization attacks on partner payloads, request signing, payload validation, redaction and PII, audit logging, least privilege and dependency risk of SDKs. General application security (settings, auth, CSRF, XSS, comprehensive SSRF and secret management) is owned by [django-security-engineering](../../django-security-engineering/SKILL.md); this reference summarises and links rather than repeating it.

## 1. Purpose and scope

Integrations are trust boundaries in both directions: Django sends credentials and data to systems it does not control, and accepts data and callbacks from them. Integration-specific failure modes include a leaked partner API key, a webhook endpoint that accepts forged events, an XML parser that reads `/etc/passwd`, an "import from URL" feature used to reach cloud metadata, or full customer payloads copied into logs and APM.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Libraries referenced: `httpx` [third-party: httpx], `defusedxml` [third-party: defusedxml], `lxml` [third-party: lxml], `pydantic` [third-party: pydantic], `cryptography` [third-party: cryptography], `pip-audit` [third-party: pip-audit].

Use this reference to review an integration design or code change. The protocol-level authentication details live in [authentication-and-identity-integrations](authentication-and-identity-integrations.md); webhook verification mechanics in [webhooks-and-inbound-integrations](webhooks-and-inbound-integrations.md).

## 2. Core concepts

### Integration threat model (STRIDE applied to integration boundaries)

| Boundary | Threat | Example | Primary control |
|---|---|---|---|
| Outbound HTTP | Spoofing (of provider) | MITM, DNS hijack, disabled TLS verify | TLS verification, hostname pinning in config, mTLS |
| Outbound HTTP | Information disclosure | Over-sharing PII in requests; tokens in URLs | Data minimisation, header auth, redaction |
| Outbound to user-supplied URL | SSRF (elevation) | Webhook subscription to `169.254.169.254` | Allow-list / private-IP block after resolution, egress proxy |
| Inbound webhook | Spoofing, tampering, replay | Forged "payment succeeded" | HMAC/asymmetric signature over raw body, timestamp window, dedupe |
| Inbound payload | Tampering, DoS | XXE, billion laughs, huge JSON, pickle | Safe parsers, size limits, schema validation |
| Broker messages | Spoofing, tampering | Any producer can publish to a shared queue | Broker auth per producer, message signing for cross-org, schema validation |
| Credentials at rest | Disclosure | Secrets in repo, task args, logs | Secret manager, runtime injection, scrubbing |
| Provider compromise | Tampering | Malicious data from a breached partner | Treat partner data as untrusted input; validate; least privilege scopes |
| SDK/dependency | Tampering | Typosquatted SDK, vulnerable XML lib | Pinning with hashes, audit, minimal SDK footprint |
| Files from partners | Elevation | Path traversal in SFTP filenames, macro docs | Server-generated names, type validation, scanning |

### Principles

1. **Partner data is untrusted input**, even from an authenticated, contracted partner. Authentication proves *who sent it*, not that it is safe or correct.
2. **Authenticate every inbound channel** — signature, mTLS, or token. IP allow-lists are a secondary layer (cloud provider IP ranges change and are shared).
3. **Every outbound credential is scoped, per-environment, rotatable and audited.**
4. **Minimise data sent and stored**: send only fields the provider needs; store only what you must reconcile.
5. **Fail closed**: verification errors reject; unknown event types are recorded and ignored, never executed by default.

## 3. Internal mechanics

### TLS verification in Python clients

`httpx` and `requests` verify certificates and hostnames by default using `certifi`'s bundle [third-party: certifi] (httpx may use the system trust store via `truststore` in newer versions `VERIFY`). Verification is lost by:
- `verify=False` (often left from debugging, or set via env like `REQUESTS_CA_BUNDLE=""`),
- custom `SSLContext` with `check_hostname=False` / `CERT_NONE`,
- corporate TLS inspection proxies requiring a private CA — add the CA to a bundle, never disable verification.

### Signing and verification

- **HMAC (shared secret)**: integrity + authenticity between two parties; both can forge, so not non-repudiation. Compare with `hmac.compare_digest`.
- **Asymmetric signatures** (Ed25519, ECDSA, RSA-PSS): provider signs, you verify with a public key; needed when many consumers verify or non-repudiation matters.
- **Canonicalisation**: signature must cover exact bytes. Re-serialising JSON changes bytes (key order, whitespace, unicode escapes) → verify `request.body` before parsing.
- **HTTP Message Signatures (RFC 9421)** standardise signing request components (method, path, headers, `Content-Digest` per RFC 9530); adoption is provider-specific.

### XML attack mechanics

- **XXE**: `<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>` makes the parser read local files or perform SSRF.
- **Billion laughs / quadratic blowup**: nested entity expansion exhausts memory.
- **XSLT / XInclude**: remote fetches and code paths in lxml if enabled.
Python's `xml.etree` does not resolve external entities by default in modern versions but is still vulnerable to some expansion attacks depending on expat version (`VERIFY` against Python's XML vulnerabilities table). `defusedxml` blocks these classes; `lxml` needs `XMLParser(resolve_entities=False, no_network=True, huge_tree=False)` and newer lxml versions changed defaults (`VERIFY` for lxml ≥ 5).

### Deserialization

`pickle`, `marshal`, `yaml.load` (unsafe loader), `jsonpickle`, and Celery's pickle serializer execute code on load. Any byte stream crossing a trust boundary must be parsed with a data-only format (JSON, `yaml.safe_load`, protobuf/Avro with schemas).

## 4. Engineering decisions and trade-offs

| Decision | Recommended | Trade-off |
|---|---|---|
| Inbound authentication | Provider signature (HMAC/asymmetric) + timestamp | IP allow-list alone is brittle; mTLS strongest but costly to operate |
| Outbound credential | Workload identity > cert/assertion > secret | Platform lock-in vs secret rotation burden |
| User-supplied URLs (webhook targets, imports) | Egress proxy with deny-private + app-level validation | Proxy is infrastructure work but survives DNS rebinding |
| Secret delivery | Secret manager → env/file at start, refreshed on rotation | Restart-on-rotate vs runtime refetch complexity |
| PII to providers | Tokenise/pseudonymise; send IDs not profiles | Some providers (email, payments) need real data — contract + DPA |
| Payload logging | Structured metadata only; sampled redacted bodies behind a flag | Debuggability vs breach surface |
| Partner file intake | Quarantine bucket → scan → validate → promote | Latency and cost of scanning |
| Message signing on internal brokers | Rely on broker auth/TLS internally; sign for cross-org | Signing adds key management |

## 5. Django implementation patterns

### Secure outbound client factory (enforced defaults)

```python
# apps/integrations/http.py (security-relevant parts)
import logging
import ssl
import httpx

log = logging.getLogger("integrations.http")
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}

def _log_request(request: httpx.Request) -> None:
    log.info("outbound.request", extra={
        "method": request.method, "host": request.url.host, "path": request.url.path,  # no query string
        "headers": {k: ("***" if k.lower() in SENSITIVE_HEADERS else v) for k, v in request.headers.items()},
    })

def build_client(name: str, *, base_url: str, timeout: httpx.Timeout, auth: httpx.Auth | None = None,
                 verify: ssl.SSLContext | bool = True) -> httpx.Client:
    if verify is False:
        raise ValueError("TLS verification may not be disabled; supply a CA bundle instead")
    if not base_url.startswith("https://"):
        raise ValueError(f"{name}: integrations must use https")
    return httpx.Client(
        base_url=base_url, timeout=timeout, auth=auth, verify=verify,
        follow_redirects=False,                 # redirects can leave the allow-listed host
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        headers={"User-Agent": f"acme-platform/{name}"},
        event_hooks={"request": [_log_request]},
    )
```

Allow `http://` only via an explicit setting for local stubs in development.

### SSRF guard for user-supplied URLs (summary)

The full treatment — DNS rebinding, IPv6/IPv4-mapped addresses, redirects, egress proxies — is in [ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md). Integration rules:

- Validate at **subscription time and at delivery time** (DNS changes).
- Resolve, check every address is public (`ipaddress.ip_address(a).is_global`), then connect to that resolved IP with the original Host/SNI — or route through an egress proxy that enforces it.
- `follow_redirects=False`; cap response size; short timeouts; scheme `https` only; deny ports other than 443 unless contractually needed.

```python
import ipaddress, socket
from urllib.parse import urlsplit

class UnsafeDestination(ValueError): ...

def assert_public_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise UnsafeDestination("https URL without credentials required")
    for *_, sockaddr in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise UnsafeDestination("destination resolves to a non-public address")
```

This check alone is TOCTOU-vulnerable (rebinding between check and connect); pair it with an egress proxy or IP-pinned connection for features where arbitrary customers control URLs.

### Safe XML parsing

```python
from defusedxml import ElementTree as SafeET          # [third-party: defusedxml]
from lxml import etree                                # [third-party: lxml]

def parse_partner_xml(body: bytes, max_bytes: int = 5_000_000):
    if len(body) > max_bytes:
        raise ValueError("payload too large")
    return SafeET.fromstring(body, forbid_dtd=True)

SAFE_LXML = etree.XMLParser(resolve_entities=False, no_network=True, dtd_validation=False,
                            load_dtd=False, huge_tree=False)
```

SOAP stacks (zeep) use lxml; check their parser settings in [soap-and-legacy-integrations](soap-and-legacy-integrations.md).

### Payload validation at the anti-corruption layer

```python
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field   # [third-party: pydantic]

class PartnerOrder(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    order_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9-]+$")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
```

Validation errors raise `ContractViolationError`; never `**payload` into a model or `Model.objects.create(**data)` (mass assignment of `is_staff`, `tenant_id`, …).

### Redaction for logs, errors and traces

```python
import re
REDACT_KEYS = {"password", "secret", "token", "access_token", "refresh_token", "card_number",
               "cvv", "iban", "ssn", "authorization", "api_key"}
EMAIL = re.compile(r"[^@\s]+@[^@\s]+")

def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return EMAIL.sub("<email>", obj)
    return obj
```

Apply in logging filters, Sentry `before_send`, OpenTelemetry span attribute processors, and when persisting `WebhookEvent.payload` if the provider sends card or health data you must not store. See [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).

### Credential handling rules

- Load via settings from env/secret files (`/run/secrets/...`, Key Vault/Secrets Manager); see [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md).
- Never pass secrets as Celery task arguments (stored in broker/result backend, shown in Flower); pass a credential *name* and resolve inside the task.
- Exclude secrets from `repr()` of client objects and dataclasses (`field(repr=False)`).
- Rotation: support overlapping old/new values (`ACME_WEBHOOK_SECRETS = [new, old]`), deploy new, switch provider, remove old.
- Per-environment and per-integration credentials; production keys never in staging.

### Audit logging of integration actions

Record who/what initiated security-relevant outbound actions (refunds, payouts, data exports, permission grants at a partner) with actor, integration, operation, external reference, outcome, correlation id — in an append-only audit table or log stream, separate from debug logs.

### Least privilege at providers

- Scopes/roles: request the minimum (`contacts.read`, not `full_access`); separate read and write clients if the provider supports it.
- Cloud: IAM role per workload, resource-level conditions (bucket prefix, queue ARN), no wildcard actions.
- Database access for partners: never; expose an API or file drop.

## 6. Production use cases

- **Payment webhook forging attempt**: attacker posts `payment_intent.succeeded`; signature check fails → 400, metric `webhook.signature_invalid` alerts at threshold, nothing stored.
- **Customer-configured webhooks (SaaS)**: subscriptions validated, deliveries through an egress proxy that blocks RFC 1918, link-local and metadata ranges.
- **Partner XML catalogue import**: defusedxml parse, XSD validation, 50 MB cap, staged in quarantine bucket.
- **Secret leak in a public repo**: provider key revoked within minutes via runbook; overlapping-key support means no downtime.
- **APM capturing request bodies by default**: instrumentation configured to drop bodies and query strings for integration hosts.

## 7. Advantages and disadvantages

| Control | Advantage | Disadvantage |
|---|---|---|
| Central client factory | One place to enforce TLS, redirects, redaction | Teams bypass it with raw `requests` unless linted |
| Egress proxy | Network-level SSRF and exfiltration control | Infra cost, extra hop, proxy becomes critical path |
| Schema validation at the boundary | Rejects malformed/malicious data early | Must evolve with provider versions; strictness can break on additive changes |
| Asymmetric webhook signatures | Non-repudiation, no shared secret | Key distribution, slower verification |
| Payload redaction | Limits breach impact | Harder debugging; requires maintained key lists |

## 8. Security and performance considerations

- TLS 1.2 minimum; prefer 1.3. Connection pooling keeps TLS handshake cost amortised — reuse clients ([http-and-rest-api-integrations](http-and-rest-api-integrations.md)).
- Body size limits at the proxy (`client_max_body_size`) and Django (`DATA_UPLOAD_MAX_MEMORY_SIZE`); bound response reads from partners (`stream` + byte cap).
- Signature verification is cheap; schema validation of large payloads is not — validate in the worker for big files, in the request for small webhooks.
- Rate-limit inbound integration endpoints to resist floods with invalid signatures (DRF throttles or edge).
- Regex in validators must be linear-time to avoid ReDoS from partner input.
- Dependency security: provider SDKs pull large transitive trees; pin with hashes, run `pip-audit` in CI, prefer plain HTTP for small API surfaces. See [dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md).
- PII: data processing agreements, residency of provider regions, retention of stored payloads (prune `WebhookEvent` bodies after N days).

## 9. Common mistakes and anti-patterns

- `verify=False` to "fix" a certificate error, or a global `ssl._create_unverified_context`.
- Comparing signatures with `==`; verifying a re-serialised JSON body instead of raw bytes.
- `xml.dom.minidom`/`lxml.etree.fromstring` on partner XML with default parser and DTDs enabled.
- Celery `accept_content=["pickle"]` with a broker reachable by other systems.
- `requests.get(user_url)` in a "fetch avatar from URL" or "test webhook" feature.
- Logging `response.text` on errors (contains PII or tokens echoed back by providers).
- API keys in query strings, task args, exception messages, or `settings.py` literals.
- One "god" credential with admin scope shared by all environments and integrations.
- Trusting `X-Forwarded-For`-based IP allow-lists without controlling the proxy chain.
- Executing handler code chosen by an untrusted `type` field via `getattr`/`importlib` (use an explicit registry).

## 10. Troubleshooting and validation

| Check | How |
|---|---|
| TLS verification never disabled | `rg "verify\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False"` in CI |
| No raw HTTP calls outside clients | Lint rule / `rg "requests\.(get|post)|httpx\.(get|post)"` outside `integrations/` |
| Unsafe XML/deserialisation | `rg "pickle\.loads|yaml\.load\(|etree\.fromstring|minidom"`; bandit [third-party: bandit] |
| Secrets not in code | Secret scanning (gitleaks/trufflehog) in CI |
| Webhook forgery rejected | Tests with bad signature, stale timestamp, modified body |
| SSRF blocked | Tests with `127.0.0.1`, `[::1]`, `169.254.169.254`, `10.0.0.1`, decimal IP, redirect-to-private |
| Logs free of secrets/PII | Sample production logs; grep for `Bearer `, `@`, card patterns |
| Cert expiry | Alert on mTLS client cert and partner cert expiry ≥ 30 days ahead |

Certificate errors: `CERTIFICATE_VERIFY_FAILED` → missing intermediate on the server or private CA not in bundle; diagnose with `openssl s_client -showcerts -connect host:443 -servername host`.

## 11. Related references and skills

- [authentication-and-identity-integrations](authentication-and-identity-integrations.md) — OAuth/OIDC/JWT/mTLS mechanics.
- [webhooks-and-inbound-integrations](webhooks-and-inbound-integrations.md) — signature verification, replay protection.
- [soap-and-legacy-integrations](soap-and-legacy-integrations.md) — WS-Security, XML parser configuration.
- [file-transfer-and-object-storage](file-transfer-and-object-storage.md) — SFTP host keys, presigned URL scoping.
- [integration-observability-and-operations](integration-observability-and-operations.md) — redacted structured logs, audit events.
- django-security-engineering: [threat-modeling](../../django-security-engineering/references/threat-modeling.md), [ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md), [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md), [injection-prevention](../../django-security-engineering/references/injection-prevention.md), [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md), [dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md), [file-upload-security](../../django-security-engineering/references/file-upload-security.md).
- django-testing: [security-testing](../../django-testing/references/security-testing.md).

## 12. Official documentation and external references

- Django security overview — https://docs.djangoproject.com/en/5.2/topics/security/
- Python `ssl` — https://docs.python.org/3/library/ssl.html ; `hmac` — https://docs.python.org/3/library/hmac.html
- Python XML vulnerabilities — https://docs.python.org/3/library/xml.html#xml-vulnerabilities
- Python `pickle` warning — https://docs.python.org/3/library/pickle.html
- defusedxml — https://github.com/tiran/defusedxml
- lxml parser options — https://lxml.de/parsing.html
- HTTPX SSL — https://www.python-httpx.org/advanced/ssl/
- RFC 8446 TLS 1.3 — https://www.rfc-editor.org/rfc/rfc8446
- RFC 9421 HTTP Message Signatures — https://www.rfc-editor.org/rfc/rfc9421 ; RFC 9530 Digest Fields — https://www.rfc-editor.org/rfc/rfc9530
- OWASP SSRF Prevention Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- OWASP XXE Prevention Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html
- OWASP Deserialization Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html
- OWASP API Security Top 10 (2023) — https://owasp.org/API-Security/editions/2023/en/0x00-header/
- Celery security — https://docs.celeryq.dev/en/stable/userguide/security.html
- pip-audit — https://github.com/pypa/pip-audit
