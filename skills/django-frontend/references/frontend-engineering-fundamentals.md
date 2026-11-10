# Frontend Engineering Fundamentals

> **Scope:** How browsers fetch, parse, render and run a Django-served UI, and how to choose a frontend architecture (server-rendered, HTMX, Alpine, islands, SPA) from requirements. It does not cover template mechanics ([django-template-engineering.md](django-template-engineering.md)), detailed performance tuning ([frontend-performance.md](frontend-performance.md)) or the full reference architectures ([production-frontend-architectures.md](production-frontend-architectures.md)).

## 1. Purpose and scope

Use this reference when an agent must:

- Explain or reason about *why* a page is slow, janky, flashes unstyled content, or behaves differently on back/forward navigation.
- Decide where rendering happens (server, client, both) and how much JavaScript a feature justifies.
- Pick between plain Django templates, HTMX, Alpine.js, React islands, or a full SPA — and defend the choice.
- Set correct browser-facing HTTP behaviour (caching, cookies, `Vary`) for Django responses.
- Check browser support for a platform feature before relying on it.

Out of scope (one-line summaries + links): the Django request/response lifecycle on the server → [request-response-lifecycle.md](../../django-core/references/request-response-lifecycle.md); server-side caching → [caching-engineering.md](../../django-performance/references/caching-engineering.md); cookie/session hardening → [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md).

## 2. Core concepts

| Concept | One-line definition | Why an agent cares |
|---|---|---|
| Navigation | Browser loads a new document for a URL (link click, form submit, address bar, `location.assign`) | Full page lifecycle: unload old doc, fetch, parse, render; history entry created |
| Subresource | CSS, JS, images, fonts, `fetch()`/XHR loaded *by* a document | Prioritised and cached individually; can block rendering |
| DOM | Live tree of nodes built from HTML; scripts mutate it | Everything HTMX/Alpine/React does is a DOM mutation |
| CSSOM | Parsed stylesheet rules | Rendering needs DOM + CSSOM, so CSS is render-blocking |
| Render tree / layout tree | Visible elements with computed styles | `display:none` elements are absent; `visibility:hidden` occupy space |
| Critical rendering path | Bytes → DOM/CSSOM → style → layout → paint → composite | Shortening it improves FCP/LCP |
| Event loop | Single main thread runs tasks, microtasks and rendering steps | A long task blocks input → poor INP |
| Progressive enhancement | Baseline works with HTML + server; JS layers improve it | Resilience, accessibility, SEO by default |
| Graceful degradation | Build the rich version; ensure acceptable fallback | Used when the core experience *is* the rich interaction |
| SSR / CSR / hybrid | Where HTML is produced: server, client, or both | Drives SEO, TTFB/LCP, complexity, team skills |
| Islands | Mostly server HTML with isolated interactive widgets hydrated independently | Pay JS cost only where interactivity exists |
| Baseline | web-features status: *Newly available* (all core browsers) / *Widely available* (~30 months later) | Quick "can I ship this without fallback?" signal |

### HTML, CSS and JS responsibilities

- **HTML**: content, structure, semantics, native behaviour (links navigate, forms submit, `<dialog>` traps focus when modal, `<details>` toggles). See [html-and-semantic-ui.md](html-and-semantic-ui.md).
- **CSS**: presentation, layout, responsive behaviour, state styling (`:hover`, `:focus-visible`, `:invalid`, `:has()`), motion. See [css-engineering.md](css-engineering.md).
- **JS**: behaviour the platform does not provide declaratively — partial updates, client state, rich widgets. See [javascript-engineering.md](javascript-engineering.md).

Rule: push work to the lowest layer that can do it. A `<details>` beats a JS accordion; a CSS `:has()` rule beats a class toggled by JS; a server redirect beats client routing.

### Component-based UI and design systems (overview)

