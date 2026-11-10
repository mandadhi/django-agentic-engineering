# JavaScript Engineering for Django UIs

> **Scope:** Plain modern JavaScript (ES2022+) as used in server-rendered Django apps: modules, scope, async, events and delegation, DOM APIs, fetch/CSRF/FormData/JSON, storage, cancellation, errors, debounce/throttle, lazy loading, memory leaks and debugging. HTMX patterns live in [htmx-engineering.md](htmx-engineering.md), Alpine in [alpine-js-engineering.md](alpine-js-engineering.md), bundling/static pipelines in [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md), and XSS/CSP depth in [frontend-security.md](frontend-security.md).

## 1. Purpose and scope

Use this reference when you must write JavaScript that a Django template page loads: a progressive enhancement, a small widget, glue between HTMX/Alpine and the browser, or a page module that talks to a Django view with `fetch`.

The governing rule: **JavaScript is the last layer, not the first.** Order of preference for any interaction:

1. Plain HTML (links, forms, `<details>`, `<dialog>`, input types, `required`, `pattern`).
2. CSS (`:hover`, `:focus-visible`, `:has()`, `:target`, transitions, `prefers-reduced-motion`).
3. Server round trip with HTMX (partial HTML from a Django view).
4. Alpine.js for local, ephemeral UI state.
5. Hand-written JS module (this reference) for things the above cannot do: clipboard, autosave, IntersectionObserver, complex keyboard handling, third-party widget glue.
6. A React island only for genuinely app-like surfaces ([django-react-integration.md](django-react-integration.md)).

Out of scope: TypeScript tooling, bundler config, React, Node/SSR.

## 2. Core concepts

### Modules
- `<script type="module">` is **deferred by default**, runs in **strict mode**, has its own top-level scope (no accidental globals), executes **once per resolved URL** even if included twice, and is subject to CORS for cross-origin URLs.
- `import`/`export` are static and hoisted; `import()` is dynamic and returns a promise.
- Top-level `await` is allowed in modules (blocks dependants of that module, not the page parse).
- Import maps (`<script type="importmap">`) map bare specifiers (`"app/"`, `"alpinejs"`) to URLs. Baseline widely available; must appear **before** any module script that uses the mapped specifiers.

### Scope and closures
- `const` by default, `let` when reassigned, never `var` (function-scoped, hoisted, leaks across blocks).
- Block scope means each loop iteration with `let` gets a fresh binding — closures in `for (let i…)` capture the right `i`.
- A closure keeps every variable it references alive. That is how debounce works — and how memory leaks happen (§3).

### Values and data
- Objects and arrays are references; spread (`{...obj}`, `[...arr]`) copies one level. `structuredClone()` deep-copies plain data (Baseline 2022).
- Destructuring with defaults: `const { url, method = "POST", signal } = options;`
- Optional chaining / nullish: `el?.dataset.id ?? ""` — `??` only falls back on `null`/`undefined`, unlike `||` which also replaces `0` and `""`.
- `Object.groupBy`, `Array.prototype.findLast`, `at()`: check Baseline before relying on the newest ones [VERIFY: Baseline status of Object.groupBy for your browser targets].

### Async
- A Promise represents a value that settles later. `async` functions always return a promise; `await` suspends the function, not the thread.
- `Promise.all` fails fast; `Promise.allSettled` waits for all; `Promise.race`/`Promise.any` for first-wins.
- Unhandled rejections surface in the console and in `unhandledrejection` events — always `await` or `.catch()`.

### Events
- Events dispatch through **capture** (window → target), **target**, then **bubble** (target → window). Most UI events bubble; `focus`/`blur` do not (use `focusin`/`focusout`), `mouseenter`/`mouseleave` do not.
- **Delegation**: one listener on a stable ancestor (`document` or a page root) that checks `event.target.closest(selector)`. It survives HTMX swaps because the listener is not on the replaced nodes.
- `CustomEvent` with `detail` is the decoupling mechanism between modules, HTMX (`HX-Trigger`) and Alpine (`$dispatch`).

## 3. Internal mechanics

