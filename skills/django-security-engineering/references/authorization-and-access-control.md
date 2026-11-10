# Authorization and Access Control

> Scope: authentication vs authorisation; Django model permissions and groups and their limits;
> object-level authorisation; RBAC, ABAC and relationship-based models; ownership checks; policy
> functions and scoped selectors; enforcement in views, services, templates and the admin;
> related-object and state-transition authorisation; privilege escalation; IDOR prevention;
> permission caching; permission-matrix testing.
> Not covered here: verifying identity ([authentication-security.md](./authentication-security.md));
> DRF permission classes and API-specific exposure ([api-security.md](./api-security.md),
> [django-rest-framework permissions](../../django-rest-framework/references/permissions-and-access-control.md));
> tenant isolation ([multi-tenant-security.md](./multi-tenant-security.md)); custom manager
> mechanics ([django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md));
> where services and selectors live ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).

## 1. Purpose and scope

Broken access control is the top OWASP risk category, and Django cannot prevent it for you: the
framework knows who the user is, but only your code knows *which invoices that user may see*.
This reference covers how to design an authorisation model, enforce it at a choke point that
every entry point (view, API, admin, task, command) passes through, and prove it with tests.

Code targets Django 5.2 LTS; examples were not executed.

## 2. Core concepts

| Term | Meaning |
|---|---|
| Authentication | Establishes identity (`request.user`). Says nothing about access. |
| Authorisation | Decides whether *this actor* may perform *this action* on *this resource* now. |
| Model permission | Django's `app_label.codename` (`add/change/delete/view_<model>` + custom); type-level. |
| Object permission | Decision about a specific row. Not provided by `ModelBackend`. |
| RBAC | Permissions granted via roles (Django groups, or a membership role field). |
| ABAC | Decisions from attributes of actor, resource and context (department, status, amount, time). |
| ReBAC | Decisions from relationships (owner, member of project, manager of team). |
| Policy | A function/class encoding a decision: `can_approve(user, expense) -> bool`. |
| Scope | The set of rows an actor may access, expressed as a QuerySet. |
| IDOR | Insecure Direct Object Reference: accessing a resource by changing its identifier because the lookup was not scoped. |
| Default-deny | Absence of a grant means no access. |

**Framework provides:** `User.has_perm()/has_perms()/has_module_perms()`, groups, auto-created model
permissions, `permission_required`/`PermissionRequiredMixin`/`UserPassesTestMixin`, `perms` template
context, admin permission hooks, `PermissionDenied` → 403 handling, pluggable backends for object
permissions.

**Developer owns:** the authorisation model, object-level rules, query scoping, enforcement in every
entry point, protection of permission-bearing fields, and tests that prove denial.

## 3. Internal mechanics

### Permission resolution

`user.has_perm(perm, obj=None)`:

1. Inactive user → `False` (via the backend; `ModelBackend` returns no permissions for inactive users).
2. Active superuser → `True` unconditionally, for every perm and every `obj`.
3. Otherwise each backend in `AUTHENTICATION_BACKENDS` with a `has_perm` method is asked; the first
   `True` wins; a backend raising `PermissionDenied` stops evaluation with `False`.

`ModelBackend.has_perm(user, perm, obj)` returns `False` whenever `obj is not None` — it only
answers type-level questions. Object permissions need another backend (django-guardian,
django-rules — third-party) or, more commonly, explicit policy code.

### Caching

`ModelBackend` caches permission sets on the user instance (`_user_perm_cache`,
`_group_perm_cache`, `_perm_cache`). Granting a permission and then calling `has_perm` on the same
instance returns the stale result; re-fetch the user (`User.objects.get(pk=...)`). Across
requests there is no cache unless you add one — each request loads a fresh user.

### Mixins and decorators

- `permission_required("app.perm", raise_exception=False)` / `PermissionRequiredMixin`: anonymous
  users are redirected to login; authenticated users without the permission get `PermissionDenied`
  (403) via `AccessMixin.handle_no_permission` (mixins raise 403 for authenticated users regardless
  of `raise_exception`; the decorator redirects unless `raise_exception=True`).
