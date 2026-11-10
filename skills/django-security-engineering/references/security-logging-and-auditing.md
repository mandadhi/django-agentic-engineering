# Security Logging and Auditing

> Scope: which security events a Django application must record, how to structure them,
> authentication and authorisation event capture (auth signals, DRF denials, Django's
> `django.security.*` loggers), redaction of sensitive data, log injection, audit trails for
> business data (append-only, tamper-evident), third-party audit packages, retention and privacy,
> monitoring and alerting rules, and readiness for incident investigation.
> Not covered here: `LOGGING` dictConfig mechanics, handlers and error reporting
> ([django-core logging-exceptions.md](../../django-core/references/logging-exceptions.md));
> responding to an incident ([security-testing-and-incident-response.md](./security-testing-and-incident-response.md));
> secret handling ([secrets-and-cryptography.md](./secrets-and-cryptography.md)); tenant
> scoping of audit data ([multi-tenant-security.md](./multi-tenant-security.md)).

## 1. Purpose and scope

Logs serve two security jobs: **detection** (notice an attack while it happens) and
**investigation** (reconstruct who did what, to which data, when). Django logs request errors
and a few security exceptions out of the box; it does **not** log successful or failed logins,
permission denials, data exports or role changes. Those are application responsibilities. Logs
are also a data store with their own risks — they leak secrets, carry injected content and are
a target for tampering.

OWASP Top 10 2021 A09 "Security Logging and Monitoring Failures" and ASVS V7 / V16 (5.0, VERIFY
chapter numbering) are the reference requirements.

## 2. Core concepts

- **Operational log vs security log vs audit trail.**

  | Kind | Purpose | Store | Retention |
  |---|---|---|---|
  | Operational (app/server logs) | Debugging, errors | Log pipeline | Days–weeks |
  | Security events | Detection, alerting | Log pipeline → SIEM | Months (policy-driven) |
  | Audit trail | Accountability for business actions | DB table / WORM store | Years (legal/regulatory) |

- **Built in vs application.** Built-in: `django.request` (4xx/5xx), `django.security.*`
  (e.g. `django.security.DisallowedHost`, `django.security.SuspiciousOperation` subclasses,
  `django.security.csrf` for CSRF failures — Django ≥ 4.1, earlier via `django.request`; VERIFY),
  admin `LogEntry` for admin-site changes, auth signals. Application: everything else below.
- **Minimum security event set.**
  - Authentication: login success, login failure, logout, lockout, MFA enrol/disable/fail,
    password change/reset request/reset complete, token issue/revoke.
  - Authorisation: 403 denials, out-of-scope 404s on detail routes (sampled), privilege changes
    (role, group, `is_staff`, `is_superuser`), impersonation start/stop.
  - Input/abuse: CSRF failures, `DisallowedHost`, `SuspiciousFileOperation`, throttle (429),
    upload rejections, webhook signature failures.
  - Data: bulk export, report download, deletion, sensitive field reads where required.
  - Configuration: feature flags, security settings changed via admin, API key creation.
- **Event fields (who/what/when/where/outcome):** timestamp (UTC, ISO 8601), event name,
  outcome, actor id (not username/email if avoidable), tenant id, source IP (from trusted proxy),
  user agent (truncated), request id, target type/id, reason code.
- **OWASP Logging Vocabulary** gives stable names, e.g. `authn_login_success:<user>`,
  `authn_login_fail:<user>`, `authn_login_lock:<user>`, `authn_password_change`, `authz_fail`,
  `authz_admin`, `privilege_permissions_changed`, `session_created`, `session_expired`,
  `input_validation_fail`, `malicious_csrf`, `upload_rejected` (VERIFY exact names against the
  cheat sheet). Adopt one vocabulary and keep it stable — alerts depend on it.

## 3. Internal mechanics

- **Auth signals** (`django.contrib.auth.signals`): `user_logged_in(sender, request, user)`
  sent by `login()`; `user_logged_out(sender, request, user)` by `logout()` (user may be `None`);
  `user_login_failed(sender, credentials, request)` by `authenticate()` when every backend
  fails. `credentials` is passed through `_clean_credentials()`, which masks keys matching
  `api|token|key|secret|password|signature` — but the username is still present (VERIFY regex
  in 5.2 source). `request` can be `None` if the caller did not pass it to `authenticate()`.
