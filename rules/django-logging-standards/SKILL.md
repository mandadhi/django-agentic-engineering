---
name: django-logging-standards
description: >-
  Mandatory Django engineering rule for logging and error reporting: module loggers, central
  LOGGING configuration, no secrets or excess personal data in logs, logging exceptions once,
  log-injection safety, security event logging, and safe production error reporting. Apply when
  adding logging, handling exceptions or configuring error reporting.
project: django-agentic-engineering
abstract: >-
  Enforceable logging standards that make Django applications diagnosable without leaking
  sensitive data, independent of the log platform used.
sdlc_phase: Development
tech_stack: [Python, Django]
domain: [Software Engineering, Observability]
---

# Rule: Django Logging Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Logs must explain what happened in production without becoming a source of leaked data.

## Scope

Applies to application logging, the `LOGGING` setting, exception reporting and security event
records. Choosing a log platform and building dashboards are out of scope.

## Mandatory requirements

- **LOG-M1** Modules log through `logging.getLogger(__name__)`. `print()` is never used for diagnostics.
- **LOG-M2** Logging is configured in one place through the `LOGGING` setting, with levels
  that differ by environment.
- **LOG-M3** Logs never contain passwords, secrets, tokens, session identifiers, full
  `Authorization` headers or full request bodies. Personal data is limited to what the purpose
  needs, and masked where possible.
- **LOG-M4** An exception is logged once, with its traceback (`logger.exception`), at the layer
  that handles it. It is not logged again at every layer it passes through.
- **LOG-M5** User-controlled values are passed as logging arguments, not concatenated into the
  message. Output is structured, or the formatter neutralises line breaks, to prevent log injection.
- **LOG-M6** Security-relevant events are logged with actor, action, target, outcome and
  request or correlation id. That includes authentication success and failure, permission
  denials, privilege changes and bulk exports of sensitive data.
- **LOG-M7** Production errors are reported through the configured channel (for example
  `ADMINS` mail or an error tracker), never through debug pages. Views that handle secrets mark
  them with `sensitive_post_parameters` / `sensitive_variables`.

## Recommendations

- **LOG-R1** Use structured (JSON) logs with a request id that flows through tasks and outbound calls.
- **LOG-R2** Use levels consistently: DEBUG for diagnostics, INFO for business events,
  WARNING for recoverable problems, ERROR for failures that need attention.
- **LOG-R3** Write logs to stdout or stderr in containers, and let the platform collect them.
- **LOG-R4** Do not log inside tight loops. Aggregate instead.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `logger.info(f"login {request.POST}")` | Log the event fields only | LOG-M3, LOG-M5 |
| `except X: logger.error(e); raise` in every layer | Log once where the exception is handled | LOG-M4 |
| `print("debug", obj)` | `logger.debug(...)` | LOG-M1 |
| `logger.error(str(e))` that loses the traceback | `logger.exception(...)` | LOG-M4 |
| Permission denials not recorded | Security event with actor, action and target | LOG-M6 |

## Security and maintainability

Logs are often kept longer, and are more widely readable, than the data they describe. One
consistent configuration makes incidents faster to investigate.

## Verification

Grep the changed code for `print(`, f-string log messages, `request.POST`/`request.body` in log
calls, and `logger.error(` inside `except` blocks. Review the `LOGGING` setting for every environment.

## Related skills

[django-core](../../skills/django-core/SKILL.md) (logging and exceptions) ·
[django-security-engineering](../../skills/django-security-engineering/SKILL.md) (security logging and auditing) ·
[django-performance](../../skills/django-performance/SKILL.md) (profiling and observability).
