# Frontend Accessibility

> **Scope:** Building and verifying accessible Django UIs against WCAG 2.2 AA: keyboard operation, focus management (including after HTMX swaps and in dialogs), screen-reader semantics, when to use ARIA, accessible forms and error summaries, contrast, motion, navigation, tables, live regions and accessibility testing. Element-level markup choices are in [html-and-semantic-ui.md](html-and-semantic-ui.md); form rendering mechanics in [django-forms-and-validation.md](django-forms-and-validation.md); test-suite wiring in [frontend-testing.md](frontend-testing.md).

## 1. Purpose and scope

Use this reference when an agent designs, implements, reviews or tests any UI that users operate — pages, HTMX partials, Alpine widgets, React islands. Accessibility is a correctness requirement (and frequently a legal one), not a polish step: a form that cannot be submitted by keyboard is broken.

Target: **WCAG 2.2 Level AA** (W3C Recommendation, October 2023) unless the project states otherwise.

## 2. Core concepts

### 2.1 WCAG structure

- Principles **POUR**: Perceivable, Operable, Understandable, Robust.
- Levels: **A** (minimum), **AA** (usual legal/contractual target), **AAA** (not required wholesale).
- 4.1.1 Parsing is obsolete in WCAG 2.2.

Success criteria agents hit most often:

| SC | Level | What it means in practice |
|---|---|---|
| 1.1.1 Non-text Content | A | `alt` on images; names for icon buttons |
| 1.3.1 Info and Relationships | A | Structure in markup: headings, labels, table headers, fieldsets |
| 1.3.5 Identify Input Purpose | AA | `autocomplete` tokens on personal-data fields |
| 1.4.1 Use of Color | A | Errors/status not by colour alone |
| 1.4.3 Contrast (Minimum) | AA | Text 4.5:1; large text (≥ 24 px, or ≥ 18.66 px bold) 3:1 |
| 1.4.10 Reflow | AA | Usable at 320 CSS px width without 2-D scrolling (data tables excepted) |
| 1.4.11 Non-text Contrast | AA | UI component boundaries, focus indicators, icons 3:1 |
| 1.4.13 Content on Hover or Focus | AA | Tooltips dismissible, hoverable, persistent |
| 2.1.1 Keyboard / 2.1.2 No Keyboard Trap | A | Everything operable by keyboard; you can always Tab out |
| 2.3.3 Animation from Interactions | AAA | Respect reduced motion (do it anyway) |
| 2.4.1 Bypass Blocks | A | Skip link / landmarks |
| 2.4.2 Page Titled | A | Unique, descriptive `<title>` |
| 2.4.3 Focus Order | A | Order matches meaning |
| 2.4.7 Focus Visible | AA | Visible focus indicator |
| 2.4.11 Focus Not Obscured (Minimum) | AA, new in 2.2 | Sticky headers/cookie banners must not fully hide the focused element |
| 2.5.7 Dragging Movements | AA, new in 2.2 | Single-pointer alternative to drag (e.g. "Move up/down" buttons) |
| 2.5.8 Target Size (Minimum) | AA, new in 2.2 | Targets ≥ 24×24 CSS px (or spacing exception) |
| 3.3.1 Error Identification / 3.3.3 Error Suggestion | A / AA | Errors described in text with how to fix |
| 3.3.2 Labels or Instructions | A | Visible labels, required indicators, formats |
| 3.3.7 Redundant Entry | A, new in 2.2 | Don't make users re-enter info in the same process |
| 3.3.8 Accessible Authentication (Minimum) | AA, new in 2.2 | No cognitive test to log in; allow paste and password managers |
| 4.1.2 Name, Role, Value | A | Custom widgets expose name/role/state |
| 4.1.3 Status Messages | AA | Status updates announced without moving focus (live regions) |

### 2.2 Assistive technology and the accessibility tree

Screen readers (NVDA, JAWS, VoiceOver, TalkBack, Narrator), voice control, switch access and magnifiers consume the **accessibility tree** — role, name, description, state, value per node — that the browser derives from DOM + ARIA + CSS. If it is wrong in the tree, it is wrong for those users, regardless of how it looks.

### 2.3 Accessible name computation (simplified order)

