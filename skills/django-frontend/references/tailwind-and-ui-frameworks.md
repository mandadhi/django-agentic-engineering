# Tailwind and UI Frameworks

> **Scope:** Choosing and using utility-first CSS (Tailwind v4, contrasted with v3), Bootstrap 5.3, Tailwind component libraries (daisyUI, Flowbite — third-party) and shadcn-style copy-in components in Django projects: tokens, variants, dark mode, custom utilities, component extraction into Django includes/inclusion tags, build integration and production CSS size. Language-level CSS is in [css-engineering.md](css-engineering.md); token/design-system strategy in [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md); the general static pipeline (collectstatic, manifest storage, CI) in [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md).

## 1. Purpose and scope

Use this when deciding whether a Django project should use Tailwind, Bootstrap or hand-written CSS; when setting up or migrating Tailwind (v3 → v4); when classes "don't work in production"; or when markup is drowning in duplicated class strings. **Tailwind is not always the best choice** — §4 has the decision table.

Version baseline: Tailwind CSS 4.x (released January 2025), Bootstrap 5.3.x. Third-party Django integrations are named as such and their APIs marked VERIFY.

## 2. Core concepts

### Utility-first: rationale and costs

Utility-first means styling with single-purpose classes (`flex gap-4 p-6 text-sm`) generated from a token set, instead of authoring a named class per component.

Rationale:
- No naming and no "where does this rule live" problem; styles are co-located with markup.
- CSS size grows with the *number of distinct utilities used*, not with features — stays roughly flat.
- Deleting a template deletes its styles (no dead CSS).
- Tokens (`@theme`) constrain choices → consistency.

Costs:
- Long class attributes; noisy diffs; harder-to-read templates.
- Duplication across templates unless you extract components (Django includes/inclusion tags — §5).
- A build step that must *see* every template (missed sources = missing styles in production).
- Team must learn the vocabulary; designers editing CSS files lose their entry point.
- Third-party HTML you don't control (django-allauth templates, rich-text output, admin) can't be styled with utilities without overriding templates or a `prose`-style plugin.

### Tailwind v4 vs v3

| Aspect | v3 | v4 |
|---|---|---|
| Config | `tailwind.config.js` (`theme.extend`, `plugins`) | CSS-first: `@theme { … }` in your CSS; `@config "./tailwind.config.js"` loads a legacy JS config |
| Entry | `@tailwind base; @tailwind components; @tailwind utilities;` | `@import "tailwindcss";` |
| Content detection | Explicit `content: ["./templates/**/*.html", …]` globs | Automatic detection (skips `.gitignore`d and binary files); add paths with `@source "../templates";`; `@import "tailwindcss" source("../src");` sets the base path [VERIFY: `source()` syntax and `@source not` (v4.1)] |
| Tokens at runtime | Theme only in JS; CSS variables optional | Every `@theme` token becomes a CSS custom property on `:root` (`--color-brand-500`), usable in your own CSS |
| Custom utilities | `@layer utilities { … }` or plugin `addUtilities` | `@utility name { … }` (participates in variants) |
| Custom variants | plugin `addVariant` | `@custom-variant name (selector);` / `@variant` to apply variants inside CSS |
| Dark mode | `darkMode: 'media'` (default) or `'class'`/`'selector'` | `dark:` uses `prefers-color-scheme` by default; class/attr via `@custom-variant dark (&:where(.dark, .dark *));` |
| Cascade layers | emulated via its own `@layer` handling | native `@layer theme, base, components, utilities` |
| Container queries | `@tailwindcss/container-queries` plugin | built in: `@container`, `@md:` variants |
| Arbitrary CSS var | `bg-[--brand]` | `bg-(--brand)` [VERIFY: v4 CSS-variable shorthand syntax] |
| Important modifier | `!bg-red-500` | `bg-red-500!` (prefix form still accepted [VERIFY: v4 leading-! support]) |
| Variant stacking order | right-to-left | left-to-right [VERIFY: confirm against current official docs] |
| Packages | `tailwindcss` (PostCSS plugin + CLI) | `@tailwindcss/vite`, `@tailwindcss/cli`, `@tailwindcss/postcss`; standalone CLI binary |
| Renamed defaults | — | e.g. `shadow-sm`→`shadow-xs`, `rounded-sm`→`rounded-xs`, `outline-none`→`outline-hidden`; default `ring` width 3px→1px; default border colour gray-200→`currentColor` [VERIFY: full list in upgrade guide] |
| Browser support | Broad (older Safari OK) | Modern only: Safari 16.4+, Chrome 111+, Firefox 128+ [VERIFY: confirm against current official docs] — relies on `@property`, `color-mix()`, cascade layers |
| Migration | — | `npx @tailwindcss/upgrade` codemod (requires Node 20+, run on a clean branch) [VERIFY: Node version and what it migrates] |

