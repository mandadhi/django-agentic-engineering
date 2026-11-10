# Browser and End-to-End Testing

> **Scope:** Driving a real browser against a live Django server to prove critical user
> journeys: when that is justified, Django's live-server test classes and pytest-django's
> `live_server`, Playwright and Selenium usage, authentication shortcuts, page objects, test
> environments and E2E reliability.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · Playwright for Python 1.4x ·
> Selenium 4.x · **Depends on:** **[Django]** `LiveServerTestCase`, **[Django]**
> `StaticLiveServerTestCase` (`django.contrib.staticfiles`), **[pytest-django]** `live_server`,
> **[third-party: playwright, pytest-playwright, selenium, axe-core/axe-playwright-python]**.

## 1. Purpose and scope

E2E tests prove that HTML, JavaScript, Django views, sessions, CSRF, static files and the
database work together in a browser. They are the most expensive and most flake-prone tests in a
Django suite, so this reference is as much about *when not* to write them as how.

Owned here: live-server mechanics for browser tests, Playwright/Selenium patterns, browser
authentication, page objects, E2E environment and reliability.

Not owned here:

- View/form behaviour testable with the Django test client →
  [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md).
- Why `live_server` uses transactional DB isolation →
  [database-and-transaction-testing.md](database-and-transaction-testing.md).
- Running E2E jobs and uploading traces in CI → [ci-cd-test-automation.md](ci-cd-test-automation.md).
- Flaky quarantine policy → [performance-and-reliability-testing.md](performance-and-reliability-testing.md).
- Frontend tooling, JS unit tests (Jest/Vitest) → [django-frontend](../../django-frontend/SKILL.md).
- Load testing → [django-performance](../../django-performance/SKILL.md).

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Live server** | A real HTTP server started in a thread of the test process, serving the Django app on `localhost:<random port>`. |
| **Browser driver** | Library controlling a browser: Playwright (own protocol, bundled browsers) or Selenium (WebDriver/BiDi). |
| **Journey** | End-to-end user flow with business value: sign-up → verify → first purchase. |
| **Web-first assertion** | Assertion that retries until a condition holds or times out (Playwright `expect`). |
| **Locator** | A lazy element query re-evaluated on every action (Playwright); prefer role, label, test id. |
| **Page object** | Class wrapping a page's locators and actions so tests read as user intent. |
| **Storage state** | Serialized cookies/localStorage reused to start a browser already authenticated. |
| **Trace** | Playwright recording (DOM snapshots, network, console) for post-mortem debugging. |

### When E2E is justified

| Justified | Not justified (use the test client) |
|---|---|
| Revenue/critical journeys (checkout, sign-up, login with MFA) | Every form validation message |
| Behaviour that only exists in JS (HTMX swaps, dynamic formsets, drag-and-drop, client-side validation) | Permission matrices — test via `Client`/`APIClient` |
| Integration of static assets, CSP, CSRF token handling in JS | Template context contents |
| Cross-page flows with redirects, sessions and third-party widgets (payment iframe sandbox) | API response shapes |
| Smoke check of a deployed environment | Business rules — service tests |

Rule: if a bug can be caught without JavaScript, catch it below the browser.

## 3. Internal mechanics

### Django live server classes **[Django]**

- `LiveServerTestCase` subclasses `TransactionTestCase`. It starts a `LiveServerThread`
  (threaded WSGI server) on `localhost` with port 0 (OS-assigned) and exposes
  `self.live_server_url`.
- Because the server thread uses its own DB connection, test data must be **committed** to be
  visible — hence `TransactionTestCase` semantics: tables are flushed after each test (slower;
  data created by data migrations is lost unless `serialized_rollback = True`).
- `LiveServerTestCase` does **not** serve static files via finders; `StaticLiveServerTestCase`
  (`django.contrib.staticfiles.testing`) serves them like `runserver` does, from app `static/`
  dirs without `collectstatic`.
- With in-memory SQLite, Django shares the connection with the server thread so data is visible;
  with PostgreSQL each thread has its own connection.

### pytest-django `live_server` **[pytest-django]**

- Session-scoped fixture that starts the live server once; requesting it enables transactional DB
  access for the test (`transactional_db`) so the server sees committed data `VERIFY` (fixture
  docs say it implies `transactional_db`).
- Static files are served when `django.contrib.staticfiles` is installed.
- `live_server.url` gives the base URL.

### Playwright sync API and Django's async safety **[third-party: playwright]**

