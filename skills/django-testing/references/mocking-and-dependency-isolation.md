# Mocking and Dependency Isolation

> **Scope:** The mechanics of replacing dependencies in Django tests — `unittest.mock`, patching,
> autospec, pytest `monkeypatch`, fakes and stubs, HTTP and cloud-SDK doubles, email and storage
> backends, and control of time and randomness — and how to keep doubles from coupling tests
> to implementation.
> **Baseline:** Django 5.2 LTS · Python ≥ 3.10 · pytest ≥ 8 · pytest-django 4.x · **Depends on:**
> **[Python]** `unittest.mock` · **[pytest]** `monkeypatch` · **[pytest-django]** `settings`, `mailoutbox` ·
> **[Django]** locmem email, `InMemoryStorage`, `override_settings` · **[third-party: pytest-mock]** ·
> **[third-party: responses]** · **[third-party: respx]** · **[third-party: vcrpy / pytest-recording]** ·
> **[third-party: pytest-socket]** · **[third-party: moto]** · **[third-party: time-machine / freezegun]**

## 1. Purpose and scope

A Django test should exercise real code and a real test database, and replace only what
crosses the process boundary or is non-deterministic: HTTP APIs, cloud SDKs, SMTP, object
storage, brokers, clocks and random sources. This reference owns **how** to build and install
those doubles.

**A double proves your code's behaviour given an assumed response. It never proves the real
service behaves that way.** Real-integration confidence comes from contract, sandbox or
recorded tests — scenarios owned by [integration-and-background-task-testing.md](integration-and-background-task-testing.md).

Out of scope: what to test at each integration boundary (integration reference), Celery task
testing (integration reference), async mock specifics beyond `AsyncMock`
([async-testing.md](async-testing.md)).

## 2. Core concepts

### Test-double taxonomy (with Django examples)

| Double | What it does | Django example |
|---|---|---|
| **Dummy** | Fills a parameter; never used | `actor=None` for a function that only logs the actor in another branch |
| **Stub** | Returns canned answers; no assertions on calls | `get_exchange_rate` returns `Decimal("1.08")` |
| **Fake** | Working lightweight implementation | `FakePaymentGateway` storing charges in a dict; locmem email backend; `InMemoryStorage` |
| **Mock** | Records calls; the test asserts on them | `MagicMock` for `send_sms` with `assert_called_once_with(...)` |
| **Spy** | Wraps the real object and records calls | `mocker.spy(services, "recalculate_totals")` |

Rule of thumb: **prefer fakes for collaborators you own the interface of, stubs for queries,
mocks only for outgoing commands whose occurrence is the behaviour** (an email sent, a charge
requested). Never mock the ORM or the unit under test.

### Provenance of the tools

| Tool | Provenance | Use for |
|---|---|---|
| `Mock`, `MagicMock`, `AsyncMock`, `patch`, `create_autospec`, `seal` | **[Python]** `unittest.mock` (`AsyncMock` Python ≥ 3.8) | Any double |
| `monkeypatch` fixture | **[pytest]** | Attributes, env vars, dict items, `sys.path`; auto-undone |
| `mocker` fixture | **[third-party: pytest-mock]** | `patch` without decorators/context managers, `spy`, auto-cleanup |
| `settings` fixture | **[pytest-django]** | Changing Django settings with auto-restore |
| `override_settings` | **[Django]** | Same, unittest style; also fires `setting_changed` |
| `mailoutbox` | **[pytest-django]** | `django.core.mail.outbox` cleared per test |
| `responses` | **[third-party]** | Mocks `requests` at the adapter level |
| `respx` / `httpx.MockTransport` | **[third-party]** / **[third-party: httpx]** | Mocks `httpx` at the transport level |
| `vcrpy` / `pytest-recording` | **[third-party]** | Record/replay real HTTP |
| `pytest-socket` | **[third-party]** | Fail any test that opens a socket |
| `moto` | **[third-party]** | In-process fake of AWS APIs for boto3 |
| `botocore.stub.Stubber` | **[third-party: botocore]** | Strict canned boto3 responses |
| `time-machine` / `freezegun` | **[third-party]** | Freeze or travel time |

