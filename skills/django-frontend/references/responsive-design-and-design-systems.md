# Responsive Design and Design Systems

> **Scope:** Mobile-first workflow, content-driven breakpoints, fluid layout and typography, responsive image *markup*, responsive navigation and tables, dashboard layouts, design tokens (primitive → semantic → component), colour/type/spacing systems, reusable UI-state components (loading/empty/error), touch targets, reflow, and how far to take a design system in Django templates. CSS language mechanics are in [css-engineering.md](css-engineering.md); image delivery/performance in [frontend-performance.md](frontend-performance.md); state *logic* for loading/empty/error in [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md).

## 1. Purpose and scope

Use this when a Django UI must work from a 320 CSS px phone to a 2560 px monitor, when building the shared components (buttons, cards, tables, empty states) used across many templates, or when someone proposes "a design system". The goal is a small, consistent, accessible component set rendered server-side — not a framework.

Accessibility requirements that drive this reference (WCAG 2.2 AA):
- **1.4.10 Reflow** — content usable at 320 CSS px wide (≈ 1280 px at 400% zoom) without two-dimensional scrolling, except content that needs 2D layout (data tables, maps, diagrams).
- **1.4.4 Resize Text** — text resizable to 200% without loss.
- **1.4.12 Text Spacing** — no loss when users increase line/letter/word/paragraph spacing.
- **2.5.8 Target Size (Minimum)** — pointer targets ≥ 24×24 CSS px, or enough spacing that a 24 px circle centred on each target doesn't overlap another (exceptions: inline links in text, equivalent control elsewhere, UA-default controls, essential). 2.5.5 (AAA) is 44×44.
- **1.3.4 Orientation** — don't lock to portrait/landscape.

## 2. Core concepts

### Mobile-first

Write the base (unqueried) styles for the narrowest layout; add `@media (width >= …)` layers that *add* columns. Benefits: the simplest layout is the default (also what old/odd browsers get), fewer overrides, and narrow-screen constraints force content prioritisation. Desktop-first (`max-width` queries) produces override-of-override cascades.

### Content-driven breakpoints

Don't pick breakpoints from device lists. Resize the browser until the layout breaks (line length > ~75ch, cards too squeezed, nav wraps) and add a breakpoint there. Keep a few **named, global** breakpoints for the shell and use **container queries** for components.

| Token | Value | Typical meaning |
|---|---|---|
| `--bp-sm` | 40rem (640 px) | two-column forms / cards |
| `--bp-md` | 48rem (768 px) | tables gain columns |
| `--bp-lg` | 64rem (1024 px) | persistent sidebar |
| `--bp-xl` | 80rem (1280 px) | dashboard 3–4 columns |

Custom properties cannot be used inside media query conditions; keep breakpoint values in one documented place (or use `@custom-media` via a build tool — not natively supported [VERIFY: @custom-media browser support]).

Use `rem` in media queries: if a user sets a larger default font size, rem breakpoints trigger earlier, giving text more room.

### Fluid layout

Prefer intrinsic layouts that need no breakpoints: `repeat(auto-fit, minmax(min(100%, 18rem), 1fr))`, `flex-wrap` with `flex-basis`, `max-inline-size: 65ch` measure, `clamp()` for gutters.

### Fluid typography

```css
:root {
  --step--1: clamp(0.83rem, 0.80rem + 0.15vw, 0.94rem);
  --step-0:  clamp(1rem,    0.95rem + 0.25vw, 1.125rem);
  --step-1:  clamp(1.2rem,  1.10rem + 0.50vw, 1.5rem);
  --step-2:  clamp(1.44rem, 1.25rem + 0.90vw, 2rem);
  --step-3:  clamp(1.73rem, 1.40rem + 1.60vw, 2.67rem);
}
```

The `rem` term in the preferred value keeps text responsive to browser zoom and user font settings; a pure `vw` value does not scale with zoom and can fail 1.4.4. Keep the max/min ratio ≤ ~2.5 so 200% zoom still enlarges text. Body text in apps (tables, forms) usually should *not* be fluid — fixed `1rem` reads better; use fluid steps for headings and marketing pages.

### Responsive images

