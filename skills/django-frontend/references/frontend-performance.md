# Frontend Performance

> **Scope:** Owns browser-side performance of Django frontends: measurement (field and lab), Core Web Vitals and their Django-specific causes, JS/CSS/image/font delivery, resource hints, browser caching and compression from the client's view, rendering performance, third-party scripts, waterfalls, budgets and CI enforcement, and HTMX-specific costs. Server time (queries, caching, TTFB root causes, CDN/server configuration) belongs to [django-performance](../../django-performance/SKILL.md); the static pipeline mechanics belong to [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md).

## 1. Purpose and scope

Goal: pages that load fast, stay stable and respond quickly **for real users on real devices**, measured at the 75th percentile. Workflow this reference enforces:

1. **Measure field data** (what users experience).
2. **Diagnose in the lab** (reproducible traces).
3. **Fix the biggest contributor** to the failing metric.
4. **Verify in the field** and **guard with a budget in CI**.

Never optimize without a measurement showing the problem; never accept a lab score as proof that users are fine.

## 2. Core concepts

### Core Web Vitals (CWV)

| Metric | Measures | Good | Needs improvement | Poor |
|---|---|---|---|---|
| **LCP** Largest Contentful Paint | Loading: render time of largest image/text block in viewport | ≤ 2.5 s | ≤ 4.0 s | > 4.0 s |
| **INP** Interaction to Next Paint | Responsiveness: latency from interaction to next frame, near-worst interaction of the visit | ≤ 200 ms | ≤ 500 ms | > 500 ms |
| **CLS** Cumulative Layout Shift | Visual stability: largest burst ("session window") of unexpected shifts | ≤ 0.1 | ≤ 0.25 | > 0.25 |

Assessed at the **75th percentile** of page loads, segmented mobile/desktop. INP replaced FID as a CWV in March 2024. Diagnostic (not CWV): **TTFB** (good ≤ 0.8 s), **FCP** (good ≤ 1.8 s), Total Blocking Time (lab proxy for INP).

### LCP breakdown

LCP = **TTFB** + **resource load delay** (time until the browser starts fetching the LCP resource) + **resource load duration** + **element render delay**. Each sub-part points to a different fix: TTFB → server ([django-application-performance.md](../../django-performance/references/django-application-performance.md)); load delay → discoverability/priority; duration → bytes/format/CDN; render delay → render-blocking CSS/JS, fonts, client rendering.

### INP breakdown

INP = **input delay** (main thread busy with other tasks) + **processing time** (your event handlers) + **presentation delay** (style/layout/paint of the resulting update). Long tasks (> 50 ms) cause input delay; heavy handlers cause processing time; huge DOM/complex CSS cause presentation delay.

### Field vs lab

| | Field (RUM) | Lab |
|---|---|---|
| Tools | CrUX (Chrome UX Report, PageSpeed Insights, Search Console), `web-vitals` library → your analytics | Lighthouse, DevTools Performance panel, WebPageTest |
| Strength | Truth: real devices, networks, interactions | Reproducible, detailed traces, pre-release |
| Weakness | Aggregated, delayed (CrUX = trailing 28 days), Chromium only, needs traffic; authenticated pages are not in CrUX unless your own RUM | Single synthetic run; INP needs real interactions (Lighthouse reports TBT instead) |

Authenticated Django apps (dashboards, admin) usually have **no useful CrUX data** — ship your own RUM with `web-vitals`.

## 3. Internal mechanics

### Critical rendering path in brief

HTML parse → discover resources → CSS is **render-blocking** (no paint until CSSOM ready); classic `<script>` without `defer`/`async` is **parser-blocking**; `type="module"` is deferred by default. The preload scanner discovers `<img src>`, `<link>`, `<script src>` in HTML early — but **not** resources referenced only in CSS (background images, fonts) or injected by JS. Full pipeline: [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md).

### Common causes in Django apps