Playwright's sync API runs an asyncio event loop in the calling thread. Django's ORM detects a
running event loop and raises `SynchronousOnlyOperation` when the test then calls the ORM
(factories, assertions). The documented workaround for **tests only** is:

```python
# conftest.py (E2E only)
import os
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
```

Never set this in production settings: it disables a safety check that prevents real
cross-thread connection misuse.

### Auto-waiting **[third-party: playwright]**

Every Playwright action waits for the element to be attached, visible, stable, enabled and
receiving events; `expect(...)` retries for the default 5 s. Selenium does not auto-wait; it
requires explicit `WebDriverWait` conditions.

## 4. Engineering decisions and trade-offs

| Decision | Options | Guidance |
|---|---|---|
| Tool | Playwright vs Selenium | Playwright for new suites (auto-wait, traces, bundled browsers, parallel contexts). Selenium when the org has a Grid/cloud provider contract or needs a browser Playwright doesn't drive. |
| Runner integration | pytest-playwright + `live_server` vs `StaticLiveServerTestCase` | Follow the suite's runner. Both can coexist; pytest runs `StaticLiveServerTestCase` classes. |
| Server under test | In-process live server vs deployed environment | In-process for PR/merge tests (data control, fast reset). Deployed staging for smoke tests only — no destructive data setup there. |
| Auth | UI login every test vs injected session vs `storage_state` | One test exercises real UI login; all others inject a session (fast, no flake surface). |
| Data setup | Through UI vs factories | Factories (committed); UI setup multiplies runtime and flake surface. |
| Structure | Page objects vs inline locators | Page objects once ≥ 3 tests touch a page; keep them thin (locators + actions, no assertions). |
| Browsers | Chromium only vs matrix | Chromium on PRs; Firefox/WebKit nightly if the product supports them. Each browser roughly multiplies runtime. |
| Retries | `--reruns` vs none | Prefer traces + fixes. If retries are used, report them and track the flaky rate. |

## 5. Django implementation patterns

### Playwright with pytest-django

```python
# tests/e2e/conftest.py
import os
import pytest

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")  # tests only, see §3


@pytest.fixture
def base_url(live_server):
    # pytest-playwright reads `base_url`, so page.goto("/path") becomes relative
    return live_server.url


@pytest.fixture
def logged_in_page(page, live_server, client, django_user_model):
    """Authenticate by injecting a real Django session cookie — no UI login."""
    user = django_user_model.objects.create_user(email="buyer@example.test", password="x")
    client.force_login(user)  # creates a session row, committed under transactional_db
    session_cookie = client.cookies["sessionid"]
    page.context.add_cookies([{
        "name": "sessionid",
        "value": session_cookie.value,
        "url": live_server.url,
    }])
    page.user = user
    return page
```

Adjust the cookie name if `SESSION_COOKIE_NAME` is customised. `pytest-playwright` provides
`page`, `context`, `browser` fixtures and the CLI options `--browser`, `--headed`, `--tracing`,
`--screenshot`, `--video` **[third-party: pytest-playwright]**.

```python
# tests/e2e/test_checkout.py
import re

import pytest
from playwright.sync_api import Page, expect

from apps.catalog.tests.factories import ProductFactory

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def test_customer_can_buy_a_product(logged_in_page: Page):
    page = logged_in_page
    product = ProductFactory(name="Walnut desk", price="349.00", stock=3)

    page.goto(f"/products/{product.slug}/")
    page.get_by_role("button", name="Add to basket").click()
    expect(page.get_by_test_id("basket-count")).to_have_text("1")   # HTMX swap, auto-retried

    page.get_by_role("link", name="Checkout").click()
    page.get_by_label("Delivery address").fill("1 Test Street, Testville")
    page.get_by_role("button", name="Place order").click()

    expect(page).to_have_url(re.compile(r"/orders/\d+/$"))
    expect(page.get_by_role("heading", name="Thank you")).to_be_visible()

    # Verify the durable effect at the DB boundary, not only the UI
    order = page.user.orders.get()
    assert order.status == "placed"
    product.refresh_from_db()
    assert product.stock == 2
```

`expect(page).to_have_url()` accepts a string or a compiled `re.Pattern` — not a callable.

Use `data-testid` attributes only where role/label locators are ambiguous; configure a custom
attribute with `selectors.set_test_id_attribute("data-test")` if the frontend uses another name.

### `StaticLiveServerTestCase` with Playwright (unittest style)

