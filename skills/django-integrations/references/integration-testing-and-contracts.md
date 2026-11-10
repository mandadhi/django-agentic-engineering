# Integration Testing and Contracts

**Scope:** Owns how to test Django integrations with external systems: choosing the seam (port fake, HTTP transport mock, real sandbox), HTTPX `MockTransport`, respx and responses, recorded-fixture schema validation, consumer-driven contracts with Pact, webhook and authentication tests, timeout/retry/failure simulation, broker and cloud SDK test doubles, and sandbox environments. Test framework mechanics, fixtures, factories, test-database isolation and CI layout belong to [django-testing](../../django-testing/SKILL.md).

## 1. Purpose and scope

Integration code fails at the edges: a field renamed by the provider, a 429 nobody handled, a retry that duplicated a charge, a webhook signature computed over re-serialised JSON. Unit tests with `Mock()` everywhere prove none of that. This reference tells an agent *which* double to use at *which* seam, how to prove failure behaviour deterministically, and how to detect contract drift before production does.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x, pytest ≥ 8 with pytest-django `[third-party: pytest-django]`. Labels follow django-testing conventions: **[Django]**, **[pytest]**, **[Python]**, **[third-party: package]**.

Covered: test-double strategy per layer; HTTP mocking; contract validation; Pact; webhook, auth, timeout and retry tests; broker and cloud SDK tests; sandbox usage. Not covered: test runner configuration, DB isolation semantics, `TestCase` vs `TransactionTestCase` choice (see [database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md)), general mocking mechanics ([mocking-and-dependency-isolation](../../django-testing/references/mocking-and-dependency-isolation.md)).

## 2. Core concepts

| Seam | Double | Proves | Does not prove |
|---|---|---|---|
| Domain → port (`PaymentGateway` Protocol) | In-memory **fake** implementing the port | Domain/service logic, state machines, idempotency | Wire format, HTTP handling |
| Adapter → HTTP transport | `httpx.MockTransport`, respx, responses | Request shape, headers, auth, status/timeout mapping, retries, parsing | Provider actually accepts the request |
| Adapter → recorded real responses | Fixtures validated against provider schema | Parsing against realistic payloads; drift detection when re-recorded | Live behaviour |
| Consumer ↔ provider | Pact contract | Both sides agree on interactions you use | Business semantics beyond the examples |
| Adapter → live sandbox | Provider test mode | End-to-end auth, real validation, real webhooks | Production limits, data, performance |
| Adapter → local emulator | Azurite, LocalStack `[third-party]`, moto `[third-party: moto]`, testcontainers `[third-party: testcontainers]` brokers | SDK usage, broker semantics (ack, redelivery) | Cloud IAM, quotas, managed-service guarantees |

Principles specific to integrations:

- **Mock what you don't own at the outermost seam you control.** Mock the transport, not `AcmePayGateway._post`.
- **Never let tests reach the network by accident.** Make unmocked requests fail loudly.
- **Every failure class is a test case**: connect error, read timeout (ambiguous), 429 with `Retry-After`, 5xx, 4xx, malformed body, contract violation, duplicate delivery, out-of-order delivery.
- **Fakes must be verified.** A fake that diverges from the real adapter gives false confidence; run a shared contract test suite against both the fake and (in a slower job) the real adapter/sandbox.
- **A mock never proves a real integration works** — keep a small sandbox/contract layer.

## 3. Internal mechanics

### HTTPX transports

`httpx.Client(transport=...)` delegates sending to a transport. `httpx.MockTransport(handler)` calls `handler(request) -> httpx.Response` instead of opening sockets; raising an `httpx` exception in the handler (e.g. `httpx.ReadTimeout("...", request=request)`) simulates network failure. Because the client's own logic (base URL, auth, headers, event hooks, `raise_for_status`) still runs, this is the highest-fidelity in-process double. `httpx.AsyncClient` accepts the same `MockTransport` with an async or sync handler.

This requires the adapter to accept an injectable client/transport — design `build_client(..., transport=None)` for it.

### respx and responses