| Metric | Cause | Fix |
|---|---|---|
| LCP | Slow TTFB (N+1 queries, no caching, cold workers) | [django-performance](../../django-performance/SKILL.md) |
| LCP | Hero image lazy-loaded, CSS background, or injected by JS | Real `<img>` in HTML, `fetchpriority="high"`, no `loading="lazy"` |
| LCP | Render-blocking CSS bundle (all of Bootstrap/Tailwind unpurged), sync `<script>` in `<head>` | Small CSS, `defer`/module scripts |
| LCP | LCP element rendered client-side (React mount) | Server-render the content; island hydration after |
| LCP | Web font blocks text (`font-display: block`) | `font-display: swap`/`optional`, preload |
| CLS | `<img>` without `width`/`height` | Dimensions or `aspect-ratio` |
| CLS | Late cookie banner/announcement bar pushing content | Overlay (fixed position) or reserve space in server HTML |
| CLS | HTMX swap/`hx-trigger="load"` widget changes height | Reserve `min-height` skeleton matching final size |
| CLS | Font swap with different metrics | `size-adjust`/metric overrides, `font-display: optional` |
| INP | Long tasks: big bundles evaluated at startup, analytics/tag managers | Code split, defer third parties, break up work |
| INP | Heavy handlers (sync filtering of 10k rows, layout thrash) | Server-side filter (HTMX), yield to main thread |
| INP | Huge DOM (unpaginated tables, 5k+ nodes) | Paginate, `content-visibility`, virtualize only if necessary |
| INP | Alpine/React re-rendering large lists on each keystroke | Debounce, keyed lists, memoization |

### Yielding to the main thread

```js
// static/js/yield.js
export function yieldToMain() {
  if (globalThis.scheduler?.yield) return scheduler.yield();   // Chromium; [VERIFY: scheduler.yield Baseline status]
  return new Promise((resolve) => setTimeout(resolve, 0));
}

export async function processInChunks(items, fn, chunk = 200) {
  for (let i = 0; i < items.length; i += chunk) {
    items.slice(i, i + chunk).forEach(fn);
    await yieldToMain();                     // lets pending input be handled → better INP
  }
}
```

Give immediate visual feedback first (toggle a class, show spinner), then do the expensive work after yielding.

### Layout thrashing

Reading layout (`offsetHeight`, `getBoundingClientRect`) after writing styles forces synchronous layout; in a loop this is O(n) layouts. Batch all reads, then all writes; use `requestAnimationFrame` for visual updates; animate `transform`/`opacity` only.

### `content-visibility`

`content-visibility: auto` skips rendering work for off-screen sections; pair with `contain-intrinsic-size: auto 600px` to avoid scrollbar jumps/CLS. Supported in Chromium and Firefox, Safari later [VERIFY: content-visibility Baseline status]. Good for long server-rendered reports; unnecessary for short pages.

## 4. Engineering decisions and trade-offs

| Decision | Prefer A when… | Prefer B when… |
|---|---|---|
| Server-rendered + HTMX vs SPA | **SSR/HTMX**: content-first pages, LCP matters, little client state | **SPA**: long sessions with heavy client interaction where initial load is amortized |
| Inline critical CSS vs single external CSS | **Inline critical**: large CSS (> ~50 kB compressed), first-visit-heavy marketing pages | **External only** (default): app CSS is small (Tailwind output often 10–30 kB compressed), mostly repeat visits benefit from cache, avoids build complexity and CSP `style-src` hashes |
| Bundle per page vs one bundle | **Per-page entries / dynamic import**: pages with heavy widgets (charts, editors) | **One small bundle**: total JS < ~50 kB compressed |
| AVIF vs WebP | **AVIF**: best compression for photos (slower encoding) | **WebP**: faster encoding, broad support; offer both via `<picture>` with JPEG fallback if needed |
| Self-host fonts vs Google Fonts CSS | **Self-host** (default): no extra connection, control `font-display`/subsetting, simpler CSP | **Hosted**: prototypes only |
| `font-display: swap` vs `optional` | **swap**: brand font must show | **optional**: stability over branding (no late swap/CLS) |
| Polling vs SSE/WebSocket | **Polling**: low frequency (≥ 5 s), simple infra | **SSE**: frequent updates, many clients |
| `loading="lazy"` | Below-the-fold images/iframes | Never on the LCP image or above-the-fold content |

## 5. Django/frontend implementation patterns

### 5.1 Measure first: RUM with `web-vitals`

