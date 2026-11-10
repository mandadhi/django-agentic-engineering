# Logging and exceptions

> Scope: Python logging in Django, the `LOGGING` setting, Django's loggers, structured logs, request correlation IDs, exception handling and reporting, sensitive data filtering, production debugging. Not covered here: how exceptions travel through the handler ([request-response-lifecycle.md](./request-response-lifecycle.md)), error views ([urls-views.md](./urls-views.md)), the request-ID middleware itself ([middleware.md](./middleware.md)), production settings ([production-configuration.md](./production-configuration.md)), APM/profiling ([django-performance](../../django-performance/SKILL.md)), DRF exception handlers ([django-rest-framework](../../django-rest-framework/SKILL.md)).

## Purpose and overview

Django uses the standard library `logging` module — there is no Django-specific logging API. The engineering goals are: every unhandled error is recorded once with a traceback and a correlation ID; logs are machine-parseable; secrets and personal data never reach them.

## How it works internally

- During `django.setup()`, `django.utils.log.configure_logging()` applies `django.utils.log.DEFAULT_LOGGING` first, then calls `LOGGING_CONFIG` (default `logging.config.dictConfig`) with your `LOGGING` dict. Setting `LOGGING_CONFIG = None` disables Django's configuration entirely (you then configure logging yourself).
- `DEFAULT_LOGGING` (verify in `django/utils/log.py` for your version): the `django` logger sends to console only when `DEBUG=True` (`RequireDebugTrue` filter) and to `mail_admins` (`AdminEmailHandler`) when `DEBUG=False`; `django.server` logs `runserver` requests.
- `disable_existing_loggers: True` disables loggers that already exist at configuration time — including `django.*` — and silently drops their output. Keep it `False`.
- Error routing: when a request fails, `response_for_exception()` logs via `log_response()` to `django.request` (ERROR with `exc_info` for 5xx, WARNING for 4xx), sends `got_request_exception`, and returns `handler500`. `SuspiciousOperation` subclasses log to `django.security.<ClassName>`.
- `AdminEmailHandler` builds a report through `DEFAULT_EXCEPTION_REPORTER` (Django ≥ 3.1) and filters it with `DEFAULT_EXCEPTION_REPORTER_FILTER` (default `SafeExceptionReporterFilter`), which masks settings whose names match `API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE` and values marked by the sensitive decorators. The technical 500 page under `DEBUG=True` uses the same filter.

Django's loggers:

| Logger | Content |
|---|---|
| `django` | catch-all parent |
| `django.request` | 4xx/5xx from the request handler |
| `django.server` | `runserver` access log |
| `django.template` | template rendering issues (DEBUG) |
| `django.db.backends` | every SQL query, only when `DEBUG=True` |
| `django.db.backends.schema` | migration SQL |
| `django.security.*` | `SuspiciousOperation`, e.g. `django.security.DisallowedHost`, `django.security.csrf` |
| `django.contrib.sessions` | session save errors |

## Core concepts and APIs

- **Logger** (`logging.getLogger(__name__)`) — named, hierarchical; module names make filtering per app trivial (`apps.billing`).
- **Handler** — destination (`StreamHandler` to stdout, `AdminEmailHandler`).
- **Formatter** — line shape (key=value or JSON).
- **Filter** — drop or enrich records (`RequireDebugFalse`, `RequireDebugTrue`, `CallbackFilter`, custom).
- **Levels** — DEBUG (diagnostics, off in prod), INFO (business events), WARNING (unexpected but handled), ERROR (operation failed), CRITICAL (service unusable).
- `logger.exception()` inside `except` = ERROR + traceback.
- `django.views.decorators.debug.sensitive_variables(*names)` and `sensitive_post_parameters(*names)` — mask locals/POST fields in error reports (async views supported from Django 5.0).
- `ADMINS` + `SERVER_EMAIL` + `EMAIL_*` drive `mail_admins`. Django **6.0** changes `ADMINS`/`MANAGERS` to plain email strings and deprecates the `(name, email)` tuple form — check the 6.0 release notes for the target project.

## Implementation examples

