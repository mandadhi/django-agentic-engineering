# HTMX Engineering with Django

> **Scope:** Owns HTMX 2.x architecture, attributes, request/swap lifecycle, events, and complete Django patterns (search, pagination, infinite scroll, modals, inline edit, delete, toasts, polling), plus HTMX-specific CSRF, auth, error handling, history and security. Does not own Alpine.js ([alpine-js-engineering.md](alpine-js-engineering.md)), form rendering/validation UI depth ([django-forms-and-validation.md](django-forms-and-validation.md)), template composition ([django-template-engineering.md](django-template-engineering.md)) or backend security enforcement ([django-security-engineering](../../django-security-engineering/SKILL.md)).

## 1. Purpose and scope

HTMX lets any element issue an HTTP request and swap the returned **HTML** into the DOM. With Django this keeps rendering, validation, authorization and state on the server while giving SPA-like interactivity with a few kilobytes of library and no build step.

Use this reference to:

- decide whether HTMX fits a feature (§4);
- implement the standard interaction patterns with views, URLs and templates (§5);
- get CSRF, login redirects, errors, history and security right (§5, §8);
- debug swaps that "do nothing" (§10).

Baseline: **HTMX 2.x**, Django 5.2 LTS. Where 1.x behaved differently it is called out.

## 2. Core concepts

### Hypermedia as the engine of state

The server returns HTML that already contains the next set of affordances (links, buttons, forms with `hx-*` attributes). The client holds no domain model; it only knows how to issue requests and swap responses. Consequences:

- One source of truth for permissions and validation: the Django view.
- No JSON API is required for the UI (you may still have one for other clients).
- Every endpoint returning a fragment is a real, URL-addressable, permission-checked view.

### Attribute inheritance

Most `hx-*` attributes are inherited from ancestors: `hx-target`, `hx-swap`, `hx-headers`, `hx-include`, `hx-indicator`, `hx-confirm`, `hx-boost`, `hx-push-url`, `hx-sync`, `hx-vals`, `hx-ext`, etc. Put `hx-headers` (CSRF) on `<body>`, `hx-target`/`hx-swap` on a container. Opt out with `hx-disinherit="*"` or `hx-disinherit="hx-target hx-swap"`; flip the default with `htmx.config.disableInheritance = true` and then opt in using `hx-inherit` (HTMX 2.x).

Inheritance is the most common source of "why did it swap *there*?" — always check ancestors.

### Core attributes

| Attribute | Purpose |
|---|---|
| `hx-get` / `hx-post` / `hx-put` / `hx-patch` / `hx-delete` | Issue a request of that method to the URL |
| `hx-trigger` | Which event(s) fire the request (default: `click`; `change` for input/select/textarea; `submit` for forms) |
| `hx-target` | Element to swap into (default: the element itself) |
| `hx-swap` | How to swap (default `innerHTML`, configurable via `htmx.config.defaultSwapStyle`) |
| `hx-select` | Pick a subset of the response (CSS selector) to swap |
| `hx-select-oob` | Pick additional elements of the response to swap out-of-band |
| `hx-swap-oob` | On a response element: swap it by `id` elsewhere in the page |
| `hx-include` | Include values of other inputs/forms (`closest form`, `#filters`) |
| `hx-vals` | Add static JSON params (`hx-vals='{"status": "open"}'`); `js:` prefix requires eval |
| `hx-params` | Filter params sent (`*`, `none`, `not a,b`, `a,b`) |
| `hx-headers` | Extra request headers (JSON) |
| `hx-encoding` | `multipart/form-data` for file uploads |
| `hx-indicator` | Element(s) receiving the `htmx-request` class during the request |
| `hx-disabled-elt` | Elements to set `disabled` during the request (`this`, `find button`) |
| `hx-confirm` | `window.confirm()` before sending (intercept with `htmx:confirm` for custom dialogs) |
| `hx-prompt` | `window.prompt()`; value sent in `HX-Prompt` header |
| `hx-push-url` / `hx-replace-url` | Update the address bar / history |
| `hx-boost` | Turn normal links/forms into AJAX requests that swap `<body>` |
| `hx-sync` | Coordinate concurrent requests (`this:replace`, `closest form:abort`, `queue last`) |
| `hx-preserve` | Keep an element (by `id`) untouched across swaps (e.g. a playing video) |
| `hx-disable` | Disable all HTMX processing on the element and descendants |
| `hx-history` / `hx-history-elt` | `false` excludes page from history cache / choose snapshot element |
| `hx-validate` | Run HTML constraint validation before non-form requests |
| `hx-request` | Request config (`timeout`, `credentials`, `noHeaders`) |
| `hx-on:<event>` | Inline event handler (HTMX 2.x syntax `hx-on:htmx:after-request`, shorthand `hx-on::after-request`); requires eval |
| `hx-ext` | Enable an extension (extensions ship separately in 2.x) |

### HTTP methods and Django parsing

| Method | HTMX sends params in | Django exposes them as |
|---|---|---|
| `hx-get` | query string | `request.GET` |
| `hx-post` | form-encoded body | `request.POST` (+ `request.FILES` for multipart) |
| `hx-put` / `hx-patch` | form-encoded body | **not** `request.POST` — parse `QueryDict(request.body)` |
| `hx-delete` | **query string** (HTMX 2.x; 1.x sent a body) | `request.GET` |