- respx `[third-party: respx]` patches HTTPX transports globally (or per-client) and provides a route router (`respx.post(url).mock(return_value=...)`), call recording and side-effect sequences. Version support for httpx should be checked against the lock file `VERIFY`.
- responses `[third-party: responses]` patches `requests` adapters (`HTTPAdapter.send`), with `responses.add(...)`, `assert_all_requests_are_fired`, and matchers for body/headers.
- Both fail unmocked requests by default (respx `assert_all_mocked=True`; responses raises `ConnectionError`) — keep it that way.

### Pact

Consumer tests run against a local mock provider that records interactions into a pact file (JSON). The provider side replays those interactions against the real provider service (with provider-state setup hooks) and reports verification to a Pact Broker / PactFlow. `pact-python` `[third-party: pact-python]` v2 wrapped the Ruby standalone; v3 uses the Rust core via FFI and changes the API (`Pact(...).upon_receiving(...).with_request(...)`) — check the installed major version before copying examples `VERIFY`.

Pact only works when you can get the *provider team* to verify — it suits internal services and partner APIs you co-own, not public SaaS APIs. For third-party APIs, use schema validation of recorded fixtures plus sandbox tests.

### Celery in tests

`CELERY_TASK_ALWAYS_EAGER=True` runs tasks inline, but bypasses serialisation, routing, acks and `on_commit` timing differences; `self.retry()` in eager mode re-raises/executes synchronously. Prefer calling the task function directly and asserting dispatch separately. See [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md).

### `on_commit` in tests

Inside Django `TestCase`, transactions never commit so `on_commit` callbacks don't run; use `self.captureOnCommitCallbacks(execute=True)` **[Django]** or pytest-django's `django_capture_on_commit_callbacks` fixture.

## 4. Engineering decisions and trade-offs

