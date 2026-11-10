# Frontend Testing

> **Scope:** Test strategy and techniques for the browser-facing layer of Django apps: rendered HTML and templates, HTMX fragment endpoints, forms as the user experiences them, JS unit/DOM tests (Vitest + Testing Library), React component tests, Playwright browser/E2E tests, accessibility, visual regression, responsive and cross-browser checks, flake control and CI layout. General Django test architecture (fixtures, factories, database, test client internals, CI pipelines) belongs to [django-testing](../../django-testing/SKILL.md); API tests belong to [django-rest-framework-testing.md](../../django-testing/references/django-rest-framework-testing.md).

## 1. Purpose and scope

Use this reference to decide **which layer** proves a frontend behaviour and to write that test well. Guiding rule: test each behaviour at the **lowest layer that can actually prove it**, and reserve real browsers for what only a browser can prove (JS execution, focus, layout, network timing, cross-browser engines).

Out of scope here: pytest/pytest-django setup ([pytest-and-pytest-django.md](../../django-testing/references/pytest-and-pytest-django.md)), view/form/middleware unit tests in depth ([views-forms-and-middleware-testing.md](../../django-testing/references/views-forms-and-middleware-testing.md)), general E2E infrastructure ([browser-and-end-to-end-testing.md](../../django-testing/references/browser-and-end-to-end-testing.md)), security test suites ([security-testing.md](../../django-testing/references/security-testing.md)).

## 2. Core concepts

| Layer | Scope | Tooling | Speed | What it proves | What it cannot prove |
|---|---|---|---|---|---|
| Unit (Python) | Template tag/filter, form, view helper | Django `TestCase`/pytest | ms | Logic, escaping of a filter | Anything rendered in a browser |
| Rendering / integration (Python) | View → template → HTML response | Django test client, `assertContains`, HTML parser | 10–100 ms | Status, template, context, markup structure, HTMX headers, permission-dependent output | JS behaviour, CSS, focus |
| JS unit | Pure functions/modules | Vitest | ms | Formatting, reducers, API client normalisation | DOM integration |
| DOM / component | One component or widget in jsdom/happy-dom | Vitest + Testing Library (+ MSW, third-party) | 10–200 ms | Rendering by state, user events, accessible names, API contract assumptions | Real layout, real focus rings, real network, CSS |
| Browser | One page/flow in a real engine against a live Django | Playwright (pytest-playwright or @playwright/test) | 0.5–5 s | JS + HTML + CSS together, HTMX swaps, keyboard/focus, dialogs | Other browsers/devices unless configured |
| E2E | Critical journey across many pages, real DB, real auth | Playwright | seconds–minutes | The product works end to end (login → create invoice → download) | Edge cases (too slow to enumerate) |

Proportion: many rendering + component tests, fewer browser tests, a handful of E2E journeys. A suite that is mostly E2E is slow and flaky; one with none misses integration breaks.

## 3. Internal mechanics

- **Django test client** never runs a browser. It calls the WSGI handler in-process, returns an `HttpResponse` with `.templates` and `.context` populated (via template-rendered signals), and passes extra request headers with `headers={...}` (Django ≥ 4.2). JS never executes; `hx-*` attributes are just text.
- **`assertHTMLEqual` / `assertContains(html=True)` / `assertInHTML`** parse both sides with Django's HTML parser and compare as DOM-ish trees: attribute order and insignificant whitespace are ignored. Plain `assertContains` is a substring match on the decoded body.
- **jsdom / happy-dom** implement DOM APIs in Node. No layout engine: `getBoundingClientRect` returns zeros, CSS visibility is mostly unresolved, `<dialog>`/focus behaviours may be partial [VERIFY: jsdom version support for `HTMLDialogElement.showModal`]. Use them for logic and semantics, not layout.
- **Testing Library queries** resolve elements through the accessibility tree approximations (role, accessible name, label). `getByRole("button", { name: "Save" })` fails if the control has no accessible name — the test doubles as an a11y check.
- **MSW (third-party)** intercepts `fetch` at the network layer (in Node via request interception), so the component's real API client code runs.
- **Playwright** drives Chromium, Firefox and WebKit via its own protocol. Locators are lazy and re-resolved; actions auto-wait for the element to be attached, visible, stable, enabled and receiving events; web-first assertions (`expect(locator).to_be_visible()`) retry until timeout. Each test gets a fresh **browser context** (isolated cookies/storage), cheap compared with a new browser.
- **pytest-django `live_server`** starts Django in a thread on a random port, using the test database. With the default `db` fixture, data lives in a transaction the server thread cannot see — browser tests need `transactional_db` (the `live_server` fixture uses transactional behaviour [VERIFY: pytest-django live_server DB semantics for your version]).
- **Playwright sync API + Django ORM**: the sync API runs an event loop in the test thread, so ORM calls in the test raise `SynchronousOnlyOperation`. Tests commonly set `DJANGO_ALLOW_ASYNC_UNSAFE=true` **in the test environment only**.