`request.POST` is populated only for `POST` requests. For form-heavy workflows, prefer `hx-post` (full `ModelForm` + `request.FILES` support) and route semantics by URL (`/tasks/12/edit/`). Use PUT/PATCH when you want method-level routing and accept manual parsing (§5.6). Django's CSRF middleware checks **all** non-safe methods (POST, PUT, PATCH, DELETE).

### Request headers HTMX sends

`HX-Request: true`, `HX-Target` (target id), `HX-Trigger` (triggering element id), `HX-Trigger-Name`, `HX-Boosted`, `HX-Current-URL`, `HX-History-Restore-Request`, `HX-Prompt`.

### Response headers HTMX honors

| Header | Effect |
|---|---|
| `HX-Redirect` | Full-page navigation to URL |
| `HX-Location` | AJAX navigation (like a boosted link) to URL or JSON spec |
| `HX-Refresh: true` | Full page reload |
| `HX-Push-Url` / `HX-Replace-Url` | Set the URL (or `false` to prevent) |
| `HX-Retarget` | CSS selector overriding `hx-target` |
| `HX-Reswap` | Override `hx-swap` |
| `HX-Reselect` | Override `hx-select` |
| `HX-Trigger` / `HX-Trigger-After-Swap` / `HX-Trigger-After-Settle` | Fire client events (string or JSON) |

Status **286** stops `every Ns` polling.

## 3. Internal mechanics

### Request → swap → settle lifecycle

1. **Trigger**: an event matching `hx-trigger` fires on the element (after filters/modifiers such as `delay`, `changed`).
2. **Confirm/validate**: `htmx:confirm`, `hx-confirm`; forms run constraint validation (`htmx:validation:validate`).
3. **Collect params**: closest form values (for non-GET in a form), the element's own `name`/`value`, `hx-include`, `hx-vals`, filtered by `hx-params`.
4. **`htmx:configRequest`**: last chance to mutate headers/params (CSRF hook, §5.1).
5. **`htmx:beforeRequest`** → XHR sent; `htmx-request` class added to the indicator; `hx-disabled-elt` disabled.
6. Response arrives → **`htmx:beforeOnLoad`** → response headers processed (`HX-Trigger` events fire, `HX-Redirect`/`HX-Location`/`HX-Refresh` short-circuit).
7. **`htmx.config.responseHandling`** decides swap vs. no swap vs. error for the status code. Defaults (HTMX 2.x): 204 → no swap; 2xx/3xx → swap; 4xx/5xx → no swap + `htmx:responseError` [VERIFY: exact default array in installed htmx version].
8. **`htmx:beforeSwap`** (can set `evt.detail.shouldSwap`, `evt.detail.target`, `evt.detail.isError`) → old content gets `htmx-swapping` → after `swap:` delay, content replaced; OOB elements swapped; `<title>` in response updates the document title.
9. New content gets `htmx-added`, then **settle**: after `settle:` delay (default ~20 ms) attributes copied from old → new elements with the same `id` are transitioned (enables CSS transitions), `htmx-settling` removed. **`htmx:afterSwap`**, **`htmx:afterSettle`**, **`htmx:load`** (per new element) fire; new content is processed for `hx-*` attributes.
10. `htmx:afterRequest` fires in all cases; `htmx:sendError` on network failure; `htmx:timeout` on timeout.

Key facts:

- XHR follows redirects transparently. HTMX never sees a 302; it sees the final page (e.g. the login form) — this is why login redirects must be converted (§5.2).
- Swapped `<script>` tags execute when `htmx.config.allowScriptTags` is true (the default).
- Only elements present at load or swapped by HTMX are processed; HTML inserted by other code needs `htmx.process(el)`.

### hx-swap strategies and modifiers

| Value | Effect |
|---|---|
| `innerHTML` | Replace children of target (default) |
| `outerHTML` | Replace the target itself |
| `textContent` | Set text, no HTML parsing (HTMX 2.x) |
| `beforebegin` / `afterbegin` / `beforeend` / `afterend` | Insert relative to target |
| `delete` | Remove target regardless of response |
| `none` | Do not swap (OOB swaps and headers still processed) |

Modifiers: `swap:300ms` (delay before swap, lets exit animations run), `settle:100ms`, `scroll:top|bottom` (`scroll:#list:bottom`), `show:top|bottom|none` (`show:window:top`), `focus-scroll:true|false`, `transition:true` (View Transitions API where supported), `ignoreTitle:true`.

### Extended selectors (hx-target, hx-include, hx-indicator, hx-disabled-elt, from:)

`this`, `closest <sel>`, `find <sel>`, `next` / `next <sel>`, `previous` / `previous <sel>`, plus `document`, `window`, `body` where they make sense (e.g. `from:body`).

### hx-trigger syntax

```text
hx-trigger="<event>[filter] modifiers, <event2> ..."
```

| Modifier / special | Meaning |
|---|---|
| `changed` | Fire only if the element's value changed |
| `delay:300ms` | Debounce: reset timer on each event |
| `throttle:1s` | Fire at most once per interval |
| `once` | Fire only the first time |
| `from:<sel>` | Listen on another element (`from:body` for server-sent `HX-Trigger` events) |
| `target:<sel>` | Only when `event.target` matches |
| `consume` | Stop propagation to parent HTMX handlers |
| `queue:first|last|all|none` | Queueing while a request is in flight |
| `[ctrlKey]` | JS filter expression (requires eval) |
| `load` | Fire when the element is loaded/swapped in |
| `revealed` | Fire when scrolled into viewport (window scroll) |
| `intersect` | IntersectionObserver-based; options `root:<sel>`, `threshold:0.5` |
| `every 2s` | Polling |