## 3. Internal mechanics

### `patch` replaces a name, not an object

`patch("a.b.c")` imports `a.b`, saves attribute `c`, sets it to a `MagicMock`, and restores it
on exit. It changes **one reference**. If `apps/orders/services.py` does
`from apps.notifications.sms import send_sms`, the service holds its own reference — so patch
`apps.orders.services.send_sms` (where it is **looked up**), not
`apps.notifications.sms.send_sms`. If the service does `from apps.notifications import sms`
and calls `sms.send_sms`, patch `apps.notifications.sms.send_sms`.

### Autospec

A bare `MagicMock` accepts any attribute and any call signature, so a renamed method or a
wrong argument list still "passes". `autospec=True` / `create_autospec(obj)` builds a mock
whose attributes and call signatures mirror the real object: calling with wrong arguments
raises `TypeError`, accessing a missing attribute raises `AttributeError`.
`spec_set=True` additionally forbids *setting* attributes that don't exist. `seal(mock)`
(Python ≥ 3.7) stops auto-creation of child attributes after configuration.

### `side_effect`

- An exception class/instance → raised on call.
- An iterable → each call returns the next item (exceptions in the iterable are raised);
  exhausting it raises `StopIteration`.
- A callable → called with the same args; its return value is returned (unless it returns `DEFAULT`).

### `monkeypatch` vs `patch`

Both swap attributes and restore them. `monkeypatch` is a **[pytest]** fixture with
function scope and also handles `setenv`/`delenv`/`setitem`/`chdir`; it doesn't create mock
objects or record calls. Use it to install your own fake; use `patch`/`mocker` when you need
call recording.

### Django test-environment substitutions

The **[Django]** test runner's `setup_test_environment()` (also invoked by **[pytest-django]**)
sets `EMAIL_BACKEND` to `django.core.mail.backends.locmem.EmailBackend`, which appends
messages to `django.core.mail.outbox`. Django's `TestCase`/`SimpleTestCase` empty the outbox
before each test; pytest-django's `mailoutbox` fixture does the same.

### Transport-level HTTP mocking

`responses` installs a fake `requests` transport adapter; `respx` routes `httpx` transports.
Both intercept **below** your client code, so your timeouts, headers, retries, JSON parsing
and error mapping still run. Patching `requests.get` itself skips all of that.

## 4. Engineering decisions and trade-offs

### Where to cut

| Seam | Example | Verdict |
|---|---|---|
| Inside the unit under test | Patching `Order.save` in a test of `place_order` | Never — tests the mock |
| Your own collaborators (same process, same DB) | `recalculate_totals()` called by `place_order` | Usually don't — use the real thing |
| Your gateway/adapter interface | `PaymentGateway.charge()` | **Best seam** — fake it, injected via settings or argument |
| The HTTP transport | `requests`/`httpx` | Best for testing the adapter itself |
| The third-party SDK object | `stripe.PaymentIntent.create` | Acceptable with autospec; prefer the gateway seam |

### Mock vs fake

| | Mocks | Fakes |
|---|---|---|
| Setup cost | Low per test | Upfront class, reused |
| Coupling | High — tests know call shapes | Low — tests assert on outcomes (`fake.charges`) |
| Realism | Whatever you configured | Can model state (refund only an existing charge) |
| Drift risk | Silent (use autospec) | Guard with a shared contract test run against fake and real |

### Record/replay (VCR) trade-off

Cassettes give realistic payloads cheaply but **age**: the provider changes, the cassette
doesn't. Scrub secrets, store them in review, and re-record on a schedule; never treat them as
proof that today's API still matches.

## 5. Django implementation patterns

### Patch where it is looked up, with autospec