- **Signals fire only on Django's auth paths.** DRF token/JWT auth classes do not call
  `login()`; a token-authenticated request emits no `user_logged_in`. JWT obtain views in
  simplejwt call `authenticate()` so failures emit `user_login_failed` (VERIFY), success does not
  emit `user_logged_in` unless `UPDATE_LAST_LOGIN` behaviour triggers it (VERIFY).
- **Exception → log path.** `SuspiciousOperation` raised in a view becomes a 400 and is logged
  to `django.security.<ClassName>`. `PermissionDenied` becomes 403 via `django.request` only at
  WARNING if `django.request` logs 4xx (it does for 4xx except 404 — VERIFY). DRF handles
  `PermissionDenied`/`NotAuthenticated` itself and does **not** log them — add logging in the
  exception handler.
- **Error reports.** `AdminEmailHandler` and `django.views.debug` use
  `DEFAULT_EXCEPTION_REPORTER_FILTER` (`SafeExceptionReporterFilter`), which masks settings
  matching `API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE` and variables/POST params
  marked with `@sensitive_variables` / `@sensitive_post_parameters`. Third-party error trackers
  (Sentry, etc.) use **their own** scrubbing, not Django's filter.
- **Log injection.** `logging` writes `%s` arguments verbatim; a username like
  `alice\n2026-01-01 INFO authn_login_success:admin` forges a line in plain-text logs. Django
  5.2.2 (and backports) escapes `request.path` and similar values in `django.utils.log`
  response logging after CVE-2025-48432 (VERIFY versions). Application log calls are not
  covered — structured (JSON) output neutralises newlines by encoding them.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Format | Plain text vs JSON | JSON (python-json-logger or structlog, third-party) — fields are queryable and newlines encoded. |
| Where security events go | Same stream with a `security` logger vs separate sink | Dedicated `security.*` logger namespace routed to the SIEM; same pipeline is fine. |
| Audit trail store | Log pipeline vs DB table vs both | DB table when the product must show history or compliance needs queries; ship a copy to append-only storage for tamper-evidence. |
| Generic history (all field changes) | django-simple-history, django-auditlog, django-reversion (all third-party) | simple-history: full row snapshots per model (storage-heavy, easy diffs). auditlog: JSON diffs in one table. reversion: versioning/restore-oriented. None captures reads, bulk `update()`, or raw SQL. |
| Explicit vs automatic audit | Service-layer `audit.record(...)` vs signals/model hooks | Explicit calls for business actions (intent, reason); automatic history as a safety net. |
| Logging identity | User id vs email | Ids; emails are personal data and change. Map ids to people at investigation time. |
| Sampling | Full vs sampled | Never sample auth, authz-change, admin or audit events; sampling 404s/429s is acceptable. |

## 5. Django implementation patterns

### Request context (request id, actor, tenant, client IP)

```python
# core/logging_context.py
import contextvars, logging, uuid

request_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("request_ctx", default={})

class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_ctx.set({"request_id": rid[:64], "ip": request.META.get("REMOTE_ADDR")})
        try:
            response = self.get_response(request)
        finally:
            request_ctx.reset(token)
        response["X-Request-ID"] = rid[:64]
        return response

class ContextFilter(logging.Filter):
    def filter(self, record):
        for k, v in request_ctx.get().items():
            setattr(record, k, v)
        return True
```

`REMOTE_ADDR` is the client only if the proxy sets it (e.g. gunicorn `--forwarded-allow-ips`
with a proxy-aware middleware) — never parse `X-Forwarded-For` left-most entry blindly; see
[secure-django-configuration.md](./secure-django-configuration.md). Actor/tenant ids are added
after authentication (e.g. in a later middleware or DRF `initial()` override), because DRF
authenticates inside the view.

### Authentication events via signals

```python
# accounts/signals.py  (connect in AppConfig.ready())
import hashlib, logging
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

log = logging.getLogger("security.authn")

def _subject(identifier: str) -> str:
    # Pseudonymise attempted identifiers: correlate attacks without storing typed passwords/emails.
    return hashlib.sha256(identifier.strip().lower().encode()).hexdigest()[:16]

@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    log.info("authn_login_success", extra={"event": "authn_login_success", "user_id": user.pk})

@receiver(user_login_failed)
def on_login_failed(sender, credentials, request, **kwargs):
    ident = str(credentials.get("username", ""))[:254]
    log.warning("authn_login_fail", extra={"event": "authn_login_fail", "subject": _subject(ident)})

@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    log.info("authn_logout", extra={"event": "authn_logout", "user_id": getattr(user, "pk", None)})
```