## 4. Engineering decisions and trade-offs

| Prefer Python rendering tests when… | Prefer Playwright when… |
|---|---|
| The behaviour is "server returns X markup/headers for Y user/state". | The behaviour needs JS, CSS, focus, or real timing (HTMX swap, Alpine toggle, dialog). |
| Permission-dependent UI (button absent for viewer). Note: also test the endpoint rejects the action. | A keyboard user must be able to complete the flow. |

| Prefer pytest-playwright + `live_server` when… | Prefer @playwright/test (JS) against a running server when… |
|---|---|
| Django-first team; want factories/ORM setup inside tests; one CI job. | React/Vite-heavy frontend; need `toHaveScreenshot`, projects matrix, UI mode, sharding, `@axe-core/playwright`. |
| Server-rendered/HTMX apps. | Frontend team owns tests; seed data via management command or API. |

| Prefer jsdom component tests when… | Prefer browser component/page tests when… |
|---|---|
| Logic, rendering by state, events, API error handling. | Layout, scroll, intersection, drag-and-drop, real `<dialog>`, CSS-driven visibility. |

| Visual regression is worth it when… | Skip it when… |
|---|---|
| Design system components, PDF/print layouts, charts, marketing pages with stable content. | UI changes weekly, data is dynamic, or the team won't triage diffs. |

## 5. Django/frontend implementation patterns

### 5.1 Template and HTML rendering tests

```python
# projects/tests/test_project_pages.py
import pytest
from bs4 import BeautifulSoup  # beautifulsoup4 (third-party)
from django.urls import reverse
from pytest_django.asserts import assertContains, assertNotContains, assertTemplateUsed, assertInHTML

@pytest.mark.django_db
def test_project_list_renders_table_for_member(client, member, project_factory):
    project_factory.create_batch(3, tenant=member.tenant)
    client.force_login(member)
    resp = client.get(reverse("projects:list"))

    assertTemplateUsed(resp, "projects/list.html")
    assertContains(resp, "<h1>Projects</h1>", html=True)
    soup = BeautifulSoup(resp.content, "html.parser")
    table = soup.select_one("table")
    assert table.select_one("caption").get_text(strip=True) == "Projects"
    assert [th["scope"] for th in table.select("thead th")] == ["col"] * 4
    assert len(table.select("tbody tr")) == 3
    # every input has a label (structure check, not a substitute for axe)
    for inp in soup.select("input:not([type=hidden])"):
        assert soup.select_one(f'label[for="{inp["id"]}"]') or inp.get("aria-label")

@pytest.mark.django_db
def test_viewer_does_not_see_delete_and_cannot_delete(client, viewer, project):
    client.force_login(viewer)
    assertNotContains(client.get(project.get_absolute_url()), "Delete project")
    # hidden button is UX only - the endpoint must refuse too
    assert client.post(reverse("projects:delete", args=[project.pk])).status_code == 403

@pytest.mark.django_db
def test_user_content_is_escaped(client, member, project_factory):
    project_factory(tenant=member.tenant, name="<script>alert(1)</script>")
    client.force_login(member)
    resp = client.get(reverse("projects:list"))
    assertContains(resp, "&lt;script&gt;alert(1)&lt;/script&gt;")
    assertInHTML('<td>&lt;script&gt;alert(1)&lt;/script&gt;</td>', resp.content.decode())
```

