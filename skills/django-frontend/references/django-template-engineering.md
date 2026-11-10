# Django Template Engineering

> **Scope:** The Django Template Language (DTL) used as a UI composition system: template organization, inheritance and blocks, includes, partials, context processors, custom filters/tags, reusable components, escaping, presentation-logic boundaries, rendering performance and debugging. It does not cover template loader/engine lifecycle internals ([django-core](../../django-core/SKILL.md)), form rendering details ([django-forms-and-validation.md](django-forms-and-validation.md)) or HTMX request flows ([htmx-engineering.md](htmx-engineering.md)).

## 1. Purpose and scope

Use this reference when you create or refactor server-rendered UI in Django: page layouts, a
component library of includes/inclusion tags, dashboards, data tables, pagination, or when you
need to decide where presentation logic lives. The goal is templates that are:

- **Composable** — layouts → pages → components → partials, each with an explicit input contract.
- **Dumb** — no queries, no permission decisions, no business rules; they render a prepared
  view model.
- **Safe** — autoescape always on; HTML is built with `format_html`, never string concatenation.
- **Cheap** — cached loader, no lazy queries triggered in loops, fragment caching where measured.

Out of scope: semantic markup choices ([html-and-semantic-ui.md](html-and-semantic-ui.md)), CSS
class strategy ([tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md)), Jinja2.

## 2. Core concepts

| Concept | What it is | Rule of thumb |
|---|---|---|
| Layout | `layouts/base.html` with `{% block %}` slots | One per "shell" (app, auth, print, email) |
| Page | Extends a layout, fills blocks | One per view; lives in `<app>/` |
| Component | Include or inclusion tag with a documented context contract | Always `only` (include) or explicit args (tag) |
| Partial | Named fragment inside a page, renderable alone (`page.html#rows`) | For HTMX swaps of part of a page |
| Context processor | Function adding vars to every `RequestContext` | Cheap or lazy; global chrome only |
| Filter | `{{ value|filter:arg }}` — pure value transform | Formatting only; no I/O |
| `simple_tag` | `{% tag a b as x %}` — returns a value/escaped string | Small computed values, URL builders |
| `inclusion_tag` | Renders a template with a context the function builds | Components needing prep logic |
| View model | Plain dataclass/dict prepared by a selector/view | Everything the template needs, precomputed |

### Directory organization

```text
templates/                         # project-level (TEMPLATES["DIRS"])
├── layouts/
│   ├── base.html                  # <html>, <head>, skip link, header/nav, messages, main
│   ├── app.html                   # extends base: sidebar + content area
│   └── auth.html                  # extends base: centered card, no nav
├── components/                    # reusable, context-contract documented at top
│   ├── button.html
│   ├── card.html
│   ├── nav.html
│   ├── pagination.html
│   ├── data_table.html
│   ├── stat_card.html
│   └── field.html
├── partials/                      # cross-app fragments (messages, toasts, empty states)
│   └── messages.html
├── 404.html  500.html  403.html   # 500.html gets no context: keep static
projects/templates/projects/       # app templates (APP_DIRS), namespaced by app label
├── project_list.html
├── project_detail.html
└── _task_row.html                 # app-private fragment
```

- Project `DIRS` hold shared chrome and components; apps hold their own pages under
  `<app>/templates/<app>/` so names never collide (`"projects/project_list.html"`).
- Order: `DIRS` are searched before app directories; earlier `INSTALLED_APPS` win among apps.
  This is how you override third-party templates (e.g. `templates/admin/base_site.html`).
- A leading underscore marks "fragment, not a page" — convention only.

## 3. Internal mechanics

Only what changes how you write templates; the engine/loader lifecycle belongs to
[django-core](../../django-core/SKILL.md).

- **Compile once, render many.** Templates are parsed into a node tree; with the cached loader the
  tree is reused across requests. Since Django 4.1 the cached loader is enabled whenever
  `OPTIONS["loaders"]` is not set, including in development (the autoreloader clears it on
  change). Implication: tag/node classes must be stateless across renders — store per-render
  state in `context.render_context`, not on `self`.
