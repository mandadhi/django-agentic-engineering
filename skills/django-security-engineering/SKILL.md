---
name: django-security-engineering
description: >-
  Django security engineering specialist — threat modelling and trust boundaries, production
  security settings (DEBUG, ALLOWED_HOSTS, HTTPS/HSTS, proxies), authentication and password
  handling, sessions and cookies, authorisation and IDOR prevention, CSRF, CORS/CSP and browser
  headers, injection and XSS, file uploads, SSRF, secrets and cryptography, DRF API security,
  multi-tenant isolation, security logging and auditing, supply-chain security, and security
  testing and incident response. Load when designing, implementing, reviewing, testing or
  troubleshooting security-relevant Django or DRF code, settings or deployments.
project: django-agentic-engineering
abstract: >-
  Keep Django's protections on, add the ones it cannot provide, enforce access at the data
  boundary, and prove every control with a failing-case test. A concise assessment workflow and
  navigation guide here; eighteen focused references loaded one to three at a time.
sdlc_phase: Security
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, Security]
---

# Django Security Engineering Specialist

Engineering judgement for building and reviewing secure Django systems: what the framework
protects by default, what it cannot know and the application must implement, and how to verify
both. Baseline: **Django 5.2 LTS + DRF 3.16**; behaviour tied to a release is qualified
"(Django ≥ X.Y)" and uncertain claims are marked `VERIFY`. Check the project's lock file before
recommending a version-specific setting. Third-party packages (django-cors-headers, django-csp,
django-axes, django-otp, simplejwt, nh3, …) are always named as such — never assume installed.

Organisation-wide OWASP review and secret-detection skills may exist as plugin skills
(`owasp-top10-review`, `secret-detection-patterns`, `security-scan-gates`); follow them when
present. This skill covers how those risks appear — and are prevented — in Django.

## Activate when

- Writing login, signup, password reset, MFA, token, session, permission or tenant code.
- Touching `csrf_exempt`, `mark_safe`/`|safe`, `raw()`/`extra()`/`RawSQL`/`cursor.execute`,
  `subprocess`, `pickle`/`yaml`, uploads, redirects, or outbound requests to user-supplied URLs.
- Changing production settings, middleware order, proxy/TLS setup, CORS, CSP or cookies.
- Adding or reviewing an API endpoint, serializer fields, throttles or error responses.
- Handling secrets, keys, signed values, encryption or sensitive personal data.
- Reviewing a change, a dependency upgrade, or a security finding; responding to an incident.

## Security engineering principles

1. **Framework protections stay on.** CSRF, auto-escaping, ORM parameterisation, clickjacking
   protection and password hashing are defaults; every opt-out needs a written reason and a test.
2. **Django cannot authorise for you.** Authentication identifies; authorisation is application
   code — default-deny, enforced in scoped querysets and services, not only in views or templates.
3. **Never trust the client.** Ownership, tenant, role, price and state transitions are set and
   checked server-side; every input is validated and bounded.
4. **Defence in depth, ordered by boundary.** Edge (TLS, WAF, rate limits) → Django middleware →
   view/permission → service → database constraints; browser controls (CSP, CORS, SameSite) only
   add to server-side controls, never replace them.
5. **Secure by default, explicit exceptions.** Public endpoints, CSRF exemptions, wide CORS and
   raw SQL are opt-in, reviewed and documented.
6. **Least privilege and least data.** Minimal permissions, DB roles, token scopes and response
   fields; collect, log and retain only what is needed.
7. **Fail closed, reveal little.** Errors deny access and leak no stack traces, existence or
   internal identifiers.
8. **Every control has a failing-case test.** Prove the attack is blocked, not that the happy path
   works.

## Security assessment workflow

1. **Frame** — application type (HTML, API, both), users and roles, tenancy, data sensitivity,
   compliance needs, Django/DRF versions, deployment topology (proxy, CDN, storage).
2. **Map** — assets, actors, trust boundaries and entry points (views, APIs, admin, uploads,
   webhooks, tasks, commands, outbound calls) → `threat-modeling.md`.
3. **Analyse threats** — STRIDE per boundary; rank by likelihood × impact; list the concrete
   vulnerability classes in scope (table below).
4. **Select controls** — framework default, Django setting, application code, third-party
   package, or infrastructure; prefer the most central, least bypassable layer.
5. **Implement at the right boundary** — settings/middleware for global concerns, permission
   classes and scoped querysets for access, services for business rules, the DB for invariants.
6. **Validate access and isolation** — anonymous, wrong user, wrong role, other tenant, and
   privileged paths for every endpoint and object.
7. **Test failure scenarios** — CSRF enforced, injection payloads inert, uploads rejected, SSRF
   targets blocked, throttles trigger, errors disclose nothing.
8. **Review production** — `check --deploy`, headers on the real response, proxy/TLS trust,
   secrets source, dependency audit, logging and alerting, rollback/rotation readiness.