1. `aria-labelledby` (references visible text — preferred when ARIA is needed)
2. `aria-label`
3. Native source: `<label>`, `alt`, `<caption>`, `<legend>`, `<figcaption>`, `title` for some elements
4. Content (for roles that allow name-from-content: button, link, heading, cell…)
5. `title` / `placeholder` as last-resort fallbacks (do not rely on them)

`aria-describedby` supplies the *description* (help text, error), read after the name.

### 2.4 The rules of ARIA use (W3C)

1. If a native element provides the semantics/behaviour, use it.
2. Don't change native semantics (`<h2 role="tab">`) unless you must.
3. All interactive ARIA controls must be keyboard operable.
4. Don't use `role="presentation"`/`aria-hidden="true"` on focusable elements.
5. All interactive elements need an accessible name.

ARIA changes only the tree — it adds no behaviour, focusability or keyboard handling.

## 3. Internal mechanics

### 3.1 Focus

- **Sequential focus order** = DOM order of focusable elements (`a[href]`, `button`, form controls, `[tabindex="0"]`, `summary`, `iframe`, elements with `contenteditable`). CSS reordering (`order`, grid placement, `flex-direction: row-reverse`) does **not** change it — visual and focus order can diverge (2.4.3).
- `tabindex="0"`: add to tab order (only for custom widgets/scroll regions). `tabindex="-1"`: focusable by script only (headings, `main`, error summary as focus targets). Positive `tabindex`: never.
- `:focus-visible` matches when the browser heuristics decide focus should be shown (keyboard focus; text inputs on any focus). Use it to style rings without showing them on mouse clicks for buttons.
- When the focused element is removed from the DOM (e.g. by an HTMX `outerHTML` swap), focus falls back to `<body>`; screen-reader users lose their place and the next Tab starts from the top.
- `inert` attribute removes a subtree from focus order and the accessibility tree (Baseline 2023 [VERIFY: inert Baseline date]).

### 3.2 Modal dialogs

`dialog.showModal()` makes everything outside the dialog inert, places it in the top layer, moves focus into it and closes on Escape (`cancel` event). On close, browsers restore focus to the previously focused element [VERIFY: consistency of focus restoration across engines]. A `div`-based modal must re-implement all of this (focus trap, `aria-modal`, background `inert`, Escape, focus return) — reason enough to use `<dialog>`.

### 3.3 Live regions

- A live region (`aria-live="polite"|"assertive"`, `role="status"` = polite, `role="alert"` = assertive) is **observed for changes after it exists**. Content inserted together with the region is often not announced. Render an empty region in the base layout; change its text later.
- `aria-atomic="true"` reads the whole region rather than only the changed node.
- Assertive interrupts the user — reserve for errors that block progress.
- Repeated identical text may not re-announce; clear then set (with a short delay) when repeating.

### 3.4 HTMX swaps and the accessibility tree

- HTMX replaces DOM nodes; the accessibility tree updates, but nothing is announced unless the swapped content sits in a live region or focus moves into it.
- During a swap HTMX attempts to preserve focus and selection if the focused element has an `id` that also exists in the new content [VERIFY: HTMX 2.x focus-preservation rules]. Give inputs stable ids in partials.
- `hx-swap` modifiers `focus-scroll:true|false` control scrolling to a focused element; `show:`/`scroll:` control viewport scrolling — neither moves focus for you.
- Events to hook: `htmx:afterSwap` (new content in DOM), `htmx:afterSettle` (attributes settled; safest point to move focus), `htmx:responseError`/`htmx:sendError` (announce failures).
- HTMX 2.x does not swap 4xx/5xx by default; validation-error responses need a deliberate strategy (return 200 with the re-rendered form, or configure `htmx.config.responseHandling` [VERIFY: exact config shape]) — see [htmx-engineering.md](htmx-engineering.md).

### 3.5 Reduced motion and other user preferences

Media features `prefers-reduced-motion: reduce`, `prefers-contrast: more`, `forced-colors: active` (Windows High Contrast), `prefers-color-scheme`. In forced-colors mode the browser replaces colours; `box-shadow` focus rings disappear — use `outline` (with `outline-color: transparent` fallback trick if needed) so a ring remains.

## 4. Engineering decisions and trade-offs

### 4.1 Native HTML first, ARIA only when…

