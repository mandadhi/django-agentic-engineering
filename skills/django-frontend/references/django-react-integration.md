# Django + React Integration

> **Scope:** How a React 19 frontend talks to a Django backend: deployment topologies, session vs token auth from the client, CSRF/CORS/cookies from the SPA side, a production API client, loading/error/retry, DRF pagination consumption, routing, Vite dev proxy, env vars, builds and SPA hosting. API design (serializers, viewsets, error format, pagination classes) belongs to [django-rest-framework](../../django-rest-framework/SKILL.md); backend CSRF/CORS enforcement belongs to [django-security-engineering](../../django-security-engineering/SKILL.md).

## 1. Purpose and scope

Use this reference when a task involves React *and* Django in the same product. It answers:

- Which topology (same-origin SPA, cross-origin SPA, or React islands inside Django templates)?
- How does the browser authenticate, and how does CSRF work for `fetch` calls?
- What does a correct API client look like (CSRF, cancellation, error normalisation, retries)?
- How are routing, dev servers, env vars, builds and deployment wired?

It does **not** decide *whether* to use React at all — see §4 decision guidance and
[production-frontend-architectures.md](production-frontend-architectures.md). Default stance: server-rendered
templates (optionally with HTMX) until a concrete requirement demands client-side state that templates cannot
express cheaply. React is a cost (second build, second router, duplicated validation UX, API surface), not an upgrade.

## 2. Core concepts

| Concept | Meaning for a Django + React app |
|---|---|
| Origin | scheme + host + port. `https://app.example.com` and `https://api.example.com` are **different origins** but the **same site** (same registrable domain). |
| Same-site vs cross-site | Drives `SameSite` cookie behaviour. `example.com` vs `example-api.net` is cross-site → cookies need `SameSite=None; Secure`, and are increasingly blocked as third-party cookies. |
| CORS | Browser rule that lets a cross-origin page *read* a response. Configured on the server (django-cors-headers, third-party). It is **not** a protection against CSRF. |
| CSRF | Attack where another site makes the browser send a state-changing, cookie-authenticated request. Django's `CsrfViewMiddleware` requires a token (`X-CSRFToken` header or form field) plus Origin/Referer checks. |
| Session auth | Django session cookie (`sessionid`, HttpOnly). Browser attaches it automatically → CSRF protection required. |
| Token auth | `Authorization: Bearer …` header set by JS. Not auto-attached → no CSRF, but token must be stored somewhere JS can read → XSS can steal it. |
| SPA shell | The built `index.html` that loads hashed JS/CSS bundles; all client routes return it. |
| Island | A React root mounted on one element of a server-rendered Django page. |

### The three topologies

| | A. Same-origin SPA | B. Cross-origin SPA | C. React islands |
|---|---|---|---|
| Shape | One origin. Django serves `index.html` (or a reverse proxy routes `/api/*` → Django, `/*` → static SPA). | SPA on `app.example.com` (CDN), API on `api.example.com`. | Django templates render pages; React mounts into specific `<div>`s with props from `json_script`. |
| Auth | Session cookie + CSRF (simplest, safest). | Session cookie with CORS credentials, or tokens. | Django session, as for any template page. |
| CORS needed | No | Yes | No |
| Routing | React Router for app; Django catch-all for unknown non-API paths. | React Router; CDN 404 fallback to `index.html`. | Django URLs; no client router (or one per island). |
| SEO / first paint | Weak (CSR) unless pre-rendered. | Weak (CSR). | Strong — HTML is server-rendered. |
| Deploy units | One (or proxy + one). | Two, versioned independently. | One. |
| Best for | Authenticated apps owned by one team. | Separate frontend team/release cadence, multiple clients of the API. | Mostly-server-rendered apps with a few rich widgets (charts, editors, kanban). |

Prefer **C** if most pages are documents/forms; **A** if the whole product is an app behind login; **B** only when an organisational or scaling reason forces separate origins.

## 3. Internal mechanics

### 3.1 How Django's CSRF check sees a `fetch` call