```js
// static/js/rum.js  (bundled; web-vitals is an npm package by the Chrome team)
import { onCLS, onINP, onLCP, onTTFB } from "web-vitals/attribution";

function send(metric) {
  const body = JSON.stringify({
    name: metric.name,
    value: metric.value,
    rating: metric.rating,                 // "good" | "needs-improvement" | "poor"
    id: metric.id,
    page: document.body.dataset.routeName, // route name, not full URL with IDs/PII
    target: metric.attribution?.interactionTarget ?? metric.attribution?.target ?? null,
  });
  navigator.sendBeacon("/rum/", new Blob([body], { type: "application/json" }));
}

onCLS(send); onINP(send); onLCP(send); onTTFB(send);
```

```python
# rum/views.py — minimal collector; store aggregates, rate-limit, no PII
import json

from django.http import HttpResponse
from django.views.decorators.http import require_POST

from .models import VitalSample

ALLOWED = {"CLS", "INP", "LCP", "TTFB"}


@require_POST
def collect(request):
    try:
        data = json.loads(request.body[:2048])
    except ValueError:
        return HttpResponse(status=400)
    if data.get("name") in ALLOWED and isinstance(data.get("value"), (int, float)):
        VitalSample.objects.create(
            name=data["name"], value=data["value"], rating=str(data.get("rating"))[:20],
            page=str(data.get("page"))[:100], target=str(data.get("target"))[:200],
        )
    return HttpResponse(status=204)
```

`sendBeacon` posts cookies but cannot set an `X-CSRFToken` header; either include the token in the body and validate it, or treat the endpoint as a non-authenticated write that changes nothing sensitive and exempt it deliberately (with rate limiting and strict validation) — document the decision ([csrf-protection.md](../../django-security-engineering/references/csrf-protection.md)). Expose the route name in the base template: `<body data-route-name="{{ request.resolver_match.view_name }}">`. Report p75 per route; dashboards: [profiling-and-observability.md](../../django-performance/references/profiling-and-observability.md).

### 5.2 Lab workflow

1. Lighthouse (DevTools, mobile preset) on a production-like build (`DEBUG = False`, compressed, hashed assets) — never on `runserver` with `DEBUG = True`.
2. DevTools **Performance** panel: record load and interactions with CPU 4× slowdown; look at the LCP marker, long tasks (red corners), layout shifts track, "Interactions" track for INP.
3. **Network** panel waterfall: check what starts late (LCP image after CSS/JS), what blocks rendering, request count, compression (`content-encoding`), cache headers.
4. WebPageTest for real devices/locations, filmstrips and repeat-view comparisons.
5. Django Debug Toolbar / server timing for TTFB contributors ([django-performance](../../django-performance/SKILL.md)).

### 5.3 LCP image and responsive images

```django
{# templates/projects/detail.html — hero is the LCP element #}
<img src="{% static 'img/hero-1200.avif' %}"
     srcset="{% static 'img/hero-600.avif' %} 600w, {% static 'img/hero-1200.avif' %} 1200w, {% static 'img/hero-1800.avif' %} 1800w"
     sizes="(min-width: 64rem) 60rem, 100vw"
     width="1200" height="600" alt="{{ project.name }} site overview"
     fetchpriority="high" decoding="async">

{# below the fold #}
<picture>
  <source type="image/avif" srcset="{{ photo.avif_url }}">
  <source type="image/webp" srcset="{{ photo.webp_url }}">
  <img src="{{ photo.jpeg_url }}" width="{{ photo.width }}" height="{{ photo.height }}"
       alt="{{ photo.caption }}" loading="lazy" decoding="async">
</picture>
```

- `width`/`height` let the browser compute aspect ratio before load (no CLS); CSS `img { max-width: 100%; height: auto; }` keeps it responsive.
- `fetchpriority="high"` on the single LCP image (Baseline 2024); no `loading="lazy"` on it.
- If the LCP image must be a CSS background, preload it: `<link rel="preload" as="image" href="…" fetchpriority="high">` (with `imagesrcset`/`imagesizes` for responsive).
- Generate variants at upload time (background task), not per request. Markup patterns for art direction: [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md).

### 5.4 Fonts