- Use `html=True`/`assertInHTML` for markup fragments; plain `assertContains` for text.
- Parse with an HTML parser (stdlib `html.parser` via BeautifulSoup, or lxml — third-party) for structure: headings order, `aria-*` relationships, form field ids. Never regex HTML.
- Test custom template tags/filters directly with `Template("{% load ui %}{% badge status %}").render(Context({...}))`.

### 5.2 HTMX request tests

```python
# tasks/tests/test_htmx.py
import json
import pytest
from django.urls import reverse

HX = {"HX-Request": "true", "HX-Target": "task-list", "HX-Current-URL": "http://testserver/projects/1/"}

@pytest.mark.django_db
def test_task_list_returns_fragment_for_htmx_and_page_otherwise(client, member, project):
    client.force_login(member)
    url = reverse("tasks:list", args=[project.pk])

    full = client.get(url)
    frag = client.get(url, headers=HX)

    assert b"<html" in full.content
    assert b"<html" not in frag.content
    assert [t.name for t in frag.templates][0] == "tasks/_task_list.html"
    # same URL, two representations -> caches must key on HX-Request
    assert "HX-Request" in frag.headers.get("Vary", "")
    assert "HX-Request" in full.headers.get("Vary", "")

@pytest.mark.django_db
def test_create_task_triggers_event_and_returns_row(client, member, project):
    client.force_login(member)
    resp = client.post(reverse("tasks:create", args=[project.pk]), {"title": "Ship"}, headers=HX)
    assert resp.status_code == 200
    assert json.loads(resp.headers["HX-Trigger"]) == {"taskCreated": {"projectId": project.pk}}
    assert b"<tr" in resp.content and b"Ship" in resp.content   # row fragment, not a page

@pytest.mark.django_db
def test_invalid_form_returns_422_fragment_with_errors(client, member, project):
    client.force_login(member)
    resp = client.post(reverse("tasks:create", args=[project.pk]), {"title": ""}, headers=HX)
    assert resp.status_code == 422          # requires htmx responseHandling to swap 422 [VERIFY: your htmx responseHandling config]
    assert b'aria-invalid="true"' in resp.content

@pytest.mark.django_db
def test_session_expired_htmx_gets_client_redirect(client, project):
    # Assumes the project's login-required handling answers HTMX requests with HX-Redirect
    # instead of a 302 that htmx would follow and swap the login page into the target.
    resp = client.get(reverse("tasks:list", args=[project.pk]), headers=HX)
    assert resp.status_code in (200, 204)
    assert resp.headers["HX-Redirect"].startswith("/accounts/login/")
```

What to cover for each HTMX endpoint: fragment vs full page, `Vary: HX-Request`, `HX-*` response headers (`HX-Trigger`, `HX-Redirect`, `HX-Retarget`, `HX-Reswap`, `HX-Push-Url`), error status and markup, permissions, and CSRF (the Django test client skips CSRF checks by default; use `Client(enforce_csrf_checks=True)` for one test that proves the token wiring). Patterns themselves: [htmx-engineering.md](htmx-engineering.md).

### 5.3 Form interaction tests

Server-side (fast, many cases): post invalid data, assert status, error markup, `aria-invalid`, `aria-describedby` linkage, and that submitted values are re-rendered. Browser (few cases): keyboard-only completion, focus moved to the error summary or first invalid field, client-side validation does not block server validation. See [django-forms-and-validation.md](django-forms-and-validation.md).

```python
@pytest.mark.django_db
def test_invoice_form_errors_are_linked(client, member):
    client.force_login(member)
    resp = client.post(reverse("invoices:create"), {"amount": "-5"})
    soup = BeautifulSoup(resp.content, "html.parser")
    amount = soup.select_one("#id_amount")
    assert amount["aria-invalid"] == "true"
    described = amount["aria-describedby"].split()
    assert any("must be positive" in soup.find(id=i).get_text() for i in described)
```

### 5.4 JS unit tests with Vitest

