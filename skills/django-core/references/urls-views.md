# URLs and views

> Scope: URL configuration, converters, namespaces, reversing; function-based, class-based and generic views; request/response objects, redirects, decorators, view composition, error views, view testing. Not covered here: request flow internals ([request-response-lifecycle.md](./request-response-lifecycle.md)), forms ([forms-validation.md](./forms-validation.md)), auth decorators' semantics ([authentication-sessions.md](./authentication-sessions.md)), view caching ([caching.md](./caching.md)), async views ([asynchronous-django.md](./asynchronous-django.md)), API views ([django-rest-framework](../../django-rest-framework/SKILL.md)), querysets ([django-orm](../../django-orm/SKILL.md)).

## Purpose and overview

URLconfs map paths to callables; views translate an `HttpRequest` into an `HttpResponse`. Keep views **thin**: parse input, enforce access, call a service/selector, render. Business logic in views cannot be reused by commands, tasks or APIs — see [django-architecture](../../django-architecture/SKILL.md).

## How it works internally

- `ROOT_URLCONF` (or `request.urlconf`) names a module with `urlpatterns`. The resolver walks patterns **in order** and stops at the first match; `include()` creates a nested resolver that matches a prefix and continues with the remainder.
- `path()` compiles its route into a regex using converters; each converter's `to_python()` runs on match and `to_url()` on reverse. A `ValueError` from `to_python()` means "no match, try the next pattern"; other exceptions propagate as errors.
- The match produces a `ResolverMatch` stored on `request.resolver_match` (func, args, kwargs, url_name, app_names, namespaces, route).
- Reverse resolution builds a lookup table per URLconf/namespace; reversing is cached after first use.
- `View.as_view(**initkwargs)` returns a function that, per request, instantiates the class, calls `setup()` (sets `self.request/args/kwargs`) and then `dispatch()`, which picks the handler by lowercase HTTP method or returns 405 via `http_method_not_allowed()`. One instance per request — instance attributes are request-safe.

## Core concepts and APIs

| Topic | API |
|---|---|
| Patterns | `path()`, `re_path()`, `include()` |
| Built-in converters | `str` (default, no `/`), `int`, `slug`, `uuid`, `path` (includes `/`) |
| Custom converter | class with `regex`, `to_python`, `to_url` + `register_converter()` |
| Namespaces | `app_name` in the included module; instance namespace via `include(..., namespace=...)` |
| Reverse | `reverse()`, `reverse_lazy()` (for class attrs/settings), `{% url %}`, `Model.get_absolute_url()` |
| Reverse with query/fragment | `reverse(..., query={...}, fragment=...)` — **added in Django 5.2** |
| Shortcuts | `render`, `redirect`, `get_object_or_404`, `get_list_or_404` |
| Responses | `HttpResponse`, `JsonResponse`, `StreamingHttpResponse`, `FileResponse`, `HttpResponseRedirect` / `PermanentRedirect`, `HttpResponseNotModified`, `HttpResponseBadRequest`, `HttpResponseNotFound`, `HttpResponseForbidden`, `HttpResponseNotAllowed`, `HttpResponseGone`, `HttpResponseServerError` |
| Request | `method`, `GET`, `POST`, `FILES`, `body`, `headers` (case-insensitive), `META`, `COOKIES`, `user`, `session`, `resolver_match`, `get_host()`, `build_absolute_uri()`, `is_secure()`, `accepts()` |
| Error views | `handler400`, `handler403`, `handler404`, `handler500` in the root URLconf |

Exceptions as control flow: raise `Http404`, `PermissionDenied`, or `BadRequest` (Django ≥ 3.2) from views/services instead of building error responses by hand — the handler routes them to the configured error views consistently.

## Implementation examples

URLconfs with namespaces:

```python
# config/urls.py
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("orders/", include("apps.orders.urls")),
    path("api/v1/orders/", include("apps.orders.api.urls")),
]
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"

# apps/orders/urls.py
from django.urls import path
from . import views

app_name = "orders"
urlpatterns = [
    path("", views.OrderListView.as_view(), name="list"),
    path("<int:pk>/", views.order_detail, name="detail"),
    path("<int:pk>/cancel/", views.cancel_order, name="cancel"),
]
```

Custom converter (validated at the routing layer, so views get typed values):

