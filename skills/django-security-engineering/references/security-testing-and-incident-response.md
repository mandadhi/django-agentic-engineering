# Security Testing and Incident Response

> Scope: proving Django security controls with automated tests — policy unit tests, endpoint
> permission matrices, CSRF, authentication, upload, injection/XSS, SSRF and header tests,
> "every URL is protected" checks, `check --deploy` in CI, SAST/DAST/pentest positioning and
> security regression tests — and responding to incidents: phases, Django-specific containment
> playbooks (leaked `SECRET_KEY`, compromised accounts, leaked DB credentials, compromised
> dependency), exposure assessment, notification and postmortems.
> Not covered here: test architecture, factories and CI layout ([django-testing](../../django-testing/SKILL.md));
> DRF test client mechanics ([drf-testing-and-debugging.md](../../django-rest-framework/references/drf-testing-and-debugging.md));
> dependency scanning tools and gates ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md));
> what each control is and how to implement it (the owning reference in this skill);
> logging and audit design ([security-logging-and-auditing.md](./security-logging-and-auditing.md)).

## 1. Purpose and scope

A security control that is not tested will be removed by a refactor. Happy-path tests prove the
feature works; security tests prove the **attack fails** — the other user gets 404, the forged
POST gets 403, the payload renders escaped. This reference gives an agent a repeatable test
catalogue and, when prevention fails, a Django-specific incident playbook.

Tools mentioned (bandit, semgrep, OWASP ZAP, pip-audit) are external; this reference describes
how to use them, it does not run them. Code examples were not executed.

## 2. Core concepts

- **Security test pyramid.**

  | Layer | What it proves | Example |
  |---|---|---|
  | Unit (policy) | Authorisation logic, validators, sanitisers, URL checks | `can_edit(user, invoice)` truth table |
  | Integration (endpoint) | Controls wired at the boundary | Other-tenant GET → 404 |
  | Configuration | Production settings and headers | `check --deploy`, header assertions |
  | Static analysis (SAST) | Dangerous code patterns | bandit/semgrep in CI |
  | Dynamic (DAST) | Running app behaviour | ZAP baseline against staging |
  | Manual (pentest, review) | Business logic, chained issues | Scoped engagement before major release |

- **Negative tests first.** For each protected resource: anonymous, authenticated-wrong-user,
  wrong-role, other-tenant, and correct-user. Expected statuses are part of the contract
  (302/401, 404, 403, 404, 200).
- **Security regression test.** Every fixed vulnerability gets a test reproducing the original
  exploit, named after the finding/ticket, that must never be deleted silently.
- **Incident response phases** (NIST SP 800-61 Rev. 2 lifecycle): Preparation → Detection &
  Analysis → Containment, Eradication & Recovery → Post-incident activity. Rev. 3 (2025) reframes
  these around NIST CSF 2.0 functions (Govern, Identify, Protect, Detect, Respond, Recover) —
  VERIFY current revision and mapping.

## 3. Internal mechanics

- **Django test client skips CSRF by default.** `django.test.Client` sets
  `request._dont_enforce_csrf_checks = True`; tests pass even if CSRF is broken. Use
  `Client(enforce_csrf_checks=True)`; DRF's `APIClient(enforce_csrf_checks=True)` does the same
  for `SessionAuthentication`.
- **`force_login` / `force_authenticate`** bypass authentication backends, MFA and lockout —
  fine for authorisation tests, wrong for authentication tests (use real `client.post(login_url)`).
- **Test settings differ from production.** Fast hashers (`MD5PasswordHasher`), `DEBUG` forced
  `False` by the test runner, `ALLOWED_HOSTS` includes `testserver`, locmem caches (throttles
  reset per test but may leak between tests in one process). Configuration tests must load the
  production settings module.
- **`check --deploy`** runs the `security` check tag (`security.W001`…), e.g. missing
  `SecurityMiddleware`, HSTS, `SECURE_SSL_REDIRECT`, insecure `SECRET_KEY`, `DEBUG`,
  `ALLOWED_HOSTS`, cookie flags. `--fail-level WARNING` makes warnings fail CI. It checks
  settings, not what the proxy actually serves.
