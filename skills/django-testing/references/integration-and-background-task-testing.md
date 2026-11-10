# Integration and Background Task Testing

> **Scope:** What to prove — and at which layer — where a Django application meets the outside
> world: third-party APIs, inbound webhooks, email, object storage, Celery tasks, retries,
> idempotency, event-driven workflows and management commands.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · Celery 5.x · **Depends on:**
> **[Django]** test client, `call_command`, locmem email, `captureOnCommitCallbacks` ·
> **[pytest-django]** `django_capture_on_commit_callbacks`, `mailoutbox` ·
> **[third-party: celery]** · **[third-party: responses / respx]** · **[third-party: moto]** ·
> optional **[third-party: MinIO / LocalStack]** containers

## 1. Purpose and scope

Integration boundaries are where production incidents concentrate: a provider returns 429, a
webhook arrives twice, a task retries after half its work committed, an email is sent for an
order that rolled back. This reference owns the **scenarios** that must be tested at each
boundary and the **layering** of those tests (fast doubles vs real-ish dependencies vs live
smoke tests).

The *mechanics* of building doubles (patching, `responses`, `respx`, moto, time control) are in
[mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md). Transactional
mechanics of `on_commit` are in [database-and-transaction-testing.md](database-and-transaction-testing.md).
Designing the clients, task topology and webhook endpoints is owned by
[django-integrations](../../django-integrations/SKILL.md).

## 2. Core concepts

### Three layers for every external dependency

| Layer | Runs | Uses | Proves | Marker |
|---|---|---|---|---|
| **Adapter unit tests** | Every commit | Transport-level mocks (`responses`/`respx`), fakes | Your request building, error mapping, retries, parsing | default |
| **Contract tests** | Every commit or nightly | Recorded cassettes, provider sandbox, consumer-driven contracts, local emulators (moto/MinIO/LocalStack) | The provider still accepts your requests and returns the shapes you parse | `@pytest.mark.contract` |
| **Live smoke tests** | Nightly / pre-release, in a staging env | Real sandbox credentials | Credentials, network path, TLS, quotas | `@pytest.mark.live` (excluded by default) |

The first layer gives speed and determinism; the second and third catch drift that mocks
cannot. A suite with only the first layer has **no evidence the integration works**.

### Boundary scenarios checklist

For each outbound integration: success · validation/4xx mapping · auth failure (401/403) ·
not found · rate limited (429 + `Retry-After`) · server error (5xx) · timeout · connection
error · malformed/partial body · slow response vs your timeout · idempotent retry.

For each inbound integration (webhooks): valid signature · invalid signature · missing
signature · stale timestamp (replay window) · duplicate delivery · out-of-order delivery ·
unknown event type · malformed body · processing failure after acknowledgement.

For each background task: happy path · enqueued only after commit · not enqueued on rollback ·
retry on transient error · no retry on permanent error · max retries exhausted · idempotent
on re-execution · arguments serialisable.

## 3. Internal mechanics

### Celery execution paths in tests

| Path | What happens | Exercises |
|---|---|---|
| Call the function: `send_invoice(invoice_id)` | Plain Python call of the task body | Business logic only |
| `task.apply(args=...)` | Runs synchronously in-process, returns an `EagerResult`; honours `self.retry()` by re-applying locally (VERIFY retry semantics with `apply` in your Celery version) | Bound-task `self`, retry flow |
| `task.delay()` with `task_always_eager=True` | `delay` behaves like `apply` | Wiring of callers — but **no serialisation, no broker, no routing, no acks** |
| `celery_worker` fixture (`celery.contrib.pytest`) | Real worker thread with an in-memory broker/backend | Serialisation, routing, result backend, signals |
| Real broker in CI (Redis/RabbitMQ service) | Full path | Broker config, visibility timeouts |

Celery's documentation explicitly discourages `task_always_eager` for testing because eager
mode differs from real execution (no serialisation, synchronous `.get()`, different error
propagation). Use it, if at all, for a few end-to-end flow tests with
`task_eager_propagates=True`.

### `on_commit` and tasks

