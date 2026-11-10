---
name: django-frontend
description: >-
  Django frontend engineering specialist — frontend architecture selection (server-rendered,
  HTMX, Alpine.js, React islands, SPA), Django templates as UI composition, semantic HTML, CSS and
  design systems, responsive design, JavaScript, HTMX 2.x, Alpine.js 3.x, Tailwind CSS 4.x /
  Bootstrap, form UX and client validation, React 19 + Vite integration with Django/DRF, frontend
  state, accessibility (WCAG 2.2), browser-side security, Core Web Vitals, static assets and
  build pipelines, frontend testing (Playwright, Vitest, Testing Library) and production frontend
  architectures. Load when designing, building, reviewing, testing or shipping the browser-facing
  part of a Django application.
project: django-agentic-engineering
abstract: >-
  Choose the simplest frontend architecture that meets the requirements, build it on semantic
  HTML and server-rendered Django templates, enhance progressively with HTMX/Alpine or targeted
  React, and prove accessibility, security and performance in the browser. A concise workflow and
  navigation guide here; eighteen focused references loaded one to three at a time.
sdlc_phase: Development
tech_stack: [Python, Django, Django Templates, HTML, CSS, JavaScript, HTMX, Alpine.js, Tailwind CSS, React, Vite]
domain: [Software Engineering, Web, Frontend]
---

# Django Frontend Engineering Specialist

Engineering judgement for the browser-facing side of Django: which frontend architecture to
choose, how to compose templates into reusable UI, when JavaScript earns its place, and how to
verify accessibility, security and performance where users actually experience them.

Baseline: **Django 5.2 LTS · Python 3.12+ · HTML Living Standard · modern CSS · ES2022+ ·
HTMX 2.x · Alpine.js 3.x · Tailwind CSS 4.x · React 19.x · Vite**. Version-specific behaviour is
qualified ("Django ≥ 5.1", "Tailwind v4", "HTMX 2.x"); uncertain claims carry `[VERIFY: …]`.
Check the project's `requirements`/lock file and `package-lock.json` before recommending a
version-specific API. Third-party packages (django-htmx, django-template-partials, django-vite,
WhiteNoise, django-cors-headers, django-csp, …) are named as such — never assume installed.

## Activate when

- Choosing or reviewing a frontend architecture for a Django project.
- Writing or refactoring templates, layouts, components, partials or custom template tags.
- Building forms UX, client-side validation, dynamic fields, uploads or multi-step flows.
- Adding interactivity with HTMX, Alpine.js or vanilla JavaScript.
- Styling with CSS, Tailwind or Bootstrap; establishing design tokens or a design system.
- Integrating a React (or other SPA) frontend with Django/DRF: auth, CSRF, CORS, builds.
- Configuring static files, Vite/Tailwind builds, WhiteNoise/CDN delivery.
- Fixing accessibility, Core Web Vitals, XSS/CSP or browser-compatibility issues.
- Writing template, HTMX, JavaScript, component, browser, a11y or visual tests.

## Frontend engineering principles

1. **Simplest architecture that satisfies the requirements.** Server-rendered templates first;
   add HTMX for server interactions, Alpine for local UI state, React only where rich
   client-side state justifies a build pipeline and an API. Never default to an SPA.
2. **Progressive enhancement.** Semantic HTML that works (links navigate, forms submit) before
   any JavaScript; enhancements degrade to that baseline.
3. **The server is the authority.** Validation, permissions and business rules live in Django.
   Client validation is UX; hidden or disabled UI is not a security control.
4. **Native HTML before ARIA, CSS before JavaScript.** `<button>`, `<dialog>`, `<details>`,
   form validation attributes and CSS states first; ARIA only to fill genuine semantic gaps.
5. **Templates present, they don't compute.** Views/selectors prepare data; templates compose
   reusable components. Auto-escaping stays on; data reaches JS via `json_script`.
6. **Every interaction has loading, empty, error and success states** — and they are announced
   to assistive technology.
7. **Measure before optimising.** Field Core Web Vitals and DevTools traces decide what to fix.
8. **Assets are built, fingerprinted and collected** at deploy time; development serving is
   never production delivery.

## Frontend engineering workflow

