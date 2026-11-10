# API Versioning and Documentation

> **Scope.** Owns: API versioning principles, DRF's versioning classes and settings,
> compatibility, deprecation, OpenAPI schema generation (built-in and third-party), schema
> customisation, documenting endpoints, authentication, examples and errors, and CI checks for
> contract changes. Does not cover: URL structure ([routers-and-url-design.md](routers-and-url-design.md)),
> error body design ([exception-handling-and-error-responses.md](exception-handling-and-error-responses.md)),
> or release/deployment process ([django-testing](../../django-testing/SKILL.md) for CI test strategy).

## 1. Purpose and scope

An API is a contract. Versioning controls how it changes; documentation states what it is. This
reference covers both as implemented in DRF and its ecosystem.

## 2. Core concepts

**Compatible vs breaking changes.**

| Usually compatible | Breaking |
|---|---|
| Adding an optional request field | Removing/renaming a field, changing its type or meaning |
| Adding a response field | Making an optional field required |
| Adding an endpoint or enum value (if clients tolerate unknowns) | Changing status codes, error codes or pagination envelope |
| Relaxing validation | Tightening validation, changing defaults |

**DRF versioning classes** (`rest_framework.versioning`):

| Class | Version source | Example |
|---|---|---|
| `URLPathVersioning` | `version` URL kwarg | `/api/v1/orders/` |
| `NamespaceVersioning` | URL namespace | `include(..., namespace="v1")` |
| `AcceptHeaderVersioning` | `Accept` media-type parameter | `Accept: application/json; version=1.0` |
| `HostNameVersioning` | Hostname | `v1.api.example.com` |
| `QueryParameterVersioning` | Query param | `/orders/?version=1` |

There is **no `DefaultVersioning` class** in DRF. "Default version" is the `DEFAULT_VERSION`
setting (used when the client supplies none); versioning is off unless
`DEFAULT_VERSIONING_CLASS` is set. Related settings: `ALLOWED_VERSIONS`, `VERSION_PARAM`.

**Schema generation.**

| Tool | Package | Status |
|---|---|---|
| `rest_framework.schemas.openapi` (`generateschema`, `get_schema_view`) | Built into DRF | Deprecated in favour of third-party packages (deprecation notice since DRF 3.15 — **VERIFY** removal timeline) |
| CoreAPI schemas | Built-in (legacy) | Deprecated; do not use for new work |
| `drf-spectacular` | Third-party | OpenAPI 3; widely used; `@extend_schema`, `manage.py spectacular` |
| `drf-yasg` | Third-party | OpenAPI 2.0 (Swagger) only |

Swagger UI / ReDoc are separate front-ends that render an OpenAPI document; drf-spectacular
ships views for them (with optional `drf-spectacular-sidecar` for self-hosted assets).

## 3. Internal mechanics

- During `initial()`, `determine_version()` calls the versioning class's
  `determine_version(request, *args, **kwargs)`, setting `request.version` and
  `request.versioning_scheme`. Disallowed versions raise `NotFound` (path/namespace/host/query) or
  `NotAcceptable` (Accept header).
- `rest_framework.reverse.reverse()` delegates to `request.versioning_scheme.reverse()` so
  generated URLs keep the caller's version (e.g. `NamespaceVersioning` reverses within the
  namespace).
- Schema generators walk the URLconf, instantiate each view per method/action, and introspect
  serializers, filters, pagination and authentication. Anything computed dynamically per request
  (`get_serializer_class()` reading `request.user`) is invisible or guessed — annotate it.

## 4. Engineering decisions and trade-offs

| Scheme | Pros | Cons |
|---|---|---|
| URL path | Visible, cache-friendly, easy routing and docs per version | URLs change per version |
| Namespace | Same as path, cleaner reversing | Requires namespaced includes |
| Accept header | URLs stable; "pure" REST | Hard to test in browsers; caches need `Vary: Accept`; less visible |
| Hostname | Clean separation by deployment | DNS/TLS per version; awkward locally |
| Query param | Easy to try | Easily dropped by clients/proxies; mixes with filters |

Recommendation: **URL path** (or namespace) with major versions only (`v1`, `v2`); evolve within
a version through compatible changes; create a new major version only for unavoidable breaks.
Version the *API*, not individual endpoints, unless the organisation's policy says otherwise.

**Implementing a v2.** Prefer branching at the edges — `get_serializer_class()` by
`request.version` — while services stay version-agnostic. Separate ViewSets per version only when
behaviour diverges substantially.

**Deprecation.** Announce, document, measure usage per version, signal with response headers
(`Deprecation`, `Sunset` — RFC 9745 / RFC 8594), then remove after the published date.

## 5. Django/DRF implementation patterns

**URL path versioning:**

```python
REST_FRAMEWORK = {
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1", "v2"],
}

urlpatterns = [path("api/<str:version>/", include("config.api_urls"))]
```

**Version-specific serializers:**

```python
def get_serializer_class(self):
    if self.request.version == "v2":
        return OrderOutputSerializerV2      # e.g. money as {"amount", "currency"}
    return OrderOutputSerializer
```

