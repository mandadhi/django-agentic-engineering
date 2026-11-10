# Frontend State and Interaction Design

> **Scope:** Where UI state lives in a Django frontend (DOM, Alpine, server, URL, storage), how it stays consistent, and the interaction patterns built on it: URL-driven search/filter/pagination, optimistic UI, loading/empty/error states, confirmations, toasts, modals, multi-step flows, race conditions, cancellation and double-submit prevention. Directive/attribute mechanics live in [htmx-engineering.md](htmx-engineering.md) and [alpine-js-engineering.md](alpine-js-engineering.md); visual design of UI states in [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md); SPA client state in [django-react-integration.md](django-react-integration.md).

## 1. Purpose and scope

Use this reference when deciding *where a piece of state belongs* and *how an interaction behaves over time* (in flight, failed, superseded, repeated). Most frontend bugs in Django apps are not rendering bugs but state bugs: a filter lost on reload, a toast that never appears after an HTMX save, a stale response overwriting a fresh one, an invoice created twice.

Default stance for server-rendered Django:
- **The server (database + Django view) is the source of truth.** The browser holds copies and transient UI state only.
- **The URL holds anything a user would bookmark, share or expect Back to restore.**
- **Local UI state stays local** (one element or one Alpine component).
- **No global client store by default.** No Redux/Zustand-style layer unless you've built a React SPA that needs one.

## 2. Core concepts

### State taxonomy
| Kind | Examples | Lives in | Lifetime | Owner |
|---|---|---|---|---|
| Server/domain state | Tasks, invoice status, permissions | Database; rendered by Django | Durable | Django view |
| URL state | `?q=`, `?status=`, `?page=`, `?sort=`, active tab that should be linkable | Query string / path | Shareable, history | Django view reads `request.GET` |
| Form state (unsaved) | Field values, client hints, server errors | DOM inputs (`<form>`), re-rendered Django form | Until submit/leave | Browser, then Django form |
| Local UI state | Dropdown open, accordion, selected rows, hover | DOM attributes (`open`, `aria-expanded`) or Alpine `x-data` | Element lifetime | Component |
| Cross-component UI state | Cart count badge, sidebar collapsed, notification count | Alpine store / server-rendered OOB fragment / CustomEvents | Page lifetime | Smallest common owner |
| Session state | Current tenant, wizard draft, flash messages | Django session / DB draft row | Login session | Django |
| Persistent preferences | Theme, table density, dismissed tips | User model (cross-device) or `localStorage` (device) | Long | User / browser |
| Request/async state | Loading, error, in-flight, superseded | CSS classes (`htmx-request`), `aria-busy`, Alpine flags | One request | Request initiator |

Rules:
1. **Single source of truth per fact.** If the server knows it, don't duplicate it in JS; re-render.
2. **Derived state is computed, not stored.** "3 selected" derives from checked checkboxes.
3. **Lift state only as far as needed.** Element → component → page store → server.

### URL as state
A GET form is the simplest state machine: inputs serialize to the query string, the view reads `request.GET`, the page renders, Back/Forward and bookmarks work, and nothing needs JS. HTMX enhances it (partial swap + `hx-push-url`), it does not replace it.

### Request lifecycle states
Every async interaction has: **idle → pending → success | error | superseded (cancelled)**. Design UI for all five; "superseded" must be silent.

### Consistency models
- **Pessimistic**: wait for the server, then show the result. Default for money, permissions, irreversible actions.
- **Optimistic**: show the expected result immediately, reconcile with the server, roll back on failure. Only for low-risk, high-frequency, likely-to-succeed actions.

## 3. Internal mechanics