If you must support browsers older than the v4 floor, stay on v3.4.

### `@theme` tokens

```css
/* static_src/css/app.css (Tailwind v4) */
@import "tailwindcss";
@source "../../templates";
@source "../../apps";          /* app templates + Python files that contain class strings */

@theme {
  --font-sans: "Inter", ui-sans-serif, system-ui, sans-serif;
  --color-brand-50:  oklch(0.97 0.02 255);
  --color-brand-500: oklch(0.60 0.17 255);
  --color-brand-700: oklch(0.45 0.16 255);
  --breakpoint-3xl: 112rem;               /* adds 3xl: variant */
  --radius-card: 0.75rem;                 /* rounded-card */
  --spacing: 0.25rem;                     /* base for p-4, gap-6, … (v4 single spacing scale) */
}
```

Namespaces map to utilities: `--color-*` → `bg-*`, `text-*`, `border-*`…; `--font-*` → `font-*`; `--text-*` → font sizes; `--breakpoint-*` → responsive variants; `--radius-*` → `rounded-*`; `--shadow-*` → `shadow-*`. `--color-*: initial;` clears the default palette before defining your own [VERIFY: confirm against current official docs]. Use `@theme inline` when a token references another variable and must resolve at use site [VERIFY: confirm against current official docs].

Semantic tokens for theming: define semantic variables that switch per theme and map Tailwind tokens to them.

```css
@theme {
  --color-surface: var(--surface);
  --color-fg: var(--fg);
  --color-accent: var(--accent);
}
:root  { --surface: #fff; --fg: #16181d; --accent: var(--color-brand-500); }
.dark  { --surface: #1f2229; --fg: #f7f7f8; --accent: oklch(0.78 0.12 255); }
```

Now `bg-surface text-fg` works in both themes without `dark:` on every element.

### Variants

- Responsive (mobile-first, min-width): `sm:` 40rem, `md:` 48rem, `lg:` 64rem, `xl:` 80rem, `2xl:` 96rem; v4 adds `max-md:` and ranges `md:max-xl:`.
- Container: `@container` on parent, `@sm:`/`@lg:` on children (v4 built in).
- State: `hover:`, `focus-visible:`, `active:`, `disabled:`, `aria-expanded:`, `aria-[current=page]:`, `data-[state=open]:`, `open:` (details/dialog), `user-invalid:` [VERIFY: confirm against current official docs], `has-[:checked]:`, `group-hover:`/`peer-checked:` (on `group`/`peer` ancestors/siblings), `motion-safe:`/`motion-reduce:`, `print:`, `forced-colors:`.
- In v4, `hover:` only applies on devices that support hover (`@media (hover: hover)`) [VERIFY: confirm against current official docs].

Prefer state variants keyed to real semantics (`aria-expanded:`, `aria-[current=page]:`, `disabled:`) over JS-toggled presentational classes — the ARIA state and the style can't drift.

### Dark mode

```css
/* v4: class- or attribute-based dark mode */
@custom-variant dark (&:where(.dark, .dark *));
/* or match the data-theme attribute rendered server-side */
@custom-variant dark (&:where([data-theme=dark], [data-theme=dark] *));
```

Render the attribute server-side from a cookie/profile (see [css-engineering.md](css-engineering.md) §5) to avoid a flash; set `color-scheme` so native controls match (`scheme-light`/`scheme-dark` utilities in v4 [VERIFY: confirm against current official docs]). v3 equivalent: `darkMode: 'class'` (or `['selector', '[data-theme="dark"]']` from 3.4.1 [VERIFY: confirm against current official docs]).

### Custom utilities and variants (v4)

```css
@utility container-app {
  margin-inline: auto;
  max-inline-size: 80rem;
  padding-inline: clamp(1rem, 4vw, 2rem);
}
@utility scrollbar-stable { scrollbar-gutter: stable; }

/* functional utility: tab-2, tab-4, … [VERIFY: --value() syntax] */
@utility tab-* { tab-size: --value(integer); }

@custom-variant htmx-request (&.htmx-request, .htmx-request &);
```