| Need | Markup |
|---|---|
| Same image, different resolutions | `<img srcset="… 480w, … 960w" sizes="…">` |
| Fixed-size image (avatar), HiDPI | `srcset="a.jpg 1x, a@2x.jpg 2x"` |
| Different crop per layout (art direction) | `<picture><source media="…" srcset="…">…<img></picture>` |
| Modern format with fallback | `<picture><source type="image/avif" srcset="…"><img src="….jpg"></picture>` |

- `sizes` tells the browser the rendered width *before* layout; wrong `sizes` → oversized downloads. `sizes="auto"` with `loading="lazy"` is a newer option [VERIFY: sizes=auto browser support].
- Always set `width` and `height` attributes (intrinsic ratio) so the browser reserves space → no CLS. CSS `max-inline-size: 100%; block-size: auto` keeps it responsive.
- `loading="lazy"` for below-the-fold images only; never on the LCP image. LCP hero: `fetchpriority="high"`.
- `alt` is required: descriptive for informative images, `alt=""` for decorative.

### Design tokens — three tiers

| Tier | Example | Who references it |
|---|---|---|
| Primitive (palette/scale) | `--blue-600`, `--space-4`, `--font-size-3` | only semantic tokens |
| Semantic (purpose) | `--color-accent`, `--color-text-muted`, `--color-danger-bg`, `--space-stack` | components, utilities |
| Component (optional) | `--button-bg`, `--card-padding` | the one component; defaults to semantic tokens |

Themes (dark, tenant brand, high density) reassign **semantic** tokens; components never reference primitives directly. Add component tokens only when a component needs independent theming.

### Systems

- **Colour**: neutral ramp (≈ 10 steps), one accent, status colours (success/warning/danger/info) each with bg/fg/border semantic tokens. Validate contrast for every text/background pair in both themes: 4.5:1 body text, 3:1 large text and UI component boundaries/focus indicators (1.4.3, 1.4.11). Never convey status by colour alone (1.4.1) — pair with text/icon.
- **Type**: one or two families (system font stack is free and fast), a modular scale of 5–7 steps, line-height 1.4–1.6 body / 1.1–1.3 headings, `font-variant-numeric: tabular-nums` for numbers in tables.
- **Spacing**: a 4 px-based scale (`0.25rem` steps) with ~8 tokens; layout primitives (stack, cluster, sidebar) consume them.
- **Radius, shadow, z-index, motion duration**: 3–4 tokens each.

### UI states

Every data-driven component has at least: **loading**, **empty** (two kinds: "nothing yet" vs "no results for this filter"), **error**, **populated**, often **partial** (some failed) and **permission-limited**. Design them as first-class components, not afterthoughts.

## 3. Internal mechanics

### How the browser picks an image from `srcset`/`sizes`

1. Preload scanner sees `<img>` before CSS/layout is known.
2. Evaluates `sizes` media conditions left-to-right; first match gives the slot width (e.g., `33vw`).
3. Multiplies by device pixel ratio → target pixel width; chooses the candidate from `srcset` (browsers may pick lower on slow networks/data-saver, or keep a cached larger one).
4. For `<picture>`, the first `<source>` whose `media` and `type` match wins; the `<img>` supplies `alt`, dimensions, `loading`, and the fallback.

Because selection happens before layout, `sizes` must approximate the CSS layout. If the image is 100% wide below 40rem, half above, and capped at 36rem: `sizes="(width >= 80rem) 36rem, (width >= 40rem) 50vw, 100vw"`.

### Aspect ratio from attributes

Browsers map `width`/`height` attributes to a default `aspect-ratio: auto w / h`, so space is reserved even with `block-size: auto` before the image loads.

### Zoom, `rem` and reflow

Browser zoom enlarges the CSS px → the viewport *in CSS px* shrinks (1280 px at 400% ≈ 320 CSS px), so mobile-first rules apply — which is why responsive layouts satisfy 1.4.10 almost for free. `rem` media queries also respond to user default font size changes, which zoom does not alter.

### Touch input

`@media (pointer: coarse)` and `(hover: none)` describe the *primary* input; `any-pointer`/`any-hover` describe any available input. Laptops with touchscreens report `pointer: fine`. Don't hide functionality behind hover; use hover as enhancement.

### Native top layer for navigation overlays

`<dialog>.showModal()` puts the dialog in the top layer, makes the rest of the document inert, traps focus inside, closes on Escape and returns focus to the previously focused element on close (in current browsers). That is the off-canvas drawer behaviour you would otherwise hand-roll. The `popover` attribute (Baseline 2024) gives light-dismiss non-modal overlays without JS.