```python
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import sync_playwright, expect


class LoginJourneyTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def test_login_with_ui(self):
        UserFactory(email="a@example.test", password="correct horse")
        page = self.browser.new_page()
        page.goto(f"{self.live_server_url}/accounts/login/")
        page.get_by_label("Email").fill("a@example.test")
        page.get_by_label("Password").fill("correct horse")
        page.get_by_role("button", name="Log in").click()
        expect(page.get_by_role("link", name="Log out")).to_be_visible()
        page.close()
```

This is the one test that exercises the real login form (CSRF token, password hasher, session
creation); others use cookie injection.

### Reusing UI login via storage state

For a deployed environment where you cannot create sessions directly:

```python
@pytest.fixture(scope="session")
def auth_state(browser, tmp_path_factory, base_url):
    path = tmp_path_factory.mktemp("auth") / "state.json"
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{base_url}/accounts/login/")
    # ... fill and submit ...
    ctx.storage_state(path=str(path))
    ctx.close()
    return path
```

Then `browser.new_context(storage_state=auth_state)`. Treat the state file as a credential; don't
upload it as an artifact.

### Selenium 4 **[third-party: selenium]**

```python
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class BasketSeleniumTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        cls.driver = webdriver.Chrome(options=options)  # Selenium Manager resolves the driver (≥ 4.6)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        super().tearDownClass()

    def test_add_to_basket(self):
        product = ProductFactory()
        self.driver.get(f"{self.live_server_url}/products/{product.slug}/")
        self.driver.find_element(By.CSS_SELECTOR, "[data-testid=add-to-basket]").click()
        WebDriverWait(self.driver, 5).until(
            EC.text_to_be_present_in_element((By.CSS_SELECTOR, "[data-testid=basket-count]"), "1")
        )
```

Use explicit waits only; mixing `implicitly_wait` with `WebDriverWait` produces unpredictable
combined timeouts (documented Selenium warning).

### Thin page objects

```python
class CheckoutPage:
    def __init__(self, page):
        self.page = page
        self.address = page.get_by_label("Delivery address")
        self.place_order = page.get_by_role("button", name="Place order")

    def open(self):
        self.page.goto("/checkout/")
        return self

    def submit(self, address: str):
        self.address.fill(address)
        self.place_order.click()
```

Assertions stay in tests. Page objects that grow business logic, waits and assertions become a
second application to maintain.

### Accessibility smoke (optional)

`axe-playwright-python` **[third-party]** or injecting axe-core can assert no serious violations on
key pages. Treat it as a smoke check — it complements, not replaces, manual accessibility review.

## 6. Production use cases

- **E-commerce checkout**: 6 E2E journeys (guest checkout, logged-in checkout, coupon, payment
  sandbox failure, address validation via JS, order history) run on merge; hundreds of client-level
  tests cover the rest.
- **HTMX admin dashboard**: E2E verifies partial swaps preserve CSRF tokens and focus management;
  the views behind each swap are tested with `Client` + `HX-Request` headers.
- **Post-deploy smoke**: Playwright run against staging with a dedicated test tenant and
  `storage_state` login; read-only assertions only.
- **Regression for a CSP break**: a test loads the page and asserts no console errors
  (`page.on("console", ...)`), catching inline scripts blocked by a new CSP.

## 7. Advantages and disadvantages

| Aspect | Advantages | Disadvantages |
|---|---|---|
| Confidence | Only level that exercises JS, CSS, cookies and real browser behaviour together | Failures are often far from the cause |
| Cost | Few tests cover a lot of wiring | Seconds per test, transactional DB, browser install in CI |
| Playwright | Auto-wait, traces, multiple contexts, fast | Extra dependency and browser binaries; sync API needs async-unsafe flag |
| Selenium | Standard WebDriver, grids and vendor clouds | Manual waits, slower, more flake-prone |
| Page objects | Locator changes in one place | Abstraction tax, can hide intent |

## 8. Security and performance considerations

**Security**

- `DJANGO_ALLOW_ASYNC_UNSAFE` only in E2E test configuration, never in deployed settings.
- Test credentials and `storage_state` files are secrets in deployed-environment smoke tests;
  use dedicated low-privilege accounts and a dedicated tenant.
- Don't disable CSRF or CSP for E2E — a test that passes only with protections off proves
  nothing. Browser tests are a good place to prove CSRF integration in JS still works
  ([security-testing.md](security-testing.md)).
- Traces and videos capture everything on screen, including personal data; use synthetic data.

**Performance**

- Live-server tests are transactional: each test flushes tables. Keep E2E count small; run
  them in a separate job.
