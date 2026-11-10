# Threat Modeling for Django Systems

> Scope: identifying assets, actors and trust boundaries in a Django deployment; building an
> attack-surface inventory from the project itself; STRIDE analysis with Django-specific
> threats; risk rating; turning threats into security requirements and tests; a lightweight
> threat-model template; when to re-model.
> Not covered here: the principles behind controls
> ([security-fundamentals.md](./security-fundamentals.md)); the controls themselves (owning
> references linked from each threat); architecture decisions and ADRs
> ([django-architecture](../../django-architecture/references/architecture-decision-records.md)).

## 1. Purpose and scope

Threat modeling answers four questions (Shostack's framing, used by the OWASP Threat Modeling
Cheat Sheet): *What are we working on? What can go wrong? What are we going to do about it? Did
we do a good enough job?* For a Django team it should be a short, repeatable engineering
activity — a markdown file next to the code, updated with the feature — not a document produced
once for an audit.

The output is a ranked list of threats, each mapped to a control, an owner and a test.

## 2. Core concepts

- **Asset** — something worth protecting: personal data, credentials, sessions, API tokens,
  `SECRET_KEY`, payment state, tenant data, admin capability, availability, audit trail integrity.
- **Actor** — who interacts: anonymous visitor, authenticated user, user of another tenant,
  tenant admin, staff/superuser, API client, webhook sender, background worker, operator with
  shell access, CI pipeline, dependency maintainer.
- **Trust boundary** — where data or control passes between parties with different privileges.
  Every boundary crossing requires validation, authentication or authorisation.
- **Entry point / attack surface** — every place input enters: URLs, query strings, bodies,
  headers, cookies, uploaded files, webhooks, task payloads, management command arguments,
  admin, data imports, outbound responses parsed by the app.
- **Threat** — a way an actor could harm an asset across a boundary.
- **Vulnerability** — a weakness that makes a threat realisable.
- **Risk** — likelihood × impact of a threat, given existing controls.
- **Control / mitigation** — a measure that reduces likelihood or impact.

**Typical Django deployment and its boundaries**

```text
[Browser / mobile / API client]          (untrusted)
        │  TB1: internet → edge (TLS, WAF, CDN cache)
[CDN / load balancer / reverse proxy]    (sets X-Forwarded-*; may cache)
        │  TB2: proxy → app (trusted headers only if proxy overwrites them)
[gunicorn / uvicorn workers → Django]    (middleware, views, DRF, admin)
   │            │            │            │
 TB3          TB4          TB5          TB6
[PostgreSQL] [Redis cache/ [Object      [Third-party APIs,
             session/broker] storage S3]  IdP, payment, email]
                 │
              TB7: broker → [Celery workers] (same code, often more privileged)
[Operators / CI/CD / admin users]  TB8: privileged access paths
```

Key boundary facts: the cache and broker are often unauthenticated inside the network and hold
sessions or task payloads; Celery workers run with application credentials but without a request
user; object storage may be public by misconfiguration; the proxy decides whether
`X-Forwarded-Proto` and client IPs can be trusted.

## 3. Internal mechanics

**Building the attack-surface inventory from the project** — don't rely on memory.

Built-in: iterate the URL resolver.

```python
# scripts/list_urls.py — run with: python manage.py shell < scripts/list_urls.py
from django.urls import get_resolver, URLPattern, URLResolver

def walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            cb = p.callback
            view = getattr(cb, "view_class", None) or getattr(cb, "cls", None) or cb
            yield prefix + str(p.pattern), f"{view.__module__}.{getattr(view, '__qualname__', view)}", p.name

for route, view, name in walk(get_resolver().url_patterns):
    print(f"{route}\t{view}\t{name}")
```

Third-party alternative: `python manage.py show_urls` from **django-extensions** (not built-in;
keep it out of production `INSTALLED_APPS`).

Complete the inventory beyond URLs:

| Surface | Where to look |
|---|---|
| HTTP endpoints | URL resolver output; DRF routers; `@action`s; admin site registrations |
| Auth flows | login/logout/reset/signup/MFA views, `AUTHENTICATION_BACKENDS`, DRF auth classes |
| Public endpoints | `login_not_required`, `AllowAny`, `authentication_classes = []`, `csrf_exempt` |
| Webhooks | Views receiving third-party callbacks (signature verification?) |
| Uploads | `FileField`/`ImageField`, `request.FILES`, DRF `MultiPartParser` |
| Outbound calls | `requests`/`httpx`/`urllib` uses, especially with user-supplied URLs |
| Background work | Celery tasks and their arguments; scheduled jobs |
| Privileged paths | Admin, management commands, `shell`, data migrations, CI deploy jobs |
| Data stores | DB, cache, session engine, broker, object storage buckets and their ACLs |
| Rendering | Templates using `|safe`, `mark_safe`, `autoescape off`; email templates |
| Dependencies | Lock file, base image (see [dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md)) |

## 4. Engineering decisions and trade-offs

**Method choice**

| Method | Use when | Cost |
|---|---|---|
| STRIDE per element / per boundary | Default for features and systems | Low–medium |
| Attack trees | A single high-value goal (e.g. "transfer funds from another tenant") | Medium |
| Abuse/misuse cases | Product teams writing stories ("as an attacker I…") | Low |
| PASTA / LINDDUN | Risk-centric enterprise programmes; privacy threats (LINDDUN) | High |

**Scope**: model a feature when it adds a boundary, data class or actor; model the system once
and keep it current. A per-PR checklist (section 10) catches drift between full models.

**Risk rating**: a simple 1–3 likelihood × 1–3 impact grid is enough for most teams; use the
OWASP Risk Rating Methodology (likelihood factors: skill, motive, opportunity, discoverability,
exploitability; impact: confidentiality, integrity, availability, accountability, business) when
prioritising across many findings or communicating with risk owners. CVSS rates *vulnerabilities*,
not design threats — don't force it onto threat lists.