```python
# apps/orders/converters.py
class OrderNumberConverter:
    regex = r"ORD-[0-9]{8}"
    def to_python(self, value: str) -> str: return value
    def to_url(self, value: str) -> str: return value

# apps/orders/urls.py
from django.urls import register_converter
register_converter(OrderNumberConverter, "ordno")
# path("by-number/<ordno:number>/", ...)
```

Function-based view with a scoped lookup (authorisation and lookup in one step):

```python
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from apps.orders.models import Order
from apps.orders.services import cancel_order as cancel_order_service

@login_required
def order_detail(request, pk: int):
    order = get_object_or_404(Order.objects.filter(customer=request.user), pk=pk)
    return render(request, "orders/detail.html", {"order": order})

@login_required
@require_POST
def cancel_order(request, pk: int):
    order = get_object_or_404(Order.objects.filter(customer=request.user), pk=pk)
    cancel_order_service(order=order, actor=request.user)
    return redirect("orders:detail", pk=order.pk)       # POST/Redirect/GET
```

Bad vs good lookup:

```python
# BAD: IDOR — any logged-in user can view any order
order = get_object_or_404(Order, pk=pk)
# GOOD: scope first
order = get_object_or_404(Order.objects.filter(customer=request.user), pk=pk)
```

Class-based generic view:

```python
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

class OrderListView(LoginRequiredMixin, ListView):      # mixins left of the base view
    template_name = "orders/list.html"
    paginate_by = 25
    context_object_name = "orders"

    def get_queryset(self):
        return (Order.objects.filter(customer=self.request.user)
                .select_related("shipping_address").order_by("-created_at"))
```

Decorating CBVs:

```python
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

@method_decorator(never_cache, name="dispatch")
class CheckoutView(LoginRequiredMixin, TemplateView): ...
```

Safe redirect to a user-supplied `next`:

```python
from django.utils.http import url_has_allowed_host_and_scheme

next_url = request.POST.get("next", "")
if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                       require_https=request.is_secure()):
    next_url = reverse("orders:list")
return redirect(next_url)
```

Custom error views — keep them dependency-free (no DB, minimal context), because they run when things are already broken:

```python
# apps/core/views.py
from django.shortcuts import render

def not_found(request, exception):
    return render(request, "errors/404.html", status=404)

def server_error(request):                     # handler500 takes no exception arg
    return render(request, "errors/500.html", status=500)
```

Streaming a large CSV without loading it into memory:

```python
import csv
from django.http import StreamingHttpResponse

class Echo:
    def write(self, value): return value

def export_orders(request):
    rows = Order.objects.filter(customer=request.user).values_list("pk", "total").iterator()
    writer = csv.writer(Echo())
    response = StreamingHttpResponse((writer.writerow(r) for r in rows), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders.csv"'
    return response
```

## Production patterns

- **Name every URL** and reverse everywhere; renaming a path then changes one line.
- **Namespace every app** (`app_name`) to avoid name collisions between apps.
- **POST/Redirect/GET** after successful state changes to avoid duplicate submissions on refresh.
- **Restrict methods** (`require_POST`, `require_http_methods`, `require_safe`) on views that change state; GET must be safe.
- **Version API prefixes** (`api/v1/`) at the root URLconf; API view details in django-rest-framework.
- **Return the right status**: 404 for "not yours" lookups (avoid existence leaks), 403 when existence is not secret, 400 for malformed input, 405 for wrong method.
- Since **Django 5.0**, most built-in view decorators (`require_http_methods`, `cache_control`, `never_cache`, `csrf_exempt`, `login_required`, etc.) support async views — verify per decorator in the release notes for your version.

## Decision guidance

**FBV vs CBV**

| Choose FBV when | Choose CBV / generic when |
|---|---|
| Logic is linear and specific | The view is standard CRUD/list/detail/form handling |
| Explicit control flow matters more than reuse | Several views share behaviour via mixins |
| Multiple HTTP methods share little code | You need per-method handlers (`get`, `post`) with shared `setup` |
| Team is unfamiliar with the CBV MRO | You want to override small hooks (`get_queryset`, `form_valid`) |

Generic views pay off only when you use their hooks; overriding `get()` entirely on a `DetailView` is a sign an FBV would be clearer. Deep mixin stacks (4+) make behaviour hard to trace — prefer composition via services or small explicit mixins.

**Decorator vs mixin vs middleware**: decorators for FBVs and per-view concerns; `method_decorator` or access mixins for CBVs; middleware only for concerns spanning almost all URLs ([middleware.md](./middleware.md)).

**`path` vs `re_path`**: `path` with converters by default; `re_path` only for legacy patterns that converters cannot express.