### Script loading
| Markup | Download | Execute | Order |
|---|---|---|---|
| `<script src>` in `<head>` | blocks parser | immediately | document order |
| `<script defer src>` | parallel | after parse, before `DOMContentLoaded` | document order |
| `<script async src>` | parallel | as soon as downloaded | none |
| `<script type="module" src>` | parallel (plus its import graph) | like `defer` | document order |
| `<script type="module" async>` | parallel | as soon as graph ready | none |

Module graphs are fetched recursively; deep import chains create request waterfalls. Mitigate with `<link rel="modulepreload" href="…">` for critical modules or with a bundler ([static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)). Cross-cutting browser pipeline detail: [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md).

### Event loop in one paragraph
One main thread runs a task (script, event callback, timer), then drains **all microtasks** (promise reactions, `queueMicrotask`, MutationObserver callbacks), then the browser may render (style → layout → paint). A long task (> 50 ms) delays input handling and hurts INP. Break long work up (`await new Promise(r => setTimeout(r))`, or chunking) and never loop over thousands of DOM nodes reading and writing layout alternately.

### Layout thrashing
Reading a layout property (`offsetHeight`, `getBoundingClientRect()`) after a style write forces synchronous layout. Batch reads first, then writes; or use `requestAnimationFrame` for writes.

### DOM construction and safety
- `textContent` sets text — no parsing, no XSS, fast.
- `innerHTML` parses HTML — an XSS sink for any untrusted string, destroys existing child nodes and their listeners, and re-parses on every assignment.
- `<template>` holds inert markup (scripts don't run, images don't load) until you `content.cloneNode(true)`. Best way to build repeated DOM from server-authored markup: Django renders the template, JS clones it and fills text via `textContent`.
- `element.replaceChildren(...nodes)` clears and inserts in one step.

### fetch semantics
- `fetch()` **rejects only on network failure, CORS failure or abort**. HTTP 4xx/5xx **resolve** with `response.ok === false`. You must check.
- Same-origin requests send cookies by default (`credentials: "same-origin"`), so Django session auth works and CSRF is required for unsafe methods.
- Redirects are followed transparently; `response.redirected` and `response.url` tell you. A login redirect therefore looks like a 200 HTML page — detect it (§5).
- The body can be read once (`response.json()`/`.text()`); clone if you need both.

### Django CSRF from JS
Django's `CsrfViewMiddleware` accepts the token in the `X-CSRFToken` header (setting `CSRF_HEADER_NAME`, default `HTTP_X_CSRFTOKEN`). Sources for the token:
- the `csrftoken` cookie (`CSRF_COOKIE_NAME`), readable because `CSRF_COOKIE_HTTPONLY` defaults to `False`;
- a `{% csrf_token %}` hidden input or a `<meta name="csrf-token">` you render — **required** if `CSRF_USE_SESSIONS = True` or `CSRF_COOKIE_HTTPONLY = True`.
The cookie is only set once a view rendered a template using the token or called `get_token()` (or is decorated with `ensure_csrf_cookie`). Backend detail: [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md).

### Storage mechanics
| Store | Size (approx.) | API | Sent to server | Readable by XSS | Lifetime |
|---|---|---|---|---|---|
| `localStorage` | ~5 MB per origin [VERIFY: per-browser quota] | synchronous, strings only | no | yes | until cleared |
| `sessionStorage` | ~5 MB per origin | synchronous, strings | no | yes | per tab |
| Cookie (non-HttpOnly) | ~4 KB each | `document.cookie` string | every matching request | yes | `Max-Age`/session |
| HttpOnly cookie | ~4 KB | not readable by JS | every request | no | server-set |
| IndexedDB | large, quota-managed | async | no | yes | until cleared |

Storage is **per origin** (scheme+host+port), synchronous Web Storage blocks the main thread, and anything JS can read, an XSS payload can read. Never store tokens, PII or authorization state in Web Storage. Private browsing and quota errors throw — wrap in `try`.

