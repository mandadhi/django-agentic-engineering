# Multi-Tenant Security

> Scope: tenant isolation as a security property — isolation models and their failure modes,
> tenant resolution and membership verification, request-scoped tenant context, tenant-scoped
> managers and every way they are bypassed, cross-tenant foreign keys, PostgreSQL row-level
> security as defence in depth, background tasks, caches, files, search, exports and logs, and
> tenant-aware testing.
> Not covered here: per-user permissions inside a tenant
> ([authorization-and-access-control.md](./authorization-and-access-control.md)); API object
> exposure ([api-security.md](./api-security.md)); tenancy architecture and app boundaries
> ([django-architecture modular-monolith](../../django-architecture/references/modular-monolith.md),
> [django-architecture scalability-patterns](../../django-architecture/references/scalability-patterns.md));
> manager/QuerySet mechanics ([django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md));
> PostgreSQL policy internals ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

In a multi-tenant system the worst bug is not a crash — it is one customer seeing another's
data. Django has no concept of tenants; isolation is entirely application (and optionally
database) design. This reference treats tenancy as a trust boundary: how the tenant is
determined, how it is propagated to every data access path, and how isolation is verified and
backed up by a second layer.

Code targets Django 5.2 LTS with PostgreSQL; examples were not executed.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Tenant | The isolation unit (organisation, account, workspace). |
| Tenant context | The tenant the current request/task acts for; must be derived server-side. |
| Membership | Proof that the actor belongs to the tenant (and with what role). |
| Row-level (shared schema) | All tenants in the same tables, discriminated by `tenant_id`. |
| Schema-per-tenant | One PostgreSQL schema per tenant, selected via `search_path` (e.g. django-tenants, third-party). |
| Database-per-tenant | Separate databases selected via DB routers / connection aliases. |
| Cross-tenant reference | A row in tenant A pointing to a row in tenant B — a latent leak. |
| Defence in depth | A second isolation layer (RLS, schema, DB credentials) that holds when application scoping fails. |

**Framework provides:** managers and QuerySets you can scope, DB routers, multiple connections,
middleware hooks, cache key prefix/function hooks, storage abstraction.

**Developer owns:** everything that makes those tenant-aware — resolution, membership checks,
default scoping, bypass prevention, task/caching/storage propagation, and tests.

## 3. Internal mechanics

### Where scoping silently disappears