| Need | Native first | ARIA only when… |
|---|---|---|
| Clickable action | `<button>` | Never wrap `div` with `role="button"` unless you cannot change the element (third-party markup) — then add `tabindex="0"`, Enter/Space handlers |
| Navigation link | `<a href>` | — |
| Current page in nav | — (no native) | `aria-current="page"` always |
| Expand/collapse | `<details>/<summary>` | Toggle button controls a separate region: `<button aria-expanded aria-controls>` |
| Modal | `<dialog>` + `showModal()` | Legacy support needed: `role="dialog" aria-modal="true"` + focus trap (Alpine Focus `x-trap`) + `inert` background |
| Label a control | `<label for>` | No visible label possible (icon button, search with visible button): `aria-label`; reuse visible text with `aria-labelledby` |
| Help/error text | Django ≥ 5.0 `as_field_group` (help text `aria-describedby`) | Add error ids to `aria-describedby`, `aria-invalid="true"` when the widget does not |
| Required | `required` attribute | `aria-required="true"` only on custom widgets |
| Status message | `<output>` (for calculation results) | `role="status"` / `aria-live="polite"` region for async results |
| Error alert | — | `role="alert"` for blocking errors appearing without page load |
| Tabs | Separate pages/links, or headings + content | Real tabs: `role="tablist"/"tab"/"tabpanel"`, `aria-selected`, arrow-key roving tabindex (APG pattern) |
| Combobox/autocomplete | `<input list>` + `<datalist>` for simple cases | Async suggestions: APG combobox pattern, fully keyboard-tested |
| Hide decorative icon | `alt=""` on `<img>` | Inline SVG: `aria-hidden="true" focusable="false"` |
| Landmark | `<nav>`, `<main>`, `<aside>`, `<header>`, `<footer>`, `<search>` | Multiple same-type landmarks need distinct `aria-label`s |
| Sorting state | — | `aria-sort` on the sorted `<th>` |

### 4.2 Focus strategy after a dynamic update

| Update | Prefer moving focus to… | Prefer announcing (live region) when… |
|---|---|---|
| HTMX page-like navigation (`hx-boost`, tab change replacing `main`) | New `<h1>` (`tabindex="-1"`) and update `<title>` | — |
| Form submit with errors | Error summary (or first invalid field for single-field forms) | — |
| Inline edit saved (row swapped back to read mode) | The row's "Edit" button (same place the user was) | Also announce "Saved" via `role="status"` |
| Item deleted | Next item's action, or the list heading if empty | Announce "Invoice INV-104 deleted" |
| Dialog opened | First field / safe action inside dialog | — |
| Dialog closed | Invoking control (or its replacement after swap) | — |
| Background refresh (polling counters) | Do **not** move focus | Only if the change matters; otherwise silent |
| Search results updated while typing | Keep focus in the input | Announce result count ("12 tasks found"), debounced |

### 4.3 Validation timing

| Prefer validate on submit (server) when… | Prefer inline (on blur/change) when… |
|---|---|
| Default for Django forms; consistent; no premature errors | Format-heavy fields where early feedback saves effort (username availability, password rules) |
| Screen-reader users get one summary | Errors appear on blur, not on every keystroke, and are linked via `aria-describedby` |

Client-side checks are UX; the server always validates.

## 5. Django/frontend implementation patterns

### 5.1 Skip link, landmarks and global live regions (base layout)

```django
{# templates/base.html (excerpt) #}
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <header>
    {% include "partials/primary_nav.html" %}
  </header>
  <main id="main" tabindex="-1">
    {% if messages %}
      <div class="messages" role="status">
        {% for message in messages %}<p class="message message--{{ message.tags }}">{{ message }}</p>{% endfor %}
      </div>
    {% endif %}
    {% block content %}{% endblock %}
  </main>
  {# Empty at load; JS/HTMX writes into them later #}
  <div id="sr-status" class="visually-hidden" role="status" aria-atomic="true"></div>
  <div id="sr-alert" class="visually-hidden" role="alert" aria-atomic="true"></div>
</body>
```

```css
.skip-link {
  position: absolute; inset-inline-start: 1rem; top: -100vh;
  padding: .5rem 1rem; background: var(--color-surface); color: var(--color-text);
  z-index: 1000;
}
.skip-link:focus { top: 1rem; }

main:focus { outline: none; }          /* programmatic focus target, not interactive */

:focus-visible {
  outline: 3px solid var(--color-focus);   /* ≥ 3:1 against adjacent colours (1.4.11) */
  outline-offset: 2px;
}

html { scroll-padding-top: 5rem; }       /* sticky header height: keeps focus visible (2.4.11) */

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

Django `messages` rendered on a full page load inside `role="status"` may not be announced (region and content arrive together); they are still read in normal reading order because they sit at the top of `main`. For HTMX requests, push messages into `#sr-status` instead (5.4).