```python
# apps/orders/services.py
from apps.notifications.sms import send_sms

def ship_order(*, order):
    ...
    send_sms(to=order.customer.phone, body=f"Order {order.reference} shipped")
```

```python
def test_ship_order_notifies_customer(db, mocker):
    order = OrderFactory(customer__phone="+447700900123")
    send = mocker.patch("apps.orders.services.send_sms", autospec=True)

    ship_order(order=order)

    send.assert_called_once_with(to="+447700900123", body=f"Order {order.reference} shipped")
```

Without `mocker` (no pytest-mock):

```python
from unittest import mock

@mock.patch("apps.orders.services.send_sms", autospec=True)
def test_ship_order_notifies_customer(send, db):
    ...
```

Assert on **meaningful arguments**, not on every incidental call. `call_args.kwargs["to"]` is
better than an exact full-call assertion when the body text is not the behaviour under test.

### Fake gateway injected via settings

```python
# apps/payments/gateways.py
from django.conf import settings
from django.utils.module_loading import import_string

class PaymentGateway(Protocol):
    def charge(self, *, amount: Decimal, token: str, idempotency_key: str) -> str: ...

def get_gateway() -> PaymentGateway:
    return import_string(settings.PAYMENT_GATEWAY)()
```

```python
# apps/payments/testing.py  — shipped with the app, usable by other apps' tests
class FakePaymentGateway:
    instances: list["FakePaymentGateway"] = []

    def __init__(self):
        self.charges: dict[str, Decimal] = {}
        self.fail_with: Exception | None = None
        FakePaymentGateway.instances.append(self)

    def charge(self, *, amount, token, idempotency_key):
        if self.fail_with:
            raise self.fail_with
        if idempotency_key in self.charges:          # models provider idempotency
            return f"ch_{idempotency_key}"
        self.charges[idempotency_key] = amount
        return f"ch_{idempotency_key}"
```

```python
@pytest.fixture
def fake_gateway(settings, monkeypatch):
    gw = FakePaymentGateway()
    settings.PAYMENT_GATEWAY = "apps.payments.testing.FakePaymentGateway"
    monkeypatch.setattr("apps.payments.gateways.get_gateway", lambda: gw)
    return gw

def test_checkout_charges_order_total(db, fake_gateway):
    order = OrderBuilder().with_line(qty=2, price=Decimal("5.00")).build()
    checkout(order=order, token="tok_test")
    assert list(fake_gateway.charges.values()) == [Decimal("10.00")]
```

The test asserts an **outcome** (what was charged), survives refactors of how `checkout`
calls the gateway, and the fake encodes provider semantics (idempotency) once. See
[django-architecture: dependency-management.md](../../django-architecture/references/dependency-management.md)
for designing the seam.

### Settings and environment

```python
def test_feature_disabled(settings):
    settings.ORDERS_ALLOW_BACKORDER = False           # [pytest-django], auto-restored

def test_reads_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-2")      # [pytest]
    monkeypatch.delenv("HTTP_PROXY", raising=False)
```

Never assign `django.conf.settings.X = ...` directly — it is not restored and leaks into
later tests. Settings read at import time (module-level constants) are **not** affected by
either fixture; patch the constant itself or read settings lazily.

### HTTP: requests + responses

```python
import responses
from responses import matchers

@responses.activate
def test_fetch_customer_maps_404_to_none():
    responses.get(
        "https://crm.example.test/v2/customers/42",
        status=404,
        match=[matchers.header_matcher({"Authorization": "Bearer test-token"})],
    )
    assert crm_client(token="test-token").fetch_customer(42) is None
```

`responses` raises `ConnectionError` for any unregistered URL, so unexpected calls fail loudly.
`assert_all_requests_are_fired=True` (in `RequestsMock`) fails if a registered route was not
called.

### HTTP: httpx + respx or MockTransport

