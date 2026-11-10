# Security Fundamentals for Django Applications

> Scope: security principles applied to Django; the split between what Django protects by
> default and what the application must implement; layered enforcement (edge → middleware →
> view → service → database); secure architecture patterns; OWASP Top 10 and ASVS mapping to
> this skill's references; Django's security release policy and supported versions; reporting
> vulnerabilities.
> Not covered here: identifying threats for a specific system
> ([threat-modeling.md](./threat-modeling.md)); production settings
> ([secure-django-configuration.md](./secure-django-configuration.md)); access-control
> implementation ([authorization-and-access-control.md](./authorization-and-access-control.md));
> dependency management ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md));
> middleware and request-lifecycle internals ([django-core](../../django-core/SKILL.md)).

## 1. Purpose and scope

Django ships strong defaults: CSRF middleware, context-aware template auto-escaping,
parameterised ORM queries, host-header validation, clickjacking protection and salted, slow
password hashing. Most Django vulnerabilities in real systems are therefore not framework bugs
but **application decisions**: a missing ownership check, a `csrf_exempt`, a `mark_safe` on user
data, an f-string in `raw()`, an upload served from the app origin, an unvalidated outbound URL.

This reference gives the agent the mental model for all other references: which protections
exist, which ones the code can silently turn off, which ones Django cannot provide at all, and
at which architectural layer each control belongs.

## 2. Core concepts

**Principles, in Django terms**

| Principle | Meaning | Django expression |
|---|---|---|
| Least privilege | Grant the minimum access needed | Narrow permissions/groups; DB role without DDL at runtime; scoped API tokens; `is_staff` only for admin users |
| Defence in depth | Several independent controls per threat | Scoped queryset **and** permission check **and** DB FK/constraint; CSP **and** auto-escaping |
| Fail closed | Errors deny, never allow | Default `IsAuthenticated`; `LoginRequiredMiddleware` (Django ≥ 5.1); a permission function returns `False` on unexpected state |
| Complete mediation | Every access is checked, every time | Checks in the service/selector used by views, APIs, tasks, admin actions — not only in the template |
| Secure defaults | The safe path is the easy path | Project base classes/mixins that are already scoped; opt-outs are explicit and greppable |
| Minimise attack surface | Fewer entry points, fewer features exposed | Unused admin, debug toolbars, browsable API, `@action`s and HTTP methods removed in production |
| Separation of duties | No single actor completes sensitive flows alone | Approvals for refunds/role grants; audit of admin actions |
| Don't trust the client | All request data is attacker-controlled | Headers (`Host`, `X-Forwarded-*`), cookies, hidden fields, JSON body, file names and MIME types |
| Economy of mechanism | Simple controls are reviewable | One authorisation vocabulary (`for_user()`), one error format |

**Framework protection vs developer responsibility**