### 5.2 Accessible form with error summary

```python
# invoices/views.py
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import InvoiceForm


@login_required
def invoice_create(request):
    form = InvoiceForm(request.POST or None, tenant=request.tenant)
    if request.method == "POST" and form.is_valid():
        invoice = form.save()
        messages.success(request, f"Invoice {invoice.number} created.")
        return redirect(invoice)
    status = 422 if form.errors else 200
    return render(request, "invoices/invoice_form.html", {"form": form}, status=status)
```

(The skill-wide convention is 422 for invalid submissions. HTMX 2.x does not swap 4xx by default, so the project configures `htmx.config.responseHandling` to swap 422 — see [htmx-engineering.md](htmx-engineering.md). If that config is absent, return 200 for HTMX requests instead.)

```django
{# templates/partials/error_summary.html #}
{% if form.errors %}
<div id="error-summary" class="error-summary" tabindex="-1" aria-labelledby="error-summary-title">
  <h2 id="error-summary-title">There {{ form.errors|length|pluralize:"is a problem,are problems" }} with this form</h2>
  <ul>
    {% for error in form.non_field_errors %}<li>{{ error }}</li>{% endfor %}
    {% for field in form %}
      {% for error in field.errors %}
        <li><a href="#{{ field.auto_id }}">{{ field.label }}: {{ error }}</a></li>
      {% endfor %}
    {% endfor %}
  </ul>
</div>
{% endif %}
```

```django
{# templates/invoices/invoice_form.html #}
{% extends "base.html" %}
{% block title %}{% if form.errors %}Error: {% endif %}New invoice · Acme Projects{% endblock %}
{% block content %}
<h1>New invoice</h1>
{% include "partials/error_summary.html" %}
<form method="post" novalidate>
  {% csrf_token %}
  {{ form.billing_email.as_field_group }}
  <div class="field">
    <label for="{{ form.amount.id_for_label }}">Amount (EUR) <span aria-hidden="true">*</span></label>
    <input type="text" inputmode="decimal" name="{{ form.amount.html_name }}"
           id="{{ form.amount.id_for_label }}" value="{{ form.amount.value|default_if_none:'' }}"
           required
           {% if form.amount.errors %}aria-invalid="true" aria-describedby="{{ form.amount.auto_id }}_error"{% endif %}>
    {% if form.amount.errors %}
      <p id="{{ form.amount.auto_id }}_error" class="field-error">
        <span class="visually-hidden">Error:</span> {{ form.amount.errors|join:" " }}
      </p>
    {% endif %}
  </div>
  <button type="submit">Create invoice</button>
</form>
{% endblock %}
```

```js
// static/js/a11y.js — focus the summary after a failed full-page submit
const summary = document.getElementById("error-summary");
if (summary) summary.focus();
```

- "Error:" prefix in `<title>` tells screen-reader users immediately (they hear the title on load).
- Error text is in words, adjacent to the field, linked by `aria-describedby`, and not colour-only (icon + text).
- For radio/checkbox groups link the summary to the first input (`{{ field.auto_id }}_0`) or the fieldset id [VERIFY: id_for_label/auto_id output for RadioSelect in Django 5.2].
- `as_field_group` (Django ≥ 5.0) wires help text; check rendered HTML for error linkage on your version before hand-rolling.

### 5.3 Focus management after HTMX swaps

Convention: the server marks where focus should go; one small listener applies it.

```django
{# templates/tasks/_task_row.html — returned after inline save #}
<tr id="task-{{ task.pk }}">
  <th scope="row">{{ task.title }}</th>
  <td>{{ task.get_status_display }}</td>
  <td>
    <button type="button" {% if focus_edit %}data-focus-after-swap{% endif %}
            hx-get="{% url 'tasks:edit_row' task.pk %}" hx-target="closest tr" hx-swap="outerHTML">
      Edit<span class="visually-hidden"> {{ task.title }}</span>
    </button>
  </td>
</tr>
```

