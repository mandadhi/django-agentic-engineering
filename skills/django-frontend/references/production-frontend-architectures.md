# Production Frontend Architectures

> **Scope:** Eight reference architectures for Django frontends — server-rendered business app, Django + HTMX, Django + Alpine, Django + Tailwind dashboard, DRF + React SPA, hybrid templates + React islands, multi-tenant SaaS dashboard, admin/reporting UI — with a decision matrix and an escalation path. It composes the other django-frontend references and does not re-explain techniques; backend architecture (layers, services, modular monolith) belongs to [django-architecture](../../django-architecture/SKILL.md).

## 1. Purpose and scope

Use this reference at the **start** of frontend work, or when an existing frontend is being re-architected, to pick the simplest architecture that meets the requirements and to get a consistent blueprint (components, directory layout, rendering, state, auth, security, assets, performance, testing, deployment).

Principles:
- **Prefer the simplest architecture that satisfies the requirements.** Server-rendered HTML is the default; add HTMX, then Alpine, then React islands, then an SPA only when a named requirement forces it.
- Architectures compose: most real products are architecture 1 or 2 plus Tailwind (4), with one or two islands (6).
- Every architecture enforces authorisation on the server; frontend visibility is UX only.

## 2. Core concepts

Selection axes — score the product on these before choosing:

| Axis | Low end (favours server rendering) | High end (favours client rendering) |
|---|---|---|
| Interaction density | Pages, forms, lists, filters | Continuous manipulation: drag, canvas, live collaborative editing |
| Client state lifetime | Per request | Long-lived across views, offline, undo stacks |
| Latency tolerance | One round trip per action is fine | Sub-100 ms feedback without server round trip required |
| SEO / shareability | Public, indexable, link previews | Behind login, no indexing |
| API consumers | Only this web UI | Mobile apps, partners, multiple frontends |
| Team skills | Django/Python-centric | Dedicated frontend engineers with React experience |
| Release coupling | Frontend ships with backend | Independent frontend releases required |
| Accessibility risk budget | Native HTML controls suffice | Custom widgets (each one an a11y liability to test) |
| Operational budget | One deployable, one build | Two pipelines, CDN, API versioning |

Rendering styles used below:

| Style | Who renders HTML | Where state lives |
|---|---|---|
| MPA (multi-page) | Django per request | Server (DB, session), URL |
| MPA + HTMX | Django, full pages and fragments | Server, URL; tiny DOM state |
| MPA + Alpine | Django; Alpine adds behaviour | Server + component-local JS state |
| Islands | Django page; React renders sub-trees | Server + per-island React state |
| SPA | React in the browser from JSON | Client cache of server state + client UI state |

## 3. Internal mechanics

How a single user action ("mark task done") flows in each style:

| Style | Request | Response | Browser work |
|---|---|---|---|
| MPA | `POST /tasks/7/done/` form submit with `csrfmiddlewaretoken` | 302 → `GET /projects/1/` full page (PRG) | Full navigation, parse, style, paint; scroll reset unless restored |
| HTMX | `hx-post` with `HX-Request: true`, CSRF header via `hx-headers` | 200 HTML fragment (`<tr>`), optional `HX-Trigger` | Swap target, settle, re-process `hx-*` attributes in new content |
| Alpine | Usually none (pure client toggle) or `fetch` | JSON or none | Reactive update of bound DOM |
| Island | `fetch` POST JSON with `X-CSRFToken` | JSON (DRF) | React reconciles island subtree; rest of page untouched |
| SPA | Same as island, plus client router transitions without document loads | JSON | Client router renders route; cache invalidation updates other views |

Initial load:
- MPA/HTMX/Alpine: HTML arrives with content → fast LCP; JS is small and deferred; INP depends on server latency per interaction.
- Islands: HTML with content and fallback markup; island bundle loads and hydrates/mounts only where present.
- SPA: shell HTML → JS bundle download/parse/execute → API calls → render. LCP waits on JS + data unless pre-rendered.

Details: [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md), [htmx-engineering.md](htmx-engineering.md), [django-react-integration.md](django-react-integration.md).

## 4. Engineering decisions and trade-offs

### 4.1 Decision matrix