Load one to three references per step — never all.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [security-fundamentals.md](references/security-fundamentals.md) | Principles, framework-vs-developer responsibility map, defence in depth, secure architecture, OWASP Top 10 / ASVS mapping |
| [threat-modeling.md](references/threat-modeling.md) | Assets, actors, trust boundaries, attack surface inventory, STRIDE, risk rating, security requirements |
| [secure-django-configuration.md](references/secure-django-configuration.md) | `DEBUG`, `ALLOWED_HOSTS`, `SECRET_KEY` loading, `SecurityMiddleware`, HTTPS redirect, HSTS, proxy/TLS headers, admin hardening, `check --deploy` |
| [authentication-security.md](references/authentication-security.md) | Password hashing/validators, backends, login/logout/reset lifecycle, MFA, enumeration, brute-force protection, SSO considerations |
| [session-and-cookie-security.md](references/session-and-cookie-security.md) | Session engines, fixation/rotation, expiry, invalidation, cookie flags, `SameSite`, cookie prefixes, signed cookies |
| [authorization-and-access-control.md](references/authorization-and-access-control.md) | Permissions/groups, RBAC/ABAC, object-level checks, ownership, IDOR, enforcement in views/services/templates, admin permissions |
| [csrf-protection.md](references/csrf-protection.md) | CSRF mechanics, middleware internals, tokens, AJAX/SPA integration, `CSRF_TRUSTED_ORIGINS`, exemptions, login CSRF |
| [cors-and-browser-security.md](references/cors-and-browser-security.md) | Same-origin policy, CORS (django-cors-headers), CSP (django-csp / Django ≥ 6.0), framing, `nosniff`, Referrer/COOP/Permissions policies |
| [injection-prevention.md](references/injection-prevention.md) | SQL injection (raw/extra/identifiers), command, template, header/email, XML, unsafe deserialisation, input handling |
| [cross-site-scripting.md](references/cross-site-scripting.md) | Reflected/stored/DOM XSS, auto-escaping contexts, `mark_safe`/`format_html`, `json_script`, sanitisation (nh3), rich text |
| [file-upload-security.md](references/file-upload-security.md) | Upload limits, type/content validation, MIME spoofing, filenames/path traversal, private media, serving, scanning, image/document parsing |
| [ssrf-prevention.md](references/ssrf-prevention.md) | SSRF mechanics, URL/IP validation, redirects, DNS rebinding, egress control, open redirects, webhook/URL-fetch features |
| [secrets-and-cryptography.md](references/secrets-and-cryptography.md) | Secret storage/rotation, `SECRET_KEY` scope, `django.core.signing`, hashing vs encryption, field encryption, keys, tokens, `secrets` |
| [api-security.md](references/api-security.md) | DRF auth/permission defaults, token & JWT risks, throttling, mass assignment, excessive data exposure, pagination/filter abuse, API errors |
| [multi-tenant-security.md](references/multi-tenant-security.md) | Tenant resolution, queryset isolation, cross-tenant FKs, tasks, caches, files, isolation models, PostgreSQL RLS |
| [security-logging-and-auditing.md](references/security-logging-and-auditing.md) | Security events, auth/authz auditing, redaction, audit trails, log injection, monitoring and alerting |
| [dependency-and-supply-chain-security.md](references/dependency-and-supply-chain-security.md) | Pinning/hashes, vulnerability scanning, updates, malicious packages, SBOM, CI/CD and image hardening, secret scanning |
| [security-testing-and-incident-response.md](references/security-testing-and-incident-response.md) | Security test suites, permission matrices, SAST/DAST, regression tests, incident response and Django-specific containment |

## Task-based loading

| Task | Load |
|---|---|
| Security design for a new feature or system | threat-modeling.md, security-fundamentals.md |
| Preparing or reviewing production settings | secure-django-configuration.md, session-and-cookie-security.md (+ cors-and-browser-security.md) |
| "Works locally, fails behind the proxy" (redirect loops, CSRF origin, host errors) | secure-django-configuration.md, csrf-protection.md |
| Building login, signup, password reset or MFA | authentication-security.md, session-and-cookie-security.md |
| Stopping credential stuffing / brute force | authentication-security.md, api-security.md (throttles) |
| Adding permissions, roles or ownership checks | authorization-and-access-control.md |
| Fixing an IDOR finding | authorization-and-access-control.md (+ api-security.md or multi-tenant-security.md) |
| SPA or mobile client talking to Django | csrf-protection.md, cors-and-browser-security.md, api-security.md |
| Adding CSP or security headers | cors-and-browser-security.md, cross-site-scripting.md |
| Using raw SQL, dynamic ordering, shell commands or deserialisation | injection-prevention.md |
| Rendering user HTML / rich-text editor / `mark_safe` review | cross-site-scripting.md |
| User uploads, avatars, document processing, private downloads | file-upload-security.md |
| Fetching user-supplied URLs, webhooks, link previews, `next=` redirects | ssrf-prevention.md |
| Managing secrets, rotating `SECRET_KEY`, encrypting fields, issuing tokens | secrets-and-cryptography.md |
| Securing a DRF API or reviewing serializer exposure | api-security.md, authorization-and-access-control.md |
| SaaS / multi-tenant data isolation | multi-tenant-security.md, authorization-and-access-control.md |
| Audit trails, security events, log redaction | security-logging-and-auditing.md |
| Dependency upgrade, CVE triage, CI security gates | dependency-and-supply-chain-security.md |
| Writing security tests or a pre-release security review | security-testing-and-incident-response.md (+ owner of each control) |
| Suspected breach, leaked secret, compromised account | security-testing-and-incident-response.md, secrets-and-cryptography.md |