```css
/* static/css/fonts.css — self-hosted, subset WOFF2 */
@font-face {
  font-family: "Inter";
  src: url("../fonts/inter-latin-var.woff2") format("woff2");
  font-weight: 100 900;
  font-style: normal;
  font-display: swap;
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+2000-206F, U+20AC, U+2122;
}
@font-face {                     /* metric-matched fallback reduces swap CLS */
  font-family: "Inter Fallback";
  src: local("Arial");
  size-adjust: 107%;             /* [VERIFY: compute overrides for your font with a metrics tool] */
}
body { font-family: "Inter", "Inter Fallback", system-ui, sans-serif; }
```

```django
<link rel="preload" href="{% static 'fonts/inter-latin-var.woff2' %}" as="font" type="font/woff2" crossorigin>
```

Font preloads **require `crossorigin`** even same-origin (fonts are fetched in CORS mode); without it the preload is wasted and downloaded twice. Preload only the one or two fonts used above the fold. One variable font often replaces 4–6 static files. Consider `system-ui` stacks for internal apps — zero font cost.

### 5.5 JavaScript delivery

```django
{# base.html #}
<script src="{% static 'vendor/htmx-2.x.min.js' %}" defer></script>
<script type="module" src="{% static 'js/app.js' %}"></script>
{% block page_scripts %}{% endblock %}
```

```js
// static/js/reports.js — load the chart library only when the chart is near the viewport
const el = document.querySelector("#revenue-chart");
if (el) {
  const io = new IntersectionObserver(async ([entry]) => {
    if (!entry.isIntersecting) return;
    io.disconnect();
    const { renderRevenueChart } = await import("./charts/revenue.js");  // separate chunk
    renderRevenueChart(el, JSON.parse(document.getElementById("revenue-data").textContent));
  }, { rootMargin: "200px" });
  io.observe(el);
}
```

- **Code splitting**: dynamic `import()` creates separate chunks (Vite/Rollup, esbuild with `splitting`). Use per-page entries for heavy pages.
- **Tree shaking** works only with ES module imports and side-effect-free packages: `import { debounce } from "lodash-es"`, not `import _ from "lodash"`. Check with a bundle visualizer.
- **Hydration/islands**: mount React only where needed, after server HTML shows content ([django-react-integration.md](django-react-integration.md)).
- Avoid shipping both jQuery and a framework; audit `package.json` for duplicates (`npm ls <pkg>`).
- `<link rel="modulepreload" href="…">` for critical module chunks discovered late (Vite manifest lists imports; django-vite (third-party) can emit these [VERIFY: django-vite tag/option names]).

### 5.6 CSS delivery

- One hashed CSS file in `<head>` is fine for most Django apps. Tailwind v4/v3 generate only used classes; production output for a dashboard is typically small — verify actual compressed size. Classes concatenated in Python/JS strings are not generated (functional bug, not perf) ([tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md)).
- Bootstrap: import only needed Sass partials instead of the full bundle.
- Critical CSS inlining trades cacheability and CSP simplicity for first-paint speed; adopt only when measurements show CSS download dominating render delay.
- Don't `@import` CSS from CSS (serial requests); don't load print/other-media CSS as blocking — use `media="print"`.

### 5.7 Resource hints

```django
<link rel="preconnect" href="https://media.example-cdn.com" crossorigin>
<link rel="dns-prefetch" href="https://media.example-cdn.com">
<link rel="preload" as="image" href="{% static 'img/hero-1200.avif' %}" fetchpriority="high">
<link rel="modulepreload" href="{% static 'js/app.js' %}">
```

| Hint | Use for | Overuse cost |
|---|---|---|
| `preconnect` | 1–3 critical third-party/CDN origins used early | Wasted sockets/TLS; competes with critical requests |
| `dns-prefetch` | Fallback/cheap hint for less critical origins | Minimal |
| `preload` | Late-discovered critical resource (LCP bg image, above-fold font) | Steals bandwidth; console warning if unused within ~3 s |
| `modulepreload` | Module graph of the entry point | Same as preload |
| `fetchpriority` | Raise LCP image, lower non-critical (`low`) | Diminishing returns if overused |

Django can send early hints only via the server/CDN; `103 Early Hints` support depends on the front proxy/CDN [VERIFY: whether your CDN/proxy emits 103 from Link headers].