| Area | Django provides (built-in) | Developer must |
|---|---|---|
| CSRF | `CsrfViewMiddleware`, `{% csrf_token %}`, origin/referer checks | Keep middleware on; send token from JS; never `csrf_exempt` cookie-authenticated views; configure `CSRF_TRUSTED_ORIGINS` → [csrf-protection.md](./csrf-protection.md) |
| XSS | HTML auto-escaping in DTL and Jinja2 (when `autoescape` enabled) | Avoid `mark_safe`/`|safe` on user data; escape in JS/URL/CSS contexts; sanitise rich text; add CSP → [cross-site-scripting.md](./cross-site-scripting.md) |
| SQL injection | Parameterised ORM queries | Parameterise `raw()`/`cursor.execute`/`RawSQL`; allow-list identifiers and `order_by` fields → [injection-prevention.md](./injection-prevention.md) |
| Clickjacking | `XFrameOptionsMiddleware`, default `DENY` (Django ≥ 3.0) | Keep it; use CSP `frame-ancestors` where embedding is needed → [cors-and-browser-security.md](./cors-and-browser-security.md) |
| Host header | `ALLOWED_HOSTS` validation in `request.get_host()` | Set an explicit list; don't use `USE_X_FORWARDED_HOST` unless the proxy sets it → [secure-django-configuration.md](./secure-django-configuration.md) |
| Passwords | Salted adaptive hashers (PBKDF2 default; Argon2/scrypt available), validators | Enable validators; choose hasher; rate-limit login; MFA → [authentication-security.md](./authentication-security.md) |
| Sessions | Signed session id, rotation on `login()`, flush on `logout()` | Secure cookie flags, expiry policy, invalidation on password change → [session-and-cookie-security.md](./session-and-cookie-security.md) |
| Authorisation | Permission model, `has_perm`, `PermissionRequiredMixin`, DRF permission classes | **Everything object-level**: ownership, tenant scoping, IDOR prevention → [authorization-and-access-control.md](./authorization-and-access-control.md) |
| SSRF | Nothing | Validate/allow-list outbound URLs, block private ranges, control redirects → [ssrf-prevention.md](./ssrf-prevention.md) |
| Upload content | Size limits for request data, storage path sanitisation | Type/content validation, private storage, safe serving, scanning → [file-upload-security.md](./file-upload-security.md) |
| Rate limiting | Nothing in Django; DRF throttles (cache-based, best-effort) | Edge limits + app limits for login/OTP/reset → [api-security.md](./api-security.md) |
| CSP | Nothing built in before Django 6.0 (third-party django-csp); built-in CSP middleware in Django ≥ 6.0 (`VERIFY` exact setting names) | Define and deploy a policy → [cors-and-browser-security.md](./cors-and-browser-security.md) |
| HTTPS/HSTS | `SecurityMiddleware` settings | Turn them on; configure the proxy correctly → [secure-django-configuration.md](./secure-django-configuration.md) |
| Secrets | `SECRET_KEY`, `SECRET_KEY_FALLBACKS` (Django ≥ 4.1), signing | Store outside the repo, rotate, scope → [secrets-and-cryptography.md](./secrets-and-cryptography.md) |
| Tenant isolation | Nothing | Scoped managers, task/cache/file isolation → [multi-tenant-security.md](./multi-tenant-security.md) |
| Audit logging | `django.security.*` loggers, auth signals, admin `LogEntry` | Define events, redact, retain, alert → [security-logging-and-auditing.md](./security-logging-and-auditing.md) |

The rule: **a default protection is only as good as the code paths that cannot bypass it.**
Every opt-out API (`csrf_exempt`, `mark_safe`, `extra`, `RawSQL`, `AllowAny`, `@login_not_required`)
is a review trigger.

## 3. Internal mechanics

**Where Django's defaults execute** (order from the default `MIDDLEWARE` in a new 5.2 project):

1. `SecurityMiddleware` — HTTPS redirect, HSTS, `nosniff`, Referrer-Policy, COOP headers.
2. `SessionMiddleware` — loads the session lazily from the cookie.
3. `CommonMiddleware` — URL normalisation (`APPEND_SLASH`); `DISALLOWED_USER_AGENTS`.
4. `CsrfViewMiddleware` — rejects unsafe methods without a valid token/origin, in `process_view`.
5. `AuthenticationMiddleware` — sets lazy `request.user`.
6. `MessageMiddleware`.
7. `XFrameOptionsMiddleware` — sets `X-Frame-Options` on responses.

Host validation is not middleware: `request.get_host()` raises `DisallowedHost` (→ 400) when the
host is not in `ALLOWED_HOSTS`; it is called by `CommonMiddleware`, CSRF checks and URL building.
Auto-escaping happens at render time: variables are escaped unless they are `SafeString`.
ORM parameterisation happens in the SQL compiler: values become driver parameters, never
interpolated text. Order and internals are owned by [django-core middleware](../../django-core/references/middleware.md)
and [request lifecycle](../../django-core/references/request-response-lifecycle.md).

**What Django cannot know**: which user may see which row, whether a URL points to an internal
service, whether a PNG is actually a polyglot, whether a request rate is abusive, or whether a
field should be visible to the client. These require domain knowledge, hence application code.

