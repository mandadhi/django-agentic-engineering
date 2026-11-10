# SOAP and Legacy Integrations

**Scope:** Integrating Django with SOAP services and other legacy enterprise systems: SOAP and WSDL
fundamentals, XML namespaces and schema validation, the zeep client, WS-Security concepts, secure XML
parsing, REST-to-SOAP adapters and incremental modernisation. Token/certificate mechanics in general are in
[authentication-and-identity-integrations.md](authentication-and-identity-integrations.md); file-drop and SFTP
legacy exchanges are in [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md).

## 1. Purpose and scope

Equip an agent to consume (and occasionally expose) SOAP services reliably and safely, isolate their XML
and vendor quirks behind a domain gateway, and plan modernisation without a big-bang rewrite.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Libraries:
zeep `[third-party: zeep]` (SOAP client), lxml `[third-party: lxml]` (XML/XSD), defusedxml
`[third-party: defusedxml]` (hardened parsing), xmlsec `[third-party: xmlsec]` (XML signatures, optional).
Exposing SOAP from Django (spyne `[third-party: spyne]`) is rare; prefer a REST facade unless a partner
mandates it.

**Standards vs vendors.** SOAP 1.1 (W3C Note), SOAP 1.2 (W3C Recommendation), WSDL 1.1/2.0, XML Schema and
OASIS WS-Security are *standards*. Real services frequently deviate: non-conformant WSDLs, mixed
namespaces, faults returned with HTTP 200, proprietary auth headers, partial WS-Security profiles. This file
marks such behaviour "(vendor-specific)".

## 2. Core concepts

| Concept | Meaning |
|---|---|
| SOAP envelope | `Envelope` → optional `Header` (auth, routing, correlation) → `Body` (payload or `Fault`) |
| SOAP 1.1 vs 1.2 | 1.1: namespace `http://schemas.xmlsoap.org/soap/envelope/`, `text/xml`, `SOAPAction` header. 1.2: `http://www.w3.org/2003/05/soap-envelope`, `application/soap+xml; action=...` |
| WSDL | Machine-readable contract: `types` (XSD), `message`, `portType`/`interface` (operations), `binding` (SOAP style/encoding), `service`/`port` (endpoint URL) |
| Style/use | `document/literal` (wrapped) is the interoperable norm; `rpc/encoded` is legacy and poorly supported (zeep has limited support `VERIFY`) |
| XML namespace | URI qualifying element names; prefix is arbitrary — compare by `{uri}local`, never by prefix |
| XSD | Schema for types; enables validation and code generation |
| SOAP Fault | Error element: `faultcode`/`faultstring`/`detail` (1.1) or `Code`/`Reason`/`Detail` (1.2); HTTP 500 per spec, but 200 is common (vendor-specific) |
| WS-Security | OASIS standard for message-level security: `UsernameToken`, `Timestamp`, X.509 `BinarySecurityToken`, XML Signature, XML Encryption |
| MTOM/XOP | Binary attachments outside the XML body (optimised); SwA is the older variant |
| WS-Addressing | Message routing headers (`To`, `Action`, `MessageID`, `RelatesTo`) |

**Transport vs message security.** TLS protects the connection hop; WS-Security protects the message itself
(survives intermediaries, provides non-repudiation with signatures). Most modern deployments use TLS (often
mTLS) plus at most `UsernameToken`; full signing/encryption appears in government, banking and healthcare.

## 3. Internal mechanics

### What zeep does

1. Downloads and parses the WSDL and every imported XSD (network calls at client construction!).
2. Builds Python type factories from the XSD; operations become `client.service.OperationName(...)`.
3. Serialises arguments to XML via the binding, applies plugins (WS-Security, logging), POSTs via a
   `Transport` (requests `Session` under the hood).
4. Parses the response, raises `zeep.exceptions.Fault` for SOAP faults, returns `CompoundValue` objects
   (`zeep.helpers.serialize_object` converts to `dict`/`OrderedDict`).

Implications:
- Construction is expensive (hundreds of ms to seconds for large WSDLs) — construct once per process, and
  **cache WSDL/XSD** (`zeep.cache.SqliteCache` or `InMemoryCache`) or ship a local WSDL copy.
- Fetching WSDL from production at import time couples app startup to partner availability. Vendor a pinned
  copy of the WSDL in the repo and point `wsdl=` to the file; override the endpoint with `create_service`.
- zeep's `Transport(timeout=..., operation_timeout=...)`: `timeout` applies to WSDL/XSD loading,
  `operation_timeout` to service calls `VERIFY` exact semantics for your version.

### XML parsing risks