### 5.8 Browser caching and compression

| Resource | Cache-Control | Why |
|---|---|---|
| Hashed static (`app.3f9c1a.js`, via `ManifestStaticFilesStorage` / WhiteNoise / Vite) | `public, max-age=31536000, immutable` | Content change = new URL; revalidation never needed |
| Unhashed static | short `max-age` + ETag | Can change in place |
| Public HTML | `max-age=0, must-revalidate` or short CDN TTL with `Vary` as needed | Must reflect deploys |
| Authenticated HTML | `private` (and `no-store` for sensitive data) | Never in shared caches ([frontend-security.md](frontend-security.md)) |
| HTMX fragments from dual-mode URLs | `Vary: HX-Request` | Separate cache entries |

WhiteNoise (third-party) serves hashed files with far-future headers and, with `CompressedManifestStaticFilesStorage`, precompresses gzip and Brotli (if a Brotli package is installed) at `collectstatic` time so no per-request compression is needed [VERIFY: WhiteNoise storage class name and Brotli dependency for installed version]. Configure via `STORAGES["staticfiles"]` (Django ≥ 4.2). HTML responses can be compressed by the proxy/CDN or `GZipMiddleware` (be aware of BREACH for pages reflecting secrets + user input). Pipeline: [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md); server/CDN delivery: [http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md).

Check that responses are bfcache-eligible (no `unload` handlers; `Cache-Control: no-store` may block it) — instant back/forward navigations are a large perceived-perf win (DevTools → Application → Back/forward cache).

### 5.9 Third-party scripts

- Inventory every tag (analytics, chat, A/B, maps); each needs an owner and a measured cost.
- Load with `defer`/`async`, after consent, only on pages that need them; replace heavy embeds (video, maps) with a click-to-load facade.
- Tag managers run arbitrary code on the main thread → INP; audit regularly.
- Security side: [frontend-security.md](frontend-security.md).

### 5.10 HTMX-specific performance

- **Fragment size**: return the changed region only; OOB for small distant updates; `hx-select` is a smell when it discards most of a large response.
- **Request coordination**: `hx-sync="this:replace"` cancels stale searches; `delay:300ms` debounces; `hx-disabled-elt` prevents double submits.
- **Polling cost**: requests/s ≈ active tabs ÷ interval. 1,000 tabs at `every 2s` = 500 req/s. Lengthen intervals, stop on completion (status 286 or drop `hx-trigger`), back off server-side (`load delay:Nms`), pause on hidden tabs, or use SSE.
- **Lazy widgets**: `hx-trigger="load"`/`intersect once` for slow dashboard cards — the page's LCP is the shell, cards fill in; reserve their height to avoid CLS.
- **Swap cost**: large `innerHTML` swaps re-parse and re-layout; prefer row-level `outerHTML` for inline edits. `hx-swap` `transition:true` uses View Transitions where supported.
- **Server side**: fragment endpoints need the same query optimization as pages ([django-application-performance.md](../../django-performance/references/django-application-performance.md)).
- Details: [htmx-engineering.md](htmx-engineering.md).

### 5.11 Performance budgets in CI

`lighthouserc.json` for Lighthouse CI (`@lhci/cli`):

```json
{
  "ci": {
    "collect": {
      "url": ["http://localhost:8000/", "http://localhost:8000/projects/"],
      "numberOfRuns": 3
    },
    "assert": {
      "assertions": {
        "largest-contentful-paint": ["error", { "maxNumericValue": 2500 }],
        "cumulative-layout-shift": ["error", { "maxNumericValue": 0.1 }],
        "total-blocking-time": ["warn", { "maxNumericValue": 200 }],
        "resource-summary:script:size": ["error", { "maxNumericValue": 150000 }],
        "resource-summary:stylesheet:size": ["warn", { "maxNumericValue": 50000 }]
      }
    },
    "upload": { "target": "temporary-public-storage" }
  }
}
```

```bash
npx @lhci/cli autorun   # runs collect → assert → upload using lighthouserc.json
```