```django
{# templates/tasks/_task_row_form.html — edit mode #}
<tr id="task-{{ task.pk }}">
  <td colspan="3">
    <form hx-post="{% url 'tasks:edit_row' task.pk %}" hx-target="closest tr" hx-swap="outerHTML">
      {% csrf_token %}
      <label for="title-{{ task.pk }}">Title</label>
      <input id="title-{{ task.pk }}" name="title" value="{{ form.title.value|default_if_none:'' }}"
             data-focus-after-swap
             {% if form.title.errors %}aria-invalid="true" aria-describedby="title-{{ task.pk }}-error"{% endif %}>
      {% if form.title.errors %}<p id="title-{{ task.pk }}-error">{{ form.title.errors|join:" " }}</p>{% endif %}
      <button type="submit">Save</button>
      {# tasks:row view passes focus_edit=True when ?focus=1, so Cancel also returns focus to Edit #}
      <button type="button" hx-get="{% url 'tasks:row' task.pk %}?focus=1" hx-target="closest tr" hx-swap="outerHTML">Cancel</button>
    </form>
  </td>
</tr>
```

```js
// static/js/a11y.js
// Only server responses emit the marker (the full list page never does), so a document query is safe.
document.body.addEventListener("htmx:afterSettle", () => {
  const el = document.querySelector("[data-focus-after-swap]");
  if (el) {
    el.focus({ preventScroll: false });
    el.removeAttribute("data-focus-after-swap");
  }
});

// Announcements requested by the server via HX-Trigger: {"announce": "Task saved"}
document.body.addEventListener("announce", (event) => {
  const region = document.getElementById("sr-status");
  region.textContent = "";
  setTimeout(() => { region.textContent = event.detail.value; }, 50);
});

document.body.addEventListener("htmx:responseError", () => {
  document.getElementById("sr-alert").textContent = "Something went wrong. Your change was not saved.";
});
```

```python
# tasks/views.py
import json

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .forms import TaskTitleForm
from .models import Task


@login_required
def edit_row(request, pk):
    task = get_object_or_404(Task, pk=pk, project__tenant=request.tenant)  # authz on every fragment
    form = TaskTitleForm(request.POST or None, instance=task)
    if request.method == "POST" and form.is_valid():
        form.save()
        response = render(request, "tasks/_task_row.html", {"task": task, "focus_edit": True})
        response["HX-Trigger"] = json.dumps({"announce": f"Task “{task.title}” saved"})
        return response
    return render(request, "tasks/_task_row_form.html", {"task": task, "form": form})
```

Behaviour: Edit → focus lands in the title input; Save → focus returns to the row's Edit button and "Task saved" is announced; validation error → focus stays on the input, which is described by its error. The `event.detail.value` shape for `HX-Trigger` JSON payloads should be confirmed [VERIFY: HTMX 2.x event.detail for HX-Trigger object values]. django-htmx (third-party) offers `trigger_client_event` to set this header [VERIFY: helper name/signature].

For `hx-boost` or swaps that replace `main`: focus the new `<h1 tabindex="-1">` and update `document.title` (HTMX updates the title from a `<title>` in the response).

### 5.4 Dialog loaded by HTMX

```django
<button type="button" hx-get="{% url 'invoices:send_dialog' invoice.pk %}"
        hx-target="#dialog-host" hx-swap="innerHTML">
  Send invoice
</button>
<div id="dialog-host"></div>
```

```django
{# templates/invoices/_send_dialog.html #}
<dialog id="send-dialog" aria-labelledby="send-title" data-open-on-load>
  <form hx-post="{% url 'invoices:send' invoice.pk %}" hx-target="#dialog-host">
    {% csrf_token %}
    <h2 id="send-title">Send invoice {{ invoice.number }}</h2>
    {{ form.recipient.as_field_group }}
    <button type="submit">Send</button>
    <button type="submit" formmethod="dialog" formnovalidate>Cancel</button>
  </form>
</dialog>
```

```js
let dialogOpener = null;
document.body.addEventListener("htmx:beforeRequest", (e) => {
  if (e.detail.target?.id === "dialog-host" && !e.detail.elt.closest("dialog")) {
    dialogOpener = e.detail.elt;                   // remember invoking control
  }
});
document.body.addEventListener("htmx:afterSettle", () => {
  const dlg = document.querySelector("dialog[data-open-on-load]:not([open])");
  if (dlg) {
    dlg.addEventListener("close", () => { dialogOpener?.focus(); dlg.remove(); }, { once: true });
    dlg.showModal();
  }
});
```