## 4. Engineering decisions and trade-offs

**Layered enforcement map** — put each control at the most central layer that has the needed
context.

| Layer | Owns | Examples | Limit |
|---|---|---|---|
| Edge (CDN/WAF/LB/proxy) | Volume, TLS, coarse filtering | TLS termination, request size caps, IP rate limits, bot rules | No identity or domain context |
| Django middleware | Global, request-wide concerns | Security headers, CSRF, session, host checks, tenant resolution, login-required default | No object context |
| View / DRF permission | Endpoint-level policy | `IsAuthenticated`, role checks, `has_object_permission` | Easy to forget on a new view |
| Service / selector | Business rules and data scoping | `Invoice.objects.for_user(user)`, `approve_refund(actor, refund)` | Must be the only path to data |
| Database | Invariants that must always hold | FKs, `UniqueConstraint`, `CheckConstraint`, DB role privileges, PostgreSQL RLS | Coarse; errors are not user-friendly |
| Browser | Damage limitation | CSP, SameSite, HttpOnly, framing | Advisory; attacker-controlled clients ignore it |

Trade-offs:

- **Central vs local**: middleware and base classes are hard to forget but blunt; per-view
  decorators are precise but easy to omit. Combine: default-deny globally, opt in precisely.
- **Defence in depth vs duplication**: duplicate *enforcement* (queryset scope + permission),
  never duplicate *policy definition* — one function decides "can X do Y to Z".
- **Strictness vs usability**: lockout policies, short sessions and strict CSP all cost UX;
  choose per asset sensitivity (ASVS level), and document the decision.
- **Built-in vs third-party**: prefer built-ins; a package adds supply-chain risk
  ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md)) but is
  better than hand-rolled crypto, sanitisers or MFA.

**ASVS levels** (OWASP Application Security Verification Standard): L1 — baseline for all
applications; L2 — most applications handling sensitive or business data (the sensible default
target for B2B SaaS); L3 — high-value, high-assurance systems. ASVS 5.0 (2025) reorganised the
chapters (`VERIFY` chapter numbering before citing requirement ids).

## 5. Django implementation patterns

**Default-deny for HTML views** (Django ≥ 5.1):

```python
# settings.py
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",  # Django >= 5.1
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
```

```python
# views.py — public views opt out explicitly and visibly
from django.contrib.auth.decorators import login_not_required

@login_not_required
def pricing(request):
    ...
```

**Default-deny for APIs (DRF)**:

```python
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
```

**Selector as the access choke point** — authorisation and retrieval in one step, reused by
views, APIs, tasks and admin actions (architecture: [service-layer](../../django-architecture/references/service-layer.md);
managers: [managers-and-querysets](../../django-orm/references/managers-and-querysets.md)):

```python
# billing/selectors.py
from django.db.models import QuerySet
from .models import Invoice

def invoices_visible_to(user) -> QuerySet[Invoice]:
    if not user.is_authenticated:
        return Invoice.objects.none()
    if user.has_perm("billing.view_all_invoices"):
        return Invoice.objects.filter(organization=user.organization)
    return Invoice.objects.filter(organization=user.organization, owner=user)
```

```python
# billing/views.py
from django.shortcuts import get_object_or_404, render
from .selectors import invoices_visible_to

def invoice_detail(request, pk: int):
    invoice = get_object_or_404(invoices_visible_to(request.user), pk=pk)  # 404, not 403
    return render(request, "billing/invoice_detail.html", {"invoice": invoice})
```

**Policy function for writes** — one place answers "may this actor do this":

```python
# billing/policies.py
def can_refund(actor, invoice) -> bool:
    return (
        actor.is_active
        and actor.organization_id == invoice.organization_id
        and actor.has_perm("billing.refund_invoice")
        and invoice.status == invoice.Status.PAID
    )
```

