# HTML and Semantic UI

> **Scope:** Writing correct, semantic HTML in Django templates: document structure, landmarks, headings, navigation, links vs buttons, forms markup (labels, input types, fieldset/legend), tables, `<dialog>`, `<details>`/`<summary>`, metadata, SEO and Open Graph. Template composition mechanics belong to [django-template-engineering.md](django-template-engineering.md); form behaviour and validation UX to [django-forms-and-validation.md](django-forms-and-validation.md); WCAG, focus management and ARIA testing to [frontend-accessibility.md](frontend-accessibility.md).

## 1. Purpose and scope

Use this reference when an agent writes or reviews markup for a Django page or partial and needs to choose the right element, structure the document for assistive technology and crawlers, or emit correct metadata. Semantic HTML is the cheapest accessibility, SEO and robustness win available: it gives keyboard behaviour, accessible names/roles/states, and default styling for free.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Semantics | Element conveys meaning (role) to the accessibility tree, search engines and browser features (reader mode, autofill) |
| Landmarks | `header` (banner, when not inside `article`/`section`/`main`…), `nav`, `main`, `aside` (complementary), `footer` (contentinfo, same caveat), `form` with accessible name, `section` with accessible name (region), `search` |
| Content categories | Flow, phrasing, interactive, etc. Interactive content must not nest inside interactive content (`<a>` inside `<button>` is invalid) |
| Accessible name | Text a screen reader announces: from content (`<button>Save</button>`), `<label>`, `alt`, `aria-labelledby`/`aria-label` |
| Implicit role | Native role, e.g. `<button>` = button, `<a href>` = link, `<input type=checkbox>` = checkbox |
| Document outline | Created by heading levels `h1`–`h6`, *not* by nested `section`s (the HTML "outline algorithm" was never implemented and was removed from the spec) |

### When semantic elements beat `<div>`

| Need | Use | Not | What you get for free |
|---|---|---|---|
| Action on the page | `<button type="button">` | `<div onclick>` | Focusable, Enter/Space activation, role=button, `disabled`, form participation |
| Go to a URL | `<a href>` | `<span onclick="location=…">` | Middle-click/new tab, copy link, crawlable, history, role=link |
| Primary content | `<main>` (one visible per page) | `<div id="content">` | Landmark navigation, skip-link target |
| Site/section navigation | `<nav aria-label="…">` | `<div class="nav">` | Landmark; AT lists navigations |
| Self-contained item (card, comment, post) | `<article>` | `<div class="card">` | Reader mode, syndication semantics |
| Thematic group with a heading | `<section aria-labelledby>` | `<div>` | Region landmark when named |
| Expand/collapse | `<details>`/`<summary>` | JS + `div` | Keyboard, state announced, works without JS, find-in-page can open it in supporting browsers |
| Modal/non-modal dialog | `<dialog>` | `div.modal` + JS focus trap | `showModal()`: top layer, inert background, Escape to close |
| Tabular data | `<table>` + `<th scope>` | CSS grid of divs | Row/column header announcements, table navigation |
| Label for a control | `<label for>` | `<span>` above input | Name, larger click target |
| Group of related controls | `<fieldset>` + `<legend>` | `div` + `h3` | Group name announced with each control |
| Date/time | `<time datetime>` | text | Machine-readable |
| Navigation state | `aria-current="page"` | `.active` class only | Announced "current page" |

Use `<div>`/`<span>` only for pure styling/layout hooks with no meaning.

## 3. Internal mechanics

### 3.1 From markup to the accessibility tree

The browser maps each DOM node to an accessibility object with **role, name, description, state, value** using HTML-AAM mappings. `<div>` maps to a generic role with no name source from attributes; `<button>` maps to role button, name from content, state from `disabled`/`aria-pressed`/`aria-expanded`. Screen readers, voice control and switch software consume this tree, not the pixels. CSS `display:none` and `hidden` remove nodes from it; `visibility:hidden` too; `opacity:0` does not.