**Treatment**: mitigate (default), eliminate (remove the feature/surface), transfer (e.g. card
data to a payment provider's hosted fields), accept (documented, owner-signed, time-boxed).

## 5. Django implementation patterns

**STRIDE with Django-specific threats and owning controls**

| STRIDE | Example threat in a Django system | Control (reference) |
|---|---|---|
| **S**poofing | Credential stuffing against `/accounts/login/` | Throttling/lockout, MFA, breached-password checks ([authentication-security.md](./authentication-security.md)) |
| | Session theft via non-`Secure` cookie or XSS | Cookie flags, CSP, HTTPS ([session-and-cookie-security.md](./session-and-cookie-security.md)) |
| | Forged webhook from "payment provider" | HMAC signature verification, timestamp check, `hmac.compare_digest` ([secrets-and-cryptography.md](./secrets-and-cryptography.md)) |
| | Spoofed `X-Forwarded-For` defeats IP throttles | Trust only proxy-set headers; DRF `NUM_PROXIES` ([api-security.md](./api-security.md)) |
| **T**ampering | Mass assignment: `{"role": "admin"}` in a profile update | Explicit serializer/form fields, read-only server fields ([api-security.md](./api-security.md)) |
| | CSRF on a state-changing form | `CsrfViewMiddleware`, no exemptions ([csrf-protection.md](./csrf-protection.md)) |
| | SQL injection via `order_by` or raw SQL | Allow-listed identifiers, parameters ([injection-prevention.md](./injection-prevention.md)) |
| | Tampered Celery payload in an unauthenticated Redis broker | Broker auth/TLS, JSON serializer only, re-authorise in task ([multi-tenant-security.md](./multi-tenant-security.md)) |
| **R**epudiation | Admin changes a user's role; no record of who | Audit trail with actor, request id ([security-logging-and-auditing.md](./security-logging-and-auditing.md)) |
| **I**nformation disclosure | IDOR: `/api/invoices/1043/` of another tenant | Scoped querysets, 404 ([authorization-and-access-control.md](./authorization-and-access-control.md)) |
| | `DEBUG = True` error page leaks settings | Production settings, `check --deploy` ([secure-django-configuration.md](./secure-django-configuration.md)) |
| | Serializer `fields = "__all__"` exposes `password`/internal flags | Explicit fields ([api-security.md](./api-security.md)) |
| | Public S3 bucket for user documents | Private storage, signed URLs ([file-upload-security.md](./file-upload-security.md)) |
| | SSRF to cloud metadata endpoint via "import from URL" | Egress allow-list, IP checks ([ssrf-prevention.md](./ssrf-prevention.md)) |
| | Cache key without tenant id serves another tenant's page | Tenant-qualified cache keys ([multi-tenant-security.md](./multi-tenant-security.md)) |
| **D**enial of service | Huge JSON body or 10 000 form fields | `DATA_UPLOAD_MAX_MEMORY_SIZE`, `DATA_UPLOAD_MAX_NUMBER_FIELDS`, edge body limits ([secure-django-configuration.md](./secure-django-configuration.md)) |
| | `?page_size=1000000` or unindexed filter | Max page size, bounded filters ([api-security.md](./api-security.md)) |
| | Decompression bomb in image upload | Pillow `MAX_IMAGE_PIXELS`, size checks ([file-upload-security.md](./file-upload-security.md)) |
| **E**levation of privilege | Self-registration sets `is_staff` | Server-owned fields; admin restricted ([authorization-and-access-control.md](./authorization-and-access-control.md)) |
| | Stored XSS in admin-viewed content hijacks staff session | Escaping, sanitisation, CSP ([cross-site-scripting.md](./cross-site-scripting.md)) |
| | Malicious dependency executes at install | Hash pinning, review ([dependency-and-supply-chain-security.md](./dependency-and-supply-chain-security.md)) |

**Turning a threat into a requirement and a test**

```text
Threat T-07  (Information disclosure, boundary TB2)
  A user of tenant A requests /api/projects/<id>/ for a project of tenant B.
Requirement R-07  Every project lookup is scoped to request.user's tenant; out-of-scope ids return 404.
Control  ProjectViewSet.get_queryset() -> projects_for(request.user); no other Project query in api.py.
Test     test_project_detail_other_tenant_returns_404 (APIClient as tenant A user, tenant B project).
Status   Mitigated — PR #412.
```

```python
# projects/tests/test_security.py
import pytest
from rest_framework.test import APIClient

@pytest.mark.django_db
def test_project_detail_other_tenant_returns_404(user_factory, project_factory):
    attacker = user_factory(tenant__slug="a")
    victim_project = project_factory(tenant__slug="b")
    client = APIClient()
    client.force_authenticate(attacker)
    response = client.get(f"/api/projects/{victim_project.pk}/")
    assert response.status_code == 404
```

(Factories and pytest-django are project conventions — see [django-testing](../../django-testing/SKILL.md).)

**Lightweight threat-model template** — keep as `docs/security/threat-model-<feature>.md`:

```markdown
**Threat model: <feature/system>   (updated YYYY-MM-DD, owner: <team>)**

### Scope and context
- What changes / what is being built:
- Django/DRF versions, deployment topology:
- Data classes handled (PII, credentials, payment, health, tenant data):
- ASVS target level:

### Assets
| Asset | Sensitivity | Where stored |

### Actors
| Actor | Trust level | How authenticated |

### Data flow and trust boundaries
<diagram or list: client → proxy → Django → DB/cache/broker/storage/third parties>

### Entry points
| Entry point | Method | Auth | Object scope | Notes |

### Threats
| ID | STRIDE | Threat | Likelihood | Impact | Risk | Control | Test | Status |

### Accepted risks
| ID | Rationale | Owner | Review date |

### Open questions / VERIFY
```

## 6. Production use cases

- **New "export my data" feature**: assets = full PII dataset; threats = IDOR on export id,
  export link shared/leaked, export of other tenant via task argument, DoS via repeated exports.
  Controls = scoped selector, short-lived signed URL, re-authorise in Celery task, throttle.
- **Adding SSO (OIDC)**: new actor (IdP) and boundary; threats = account linking by unverified
  email, `state`/`nonce` misuse, open redirect on callback. Hand off to
  [authentication-security.md](./authentication-security.md) and [django-integrations](../../django-integrations/SKILL.md).
- **Moving uploads to S3**: boundary changes from local disk to a cloud service; threats =
  public ACLs, overly broad IAM, presigned URL lifetime, content-type confusion.
- **Opening the API to partners**: new actor class; threats = token leakage, scope creep,
  scraping via pagination, partner-side SSRF via callback URLs.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Per-feature lightweight model | Cheap, current, close to code | Can miss system-wide threats; needs a periodic system review |
| Full system model | Catches cross-cutting issues (cache, broker, admin) | Goes stale without an owner |
| STRIDE | Systematic, easy to teach | Produces many low-value items without risk rating |
| Code-derived inventory | Objective; finds forgotten endpoints | Doesn't reveal design flaws by itself |

## 8. Security and performance considerations

- Availability is an asset: include rate limits, payload sizes, expensive queries and pagination
  in the model; unbounded work is a security issue.
- Background workers and management commands bypass request middleware — authorisation and
  tenant context must be re-established explicitly.
- Caches and CDNs are trust boundaries: a cached authenticated response is an information
  disclosure (`Vary: Cookie`, `Cache-Control: private` — see [django-core caching](../../django-core/references/caching.md)).
- The threat model itself may be sensitive (lists weaknesses and accepted risks); keep it in the
  private repo with normal access controls.

## 9. Common mistakes and anti-patterns

- Modeling only the HTTP layer, ignoring Celery, admin, commands, imports and CI.
- Listing threats without controls, owners or tests — a document, not engineering.
- Assuming the internal network is trusted (Redis, broker, metadata service).
- Treating "authenticated user" as one actor; other users, other tenants and staff differ.
- Accepting risks without an owner or review date.
- Writing the model after implementation and never updating it.
- Rating everything "high"; no prioritisation, nothing gets fixed.

## 10. Troubleshooting and validation

Per-PR security checklist (re-model triggers):

- [ ] New or changed URL / `@action` / admin registration? → update the entry-point table.
- [ ] New actor, role, tenant concept or auth mechanism?
- [ ] New data class (payment, health, government ID) or new store/bucket?
- [ ] New third-party integration, webhook or outbound URL fetch?
- [ ] New file upload or file-processing library?
- [ ] New opt-out (`csrf_exempt`, `AllowAny`, `mark_safe`, raw SQL)?
- [ ] New Celery task that touches user/tenant data?

Validation: each "Mitigated" threat links to a passing test; diff the URL inventory between
releases (new routes without threat entries are findings); review accepted risks on their dates.

## 11. Related references and skills

- [security-fundamentals.md](./security-fundamentals.md) — principles, defaults, OWASP mapping.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — tests derived from threats.
- [authorization-and-access-control.md](./authorization-and-access-control.md), [multi-tenant-security.md](./multi-tenant-security.md) — most frequent high-impact threats.
- [django-architecture](../../django-architecture/SKILL.md) — boundaries and ADRs.
- [django-core urls-views](../../django-core/references/urls-views.md) — URL resolver mechanics.
- [django-integrations](../../django-integrations/SKILL.md) — third-party boundaries.

## 12. Official documentation and external references

- OWASP Threat Modeling Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html
- OWASP Threat Modeling Process: https://owasp.org/www-community/Threat_Modeling_Process
- OWASP Risk Rating Methodology: https://owasp.org/www-community/OWASP_Risk_Rating_Methodology
- Threat Modeling Manifesto: https://www.threatmodelingmanifesto.org/
- Microsoft STRIDE: https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats
- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- Django URL resolver (`get_resolver`, `URLPattern`): https://docs.djangoproject.com/en/5.2/ref/urlresolvers/
- Django security overview: https://docs.djangoproject.com/en/5.2/topics/security/
- django-extensions `show_urls` (third-party): https://django-extensions.readthedocs.io/en/latest/command_extensions.html