### Cancellation
`AbortController` creates a signal; passing it to `fetch`, `addEventListener` (`{ signal }`) or your own code lets one `abort()` cancel them all. `AbortSignal.timeout(ms)` gives a signal that aborts with a `TimeoutError` `DOMException`; `AbortSignal.any([a, b])` combines signals [VERIFY: AbortSignal.any Baseline status for your targets]. Aborting a fetch rejects with `AbortError` — the server may still have processed the request.

### Memory leaks
The garbage collector frees objects unreachable from roots (globals, live DOM, active closures, pending timers/observers). Typical leaks in Django pages that swap HTML:
- listeners attached directly to nodes that HTMX later removes **while something else still references them** (a module-level array, a closure in a long-lived listener);
- `setInterval` never cleared — keeps its closure (and the DOM it references) alive forever and keeps firing;
- `IntersectionObserver`/`ResizeObserver`/`MutationObserver` never `disconnect()`ed;
- caches (`Map`) keyed by DOM nodes — use `WeakMap`;
- third-party widgets initialized per swap without `destroy()`.

## 4. Engineering decisions and trade-offs

### Do I need JavaScript at all?
| Need | Prefer | Only then JS |
|---|---|---|
| Show/hide section | `<details>/<summary>` | Animated, coordinated panels (Alpine) |
| Modal | `<dialog>` + tiny `showModal()` call | — |
| Validation | HTML attributes + Django form errors | Cross-field live hints |
| Filter list | GET `<form>` (+ HTMX) | Client-only filtering of < ~200 static rows |
| Tabs | Server links / `:target` / Alpine | Complex keyboard widget |
| Confirm delete | `<dialog>` or `confirm()` progressive enhancement | — |
| Clipboard, observers, autosave | — | JS module |

### Module delivery
| Prefer | When |
|---|---|
| Native ES modules served by Django staticfiles (no build) | < ~10 modules, no npm deps or only ESM-ready CDN-free vendored files, small team |
| Import map + vendored libs in `static/vendor/` | You want bare specifiers without a bundler |
| Bundler (Vite/esbuild) | npm dependencies, TypeScript, many modules, need minification/tree-shaking, React islands |

Caveat: `ManifestStaticFilesStorage` hashes filenames; relative `import "./x.js"` inside modules is not rewritten unless JS module support is enabled on the storage class [VERIFY: `support_js_module_import_aggregation` attribute and its default in Django 5.2]. Import maps referencing `{% static %}` URLs avoid that: the template tag yields hashed URLs.

### Event wiring
| Prefer delegation when | Prefer direct listeners when |
|---|---|
| Content is swapped by HTMX / re-rendered | Element is static for page life |
| Many similar elements (rows, buttons) | High-frequency events on one element (`pointermove`, `scroll`) |
| You want zero per-node init/cleanup | You need non-bubbling events (`mouseenter`) |

### `fetch` + JSON vs HTMX + HTML
| Prefer HTMX/HTML fragments | Prefer fetch + JSON |
|---|---|
| Result is rendered UI | Result feeds a non-DOM consumer (clipboard, chart lib, map) |
| Server owns formatting, permissions, i18n | Tiny status payload (`{"saved_at": …}`) |
| You want one source of templates | You already have a DRF endpoint |

### Debounce vs throttle
- **Debounce**: run once after input pauses (search-as-you-type, autosave). Trailing edge.
- **Throttle**: run at most every N ms during continuous events (scroll position, resize). Prefer CSS / `IntersectionObserver` / `ResizeObserver` over throttled scroll handlers entirely.

## 5. Django/frontend implementation patterns

### Module layout under `static/js/`
```text
myproject/static/js/
  core/
    csrf.js          # getCsrfToken()
    http.js          # request() wrapper: CSRF, JSON, errors, timeout
    dom.js           # delegate(), qs(), fromTemplate()
    timing.js        # debounce(), throttle()
  features/
    confirm-delete.js
    autosave.js
    clipboard.js
    lazy-chart.js
  pages/
    task-detail.js   # entry: imports features used on that page
  main.js            # site-wide entry: delegated, always-on behaviour
```
One entry per page (or per feature cluster); shared code in `core/`. No module touches the DOM at import time except entries; features export `init(root)` functions that return a cleanup function.