- `UserPassesTestMixin.test_func()` — runs **before** `get_object()`, so it cannot see the object
  unless you load it yourself; prefer scoping `get_queryset()` for object rules.
- Generic `DetailView/UpdateView/DeleteView` call `get_object()` → `get_queryset().get(pk=...)`;
  overriding `get_queryset()` makes out-of-scope ids raise `Http404`.

### Admin

`ModelAdmin` enforces model permissions through `has_view_permission`, `has_add_permission`,
`has_change_permission`, `has_delete_permission` (each receives `request` and optionally `obj`) and
`has_module_permission`. Row visibility is `get_queryset(request)`; FK choices are
`formfield_for_foreignkey`; bulk actions call `has_<perm>_permission` via `allowed_permissions`.
The admin requires `is_staff` (and `is_active`) to log in.

## 4. Engineering decisions and trade-offs

| Model | Use when | Costs |
|---|---|---|
| Model perms + groups (built-in) | Back-office features, staff capabilities, "can export reports" | No per-object rules; groups grow unmanaged |
| Ownership (`obj.owner == user`) | Personal data, user-generated content | Breaks down with sharing/teams |
| Membership roles (ReBAC-lite: `Membership(user, org, role)`) | SaaS orgs, projects, teams | Needs scoping everywhere; role inheritance design |
| ABAC policies | Approvals by amount/status/department, time-bound access | Harder to audit; must be centralised and tested |
| Per-object ACL (django-guardian, third-party) | Arbitrary sharing of individual objects | Row explosion; query cost for lists |
| Rule engine (django-rules, third-party) | Predicate composition reused in views/templates/DRF | Predicates evaluate per object; lists still need querysets |
| External policy engine (OPA, Oso, OpenFGA, Cedar — third-party) | Many services sharing one policy | Network dependency, list-filtering complexity |

Key decisions:

- **Express rules twice, consistently: as a predicate and as a scope.** `can_view(user, obj)` for
  single objects and `visible_to(user)` QuerySet for lists. Both live in the same module and share
  tests. Lists filtered in Python after fetching do not scale and leak counts.
- **404 vs 403:** return 404 for objects outside the actor's *visibility* scope (do not confirm
  existence); return 403 when the actor can see the object but not perform the action.
- **Where to enforce:** the service/selector is the choke point. Views add the HTTP translation.
  Templates only hide UI. The admin and DRF are separate entry points that must reuse the same
  policies.
- **Superuser semantics:** `is_superuser` bypasses every `has_perm`. Restrict superusers to a few
  break-glass accounts with MFA; do not use superuser as the "admin role" in your product.

## 5. Django implementation patterns

### Policies and scopes in one module

```python
# billing/policies.py
from django.db.models import Q, QuerySet

from billing.models import Invoice
from orgs.models import Role


def invoices_visible_to(user) -> QuerySet[Invoice]:
    if not user.is_authenticated:
        return Invoice.objects.none()
    return Invoice.objects.filter(
        Q(organisation__memberships__user=user)
        & Q(organisation__memberships__is_active=True)
    ).distinct()


def can_edit_invoice(user, invoice: Invoice) -> bool:
    if invoice.status != Invoice.Status.DRAFT:
        return False                              # state-dependent rule
    return invoice.organisation.memberships.filter(
        user=user, is_active=True, role__in=[Role.OWNER, Role.BILLING_ADMIN]
    ).exists()


def can_approve_invoice(user, invoice: Invoice) -> bool:
    if invoice.created_by_id == user.pk:
        return False                              # separation of duties
    limit = user.approval_limit or 0              # ABAC: attribute of actor
    return invoice.total <= limit and user.has_perm("billing.approve_invoice")
```

A custom QuerySet method (`Invoice.objects.visible_to(user)`) is equivalent; see
[django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md).
Avoid `.distinct()` costs on large tables by filtering via `Exists(Subquery)` instead of a join.