A *component* is a unit of markup + style + behaviour with a defined interface (inputs, slots, states). In Django this is an `{% include %}` with `only`, an inclusion tag, or a partial; in React it is a function component. A *design system* is the shared vocabulary above components: tokens (colour, type, spacing), component specs, states (loading/empty/error/disabled), accessibility rules and usage guidance. Detail lives in [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) and [django-template-engineering.md](django-template-engineering.md).

### Responsive design (overview)

Mobile-first CSS, fluid layouts (grid/flex, `clamp()`), media queries for viewport, container queries for components, responsive images (`srcset`/`sizes`), touch-target sizing (WCAG 2.2 2.5.8, 24×24 CSS px minimum). Always include `<meta name="viewport" content="width=device-width, initial-scale=1">`. Detail: [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md).

## 3. Internal mechanics

### 3.1 Browser architecture (Chromium model; Firefox/Safari are analogous)

| Process | Responsibility |
|---|---|
| Browser process | UI chrome, navigation coordination, permissions, storage access |
| Network service (utility process) | DNS, connections, HTTP cache, cookies jar enforcement |
| Renderer process (per site, site isolation) | HTML parser, CSS engine, JS engine (V8), layout, paint recording; one **main thread** plus compositor and raster threads |
| GPU process | Rasterisation and compositing to screen |

Consequences: a cross-site iframe runs in another renderer (cannot share JS state); a busy main thread freezes input handling but compositor-only animations (`transform`, `opacity`) can keep running.

### 3.2 Navigation lifecycle (browser view)

1. **Input** — URL typed / link clicked / form submitted. `beforeunload` may fire on the old page.
2. **Connection** — DNS → TCP + TLS (HTTP/1.1, HTTP/2) or QUIC (HTTP/3). Reused connections skip this. `<link rel="preconnect">` warms it early.
3. **Request** — method, URL, headers (`Cookie`, `Accept`, `Accept-Language`, `Sec-Fetch-*`, `Origin` on cross-origin/POST), body.
4. **Response** — status + headers. `3xx` → follow `Location`. `Content-Type: text/html` → commit navigation to a renderer. `Content-Disposition: attachment` → download, no navigation.
5. **Commit** — new document created, history entry pushed (or replaced), old document unloaded or placed in **bfcache** (back/forward cache).
6. **Parse & render** — streaming: the parser starts before the full body arrives (so Django `StreamingHttpResponse` or early flushing can help TTFB→FCP, though most Django pages render fully first).
7. **Load events** — `DOMContentLoaded` (DOM parsed, deferred scripts run) → `load` (subresources done).

Form POST → Django returns `302` → browser GETs the new URL (**Post/Redirect/Get**). This prevents duplicate submissions on refresh and is why Django class-based `FormView` redirects on success.

### 3.3 Subresources and the parser

- The HTML parser builds the DOM incrementally. A **classic `<script>`** without `defer`/`async` stops parsing until it is fetched and executed (it might call `document.write`).
- **CSS is render-blocking** (no paint until CSSOM ready) and **script-blocking** (a classic script after a stylesheet waits for it, because the script may read styles).
- A **preload scanner** looks ahead in the raw HTML to start fetching `src`/`href` resources early — resources injected by JS are invisible to it.

| Script form | Fetch | Execute | Order preserved |
|---|---|---|---|
| `<script src>` | Blocks parser | Immediately | Yes |
| `<script defer src>` | Parallel | After parse, before `DOMContentLoaded` | Yes |
| `<script async src>` | Parallel | As soon as fetched (may interrupt parse) | No |
| `<script type="module" src>` | Parallel (+ dependency graph) | Deferred by default | Yes |
| `<script type="module" async>` | Parallel | As soon as graph ready | No |

Default for Django pages: `defer` (or `type="module"`) for app code; `async` only for independent scripts such as analytics.

### 3.4 Rendering pipeline

```text
HTML ─▶ DOM ─┐
             ├─▶ Style (computed styles) ─▶ Layout (geometry) ─▶ Paint (display lists)
CSS ─▶ CSSOM ┘                                                    │
                                                                  ▼
                                              Layerize ─▶ Raster (GPU) ─▶ Composite ─▶ Screen
```