### Browser history and URL state
- Full-page GET navigations add a history entry; the browser restores the page (possibly from bfcache, with JS state intact — handle `pageshow` with `event.persisted` if you show stale counters).
- `hx-push-url="true"` (or the `HX-Push-Url` response header) calls `history.pushState` after the swap and HTMX snapshots the DOM into its history cache (HTMX 2.x stores snapshots in `localStorage` by default [VERIFY: `htmx.config.historyCacheSize` / storage behaviour in your HTMX version]). On Back, HTMX restores from cache or, on miss, re-requests the URL with `HX-History-Restore-Request: true` — your view must then return a **full page**, not a fragment.
- `hx-replace-url` uses `replaceState` (no new entry) — use for typing in a search box so Back doesn't step through every keystroke.

### `{% querystring %}` (Django ≥ 5.1)
Builds a query string from `request.GET` with overrides: `{% querystring page=3 %}` keeps `q` and `status`, replaces `page`; `{% querystring page=None %}` removes a key. Requires `request` in the template context (the `request` context processor). Output starts with `?`.

### HTMX request coordination
- `hx-sync` coordinates requests between elements: `hx-sync="this:replace"` aborts the in-flight request from that element and issues the new one; `closest form:abort` makes an input's request yield to its form's submit; `queue first|last|all` queues. [VERIFY: exact strategy keywords in htmx.org `hx-sync` docs]
- While a request is pending HTMX adds `htmx-request` to the element (or `hx-indicator` target); `hx-disabled-elt` disables elements for the request duration.
- In HTMX 2.x, 4xx/5xx responses are not swapped by default; handle `htmx:responseError` or configure `htmx.config.responseHandling` [VERIFY: config shape]. A 422 with re-rendered form errors therefore needs explicit config or a 200 response.
- `HX-Trigger` response header (JSON allowed) dispatches DOM events on the triggering element after the response; they bubble to `document`/`window` listeners. Variants `HX-Trigger-After-Swap` / `HX-Trigger-After-Settle` change timing.
- Out-of-band swaps: elements with `hx-swap-oob="true"` in the response replace elements with the same `id` anywhere on the page.

### Why out-of-order responses happen
Requests run concurrently over HTTP/2; response time depends on query cost, not send order. `q=ab` (slow, many matches) can resolve after `q=abc` (fast). Without cancellation or ordering, the last *arriving* response wins, not the last *sent*. Aborting a request stops the client from using the response; it does not stop the server.

### Double submit mechanics
Double-click, Enter + click, a retry after a slow network, or Back + resubmit all send the same POST twice. Client-side disabling reduces it; only the server (idempotency key, unique constraint, state check, POST/Redirect/GET) prevents duplicate effects.

### Live regions
Screen readers announce changes inside an element with `role="status"` (implicit `aria-live="polite"`) or `role="alert"` (assertive). The region must **already exist in the DOM** before content changes; inserting a new region with text is often not announced. Accessibility depth: [frontend-accessibility.md](frontend-accessibility.md).

## 4. Engineering decisions and trade-offs

### Where should this state live?
| Question | If yes → |
|---|---|
| Should reload/bookmark/share/Back restore it? | URL (query string or path) |
| Is it a business fact or permission-dependent? | Server (DB), re-rendered |
| Must it survive across devices? | User/profile model |
| Device preference, harmless if lost? | `localStorage` (try/catch) |
| Only one widget cares? | DOM attribute / Alpine `x-data` |
| Two or three distant widgets care, page lifetime? | Alpine store or CustomEvent; or server OOB swap |
| Spans several requests in a flow? | Session or draft model row |

### Optimistic vs pessimistic
| Prefer optimistic when… | Prefer pessimistic when… |
|---|---|
| Toggle/star/reorder/mark-read; failure is rare | Payments, invoices sent, deletes without undo, permission changes |
| Action is idempotent and reversible | Server computes the result (totals, numbering, validation) |
| Latency is visible and frequent | Conflicts with other users are likely |
| Rollback is trivial and explainable | Rollback would confuse ("it said saved…") |

### Re-render vs patch
| Prefer re-rendering a server fragment | Prefer client-side patch |
|---|---|
| Several derived values change (row, totals, badge) | One isolated cosmetic value |
| Permissions/formatting/i18n in templates | Purely local state (open/closed) |
| You want one template as source of truth | Network round trip is unacceptable (drag preview) |