A tenant-filtering default manager (`objects = TenantManager()`) is applied by `Model.objects`
and by reverse related managers (they subclass the model's default manager). It is **not**
applied by:

| Path | Why |
|---|---|
| Forward FK/O2O access (`invoice.customer`) | Uses `Model._base_manager`, which is unfiltered by design |
| `Model._base_manager`, `Meta.base_manager_name` misuse | Explicitly unfiltered |
| A second manager (`all_objects`) | Intended bypass — and easy to use by accident |
| `raw()`, `cursor.execute`, `RawSQL`, `extra()` | Bypass managers entirely |
| `QuerySet.update()/delete()` on an unscoped queryset | No per-row checks |
| `prefetch_related` with a custom `Prefetch(queryset=...)` | Uses whatever queryset you pass |
| Subqueries/`OuterRef` over another model | Only scoped if that inner queryset is scoped |
| `ModelChoiceField`/DRF `PrimaryKeyRelatedField` querysets | Defined at import time, often `.all()` on base manager or with no tenant context |
| Admin, `dumpdata`, `loaddata`, migrations, management commands | Run with no request/tenant context |
| Cache, search index, file storage, analytics pipelines | Outside the ORM |

If the default manager reads tenant from a context variable, a missing context must **fail
closed** (raise or return `.none()`), never fall back to "all tenants".

### Context propagation

`contextvars.ContextVar` is per-thread and per-asyncio-task, so it is correct under WSGI threads
and ASGI. Thread-locals are not safe for async code. A context var set in middleware must be reset
in `finally`; otherwise the value can bleed into the next request handled by a reused thread.
Celery workers, `asyncio.create_task`, and `ThreadPoolExecutor` do not inherit the request's context
automatically in all cases (tasks run in another process entirely) — pass the tenant id explicitly.

### Schema-per-tenant mechanics (django-tenants, third-party)

Middleware resolves the tenant from the hostname and sets `search_path` on the connection; shared
apps live in `public`. Risks: any code path that runs SQL before the middleware or after a
connection is reused without resetting `search_path`; raw SQL using schema-qualified names; Celery
tasks that forget to switch schema (django-tenants provides helpers such as `schema_context` /
`tenant_context`).

### PostgreSQL RLS mechanics

`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `CREATE POLICY` filters rows for roles that do not
own the table. The **table owner and superusers bypass RLS** unless `FORCE ROW LEVEL SECURITY` is
set (superusers and `BYPASSRLS` roles always bypass). The application typically sets a
transaction-local GUC — `SELECT set_config('app.tenant_id', %s, true)` — that the policy reads with
`current_setting('app.tenant_id', true)`.

## 4. Engineering decisions and trade-offs

| Model | Isolation strength | Security risks | Operational cost |
|---|---|---|---|
| Row-level, application scoping only | Weakest — one missed filter leaks | Every bypass path in §3 | Lowest |
| Row-level + PostgreSQL RLS | Strong — DB enforces even if code forgets | Pooling/GUC mistakes; owner-role bypass; migrations run as owner | Moderate |
| Schema-per-tenant | Strong logical separation | `search_path` reset bugs; shared tables in `public`; migrations × N schemas | High at large tenant counts |
| Database-per-tenant | Strongest; per-tenant credentials/keys possible | Router mistakes; connection explosion | Highest |

Guidance:

- Default to **row-level with an enforced tenant manager and composite constraints**; add **RLS**
  for sensitive data or regulated customers.
- Choose schema/DB-per-tenant for contractual or regulatory isolation, per-tenant encryption keys,
  data residency, or noisy-neighbour needs — not as a substitute for scoping discipline.
- Decide early whether users can belong to multiple tenants. If yes, tenant context comes from the
  URL/subdomain/header **plus a membership check**, never from the user record alone.
- Record the choice in an ADR; migrating isolation models later is expensive.

## 5. Django implementation patterns

### Tenant resolution with membership verification

```python
# tenancy/context.py
from contextvars import ContextVar

_current_tenant: ContextVar["Tenant | None"] = ContextVar("current_tenant", default=None)


class TenantNotSet(RuntimeError):
    pass


def get_current_tenant():
    tenant = _current_tenant.get()
    if tenant is None:
        raise TenantNotSet("No tenant in context")   # fail closed
    return tenant


def set_current_tenant(tenant):
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)
```

```python
# tenancy/middleware.py
from django.http import Http404

from tenancy.context import reset_current_tenant, set_current_tenant
from tenancy.models import Membership, Tenant