```js
// vite.config.js (excerpt)
export default defineConfig({
  test: {
    environment: "jsdom",               // or "happy-dom"; "node" for pure modules
    setupFiles: ["./src/test/setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
    restoreMocks: true,
  },
});
```

```js
// src/test/setup.js
import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { cleanup } from "@testing-library/react";
import { server } from "./msw-server";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => { server.resetHandlers(); cleanup(); });
afterAll(() => server.close());
```

```js
// src/lib/money.test.js
import { describe, expect, it } from "vitest";
import { formatMoney } from "./money";

describe("formatMoney", () => {
  it.each([[1234.5, "EUR", "€1,234.50"], [0, "USD", "$0.00"]])("%s %s", (v, c, out) => {
    expect(formatMoney(v, c, "en-US")).toBe(out);
  });
});
```

### 5.5 DOM tests for vanilla/Alpine-free widgets with Testing Library

```js
// static/src/disclosure.test.js
import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { initDisclosures } from "./disclosure";

it("toggles aria-expanded and content", async () => {
  document.body.innerHTML = `
    <button type="button" aria-expanded="false" aria-controls="filters">Filters</button>
    <div id="filters" hidden>…</div>`;
  initDisclosures(document);
  const user = userEvent.setup();
  const btn = screen.getByRole("button", { name: "Filters" });
  await user.click(btn);
  expect(btn).toHaveAttribute("aria-expanded", "true");
  expect(document.getElementById("filters")).not.toHaveAttribute("hidden");
});
```

Query priority: `getByRole` (with `name`) → `getByLabelText` → `getByText` → `getByTestId` as a last resort. Prefer `findBy*` (async) over manual waits.

### 5.6 React component tests with MSW

```js
// src/test/msw-server.js
import { setupServer } from "msw/node";   // msw (third-party) v2 API
import { http, HttpResponse } from "msw";
export const server = setupServer(
  http.get("/api/projects/:id/tasks/", () =>
    HttpResponse.json({ count: 1, next: null, previous: null, results: [{ id: 1, title: "Write tests" }] })),
);
```

```jsx
// src/features/tasks/TaskList.test.jsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/msw-server";
import { TaskList } from "./TaskList";

it("renders tasks from the API", async () => {
  render(<TaskList projectId={1} />);
  expect(screen.getByRole("status")).toHaveTextContent(/loading/i);
  expect(await screen.findByRole("listitem")).toHaveTextContent("Write tests");
});

it("shows an error and retries", async () => {
  let calls = 0;
  server.use(http.get("/api/projects/:id/tasks/", () =>
    ++calls === 1 ? HttpResponse.json({ detail: "Boom" }, { status: 500 })
                  : HttpResponse.json({ count: 0, next: null, previous: null, results: [] })));
  render(<TaskList projectId={1} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Boom");
  await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("No tasks yet.")).toBeInTheDocument();
});
```

- Mock at the network boundary (MSW), not by mocking your own API module — that keeps error normalisation under test.
- Keep MSW fixtures aligned with DRF serializers: generate them from the OpenAPI schema or snapshot a real response in a backend test ([api-versioning-and-documentation.md](../../django-rest-framework/references/api-versioning-and-documentation.md)).
- Wrap components needing providers (QueryClient, Router) in a `renderWithProviders` helper; create a fresh `QueryClient` per test with retries off.

### 5.7 Playwright with Django (Python)

```python
# conftest.py
import os
import pytest
from django.conf import settings

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")  # test-only: Playwright sync API + ORM

@pytest.fixture
def logged_in_page(page, live_server, member, client):
    # Reuse Django's session machinery instead of driving the login form every test.
    client.force_login(member)
    page.context.add_cookies([{
        "name": settings.SESSION_COOKIE_NAME,
        "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
        "url": live_server.url,
    }])
    return page
```