Code that enqueues tasks should do so in `transaction.on_commit(...)`. In a `TestCase` /
`django_db` test the outer transaction never commits, so callbacks never run unless captured:
**[Django]** `TestCase.captureOnCommitCallbacks(execute=True)` (Django ≥ 3.2) or
**[pytest-django]** `django_capture_on_commit_callbacks(execute=True)`. Capture also lets you
assert that a rollback enqueues **nothing**.

### Webhook request bodies

Signature schemes (HMAC over the raw body plus a timestamp) require the **exact bytes**. The
Django test client re-encodes dicts, so webhook tests must send a pre-serialised body with an
explicit `content_type` and compute the signature over those same bytes.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Guidance |
|---|---|---|---|
| Outbound API in unit tests | Patch the SDK call | Mock the transport | Transport mock when testing your adapter; fake the adapter when testing services above it |
| Contract confidence | Recorded cassettes | Provider sandbox / emulator | Cassettes for shape, sandbox nightly for drift; never live in PR builds |
| Object storage | moto (in-process) | MinIO/LocalStack container | moto for unit/integration speed; a container when testing presigned URLs end-to-end, multipart uploads or SDK behaviour moto doesn't model |
| Celery tasks | Call function directly | `celery_worker` | Direct for logic (most tests); worker for a few serialisation/routing tests |
| Enqueue assertions | Patch `.delay`/`.apply_async` | Run eagerly and assert effects | Patch for caller tests; eager effects only in flow tests |
| Webhook processing | Synchronous in view | Persist + task | Test both halves separately: view (verify, persist, 2xx) and task (process idempotently) |

Mocks validate **your assumptions** about the provider. Decide explicitly which layer catches
the provider changing its behaviour — if nothing does, say so in the test strategy.

## 5. Django implementation patterns

### Outbound adapter: error mapping

```python
# apps/shipping/client.py — adapter under test (httpx)
class CarrierError(Exception): ...
class CarrierRateLimited(CarrierError):
    def __init__(self, retry_after: int): self.retry_after = retry_after
class CarrierUnavailable(CarrierError): ...
```

```python
import httpx, pytest, respx

BASE = "https://api.carrier.example.test/v1"

@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(429, headers={"Retry-After": "30"}), CarrierRateLimited),
        (httpx.Response(503), CarrierUnavailable),
        (httpx.Response(500, text="<html>oops</html>"), CarrierUnavailable),
        (httpx.Response(200, text="not json"), CarrierError),
        (httpx.Response(401), CarrierError),
    ],
)
@respx.mock
def test_create_shipment_error_mapping(response, expected):
    respx.post(f"{BASE}/shipments").mock(return_value=response)
    with pytest.raises(expected):
        CarrierClient(api_key="test").create_shipment(ShipmentInputFactory.build())

@respx.mock
def test_create_shipment_timeout_is_unavailable():
    respx.post(f"{BASE}/shipments").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(CarrierUnavailable):
        CarrierClient(api_key="test").create_shipment(ShipmentInputFactory.build())

@respx.mock
def test_rate_limit_exposes_retry_after():
    respx.post(f"{BASE}/shipments").mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(CarrierRateLimited) as exc:
        CarrierClient(api_key="test").create_shipment(ShipmentInputFactory.build())
    assert exc.value.retry_after == 30
```

Also assert the client configures a **timeout** (a client without one hangs a worker forever):
`assert client.http.timeout.read == 10`.

### Contract and live markers

```toml
[tool.pytest.ini_options]
markers = [
  "contract: talks to recorded cassettes or local emulators",
  "live: talks to real sandbox services; needs credentials",
]
addopts = "-m 'not live'"
```

```python
@pytest.mark.live
def test_carrier_sandbox_accepts_shipment():
    client = CarrierClient(api_key=os.environ["CARRIER_SANDBOX_KEY"])
    ref = client.create_shipment(ShipmentInputFactory.build(country="GB"))
    assert ref.startswith("SHP")
```

Nightly CI runs `pytest -m live` with sandbox secrets; PR CI never has them
([ci-cd-test-automation.md](ci-cd-test-automation.md)).

### Webhooks: signature, replay, duplicates