### Base template with import map
```django
{# templates/base.html #}
{% load static %}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Projects{% endblock %}</title>
  <script type="importmap">
  {
    "imports": {
      "app/core/": "{% static 'js/core/' %}",
      "app/features/": "{% static 'js/features/' %}"
    }
  }
  </script>
  <script type="module" src="{% static 'js/main.js' %}"></script>
  {% block page_js %}{% endblock %}
</head>
<body>{% block content %}{% endblock %}</body>
</html>
```
Directory-prefix mappings resolve to unhashed paths for files under them; if you use manifest storage, map each module explicitly (`"app/core/http.js": "{% static 'js/core/http.js' %}"`) so every URL is hashed. With a CSP, the import map is an inline script and needs a nonce ([frontend-security.md](frontend-security.md)).

### CSRF and a `request()` wrapper
```js
// static/js/core/csrf.js
export function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta) return meta.content;
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}
```

```js
// static/js/core/http.js
import { getCsrfToken } from "app/core/csrf.js";

export class HttpError extends Error {
  constructor(response, body) {
    super(`HTTP ${response.status}`);
    this.name = "HttpError";
    this.status = response.status;
    this.body = body;
  }
}

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export async function request(url, { method = "GET", json, body, signal, timeout = 10_000, headers = {} } = {}) {
  const h = new Headers(headers);
  h.set("Accept", "application/json");
  if (!SAFE.has(method)) h.set("X-CSRFToken", getCsrfToken());
  if (json !== undefined) {
    h.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const timeoutSignal = AbortSignal.timeout(timeout);
  const finalSignal = signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;

  const response = await fetch(url, { method, headers: h, body, signal: finalSignal, credentials: "same-origin" });

  // Session expired: Django redirected to the login page and fetch followed it.
  if (response.redirected && new URL(response.url).pathname.startsWith("/accounts/login/")) {
    window.location.assign(response.url);
    throw new HttpError(response, null);
  }
  const type = response.headers.get("Content-Type") || "";
  const data = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new HttpError(response, data);
  return data;
}
```
Never set `Content-Type` manually when sending `FormData` — the browser must add the multipart boundary.

### Django view returning JSON for a JS caller
```python
# tasks/views.py
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from .forms import TaskNotesForm
from .models import Task


@login_required
@require_POST
def autosave_notes(request, pk):
    task = get_object_or_404(Task, pk=pk, project__members=request.user)
    form = TaskNotesForm(request.POST, instance=task)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=422)
    task = form.save()
    return JsonResponse({"saved_at": task.updated_at.isoformat()})
```
Authorization is in the queryset; any client-side disabling is cosmetic.

### Passing Django data to JS: `json_script`
```django
{{ chart_data|json_script:"burndown-data" }}
<canvas id="burndown" data-endpoint="{% url 'projects:burndown' project.pk %}"></canvas>
```
```js
const data = JSON.parse(document.getElementById("burndown-data").textContent);
```
`json_script` escapes `<`, `>`, `&` so the JSON cannot close the `<script>` element. Never write `var data = {{ data|safe }};` or interpolate values into inline JS strings. Small scalars (URLs, IDs) belong in `data-*` attributes (read via `el.dataset`), which are HTML-attribute-escaped by autoescape.

### Delegation helper and confirm-before-delete (progressive enhancement)
Baseline HTML works without JS: a POST form to a confirmation-free delete view guarded server-side, or a link to a Django confirm page.
```django
<form method="post" action="{% url 'tasks:delete' task.pk %}"
      data-confirm="Delete “{{ task.title }}”? This cannot be undone.">
  {% csrf_token %}
  <button type="submit" class="btn btn-danger">Delete</button>
</form>
```
```js
// static/js/core/dom.js
export function delegate(root, type, selector, handler, options) {
  const listener = (event) => {
    const match = event.target.closest(selector);
    if (match && root.contains(match)) handler(event, match);
  };
  root.addEventListener(type, listener, options);
  return () => root.removeEventListener(type, listener, options);
}

export function fromTemplate(id) {
  return document.getElementById(id).content.firstElementChild.cloneNode(true);
}
```
```js
// static/js/features/confirm-delete.js
import { delegate } from "app/core/dom.js";

export function init(root = document) {
  return delegate(root, "submit", "form[data-confirm]", (event, form) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
}
```
`submit` bubbles, so one listener covers every current and future (HTMX-swapped) form. `data-confirm` text is autoescaped by Django; `confirm()` renders text, never HTML. For a styled confirm use `<dialog>` ([frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md)); with HTMX forms `hx-confirm` does the same declaratively.