- **Inheritance resolution.** `{% extends %}` must be the first tag. The child's top-level blocks
  replace the parent's same-named blocks; content outside blocks in a child is ignored.
  `{{ block.super }}` renders the parent's version. Block names are unique per template.
- **Includes render independently.** `{% include %}` renders another template with the current
  context (or only the passed vars with `only`). Blocks inside an included template do not
  participate in the includer's inheritance chain. Context is a stack: `with` pushes a layer that
  is popped after the include.
- **Variable resolution.** `{{ a.b }}` tries dict lookup, attribute, then list index; callables
  are called with no arguments (unless `do_not_call_in_templates = True`). Any exception marked
  `silent_variable_failure` and missing lookups render as `string_if_invalid` (default `""`).
  This is why a `.all` on a relation or a property that queries the DB silently runs SQL inside a
  loop.
- **Autoescape.** Variable output is HTML-escaped (`< > ' " &`) unless the value is a `SafeString`.
  `mark_safe`, `format_html`, `|safe`, and `{% autoescape off %}` produce or allow `SafeString`.
  Filters registered with `is_safe=True` / `needs_autoescape=True` participate in this.
- **Context processors** run when a `RequestContext` is built (every `render()` /
  `TemplateResponse`), before any template code, whether or not the template uses the vars.
- **Partials** (`{% partialdef %}`) are compiled as named sub-nodes of the parent template; a
  loader resolves `"page.html#name"` to that sub-node, so the fragment and the page share one
  source of truth. Built in from Django 6.0; on 5.2 provided by django-template-partials
  (third-party).

## 4. Engineering decisions and trade-offs

### Include vs inclusion tag vs partial

| Prefer | When |
|---|---|
| `{% include "components/x.html" with a=b only %}` | Component is pure markup over given values |
| `inclusion_tag` | Component needs prep logic (derive classes, build URLs, compute page windows) or a stable Python API with defaults |
| `simple_tag` | You need a value (string/URL/number), optionally `as var` |
| `simple_block_tag` (Django ≥ 5.2) | Component wraps caller-provided markup (card body, modal content) |
| Partial (`{% partialdef %}`) | A page region must also be rendered alone (HTMX swap, polling) without a second file |
| Separate `_fragment.html` file | Fragment reused by several pages |

### Where logic lives

| Logic | Location |
|---|---|
| Querying, filtering, permission checks, tenant scoping | Selector/service, called from the view |
| "Can the user edit?" | View computes `can_edit` (backed by real permission check) |
| Formatting (money, dates, pluralization) | Filters / `humanize` / `localize` |
| CSS class choices from state (`status → badge color`) | View model property or inclusion tag; never string-built in markup |
| Display ordering/grouping | Selector (`order_by`) — `regroup` only re-groups already-sorted data |

### Template-level caching

| Prefer `{% cache %}` when | Avoid when |
|---|---|
| Expensive fragment, measured, same for many users (key by tenant/locale) | Fragment contains CSRF tokens, per-user data not in key, or flash messages |
| Invalidation by TTL is acceptable | Correctness needs immediate invalidation (use view-level/low-level cache with explicit keys) |

## 5. Django/frontend implementation patterns

### Settings

```python
# config/settings/base.py
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",   # needed by {% querystring %}
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.ui",
            ],
            "builtins": ["core.templatetags.ui"],   # optional: auto-load the component library
        },
    }
]

# config/settings/dev.py
TEMPLATES[0]["OPTIONS"]["string_if_invalid"] = "!!MISSING %s!!"   # surface typos; breaks admin's reliance on ""
```

### Cheap context processor

```python
# core/context_processors.py
from django.utils.functional import SimpleLazyObject

def ui(request):
    def tenant_nav():
        # Evaluated only if a template touches `main_nav`; selector does the scoping.
        from core.selectors import nav_items_for
        return nav_items_for(request.user, getattr(request, "tenant", None))

    return {
        "main_nav": SimpleLazyObject(tenant_nav),
        "app_name": "Acme Projects",
    }
```