On success the server returns an empty body plus `HX-Trigger` (announce) and the client closes the dialog, or returns a re-rendered row with `hx-swap-oob`. Alpine alternative for non-native overlays: Focus plugin `x-trap` (see [alpine-js-engineering.md](alpine-js-engineering.md)) — still prefer `<dialog>`.

### 5.5 Disclosure navigation (mobile menu)

```django
<nav aria-label="Primary">
  <button type="button" class="nav-toggle" aria-expanded="false" aria-controls="primary-menu">
    Menu
  </button>
  <ul id="primary-menu" role="list" hidden>
    ...
  </ul>
</nav>
```

```js
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-toggle");
  if (!btn) return;
  const menu = document.getElementById(btn.getAttribute("aria-controls"));
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", String(!open));
  menu.hidden = open;
});
```

Do not use `role="menu"`/`menuitem` for site navigation — that is an application-menu pattern with arrow-key expectations; a disclosure with links is correct.

### 5.6 Accessible tables (summary)

`<caption>`, `<th scope="col|row">`, `aria-sort` on the sorted header, sort controls as links/buttons inside `<th>`, focusable named scroll wrapper for wide tables, row actions with unique names ("Edit invoice INV-104"). Markup: [html-and-semantic-ui.md](html-and-semantic-ui.md) §5.4. Announce result counts after HTMX filtering via `#sr-status`.

### 5.7 Reduced motion in JS and Alpine

```js
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
function scrollToEl(el) {
  el.scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
}
```

Avoid auto-playing carousels; if unavoidable provide pause (2.2.2). For HTMX `transition:true` (View Transitions) also honour reduced motion in CSS.

### 5.8 Drag alternatives (2.5.7)

Kanban/task ordering with drag-and-drop must offer buttons: `<button hx-post="{% url 'tasks:move' task.pk %}" hx-vals='{"direction": "up"}'>Move “{{ task.title }}” up</button>`, plus an announcement of the new position.

## 6. Production use cases

| Use case | Key techniques |
|---|---|
| Multi-tenant dashboard | Skip link, labelled landmarks, `aria-current`, focus-visible rings, 24 px targets on icon buttons |
| Invoice form | Error summary + `Error:` title prefix, `aria-invalid`/`aria-describedby`, `autocomplete`, fieldsets |
| Inline-edit task table (HTMX) | Focus to input on edit, back to Edit button on save, `role="status"` announcement |
| Live search | Focus stays in input, debounced result-count announcement |
| Confirm/send dialogs | `<dialog>` + `showModal()`, focus return to opener |
| Kanban board | Keyboard/button alternative to drag, position announcements |
| Notifications/toasts | Polite live region, persistent long enough or dismissible, never the only place a critical error appears |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Native HTML + server rendering | Most a11y for free; robust; easy to test | Some widgets limited in styling |
| HTMX partial updates | Small, server-owned markup; no client framework | Must add focus/announcement conventions explicitly |
| Alpine widgets | Local behaviour close to markup; Focus plugin helps | Easy to forget keyboard/ARIA state sync (`aria-expanded`) |
| React islands / component libraries | Mature accessible primitives exist (headless libraries) | Heavier; library a11y quality varies; must still be tested |
| Automated testing (axe) | Fast, CI-friendly, catches regressions | Detects only a subset of issues; no substitute for keyboard/SR passes |

## 8. Security and performance considerations

**Security**

- `aria-hidden`, `hidden`, `disabled` and visually hidden elements are presentation only; never hide privileged actions client-side instead of authorising them server-side.
- Announcement text from `HX-Trigger` and `messages` must be escaped: set `textContent`, never `innerHTML`. JSON in headers is data, not markup.
- 3.3.8 Accessible Authentication: allow paste into password/OTP fields and password-manager autofill (`autocomplete="current-password"`, `one-time-code`); don't block paste "for security". CAPTCHA needs an accessible alternative. Backend auth hardening: [authentication-security.md](../../django-security-engineering/references/authentication-security.md).
- Session timeouts: warn and allow extension (2.2.1 Timing Adjustable) without weakening server-side expiry.