### Views: scope the lookup, then check the action

```python
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404


@require_http_methods(["GET", "POST"])
def invoice_edit(request, pk):
    invoice = get_object_or_404(invoices_visible_to(request.user), pk=pk)   # 404 if not visible
    if not can_edit_invoice(request.user, invoice):
        raise PermissionDenied                                             # 403 if visible but not editable
    form = InvoiceForm(request.POST or None, instance=invoice, user=request.user)
    if request.method == "POST" and form.is_valid():
        update_invoice(actor=request.user, invoice=invoice, data=form.cleaned_data)
        return redirect(invoice)
    return render(request, "billing/invoice_form.html", {"form": form})
```

```python
class InvoiceUpdateView(LoginRequiredMixin, UpdateView):
    model = Invoice
    form_class = InvoiceForm

    def get_queryset(self):
        return invoices_visible_to(self.request.user).select_related("organisation")

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_edit_invoice(self.request.user, obj):
            raise PermissionDenied
        return obj
```

### Services enforce too (pass the actor)

```python
# billing/services.py
from django.db import transaction


@transaction.atomic
def approve_invoice(*, actor, invoice_id: int) -> Invoice:
    invoice = invoices_visible_to(actor).select_for_update().get(pk=invoice_id)
    if not can_approve_invoice(actor, invoice):
        raise PermissionDenied("Not allowed to approve this invoice.")
    if invoice.status != Invoice.Status.SUBMITTED:
        raise InvalidTransition(invoice.status, Invoice.Status.APPROVED)
    invoice.status = Invoice.Status.APPROVED
    invoice.approved_by = actor
    invoice.save(update_fields=["status", "approved_by", "updated_at"])
    audit("invoice_approved", actor=actor, target=invoice)
    return invoice
```

Enforcing in the service means views, DRF, admin actions, tasks and management commands all get
the same rule. Background tasks must receive an actor id (or an explicit system actor) and re-check;
they must never trust that "the view already checked" if the state can change between enqueue and run.

### Related-object authorisation (forms and serializers)

A `ModelChoiceField` defaults to `Model.objects.all()` — any id in the database validates.

```python
class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["customer", "due_date", "notes"]          # explicit; never "__all__"

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = customers_visible_to(user)
```

DRF equivalent: scope `PrimaryKeyRelatedField(queryset=...)` per request in `__init__` or
`get_fields()` ([api-security.md](./api-security.md)).

### Privilege escalation via fields

- Never include `is_staff`, `is_superuser`, `groups`, `user_permissions`, `role`, `owner`,
  `organisation`, `approved_by`, `status` in user-editable forms/serializers. Set them in services.
- Role assignment is itself an authorised action: an org admin may grant roles ≤ their own and
  never `is_staff`; check it in the service.
- `UserChangeForm`-style admin forms are for staff only; customise `ModelAdmin.get_readonly_fields`
  so non-superuser staff cannot edit `is_superuser`/`user_permissions`.

### State-transition authorisation

Model transitions as a table of `(from_state, to_state) → policy`. Check both the transition's
validity and the actor's right to make it, inside the same transaction, with a row lock or a
conditional update (`filter(pk=..., status=SUBMITTED).update(...)`) to avoid a race that skips the check
([django-orm transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)).

### Templates hide, they do not protect

```django
{% if perms.billing.approve_invoice %}
  <form method="post" action="{% url 'billing:approve' invoice.pk %}">{% csrf_token %}
    <button>Approve</button>
  </form>
{% endif %}
```

The `approve` view must still call `can_approve_invoice`. For object rules, pass precomputed
booleans (`can_approve`) from the view rather than calling policies in templates.

### Admin hardening

```python
@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["number", "organisation", "status", "total"]
    readonly_fields = ["approved_by", "created_by"]

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("organisation")
        if request.user.is_superuser:
            return qs
        return qs.filter(organisation__in=request.user.support_organisations.all())

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organisation" and not request.user.is_superuser:
            kwargs["queryset"] = request.user.support_organisations.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
```