## 4. Engineering decisions and trade-offs

### Navigation patterns

| Prefer a disclosure button (`<button aria-expanded>` toggling a list) when… | Prefer an off-canvas `<dialog>` when… | Prefer visible nav (no toggle) when… |
|---|---|---|
| ≤ ~8 items, nav pushes content down | many items/sections, account switcher, filters | ≤ 5 items fit at 320 px (horizontal scroll strip or wrap) |
| Must work without JS (use `<details>` or render open + CSS) | Modal behaviour (focus containment) is desired | best discoverability |

### Responsive tables

| Pattern | Prefer when | Costs |
|---|---|---|
| Horizontal scroll container (focusable region) | Real tabular data, users compare across rows/columns (invoices, reports) | Needs scroll affordance; 1.4.10 explicitly allows 2D scroll for tables |
| Priority columns (hide low-priority columns below a breakpoint, show in detail view) | Many columns, some optional | Hidden data must be reachable elsewhere (row link) |
| Stacked cards (each row → card with `label: value`) | Lists of records where users act per item, not compare | Loses table semantics if you restyle `<table>` with `display: block` (screen readers may drop table semantics); duplicate markup or render a list instead; compare-across-rows impossible |
| Render a different template for small screens | — | Avoid: Django can't know viewport width; use CSS |

### How much design system?

| Stop at "tokens + base styles + ~10 includes" when… | Go further (documented component library, visual tests, versioning) when… |
|---|---|
| One product, one team, < ~50 templates | Several Django projects/teams share UI |
| Designers work in the code | Tenants/white-label need guaranteed theming contracts |
| Components change with features | Components are stable and reused > 5× each |

Rule of thumb: extract a component on the **third** duplicate, not the first. A component earns its include when it has non-trivial markup *and* accessibility behaviour (labels, ARIA wiring, states).

### Component implementation in Django

| `{% include "components/x.html" with … only %}` | Inclusion tag (`@register.inclusion_tag`) |
|---|---|
| Pure presentation, few params | Needs computation (srcset building, permission checks, URL building, defaults validation) |
| Designers edit freely | Want a Python-level API with defaults and type checks |

`only` prevents accidental context leakage and makes the include's contract explicit. Partials (`{% partialdef %}`) are built in only from Django 6.0; on 5.2 use django-template-partials (third-party) — see [django-template-engineering.md](django-template-engineering.md).

## 5. Django/frontend implementation patterns

### Directory layout

```text
apps/ui/
  templatetags/ui.py
  templates/ui/
    button.html  card.html  empty_state.html  error_state.html
    skeleton_rows.html  responsive_image.html  stat_tile.html
static/css/
  tokens.css  layout.css  components/*.css
```

### Tokens (primitive → semantic → component)

```css
/* tokens.css */
@layer tokens {
  :root {
    /* primitive */
    --gray-0: #fff; --gray-50: #f7f7f8; --gray-200: #e3e4e8; --gray-600: #5b5f6b; --gray-900: #16181d;
    --blue-600: #2459d6; --blue-300: #8fb0ff; --red-700: #b42318; --red-50: #fef3f2;
    --size-1: 0.25rem; --size-2: 0.5rem; --size-3: 0.75rem; --size-4: 1rem; --size-6: 1.5rem; --size-8: 2rem;

    /* semantic */
    color-scheme: light dark;
    --color-bg:           light-dark(var(--gray-50), var(--gray-900));
    --color-surface:      light-dark(var(--gray-0), #1f2229);
    --color-text:         light-dark(var(--gray-900), var(--gray-50));
    --color-text-muted:   light-dark(var(--gray-600), #a6aab5);
    --color-border:       light-dark(var(--gray-200), #353944);
    --color-accent:       light-dark(var(--blue-600), var(--blue-300));
    --color-on-accent:    light-dark(var(--gray-0), var(--gray-900));
    --color-danger-fg:    light-dark(var(--red-700), #ffb4ab);
    --color-danger-bg:    light-dark(var(--red-50), #3b1513);
    --space-inline:       var(--size-4);
    --space-stack:        var(--size-4);
    --radius:             0.375rem;
    --target-min:         2.75rem;   /* 44px: comfortably above WCAG 2.5.8's 24px minimum */
  }
}
```

Dark-mode toggle and `light-dark()` fallbacks: see [css-engineering.md](css-engineering.md) §5.