**Sunset headers for a deprecated version** (middleware or a shared `finalize_response`):

```python
class DeprecationHeadersMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if getattr(request, "version", None) == "v1":
            response["Deprecation"] = "@1767225600"          # RFC 9745 structured date (VERIFY format)
            response["Sunset"] = "Wed, 30 Jun 2027 23:59:59 GMT"
            response["Link"] = '<https://docs.example.com/migrate-v2>; rel="deprecation"'
        return response
```

**drf-spectacular setup (third-party):**

```python
INSTALLED_APPS += ["drf_spectacular"]
REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"] = "drf_spectacular.openapi.AutoSchema"
SPECTACULAR_SETTINGS = {"TITLE": "Acme API", "VERSION": "1.0.0", "SERVE_INCLUDE_SCHEMA": False}

urlpatterns += [
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
```

**Annotating what introspection can't see:**

```python
@extend_schema(
    request=CancelOrderInputSerializer,
    responses={200: OrderDetailOutputSerializer, 409: ProblemSerializer},
    examples=[OpenApiExample("Cancel", value={"reason": "customer_request"}, request_only=True)],
    description="Cancels an unshipped order. Idempotent: cancelling a cancelled order returns 200.",
)
@action(detail=True, methods=["post"])
def cancel(self, request, pk=None): ...
```

Document authentication via `SPECTACULAR_SETTINGS`/auth extensions (drf-spectacular maps built-in
DRF authentication classes and common third-party ones; custom classes need an
`OpenApiAuthenticationExtension`).

**Built-in DRF generator (if already in use):**

```bash
python manage.py generateschema --file openapi-schema.yml   # needs PyYAML (and uritemplate)
```

**CI contract check:**

```bash
python manage.py spectacular --file schema.yml --validate --fail-on-warn
git diff --exit-code schema.yml        # changes must be committed and reviewed
```

## 6. Production use cases

- **Public API**: `/api/v1/` path versioning, committed OpenAPI file, published docs, changelog,
  `Sunset` headers before removal.
- **Mobile apps**: old app versions keep calling `v1` for months; usage metrics per version
  decide sunset dates.
- **Client SDK generation** from the committed schema; schema accuracy is therefore a test target.
- **Internal APIs**: single version, additive changes only, schema diff required in review.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Versioning makes breaking changes explicit and schedulable | Each live version multiplies tests and maintenance |
| Generated schemas stay close to code | Generated schemas are only as accurate as annotations |
| Committed schema diffs surface accidental contract changes | Third-party schema tools have their own upgrade cadence |

## 8. Security and performance considerations

- Decide whether docs/schema endpoints are public; they reveal the full surface area. Protect
  internal API docs with authentication or don't deploy them.
- Old versions keep old security behaviour — sunset them; don't leave unmaintained versions live.
- Schema generation in a request (`SpectacularAPIView`) is expensive; cache it or serve a static
  file in production.
- Examples in docs must not contain real tokens or customer data.

## 9. Common mistakes and anti-patterns

- Assuming DRF bundles drf-spectacular, Swagger UI or JWT docs.
- Breaking changes inside `v1` ("it's just a rename").
- A version per endpoint with no policy.
- `QueryParameterVersioning` mixed with filter params on public APIs.
- Schema not validated in CI; hand-edited schema drifting from code.
- `SerializerMethodField` without return type hints → schema type `string`.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| `request.version` is `None` | No `DEFAULT_VERSIONING_CLASS`; URL lacks `version` kwarg |
| 404 "Invalid version in URL path" | Version not in `ALLOWED_VERSIONS` |
| Schema warnings "could not resolve serializer" | `APIView` without `serializer_class`; add `@extend_schema` |
| Wrong schema for actions | `get_serializer_class()` branches on `request` attributes unknown at generation time |
| `reverse()` produces wrong version URLs | Using `django.urls.reverse` instead of `rest_framework.reverse.reverse` with `request` |

## 11. Related references and skills

- [routers-and-url-design.md](routers-and-url-design.md)
- [exception-handling-and-error-responses.md](exception-handling-and-error-responses.md)
- [pagination.md](pagination.md) — documenting envelopes
- [drf-testing-and-debugging.md](drf-testing-and-debugging.md) — schema checks in tests
- [django-architecture architecture-decision-records](../../django-architecture/references/architecture-decision-records.md) — recording versioning policy

## 12. Official documentation and external references

- Versioning: <https://www.django-rest-framework.org/api-guide/versioning/>
- Schemas: <https://www.django-rest-framework.org/api-guide/schemas/>
- Documenting your API: <https://www.django-rest-framework.org/topics/documenting-your-api/>
- drf-spectacular (third-party): <https://drf-spectacular.readthedocs.io/>
- OpenAPI Specification: <https://spec.openapis.org/oas/latest.html>
- RFC 8594 (Sunset): <https://www.rfc-editor.org/rfc/rfc8594>; RFC 9745 (Deprecation header): <https://www.rfc-editor.org/rfc/rfc9745> (**VERIFY** RFC number/status)