### Autosave with debounce + abort
```js
// static/js/core/timing.js
export function debounce(fn, wait = 300) {
  let timer;
  const debounced = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  debounced.cancel = () => clearTimeout(timer);
  return debounced;
}

export function throttle(fn, interval = 100) {
  let last = 0, timer;
  return (...args) => {
    const remaining = interval - (Date.now() - last);
    clearTimeout(timer);
    if (remaining <= 0) { last = Date.now(); fn(...args); }
    else timer = setTimeout(() => { last = Date.now(); fn(...args); }, remaining);
  };
}
```
```django
<form id="notes-form" method="post" action="{% url 'tasks:autosave_notes' task.pk %}" data-autosave>
  {% csrf_token %}
  <label for="id_notes">Notes</label>
  <textarea id="id_notes" name="notes" rows="8">{{ form.notes.value|default:"" }}</textarea>
  <p data-autosave-status role="status" class="text-sm"></p>
  <button type="submit">Save</button>  {# still works with JS disabled #}
</form>
```
```js
// static/js/features/autosave.js
import { request, HttpError } from "app/core/http.js";
import { debounce } from "app/core/timing.js";

export function init(form) {
  const status = form.querySelector("[data-autosave-status]");
  let inFlight = null;

  const save = debounce(async () => {
    inFlight?.abort();                       // newest edit wins
    inFlight = new AbortController();
    status.textContent = "Saving…";
    try {
      const data = await request(form.action, {
        method: "POST", body: new FormData(form), signal: inFlight.signal,
      });
      status.textContent = `Saved at ${new Date(data.saved_at).toLocaleTimeString()}`;
    } catch (err) {
      if (err.name === "AbortError") return; // superseded, not a failure
      status.textContent = err instanceof HttpError && err.status === 422
        ? "Not saved: fix the errors and try again."
        : "Not saved. Check your connection.";
    }
  }, 800);

  form.addEventListener("input", save);
  return () => { save.cancel(); inFlight?.abort(); form.removeEventListener("input", save); };
}
```
`role="status"` announces politely; do not announce every keystroke (debounce does that). Abort only cancels the *client's* wait — a slow earlier request may still commit after a later one. If ordering matters server-side, send a version/`updated_at` and reject stale writes (see race conditions in [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md)).

### Copy to clipboard
```django
<div class="flex gap-2">
  <input id="invite-url" value="{{ invite_url }}" readonly>
  <button type="button" data-copy-target="#invite-url" hidden>Copy link</button>
  <span data-copy-status role="status" class="sr-only"></span>
</div>
```
```js
// static/js/features/clipboard.js
import { delegate } from "app/core/dom.js";

export function init(root = document) {
  if (!navigator.clipboard) return () => {};          // insecure context or old browser
  root.querySelectorAll("[data-copy-target][hidden]").forEach((b) => (b.hidden = false));
  return delegate(root, "click", "[data-copy-target]", async (_event, button) => {
    const source = document.querySelector(button.dataset.copyTarget);
    const status = button.parentElement.querySelector("[data-copy-status]");
    try {
      await navigator.clipboard.writeText(source.value ?? source.textContent);
      status.textContent = "Link copied";
    } catch {
      source.select?.();
      status.textContent = "Copy failed — press Ctrl+C";
    }
  });
}
```
The button is hidden until JS proves the API exists (`navigator.clipboard` requires a secure context — HTTPS or localhost). The readonly input remains selectable without JS.