## Skill pairings and handoffs

| Topic | Skill |
|---|---|
| Middleware internals, settings mechanics, request lifecycle, auth/session mechanics, storage, logging config | [django-core](../django-core/SKILL.md) |
| Where enforcement lives: services, selectors, app boundaries, tenancy architecture | [django-architecture](../django-architecture/SKILL.md) |
| Query construction, raw SQL mechanics, custom managers for scoping, constraints | [django-orm](../django-orm/SKILL.md) |
| Serializers, views, auth/permission/throttle classes and their implementation | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Test architecture, factories, CI test layout | [django-testing](../django-testing/SKILL.md) |
| Templates, JavaScript, frontend build and asset pipelines | [django-frontend](../django-frontend/SKILL.md) |
| Celery, webhooks, identity providers, outbound HTTP clients | [django-integrations](../django-integrations/SKILL.md) |
| Caching strategy, load and abuse capacity | [django-performance](../django-performance/SKILL.md) |
| Imports/exports and large-file processing pipelines | [django-data-processing](../django-data-processing/SKILL.md) |
| Database roles, RLS internals, encryption at rest | [postgresql-engineering](../postgresql-engineering/SKILL.md) |

Rule of thumb: this skill decides **what must be protected, which control, and how to prove it**;
the owning skill explains **how the framework mechanism works**.

## Security decision guidelines

| Decision | Prefer | Because |
|---|---|---|
| Where to enforce access | Scoped queryset/selector + permission check in the service | One choke point; views, APIs, tasks and admin all inherit it |
| Missing object vs forbidden object | 404 for objects outside the caller's scope | Avoids confirming existence (IDOR enumeration) |
| Browser auth | Same-site session cookies + CSRF | HttpOnly, revocable server-side, no token in JS storage |
| Third-party / mobile API auth | Opaque or hashed tokens, short-lived, scoped; JWT only with a revocation plan | Stateless tokens cannot be revoked early without extra state |
| Rate limiting | Edge for volume, app (DRF throttles / django-axes) for business abuse | Each protects what the other cannot see |
| HTML from users | Escape; sanitise with an allow-list (nh3) only for rich text | Sanitisers are the exception, not the default |
| Raw SQL / dynamic identifiers | ORM expressions first; params + allow-listed identifiers otherwise | Parameters cannot bind identifiers |
| Secrets | Secret manager → injected env/file at runtime; never in repo or image | Rotation, audit and least exposure |
| Sensitive data at rest | Hash if you only verify; encrypt only if you must read it back | Hashes cannot leak plaintext |
| Uploaded files | Private storage, server-generated names, served via signed URLs or a checked view | Content cannot execute on the app origin |
| Tenancy | Row-level scoping with enforced managers; schema/DB isolation for regulatory need | Isolation strength vs operational cost |

When two controls are equivalent, pick the one that is **default-on, centrally enforced and
testable**. Record hard-to-reverse choices (auth model, tenancy model, token format) in an ADR.

## Validation checklist

- [ ] `python manage.py check --deploy` passes on production settings (or each warning is justified).
- [ ] `DEBUG = False`; explicit `ALLOWED_HOSTS`; `SECRET_KEY` from a secret source; HTTPS + HSTS;
      `SECURE_PROXY_SSL_HEADER` only behind a proxy that overwrites the header.
- [ ] Session and CSRF cookies are `Secure`; session cookie `HttpOnly`; `SameSite` deliberate.
- [ ] Every view/endpoint is default-deny; public ones are explicit (`login_not_required`, `AllowAny`).
- [ ] Every object lookup is scoped to user/tenant; cross-user and cross-tenant tests return 404.
- [ ] Serializers/forms list explicit fields; server-owned fields are read-only.
- [ ] No `csrf_exempt`, `mark_safe`, `|safe`, raw SQL, `shell=True` or unsafe deserialisation
      without justification and a test.
- [ ] Login, reset, OTP and token endpoints are rate-limited and do not enumerate accounts.
- [ ] Uploads are size/type/content validated, renamed, stored privately, never executed.
- [ ] Outbound fetches of user URLs are allow-listed or block private ranges after resolution.
- [ ] CSP, framing, `nosniff` and referrer headers present on real responses; CORS is an allow-list.
- [ ] Secrets absent from code, logs, errors and images; rotation procedure documented.
- [ ] Security events are logged with actor/action/object/request id and no sensitive payloads.
- [ ] Dependencies pinned with hashes, audited in CI; Django/DRF on a supported release.
- [ ] Each control above has a failing-case test; claims marked `VERIFY` were checked or disclosed.
- Docs: <https://docs.djangoproject.com/en/5.2/topics/security/> ·
  <https://cheatsheetseries.owasp.org/> · <https://owasp.org/www-project-application-security-verification-standard/>