- **XXE (XML External Entity)**: a `DOCTYPE` declaring an external entity (`SYSTEM "file:///etc/passwd"` or
  an internal URL) makes the parser read files or perform SSRF.
- **Billion laughs / entity expansion**: nested entities expand to gigabytes.
- **External DTD/XSD fetching**: parser retrieves remote resources.
- Python's `xml.etree`/`xml.dom` stdlib parsers are not hardened for untrusted input (Python docs list
  vulnerabilities; expat ≥ 2.4.1 mitigates entity expansion `VERIFY`). lxml resolves entities by default unless
  configured (`resolve_entities=False`, `no_network=True`, which is default `VERIFY`).
- zeep uses lxml with `XMLParser(resolve_entities=False)` and offers `Settings(forbid_dtd=True,
  forbid_entities=True, forbid_external=True)`; defaults `forbid_entities=True`, `forbid_external=True`,
  `forbid_dtd=False` `VERIFY`.

## 4. Engineering decisions and trade-offs

### Client approach

| Option | Use when | Trade-off |
|---|---|---|
| zeep with vendored WSDL | Standard document/literal service, many operations | Dependency; WSDL quirks surface as cryptic errors |
| Hand-built envelope + httpx + lxml | 1–3 operations, broken WSDL, or zeep cannot handle binding | You maintain XML templates and namespaces |
| Vendor SDK (Java/.NET only) | Rarely viable from Python | Sidecar service or iPaaS needed |
| iPaaS / API gateway translation (Azure APIM SOAP-to-REST, Logic Apps, MuleSoft) | Org already runs it; many consumers | Extra hop, cost, another team owns mapping |

### Synchronous vs asynchronous

Legacy ERPs are often slow (seconds) and have maintenance windows. Default to background tasks for writes;
sync calls only for fast lookups with tight timeouts and a cache.

### Schema validation

Validate **outbound** requests against the XSD in tests (catches mapping bugs before the partner does).
Validate **inbound** responses in production only for fields you rely on — strict full-schema validation of
partner responses breaks when the vendor adds elements in violation of its own published schema.

### Modernisation strategy

| Strategy | When |
|---|---|
| Keep SOAP behind a gateway in Django (ACL) | System remains for years; you just need to consume it cleanly |
| REST facade (your service or APIM) over SOAP | Many internal consumers need the legacy data |
| Strangler fig | Legacy is being replaced; route capability by capability to the new system |
| Event-carried state transfer / CDC from legacy DB | Read-heavy needs; legacy cannot handle query load (requires DB access agreement) |
| Batch file exchange | Legacy only supports nightly files (see file transfer reference) |

## 5. Django implementation patterns

### Layout

```text
apps/erp/integrations/legacyerp/
├── wsdl/OrderService.wsdl      # vendored, pinned; XSDs alongside
├── client.py                   # zeep client construction, transport, WS-Security, fault mapping
├── gateway.py                  # implements OrderSystem port; maps domain ↔ zeep objects
└── mappers.py
```

### Client construction

```python
# client.py
from functools import cache
from pathlib import Path

import requests
from django.conf import settings
from zeep import Client, Settings
from zeep.cache import InMemoryCache
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

WSDL = Path(__file__).parent / "wsdl" / "OrderService.wsdl"
SERVICE_NS = "{http://legacyerp.example.com/orders/v3}"


@cache
def get_service():
    session = requests.Session()
    session.verify = settings.LEGACYERP_CA_BUNDLE              # private CA common in enterprise
    if settings.LEGACYERP_CLIENT_CERT:                          # mTLS (cert, key) paths from secret mount
        session.cert = (settings.LEGACYERP_CLIENT_CERT, settings.LEGACYERP_CLIENT_KEY)
    transport = Transport(session=session, timeout=10, operation_timeout=15, cache=InMemoryCache())
    client = Client(
        str(WSDL),
        transport=transport,
        settings=Settings(strict=False, xml_huge_tree=False, forbid_dtd=True,
                          forbid_entities=True, forbid_external=True),
        wsse=UsernameToken(settings.LEGACYERP_USER, settings.LEGACYERP_PASSWORD, use_digest=False),
    )
    # Endpoint from settings, not from the vendored WSDL (differs per environment)
    return client.create_service(f"{SERVICE_NS}OrderServiceSoap12Binding", settings.LEGACYERP_ENDPOINT)
```

`strict=False` tolerates some non-conformant responses (vendor-specific deviations) — keep it off in contract
tests so you notice them.

### Fault and error normalisation