```python
# e2e/test_tasks_flow.py
import re
import pytest
from playwright.sync_api import expect

@pytest.mark.django_db(transaction=True)
def test_add_task_inline_with_keyboard(logged_in_page, live_server, project):
    page = logged_in_page
    page.goto(f"{live_server.url}/projects/{project.pk}/")
    page.get_by_label("New task").fill("Ship invoice export")
    page.keyboard.press("Enter")                         # HTMX form submit
    row = page.get_by_role("row", name=re.compile("Ship invoice export"))
    expect(row).to_be_visible()                          # auto-waits for the swap
    expect(page.get_by_label("New task")).to_be_focused()
    expect(page.get_by_role("status")).to_have_text("Task added")

@pytest.mark.django_db(transaction=True)
def test_delete_dialog_traps_focus_and_escape_closes(logged_in_page, live_server, project):
    page = logged_in_page
    page.goto(f"{live_server.url}/projects/{project.pk}/settings/")
    trigger = page.get_by_role("button", name="Delete project")
    trigger.click()
    dialog = page.get_by_role("dialog", name="Delete project?")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()                      # focus returned
```

Run: `pytest e2e --browser chromium --browser firefox --tracing retain-on-failure` (pytest-playwright CLI options [VERIFY: option names for installed pytest-playwright]). Open traces with `playwright show-trace trace.zip`.

### 5.8 Playwright with @playwright/test against a running server

```js
// playwright.config.js
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  retries: process.env.CI ? 2 : 0,
  forbidOnly: !!process.env.CI,
  reporter: [["html", { open: "never" }]],
  use: { baseURL: "http://127.0.0.1:8001", trace: "on-first-retry" },
  webServer: {
    command: "python manage.py migrate && python manage.py seed_e2e && python manage.py runserver 127.0.0.1:8001 --noreload",
    url: "http://127.0.0.1:8001/healthz/",
    reuseExistingServer: !process.env.CI,
    env: { DJANGO_SETTINGS_MODULE: "config.settings.e2e" },
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.js/ },
    { name: "chromium", use: { ...devices["Desktop Chrome"], storageState: "e2e/.auth/member.json" }, dependencies: ["setup"] },
    { name: "firefox",  use: { ...devices["Desktop Firefox"], storageState: "e2e/.auth/member.json" }, dependencies: ["setup"] },
    { name: "webkit",   use: { ...devices["Desktop Safari"], storageState: "e2e/.auth/member.json" }, dependencies: ["setup"] },
    { name: "mobile",   use: { ...devices["Pixel 7"], storageState: "e2e/.auth/member.json" }, dependencies: ["setup"] },
  ],
});
```

```js
// e2e/auth.setup.js
import { test as setup, expect } from "@playwright/test";
setup("log in as member", async ({ page }) => {
  await page.goto("/accounts/login/");
  await page.getByLabel("Email").fill("member@example.test");
  await page.getByLabel("Password").fill(process.env.E2E_PASSWORD ?? "e2e-only-password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.context().storageState({ path: "e2e/.auth/member.json" });
});
```

- `e2e` settings: dedicated database, `DEBUG=False`-like behaviour where practical, static files built (`npm run build` + `collectstatic`) so tests exercise production assets. `runserver` serves static only with `DEBUG=True`; with `DEBUG=False` use WhiteNoise (third-party) or `--insecure` [VERIFY: runserver static flag behaviour].
- Keep `.auth/` out of version control; the seeded user exists only in the e2e database.

### 5.9 Accessibility tests

```js
// e2e/a11y.spec.js
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const path of ["/", "/projects/", "/projects/1/", "/invoices/new/"]) {
  test(`no axe violations: ${path}`, async ({ page }) => {
    await page.goto(path);
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .exclude("#third-party-chat")
      .analyze();
    expect(results.violations).toEqual([]);
  });
}
```

- Python: a third-party wrapper such as axe-playwright-python [VERIFY: package name/API]; or inject axe-core with `page.add_script_tag` and evaluate `axe.run()`.
- Scan **states**, not just pages: open dialogs, validation errors displayed, menus expanded, HTMX-swapped content, dark mode.
- Automated tools catch roughly a third to a half of WCAG issues. They **cannot** judge: meaningful alt text, logical focus order, whether focus moves sensibly after a swap, keyboard traps in custom widgets, screen reader announcement quality, cognitive clarity, error message usefulness, 2.4.11 focus obscured by sticky headers in all cases. Add scripted keyboard tests (§5.7) and periodic manual screen reader passes (NVDA/JAWS + Firefox/Chrome, VoiceOver + Safari). See [frontend-accessibility.md](frontend-accessibility.md).