### 3.2 Parser error recovery

The HTML parser never "fails": it repairs. Unclosed `<p>` before a `<div>` is implicitly closed; a `<form>` inside a `<form>` is dropped; a `<table>` with stray text moves content outside the table ("foster parenting"). Practical effect: Django templates that produce invalid nesting can render visually fine while the DOM (and HTMX swap targets) differ from the source. `<tr>` fragments returned for HTMX must be swapped into `<tbody>`, not parsed standalone into a `<div>` (HTMX handles table-element fragments specially [VERIFY: HTMX 2.x table-fragment parsing behaviour]).

### 3.3 Forms

- Submission collects *successful controls*: named, not `disabled`, checked (for checkbox/radio), inside the form or associated via `form="id"`.
- `disabled` fields are **not submitted**; `readonly` fields are. Neither is a security control — the server must ignore/validate them.
- Implicit submission: Enter in a text field submits the form if there is a submit button (or only one single-line field).
- `<button>` defaults to `type="submit"` inside a form — always set `type="button"` for non-submitting buttons.
- Constraint validation (`required`, `pattern`, `min`, `type=email`) runs before submit unless `novalidate`; it is UX only.
- `autocomplete` tokens (`email`, `name`, `current-password`, `new-password`, `one-time-code`, `street-address`, `postal-code`, `tel`) drive autofill and help WCAG 1.3.5 (Identify Input Purpose).

### 3.4 Dialog

- `dialog.showModal()`: element goes to the **top layer**, `::backdrop` is shown, the rest of the document becomes inert, focus moves inside (first focusable, or element with `autofocus`), Escape fires `cancel` then closes.
- `dialog.show()`: non-modal, no inertness, no Escape handling.
- `<form method="dialog">` inside a dialog closes it on submit and sets `dialog.returnValue` to the submit button's `value`; no HTTP request.
- On close, browsers return focus to the previously focused element in current implementations [VERIFY: focus-restoration behaviour across Chrome/Firefox/Safari]; verify and restore manually if needed.

### 3.5 Details/summary

`<details>` toggles `open`; `<summary>` is the focusable, keyboard-operable disclosure button. The `toggle` event fires after state change. Elements sharing `name` on `<details>` form an exclusive accordion (only one open) — newer feature [VERIFY: Baseline status of details name attribute].

### 3.6 Metadata

`<head>` metadata is parsed before body rendering: `<meta charset>` must be within the first 1024 bytes; `<meta name="viewport">` controls mobile layout; `<link rel="canonical">`, `<meta name="robots">`, `<meta name="description">` are read by crawlers; Open Graph (`og:*`, `property` attribute) and `twitter:*` tags are read by link-preview bots, which do not run JavaScript — they must be server-rendered.

## 4. Engineering decisions and trade-offs

### 4.1 Link or button?

| Prefer `<a href>` when… | Prefer `<button>` when… |
|---|---|
| It navigates to a URL (GET, idempotent) | It changes state (POST/PUT/DELETE) or acts on the page (open dialog, toggle) |
| Users may open it in a new tab or share it | It submits a form |
| It is an HTMX GET that should also work without JS (`href` + `hx-get`, or `hx-boost`) | It is an HTMX `hx-post`/`hx-delete` |

A "Delete" must be a `<button>` in a `<form method="post">` (with `{% csrf_token %}`), never a link — GET must not mutate state (prefetchers and crawlers follow links).

### 4.2 Native element or custom widget?

| Prefer native when… | Prefer a custom (ARIA) widget when… |
|---|---|
| `<select>`, `<input type=date>`, `<details>`, `<dialog>` meet the requirement | Requirement truly cannot be met natively (combobox with async suggestions, tree grid) |
| Mobile users benefit from OS pickers | You can implement the full APG keyboard pattern and test with screen readers |
| You lack time to test across AT | — |