Also: autocomplete endpoints (`autocomplete_fields`) use the *related* admin's `get_search_results`
and permission checks — scope the related admin too. Custom admin views must be wrapped with
`self.admin_site.admin_view(...)`.

### Object permissions backend (optional)

```python
# Only if you want has_perm(perm, obj) semantics everywhere (e.g. with django-rules/guardian).
class PolicyBackend:
    def authenticate(self, request, **kwargs):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        if obj is None or not user_obj.is_active:
            return False
        policy = OBJECT_POLICIES.get(perm)
        return bool(policy and policy(user_obj, obj))
```

Remember lists still need scopes; a per-object `has_perm` over 10,000 rows is an N+1 of policy checks.

## 6. Production use cases

- **SaaS organisations:** `Membership(user, org, role)`; selectors scoped by active membership;
  role hierarchy owner > admin > member > viewer; invite flow is an authorised action.
- **Approval workflow:** ABAC limits by amount and department, separation of duties (creator ≠
  approver), transitions locked in a transaction, audit trail per decision.
- **Support staff impersonation/read access:** time-boxed, ticket-referenced grant; read-only
  scope; every access audited; banner shown to the user.
- **File downloads:** download view resolves the file through the scoped queryset, never by
  storage path ([file-upload-security.md](./file-upload-security.md)).
- **Reports and exports:** run under the requesting actor's scope; exports are as sensitive as
  the list view and need the same tests.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Scoped selectors + policy functions | Explicit, testable, works for lists and objects, no dependencies | Discipline required: every entry point must use them |
| Built-in model permissions | Integrated with admin, templates, decorators | Type-level only; superuser bypass; group sprawl |
| django-guardian ACLs | Arbitrary per-object sharing | Large permission tables; list filtering cost |
| django-rules predicates | Composable, integrates with `has_perm` and DRF | Object-at-a-time; scopes still separate |
| External policy engine | Centralised across services | Latency, availability, partial-evaluation complexity |

## 8. Security and performance considerations

- **IDOR root cause** is always an unscoped lookup: `Model.objects.get(pk=request.GET["id"])`.
  Random UUIDs reduce guessability but are not authorisation — ids leak via URLs, logs, emails and
  referrers.
- **Indirect references** (FKs in payloads, filter params like `?owner=5`, nested routes
  `/orgs/1/projects/9/`) must be validated against the parent scope: check that project 9 belongs to
  org 1 *and* the user belongs to org 1.
- **Mass operations** (`bulk_update`, admin actions, `QuerySet.update()`) bypass per-object policy
  checks — apply the scope filter to the queryset first.
- **Counts and existence leak:** `exists()`, totals, autocomplete and search suggestions must be scoped.
- **Caching decisions:** cache per (actor, permission, object version) only with short TTLs and
  explicit invalidation on role changes; stale grants are privilege persistence after revocation.
- **Performance:** scopes should use indexed join keys (`organisation_id`, membership `(user_id, org_id)`);
  use `Exists()` over `distinct()` joins; precompute role per request (e.g. one membership query in
  middleware, stored on `request`).
- **Revocation:** removing a role must take effect on the next request — avoid baking roles into
  long-lived sessions or JWT claims without a revocation strategy.

## 9. Common mistakes and anti-patterns

| Mistake | Consequence | Fix |
|---|---|---|
| `queryset = Model.objects.all()` in a detail/update view behind `LoginRequiredMixin` | IDOR | Scoped `get_queryset()` |
| `user.has_perm("app.change_x", obj)` with only `ModelBackend` | Always `False` (or code falls back to unconditional allow) | Policy function or object backend |
| Authorising in the template or by hiding buttons | Direct POST succeeds | Check in view/service |
| `UserPassesTestMixin` checking `self.get_object()` with an unscoped queryset | Works, but error-prone and double query | Scope queryset; check action in `get_object` |
| Forms with `fields = "__all__"` or `ModelChoiceField` over all rows | Mass assignment, cross-owner FK | Explicit fields; scoped choice querysets |
| Trusting `owner_id`/`org_id` from the request | Write into another account | Set from `request.user`/membership in service |
| Checking at enqueue time only | Stale authorisation in tasks | Re-check in task with actor id |
| Using `is_superuser` as product admin role | Unlimited bypass | Product roles via memberships/perms |
| 403 for objects user cannot see | Existence oracle | 404 |
| Admin `get_queryset` scoped but `autocomplete_fields` related admin not | Data leak via autocomplete | Scope every admin involved |

