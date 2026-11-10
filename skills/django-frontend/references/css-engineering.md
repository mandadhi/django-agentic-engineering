# CSS Engineering

> **Scope:** How CSS actually resolves (cascade, specificity, `@layer`, inheritance), layout (box model, positioning, flex, grid, subgrid), units, media/container queries, custom properties, motion, selectors, nesting, architecture/naming, scoping in Django templates, tokens, dark mode and compatibility. Responsive *workflow*, breakpoints, type/spacing systems and UI-state components live in [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md); Tailwind/Bootstrap in [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md); CSS delivery/critical CSS in [frontend-performance.md](frontend-performance.md).

## 1. Purpose and scope

Use this reference when writing or reviewing hand-authored CSS for a Django app: deciding where a rule goes, why a rule "doesn't apply", how to lay out an app shell or card grid, how to theme (light/dark, tenant brand) with custom properties, and how to keep CSS maintainable as templates multiply.

Baseline assumption: evergreen browsers. Where a feature is recent, its Baseline status is stated; "Baseline widely available" = supported in all core browsers for ≥ 30 months; "newly available" = supported in all core browsers recently. Statuses move — re-check on MDN/web.dev before relying on a "newly available" feature without fallback.

Not covered: rendering pipeline internals (see [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md)), building/fingerprinting CSS (see [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)).

## 2. Core concepts

### Rule anatomy and selector types

```css
/* selector list        { declaration block } */
.invoice-table th,
.invoice-table td { padding: var(--space-2) var(--space-3); }
```

| Selector | Example | Specificity (a,b,c) |
|---|---|---|
| Universal | `*` | 0,0,0 |
| Type / pseudo-element | `td`, `::before` | 0,0,1 |
| Class / attribute / pseudo-class | `.card`, `[aria-current="page"]`, `:hover` | 0,1,0 |
| ID | `#main` | 1,0,0 |
| `:where(...)` | `:where(.card, #x)` | 0,0,0 always |
| `:is(...)`, `:not(...)`, `:has(...)` | `:is(.a, #b)` | the **most specific** argument (here 1,0,0) |
| Inline `style=""` | — | beats all selectors (not part of a,b,c) |

Consequences:
- `:where()` is the tool for **zero-specificity defaults** (resets, library base styles) that any later class overrides.
- `:is(#sidebar, .nav) a` has ID specificity even when it matches via `.nav` — a common surprise. Use `:where()` if that is unwanted.
- Combinators (` `, `>`, `+`, `~`) add nothing. Avoid IDs in CSS selectors; reserve IDs for fragment links, `aria-*` references and `label[for]`.

### Cascade order (what wins)

Evaluated in this order; the first criterion that differs decides:

1. **Origin and importance**: user-agent < user < author for normal declarations; reversed for `!important` (UA `!important` wins over everything). Transitions sit on top; animations sit above normal author declarations.
2. **Context** (shadow DOM encapsulation).
3. **Element-attached styles** (`style=""`).
4. **Cascade layers** (`@layer`): for normal declarations, later layers beat earlier ones and **unlayered styles beat all layers**; for `!important` the order reverses (earliest layer's `!important` wins).
5. **Specificity**.
6. **Order of appearance** (scope proximity with `@scope` is evaluated between specificity and order).

### Inheritance

Inherited by default: mostly text properties (`color`, `font-*`, `line-height`, `letter-spacing`, `visibility`, `cursor`, `direction`) and **all custom properties**. Not inherited: box/layout properties (`margin`, `padding`, `border`, `display`, `width`, `background`). Keywords: `inherit`, `initial` (spec initial value, e.g. `display: inline`), `unset` (inherit if inherited, else initial), `revert` (back to UA/user origin), `revert-layer` (back to previous layer).

Form controls do not inherit fonts in most UA stylesheets — add `button, input, select, textarea { font: inherit; }` in the reset.

### Box model

`width` applies to the content box by default. Always:

```css
*, *::before, *::after { box-sizing: border-box; }
```

Vertical margins between block siblings **collapse** (largest wins) in normal flow — not inside flex/grid containers. Prefer `gap` in flex/grid and a "stack" pattern over margins on components.

### Display and formatting contexts

- `display` has an outer (block/inline) and inner (flow/flex/grid/flow-root) part. `display: flow-root` creates a new block formatting context (contains floats, stops margin collapse) without the `overflow: hidden` clipping hack.
- `display: contents` removes the element's box — historically broke accessibility semantics of some elements (buttons, tables, lists) in some browsers. Avoid on semantic elements. [VERIFY: current a11y status of `display: contents` per browser]
- `hidden` attribute = `display: none` from the UA stylesheet; an author `display: flex` on the same element overrides it. Add `[hidden] { display: none !important; }` in the reset.

### Positioning

| Value | Relative to | In flow? | Notes |
|---|---|---|---|
| `static` | — | yes | default |
| `relative` | own position | yes (space kept) | establishes containing block for absolute children |
| `absolute` | nearest positioned ancestor | no | |
| `fixed` | viewport (unless an ancestor has `transform`, `filter`, `contain: paint`…) | no | transformed ancestor = common bug |
| `sticky` | nearest **scrolling** ancestor | yes | needs `top`/`inset-block-start`; fails if any ancestor between it and the scroller has `overflow: hidden/auto` |

`z-index` only compares within the same **stacking context**. New contexts are created by positioned elements with `z-index`, `opacity < 1`, `transform`, `filter`, `isolation: isolate`, `will-change`, flex/grid children with `z-index`, etc. Use `isolation: isolate` on components to contain their z-index games. Use a small z-index token scale (`--z-header: 10; --z-dropdown: 20; --z-toast: 40`). Prefer native top layer (`<dialog>.showModal()`, `popover`) over z-index wars for overlays.

### Flexbox vs grid

- **Flexbox**: one-dimensional, content-sized. Toolbars, button groups, nav lists, media objects, "push last item right" (`margin-inline-start: auto`).
- **Grid**: two-dimensional, container-defined tracks. Page shells, card grids, forms with aligned labels, dashboards.
- `flex: 1` = `1 1 0%`; items may still refuse to shrink below their min-content — set `min-width: 0` (or `min-inline-size: 0`) on flex children containing long text/tables. The grid equivalent: `minmax(0, 1fr)` instead of `1fr`.
- **Subgrid** (Baseline 2023 — newly available, verify widely-available date): `grid-template-rows: subgrid` lets cards in a row align their header/body/footer across siblings.

### Units

| Unit | Use for | Avoid for |
|---|---|---|
| `rem` | font sizes, spacing tokens, media query breakpoints (`em`/`rem` in MQ track user font size) | hairline borders |
| `em` | padding relative to the component's own font size (buttons) | nested font sizes (compounds) |
| `%` | widths relative to containing block | heights without a defined parent height |
| `vw`/`vh` | — | `100vh` on mobile (address bar); font sizes alone (breaks zoom, WCAG 1.4.4) |
| `dvh`/`svh`/`lvh` | full-height mobile layouts (`min-block-size: 100dvh`) — Baseline 2022/2023 | |
| `ch` | measure: `max-inline-size: 65ch` | |
| `fr` | grid tracks | |
| `cqi`/`cqw` | sizes relative to a query container | |
| `clamp(min, preferred, max)` | fluid type/spacing; always include a `rem` term in `preferred` so zoom still scales | |

### Media queries and container queries

```css
/* Range syntax: Baseline 2023 (newly available, Safari 16.4+) */
@media (width >= 48rem) { … }
@media (40rem <= width < 64rem) { … }
@media (prefers-reduced-motion: reduce) { … }
@media (hover: hover) and (pointer: fine) { … }
@media (forced-colors: active) { … }

/* Container queries (size): Baseline 2023 */
.card-list { container: cards / inline-size; }
@container cards (inline-size >= 36rem) { .card { grid-template-columns: 8rem 1fr; } }
```

Media queries answer "how big is the viewport"; container queries answer "how big is the slot this component was dropped into" — the right question for reusable Django includes that appear in both a sidebar and a main column.

### Custom properties

Custom properties are inherited, computed at **computed-value time** per element, and can be overridden by any descendant scope — that is what makes them good for theming. `var(--x, fallback)` uses the fallback only if `--x` is not defined (guaranteed-invalid), not if it is defined with a value invalid for the property (that yields "invalid at computed-value time" → the property becomes `unset`). `@property` (Baseline 2024 — [VERIFY: @property Baseline status]) registers type/inheritance/initial value, enabling animation of custom properties.

### Pseudo-classes and pseudo-elements

| Selector | Use | Status |
|---|---|---|
| `:focus-visible` | keyboard-focus rings without mouse-click rings | Baseline 2022 |
| `:focus-within` | style a field group / menu containing focus | widely available |
| `:has()` | parent/relational: `.field:has(:user-invalid)`, `form:has(input:checked)` | Baseline 2023 (Firefox 121) |
| `:user-invalid` / `:user-valid` | invalid styles only after user interaction (unlike `:invalid`, which fires on page load) | Baseline 2023 newly available [VERIFY: :user-invalid Baseline status] |
| `:is()` / `:where()` | grouping; specificity control | widely available |
| `:not()` with lists | `:not(.a, .b)` | widely available |
| `::before`/`::after` | decoration; content is exposed to AT if non-empty text — don't put meaning there | |
| `::marker`, `::placeholder`, `::backdrop`, `::selection`, `::file-selector-button` | | |

### Nesting

Native CSS nesting is Baseline 2023 (Dec); the "relaxed" syntax allowing bare type selectors (`.card { h2 { … } }`) shipped slightly later [VERIFY: relaxed-nesting Baseline date]. Nested selectors behave like `:is()` for specificity (`.a, #b { .c {} }` → `:is(.a, #b) .c` → ID specificity). Keep nesting ≤ 2 levels; use it for states (`&:hover`, `&[aria-expanded="true"]`) and media queries, not to mirror DOM depth.

## 3. Internal mechanics

### From stylesheet to computed value

1. Parse → CSSOM. Invalid declarations are dropped individually at parse time (that's why `@supports` and declaration fallbacks work: `display: block; display: grid;`).
2. **Cascade**: for each element+property, collect matching declarations and sort by the order in §2 → *cascaded value*.
3. **Defaulting**: no cascaded value → inherit or initial → *specified value*.
4. **Computed value**: resolve relative units that don't need layout (`em` → px, `var()` substitution).
5. **Used value**: after layout (`%` widths, `auto`).

Selector matching runs **right to left**: `.sidebar ul li a` first finds all `a`, then walks ancestors. Deep descendant selectors and broad `:has()` on frequently mutated subtrees cost style recalc; flat class selectors are cheapest. In practice selector cost rarely matters vs. layout thrash and huge DOMs — measure before optimising.

### `@layer` mechanics

```css
/* Declare order once, at the top of the first stylesheet. */
@layer reset, tokens, base, layout, components, utilities;

@import url("vendor/datepicker.css") layer(vendor);   /* third-party CSS into a layer */
@layer base { a { color: var(--color-link); } }
@layer utilities { .u-hidden { display: none; } }
```

- Layer order is fixed by **first appearance** of the layer name; later `@layer x {}` blocks append to the existing layer.
- A `.u-hidden` (0,1,0) in `utilities` beats `.card .title` (0,2,0) in `components` because layer order is checked before specificity — this removes most `!important` usage.
- Unlayered CSS beats every layer. Third-party CSS you can't wrap stays unlayered and will win — import it via `layer(vendor)` instead.
- `!important` inside an *early* layer beats `!important` in later layers — reserve it for the reset (`[hidden]`) and `prefers-reduced-motion` overrides.
- `@import ... layer()` must precede other rules; `@import` serialises downloads (see [frontend-performance.md](frontend-performance.md)) — in production, bundle instead.

### `position: sticky` mechanics

A sticky box is positioned relative to its **nearest scroll container** and constrained by its **containing block** (its parent). It sticks only while the parent is in view. Failure modes: an ancestor with `overflow: hidden|auto|scroll` becomes the scroll container (sticky relative to it, often never scrolling); parent exactly as tall as the sticky element; missing `top`. `overflow: clip` does not create a scroll container and is safe.

### Animation and the compositor

Changes to `transform` and `opacity` (and `filter` in most engines) can run on the compositor thread without layout or paint. Animating `width`, `height`, `top`, `left`, `margin`, `box-shadow` forces layout and/or paint per frame. Pattern: animate `transform: translate/scale` and `opacity`; use `will-change` only briefly, never globally (it allocates layers/memory).

```css
.toast { transition: opacity 150ms ease-out, transform 150ms ease-out; }
.toast[data-state="entering"] { opacity: 0; transform: translateY(0.5rem); }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

`transition-behavior: allow-discrete` and `@starting-style` allow entry/exit transitions on `display`/`<dialog>`/`popover` (Baseline 2024 newly available [VERIFY: confirm against current official docs]); treat as progressive enhancement.

### `light-dark()` and `color-scheme`

`color-scheme: light dark` on `:root` tells the UA which schemes the page supports: it switches UA-rendered parts (form controls, scrollbars, default canvas) and makes `light-dark(a, b)` resolve to `a` or `b` per the element's used color scheme. Without `color-scheme`, `light-dark()` always picks the light value. `light-dark()` is Baseline 2024 (May) newly available — fine for apps targeting evergreen browsers; provide a `@supports not (color: light-dark(#000, #fff))` fallback if you support older Safari (< 17.5). [VERIFY: exact Safari version]

## 4. Engineering decisions and trade-offs

### Layout primitive

| Prefer grid when… | Prefer flexbox when… |
|---|---|
| Rows and columns must align (shell, dashboards, forms) | Items flow in one direction and size to content (toolbars, tags) |
| You want the container to define the tracks | Wrapping items need not align into columns |
| You need overlap via named areas | You need "space-between" / "push right" |

### Responsiveness trigger

| Prefer container queries when… | Prefer media queries when… |
|---|---|
| A component (card, stat tile, table toolbar) is reused in slots of different widths | Changing the page shell (sidebar → drawer) |
| | Querying user preferences (motion, color scheme, hover/pointer) |

### CSS architecture

| Approach | Strengths | Costs | Prefer when |
|---|---|---|---|
| **BEM** (`.card__title--muted`) | explicit ownership, flat specificity, greppable | verbose; modifier explosion | many contributors, hand-written CSS, server templates |
| **Utility-first** (Tailwind or own utilities) | no naming, no dead CSS growth, fast iteration | noisy markup; needs component extraction discipline; build step | design system expressed as tokens, team accepts the build (see [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md)) |
| **CUBE CSS** (Composition, Utility, Block, Exception) | layout primitives + a few utilities + small blocks; exceptions via `data-*` | conventions must be learned | small-to-mid apps, hand-written CSS, wants low CSS volume |
| **ITCSS** (settings → tools → generic → elements → objects → components → utilities) | specificity grows with layer; maps cleanly onto `@layer` | ceremony for small apps | large CSS codebases, Sass users |

Default recommendation for a server-rendered Django app with hand-written CSS: **`@layer` ordering (ITCSS-ish) + CUBE-style composition primitives + BEM-ish component prefixes + a tiny utility layer**. Don't mix two naming systems in one component.

### Scoping strategy in Django

| Option | How | Trade-off |
|---|---|---|
| Component class prefix (`.c-invoice-row`, `.invoice-row__total`) | one CSS file per template component, prefix = component name | convention only; zero runtime; recommended default |
| `@scope (.c-card) to (.c-card__slot) { … }` | native scoping with lower boundary (donut scope) | Chromium 118+, Safari 17.4+, Firefox shipped later — [VERIFY: @scope Baseline status (newly available?) on MDN]; progressive only until widely available |
| Inline `<style>` in includes | co-location | duplicated per include render; CSP needs nonce/hash; avoid |
| Shadow DOM web components | true encapsulation | breaks global tokens/utilities unless shared; forms participation needs ElementInternals; overkill for most Django apps |

### Dark mode strategy

| Prefer `prefers-color-scheme` only when… | Prefer explicit `data-theme` + server-rendered preference when… |
|---|---|
| No user toggle needed | Users toggle, preference must persist across devices |
| Zero JS, zero flash | Tenant/brand themes also vary |

Hybrid (recommended): default to OS preference; when the user picks a theme, store it (cookie or user profile) and render `data-theme` on `<html>` **server-side** to avoid a flash of wrong theme.

## 5. Django/frontend implementation patterns

### File organisation and loading

```text
static/css/
  app.css              # @layer order + @imports (dev) / bundled entry (prod)
  reset.css  tokens.css  base.css  layout.css  utilities.css
  components/
    c-card.css  c-data-table.css  c-app-shell.css  c-form.css
templates/
  components/card.html  components/data_table.html
```

```css
/* static/css/app.css — entry. In production bundle these (see static-assets reference). */
@layer reset, tokens, base, layout, components, utilities;
@import url("reset.css") layer(reset);
@import url("tokens.css") layer(tokens);
@import url("base.css") layer(base);
@import url("layout.css") layer(layout);
@import url("components/c-card.css") layer(components);
@import url("components/c-data-table.css") layer(components);
@import url("utilities.css") layer(utilities);
```

```css
/* reset.css */
*, *::before, *::after { box-sizing: border-box; }
* { margin: 0; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body { min-block-size: 100dvh; line-height: 1.5; }
img, picture, svg, video { display: block; max-inline-size: 100%; block-size: auto; }
button, input, select, textarea { font: inherit; color: inherit; }
[hidden] { display: none !important; }
:where(a:not([class])) { text-decoration-skip-ink: auto; }
```

### Tokens as custom properties + dark mode

```css
/* tokens.css */
:root {
  color-scheme: light dark;
  /* primitives */
  --blue-600: oklch(0.55 0.18 255);
  --gray-50: oklch(0.98 0 0);  --gray-900: oklch(0.2 0 0);
  /* semantic (what components use) */
  --color-bg:      light-dark(var(--gray-50), var(--gray-900));
  --color-text:    light-dark(var(--gray-900), var(--gray-50));
  --color-accent:  var(--blue-600);
  --color-focus:   light-dark(var(--blue-600), oklch(0.8 0.12 255));
  --space-1: 0.25rem; --space-2: 0.5rem; --space-3: 0.75rem; --space-4: 1rem; --space-6: 1.5rem;
  --radius-2: 0.375rem;
  --shadow-1: 0 1px 2px oklch(0 0 0 / 0.08);
  --z-header: 10; --z-dropdown: 20;
}
/* explicit user choice overrides OS */
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"]  { color-scheme: dark; }

/* fallback for browsers without light-dark() */
@supports not (color: light-dark(#000, #fff)) {
  :root { --color-bg: var(--gray-50); --color-text: var(--gray-900); }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { --color-bg: var(--gray-900); --color-text: var(--gray-50); }
  }
  :root[data-theme="dark"] { --color-bg: var(--gray-900); --color-text: var(--gray-50); }
}
```

`oklch()` is Baseline 2023; if you support older browsers, use hex primitives.

Server-rendered theme preference (no flash, no JS required to apply it):

```python
# apps/accounts/views.py
from django.http import HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

THEMES = {"light", "dark", "system"}

@require_POST
def set_theme(request):
    theme = request.POST.get("theme", "system")
    if theme not in THEMES:
        theme = "system"
    next_url = request.POST.get("next", "/")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
        next_url = "/"
    response = HttpResponseRedirect(next_url)
    response.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax", secure=request.is_secure())
    return response
```

```python
# apps/core/context_processors.py  (add to TEMPLATES OPTIONS context_processors)
def theme(request):
    value = request.COOKIES.get("theme")
    return {"theme": value if value in {"light", "dark"} else ""}
```

```django
{# templates/base.html #}
{% load static %}
<!doctype html>
<html lang="en"{% if theme %} data-theme="{{ theme }}"{% endif %}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <link rel="stylesheet" href="{% static 'css/app.css' %}">
  <title>{% block title %}Projects{% endblock %}</title>
</head>
<body>{% block body %}{% endblock %}</body>
</html>
```

```django
{# theme switcher: a real form, works without JS #}
<form method="post" action="{% url 'accounts:set_theme' %}" class="c-theme-switch">
  {% csrf_token %}
  <input type="hidden" name="next" value="{{ request.get_full_path }}">
  <fieldset>
    <legend>Theme</legend>
    {% for value, label in theme_choices %}
      <label><input type="radio" name="theme" value="{{ value }}"{% if theme == value or not theme and value == "system" %} checked{% endif %}> {{ label }}</label>
    {% endfor %}
  </fieldset>
  <button type="submit">Apply</button>
</form>
```

### Layout: app shell (sidebar + header + main) with grid

```django
{# templates/layouts/app_shell.html #}
{% extends "base.html" %}
{% block body %}
<a class="u-skip-link" href="#main">Skip to content</a>
<div class="c-app-shell">
  <header class="c-app-shell__header">{% include "components/topbar.html" %}</header>
  <nav class="c-app-shell__nav" aria-label="Primary">{% include "components/sidebar_nav.html" %}</nav>
  <main id="main" class="c-app-shell__main" tabindex="-1">{% block content %}{% endblock %}</main>
</div>
{% endblock %}
```

```css
@layer layout {
  .c-app-shell {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto 1fr;
    grid-template-areas: "header" "nav" "main";
    min-block-size: 100dvh;
  }
  .c-app-shell__header { grid-area: header; position: sticky; top: 0; z-index: var(--z-header);
                         background: var(--color-bg); border-block-end: 1px solid var(--color-border); }
  .c-app-shell__nav    { grid-area: nav; }
  .c-app-shell__main   { grid-area: main; padding: var(--space-4); min-inline-size: 0; }

  @media (width >= 64rem) {
    .c-app-shell {
      grid-template-columns: 16rem minmax(0, 1fr);
      grid-template-rows: auto 1fr;
      grid-template-areas: "nav header" "nav main";
    }
    .c-app-shell__nav { position: sticky; top: 0; block-size: 100dvh; overflow-y: auto; }
  }
}
```

`minmax(0, 1fr)` and `min-inline-size: 0` stop a wide table in `main` from blowing out the grid. DOM order (header, nav, main) matches reading/focus order; grid areas only change visual placement — never reorder visually in a way that contradicts DOM order (WCAG 1.3.2/2.4.3).

### Layout: card grid with auto-fit/minmax + subgrid alignment

```css
@layer components {
  .c-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
    gap: var(--space-4);
  }
  .c-card {
    display: grid;
    grid-row: span 3;
    grid-template-rows: subgrid;            /* header / body / footer align across the row */
    gap: var(--space-2);
    padding: var(--space-4);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }
  @supports not (grid-template-rows: subgrid) {
    .c-card { grid-template-rows: auto 1fr auto; grid-row: auto; }
  }
  .c-card__title a { color: inherit; text-decoration: none; }
  .c-card__title a::after { content: ""; position: absolute; inset: 0; } /* whole card clickable */
  .c-card { position: relative; }
  .c-card:has(a:focus-visible) { outline: 2px solid var(--color-focus); outline-offset: 2px; }
}
```

`min(100%, 18rem)` prevents overflow on screens narrower than 18rem. `auto-fit` collapses empty tracks (items stretch); `auto-fill` keeps them (items keep their size). The "stretched link" keeps a single link per card (one tab stop, sensible link text) instead of wrapping the whole card in `<a>`.

### Layout: holy grail (header, footer, two asides)

```css
.l-holy-grail {
  display: grid;
  min-block-size: 100dvh;
  grid-template: auto 1fr auto / minmax(0, 1fr);
}
@media (width >= 60rem) {
  .l-holy-grail {
    grid-template:
      "header header header" auto
      "left   main   right"  1fr
      "footer footer footer" auto / 14rem minmax(0, 1fr) 14rem;
  }
  .l-holy-grail > header { grid-area: header; }
  .l-holy-grail > main   { grid-area: main; }
  .l-holy-grail > .left  { grid-area: left; }
  .l-holy-grail > .right { grid-area: right; }
  .l-holy-grail > footer { grid-area: footer; }
}
```

### Layout: form with aligned labels

```css
@layer components {
  .c-form { display: grid; gap: var(--space-4); max-inline-size: 40rem; }
  .c-form__row { display: grid; gap: var(--space-1); }
  @container form (inline-size >= 36rem) {
    .c-form__row { grid-template-columns: 12rem minmax(0, 1fr); align-items: start; }
    .c-form__row > :not(label) { grid-column: 2; }
  }
  .c-form-wrap { container: form / inline-size; }
  .c-form :is(input, select, textarea) { inline-size: 100%; min-block-size: 2.75rem; }
  .c-form :user-invalid { border-color: var(--color-danger); }
  .c-form__row:has(:user-invalid) .c-form__hint { color: var(--color-danger); }
  .c-form :focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; }
}
```

Colour alone must not signal errors (WCAG 1.4.1): server-rendered error text stays the source of truth (see [django-forms-and-validation.md](django-forms-and-validation.md)).

### Layout: sticky table header in a scroll container

```django
<div class="c-data-table" role="region" aria-labelledby="tasks-caption" tabindex="0">
  <table>
    <caption id="tasks-caption">Open tasks ({{ page_obj.paginator.count }})</caption>
    <thead><tr><th scope="col">Task</th><th scope="col">Assignee</th><th scope="col" class="u-num">Hours</th></tr></thead>
    <tbody>
      {% for task in page_obj %}
        <tr><td>{{ task.title }}</td><td>{{ task.assignee|default:"—" }}</td><td class="u-num">{{ task.hours }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
```

```css
@layer components {
  .c-data-table { max-block-size: 70dvh; overflow: auto; border: 1px solid var(--color-border); }
  .c-data-table:focus-visible { outline: 2px solid var(--color-focus); }
  .c-data-table table { border-collapse: separate; border-spacing: 0; inline-size: 100%; }
  .c-data-table thead th {
    position: sticky; top: 0; z-index: 1;
    background: var(--color-surface);            /* opaque, or rows show through */
    border-block-end: 1px solid var(--color-border);
  }
  .c-data-table th, .c-data-table td { padding: var(--space-2) var(--space-3); text-align: start; }
  .u-num { text-align: end; font-variant-numeric: tabular-nums; }
}
```

The sticky header works because `.c-data-table` *is* the scroll container. `border-collapse: separate` avoids borders disappearing on sticky cells in some engines. The focusable region lets keyboard users scroll it (see [frontend-accessibility.md](frontend-accessibility.md)).

### Feature detection

```css
@supports (container-type: inline-size) { … }
@supports selector(:has(a)) { … }
@supports not (anchor-name: --x) { /* fallback positioning */ }
```

## 6. Production use cases

- **Multi-tenant branding**: tenant colours rendered as custom properties in a `<style nonce>` block or, better, a per-tenant CSS file; components only reference semantic tokens (`--color-accent`), so one stylesheet serves all tenants. Validate tenant-supplied colours server-side (hex regex) — never interpolate raw tenant strings into CSS.
- **Admin/reporting tables**: sticky header + `tabular-nums` + container-query toolbar; print stylesheet via `@media print { .c-app-shell__nav { display: none; } }`.
- **Dashboard widgets reused in different columns**: container queries per widget.
- **Third-party widget CSS (date picker, charts)**: `@import ... layer(vendor)` placed before `components` so your styles override it without `!important`.
- **High-contrast users**: `@media (forced-colors: active)` — use `outline` (not only `box-shadow`) for focus, `border` with `transparent` colour on buttons so they get a border in forced colours.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Hand-written CSS + `@layer` + tokens | no build required, small CSS, full control, readable markup | needs naming discipline; dead CSS accumulates without ownership |
| Native nesting | no Sass for most needs | specificity via `:is()` semantics surprises; deep nesting temptation |
| Container queries | truly reusable components | need explicit `container-type`; size containment can collapse containers with no definite width |
| `light-dark()` | single declaration per token | requires `color-scheme`; newer Baseline |
| Custom properties | runtime theming, inherit through DOM | no type checking (unless `@property`); invalid values fail silently |

## 8. Security and performance considerations

**Security**
- CSS is an injection vector: attribute selectors + `background: url()` can exfiltrate attribute values (e.g., CSRF tokens in `value=""`) if attackers inject styles. Never render user-controlled text into `<style>` or `style=""`; if tenants pick colours, validate against a strict pattern and render via `format_html` or a CSS file. See [frontend-security.md](frontend-security.md).
- CSP: inline `<style>` and `style=""` need `style-src 'unsafe-inline'` or nonces/hashes; prefer external files and classes. HTMX/Alpine `x-show`/transitions set inline styles via CSSOM, which `style-src` does not block (CSSOM manipulation isn't covered) [VERIFY: CSP treatment of CSSOM style changes in target browsers].
- Hiding UI with CSS (`display: none`, `visibility`) is not access control — the markup and data are still delivered. Don't render what the user isn't authorised to see.

**Performance**
- CSS is render-blocking; keep the critical stylesheet small, avoid `@import` chains in production (each is a sequential request), bundle and fingerprint (see [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)).
- Animate `transform`/`opacity` only; avoid layout-triggering transitions on large lists.
- `content-visibility: auto` + `contain-intrinsic-size` on long off-screen sections (Baseline 2024 newly available [VERIFY: content-visibility Baseline status]) can cut rendering cost of long reports.
- Reserve space for images/embeds (`aspect-ratio`, `width`/`height` attributes) to avoid CLS.
- Big `:has()` selectors at the root (`body:has(...)`) re-evaluate on many DOM mutations; scope them to components.

## 9. Common mistakes and anti-patterns

1. **`!important` to win a specificity fight** → fix the architecture: move the rule to a later `@layer` or lower the competitor's specificity with `:where()`.
2. **Styling with IDs** (`#sidebar .link`) → use classes; IDs make overrides require IDs.
3. **`:is(#id, .class)` unintentionally raising specificity** → `:where()`.
4. **Third-party CSS left unlayered while yours is layered** → it wins everything; import it into a `vendor` layer.
5. **`height: 100vh` on mobile** → `min-block-size: 100dvh` (fallback `100vh` first).
6. **Grid/flex child overflowing with long text/tables** → `minmax(0, 1fr)` / `min-inline-size: 0`.
7. **Sticky not sticking** → remove `overflow: hidden` from ancestors (use `overflow: clip`), add `top: 0`, ensure the parent is taller than the sticky element.
8. **`outline: none` on focus** without replacement → use `:focus-visible` with a ≥ 3:1 contrast outline (WCAG 2.4.7, 1.4.11).
9. **`:invalid` styling** shows errors before users type → `:user-invalid`.
10. **Font sizes in `px` or pure `vw`** → `rem` and `clamp()` with a `rem` term (zoom/WCAG 1.4.4).
11. **Media queries in `px` for component layout** → `rem`/`em` breakpoints or container queries.
12. **Visual reordering (`order`, grid areas) that contradicts DOM order** → change the DOM order instead.
13. **Information in `::before` content** (e.g., required asterisk only via CSS) → put meaning in markup; CSS-generated text is inconsistently announced.
14. **`display: contents` on `<button>`/`<ul>`/`<table>`** → wrap differently; it can drop semantics.
15. **Classes assembled dynamically in templates** (`class="badge-{{ status }}"`) without a CSS rule for every value → map statuses to known classes in Python or use a `data-status` attribute selector with an explicit allow-list.
16. **Per-include `<style>` blocks** → move into the component's CSS file.
17. **Animating `width/height/top`** → `transform`; and always honour `prefers-reduced-motion`.
18. **Dark mode by inverting colours (`filter: invert`)** → semantic tokens with designed dark values; check contrast for both themes.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Rule not applied | Lower layer, lower specificity, or overridden later | DevTools Styles pane: struck-through declaration shows the winner and its layer |
| Rule ignored entirely | Invalid value / typo / unsupported property | DevTools shows a warning icon; check `@supports` |
| Custom property "does nothing" | Defined on a scope that's not an ancestor, or invalid at computed-value time | Computed pane → filter `--name`; check the element actually inherits it |
| `light-dark()` always light | `color-scheme` not set (or set to `light`) | Computed `color-scheme` on `:root` |
| Flash of wrong theme | Theme applied by JS after first paint | Render `data-theme` server-side |
| Sticky header scrolls away | Ancestor `overflow`, missing `top`, transparent background | Walk ancestors in DevTools for `overflow` |
| `z-index: 9999` still below | Different stacking context | DevTools Layers panel / 3D view; add `isolation: isolate` on the right ancestor |
| Horizontal scroll on mobile | Fixed-width element, `100vw` (includes scrollbar), unbreakable string | `* { outline: 1px solid red }` temporarily; `overflow-wrap: anywhere` for long tokens |
| Container query never matches | No `container-type` on an ancestor, or name mismatch | DevTools shows "container" badge on containers |
| Layout shift on load | Images without dimensions, late webfonts | Performance panel → Layout Shift regions |
| Focus ring missing | `outline: none` in reset or framework | Tab through the page; check `:focus-visible` rules |

Validation steps:
1. Keyboard-tab through each layout; focus always visible and not obscured by sticky headers (WCAG 2.4.11) — add `scroll-padding-top` equal to header height on `html`.
2. Zoom to 400% / 320 CSS px width: no two-dimensional scrolling except tables (see [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md)).
3. Toggle emulated `prefers-color-scheme`, `prefers-reduced-motion`, `forced-colors` in DevTools Rendering panel.
4. Contrast check both themes (DevTools, axe).
5. Check Baseline status on MDN for every newly-available feature you use without a fallback.
6. Stylelint (third-party) with a config that bans IDs and `!important` outside allowed layers.
7. Visual regression snapshots for the shell and key components (see [frontend-testing.md](frontend-testing.md)).

## 11. Related references and skills

- [frontend-engineering-fundamentals.md](frontend-engineering-fundamentals.md) — rendering pipeline, reflow/repaint, compatibility strategy.
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — breakpoints, fluid type, token tiers, UI states.
- [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) — utility frameworks and Bootstrap.
- [html-and-semantic-ui.md](html-and-semantic-ui.md) — semantic structure the CSS hangs on.
- [django-template-engineering.md](django-template-engineering.md) — component includes/inclusion tags.
- [frontend-accessibility.md](frontend-accessibility.md) — focus, contrast, reduced motion, forced colours.
- [frontend-performance.md](frontend-performance.md) — CSS delivery, CLS.
- [frontend-security.md](frontend-security.md) — CSS injection, CSP for styles.
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — bundling, fingerprinting.
- [frontend-testing.md](frontend-testing.md) — visual regression, responsive testing.
- [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md) — server-side escaping.

## 12. Official documentation and external references

- MDN — Cascade, specificity and inheritance: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_cascade/Cascade
- MDN — Specificity: https://developer.mozilla.org/en-US/docs/Web/CSS/Specificity
- MDN — `@layer`: https://developer.mozilla.org/en-US/docs/Web/CSS/@layer
- MDN — `@scope`: https://developer.mozilla.org/en-US/docs/Web/CSS/@scope
- MDN — CSS grid layout: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_grid_layout
- MDN — Subgrid: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_grid_layout/Subgrid
- MDN — Container queries: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_containment/Container_queries
- MDN — Using media queries (range syntax): https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_media_queries/Using_media_queries
- MDN — `light-dark()`: https://developer.mozilla.org/en-US/docs/Web/CSS/color_value/light-dark
- MDN — `color-scheme`: https://developer.mozilla.org/en-US/docs/Web/CSS/color-scheme
- MDN — CSS nesting: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_nesting
- MDN — `:has()`, `:user-invalid`, `:focus-visible`: https://developer.mozilla.org/en-US/docs/Web/CSS/:has
- MDN — `position: sticky`: https://developer.mozilla.org/en-US/docs/Web/CSS/position
- MDN — Stacking context: https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_positioned_layout/Understanding_z-index/Stacking_context
- web.dev — Baseline: https://web.dev/baseline
- web.dev — Learn CSS: https://web.dev/learn/css
- W3C — CSS Cascading and Inheritance Level 5: https://www.w3.org/TR/css-cascade-5/
- W3C WAI — Understanding SC 1.4.10 Reflow: https://www.w3.org/WAI/WCAG22/Understanding/reflow.html
- Django — Context processors: https://docs.djangoproject.com/en/5.2/ref/templates/api/#writing-your-own-context-processors