### 4.3 Table or list/cards?

| Prefer `<table>` when… | Prefer `<ul>`/`<article>` cards when… |
|---|---|
| Data has comparable columns (invoices: number, client, amount, due) | Items are heterogeneous or content-heavy |
| Users sort/scan across rows | Primary action is "open item" |
| Responsive: allow horizontal scroll in a focusable, labelled wrapper | Mobile-first feed |

Never use tables for layout. See [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) for responsive tables.

### 4.4 Section vs article vs div

| Use `<article>` | Use `<section>` | Use `<div>` |
|---|---|---|
| Content makes sense standalone (invoice card, comment) | Thematic grouping that has a heading | No semantic meaning; styling/layout wrapper |

## 5. Django/frontend implementation patterns

### 5.1 Base layout with metadata block

```django
{# templates/base.html #}
{% load static %}
<!doctype html>
<html lang="{{ LANGUAGE_CODE|default:'en' }}" dir="{% if LANGUAGE_BIDI %}rtl{% else %}ltr{% endif %}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Acme Projects{% endblock %}</title>
  <meta name="description" content="{% block meta_description %}Plan projects, track tasks and send invoices.{% endblock %}">
  {% block meta_robots %}{% endblock %}
  <link rel="canonical" href="{% block canonical %}{{ request.build_absolute_uri }}{% endblock %}">
  {% block opengraph %}{% include "partials/opengraph.html" %}{% endblock %}
  <link rel="icon" href="{% static 'img/favicon.svg' %}" type="image/svg+xml">
  <link rel="stylesheet" href="{% static 'css/app.css' %}">
  <script type="module" src="{% static 'js/app.js' %}"></script>
  {% block head_extra %}{% endblock %}
</head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <header class="site-header">
    <a href="{% url 'dashboard:home' %}" class="brand">
      <img src="{% static 'img/logo.svg' %}" alt="Acme Projects home" width="120" height="32">
    </a>
    {% include "partials/primary_nav.html" %}
  </header>
  <main id="main" tabindex="-1">
    {% block content %}{% endblock %}
  </main>
  <footer class="site-footer">
    <p><small>&copy; {% now "Y" %} Acme Ltd.</small></p>
  </footer>
</body>
</html>
```

`LANGUAGE_CODE`/`LANGUAGE_BIDI` require the `django.template.context_processors.i18n` context processor. Canonical URLs built from `request.build_absolute_uri` include the query string — override `{% block canonical %}` on filtered list pages (or build from `request.path`) to avoid indexing every filter combination.

A page then sets only what it owns:

```django
{# templates/invoices/invoice_detail.html #}
{% extends "base.html" %}
{% block title %}Invoice {{ invoice.number }} · Acme Projects{% endblock %}
{% block meta_description %}Invoice {{ invoice.number }} for {{ invoice.client.name }}.{% endblock %}
{% block meta_robots %}<meta name="robots" content="noindex">{% endblock %}
{% block content %}
<article aria-labelledby="invoice-heading">
  <h1 id="invoice-heading">Invoice {{ invoice.number }}</h1>
  <p>Issued <time datetime="{{ invoice.issued_on|date:'Y-m-d' }}">{{ invoice.issued_on|date:"j F Y" }}</time></p>
  ...
</article>
{% endblock %}
```

Title pattern: specific → general (`Invoice INV-104 · Acme Projects`) so the distinguishing part is read/visible first (WCAG 2.4.2 Page Titled).

### 5.2 Primary navigation with `aria-current`

```django
{# templates/partials/primary_nav.html #}
{% with current=request.resolver_match.url_name %}
<nav aria-label="Primary">
  <ul role="list">
    <li><a href="{% url 'dashboard:home' %}" {% if current == 'home' %}aria-current="page"{% endif %}>Dashboard</a></li>
    <li><a href="{% url 'projects:list' %}" {% if request.resolver_match.namespace == 'projects' %}aria-current="page"{% endif %}>Projects</a></li>
    <li><a href="{% url 'invoices:list' %}" {% if request.resolver_match.namespace == 'invoices' %}aria-current="page"{% endif %}>Invoices</a></li>
  </ul>
</nav>
{% endwith %}
```