### Out-of-band swaps

Any top-level element in the response with `hx-swap-oob="true"` (or a strategy, `hx-swap-oob="beforeend:#toasts"`) is removed from the main content and swapped into the element with the same `id`. Table-row OOB content needs wrapping in `<template>` because a bare `<tr>` cannot be parsed at fragment top level (HTMX 2.x) [VERIFY: template-wrapping behavior in installed version].

### History

`hx-push-url="true"` snapshots the current `hx-history-elt` (default `<body>`) into `localStorage` (`htmx.config.historyCacheSize`, default 10) before navigating. On Back, HTMX restores from the cache; on a cache miss it issues a GET with `HX-Request` **and** `HX-History-Restore-Request: true` and expects a **full page** (it swaps the body) [VERIFY: refreshOnHistoryMiss default]. Therefore any pushed URL must render the complete page for a history-restore request and for a direct browser load.

## 4. Engineering decisions and trade-offs

### HTMX vs custom JS vs SPA

| Prefer HTMX when… | Prefer custom JS / Alpine when… | Prefer an SPA / React island when… |
|---|---|---|
| UI is CRUD, dashboards, filters, wizards, admin-like | Purely client-side state (toggle, tabs, disclosure, local filter of already-loaded data) | Offline/local-first is required |
| Server owns validation and permissions | Zero-latency feedback is required (no round trip) | Rich client-side editing (canvas, spreadsheet, drag-heavy boards, collaborative editors) |
| SEO/progressive enhancement matter | Small widget around an HTMX page | Heavy optimistic UI across many views |
| Team is Django-centric | — | Same API already serves mobile clients and the team has frontend capacity |

Rule: start with plain links/forms, add HTMX where a full reload hurts, add Alpine for local state, reach for React islands only for genuinely client-heavy widgets. See [production-frontend-architectures.md](production-frontend-architectures.md).

### Fragment strategy

| Prefer same view + fragment when… | Prefer dedicated fragment endpoint when… |
|---|---|
| URL is navigable/bookmarkable (list, search, pagination) | Fragment has no standalone meaning (row edit form, modal body) |
| `hx-push-url` is used | Endpoint has different permissions/rate limits |

When one URL returns both: send `Vary: HX-Request` (`@vary_on_headers("HX-Request")`) so caches (browser, CDN, Django cache) keep them apart.

### Validation error responses

| Option | Prefer when… | Cost |
|---|---|---|
| Return **200** with the re-rendered form | Simplest; works with default config | Status code lies; harder to assert in tests/monitoring |
| Return **422** + configure `responseHandling` to swap 422 | Want correct status codes | One global config line |
| Return 4xx + `HX-Retarget`/`HX-Reswap` | Errors go to a different region | Retarget does not by itself make a non-swappable status swap [VERIFY: whether HX-Retarget overrides responseHandling swap:false in 2.x] — combine with responseHandling |

Recommended config (one place, base template):

```html
<meta name="htmx-config" content='{
  "selfRequestsOnly": true,
  "allowEval": false,
  "allowScriptTags": false,
  "includeIndicatorStyles": false,
  "historyCacheSize": 10,
  "responseHandling": [
    {"code": "204", "swap": false},
    {"code": "[23]..", "swap": true},
    {"code": "422", "swap": true},
    {"code": "[45]..", "swap": false, "error": true}
  ]
}'>
```

`allowEval: false` disables `hx-on`, `js:` in `hx-vals` and `[filter]` expressions — the price of a CSP without `'unsafe-eval'`. `includeIndicatorStyles: false` avoids HTMX injecting a `<style>` (CSP `style-src`); ship the indicator CSS yourself.

### PUT/PATCH vs POST

| Prefer `hx-post` when… | Prefer `hx-put`/`hx-patch` when… |
|---|---|
| Using `ModelForm`, file uploads, or class-based `FormView` | Method-routed endpoints (`/tasks/12/` handles GET/PATCH/DELETE) and you parse bodies yourself |

## 5. Django/frontend implementation patterns

### 5.1 Setup, CSRF and helpers

Vendor a pinned `htmx.min.js` into static files (see [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)); if using a CDN, pin the exact version with SRI (see [frontend-security.md](frontend-security.md)).

```django
{# templates/base.html #}
{% load static %}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Projects{% endblock %}</title>
  <meta name="htmx-config" content='{"selfRequestsOnly": true, "allowEval": false, "allowScriptTags": false, "includeIndicatorStyles": false, "responseHandling": [{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"422","swap":true},{"code":"[45]..","swap":false,"error":true}]}'>
  <link rel="stylesheet" href="{% static 'css/app.css' %}">
  <script src="{% static 'vendor/htmx-2.x.min.js' %}" defer></script>
  <script src="{% static 'js/app.js' %}" type="module"></script>
</head>
<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
  <main id="main">{% block content %}{% endblock %}</main>
  <div id="modal-root"></div>
  <div id="toasts" role="status" aria-live="polite" class="toasts"></div>
</body>
</html>
```