```python
import hashlib, hmac, json, time
from django.urls import reverse

SECRET = "whsec_test"

def signed(payload: dict, *, ts: int | None = None, secret: str = SECRET):
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"HTTP_X_SIGNATURE": f"t={ts},v1={sig}"}

@pytest.fixture(autouse=True)
def _webhook_secret(settings):
    settings.PAYMENTS_WEBHOOK_SECRET = SECRET

URL = reverse("payments:webhook")

def test_valid_webhook_is_accepted_and_stored(client, db, django_capture_on_commit_callbacks, mocker):
    process = mocker.patch("apps.payments.views.process_payment_event.delay")
    body, headers = signed({"id": "evt_1", "type": "payment.succeeded", "data": {"order": "ORD-1"}})
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(URL, body, content_type="application/json", **headers)
    assert response.status_code == 200
    event = WebhookEvent.objects.get(provider_id="evt_1")
    process.assert_called_once_with(event.pk)

@pytest.mark.parametrize("mutate", ["bad_sig", "missing", "stale"])
def test_rejected_webhooks_store_nothing(client, db, mutate):
    payload = {"id": "evt_2", "type": "payment.succeeded"}
    if mutate == "bad_sig":
        body, headers = signed(payload, secret="wrong")
    elif mutate == "missing":
        body, headers = signed(payload)[0], {}
    else:
        body, headers = signed(payload, ts=int(time.time()) - 3600)   # outside 5-min window
    response = client.post(URL, body, content_type="application/json", **headers)
    assert response.status_code in (400, 401, 403)
    assert not WebhookEvent.objects.exists()

def test_duplicate_delivery_is_processed_once(client, db, django_capture_on_commit_callbacks, mocker):
    process = mocker.patch("apps.payments.views.process_payment_event.delay")
    body, headers = signed({"id": "evt_3", "type": "payment.succeeded"})
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post(URL, body, content_type="application/json", **headers).status_code == 200
        assert client.post(URL, body, content_type="application/json", **headers).status_code == 200
    assert WebhookEvent.objects.filter(provider_id="evt_3").count() == 1
    assert process.call_count == 1
```

Notes:

- Freeze time if the replay window is tight ([mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md)).
- Webhook endpoints are typically `csrf_exempt`; the Django test client does not enforce CSRF
  by default, so add one test with `Client(enforce_csrf_checks=True)` proving the endpoint
  still works — and that the exemption is limited to it ([security-testing.md](security-testing.md)).
- Duplicate detection should rest on a **DB unique constraint** (`provider_id`), not a
  check-then-insert; test the constraint path too.
- Out-of-order events: deliver `payment.refunded` before `payment.succeeded` and assert the
  final state is correct (or the early event is parked for retry).
- Signature verification itself is a security control — see
  [django-security-engineering: ssrf-prevention.md](../../django-security-engineering/references/ssrf-prevention.md)
  for webhook feature risks and [django-security-engineering: secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md)
  for constant-time comparison.

### Email integration

```python
def test_order_confirmation_email(db, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        order = place_order(customer=UserFactory(email="ada@example.test"), lines=[...])
    msg = mailoutbox[0]
    assert msg.to == ["ada@example.test"]
    assert order.reference in msg.subject
    html, mime = msg.alternatives[0]
    assert mime == "text/html" and order.reference in html
    assert msg.attachments[0][0] == f"{order.reference}.pdf"
    assert msg.extra_headers.get("Reply-To") == "support@example.test"

def test_no_email_when_order_rolls_back(db, mailoutbox, django_capture_on_commit_callbacks, fake_gateway):
    fake_gateway.fail_with = PaymentDeclined()
    with django_capture_on_commit_callbacks(execute=True) as callbacks, pytest.raises(PaymentDeclined):
        place_order(customer=UserFactory(), lines=[...])
    assert callbacks == [] and mailoutbox == []
```

Template rendering errors (missing context variables) surface only if templates are rendered
in tests — rendering through the real send path covers them. Real SMTP/ESP delivery
(bounce handling, DKIM) belongs to the live layer or the provider's tooling.

### Cloud storage integration

```python
def test_presigned_download_url_points_at_private_object(db, s3, settings):
    settings.AWS_STORAGE_BUCKET_NAME = "invoices"       # django-storages settings, if used
    invoice = InvoiceFactory()
    archive_invoice(invoice=invoice)
    url = invoice_download_url(invoice=invoice, expires=300)
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=300" in url
```