### Layout primitives (CUBE-style composition)

```css
@layer layout {
  .l-stack > * + * { margin-block-start: var(--stack-gap, var(--space-stack)); }
  .l-cluster { display: flex; flex-wrap: wrap; gap: var(--cluster-gap, var(--size-2)); align-items: center; }
  .l-sidebar { display: flex; flex-wrap: wrap; gap: var(--size-6); }
  .l-sidebar > :first-child { flex-basis: 16rem; flex-grow: 1; }
  .l-sidebar > :last-child  { flex-basis: 0; flex-grow: 999; min-inline-size: 60%; } /* wraps without a media query */
  .l-center { max-inline-size: var(--measure, 72rem); margin-inline: auto; padding-inline: clamp(var(--size-4), 4vw, var(--size-8)); }
}
```

### Responsive image inclusion tag

Assume an `Attachment` model with stored renditions (generated at upload by your pipeline) exposing `renditions: dict[int, str]` (width → URL) plus `width`/`height`.

```python
# apps/ui/templatetags/ui.py
from django import template

register = template.Library()

@register.inclusion_tag("ui/responsive_image.html")
def responsive_image(image, alt, sizes="100vw", lazy=True, priority=False, css_class=""):
    """Render <img srcset> with intrinsic dimensions to avoid CLS.

    `image` must expose .url, .width, .height, .renditions {width: url}.
    `alt` is required on purpose: pass "" explicitly for decorative images.
    """
    renditions = sorted(image.renditions.items())
    srcset = ", ".join(f"{url} {w}w" for w, url in renditions)
    return {
        "src": renditions[-1][1] if renditions else image.url,
        "srcset": srcset,
        "sizes": sizes,
        "alt": alt,
        "width": image.width,
        "height": image.height,
        "loading": "eager" if priority or not lazy else "lazy",
        "fetchpriority": "high" if priority else "",
        "css_class": css_class,
    }
```

```django
{# apps/ui/templates/ui/responsive_image.html #}
<img src="{{ src }}"{% if srcset %} srcset="{{ srcset }}" sizes="{{ sizes }}"{% endif %}
     width="{{ width }}" height="{{ height }}" alt="{{ alt }}"
     loading="{{ loading }}" decoding="async"{% if fetchpriority %} fetchpriority="{{ fetchpriority }}"{% endif %}
     {% if css_class %}class="{{ css_class }}"{% endif %}>
```

```django
{% load ui %}
{# project header hero (likely LCP) #}
{% responsive_image project.cover alt=project.cover_alt sizes="(width >= 64rem) 60rem, 100vw" priority=True %}
{# card thumbnails in a 1/2/3-column grid #}
{% responsive_image p.cover alt="" sizes="(width >= 80rem) 24rem, (width >= 40rem) 50vw, 100vw" %}
```

URLs are auto-escaped by the template engine. Art direction / AVIF fallback:

```django
<picture>
  <source type="image/avif" srcset="{{ banner.avif_srcset }}" sizes="100vw">
  <source media="(width < 40rem)" srcset="{{ banner.mobile_crop_url }}">
  <img src="{{ banner.url }}" width="{{ banner.width }}" height="{{ banner.height }}" alt="{{ banner.alt }}">
</picture>
```

Note: with different crops, the `<img>` width/height only describe the fallback ratio; set `width`/`height` on each `<source>` (supported in current browsers [VERIFY: source width/height support]) or use CSS `aspect-ratio` per breakpoint.

### Navigation: disclosure button (progressive)

```django
{# templates/components/topbar.html #}
<div class="c-topbar">
  <a class="c-topbar__brand" href="{% url 'dashboard' %}">Acme Projects</a>
  <button class="c-topbar__toggle" type="button" aria-expanded="false" aria-controls="primary-nav" hidden>
    Menu
  </button>
  <nav id="primary-nav" class="c-topbar__nav" aria-label="Primary">
    <ul class="l-cluster" role="list">
      {% for item in nav_items %}
        <li><a href="{{ item.url }}"{% if item.active %} aria-current="page"{% endif %}>{{ item.label }}</a></li>
      {% endfor %}
    </ul>
  </nav>
</div>
```

