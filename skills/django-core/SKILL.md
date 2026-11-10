---
name: django-core
description: >-
  Django Core engineering specialist — framework architecture, project structure, settings,
  the request/response lifecycle, URLs and views, middleware, models and app startup, forms and
  validation, authentication and sessions, signals and management commands, file handling,
  caching, async Django, ASGI/WSGI, logging and Django-specific production configuration. Load
  when building, debugging, reviewing or maintaining any Django application.
project: django-agentic-engineering
abstract: >-
  The foundation every other Django skill assumes. A concise workflow and decision guide here;
  sixteen focused references under references/ that are loaded one or two at a time, only when
  the task needs them.
sdlc_phase: Development
tech_stack: [Python, Django]
domain: [Software Engineering]
---

# Django Core Engineering Specialist

Foundational Django knowledge for designing, implementing, debugging and maintaining real
applications. Baseline: **Django 5.2 LTS** (notes on 6.0 where behaviour differs). Before
recommending a version-sensitive API, check the target project's version
(`python -m django --version` or the lock file) and fall back to what that version supports.
Prefer what Django ships before adding a dependency.

## Activate when

- Developing or extending a Django application: views, URLs, forms, models, commands.
- Configuring a project: settings, environments, secrets, logging, storage, caches.
- Tracing or debugging the request lifecycle, middleware, exceptions or startup (`ready()`).
- Working with authentication, permissions or sessions.
- Adding async views, choosing ASGI vs WSGI, or preparing Django-level production settings.

Hand off when the topic leaves Django Core:

| Topic | Skill |
|---|---|
| Query design, migrations, indexes, transactions in depth | [django-orm](../django-orm/SKILL.md) |
| APIs, serializers, DRF auth/permissions | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Hardening, threat modelling, CSP, auditing | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Test architecture, fixtures, factories, CI | [django-testing](../django-testing/SKILL.md) |
| Profiling, load, query optimisation | [django-performance](../django-performance/SKILL.md) |
| App boundaries, service layer design | [django-architecture](../django-architecture/SKILL.md) |

## Engineering workflow

1. **Understand the requirement** — expected behaviour, constraints, the Django version.
2. **Identify the Django components involved** (table below) and load only those references.
3. **Inspect only the code you need** — the app's `apps.py`, `urls.py`, the view/form/model in
   question, and the relevant settings block. Don't sweep the repository.
4. **Choose the approach** using the decision guide; follow the project's existing conventions
   when they are sound.
5. **Weigh security, maintainability and performance** — each reference has a section for each.
6. **Implement with Django conventions**: thin views, logic in services, named URLs, settings
   from the environment, `AppConfig.ready()` for wiring only.
7. **Validate**: run the relevant tests, `python manage.py check` (and `check --deploy` for
   production settings), `makemigrations --check --dry-run` when models changed.
8. **Explain** the decisions and trade-offs that a reviewer would otherwise have to rediscover.

## Knowledge navigation

Load the one or two references that match the task. Each is self-contained.

| Task involves… | Load |
|---|---|
| How Django fits together, MVT, app registry, extension points | [fundamentals.md](references/fundamentals.md) |
| Layout, app boundaries, modular monolith, large codebases | [project-structure.md](references/project-structure.md) |
| `settings.py`, env vars, secrets, per-environment config | [settings-configuration.md](references/settings-configuration.md) |
| What happens between socket and response; exception flow | [request-response-lifecycle.md](references/request-response-lifecycle.md) |
| URLconfs, reversing, FBVs/CBVs/generic views, decorators | [urls-views.md](references/urls-views.md) |
| Writing or ordering middleware | [middleware.md](references/middleware.md) |
| Model basics, `save()`/`delete()`, managers, AppConfig, `ready()` | [models-application-lifecycle.md](references/models-application-lifecycle.md) |
| Forms, ModelForms, formsets, validation | [forms-validation.md](references/forms-validation.md) |
| Users, backends, permissions, login, sessions | [authentication-sessions.md](references/authentication-sessions.md) |
| Signals, `on_commit`, management commands | [signals-management-commands.md](references/signals-management-commands.md) |
| Uploads, `FileField`, storage backends, media | [file-handling.md](references/file-handling.md) |
| Cache framework, per-view/fragment caching, invalidation | [caching.md](references/caching.md) |
| `async def` views, async ORM, `sync_to_async`, blocking calls | [asynchronous-django.md](references/asynchronous-django.md) |
| WSGI vs ASGI, servers, workers, WebSockets/Channels | [asgi-wsgi.md](references/asgi-wsgi.md) |
| `LOGGING`, request IDs, error reporting, sensitive data | [logging-exceptions.md](references/logging-exceptions.md) |
| DEBUG, hosts, HTTPS, cookies, static, connections, health checks | [production-configuration.md](references/production-configuration.md) |

Typical pairings: a new endpoint → `urls-views` (+ `forms-validation`); a "works locally, fails
in prod" bug → `production-configuration` + `logging-exceptions`; a slow page →
`caching` (then django-performance).

## Decision guide

Defaults, not rules — pick against the actual requirement and say why.

| Choice | Prefer the first when… | Prefer the second when… |
|---|---|---|
| **FBV vs CBV** | Logic is bespoke, branching, or a single HTTP method; readability matters most | The view is standard CRUD/list/detail a generic view already models, or several views share behaviour through mixins |
| **Middleware vs decorator** | The concern applies to (nearly) every request: request IDs, security headers, tenant resolution | It applies to specific views: auth requirements, caching, method restrictions, rate limits on one endpoint |
| **Model methods vs service layer** | Behaviour depends only on one instance's own fields (`order.is_overdue()`), no I/O | It spans models, needs a transaction, calls external systems, or is a use case (`place_order`) |
| **Signals vs explicit calls** | Reacting to framework events, or an app must not import another (true decoupling) | It is your own business workflow — explicit calls are visible, testable and share the transaction |
| **Sync vs async views** | Work is ORM/CPU-bound, libraries are sync, or the stack is WSGI | The view awaits concurrent external I/O, streams, or long-polls — and runs under ASGI |
| **WSGI vs ASGI** | Fully synchronous app; mature, simple process/thread model | Async views, streaming/long-lived connections, WebSockets (via Channels) |
| **Caching strategy** | Low-level `cache.get/set` for expensive, explicitly-invalidated data | Per-view/fragment caching for anonymous or uniform output; site-wide middleware only for mostly-public sites |
| **Auth mechanism** | Session auth (built-in) for browser apps on the same site | Token/JWT via DRF for third-party or mobile API clients; SSO/OIDC via a custom backend for enterprise identity |

Cross-cutting principles:

- **Configuration comes from the environment**; required secrets fail fast at startup.
- **Business logic lives in services**, invoked from views, commands and tasks — never hidden in
  `save()` overrides, signals or middleware.
- **Every URL is named** and reversed; every app has an explicit `AppConfig`.
- **Make side effects transaction-aware** (`transaction.on_commit`) when they leave the DB.
- **Don't guess version behaviour** — qualify it or verify it.

## Validation checklist

- `python manage.py check` passes; production settings pass `check --deploy`.
- New views, forms, middleware and commands have tests (see django-testing).
- No secrets, `DEBUG=True` defaults, or hard-coded hosts introduced.
- Docs: <https://docs.djangoproject.com/en/stable/> (match the URL version to the project's).