### Base layout

```django
{# templates/layouts/base.html #}
{% load static %}
<!doctype html>
<html lang="{{ LANGUAGE_CODE|default:'en' }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}{{ app_name }}{% endblock %}</title>
  <link rel="stylesheet" href="{% static 'css/app.css' %}">
  {% block head_extra %}{% endblock %}
  <script src="{% static 'vendor/htmx.min.js' %}" defer></script>
</head>
<body class="{% block body_class %}{% endblock %}">
  <a class="skip-link" href="#main">Skip to content</a>
  {% block header %}
    <header class="site-header">
      {% include "components/nav.html" with items=main_nav current=request.resolver_match.url_name user=request.user only %}
    </header>
  {% endblock %}
  {% include "partials/messages.html" %}
  <main id="main" tabindex="-1">
    {% block content %}{% endblock %}
  </main>
  {% block scripts %}{% endblock %}
</body>
</html>
```

```django
{# templates/layouts/app.html #}
{% extends "layouts/base.html" %}
{% block body_class %}layout-app{% endblock %}
{% block content %}
  <div class="app-grid">
    <aside aria-label="Section navigation">{% block sidebar %}{% endblock %}</aside>
    <div class="app-content">
      <h1>{% block page_title %}{% endblock %}</h1>
      {% block page %}{% endblock %}
    </div>
  </div>
{% endblock %}
```

```django
{# projects/templates/projects/project_detail.html #}
{% extends "layouts/app.html" %}
{% block title %}{{ project.name }} · {{ block.super }}{% endblock %}
{% block page_title %}{{ project.name }}{% endblock %}
{% block page %}…{% endblock %}
```

### Nav component (include with an explicit contract)

```django
{# templates/components/nav.html
   Contract: items: iterable of NavItem(label, url_name, url, icon?) ; current: str url_name ; user #}
<nav aria-label="Main">
  <ul class="nav">
    {% for item in items %}
      <li>
        <a href="{{ item.url }}"
           {% if item.url_name == current %}aria-current="page" class="nav-link is-active"{% else %}class="nav-link"{% endif %}>
          {{ item.label }}
        </a>
      </li>
    {% endfor %}
  </ul>
  {% if user.is_authenticated %}
    <form method="post" action="{% url 'logout' %}">
      {% csrf_token %}
      <button type="submit" class="nav-link">Sign out</button>
    </form>
  {% endif %}
</nav>
```