- **SECRET_KEY dependencies** (for incident response): sessions' auth hash
  (`get_session_auth_hash` → `salted_hmac` with `SECRET_KEY`), signed-cookie session backend,
  `PasswordResetTokenGenerator`, `django.core.signing` (incl. `signing.dumps`, `TimestampSigner`,
  `get_signed_cookie`), messages cookie storage, and third-party users (simplejwt default
  `SIGNING_KEY`). `SECRET_KEY_FALLBACKS` (Django ≥ 4.1) keeps old signatures valid during
  rotation.
- **Password change invalidates sessions.** `SessionMiddleware` + `AuthenticationMiddleware`
  verify the stored session auth hash on each request; after `set_password()` + `save()`, every
  other session for that user fails verification and is flushed (the current one survives only
  if `update_session_auth_hash()` is called).

## 4. Engineering decisions and trade-offs

| Decision | Guidance |
|---|---|
| Permission matrix granularity | One parametrised test per resource × action × role beats ad-hoc tests; generate cases from a declarative table. |
| Where to test authorisation | Policy unit tests for logic; endpoint tests to prove it is wired; both are needed. |
| SAST rule set | bandit for Python patterns (`B602` shell, `B301` pickle, `B703`/`B308` mark_safe); semgrep Django/DRF rules for framework misuse (`csrf_exempt`, raw SQL, `extra`). Triage once; baseline the rest; fail CI on new findings. |
| DAST | ZAP baseline (passive) on every staging deploy; authenticated active scans on a schedule against a non-production environment only. |
| Pentest | Before launch, major auth/tenancy changes, or annually; give testers roles and test tenants; fix → regression test. |
| Incident severity | Pre-define levels by data class and blast radius so containment decisions are not debated during the incident. |
| Rotating `SECRET_KEY` with or without fallback | With fallback for routine rotation; **without** fallback when the key leaked — invalidation is the point. |

## 5. Django implementation patterns

### Permission matrix (pytest + pytest-django, third-party)

```python
# tests/security/test_invoice_access.py
import pytest
from django.urls import reverse

CASES = [
    # actor,          method,   route,             expected
    ("anonymous",     "get",    "invoice-detail",  401),
    ("other_tenant",  "get",    "invoice-detail",  404),
    ("same_tenant_viewer", "delete", "invoice-detail", 403),
    ("owner",         "get",    "invoice-detail",  200),
    ("other_tenant",  "post",   "invoice-void",    404),
]

@pytest.mark.django_db
@pytest.mark.parametrize("actor,method,route,expected", CASES)
def test_invoice_access_matrix(api_client_for, invoice, actor, method, route, expected):
    client = api_client_for(actor, tenant=invoice.account)
    url = reverse(route, kwargs={"pk": invoice.pk})
    response = getattr(client, method)(url)
    assert response.status_code == expected
```

`api_client_for` is a fixture returning an `APIClient` with `force_authenticate` for the actor
(or none for anonymous). For `unittest`-style suites, use `self.subTest(actor=...)` in a loop.

### Mass assignment

```python
def test_cannot_escalate_or_move_tenant(api_client_for, invoice, other_account):
    client = api_client_for("owner", tenant=invoice.account)
    client.patch(reverse("invoice-detail", kwargs={"pk": invoice.pk}),
                 {"account": other_account.pk, "status": "paid", "total": "0.00"}, format="json")
    invoice.refresh_from_db()
    assert invoice.account_id != other_account.pk and invoice.status != "paid"
```

### CSRF is enforced

```python
from django.test import Client

def test_transfer_requires_csrf(db, user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post(reverse("transfer"), {"amount": "10"})
    assert response.status_code == 403
```

Add one test per `csrf_exempt` view asserting its alternative control (signature, token auth).

### Authentication behaviour