## Security considerations

- Always scope querysets by the current user/tenant before `get_object_or_404`.
- Never redirect to unvalidated user input — use `url_has_allowed_host_and_scheme`.
- Do not `csrf_exempt` session-authenticated views; token-authenticated APIs follow DRF guidance.
- `JsonResponse` refuses non-dict data unless `safe=False`; keep dicts at the top level.
- `FileResponse` with user-controlled paths → path traversal; resolve via storage and model records ([file-handling.md](./file-handling.md)).
- Error views must not render exception details. Depth: [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- N+1 queries in templates come from views passing unoptimised querysets; use `select_related`/`prefetch_related` in `get_queryset` ([django-orm](../../django-orm/SKILL.md)).
- Paginate list views (`paginate_by`); never render unbounded querysets.
- Use `StreamingHttpResponse`/`FileResponse` for large payloads; let the web server/CDN serve static and media.
- Per-view caching via `cache_page` — trade-offs in [caching.md](./caching.md).

## Testing

```python
from django.test import TestCase
from django.urls import resolve, reverse
from apps.orders import views

class OrderUrlTests(TestCase):
    def test_detail_resolves(self):
        match = resolve("/orders/5/")
        self.assertEqual(match.func, views.order_detail)
        self.assertEqual(match.kwargs, {"pk": 5})
        self.assertEqual(reverse("orders:detail", kwargs={"pk": 5}), "/orders/5/")

class OrderViewTests(TestCase):
    def setUp(self):
        self.user = make_user()                 # project factory
        self.order = make_order(customer=self.user)

    def test_other_users_order_is_404(self):
        self.client.force_login(make_user())
        response = self.client.get(reverse("orders:detail", kwargs={"pk": self.order.pk}))
        self.assertEqual(response.status_code, 404)

    def test_cancel_requires_post(self):
        self.client.force_login(self.user)
        url = reverse("orders:cancel", kwargs={"pk": self.order.pk})
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("orders:detail", kwargs={"pk": self.order.pk}))
```

- `Client` for integration (middleware, URLs, templates); `RequestFactory` to unit-test a view function in isolation (set `request.user` yourself).
- Test custom error pages with `@override_settings(DEBUG=False)` and `Client(raise_request_exception=False)`.
- `assertTemplateUsed`, `assertContains`, `assertNumQueries` for rendering and query counts. More in [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- Hard-coded URLs in templates/redirects instead of `{% url %}`/`reverse`.
- `reverse()` at import time (class attributes, settings) → circular import; use `reverse_lazy`.
- Missing trailing slash conventions with `APPEND_SLASH` + POST → `RuntimeError` in DEBUG (Django cannot redirect a POST keeping data).
- Catch-all patterns (`<path:slug>`) placed before specific ones, shadowing them.
- Fat views with business logic, emails and multi-model writes.
- Overriding `dispatch()` for auth checks instead of using mixins/decorators.
- Mixins in the wrong MRO order (`ListView, LoginRequiredMixin`) — the access check is skipped.
- Returning 200 with an error message instead of a proper status.

## Troubleshooting

| Symptom | Check |
|---|---|
| `NoReverseMatch` | name/namespace typo, missing `app_name`, wrong kwargs type/name, converter `to_url` rejecting value |
| Wrong view responds | pattern order; `resolve(path)` in shell; `include()` prefix ends with `/` |
| 404 in DEBUG lists tried patterns but yours missing | URLconf not included, or `request.urlconf` overridden by middleware |
| 405 unexpected | `require_*` decorator or CBV lacks method handler |
| Custom 404 not shown | `DEBUG=True` (technical page wins) or handler path wrong; `manage.py check` validates handlers |
| CBV attribute leaks between requests | mutable class attribute (e.g. list) mutated in a handler |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/http/urls/
- https://docs.djangoproject.com/en/stable/ref/urls/
- https://docs.djangoproject.com/en/stable/ref/urlresolvers/
- https://docs.djangoproject.com/en/stable/topics/http/views/
- https://docs.djangoproject.com/en/stable/topics/class-based-views/
- https://docs.djangoproject.com/en/stable/ref/class-based-views/
- https://docs.djangoproject.com/en/stable/topics/http/decorators/
- https://docs.djangoproject.com/en/stable/ref/request-response/
- https://docs.djangoproject.com/en/stable/topics/http/shortcuts/
- https://docs.djangoproject.com/en/stable/ref/views/#error-views