### Page entry and HTMX-safe initialization
```js
// static/js/main.js — site-wide, delegated only (no per-node state)
import { init as confirmDelete } from "app/features/confirm-delete.js";
import { init as clipboard } from "app/features/clipboard.js";
confirmDelete(document);
clipboard(document);
```
```js
// static/js/pages/task-detail.js — per-node features need init + cleanup per swap
import { init as autosave } from "app/features/autosave.js";

const cleanups = new WeakMap();

function mount(root) {
  root.querySelectorAll("form[data-autosave]").forEach((form) => {
    if (!cleanups.has(form)) cleanups.set(form, autosave(form));
  });
}
mount(document);
document.addEventListener("htmx:load", (e) => mount(e.target));          // new content
document.addEventListener("htmx:beforeCleanupElement", (e) => {          // removed content
  cleanups.get(e.target)?.();
});
```
Prefer `htmx.onLoad(fn)` if `htmx` is a global; event names per HTMX 2.x docs [VERIFY: `htmx:beforeCleanupElement` fires for each removed element in your HTMX version].

### Lazy loading
```django
<img src="{{ project.cover.url }}" alt="" width="640" height="360" loading="lazy" decoding="async">
<section data-lazy-module="app/features/lazy-chart.js" data-endpoint="{% url 'projects:burndown' project.pk %}">
  <h2>Burndown</h2>
  <p>Loading chart…</p>
</section>
```
```js
// static/js/main.js (continued)
const io = new IntersectionObserver((entries, observer) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    observer.unobserve(entry.target);
    import(entry.target.dataset.lazyModule)        // dynamic import, resolved via import map
      .then((mod) => mod.init(entry.target))
      .catch(() => { entry.target.querySelector("p").textContent = "Chart unavailable."; });
  }
}, { rootMargin: "200px" });
document.querySelectorAll("[data-lazy-module]").forEach((el) => io.observe(el));
```
Never lazy-load the LCP image (`loading="lazy"` on the hero delays LCP). Restrict `data-lazy-module` values to an allow-list if attribute content could ever be user-influenced. Native lazy loading on `<img>`/`<iframe>` is Baseline widely available. Performance depth: [frontend-performance.md](frontend-performance.md).

### Error handling layers
```js
window.addEventListener("unhandledrejection", (e) => report("unhandledrejection", e.reason));
window.addEventListener("error", (e) => report("error", e.error ?? e.message));
function report(kind, detail) {
  navigator.sendBeacon?.("/frontend-errors/", JSON.stringify({ kind, detail: String(detail), url: location.pathname }));
}
```
`sendBeacon` cannot set the CSRF header; the endpoint must be CSRF-exempt, accept only this narrow payload, rate-limit, and never trust its contents. Per call site: catch expected errors (`HttpError` 4xx → show message; `AbortError` → ignore; `TypeError` from `fetch` → offline message), rethrow the rest.

## 6. Production use cases

- **Invoice list bulk actions**: delegated `change` listener on `#invoice-table` toggling a "selected count" and enabling a bulk-action button; the actual action is a normal POST form listing selected IDs; server re-checks permissions per ID.
- **Tenant dashboard widgets**: each widget section lazy-imports its chart module when visible; data from `json_script` or a small JSON view; skeleton text is server-rendered.
- **Task notes autosave**: §5 pattern, plus `beforeunload` warning only while a save is pending.
- **Copy API key ID / invite links**: clipboard pattern; never render secrets into the page you would not show in plain text anyway.
- **Keyboard shortcuts** (`/` focuses search): one `keydown` listener on `document`, ignored when focus is in an input/textarea/contenteditable; document shortcuts in a help dialog; allow disabling (WCAG 2.1.4 Character Key Shortcuts).

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Vanilla ES modules, no build | Zero toolchain, debuggable source, cacheable per file | Request waterfalls for deep graphs, no npm resolution, no minification |
| Delegated listeners | Survives swaps, no per-node cleanup, fewer listeners | `closest()` per event; can't use non-bubbling events; easy to over-match selectors |
| `fetch` + JSON | Precise control, works with DRF | Duplicates rendering logic in JS, manual CSRF/errors/redirects |
| `json_script` | Safe, no extra request | Inflates HTML, data is static for page life |
| Web Storage | Simple, synchronous | XSS-readable, blocking, per-origin, strings only, can throw |