| Change | Triggers | Cost |
|---|---|---|
| Geometry: `width`, `top`, `font-size`, adding nodes, changing text | Style → **Layout (reflow)** → Paint → Composite | Highest; can cascade to ancestors/siblings |
| Paint-only: `color`, `background`, `box-shadow` | Style → **Paint (repaint)** → Composite | Medium |
| Compositor-only: `transform`, `opacity` (on a promoted layer) | Composite | Lowest; can run off main thread |

**Layout thrashing** (forced synchronous layout): interleaving DOM writes with geometry reads (`offsetHeight`, `getBoundingClientRect()`, `scrollTop`, `getComputedStyle`) forces layout on every read.

```js
// Bad: read/write interleaved → N forced layouts
rows.forEach((row) => {
  row.style.height = `${row.offsetHeight + 10}px`;
});

// Good: batch reads, then batch writes in the next frame
const heights = rows.map((row) => row.offsetHeight);
requestAnimationFrame(() => {
  rows.forEach((row, i) => { row.style.height = `${heights[i] + 10}px`; });
});
```

`content-visibility: auto` lets the browser skip rendering off-screen sections (check Baseline status before relying on it [VERIFY: Baseline status of content-visibility]); `contain: layout paint` limits reflow scope.

### 3.5 Event loop

```text
loop:
  run one task (macrotask): parser chunk, event callback, setTimeout, message, network callback
  drain ALL microtasks: Promise reactions, queueMicrotask, MutationObserver
  if it is time to render (~every 16.7 ms at 60 Hz):
      run requestAnimationFrame callbacks
      style → layout → paint → composite
      (ResizeObserver / IntersectionObserver callbacks are tied to this step)
```

- **Long task**: any task > 50 ms. Input arriving during it waits → poor **INP** (good ≤ 200 ms at p75).
- Microtasks run to completion before rendering: an infinite promise chain freezes the page just like a `while(true)`.
- Break long work up: `await scheduler.yield()` where supported [VERIFY: scheduler.yield() Baseline status], otherwise `await new Promise(r => setTimeout(r, 0))`.
- `requestAnimationFrame` is for visual updates synced to the next frame, not for general deferral.
- HTMX swaps, Alpine reactivity and React commits all run on this same main thread.

### 3.6 HTTP caching from the browser's view

| Header (response) | Browser behaviour |
|---|---|
| `Cache-Control: max-age=N` | Fresh for N s; reused without a request |
| `Cache-Control: no-cache` | Stored, but revalidated every time (`If-None-Match`/`If-Modified-Since` → `304`) |
| `Cache-Control: no-store` | Not stored at all (also may make the page bfcache-ineligible [VERIFY: current Chrome bfcache policy for no-store]) |
| `Cache-Control: private` | Browser may cache; shared caches (CDN) must not |
| `Cache-Control: public, max-age=31536000, immutable` | For fingerprinted static files only |
| `ETag` / `Last-Modified` | Validators enabling `304 Not Modified` |
| `Vary: Cookie, HX-Request` | Cache key includes these request headers |

Django tools: `@cache_control(...)`, `@never_cache`, `@vary_on_headers(...)`, `@condition(etag_func=..., last_modified_func=...)`, `ConditionalGetMiddleware`. Authenticated HTML: `private, no-cache` (or `never_cache`) — never `public`. Static files: hashed names via `ManifestStaticFilesStorage` + long `max-age` — see [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md).

### 3.7 Cookies from the browser's view