[VERIFY: Lighthouse CI assertion keys and resource-summary syntax against current @lhci/cli docs]. For authenticated pages use a seeded test user and a Puppeteer login script, or a fixture page. Use `"upload": {"target": "filesystem"}` for private apps — `temporary-public-storage` publishes reports publicly. Add a bundle-size check on build output (compressed bytes per entry) so regressions fail in the PR. CI integration: [ci-cd-test-automation.md](../../django-testing/references/ci-cd-test-automation.md).

Example budget for a server-rendered dashboard (mobile, p75 field): LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1; JS ≤ 100–150 kB compressed per page; CSS ≤ 50 kB; requests ≤ 40; HTMX fragment p75 server time ≤ 200 ms.

## 6. Production use cases

- **Tenant dashboard with slow LCP**: field RUM shows LCP 4 s; breakdown shows 2.2 s TTFB → fix queries/caching first ([production-performance-troubleshooting.md](../../django-performance/references/production-performance-troubleshooting.md)); then lazy-load KPI cards via HTMX with reserved height.
- **Invoice list INP 600 ms**: Alpine filters 8k rows client-side on each keystroke → server-side filtering via HTMX with debounce + pagination; INP < 150 ms.
- **Marketing page CLS 0.3**: cookie banner injected at top + hero without dimensions → fixed-position banner, `width`/`height`, font metric overrides.
- **Report page with charts**: chart library (200 kB) moved to dynamic import on intersection; initial JS halved.
- **Status board polling every 1 s**: 3,000 req/s at peak → 10 s interval + SSE for live board + stop when hidden.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Server-rendered HTML (+HTMX) | Fast LCP, little JS, cacheable | TTFB-sensitive; round-trip per interaction |
| SPA | Fast in-app transitions after load | Large JS, slow LCP/INP on low-end devices without SSR |
| RUM | Real p75 per route, catches device mix | Instrumentation + storage + privacy work |
| Lighthouse CI | Prevents regressions pre-merge | Lab variance; needs stable environment, multiple runs |
| Critical CSS | Faster first paint | Build complexity, duplicated bytes, CSP hashes |
| Aggressive preloading | Earlier critical fetches | Easily counterproductive when overused |

## 8. Security and performance considerations

### Performance

- Fix in order of impact on the failing metric's p75, not in order of ease.
- Keep the LCP resource in initial HTML; keep the main thread free during load; reserve space for anything that appears late.
- Hashed + immutable static, compressed (Brotli/gzip), served from CDN or WhiteNoise.

### Security

- RUM endpoint: validate and cap payloads, rate-limit, store no PII or full URLs with tokens/IDs; consider consent requirements ([security-logging-and-auditing.md](../../django-security-engineering/references/security-logging-and-auditing.md)).
- Third-party performance tools and CDNs are also supply-chain risks: pin versions, SRI, minimal scope ([frontend-security.md](frontend-security.md)).
- Don't cache authenticated HTML publicly to "improve TTFB"; use `private` and server-side caching instead.
- Lighthouse CI `temporary-public-storage` uploads reports publicly — never for internal apps.
- Source maps help profiling but should not be public for private apps.

## 9. Common mistakes and anti-patterns

1. **Optimizing from a single Lighthouse score on `runserver`** → measure production-like builds; rely on field p75.
2. **`loading="lazy"` on the hero/LCP image** → remove; add `fetchpriority="high"`.
3. **Images without `width`/`height`** → add dimensions or `aspect-ratio`.
4. **Sync `<script>` in `<head>`** → `defer` or `type="module"`.
5. **Font preload without `crossorigin`** → add it (otherwise double download).
6. **Preloading everything** → preload only late-discovered critical resources.
7. **Shipping full icon/chart/utility libraries** → ESM named imports, dynamic import for heavy widgets.
8. **Client-side filtering of thousands of rows** → server pagination/filtering.
9. **Banner injected above content after load** → reserve space or overlay.
10. **Short `max-age` on hashed assets** → `immutable` one-year caching.
11. **No `Vary: HX-Request` on dual-mode URLs** → wrong cached variant, extra re-requests.
12. **Polling at 1–2 s forever** → stop on completion, back off, SSE.
13. **Blaming the frontend for TTFB** → check the LCP breakdown; route server time to django-performance.
14. **Treating budgets as aspirational** → enforce in CI with failing assertions.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| High LCP, high TTFB | Server/DB/caching | Server-Timing, APM, Debug Toolbar |
| High LCP, low TTFB, image starts late | Not discoverable (CSS bg/JS), lazy, low priority | Network waterfall start time vs HTML; `fetchpriority` |
| High LCP, text element | Render-blocking CSS/JS, font block | Performance panel render-blocking flags; `font-display` |
| CLS in field but not lab | Late banners, ads, HTMX widgets after user scroll, fonts on slow networks | `web-vitals/attribution` target selector; Layout Shifts track |
| High INP | Long tasks / heavy handler / large DOM | Performance panel interaction → task breakdown; DOM size in Lighthouse |
| Assets re-downloaded on every visit | Missing hashing/immutable headers | Response headers; `ManifestStaticFilesStorage` in `STORAGES` |
| Uncompressed responses | No precompression / proxy config | `content-encoding` header in Network panel |
| Lighthouse scores vary run to run | Environment noise | Multiple runs, median; stable CI runner; throttling settings |