Request-scoped context (absorbed from the project's settings reference):

```python
# config/logging.py
import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
```

`RequestIdMiddleware` (in `config/middleware.py`, see [middleware.md](./middleware.md)) sets and resets the variable; ContextVars propagate across `sync_to_async`/`async_to_sync`, so the ID survives under ASGI.

`LOGGING` for stdout key=value logs plus admin email on errors:

```python
# config/settings.py
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.logging.RequestIdFilter"},
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "formatters": {
        "kv": {"format": "ts=%(asctime)s level=%(levelname)s logger=%(name)s "
                         "request_id=%(request_id)s msg=%(message)s"},
    },
    "handlers": {
        "stdout": {"class": "logging.StreamHandler", "formatter": "kv", "filters": ["request_id"]},
        "mail_admins": {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR",
                        "filters": ["require_debug_false"], "include_html": False},
    },
    "root": {"handlers": ["stdout"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["mail_admins"], "level": "ERROR", "propagate": True},
        "django.security": {"level": "WARNING", "propagate": True},
        "django.db.backends": {"level": "WARNING"},      # set DEBUG locally to see SQL
    },
}
```

Notes: `propagate: True` sends `django.request` records to root's stdout handler too, so each error appears once in stdout and once by email. If an error tracker (e.g. Sentry's Django integration) is installed, drop `mail_admins` — two alert channels for the same error cause fatigue.

JSON output with the standard library only:

```python
# config/logging.py
import json

class JsonFormatter(logging.Formatter):
    RESERVED = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items()
                        if k not in self.RESERVED and not k.startswith("_")})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
```

Structured events from services:

```python
# apps/orders/services.py
logger = logging.getLogger(__name__)

def place_order(*, customer, cart) -> Order:
    order = ...
    logger.info("order_placed", extra={"order_id": order.pk, "customer_id": customer.pk,
                                       "total": str(order.total)})
    return order
```

Bad vs good exception handling:

```python
# BAD: swallows the traceback, logs PII, and hides the failure from callers
try:
    charge_card(order, card_number)
except Exception as e:
    logger.error(f"payment failed for {card_number}: {e}")

# GOOD: catch what you can handle, keep the traceback, log identifiers, re-raise a domain error
try:
    charge_card(order, token)
except PaymentGatewayTimeout:
    logger.warning("payment_timeout", extra={"order_id": order.pk})
    raise PaymentProviderUnavailable from None
except PaymentGatewayError:
    logger.exception("payment_failed", extra={"order_id": order.pk})
    raise
```

Masking sensitive data in error reports:

```python
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

@sensitive_post_parameters("card_number", "cvv")
@login_required
def pay(request): ...

@sensitive_variables("token")
def charge_card(order, token): ...
```

Custom exception reporter filter (extend the masking pattern):

```python
# config/logging.py
import re
from django.views.debug import SafeExceptionReporterFilter

class ProjectReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(r"API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE|DSN|IBAN",
                                 flags=re.IGNORECASE)
# settings: DEFAULT_EXCEPTION_REPORTER_FILTER = "config.logging.ProjectReporterFilter"
```

Domain exception hierarchy — lets views/middleware map errors to responses consistently:

```python
# apps/billing/exceptions.py
class BillingError(Exception): """Base for billing failures callers may handle."""
class PaymentDeclined(BillingError): ...
class PaymentProviderUnavailable(BillingError): ...
```

Map them at one boundary (view or `process_exception` middleware); keep services free of HTTP concerns.

## Production patterns

- **stdout only**; let the platform ship logs. File handlers in containers fill disks and lose logs on restart.
- **One correlation ID per request**, returned in `X-Request-ID`, included in every log record, and propagated to outbound calls and background tasks (pass it in task arguments).
- **Event names, not prose**: `order_placed` with `extra` fields is searchable; f-strings are not, and they format eagerly even when the level is disabled — prefer `logger.info("x %s", value)` or `extra`.
- **Log once, where handled.** Logging and re-raising at every layer produces duplicate stack traces.
- **Alert on `django.request` ERROR and `django.security.*`**, not on WARNING 404s.
- **Error tracker** (Sentry or similar) for grouping, release tracking and breadcrumbs; configure its `before_send`/scrubbing for PII. It complements, not replaces, logs.
- **Observability** beyond logs (metrics, traces) — record route-level latency with `resolver_match.route` labels; tooling in [django-performance](../../django-performance/SKILL.md).