Alternative CSRF hook (useful when the body is cached or when HTML comes from a CDN): read a `<meta name="csrf-token">` or the `csrftoken` cookie (readable because `CSRF_COOKIE_HTTPONLY` defaults to False) in `htmx:configRequest`.

```js
// static/js/app.js
document.body.addEventListener("htmx:configRequest", (evt) => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (token && evt.detail.verb !== "get") evt.detail.headers["X-CSRFToken"] = token;
});
```

Never `@csrf_exempt` an HTMX endpoint. Details: [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md).

```css
/* static/css/app.css — replaces includeIndicatorStyles */
.htmx-indicator { opacity: 0; transition: opacity 150ms ease-in; }
.htmx-request .htmx-indicator, .htmx-request.htmx-indicator { opacity: 1; }
@media (prefers-reduced-motion: reduce) { .htmx-indicator { transition: none; } }
```

Helpers without third-party code:

```python
# core/htmx.py
from django.http import HttpRequest


def is_htmx(request: HttpRequest) -> bool:
    """True for HTMX partial requests; False for full loads and history restores."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
        and request.headers.get("HX-Boosted") != "true"
    )
```

With **django-htmx (third-party)**: add `django_htmx.middleware.HtmxMiddleware`; then `request.htmx` is truthy for HTMX requests and exposes `.boosted`, `.target`, `.trigger`, `.current_url`, `.history_restore_request`. `django_htmx.http` offers `HttpResponseClientRedirect`, `HttpResponseClientRefresh`, `HttpResponseLocation`, `HttpResponseStopPolling`, `push_url`, `replace_url`, `retarget`, `reswap`, `trigger_client_event` [VERIFY: helper names against installed django-htmx version].

### 5.2 Authentication: session expiry inside a fragment

`@login_required` returns 302 → XHR follows → HTMX swaps the **login page** into a table cell. Convert redirects to login into `HX-Redirect` for HTMX requests, and point `next` at the page the user was on (`HX-Current-URL`), not the fragment URL.

```python
# core/middleware.py
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


class HtmxLoginRedirectMiddleware:
    """Place after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.headers.get("HX-Request") != "true" or response.status_code not in (301, 302):
            return response
        login_url = resolve_url(settings.LOGIN_URL)
        if not response.url.startswith(login_url):
            return response
        current = request.headers.get("HX-Current-URL", "/")
        if url_has_allowed_host_and_scheme(current, {request.get_host()}, require_https=request.is_secure()):
            parts = urlsplit(current)
            next_path = parts.path + (f"?{parts.query}" if parts.query else "")
        else:
            next_path = "/"
        client = HttpResponse(status=204)
        client["HX-Redirect"] = f"{login_url}?{urlencode({'next': next_path})}"
        return client
```

For permission failures (403) keep the status and handle it in `htmx:responseError` (toast "You no longer have access"), never swap a login or error page into a fragment. Auth backend details: [authentication-sessions.md](../../django-core/references/authentication-sessions.md).

### 5.3 Partial templates: same view returns page or fragment

On Django 5.2 use separate include files, or **django-template-partials (third-party)** (`{% partialdef rows %}` + render `"tasks/task_list.html#rows"`). Built-in `{% partialdef %}` exists only from Django 6.0.

```python
# tasks/views.py
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_headers

from core.htmx import is_htmx
from .forms import TaskFilterForm
from .models import Task


@login_required
@require_GET
@vary_on_headers("HX-Request")
def task_list(request):
    form = TaskFilterForm(request.GET or None)
    tasks = Task.objects.for_tenant(request.tenant).select_related("assignee").order_by("-updated_at")
    if form.is_valid():
        tasks = form.filter(tasks)
    page = Paginator(tasks, 25).get_page(request.GET.get("page"))
    context = {"form": form, "page": page}
    template = "tasks/_results.html" if is_htmx(request) else "tasks/task_list.html"
    return render(request, template, context)
```

### 5.4 Active search with filters + pagination

```django
{# templates/tasks/task_list.html #}
{% extends "base.html" %}
{% block content %}
<h1>Tasks</h1>
<form id="task-filters" action="{% url 'tasks:list' %}" method="get" role="search"
      hx-get="{% url 'tasks:list' %}" hx-trigger="input delay:300ms, change, submit"
      hx-target="#results" hx-push-url="true" hx-sync="this:replace"
      hx-indicator="#results-spinner">
  <label for="id_q">Search tasks</label>
  <input id="id_q" type="search" name="q" value="{{ form.q.value|default:'' }}" autocomplete="off">
  <label for="id_status">Status</label>
  {{ form.status }}
  <button type="submit">Filter</button>
  <span id="results-spinner" class="htmx-indicator" aria-hidden="true">Loading…</span>
</form>
<div id="results">{% include "tasks/_results.html" %}</div>
{% endblock %}
```