### Notifications
| Prefer | When |
|---|---|
| Inline message next to the control | Validation errors, field-level outcomes |
| Django messages rendered on next page (PRG) | Full-page form submit |
| Messages as OOB fragment or `HX-Trigger` toast | HTMX request with no navigation |
| Persistent banner | System state (read-only mode, trial expired) |
| `role="alert"` | Time-critical errors only — never for success |

### Modal vs page
| Prefer a page (own URL) | Prefer a modal (`<dialog>`) |
|---|---|
| Long forms, multi-step, needs deep link | Short confirm or 1–3 fields |
| Users open in new tab / share | Context of underlying page matters |
| Mobile-heavy usage | Quick edit inline in a list |

## 5. Django/frontend implementation patterns

### URL-driven search, filter, sort, pagination
```python
# tasks/views.py
from django.core.paginator import Paginator
from django.shortcuts import render
from .forms import TaskFilterForm
from .models import Task


def task_list(request, project_pk):
    form = TaskFilterForm(request.GET or None)
    tasks = Task.objects.filter(project__pk=project_pk, project__members=request.user)
    if form.is_valid():
        if q := form.cleaned_data["q"]:
            tasks = tasks.filter(title__icontains=q)
        if status := form.cleaned_data["status"]:
            tasks = tasks.filter(status=status)
        tasks = tasks.order_by(form.cleaned_data["sort"] or "-updated_at")  # sort is a ChoiceField: allow-listed
    page = Paginator(tasks.select_related("assignee"), 25).get_page(request.GET.get("page"))
    context = {"form": form, "page_obj": page, "project_pk": project_pk}
    template = "tasks/_results.html" if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request") else "tasks/list.html"
    response = render(request, template, context)
    response["Vary"] = "HX-Request"
    return response
```
With django-htmx (third-party) use `request.htmx` and `request.htmx.history_restore_request` instead of raw headers [VERIFY: attribute names in installed version]. Never pass raw `request.GET["sort"]` to `order_by` — validate via a `ChoiceField`.