## 8. Security and performance considerations

### Security
- **Sinks**: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, `setTimeout("string")`, `href`/`src` with `javascript:` URLs. Use `textContent`, `setAttribute` on safe attributes, `<template>` cloning. Full list and Trusted Types: [frontend-security.md](frontend-security.md), [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md).
- **URLs from data**: validate with `new URL(value, location.origin)` and allow only `http:`/`https:` and expected origins before assigning to `href`/`location`.
- **CSRF**: send `X-CSRFToken` on every unsafe same-origin request; never add CSRF tokens to cross-origin requests; never disable CSRF to "make fetch work".
- **Secrets**: nothing shipped to the browser is secret — not inline config, not bundled env vars, not `data-*`.
- **Client checks are UX only**: hidden/disabled buttons and JS validation do not authorize anything; the Django view must enforce.
- **`postMessage`**: always check `event.origin` and validate `event.data` shape.

### Performance
- Ship less JS: every KB parses and compiles on the main thread. Prefer HTML/CSS/HTMX.
- Avoid long tasks; debounce input handlers; batch DOM reads/writes; use `DocumentFragment` or `replaceChildren` for bulk inserts.
- Passive listeners for `touchstart`/`wheel` (`{ passive: true }`) when you don't `preventDefault`.
- `modulepreload` critical modules; lazy-import below-the-fold features.
- Avoid synchronous `localStorage` in hot paths (input handlers, scroll).

## 9. Common mistakes and anti-patterns

1. **Assuming `fetch` throws on 500** → check `response.ok`, throw a typed error.
2. **Missing CSRF header** → 403 "CSRF token missing"; use the `request()` wrapper; render `{% csrf_token %}`/meta if the cookie is HttpOnly or session-stored.
3. **Setting `Content-Type: multipart/form-data` with FormData** → missing boundary, Django sees empty `POST`; let the browser set it.
4. **Binding listeners per element after each HTMX swap** → duplicates or dead listeners; use delegation or init+cleanup on `htmx:load`/cleanup events.
5. **`innerHTML = userValue`** → XSS; use `textContent` or `<template>`.
6. **`{{ value|safe }}` or `'{{ value }}'` inside `<script>`** → injection; use `json_script` / `data-*`.
7. **`setInterval` polling without clearing** → leaks and requests after navigation; store the id, clear on cleanup or `visibilitychange`; or use `hx-trigger="every 10s"` on the swapped element.
8. **Storing auth tokens in `localStorage`** → exfiltrated by any XSS; use Django session cookies (HttpOnly).
9. **Treating an aborted request as "never happened"** → server may have committed; design idempotent endpoints.
10. **`var`, implicit globals, `window.myApp = …` everywhere** → collisions; use modules and explicit exports.
11. **Rebuilding whole tables in JS on every keystroke** → jank; filter server-side or debounce and diff minimally.
12. **Lazy-loading above-the-fold images** → worse LCP; eager-load the LCP image, consider `fetchpriority="high"`.
13. **Swallowing errors with empty `catch {}`** → silent failure; at minimum tell the user and report.
14. **Custom JS confirm/menus/tabs without keyboard support** → inaccessible; use native elements or follow APG patterns ([frontend-accessibility.md](frontend-accessibility.md)).

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| 403 on POST from fetch | No/wrong `X-CSRFToken`, cookie not set, HttpOnly cookie | Network → request headers; `document.cookie`; `CSRF_COOKIE_HTTPONLY`, `CSRF_USE_SESSIONS`; Django 403 page reason |
| 403 "Origin checking failed" | HTTPS behind proxy, `CSRF_TRUSTED_ORIGINS` missing scheme | `SECURE_PROXY_SSL_HEADER`, trusted origins include `https://` |
| `response.json()` throws "Unexpected token <" | Server returned HTML (login redirect, 500 debug page) | `response.redirected`, `response.url`, status, Content-Type |
| Handler fires twice | Listener added on every swap / module included via two URLs | Elements panel → Event Listeners; check module URLs are identical |
| Handler stops after HTMX swap | Listener on replaced node | Use delegation; check `htmx:load` init |
| `Failed to resolve module specifier` | Bare specifier without import map, or map after the module script | Order of `<script type="importmap">`; console |
| Module 404 in production only | Hashed filenames; relative imports not rewritten | Network tab URLs; manifest storage settings |
| `request.POST` empty | JSON body sent; or manual multipart Content-Type | Parse `json.loads(request.body)` or send FormData |
| Memory grows over session | Timers/observers/listeners retained | DevTools Memory → heap snapshots before/after swaps; "Detached" nodes filter |
| Clipboard throws | Not secure context / no user activation / permissions | `window.isSecureContext`; call from click handler |