With moto, presigned URLs are generated correctly but fetching them over HTTP requires moto's
server mode; to prove the URL is actually downloadable (and expires), use a MinIO/LocalStack
container in a contract-marked test. Storage access control (private ACLs, path traversal in
keys) is covered in [security-testing.md](security-testing.md).

### Celery: four kinds of tests

```python
# apps/billing/tasks.py
@shared_task(bind=True, autoretry_for=(CarrierUnavailable,), retry_backoff=True, max_retries=5)
def sync_shipment(self, shipment_id: int) -> None:
    shipment = Shipment.objects.get(pk=shipment_id)
    if shipment.carrier_ref:           # idempotency guard
        return
    ref = get_carrier().create_shipment(shipment.as_input())
    Shipment.objects.filter(pk=shipment_id, carrier_ref="").update(carrier_ref=ref)
```

1. **Task logic** — call it directly:

```python
def test_sync_shipment_stores_ref(db, fake_carrier):
    shipment = ShipmentFactory()
    sync_shipment(shipment.pk)                      # __call__ runs the body in-process
    shipment.refresh_from_db()
    assert shipment.carrier_ref == fake_carrier.refs[0]
```

2. **Idempotency** — run twice, one effect:

```python
def test_sync_shipment_is_idempotent(db, fake_carrier):
    shipment = ShipmentFactory()
    sync_shipment(shipment.pk)
    sync_shipment(shipment.pk)
    assert len(fake_carrier.refs) == 1
```

3. **Retry policy** — transient errors retry, permanent errors don't:

```python
def test_transient_error_triggers_retry(db, fake_carrier, mocker):
    fake_carrier.fail_with = CarrierUnavailable()
    retry = mocker.patch.object(sync_shipment, "retry", side_effect=Retry())
    with pytest.raises(Retry):
        sync_shipment.apply(args=[ShipmentFactory().pk], throw=True)   # VERIFY apply/throw semantics
    retry.assert_called_once()

def test_permanent_error_does_not_retry(db, fake_carrier):
    fake_carrier.fail_with = CarrierError("invalid address")
    result = sync_shipment.apply(args=[ShipmentFactory().pk])
    assert result.failed() and isinstance(result.result, CarrierError)
```

Prefer asserting on the **policy** (`autoretry_for`, `max_retries`, backoff) through behaviour
like this over asserting configuration attributes. Exhaustion: set `max_retries=0` via
`sync_shipment.max_retries` patch and assert `MaxRetriesExceededError` or the final failure
handling (dead-letter row, alert).

4. **Enqueue wiring** — the caller enqueues after commit, with serialisable arguments:

```python
def test_create_shipment_enqueues_sync_after_commit(db, django_capture_on_commit_callbacks, mocker):
    delay = mocker.patch("apps.billing.services.sync_shipment.delay")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        shipment = create_shipment(order=OrderFactory(paid=True))
    delay.assert_not_called()                 # nothing before commit
    for cb in callbacks:
        cb()
    delay.assert_called_once_with(shipment.pk)   # an int, not a model instance
```

A small number of tests may run a real worker to catch serialisation and routing problems:

```python
# conftest.py
pytest_plugins = ("celery.contrib.pytest",)

@pytest.fixture(scope="session")
def celery_config():
    return {"broker_url": "memory://", "result_backend": "cache+memory://"}

@pytest.mark.django_db(transaction=True)      # worker thread uses its own DB connection
def test_sync_shipment_through_worker(celery_app, celery_worker, fake_carrier):
    shipment = ShipmentFactory()
    sync_shipment.delay(shipment.pk).get(timeout=10)
    shipment.refresh_from_db()
    assert shipment.carrier_ref
```

