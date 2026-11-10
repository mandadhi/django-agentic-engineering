# Secrets and Cryptography

> Scope: secret inventory and storage; environment variables and their limits; fail-fast
> loading; rotation (including `SECRET_KEY` and `SECRET_KEY_FALLBACKS`); what `SECRET_KEY`
> actually protects; `django.core.signing`; hashing vs HMAC vs encryption; symmetric,
> envelope and asymmetric encryption; key management; secure token generation and storage;
> secure randomness; sensitive data protection.
> Not covered here: settings-module mechanics ([django-core settings-configuration](../../django-core/references/settings-configuration.md));
> password hashers and validators ([authentication-security.md](./authentication-security.md));
> session engines and cookie flags ([session-and-cookie-security.md](./session-and-cookie-security.md));
> JWT design and API token lifecycles ([api-security.md](./api-security.md)); log redaction
> ([security-logging-and-auditing.md](./security-logging-and-auditing.md)); secret scanning in
> CI ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md));
> database encryption at rest ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

Secrets are the credentials that turn "read the code" into "own the system": `SECRET_KEY`, database
and cache passwords, cloud keys, payment and email provider keys, OAuth client secrets, webhook
signing keys, encryption keys. Cryptography is how the application uses keys to prove integrity
(signing), keep data confidential (encryption) and verify knowledge without storing it (hashing).

Django provides: password hashing, `django.core.signing` (HMAC-based integrity with timestamps),
`django.utils.crypto` (`constant_time_compare`, `get_random_string`, `salted_hmac`), and
`SECRET_KEY_FALLBACKS` for rotation. Django does **not** provide: a secret store, encryption of
model fields, key management, or rotation of any secret other than `SECRET_KEY`. Those are
infrastructure and application responsibilities.

## 2. Core concepts