### 5.10 Visual regression

```js
// e2e/visual.spec.js
import { test, expect } from "@playwright/test";

test("invoice detail", async ({ page }) => {
  await page.goto("/invoices/1001/");                       // seeded, fixed data
  await page.evaluate(() => document.fonts.ready);
  await expect(page).toHaveScreenshot("invoice-detail.png", {
    fullPage: true,
    animations: "disabled",
    mask: [page.getByTestId("relative-time"), page.locator(".avatar")],
    maxDiffPixelRatio: 0.01,
  });
});
```

Stabilise: seeded data and frozen clock (`page.clock` [VERIFY: Playwright version supports Clock API]), self-hosted fonts awaited via `document.fonts.ready`, `animations: "disabled"` plus `prefers-reduced-motion` emulation, masked dynamic regions, fixed viewport, and **one OS/browser image** for baselines (screenshots differ across platforms — Playwright stores per-project/platform snapshots). Generate/update baselines in the CI container (`npx playwright test --update-snapshots`), review diffs in PRs. Component-level screenshots (`expect(locator).toHaveScreenshot()`) are less brittle than full pages.

### 5.11 Responsive testing

- Test layout-affecting behaviours at the breakpoints you actually design for (e.g. 375, 768, 1280 CSS px): navigation collapses into a disclosure, tables switch to stacked cards, no horizontal scroll (`document.documentElement.scrollWidth <= innerWidth`), tap targets ≥ 24×24 CSS px (WCAG 2.5.8).
- Playwright device descriptors emulate viewport, DPR, user agent and touch — **not** real mobile browser engines, performance, virtual keyboards or OS font scaling. Use real devices for those.
- Test 200% zoom / text resize and 320 CSS px reflow (WCAG 1.4.10) for key pages.

### 5.12 Browser compatibility testing

- Run the Chromium, Firefox and WebKit projects for the critical suite; WebKit approximates Safari but is not Safari-on-iOS.
- Use a real-device cloud (third-party services) for iOS Safari and Android Chrome when analytics show material traffic, before releases that touch layout, input, or media.
- Pin Playwright and its browsers in CI (install browsers with `npx playwright install --with-deps` or the matching Python command) so engine updates are deliberate.

### 5.13 Flake reduction

| Source of flake | Fix |
|---|---|
| `sleep`/`wait_for_timeout` | Web-first assertions (`expect(...).to_be_visible()`) and waiting for a specific response (`page.expect_response`). |
| CSS selectors tied to classes | `get_by_role`/`get_by_label`; `data-testid` only where no accessible handle exists. |
| Shared data between tests | Fresh data per test (factories, unique names), isolated browser contexts, transactional DB or per-worker DB. |
| Animations/transitions | Emulate reduced motion; disable transitions in e2e CSS. |
| Time/locale/timezone | Fix `locale`/`timezone_id` in context options; freeze clock. |
| Third-party scripts/network | Block with `page.route` in tests. |
| Order dependence | Run with random order and in parallel locally before merging. |
| Retries hiding bugs | Retries only in CI, and track tests that needed a retry. |

### 5.14 CI layout

| Stage | Runs | Trigger |
|---|---|---|
| 1. Lint & types | ruff, eslint, `djlint`/template lint (third-party), TypeScript | every push |
| 2. Fast tests (parallel) | pytest (unit + rendering + HTMX); Vitest (unit + component) | every push |
| 3. Build | `npm ci && npm run build`, `collectstatic` | every push |
| 4. Browser | Playwright Chromium suite + axe, sharded | every PR |
| 5. Extended | Firefox/WebKit/mobile projects, visual regression | PR to main / nightly |
| 6. Post-deploy smoke | 3–5 E2E journeys against staging | after deploy |

Upload Playwright HTML report and traces as artifacts on failure. Pipeline mechanics: [ci-cd-test-automation.md](../../django-testing/references/ci-cd-test-automation.md).

## 6. Production use cases

