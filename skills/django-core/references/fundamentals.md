# Django fundamentals

> Scope: Django's architecture, MVT, project vs app, component responsibilities, the app registry, the URL dispatcher, request/response objects and extension points — the mental model other references build on. Not covered here: request flow detail ([request-response-lifecycle.md](./request-response-lifecycle.md)), views ([urls-views.md](./urls-views.md)), AppConfig/`ready()` ([models-application-lifecycle.md](./models-application-lifecycle.md)), layout ([project-structure.md](./project-structure.md)), querying ([django-orm](../../django-orm/SKILL.md)), app boundaries ([django-architecture](../../django-architecture/SKILL.md)).

## Purpose and overview

Django is a "batteries-included", convention-driven web framework. It optimises for:

- **Explicit over implicit** — URLs map to views in code you can read; settings are a Python module; apps are listed in `INSTALLED_APPS`.
- **Loose coupling** — the template layer knows nothing about HTTP, the ORM knows nothing about views, middleware is a chain of callables. Each layer can be replaced (Jinja2, a different auth backend, a non-ORM data source).
- **DRY with a single source of truth** — the model defines the schema, admin, ModelForms and migrations.
- **Secure defaults** — CSRF, XSS auto-escaping, clickjacking headers and SQL parameterisation are on unless you turn them off.

Why it matters for an agent: most "Django bugs" come from fighting a convention (importing models at module load, putting logic in the wrong layer, bypassing the registry). Knowing which component owns which responsibility tells you where a fix belongs.

## How it works internally

Startup (both `manage.py` and the ASGI/WSGI entry point):

1. `DJANGO_SETTINGS_MODULE` is read; `django.conf.settings` is a **lazy** object — the module is imported on first attribute access.
2. `django.setup()` configures logging (`LOGGING_CONFIG` + `LOGGING`) and calls `apps.populate(INSTALLED_APPS)`.
3. `apps.populate()` runs in three phases:
   - **Phase 1** — import each app module and instantiate its `AppConfig`.
   - **Phase 2** — import each app's `models` module; model classes register themselves via the `ModelBase` metaclass.
   - **Phase 3** — call every `AppConfig.ready()` in `INSTALLED_APPS` order.
4. The handler (`WSGIHandler` / `ASGIHandler`) builds the middleware chain once (`load_middleware()`), then serves requests.

Per request: handler → `HttpRequest` → middleware (in) → URL resolver → view → `HttpResponse` → middleware (out) → server. Full detail in [request-response-lifecycle.md](./request-response-lifecycle.md).

Consequence: anything that touches models before phase 2 completes raises `AppRegistryNotReady` ("Apps aren't loaded yet"). Anything that queries the database at import time runs before migrations and before the test database exists.

## Core concepts and APIs

### MVT (Model–View–Template)

| Django term | Responsibility | Classic MVC analogue |
|---|---|---|
| Model | Data schema, row-level behaviour, validation | Model |
| View | Takes a request, decides what happens, returns a response | Controller |
| Template | Presentation of data the view hands over | View |
| URLconf | Maps paths to views | Router (part of controller) |

The "controller" in Django is the framework itself plus URLconf. A view is **not** a template; it is the request handler. Business logic belongs in neither the view nor the template — put it in a service/domain layer the view calls (see [project-structure.md](./project-structure.md)).

### Project vs application

| | Project | Application |
|---|---|---|
| What | The deployable configuration: settings, root URLconf, ASGI/WSGI entry points | A Python package providing one cohesive feature (models, views, admin, migrations, templates) |
| Count | One per deployment | Many |
| Reusable | No | Ideally yes (or at least self-contained) |
| Created by | `django-admin startproject config .` | `python manage.py startapp orders apps/orders` |

An app is "installed" only when listed in `INSTALLED_APPS`. Only installed apps get models registered, migrations run, templates/static discovered, management commands found and `ready()` called.

### Component map