Users sometimes type their password into the username field — another reason to hash or
truncate attempted identifiers rather than log them raw.

### DRF denials

Log 401/403 in the project exception handler (pattern in [api-security.md](./api-security.md)).
For HTML views, a `handler403` view or middleware inspecting `response.status_code` works.

### Redaction

```python
# core/logging_filters.py
import logging, re

SENSITIVE = re.compile(r"(authorization|cookie|password|token|secret|api[_-]?key|session)", re.I)

class RedactFilter(logging.Filter):
    def filter(self, record):
        for key in list(vars(record)):
            if SENSITIVE.search(key):
                setattr(record, key, "[REDACTED]")
        return True
```

```python
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

@sensitive_post_parameters("password", "card_number")
def checkout(request): ...

@sensitive_variables("api_key")
def call_provider(api_key, payload): ...
```

Never log: `request.POST`/`request.body`, `Authorization`, `Cookie`/`Set-Cookie`, tokens, reset
links, OTP codes, full card/ID numbers, `request.META` dumps. Configure the error tracker's
scrubbing (e.g. Sentry `send_default_pii=False`, `before_send`) separately.

### Audit trail model

```python
# audit/models.py
from django.conf import settings
from django.db import models

class AuditEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                              related_name="+")
    actor_repr = models.CharField(max_length=150)        # survives user deletion/anonymisation
    tenant_id = models.BigIntegerField(null=True, db_index=True)
    action = models.CharField(max_length=64)             # "invoice.void", "user.role_granted"
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64)
    changes = models.JSONField(default=dict)              # minimal diff, no secrets/PII beyond need
    request_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=["target_type", "target_id", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError("AuditEvent is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("AuditEvent is append-only")
```

```python
# audit/services.py
def record(*, actor, action, target, changes=None, tenant_id=None):
    ctx = request_ctx.get()
    AuditEvent.objects.create(
        actor=actor, actor_repr=str(actor.pk) if actor else "system", tenant_id=tenant_id,
        action=action, target_type=target._meta.label_lower, target_id=str(target.pk),
        changes=changes or {}, request_id=ctx.get("request_id", ""), ip=ctx.get("ip"),
    )
```

Call `record()` inside the same `transaction.atomic()` as the business change, so an audited
action and its audit row commit together. Model guards stop ORM mistakes, not a DB user — for
real append-only, `REVOKE UPDATE, DELETE` on the table from the application DB role, or ship
events to WORM/object-lock storage. Hash-chaining (`hash = sha256(prev_hash + canonical_row)`)
makes deletions detectable but needs serialised writes; usually external immutable storage is
the simpler tamper-evidence.

### Admin auditing

`django.contrib.admin.models.LogEntry` records add/change/delete through the admin site only,
with a free-text change message; it misses admin actions that call `queryset.update()`, logins
to the admin, and views. Audit custom admin actions explicitly and alert on new superusers.

## 6. Production use cases

- **Credential stuffing detection**: `authn_login_fail` grouped by subject hash and by IP →
  alert on > N failures/min across many subjects from few IPs, or many IPs for one subject.
- **Privilege escalation trail**: every role/group/`is_staff` change audited with actor and reason;
  alert on any new superuser.
- **Data exfiltration**: export events with row counts; alert on volume anomalies per user.
- **CSP violation reporting**: report endpoint logs (rate-limited, size-capped) for XSS signals —
  see [cors-and-browser-security.md](./cors-and-browser-security.md).
- **Regulated domains**: audit trail of record access for healthcare/finance, retained per policy.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| JSON logs + SIEM | Queryable, alertable, injection-resistant | Pipeline cost; schema discipline needed |
| DB audit table | Product-visible history, transactional with change | Grows fast; DB admin can still tamper |
| WORM/object-lock copy | Tamper-evident | Extra infrastructure; deletion for privacy is harder |
| Third-party history packages | Fast to adopt, complete row history | Miss bulk/raw writes; storage-heavy; capture PII by default |
| Signals for auth events | Zero changes to views | Blind to token/JWT auth paths |