| Primitive | Property | Reversible? | Django/Python tool |
|---|---|---|---|
| Password hash (slow, salted) | Verify a low-entropy secret | No | `make_password` / `check_password` (hashers) |
| Fast hash (SHA-256) | Fingerprint a **high-entropy** value (API token) | No | `hashlib.sha256` |
| HMAC | Integrity + authenticity with a shared key | No (it's a tag) | `hmac`, `salted_hmac`, `Signer` |
| Signing (Django) | Tamper-evident, **readable** payload | Payload is plaintext/base64 | `django.core.signing` |
| Symmetric AEAD encryption | Confidentiality + integrity | Yes, with key | `cryptography` (third-party): Fernet, AES-GCM |
| Asymmetric signature | Third parties verify without the private key | No | `cryptography`: Ed25519, RSA-PSS |
| Envelope encryption | Data keys encrypted by a KMS master key | Yes | Cloud KMS SDK + `cryptography` |

Rules that prevent most design errors:

- **Hash** what you only need to verify (passwords, API tokens, reset codes).
- **Sign** what the client may read but must not change (unsubscribe links, signed IDs).
- **Encrypt** only what the server must read back in plaintext later (third-party OAuth refresh
  tokens, bank details) — and accept that encryption makes key management your problem.
- Randomness for security comes from `secrets` (OS CSPRNG), never `random`.

## 3. Internal mechanics

### What `SECRET_KEY` protects

| Uses `SECRET_KEY` | Effect of changing it without fallbacks |
|---|---|
| Session data encoding (`SessionBase.encode` signs session payloads; `signed_cookies` engine stores the whole session in a signed cookie) | Existing sessions become invalid |
| Session auth hash (`get_session_auth_hash`, HMAC of the password hash) | All users logged out |
| `PasswordResetTokenGenerator` | Outstanding reset links invalid |
| Messages `CookieStorage` | Pending flash messages dropped |
| `django.core.signing` default key (`Signer`, `dumps`, `get_signed_cookie`) | Signed values fail to verify |
| Third-party packages using `salted_hmac` or signing | Package-specific |

It does **not** protect stored password hashes (those use per-password salts) and is not an
encryption key. Using it as one couples unrelated systems to one rotation cycle — derive or use
separate keys.

### Rotation with `SECRET_KEY_FALLBACKS` (Django ≥ 4.1)

New signatures always use `SECRET_KEY`; verification tries `SECRET_KEY` then each fallback. The
session auth hash also checks fallbacks so rotation does not log everyone out (`VERIFY`: fallback
support for session auth hashes landed in a 4.1.x/4.2 patch release). Remove old keys after the
longest-lived artefact expires (session age, `PASSWORD_RESET_TIMEOUT` — 3 days by default,
signed-URL `max_age`). Fallbacks cost one extra HMAC per failed verification.

### `django.core.signing`

- `Signer(key=None, *, sep=":", salt=None, algorithm="sha256", fallback_keys=None)` — keyword-only
  arguments other than `key` (positional use deprecated in 4.2, removed in 5.1).
- `TimestampSigner` adds a timestamp; `unsign(value, max_age=...)` raises `SignatureExpired`.
- `signing.dumps(obj, salt=..., compress=False)` / `signing.loads(s, salt=..., max_age=...)` —
  JSON-serialised, URL-safe base64, HMAC-SHA256 with a key derived from `SECRET_KEY` and salt.
- `Signer.sign_object()` / `unsign_object()` (Django ≥ 3.1).
- Failures raise `BadSignature` (`SignatureExpired` subclasses it).
- The **salt is a namespace**, not a secret: different salts make a signature valid for exactly one
  purpose. Without it, a value signed for "unsubscribe" might be accepted by "confirm email".

### `django.utils.crypto`

- `constant_time_compare(a, b)` — wraps `hmac.compare_digest`.
- `get_random_string(length, allowed_chars=...)` — `secrets`-based; `length` required (Django ≥ 4.0).
- `salted_hmac(key_salt, value, secret=None, *, algorithm="sha1")` — callers should pass
  `algorithm="sha256"`.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Secret delivery | Env vars, mounted files (K8s Secrets / Docker secrets), secret manager SDK at startup, sidecar/agent | Secret manager as source of truth; deliver as files or env at runtime; SDK fetch at startup when rotation must not require redeploy |
| Env vars | Simple, 12-factor | Leak via `/proc/<pid>/environ`, child processes, crash reporters, debug pages, `printenv`/`env` in CI logs; no access audit. Acceptable for low-sensitivity values or when the platform injects them from a manager |
| Field encryption | App-level (AEAD in Python), DB-level (pgcrypto), disk/TDE only | App-level for specific high-value fields; disk/TDE always; pgcrypto puts keys in SQL logs — avoid |
| Key location | Same DB, settings/env, KMS | Never next to the ciphertext; KMS for master keys, data keys encrypted with it |
| Token storage | Plaintext, fast hash, password hash | Fast hash (SHA-256) for ≥ 128-bit random tokens; password hashing only for human-chosen secrets |
| Webhook signing | Shared-secret HMAC, asymmetric | HMAC per customer for simplicity; Ed25519 when many verifiers must not be able to forge |
| Signed vs encrypted URL params | `signing.dumps`, encryption | Signing unless the payload itself is confidential (then don't put it in a URL at all) |

## 5. Django implementation patterns

### 5.1 Fail-fast secret loading

```python
# config/settings/base.py
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def read_secret(name: str, *, required: bool = True) -> str | None:
    """Prefer a mounted file (NAME_FILE), fall back to the env var; never a default value."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    value = os.environ.get(name)
    if required and not value:
        raise ImproperlyConfigured(f"Missing required secret: {name}")
    return value


SECRET_KEY = read_secret("DJANGO_SECRET_KEY")
SECRET_KEY_FALLBACKS = [
    k for k in (read_secret("DJANGO_SECRET_KEY_PREVIOUS", required=False) or "").split(",") if k
]
```

Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(50))"` or
`django.core.management.utils.get_random_secret_key()`. Never ship a fallback literal such as
`os.environ.get("SECRET_KEY", "dev-key")` in shared settings — the default becomes production's
key the day the variable is missing. Use a separate dev settings module instead.

### 5.2 Purpose-bound signed links

```python
# accounts/links.py
from django.core import signing

UNSUBSCRIBE_SALT = "accounts.unsubscribe.v1"
MAX_AGE = 60 * 60 * 24 * 30


def make_unsubscribe_token(user_id: int, list_id: int) -> str:
    return signing.dumps({"u": user_id, "l": list_id}, salt=UNSUBSCRIBE_SALT)


def read_unsubscribe_token(token: str) -> dict:
    try:
        return signing.loads(token, salt=UNSUBSCRIBE_SALT, max_age=MAX_AGE)
    except signing.SignatureExpired:
        raise LinkExpired
    except signing.BadSignature:
        raise LinkInvalid
```

The payload is readable base64 — do not put email addresses or anything confidential in it.
Signed tokens are replayable until `max_age`; for single-use semantics, store a nonce or a
"used" flag, or bind the token to mutable state (as `PasswordResetTokenGenerator` does with the
password hash and last login).

### 5.3 API tokens: random, prefixed, hashed

```python
# apikeys/services.py
import hashlib
import secrets

from django.utils.crypto import constant_time_compare

from .models import ApiKey

PREFIX = "sk_live_"


def issue_api_key(*, owner, scopes: list[str]) -> str:
    raw = PREFIX + secrets.token_urlsafe(32)           # 256 bits of entropy
    ApiKey.objects.create(
        owner=owner,
        lookup=raw[: len(PREFIX) + 8],                 # non-secret index for lookup/display
        digest=hashlib.sha256(raw.encode()).hexdigest(),
        scopes=scopes,
    )
    return raw                                         # shown once; never stored or logged


def verify_api_key(raw: str) -> ApiKey | None:
    key = (ApiKey.objects.select_related("owner")
           .filter(lookup=raw[: len(PREFIX) + 8], revoked_at__isnull=True).first())
    if key is None:
        return None
    candidate = hashlib.sha256(raw.encode()).hexdigest()
    return key if constant_time_compare(candidate, key.digest) else None
```

A fast hash is correct here because the token is high-entropy; a slow password hash would add
latency to every API call for no gain. A recognisable prefix lets secret scanners detect leaks.
DRF's built-in `authtoken` stores keys in plaintext — see [api-security.md](./api-security.md).

### 5.4 Symmetric field encryption with key rotation

```python
# core/crypto.py  — requires `cryptography` (third-party)
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings

# FIELD_ENCRYPTION_KEYS: newest first, each from Fernet.generate_key(), loaded from a secret store.
_fernet = MultiFernet([Fernet(k) for k in settings.FIELD_ENCRYPTION_KEYS])


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def rotate(token: str) -> str:
    return _fernet.rotate(token.encode()).decode()  # re-encrypt under the newest key
```

Fernet is AES-128-CBC + HMAC-SHA256 with a timestamp — authenticated and hard to misuse. For
AES-256 or associated data (binding ciphertext to a row id so it cannot be swapped between rows),
use `cryptography.hazmat.primitives.ciphers.aead.AESGCM` with a random 96-bit nonce per message
and a key id prefix. Rotation is a data migration: add the new key first, run `rotate()` in
batches, then remove the old key.

Encrypted columns cannot be filtered, sorted or uniquely indexed; if you must look a value up,
store a separate keyed HMAC (blind index) of the normalised value. Third-party field packages
exist (e.g. django-cryptography, django-fernet-encrypted-fields); several older ones are
unmaintained — check activity and Django 5.2 support before adopting (`VERIFY`).

### 5.5 Envelope encryption with a KMS

1. Request a data key from KMS (`GenerateDataKey`) → plaintext key + KMS-encrypted key.
2. Encrypt the data locally with AES-GCM using the plaintext key; discard it from memory.
3. Store ciphertext + encrypted data key + key id.
4. To decrypt: KMS `Decrypt` the data key (IAM-controlled, audited), then decrypt locally.

Master keys never leave the KMS; access is logged; rotating the master key does not require
re-encrypting data. Cache decrypted data keys briefly to control KMS latency and cost.

### 5.6 Signing outgoing webhooks

```python
import hashlib
import hmac
import time


def sign_webhook(secret: bytes, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Signature-Timestamp": ts, "X-Signature": f"v1={sig}"}
```

Receivers recompute over the **raw** body, compare with `hmac.compare_digest`, and reject
timestamps older than ~5 minutes to limit replay. For incoming webhooks, the same check runs on
`request.body` before parsing. Asymmetric alternative: sign with an Ed25519 private key
(`cryptography`), publish the public key; receivers can verify but never forge.

### 5.7 JWT signing keys (brief)

If you issue JWTs, the signing key is a secret with its own rotation (`kid` header, JWKS), pinned
algorithms (never accept `none` or let the token choose), and short lifetimes (RFC 8725). Design
and library choice are owned by [api-security.md](./api-security.md).

## 6. Production use cases

- **Planned `SECRET_KEY` rotation:** deploy new key with old in `SECRET_KEY_FALLBACKS` → wait
  `max(SESSION_COOKIE_AGE, PASSWORD_RESET_TIMEOUT, longest signing max_age)` → remove fallback.
- **Emergency rotation after leak:** replace the key **without** fallbacks (accept mass logout),
  flush sessions, rotate every other credential that shared the exposure path, review logs
  ([security-testing-and-incident-response.md](./security-testing-and-incident-response.md)).
- **Database credentials:** dynamic or dual credentials (two users, swap via config, revoke old);
  `CONN_MAX_AGE` connections keep old credentials until recycled — restart or let them age out.
- **Storing customers' third-party OAuth refresh tokens:** AES-GCM with row-id associated data,
  keys in KMS, decrypt only in the worker that calls the provider.
- **Secure random for codes:** `secrets.randbelow(10**6)` formatted to 6 digits for email OTPs,
  stored hashed with expiry and attempt counter ([authentication-security.md](./authentication-security.md)).

## 7. Advantages and disadvantages

| Technique | Advantage | Disadvantage |
|---|---|---|
| Secret manager | Central, audited, rotatable | Availability dependency at startup; IAM design needed |
| Env vars | Universal, simple | Broad exposure, no audit, rotation needs restart |
| Mounted secret files | Not inherited by child env; can update in place | Must handle reload; file permissions matter |
| `django.core.signing` | Built-in, key-rotation aware, simple | Not confidential; replayable within `max_age` |
| App-level field encryption | Protects against DB dump/backup leaks | No querying; key management; performance cost |
| Hashed tokens | DB leak doesn't leak usable tokens | Tokens can't be shown again; lost = reissue |
| Asymmetric signatures | Verifiers can't forge | Larger signatures; key distribution |

## 8. Security and performance considerations

- Keep separate keys per purpose (session signing, field encryption, webhook signing, JWT);
  compromise or rotation of one must not affect the others.
- Secrets must never appear in: the repository (including history), Docker image layers and
  build args, `settings` dumps, debug pages (`DEBUG=False`), error reports (configure
  `sensitive_variables` / `sensitive_post_parameters` and a filtered exception reporter —
  [security-logging-and-auditing.md](./security-logging-and-auditing.md)), URLs (query strings
  end up in proxy logs and `Referer`), frontend bundles.
- Grant secret access per service identity (least privilege) and log reads in the manager.
- Compare secrets and MACs only with `constant_time_compare` / `hmac.compare_digest`.
- `random`, `uuid1`, timestamps and `hash()` are not unpredictable; `uuid4` is random but use
  `secrets` for credentials to make intent explicit and entropy measurable.
- Never invent algorithms or modes: no ECB, no unauthenticated CBC, no reused GCM nonces, no
  MD5/SHA-1 for security decisions, no custom key derivation.
- Data minimisation is the cheapest protection: do not collect, log, cache or back up sensitive
  data you do not need; apply retention and deletion to backups too.
- Slow hashing and KMS calls cost latency; keep them off per-request hot paths (fast hash for
  API tokens, cached data keys).

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| `SECRET_KEY = os.environ.get("SECRET_KEY", "changeme")` | Known key in prod → forged sessions/reset tokens | Fail fast; no default |
| Same `SECRET_KEY` across environments | Staging leak compromises production | Unique per environment |
| Using `SECRET_KEY` as an encryption key | Rotation breaks decryption | Dedicated encryption keys |
| `signing.dumps(data)` with no salt, reused everywhere | Cross-purpose token reuse | Unique salt per purpose and version |
| Treating signed data as secret | PII exposed in URLs | Store server-side, sign only an id |
| Encryption key in the same DB/table as ciphertext | DB dump decrypts everything | KMS / secret store |
| API tokens stored plaintext | DB read = account takeover | SHA-256 digest + prefix lookup |
| `token == provided` | Timing side channel | `constant_time_compare` |
| `random.choice` for codes | Predictable tokens | `secrets` |
| Secrets passed as Docker `ARG`/`ENV` at build | Persist in image history | Runtime injection or BuildKit secrets |
| Rotating `SECRET_KEY` without fallbacks during routine rotation | Mass logout, broken links | `SECRET_KEY_FALLBACKS` window |

## 10. Troubleshooting and validation

- **Everyone logged out after deploy:** `SECRET_KEY` changed without fallbacks, or instances run
  different keys (check that all replicas read the same secret version).
- **`BadSignature` on links that worked yesterday:** salt changed, key rotated past fallbacks, or
  the value was altered by an email client (line wrapping) — use URL-safe tokens in paths.
- **`InvalidToken` from Fernet:** wrong/rotated-out key or truncated column (ciphertext is longer
  than plaintext — size columns for base64 overhead).
- **Secret visible in error report:** add `sensitive_variables` to the function and confirm the
  reporter filter is active in production.

Validation:

- `python manage.py check --deploy` flags weak or default-looking `SECRET_KEY` (`security.W009`).
- Secret scanning (gitleaks/trufflehog — third-party) on the repo and images
  ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md)).
