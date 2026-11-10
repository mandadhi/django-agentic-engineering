---
name: django-project-discovery
description: >-
  Shared reference for discovering the facts of an existing Django codebase read-only: where each
  fact lives, which signals win when they conflict, which commands are safe, and how to record
  evidence and gaps.
kind: shared-reference
agents: [django-project-analyzer, django-architecture-agent, django-documentation-agent]
project: django-agentic-engineering
abstract: >-
  A lookup table from question to evidence for Django projects, safe read-only commands, pitfalls
  that produce wrong conclusions, and a sampling method for large repositories.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, AI Agents]
---

# Django Project Discovery

## Purpose

Agents that design, document or review a Django project first need facts about it. This reference
says where each fact is found, how to resolve conflicting signals, and which inspection commands
are safe to run, so different agents reach the same answer from the same repository.

## Scope

Read-only discovery of a Django repository: layout, settings, apps, interfaces, data model,
background work, integrations, tests and tooling. It does not cover what to recommend; that is
the job of the skills and rules linked at the end.

## How to use

Look up the question in the table, read the primary evidence, and fall back to the secondary
evidence only when the primary is absent. Record each answer with its `file:line`, or mark it
`inferred` or `unknown`. Results go into the
[project analysis](../templates/project-analysis.template.md) template.

## Where each fact lives

| Question | Primary evidence | Secondary evidence | Pitfall |
|---|---|---|---|
| Where is the project root? | Directory holding `manage.py` | `pyproject.toml` with Django dependency | Repositories may hold several projects, or `manage.py` under `src/` |
| Which settings module runs? | `DJANGO_SETTINGS_MODULE` default in `manage.py`, `wsgi.py`, `asgi.py` | CI and container environment, `pytest.ini`/`pyproject.toml` `DJANGO_SETTINGS_MODULE` | Tests often use a different module than production |
| How are settings split? | `settings/` package imports (`from .base import *`) | Environment-reading helper calls | Late imports can override earlier values; read top to bottom |
| Which apps are installed? | `INSTALLED_APPS` in the selected settings, after all overrides | `apps.py` `AppConfig.name` | A package with `models.py` is not an app unless installed |
| What is each app's label? | `AppConfig.label`, else the last part of `name` | Migration dependencies | Labels, not package names, appear in migrations and `app_label` |
| Which user model is used? | `AUTH_USER_MODEL` | Migrations depending on `settings.AUTH_USER_MODEL` | Code importing `User` directly may contradict it |
| What are the URL entry points? | `ROOT_URLCONF` and its `include()` tree | `urls.py` in each app | Routers and `re_path` hide paths; follow every include |
| Which API framework and auth? | `REST_FRAMEWORK` setting, router registrations | Per-view `authentication_classes`/`permission_classes` | Per-view settings override the defaults |
| What is the database? | `DATABASES` `ENGINE`, database URL parser calls | Driver in the lock file, docker compose services | Values from the environment are known only by name |
| What runs in the background? | Celery app module, `CELERY_*` or task-queue settings, `@shared_task` | Procfile, compose services, beat schedule | Scheduled tasks may live in the database scheduler |
| Which caches and storage? | `CACHES`, `STORAGES` or file storage settings | Imports of storage backends | Defaults apply when a setting is absent |
| Which outbound integrations? | HTTP client imports and wrappers, SDK imports | Settings names ending in `_URL`, `_API_KEY` (names only) | Do not read secret values to confirm |
| What does the data model look like? | `models.py` or `models/` packages | Latest migrations per app | Abstract and proxy models produce no tables |
| How are tests run? | `pytest.ini`, `pyproject.toml` `[tool.pytest.ini_options]`, `setup.cfg`, `tox.ini` | CI workflow commands | Two runners can coexist; the CI command is authoritative |
| Which quality tools? | `pyproject.toml` tool sections, `.pre-commit-config.yaml` | CI steps | Tools configured but not run in CI are not enforced |

## Safe and unsafe commands

| Safe (read-only, no database, no network) | Unsafe for discovery |
|---|---|
| `git ls-files`, `git log --oneline -n 20`, directory listings | `manage.py migrate`, `flush`, `dbshell`, `loaddata`, `runserver` |
| `python -m django --version` in an existing environment | `pip install`, `poetry install`, any package download |
| `python manage.py check` and `showmigrations --plan` only when the settings import without secrets and the task allows it | Commands that connect to a non-test database or the network |
| Reading files with search tools | Reading `.env`, key files or `secrets/` |

When a command needs configuration that is not present, stop and record the gap instead of
creating configuration.

## Signals of architecture

| Signal | How to find it | What it suggests |
|---|---|---|
| Cross-app imports | `grep -rn "from <app>\." --include=*.py` per app | Coupling and dependency direction |
| Service or selector modules | `services.py`, `selectors.py`, `domain/` packages | A layered style is in use |
| Fat views | Long view functions with ORM writes and external calls | Business logic in the interface layer |
| Signals as workflow | `post_save` receivers with side effects | Hidden control flow |
| Raw SQL | `raw(`, `cursor.execute`, `RawSQL`, `extra(` | Review points for security and portability |

## Large repositories

Profile every app's `apps.py`, `models`, `urls` and settings fully. For views, serializers and
tests, sample a fixed number of files per app chosen by size and recent change
(`git log --format= --name-only | sort | uniq -c | sort -rn | head`), and state the sampling rule
in the limitations.

## Related skills and rules

- Skills: [django-core](../skills/django-core/SKILL.md)
  ([project structure](../skills/django-core/references/project-structure.md),
  [settings](../skills/django-core/references/settings-configuration.md)),
  [django-architecture](../skills/django-architecture/SKILL.md)
  ([application boundaries](../skills/django-architecture/references/django-application-boundaries.md)),
  [django-orm](../skills/django-orm/SKILL.md).
- Rules: [django-project-structure](../rules/django-project-structure/SKILL.md),
  [django-configuration-standards](../rules/django-configuration-standards/SKILL.md),
  [django-security-standards](../rules/django-security-standards/SKILL.md) (`SEC-A1`, `SEC-A2`).
- Version detection: [django-version-compatibility](django-version-compatibility.md).