- The browser attaches cookies matching domain/path/`Secure`/`SameSite` to every request — including subresources and `fetch()` (same-origin default `credentials: 'same-origin'`).
- `SameSite=Lax` (Django's default for `SESSION_COOKIE_SAMESITE` and `CSRF_COOKIE_SAMESITE`): sent on top-level GET navigations from other sites, not on cross-site POST/iframes/fetch.
- `HttpOnly`: invisible to `document.cookie`. Django's session cookie is `HttpOnly` by default; the CSRF cookie is not (`CSRF_COOKIE_HTTPONLY` default `False`) so JS can read it for headers.
- Cookies are not a client-side store for app data; see [frontend-security.md](frontend-security.md).

### 3.8 Client-server communication patterns

| Pattern | Mechanism | Django side |
|---|---|---|
| Full navigation | `<a href>`, `<form>` | Normal views, PRG |
| Fragment swap | HTMX `fetch` → HTML partial | Same view returns partial when `HX-Request` present, `Vary: HX-Request` |
| JSON fetch | `fetch()` → JSON | `JsonResponse` or DRF |
| Server push | SSE (`EventSource`) / WebSocket | ASGI view streaming / Channels (third-party) |
| Polling | `hx-trigger="every 10s"` / `setInterval` | Cheap cached view |

## 4. Engineering decisions and trade-offs

### 4.1 Architecture selection framework

Score the feature/app against these criteria before choosing:

| Criterion | Questions |
|---|---|
| Requirements | Mostly CRUD/forms/reports? Or a canvas/editor/real-time collaborative tool? |
| Complexity of client state | Does state live on the server (DB) or mostly in the browser between saves? |
| Interactivity | Per-interaction latency tolerance; number of interdependent widgets on one screen |
| Offline needs | Must it work without network (service worker, local DB, sync)? |
| SEO / shareability | Public, indexable pages? Link previews? |
| Team skills | Python/Django-heavy team vs dedicated frontend engineers |
| Operational overhead | One deployable vs separate build, CDN, API versioning, CORS, auth tokens |

### 4.2 Decision table

| Choose | When | Avoid when |
|---|---|---|
| **Server-rendered Django templates** | CRUD, admin, reports, content, forms; SEO matters; small team | Sub-100 ms interactions on many widgets |
| **+ HTMX (HTMX 2.x)** | Partial updates (inline edit, filters, pagination, modals, infinite scroll) with server as source of truth | Heavy client-only state, offline, drag-heavy canvases |
| **+ Alpine.js (3.x)** | Local UI state: toggles, menus, tabs, client-side filtering of small lists, form niceties | Complex shared state across the page, large data |
| **Islands (React 19 / other) in templates** | One or a few rich widgets (scheduler, chart builder, rich editor) inside an otherwise server-rendered app | Most of the screen is the island — then consider SPA |
| **SPA (React + DRF)** | App-like workflows with lots of client state, offline/PWA, multiple clients sharing an API, dedicated FE team | SEO-critical pages, small team, simple CRUD |

Defaults: start server-rendered; add HTMX for partial updates; add Alpine for local state; add an island only for a widget that genuinely needs a component framework. Escalate to SPA only with an explicit justification recorded as an ADR ([architecture-decision-records.md](../../django-architecture/references/architecture-decision-records.md)).

### 4.3 SSR vs CSR vs hybrid

| Prefer SSR (Django templates) when… | Prefer CSR when… | Prefer hybrid/islands when… |
|---|---|---|
| First load speed and LCP matter | Long sessions amortise initial JS cost | Most of the page is content/forms but one area is app-like |
| Content must be indexable without JS | Behind login, SEO irrelevant | You want SSR benefits and a rich widget |
| Server holds the truth (permissions, data) | Offline or heavy optimistic UI required | You can pass initial data with `json_script` |
| Small team, one codebase | Separate FE team and API consumers | Gradual migration from templates |

### 4.4 Progressive enhancement vs graceful degradation

| Prefer progressive enhancement when… | Prefer graceful degradation when… |
|---|---|
| The core task is forms/links/content (almost every Django app) | The core task *is* the rich interaction (drawing, maps, real-time editor) |
| Users are on unreliable networks/devices | Audience and browsers are controlled (internal tool) |
| Accessibility and SEO are requirements | A server fallback would duplicate significant logic |

### 4.5 Worked examples

1. **Invoice management for a 6-person Django team** (list, filter, create, PDF export, approval). Server state, forms, moderate interactivity, no offline, some SEO (none — behind login). → Django templates + HTMX for filters/pagination/inline status changes + Alpine for dropdowns. No build step beyond Tailwind CLI.
2. **Public marketing site + docs + pricing calculator.** SEO critical, interactivity confined to the calculator. → Server-rendered pages; calculator as an Alpine component (or a small React island if it already exists). Cache HTML at the CDN for anonymous users.
3. **Project-planning Gantt editor inside a SaaS dashboard.** Drag/resize, undo, keyboard shortcuts, dozens of interdependent bars; rest of app is CRUD. → Templates + HTMX for the app; a React island mounted on the Gantt page, fed initial data via `json_script`, saving via DRF endpoints with session auth + CSRF.
4. **Field-inspection app used offline on tablets, plus an iOS app later.** Offline sync, shared API, dedicated FE engineers. → DRF API + SPA/PWA (React). Accept the operational overhead (separate build, API versioning, auth). See [django-react-integration.md](django-react-integration.md).
5. **Multi-tenant analytics dashboard with live counters.** Server-owned data, many panels, periodic refresh. → Templates + HTMX polling/SSE per panel, chart library loaded lazily per panel (island-style). No SPA.

## 5. Django/frontend implementation patterns

### 5.1 Base layout that respects the critical rendering path

```django
{# templates/base.html #}
{% load static %}
<!doctype html>
<html lang="{{ LANGUAGE_CODE|default:'en' }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Dashboard{% endblock %} · Acme</title>
  {# Render-blocking by design: keep it small and fingerprinted #}
  <link rel="stylesheet" href="{% static 'css/app.css' %}">
  {# Non-blocking app JS; module scripts are deferred by default #}
  <script type="module" src="{% static 'js/app.js' %}"></script>
  <script defer src="{% static 'vendor/htmx.min.js' %}"></script>
  {% block head_extra %}{% endblock %}
</head>
<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
  <a class="skip-link" href="#main">Skip to main content</a>
  {% include "partials/site_header.html" %}
  <main id="main" tabindex="-1">{% block content %}{% endblock %}</main>
</body>
</html>
```

Notes: `hx-headers` on `<body>` sends the CSRF token on HTMX requests — detail and alternatives in [htmx-engineering.md](htmx-engineering.md). Self-host HTMX or pin and add SRI ([frontend-security.md](frontend-security.md)).

### 5.2 One URL, page or fragment, with correct caching

```python
# projects/views.py
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.vary import vary_on_headers

from .models import Task


@login_required
@vary_on_headers("HX-Request")            # browser/CDN must not mix page and fragment
@cache_control(private=True, no_cache=True)
def task_list(request, project_id):
    tasks = (
        Task.objects.filter(project_id=project_id, project__tenant=request.tenant)
        .select_related("assignee")
        .order_by("-updated_at")
    )
    page = Paginator(tasks, 25).get_page(request.GET.get("page"))
    template = (
        "projects/_task_rows.html"
        if request.headers.get("HX-Request")
        else "projects/task_list.html"
    )
    return render(request, template, {"page": page, "project_id": project_id})
```

`request.headers.get("HX-Request")` works without dependencies; django-htmx (third-party) exposes `request.htmx` via `HtmxMiddleware`.

### 5.3 Progressive enhancement: works without JS, better with HTMX

```django
{# projects/task_list.html #}
{% extends "base.html" %}
{% block content %}
<h1>Tasks</h1>
<form method="get" action="{{ request.path }}" role="search"
      hx-get="{{ request.path }}" hx-target="#task-rows" hx-push-url="true"
      hx-trigger="input delay:300ms, submit">
  <label for="q">Search tasks</label>
  <input id="q" name="q" type="search" value="{{ request.GET.q }}">
  <button type="submit">Search</button>   {# works with JS disabled #}
</form>
<table>
  <caption>Tasks for this project</caption>
  <thead><tr><th scope="col">Title</th><th scope="col">Assignee</th><th scope="col">Updated</th></tr></thead>
  <tbody id="task-rows">{% include "projects/_task_rows.html" %}</tbody>
</table>
{% endblock %}
```

Without JS: a normal GET navigation. With HTMX: only rows are swapped and the URL updated. The server applies identical filtering in both paths. (A `<form role="search">` can become the `<search>` element; see [html-and-semantic-ui.md](html-and-semantic-ui.md).)

### 5.4 Server data to an island

```django
{{ gantt_initial|json_script:"gantt-data" }}
<div id="gantt-root" data-endpoint="{% url 'api:task-list' project.pk %}">
  <p>Loading planner… <a href="{% url 'projects:task_list' project.pk %}">Use the list view</a></p>
</div>
<script type="module" src="{% static 'js/gantt-island.js' %}"></script>
```

```js
// static/js/gantt-island.js
const el = document.getElementById("gantt-root");
const data = JSON.parse(document.getElementById("gantt-data").textContent);
const { mountGantt } = await import("./gantt/mount.js"); // code-split; loaded only on this page
mountGantt(el, { data, endpoint: el.dataset.endpoint });
```

`json_script` escapes `<`, `>`, `&` so data cannot break out of the `<script>`. Never interpolate context into inline JS with `{{ }}`.

### 5.5 Feature detection and `@supports`

```css
/* Baseline-safe default first, enhancement second */
.card-grid { display: grid; grid-template-columns: 1fr; gap: 1rem; }

@supports (container-type: inline-size) {
  .card-grid { container-type: inline-size; }
  @container (min-width: 40rem) {
    .card { display: grid; grid-template-columns: 8rem 1fr; }
  }
}

@supports selector(:has(input:checked)) {
  .row:has(input:checked) { background: var(--color-surface-selected); }
}
```

```js
// JS feature detection — never user-agent sniffing
if (typeof HTMLDialogElement === "function") {
  // native <dialog>
}
if ("showPopover" in HTMLElement.prototype) {
  // Popover API available
}
if (CSS.supports("anchor-name: --a")) {
  // CSS anchor positioning [VERIFY: Baseline status before relying on it]
}
```

### 5.6 bfcache-friendly pages

- Do not register `unload` handlers; use `pagehide`/`visibilitychange`.
- Re-sync stale UI when restored:

```js
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    // Page restored from bfcache: refresh time-sensitive fragments
    document.querySelectorAll("[data-refresh-on-restore]").forEach((el) => {
      htmx.trigger(el, "refresh");
    });
  }
});
```

### 5.7 SEO essentials in Django

- Server-render indexable content; Google renders JS but with delay and budget limits — do not depend on it for public pages.
- Unique `<title>` and `<meta name="description">` per page via blocks; canonical URL; `django.contrib.sitemaps`; `robots.txt` view; correct status codes (`404` via `Http404`, `410` for removed, `301` for moved).
- Use real `<a href>` links for crawlable navigation (HTMX `hx-boost` keeps them as real links).
- Markup and Open Graph detail: [html-and-semantic-ui.md](html-and-semantic-ui.md).

## 6. Production use cases

| Use case | Architecture | Key fundamentals applied |
|---|---|---|
| Internal ops dashboard (tenants, invoices) | Templates + HTMX + Alpine | `Vary: HX-Request`, private caching, PRG, progressive enhancement |
| Public content + docs | Templates, CDN-cached anonymous HTML | SEO, canonical, bfcache, minimal JS |
| Scheduling widget in CRUD app | Island | `json_script`, dynamic import, feature detection |
| Offline field app | SPA/PWA + DRF | Service worker, CSR, token/session strategy |
| Live counters | HTMX polling or SSE | Event-loop budget, small fragments, cache headers on poll endpoint |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Server-rendered | Fast first paint, SEO, one codebase, security logic in one place, minimal JS | Full reloads for every interaction; harder rich widgets |
| HTMX | Partial updates with HTML over the wire; little JS; server remains source of truth | Many small endpoints/partials; client state awkward; network round-trip per interaction |
| Alpine | Tiny, declarative local state in templates | Standard build needs CSP `'unsafe-eval'`; logic in attributes gets unwieldy |
| Islands | Rich UX only where needed; SSR elsewhere | Two mental models; build pipeline; data hand-off |
| SPA | App-like UX, offline, API reuse | Larger JS, SEO work, separate deploy, CORS/CSRF/auth complexity, duplicated validation |

## 8. Security and performance considerations

**Security**

- Client-side checks (hidden buttons, `disabled`, HTML5 validation, route guards) are UX, never authorization. The Django view must enforce permissions and tenancy on every request, including HTMX fragments and JSON endpoints.
- Anything shipped to the browser (bundled env vars such as `VITE_*`, `json_script` payloads, HTML comments) is public.
- `fetch()` defaults to same-origin credentials; cross-origin needs CORS on the server *and* still CSRF protection for cookie-authenticated writes — CORS ≠ CSRF. Detail: [frontend-security.md](frontend-security.md), [cors-and-browser-security.md](../../django-security-engineering/references/cors-and-browser-security.md).
- Responses that differ by user must not be cached as `public` (CDN leaks). Use `private`/`no-cache` and `Vary`.

**Performance**

- Keep CSS small and fingerprinted (it blocks render); `defer`/module scripts; avoid parser-blocking scripts in `<head>`.
- Expose critical resources to the preload scanner (real `<img src>`/`<link>` in HTML, not JS-injected); preload the LCP image or set `fetchpriority="high"`.
- Avoid long tasks (> 50 ms); batch DOM reads/writes; animate `transform`/`opacity`.
- Targets: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 at p75. Detail: [frontend-performance.md](frontend-performance.md); backend TTFB: [django-application-performance.md](../../django-performance/references/django-application-performance.md).

## 9. Common mistakes and anti-patterns

1. **Defaulting to an SPA for a CRUD app.** → Start with templates + HTMX; record any SPA decision with criteria from §4.1.
2. **Same URL returns page or fragment without `Vary: HX-Request`.** → Add `@vary_on_headers("HX-Request")`; otherwise back button / cache shows a bare fragment.
3. **Classic `<script src>` in `<head>` without `defer`.** → Use `defer` or `type="module"`.
4. **Reading layout in a loop after writes (thrashing).** → Batch reads then writes; use `requestAnimationFrame`.
5. **Animating `top`/`left`/`height`.** → Animate `transform`/`opacity`; respect `prefers-reduced-motion`.
6. **`public` caching of authenticated HTML.** → `@never_cache` or `cache_control(private=True, no_cache=True)`.
7. **User-agent sniffing.** → Feature detection (`in`, `CSS.supports`, `@supports`).
8. **Using JS for things HTML/CSS do natively** (accordions, modals, tooltips via title-like hacks). → `<details>`, `<dialog>`, popover, CSS.
9. **Inline `{{ var }}` inside `<script>`.** → `json_script`.
10. **`unload` handlers / `Cache-Control: no-store` everywhere.** → Breaks bfcache; use `pagehide`; reserve `no-store` for sensitive pages.
11. **Relying on client-side rendering for public SEO pages.** → Server-render; verify with view-source, not DevTools Elements.
12. **Form POST returning 200 HTML on success.** → PRG (`redirect()`), or HTMX `HX-Redirect`/`HX-Location`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Back button shows raw fragment | Missing `Vary: HX-Request`, or `hx-push-url` on a fragment response | Response headers in DevTools Network; HTMX history config |
| Flash of unstyled content | CSS loaded late or injected by JS | Network waterfall; ensure `<link rel=stylesheet>` in `<head>` |
| Blank page until large JS loads | Parser-blocking script or CSR-only content | Performance panel; view-source shows content? |
| Input lag / poor INP | Long tasks on main thread | Performance panel → Long tasks; web-vitals attribution |
| Layout jumps (CLS) | Images without `width`/`height`, late-injected banners, web fonts | Layout Shift regions in Performance panel |
| Stale data after Back | bfcache restore | `pageshow` with `event.persisted`; Application → Back/forward cache test |
| CSRF 403 on fetch/HTMX | Header missing, cookie not sent cross-site | Request headers; `SameSite`; `CSRF_TRUSTED_ORIGINS` (scheme required) |
| Feature works in Chrome only | Not Baseline | MDN compat table / Baseline badge; add `@supports` fallback |
| Page not indexed | Content requires JS, `noindex`, wrong status | view-source, `curl -I`, Search Console URL inspection |

Validation steps:

```bash
# Headers a browser will see
curl -sI https://app.example.com/projects/1/tasks/ -H "Cookie: sessionid=..." | grep -iE "cache-control|vary|content-type"
# Fragment variant
curl -s https://app.example.com/projects/1/tasks/ -H "HX-Request: true" -H "Cookie: sessionid=..." | head
```

- Disable JavaScript (DevTools → Command menu → "Disable JavaScript") and complete the core tasks.
- Run Lighthouse and the Performance panel on a throttled profile; confirm no render-blocking scripts.
- Check every new CSS/JS feature against Baseline; add a fallback if only *Newly available* and the audience includes older devices.

## 11. Related references and skills

- [html-and-semantic-ui.md](html-and-semantic-ui.md), [css-engineering.md](css-engineering.md), [javascript-engineering.md](javascript-engineering.md)
- [htmx-engineering.md](htmx-engineering.md), [alpine-js-engineering.md](alpine-js-engineering.md), [django-react-integration.md](django-react-integration.md)
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md), [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md)
- [frontend-performance.md](frontend-performance.md), [frontend-security.md](frontend-security.md), [frontend-accessibility.md](frontend-accessibility.md)
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md), [production-frontend-architectures.md](production-frontend-architectures.md)
- [request-response-lifecycle.md](../../django-core/references/request-response-lifecycle.md), [caching.md](../../django-core/references/caching.md)
- [architectural-styles.md](../../django-architecture/references/architectural-styles.md), [architecture-decision-records.md](../../django-architecture/references/architecture-decision-records.md)
- [http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md)
- [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md)