```python
import requests
from zeep.exceptions import Fault, TransportError, ValidationError as ZeepValidationError, XMLSyntaxError

from apps.integrations.errors import (
    AmbiguousOutcomeError, AuthenticationError, ContractViolationError,
    PermanentIntegrationError, TransientIntegrationError,
)

PROVIDER = "legacyerp"
TRANSIENT_FAULTS = {"Server.Busy", "Server.Timeout"}      # vendor-specific codes, from partner docs


def call(operation: str, *, idempotent: bool, **kwargs):
    ctx = {"provider": PROVIDER, "operation": operation}
    try:
        return getattr(get_service(), operation)(**kwargs)
    except Fault as exc:
        code = getattr(exc, "code", None) or ""
        if "Authentication" in code or "Security" in code:
            raise AuthenticationError(code, **ctx) from exc
        if code in TRANSIENT_FAULTS:
            raise TransientIntegrationError(code, **ctx) from exc
        raise PermanentIntegrationError(code, **ctx) from exc       # fault string may contain data — log code only
    except requests.exceptions.ConnectionError as exc:
        raise TransientIntegrationError(str(exc), **ctx) from exc
    except requests.exceptions.Timeout as exc:                     # includes ReadTimeout
        if isinstance(exc, requests.exceptions.ConnectTimeout) or idempotent:
            raise TransientIntegrationError(str(exc), **ctx) from exc
        raise AmbiguousOutcomeError(str(exc), **ctx) from exc
    except TransportError as exc:                                  # non-200 without parsable fault
        if exc.status_code >= 500:
            raise TransientIntegrationError(f"http {exc.status_code}", **ctx) from exc
        raise PermanentIntegrationError(f"http {exc.status_code}", **ctx) from exc
    except (XMLSyntaxError, ZeepValidationError) as exc:
        raise ContractViolationError(str(exc), **ctx) from exc
```

Some services report errors inside a successful body (`<ResultCode>E042</ResultCode>`) — check in the
gateway; HTTP 200 + no Fault is not business success.

### Gateway: domain ↔ XML mapping

```python
# gateway.py
from decimal import Decimal
from zeep.helpers import serialize_object

from apps.orders.domain import ErpOrderRef, OrderSystem
from . import client


class LegacyErpGateway(OrderSystem):
    def submit_order(self, order) -> ErpOrderRef:
        result = client.call(
            "SubmitOrder", idempotent=False,
            ExternalReference=order.number,                 # our id: lets us look up after a timeout
            Lines={"Line": [{"Sku": l.sku, "Qty": l.quantity, "UnitPrice": str(l.unit_price)}
                            for l in order.lines]},
        )
        data = serialize_object(result, dict)
        if data.get("ResultCode") != "OK":
            raise client.PermanentIntegrationError(data.get("ResultCode", "unknown"),
                                                   provider=client.PROVIDER, operation="SubmitOrder")
        return ErpOrderRef(erp_id=data["OrderId"], total=Decimal(data["Total"]))

    def find_by_reference(self, number: str) -> ErpOrderRef | None:
        data = serialize_object(client.call("FindOrder", idempotent=True, ExternalReference=number), dict)
        return ErpOrderRef(erp_id=data["OrderId"], total=Decimal(data["Total"])) if data else None
```

Send **your** reference on create so an ambiguous timeout can be resolved with `find_by_reference` before
retrying ([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)). For complex
XSD types build arguments with `Client.type_factory(namespace)` rather than nested dicts.

### Hand-built envelope with httpx (no zeep)

```python
from defusedxml import ElementTree as SafeET          # hardened parser for responses
from xml.sax.saxutils import escape

NS = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "r": "http://legacyerp.example.com/rates/v1"}

ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:r="http://legacyerp.example.com/rates/v1">
  <s:Body><r:GetRate><r:Currency>{currency}</r:Currency></r:GetRate></s:Body>
</s:Envelope>"""


def get_rate(currency: str) -> str:
    body = ENVELOPE.format(currency=escape(currency))          # escape every interpolated value
    response = rates_client().post("", content=body.encode(), headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": '"http://legacyerp.example.com/rates/v1/GetRate"',   # SOAP 1.1; quoted per spec
    })
    root = SafeET.fromstring(response.content)                 # raises on DTD/entities
    fault = root.find(".//s:Fault", NS)
    if fault is not None:
        raise PermanentIntegrationError(fault.findtext("faultcode", ""), provider="legacyerp-rates",
                                        operation="GetRate")
    return root.findtext(".//r:Rate", namespaces=NS)
```

String templates are acceptable for small, fixed envelopes **only with escaping**; for anything dynamic build
with `lxml.etree` / `ElementMaker` to avoid XML injection.

### Secure parsing of any XML (SOAP, files, webhooks)