```js
// static/js/nav.js — the button is hidden until JS runs, so no-JS users get an always-visible nav.
for (const btn of document.querySelectorAll(".c-topbar__toggle")) {
  const nav = document.getElementById(btn.getAttribute("aria-controls"));
  const mq = window.matchMedia("(width >= 48rem)");
  const sync = () => {
    btn.hidden = mq.matches;
    nav.hidden = !mq.matches && btn.getAttribute("aria-expanded") !== "true";
  };
  btn.addEventListener("click", () => {
    btn.setAttribute("aria-expanded", String(btn.getAttribute("aria-expanded") !== "true"));
    sync();
  });
  mq.addEventListener("change", sync);
  sync();
}
```

`aria-expanded` is needed here because a plain `<button>` doesn't expose the state natively. `role="list"` restores list semantics that Safari/VoiceOver drops when `list-style: none` is applied. A zero-JS alternative is `<details><summary>Menu</summary><nav>…</nav></details>`.

### Navigation: off-canvas drawer with `<dialog>`

```django
<button type="button" class="c-btn" data-open-drawer="nav-drawer">Menu</button>
<dialog id="nav-drawer" class="c-drawer" aria-labelledby="nav-drawer-title">
  <div class="c-drawer__header">
    <h2 id="nav-drawer-title">Navigation</h2>
    <form method="dialog"><button class="c-btn c-btn--ghost">Close</button></form>
  </div>
  {% include "components/sidebar_nav.html" %}
</dialog>
```

```css
@layer components {
  .c-drawer {
    margin: 0; inset-block: 0; inset-inline-start: 0;
    block-size: 100dvh; max-block-size: none; inline-size: min(20rem, 85vw);
    border: 0; padding: var(--size-4); background: var(--color-surface); color: var(--color-text);
  }
  .c-drawer::backdrop { background: oklch(0 0 0 / 0.4); }
  @media (prefers-reduced-motion: no-preference) {
    .c-drawer[open] { animation: drawer-in 180ms ease-out; }
    @keyframes drawer-in { from { transform: translateX(-100%); } }
  }
}
```

```js
document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-open-drawer]");
  if (opener) document.getElementById(opener.dataset.openDrawer)?.showModal();
  const dlg = e.target.closest("dialog.c-drawer");
  if (dlg && e.target === dlg) dlg.close(); // click on backdrop area
});
```

Newer browsers support `<dialog closedby="any">` and invoker commands (`command="show-modal"`) which remove the JS — progressive only [VERIFY: closedby / invoker commands Baseline status].

### Responsive table include

```django
{# billing/templates/billing/_invoice_lines.html #}
<div class="c-table-scroll" role="region" aria-labelledby="inv-lines-cap" tabindex="0">
  <table>
    <caption id="inv-lines-cap">Line items for invoice {{ invoice.number }}</caption>
    <thead><tr><th scope="col">Item</th><th scope="col" class="is-optional">SKU</th><th scope="col">Qty</th><th scope="col">Amount</th></tr></thead>
    <tbody>{% for line in invoice.lines.all %}
      <tr><th scope="row">{{ line.description }}</th><td class="is-optional">{{ line.sku }}</td><td>{{ line.quantity }}</td><td>{{ line.amount }}</td></tr>
    {% endfor %}</tbody>
  </table>
</div>
```

`{% include %}` cannot wrap arbitrary content (no slots), so put the wrapper in the table template itself rather than passing pre-rendered HTML (which would need `|safe`). Sticky-header variant: [css-engineering.md](css-engineering.md) §5. Rules: wrapper is a named `region` with `tabindex="0"` so keyboard users can scroll it; `<caption>` names the table; add a visible shadow cue on the scrolling edge:

```css
.c-table-scroll {
  overflow-x: auto;
  background:
    linear-gradient(to right, var(--color-surface) 30%, transparent) left / 2rem 100% no-repeat local,
    linear-gradient(to left,  var(--color-surface) 30%, transparent) right / 2rem 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, oklch(0 0 0 / .2), transparent) left / .75rem 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, oklch(0 0 0 / .2), transparent) right / .75rem 100% no-repeat scroll;
}
@media (width < 48rem) { .c-table-scroll .is-optional { display: none; } } /* priority columns */
```

### Dashboard layout