`aria-current="page"` is the native-semantics way to mark the active link; styling hooks off it
or a class. Logout is a POST form (Django ≥ 5.0's `LogoutView` no longer accepts GET).

### Component library: filters, simple_tag, inclusion_tag, block tag

```python
# core/templatetags/ui.py
from decimal import Decimal
from django import template
from django.utils.html import format_html, format_html_join
from django.utils.formats import number_format

register = template.Library()

BADGE_TONES = {"open": "info", "blocked": "danger", "done": "success"}

@register.filter
def money(value: Decimal | None, currency: str = "EUR") -> str:
    if value is None:
        return "—"
    return f"{number_format(value, decimal_pos=2, use_l10n=True)} {currency}"   # escaped on output

@register.simple_tag
def badge(status: str, label: str):
    tone = BADGE_TONES.get(status, "neutral")          # whitelist → no class injection
    return format_html('<span class="badge badge--{}">{}</span>', tone, label)

@register.simple_tag
def attrs(**kwargs):
    """{% attrs id=x data_task=task.pk %} → safe attribute string (keys from code, values escaped)."""
    return format_html_join(" ", '{}="{}"', ((k.replace("_", "-"), v) for k, v in kwargs.items() if v is not None))

@register.inclusion_tag("components/button.html")
def button(label, *, variant="primary", type="button", href=None, disabled=False):
    return {"label": label, "variant": variant, "type": type, "href": href, "disabled": disabled}

@register.inclusion_tag("components/pagination.html", takes_context=True)
def pagination(context, page_obj, window=2):
    current, last = page_obj.number, page_obj.paginator.num_pages
    pages = sorted({1, last, *range(max(1, current - window), min(last, current + window) + 1)})
    return {"page_obj": page_obj, "pages": pages, "request": context["request"]}

@register.simple_block_tag   # Django ≥ 5.2
def card(content, title, *, level=2):
    return format_html(
        '<section class="card"><h{0} class="card__title">{1}</h{0}><div class="card__body">{2}</div></section>',
        int(level), title, content,   # `content` is already-rendered SafeString from the block body
    )
```

```django
{# templates/components/button.html — Contract: label, variant, type, href?, disabled #}
{% if href %}
  <a class="btn btn--{{ variant }}" href="{{ href }}">{{ label }}</a>
{% else %}
  <button class="btn btn--{{ variant }}" type="{{ type }}"{% if disabled %} disabled{% endif %}>{{ label }}</button>
{% endif %}
```

Links navigate, buttons act — do not render `<a>` with `onclick` or `<div role="button">`.
A disabled button is UX only; the view must still reject the action.

### Pagination component with `{% querystring %}` (Django ≥ 5.1)

```django
{# templates/components/pagination.html #}
{% if page_obj.has_other_pages %}
<nav aria-label="Pagination">
  <ul class="pagination">
    {% if page_obj.has_previous %}
      <li><a href="{% querystring page=page_obj.previous_page_number %}" rel="prev">Previous</a></li>
    {% endif %}
    {% for n in pages %}
      <li>
        {% if n == page_obj.number %}
          <a href="{% querystring page=n %}" aria-current="page">{{ n }}</a>
        {% else %}
          <a href="{% querystring page=n %}">{{ n }}</a>
        {% endif %}
      </li>
    {% endfor %}
    {% if page_obj.has_next %}
      <li><a href="{% querystring page=page_obj.next_page_number %}" rel="next">Next</a></li>
    {% endif %}
  </ul>
</nav>
{% endif %}
```

`{% querystring %}` copies `request.GET` (needs the `request` context processor or a `request`
var — that is why the inclusion tag passes it) and overrides/removes keys (`page=None` removes),
preserving filters and sort. Output is escaped for HTML attributes. [VERIFY: exact
leading-`?` and empty-result behavior in your Django version — it changed after 5.1.]
On Django < 5.1 write a small `simple_tag(takes_context=True)` that copies `request.GET`, sets
keys and returns `"?" + qd.urlencode()`.

### Sortable, paginated data table

```python
# projects/views.py
from dataclasses import dataclass
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from .selectors import tasks_for_project

SORTS = {"title": "title", "due": "due_date", "status": "status"}   # whitelist: URL key → field

@dataclass(frozen=True)
class Column:
    key: str
    label: str
    sortable: bool = True

COLUMNS = [Column("title", "Title"), Column("status", "Status"), Column("due", "Due"), Column("assignee", "Assignee", False)]

@login_required
def task_list(request, project_id):
    project, qs = tasks_for_project(request.user, project_id)   # 404s / scopes by tenant inside
    sort = request.GET.get("sort", "due")
    desc = sort.startswith("-")
    field = SORTS.get(sort.lstrip("-"), "due_date")
    qs = qs.select_related("assignee").order_by(f"-{field}" if desc else field, "pk")
    page_obj = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "projects/task_list.html", {
        "project": project, "page_obj": page_obj, "columns": COLUMNS,
        "sort_key": sort.lstrip("-"), "sort_desc": desc,
    })
```

```django
{# projects/templates/projects/task_list.html #}
{% extends "layouts/app.html" %}
{% load ui %}
{% block page_title %}Tasks · {{ project.name }}{% endblock %}
{% block page %}
<div class="table-wrap" role="region" aria-labelledby="tasks-caption" tabindex="0">
  <table class="data-table">
    <caption id="tasks-caption">Tasks ({{ page_obj.paginator.count }})</caption>
    <thead>
      <tr>
        {% for col in columns %}
          {% if col.sortable %}
            {% if col.key == sort_key %}
              <th scope="col" aria-sort="{{ sort_desc|yesno:'descending,ascending' }}">
                <a href="{% querystring sort=sort_desc|yesno:',-'|add:col.key page=None %}">{{ col.label }}</a>
              </th>
            {% else %}
              <th scope="col"><a href="{% querystring sort=col.key page=None %}">{{ col.label }}</a></th>
            {% endif %}
          {% else %}
            <th scope="col">{{ col.label }}</th>
          {% endif %}
        {% endfor %}
      </tr>
    </thead>
    <tbody>
      {% for task in page_obj %}
        <tr>
          <th scope="row"><a href="{{ task.get_absolute_url }}">{{ task.title }}</a></th>
          <td>{% badge task.status task.get_status_display %}</td>
          <td>{{ task.due_date|date:"SHORT_DATE_FORMAT"|default:"—" }}</td>
          <td>{{ task.assignee.get_full_name|default:"Unassigned" }}</td>
        </tr>
      {% empty %}
        <tr><td colspan="{{ columns|length }}">No tasks yet. {% button "New task" href=project.new_task_url %}</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% pagination page_obj %}
{% endblock %}
```

Notes: `aria-sort` goes only on the currently sorted header; the header link toggles direction
(`yesno:',-'` prepends `-` when switching from ascending). Sorting via links keeps the state in
the URL (shareable, back-button friendly) and works without JS; HTMX can later boost it
(`hx-boost` or `hx-get` targeting the table region — see [htmx-engineering.md](htmx-engineering.md)).
The `select_related` prevents N+1 from `task.assignee`.

### Dashboard cards with a view model

```python
# dashboard/selectors.py
from dataclasses import dataclass

@dataclass(frozen=True)
class StatCard:
    label: str
    value: str          # preformatted
    delta_pct: float | None
    href: str

    @property
    def trend(self) -> str:      # presentation state computed in Python, not in the template
        if self.delta_pct is None:
            return "flat"
        return "up" if self.delta_pct > 0 else "down" if self.delta_pct < 0 else "flat"

def dashboard_cards(user, tenant) -> list[StatCard]:
    stats = Invoice.objects.for_tenant(tenant).dashboard_aggregates()   # one aggregate query
    return [
        StatCard("Open invoices", str(stats["open_count"]), stats["open_delta"], reverse("billing:invoice_list") + "?status=open"),
        StatCard("Overdue", money(stats["overdue_total"]), stats["overdue_delta"], reverse("billing:invoice_list") + "?status=overdue"),
    ]
```

```django
{# templates/components/stat_card.html — Contract: card: StatCard #}
<article class="stat-card stat-card--{{ card.trend }}">
  <h2 class="stat-card__label"><a href="{{ card.href }}">{{ card.label }}</a></h2>
  <p class="stat-card__value">{{ card.value }}</p>
  {% if card.delta_pct is not None %}
    <p class="stat-card__delta">
      <span aria-hidden="true">{% if card.trend == "up" %}▲{% elif card.trend == "down" %}▼{% endif %}</span>
      {{ card.delta_pct|floatformat:1 }}% vs last month
    </p>
  {% endif %}
</article>
```

```django
{# dashboard/templates/dashboard/home.html #}
{% extends "layouts/app.html" %}
{% load cache %}
{% block page_title %}Dashboard{% endblock %}
{% block page %}
  <div class="stat-grid">
    {% for card in cards %}{% include "components/stat_card.html" with card=card only %}{% endfor %}
  </div>
  {% cache 300 tenant_activity request.tenant.pk LANGUAGE_CODE %}
    {% include "dashboard/_activity.html" with items=activity only %}
  {% endcache %}
{% endblock %}
```

Trend is conveyed by text ("vs last month" with sign/arrow) not color alone. Caveat: with
`{% cache %}`, `activity` is still computed in the view even on a cache hit unless it is lazy —
pass a callable/lazy object or cache in the selector instead.

### Form field component (minimal; full treatment in the forms reference)

```django
{# templates/components/field.html — Contract: field: BoundField #}
<div class="field{% if field.errors %} field--invalid{% endif %}">
  {{ field.label_tag }}
  {{ field }}
  {% if field.help_text %}<p class="field__help" id="{{ field.auto_id }}_helptext">{{ field.help_text }}</p>{% endif %}
  {% for error in field.errors %}<p class="field__error" id="{{ field.auto_id }}_error_{{ forloop.counter }}">{{ error }}</p>{% endfor %}
</div>
```

Django ≥ 5.0 offers `{{ field.as_field_group }}` with a project-wide field template — prefer
that; see [django-forms-and-validation.md](django-forms-and-validation.md) for error wiring
(`aria-invalid`, `aria-describedby`).

### Partials for HTMX fragments

```django
{# projects/templates/projects/task_board.html — Django 5.2 + django-template-partials (third-party) #}
{% extends "layouts/app.html" %}
{% load partials %}
{% block page %}
  {% partialdef column inline %}
    <section id="col-{{ column.key }}" aria-labelledby="col-{{ column.key }}-h">
      <h2 id="col-{{ column.key }}-h">{{ column.label }}</h2>
      {% for task in column.tasks %}{% include "projects/_task_row.html" with task=task only %}{% endfor %}
    </section>
  {% endpartialdef %}
{% endblock %}
```

```python
def board_column(request, project_id, key):
    column = board_column_for(request.user, project_id, key)
    return render(request, "projects/task_board.html#column", {"column": column})
```

Install with `INSTALLED_APPS += ["template_partials"]`; on Django 6.0 drop the package and the
`{% load %}`. [VERIFY: `inline` argument syntax — `inline` vs `inline=True` differs between
django-template-partials versions and Django 6.0.] Inline partials inside a loop render once per
iteration with the loop context, so name the loop variable as the partial expects.

### Iteration, grouping, conditionals

```django
{% regroup tasks by get_status_display as by_status %}   {# tasks must be ordered by status #}
{% for group in by_status %}
  <h3>{{ group.grouper }} <span class="count">({{ group.list|length }})</span></h3>
  <ul>
    {% for t in group.list %}
      <li class="{% cycle 'row-a' 'row-b' %}">{{ forloop.counter }}. {{ t.title }}{% if forloop.last %} (last){% endif %}</li>
    {% endfor %}
  </ul>
{% empty %}
  {% include "partials/empty_state.html" with message="Nothing here yet." only %}
{% endfor %}

{% with total=invoice.total_cached %}{% if total > 0 %}{{ total|money }}{% endif %}{% endwith %}
<a href="{% url 'billing:invoice_detail' pk=invoice.pk %}">View</a>
{% url 'billing:invoice_pdf' pk=invoice.pk as pdf_url %}{% if pdf_url %}<a href="{{ pdf_url }}" download>PDF</a>{% endif %}
```

### Escaping and passing data to JS

```python
from django.utils.html import format_html, format_html_join

def assignee_list(users):
    # Never f-strings/concat for HTML; format_html escapes every argument.
    return format_html("<ul>{}</ul>", format_html_join("", "<li>{}</li>", ((u.get_full_name(),) for u in users)))
```

```django
{{ chart_series|json_script:"chart-data" }}
<script type="module" src="{% static 'js/charts.js' %}"></script>
```

`json_script` emits `<script type="application/json">` with `<`, `>`, `&` escaped; read it with
`JSON.parse(document.getElementById("chart-data").textContent)` (details:
[javascript-engineering.md](javascript-engineering.md)). Never write `var x = {{ data|safe }}`.

## 6. Production use cases

- **Multi-tenant SaaS shell:** `layouts/app.html` with tenant-aware nav from a lazy context
  processor; pages per app; components in `templates/components/` auto-loaded via `builtins`.
- **Admin/reporting UI:** sortable, paginated tables using a column whitelist and
  `{% querystring %}`; CSV export link reuses the same query string.
- **HTMX dashboards:** each widget is a partial; the page renders all, a polling endpoint renders
  `"dashboard/home.html#widget"`; `Vary: HX-Request` when one URL returns both.
- **Overriding third-party templates:** project `DIRS` shadow `admin/`, `registration/`,
  `django/forms/` templates without forking packages.
- **Email templates:** separate `layouts/email.html` (inline-safe CSS), never sharing the web
  base (different escaping needs for plain-text variants: `{% autoescape off %}` only in `.txt`).

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Zero client JS needed; fast first paint, SEO-friendly | No type checking of context; typos fail silently |
| Autoescape by default; small XSS surface | Component "props" are informal (document contracts) |
| Inheritance + includes cover most layout needs | Includes add per-render overhead in tight loops |
| Partials enable HTMX without duplicating markup | Logic creep: easy to sneak queries into templates via lazy attributes |
| Cached loader makes rendering cheap | Limited expressions push logic into tags (good) or views |

## 8. Security and performance considerations

### Security

- Autoescape stays on. `|safe`, `mark_safe` and `{% autoescape off %}` require that the value is
  fully trusted or already sanitized; audit with `grep -rn "|safe\|mark_safe\|autoescape off"`.
- Build HTML in Python only with `format_html`/`format_html_join`. Calling `format_html` with no
  args (as a `mark_safe` substitute) is deprecated since Django 5.0 — use `mark_safe` on a
  constant, or pass the values as args [VERIFY: removal version].
- Attribute contexts: always quote attributes (`href="{{ url }}"`). Escaping does not make a
  `javascript:` URL safe — validate user-supplied URLs server-side (see
  [frontend-security.md](frontend-security.md)).
- Do not interpolate into `<script>` or inline event handlers; use `json_script` + static JS.
- Templates are not authorization: `{% if perms.billing.delete_invoice %}` hides a button, the
  view must enforce the permission. Never put secrets or other tenants' data in context
  "because the template doesn't show it" (`{% debug %}`, error pages, and future edits leak it).
- `string_if_invalid` must be `""` in production.
- Deep guidance: [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md).

### Performance

- Keep the default (cached) loader; never configure `loaders` without wrapping in
  `django.template.loaders.cached.Loader` in production.
- No queries in templates: prefetch in the selector (`select_related`/`prefetch_related`), pass
  counts as annotations, avoid `{{ obj.related.all|length }}` (use `.count` annotations).
  Verify with django-debug-toolbar (third-party) or `assertNumQueries` in tests.
- Includes/inclusion tags inside 500-row loops cost measurable time; for large tables, inline
  the row markup or use one fragment per row, and paginate.
- Context processors run on every render: return lazy objects for anything that queries.
- `{% cache %}` keys must include every input that changes the output (tenant, user/role,
  language, timezone); measure before adding.
- Rendering performance beyond templates: [django-application-performance.md](../../django-performance/references/django-application-performance.md).

## 9. Common mistakes and anti-patterns

1. **Queries in loops** (`{% for p in projects %}{{ p.owner.name }}{{ p.tasks.count }}`) → annotate
   counts and `select_related` in the selector.
2. **Includes without `only`** silently depending on ambient vars → `with … only` and a contract
   comment at the top of each component.
3. **Business rules in templates** (`{% if invoice.total > 1000 and user.is_staff %}`) → compute
   `requires_approval` in the view model.
4. **Building class names by concatenation** (`class="badge-{{ status }}"`) → whitelist mapping
   in Python; also required for Tailwind class detection.
5. **`|safe` on user content / `mark_safe(f"...{name}...")`** → `format_html`.
6. **Heavy context processors** (querying notifications on every page incl. HTMX fragments) →
   `SimpleLazyObject` or load via a dedicated endpoint.
7. **`{% regroup %}` on unsorted data** → order by the grouping key first.
8. **Blocks in included templates expecting to be overridden** → use `{% extends %}` or a
   block tag component.
9. **Hard-coded URLs/paths** (`href="/projects/{{ id }}/"`, `/static/app.css`) → `{% url %}`,
   `get_absolute_url`, `{% static %}`.
10. **Per-user data inside `{% cache %}` keyed only by tenant** → add user/role to the key or
    move the fragment out.
11. **`500.html` extending a base that uses context processors/DB** → keep it static.
12. **Duplicating a page fragment for HTMX** → partials or a shared `_fragment.html`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `TemplateDoesNotExist` | Wrong name/namespace, app not in `INSTALLED_APPS`, `APP_DIRS` false | Debug page "Template-loader postmortem" lists every path tried |
| Variable renders empty | Typo, missing context key, exception swallowed | Set `string_if_invalid` in dev; `{% debug %}` locally |
| Edits to templates not visible | Custom `loaders` with cached loader and no reload, stale container image | Remove explicit loaders in dev; rebuild image |
| HTML shown as text (`&lt;div&gt;`) | Python built a plain `str` of HTML | Return `format_html(...)` / `SafeString` |
| Page slow, many SQL queries | Lazy relations in loops, context processors | debug-toolbar SQL + Templates panels; `assertNumQueries` |
| Custom tag "not a registered tag library" | Missing `templatetags/__init__.py`, server not restarted, wrong `{% load %}` name | Module name = load name; restart runserver |
| `{% querystring %}` invalid tag | Django < 5.1 | Upgrade or custom tag |
| Partial not found (`page.html#x`) | Package not installed / name mismatch / Django 5.2 without django-template-partials | `INSTALLED_APPS`, partial name |
| Wrong active nav item | `resolver_match.url_name` lacks namespace | Compare `request.resolver_match.view_name` |

Validation steps:

1. Render tests: `response = client.get(url)`; `assertTemplateUsed`, `assertContains(response,
   'aria-current="page"', html=False)`, and `assertNumQueries` around the request (see
   [frontend-testing.md](frontend-testing.md)).
2. Run with `string_if_invalid` set in a CI settings variant and fail on `!!MISSING` in output.
3. `python manage.py check --deploy` and a grep audit for `|safe`/`mark_safe`.
4. Validate HTML output (Nu HTML Checker) for key pages; run axe in browser tests.

## 11. Related references and skills

- [html-and-semantic-ui.md](html-and-semantic-ui.md) — markup semantics used inside components
- [django-forms-and-validation.md](django-forms-and-validation.md) — form/field templates
- [htmx-engineering.md](htmx-engineering.md) — fragment responses, OOB swaps
- [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) — classes in components
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — tables, dashboard layouts, UI states
- [frontend-accessibility.md](frontend-accessibility.md) — nav, tables, live regions
- [frontend-security.md](frontend-security.md) — escaping, safe HTML, CSP
- [frontend-testing.md](frontend-testing.md) — template rendering tests
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — `{% static %}` and manifests
- [django-core](../../django-core/SKILL.md) — [urls-views.md](../../django-core/references/urls-views.md), [request-response-lifecycle.md](../../django-core/references/request-response-lifecycle.md)
- [layered-architecture.md](../../django-architecture/references/layered-architecture.md), [service-layer.md](../../django-architecture/references/service-layer.md) — selectors/services
- [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md)
- [caching-engineering.md](../../django-performance/references/caching-engineering.md)

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/ref/templates/language/
- https://docs.djangoproject.com/en/5.2/ref/templates/builtins/
- https://docs.djangoproject.com/en/5.2/ref/templates/api/
- https://docs.djangoproject.com/en/5.2/howto/custom-template-tags/
- https://docs.djangoproject.com/en/5.2/topics/templates/
- https://docs.djangoproject.com/en/5.2/ref/utils/#module-django.utils.html
- https://docs.djangoproject.com/en/5.2/topics/cache/#template-fragment-caching
- https://docs.djangoproject.com/en/5.2/topics/pagination/
- https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-current
- https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-sort
- https://www.w3.org/WAI/tutorials/tables/
- https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