```python
from lxml import etree

SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                              dtd_validation=False, huge_tree=False, remove_comments=True)


def parse_and_validate(xml_bytes: bytes, schema: etree.XMLSchema) -> etree._Element:
    if b"<!DOCTYPE" in xml_bytes[:1024].upper():
        raise ContractViolationError("DTD not allowed", provider="legacyerp", operation="parse")
    doc = etree.fromstring(xml_bytes, parser=SAFE_PARSER)
    if not schema.validate(doc):
        raise ContractViolationError(str(schema.error_log.last_error), provider="legacyerp", operation="parse")
    return doc

# Load schemas from vendored files once at import; never fetch xsd:import URLs at runtime.
ORDER_SCHEMA = etree.XMLSchema(etree.parse("apps/erp/integrations/legacyerp/wsdl/orders.xsd", SAFE_PARSER))
```

### WS-Security options in zeep

| Requirement | zeep construct |
|---|---|
| UsernameToken (plain text over TLS) | `UsernameToken(user, pwd)` |
| UsernameToken PasswordDigest + nonce + created | `UsernameToken(user, pwd, use_digest=True)` — digest is legacy SHA-1 based; protects little beyond TLS |
| Timestamp | `zeep.wsse.utils` / combined via `Compose` `VERIFY` exact helper |
| X.509 signature | `zeep.wsse.signature.Signature(key_file, cert_file)` (needs xmlsec) |
| Several | `wsse=[UsernameToken(...), Signature(...)]` (list supported `VERIFY` order semantics) |

Vendor quirks to expect: required `mustUnderstand` flags, specific canonicalisation algorithms, signing only
certain parts (`Body`, `Timestamp`), clock-skew tolerance of a few minutes. Capture a working sample from
the vendor (SoapUI project) and diff your XML against it.

### Exposing a REST facade from Django

Internal consumers call a DRF endpoint; the view calls a service, which calls the gateway; responses are your
JSON contract, never passthrough XML. Cache reads; queue writes. DRF mechanics:
[django-rest-framework](../../django-rest-framework/SKILL.md).

## 6. Production use cases

- **ERP order submission**: Celery task, vendored WSDL, mTLS + UsernameToken, `ExternalReference` for
  reconciliation, ERP maintenance window handled by pausing the queue and letting retries back off.
- **Government tax/e-invoicing service**: signed WS-Security (X.509, timestamps), strict XSD validation of
  outbound documents, archived signed request/response for audit retention.
- **Insurance/banking legacy lookup**: sync policy lookup with 2 s timeout and 15 min cache; REST facade so
  the SPA never sees SOAP.
- **Strangler migration of a legacy CRM**: Django gateway routes `get_customer` to the new CRM for migrated
  regions (feature flag per region) and to SOAP otherwise; dual-write disabled, sync job reconciles.

## 7. Advantages and disadvantages

| Aspect | Advantages | Disadvantages |
|---|---|---|
| SOAP/WSDL | Formal contract, tooling for codegen, message-level security, mature in enterprises | Verbose, slow parsing, interoperability quirks, harder debugging |
| zeep | Rich WSDL support, WS-Security plugins | Large WSDL load time, opaque errors, limited rpc/encoded |
| Hand-built XML | Total control, tiny footprint | Manual namespaces/escaping; drift from WSDL |
| REST facade/strangler | Isolates legacy, enables gradual replacement | Another component to own; mapping duplication risk |

## 8. Security and performance considerations

**Security**
- Never parse partner or user XML with default stdlib parsers; use defusedxml or a locked-down lxml parser;
  set zeep `forbid_dtd=True`. XXE detail: [integration-security.md](integration-security.md),
  [django-security-engineering injection-prevention](../../django-security-engineering/references/injection-prevention.md).
- Do not let zeep fetch WSDL/XSD from URLs supplied at runtime (SSRF); vendor them.
- Escape all interpolated values or build XML with a tree API (XML injection can alter operations).
- Credentials (UsernameToken passwords, private keys) from the secret store; private keys mounted as files with
  restricted permissions, rotation dates tracked.
- SOAP faults and zeep debug logging (`zeep.transports` at DEBUG) dump full envelopes including
  `UsernameToken` passwords — never enable DEBUG XML logging in production; redact if logging envelopes.

**Performance**
- WSDL parsing once per process; cache; vendored files avoid network at startup.
- XML is CPU-heavy: large responses (> 10 MB) should be streamed with `lxml.etree.iterparse` and
  `clear()`ed elements; avoid `serialize_object` on huge graphs.
- Legacy throughput limits are often low (a handful of concurrent sessions) — dedicated Celery queue with low
  concurrency (bulkhead).