- Tests: tampered and expired signed tokens rejected; token from one salt rejected by another;
  API key digest stored, raw key never persisted; rotation test with old key in fallbacks still
  verifies and new signatures use the new key.

```python
import pytest
from django.core import signing
from django.test import override_settings


def test_signed_value_survives_rotation():
    with override_settings(SECRET_KEY="old-" * 10):
        token = signing.dumps({"u": 1}, salt="x")
    with override_settings(SECRET_KEY="new-" * 10, SECRET_KEY_FALLBACKS=["old-" * 10]):
        assert signing.loads(token, salt="x") == {"u": 1}
    with override_settings(SECRET_KEY="new-" * 10, SECRET_KEY_FALLBACKS=[]):
        with pytest.raises(signing.BadSignature):
            signing.loads(token, salt="x")
```

## 11. Related references and skills

- [authentication-security.md](./authentication-security.md) — password hashing, reset tokens, OTPs.
- [session-and-cookie-security.md](./session-and-cookie-security.md) — signed cookies, session invalidation.
- [api-security.md](./api-security.md) — token and JWT design.
- [secure-django-configuration.md](./secure-django-configuration.md) — `SECRET_KEY` in production settings.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — redaction, `sensitive_variables`.
- [dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md) — secret scanning, image secrets.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — leak response.
- [django-core settings-configuration](../../django-core/references/settings-configuration.md) · [django-integrations](../../django-integrations/SKILL.md) · [postgresql-engineering](../../postgresql-engineering/SKILL.md)

## 12. Official documentation and external references

- Cryptographic signing: https://docs.djangoproject.com/en/5.2/topics/signing/
- `SECRET_KEY` / `SECRET_KEY_FALLBACKS`: https://docs.djangoproject.com/en/5.2/ref/settings/#secret-key
- Deployment checklist — secret key: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/#secret-key
- `django.utils.crypto`: https://docs.djangoproject.com/en/5.2/ref/utils/#module-django.utils.crypto (`VERIFY` anchor)
- Filtering sensitive information: https://docs.djangoproject.com/en/5.2/howto/error-reporting/#filtering-sensitive-information
- Python `secrets`: https://docs.python.org/3/library/secrets.html · `hmac`: https://docs.python.org/3/library/hmac.html
- pyca/cryptography (third-party): https://cryptography.io/en/latest/ (Fernet, AEAD, Ed25519)
- OWASP Secrets Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- OWASP Cryptographic Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html
- OWASP Key Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html
- RFC 2104 (HMAC): https://www.rfc-editor.org/rfc/rfc2104 · RFC 8725 (JWT BCP): https://www.rfc-editor.org/rfc/rfc8725