```css
.site-header nav a[aria-current="page"] { font-weight: 700; text-decoration: underline; text-underline-offset: .25em; }
```

- Style from `aria-current`, not a parallel `.active` class — one source of truth, and it is announced.
- `role="list"` restores list semantics that Safari/VoiceOver drops when `list-style: none` is applied; it is one of the few redundant-role cases worth keeping.
- For section-level matches (namespace), `aria-current="page"` is acceptable; `aria-current="location"` or `"true"` are alternatives for "within this section".
- Breadcrumbs: `<nav aria-label="Breadcrumb"><ol>…<li><a aria-current="page" href="…">Invoice INV-104</a></li></ol></nav>`.

### 5.3 Headings

- One `<h1>` per page describing the page (not the site logo).
- Do not skip levels going down (`h2` → `h4`); choose levels by structure, style with CSS classes.
- Partials rendered in different contexts: pass the level in.

```django
{# partials/card.html — usage: {% include "partials/card.html" with title=p.name level=3 only %} #}
{% if level == 2 %}<h2 class="card__title">{{ title }}</h2>{% else %}<h3 class="card__title">{{ title }}</h3>{% endif %}
```

### 5.4 Data table with caption and scope

```django
{# templates/invoices/_invoice_table.html #}
<div class="table-scroll" role="region" aria-labelledby="invoices-caption" tabindex="0">
  <table>
    <caption id="invoices-caption">
      Invoices for {{ tenant.name }}
      {% if page.paginator.count %}<span class="visually-hidden">, {{ page.paginator.count }} total, page {{ page.number }} of {{ page.paginator.num_pages }}</span>{% endif %}
    </caption>
    <thead>
      <tr>
        <th scope="col">Number</th>
        <th scope="col">Client</th>
        <th scope="col" class="num">Amount</th>
        <th scope="col">Due</th>
        <th scope="col">Status</th>
        <th scope="col"><span class="visually-hidden">Actions</span></th>
      </tr>
    </thead>
    <tbody>
      {% for invoice in page %}
      <tr>
        <th scope="row"><a href="{{ invoice.get_absolute_url }}">{{ invoice.number }}</a></th>
        <td>{{ invoice.client.name }}</td>
        <td class="num">{{ invoice.total }}</td>
        <td><time datetime="{{ invoice.due_on|date:'Y-m-d' }}">{{ invoice.due_on|date:"j M Y" }}</time></td>
        <td>{{ invoice.get_status_display }}</td>
        <td>
          <a href="{% url 'invoices:edit' invoice.pk %}">Edit<span class="visually-hidden"> invoice {{ invoice.number }}</span></a>
        </td>
      </tr>
      {% empty %}
      <tr><td colspan="6">No invoices yet. <a href="{% url 'invoices:create' %}">Create the first invoice</a>.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
```

- `<caption>` is the table's accessible name; `scope="col"`/`scope="row"` associate headers with cells.
- The scroll wrapper is focusable (`tabindex="0"`) so keyboard users can scroll it, and named so the stop makes sense.
- Sort controls go *inside* `<th>` as `<a href="?{% querystring sort='due' %}">` (Django ≥ 5.1 `{% querystring %}`) and the active column gets `aria-sort="ascending|descending"` on the `<th>`.
- Complex headers (multi-level) need `id`/`headers`; prefer splitting the table.

```css
.visually-hidden {
  position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0;
}
.num { text-align: end; font-variant-numeric: tabular-nums; }
```

### 5.5 Form with fieldset/legend