```python
import httpx, respx

@respx.mock
def test_rates_retry_then_succeed():
    route = respx.get("https://rates.example.test/v1/latest").mock(
        side_effect=[httpx.ConnectTimeout("boom"), httpx.Response(200, json={"EUR": "1.08"})]
    )
    assert fetch_rates()["EUR"] == Decimal("1.08")
    assert route.call_count == 2
```

Dependency-free alternative when the client accepts an injected `httpx.Client`:

```python
def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Api-Key"] == "test"
    return httpx.Response(200, json={"status": "ok"})

client = RatesClient(http=httpx.Client(transport=httpx.MockTransport(handler)))
```

### Record/replay with secret scrubbing

```python
# conftest.py — [third-party: pytest-recording] on top of vcrpy
@pytest.fixture(scope="module")
def vcr_config():
    return {
        "filter_headers": ["authorization", "x-api-key"],
        "filter_query_parameters": ["api_key"],
        "record_mode": "none",             # CI never hits the network
    }

@pytest.mark.vcr
def test_geocode_known_address():
    assert geocode("10 Downing Street, London").country == "GB"
```

Review cassettes for PII and tokens in **bodies** too — header filters don't scrub JSON.

### Block the network

```toml
# pyproject.toml — [third-party: pytest-socket]
[tool.pytest.ini_options]
addopts = "--disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,localhost"
```

PostgreSQL/Redis on localhost or a Unix socket keep working; any accidental real API call
fails with `SocketBlockedError`. Remote CI DB hosts (e.g. a `postgres` service hostname) must
be added to `--allow-hosts` (VERIFY whether hostnames or only IPs are accepted in your
pytest-socket version).

### Cloud SDKs: moto and Stubber

```python
import boto3
from moto import mock_aws          # moto >= 5; earlier versions had per-service mock_s3, ...

@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="invoices", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        yield client

def test_archive_invoice_uploads_pdf(db, s3):
    invoice = InvoiceFactory()
    archive_invoice(invoice=invoice)
    obj = s3.get_object(Bucket="invoices", Key=f"{invoice.tenant_id}/{invoice.number}.pdf")
    assert obj["ContentType"] == "application/pdf"
```

Set fake credentials so a misconfigured test can never reach a real account. `Stubber` is
stricter (exact expected calls in order) and suits testing error handling:

```python
from botocore.stub import Stubber
client = boto3.client("s3", region_name="eu-west-1")
with Stubber(client) as stub:
    stub.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(ArchiveError):
        archive_invoice(invoice=invoice, s3=client)
```

### Email

```python
def test_password_reset_sends_one_email(client, mailoutbox, user):
    client.post(reverse("password_reset"), {"email": user.email})
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == [user.email]
    assert "/reset/" in mailoutbox[0].body
```

No patching needed: the locmem backend is a fake. Patch `send_mail` only when you must prove
it was *not* called and the locmem outbox is not reachable (rare).

### Storage

```python
# config/settings/test.py — Django >= 4.2
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
```

Or per test when the code writes to the filesystem: `settings.MEDIA_ROOT = tmp_path`. Fields
with an explicit `storage=` argument bypass `STORAGES["default"]`; use a callable storage
(Django ≥ 3.1) or a storage alias so tests can substitute it.

### Time

Prefer an injected clock in domain code; fall back to a time library at the edges.

```python
import time_machine     # [third-party: time-machine]; freezegun has the same shape

@time_machine.travel("2026-03-29 00:59:00+00:00", tick=False)
def test_reminder_sent_after_dst_change(db):
    ...

def test_token_expires(db):
    with time_machine.travel("2026-01-01 12:00Z", tick=False) as traveller:
        token = issue_token(user=UserFactory())
        traveller.shift(dt.timedelta(minutes=16))
        assert not token_is_valid(token)
```

Pitfalls: patching `django.utils.timezone.now` only affects callers that look it up through
the module (`timezone.now()`), not code that did `from django.utils.timezone import now`;
`auto_now`/`auto_now_add` fields call `timezone.now()` internally — time libraries cover them,
`patch` may not; `freezegun` can be slow on large suites because it scans loaded modules,
`time-machine` patches at the C level and is faster. DB-side `Now()`/`db_default=Now()` is
**not** affected by either — the database clock keeps running.