```django
{# projects/templates/projects/dashboard.html #}
{% extends "layouts/app_shell.html" %}
{% block content %}
<h1>Dashboard</h1>
<section class="c-dashboard" aria-label="Key metrics">
  {% for stat in stats %}{% include "ui/stat_tile.html" with label=stat.label value=stat.value delta=stat.delta only %}{% endfor %}
</section>
<div class="c-dashboard c-dashboard--panels">
  <section class="c-panel" aria-labelledby="overdue-h"><h2 id="overdue-h">Overdue tasks</h2>{% include "projects/_overdue.html" %}</section>
  <section class="c-panel" aria-labelledby="invoices-h"><h2 id="invoices-h">Unpaid invoices</h2>{% include "billing/_unpaid.html" %}</section>
</div>
{% endblock %}
```

```css
@layer components {
  .c-dashboard { display: grid; gap: var(--size-4); grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr)); }
  .c-dashboard--panels { grid-template-columns: repeat(auto-fit, minmax(min(100%, 28rem), 1fr)); margin-block-start: var(--size-6); }
  .c-panel { container: panel / inline-size; background: var(--color-surface); border: 1px solid var(--color-border);
             border-radius: var(--radius); padding: var(--size-4); min-inline-size: 0; }
  @container panel (inline-size < 30rem) { .c-panel .is-optional { display: none; } }
}
```

