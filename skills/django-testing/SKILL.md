---
name: django-testing
description: >-
  Django testing engineering specialist — test strategy and risk-based test design, Django's
  test framework (SimpleTestCase/TestCase/TransactionTestCase/LiveServerTestCase), pytest and
  pytest-django, model/ORM and migration tests, service and architecture tests, views, forms,
  middleware and DRF API tests, security regression tests, factories and test data, mocking,
  external integrations and Celery tasks, async tests, transaction and concurrency tests, query
  counts and suite performance, coverage and mutation testing, CI/CD test automation, browser
  E2E tests, and debugging failing or flaky tests. Load when writing, reviewing, designing,
  speeding up or troubleshooting tests for a Django or DRF codebase.
project: django-agentic-engineering
abstract: >-
  Tests prove behaviour at the boundaries the team owns, run deterministically and fast, and
  fail for one reason. A concise workflow and navigation guide here; eighteen focused references
  under references/ loaded one to three at a time, only when the task needs them.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, pytest, pytest-django, PostgreSQL]
domain: [Software Engineering, Quality Assurance]
---

# Django Testing Engineering Specialist

Engineering judgement for Django test suites: decide what must be proven and at which level,
write isolated and deterministic tests, keep the suite fast, and wire it into CI so regressions
cannot merge. Baseline: **Django 5.2 LTS, DRF 3.16, pytest ≥ 8, pytest-django 4.x**;
version-dependent behaviour is qualified "(Django ≥ X.Y)" and uncertain claims are marked
`VERIFY`. Check the project's lock file and existing test style before recommending a tool.

Every reference labels where a feature comes from — **[Django]**, **[pytest]**,
**[pytest-django]**, **[DRF]**, **[Python]** or **[third-party: package]** — because the same
idea (fixtures, DB access, settings overrides) has different APIs in each. Never assume a
third-party package is installed.

An organisation-wide test-design plugin skill (`qa-test-design`) may exist; follow it when
present. This skill is the Django implementation.

## Activate when

- Writing or reviewing tests for a model, QuerySet, service, selector, view, form, middleware,
  API endpoint, task, command, webhook or migration.
- Designing a test strategy or deciding what a change must be tested with.
- Setting up, configuring, speeding up or parallelising a test suite or its CI job.
- A test fails, is flaky, passes alone but fails in the suite, or fails only in CI.
- Assessing coverage gaps, regression risk or whether a change is adequately tested.

## Testing engineering principles

1. **Test behaviour, not implementation.** Assert outcomes callers observe (return values, DB
   state, responses, emitted messages) — never private call sequences.
2. **Test at the boundary you own.** Services and selectors directly; endpoints through the test
   client; tasks by calling them. Don't re-test Django, DRF or third-party libraries.
3. **The cheapest level that can catch the bug.** Unit for rules, integration for DB/HTTP
   wiring, a few E2E tests for critical journeys.
4. **Isolated and deterministic.** No shared mutable data, no real network, no wall-clock or
   unseeded randomness, no order dependence. A test passes alone, in any order, in parallel.
5. **Real database, fake edges.** Use the production database engine; replace only what crosses
   the process boundary (HTTP, email, storage, clocks, brokers). Mocks never prove a real
   integration works.
6. **Every failure path is a requirement.** Validation errors, denied access, other tenant's
   data, timeouts, retries and duplicates are tested, not only the happy path.
7. **Minimal, explicit test data.** Factories build valid objects; each test states only what it
   asserts on.
8. **Fast feedback is a feature.** Measure suite time; slow tests are a defect to fix.
9. **Coverage finds untested code; it does not prove tested code.** Gate on it, never optimise for it.
10. **Every bug fix ships with a test that failed before the fix.**

## Testing workflow

1. **Understand the feature** — acceptance criteria, actors/roles, the Django/DRF/pytest
   versions, existing test conventions (`conftest.py`, factories, runner).