The `htmx-request:` variant lets you style in-flight HTMX requests: `htmx-request:opacity-50` (HTMX adds `htmx-request` to the requesting element or `hx-indicator` target).

### Bootstrap 5.3 at a glance

- **Sass variables and maps** (`$primary`, `$spacer`, `$theme-colors`) — customise by compiling Bootstrap from source; unused component imports can be dropped.
- **CSS variables** (`--bs-primary`, `--bs-body-bg`, component-level `--bs-btn-bg`) — runtime overrides without recompiling.
- **Color modes** (5.3+): `data-bs-theme="dark"` on `<html>` or any element; `light`/`dark` built in; an "auto" mode needs a small script from the docs (or server-render the attribute).
- **JS components** (modal, dropdown, collapse, tooltip, popover, offcanvas, toast) — no jQuery since v5; initialised by `data-bs-*` attributes or JS; tooltips/popovers require explicit initialisation and Popper (bundled in `bootstrap.bundle.js`).
- **Accessibility**: components ship ARIA wiring and focus handling, but authors must supply labels, keep the documented markup (e.g. `aria-labelledby` on modals), and check contrast after re-theming. Bootstrap's JS sanitiser for tooltip/popover HTML exists but don't feed it user HTML.
- Sizes (approx., minified+gzipped): CSS ~30 KB, JS bundle ~25 KB [VERIFY: with current release].

### Component libraries and copy-in components

| Option | What it is | Fit for Django |
|---|---|---|
| **daisyUI** (third-party) | Tailwind plugin adding semantic component classes (`btn btn-primary`, `card`, `modal`) and themes; CSS-only | Good: classes are short and server-template friendly; v5 loads via `@plugin "daisyui";` in Tailwind v4 [VERIFY: confirm against current official docs] |
| **Flowbite** (third-party) | Tailwind component markup + vanilla JS for interactive components (data attributes) | OK: copy markup into includes; audit its JS a11y and init after HTMX swaps [VERIFY: re-init API] |
| **shadcn/ui** (third-party) | *Not a dependency*: a CLI copies React component source (Radix-based primitives + Tailwind) into your repo, which you own and edit | React-only; relevant for React islands/SPAs. The *idea* maps to Django: copy component templates into `templates/ui/`, own them, don't depend on a package |
| **Headless UI / Radix** (third-party) | Unstyled accessible React/Vue primitives | React islands only |
| Bootstrap | Full framework (CSS + JS) | Fastest to "decent admin UI"; least distinctive |

## 3. Internal mechanics

### How Tailwind generates CSS

1. **Scan** sources as plain text (not parsed HTML/Python): any token that *could* be a class (`bg-brand-500`, `md:grid-cols-3`) is a candidate. v4 uses automatic detection from the project root (skipping gitignored and binary files) plus `@source` paths; v3 uses `content` globs.
2. **Match** candidates against utilities/variants derived from the theme. Non-matching strings are ignored.
3. **Emit** only matched utilities into the `utilities` layer, ordered deterministically (variants, then property order) — *class order in the HTML attribute has no effect*.

Consequences:
- `class="bg-{{ status }}-500"` never works: `bg-red-500` doesn't appear literally in any source. Write complete class names in a mapping.
- Class strings in Python (`STATUS_CLASSES = {"overdue": "bg-red-100 text-red-800"}`) work **only if** those `.py` files are scanned (`@source "../../apps";` or detection covers them).
- Templates in installed third-party packages (site-packages) are not scanned unless you add their path with `@source` — prefer overriding those templates in your project.
- Safelisting: v3 `safelist` in config; v4 `@source inline("bg-red-500 …")` [VERIFY: v4.1+].

### Cascade layers in v4

v4 output is wrapped in native layers `theme, base, components, utilities`. Your unlayered CSS beats all of them — so a stray `.card a { color: red }` in an unlayered file overrides `text-blue-600`. Put custom CSS in `@layer components { … }` (or use `@utility`) so utilities still win.

### `@apply`

`@apply` copies utility declarations into your rule at build time. In v4, a separate CSS file (or Vue/Svelte `<style>`) using `@apply` must `@reference "../app.css";` to know the theme [VERIFY: confirm against current official docs]. Heavy `@apply` recreates the naming/ownership problem utilities were meant to remove *and* duplicates declarations in output.

### Bootstrap mechanics