| Requirement | 1 SSR | 2 HTMX | 3 Alpine | 4 Tailwind dash | 5 SPA | 6 Islands | 7 SaaS | 8 Admin/report |
|---|---|---|---|---|---|---|---|---|
| Mostly forms/CRUD | ●●● | ●●● | ●● | ●●● | ● | ●● | ●●● | ●●● |
| Partial updates without reload | ● | ●●● | ●● | ●●● | ●●● | ●●● | ●●● | ●● |
| Rich client-only widgets | – | ● | ●● | ●● | ●●● | ●●● | ●● | ● |
| SEO / public pages | ●●● | ●●● | ●●● | ● | ● | ●●● | ●● | – |
| Multiple API clients | – | – | – | – | ●●● | ●● | ●● | – |
| Offline / long-lived client state | – | – | ● | – | ●●● | ●● | – | – |
| Small Python-only team | ●●● | ●●● | ●●● | ●●● | ● | ●● | ●● | ●●● |
| Operational simplicity | ●●● | ●●● | ●●● | ●●● | ● | ●● | ●● | ●●● |

(●●● strong fit, ● weak, – poor.) 7 and 8 are product shapes usually *built from* 2/4 (+6).

### 4.2 Prefer X when / prefer Y when

| Prefer | When |
|---|---|
| 1 Server-rendered | Content and forms; low interactivity; strongest a11y/SEO defaults. |
| 2 HTMX | 1 plus inline edit, live filters, pagination without reload, modals with server forms. |
| 3 Alpine | 1/2 plus client-only UI behaviour (menus, tabs, disclosure, client-side form niceties). |
| 4 Tailwind dashboard | Data-dense internal UI needing a consistent design system fast; combine with 2/3. |
| 5 DRF + React SPA | Highly interactive app, offline/real-time, multiple API consumers, dedicated frontend team. |
| 6 Hybrid islands | Mostly 1/2, but a few screens need React-level state (board, editor, chart builder). |
| 7 Multi-tenant SaaS | Tenant isolation, branding, roles, billing — choose rendering via 2/4 (+6). |
| 8 Admin/reporting | Staff-only data management and reports; start from Django admin, extend with templates/HTMX. |

### 4.3 Start simple, escalate

```text
1 Server-rendered templates
   └─ need partial updates? ───────────────► 2 + HTMX
        └─ need client-only UI behaviour? ──► 3 + Alpine (small, declarative)
             └─ one screen needs complex client state? ─► 6 React island on that screen
                  └─ most screens need it / other clients / offline ─► 5 SPA (topology A first)
```

Escalation triggers must be **named requirements** (e.g., "drag-and-drop reorder with undo", "works offline in the warehouse"), not "modern UX". De-escalation is legitimate: replacing an SPA screen that is just a form with a server-rendered page is a simplification, not a regression.

## 5. Django/frontend implementation patterns

Each architecture uses the same structure. Code for individual techniques lives in the linked references.

### 5.1 Architecture 1 — Server-rendered business app

| Aspect | Decision |
|---|---|
| Requirements | CRUD over domain objects, forms, lists with filters/pagination, reports; dozens to thousands of users; SEO for public parts. |
| Components | Django views (CBV/FBV), DTL templates with base layout + includes/partials, Django forms, minimal vanilla JS modules, one CSS bundle. |
| Rendering | Full-page SSR, Post/Redirect/Get, `{% querystring %}` (Django ≥ 5.1) for filter/pagination links. |
| State | Server (DB/session) + URL query params; messages framework for flash. |
| Authentication | Django session auth, `LoginRequiredMiddleware` (Django ≥ 5.1) or `login_required`; `login_not_required` for public views. |
| Security | Autoescaping, `{% csrf_token %}`, permission checks in views/querysets, CSP via django-csp (third-party) on 5.2. |
| Asset management | `STATICFILES_DIRS` + `ManifestStaticFilesStorage` via `STORAGES["staticfiles"]`, WhiteNoise (third-party) or CDN. |
| Performance | Query optimisation (`select_related`/`prefetch_related`), template fragment caching, small deferred JS, compressed CSS. |
| Testing | Rendering tests per view + permission matrix; few Playwright journeys. |
| Deployment | One container: gunicorn/uvicorn + WhiteNoise, `collectstatic` at build. |
| Trade-offs | + Simplest, accessible by default, SEO; − every interaction is a full navigation. |

```text
project/
├── config/settings/{base,dev,prod}.py
├── apps/projects/{models,views,forms,urls}.py
├── templates/
│   ├── base.html
│   ├── components/{_pagination,_form_field,_empty_state}.html
│   └── projects/{list,detail,form}.html
└── static/{css/app.css, js/app.js}
```