**Performance**

- Fast, stable pages are accessible pages: layout shifts (CLS) move targets under pointers and magnifier users; long tasks delay keyboard response (INP).
- Live regions and `MutationObserver`-heavy a11y scripts are cheap; avoid rewriting large regions just to trigger announcements — write a short sentence to a dedicated region.
- Respect `prefers-reduced-motion` to also cut animation work.

## 9. Common mistakes and anti-patterns

1. **`outline: none` with no replacement.** → `:focus-visible` outline ≥ 3:1 contrast, 2 px+.
2. **`div`/`span` click targets.** → `<button>`/`<a href>`.
3. **ARIA on native elements** (`<button role="button">`, `<nav role="navigation">`, `aria-label` duplicating visible text). → Remove; ARIA only per §4.1.
4. **`aria-label` that differs from visible text** (breaks voice control, 2.5.3 Label in Name). → Visible text must be contained in the name.
5. **Focus lost to `<body>` after HTMX `outerHTML` swap.** → `data-focus-after-swap` convention (5.3) or stable ids.
6. **Live region injected with its content.** → Region exists at page load; change its text later.
7. **Everything `role="alert"`/`aria-live="assertive"`.** → Polite `role="status"` by default.
8. **Errors shown only in red / only on top.** → Summary + inline text + `aria-describedby` + icon.
9. **Placeholder-only labels; `title` tooltips as labels.** → Visible `<label>`.
10. **`tabindex="1"`+.** → DOM order; `0`/`-1` only.
11. **Custom modal without focus trap/return.** → `<dialog>` + `showModal()`; restore focus to opener.
12. **`role="menu"` for site nav.** → Disclosure button + list of links.
13. **Sticky header hiding focused elements.** → `scroll-padding-top`.
14. **Tiny icon buttons (16 px).** → ≥ 24×24 CSS px hit area (padding counts).
15. **Disabling submit until the form is valid.** → Keep enabled; show errors on submit (disabled buttons are unfocusable and unexplained).
16. **Testing only with axe.** → Add keyboard pass and screen-reader smoke test.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Tab skips a control | Not focusable (`div`), `tabindex="-1"`, inside `inert`/`hidden` | Accessibility pane; `document.activeElement` while tabbing |
| Tab order jumps around | CSS reordering or positive tabindex | Compare DOM order to visual order |
| Focus disappears after HTMX action | Focused node replaced | `document.activeElement === document.body` after swap |
| No focus ring visible | `outline: none`, low-contrast ring, ring hidden by `overflow:hidden` | Computed styles on `:focus-visible` |
| SR doesn't announce status | Region created with content, or `display:none` region | Region present at load? Uses `visually-hidden`, not `display:none` |
| SR reads "button" with no name | Icon-only button | Name in Accessibility pane |
| Field error not read | Missing `aria-describedby`/wrong id | ids match; `aria-invalid` present |
| Background still reachable in modal | `show()` instead of `showModal()`, or div modal | Try Tab/Shift+Tab and VO/NVDA virtual cursor |
| Windows High Contrast loses focus ring | `box-shadow` ring | Emulate `forced-colors: active` in DevTools Rendering |
| Content cut off at 400% zoom | Fixed widths/heights | Zoom to 400% / 320 CSS px viewport |

### Validation steps

**1. Automated (CI, every PR)**

```js
// tests/a11y.spec.js — @playwright/test + @axe-core/playwright
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const path of ["/", "/projects/", "/invoices/", "/invoices/new/"]) {
  test(`no detectable a11y violations on ${path}`, async ({ page }) => {
    await page.goto(path);
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(results.violations).toEqual([]);
  });
}
```

Python projects: pytest-playwright plus a third-party axe wrapper such as axe-playwright-python [VERIFY: package name and API]. Also scan states, not just pages: open the dialog, trigger validation errors, then analyse.

```python
# tests/e2e/test_task_inline_edit.py — pytest-playwright, keyboard + focus assertions
from playwright.sync_api import Page, expect


def test_inline_edit_keeps_focus(page: Page, live_server, logged_in):
    page.goto(f"{live_server.url}/projects/1/tasks/")
    page.get_by_role("button", name="Edit Write spec").click()
    title = page.get_by_role("textbox", name="Title")
    expect(title).to_be_focused()
    title.fill("Write API spec")
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Edit Write API spec")).to_be_focused()
    expect(page.get_by_role("status")).to_have_text("Task “Write API spec” saved")
```