Compiled CSS uses CSS variables for runtime theming; colour modes work by redefining `--bs-*` variables under `[data-bs-theme=dark]`. JS components attach via `data-bs-toggle` delegated listeners on `document` (most components), so HTMX-swapped markup using data attributes generally works without re-init; tooltips/popovers must be instantiated per element (re-init after swaps, dispose before removal).

## 4. Engineering decisions and trade-offs

### Tailwind vs hand-written CSS vs Bootstrap

| Prefer **hand-written CSS** (+ `@layer`, tokens) when… | Prefer **Tailwind** when… | Prefer **Bootstrap 5.3** when… |
|---|---|---|
| No Node/build toolchain wanted (pure Django + collectstatic) | Team already fluent in utilities, or a custom design is iterated in markup | Internal/admin tools need a complete, conventional UI fast |
| UI is small (< ~30 templates) or long-lived and stable | Many templates, consistent tokens, design changes frequently | Need ready JS components (modal, dropdown, tooltip) without writing JS |
| Content is mostly CMS/rich text you can't annotate with classes | You'll extract components into includes (discipline exists) | Team prefers semantic component classes |
| Must support browsers below Tailwind v4's floor without v3 | Modern browser targets (or stay on v3.4) | Designers accept the Bootstrap look or theme via Sass |
| Designers maintain CSS files | You want tiny, flat production CSS regardless of app size | Bundle weight (~55 KB CSS+JS) acceptable |

Mixing: Tailwind + Bootstrap in one app = two resets, two token systems, class collisions (`container`, `hidden`, `collapse`) — avoid. Tailwind + a few hand-written component CSS rules in `@layer components` is fine.

### Component extraction strategy

| Prefer Django include / inclusion tag when… | Prefer `@apply` / `@utility` when… |
|---|---|
| The component has markup structure (button with icon + spinner, card, badge, table) | One-off class used where you can't change markup (third-party HTML, Markdown output) |
| Variants map to states (`primary`, `danger`, `size=sm`) | A tiny, single-element primitive (`container-app`, `focus-ring`) |
| Accessibility wiring belongs with the styling | — |

### Build integration

| Option | Prefer when | Notes |
|---|---|---|
| Tailwind standalone CLI binary | No Node in the project; simple Django + collectstatic | Download a pinned binary per platform in CI; plugins requiring npm packages unavailable [VERIFY: v4 standalone plugin support] |
| `@tailwindcss/cli` via npm | Node already present; want lockfile-pinned version | `npx @tailwindcss/cli -i … -o … --minify` |
| `@tailwindcss/vite` | You already use Vite (React islands, JS bundling) | Use django-vite (third-party) or a manifest reader for fingerprinted URLs [VERIFY: tag names] |
| `@tailwindcss/postcss` | Existing PostCSS pipeline | |
| django-tailwind (third-party) | Want `manage.py` commands and a theme app | Wraps npm; check which Tailwind major the installed version targets [VERIFY: command names, v4 support] |
| django-tailwind-cli (third-party) | Want `manage.py` commands using the standalone CLI (no Node) | [VERIFY: command names and config settings] |

## 5. Django/frontend implementation patterns

### Project layout and build (Tailwind v4, npm CLI)

```text
static_src/css/app.css     # Tailwind entry (not served directly)
static/css/app.css         # build output → STATICFILES_DIRS → collectstatic → fingerprinted
templates/  apps/*/templates/
package.json
```

```json
{
  "private": true,
  "scripts": {
    "css:dev": "tailwindcss -i static_src/css/app.css -o static/css/app.css --watch",
    "css:build": "tailwindcss -i static_src/css/app.css -o static/css/app.css --minify"
  },
  "devDependencies": { "tailwindcss": "^4.1.0", "@tailwindcss/cli": "^4.1.0" }
}
```

[VERIFY: pin exact current versions; the `tailwindcss` bin is provided by `@tailwindcss/cli` in v4.]

Standalone binary alternative (no Node):

```bash
# CI / Dockerfile build stage — pin the release and verify its checksum
./tailwindcss -i static_src/css/app.css -o static/css/app.css --minify
python manage.py collectstatic --noinput
```

```django
{% load static %}
<link rel="stylesheet" href="{% static 'css/app.css' %}">
```