class TenantMiddleware:
    """Resolve tenant from subdomain and require active membership. Place after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        slug = request.get_host().split(":")[0].split(".")[0]   # host already validated by ALLOWED_HOSTS
        tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
        request.tenant = tenant
        request.membership = None
        if tenant is not None and request.user.is_authenticated:
            request.membership = (
                Membership.objects.filter(tenant=tenant, user=request.user, is_active=True).first()
            )
            if request.membership is None:
                raise Http404   # authenticated but not a member: do not reveal the tenant exists
        token = set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            reset_current_tenant(token)
```

A header such as `X-Tenant-ID` is acceptable for APIs **only** with the same membership check —
it is a selector, not a credential. Never derive the tenant from a request body field.

Async views: write an async-capable middleware (`sync_capable`/`async_capable`) and use
`request.auser()`; the context var works the same way.

### Tenant-scoped model base and manager

```python
# tenancy/models.py
from django.db import models

from tenancy.context import get_current_tenant


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(tenant=get_current_tenant())   # raises if unset


class TenantScopedModel(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT, db_index=True,
                               editable=False)

    objects = TenantManager()
    unscoped = models.Manager()   # explicit, greppable bypass for platform code only

    class Meta:
        abstract = True
        base_manager_name = "unscoped"   # forward relations stay functional; see §3

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            self.tenant = get_current_tenant()
        elif self.tenant_id != get_current_tenant().pk:
            raise PermissionError("Cross-tenant write")   # VERIFY fits platform/admin flows
        super().save(*args, **kwargs)
```

Implicit context managers are convenient but hide the dependency; many teams prefer explicit
`Invoice.objects.for_tenant(request.tenant)` in selectors, plus a lint/test that forbids unscoped
access. Either way, keep `unscoped` usage in a short allow-list reviewed in code review (CI grep).

### Prevent cross-tenant references

Application check plus database guarantee:

```python
class Project(TenantScopedModel):
    name = models.CharField(max_length=200)

    class Meta(TenantScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["tenant", "id"], name="project_tenant_id_uniq"),
        ]


class Task(TenantScopedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    def clean(self):
        if self.project.tenant_id != self.tenant_id:
            raise ValidationError({"project": "Invalid project."})
```

A composite FK `(tenant_id, project_id) → project(tenant_id, id)` makes cross-tenant references
impossible at the DB level. Django 5.2 adds `CompositePrimaryKey`, but composite *foreign keys*
are not supported by the ORM as of 5.2 — implement via a migration with `RunSQL` (VERIFY current
status in your version). Validate every user-supplied FK id against the tenant scope in forms and
serializers (scoped `ModelChoiceField`/`PrimaryKeyRelatedField` querysets built per request).

### PostgreSQL RLS as defence in depth

```python
# migration: enable RLS on a tenant table (app connects as a non-owner role)
from django.db import migrations

SQL = """
ALTER TABLE billing_invoice ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_invoice FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON billing_invoice
    USING (tenant_id = current_setting('app.tenant_id', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint);
"""
REVERSE = """
DROP POLICY IF EXISTS tenant_isolation ON billing_invoice;
ALTER TABLE billing_invoice NO FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_invoice DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [("billing", "0007_invoice_tenant")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
```

```python
# Set the GUC per transaction so pooled connections cannot carry it to another tenant.
from django.db import connection, transaction


def run_in_tenant(tenant_id, fn, *args, **kwargs):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        return fn(*args, **kwargs)
```

RLS rules of thumb:

- Use `set_config(..., true)` (transaction-local, like `SET LOCAL`) and wrap requests in a
  transaction (`ATOMIC_REQUESTS` or a middleware) — session-level `SET` leaks across requests with
  persistent connections (`CONN_MAX_AGE`), Django's psycopg pool (Django ≥ 5.1, VERIFY), and
  PgBouncer transaction pooling (VERIFY your pooler mode).
- `current_setting(..., true)` returns NULL when unset → policy matches no rows → fails closed.
- Run migrations as the owner role and the app as a separate non-owner, non-`BYPASSRLS` role.
- Platform jobs that legitimately cross tenants use a distinct role/connection alias, audited.

### Background tasks

```python
# tenancy/tasks.py — every tenant task receives tenant_id explicitly
from functools import wraps

from tenancy.context import reset_current_tenant, set_current_tenant
from tenancy.models import Tenant


def tenant_task(fn):
    @wraps(fn)
    def wrapper(tenant_id, *args, **kwargs):
        tenant = Tenant.objects.get(pk=tenant_id, is_active=True)
        token = set_current_tenant(tenant)
        try:
            return fn(tenant, *args, **kwargs)
        finally:
            reset_current_tenant(token)
    return wrapper


@app.task                     # Celery (third-party) or Django >= 6.0 tasks framework
@tenant_task
def generate_invoice_pdf(tenant, invoice_id, actor_id):
    invoice = Invoice.objects.get(pk=invoice_id)          # scoped by context
    ...
```

Never enqueue only an object id and let the task look it up unscoped; never rely on a context var
being "still set" in a worker; include the actor and re-authorise if permissions matter.
Scheduled jobs iterate tenants explicitly and isolate failures per tenant.

### Cache isolation

```python
# settings.py — global prefix per environment; tenant in every key
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL"),
        "KEY_PREFIX": "app-prod",
    }
}


def tenant_cache_key(tenant, *parts):
    return ":".join(["t", str(tenant.pk), *map(str, parts)])