## 9. Common mistakes and anti-patterns

- Loading the WSDL from the partner's URL at import time — app fails to boot when the partner is down.
- `xml.etree.ElementTree.fromstring(untrusted)` or lxml with entity resolution on.
- Matching elements by prefix (`ns1:Order`) instead of namespace URI.
- Treating HTTP 200 as success without checking `Fault` or in-body result codes.
- Using the WSDL's hard-coded endpoint (often a test or internal hostname).
- Passing zeep `CompoundValue` objects into models/JSONField or templates.
- Retrying a non-idempotent SOAP create after a timeout without a lookup by external reference.
- Logging full envelopes (credentials, personal data) at INFO.
- Assuming WS-Security conformance — vendors implement profiles partially.

## 10. Troubleshooting and validation

| Symptom | Cause / check |
|---|---|
| `zeep.exceptions.LookupError: No type 'X' in namespace` | Wrong namespace or missing imported XSD; inspect `python -m zeep path/to/wsdl` output |
| `Server was unable to process request` / generic fault | Envelope mismatch: compare with vendor SoapUI sample (namespaces, element order, SOAPAction) |
| 415/400 on every call | SOAP 1.1 vs 1.2 content type/binding mismatch |
| `wsse:InvalidSecurity` / timestamp expired | Clock skew > tolerance; NTP on workers; wrong digest/signature algorithm |
| Slow first request per worker | WSDL parse; enable caching / warm on `worker_process_init` |
| Memory spikes | Huge responses fully materialised — use iterparse/streaming |
| `XMLSyntaxError` on response | Partner returned HTML error page (proxy/gateway) — check status and content type first |

Inspect traffic with zeep's `HistoryPlugin` in tests or a local debugging session
(`history.last_sent["envelope"]`), never in production logs. Validation: XSD-validate generated request
envelopes in unit tests; contract tests against recorded vendor responses including faults; a staging
smoke test against the partner sandbox
([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [integration-architecture-patterns.md](integration-architecture-patterns.md) — anti-corruption layer, strangler, facade.
- [authentication-and-identity-integrations.md](authentication-and-identity-integrations.md) — mTLS, certificates.
- [integration-security.md](integration-security.md) — XXE, XML injection, credential handling.
- [integration-reliability-and-resilience.md](integration-reliability-and-resilience.md) — ambiguous outcomes, bulkheads.
- [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md) — legacy file-based exchanges.
- [data-synchronization-and-reconciliation.md](data-synchronization-and-reconciliation.md) — syncing legacy master data.
- [django-security-engineering injection-prevention](../../django-security-engineering/references/injection-prevention.md).
- [django-architecture design-patterns](../../django-architecture/references/design-patterns.md).

## 12. Official documentation and external references

- SOAP 1.2 Part 1 (W3C Recommendation): <https://www.w3.org/TR/soap12-part1/>
- SOAP 1.1 (W3C Note): <https://www.w3.org/TR/2000/NOTE-SOAP-20000508/>
- WSDL 1.1 (W3C Note): <https://www.w3.org/TR/wsdl.html> `VERIFY` URL; WSDL 2.0: <https://www.w3.org/TR/wsdl20/>
- XML Namespaces: <https://www.w3.org/TR/xml-names/>; XML Schema 1.1: <https://www.w3.org/TR/xmlschema11-1/>
- OASIS WS-Security 1.1.1 SOAP Message Security: <https://docs.oasis-open.org/wss-m/wss/v1.1.1/wss-SOAPMessageSecurity-v1.1.1.html> `VERIFY`
- WS-I Basic Profile: <https://www.ws-i.org/Profiles/BasicProfile-1.1.html> `VERIFY` (archived)
- zeep: <https://docs.python-zeep.org/en/master/>, settings: <https://docs.python-zeep.org/en/master/settings.html>,
  WS-Security: <https://docs.python-zeep.org/en/master/wsse.html>
- lxml parsing and security: <https://lxml.de/parsing.html>, <https://lxml.de/FAQ.html#how-do-i-use-lxml-safely-as-a-web-service-endpoint>
- defusedxml: <https://github.com/tiran/defusedxml>
- Python XML vulnerabilities: <https://docs.python.org/3/library/xml.html#xml-vulnerabilities>
- OWASP XXE Prevention Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html>
- Azure API Management — import SOAP API / SOAP to REST: <https://learn.microsoft.com/en-us/azure/api-management/import-soap-api>
- Strangler Fig pattern (Azure Architecture Center): <https://learn.microsoft.com/en-us/azure/architecture/patterns/strangler-fig>