Order panels by priority in the DOM (that's the mobile order and screen-reader order). Load expensive panels lazily with HTMX `hx-trigger="revealed"` or `load` (see [htmx-engineering.md](htmx-engineering.md)) and show a skeleton meanwhile.

### UI-state components

```python
# apps/ui/templatetags/ui.py (continued)
@register.inclusion_tag("ui/empty_state.html")
def empty_state(title, body="", action_url="", action_label="", kind="first-use"):
    if kind not in {"first-use", "no-results", "cleared"}:
        raise ValueError(f"unknown empty_state kind: {kind!r}")
    return {"title": title, "body": body, "action_url": action_url, "action_label": action_label, "kind": kind}

@register.inclusion_tag("ui/error_state.html")
def error_state(title="Something went wrong", body="", retry_url="", reference=""):
    return locals()
```

```django
{# ui/empty_state.html #}
<div class="c-state c-state--empty" data-kind="{{ kind }}">
  <h2 class="c-state__title">{{ title }}</h2>
  {% if body %}<p>{{ body }}</p>{% endif %}
  {% if action_url %}<a class="c-btn c-btn--primary" href="{{ action_url }}">{{ action_label }}</a>{% endif %}
</div>
```

```django
{# ui/error_state.html — announced when swapped in #}
<div class="c-state c-state--error" role="alert">
  <h2 class="c-state__title">{{ title }}</h2>
  {% if body %}<p>{{ body }}</p>{% endif %}
  {% if reference %}<p class="c-state__meta">Reference: <code>{{ reference }}</code></p>{% endif %}
  {% if retry_url %}<a class="c-btn" href="{{ retry_url }}">Try again</a>{% endif %}
</div>
```

```django
{# ui/skeleton_rows.html — decorative placeholder; status text for AT #}
<div class="c-skeleton" aria-busy="true">
  <p class="u-visually-hidden" role="status">Loading {{ label|default:"content" }}…</p>
  {% for _ in rows_range %}<div class="c-skeleton__row" aria-hidden="true"></div>{% endfor %}
</div>
```

```django
{# usage in a list template #}
{% load ui %}
{% if page_obj.object_list %}
  {% include "projects/_task_table.html" %}
{% elif request.GET.q %}
  {% url 'projects:task_list' as clear_url %}
  {% empty_state "No tasks match your search" body="Try a different term or clear filters." action_url=clear_url action_label="Clear search" kind="no-results" %}
{% else %}
  {% url 'projects:task_create' as new_url %}
  {% empty_state "No tasks yet" body="Tasks you create will appear here." action_url=new_url action_label="Create task" %}
{% endif %}
```

```css
@layer components {
  .c-skeleton__row { block-size: 1.25rem; margin-block: var(--size-2); border-radius: var(--radius);
                     background: var(--color-border); }
  @media (prefers-reduced-motion: no-preference) {
    .c-skeleton__row { animation: pulse 1.2s ease-in-out infinite; }
    @keyframes pulse { 50% { opacity: .5; } }
  }
  .c-skeleton__row:nth-child(3n) { inline-size: 70%; }
}
```

Skeleton dimensions should approximate the final content to avoid CLS when the real content swaps in. Pass `rows_range` from the view or a small `range` filter; don't build one in templates via string tricks.

### Touch targets

```css
@layer components {
  .c-btn, .c-icon-btn { min-block-size: var(--target-min); min-inline-size: var(--target-min);
                        display: inline-flex; align-items: center; justify-content: center; gap: var(--size-2); }
  /* small visual icon, large hit area */
  .c-icon-btn { padding: var(--size-2); }
  .c-table .c-icon-btn { min-block-size: 1.5rem; min-inline-size: 1.5rem; } /* 24px floor in dense tables */
}
```

## 6. Production use cases

- **Field-service/mobile users on invoices**: mobile-first invoice detail; line-items table in a scroll region; primary actions in a sticky bottom bar (respect `env(safe-area-inset-bottom)`), and ensure sticky bars don't obscure focused fields (2.4.11; use `scroll-padding-block-end`).
- **Multi-tenant SaaS**: one stylesheet; tenant overrides only semantic tokens (`--color-accent`, `--color-on-accent`) after a server-side contrast check of the tenant colour against white/black.
- **Reporting dashboard on wall displays**: `--measure` caps line length; `auto-fit` grids scale to 4 columns; numbers use tabular figures.
- **Search pages**: distinct "no results" empty state with a clear-filters action vs "first use" state with a create action.
- **Lazy panels**: skeleton → HTMX swap → error state partial on failure (HTMX 2.x doesn't swap 4xx/5xx by default; render the error partial with 200 or configure response handling — see [htmx-engineering.md](htmx-engineering.md)).

## 7. Advantages and disadvantages

| Choice | Advantages | Disadvantages |
|---|---|---|
| Mobile-first + intrinsic layouts | Fewer breakpoints, reflow compliance nearly free | Desktop-first designs need translation |
| Fluid type with `clamp()` | No breakpoint jumps | Harder to reason about exact sizes; zoom pitfalls if `rem` term omitted |
| Container queries for components | Components portable across slots | Requires `container-type` on wrappers |
| Three-tier tokens | Theming without touching components | Indirection; over-tokenisation slows work |
| Inclusion-tag components | Python API, defaults, validation | More files; designers need Python knowledge for changes |
| Stacked-card tables | Readable on phones | Loses comparison and (if done via CSS on `<table>`) semantics |

## 8. Security and performance considerations

**Security**
- Components must not mark content safe: no `|safe` on titles/bodies passed into `empty_state`/`error_state`; build HTML in Python only with `format_html`. See [frontend-security.md](frontend-security.md).
- Error states must not leak exception text or stack traces to users; show a correlation reference ID only.
- Hidden nav items/columns (CSS) are not authorisation: filter `nav_items` and columns by permission in the view/context processor.
- Tenant-supplied colours/logo URLs: validate format (hex) and origin; never inject raw strings into `style`.

**Performance**
- Correct `sizes` is the biggest image win; measure delivered vs rendered size in DevTools.
- `width`/`height` on every `<img>`; skeletons sized like the final content → CLS ≤ 0.1.
- Don't lazy-load the LCP image; give it `fetchpriority="high"`.
- Avoid shipping both a mobile and a desktop copy of large markup (duplicate DOM hidden via CSS still costs parse/style).
- Lazy panels reduce TTFB of the dashboard but add requests; batch small widgets server-side. Backend query cost → [django-performance](../../django-performance/SKILL.md).

## 9. Common mistakes and anti-patterns

1. **Device-based breakpoints (`iPhone`, `iPad`)** → content-driven breakpoints + container queries.
2. **Desktop-first `max-width` override chains** → mobile-first `min-width`/range queries.
3. **`font-size: 4vw`** → `clamp(1.5rem, 1rem + 2vw, 3rem)`.
4. **Images without `width`/`height`** → always set them; use CSS for fluid sizing.
5. **`loading="lazy"` on the hero** → eager + `fetchpriority="high"`.
6. **`sizes` omitted with `w` descriptors** → browser assumes `100vw`, downloads too big.
7. **Hamburger menu at all widths** → visible nav when it fits.
8. **Div-based "drawer" without focus management** → `<dialog>.showModal()`.
9. **Restyling `<table>` rows as `display: block` cards** → keep the table in a scroll region, or render a semantic list for card layouts.
10. **Scroll container that keyboard users can't reach** → `tabindex="0"` + `role="region"` + accessible name.
11. **Hover-only actions in tables** → always-visible action (or visible on `:focus-within`).
12. **Icon buttons < 24×24 px or tightly packed** → `min-*-size` tokens, gap ≥ spacing exception.
13. **One generic "No data" message for all cases** → differentiate first-use / no-results / error.
14. **Spinner with no text for screen readers** → `role="status"` text; `aria-busy` on the region.
15. **Design system built before the product** → extract from real duplicates; stop when components stop repeating.
16. **Components referencing primitive tokens** (`var(--blue-600)`) → semantic tokens so themes work.
17. **Building a component per Figma variant** → one component, a few modifiers; reject variants without a use case.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Horizontal scroll at 320 px | fixed widths, long URLs/tokens, `100vw` | DevTools device mode 320 px; `overflow-wrap: anywhere` on user content |
| Browser downloads largest image on phone | missing/wrong `sizes` | Network panel → image size vs `currentSrc` in Console |
| Layout shift when images load | no dimensions | Performance → Layout shifts; Lighthouse CLS audit |
| Text doesn't grow on zoom | `vw`-only font sizes | Zoom to 200%; inspect computed `font-size` |
| Drawer doesn't trap focus / Escape doesn't close | opened with `show()` not `showModal()`, or a div | Check method; test with keyboard |
| Menu button announced without state | missing `aria-expanded` sync | Accessibility tree in DevTools |
| Sticky footer hides focused input | no `scroll-padding` | Tab through form on a small viewport |
| Component breaks in sidebar but fine in main | media query instead of container query | Add container, move rules to `@container` |
| Skeleton → content jump | skeleton height ≠ content | Compare heights; set `min-block-size` on the panel |

Validation checklist:
1. Test at 320, 375, 768, 1024, 1440 CSS px and at 200%/400% zoom on 1280 px.
2. Text-spacing bookmarklet / user stylesheet for 1.4.12.
3. Keyboard-only pass on nav, drawer, tables, states.
4. Screen reader spot check: menu state, region names, status announcements.
5. Contrast check every semantic text/background pair in light and dark.
6. Lighthouse/field CLS and LCP for pages with images ([frontend-performance.md](frontend-performance.md)).
7. Playwright responsive projects (viewports) + axe scans ([frontend-testing.md](frontend-testing.md)).

## 11. Related references and skills

- [css-engineering.md](css-engineering.md) — cascade, grid, container queries, dark-mode mechanics.
- [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) — tokens and breakpoints in Tailwind/Bootstrap.
- [django-template-engineering.md](django-template-engineering.md) — includes, inclusion tags, component organisation.
- [html-and-semantic-ui.md](html-and-semantic-ui.md) — `<dialog>`, `<details>`, tables, nav markup.
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — loading/empty/error state logic.
- [frontend-accessibility.md](frontend-accessibility.md) — targets, reflow, focus, live regions.
- [frontend-performance.md](frontend-performance.md) — images, LCP/CLS.
- [htmx-engineering.md](htmx-engineering.md) — lazy panels and swaps.
- [frontend-testing.md](frontend-testing.md) — responsive and visual tests.
- [file-handling.md](../../django-core/references/file-handling.md) — storing uploads/renditions.

## 12. Official documentation and external references

- MDN — Responsive design: https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/CSS_layout/Responsive_Design
- MDN — Responsive images: https://developer.mozilla.org/en-US/docs/Web/HTML/Guides/Responsive_images
- MDN — `<picture>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/picture
- MDN — `<dialog>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog
- MDN — `clamp()`: https://developer.mozilla.org/en-US/docs/Web/CSS/clamp
- MDN — Container queries: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_containment/Container_queries
- WHATWG HTML — Images (srcset/sizes): https://html.spec.whatwg.org/multipage/images.html
- web.dev — Learn Responsive Design: https://web.dev/learn/design
- web.dev — Optimize CLS: https://web.dev/articles/optimize-cls
- W3C WAI — Understanding 1.4.10 Reflow: https://www.w3.org/WAI/WCAG22/Understanding/reflow.html
- W3C WAI — Understanding 2.5.8 Target Size (Minimum): https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html
- W3C WAI — Understanding 1.4.12 Text Spacing: https://www.w3.org/WAI/WCAG22/Understanding/text-spacing.html
- W3C WAI ARIA APG — Disclosure pattern: https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/
- Django — Inclusion tags: https://docs.djangoproject.com/en/5.2/howto/custom-template-tags/#inclusion-tags
- Django — `include` tag: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#include