2. **Identify rules, dependencies and boundaries** — business invariants, DB constraints,
   transactions, external systems, permissions, tenancy → `testing-fundamentals.md`.
3. **Select test levels** — which behaviour is proven by unit, integration, API or E2E tests;
   choose the test class / DB marker → `django-testing-framework.md`, `pytest-and-pytest-django.md`.
4. **Design test data and fixtures** — factories, traits, scope → `fixtures-and-test-data.md`.
5. **Implement isolated, deterministic tests** — the owner reference for the component (table below).
6. **Test failure scenarios and edge cases** — validation, permissions, errors, retries,
   concurrency, boundaries of time and size.
7. **Validate database, security and integration behaviour** — constraints, `on_commit`,
   object-level access, tenant isolation, external-call failure modes.
8. **Evaluate coverage, maintainability and speed** — branch coverage of the change, query
   counts, suite duration, flakiness → `coverage-and-quality-analysis.md`,
   `performance-and-reliability-testing.md`.

Inspect only the code under test, its factories and the nearest `conftest.py`. Load one to three
references per step — never all.

## Reference navigation

Each reference stands alone and follows the same 12-section structure.

| Reference | Owns |
|---|---|
| [testing-fundamentals.md](references/testing-fundamentals.md) | Principles, pyramid/trophy, test levels, isolation, determinism, risk-based selection, what not to test, layout and naming, strategy documents |
| [django-testing-framework.md](references/django-testing-framework.md) | Django runner, `SimpleTestCase`/`TestCase`/`TransactionTestCase`/`LiveServerTestCase`, discovery, test settings, test DB lifecycle, `--parallel`, tags, `override_settings` |
| [pytest-and-pytest-django.md](references/pytest-and-pytest-django.md) | pytest fixtures/scopes/markers/parametrize/selection/config, pytest-django markers and fixtures, `--reuse-db`, plugins, pytest vs unittest choice |
| [model-and-orm-testing.md](references/model-and-orm-testing.md) | Model validation, methods, constraints, relationships, QuerySets, managers, `db_default`, signals, migration and data-migration tests |
| [service-and-architecture-testing.md](references/service-and-architecture-testing.md) | Services, selectors, business rules, domain events, transaction boundaries, DI/fakes, app-boundary contracts, modular-monolith tests |
| [views-forms-and-middleware-testing.md](references/views-forms-and-middleware-testing.md) | Test `Client`, `RequestFactory`, FBV/CBV, login/permission behaviour, forms, redirects, templates/context, middleware, files, sessions/cookies |
| [django-rest-framework-testing.md](references/django-rest-framework-testing.md) | API test strategy, `APIClient`/`APIRequestFactory`/`APITestCase`, auth/permission matrices, serializers, pagination/filters, throttling, negotiation, contract tests |
| [security-testing.md](references/security-testing.md) | Login/logout/reset flows, session rotation, CSRF enforcement, CORS boundary, uploads, IDOR, tenant isolation, security regression tests |
| [fixtures-and-test-data.md](references/fixtures-and-test-data.md) | pytest data fixtures, Django JSON fixtures, factory_boy, model_bakery, Faker, builders, `setUpTestData`, scopes, large datasets |
| [mocking-and-dependency-isolation.md](references/mocking-and-dependency-isolation.md) | `unittest.mock`, `patch`/autospec, `monkeypatch`, pytest-mock, HTTP/SDK/email/storage doubles, time and randomness, mock vs fake vs stub |
| [integration-and-background-task-testing.md](references/integration-and-background-task-testing.md) | External API and webhook tests, email/storage integration, Celery tasks, retries, idempotency, event workflows, management commands |
| [async-testing.md](references/async-testing.md) | Async views, `AsyncClient`, async tests in Django and pytest-asyncio, async ORM and DB access, event-loop scopes, async HTTP, concurrency |
| [database-and-transaction-testing.md](references/database-and-transaction-testing.md) | Isolation mechanics, `TestCase` vs `TransactionTestCase`, `atomic`, `on_commit`, locking/concurrency tests, multi-DB, SQLite vs PostgreSQL |
| [performance-and-reliability-testing.md](references/performance-and-reliability-testing.md) | Query-count budgets, N+1 detection, suite profiling and speed-ups, flaky detection and quarantine, perf-regression and load-test boundaries |
| [coverage-and-quality-analysis.md](references/coverage-and-quality-analysis.md) | coverage.py, pytest-cov, branch/diff coverage, exclusions, thresholds, mutation testing, static analysis, coverage-gap analysis |
| [ci-cd-test-automation.md](references/ci-cd-test-automation.md) | CI jobs, DB services, environment, xdist and sharding, artifacts, coverage publishing, PR gates, fast/slow suites, CI reliability |
| [browser-and-end-to-end-testing.md](references/browser-and-end-to-end-testing.md) | Playwright, Selenium, `StaticLiveServerTestCase`/`live_server`, browser auth, page objects, E2E reliability, when E2E is justified |
| [test-maintenance-and-troubleshooting.md](references/test-maintenance-and-troubleshooting.md) | Debugging failures, fixture errors, state leakage, time bugs, order/parallel failures, flaky root causes, environment issues, suite upkeep |