1. **Understand** product requirements, users, devices and critical workflows.
2. **Identify** rendering and interaction needs: SEO, interactivity depth, offline, real-time.
3. **Choose** server-rendered, HTMX, hybrid (islands) or SPA → *Architecture selection* below.
4. **Define** the template/component structure and directory layout.
5. **Establish** design tokens and responsive layout rules.
6. **Implement** semantic HTML and accessible interactions.
7. **Add JavaScript only where necessary** (HTMX/Alpine/vanilla/React, in that order of weight).
8. **Integrate** Django forms and backend communication (fragments or API).
9. **Configure** static assets and build tooling.
10. **Implement** loading, empty and error states.
11. **Test** accessibility, responsiveness and browser behaviour.
12. **Optimise** performance from measurements.
13. **Validate** production asset delivery and security headers.

Load one to three references per step — never all.

## Architecture selection

| Requirement profile | Choose | Reference |
|---|---|---|
| CRUD, content, forms, reports; SEO; small team | Server-rendered templates | [production-frontend-architectures.md](references/production-frontend-architectures.md) |
| Above + live search, inline edit, partial updates, modals | Templates + HTMX | [htmx-engineering.md](references/htmx-engineering.md) |
| Above + local widgets (tabs, dropdowns, toggles, client-only filters) | + Alpine.js | [alpine-js-engineering.md](references/alpine-js-engineering.md) |
| Mostly server-rendered, a few rich widgets (editor, chart builder, kanban) | Hybrid: templates + React islands | [django-react-integration.md](references/django-react-integration.md) |
| App-like UX, heavy client state, offline, multiple clients sharing an API, separate FE team | DRF API + React SPA | [django-react-integration.md](references/django-react-integration.md) |

Escalate only when a concrete requirement fails at the current level; record the decision in an
ADR ([django-architecture](../django-architecture/SKILL.md)). Full criteria:
[frontend-engineering-fundamentals.md](references/frontend-engineering-fundamentals.md).

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [frontend-engineering-fundamentals.md](references/frontend-engineering-fundamentals.md) | Browser architecture, HTTP from the browser, DOM/CSSOM, rendering pipeline, reflow/repaint, event loop, progressive enhancement, SSR vs CSR, SEO, compatibility, architecture selection criteria |
| [django-template-engineering.md](references/django-template-engineering.md) | Template organization, inheritance/blocks, includes, partials, context processors, custom tags/filters, reusable components (layouts, nav, tables, dashboards), escaping, rendering performance, debugging |
| [html-and-semantic-ui.md](references/html-and-semantic-ui.md) | Document structure, semantic elements, headings, nav, form markup, tables, buttons vs links, inputs/labels/fieldsets, `<dialog>`, `<details>`, metadata, SEO/Open Graph |
| [css-engineering.md](references/css-engineering.md) | Cascade, specificity, `@layer`, box model, positioning, flex/grid, units, media/container queries, custom properties, animation, CSS architecture & naming, tokens, dark mode |
| [responsive-design-and-design-systems.md](references/responsive-design-and-design-systems.md) | Mobile-first, breakpoints, fluid type/layout, responsive images, nav patterns, responsive tables, dashboards, tokens, UI states, lightweight design systems |
| [javascript-engineering.md](references/javascript-engineering.md) | ES modules, async, events/delegation, DOM, fetch/FormData, storage/cookies, AbortController, debounce/throttle, lazy/dynamic imports, memory leaks, debugging |
| [htmx-engineering.md](references/htmx-engineering.md) | HTMX 2.x attributes, requests, targets/swaps, triggers, OOB, history, partial views, search/pagination/infinite scroll/modals/inline edit, CSRF, auth, errors, security |
| [alpine-js-engineering.md](references/alpine-js-engineering.md) | Alpine 3.x directives, components, stores, events, lifecycle, HTMX/Django integration, CSP build, accessible widgets |
| [tailwind-and-ui-frameworks.md](references/tailwind-and-ui-frameworks.md) | Tailwind v4 vs v3, `@theme` tokens, variants, dark mode, component extraction, build integration, Bootstrap comparison, component libraries, shadcn-style components |
| [django-forms-and-validation.md](references/django-forms-and-validation.md) | Form rendering/templates, widgets, error presentation, HTML5/client vs server validation, dynamic/dependent fields, HTMX/AJAX submit, uploads UI, multi-step, formsets UI |
| [django-react-integration.md](references/django-react-integration.md) | Topologies (same-origin, cross-origin, islands), session vs token auth, CSRF/CORS/cookies from the SPA, API client, loading/retry, pagination, routing, Vite proxy, env vars, SPA deploy |
| [frontend-state-and-interaction-design.md](references/frontend-state-and-interaction-design.md) | Local/server/form/URL/persistent state, optimistic UI, UI states, dialogs, toasts, search/filter/pagination state, race conditions, cancellation, avoiding global state |
| [frontend-accessibility.md](references/frontend-accessibility.md) | WCAG 2.2 levels, keyboard, focus management, screen readers, ARIA (and when not), accessible forms/dialogs/nav/tables, live regions, contrast, reduced motion, a11y testing |
| [frontend-security.md](references/frontend-security.md) | XSS/DOM XSS sinks, safe HTML, CSP (nonces, HTMX/Alpine impact), cookies, CSRF in JS, CORS, clickjacking, open redirects, client secrets, storage, dependencies, SRI, data exposure |
| [frontend-performance.md](references/frontend-performance.md) | Core Web Vitals, measurement (Lighthouse, RUM), bundles, splitting, CSS delivery, images, fonts, caching/compression, rendering, third parties, budgets |
| [static-assets-and-build-engineering.md](references/static-assets-and-build-engineering.md) | Staticfiles pipeline, `STORAGES`, `collectstatic`, Manifest storage, WhiteNoise, CDN, fingerprinting, source maps, Vite/Tailwind CLI, dev vs prod, npm lockfiles, CI |
| [frontend-testing.md](references/frontend-testing.md) | Unit vs integration vs browser vs E2E, template/HTMX tests, Vitest, Testing Library, React tests, Playwright, axe, visual regression, responsive/cross-browser |
| [production-frontend-architectures.md](references/production-frontend-architectures.md) | Eight reference architectures with structure, rendering, state, auth, security, assets, performance, testing, deployment and trade-offs |