### 5.2 Architecture 2 — Django + HTMX

| Aspect | Decision |
|---|---|
| Requirements | Architecture 1 plus inline edits, live search, infinite lists, modal forms, polling status. |
| Components | Views returning full page or fragment; django-template-partials (third-party) on 5.2 (`{% partialdef %}` built in from Django 6.0); django-htmx (third-party) middleware; htmx 2.x (vendored, not CDN). |
| Rendering | SSR pages + HTML fragments; same URL serves both with `Vary: HX-Request`; `hx-boost` optional. |
| State | Server + URL (`hx-push-url`); events via `HX-Trigger` for cross-component refresh. |
| Authentication | Session; expired sessions answered with `HX-Redirect` to login, not a swapped login page. |
| Security | CSRF via `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'` on `<body>`; fragments escaped; `selfRequestsOnly` left on (htmx 2.x default); never swap untrusted HTML. |
| Asset management | htmx + small app JS in static; no bundler required. |
| Performance | Small fragments, `hx-trigger="input changed delay:300ms"`, `hx-sync` to cancel stale requests, cache-friendly GET fragments. |
| Testing | HTMX request tests (headers, fragment vs page, `Vary`) + Playwright for swaps and focus. |
| Deployment | Same as 1. |
| Trade-offs | + SPA-like feel with one codebase; − focus/announcements after swaps must be handled; complex client state is awkward. |

```text
templates/projects/
├── detail.html            # {% partialdef task-table %} ... {% endpartialdef %}
├── _task_row.html
└── _task_form.html
apps/projects/views.py     # if request.htmx: render "detail.html#task-table"
static/vendor/htmx-2.x.min.js
```

### 5.3 Architecture 3 — Django + Alpine

| Aspect | Decision |
|---|---|
| Requirements | Server-rendered pages needing client-only behaviour: dropdowns, tabs, disclosure, client-side filtering of small lists, dependent selects without round trip. |
| Components | Alpine.js 3.x; `Alpine.data()` components registered in modules; `Alpine.store()` sparingly; often combined with HTMX. |
| Rendering | SSR; Alpine enhances existing markup (`x-cloak` to avoid flashes). |
| State | Component-local (`x-data`); initial data from `json_script`; `$persist` only for non-sensitive preferences. |
| Authentication | Session (unchanged). |
| Security | Standard Alpine build needs CSP `'unsafe-eval'`; use `@alpinejs/csp` build for strict CSP [VERIFY: CSP build capabilities]; never bind untrusted strings with `x-html`. |
| Asset management | Alpine via npm + small bundle, or vendored file; Alpine plugins (Focus for `x-trap`) as needed. |
| Performance | Tiny runtime; avoid large `x-data` objects and heavy `x-effect`. |
| Testing | DOM tests for registered components; Playwright for keyboard behaviour. |
| Deployment | Same as 1. |
| Trade-offs | + Declarative, no build required; − logic in attributes is hard to test at scale; prefer native HTML (`<details>`, `<dialog>`) before Alpine. |

```text
static/src/
├── main.js                # import Alpine; Alpine.data("taskFilters", taskFilters); Alpine.start()
└── components/{task-filters,user-menu}.js
templates/components/_user_menu.html   # x-data="userMenu"
```

### 5.4 Architecture 4 — Django + Tailwind dashboard

| Aspect | Decision |
|---|---|
| Requirements | Data-dense internal dashboard: KPI cards, tables, charts, filters, dark mode, consistent design system quickly. |
| Components | Tailwind CSS 4.x (CSS-first `@theme` tokens) via `@tailwindcss/cli` or `@tailwindcss/vite`; template component includes; HTMX for filters/pagination; optional Alpine; chart library or island for charts. |
| Rendering | SSR + HTMX fragments; charts from `json_script` data. |
| State | URL params for filters/date ranges (shareable dashboards); server for data. |
| Authentication | Session; role-based menu rendering + server permission checks. |
| Security | No dynamic class names from user input; escape chart labels; CSP. |
| Asset management | Tailwind scans `templates/` (v4 `@source "../templates"` if outside auto-detection); classes never built by string concatenation; built CSS into `static/` → `collectstatic`. |
| Performance | Single small CSS file; aggregate queries server-side; cache expensive KPI fragments; lazy-load chart panels with `hx-trigger="revealed"`. |
| Testing | Rendering tests per widget; visual regression on key dashboard states; axe incl. dark mode. |
| Deployment | Node only at build time (CI); runtime is Django only. |
| Trade-offs | + Fast consistent UI, tiny CSS; − class-heavy templates (extract components), Node in the build. |