## Task-based loading

| Task | Load |
|---|---|
| Plan tests for a new feature / write a test strategy | testing-fundamentals.md (+ owner of the main component) |
| Set up a new test suite or migrate unittest → pytest | pytest-and-pytest-django.md, django-testing-framework.md |
| Choose `TestCase` vs `TransactionTestCase` / DB marker | django-testing-framework.md, database-and-transaction-testing.md |
| Test models, constraints, managers or signals | model-and-orm-testing.md (+ fixtures-and-test-data.md) |
| Test a data migration | model-and-orm-testing.md |
| Test a service, selector or business rule | service-and-architecture-testing.md (+ mocking-and-dependency-isolation.md) |
| Test HTML views, forms, middleware or file downloads | views-forms-and-middleware-testing.md |
| Test a DRF endpoint or serializer | django-rest-framework-testing.md (+ security-testing.md for access) |
| Prove permissions, IDOR, CSRF or tenant isolation | security-testing.md (+ django-rest-framework-testing.md) |
| Build or refactor factories / shared test data | fixtures-and-test-data.md |
| Replace an external dependency in tests | mocking-and-dependency-isolation.md |
| Test webhooks, third-party APIs, email, storage or Celery | integration-and-background-task-testing.md, mocking-and-dependency-isolation.md |
| Test async views or async services | async-testing.md (+ database-and-transaction-testing.md) |
| Test `on_commit`, locking, races or multi-DB routing | database-and-transaction-testing.md |
| Catch N+1 queries / add query budgets | performance-and-reliability-testing.md (+ model-and-orm-testing.md) |
| Suite is slow | performance-and-reliability-testing.md, ci-cd-test-automation.md |
| Find untested code / set coverage gates | coverage-and-quality-analysis.md |
| Build or fix the CI test pipeline | ci-cd-test-automation.md (+ coverage-and-quality-analysis.md) |
| Add browser / end-to-end tests | browser-and-end-to-end-testing.md |
| Test fails, is flaky, or fails only in CI / in parallel | test-maintenance-and-troubleshooting.md (+ performance-and-reliability-testing.md) |
| Review a PR's tests | testing-fundamentals.md, then the owner of each finding |

## Skill pairings and handoffs

This skill owns **how to prove behaviour**; the paired skill owns **how the mechanism works**.