```django
{# tasks/list.html #}
{% extends "base.html" %}
{% block content %}
<form method="get" role="search"
      hx-get="{% url 'tasks:list' project_pk %}"
      hx-target="#results"
      hx-trigger="input changed delay:300ms from:input[name=q], change from:select, submit"
      hx-sync="this:replace"
      hx-push-url="true"
      hx-indicator="#results">
  <label for="id_q">Search tasks</label> {{ form.q }}
  <label for="id_status">Status</label> {{ form.status }}
  <label for="id_sort">Sort</label> {{ form.sort }}
  <button type="submit">Apply</button>
</form>
<div id="results" aria-live="polite" aria-busy="false">
  {% include "tasks/_results.html" %}
</div>
{% endblock %}
```
```django
{# tasks/_results.html #}
{% if page_obj.object_list %}
  <table>
    <caption class="sr-only">Tasks, page {{ page_obj.number }} of {{ page_obj.paginator.num_pages }}</caption>
    <thead><tr><th scope="col">Title</th><th scope="col">Status</th><th scope="col">Assignee</th></tr></thead>
    <tbody>
    {% for task in page_obj %}
      <tr><td><a href="{{ task.get_absolute_url }}">{{ task.title }}</a></td>
          <td>{{ task.get_status_display }}</td><td>{{ task.assignee|default:"—" }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
  <nav aria-label="Pagination">
    {% if page_obj.has_previous %}<a href="{% querystring page=page_obj.previous_page_number %}" hx-get="{% querystring page=page_obj.previous_page_number %}" hx-target="#results" hx-push-url="true">Previous</a>{% endif %}
    {% if page_obj.has_next %}<a href="{% querystring page=page_obj.next_page_number %}" hx-get="{% querystring page=page_obj.next_page_number %}" hx-target="#results" hx-push-url="true">Next</a>{% endif %}
  </nav>
{% else %}
  <p>No tasks match these filters. <a href="{% url 'tasks:list' project_pk %}">Clear filters</a></p>
{% endif %}
```
Notes: changing a filter should reset `page` (the form doesn't include `page`, so it drops naturally). `{% querystring %}` keeps current filters on pagination links. Links work without JS. With `hx-push-url="true"` on a form `hx-get`, the pushed URL includes the parameters. Prefer `hx-replace-url` on the search input if every keystroke would otherwise create a history entry. A `CSS .htmx-request` rule can dim `#results`; toggle `aria-busy` via `htmx:beforeRequest`/`htmx:afterSettle` if needed.

### Loading, empty and error states
Every async region needs four renderings:
- **Loading**: keep old content visible but dimmed (`.htmx-request #results { opacity:.6 }`) for fast responses; skeleton for first loads; never block the whole page.
- **Empty**: distinguish "no data yet" (call to action: "Create your first invoice") from "no results for filters" (show "Clear filters").
- **Error**: human message + retry; keep user input; don't swap a Django debug page into a fragment.
- **Success/partial**: show what changed.
```js
// static/js/core/htmx-errors.js — one global handler for HTMX failures
document.body.addEventListener("htmx:responseError", (e) => {
  const status = e.detail.xhr.status;
  const message = status === 403 ? "You don't have permission to do that."
                : status === 404 ? "That item no longer exists."
                : "Something went wrong. Please try again.";
  document.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message } }));
});
document.body.addEventListener("htmx:sendError", () => {
  document.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message: "Network error — check your connection." } }));
});
```

### Toasts: Django messages framework + HTMX
Render a messages region on every page; it is the live region.
```django
{# templates/_messages.html #}
<div id="messages" class="toast-stack" role="status" {% if oob %}hx-swap-oob="true"{% endif %}>
  {% for message in messages %}
    <div class="toast toast-{{ message.tags }}">
      <p>{{ message }}</p>
      <button type="button" class="toast-close" aria-label="Dismiss">×</button>
    </div>
  {% endfor %}
</div>
```
Option A — **OOB fragment**: after an HTMX action, render the fragment plus `{% include "_messages.html" with oob=True %}`. Iterating `messages` marks them consumed, so they don't reappear on the next full page.
Option B — **`HX-Trigger` event** (good when the response body is a row and the toast container is client-managed):
```python
# core/htmx.py
import json
from django.contrib import messages


class HtmxMessagesMiddleware:
    """Move Django messages into an HX-Trigger 'messages' event for HTMX responses without redirects."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.headers.get("HX-Request") or 300 <= response.status_code < 400 or "HX-Redirect" in response:
            return response
        items = [{"level": m.level_tag, "message": str(m)} for m in messages.get_messages(request)]
        if items:
            existing = response.headers.get("HX-Trigger", "")
            triggers = json.loads(existing) if existing.startswith("{") else {n.strip(): None for n in existing.split(",") if n.strip()}
            triggers["messages"] = {"items": items}   # object payload → event.detail.items
            response["HX-Trigger"] = json.dumps(triggers)
        return response
```
```js
// static/js/features/toasts.js
const stack = document.getElementById("messages");
const tpl = document.getElementById("toast-template");   // <template> rendered in base.html

function show({ level, message }) {
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.classList.add(`toast-${level}`);
  node.querySelector("p").textContent = message;          // text, never HTML
  stack.append(node);
  if (level !== "error") setTimeout(() => node.remove(), 6000);  // errors stay until dismissed
}
document.body.addEventListener("messages", (e) => e.detail.items.forEach(show));
document.addEventListener("toast", (e) => show(e.detail));
stack.addEventListener("click", (e) => e.target.closest(".toast-close")?.closest(".toast")?.remove());
```
Always send an object payload (`{"messages": {"items": [...]}}`): non-object payloads (strings/arrays) are wrapped differently in `event.detail` [VERIFY: HX-Trigger detail shape for non-object payloads in HTMX 2.x]. The comma-separated (non-JSON) `HX-Trigger` form is converted to a dict above so earlier triggers survive. Accessibility: success toasts polite and auto-dismiss after ≥ 5 s, pause on hover/focus if possible (WCAG 2.2.1); errors persist; a toast must never be the only place critical info appears; don't move focus to toasts.

### Confirmation dialogs
- Prefer **undo** over confirm for reversible actions (archive, mark done).
- Use confirm for destructive, irreversible actions; state the object and consequence: "Delete invoice INV-0042? This cannot be undone."
- Simple: `hx-confirm="…"` or the `data-confirm` pattern in [javascript-engineering.md](javascript-engineering.md).
- Styled: native `<dialog>`:
```django
<button type="button" onclick="document.getElementById('confirm-delete-{{ invoice.pk }}').showModal()">Delete</button>
<dialog id="confirm-delete-{{ invoice.pk }}" aria-labelledby="cd-title-{{ invoice.pk }}">
  <form method="post" action="{% url 'invoices:delete' invoice.pk %}">
    {% csrf_token %}
    <h2 id="cd-title-{{ invoice.pk }}">Delete invoice {{ invoice.number }}?</h2>
    <p>This cannot be undone.</p>
    <button type="submit" formmethod="dialog" value="cancel" autofocus>Cancel</button>
    <button type="submit" class="btn-danger">Delete</button>
  </form>
</dialog>
```
`formmethod="dialog"` closes without submitting; `autofocus` on Cancel makes the safe action default. Inline `onclick` needs CSP `'unsafe-inline'` — in CSP-strict apps use a delegated listener (`data-dialog-open="id"`). The server still checks permission and state.

### Modals loaded from the server
```django
{# base.html #}
<dialog id="modal" aria-labelledby="modal-title"><div id="modal-body"></div></dialog>

<a href="{% url 'tasks:edit' task.pk %}"
   hx-get="{% url 'tasks:edit' task.pk %}" hx-target="#modal-body" hx-swap="innerHTML">Edit</a>
```
```js
// open after content arrives; close on server signal
document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.detail.target.id === "modal-body") document.getElementById("modal").showModal();
});
document.body.addEventListener("closeModal", () => document.getElementById("modal").close());
document.getElementById("modal").addEventListener("close", (e) => e.target.querySelector("#modal-body").replaceChildren());
```
The edit view returns the form fragment (with `<h2 id="modal-title">`) on GET and on invalid POST; on success it returns an OOB row update plus `HX-Trigger: {"closeModal": true, ...}`. The link's `href` is a full edit page for non-JS and new-tab use. `showModal()` gives focus containment, inert background and Escape; return focus to the opener is automatic on close in current browsers [VERIFY: focus restoration behaviour per browser].

### Optimistic toggle with rollback
```django
<button type="button" class="star" aria-pressed="{{ task.is_starred|yesno:'true,false' }}"
        data-url="{% url 'tasks:star' task.pk %}">
  <span class="sr-only">Star “{{ task.title }}”</span>★
</button>
```
```js
import { request } from "app/core/http.js";
import { delegate } from "app/core/dom.js";

const pending = new WeakMap();   // button → AbortController

delegate(document, "click", "button.star[data-url]", async (_e, btn) => {
  const previous = btn.getAttribute("aria-pressed") === "true";
  const next = !previous;
  btn.setAttribute("aria-pressed", String(next));          // optimistic
  pending.get(btn)?.abort();
  const ctrl = new AbortController();
  pending.set(btn, ctrl);
  try {
    const data = await request(btn.dataset.url, { method: "POST", json: { starred: next }, signal: ctrl.signal });
    btn.setAttribute("aria-pressed", String(data.starred));  // reconcile with server truth
  } catch (err) {
    if (err.name === "AbortError") return;                    // superseded by a newer click
    btn.setAttribute("aria-pressed", String(previous));       // rollback
    document.dispatchEvent(new CustomEvent("toast", { detail: { level: "error", message: "Couldn't update star." } }));
  }
});
```
The endpoint sets an absolute value (`starred: true/false`), not "toggle", so repeated/reordered requests converge.

### Race conditions: request IDs for non-HTMX fetches
```js
let latest = 0;
async function loadSuggestions(q) {
  const id = ++latest;
  const html = await request(`/tasks/suggest/?${new URLSearchParams({ q })}`, { headers: { Accept: "text/html" } });
  if (id !== latest) return;          // stale response: ignore
  list.innerHTML = html;              // trusted, server-rendered, autoescaped fragment from same origin
}
```
Combine with `AbortController` to also save bandwidth. For HTMX use `hx-sync`. For server-side write races (two users editing), use optimistic concurrency: include a `version`/`updated_at` hidden field, and reject with 409 + re-rendered form when it doesn't match.

### Double-submit prevention (layered)
```django
<form method="post" hx-post="{% url 'invoices:create' %}" hx-disabled-elt="find button[type=submit]" hx-sync="this:drop">
  {% csrf_token %}
  <input type="hidden" name="idempotency_key" value="{{ idempotency_key }}">
  {{ form.as_div }}
  <button type="submit">Create invoice</button>
</form>
```
```python
# invoices/views.py (excerpt)
from django.db import IntegrityError, transaction

def create_invoice(request):
    form = InvoiceForm(request.POST)
    if form.is_valid():
        try:
            with transaction.atomic():
                invoice = form.save(commit=False)
                invoice.tenant = request.tenant
                invoice.idempotency_key = request.POST["idempotency_key"]  # unique=True per tenant
                invoice.save()
        except IntegrityError:
            invoice = Invoice.objects.get(tenant=request.tenant, idempotency_key=request.POST["idempotency_key"])
        return redirect(invoice)   # PRG; HTMX: HX-Redirect / HX-Location
    ...
```
Client layers (`hx-disabled-elt`, `hx-sync="this:drop"`, or disabling the submit button in a `submit` handler for non-HTMX forms) are UX; the unique key + PRG is the guarantee. Don't disable the button on `click` (it can cancel the submit in some browsers); disable on `submit`.

### Multi-step workflows
- One URL per step (`/onboarding/step/2/`) so Back/reload work; the server decides which step is allowed next.
- Persist progress server-side: a draft model (`status="draft"`) for long or resumable flows; session for short anonymous ones. Never trust a hidden "step" field for authorization.
- Validate each step on the server; re-validate everything on final submit.
- Show progress ("Step 2 of 4") as text inside an `<ol>` with `aria-current="step"` on the current item.
- django-formtools (third-party) `SessionWizardView` is an option; form-level patterns: [django-forms-and-validation.md](django-forms-and-validation.md).

### Cross-component sync without global state
- **Server-driven**: response includes OOB fragments for every region that changed (`#task-row-7`, `#project-progress`, `#nav-badge`).
- **Event-driven**: `HX-Trigger: {"tasksChanged": {}}` and other regions declare `hx-get=… hx-trigger="tasksChanged from:body"` to refetch themselves.
- **Client-only**: an Alpine store for genuinely client UI state (sidebar collapsed). See [alpine-js-engineering.md](alpine-js-engineering.md).

## 6. Production use cases

- **Invoice list (multi-tenant)**: filters/sort/page in URL; `hx-sync="this:replace"`; empty state differs for "no invoices yet" vs "no matches"; bulk "mark paid" pessimistic with progress text and per-row OOB updates.
- **Task board**: optimistic status change with rollback; server returns authoritative row; `tasksChanged` refreshes column counts.
- **Settings page**: one form, PRG, Django messages toast on next render; theme preference saved to user model, mirrored in `localStorage` only to avoid a flash before load.
- **Project onboarding wizard**: draft `Project` row, step URLs, resumable.
- **Global search box**: `hx-replace-url` while typing, push on submit; request cancellation; results region `aria-live="polite"` with a result count sentence.

## 7. Advantages and disadvantages

| Strategy | Advantages | Disadvantages |
|---|---|---|
| Server as source of truth, re-render | No sync bugs, permissions centralized, simple tests | Round-trip latency, more HTML over wire |
| URL state | Shareable, Back works, cache-friendly GETs, SEO | Length limits, must validate every param |
| Optimistic UI | Feels instant | Rollback complexity, misleading on failure, needs idempotent API |
| OOB swaps | Multi-region updates in one response | Coupling between views and page layout ids |
| `HX-Trigger` events | Decoupled regions | Extra requests, harder to trace |
| Alpine store | Simple shared client state | Lost on reload, invisible to server |
| `localStorage` | Survives reload, no server | Per-device, XSS-readable, may throw |

## 8. Security and performance considerations

### Security
- **URL state is user input.** Validate every `request.GET` param through a form; allow-list sort fields; scope querysets by user/tenant ([authorization-and-access-control.md](../../django-security-engineering/references/authorization-and-access-control.md)).
- Don't put sensitive data in URLs (logged, in Referer, history). No tokens, emails or search terms that are PII in query strings for shared links where avoidable.
- Toast/message content must be text (`textContent`); Django messages are autoescaped in templates unless marked safe — don't `mark_safe` user data in messages.
- Optimistic UI must never imply success for an authorization failure; reconcile and roll back.
- Disabled buttons, hidden steps and client validation are not controls; the server re-checks state transitions (e.g., can't pay a void invoice).
- `HX-History-Restore-Request` / history cache stores page HTML in `localStorage` — avoid caching pages with sensitive data (`hx-history="false"` on such pages) [VERIFY: attribute behaviour in HTMX 2.x].

### Performance
- Debounce search (`delay:300ms`); cancel superseded requests (`hx-sync`, `AbortController`).
- Return the smallest fragment; send `Vary: HX-Request` so caches don't mix fragments and pages.
- Keep loading indicators delayed (~150–300 ms) to avoid flicker on fast responses.
- Avoid polling for state; prefer event-triggered refresh or SSE where needed.
- Pagination via `Paginator` with indexed ordering; avoid `COUNT(*)` on huge tables if not needed (backend: [django-application-performance.md](../../django-performance/references/django-application-performance.md)).

## 9. Common mistakes and anti-patterns

1. **Filters held only in JS/Alpine** → lost on reload, not shareable; put them in a GET form/URL.
2. **Fragment returned on history restore** → Back shows a naked fragment; check `HX-History-Restore-Request`, return full page, set `Vary`.
3. **Pagination links drop filters** → use `{% querystring page=… %}`.
4. **Changing a filter keeps `page=7`** → empty page; exclude `page` from the filter form.
5. **Last-arriving response wins** → `hx-sync="this:replace"`, `AbortController`, or request IDs.
6. **Treating abort as rollback** → server may have committed; use idempotent absolute-value endpoints.
7. **"Toggle" endpoints** → reorder/duplicate-sensitive; send the target value.
8. **Optimistic UI for irreversible/financial actions** → pessimistic with clear pending state.
9. **Only client-side double-submit protection** → add idempotency key/unique constraint + PRG.
10. **Toasts for validation errors** → show inline at the field ([django-forms-and-validation.md](django-forms-and-validation.md)).
11. **Live region injected with the message** → not announced; render the empty region at load.
12. **Auto-dismissing error toasts** → users miss them; persist errors.
13. **Messages not consumed in HTMX responses** → they appear on an unrelated later page; render or move them on each HTMX response.
14. **Global store for server data** → stale copies; re-render from server.
15. **Custom div modals** → no focus trap/Escape/inert; use `<dialog>.showModal()`.
16. **Empty state indistinguishable from error/loading** → render explicit states.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Back button shows partial HTML | Fragment served for restore / cached without Vary | Request headers `HX-History-Restore-Request`; response `Vary` |
| Search results flicker to old query | Out-of-order responses | Network waterfall timing; add `hx-sync` |
| Toast never appears after HTMX save | Messages not rendered/moved; event name mismatch; 4xx not processed | Response headers (`HX-Trigger`), Elements for `#messages`, listener event name case |
| Toast shows on next page instead | Messages consumed on later full render | Middleware/OOB on HTMX responses |
| Duplicate records | Double submit | Server logs for two POSTs; idempotency key present? |
| Modal opens empty / twice | `showModal` on wrong event or listener per swap | `htmx:afterSwap` target check; listener registered once |
| Optimistic state stuck after failure | Missing rollback path for non-abort errors | Force 500 in dev; observe UI |
| Screen reader silent on result update | Region not live, or recreated | Accessibility tree in DevTools; region exists at load |
| Filters reset on pagination | Links built manually | Use `{% querystring %}` (Django ≥ 5.1) |

### Validation steps
- Reload, Back/Forward, copy-paste URL into a new tab for every list view: same state restored.
- Throttle network (Slow 4G), type quickly in search: final results match final query.
- Double-click every create button: one record.
- Kill the server mid-request: error state shown, input preserved.
- Run Playwright tests for these flows, including `expect(page).to_have_url(...)` after filtering ([frontend-testing.md](frontend-testing.md), [browser-and-end-to-end-testing.md](../../django-testing/references/browser-and-end-to-end-testing.md)).
- Screen reader / axe pass on toasts, dialogs and result regions.

## 11. Related references and skills

- [htmx-engineering.md](htmx-engineering.md) — `hx-sync`, `hx-push-url`, OOB, `HX-Trigger`, error handling.
- [alpine-js-engineering.md](alpine-js-engineering.md) — local and store state, `$dispatch`.
- [javascript-engineering.md](javascript-engineering.md) — fetch wrapper, AbortController, debounce.
- [django-forms-and-validation.md](django-forms-and-validation.md) — inline errors, multi-step forms.
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — visual loading/empty/error states.
- [frontend-accessibility.md](frontend-accessibility.md) — live regions, dialogs, focus.
- [html-and-semantic-ui.md](html-and-semantic-ui.md) — `<dialog>`, forms, `<details>`.
- [django-react-integration.md](django-react-integration.md) — SPA state, `useOptimistic`.
- [frontend-security.md](frontend-security.md) — storage and URL safety.
- [urls-views.md](../../django-core/references/urls-views.md), [authentication-sessions.md](../../django-core/references/authentication-sessions.md).
- [pagination.md](../../django-rest-framework/references/pagination.md) — API-side pagination.

## 12. Official documentation and external references

- Django `querystring` tag: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#querystring
- Django messages framework: https://docs.djangoproject.com/en/5.2/ref/contrib/messages/
- Django pagination: https://docs.djangoproject.com/en/5.2/topics/pagination/
- Django sessions: https://docs.djangoproject.com/en/5.2/topics/http/sessions/
- htmx `hx-sync`: https://htmx.org/attributes/hx-sync/
- htmx `hx-push-url`: https://htmx.org/attributes/hx-push-url/
- htmx response headers: https://htmx.org/reference/#response_headers
- htmx `hx-swap-oob`: https://htmx.org/attributes/hx-swap-oob/
- MDN History API: https://developer.mozilla.org/en-US/docs/Web/API/History_API
- MDN `<dialog>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog
- MDN ARIA live regions: https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/ARIA_Live_Regions
- MDN AbortController: https://developer.mozilla.org/en-US/docs/Web/API/AbortController
- WAI-ARIA APG Dialog (Modal) pattern: https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/
- WCAG 2.2 Understanding 2.2.1 Timing Adjustable: https://www.w3.org/WAI/WCAG22/Understanding/timing-adjustable.html
- WCAG 2.2 Understanding 4.1.3 Status Messages: https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html
- web.dev bfcache: https://web.dev/articles/bfcache