```django
{# templates/tasks/_results.html #}
<p role="status">{{ page.paginator.count }} task{{ page.paginator.count|pluralize }} found</p>
<table>
  <thead><tr><th scope="col">Title</th><th scope="col">Assignee</th><th scope="col">Status</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
  <tbody id="task-rows">
    {% for task in page %}{% include "tasks/_row.html" %}{% empty %}
    <tr><td colspan="4">No tasks match these filters.</td></tr>
    {% endfor %}
  </tbody>
</table>
<nav aria-label="Pagination" hx-target="#results" hx-push-url="true">
  {% if page.has_previous %}
    <a href="{% querystring page=page.previous_page_number %}" hx-get="{% querystring page=page.previous_page_number %}">Previous</a>
  {% endif %}
  <span aria-current="page">Page {{ page.number }} of {{ page.paginator.num_pages }}</span>
  {% if page.has_next %}
    <a href="{% querystring page=page.next_page_number %}" hx-get="{% querystring page=page.next_page_number %}">Next</a>
  {% endif %}
</nav>
```

Notes: `href` keeps links working without JS; `{% querystring %}` (Django ≥ 5.1) preserves filters; `hx-sync="this:replace"` aborts stale searches so out-of-order responses never win; `hx-push-url` makes filters bookmarkable — so the full-page branch must handle every combination. Move focus only on explicit pagination (e.g. to the results heading), not on every keystroke.

### 5.5 Infinite scroll

```django
{# templates/activity/_feed_page.html #}
{% for event in page %}
  <li class="feed-item">{{ event.summary }} <time datetime="{{ event.created_at|date:'c' }}">{{ event.created_at|timesince }} ago</time></li>
{% endfor %}
{% if page.has_next %}
  <li hx-get="{% url 'activity:feed' %}?page={{ page.next_page_number }}"
      hx-trigger="intersect once" hx-swap="outerHTML">
    <a href="{% url 'activity:feed' %}?page={{ page.next_page_number }}">Load more</a>
  </li>
{% endif %}
```

The sentinel replaces itself with the next page plus a new sentinel. Use `intersect` (works in scroll containers; `revealed` only tracks window scroll). Keep a real link for keyboard users and no-JS; consider a "Load more" button instead of auto-loading when a footer must stay reachable. Do not `hx-push-url` each chunk.

### 5.6 Inline editing (row view ↔ edit)

```python
# tasks/views.py (continued)
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from .forms import TaskInlineForm


def _task_for(request, pk):
    # Every fragment endpoint re-checks tenant + permission.
    return get_object_or_404(Task.objects.for_tenant(request.tenant).editable_by(request.user), pk=pk)


@login_required
@require_http_methods(["GET"])
def task_row(request, pk):
    return render(request, "tasks/_row.html", {"task": _task_for(request, pk)})


@login_required
@require_http_methods(["GET", "PUT"])
def task_edit(request, pk):
    task = _task_for(request, pk)
    if request.method == "PUT":
        data = QueryDict(request.body, encoding=request.encoding)  # request.POST is empty for PUT
        form = TaskInlineForm(data, instance=task)
        if form.is_valid():
            form.save()
            response = render(request, "tasks/_row.html", {"task": task})
            response["HX-Trigger"] = '{"toast": {"level": "success", "message": "Task saved"}}'
            return response
        return render(request, "tasks/_row_edit.html", {"task": task, "form": form}, status=422)
    return render(request, "tasks/_row_edit.html", {"task": task, "form": TaskInlineForm(instance=task)})
```

```django
{# templates/tasks/_row.html #}
<tr id="task-{{ task.pk }}">
  <td>{{ task.title }}</td>
  <td>{{ task.assignee|default:"Unassigned" }}</td>
  <td>{{ task.get_status_display }}</td>
  <td>
    <button type="button" hx-get="{% url 'tasks:edit' task.pk %}" hx-target="closest tr" hx-swap="outerHTML">
      Edit<span class="visually-hidden"> {{ task.title }}</span>
    </button>
    <button type="button" hx-delete="{% url 'tasks:delete' task.pk %}" hx-target="closest tr"
            hx-swap="outerHTML swap:200ms" hx-confirm="Delete “{{ task.title }}”?">
      Delete<span class="visually-hidden"> {{ task.title }}</span>
    </button>
  </td>
</tr>
```

```django
{# templates/tasks/_row_edit.html — <form> cannot wrap <td>, so include the row's inputs #}
<tr id="task-{{ task.pk }}" hx-target="this" hx-swap="outerHTML">
  <td>
    <label class="visually-hidden" for="title-{{ task.pk }}">Title</label>
    <input id="title-{{ task.pk }}" name="title" value="{{ form.title.value|default:'' }}" required autofocus
           {% if form.title.errors %}aria-invalid="true" aria-describedby="title-err-{{ task.pk }}"{% endif %}>
    {% if form.title.errors %}<p id="title-err-{{ task.pk }}" class="error">{{ form.title.errors.0 }}</p>{% endif %}
  </td>
  <td>{{ form.assignee }}</td>
  <td>{{ form.status }}</td>
  <td>
    <button type="button" hx-put="{% url 'tasks:edit' task.pk %}" hx-include="closest tr" hx-disabled-elt="this">Save</button>
    <button type="button" hx-get="{% url 'tasks:row' task.pk %}">Cancel</button>
  </td>
</tr>
```

Accessibility: after swapping in the edit row, focus the first field (`autofocus` in swapped content is honored by HTMX [VERIFY: autofocus handling in installed version]); after save/cancel return focus to the row's Edit button (a small `htmx:afterSwap` listener). Escape-to-cancel can be a keydown listener in `app.js`.

### 5.7 Delete with confirm