1. Django sets the `csrftoken` cookie when a response is rendered with `get_token()` having been called (e.g. `{% csrf_token %}`, or the `ensure_csrf_cookie` decorator). `CSRF_COOKIE_HTTPONLY` defaults to `False` so JS can read it.
2. For unsafe methods (POST/PUT/PATCH/DELETE), `CsrfViewMiddleware` checks:
   - the `Origin` header (when present) matches the request host or an entry in `CSRF_TRUSTED_ORIGINS` (entries must include the scheme, Django ≥ 4.0);
   - over HTTPS without `Origin`, the `Referer` is same-origin or trusted;
   - the `X-CSRFToken` header (name set by `CSRF_HEADER_NAME`, default `HTTP_X_CSRFTOKEN`) matches the cookie secret.
3. DRF: `APIView` is CSRF-exempt at the Django level; `SessionAuthentication` re-applies the CSRF check **only for authenticated requests**. An anonymous login endpoint built on DRF therefore skips CSRF — protect it explicitly (e.g. `csrf_protect`) to prevent login CSRF.
4. DRF with `SessionAuthentication` returns **403** (not 401) for unauthenticated requests, because it sends no `WWW-Authenticate` header. `TokenAuthentication`/JWT schemes return 401. The client must treat both as "maybe logged out" — see §5.3.

### 3.2 How CORS with credentials works (topology B)

1. Browser sends a preflight `OPTIONS` for non-simple requests (JSON body, custom headers like `X-CSRFToken`).
2. Server must reply with `Access-Control-Allow-Origin: https://app.example.com` (never `*` with credentials), `Access-Control-Allow-Credentials: true`, and allowed headers/methods.
3. Client must send `credentials: 'include'`; otherwise cookies are neither sent nor stored.
4. Cookies still obey `SameSite`: app/api on the same site → `Lax` works; different sites → `SameSite=None; Secure` required and third-party cookie blocking (Safari ITP, Firefox, Chrome policies) can break it. Prefer same-site subdomains or topology A.
5. The SPA cannot read a `csrftoken` cookie set for `api.example.com` via `document.cookie` on `app.example.com` unless `CSRF_COOKIE_DOMAIN=".example.com"`. Simpler: a CSRF endpoint that returns the token in JSON.

### 3.3 Client routing vs server routing (topology A)

A deep link `GET /projects/42/tasks` hits Django first. Django must return the SPA shell for every client route **except** `/api/`, `/admin/`, `/static/`, `/media/` — otherwise a reload gives Django's 404, or worse, the shell is returned for a missing API path with status 200 and the client tries to parse HTML as JSON.

### 3.4 Vite dev server

`vite` serves source modules over native ESM with HMR on `localhost:5173`. `server.proxy` forwards matching paths to Django so the browser sees **one origin** in development — no CORS, cookies just work. The dev proxy does not exist in production builds.

## 4. Engineering decisions and trade-offs

### 4.1 Templates, HTMX, React or hybrid

| Prefer | When |
|---|---|
| Django templates only | CRUD, content, forms, admin-like screens; SEO matters; small team. |
| Templates + HTMX | Partial updates (inline edit, filters, infinite lists, modals) without client state. See [htmx-engineering.md](htmx-engineering.md). |
| Templates + Alpine | Small client-only behaviour (toggles, tabs, dropdowns). See [alpine-js-engineering.md](alpine-js-engineering.md). |
| React islands (C) | A few widgets with heavy client state: drag-and-drop boards, rich editors, interactive charts, spreadsheet grids. |
| Same-origin SPA (A) | Most screens are highly interactive, offline-ish or real-time; long sessions; app-like navigation; team has React skill. |
| Cross-origin SPA (B) | Separate frontend deploy/team, the API serves several clients, or frontend hosted on a CDN platform you cannot co-locate. |

### 4.2 Session cookie vs token auth

| Prefer session + CSRF when… | Prefer tokens when… |
|---|---|
| Browser is the only client, same-site deployment (A or C, or B on subdomains). | Non-browser clients (mobile, CLI, partners) share the API. |
| You want HttpOnly credentials that XSS cannot exfiltrate. | Cross-site deployment where third-party cookies are unreliable. |
| You want server-side revocation (logout kills the session). | You accept short-lived access tokens + refresh rotation complexity. |

Token storage options and risks:

| Storage | Risk |
|---|---|
| `localStorage`/`sessionStorage` | Readable by any script → one XSS = token theft that outlives the page. Avoid for long-lived tokens. |
| In-memory (JS variable) | Lost on reload; still readable by XSS during the session; needs a refresh mechanism. |
| HttpOnly refresh cookie + in-memory access token | Best token option for browsers; the refresh endpoint is cookie-authenticated again → needs CSRF protection. |