## 10. Troubleshooting and validation

- **"Has permission but still 403":** stale `_perm_cache` (re-fetch user), inactive user, a backend
  raising `PermissionDenied`, or permission codename/app label mismatch (`has_perm("billing.approve_invoice")`).
  Check `Permission.objects.filter(codename=...)` exists (custom permissions are created by `migrate`
  via `post_migrate`).
- **Custom permissions missing:** `Meta.permissions` added but migrations not run in that DB.
- **Lists show other users' rows:** a manager method used in the view but the template iterates a
  related manager (`org.invoices.all()`) that is unscoped; or a cache fragment shared across users.

Permission-matrix testing (pattern; details in [security-testing-and-incident-response.md](./security-testing-and-incident-response.md)):

```python
import pytest
from django.urls import reverse

CASES = [
    # (actor fixture, expected status)
    ("anonymous", 302),
    ("other_org_member", 404),
    ("viewer", 403),
    ("billing_admin", 200),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("actor", "expected"), CASES)
def test_invoice_edit_matrix(client, request, invoice, actor, expected):
    user = request.getfixturevalue(actor)
    if user is not None:
        client.force_login(user)
    response = client.get(reverse("billing:invoice-edit", args=[invoice.pk]))
    assert response.status_code == expected
```

Cover every unsafe method and custom action, not only GET; include "same org, wrong state" and
"creator approving own invoice" cases. A route inventory test (iterate the URLconf and assert each
view has an explicit policy annotation) catches newly added unprotected views.

## 11. Related references and skills

- [authentication-security.md](./authentication-security.md) — identity, MFA, step-up for sensitive actions.
- [api-security.md](./api-security.md) — DRF permission defaults, object permissions, serializer exposure.
- [multi-tenant-security.md](./multi-tenant-security.md) — tenant isolation beyond per-user scope.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — logging authorisation failures and grants.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — permission matrices.
- [threat-modeling.md](./threat-modeling.md) — identifying elevation-of-privilege threats.
- [django-core authentication-sessions](../../django-core/references/authentication-sessions.md) — permission and group mechanics.
- [django-architecture service-layer](../../django-architecture/references/service-layer.md) — where policies and selectors live.
- [django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md) — scoped QuerySet methods.
- [django-rest-framework permissions-and-access-control](../../django-rest-framework/references/permissions-and-access-control.md) — DRF implementation.

## 12. Official documentation and external references

- Permissions and authorisation: https://docs.djangoproject.com/en/5.2/topics/auth/default/#permissions-and-authorization
- Custom permissions and backends: https://docs.djangoproject.com/en/5.2/topics/auth/customizing/#handling-authorization-in-custom-backends
- Auth mixins and decorators: https://docs.djangoproject.com/en/5.2/topics/auth/default/#limiting-access-to-logged-in-users
- `ModelAdmin` permission methods: https://docs.djangoproject.com/en/5.2/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission
- OWASP Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- OWASP Insecure Direct Object Reference Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html
- OWASP Top 10 A01 Broken Access Control: https://owasp.org/Top10/A01_2021-Broken_Access_Control/
- OWASP ASVS V4 (Access Control): https://owasp.org/www-project-application-security-verification-standard/
- NIST SP 800-162 (ABAC): https://csrc.nist.gov/pubs/sp/800/162/upd2/final
- django-guardian: https://django-guardian.readthedocs.io/ · django-rules: https://github.com/dfunckt/django-rules