## Task-based loading

| Task | Load |
|---|---|
| Choose a frontend stack for a new Django project | frontend-engineering-fundamentals.md, production-frontend-architectures.md |
| Build base layout, navigation and reusable components | django-template-engineering.md, html-and-semantic-ui.md |
| Dashboard or data table with sorting, filters, pagination | django-template-engineering.md, htmx-engineering.md, frontend-state-and-interaction-design.md |
| Live search, inline editing, modal forms, infinite scroll | htmx-engineering.md (+ django-forms-and-validation.md) |
| Dropdowns, tabs, toggles, client-only UI state | alpine-js-engineering.md (+ frontend-accessibility.md) |
| Form UX, error display, dependent fields, uploads, wizards | django-forms-and-validation.md (+ htmx-engineering.md) |
| Page layout, CSS architecture, dark mode | css-engineering.md (+ responsive-design-and-design-systems.md) |
| Design tokens / design system / responsive dashboard | responsive-design-and-design-systems.md, css-engineering.md |
| Set up or migrate Tailwind (v3 → v4), or choose vs Bootstrap | tailwind-and-ui-frameworks.md, static-assets-and-build-engineering.md |
| Vanilla JS feature (fetch, autosave, clipboard, lazy load) | javascript-engineering.md (+ frontend-security.md) |
| React SPA or React islands on Django/DRF | django-react-integration.md, static-assets-and-build-engineering.md |
| SPA login, CSRF failures, CORS errors | django-react-integration.md, frontend-security.md |
| Race conditions, optimistic UI, toasts, URL state | frontend-state-and-interaction-design.md |
| Accessibility audit or WCAG remediation | frontend-accessibility.md, html-and-semantic-ui.md |
| XSS finding, CSP rollout, third-party scripts | frontend-security.md (+ django-security-engineering) |
| Slow page / poor LCP, INP or CLS | frontend-performance.md (+ django-performance for TTFB) |
| Static files 404, stale assets, Vite/collectstatic pipeline, CDN | static-assets-and-build-engineering.md |
| Frontend test strategy, Playwright, Vitest, axe, visual tests | frontend-testing.md (+ django-testing) |
| Multi-tenant SaaS or admin/reporting UI design | production-frontend-architectures.md (+ frontend-security.md) |