## 8. Security and performance considerations

- Logs are sensitive data: restrict access, encrypt in transit/at rest, apply retention.
- Privacy (GDPR and similar): log ids not names, pseudonymise identifiers, define retention, and
  account for erasure requests (`actor_repr` keeps the trail meaningful after anonymising users).
- Unbounded fields (user agent, filenames, search terms) — truncate to stop log flooding.
- Synchronous network log handlers on the request path add latency and failure modes; log to
  stdout and let the platform ship logs.
- Audit writes add one INSERT per action; history packages can double write volume — index only
  what investigations query.
- Clocks: NTP-synced, UTC everywhere, or timelines across services cannot be reconstructed.
- Retention must outlast detection time — breaches are often found months later.

## 9. Common mistakes and anti-patterns

- No logging of failed logins or 403s — attacks are invisible.
- `logger.info(f"login {request.POST}")` — passwords in logs.
- Logging full JWTs or session ids (replayable credentials).
- Trusting `X-Forwarded-For[0]` as the client IP.
- Plain-text logs with unescaped user input (forged lines).
- Audit rows written outside the business transaction (phantom or missing entries).
- Audit table writable/deletable by the application role, or exposed in admin with delete perms.
- Relying on admin `LogEntry` as the audit trail.
- Alerting on everything — alert fatigue hides real incidents.
- Sentry/APM capturing request bodies and headers with default PII settings.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| No `authn_login_fail` for API logins | Token/JWT path bypasses `authenticate()` signal, or custom view | Log in the obtain-token view / auth class |
| `request` is `None` in `user_login_failed` | `authenticate()` called without `request` | Pass `request=` everywhere |
| CSRF failures not visible | `django.security.csrf` not routed | Add logger to `LOGGING` |
| Every IP is the load balancer | Proxy headers not applied | Configure proxy/server, see configuration reference |
| Secrets appear in error emails/tracker | Filter not applied to third-party tracker | Configure tracker scrubbing; `@sensitive_variables` |

Validation:

- Tests asserting events: `with self.assertLogs("security.authn", "WARNING")` around a failed
  login; assert no password substring in captured output.
- Test that `AuditEvent.save()` on an existing row and `delete()` raise.
- Review a sample of production logs for tokens/emails quarterly; run secret scanning on log
  samples if tooling allows.
- Tabletop: "who downloaded customer X's data last month?" — can the logs answer it?

## 11. Related references and skills

- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — using logs during incidents.
- [authentication-security.md](./authentication-security.md) — events in the auth lifecycle.
- [authorization-and-access-control.md](./authorization-and-access-control.md) — denial semantics.
- [api-security.md](./api-security.md) — DRF exception handler logging.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — keeping secrets out of logs.
- [multi-tenant-security.md](./multi-tenant-security.md) — tenant id in every event.
- [django-core logging-exceptions.md](../../django-core/references/logging-exceptions.md) — `LOGGING` config, handlers.
- [django-architecture service-layer.md](../../django-architecture/references/service-layer.md) — where explicit audit calls live.

## 12. Official documentation and external references

- Django logging: https://docs.djangoproject.com/en/5.2/topics/logging/
- Django security loggers: https://docs.djangoproject.com/en/5.2/ref/logging/#django-security
- Filtering error reports: https://docs.djangoproject.com/en/5.2/howto/error-reporting/#filtering-error-reports
- Auth signals: https://docs.djangoproject.com/en/5.2/ref/contrib/auth/#module-django.contrib.auth.signals
- Django security releases: https://docs.djangoproject.com/en/dev/releases/security/
- OWASP Logging Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- OWASP Logging Vocabulary Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Vocabulary_Cheat_Sheet.html
- OWASP Top 10 A09:2021: https://owasp.org/Top10/A09_2021-Security_Logging_and_Monitoring_Failures/
- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- NIST SP 800-92 Log Management: https://csrc.nist.gov/pubs/sp/800/92/final
- django-auditlog (third-party): https://django-auditlog.readthedocs.io/
- django-simple-history (third-party): https://django-simple-history.readthedocs.io/