```text
frontend/
├── package.json           # tailwindcss, @tailwindcss/cli
└── src/app.css            # @import "tailwindcss"; @source "../../templates"; @theme {...}
templates/
├── layouts/dashboard.html
└── components/{_kpi_card,_data_table,_chart_panel,_filter_bar}.html
static/dist/app.css        # build output (gitignored), collected by collectstatic
```

### 5.5 Architecture 5 — DRF + React SPA

| Aspect | Decision |
|---|---|
| Requirements | Highly interactive app behind login; long-lived client state; possibly offline/real-time; API shared with other clients; frontend team. |
| Components | DRF API (versioned, OpenAPI schema); React 19 + Vite; React Router (third-party); TanStack Query (third-party) for server state; form library optional. |
| Rendering | CSR. Topology A (same-origin) first; B (cross-origin) only if required. |
| State | Server state in query cache; UI state local; URL for navigation/filters; no global store for server data. |
| Authentication | Session cookie + CSRF (same-site); tokens only for non-browser clients. |
| Security | CSRF header on unsafe methods; explicit CORS origins (B); no secrets in `VITE_*`; DRF permissions + object-level checks; CSP. |
| Asset management | Vite hashed assets; `index.html` `no-cache`; assets `immutable`. |
| Performance | Route-level code splitting; parallel data loading; bundle budgets; skeletons sized to avoid CLS. |
| Testing | DRF API tests; Vitest + RTL + MSW; Playwright E2E. |
| Deployment | A: Django serves shell + catch-all, or proxy routes `/api` → Django. B: CDN for SPA + API service. |
| Trade-offs | + Rich UX, reusable API; − two codebases/pipelines, SEO weak, accessibility of routing/focus is on you, API versioning. |

```text
backend/                       frontend/
├── config/                    ├── vite.config.js      # server.proxy /api → :8000
├── api/                       ├── src/
│   ├── v1/{routers,urls}.py   │   ├── api/client.js   # CSRF, abort, errors
│   └── ...                    │   ├── routes/          # lazy route modules
└── apps/<domain>/             │   ├── features/<domain>/
                               │   └── main.jsx
                               └── e2e/
```

### 5.6 Architecture 6 — Hybrid templates + React islands

| Aspect | Decision |
|---|---|
| Requirements | Mostly server-rendered product (1/2/4) with a few complex widgets: kanban, rich editor, interactive chart builder, spreadsheet grid. |
| Components | Django templates; Vite multi-entry build (one entry per island); django-vite (third-party) tags or manifest lookup; DRF endpoints only for island data. |
| Rendering | SSR page with meaningful fallback in the mount element; React mounts on `[data-island]`. |
| State | Initial props via `json_script`; island-local state; cross-island or island↔HTMX communication via DOM `CustomEvent`s. |
| Authentication | Page session; islands reuse cookie + `csrftoken`. |
| Security | `json_script` for props (never `|safe` in `<script>`); same DRF permission rules as views. |
| Asset management | Vite `build.manifest` (`.vite/manifest.json` in Vite ≥ 5 [VERIFY: manifest path for the Vite major in use]); shared vendor chunk; load island bundles only on pages that use them. |
| Performance | Islands lazy-mounted when visible; React shipped only to pages needing it. |
| Testing | Rendering test asserts mount node + props; component tests per island; Playwright per island page. |
| Deployment | One Django deploy; Node at build time. |
| Trade-offs | + Incremental, SEO-safe, React only where it pays; − two rendering models, props contract to keep in sync. |

```text
frontend/src/islands/{board.jsx, editor.jsx}     # entries
frontend/src/features/board/...
templates/projects/board.html   # {{ props|json_script:"board-props" }} <div data-island="board">
```

### 5.7 Architecture 7 — Multi-tenant SaaS dashboard