```python
@login_required
@require_http_methods(["DELETE"])
def task_delete(request, pk):
    task = _task_for(request, pk)
    if not request.user.has_perm("tasks.delete_task", task):
        return HttpResponse(status=403)
    task.delete()
    response = HttpResponse("")  # 200 + empty body → outerHTML removes the row; 204 would swap nothing
    response["HX-Trigger"] = '{"toast": {"level": "success", "message": "Task deleted"}, "taskCountChanged": null}'
    return response
```

`hx-confirm` uses `window.confirm()` (accessible but unstyled). For a custom dialog intercept `htmx:confirm`, call `evt.preventDefault()`, open a `<dialog>`, then `evt.detail.issueRequest(true)` on confirmation. Any params of `hx-delete` arrive in `request.GET` (HTMX 2.x).

### 5.8 Modal workflow with `<dialog>`

```django
{# trigger, e.g. in task_list.html #}
<button type="button" hx-get="{% url 'tasks:create' %}" hx-target="#modal-root">New task</button>

{# templates/tasks/_create_modal.html #}
<dialog id="task-modal" aria-labelledby="task-modal-title" data-autoshow>
  <h2 id="task-modal-title">New task</h2>
  <form hx-post="{% url 'tasks:create' %}" hx-target="#task-modal" hx-swap="outerHTML" hx-disabled-elt="find button[type=submit]">
    {{ form.title.as_field_group }}
    {{ form.assignee.as_field_group }}
    <div class="actions">
      <button type="submit">Create</button>
      <button type="button" data-close-dialog>Cancel</button>
    </div>
  </form>
</dialog>
```

```python
@login_required
@require_http_methods(["GET", "POST"])
def task_create(request):
    if not request.user.has_perm("tasks.add_task"):
        return HttpResponse(status=403)
    form = TaskCreateForm(request.POST or None, tenant=request.tenant)
    if request.method == "POST" and form.is_valid():
        form.save()
        response = HttpResponse(status=204)
        response["HX-Trigger"] = '{"modal:close": null, "taskCreated": null, "toast": {"level": "success", "message": "Task created"}}'
        return response
    status = 422 if request.method == "POST" else 200
    return render(request, "tasks/_create_modal.html", {"form": form}, status=status)
```

```js
// static/js/app.js — CSP-friendly glue (no hx-on, no inline script)
document.body.addEventListener("htmx:afterSwap", (evt) => {
  const dlg = evt.detail.elt.querySelector?.("dialog[data-autoshow]") ??
              (evt.detail.elt.matches?.("dialog[data-autoshow]") ? evt.detail.elt : null);
  if (dlg && !dlg.open) dlg.showModal();
});
document.body.addEventListener("modal:close", () => {
  document.querySelector("#modal-root dialog[open]")?.close();
});
document.addEventListener("close", (evt) => {
  if (evt.target.matches?.("#modal-root dialog")) evt.target.remove();
}, true);
document.addEventListener("click", (evt) => {
  if (evt.target.matches("[data-close-dialog]")) evt.target.closest("dialog")?.close();
});
```

The list refreshes itself: `<div id="results" hx-get="{% url 'tasks:list' %}" hx-trigger="taskCreated from:body" hx-include="#task-filters">`. `showModal()` gives focus containment, inert background and Escape; return focus to the opener when closing ([frontend-accessibility.md](frontend-accessibility.md)).

### 5.9 Toasts via HX-Trigger

```js
// static/js/toasts.js
const region = document.getElementById("toasts");
document.body.addEventListener("toast", (evt) => {
  const { level = "info", message = "" } = evt.detail;
  const el = document.createElement("div");
  el.className = `toast toast--${level}`;
  el.textContent = message;              // never innerHTML with server/user strings
  region.append(el);
  setTimeout(() => el.remove(), 6000);
});
document.body.addEventListener("htmx:responseError", (evt) => {
  const status = evt.detail.xhr.status;
  const message = status === 403 ? "You don't have permission to do that."
                : status >= 500 ? "Something went wrong. Please try again." : "Request failed.";
  document.body.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message } }));
});
document.body.addEventListener("htmx:sendError", () => {
  document.body.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message: "Network error — check your connection." } }));
});
```

For object values in `HX-Trigger` JSON the event `detail` carries the object's fields; string values arrive as `detail.value`. Build the header with `json.dumps` (or django-htmx `trigger_client_event`), never string concatenation with user input. The `#toasts` region is a persistent `role="status"` live region; errors that need immediate attention can use a separate `role="alert"` region. Don't auto-dismiss messages users must act on.

Alternative: return toasts as OOB HTML (`<div hx-swap-oob="beforeend:#toasts">…</div>`) rendered from Django's messages framework — keeps escaping in templates.

### 5.10 Polling a background job

```python
# reports/views.py
@login_required
def export_status(request, job_id):
    job = get_object_or_404(ExportJob, pk=job_id, tenant=request.tenant, requested_by=request.user)
    return render(request, "reports/_export_status.html", {"job": job})
```