## 12. Official documentation and external references

- Django cache decorators and conditional processing: https://docs.djangoproject.com/en/5.2/topics/cache/ · https://docs.djangoproject.com/en/5.2/topics/conditional-view-processing/
- Django `json_script`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- Django sitemaps: https://docs.djangoproject.com/en/5.2/ref/contrib/sitemaps/
- WHATWG HTML — event loops: https://html.spec.whatwg.org/multipage/webappapis.html#event-loops
- WHATWG HTML — scripting (`defer`/`async`/module): https://html.spec.whatwg.org/multipage/scripting.html
- MDN — How browsers work / critical rendering path: https://developer.mozilla.org/en-US/docs/Web/Performance/Critical_rendering_path
- MDN — HTTP caching: https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching
- MDN — Cookies / SameSite: https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies
- MDN — `@supports`: https://developer.mozilla.org/en-US/docs/Web/CSS/@supports
- MDN — Baseline: https://developer.mozilla.org/en-US/docs/Glossary/Baseline/Compatibility
- web.dev — Baseline: https://web.dev/baseline
- web.dev — Rendering on the web: https://web.dev/articles/rendering-on-the-web
- web.dev — bfcache: https://web.dev/articles/bfcache
- web.dev — Avoid large, complex layouts and layout thrashing: https://web.dev/articles/avoid-large-complex-layouts-and-layout-thrashing
- web.dev — Optimize long tasks: https://web.dev/articles/optimize-long-tasks
- htmx.org documentation: https://htmx.org/docs/