| App | Emphasis |
|---|---|
| Server-rendered business app | Rendering tests for every view + permission matrix; small Playwright set for login, key forms, file upload. |
| Django + HTMX dashboard | HTMX request tests per endpoint (fragment/headers/Vary); Playwright for swaps, focus after swap, history/back. |
| DRF + React SPA | DRF API tests (backend) + Vitest/RTL/MSW for components + Playwright E2E on staging-like stack. |
| Hybrid islands | Rendering test asserts `json_script` props and mount element; component tests per island; one Playwright test per island page. |
| Design-system/component library | Component tests + visual regression per component state + axe per story/page. |
| Multi-tenant SaaS | Tenant isolation tests in Python (tenant B data never rendered for A); E2E per role. |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Python rendering tests | Fast, deterministic, same language/fixtures as backend. | Blind to JS/CSS; can pass while UI is broken. |
| Vitest + Testing Library | Fast, encourages accessible markup, real API client via MSW. | jsdom ≠ browser; mocks can drift from real API. |
| pytest-playwright + live_server | One toolchain, ORM factories in tests. | Async-unsafe flag, slower DB (transactional), fewer JS-only features. |
| @playwright/test | Projects matrix, screenshots, UI mode, sharding, axe integration. | Separate seeding path; second test language. |
| Visual regression | Catches CSS regressions nothing else does. | Baseline maintenance; platform sensitivity. |
| Automated a11y | Cheap, catches common failures. | Partial coverage; false sense of compliance. |

## 8. Security and performance considerations

### Security
- Test authorisation at the endpoint, not only absence of UI: for every "hidden button" test, add a request test proving the action is refused (403/404).
- Include escaping tests for user-controlled fields rendered in templates, `json_script` props and HTMX fragments ([cross-site-scripting.md](../../django-security-engineering/references/cross-site-scripting.md)).
- At least one test with `Client(enforce_csrf_checks=True)` per form/HTMX/SPA mutation path; Playwright tests naturally exercise real CSRF.
- E2E credentials: test-only users in a test-only database; never production credentials or real secrets in `storageState` files, traces or screenshots (traces capture network bodies and cookies).
- Do not run destructive E2E against production; smoke tests in production must be read-only or use dedicated test tenants.

### Performance (of the suite and of the app)
- Keep browser tests a minority; parallelise (`pytest -n` via pytest-xdist, third-party; Playwright workers/shards).
- Reuse auth via storage state/session cookie injection instead of UI login per test.
- Build assets once per CI run and share artifacts between jobs.
- App performance budgets (Lighthouse CI, bundle size checks) belong in CI too — see [frontend-performance.md](frontend-performance.md) and [performance-testing-and-benchmarking.md](../../django-performance/references/performance-testing-and-benchmarking.md).

## 9. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| Testing HTMX endpoints only through Playwright | Header-based request tests in Python; Playwright for a few swaps. |
| `assertContains(resp, "<button class=\"btn\">Save</button>")` (brittle) | `assertContains(..., html=True)`, or parse and query by role/text. |
| Querying by CSS class or XPath | `get_by_role`/`getByRole` with accessible names. |
| `time.sleep(2)` / `waitForTimeout` | Auto-waiting assertions or explicit response waits. |
| Mocking `fetch` per test by hand | MSW handlers shared and overridden per test. |
| Snapshot-testing whole component HTML trees | Assert behaviour and accessible output; screenshots for visuals. |
| Treating axe-clean as "accessible" | Add keyboard flows and manual screen reader checks. |
| E2E data seeded once and mutated by many tests | Per-test data or per-test tenant. |
| Visual baselines generated on developer laptops | Generate in the CI container image. |
| Missing `Vary: HX-Request` not tested | Assert it in every dual-representation endpoint test. |
| Running E2E against `runserver` with `DEBUG=True` only | Also run against production-like settings and built assets. |

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| `SynchronousOnlyOperation` in Playwright tests | Sync Playwright loop + ORM | `DJANGO_ALLOW_ASYNC_UNSAFE` in test env only. |
| Browser sees no data created in test | Data in uncommitted transaction | `transactional_db` / `django_db(transaction=True)`. |
| Static files 404 under `live_server` | Assets not built / staticfiles not served | Build step ran; `live_server` static handling [VERIFY: pytest-django serves static via StaticLiveServerTestCase-like handler]. |
| `getByRole` cannot find a button | No accessible name, or element hidden | `screen.logTestingPlaygroundURL()` / `prettyDOM`; fix markup, not the query. |
| MSW "unhandled request" errors | Handler path/base mismatch | Match `VITE_API_BASE_URL` used in tests. |
| Screenshot diffs only in CI | Font/OS/rendering differences | Generate baselines in CI image; await fonts. |
| Test passes locally, fails in CI timing out | Slower CI, missing waits | Trace viewer timeline; replace sleeps with assertions. |
| HTMX test gets full page | Header not sent | `headers={"HX-Request": "true"}` (Django ≥ 4.2) or `HTTP_HX_REQUEST="true"` kwarg. |
| CSRF 403 only in Playwright | Missing `{% csrf_token %}` / `hx-headers` wiring | Real bug — the test client was masking it. |