### DevTools workflow
- **Sources**: breakpoints, conditional breakpoints, logpoints (no code edits), "Pause on caught/uncaught exceptions", DOM breakpoints (Elements → Break on subtree modification) to find who mutates a node, XHR/fetch breakpoints by URL substring, Event Listener breakpoints (e.g. all `submit`).
- **Network**: filter Fetch/XHR, inspect request headers (CSRF), response body, initiator stack, throttle to Slow 4G, "Disable cache".
- **Performance**: record an interaction, look for long tasks (red corners), forced reflow warnings, and the INP interaction track.
- **Memory**: take heap snapshot → perform swap cycle ×10 → snapshot → compare; search "Detached".
- **Console**: `monitorEvents(el, "click")`, `getEventListeners(el)` (Chrome), `$0` for selected element.

### Validation checklist
- Page works with JS disabled for core flows (forms submit, links navigate).
- No console errors on load and after each HTMX swap.
- Every unsafe fetch has CSRF; every fetch checks `ok`, handles abort and timeout.
- No `innerHTML` with non-constant strings (lint rule / grep).
- Listeners/timers/observers have cleanup paths; heap stable across 10 swaps.
- JS unit tests for `core/` with Vitest + Testing Library DOM ([frontend-testing.md](frontend-testing.md)).

## 11. Related references and skills

- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — event loop, rendering pipeline, progressive enhancement.
- [htmx-engineering.md](htmx-engineering.md) — server-driven interactions; HTMX events used for init/cleanup.
- [alpine-js-engineering.md](alpine-js-engineering.md) — declarative local state instead of hand-written DOM code.
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — race conditions, optimistic UI, toasts, dialogs.
- [frontend-security.md](frontend-security.md) — DOM XSS sinks, CSP nonces, storage security.
- [frontend-performance.md](frontend-performance.md) — INP, code splitting, lazy loading budgets.
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — serving/hashing modules, bundlers.
- [frontend-testing.md](frontend-testing.md) — Vitest/Testing Library and Playwright.
- [django-template-engineering.md](django-template-engineering.md) — `json_script`, `data-*` rendering.
- [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md), [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md).
- [urls-views.md](../../django-core/references/urls-views.md) — JSON views.

## 12. Official documentation and external references

- MDN JavaScript modules: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Modules
- MDN `<script type="importmap">`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/script/type/importmap
- MDN Fetch API: https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch
- MDN AbortController / AbortSignal: https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal
- MDN Event delegation / bubbling: https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Scripting/Event_bubbling
- MDN `<template>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/template
- MDN Web Storage API: https://developer.mozilla.org/en-US/docs/Web/API/Web_Storage_API
- MDN Clipboard API: https://developer.mozilla.org/en-US/docs/Web/API/Clipboard_API
- MDN IntersectionObserver: https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API
- MDN Memory management: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Memory_management
- WHATWG HTML scripting: https://html.spec.whatwg.org/multipage/scripting.html
- Django CSRF with AJAX: https://docs.djangoproject.com/en/5.2/howto/csrf/
- Django `json_script`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- Django static files: https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/
- web.dev Optimize long tasks: https://web.dev/articles/optimize-long-tasks
- web.dev Browser-level lazy loading: https://web.dev/articles/browser-level-image-lazy-loading
- Chrome DevTools docs: https://developer.chrome.com/docs/devtools
- OWASP DOM-based XSS Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html
- OWASP HTML5 Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