```django
{# templates/reports/export_detail.html (excerpt) #}
<div role="status" aria-live="polite">
  {% include "reports/_export_status.html" %}
</div>

{# templates/reports/_export_status.html #}
<div id="export-{{ job.pk }}"
     {% if not job.is_finished %}hx-get="{% url 'reports:export-status' job.pk %}" hx-trigger="every 3s" hx-swap="outerHTML"{% endif %}>
  {% if job.status == "done" %}
    Export ready. <a href="{% url 'reports:export-download' job.pk %}">Download CSV</a>
  {% elif job.status == "failed" %}
    Export failed. <button type="button" hx-post="{% url 'reports:export-retry' job.pk %}" hx-target="closest div" hx-swap="outerHTML">Retry</button>
  {% else %}
    <progress max="100" value="{{ job.progress }}">{{ job.progress }}%</progress> Exporting…
  {% endif %}
</div>
```

Polling stops when the returned element no longer carries `hx-trigger` (or return status 286). For server-controlled backoff use `hx-trigger="load delay:{{ next_poll_ms }}ms"` instead of `every`. Keep the poll endpoint cheap (indexed lookup, no heavy joins) — see §8. For long jobs consider SSE via the official SSE extension (separate package in 2.x).

### 5.11 OOB updates and history

```django
{# appended to a fragment response #}
<span id="task-count" hx-swap-oob="true">{{ open_task_count }}</span>
```

Pages containing sensitive data should not be snapshotted to `localStorage`: add `hx-history="false"` on an element of that page, or set `historyCacheSize: 0`.

## 6. Production use cases

- **Tenant dashboard**: KPI cards each `hx-get` with `hx-trigger="load"` (lazy loads slow widgets in parallel), refresh on `projectUpdated from:body`.
- **Invoice list**: filter form (§5.4) + bulk actions (`hx-post` with `hx-include="[name=selected]:checked"`), results count via OOB.
- **Approval queue**: inline approve/reject rows (§5.6/5.7) + toast + OOB counter in the nav badge.
- **Report export**: start with `hx-post`, return status fragment that polls (§5.10).
- **Settings wizard**: each step `hx-post` returns the next step; `hx-push-url` only if steps must be bookmarkable.
- **hx-boost** on admin-like navigation for faster page transitions — verify scripts/analytics that rely on full page loads still work.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Server is the single source of truth (validation, permissions, rendering) | Every interaction costs a round trip; latency visible on slow networks |
| ~14 kB gzipped-scale library, no build step [VERIFY: current size] | Behavior spread across attributes; inheritance surprises |
| Progressive enhancement is natural (`href`/`action` fallbacks) | No offline mode; optimistic UI is awkward |
| Reuses Django forms, templates, tests | Many small endpoints to secure and test |
| Easy to test with Django test client (`HTTP_HX_REQUEST="true"`) | Eval-based features conflict with strict CSP |

## 8. Security and performance considerations

### Security