| Topic | Skill |
|---|---|
| Request lifecycle, middleware, forms, auth/session, signals, async mechanics | [django-core](../django-core/SKILL.md) |
| Service/selector design, app boundaries, domain events, DI seams | [django-architecture](../django-architecture/SKILL.md) |
| QuerySet semantics, constraints, transactions/locking, migrations, query optimisation | [django-orm](../django-orm/SKILL.md) |
| Serializers, permissions, throttles, pagination, API debugging internals | [django-rest-framework](../django-rest-framework/SKILL.md) |
| Which controls must exist, threat models, vulnerability mitigation | [django-security-engineering](../django-security-engineering/SKILL.md) |
| Profiling, load testing, production performance | [django-performance](../django-performance/SKILL.md) |
| Celery, brokers, HTTP clients, webhooks, identity providers | [django-integrations](../django-integrations/SKILL.md) |
| PostgreSQL locking, isolation, plans, configuration | [postgresql-engineering](../postgresql-engineering/SKILL.md) |
| Templates, JavaScript and frontend build tooling | [django-frontend](../django-frontend/SKILL.md) |

Typical pairings: new API endpoint → django-rest-framework-testing + security-testing (with
django-rest-framework permissions); concurrency bug → database-and-transaction-testing (with
django-orm transactions-and-concurrency); new service → service-and-architecture-testing (with
django-architecture service-layer).

## Testing decision guidelines

| Decision | Prefer | Because |
|---|---|---|
| Runner | pytest + pytest-django; keep Django `TestCase` where the suite already uses it | Fixtures, parametrisation, plugins; pytest runs `TestCase` classes unchanged |
| Test class / DB access | `TestCase` / `django_db`; `TransactionTestCase` / `transaction=True` only for real commits, threads, or other connections | Rollback isolation is an order of magnitude faster than truncation |
| `on_commit` side effects | `captureOnCommitCallbacks` / `django_capture_on_commit_callbacks(execute=True)` | Proves the callback without a transactional test |
| Test data | Factories with minimal defaults and traits | Explicit, isolated, refactor-safe; JSON fixtures rot |
| Unit vs integration for services | Service tests against the real test DB; fake only external gateways | Mocking the ORM tests the mock |
| External HTTP | Transport-level mocks (responses/respx) + a separate contract/sandbox suite | Fast deterministic unit tests, real-API drift still detected |
| Time | Inject a clock or use time-machine/freezegun | Wall-clock tests fail at midnight, month-end and DST |
| Database engine | Production engine (PostgreSQL) in CI | SQLite differs in constraints, locking, types and JSON |
| API assertions | Status + key fields + DB effect; schema diff for contracts | Full-body snapshots break on harmless changes |
| Browser tests | A handful of critical journeys, Playwright | Highest cost and flake rate per assertion |
| Coverage | Branch coverage, ratchet threshold, diff coverage on PRs | A global % hides untested new code |
| Flaky test | Fix root cause; quarantine with an owner and deadline; never blanket reruns | Reruns hide real race conditions |

## Validation checklist

- [ ] Each acceptance criterion and business rule maps to at least one test at the cheapest level.
- [ ] Failure paths covered: invalid input, anonymous, forbidden, other user/tenant (404), external error/timeout, duplicate/retry.
- [ ] Tests assert observable behaviour (response, DB state, outbox, calls at the boundary), not internals.
- [ ] Test data comes from factories; no test depends on another test's data or order.
- [ ] No real network, SMTP, cloud or broker calls; time and randomness controlled.
- [ ] DB access explicit; transactional tests only where real commits are required.
- [ ] Hot read paths have query-count assertions.
- [ ] Bug fixes include a regression test that failed before the fix.
- [ ] Suite passes in random order and in parallel; tests run against the production DB engine in CI.
- [ ] Branch coverage of changed code reviewed; thresholds enforced in CI.
- [ ] Third-party test packages used are installed and named as third-party; `VERIFY` claims checked.
- Docs: <https://docs.djangoproject.com/en/5.2/topics/testing/> ·
  <https://docs.pytest.org/> · <https://pytest-django.readthedocs.io/>