| Aspect | Decision |
|---|---|
| Requirements | Many customer organisations; strict data isolation; roles; per-tenant branding; billing/settings; audit. |
| Components | Usually 2 + 4 (+ 6 for one or two rich screens); tenant resolution middleware (subdomain or path); context processor exposing tenant + membership; theming via CSS custom properties. |
| Rendering | SSR + HTMX; tenant branding injected as tokens (`<style>:root{--brand: …}</style>` from validated values). |
| State | Server + URL; current tenant from host/path, never from a client-editable field alone. |
| Authentication | Session; membership/role checked per request; optional SSO (third-party). Tenant switcher re-validates membership. |
| Security | Every queryset scoped by tenant (manager/service layer); cache keys include tenant; `Vary` on host if cached; brand colours/logos validated (no arbitrary CSS); uploaded logos served safely. See [multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md). |
| Asset management | Shared static bundle for all tenants; tenant assets (logos) in media storage, not static. |
| Performance | Per-tenant fragment caching keyed by tenant+role; avoid per-tenant CSS builds. |
| Testing | Cross-tenant isolation tests (tenant B data never rendered/served for A); E2E per role; visual check of branding contrast. |
| Deployment | Single app, wildcard TLS for subdomains; feature flags per tenant. |
| Trade-offs | + One codebase serves all; − isolation bugs are critical; branding vs contrast/accessibility tension (validate contrast of tenant colours). |

```text
apps/tenancy/{middleware,context_processors,models}.py
templates/
├── layouts/app.html          # tenant switcher, role-aware nav
└── settings/{branding,members,billing}.html
static/src/app.css            # tokens: var(--brand-500) with safe fallback
```

### 5.8 Architecture 8 — Admin / reporting UI

| Aspect | Decision |
|---|---|
| Requirements | Staff-only data management, bulk actions, imports/exports, scheduled and ad hoc reports, audit trail. |
| Components | Django admin (customised `ModelAdmin`, actions, filters) for CRUD; custom staff views (templates + HTMX) for workflows and reports; background tasks for heavy exports; CSV/XLSX/PDF generation server-side. |
| Rendering | SSR; report filters as GET forms; HTMX for long-running job status polling. |
| State | URL params (shareable report links); job state in DB. |
| Authentication | Staff session; `is_staff` + granular permissions; MFA (third-party) recommended; admin on non-obvious path is not a control. |
| Security | Permission per report/action; export endpoints authorised and logged; CSV formula injection mitigation; rate-limit exports. |
| Asset management | Admin's own static + small custom CSS/JS via `ModelAdmin.Media` or template overrides. |
| Performance | Paginate and `list_select_related`; avoid `count()` on huge tables (custom paginator); move exports to background jobs; read replica for reports where available. |
| Testing | Admin view tests (changelist, actions), report rendering tests with fixed data, permission tests. |
| Deployment | Same deploy as main app, or separate internal hostname behind VPN/SSO. |
| Trade-offs | + Massive leverage from Django admin; − admin UX customisation hits limits — build custom staff views rather than fighting admin internals. |

```text
apps/billing/admin.py          # InvoiceAdmin: list_filter, actions=[export_csv]
apps/reports/{views,queries,exports}.py
templates/admin/billing/invoice/change_list.html   # minimal override
templates/reports/{index,revenue,_job_status}.html
```

### 5.9 Composing: a typical production stack

```python
# config/settings/base.py (excerpt; architecture 2 + 4 + 6)
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "django_htmx",            # third-party
    "template_partials",      # third-party (Django 5.2; built-in partials from 6.0)
    "django_vite",            # third-party, islands only [VERIFY: settings shape for installed version]
    "apps.tenancy", "apps.projects", "apps.billing",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # third-party
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",  # Django ≥ 5.1
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",                 # django-csp (third-party) on 5.2
    "django_htmx.middleware.HtmxMiddleware",
    "apps.tenancy.middleware.TenantMiddleware",
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
```

```django
{# templates/layouts/app.html #}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}{{ tenant.name }}{% endblock %}</title>
  <link rel="stylesheet" href="{% static 'dist/app.css' %}">
  <script src="{% static 'vendor/htmx.min.js' %}" defer></script>
  {% block head_scripts %}{% endblock %}
</head>
<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
  <a class="skip-link" href="#main">Skip to content</a>
  {% include "components/_app_nav.html" %}
  <main id="main" tabindex="-1">{% block content %}{% endblock %}</main>
  <div id="toasts" role="status" aria-live="polite"></div>
</body>
</html>
```

## 6. Production use cases

