# Permissions and Access Control

> **Scope.** Owns: DRF's permission architecture, built-in permission classes, custom
> `has_permission()`/`has_object_permission()`, composition and ordering, queryset scoping vs
> object permissions, ownership/role/tenant patterns, per-action permissions, and authorisation of
> writable relationships. Does not cover: identifying the caller ([authentication.md](authentication.md)),
> Django's permission/group model ([django-core authentication-sessions](../../django-core/references/authentication-sessions.md)),
> tenant-scoping managers ([django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md)),
> or broader authorisation design and threat modelling ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 1. Purpose and scope

Most API data leaks are authorisation bugs: a list that isn't scoped, an action without the
right check, a related ID accepted from another tenant. This reference gives the patterns that
prevent them in DRF.

## 2. Core concepts

**Two enforcement mechanisms — both usually needed:**

| Mechanism | Where | Applies to | Question it answers |
|---|---|---|---|
| Queryset scoping | `get_queryset()` / selector | List, detail, update, delete, actions using `get_object()` | Which objects exist *for this caller*? |
| Permission classes | `permission_classes` | `has_permission`: every request; `has_object_permission`: only via `get_object()` | May this caller perform this request / act on this object? |

**Object-level permissions are not applied to list responses.** `ListModelMixin` never calls
`has_object_permission`; only the queryset limits what a list returns.

**Built-in classes** (`rest_framework.permissions`):