| Question | Choose | Because |
|---|---|---|
| Testing a service that uses a gateway | Fake implementing the port | Fast, readable, no HTTP noise; tests business behaviour |
| Testing the adapter (`client.py`/`gateway.py`) | `httpx.MockTransport` (or respx if the client can't be injected) | Exercises real request building and error mapping |
| Adapter uses `requests` | responses | Native patching of `requests` |
| Provider SDK (boto3, azure-*) | SDK-native stubbing (botocore `Stubber`), emulator (moto/Azurite), or a fake behind your port | SDK internals are not yours to mock with `patch` |
| Internal service owned by another team | Pact consumer + provider verification | Detects breaking changes before deploy on both sides |
| Public third-party API | Recorded fixtures validated against OpenAPI/JSON Schema + nightly sandbox smoke test | Provider won't run your pacts |
| Broker semantics (ack, redelivery, DLQ) | Real broker in testcontainers | In-memory fakes don't reproduce redelivery and visibility semantics |
| Webhooks | Sign payloads in the test with the same algorithm and secret; POST raw bytes | Proves verification uses the raw body |
| Live sandbox in CI | Separate, non-blocking (or nightly) job; never in the PR critical path | Sandboxes are slow, rate-limited and flaky |

Cassette recording (VCR.py `[third-party: vcrpy]`, pytest-recording `[third-party: pytest-recording]`) is convenient but cassettes rot and can capture secrets/PII; if used, filter headers, scrub bodies, and re-record on a schedule with schema validation.

## 5. Django implementation patterns

### Make adapters injectable

```python
# apps/integrations/http.py (excerpt)
def build_client(name: str, *, base_url: str, timeout: httpx.Timeout,
                 auth=None, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout, auth=auth, transport=transport,
                        event_hooks={"request": [_log_request], "response": [_log_response]})

# apps/payments/integrations/acmepay/gateway.py
class AcmePayGateway:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()
```

### Fake behind the port

```python
# tests/fakes/payments.py
@dataclass
class FakePaymentGateway:
    """Implements PaymentGateway. Honours idempotency keys like the real provider."""
    results: dict[str, GatewayResult] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    fail_next: Exception | None = None

    def create_payment(self, *, money, method_token, reference, idempotency_key, capture):
        self.calls.append(("create", {"key": idempotency_key, "amount": money}))
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if idempotency_key not in self.results:
            self.results[idempotency_key] = GatewayResult(f"pay_{len(self.results)}", "captured")
        return self.results[idempotency_key]

    def find_by_reference(self, reference):
        return next((r for r in self.results.values() if r.provider_payment_id == reference), None)
```

```python
def test_timeout_leaves_payment_unknown_and_schedules_resolution(order, django_capture_on_commit_callbacks):
    gateway = FakePaymentGateway(fail_next=AmbiguousOutcomeError(provider="acmepay", operation="create"))
    with django_capture_on_commit_callbacks() as callbacks:
        payment = start_checkout(order=order, method_token="tok_test", gateway=gateway)
    payment.refresh_from_db()
    assert payment.status == Payment.Status.UNKNOWN
    assert len(callbacks) == 1                          # resolver scheduled, not executed
```

Run the same behavioural suite against the fake and the real adapter (pointed at `MockTransport` or the sandbox) to keep the fake honest:

```python
@pytest.fixture(params=["fake", "adapter"])
def gateway(request, acmepay_transport):
    if request.param == "fake":
        return FakePaymentGateway()
    return AcmePayGateway(client=httpx.Client(base_url="https://api.acmepay.test",
                                              transport=acmepay_transport))
```

### Adapter tests with `httpx.MockTransport`

```python
# tests/payments/test_acmepay_gateway.py
import json
import httpx
import pytest

from apps.integrations.errors import AmbiguousOutcomeError, RateLimitedError, TransientIntegrationError


def make_gateway(handler) -> AcmePayGateway:
    client = httpx.Client(base_url="https://api.acmepay.test", transport=httpx.MockTransport(handler),
                          timeout=httpx.Timeout(1.0))
    return AcmePayGateway(client=client)


def test_create_payment_sends_minor_units_and_idempotency_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "pay_1", "status": "succeeded"})

    result = make_gateway(handler).create_payment(
        money=Money(Decimal("10.50"), "USD"), method_token="tok", reference="r1",
        idempotency_key="payment:r1:create", capture=True)

    assert result.status == "captured"
    body = json.loads(seen[0].content)
    assert body["amount"] == 1050 and body["currency"] == "usd"
    assert seen[0].headers["Idempotency-Key"] == "payment:r1:create"


@pytest.mark.parametrize("exc_type, expected", [
    (httpx.ConnectError, TransientIntegrationError),
    (httpx.ReadTimeout, AmbiguousOutcomeError),      # request may have been processed
])
def test_network_failures_are_classified(exc_type, expected):
    def handler(request):
        raise exc_type("simulated", request=request)
    with pytest.raises(expected):
        make_gateway(handler).create_payment(money=Money(Decimal("1.00"), "USD"), method_token="t",
                                             reference="r", idempotency_key="k", capture=True)


def test_429_exposes_retry_after():
    gateway = make_gateway(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(RateLimitedError) as info:
        gateway.create_payment(money=Money(Decimal("1.00"), "USD"), method_token="t",
                               reference="r", idempotency_key="k", capture=True)
    assert info.value.retry_after == 7
```

### respx for module-level clients

```python
import respx  # [third-party: respx]

@respx.mock(base_url="https://api.partnercrm.test", assert_all_called=True)
def test_sync_pages_until_cursor_exhausted(respx_mock):
    route = respx_mock.get("/v2/contacts")
    route.side_effect = [
        httpx.Response(200, json={"items": [{"id": "c1"}], "next": "abc"}),
        httpx.Response(200, json={"items": [{"id": "c2"}], "next": None}),
    ]
    assert [c.id for c in fetch_all_contacts()] == ["c1", "c2"]
    assert route.calls[1].request.url.params["cursor"] == "abc"
```

### responses for `requests`-based clients

```python
import responses  # [third-party: responses]
from responses import matchers

@responses.activate(assert_all_requests_are_fired=True)
def test_legacy_rates_client_sends_api_key():
    responses.get("https://rates.example.test/latest",
                  match=[matchers.header_matcher({"X-API-Key": "test-key"})],
                  json={"rates": {"EUR": "0.92"}})
    assert latest_rates()["EUR"] == Decimal("0.92")
```

### Retry and timeout behaviour without sleeping

Inject the sleep/clock so backoff tests run instantly and assert the schedule:

```python
def test_retry_call_retries_transient_then_succeeds():
    sleeps: list[float] = []
    attempts = iter([TransientIntegrationError(provider="x", operation="op"), "ok"])

    def op():
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    assert retry_call(op, max_attempts=3, base_delay=0.5, sleep=sleeps.append,
                      rng=lambda a, b: b) == "ok"
    assert sleeps == [0.5]


def test_retry_call_never_retries_ambiguous_outcome():
    calls = 0
    def op():
        nonlocal calls; calls += 1
        raise AmbiguousOutcomeError(provider="x", operation="op")
    with pytest.raises(AmbiguousOutcomeError):
        retry_call(op, max_attempts=5, sleep=lambda s: None)
    assert calls == 1
```

For Celery retry policies, call the task's `.run()` / function body and assert `self.retry` was invoked with the expected countdown (or `pytest.raises(celery.exceptions.Retry)`), rather than running workers. Freeze time for token-expiry and replay-window tests with time-machine `[third-party: time-machine]` or freezegun `[third-party: freezegun]`.

### Circuit breaker

Use an isolated cache (`LocMemCache` via `override_settings(CACHES=...)`) or a per-test Redis DB; assert open after N failures, short-circuits without calling the transport, half-open probe after the cool-down (advance a fake clock).

### Webhook tests: sign the raw body

```python
def sign(body: bytes, secret: str, ts: int) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


@pytest.mark.django_db
def test_valid_webhook_is_stored_once_and_processed_after_commit(client, settings, django_capture_on_commit_callbacks):
    settings.ACMEPAY_WEBHOOK_SECRET = "whsec_test"
    body = json.dumps({"id": "evt_1", "type": "payment.succeeded", "data": {}},
                      separators=(",", ":")).encode()          # exact bytes we sign
    ts = int(time.time())
    headers = {"HTTP_ACME_SIGNATURE": sign(body, "whsec_test", ts), "HTTP_ACME_TIMESTAMP": str(ts)}

    with django_capture_on_commit_callbacks() as callbacks:
        r1 = client.post("/webhooks/acmepay/", data=body, content_type="application/json", **headers)
        r2 = client.post("/webhooks/acmepay/", data=body, content_type="application/json", **headers)

    assert r1.status_code == r2.status_code == 200
    assert WebhookEvent.objects.filter(provider="acmepay", event_id="evt_1").count() == 1
    assert len(callbacks) == 1                                    # duplicate not re-enqueued


@pytest.mark.parametrize("mutate", [
    lambda b, h: (b + b" ", h),                                   # body changed after signing
    lambda b, h: (b, {**h, "HTTP_ACME_TIMESTAMP": str(int(time.time()) - 3600)}),   # replay
    lambda b, h: (b, {k: v for k, v in h.items() if k != "HTTP_ACME_SIGNATURE"}),   # missing
])
def test_invalid_webhooks_rejected_and_not_stored(client, mutate, signed_webhook):
    body, headers = mutate(*signed_webhook)
    response = client.post("/webhooks/acmepay/", data=body, content_type="application/json", **headers)
    assert response.status_code == 400
    assert not WebhookEvent.objects.exists()
```

Also test: unknown event type → 2xx and `IGNORED`; handler failure → retried then `FAILED`; out-of-order events converge to the correct final state; payload re-ordered keys with a *different* byte representation fail verification (proves you sign raw bytes, not re-serialised JSON).

### Authentication tests

- OAuth client credentials: the token endpoint is mocked; assert the token is cached and reused until shortly before expiry, refreshed after (frozen clock), and that a 401 from the API triggers exactly one refresh-and-retry, not a loop.
- JWT validation (inbound): generate a test RSA key pair, serve a JWKS from `MockTransport`, and test expired, wrong `aud`, wrong `iss`, `alg=none`, unknown `kid` (triggers one JWKS refresh), and tampered signature. PyJWT `[third-party: PyJWT]` and cryptography `[third-party: cryptography]` provide the primitives.
- Secrets never appear in logs: capture logs with `caplog` and assert the token value is absent.

See [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) for the behaviour under test.

### Recorded fixtures and schema validation

```text
tests/contracts/acmepay/
  openapi.yaml                    # provider spec, pinned version
  fixtures/payment_succeeded.json # recorded from sandbox, scrubbed
```

```python
import jsonschema  # [third-party: jsonschema]

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")))
def test_recorded_fixtures_match_provider_schema(path, acmepay_schemas):
    payload = json.loads(path.read_text())
    jsonschema.validate(payload, acmepay_schemas[payload["object"]])
```

Combine with the adapter's own response models (pydantic `[third-party: pydantic]` or dataclasses): a test that parses every fixture through the adapter catches drift between "what the provider sends" and "what we expect". When the provider publishes a new API version, re-record fixtures from the sandbox and let these tests show what broke.

### Consumer-driven contracts with Pact (pact-python v2 style shown)

```python
# Consumer side (our Django app calls inventory-service). pact-python v2 API — v3 differs. VERIFY
from pact import Consumer, Provider  # [third-party: pact-python]

pact = Consumer("shop-web").has_pact_with(Provider("inventory-service"), port=1234)

def test_get_stock_contract():
    (pact.given("SKU ABC exists with 5 units")
         .upon_receiving("a stock lookup")
         .with_request("GET", "/v1/stock/ABC")
         .will_respond_with(200, body={"sku": "ABC", "available": 5}))
    with pact:
        client = InventoryClient(base_url=pact.uri)
        assert client.available("ABC") == 5
```

- Publish pacts from CI with the consumer version (git SHA) and branch.
- The provider pipeline verifies pacts and publishes results; use `can-i-deploy` before deploying either side.
- Use matchers (type/regex) rather than exact values so contracts specify shape, not test data.
- Keep contracts to interactions the consumer actually uses; Pact is not a full API test of the provider.

### Broker tests with real brokers

```python
from testcontainers.rabbitmq import RabbitMqContainer  # [third-party: testcontainers]

@pytest.fixture(scope="session")
def rabbitmq_url():
    with RabbitMqContainer("rabbitmq:3.13-management") as rabbit:
        yield f"amqp://guest:guest@{rabbit.get_container_host_ip()}:{rabbit.get_exposed_port(5672)}/"


@pytest.mark.django_db(transaction=True)
def test_consumer_redelivery_is_deduplicated(rabbitmq_url):
    publish(rabbitmq_url, "orders", message_id="m1", body={"order": 1})
    publish(rabbitmq_url, "orders", message_id="m1", body={"order": 1})   # duplicate delivery
    consume_until_empty(rabbitmq_url, "orders", handler=handle_order_event)
    assert InboxMessage.objects.filter(source="orders", message_id="m1").count() == 1
    assert Shipment.objects.filter(order_id=1).count() == 1
```

Also test poison messages land in the DLQ after N attempts and that a crash between processing and ack causes redelivery (kill the consumer before ack). Kafka and Redis have equivalent testcontainers modules; Azure Service Bus has an emulator whose feature coverage should be checked `VERIFY`. Mark these tests (`@pytest.mark.integration`) and run them in a separate CI job ([django-testing](../../django-testing/SKILL.md) owns CI layout).

### Cloud SDK stubbing

```python
from botocore.stub import ANY, Stubber
import boto3

def test_upload_sets_checksum_and_sse():
    s3 = boto3.client("s3", region_name="eu-west-1")
    with Stubber(s3) as stub:
        stub.add_response("put_object", {"ETag": '"abc"'},
                          expected_params={"Bucket": "docs", "Key": "a/b.pdf", "Body": ANY,
                                           "ServerSideEncryption": "aws:kms",
                                           "ChecksumAlgorithm": "SHA256"})
        DocumentStore(client=s3).put("a/b.pdf", b"%PDF")
```

- botocore `Stubber` validates parameters against the service model — catches typos in parameter names.
- moto `[third-party: moto]` (`@mock_aws` in moto ≥ 5 `VERIFY`) emulates many AWS services in-process; good for S3/SQS flows, weaker on IAM and edge semantics.
- Azure: Azurite emulates Blob/Queue/Table storage locally (connection string `UseDevelopmentStorage=true`); for other Azure SDK clients, inject a fake behind your port or a custom `azure.core` transport. Do not patch SDK internals.
- Never use real cloud credentials in unit tests; set dummy credentials env vars in test settings so a misconfigured test can't reach real accounts.

### Network guard

Block real sockets in the default test run so an unmocked call fails fast — pytest-socket `[third-party: pytest-socket]` (`--disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,localhost`) or respx/responses strict modes. Integration-marked tests opt back in.

### Sandbox environments

- Use provider test modes with dedicated credentials stored in CI secrets; never production keys.
- Run a small smoke suite nightly and before provider API-version upgrades: authenticate, create, read, cancel/refund, receive a webhook (via a tunnel or provider CLI forwarding, provider-specific).
- Sandboxes differ from production (relaxed validation, fewer rate limits, fake bank flows, magic test card numbers). Record those differences in the integration's runbook.
- Clean up created sandbox resources or use unique prefixes; sandboxes accumulate state.

## 6. Production use cases

1. **Provider renames a field.** Nightly sandbox job re-records fixtures; schema test and adapter parse test fail; fixed before customers notice.
2. **Internal service splits an endpoint.** Inventory team's provider verification fails against shop-web's pact; `can-i-deploy` blocks the breaking release.
3. **Duplicate charges investigation.** Regression test added: read timeout on create → assert no second POST without the same idempotency key.
4. **Webhook outage after framework upgrade.** A middleware began parsing and re-encoding bodies; the raw-bytes signature test caught it in CI.
5. **Token refresh storm.** Test with 50 concurrent calls and an expiring token asserts a single refresh (lock/single-flight), preventing IdP rate limiting.
6. **Queue consumer double-processing.** testcontainers test that kills the consumer before ack proves the inbox dedupe works across redelivery.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Port fakes | Fast, readable, test business rules | Drift from reality unless contract-tested against the adapter |
| `httpx.MockTransport` | No dependency; full client pipeline exercised | Needs injectable client; handler code can grow |
| respx / responses | Ergonomic routing, call assertions, works with module-level clients | Global patching; extra dependency; version coupling to httpx/requests |
| Cassettes (VCR) | Real payloads with little effort | Rot, secret/PII leakage, hide intent |
| Schema validation of fixtures | Cheap drift detection | Only as good as the provider's published schema |
| Pact | Prevents breaking changes between owned services | Requires provider participation, broker, CI discipline |
| testcontainers / emulators | Real semantics for brokers and storage | Slower; Docker required in CI; emulator gaps |
| Sandbox tests | Highest fidelity short of prod | Flaky, slow, rate-limited; differs from prod |

## 8. Security and performance considerations

**Security**

- Never commit real credentials, tokens or cassettes containing them; scrub `Authorization`, cookies, API keys and PII from fixtures (secret scanning in CI — [dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md)).
- Sandbox credentials are still credentials: scoped, rotated, stored in CI secrets.
- Security behaviour needs negative tests: invalid signatures, replayed webhooks, expired/forged JWTs, SSRF-blocked URLs, XXE payloads rejected by the XML parser ([integration-security.md](integration-security.md), [security-testing](../../django-testing/references/security-testing.md)).
- Test that logs and error messages don't contain secrets or full payloads.

**Performance**

- Keep transport-mocked tests in the default fast suite; containers and sandboxes in separate marked jobs.
- Session-scope expensive containers; isolate data per test (unique queue names/prefixes) rather than restarting containers.
- Inject sleep/clock to avoid real backoff delays; a test suite with `time.sleep` from retry code is a slow and flaky suite.
- Load and soak testing of integrations belongs to [django-performance](../../django-performance/SKILL.md); stub the provider with a latency-injecting fake to measure your own behaviour under slow dependencies.

## 9. Common mistakes and anti-patterns

- `patch("httpx.Client.post")` or patching private adapter methods — tests pass while real request building is broken.
- Mocking at the service level only, so the adapter's error mapping and parsing are never tested.
- Tests that hit the real network (or sandbox) by default; flaky CI and leaked data.
- Fakes that always succeed and ignore idempotency keys.
- Asserting call counts on mocks instead of observable outcomes (DB state, emitted messages) — except where the call count *is* the behaviour (no duplicate POST).
- Webhook tests that post a Python dict (`client.post(..., data=dict)`) — body bytes differ from what was signed; the test doesn't prove raw-body verification.
- `CELERY_TASK_ALWAYS_EAGER` as the only Celery test strategy; hides serialization errors and `on_commit` timing.
- Real `time.sleep` in retry tests; real wall clock for token expiry.
- Contract tests with exact-value matching that break on any test data change.
- Treating Pact as a substitute for provider-side functional tests.
- Emulator-only tests for cloud IAM/permissions behaviour.
- Fixtures recorded once and never refreshed.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Test passes locally, fails in CI with connection errors | Unmocked request reaching network; CI blocks egress | Enable socket guard locally; mock or mark as integration |
| respx "route not mocked" / "not all routes called" | URL/base URL mismatch, query params, trailing slash | Inspect `respx_mock.calls`; match on path + params explicitly |
| Webhook signature test fails only in CI | Body re-serialised; different JSON key order; timestamp skew with frozen clock | Sign and send the same bytes; freeze clock consistently |
| `on_commit` callback never runs | `TestCase` wraps test in a transaction | `captureOnCommitCallbacks(execute=True)` or transactional test |
| Fake and real adapter disagree | Fake not contract-tested | Run shared behavioural suite against both |
| Pact verification fails on provider | Provider state not set up; contract over-specified | Implement state handlers; use matchers |
| testcontainers tests hang | Docker not available; port mapping; container readiness | Readiness wait strategies; CI service config |
| botocore `Stubber` "Unexpected API call" | Code made an extra call (e.g. `head_object`) | Add expected response or refactor; assert call list |

Validation checklist:

- [ ] Services tested against port fakes; adapters tested through the transport layer.
- [ ] Every failure class covered: connect error, read timeout (ambiguous), 429 + `Retry-After`, 5xx, 4xx, malformed/contract-violating body.
- [ ] Retry tests prove non-idempotent operations aren't blindly retried and idempotency keys are stable across retries.
- [ ] Webhook tests sign raw bytes and cover invalid, stale, duplicate and out-of-order deliveries.
- [ ] Auth tests cover token caching, refresh, 401 handling and JWT negative cases.
- [ ] No real network in the default suite; integration tests marked and run separately.
- [ ] Recorded fixtures scrubbed and schema-validated; refresh process documented.
- [ ] Contract tests (Pact) for internally owned service-to-service APIs, with `can-i-deploy` gating.
- [ ] Broker consumers tested for redelivery and DLQ behaviour against a real broker.
- [ ] Cloud SDK calls tested via Stubber/moto/Azurite or fakes behind ports — no SDK internals patched.

## 11. Related references and skills

- [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md) — client design that makes transports injectable.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — the retry/breaker/idempotency behaviour under test.
- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — verification and inbox logic under test.
- [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) — token and JWT behaviour under test.
- [messaging-and-event-driven-integrations.md](messaging-and-event-driven-integrations.md), [celery-and-background-integrations.md](celery-and-background-integrations.md) — consumer and task semantics.
- [azure-integrations.md](azure-integrations.md), [aws-and-cloud-integrations.md](aws-and-cloud-integrations.md) — SDK usage being stubbed.
- [django-testing](../../django-testing/SKILL.md): [integration-and-background-task-testing](../../django-testing/references/integration-and-background-task-testing.md), [mocking-and-dependency-isolation](../../django-testing/references/mocking-and-dependency-isolation.md), [async-testing](../../django-testing/references/async-testing.md), [database-and-transaction-testing](../../django-testing/references/database-and-transaction-testing.md), [security-testing](../../django-testing/references/security-testing.md).
- [django-architecture: dependency-management](../../django-architecture/references/dependency-management.md) — ports and injected implementations.

## 12. Official documentation and external references

- HTTPX — Transports / `MockTransport`: https://www.python-httpx.org/advanced/transports/
- respx: https://lundberg.github.io/respx/
- responses: https://github.com/getsentry/responses
- Django — Testing tools (`captureOnCommitCallbacks`, `override_settings`): https://docs.djangoproject.com/en/5.2/topics/testing/tools/
- pytest-django: https://pytest-django.readthedocs.io/
- Python — `unittest.mock`: https://docs.python.org/3/library/unittest.mock.html
- Pact documentation: https://docs.pact.io/
- pact-python: https://github.com/pact-foundation/pact-python
- Pact — `can-i-deploy`: https://docs.pact.io/pact_broker/can_i_deploy
- Testcontainers for Python: https://testcontainers-python.readthedocs.io/ `VERIFY` (docs host has moved before)
- botocore Stubber: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/stubber.html
- moto: https://docs.getmoto.org/
- Azurite emulator: https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite
- Celery — Testing: https://docs.celeryq.dev/en/stable/userguide/testing.html
- jsonschema: https://python-jsonschema.readthedocs.io/
- OpenAPI Specification: https://spec.openapis.org/oas/latest.html
- pytest-socket: https://github.com/miketheman/pytest-socket