| Product type | Architecture | Why |
|---|---|---|
| Internal ops tool (inventory, approvals) | 1 + 2 (+ 8 for staff admin) | Forms and lists; Python team; no SEO concerns but simplicity wins. |
| B2B SaaS (projects, invoices, tenants) | 7 built on 2 + 4, one island (6) for a planning board | Isolation and roles dominate; rich UI only where needed. |
| Analytics/BI dashboard | 4 + 2, charts as islands (6) if interactive drill-down/cross-filtering | URL-driven filters, cached aggregates. |
| E-commerce storefront | 1 + 2 + 3 | SEO, fast LCP, cart interactions via HTMX/Alpine. |
| Content/marketing + docs | 1 | Static-like pages; cache aggressively. |
| Collaborative editor / design tool | 5 (topology A) | Long-lived client state, real-time, undo. |
| Field app with offline capture | 5 with service worker | Offline is a hard requirement. |
| Public API + web + mobile | 5 or 6 over a versioned DRF API | Multiple clients justify the API investment. |
| Back-office reporting | 8 | Admin + custom report views, background exports. |

## 7. Advantages and disadvantages

| Architecture | Advantages | Disadvantages |
|---|---|---|
| 1 SSR | Simplest; fast first paint; SEO; accessible defaults; one deploy. | Full reloads; limited rich interaction. |
| 2 HTMX | Rich-feeling UX, one language, small JS. | Focus/announcement management after swaps; fragment sprawl without partials discipline. |
| 3 Alpine | Small, declarative, no build needed. | CSP `unsafe-eval` with standard build; logic in markup. |
| 4 Tailwind dash | Consistent design fast; tiny production CSS. | Verbose templates; Node build step; class scanning pitfalls. |
| 5 SPA | Richest interaction; API reuse; independent releases. | Highest cost: two stacks, auth/CSRF/CORS complexity, SEO, bundle/INP risk. |
| 6 Islands | React where it pays; SEO intact; incremental. | Two rendering models; props/API contracts. |
| 7 SaaS | One codebase for many tenants. | Isolation bugs are severe; theming vs accessibility. |
| 8 Admin/report | Huge leverage from Django admin. | Admin customisation ceiling; heavy queries. |

## 8. Security and performance considerations

### Security (all architectures)
- Authorisation in views/querysets/DRF permissions; hidden buttons and disabled inputs are never controls.
- CSRF everywhere cookies authenticate: forms (`{% csrf_token %}`), HTMX (`hx-headers`), fetch (`X-CSRFToken`).
- Output encoding: DTL autoescape, `json_script` for data into JS, React text escaping; review every `|safe`, `mark_safe`, `x-html`, `dangerouslySetInnerHTML`.
- CSP: django-csp (third-party) on 5.2; built-in from Django 6.0 [VERIFY: 6.0 setting names]. Alpine standard build and inline handlers constrain CSP choices.
- No secrets in bundles (`VITE_*` is public). Vendor or pin third-party scripts; SRI for CDN scripts.
- Multi-tenant: tenant scoping is a backend invariant, tested. See [frontend-security.md](frontend-security.md) and [django-security-engineering](../../django-security-engineering/SKILL.md).

### Performance (all architectures)
- Measure Core Web Vitals in the field: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (75th percentile).
- SSR styles: server latency dominates INP — optimise queries and cache fragments ([django-application-performance.md](../../django-performance/references/django-application-performance.md)).
- Client styles: JS bundle size and main-thread work dominate — code split, budget, defer.
- Static delivery: hashed filenames + long `Cache-Control`, compression, CDN ([http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md)).
- See [frontend-performance.md](frontend-performance.md).