```

- `cache_page` keys are derived from the request URL (including host — VERIFY for your version)
  and `Vary` headers. Path- or header-based tenancy with `cache_page` can serve tenant A's page to
  tenant B; add `vary_on_headers`/`vary_on_cookie` or avoid per-view caching for tenant pages.
- `{% cache %}` fragment keys include only the vary-on arguments you pass — always pass the tenant
  (and the user if content is user-specific).
- A custom `KEY_FUNCTION` that injects the context tenant is a backstop, but fails if called
  outside context — make it raise, not default.
- Rate-limit/throttle keys and session data are also cache entries; namespace them.

### Files, search, exports, logs

- Storage paths `tenants/<tenant_id>/<uuid>`; serve through a scoped view or signed URL, never by
  a guessable path ([file-upload-security.md](./file-upload-security.md)). Per-tenant buckets or
  prefixes with IAM policies add a second layer.
- Search indexes (Elasticsearch/OpenSearch/Postgres FTS): tenant id is a mandatory filter applied
  server-side; prefer per-tenant index aliases with filters for sensitive tenants.
- Exports and reports run under the tenant context and the requesting actor's permissions.
- Logs and error reports carry `tenant_id` for investigation but are themselves cross-tenant data —
  restrict access ([security-logging-and-auditing.md](./security-logging-and-auditing.md)).
- Sequential identifiers across tenants reveal business volume; use per-tenant numbering for
  display and UUIDs or opaque ids externally (not as authorisation).

### Admin and platform staff

The Django admin is a cross-tenant tool. Either scope `ModelAdmin.get_queryset` to the staff
member's assigned tenants or restrict the admin to a small platform team with MFA and audit every
cross-tenant read ([authorization-and-access-control.md](./authorization-and-access-control.md)).

## 6. Production use cases

- **B2B SaaS, subdomain per customer:** host → tenant → membership check in middleware; scoped
  managers; RLS on PII tables; per-tenant cache keys; Celery tasks carry `tenant_id`.
- **Users in several organisations:** org slug in URL (`/o/<slug>/...`), membership-derived role,
  org switcher that only lists memberships; API header selector with the same check.
- **Regulated tenant (health/finance):** dedicated database via router, separate credentials and
  encryption keys, isolated backups, no shared cache.
- **Marketplace (buyers and sellers):** not tenancy but bilateral visibility — model as ReBAC policies
  per [authorization-and-access-control.md](./authorization-and-access-control.md).

## 7. Advantages and disadvantages

| Control | Advantages | Disadvantages |
|---|---|---|
| Implicit context-scoped manager | Hard to forget in ordinary queries | Hidden dependency; surprising in tests/commands; bypass paths remain |
| Explicit `for_tenant()` selectors | Readable, testable, no global state | Relies on review discipline; add CI checks |
| Composite DB constraints | Cross-tenant references impossible | Raw SQL migrations; ORM lacks composite FKs |
| PostgreSQL RLS | Enforced even for raw SQL and bugs | Pooling/transaction coupling; debugging "missing rows"; owner bypass |
| Schema/DB per tenant | Strong separation, per-tenant ops | Migration and connection overhead; tooling complexity |

## 8. Security and performance considerations

- Index `tenant_id` and lead composite indexes with it (`(tenant_id, created_at)`); unscoped queries
  on large shared tables are both a leak risk and a performance problem.
- Unique constraints must include tenant (`UniqueConstraint(fields=["tenant", "slug"])`); a global
  unique constraint leaks existence across tenants ("slug already taken").
- Membership lookup per request: one indexed query; cache cautiously (revocation must be prompt).
- Tenant deactivation must take effect immediately for sessions, tokens, tasks and webhooks.
- Tenant deletion: hard-delete or crypto-shred per policy; include caches, files, search indexes,
  backups retention.
- RLS adds per-row predicate cost similar to an explicit filter; ensure policies use indexed columns.
- Noisy neighbours are a security-adjacent availability risk — per-tenant throttles and quotas.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| Tenant from `X-Tenant-ID` or body without membership check | Any user reads any tenant | Resolve + verify membership server-side |
| Context var not reset in `finally` | Next request on the thread uses previous tenant | `token`/`reset` pattern |
| Default manager falls back to all rows when context unset | Silent full leak in commands/tasks | Raise or `.none()` |
| Forward FK traversal to an unscoped related row | Cross-tenant data via a planted FK | Validate FKs; composite constraints |
| `ModelChoiceField(queryset=Project.objects.all())` at class level | Choice of other tenants' projects | Build queryset per request |
| Task receives only `invoice_id` | Runs in no/wrong tenant | Pass `tenant_id` + actor; re-scope |
| `cache_page` / `{% cache %}` without tenant | Tenant A's content served to B | Tenant in key/Vary |
| Session-level `SET app.tenant_id` with pooled connections | RLS context bleeds between tenants | Transaction-local `set_config(..., true)` |
| App DB role owns the tables | RLS silently bypassed | Non-owner app role or `FORCE ROW LEVEL SECURITY` |
| Global unique slugs/emails per tenant | Cross-tenant enumeration | Tenant-scoped uniqueness |

## 10. Troubleshooting and validation

- **`TenantNotSet` in management commands/tests:** intended; set context explicitly
  (`with tenant_context(t):` helper) or use `unscoped` deliberately.
- **Rows "disappear" after enabling RLS:** GUC not set in the same transaction, request not wrapped
  in a transaction, or a pooled connection used outside `atomic()`.
- **RLS has no effect:** app connects as table owner without `FORCE`, or as a superuser/`BYPASSRLS` role.
  Check with `SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user;`.
- **Intermittent cross-tenant data under load:** shared cache key, thread/context leak, or a
  module-level cached queryset.

Tenant-isolation tests — every suite has two tenants:

```python
import pytest
from django.urls import reverse