- **Every fragment endpoint is a public URL.** Re-check authentication, object ownership, tenant scope and permissions in every view — a hidden Edit button is not access control ([authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- **Never `hx-get` (or any GET) for mutations**: GETs are not CSRF-protected, are prefetched, cached and logged. Use POST/PUT/PATCH/DELETE; enforce with `require_http_methods`.
- **Escape fragments**: HTMX inserts HTML verbatim. Django autoescaping protects you unless you use `|safe`, `mark_safe`, or build HTML in Python without `format_html` ([cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md)).
- **User-generated HTML**: wrap rendered rich text in `hx-disable` so injected `hx-*` attributes cannot fire requests. Sanitize allowed HTML server-side.
- **Attribute injection**: user data inside `hx-vals`/`hx-headers` JSON must be JSON-encoded and then HTML-escaped; prefer `name`/`value` inputs.
- `htmx.config.selfRequestsOnly = true` (2.x default) blocks requests to other origins; keep it.
- `allowEval: false`, `allowScriptTags: false` under strict CSP; consider `historyCacheSize: 0` or `hx-history="false"` for sensitive pages (snapshots live in `localStorage`).
- CSRF on every unsafe method (§5.1). Login redirects → `HX-Redirect` (§5.2); validate `next` with `url_has_allowed_host_and_scheme`.
- Broader browser-side controls: [frontend-security.md](frontend-security.md).

### Performance

- Keep fragments small: render only the region that changes; use OOB for distant small updates instead of re-rendering the page shell.
- Fix N+1 in fragment views (`select_related`/`prefetch_related`) — HTMX multiplies request count ([django-application-performance.md](../../django-performance/references/django-application-performance.md)).
- Debounce search (`delay:300ms`) and cancel stale requests (`hx-sync="this:replace"`).
- Polling: N tabs × users × 1/interval requests. Prefer longer intervals, server-controlled backoff, stopping on completion, or SSE. Pause when hidden (`hx-trigger="every 5s [document.visibilityState === 'visible']"` needs eval; otherwise a small `visibilitychange` listener).
- Large swaps can cause CLS/INP issues; reserve space for loading regions ([frontend-performance.md](frontend-performance.md)).
- `Vary: HX-Request` on dual-mode URLs so caches don't serve a fragment as a page.

## 9. Common mistakes and anti-patterns

1. **Login page appears inside a table cell** → middleware converts login redirects to `HX-Redirect` (§5.2).
2. **Validation errors never show** (422 not swapped by default) → configure `responseHandling` for 422 or return 200.
3. **`request.POST` empty on `hx-put`/`hx-patch`** → `QueryDict(request.body)` or use `hx-post`.
4. **DELETE view reads `request.body`** → HTMX 2.x sends DELETE params in the query string; read `request.GET`.
5. **403 CSRF failure on HTMX POST** → `hx-headers` on `<body>` with `{{ csrf_token }}` or `htmx:configRequest` hook.
6. **Back button shows a bare fragment** → pushed URLs must render full pages; detect `HX-History-Restore-Request`; add `Vary: HX-Request`.
7. **Swapped into the wrong element** → inherited `hx-target` from an ancestor; use `hx-disinherit` or explicit targets.
8. **Returning 204 to remove a row** → 204 swaps nothing; return 200 with empty body or use `hx-swap="delete"`.
9. **`hx-get` on a "Mark done" button** → use `hx-post`; GET must be safe.
10. **`hx-on` silently dead under CSP** → `allowEval: false` disables it; move handlers to a module listening for HTMX events.
11. **Stale search results overwrite newer ones** → `hx-sync="this:replace"`.
12. **Third-party widgets break after swap** → initialize in `htmx:load` (receives each new element), not `DOMContentLoaded`.
13. **`HX-Trigger` JSON built by string formatting with user data** → `json.dumps`.
14. **Live region recreated by every swap (no announcements)** → keep `role="status"` container stable; swap its children.
15. **Fragment endpoints skip tenant scoping because "only the page links to them"** → scope every queryset.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Click does nothing | HTMX not loaded / element inserted without `htmx.process` / `hx-disable` ancestor | Console `htmx.version`; DevTools Network shows no XHR |
| Request sent, nothing swaps | 4xx/5xx or 204; target selector matches nothing | Response status; `htmx.logAll()`; `htmx:targetError` event |
| 403 on POST | Missing CSRF header or trusted origin | Request headers include `X-CSRFToken`; `CSRF_TRUSTED_ORIGINS` has scheme |
| Fragment rendered as whole page or vice versa | Wrong branch on `HX-Request`/`HX-Boosted`/history restore | Log headers; `Vary` present |
| Duplicate requests | Multiple triggers (`input` + `change`) without `delay`/`hx-sync` | Network panel timing |
| Event listener never fires | `HX-Trigger` name mismatch or listening on wrong element (`from:body`) | `htmx.logAll()`; event names are case-sensitive |
| Infinite scroll loads everything at once | Sentinel visible at load / inside overflow container with `revealed` | Use `intersect once`, check layout |
| Polling never stops | Returned fragment still has `hx-trigger="every …"` | Inspect response HTML; return 286 |

Validation steps:

- Django tests: `client.get(url, headers={"HX-Request": "true"})` (Django ≥ 4.2 `headers=`) asserts fragment template, status 422 on invalid, `HX-Trigger`/`HX-Redirect` headers, `Vary` header, 403/404 for other tenants ([frontend-testing.md](frontend-testing.md), [views-forms-and-middleware-testing.md](../../django-testing/references/views-forms-and-middleware-testing.md)).
- Playwright: type in search, `expect` row count; open modal, check focus inside `dialog`, Escape closes; session expiry → lands on login with correct `next` ([browser-and-end-to-end-testing.md](../../django-testing/references/browser-and-end-to-end-testing.md)).
- Debug in browser: `htmx.logAll()`; Network panel filter `HX-Request`.
- Disable JavaScript and confirm core flows still work via `href`/`action` where promised.

## 11. Related references and skills

- [alpine-js-engineering.md](alpine-js-engineering.md) — local UI state alongside HTMX
- [django-forms-and-validation.md](django-forms-and-validation.md) — form rendering, errors, HTMX submissions
- [django-template-engineering.md](django-template-engineering.md) — partials, includes, components
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — loading/empty/error states, race conditions
- [frontend-accessibility.md](frontend-accessibility.md) — focus after swaps, live regions, dialogs
- [frontend-security.md](frontend-security.md) — CSP, XSS, CSRF from the browser side
- [frontend-performance.md](frontend-performance.md) — CWV impact of swaps and polling
- [frontend-testing.md](frontend-testing.md) — HTMX request tests and E2E
- [production-frontend-architectures.md](production-frontend-architectures.md) — Django+HTMX reference architecture
- [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md), [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md), [authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md), [multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md)
- [urls-views.md](../../django-core/references/urls-views.md), [middleware.md](../../django-core/references/middleware.md), [authentication-sessions.md](../../django-core/references/authentication-sessions.md)
- [django-application-performance.md](../../django-performance/references/django-application-performance.md)

## 12. Official documentation and external references

- https://htmx.org/docs/
- https://htmx.org/reference/
- https://htmx.org/attributes/hx-trigger/
- https://htmx.org/attributes/hx-swap/
- https://htmx.org/attributes/hx-swap-oob/
- https://htmx.org/reference/#response_headers
- https://htmx.org/docs/#config
- https://htmx.org/docs/#security
- https://htmx.org/migration-guide-htmx-1/
- https://htmx.org/examples/
- https://django-htmx.readthedocs.io/ (third-party package docs)
- https://docs.djangoproject.com/en/5.2/howto/csrf/#using-csrf-protection-with-ajax
- https://docs.djangoproject.com/en/5.2/ref/request-response/#querydict-objects
- https://docs.djangoproject.com/en/5.2/topics/http/decorators/
- https://docs.djangoproject.com/en/5.2/ref/utils/#django.utils.http.url_has_allowed_host_and_scheme
- https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog
- https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles/status_role