Validation checklist before merging frontend work:
1. Rendering/HTMX tests cover new views, permissions and escaping.
2. Component tests cover loading, empty, error, success states.
3. At least one keyboard-only browser test for new interactive flows.
4. axe scan of new pages/states passes.
5. Cross-browser projects pass for critical journeys.
6. Traces/screenshots contain no real secrets.

## 11. Related references and skills

- [htmx-engineering.md](htmx-engineering.md) · [django-forms-and-validation.md](django-forms-and-validation.md) · [django-react-integration.md](django-react-integration.md) · [alpine-js-engineering.md](alpine-js-engineering.md)
- [frontend-accessibility.md](frontend-accessibility.md) · [frontend-performance.md](frontend-performance.md) · [frontend-security.md](frontend-security.md) · [responsive-design-and-design-systems.md](responsive-design-and-design-systems.md)
- [production-frontend-architectures.md](production-frontend-architectures.md) — testing strategy per architecture.
- [django-testing](../../django-testing/SKILL.md): [browser-and-end-to-end-testing.md](../../django-testing/references/browser-and-end-to-end-testing.md), [views-forms-and-middleware-testing.md](../../django-testing/references/views-forms-and-middleware-testing.md), [pytest-and-pytest-django.md](../../django-testing/references/pytest-and-pytest-django.md), [ci-cd-test-automation.md](../../django-testing/references/ci-cd-test-automation.md), [security-testing.md](../../django-testing/references/security-testing.md), [django-rest-framework-testing.md](../../django-testing/references/django-rest-framework-testing.md)
- [drf-testing-and-debugging.md](../../django-rest-framework/references/drf-testing-and-debugging.md)

## 12. Official documentation and external references

- Django testing tools: https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- Django assertions (`assertContains`, `assertHTMLEqual`, `assertInHTML`): https://docs.djangoproject.com/en/5.2/topics/testing/tools/#assertions
- Django `LiveServerTestCase`: https://docs.djangoproject.com/en/5.2/topics/testing/tools/#liveservertestcase
- Playwright Python: https://playwright.dev/python/docs/intro and pytest plugin https://playwright.dev/python/docs/test-runners
- Playwright locators / auto-waiting: https://playwright.dev/docs/locators and https://playwright.dev/docs/actionability
- Playwright authentication (storage state): https://playwright.dev/docs/auth
- Playwright visual comparisons: https://playwright.dev/docs/test-snapshots
- Playwright accessibility testing: https://playwright.dev/docs/accessibility-testing
- Playwright emulation: https://playwright.dev/docs/emulation
- Playwright trace viewer: https://playwright.dev/docs/trace-viewer
- Vitest: https://vitest.dev/guide/ and environments https://vitest.dev/guide/environment
- Testing Library queries priority: https://testing-library.com/docs/queries/about/#priority
- React Testing Library: https://testing-library.com/docs/react-testing-library/intro/
- user-event: https://testing-library.com/docs/user-event/intro
- htmx reference (headers): https://htmx.org/reference/
- W3C WAI evaluation tools overview: https://www.w3.org/WAI/test-evaluate/
- WCAG 2.2: https://www.w3.org/TR/WCAG22/