For browser-only apps, session auth is usually both simpler and safer. Server-side details: [authentication.md](../../django-rest-framework/references/authentication.md), [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md).

### 4.3 Data fetching: plain fetch vs TanStack Query

| Prefer a small `useEffect`/hook + API client | Prefer TanStack Query (third-party) |
|---|---|
| Islands with one or two requests. | Many screens share server data; caching, deduplication, background refetch, invalidation after mutations, infinite lists. |
| No need for cache invalidation across components. | You would otherwise hand-roll a cache in global state. |

Server data does **not** belong in Redux/Context-style global stores; see [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md).

### 4.4 React 19 Actions vs manual handlers

| Use | For |
|---|---|
| `useActionState` + `<form action={fn}>` | Form submissions that return a result/error state; gives `isPending` without manual flags. |
| `useOptimistic` | Showing the expected result immediately (toggle done, add comment) and reverting on failure. |
| `useFormStatus` (react-dom) | A submit button that reads pending state of its parent form. |
| Manual `onSubmit` / TanStack `useMutation` | Complex mutation flows needing cache invalidation or retries. |

React 19 form Actions on a client-only SPA call your API client; they are **not** Django form posts and still need CSRF headers.

## 5. Django/frontend implementation patterns

### 5.1 Topology A: Django serves the SPA shell

```python
# config/urls.py
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

spa = ensure_csrf_cookie(TemplateView.as_view(template_name="spa/index.html"))

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),            # DRF routers
    # Client routes: everything not owned by Django. Keep this LAST.
    re_path(r"^(?!api/|admin/|static/|media/).*$", spa, name="spa"),
]
```

- `spa/index.html` is Vite's built `index.html` copied into a template directory, or rendered with django-vite (third-party) tags. `ensure_csrf_cookie` guarantees the `csrftoken` cookie exists before the first POST.
- Unknown `/api/...` paths fall through to DRF/Django 404 JSON/HTML, never the shell.
- Alternative: nginx/Caddy serves `dist/` with `try_files $uri /index.html` and proxies `/api/`, `/admin/`, `/static/` to Django. Then add a tiny `GET /api/csrf/` endpoint (below).

### 5.2 CSRF bootstrap endpoint (topologies A-with-proxy and B)

```python
# api/views.py
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.http import JsonResponse
from django.views.decorators.http import require_GET

@require_GET
@ensure_csrf_cookie
def csrf(request):
    # Returning the token in the body lets a cross-origin SPA use it
    # even when it cannot read the API domain's cookie.
    return JsonResponse({"csrfToken": get_token(request)})
```

Topology B settings (django-cors-headers, third-party):

```python
# settings/production.py
INSTALLED_APPS += ["corsheaders"]
MIDDLEWARE.insert(0, "corsheaders.middleware.CorsMiddleware")  # before CommonMiddleware
CORS_ALLOWED_ORIGINS = ["https://app.example.com"]   # exact origins, no wildcard
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = ["https://app.example.com"]   # scheme required
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"   # same-site subdomains; "None" only if truly cross-site
CSRF_COOKIE_SAMESITE = "Lax"
# CSRF_COOKIE_DOMAIN = ".example.com"  # only if the SPA must read the cookie directly
```