```django
{# templates/invoices/invoice_form.html #}
{% extends "base.html" %}
{% block title %}New invoice · Acme Projects{% endblock %}
{% block content %}
<h1>New invoice</h1>
<form method="post" action="{% url 'invoices:create' %}" novalidate>
  {% csrf_token %}
  {% if form.non_field_errors %}
    <div class="form-errors" role="alert">{{ form.non_field_errors }}</div>
  {% endif %}

  <fieldset>
    <legend>Client</legend>
    {{ form.client.as_field_group }}
    {{ form.billing_email.as_field_group }}
  </fieldset>

  <fieldset>
    <legend>Payment terms</legend>
    {# Radio group: legend names the group, each label names one option #}
    {% for radio in form.terms %}
      <div class="radio">
        {{ radio.tag }}
        <label for="{{ radio.id_for_label }}">{{ radio.choice_label }}</label>
      </div>
    {% endfor %}
    {% if form.terms.errors %}<div id="{{ form.terms.auto_id }}_error">{{ form.terms.errors }}</div>{% endif %}
  </fieldset>

  <div class="field">
    <label for="{{ form.due_on.id_for_label }}">Due date</label>
    {{ form.due_on }}
    {{ form.due_on.errors }}
  </div>

  <button type="submit">Create invoice</button>
  <a href="{% url 'invoices:list' %}">Cancel</a>
</form>
{% endblock %}
```

```python
# invoices/forms.py
from django import forms

from .models import Invoice


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["client", "billing_email", "terms", "due_on"]
        widgets = {
            "billing_email": forms.EmailInput(attrs={"autocomplete": "email"}),
            "terms": forms.RadioSelect,
            "due_on": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {"billing_email": "Invoices are emailed here as PDF."}
```

- `as_field_group` (Django ≥ 5.0) renders label, help text, errors and widget; help text is linked via `aria-describedby` automatically (Django ≥ 5.0). Error linkage and `aria-invalid` behaviour depends on version [VERIFY: which Django version adds aria-invalid and error ids to aria-describedby].
- `novalidate` here defers to consistent server-rendered errors; remove it if you prefer browser validation as a first pass. Either way the server validates. Error UX patterns: [django-forms-and-validation.md](django-forms-and-validation.md), [frontend-accessibility.md](frontend-accessibility.md).
- `DateInput` renders `type="text"` by default; set `type="date"` explicitly and use ISO format (`format="%Y-%m-%d"`) so the value round-trips.

Input type cheat sheet:

| Data | Markup |
|---|---|
| Email | `type="email" autocomplete="email"` |
| Phone | `type="tel" autocomplete="tel"` |
| Integer quantity | `type="number" inputmode="numeric" min="1" step="1"` |
| Money | `type="text" inputmode="decimal"` (avoid `number` spinner/locale issues) [VERIFY: locale decimal handling per browser] |
| One-time code | `type="text" inputmode="numeric" autocomplete="one-time-code"` |
| Search | `type="search"` (inside `<search>` or `role="search"` form) |
| URL | `type="url"` |
| Date | `type="date"` (value `YYYY-MM-DD`) |
| New password | `type="password" autocomplete="new-password"` |
| Long text | `<textarea>` |

### 5.6 Dialog (confirm delete)

```django
{# templates/projects/_delete_dialog.html #}
<button type="button" class="btn-danger"
        onclick="document.getElementById('delete-project-{{ project.pk }}').showModal()">
  Delete project
</button>

<dialog id="delete-project-{{ project.pk }}" aria-labelledby="delete-title-{{ project.pk }}">
  <form method="post" action="{% url 'projects:delete' project.pk %}">
    {% csrf_token %}
    <h2 id="delete-title-{{ project.pk }}">Delete “{{ project.name }}”?</h2>
    <p>This removes {{ project.task_count }} tasks. This cannot be undone.</p>
    <div class="dialog-actions">
      <button type="submit" formmethod="dialog" value="cancel" autofocus>Cancel</button>
      <button type="submit" class="btn-danger">Delete project</button>
    </div>
  </form>
</dialog>
```