```python
# billing/services.py
from django.core.exceptions import PermissionDenied
from django.db import transaction

@transaction.atomic
def refund_invoice(*, actor, invoice):
    if not can_refund(actor, invoice):
        raise PermissionDenied
    ...
```

**Making opt-outs greppable**: CI can fail on new occurrences of `csrf_exempt`, `mark_safe`,
`.extra(`, `RawSQL(`, `shell=True`, `pickle.loads`, `AllowAny`, `login_not_required` unless
annotated with a justification comment (e.g. `# security: reviewed <ticket>`).

## 6. Production use cases

- **New SaaS product**: target ASVS L2; default-deny middleware + DRF settings; selectors per
  app; tenancy decision recorded in an ADR; threat model before first external user.
- **Legacy codebase review**: grep the opt-out list, inventory endpoints
  ([threat-modeling.md](./threat-modeling.md)), check every detail/update view for scoped
  lookups, run `check --deploy`, audit dependencies.
- **Adding a public API to an HTML app**: separate auth model (tokens) from browser sessions,
  re-check CSRF/CORS assumptions, apply throttles ([api-security.md](./api-security.md)).
- **Compliance-driven work (SOC 2, ISO 27001, HIPAA)**: map controls to ASVS requirements; audit
  logging and secret rotation become first-class deliverables.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Relying on Django defaults | Strong baseline at zero cost | Silent opt-outs; nothing for authorisation, SSRF, uploads, rate limiting |
| Central choke points (selectors/policies) | One place to review and test; all entry points inherit | Requires discipline: direct `Model.objects` use bypasses it |
| Global default-deny | New endpoints are safe by default | Public endpoints need explicit marking; health checks/webhooks must be exempted deliberately |
| Database-level enforcement (constraints, RLS) | Holds even for buggy code paths and shell access | Operational complexity; coarse errors; RLS needs per-connection context |
| Browser controls (CSP, SameSite) | Contain damage from missed bugs | Not a primary control; inconsistent across clients |

## 8. Security and performance considerations

- Security controls have cost: password hashing (~100s of ms by design), per-request permission
  queries, CSP nonces disabling full-page caching. Measure; never weaken a hasher on the
  request path — instead avoid hashing in loops.
- Scoped querysets usually *improve* performance (smaller result sets) when indexed on the
  scoping columns (`organization_id`, `owner_id`).
- Permission caching: `ModelBackend` caches permissions on the user object per request; a
  long-lived process caching across requests risks stale grants after revocation.
- Error handling must be both safe and observable: generic responses to clients, full detail in
  server logs with redaction ([security-logging-and-auditing.md](./security-logging-and-auditing.md)).
- **Supported versions**: Django provides security fixes for the current feature release, the
  previous feature release, and LTS releases. Django 5.2 is LTS with security support until
  April 2028 (`VERIFY` against the download page roadmap). Running an unsupported version is a
  vulnerability in itself.
- Security releases are pre-announced to the django-announce list with a severity and date, and
  fixes are released for all supported branches simultaneously.

## 9. Common mistakes and anti-patterns

- Equating authentication with authorisation: `LoginRequiredMixin` on a detail view with
  `Model.objects.all()` → IDOR.
- Hiding a button in a template instead of checking in the view/service.
- Checking permission in the view but letting a Celery task, admin action or management command
  call the model directly.
- Returning 403 for objects outside scope (confirms existence) where 404 is appropriate.
- Treating CORS, CSP or SameSite as access control.
- Trusting `X-Forwarded-For`, `X-Forwarded-Proto` or `Host` without a proxy that overwrites them.
- "Security by obscurity" as the only control: renamed admin URL, unguessable-but-unchecked IDs.
- Disabling a protection to fix a bug (e.g. `csrf_exempt` to fix a 403 behind a proxy instead of
  setting `CSRF_TRUSTED_ORIGINS`).
- Hand-rolled crypto, sanitisers, token formats or MFA.
- Detailed exception text in API responses or `DEBUG = True` in any internet-reachable environment.

## 10. Troubleshooting and validation