- Reuse one browser per session, fresh `context` per test (cheap and isolated).
- With pytest-xdist, each worker starts its own live server and browser — works, but watch memory.
- Block unneeded third-party requests (`page.route("**/analytics/**", lambda r: r.abort())`) to
  speed up and de-flake tests.

## 9. Common mistakes and anti-patterns

- `time.sleep()` / `page.wait_for_timeout()` instead of web-first assertions.
- CSS/XPath selectors tied to layout (`div > div:nth-child(3)`); prefer role/label/test id.
- Logging in through the UI in every test.
- Creating data through the UI instead of factories.
- Testing validation messages and permission matrices in the browser.
- Using `LiveServerTestCase` and wondering why CSS/JS 404 (needs `StaticLiveServerTestCase`).
- Forgetting the live server can't see uncommitted data (using `TestCase` or plain `django_db`).
- Asserting only on UI text without checking the persisted outcome.
- Order-dependent journeys ("test 2 continues where test 1 left off").
- Running full cross-browser matrix on every PR.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| `SynchronousOnlyOperation` in test body | Playwright sync API event loop + ORM | `DJANGO_ALLOW_ASYNC_UNSAFE=true` in E2E conftest |
| Page shows no data created by the test | Data not committed / wrong DB marker | `transaction=True`, `live_server`, or `LiveServerTestCase` |
| Static files 404 | `LiveServerTestCase` doesn't serve static | `StaticLiveServerTestCase`; `staticfiles` installed |
| 403 CSRF on form submit in browser | Origin/host mismatch with live server URL, cookie not set | Use `live_server.url` consistently; don't mix `127.0.0.1` and `localhost` |
| Session cookie injection ignored | Wrong cookie name/domain, `SESSION_COOKIE_SECURE=True` on http | Match `SESSION_COOKIE_NAME`; disable secure cookie flag in test settings only |
| Works headed, fails headless | Viewport/responsive layout, timing | Set explicit viewport; use web-first assertions |
| Intermittent timeouts in CI | Slow runner, external assets | Trace on failure, block third-party requests, raise per-assertion timeout locally not globally |
| `Executable doesn't exist` | Browsers not installed | `playwright install --with-deps chromium` in CI |

Debugging: `pytest -m e2e --headed --slowmo 300`, `PWDEBUG=1` (Playwright Inspector),
`--tracing retain-on-failure` then `playwright show-trace trace.zip`.

Validation checklist:

- [ ] Each E2E test maps to a named critical journey or JS-only behaviour.
- [ ] Data via factories; auth via session injection except one real-login test.
- [ ] No sleeps; locators by role/label/test id; DB outcome asserted.
- [ ] Runs in its own CI job with traces/screenshots uploaded on failure.

## 11. Related references and skills

- [views-forms-and-middleware-testing.md](views-forms-and-middleware-testing.md) — test client alternatives.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — transactional isolation behind live servers.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — factories for committed data.
- [security-testing.md](security-testing.md) — CSRF, session and login flow assertions.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — E2E job and artifacts.
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — flaky policy.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — root-cause debugging.
- [async-testing.md](async-testing.md) — event loop and async-safety background.
- [django-frontend](../../django-frontend/SKILL.md) — templates, JS and asset pipeline.
- [django-core asynchronous-django](../../django-core/references/asynchronous-django.md) — async-safety mechanics.

## 12. Official documentation and external references

- Django `LiveServerTestCase`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#liveservertestcase>
- Django `StaticLiveServerTestCase`: <https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/#specialized-test-case-to-support-live-testing>
- Django async safety (`DJANGO_ALLOW_ASYNC_UNSAFE`): <https://docs.djangoproject.com/en/5.2/topics/async/#async-safety>
- pytest-django `live_server`: <https://pytest-django.readthedocs.io/en/latest/helpers.html#live-server>
- Playwright for Python: <https://playwright.dev/python/docs/intro>
- Playwright pytest plugin: <https://playwright.dev/python/docs/test-runners>
- Playwright locators & assertions: <https://playwright.dev/python/docs/locators> · <https://playwright.dev/python/docs/test-assertions>
- Playwright authentication: <https://playwright.dev/python/docs/auth>
- Playwright trace viewer: <https://playwright.dev/python/docs/trace-viewer>
- Selenium documentation: <https://www.selenium.dev/documentation/>
- Selenium waits: <https://www.selenium.dev/documentation/webdriver/waits/>
- Selenium Manager: <https://www.selenium.dev/documentation/selenium_manager/>
- axe-core: <https://github.com/dequelabs/axe-core>