- `formmethod="dialog"` on Cancel closes without sending a request; the Delete button posts normally.
- `autofocus` on the safe action for destructive confirmations.
- Inline `onclick` is shown for brevity; under a strict CSP use a delegated listener (`data-dialog-open="id"`) — see [frontend-security.md](frontend-security.md) and [javascript-engineering.md](javascript-engineering.md). Declarative invokers (`command`/`commandfor`) may remove the need for JS [VERIFY: Baseline status of command/commandfor].
- HTMX-loaded dialog content and focus handling: [htmx-engineering.md](htmx-engineering.md), [frontend-accessibility.md](frontend-accessibility.md).

```css
dialog::backdrop { background: rgb(0 0 0 / .5); }
dialog { max-width: min(32rem, calc(100vw - 2rem)); border: 0; border-radius: .5rem; padding: 1.5rem; }
```

### 5.7 Details/summary (filters panel, FAQ)

```django
<details class="filters" {% if request.GET %}open{% endif %}>
  <summary>Filter tasks{% if active_filter_count %} ({{ active_filter_count }} active){% endif %}</summary>
  <search>
    <form method="get" action="{% url 'projects:task_list' project.pk %}">
      <label for="f-status">Status</label>
      <select id="f-status" name="status">
        <option value="">Any</option>
        {% for value, label in status_choices %}
          <option value="{{ value }}" {% if request.GET.status == value %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select>
      <button type="submit">Apply filters</button>
    </form>
  </search>
</details>
```

- Do not put interactive elements inside `<summary>` other than its text.
- Do not use `<details>` for primary navigation menus that need `aria-expanded` semantics across complex layouts — it is fine for simple disclosures.
- `<search>` landmark is recent [VERIFY: Baseline status of the search element]; `<form role="search">` is the fallback.

### 5.8 Open Graph and structured data block

```django
{# templates/partials/opengraph.html #}
{% load static %}
<meta property="og:site_name" content="Acme Projects">
<meta property="og:type" content="{{ og_type|default:'website' }}">
<meta property="og:title" content="{{ og_title|default:'Acme Projects' }}">
<meta property="og:description" content="{{ og_description|default:'Plan projects, track tasks and send invoices.' }}">
<meta property="og:url" content="{{ request.build_absolute_uri }}">
<meta property="og:image" content="{{ og_image_url|default:'https://acme.example/static/img/og-default.png' }}">
<meta property="og:image:alt" content="{{ og_image_alt|default:'Acme Projects logo' }}">
<meta name="twitter:card" content="summary_large_image">
```

- `og:image` must be an absolute URL; build it with `request.build_absolute_uri(static(...))` in the view or a context processor.
- Blocks inside an included template cannot be overridden by child pages; pass `og_*` variables from the view (or a context processor) instead.
- Only public pages need OG tags; private pages behind login get `noindex` and generic tags.

JSON-LD for public pages (escape `<`, `>`, `&` exactly as `json_script` does, since `json_script` cannot emit `type="application/ld+json"`):

```python
# core/templatetags/seo.py
import json

from django import template
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()
_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


@register.simple_tag
def json_ld(data):
    payload = json.dumps(data, cls=DjangoJSONEncoder).translate(_ESCAPES)
    return format_html('<script type="application/ld+json">{}</script>', mark_safe(payload))
```

### 5.9 Buttons vs links with HTMX

```django
{# Navigation that works without JS #}
<a href="{% url 'projects:detail' p.pk %}" hx-boost="true">{{ p.name }}</a>

{# State change: button, POST, CSRF via hx-headers on <body> #}
<button type="button" hx-post="{% url 'tasks:complete' task.pk %}" hx-target="closest tr" hx-swap="outerHTML">
  Mark “{{ task.title }}” complete
</button>
```

## 6. Production use cases