## 9. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| Starting with an SPA "because it's modern" | Start at 1/2; escalate on named requirements (§4.3). |
| Rebuilding the whole app in React for one complex screen | Island (6) for that screen. |
| HTMX + Alpine + React all on the same widget | One owner per DOM subtree; communicate via events. |
| Global client store mirroring server data | Server state in the server (MPA) or a query cache (SPA). |
| Cross-origin SPA when same-origin would work | Topology A: no CORS, simpler cookies. |
| Tenant from a hidden form field | Resolve tenant from host/path + membership on every request. |
| Per-tenant CSS builds | Shared CSS + validated CSS custom properties. |
| Fighting Django admin internals for complex workflows | Custom staff views with templates + HTMX. |
| Exports generated synchronously in the request | Background job + HTMX polling of job status. |
| Ignoring focus after HTMX swaps / SPA navigation | Move focus and announce via live region. |
| Tailwind classes assembled from strings | Map full class names in Python/templates. |
| No architecture decision recorded | Write an ADR ([architecture-decision-records.md](../../django-architecture/references/architecture-decision-records.md)). |

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| App "feels slow" on every click (SSR/HTMX) | Server latency, N+1 queries | Django Debug Toolbar/profiling; TTFB in DevTools. |
| SPA slow first load | Large bundle, waterfall of API calls | Bundle analyzer; Network waterfall; LCP element timing. |
| Cross-tenant data visible | Unscoped queryset or tenant-agnostic cache key | Isolation tests; cache key inspection. |
| HTMX fragments cached as full pages (or vice versa) | Missing `Vary: HX-Request` | Response headers; CDN cache rules. |
| Island never mounts | Bundle not loaded on page / wrong manifest path | Network tab; manifest location [VERIFY: django-vite manifest_path setting vs Vite output path]; console errors. |
| Styles missing in production only | Tailwind didn't scan templates; `collectstatic` not run | Built CSS contains the class; storage manifest. |
| Admin changelist times out | Count on huge table / missing `list_select_related` | SQL log; custom paginator. |
| Architecture keeps growing JS | Escalation without requirements | Revisit §4.3; record ADR. |

Validation before committing to an architecture:
1. Write the requirement list and score §2 axes; record in an ADR.
2. Prototype the hardest screen in the simplest candidate (often HTMX) and measure INP/LCP.
3. Confirm auth/CSRF/CSP compatibility of the chosen stack (Alpine build, inline scripts, CORS).
4. Confirm the test plan per layer ([frontend-testing.md](frontend-testing.md)).
5. Confirm deployment: one artifact or two; cache headers; rollback path.

## 11. Related references and skills

- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — SSR vs CSR, selection criteria.
- [django-template-engineering.md](django-template-engineering.md) · [htmx-engineering.md](htmx-engineering.md) · [alpine-js-engineering.md](alpine-js-engineering.md) · [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) · [django-react-integration.md](django-react-integration.md)
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) · [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) · [django-forms-and-validation.md](django-forms-and-validation.md)
- [frontend-security.md](frontend-security.md) · [frontend-performance.md](frontend-performance.md) · [frontend-accessibility.md](frontend-accessibility.md) · [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) · [frontend-testing.md](frontend-testing.md)
- [django-architecture](../../django-architecture/SKILL.md): [architectural-styles.md](../../django-architecture/references/architectural-styles.md), [modular-monolith.md](../../django-architecture/references/modular-monolith.md), [architecture-decision-records.md](../../django-architecture/references/architecture-decision-records.md)
- [django-rest-framework](../../django-rest-framework/SKILL.md) — API design for architectures 5 and 6.
- [django-security-engineering multi-tenant-security.md](../../django-security-engineering/references/multi-tenant-security.md) · [django-performance performance-architecture-decisions.md](../../django-performance/references/performance-architecture-decisions.md) · [django-core production-configuration.md](../../django-core/references/production-configuration.md)

## 12. Official documentation and external references

- Django templates: https://docs.djangoproject.com/en/5.2/topics/templates/
- Django static files: https://docs.djangoproject.com/en/5.2/howto/static-files/ and deployment https://docs.djangoproject.com/en/5.2/howto/static-files/deployment/
- Django admin: https://docs.djangoproject.com/en/5.2/ref/contrib/admin/
- Django auth (`LoginRequiredMiddleware`): https://docs.djangoproject.com/en/5.2/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware
- Django deployment checklist: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
- htmx documentation: https://htmx.org/docs/
- Alpine.js: https://alpinejs.dev/start-here and CSP build https://alpinejs.dev/advanced/csp
- Tailwind CSS: https://tailwindcss.com/docs/installation
- React: https://react.dev/learn/creating-a-react-app and https://react.dev/reference/react-dom/client/createRoot
- Vite backend integration: https://vite.dev/guide/backend-integration
- web.dev rendering on the web: https://web.dev/articles/rendering-on-the-web
- web.dev Core Web Vitals: https://web.dev/articles/vitals
- OWASP Cheat Sheet Series (index; see Authorization, CSRF, XSS sheets): https://cheatsheetseries.owasp.org/
- W3C WCAG 2.2: https://www.w3.org/TR/WCAG22/