| Class | Allows |
|---|---|
| `AllowAny` | Everyone (DRF's default if unset) |
| `IsAuthenticated` | Any authenticated user |
| `IsAdminUser` | `user.is_staff` |
| `IsAuthenticatedOrReadOnly` | Anyone for safe methods; authenticated for writes |
| `DjangoModelPermissions` | Django model perms (`add`/`change`/`delete`) per HTTP method; needs `queryset`. Whether `GET` requires `view` depends on `perms_map` in your release — historically empty (**VERIFY**) |
| `DjangoModelPermissionsOrAnonReadOnly` | As above, anonymous read |
| `DjangoObjectPermissions` | Per-object Django perms — requires an object-permission backend such as third-party `django-guardian` |

## 3. Internal mechanics

- `check_permissions(request)` runs in `initial()` for every permission instance from
  `get_permissions()`; the first `False` raises `permission_denied()` → `NotAuthenticated`
  (when the request is not authenticated) or `PermissionDenied`, using the class's `message`
  and `code` attributes if set.
- `check_object_permissions(request, obj)` is called by `GenericAPIView.get_object()` only.
  Every class's `has_object_permission()` must pass. `BasePermission.has_object_permission`
  returns `True` by default — a class implementing only `has_permission` is permissive at object
  level.
- Composition: `(A & B)`, `(A | B)`, `~A` build composite classes (DRF ≥ 3.9). For `|`, object
  checks evaluate `(A.has_permission and A.has_object_permission) or (B.has_permission and
  B.has_object_permission)` in recent releases (changed in DRF 3.14 — **VERIFY** for your version).
- `CreateModelMixin` does not call object permissions (no object exists yet); validate creation
  rights in `has_permission` and in serializer field scoping.
- `@action(permission_classes=[...])` replaces (not extends) the ViewSet's classes for that action.

## 4. Engineering decisions and trade-offs

| Need | Pattern |
|---|---|
| Users see only their/their tenant's data | Scope `get_queryset()`; out-of-scope objects 404 |
| Read for members, write for owners | Scoped queryset (members) + object permission (owner for unsafe methods) → 403 for members, 404 for strangers |
| Role-based (admin/editor/viewer) | Role on membership; permission class reads role; queryset scoped by membership |
| Back-office mapped to Django groups | `DjangoModelPermissions` (with a custom `perms_map` adding `view` if needed) |
| Per-object ACLs | Domain model of grants + scoped queryset; `DjangoObjectPermissions` + django-guardian when its model fits |
| Scopes from tokens/API keys | Permission class reading `request.auth` scopes |

**404 vs 403.** Return 404 for objects the caller must not know exist (scoping does this);
return 403 when existence is not secret but the action is forbidden.

## 5. Django/DRF implementation patterns

**Scope first, then object checks:**

```python
class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated, IsProjectAdminOrReadOnly]

    def get_queryset(self):
        return Project.objects.filter(memberships__user=self.request.user).distinct()

class IsProjectAdminOrReadOnly(permissions.BasePermission):
    message = "Only project admins can modify this project."
    code = "project_admin_required"

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.memberships.filter(user=request.user, role=Membership.Role.ADMIN).exists()
```

(For lists of objects with role-dependent fields, annotate the caller's role in `get_queryset()`
instead of querying per object.)

**Multi-tenant scoping as a reusable base or manager:**

```python
class OrganizationScopedMixin:
    def get_queryset(self):
        return super().get_queryset().filter(organization=self.request.user.organization)
```

Prefer a QuerySet method (`Project.objects.for_organization(org)`) so services and tasks share
the same scoping ([django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md)).

**Per-action permissions:**

```python
def get_permissions(self):
    if self.action in ("list", "retrieve"):
        classes = [IsAuthenticated]
    elif self.action in ("approve", "reject"):
        classes = [IsAuthenticated, IsApprover]
    else:
        classes = [IsAuthenticated, IsOwner]
    return [cls() for cls in classes]
```

**Composition:**

```python
permission_classes = [IsAuthenticated & (IsOwner | IsAdminUser)]
```

**Token scopes:**

```python
class HasScope(permissions.BasePermission):
    required_scope: str = ""

    @classmethod
    def for_scope(cls, scope):
        return type(f"HasScope_{scope}", (cls,), {"required_scope": scope})

    def has_permission(self, request, view):
        scopes = getattr(request.auth, "scopes", ())
        return self.required_scope in scopes
```

**Writable relationship authorisation** — scope related-field querysets
([nested-and-relational-serializers.md](nested-and-relational-serializers.md)) and set ownership
server-side:

```python
def perform_create(self, serializer):
    serializer.save(organization=self.request.user.organization, created_by=self.request.user)
```

**Objects loaded manually must be checked manually:**

```python
target = get_object_or_404(Project.objects.for_user(request.user), pk=request.data["target_id"])
self.check_object_permissions(request, target)
```

## 6. Production use cases

- **SaaS multi-tenancy**: every `get_queryset()` filtered by organization (resolved from the
  authenticated user or verified membership); related fields scoped; tests per endpoint with two
  tenants.
- **Owner-or-admin edit**: `IsAuthenticated & (IsOwner | IsAdminUser)` plus scoping.
- **Approval workflow**: `approve` action restricted to an approver role; the service re-checks
  the state transition.
- **Partner API**: API-key scopes checked by a permission class; tenant derived from the key.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Queryset scoping | Covers list and detail; 404 hides existence; single place | Must be applied everywhere objects are loaded (actions, serializers, services) |
| Object permissions | Expressive per-object rules, 403 messages | Not applied to lists or manually loaded objects; per-object queries |
| `DjangoModelPermissions` | Reuses admin groups | Model-level only; `view` perm not enforced for GET by default |
| Composition operators | Readable combinations | Semantics of `|` at object level changed across versions |

## 8. Security and performance considerations

- Set `DEFAULT_PERMISSION_CLASSES` to at least `IsAuthenticated`; `AllowAny` must be explicit.
- Object permissions that query (`obj.memberships.filter(...)`) cost a query per detail request —
  acceptable; never call them per row in lists.
- Check every `@action`, every nested route's parent, every related ID and every bulk endpoint.
- Don't base authorisation on client-supplied fields (`organization_id` in the payload).
- Services called from views should also accept the actor and enforce invariants that matter
  regardless of entry point ([django-architecture service-layer](../../django-architecture/references/service-layer.md)).

## 9. Common mistakes and anti-patterns

- `queryset = Model.objects.all()` + `IsOwner` object permission: lists leak everything.
- Assuming `IsAuthenticated` means "may access this object".
- Custom class implementing only `has_object_permission` but placed where lists are served —
  believed to protect them.
- Permission classes doing heavy queries in `has_permission` for every request.
- Overriding `get_object()` and forgetting `check_object_permissions()`.
- Returning 403 for other tenants' objects (confirms existence).

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| Other users' rows in a list | Unscoped `get_queryset()` |
| Object permission never runs | Object not loaded via `get_object()`; or action overrides classes |
| 401/403 confusion | Unauthenticated → `NotAuthenticated`; see [authentication.md](authentication.md) |
| `DjangoModelPermissions` assertion error | View lacks `queryset`/`get_queryset()` |
| Composite `|` behaves unexpectedly on objects | Version-specific semantics — check release notes |

Test matrix per endpoint: anonymous, authenticated stranger (404), member without role (403),
authorised role (2xx), other tenant (404) ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [authentication.md](authentication.md)
- [views-and-viewsets.md](views-and-viewsets.md) — `get_queryset()`, `get_object()`, actions
- [nested-and-relational-serializers.md](nested-and-relational-serializers.md) — scoped related fields
- [filtering-search-and-ordering.md](filtering-search-and-ordering.md) — filters apply after scoping
- [django-orm managers-and-querysets](../../django-orm/references/managers-and-querysets.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md)

## 12. Official documentation and external references

- Permissions: <https://www.django-rest-framework.org/api-guide/permissions/>
- Generic views — `get_object()`: <https://www.django-rest-framework.org/api-guide/generic-views/#get_objectself>
- Django permissions: <https://docs.djangoproject.com/en/5.2/topics/auth/default/#permissions-and-authorization>
- Third-party: django-guardian <https://django-guardian.readthedocs.io/>, drf-access-policy, django-rules (evaluate before adopting)
- OWASP API Security Top 10 (BOLA): <https://owasp.org/API-Security/>