| Use case | Key markup |
|---|---|
| Multi-tenant dashboard shell | Landmarks (`header`, `nav aria-label`, `main`, `aside aria-label="Activity"`), skip link, `aria-current` |
| Invoice list | `<table>` with `caption`, `scope`, `aria-sort`, focusable scroll region, row header link |
| Settings page | Multiple `<form>`s each with its own `<h2>` and `<fieldset>` groups |
| Destructive actions | `<dialog>` + POST form + `formmethod="dialog"` cancel |
| Filter sidebar | `<details>` + `<search>`/`role="search"` + GET form |
| Public pricing/marketing pages | Unique titles/descriptions, canonical, OG, JSON-LD, server-rendered content |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Native semantic HTML | Free keyboard/AT support, SEO, works without JS, less code | Some native widgets are hard to style (`select`, date picker) |
| ARIA-enhanced `div`s | Full visual control | Must re-implement keyboard, focus, states; easy to get wrong; extra testing |
| `<dialog>` | Built-in modality and Escape | Styling/animation of open/close needs care; older Safari quirks [VERIFY: minimum Safari version for dialog] |
| `<details>` | Zero JS | Limited animation; not a menu/tab replacement |

## 8. Security and performance considerations

**Security**

- Autoescaping protects element content and quoted attributes; always quote attribute values in templates (`value="{{ v }}"`). Unquoted attributes can be broken out of with spaces.
- Never place user data in `href` without scheme validation (`javascript:` URLs); validate on the server. In `<meta>`/OG tags user text is still escaped by Django, but strip newlines/length-limit for previews.
- JSON in `<script>`: use `json_script` or the escaping helper above; never `{{ data|safe }}`.
- `target="_blank"` links: modern browsers imply `rel="noopener"`; add `rel="noopener noreferrer"` explicitly for external links when you also want to drop the referrer.
- `disabled`/`readonly`/`hidden` inputs are editable by users with DevTools — validate everything server-side. Detail: [frontend-security.md](frontend-security.md).

**Performance**

- Set `width`/`height` (or `aspect-ratio`) on images to prevent layout shift; `loading="lazy"` for below-the-fold images, never for the LCP image.
- Fewer wrapper `div`s = smaller DOM = faster style/layout; very large DOMs (thousands of table rows) need pagination.
- Server-rendered metadata costs nothing at runtime; JS-injected metadata is invisible to preview bots.

## 9. Common mistakes and anti-patterns

1. **`<div onclick>` / `<a href="#">` as buttons.** → `<button type="button">`.
2. **`<button>` without `type` inside a form submits accidentally.** → `type="button"` for non-submit actions.
3. **Links that mutate state (`<a href="/tasks/1/delete/">`).** → POST form with `{% csrf_token %}`.
4. **Placeholder used as label.** → Visible `<label for>`; placeholder only for format hints.
5. **Multiple `<h1>` or headings chosen for size.** → Structural levels; style with classes.
6. **Tables without `<caption>`/`<th scope>`.** → Add them; use row headers for the identifying column.
7. **Radio/checkbox groups without `<fieldset>`/`<legend>`.** → Wrap them.
8. **Icon-only buttons without a name.** → Visible text or `<span class="visually-hidden">`; `aria-label` as last resort; mark the SVG `aria-hidden="true"`.
9. **Custom modal `div` with no focus management.** → `<dialog>` + `showModal()`.
10. **Same `<title>` on every page.** → `{% block title %}` per page, specific first.
11. **`role="button"` on `<button>`, `role="navigation"` on `<nav>`.** → Remove redundant roles (exception: `role="list"` noted above).
12. **Duplicate `id`s from repeated partials** (`id="delete-dialog"` in a loop). → Suffix with the object pk.
13. **OG tags injected by JS.** → Server-render in `<head>`.
14. **`alt` missing or filename as alt.** → Meaningful `alt`; `alt=""` for decorative images.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Screen reader says "clickable" / no role | `div` with click handler | Accessibility tree in DevTools (Elements → Accessibility pane) |
| Form field announced without name | Missing/incorrect `for`/`id` pair | `label.htmlFor === input.id`; duplicate ids |
| Unexpected form submit | `<button>` default type | Inspect `type` attribute |
| Table content appears outside the table | Invalid nesting / foster parenting | View DOM vs template source; HTML validator |
| HTMX row swap renders as text | Fragment parsed outside table context | Target is `<tbody>`/`<tr>`; response contains only rows |
| Link preview wrong/empty | OG tags missing, relative image URL, JS-injected | `curl` the page and inspect `<head>`; platform preview debuggers |
| Duplicate content in search | Canonical includes filter query strings | Rendered `<link rel=canonical>` |
| Dialog not closing on Escape | Opened with `show()` not `showModal()` | Call site |