## Decision guidance

| Situation | Approach |
|---|---|
| Unexpected error you cannot handle | let it propagate; Django logs it and returns 500 |
| Expected failure with a fallback | catch specific exception, log WARNING with ids, continue |
| Failure the caller must know about | raise a domain exception; map to HTTP at the view boundary |
| Need the traceback | `logger.exception` inside `except`, or `exc_info=True` |
| Small team, no tracker | `AdminEmailHandler` with `include_html=False` |
| Tracker installed | tracker + stdout; remove mail_admins |
| Structured logs | stdlib JSON formatter; `structlog` only if already in the project |

## Security considerations

- Never log passwords, tokens, session IDs, full cookies, `Authorization` headers, `request.body`, card data or unneeded personal data.
- `include_html=True` on `AdminEmailHandler` emails the full technical page (locals, settings) — keep `False` unless email is a trusted channel.
- `SafeExceptionReporterFilter` only masks by **name**; secrets in variables with innocent names still leak — use `sensitive_variables`.
- Log injection: user input with newlines can forge log lines in plain-text formats; JSON formatting escapes them.
- Hardening depth: [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- `django.db.backends` DEBUG logging only works with `DEBUG=True` and is expensive — never enable in production.
- Synchronous network handlers (SMTP via `AdminEmailHandler`, HTTP log shippers) block the request on error spikes; an error storm becomes a latency storm. Prefer stdout + async collection, or a `QueueHandler`.
- Guard expensive log argument construction with `if logger.isEnabledFor(logging.DEBUG):`.

## Testing

```python
from django.test import TestCase

class PaymentLoggingTests(TestCase):
    def test_timeout_logs_warning_without_token(self):
        with self.assertLogs("apps.billing.services", level="WARNING") as logs:
            with self.assertRaises(PaymentProviderUnavailable):
                pay_order(order=self.order, token="tok_secret")
        self.assertIn("payment_timeout", logs.output[0])
        self.assertNotIn("tok_secret", "".join(logs.output))
```

- `assertLogs` / `assertNoLogs` (Python ≥ 3.10) for log assertions.
- Test that `ProjectReporterFilter.get_safe_settings()` masks the new keys.
- Test custom 500 handling with `Client(raise_request_exception=False)` and `self.assertLogs("django.request", "ERROR")`.

## Common mistakes and anti-patterns

- `disable_existing_loggers: True` → Django's own errors vanish.
- `print()` debugging left in code; it bypasses levels and formatting.
- `except Exception: pass` or `logger.error(str(e))` without traceback.
- Logging the same exception at every layer.
- f-strings/`%` formatting in the call (`logger.info(f"...")`) — eager formatting, unsearchable messages.
- Using a custom `extra` key that collides with `LogRecord` attributes (`message`, `args`, `module`) → `KeyError`.
- Expecting SQL logs in production.

## Troubleshooting

| Symptom | Check |
|---|---|
| No logs from `django.request` | `disable_existing_loggers`, handler levels, `propagate`, `LOGGING_CONFIG=None` |
| `KeyError: 'request_id'` in formatter | formatter uses `%(request_id)s` but handler lacks the `RequestIdFilter` |
| Duplicate log lines | handler on both child logger and root with `propagate=True` |
| No admin emails | `DEBUG=True` (filter), empty `ADMINS`, wrong `EMAIL_BACKEND`, SMTP blocked |
| Request ID `-` in task logs | ID not passed to the task; ContextVars don't cross process boundaries |
| Secret visible in error report | variable name not matched; add `sensitive_variables` or extend filter |

Production debugging workflow: get the request ID from the user/response header → search logs by it → read the traceback in the tracker → reproduce locally with the same input in a test → fix and add the test.

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/logging/
- https://docs.djangoproject.com/en/stable/ref/logging/
- https://docs.djangoproject.com/en/stable/howto/error-reporting/
- https://docs.djangoproject.com/en/stable/ref/settings/#logging
- https://docs.djangoproject.com/en/stable/ref/settings/#admins
- https://docs.python.org/3/library/logging.html
- https://docs.python.org/3/howto/logging-cookbook.html
- https://docs.python.org/3/library/contextvars.html