```python
def test_login_does_not_enumerate(client, user):
    known = client.post(reverse("login"), {"username": user.username, "password": "wrong"})
    unknown = client.post(reverse("login"), {"username": "nobody@example.com", "password": "wrong"})
    assert known.status_code == unknown.status_code
    assert known.content == unknown.content.replace(b"nobody@example.com", user.username.encode())

def test_session_rotates_on_login(client, user):
    client.get(reverse("login"))
    before = client.cookies.get("sessionid")
    client.post(reverse("login"), {"username": user.username, "password": "correct-horse"})
    assert client.cookies["sessionid"].value != getattr(before, "value", None)

def test_reset_token_expires(user, settings, freezer):   # freezer: pytest-freezer (third-party)
    from django.contrib.auth.tokens import default_token_generator
    token = default_token_generator.make_token(user)
    freezer.tick(settings.PASSWORD_RESET_TIMEOUT + 1)
    assert not default_token_generator.check_token(user, token)
```

Also test lockout/throttle (N failures → 429 or locked response, and that the lock does not
reveal account existence) and that password change invalidates other sessions.

### Uploads

```python
from django.core.files.uploadedfile import SimpleUploadedFile

def test_rejects_spoofed_image(api_client_for):
    fake = SimpleUploadedFile("avatar.png", b"<?php echo 1; ?>", content_type="image/png")
    response = api_client_for("owner").post(reverse("avatar"), {"file": fake}, format="multipart")
    assert response.status_code == 400

def test_rejects_svg_and_oversized(api_client_for, settings):
    svg = SimpleUploadedFile("x.svg", b"<svg onload='alert(1)'/>", content_type="image/svg+xml")
    big = SimpleUploadedFile("x.png", b"\x89PNG" + b"0" * (settings.MAX_AVATAR_BYTES + 1),
                             content_type="image/png")
    for f in (svg, big):
        assert api_client_for("owner").post(reverse("avatar"), {"file": f},
                                            format="multipart").status_code == 400
```

Assert stored names are server-generated and that a `../../` filename never escapes the
storage root — see [file-upload-security.md](./file-upload-security.md).

### Injection and XSS regressions

```python
PAYLOAD = '"><script>alert(1)</script>'

def test_comment_is_escaped(client, post_factory):
    post = post_factory(body=PAYLOAD)
    html = client.get(post.get_absolute_url()).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

def test_sort_param_is_allow_listed(api_client_for):
    r = api_client_for("owner").get("/api/invoices/?ordering=password")
    assert r.status_code == 200   # ignored, not applied — assert default order in body
```

### SSRF guard

```python
from unittest import mock
import socket

def test_blocks_private_resolution():
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
    with mock.patch("socket.getaddrinfo", return_value=fake):
        with pytest.raises(UnsafeURLError):
            fetch_preview("https://attacker.example/")
```

### Every URL requires authentication

```python
from django.urls import get_resolver, URLPattern, URLResolver

PUBLIC = {"login", "password_reset", "health", "webhook-stripe"}

def iter_patterns(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from iter_patterns(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p

def test_no_unexpected_public_views():
    offenders = []
    for route, pattern in iter_patterns(get_resolver().url_patterns):
        view = pattern.callback
        cls = getattr(view, "cls", None) or getattr(view, "view_class", None)
        public = getattr(view, "login_required", True) is False   # Django ≥ 5.1 login_not_required
        if cls is not None and hasattr(cls, "permission_classes"):
            public = any(pc.__name__ == "AllowAny" for pc in cls.permission_classes)
        if public and pattern.name not in PUBLIC:
            offenders.append(route)
    assert offenders == []
```

The attribute set by `login_not_required` is `login_required = False` (VERIFY in 5.2 source);
ViewSets with per-action `get_permissions()` need an explicit allow-list or request-based test.

### Configuration and headers

```bash
DJANGO_SETTINGS_MODULE=config.settings.production \
  SECRET_KEY=ci-only-not-secret-$(openssl rand -hex 32) \
  python manage.py check --deploy --fail-level WARNING
```

```python
@override_settings(SECURE_SSL_REDIRECT=False)
def test_security_headers(client):
    r = client.get("/", secure=True)
    assert r["X-Content-Type-Options"] == "nosniff"
    assert r["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in r
    assert r["Strict-Transport-Security"].startswith("max-age=")
```