## Skill pairings and handoffs

| Topic | Skill |
|---|---|
| Template engine/loader lifecycle, views, URLs, Form/ModelForm mechanics, static file fundamentals, sessions | [django-core](../django-core/SKILL.md) |
| App boundaries, service/selector layers, ADRs | [django-architecture](../django-architecture/SKILL.md) |
| API endpoints, serializers, DRF auth/permissions, pagination and error formats | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Backend enforcement: CSRF/CORS/CSP settings, authz, sessions, XSS sanitisation, uploads | [django-security-engineering](../django-security-engineering/SKILL.md) |
| TTFB, queries, caching, HTTP/static delivery at the server | [django-performance](../django-performance/SKILL.md) |
| Test architecture, Django test client, pytest-django, CI | [django-testing](../django-testing/SKILL.md) |

Rule of thumb: this skill decides **what the browser receives and how users interact with it**;
the paired skill owns **the server-side mechanism and its enforcement**.

## Architecture decision guidelines

| Decision | Prefer | Because |
|---|---|---|
| Rendering model | Server-rendered HTML | SEO, fast first paint, one source of truth, no API duplication |
| Partial updates | HTMX returning template fragments from the same view | Reuses templates, forms and permissions; no client state to sync |
| Local UI behaviour | Native HTML → CSS → Alpine → vanilla module | Least code, least to break |
| Rich client app | React islands before a full SPA | Keeps Django auth, routing, forms and SEO for the rest |
| Browser auth for own frontend | Session cookie + CSRF, same-site | HttpOnly, revocable; no tokens in JS-readable storage |
| Styling | Project tokens as CSS custom properties; Tailwind or Bootstrap only if the team benefits | Frameworks add build and conventions cost |
| Components | Django includes/inclusion tags (partials where available) | One markup source for pages and fragments |
| Data to JS | `json_script` / `data-*` attributes | No inline-script XSS, works with CSP |
| Build tooling | None → Tailwind standalone CLI → Vite | Add a Node toolchain only when bundling is needed |
| Validation | Django forms/serializers authoritative; HTML5 attributes for UX | Client checks are bypassable |
| State | URL → server → local component; global store last | Shareable, back-button-safe, fewer sync bugs |

## Production validation checklist

- [ ] Architecture choice is documented and the simplest that meets requirements.
- [ ] Core pages and forms work without JavaScript, or the exception is justified.
- [ ] Semantic landmarks, one `<h1>`, logical headings, labelled controls, visible focus.
- [ ] Keyboard-only pass on critical flows; dialogs trap and restore focus; swaps announce changes.
- [ ] WCAG 2.2 AA automated checks (axe) pass; contrast and target sizes verified.
- [ ] No `|safe`/`mark_safe` on user data; no `innerHTML` with untrusted data; JSON via `json_script`.
- [ ] CSRF on every unsafe request (forms, HTMX, fetch, SPA); CORS is an explicit allow-list.
- [ ] CSP deployed (report-only first) and compatible with HTMX/Alpine/inline scripts in use.
- [ ] No secrets in templates, bundles, `VITE_*` vars or public source maps.
- [ ] Every HTMX/API endpoint enforces auth and permissions server-side; hidden UI is not relied upon.
- [ ] Loading, empty, error and success states exist for each async interaction.
- [ ] `collectstatic` runs with manifest storage in the build; hashed assets cached `immutable`; HTML not.
- [ ] `npm ci` from a committed lockfile; dependencies audited; third-party scripts reviewed (SRI where static).
- [ ] Field Core Web Vitals within budget (LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 at p75).
- [ ] Template/HTMX tests, JS/component tests and a few Playwright journeys run in CI on target browsers.
- [ ] Claims marked `[VERIFY: …]` were checked against the installed versions or disclosed.
- Docs: <https://docs.djangoproject.com/en/5.2/topics/templates/> · <https://developer.mozilla.org/> ·
  <https://htmx.org/docs/> · <https://www.w3.org/WAI/standards-guidelines/wcag/> · <https://web.dev/articles/vitals>