- `python manage.py check --deploy --settings=config.settings.production` — Django's
  `security.W0xx` checks for settings-level gaps.
- Grep the opt-out list and require each hit to have a justification and a test.
- For every URL pattern: who may call it, with which method, and which object scope applies?
  (Inventory technique in [threat-modeling.md](./threat-modeling.md).)
- Test matrix per endpoint: anonymous / authenticated-other-user / wrong role / other tenant /
  owner → expected 302|401, 404, 403, 404, 200
  ([security-testing-and-incident-response.md](./security-testing-and-incident-response.md)).
- Inspect real production response headers (`curl -sI https://app.example.com/`) — settings
  that a proxy strips or overrides don't count.
- Confirm Django and DRF versions are supported: `python -m django --version`, lock file.

**Reporting security issues in Django itself**: email `security@djangoproject.com` (never a
public ticket); for third-party packages use their security policy / GitHub private advisory.
Internally, provide a documented reporting channel and `/.well-known/security.txt` (RFC 9116).

**OWASP Top 10 (2021) → reference map** (an OWASP Top 10 2025 edition was published; `VERIFY`
category names and order before citing it):

| OWASP 2021 | Primary references |
|---|---|
| A01 Broken Access Control | [authorization-and-access-control.md](./authorization-and-access-control.md), [multi-tenant-security.md](./multi-tenant-security.md), [api-security.md](./api-security.md), [csrf-protection.md](./csrf-protection.md) |
| A02 Cryptographic Failures | [secrets-and-cryptography.md](./secrets-and-cryptography.md), [secure-django-configuration.md](./secure-django-configuration.md) |
| A03 Injection (incl. XSS) | [injection-prevention.md](./injection-prevention.md), [cross-site-scripting.md](./cross-site-scripting.md) |
| A04 Insecure Design | [threat-modeling.md](./threat-modeling.md), this reference |
| A05 Security Misconfiguration | [secure-django-configuration.md](./secure-django-configuration.md), [cors-and-browser-security.md](./cors-and-browser-security.md) |
| A06 Vulnerable and Outdated Components | [dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md) |
| A07 Identification and Authentication Failures | [authentication-security.md](./authentication-security.md), [session-and-cookie-security.md](./session-and-cookie-security.md) |
| A08 Software and Data Integrity Failures | [dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md), [injection-prevention.md](./injection-prevention.md) (deserialisation) |
| A09 Security Logging and Monitoring Failures | [security-logging-and-auditing.md](./security-logging-and-auditing.md) |
| A10 Server-Side Request Forgery | [ssrf-prevention.md](./ssrf-prevention.md) |

## 11. Related references and skills

- [threat-modeling.md](./threat-modeling.md) — apply these principles to a concrete system.
- [secure-django-configuration.md](./secure-django-configuration.md) — the settings behind the defaults.
- [authorization-and-access-control.md](./authorization-and-access-control.md) — choke-point implementation.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — proving controls.
- [django-core](../../django-core/SKILL.md) — middleware and request lifecycle mechanics.
- [django-architecture](../../django-architecture/SKILL.md) — services, selectors and boundaries.
- [django-orm](../../django-orm/SKILL.md) — scoped managers and constraints.

## 12. Official documentation and external references

- Security in Django: https://docs.djangoproject.com/en/5.2/topics/security/
- Deployment checklist: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
- Django security policies: https://docs.djangoproject.com/en/dev/internals/security/
- Supported versions: https://www.djangoproject.com/download/#supported-versions
- `LoginRequiredMiddleware`: https://docs.djangoproject.com/en/5.2/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware
- OWASP Top 10: https://owasp.org/Top10/
- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- OWASP Cheat Sheet Series: https://cheatsheetseries.owasp.org/
- OWASP Django Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Django_Security_Cheat_Sheet.html
- Saltzer & Schroeder design principles (1975): https://web.mit.edu/Saltzer/www/publications/protection/
- RFC 9116 (security.txt): https://www.rfc-editor.org/rfc/rfc9116