Also curl the deployed staging URL in a post-deploy job — proxies and CDNs can strip or
duplicate headers that Django sets.

### SAST/DAST in CI (sketch)

```yaml
security:
  steps:
    - run: bandit -r src -ll -x tests
    - run: semgrep ci --config p/django --config p/python
    - run: pip-audit -r requirements.lock --strict
    # post-deploy to staging:
    - run: zap-baseline.py -t https://staging.example.com -c zap-rules.conf
```

Rule packs and flags are tool-version-specific — VERIFY against the tools' docs.

## 6. Production use cases

### Incident playbooks (Django-specific containment)

**Leaked `SECRET_KEY`**
1. Generate a new key; deploy **without** the old key in `SECRET_KEY_FALLBACKS`.
2. Effects: all sessions invalid (users re-login), password-reset links and signed values
   invalid, signed-cookie sessions/messages invalid, simplejwt tokens invalid if signed with it.
3. Assess what the attacker could forge while it was valid: sessions for any user (if they also
   knew user password hashes / session auth hash inputs), reset tokens, signed URLs, pickled
   data if any legacy pickle serializer was used (remote code execution risk).
4. Rotate any other secret stored in the same place (env file, CI variable).

**Compromised user or admin account**
1. Lock: `user.is_active = False` or `set_unusable_password()`; save.
2. Kill sessions: password change invalidates them via the session auth hash; for an explicit
   purge with the DB backend, iterate `Session.objects.filter(expire_date__gte=now())` and delete
   those whose decoded `_auth_user_id` matches (no per-user index exists by default — consider
   tracking session keys per user, e.g. django-user-sessions, third-party).
3. Revoke tokens: `Token.objects.filter(user=user).delete()` (authtoken), knox
   `AuthToken.objects.filter(user=user).delete()`, simplejwt blacklist outstanding refresh tokens.
4. Reset MFA devices; review audit trail for actions taken; restore changed data.

**Mass session invalidation** (e.g. session hijacking campaign): `Session.objects.all().delete()`
for DB sessions, flush the session cache alias for cache sessions, or rotate `SECRET_KEY` (also
breaks signed data — choose deliberately).

**Leaked database credentials**: rotate the DB password/role, revoke the old one at the server,
check `pg_stat_activity`/DB audit logs for foreign connections, restrict network access,
assume data read → exposure assessment.

**Compromised dependency** (malicious release or critical CVE): pin to a known-good version,
rebuild images from clean lockfiles, rotate every secret the build/runtime could read, check for
persistence (new users, tokens, cron, webhooks) — see
[dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md).

### Exposure assessment

Use audit events and access logs to answer: which records, which tenants, which time window,
which actors/tokens. Request ids and actor ids from
[security-logging-and-auditing.md](./security-logging-and-auditing.md) make this tractable;
without them, assume worst case.

### Notification

Personal-data breaches may require notifying a supervisory authority (GDPR Art. 33: within 72
hours of awareness, where the breach is likely to result in risk) and affected people (Art. 34);
other regimes (state laws, HIPAA, PCI DSS, contracts) differ — VERIFY with legal for the
jurisdiction. Engineering supplies facts; legal/DPO decides.

### Post-incident

Blameless postmortem: timeline, root cause, why detection took as long as it did, regression
test for the exploit, control improvements, and updated playbooks.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Permission matrix tests | Cheap, exhaustive, catch IDOR regressions | Must be kept in sync with new actions |
| `enforce_csrf_checks` tests | Catch accidental exemptions | Need explicit setup per view |
| `check --deploy` in CI | Fast, built in | Settings only; no runtime/proxy view |
| SAST | Finds dangerous patterns early | False positives; misses logic flaws |
| DAST | Tests the deployed stack | Slow, needs auth setup; limited logic coverage |
| Pentest | Finds chained/business-logic issues | Point-in-time, costly |

## 8. Security and performance considerations

- Never run active DAST or pentests against production without written authorisation and a
  rollback plan; use production-like staging with synthetic data.
- Test data must not be real personal data; secrets in CI are test-only.
- Permission matrices grow as resources × actions × roles — parametrise, use `--reuse-db` and
  transactional tests to keep the suite fast.