**2. Keyboard pass (every new UI, ~5 minutes)**

- Unplug the mouse. Tab from the address bar: first stop is the skip link; it works.
- Every interactive element reachable, in logical order, with a visible ring never hidden by sticky UI.
- Enter/Space activate buttons; Enter follows links; Escape closes dialogs/menus; arrow keys within composite widgets only.
- After each HTMX action, focus is somewhere sensible (not `<body>`).
- No traps: you can always Tab out (except inside a modal, where Escape exits).

**3. Screen-reader smoke test (per feature, before release)**

| Pairing | Quick checks |
|---|---|
| NVDA + Firefox or Chrome (Windows) | `H` through headings, `D` landmarks, `Insert+F7` elements list, `F` form fields; submit with errors and listen for summary |
| VoiceOver + Safari (macOS) | `Ctrl+Option+U` rotor (headings, landmarks, form controls); `Ctrl+Option+Space` activate |
| VoiceOver + Safari (iOS) / TalkBack + Chrome (Android) | Swipe order, touch targets, dialog containment |

Listen for: page title, correct roles and names, states ("expanded", "current page", "invalid entry"), status announcements after HTMX actions, and no reading of background content while a modal is open.

**4. Visual checks** — contrast with DevTools picker or a contrast checker (4.5:1 / 3:1); 200% text zoom and 400% page zoom (reflow at 320 CSS px); `prefers-reduced-motion` and `forced-colors` emulation in DevTools Rendering panel.

## 11. Related references and skills

- [html-and-semantic-ui.md](html-and-semantic-ui.md) — element choice, tables, forms, dialog markup
- [django-forms-and-validation.md](django-forms-and-validation.md) — form rendering, error display, widgets
- [htmx-engineering.md](htmx-engineering.md) — swaps, events, response handling, OOB
- [alpine-js-engineering.md](alpine-js-engineering.md) — Focus plugin, `x-trap`, widget state
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — toasts, loading/empty/error states, modals
- [css-engineering.md](css-engineering.md), [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — focus styles, tokens, contrast, reflow
- [frontend-testing.md](frontend-testing.md) — wiring axe and Playwright into CI
- [browser-and-end-to-end-testing.md](../../django-testing/references/browser-and-end-to-end-testing.md)
- [authentication-security.md](../../django-security-engineering/references/authentication-security.md)

## 12. Official documentation and external references

- WCAG 2.2: https://www.w3.org/TR/WCAG22/
- What's new in WCAG 2.2: https://www.w3.org/WAI/standards-guidelines/wcag/new-in-22/
- Understanding WCAG 2.2: https://www.w3.org/WAI/WCAG22/Understanding/
- WAI-ARIA 1.2: https://www.w3.org/TR/wai-aria-1.2/
- Using ARIA (rules of ARIA use): https://www.w3.org/TR/using-aria/
- ARIA Authoring Practices Guide (patterns): https://www.w3.org/WAI/ARIA/apg/
- Accessible Name and Description Computation: https://www.w3.org/TR/accname-1.2/
- W3C WAI — Forms tutorial (notifications/validation): https://www.w3.org/WAI/tutorials/forms/notifications/
- W3C WAI — Easy checks: https://www.w3.org/WAI/test-evaluate/preliminary/
- MDN — ARIA live regions: https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/ARIA_Live_Regions
- MDN — `:focus-visible`: https://developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible
- MDN — `prefers-reduced-motion`: https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion
- MDN — `inert`: https://developer.mozilla.org/en-US/docs/Web/HTML/Global_attributes/inert
- WHATWG — dialog element: https://html.spec.whatwg.org/multipage/interactive-elements.html#the-dialog-element
- web.dev — Learn Accessibility: https://web.dev/learn/accessibility
- Django — accessibility in forms (`as_field_group`, help text): https://docs.djangoproject.com/en/5.2/topics/forms/
- htmx — events reference: https://htmx.org/events/ · response headers: https://htmx.org/reference/#response_headers
- alpinejs.dev — Focus plugin: https://alpinejs.dev/plugins/focus
- Playwright — accessibility testing: https://playwright.dev/docs/accessibility-testing