With `ManifestStaticFilesStorage` (`STORAGES["staticfiles"]`, Django ≥ 4.2) the URL is fingerprinted, so long-cache headers are safe. Decide whether `static/css/app.css` is committed or built in CI (prefer CI build; gitignore the output — and note v4 auto-detection ignores gitignored files, so the output isn't scanned back in). Details: [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md).

### Ensuring templates are scanned

```css
@import "tailwindcss";
@source "../../templates";
@source "../../apps/**/templates";
@source "../../apps/**/*.py";          /* only if Python holds class strings */
```

Test: add a never-used class (`bg-lime-950`) to a template, rebuild, grep the output. Add a CI check that fails if the build output lacks a sentinel class from each templates directory.

### Components as Django includes (variant maps, not `@apply` sprawl)

```python
# apps/ui/templatetags/ui.py
from django import template

register = template.Library()

# Full class strings so Tailwind's scanner sees them (this file is in @source).
BUTTON_BASE = (
    "inline-flex items-center justify-center gap-2 rounded-md font-medium "
    "min-h-11 px-4 focus-visible:outline-2 focus-visible:outline-offset-2 "
    "focus-visible:outline-accent disabled:opacity-50 disabled:cursor-not-allowed "
    "htmx-request:opacity-70"
)
BUTTON_VARIANTS = {
    "primary": "bg-accent text-white hover:bg-brand-700",
    "secondary": "bg-surface text-fg ring-1 ring-inset ring-gray-300 hover:bg-gray-50 dark:ring-gray-600 dark:hover:bg-gray-800",
    "danger": "bg-red-700 text-white hover:bg-red-800",
}
BUTTON_SIZES = {"md": "text-sm", "sm": "min-h-8 px-3 text-xs"}  # ≥ 24px (WCAG 2.5.8)

@register.inclusion_tag("ui/button.html")
def button(label, variant="primary", size="md", type="button", name="", value="", disabled=False, extra_class=""):
    if variant not in BUTTON_VARIANTS or size not in BUTTON_SIZES:
        raise ValueError(f"unknown button variant/size: {variant}/{size}")
    return {
        "label": label, "type": type, "name": name, "value": value, "disabled": disabled,
        "classes": f"{BUTTON_BASE} {BUTTON_VARIANTS[variant]} {BUTTON_SIZES[size]} {extra_class}".strip(),
    }

STATUS_BADGES = {
    "draft": "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-100",
    "sent": "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100",
    "overdue": "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-100",
    "paid": "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100",
}

@register.inclusion_tag("ui/status_badge.html")
def status_badge(status, label):
    return {"label": label, "classes": STATUS_BADGES.get(status, STATUS_BADGES["draft"])}
```

```django
{# apps/ui/templates/ui/button.html #}
<button type="{{ type }}" class="{{ classes }}"{% if name %} name="{{ name }}" value="{{ value }}"{% endif %}{% if disabled %} disabled{% endif %}>
  <span>{{ label }}</span>
</button>
```

```django
{# apps/ui/templates/ui/status_badge.html — text label, not colour alone #}
<span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium {{ classes }}">{{ label }}</span>
```

```django
{# billing/templates/billing/invoice_list.html #}
{% extends "layouts/app_shell.html" %}
{% load ui %}
{% block content %}
<div class="flex flex-wrap items-center justify-between gap-4">
  <h1 class="text-2xl font-semibold text-fg">Invoices</h1>
  <a href="{% url 'billing:invoice_create' %}" class="inline-flex min-h-11 items-center rounded-md bg-accent px-4 text-sm font-medium text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">New invoice</a>
</div>
<div class="mt-6 overflow-x-auto rounded-card ring-1 ring-gray-200 dark:ring-gray-700" role="region" aria-labelledby="inv-cap" tabindex="0">
  <table class="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
    <caption id="inv-cap" class="sr-only">Invoices</caption>
    <thead class="bg-gray-50 dark:bg-gray-800">
      <tr><th scope="col" class="px-4 py-3 text-start font-semibold">Number</th>
          <th scope="col" class="px-4 py-3 text-start font-semibold">Client</th>
          <th scope="col" class="px-4 py-3 text-start font-semibold max-md:hidden">Due</th>
          <th scope="col" class="px-4 py-3 text-end font-semibold">Total</th>
          <th scope="col" class="px-4 py-3 text-start font-semibold">Status</th></tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-800">
      {% for inv in page_obj %}
      <tr>
        <th scope="row" class="px-4 py-3 text-start font-medium"><a class="underline-offset-2 hover:underline" href="{{ inv.get_absolute_url }}">{{ inv.number }}</a></th>
        <td class="px-4 py-3">{{ inv.client.name }}</td>
        <td class="px-4 py-3 max-md:hidden">{{ inv.due_date|date:"Y-m-d" }}</td>
        <td class="px-4 py-3 text-end tabular-nums">{{ inv.total }}</td>
        <td class="px-4 py-3">{% status_badge inv.status inv.get_status_display %}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

Rules this demonstrates: variant → complete class string in a scanned Python file; validation of variants; ARIA/semantics live with the component; `sr-only` for the caption; state text accompanies colour.

For template-only components, `{% include "ui/card.html" with title=… only %}` plus a `{% with %}`-free design (pass a `variant` and map it with `{% if %}` branches containing literal classes) also scans correctly.

### Merging caller classes

Concatenating `extra_class="px-8"` onto a component that already has `px-4` leaves both; which wins depends on generated CSS order, not attribute order. Either expose explicit props (`size`, `full_width`) or accept only non-conflicting extras (margins/positioning). JS land uses `tailwind-merge` (third-party); there is no standard Django equivalent — don't hand-roll one.

### Bootstrap 5.3 in Django (for comparison)

```django
{% load static %}
<html lang="en" data-bs-theme="{{ theme|default:'light' }}">
<head><link rel="stylesheet" href="{% static 'vendor/bootstrap/bootstrap.min.css' %}"></head>
<body>
  <button class="btn btn-primary" type="button" data-bs-toggle="modal" data-bs-target="#confirm-delete">Delete project</button>
  <div class="modal fade" id="confirm-delete" tabindex="-1" aria-labelledby="confirm-delete-title" aria-hidden="true">
    <div class="modal-dialog"><div class="modal-content">
      <form method="post" action="{% url 'projects:delete' project.pk %}">
        {% csrf_token %}
        <div class="modal-header"><h2 class="modal-title fs-5" id="confirm-delete-title">Delete {{ project.name }}?</h2>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>
        <div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-danger">Delete</button></div>
      </form>
    </div></div>
  </div>
  <script src="{% static 'vendor/bootstrap/bootstrap.bundle.min.js' %}" defer></script>
</body></html>
```

Vendor Bootstrap via npm or a pinned download into `static/vendor/` (served by Django/WhiteNoise, covered by your CSP) rather than a CDN; if you use a CDN, add SRI. The delete view must still enforce permission and require POST — the modal is UX, not a control. For Django form rendering with Bootstrap, crispy-forms + crispy-bootstrap5 (third-party) or a custom form renderer/template (see [django-forms-and-validation.md](django-forms-and-validation.md)).

### shadcn-style copy-in components, mapped to Django

The shadcn model: components are *source you own*, copied into the repo, built on accessible primitives, styled with Tailwind and CSS-variable tokens. Django equivalent:

1. `apps/ui/templates/ui/*.html` + `apps/ui/templatetags/ui.py` are the "copied" components — no runtime package.
2. Tokens as CSS variables mapped through `@theme` (§2) so themes change without editing components.
3. Behaviour from native HTML first (`<dialog>`, `<details>`, `popover`), Alpine.js 3.x for small interactions (see [alpine-js-engineering.md](alpine-js-engineering.md)).
4. Copy from daisyUI/Flowbite/Tailwind UI (commercial, third-party) examples *into* these includes, then audit a11y — many copied snippets use `div` buttons or missing labels.

If the project has React islands, shadcn/ui itself is reasonable for those islands; keep its tokens aligned with the server-rendered `@theme` variables (see [django-react-integration.md](django-react-integration.md)).

## 6. Production use cases

- **Multi-tenant SaaS dashboard (Tailwind v4)**: semantic tokens (`--accent`, `--surface`) overridden per tenant in a validated server-rendered style block or per-tenant CSS file; components use `bg-accent`, never per-tenant classes (which wouldn't be generated).
- **Internal ops/admin tool (Bootstrap 5.3)**: forms, tables, modals in days; `data-bs-theme` from user profile; minimal custom CSS.
- **Marketing pages + app (hand-written CSS)**: long-form CMS content styled via element selectors under `.prose`-like scope; Tailwind would need the typography plugin and still can't touch CMS HTML classes.
- **Migrating v3 → v4**: run `@tailwindcss/upgrade` on a branch, diff generated CSS size and visual snapshots (renamed shadow/rounded/ring defaults change visuals), confirm browser analytics meet v4's floor, replace `content` globs with `@source` where detection misses Django template dirs.
- **HTMX-heavy UI**: `htmx-request:` custom variant for loading states; Bootstrap tooltips need re-init in `htmx:afterSwap`, Tailwind needs nothing.

## 7. Advantages and disadvantages

| Option | Advantages | Disadvantages |
|---|---|---|
| Tailwind v4 | Tiny flat CSS, tokens as CSS vars, fast builds, no config JS, built-in container queries | Modern-browser floor; noisy markup; build must scan templates; renamed defaults on upgrade |
| Tailwind v3 | Wider browser support, mature ecosystem | JS config; plugin needed for container queries; maintenance-only trajectory |
| Bootstrap 5.3 | Complete components + JS, colour modes, good docs, no build needed | Generic look, heavier CSS, overriding via specificity, Sass for deep customisation |
| daisyUI | Short semantic classes on top of Tailwind, themes | Another abstraction/version to track; theming conventions differ from your tokens |
| Flowbite | Ready markup + JS | JS init lifecycle with HTMX swaps; a11y must be audited |
| Hand-written CSS | No toolchain, full control, readable markup | Naming discipline, dead CSS risk |

## 8. Security and performance considerations

**Security**
- Build tooling is supply chain: pin versions with a lockfile (or checksum the standalone binary), run `npm ci` in CI, review plugins. See [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md).
- Never put secrets in build-time env vars that end up in CSS/JS; anything bundled is public.
- Don't let users supply class names (`class="{{ user_input }}"`): autoescaping prevents attribute breakout but users could still restyle/overlay UI (fake dialogs, hidden text). Map allow-listed values to classes server-side.
- CDN-hosted Bootstrap/Tailwind Play CDN: the Play CDN is for prototyping only (runtime JS compiler, CSP-hostile); for any CDN asset use SRI and list it in CSP. See [frontend-security.md](frontend-security.md).
- Hiding a button via `hidden`/`invisible`/`d-none` doesn't protect the endpoint — enforce permissions in views.

**Performance**
- Tailwind production CSS for a typical app is in the low tens of KB minified, single-digit KB compressed [VERIFY: for your app]; if it's hundreds of KB, you're scanning too much (e.g., `node_modules`, fixtures, data dumps) or safelisting broadly.
- Always `--minify` for production; serve fingerprinted with long cache + Brotli/gzip ([frontend-performance.md](frontend-performance.md)).
- Bootstrap: import only needed Sass partials and JS modules to drop unused components; avoid loading the full JS bundle on pages without interactive components.
- Don't ship daisyUI themes you don't use (configure the theme list) [VERIFY: option name].
- Webfonts declared in `@theme --font-*` still need `@font-face` with `font-display` and preloading decisions.

## 9. Common mistakes and anti-patterns

1. **Dynamic class names** (`text-{{ color }}-600`) → mapping of complete class strings in scanned files.
2. **Templates outside detection** (app dirs, a separate `templates/` sibling to the CSS project, third-party packages) → `@source` paths; verify with a sentinel class.
3. **Class strings in Python not scanned** → add `@source "../../apps/**/*.py"` or keep maps in templates.
4. **`@apply` for every component** (`.btn { @apply … }` ×50) → Django includes/inclusion tags; reserve `@apply` for markup you can't change.
5. **Unlayered custom CSS overriding utilities** in v4 → put it in `@layer components` or `@utility`.
6. **Mixing v3 config idioms in v4** (`@tailwind base`, `content:` globs without `@config`) → `@import "tailwindcss"` + `@source`.
7. **Upgrading to v4 without checking browser analytics** → confirm Safari 16.4+/Chrome 111+/Firefox 128+ share, else stay on 3.4.
8. **`outline-none` / `focus:outline-none` with no replacement** → `focus-visible:outline-2 focus-visible:outline-offset-2` (v4: `outline-hidden` keeps forced-colors outline).
9. **`dark:` on every element** → semantic tokens switching under the dark selector.
10. **Concatenating conflicting utilities** (`px-4 px-8`) → explicit component props.
11. **Tailwind + Bootstrap together** → pick one.
12. **Using the Play CDN in production** → build step.
13. **Copy-pasted Flowbite/Tailwind UI snippets with `div` buttons, missing labels** → audit and fix in the include once.
14. **Bootstrap tooltips/popovers not re-initialised after HTMX swaps (or leaking)** → init in `htmx:load`, dispose before swap.
15. **Committing build output and also building in CI** → one source of truth.
16. **Using `hidden`/`d-none` to "secure" admin actions** → server-side permission checks.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Class works in dev, missing in prod | Prod build scans different paths, or dev uses Play CDN | Grep minified output for the class; compare build commands |
| New class never appears | Dynamic construction, file not scanned, typo/unknown token | Search source for the literal class; add `@source`; check `@theme` namespace |
| Utility doesn't override component style | Custom CSS unlayered or more specific in a later layer | DevTools shows layer of winning rule |
| `dark:` ignored | Default is media-based; you set `.dark`/`data-theme` without `@custom-variant` | Inspect generated `dark:` selector |
| Upgrade changed shadows/borders/rings | v4 renamed/default changes | Upgrade guide; visual regression diff |
| CSS file huge | Scanning `node_modules`/data files; broad safelist | Check detection base/`@source`; output size per build |
| Styles break on older iPad | Below v4 browser floor | Browser support table; consider v3.4 |
| Bootstrap modal opens but focus not trapped / page scrolls | Missing JS bundle or wrong markup | Console errors; compare with docs markup |
| Theme flashes on load | Theme set by JS after paint | Render `data-theme`/`data-bs-theme` server-side |

Validation steps:
1. CI: build CSS, assert size budget and presence of sentinel classes; fail on build warnings.
2. Visual regression on key pages in light/dark (Playwright `toHaveScreenshot`, see [frontend-testing.md](frontend-testing.md)).
3. axe scan of component gallery page (a Django view rendering every include/variant — doubles as living documentation).
4. Keyboard pass on Bootstrap/Flowbite interactive components.
5. Check browser support analytics before any Tailwind major upgrade.

## 11. Related references and skills

- [css-engineering.md](css-engineering.md) — cascade layers, specificity, dark-mode mechanics.
- [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md) — token tiers, breakpoints, UI-state components.
- [django-template-engineering.md](django-template-engineering.md) — includes, inclusion tags, component organisation.
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — Tailwind CLI/Vite pipelines, collectstatic, CI.
- [htmx-engineering.md](htmx-engineering.md) / [alpine-js-engineering.md](alpine-js-engineering.md) — interactivity with utility-styled templates.
- [django-react-integration.md](django-react-integration.md) — React islands, shadcn/ui.
- [django-forms-and-validation.md](django-forms-and-validation.md) — framework-styled form rendering.
- [frontend-accessibility.md](frontend-accessibility.md), [frontend-performance.md](frontend-performance.md), [frontend-security.md](frontend-security.md), [frontend-testing.md](frontend-testing.md).
- [production-frontend-architectures.md](production-frontend-architectures.md) — Django + Tailwind dashboard architecture.
- [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md).

## 12. Official documentation and external references

- Tailwind CSS — Installation (CLI, Vite, PostCSS): https://tailwindcss.com/docs/installation
- Tailwind CSS — Upgrade guide (v3 → v4): https://tailwindcss.com/docs/upgrade-guide
- Tailwind CSS — Theme variables (`@theme`): https://tailwindcss.com/docs/theme
- Tailwind CSS — Detecting classes in source files: https://tailwindcss.com/docs/detecting-classes-in-source-files
- Tailwind CSS — Functions and directives: https://tailwindcss.com/docs/functions-and-directives
- Tailwind CSS — Adding custom styles: https://tailwindcss.com/docs/adding-custom-styles
- Tailwind CSS — Dark mode: https://tailwindcss.com/docs/dark-mode
- Tailwind CSS — Responsive design: https://tailwindcss.com/docs/responsive-design
- Tailwind CSS — Compatibility: https://tailwindcss.com/docs/compatibility
- Tailwind CSS v3 docs: https://v3.tailwindcss.com/docs
- Bootstrap 5.3 — Introduction: https://getbootstrap.com/docs/5.3/getting-started/introduction/
- Bootstrap 5.3 — Color modes: https://getbootstrap.com/docs/5.3/customize/color-modes/
- Bootstrap 5.3 — CSS variables: https://getbootstrap.com/docs/5.3/customize/css-variables/
- Bootstrap 5.3 — Sass: https://getbootstrap.com/docs/5.3/customize/sass/
- Bootstrap 5.3 — Accessibility: https://getbootstrap.com/docs/5.3/getting-started/accessibility/
- Django — Custom template tags (inclusion tags): https://docs.djangoproject.com/en/5.2/howto/custom-template-tags/
- Django — Managing static files: https://docs.djangoproject.com/en/5.2/howto/static-files/
- MDN — Cascade layers: https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Styling_basics/Cascade_layers