Validation steps:

- Run the Nu HTML Checker (validator.w3.org/nu) on rendered pages (not templates).
- DevTools Accessibility pane / full-page accessibility tree: every interactive element has a role and name.
- Heading outline: list all headings (browser extension or `document.querySelectorAll('h1,h2,h3,h4,h5,h6')`) and confirm order.
- Tab through the page once; then run axe (see [frontend-testing.md](frontend-testing.md)).
- Django test asserting structure:

```python
def test_invoice_table_has_caption_and_headers(client, invoice_factory, user):
    invoice_factory.create_batch(3, tenant=user.tenant)
    client.force_login(user)
    html = client.get("/invoices/").content.decode()
    assert "<caption" in html
    assert 'scope="col"' in html
    assert 'aria-current="page"' in html
```

## 11. Related references and skills

- [frontend-accessibility.md](frontend-accessibility.md) — WCAG, focus, ARIA decisions, testing
- [django-template-engineering.md](django-template-engineering.md) — layouts, includes, partials, inclusion tags
- [django-forms-and-validation.md](django-forms-and-validation.md) — form rendering, errors, widgets
- [css-engineering.md](css-engineering.md), [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md)
- [htmx-engineering.md](htmx-engineering.md), [frontend-security.md](frontend-security.md), [frontend-testing.md](frontend-testing.md)
- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — SEO, rendering, progressive enhancement
- [forms-validation.md](../../django-core/references/forms-validation.md) — Form/ModelForm mechanics
- [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md), [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md)

## 12. Official documentation and external references

- WHATWG HTML Living Standard: https://html.spec.whatwg.org/multipage/
- WHATWG — the dialog element: https://html.spec.whatwg.org/multipage/interactive-elements.html#the-dialog-element
- WHATWG — autofill tokens: https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#autofill
- MDN — HTML elements reference: https://developer.mozilla.org/en-US/docs/Web/HTML/Element
- MDN — `<dialog>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog
- MDN — `<details>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/details
- MDN — `<table>` accessibility: https://developer.mozilla.org/en-US/docs/Learn/HTML/Tables/Advanced
- MDN — `aria-current`: https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-current
- W3C HTML-AAM: https://www.w3.org/TR/html-aam-1.0/
- W3C WAI — Tables tutorial: https://www.w3.org/WAI/tutorials/tables/
- W3C WAI — Forms tutorial: https://www.w3.org/WAI/tutorials/forms/
- W3C WAI — Page structure tutorial: https://www.w3.org/WAI/tutorials/page-structure/
- Django — form rendering (`as_field_group`): https://docs.djangoproject.com/en/5.2/topics/forms/#reusable-field-group-templates
- Django — built-in template tags (`querystring`, `json_script`): https://docs.djangoproject.com/en/5.2/ref/templates/builtins/
- Django — sitemaps: https://docs.djangoproject.com/en/5.2/ref/contrib/sitemaps/
- web.dev — Learn HTML: https://web.dev/learn/html
- The Open Graph protocol: https://ogp.me/