`CORS_ALLOWED_ORIGINS` lets the SPA read responses; `CSRF_TRUSTED_ORIGINS` lets its POSTs pass the Origin check. You need both. CORS headers must also include `x-csrftoken` in allowed headers (django-cors-headers' default `CORS_ALLOW_HEADERS` includes it [VERIFY: current default list]).

### 5.3 API client module

```js
// src/api/client.js
const BASE = import.meta.env.VITE_API_BASE_URL ?? "/api"; // public, not a secret
const CROSS_ORIGIN = !BASE.startsWith("/");
const IDEMPOTENT = new Set(["GET", "HEAD", "OPTIONS"]);
let csrfToken = null;

export class ApiError extends Error {
  constructor(status, body) {
    super(normaliseMessage(body) ?? `HTTP ${status}`);
    this.status = status;
    this.body = body;
    // DRF shapes: {"detail": "..."} or {"field": ["msg"], "non_field_errors": ["msg"]}
    this.fieldErrors = body && typeof body === "object" && !("detail" in body) ? body : {};
  }
  get isAuth() { return this.status === 401 || this.status === 403; }
}

function normaliseMessage(body) {
  if (!body || typeof body !== "object") return null;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.non_field_errors)) return body.non_field_errors.join(" ");
  return null;
}

function readCookie(name) {
  return document.cookie.split("; ").find((c) => c.startsWith(`${name}=`))?.split("=")[1] ?? null;
}

async function ensureCsrf() {
  if (!CROSS_ORIGIN) return readCookie("csrftoken");
  if (!csrfToken) {
    const r = await fetch(`${BASE}/csrf/`, { credentials: "include" });
    csrfToken = (await r.json()).csrfToken;
  }
  return csrfToken;
}

const sleep = (ms, signal) =>
  new Promise((res, rej) => {
    const t = setTimeout(res, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); rej(signal.reason); }, { once: true });
  });

export async function api(path, { method = "GET", body, signal, retries = 2, headers = {} } = {}) {
  method = method.toUpperCase();
  const init = {
    method,
    signal,
    credentials: CROSS_ORIGIN ? "include" : "same-origin",
    headers: { Accept: "application/json", ...headers },
  };
  if (body !== undefined) {
    if (body instanceof FormData) init.body = body;      // let the browser set multipart boundary
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  }
  if (!IDEMPOTENT.has(method)) init.headers["X-CSRFToken"] = await ensureCsrf();

  const canRetry = IDEMPOTENT.has(method);   // never auto-retry POST/PATCH without an idempotency key
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      // Absolute URLs (DRF `next`/`previous` links) are used as-is.
      res = await fetch(/^https?:/.test(path) ? path : `${BASE}${path}`, init);
    } catch (err) {
      if (err.name === "AbortError" || !canRetry || attempt >= retries) throw err;
      await sleep(2 ** attempt * 300 + Math.random() * 200, signal);
      continue;
    }
    if (canRetry && attempt < retries && [429, 502, 503, 504].includes(res.status)) {
      const retryAfter = Number(res.headers.get("Retry-After")) * 1000;
      await sleep(retryAfter || 2 ** attempt * 300 + Math.random() * 200, signal);
      continue;
    }
    if (res.status === 204) return null;
    const isJson = res.headers.get("Content-Type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();
    if (!res.ok) {
      if (res.status === 403 && typeof data?.detail === "string" && data.detail.includes("CSRF")) {
        csrfToken = null; // token rotated (e.g. after login); caller may retry once
      }
      const error = new ApiError(res.status, data);
      if (error.isAuth) window.dispatchEvent(new CustomEvent("api:auth-error", { detail: error }));
      throw error;
    }
    return data;
  }
}
```

Notes:
- `django.contrib.auth.login()` rotates the CSRF token. After login, re-read the cookie (A) or refetch `/csrf/` (B).
- 401/403 handling: an app-level listener for `api:auth-error` checks `/api/me/`; if logged out, redirect to login preserving `location.pathname`. A 403 for a logged-in user is a permission error — show it, don't log out.
- The CSRF-failure 403 detail text is a DRF/Django message, not a contract [VERIFY: message text for your DRF version]; prefer refetching the token and retrying once on any 403 for unsafe methods only if your UX needs it.

### 5.4 Loading, error and retry states in a component

```jsx
// src/features/tasks/TaskList.jsx
import { useEffect, useState } from "react";
import { api } from "../../api/client";

export function TaskList({ projectId }) {
  const [state, setState] = useState({ status: "loading", data: null, error: null });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setState((s) => ({ ...s, status: "loading" }));
    api(`/projects/${projectId}/tasks/`, { signal: ctrl.signal })
      .then((data) => setState({ status: "success", data, error: null }))
      .catch((error) => { if (error.name !== "AbortError") setState({ status: "error", data: null, error }); });
    return () => ctrl.abort(); // cancels stale requests on projectId change/unmount
  }, [projectId, reloadKey]);

  if (state.status === "loading") return <p role="status">Loading tasks…</p>;
  if (state.status === "error")
    return (
      <div role="alert">
        <p>Could not load tasks: {state.error.message}</p>
        <button type="button" onClick={() => setReloadKey((k) => k + 1)}>Try again</button>
      </div>
    );
  if (state.data.results.length === 0) return <p>No tasks yet.</p>;
  return <ul>{state.data.results.map((t) => <li key={t.id}>{t.title}</li>)}</ul>;
}
```

Accessibility: `role="status"` for polite loading text, `role="alert"` for errors, a real `<button>` for retry, and keep focus stable when content swaps. React escapes text by default; never pass API data to `dangerouslySetInnerHTML` without sanitising (see [frontend-security.md](frontend-security.md)).

### 5.5 Consuming DRF pagination

| DRF class | Response | Client strategy |
|---|---|---|
| `PageNumberPagination` | `{count, next, previous, results}` | Numbered pager; keep `?page=` in the URL (React Router search params) so reload/back works. Show total from `count`. |
| `LimitOffsetPagination` | `{count, next, previous, results}` | Same as page; offsets drift when rows are inserted. |
| `CursorPagination` | `{next, previous, results}` (no count) | "Load more"/infinite scroll; stable under inserts; requires a fixed ordering server-side. Treat `next` as an opaque URL. |

```jsx
// TanStack Query (third-party) v5 infinite list over CursorPagination
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

export function useActivityFeed(projectId) {
  return useInfiniteQuery({
    queryKey: ["activity", projectId],
    queryFn: ({ pageParam, signal }) =>
      api(pageParam ?? `/projects/${projectId}/activity/`, { signal }),
    initialPageParam: null,
    // `next` is an opaque absolute URL from DRF; api() accepts it unchanged.
    getNextPageParam: (last) => last.next ?? undefined,
  });
}
```

Pagination design (classes, ordering, page size limits): [pagination.md](../../django-rest-framework/references/pagination.md).

### 5.6 React 19 Actions with the API client

```jsx
import { useActionState, useOptimistic } from "react";
import { useFormStatus } from "react-dom";
import { api, ApiError } from "../../api/client";

function SubmitButton() {
  const { pending } = useFormStatus();
  return <button type="submit" disabled={pending}>{pending ? "Saving…" : "Add task"}</button>;
}

export function NewTaskForm({ projectId, tasks, onCreated }) {
  const [optimisticTasks, addOptimistic] = useOptimistic(tasks, (cur, t) => [...cur, { ...t, pending: true }]);
  const [state, formAction] = useActionState(async (_prev, formData) => {
    const title = formData.get("title");
    addOptimistic({ id: `tmp-${Date.now()}`, title });
    try {
      onCreated(await api(`/projects/${projectId}/tasks/`, { method: "POST", body: { title } }));
      return { errors: {} };
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) return { errors: e.fieldErrors };
      return { errors: { non_field_errors: ["Something went wrong. Try again."] } };
    }
  }, { errors: {} });

  return (
    <>
      <ul>{optimisticTasks.map((t) => <li key={t.id} aria-busy={t.pending || undefined}>{t.title}</li>)}</ul>
      <form action={formAction} noValidate>
        <label htmlFor="title">Title</label>
        <input id="title" name="title" required aria-invalid={!!state.errors.title} aria-describedby={state.errors.title ? "title-err" : undefined} />
        {state.errors.title && <p id="title-err">{state.errors.title.join(" ")}</p>}
        {state.errors.non_field_errors && <p role="alert">{state.errors.non_field_errors.join(" ")}</p>}
        <SubmitButton />
      </form>
    </>
  );
}
```

Server validation (DRF serializer) is authoritative; `required` and disabled buttons are UX only.

### 5.7 Frontend routing

React Router (third-party; v7 merges `react-router-dom` into `react-router` [VERIFY: version and package name in use]):

```jsx
// src/main.jsx
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { AppLayout, ProjectsPage, ProjectPage, NotFound } from "./routes";

const router = createBrowserRouter([
  { path: "/", element: <AppLayout />, errorElement: <NotFound />, children: [
    { index: true, element: <ProjectsPage /> },
    { path: "projects/:projectId", element: <ProjectPage /> },
    { path: "*", element: <NotFound /> },
  ]},
]);
createRoot(document.getElementById("root")).render(<RouterProvider router={router} />);
```

- Client routes must not collide with `/api`, `/admin`, `/static`, `/media`.
- On navigation, update `document.title` and move focus to the main heading (or a skip target) so screen readers announce the new page — SPAs lose the browser's built-in announcement.
- A client-side 404 page returns HTTP 200 from the shell; for crawlers that matters (see §8).

### 5.8 Topology C: React islands in Django templates

```django
{# templates/projects/board.html #}
{% extends "base.html" %}
{% load django_vite %}  {# django-vite (third-party) [VERIFY: tag names for installed version] #}
{% block content %}
  <h1>{{ project.name }}</h1>
  {{ board_props|json_script:"board-props" }}
  <div id="board-root" data-props-id="board-props">
    <p>Loading board… <a href="{% url 'projects:task_list' project.pk %}">View as list</a></p>
  </div>
{% endblock %}
{% block scripts %}
  {% vite_hmr_client %}
  {% vite_asset "src/islands/board.jsx" %}
{% endblock %}
```

```jsx
// src/islands/board.jsx
import { createRoot } from "react-dom/client";
import { Board } from "../features/board/Board";

for (const el of document.querySelectorAll("[data-props-id]")) {
  const props = JSON.parse(document.getElementById(el.dataset.propsId).textContent);
  createRoot(el).render(<Board {...props} />);
}
```

`json_script` escapes `<`, `>`, `&` so props cannot break out of the `<script>` element. The server-rendered fallback content is replaced on mount — keep it meaningful (progressive enhancement). Islands use the page's session and the `csrftoken` cookie (ensure the page calls `get_token`, e.g. via `{% csrf_token %}` somewhere or `ensure_csrf_cookie`).

### 5.9 Vite dev proxy

```js
// vite.config.js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000" },
      "/admin": { target: "http://127.0.0.1:8000" },
      "/static": { target: "http://127.0.0.1:8000" },
    },
  },
  build: { outDir: "dist", manifest: true, sourcemap: true }, // manifest at dist/.vite/manifest.json in Vite ≥ 5 [VERIFY: manifest path for the Vite major in use]
});
```

Gotcha: with `changeOrigin: true` the proxied `Host` becomes `127.0.0.1:8000` while the browser's `Origin` stays `http://localhost:5173`, so Django's CSRF Origin check fails with 403. Either leave `changeOrigin` off (and allow `localhost` in `ALLOWED_HOSTS`) or add `"http://localhost:5173"` to `CSRF_TRUSTED_ORIGINS` in **dev settings only**.

### 5.10 Environment variables

- Only `VITE_`-prefixed variables are exposed via `import.meta.env`, and they are **inlined into the public bundle**. Put API base URLs, Sentry DSNs (public by design), feature-flag defaults there — never API keys, secrets, or signing keys.
- Built-ins: `import.meta.env.MODE`, `DEV`, `PROD`, `BASE_URL`.
- Environment-specific runtime config that must not require a rebuild: render it into the shell from Django (`{{ runtime_config|json_script:"runtime-config" }}`) or serve `/config.json`.

### 5.11 Production builds and deployment

```bash
npm ci                 # lockfile-exact install
npm run build          # vite build → dist/index.html + dist/assets/*-[hash].js|css
python manage.py collectstatic --noinput   # if Django/WhiteNoise serves dist/assets via STATICFILES_DIRS
```

| Asset | Cache-Control |
|---|---|
| `index.html` (shell) | `no-cache` (revalidate every load so new deploys are picked up) |
| `assets/*-[hash].js/css` | `public, max-age=31536000, immutable` |
| `/api/*` | Set per endpoint by Django; usually `private, no-store` for user data |

- CDN/static host (topology B or proxy-A): configure SPA fallback (unknown path → `index.html` with 200) but **not** for `/api/*` or asset paths — a missing hashed chunk must 404, not return HTML.
- Deploy assets *before* the shell; keep previous hashed assets for at least one release so open tabs can still lazy-load old chunks. Handle chunk-load failures by prompting a reload.
- Vite `base` must match where assets are served (e.g. `/static/` when Django serves them). Pipelines: [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md).

## 6. Production use cases

| Product | Topology | Notes |
|---|---|---|
| B2B dashboard behind login, one team | A (Django serves shell) | Session + CSRF; TanStack Query for server cache; no SEO needs. |
| Marketing site + logged-in app | C for marketing/content, A under `/app/` | Content stays server-rendered and indexable. |
| CRM with a kanban board and rich text editor | C | Board/editor islands; rest is templates + HTMX. |
| Public API with web + mobile clients | B with tokens for mobile, session for web | Same DRF views, multiple authentication classes. |
| Analytics UI with interactive charts | C | Chart island fed by `json_script` initial data + API for drill-down. |

## 7. Advantages and disadvantages

| Topology | Advantages | Disadvantages |
|---|---|---|
| A | No CORS; session cookies just work; one deploy; simple CSRF. | Django (or proxy) must serve the shell; CSR first-paint and SEO costs. |
| B | Independent deploys; CDN-hosted SPA; API reusable. | CORS + cookie `SameSite` complexity; two release trains to keep compatible; third-party cookie risk if cross-site. |
| C | Server-rendered by default, SEO-friendly, progressive enhancement, incremental adoption. | Two rendering models; props serialisation boundary; shared state across islands needs events/stores. |
| React vs templates generally | Rich client state, optimistic UI, offline-ish behaviour. | Duplicated routing/validation UX, bundle cost (INP/LCP), API surface to secure and version. |

## 8. Security and performance considerations

### Security
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax` (or `Strict` for high-risk apps). Never expose `sessionid` to JS.
- Every unsafe method sends `X-CSRFToken`; never `csrf_exempt` an endpoint just because "it's an API" while it accepts session cookies.
- CORS: explicit origins only; never reflect arbitrary `Origin` with credentials. CORS does not stop CSRF form posts — CSRF tokens do.
- Authorisation is enforced in DRF permissions and querysets; hiding a button or route in React is UX only. See [permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md).
- Tokens in `localStorage` are exposed to any XSS; minimise third-party scripts and use a CSP. See [frontend-security.md](frontend-security.md), [cors-and-browser-security.md](../../django-security-engineering/references/cors-and-browser-security.md), [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md).
- Login CSRF: protect the login endpoint even though the user is anonymous.

### Performance
- Code-split by route (`lazy()` + dynamic `import()`); keep the initial bundle small for LCP/INP.
- Avoid request waterfalls: fetch route data in parallel (router loaders or parallel queries); embed initial data via `json_script` in topology C/A-with-Django-shell.
- Cancel stale requests (AbortController) to avoid wasted work and race conditions.
- Cache-busted immutable assets + `no-cache` shell. HTTP/2 or HTTP/3 at the edge.
- Backend API latency and N+1s: [api-performance.md](../../django-performance/references/api-performance.md). Browser metrics: [frontend-performance.md](frontend-performance.md).

### SEO caveats
CSR shells return near-empty HTML; crawlers that render JS do so later and less reliably, social previews (Open Graph) usually don't run JS, and client-side 404s return 200. Anything that must be indexed or shared belongs in Django-rendered pages (topology C) or needs pre-rendering/SSR (out of scope for plain Vite + Django).

## 9. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| Choosing an SPA for a CRUD app "for better UX" | Start with templates + HTMX; add islands where state demands it. |
| `@csrf_exempt` on API views used by the browser with session auth | Send `X-CSRFToken`; keep CSRF on. |
| `CORS_ALLOW_ALL_ORIGINS = True` with credentials | Explicit `CORS_ALLOWED_ORIGINS`. |
| Missing `CSRF_TRUSTED_ORIGINS` (or missing scheme) in topology B | `["https://app.example.com"]`. |
| Forgetting `credentials: 'include'` cross-origin | Cookies not sent/stored → user appears logged out. |
| Catch-all route placed before `api/` or matching `/api/...` | Negative lookahead, catch-all last. |
| Secrets in `VITE_*` vars | Move to Django; frontend calls an authenticated endpoint. |
| Retrying POSTs automatically | Retry only idempotent methods; use idempotency keys server-side for POST retries. |
| Treating every 403 as "logged out" | Check `/api/me/`; distinguish auth vs permission. |
| Building props with `{{ data|safe }}` inside `<script>` | `json_script`. |
| Long-cache on `index.html` | `no-cache` on shell; immutable only on hashed assets. |
| JWT in `localStorage` for a browser-only same-site app | Session auth. |
| Ignoring `next`/`previous` and building page URLs by hand for cursor pagination | Follow opaque `next` URLs. |

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| 403 "CSRF Failed: CSRF token missing" | No `X-CSRFToken` header or no `csrftoken` cookie | DevTools → request headers; ensure `ensure_csrf_cookie` / `/csrf/` called. |
| 403 "Origin checking failed" | Origin not trusted (proxy `changeOrigin`, missing `CSRF_TRUSTED_ORIGINS`) | Compare `Origin` header to `request.get_host()`; dev settings. |
| 403 after login on first POST | CSRF token rotated on login | Refresh token after login. |
| CORS error in console, request visible in Django logs | Response lacks `Access-Control-Allow-*` / credentials header | Preflight response headers; middleware order. |
| Logged in on API domain but SPA appears logged out | Cookie `SameSite`/third-party blocking, or missing `credentials: 'include'` | Application → Cookies; "blocked" reason in Network tab. |
| Reload on `/projects/42` gives 404 | No Django catch-all / CDN fallback | URLconf order; host rewrite rules. |
| `Unexpected token '<'` parsing JSON | Shell HTML returned for an API path | Catch-all regex excludes `api/`; `BASE` URL correct. |
| Old JS after deploy | `index.html` cached | Response `Cache-Control` on shell. |
| "Failed to fetch dynamically imported module" | Old chunk deleted while tab open | Keep previous assets; reload on chunk error. |
| DRF `next` links are `http://` or internal host (mixed content / CORS) | Proxy headers not trusted | `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST` behind the proxy. |
| Anonymous user gets 403 instead of 401 | DRF `SessionAuthentication` behaviour | Expected; handle both. |

Validation steps:
1. `curl -I https://app.example.com/` → shell `Cache-Control: no-cache`; an asset → `immutable`.
2. `curl -i https://api.example.com/api/projects/ -X OPTIONS -H "Origin: https://evil.test" -H "Access-Control-Request-Method: POST"` → no `Access-Control-Allow-Origin` for untrusted origin.
3. POST without `X-CSRFToken` while logged in → 403.
4. Deep-link reload on three client routes; `/api/nonexistent/` → JSON 404.
5. Inspect the built bundle (`grep -r` over `dist/`) for anything secret-looking.
6. Playwright smoke test of login → create → reload ([frontend-testing.md](frontend-testing.md)).

## 11. Related references and skills

- [production-frontend-architectures.md](production-frontend-architectures.md) — DRF+React SPA and hybrid islands reference architectures.
- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — SSR vs CSR, architecture selection.
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — server vs client state, optimistic UI, races.
- [javascript-engineering.md](javascript-engineering.md) — fetch, AbortController, `json_script`.
- [frontend-security.md](frontend-security.md) · [frontend-performance.md](frontend-performance.md) · [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) · [frontend-testing.md](frontend-testing.md) · [htmx-engineering.md](htmx-engineering.md)
- [django-rest-framework](../../django-rest-framework/SKILL.md): [authentication.md](../../django-rest-framework/references/authentication.md), [pagination.md](../../django-rest-framework/references/pagination.md), [exception-handling-and-error-responses.md](../../django-rest-framework/references/exception-handling-and-error-responses.md), [permissions-and-access-control.md](../../django-rest-framework/references/permissions-and-access-control.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md): [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md), [cors-and-browser-security.md](../../django-security-engineering/references/cors-and-browser-security.md), [session-and-cookie-security.md](../../django-security-engineering/references/session-and-cookie-security.md)
- [django-core authentication-sessions.md](../../django-core/references/authentication-sessions.md)

## 12. Official documentation and external references

- Django CSRF: https://docs.djangoproject.com/en/5.2/howto/csrf/ and https://docs.djangoproject.com/en/5.2/ref/csrf/
- Django settings (CSRF_*, SESSION_*): https://docs.djangoproject.com/en/5.2/ref/settings/
- Django `json_script`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- MDN CORS: https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS
- MDN Set-Cookie / SameSite: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie
- MDN Fetch `credentials`: https://developer.mozilla.org/en-US/docs/Web/API/RequestInit#credentials
- MDN AbortController: https://developer.mozilla.org/en-US/docs/Web/API/AbortController
- MDN Cache-Control: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control
- React `useActionState`: https://react.dev/reference/react/useActionState
- React `useOptimistic`: https://react.dev/reference/react/useOptimistic
- React `useFormStatus`: https://react.dev/reference/react-dom/hooks/useFormStatus
- Vite server proxy: https://vite.dev/config/server-options#server-proxy
- Vite env variables: https://vite.dev/guide/env-and-mode
- Vite backend integration: https://vite.dev/guide/backend-integration
- OWASP CSRF Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- OWASP HTML5 Security (storage): https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