### Randomness and identifiers

```python
def generate_voucher_code(rng: random.Random | None = None) -> str:
    rng = rng or random.SystemRandom()
    return "".join(rng.choices(ALPHABET, k=10))

def test_voucher_code_format():
    assert generate_voucher_code(random.Random(1)) == generate_voucher_code(random.Random(1))
```

Security-sensitive code uses `secrets`; test its properties (length, alphabet, uniqueness over
N draws) rather than seeding it. For `uuid.uuid4()` assert format or patch the call site.

### Exceptions and retries

```python
def test_sync_gives_up_after_three_attempts(mocker):
    mocker.patch("apps.crm.sync.time.sleep")                 # or patch tenacity's wait
    fetch = mocker.patch(
        "apps.crm.sync.crm_client.fetch", autospec=True,
        side_effect=[CRMUnavailable(), CRMUnavailable(), CRMUnavailable()],
    )
    with pytest.raises(SyncFailed):
        sync_contacts()
    assert fetch.call_count == 3
```

With **[third-party: tenacity]**, disable waits per test via
`sync_contacts.retry.wait = tenacity.wait_none()` (restored manually) or construct the retrying
policy from settings so tests set zero wait. Celery retry scenarios live in
[integration-and-background-task-testing.md](integration-and-background-task-testing.md).

### Async doubles

`patch` auto-detects `async def` targets and uses `AsyncMock` (Python ≥ 3.8); assert with
`assert_awaited_once_with`. More in [async-testing.md](async-testing.md).

## 6. Production use cases

- **Payments:** `FakePaymentGateway` shared across checkout, refunds and subscription tests;
  a nightly contract test runs the same scenario list against the provider sandbox.
- **Notifications:** SMS/push adapters faked; email via locmem; tests assert recipients and
  template content.
- **Document archive:** moto for S3 with a fixture-created bucket; Stubber for permission
  failures.
- **Scheduling:** billing cycle and reminder logic driven by `time-machine` across month-end,
  leap day and DST transitions.
- **Network hygiene:** `pytest-socket` in CI catches a newly added SDK that phones home at import.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| `patch` + autospec | Precise, quick, signature-checked | Couples to import paths and call shapes |
| Fakes | Outcome assertions, reusable, refactor-safe | Must be maintained; can drift from the real system |
| Transport mocks | Exercise the real client code paths | URL/payload duplication across tests |
| VCR cassettes | Realistic payloads | Stale, may contain secrets, large diffs |
| moto | Rich AWS behaviour in process | Not every API/edge case; behaviour differs from AWS in corner cases |
| Time libraries | Deterministic time everywhere in Python | Do not affect DB time; global patching can surprise other libraries |

## 8. Security and performance considerations

- Doubles must never hold real credentials; use obviously fake values (`test-token`) and fake
  AWS keys so misrouted calls fail authentication.
- Cassettes and fixture payloads are code-reviewed artifacts: scrub tokens, cookies, emails
  and bodies; secret-scan them in CI.
- Don't mock security controls (permission checks, signature verification) to make a test
  pass — test them with real inputs ([security-testing.md](security-testing.md)).
- Mocks are cheap; fakes are cheap; real HTTP is slow and flaky — blocking the network speeds
  suites as well as protecting them.
- `freezegun` import scanning can add measurable time per test on large codebases; prefer
  `time-machine` or an injected clock where speed matters.

## 9. Common mistakes and anti-patterns

- **Patching the definition site** instead of the lookup site — the mock is never called and
  the real function runs (possibly hitting the network).
