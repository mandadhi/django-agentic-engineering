---
name: django-coding-standards
description: >-
  Mandatory Django engineering rule for application code: tooling compliance, where logic lives,
  error handling, correct use of Django APIs (URLs, time zones, settings, transactions), async
  safety and committed-code hygiene. Apply to every Python change in a Django project.
project: django-agentic-engineering
abstract: >-
  Enforceable coding standards that keep Django code consistent, correct and reviewable across
  projects, deferring style details to each project's configured tools.
sdlc_phase: Development
tech_stack: [Python, Django]
domain: [Software Engineering]
---

# Rule: Django Coding Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Make every Django change predictable to read, review and maintain, whoever or whatever wrote it.

## Scope

Applies to all Python code in a Django project. Model, API, database, testing, logging and
configuration specifics are in their own rules.

## Mandatory requirements

- **COD-M1** Changed code passes the formatter, linter and type checker the project already
  configures. If none is configured, follow PEP 8, and do not add a new tool unless the task allows it.
- **COD-M2** Business rules live in the layer the project designates (services, model methods
  or managers). Views, templates, URLconfs and serializer representation code MUST NOT hold them.
- **COD-M3** No bare `except:` and no silently swallowed exceptions. Catch specific exceptions,
  then handle, re-raise or log them with context.
- **COD-M4** Use Django's own mechanisms. URLs come from `reverse()` or `{% url %}`, never from
  hard-coded paths. The current time comes from `django.utils.timezone.now()` when `USE_TZ` is
  on. Settings are read from `django.conf.settings`, never by importing a settings module.
- **COD-M5** Writes that must succeed or fail together run in `transaction.atomic()`. Side
  effects that depend on the commit (emails, task dispatch, cache invalidation) are deferred
  with `transaction.on_commit()`.
- **COD-M6** Async views and consumers MUST NOT call blocking I/O or the synchronous ORM
  directly. Use the async ORM API, async clients, or `sync_to_async`.
- **COD-M7** No module-level mutable state that changes at request time, and no mutable default arguments.
- **COD-M8** No debugging leftovers (`print`, `breakpoint()`, `pdb`), no commented-out code
  and no unused imports in committed code.
- **COD-M9** If the project uses i18n, new user-facing strings are marked for translation.

## Recommendations

- **COD-R1** Add type hints to new public functions and services. Prefer keyword-only arguments
  for service entry points.
- **COD-R2** Use `TextChoices`/`IntegerChoices` and named constants instead of magic values.
- **COD-R3** Keep functions small and side effects explicit. Prefer explicit calls over signals
  for in-app control flow.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| Fat view containing business rules | Thin view calling the designated layer | COD-M2 |
| `except Exception: pass` | Specific exception, handled or logged | COD-M3 |
| `datetime.now()` with `USE_TZ = True` | `timezone.now()` | COD-M4 |
| `"/orders/%s/" % pk` in Python or templates | `reverse()` / `{% url %}` | COD-M4 |
| Sending email inside an atomic block | `transaction.on_commit()` | COD-M5 |
| `requests.get()` inside an `async def` view | Async client or `sync_to_async` | COD-M6 |

## Security and maintainability

Swallowed exceptions hide security failures, and sending side effects before commit leaks
state that is then rolled back. Consistent tooling keeps diffs reviewable.

## Verification

Run the project's lint, format and type-check commands. Grep for `except:`, `print(`,
`breakpoint(`, `datetime.now(` and hard-coded URL paths in the changed files.

## Related skills

[django-core](../../skills/django-core/SKILL.md) (URLs and views, asynchronous Django, logging and exceptions) ·
[django-architecture](../../skills/django-architecture/SKILL.md) (service layer, design patterns, anti-patterns).