Validation steps:

1. Establish baseline p75 per key route from RUM (and CrUX for public pages).
2. Reproduce in lab; record trace; identify LCP sub-part / long tasks / shift sources.
3. Apply one fix; compare lab before/after on the same environment.
4. Deploy; confirm field p75 improves over the following days/weeks (CrUX lags 28 days).
5. Encode the result as a budget assertion in CI.

## 11. Related references and skills

- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — rendering pipeline, event loop
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — hashing, WhiteNoise, Vite, source maps
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — responsive image markup, skeleton states
- [htmx-engineering.md](htmx-engineering.md) — `hx-sync`, polling, lazy loading
- [javascript-engineering.md](javascript-engineering.md) — dynamic imports, debounce, AbortController
- [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) — production CSS size
- [django-react-integration.md](django-react-integration.md) — SPA bundle and hydration costs
- [css-engineering.md](css-engineering.md) — containment, animations
- [frontend-security.md](frontend-security.md) — third-party scripts, caching of authenticated pages
- [frontend-testing.md](frontend-testing.md) — performance checks in the test strategy
- [django-performance](../../django-performance/SKILL.md): [django-application-performance.md](../../django-performance/references/django-application-performance.md), [http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md), [caching-engineering.md](../../django-performance/references/caching-engineering.md), [profiling-and-observability.md](../../django-performance/references/profiling-and-observability.md), [performance-testing-and-benchmarking.md](../../django-performance/references/performance-testing-and-benchmarking.md), [production-performance-troubleshooting.md](../../django-performance/references/production-performance-troubleshooting.md)
- [ci-cd-test-automation.md](../../django-testing/references/ci-cd-test-automation.md)

## 12. Official documentation and external references

- https://web.dev/articles/vitals
- https://web.dev/articles/lcp
- https://web.dev/articles/optimize-lcp
- https://web.dev/articles/inp
- https://web.dev/articles/optimize-inp
- https://web.dev/articles/cls
- https://web.dev/articles/optimize-cls
- https://web.dev/articles/ttfb
- https://web.dev/articles/fetch-priority
- https://web.dev/articles/font-best-practices
- https://web.dev/articles/bfcache
- https://web.dev/articles/content-visibility
- https://web.dev/articles/lighthouse-ci
- https://web.dev/articles/performance-budgets-101
- https://developer.chrome.com/docs/crux
- https://developer.chrome.com/docs/devtools/performance
- https://developer.chrome.com/docs/lighthouse/overview
- https://github.com/GoogleChrome/web-vitals
- https://github.com/GoogleChrome/lighthouse-ci
- https://developer.mozilla.org/en-US/docs/Web/HTML/Element/img
- https://developer.mozilla.org/en-US/docs/Web/HTML/Attributes/rel/preload
- https://developer.mozilla.org/en-US/docs/Web/CSS/@font-face/font-display
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control
- https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/#manifeststaticfilesstorage
- https://docs.djangoproject.com/en/5.2/ref/settings/#storages
- https://docs.djangoproject.com/en/5.2/ref/middleware/#module-django.middleware.gzip
- https://htmx.org/attributes/hx-sync/
- https://whitenoise.readthedocs.io/ (third-party package docs)