- **Bare `MagicMock` without spec** — a renamed method keeps passing.
- **Mock returning a mock** — `client.get().json().get()` chains that assert nothing real.
- **Asserting internal call order** of your own functions — every refactor breaks the test.
- **Mocking the ORM** (`Order.objects.filter` patched) — use the test DB.
- **Patching the unit under test** or a helper in the same module the test is about.
- **`settings.X = ...` without a fixture** — leaks between tests.
- **Believing a mocked integration test proves the integration** — it proves your assumptions.
- **Overly broad patches** (`patch("requests.get")` for the whole module) hiding unexpected calls.
- **Unrestored global changes**: `random.seed()` in one test, timezone set on the process.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Mock `assert_called_once` fails, real code ran | Patched wrong path | Patch the module where the name is used |
| `AttributeError: module has no attribute` in `patch` | Typo or lazy import not yet loaded | Fix path; import the module first or patch the object |
| `TypeError: missing a required argument` with autospec | Test calls or asserts with an outdated signature | Good — update the test to the real signature |
| Test passes but makes a real HTTP call | Client library not covered by the mock (e.g. `httpx` vs `requests`) | Use the matching library; enable `pytest-socket` |
| `responses.ConnectionError: Connection refused by Responses` | Unregistered URL/method/query | Register it or fix the code's URL |
| Time frozen but `created_at` wrong | DB default `Now()` or code imported `now` directly | Assert relative to DB time; patch the right name |
| Setting override has no effect | Value read at import time / cached | Read settings at call time; clear the cache |
| `StopIteration` from a mock | `side_effect` iterable exhausted | Extra unexpected call — inspect `call_args_list` |

Inspect calls with `mock.call_args_list`, `mock.mock_calls`; `pytest -p no:randomly -x -vv`
narrows ordering effects.

## 11. Related references and skills

- [integration-and-background-task-testing.md](integration-and-background-task-testing.md) — scenarios using these doubles (webhooks, Celery, retries).
- [service-and-architecture-testing.md](service-and-architecture-testing.md) — where seams belong when testing services.
- [async-testing.md](async-testing.md) — `AsyncMock`, async HTTP clients.
- [fixtures-and-test-data.md](fixtures-and-test-data.md) — seeding Faker/factories.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — time-dependent and leaking-state failures.
- [django-architecture: dependency-management.md](../../django-architecture/references/dependency-management.md) — designing injectable gateways.
- [django-core: file-handling.md](../../django-core/references/file-handling.md) — storage backends.
- [django-integrations](../../django-integrations/SKILL.md) — HTTP client and SDK design.

## 12. Official documentation and external references

- Python `unittest.mock`: <https://docs.python.org/3/library/unittest.mock.html> · "where to patch": <https://docs.python.org/3/library/unittest.mock.html#where-to-patch>
- pytest `monkeypatch`: <https://docs.pytest.org/en/stable/how-to/monkeypatch.html>
- pytest-mock: <https://pytest-mock.readthedocs.io/en/latest/>
- pytest-django helpers (`settings`, `mailoutbox`): <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- Django email testing: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#email-services> · in-memory backend: <https://docs.djangoproject.com/en/5.2/topics/email/#in-memory-backend>
- Django `InMemoryStorage`: <https://docs.djangoproject.com/en/5.2/ref/files/storage/#the-inmemorystorage-class>
- Django `override_settings`: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#overriding-settings>
- responses: <https://github.com/getsentry/responses> · respx: <https://lundberg.github.io/respx/> · httpx MockTransport: <https://www.python-httpx.org/advanced/transports/#mock-transports>
- vcrpy: <https://vcrpy.readthedocs.io/en/latest/> · pytest-recording: <https://github.com/kiwicom/pytest-recording>
- pytest-socket: <https://github.com/miketheman/pytest-socket>
- moto: <https://docs.getmoto.org/en/latest/> · botocore Stubber: <https://botocore.amazonaws.com/v1/documentation/api/latest/reference/stubber.html>
- time-machine: <https://time-machine.readthedocs.io/en/latest/> · freezegun: <https://github.com/spulec/freezegun>
- tenacity: <https://tenacity.readthedocs.io/en/latest/>