- Security tests that hit the network (DNS, external URLs) must be mocked to stay deterministic.
- Incident tooling (scripts to revoke tokens, purge sessions, rotate keys) should exist and be
  tested before an incident; writing them under pressure causes outages.

## 9. Common mistakes and anti-patterns

- Testing only the happy path; no "other user" or "other tenant" cases.
- Default test `Client` used for CSRF tests (always passes).
- `force_login` in authentication tests (skips lockout, MFA, backends).
- Asserting `!= 200` instead of the exact denial status (500 passes too).
- `check --deploy` run against development settings.
- SAST findings suppressed with blanket `# nosec`/`nosemgrep` without a reason.
- Deleting a failing security regression test to unblock a release.
- Rotating a leaked `SECRET_KEY` but keeping it in `SECRET_KEY_FALLBACKS`.
- Disabling a compromised user without revoking API tokens.
- No pre-agreed severity levels or contact list — incident starts with a meeting.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| CSRF test passes though exemption exists | Client not enforcing | `enforce_csrf_checks=True` |
| Auth tests pass, prod lockout fails | Throttle cache per process / different cache in tests | Use shared cache in staging; integration test with real cache |
| Header test passes, prod header missing | Proxy/CDN strips it, or response from static server | Post-deploy header check on real URL |
| `check --deploy` W008 behind TLS proxy | `SECURE_SSL_REDIRECT` off because proxy redirects | Silence with justification via `SILENCED_SYSTEM_CHECKS` |
| Sessions survive password reset | Custom auth backend/user lacks `get_session_auth_hash` | Use `AbstractBaseUser`; confirm session verification |

Pre-release security review checklist: matrix tests for new endpoints; new `csrf_exempt`,
`AllowAny`, `mark_safe`, raw SQL, `subprocess` reviewed; serializer field diffs; schema diff;
`check --deploy`; dependency audit; SAST clean or triaged; playbooks current.

## 11. Related references and skills

- [authorization-and-access-control.md](./authorization-and-access-control.md) · [api-security.md](./api-security.md) — what matrices verify.
- [authentication-security.md](./authentication-security.md) · [session-and-cookie-security.md](./session-and-cookie-security.md)
- [csrf-protection.md](./csrf-protection.md) · [cross-site-scripting.md](./cross-site-scripting.md) · [injection-prevention.md](./injection-prevention.md)
- [file-upload-security.md](./file-upload-security.md) · [ssrf-prevention.md](./ssrf-prevention.md)
- [secure-django-configuration.md](./secure-django-configuration.md) — `check --deploy` warnings.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — key rotation details.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) · [dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md)
- [django-testing](../../django-testing/SKILL.md) — fixtures, factories, CI structure.
- [drf-testing-and-debugging.md](../../django-rest-framework/references/drf-testing-and-debugging.md) — `APIClient`, `APIRequestFactory`.

## 12. Official documentation and external references

- Django testing tools: https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- Testing CSRF (`enforce_csrf_checks`): https://docs.djangoproject.com/en/5.2/ref/csrf/#testing
- Deployment checklist / `check --deploy`: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
- System check framework (security checks): https://docs.djangoproject.com/en/5.2/ref/checks/#security
- `SECRET_KEY_FALLBACKS`: https://docs.djangoproject.com/en/5.2/ref/settings/#secret-key-fallbacks
- Session invalidation on password change: https://docs.djangoproject.com/en/5.2/topics/auth/default/#session-invalidation-on-password-change
- DRF testing: https://www.django-rest-framework.org/api-guide/testing/
- OWASP Web Security Testing Guide: https://owasp.org/www-project-web-security-testing-guide/
- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- NIST SP 800-61 Rev. 3: https://csrc.nist.gov/pubs/sp/800/61/r3/final
- GDPR Art. 33: https://gdpr-info.eu/art-33-gdpr/
- Bandit: https://bandit.readthedocs.io/ · Semgrep: https://semgrep.dev/docs/ · OWASP ZAP: https://www.zaproxy.org/docs/