| Component | Module | Responsibility | Depends on |
|---|---|---|---|
| Settings | `django.conf` | Configuration, lazy loading | — |
| App registry | `django.apps` | Knows installed apps and models | settings |
| ORM | `django.db` | Models, querysets, migrations, transactions | registry, settings |
| URL dispatcher | `django.urls` | Resolve paths → views; reverse names → paths | settings (`ROOT_URLCONF`) |
| HTTP | `django.http` | `HttpRequest`, `HttpResponse` and subclasses | settings |
| Handlers | `django.core.handlers` | Bridge WSGI/ASGI to Django, run middleware | all above |
| Templates | `django.template` | Engines, loaders, context processors | settings, registry (app dirs) |
| Forms | `django.forms` | Input validation and HTML rendering | (optionally) ORM |
| Contrib | `django.contrib.*` | Auth, sessions, admin, messages, staticfiles... — ordinary apps | registry, ORM |
| Signals | `django.dispatch` | In-process observer pattern | — |
| Cache | `django.core.cache` | Pluggable cache backends | settings |
| Checks | `django.core.checks` | Static validation of configuration | registry |

`contrib` apps are not special: they are installed apps like yours, which is why you can swap or omit them (with care — `admin` depends on `auth`, `contenttypes`, `sessions`, `messages`).

### ORM overview

A model class maps to a table; a `QuerySet` is a lazy, chainable query description executed on iteration, slicing with a step, `len()`, `list()`, `bool()`, or terminal methods (`get`, `count`, `exists`, `first`). Managers (`Order.objects`) are the entry point. Migrations are generated from model state diffs. Detailed querying, performance and transactions → [django-orm](../../django-orm/SKILL.md); model definition basics → [models-application-lifecycle.md](./models-application-lifecycle.md).

### URL dispatcher

`ROOT_URLCONF` points to a module with `urlpatterns`. `path()` patterns use converters (`<int:pk>`), `re_path()` uses regex, `include()` delegates a prefix to another URLconf. Resolution is **first match wins**, top to bottom. `reverse()` / `{% url %}` invert names to paths. Details in [urls-views.md](./urls-views.md).

### Request and response objects

- `HttpRequest`: `method`, `path`, `GET`, `POST` (form-encoded only), `body` (raw bytes), `FILES`, `COOKIES`, `META` (WSGI environ / ASGI-derived), `headers` (case-insensitive), `user` and `session` (added by middleware), `resolver_match` (set after URL resolution).
- `HttpResponse`: `content`, `status_code`, headers via `response["X-Foo"]` or `response.headers`, `set_cookie()`. Subclasses: `JsonResponse`, `HttpResponseRedirect`, `StreamingHttpResponse`, `FileResponse`, `TemplateResponse` (lazy rendering).

### App registry

```python
from django.apps import apps

Order = apps.get_model("orders", "Order")        # by label, avoids import cycles
config = apps.get_app_config("orders")            # AppConfig instance
apps.is_installed("django.contrib.admin")         # feature detection
```

Use `apps.get_model()` in migrations (historical models), in code that must not import another app directly, and for swappable models use `django.contrib.auth.get_user_model()` or `settings.AUTH_USER_MODEL` (string) in FK definitions.

## Implementation examples

Minimal vertical slice showing each component in its lane:

```python
# apps/orders/models.py — data and row-level invariants
class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; PAID = "paid"; EXPIRED = "expired"
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

# apps/orders/services.py — business operation, transaction boundary
@transaction.atomic
def place_order(*, customer, items) -> Order:
    order = Order.objects.create(customer=customer)
    ...
    return order

# apps/orders/views.py — HTTP translation only
@login_required
@require_POST
def order_create(request):
    form = OrderForm(request.POST)
    if not form.is_valid():
        return render(request, "orders/create.html", {"form": form}, status=400)
    order = place_order(customer=request.user, items=form.cleaned_data["items"])
    return redirect("orders:detail", pk=order.pk)
```

Bad — the same view doing everything:

```python
def order_create(request):
    order = Order.objects.create(customer_id=request.POST["customer_id"])  # trusts client, no validation
    send_mail(...)                                                          # side effect before commit
    return HttpResponse(f"<h1>Order {order.id}</h1>")                       # HTML in Python, no escaping
```

## Production patterns

- **Extension points before patches.** Prefer, in order: settings → custom backends (auth, cache, storage, email, template loaders) → middleware → signals → subclassing. Never monkey-patch Django internals; upgrades break silently.
- **Respect swappable models**: always reference `settings.AUTH_USER_MODEL`, never `django.contrib.auth.models.User` directly.
- **Treat contrib apps as dependencies**: if you remove `django.contrib.sessions`, auth's session login stops working; check `python manage.py check` after changing `INSTALLED_APPS`.
- **Pin to an LTS** (5.2 LTS is supported until April 2028) and read release notes before upgrading; run tests with `-W error::DeprecationWarning` to surface removals early. Check the installed version first: `python -m django --version`.

Main extension points:

| Need | Extension point |
|---|---|
| Alter every request/response | Middleware |
| Alter one view | Decorator / CBV mixin |
| Custom login source | `AUTHENTICATION_BACKENDS` |
| Swap storage / cache / email | `STORAGES`, `CACHES`, `EMAIL_BACKEND` |
| Validate configuration | System checks (`django.core.checks.register`) |
| Startup wiring | `AppConfig.ready()` |
| CLI operations | Management commands |
| React to framework events | Signals |
| Template helpers | Custom tags/filters, context processors |

## Decision guidance

| Question | Answer |
|---|---|
| Is this a new app or part of an existing one? | New app if it owns its own models and can be described in one noun without "and". See [project-structure.md](./project-structure.md). |
| Server-rendered templates or API? | Templates for content/admin-heavy sites; JSON API ([django-rest-framework](../../django-rest-framework/SKILL.md)) for SPA/mobile clients. Mixing is normal. |
| Use a contrib app or third-party? | Contrib first (auth, sessions, messages, staticfiles, admin); add a package only for a clear gap. |
| Import model or `apps.get_model()`? | Direct import normally; `get_model()` in migrations, to break cycles, or for optional apps. |

## Security considerations

- Defaults protect you only while you use the framework's APIs: `{{ var }}` escapes, `mark_safe`/`|safe` disable it; ORM parameterises, `raw()`/`extra()` with string formatting does not; `CsrfViewMiddleware` protects only views not marked `@csrf_exempt`.
- `request.META` and `request.headers` are client-controlled (except values your trusted proxy sets). Never use `HTTP_HOST` directly — use `request.get_host()`, which validates against `ALLOWED_HOSTS`.
- Hardening → [django-security-engineering](../../django-security-engineering/SKILL.md); settings → [production-configuration.md](./production-configuration.md).

## Performance considerations

- Startup cost is dominated by importing `INSTALLED_APPS`; keep module-level code cheap.
- QuerySets are lazy: the cost is at evaluation. N+1 queries from templates touching relations are the most common Django slowdown → [django-orm](../../django-orm/SKILL.md), [django-performance](../../django-performance/SKILL.md).
- Middleware runs on every request; template rendering is CPU-bound and benefits from the cached loader (enabled automatically when `DEBUG=False` and `loaders` is not set).

## Testing

- `django.test.TestCase` wraps each test in a transaction; `Client` exercises the full stack (middleware → URL → view); `RequestFactory` builds a request for calling a view directly without middleware.
- `python manage.py check` validates the component wiring (models, URLs, settings) — run it in CI.
- Strategy and fixtures → [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- Business logic in views, templates or model `save()` with external side effects.
- `from django.contrib.auth.models import User` in a project with a custom user model.
- Querying at import time (module-level `Category.objects.all()` used as choices) — runs before migrations, caches stale data.
- Circular imports between apps' `models.py` — use string references (`"billing.Invoice"`) in FKs.
- Treating `request.POST` as the body for JSON requests (it is empty; parse `request.body`).
- Hard-coding URLs instead of reversing names.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AppRegistryNotReady: Apps aren't loaded yet` | Model import before `django.setup()` (standalone script, module-level import in an app `__init__`) | Call `django.setup()` in scripts; move imports inside functions; use a management command instead of a script |
| `ImproperlyConfigured: Requested setting X, but settings are not configured` | `DJANGO_SETTINGS_MODULE` unset | Export it, or use `manage.py` / `pytest-django` config |
| `RuntimeError: Model class ... doesn't declare an explicit app_label and isn't in INSTALLED_APPS` | App missing from `INSTALLED_APPS` or wrong `AppConfig.name` | Add the app; fix `name` to the dotted path |
| `NoReverseMatch` | Wrong name/namespace/kwargs | See [urls-views.md](./urls-views.md) |
| Template not found | App not installed, `APP_DIRS` false, wrong directory | Check `TEMPLATES` and the "Template-loader postmortem" on the debug page |

## Official documentation

- https://docs.djangoproject.com/en/stable/misc/design-philosophies/
- https://docs.djangoproject.com/en/stable/faq/general/
- https://docs.djangoproject.com/en/stable/ref/applications/
- https://docs.djangoproject.com/en/stable/topics/http/urls/
- https://docs.djangoproject.com/en/stable/ref/request-response/
- https://docs.djangoproject.com/en/stable/topics/db/models/
- https://docs.djangoproject.com/en/stable/internals/release-process/
