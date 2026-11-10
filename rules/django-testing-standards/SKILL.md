---
name: django-testing-standards
description: >-
  Mandatory Django engineering rule for tests: tests with every behaviour change and bug fix,
  test-database-only execution, isolation from networks and external services, determinism,
  access-control coverage, synthetic data, never weakening existing tests, and correct
  transaction test classes. Apply when writing, changing or running tests.
project: django-agentic-engineering
abstract: >-
  Enforceable testing standards that make Django test suites trustworthy, safe to run and
  independent of any particular test runner.
sdlc_phase: Testing
tech_stack: [Python, Django, pytest, pytest-django]
domain: [Software Engineering, Quality Assurance]
---

# Rule: Django Testing Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Tests are evidence. They must prove behaviour, run safely anywhere, and give the same result every time.

## Scope

Applies to all automated tests in a Django project, whether they use Django's runner or pytest-django.

## Mandatory requirements

- **TST-M1** Every behaviour change includes tests. Every bug fix includes a regression test
  that fails without the fix.
- **TST-M2** Tests use only the database the test runner creates. Test settings MUST NOT point
  at a shared or production database.
- **TST-M3** Tests make no real network calls and send no real email or messages. External
  services are replaced at the integration boundary, and email uses the in-memory backend.
- **TST-M4** Tests are deterministic and independent. There is no ordering dependency, no
  `sleep`, time is frozen or injected where it matters, and randomness is seeded.
- **TST-M5** Protected views and endpoints are tested at least for anonymous access, an
  authenticated user without permission, a permitted user, and access to another user's object.
- **TST-M6** Test data is synthetic and built with the project's factories or fixtures. It
  never contains production data or real credentials.
- **TST-M7** Existing tests are not deleted, skipped or weakened to make a change pass. An
  expected failure needs a reason and a tracking reference.
- **TST-M8** Code that depends on commits is tested with `TestCase.captureOnCommitCallbacks()`
  (or the pytest-django equivalent), or with `TransactionTestCase` when real commit
  behaviour is the subject.
- **TST-M9** Assertions check results and side effects, not only status codes.

## Recommendations

- **TST-R1** Use the project's existing runner and layout. Prefer pytest-django with factories
  when starting fresh.
- **TST-R2** Assert query counts (`assertNumQueries` or `django_assert_num_queries`) on hot paths.
- **TST-R3** Use a fast password hasher in test settings, and change settings in tests with
  `override_settings` or the `settings` fixture.
- **TST-R4** Measure coverage of changed code and name tests after the behaviour they prove.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `assert response.status_code == 200` as the only check | Assert payload, state and side effects | TST-M9 |
| Calling a real payment, email or HTTP API in tests | Fake at the integration boundary | TST-M3 |
| `time.sleep()` to wait for async work | Deterministic hooks or frozen time | TST-M4 |
| `@skip` added to a failing test in the same change | Fix the code or report the defect | TST-M7 |
| Large JSON fixtures copied from production | Synthetic factories | TST-M6 |
| Testing only the owner's access | Full access matrix | TST-M5 |

## Security and maintainability

Isolated test databases and synthetic data stop tests from leaking or corrupting real data.
Deterministic tests keep CI signals trustworthy.

## Verification

Run the suite with the project's command. Grep test settings for `DATABASES` hosts, and tests
for `sleep(`, real URLs and `skip`. Compare the test diff against the behaviour diff.

## Related skills

[django-testing](../../skills/django-testing/SKILL.md) (framework, pytest-django, fixtures, mocking, database and API testing) ·
[django-rest-framework](../../skills/django-rest-framework/SKILL.md) (DRF testing and debugging).