@pytest.fixture
def two_tenants(tenant_factory, user_factory, membership_factory):
    a, b = tenant_factory(slug="alpha"), tenant_factory(slug="beta")
    ua, ub = user_factory(), user_factory()
    membership_factory(tenant=a, user=ua, role="admin")
    membership_factory(tenant=b, user=ub, role="admin")
    return a, b, ua, ub


@pytest.mark.django_db
def test_cannot_read_other_tenant_invoice(client, two_tenants, invoice_factory):
    a, b, ua, _ = two_tenants
    foreign = invoice_factory(tenant=b)
    client.force_login(ua)
    url = reverse("billing:invoice-detail", args=[foreign.pk])
    assert client.get(url, HTTP_HOST="alpha.example.com").status_code == 404


@pytest.mark.django_db
def test_cannot_attach_other_tenant_project(client, two_tenants, project_factory):
    a, b, ua, _ = two_tenants
    foreign_project = project_factory(tenant=b)
    client.force_login(ua)
    r = client.post(reverse("tasks:create"), {"title": "x", "project": foreign_project.pk},
                    HTTP_HOST="alpha.example.com")
    assert r.status_code in (200, 400)       # form error, not created
    assert not foreign_project.task_set.exists()


@pytest.mark.django_db
def test_non_member_gets_404_on_tenant_host(client, two_tenants):
    _, _, ua, _ = two_tenants
    client.force_login(ua)
    assert client.get("/", HTTP_HOST="beta.example.com").status_code == 404
```

Add: tasks run with the wrong tenant id fail; cache keys differ per tenant; RLS integration test
issuing raw SQL without the GUC returns zero rows; a CI grep for `unscoped`, `_base_manager`,
`raw(`, `cursor(` in tenant apps.

## 11. Related references and skills

- [authorization-and-access-control.md](./authorization-and-access-control.md) — roles and object policies within a tenant.
- [api-security.md](./api-security.md) — scoped querysets and related fields in DRF.
- [file-upload-security.md](./file-upload-security.md) — tenant-prefixed private storage.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — tenant-tagged audit events.
- [threat-modeling.md](./threat-modeling.md) — tenancy as a trust boundary.
- [injection-prevention.md](./injection-prevention.md) — raw SQL that bypasses scoping.
- [django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md) — tenant scoping managers.
- [django-orm database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md) — composite constraints.
- [django-architecture modular-monolith](../../django-architecture/references/modular-monolith.md) — tenancy and boundaries.
- [django-core caching](../../django-core/references/caching.md) · [django-core middleware](../../django-core/references/middleware.md)
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — RLS, roles, pooling.
- [django-integrations](../../django-integrations/SKILL.md) — Celery task conventions.

## 12. Official documentation and external references

- Managers (default vs base manager): https://docs.djangoproject.com/en/5.2/topics/db/managers/
- Multiple databases and routers: https://docs.djangoproject.com/en/5.2/topics/db/multi-db/
- Cache key prefixing / `KEY_FUNCTION`: https://docs.djangoproject.com/en/5.2/topics/cache/#cache-key-prefixing
- Database transactions (`ATOMIC_REQUESTS`): https://docs.djangoproject.com/en/5.2/topics/db/transactions/
- Python `contextvars`: https://docs.python.org/3/library/contextvars.html
- PostgreSQL Row Security Policies: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- PostgreSQL `set_config`: https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADMIN-SET
- OWASP Multi-Tenant Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html (VERIFY URL)
- OWASP IDOR Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html
- django-tenants (third-party): https://django-tenants.readthedocs.io/
- PgBouncer pooling modes: https://www.pgbouncer.org/features.html