The worker runs in another thread with another connection, so rows must be **committed** —
hence `transaction=True` ([database-and-transaction-testing.md](database-and-transaction-testing.md)).
The `celery_app` fixture is a test app; tasks defined with `@shared_task` bind to it, tasks
bound to your project app may need `celery_worker` configured against it (VERIFY for your
project's app setup).

### Event-driven workflow: outbox relay

```python
def test_outbox_relay_publishes_once_and_marks_sent(db, fake_broker):
    with django_capture_on_commit_callbacks(execute=True):
        place_order(customer=UserFactory(), lines=[...])
    assert OutboxMessage.objects.filter(sent_at__isnull=True).count() == 1

    relay_outbox()          # task / command
    relay_outbox()          # re-run is harmless

    assert [m.topic for m in fake_broker.published] == ["orders.placed"]
    assert not OutboxMessage.objects.filter(sent_at__isnull=True).exists()

def test_outbox_relay_retries_after_broker_failure(db, fake_broker):
    OutboxMessageFactory()
    fake_broker.fail_next = True
    with pytest.raises(BrokerUnavailable):
        relay_outbox()
    assert OutboxMessage.objects.filter(sent_at__isnull=True).count() == 1   # not lost
    relay_outbox()
    assert len(fake_broker.published) == 1
```

Consumer tests feed duplicate and out-of-order messages to the handler. Delivery guarantees
and outbox design: [django-architecture: event-driven-architecture.md](../../django-architecture/references/event-driven-architecture.md).

### Django's tasks framework (Django ≥ 6.0)

Django 6.0 added a built-in tasks API (`django.tasks`) with backends selected by the `TASKS`
setting; it ships an immediate backend that runs tasks synchronously and a dummy backend that
records enqueued tasks without running them (`ImmediateBackend`, `DummyBackend` — VERIFY names
and behaviour against the 6.0 docs). On the 5.2 baseline this is not available; the
[django-tasks] backport package may provide it as **[third-party]** (VERIFY). The same four
kinds of tests apply.

### Management commands

```python
from io import StringIO
from django.core.management import call_command, CommandError

def test_expire_orders_dry_run_changes_nothing(db):
    old = OrderFactory(created_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC))
    out = StringIO()
    call_command("expire_orders", "--dry-run", "--older-than=30", stdout=out)
    assert "would expire 1 order" in out.getvalue()
    old.refresh_from_db()
    assert old.status == "pending"

def test_expire_orders_rejects_bad_argument(db):
    with pytest.raises(CommandError, match="older-than"):
        call_command("expire_orders", "--older-than=-1")
```

`call_command` raises `CommandError` instead of exiting; pass `stdout=`/`stderr=` to capture
output (commands must write via `self.stdout`, not `print`). Test the underlying service
function for logic; test the command for argument parsing, output and exit behaviour.

## 6. Production use cases

- **Payments provider:** adapter error-mapping matrix; webhook signature/replay/duplicate
  suite; nightly sandbox smoke; fake gateway everywhere else.
- **Shipping integration:** Celery task with retries and idempotency guard; worker-level test
  for argument serialisation; outbox relay for order events.
- **Document archive:** moto-backed unit tests, MinIO container contract test for presigned
  downloads and expiry.
- **Transactional email:** locmem assertions on recipients, subject, HTML alternative and
  attachments; rollback test proving no email on failure.
- **Ops commands:** `call_command` tests for dry-run safety and argument validation before
  running against production.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Transport mocks for adapters | Fast, deterministic, cover every error code | Encode assumptions; silent drift |
| Contract / emulator tests | Catch shape drift and SDK misuse | Slower, need containers or cassettes maintenance |
| Live smoke tests | Only real proof of credentials/network | Flaky, quota-limited, need secrets — keep few and out of PRs |
| Calling task bodies directly | Simple, fast | Misses serialisation, routing, acks |
| `task_always_eager` | Easy end-to-end flows | Hides serialisation and retry semantics; discouraged by Celery |
| `celery_worker` fixture | Realistic execution | Needs transactional DB, slower, thread-related flakiness |

## 8. Security and performance considerations

- Webhook tests must include **negative signature cases** — a verification bug that accepts
  everything passes every happy-path test.
- Never store real provider keys in test settings or cassettes; live tests read sandbox keys
  from CI secrets scoped to the nightly job.
- Assert that tasks receive **IDs, not model instances or PII** — task arguments end up in the
  broker and logs.
- Assert outbound clients set timeouts; missing timeouts are both an availability and a cost risk.
- Keep `transaction=True` worker tests few; each one flushes the DB
  ([performance-and-reliability-testing.md](performance-and-reliability-testing.md)).
- Containers (MinIO, LocalStack, broker) belong in marked suites so the default run stays fast.

## 9. Common mistakes and anti-patterns

- Only happy-path mocks for an external API — the first 429 in production is untested.
- `CELERY_TASK_ALWAYS_EAGER = True` in global test settings — every test silently bypasses
  serialisation and on-commit semantics.
- Using `transaction=True` to make `on_commit` fire instead of capturing callbacks.
- Webhook tests posting a dict (re-encoded by the client) so the signature test cannot be
  meaningful.
- Idempotency "tested" by calling once.
- Asserting `retry` configuration attributes rather than behaviour.
- Patching `.delay` but never asserting it was deferred to commit — enqueue-before-commit races
  ship to production.
- Live tests in the PR pipeline — flaky builds and leaked quotas.
- Treating cassette tests as proof the provider still works.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Task test can't see rows created in the test | Worker thread uses another connection; data uncommitted | `django_db(transaction=True)` for worker tests; call the body directly otherwise |
| `on_commit` callback never runs | Inside `TestCase`/`django_db` transaction | `django_capture_on_commit_callbacks(execute=True)` |
| Webhook signature always invalid in tests | Body re-encoded; different separators; wrong header name (`HTTP_` prefix) | Send bytes with `content_type`; pass headers as `HTTP_X_...` or `headers={...}` (Django ≥ 4.2) |
| Retry test hangs | Real backoff `countdown` or `time.sleep` | Patch `retry`, or call with `throw=True` and patched sleep |
| `kombu.exceptions.EncodeError` only in production | Eager/direct tests skipped serialisation | Add a worker-level test or assert args are JSON-serialisable |
| Live test fails intermittently | Sandbox rate limits / state | Unique references per run, cleanup, retry only in live suite |
| moto test reaches AWS | Client created before `mock_aws` started; real creds in env | Create clients inside the mock; set fake env credentials |

Validation: for each integration, map the scenario checklist (section 2) to test names and
mark gaps in the PR description.

## 11. Related references and skills

- [mocking-and-dependency-isolation.md](mocking-and-dependency-isolation.md) — mechanics of every double used here.
- [database-and-transaction-testing.md](database-and-transaction-testing.md) — `on_commit`, transactional tests.
- [async-testing.md](async-testing.md) — async HTTP clients and async tasks.
- [security-testing.md](security-testing.md) — CSRF exemptions, upload and storage access tests.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — running contract/live suites on schedules.
- [service-and-architecture-testing.md](service-and-architecture-testing.md) — domain events in-process.
- [django-integrations](../../django-integrations/SKILL.md) — client, webhook and Celery design.
- [django-architecture: event-driven-architecture.md](../../django-architecture/references/event-driven-architecture.md) — outbox and delivery guarantees.
- [django-orm: transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md) — idempotency via constraints and locking.
- [django-core: signals-management-commands.md](../../django-core/references/signals-management-commands.md) — command mechanics.

## 12. Official documentation and external references

- Celery testing: <https://docs.celeryq.dev/en/stable/userguide/testing.html> · tasks & retries: <https://docs.celeryq.dev/en/stable/userguide/tasks.html#retrying>
- Django testing tools (`captureOnCommitCallbacks`, email): <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Django `call_command`: <https://docs.djangoproject.com/en/5.2/ref/django-admin/#running-management-commands-from-your-code> · testing commands: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/#topics-testing-management-commands>
- Django email: <https://docs.djangoproject.com/en/5.2/topics/email/>
- Django 6.0 tasks framework: <https://docs.djangoproject.com/en/6.0/topics/tasks/> (VERIFY URL)
- pytest-django helpers: <https://pytest-django.readthedocs.io/en/latest/helpers.html>
- respx: <https://lundberg.github.io/respx/> · responses: <https://github.com/getsentry/responses>
- moto: <https://docs.getmoto.org/en/latest/> · LocalStack: <https://docs.localstack.cloud/> · MinIO: <https://min.io/docs/minio/linux/index.html>
- Pact (consumer-driven contracts): <https://docs.pact.io/>
