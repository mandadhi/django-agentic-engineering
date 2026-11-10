---
name: django-project-structure
description: >-
  Mandatory Django engineering rule for project and app layout: settings selection, entry
  points, app packages and AppConfig, app labels, migrations packages, cross-app references and
  startup code. Apply when creating or moving apps, modules or settings, or when assessing an
  existing project's structure.
project: django-agentic-engineering
abstract: >-
  Enforceable layout standards that keep any Django project discoverable, importable and safe to
  evolve, without prescribing a particular architecture.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django]
domain: [Software Engineering]
---

# Rule: Django Project Structure

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Keep the layout predictable, so that people, tools and agents can find settings, apps, models,
migrations and tests in any Django project.

## Scope

Applies to the project configuration package, apps, and how apps reference each other. It does
not choose an architecture style. That choice is made with `django-architecture`.

## Mandatory requirements

- **STR-M1** The project has one `manage.py` per deployable project. The settings module is
  chosen through `DJANGO_SETTINGS_MODULE`, and ASGI/WSGI entry points live in the configuration
  package. The configuration package contains no business code.
- **STR-M2** Every app is an importable package listed in `INSTALLED_APPS`, with an `apps.py`
  `AppConfig` whose `name` is the app's import path. The primary key type is fixed explicitly,
  either by `DEFAULT_AUTO_FIELD` or by `AppConfig.default_auto_field`.
- **STR-M3** App labels are unique and stable. An app label is not renamed, and a model is
  not moved between apps, without a migration plan that preserves existing tables.
- **STR-M4** Signal receivers, system checks and similar start-up wiring are connected in
  `AppConfig.ready()`. `ready()` and module import MUST NOT run database queries or network calls.
- **STR-M5** Each app keeps its migrations in its own committed `migrations/` package.
- **STR-M6** Relations to other apps' models use the `"app_label.ModelName"` string form, and
  relations to the user model use `settings.AUTH_USER_MODEL`. Code that needs the user class
  calls `get_user_model()`.
- **STR-M7** Imports between apps follow the dependency direction the project defines, and MUST
  NOT form import cycles.
- **STR-M8** Test code is never imported by production modules.

## Recommendations

- **STR-R1** Name each app after one capability. Split an oversized module into a package (for
  example `models/`) that re-exports its public names.
- **STR-R2** Keep shared helpers in a named package with a clear owner, not in a catch-all `utils.py`.
- **STR-R3** Use one layout style (flat or `src/`) consistently across the repository.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `from django.contrib.auth.models import User` in app code | `get_user_model()` / `settings.AUTH_USER_MODEL` | STR-M6 |
| Queries or HTTP calls in `ready()` or at import time | Lazy evaluation inside request/task code | STR-M4 |
| One "core" app that owns every model | Apps with distinct responsibilities | STR-R1 |
| `migrations/` ignored by version control | Committed migrations per app | STR-M5 |
| Business logic in `urls.py` or the settings package | Logic in the owning app | STR-M1 |

## Security and maintainability

Hidden start-up side effects make deployments, management commands and tests behave
differently. Stable labels and string model references keep migrations reproducible.

## Verification

`python manage.py check`. Grep for `auth.models import User`, for imports between app packages,
and for queries inside `ready()`. Confirm that every `INSTALLED_APPS` entry has an `apps.py`.

## Related skills

[django-core](../../skills/django-core/SKILL.md) (project structure, application lifecycle) ·
[django-architecture](../../skills/django-architecture/SKILL.md) (application boundaries, modular monolith).
