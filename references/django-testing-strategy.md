---
name: django-testing-strategy
description: >-
  Shared reference for choosing which Django tests a change needs: the minimum tests per kind of
  change, the test level and Django test tool for each, test data and isolation choices, and how
  to classify failures.
kind: shared-reference
agents: [django-testing-agent, django-api-agent, django-code-review-agent]
project: django-agentic-engineering
abstract: >-
  A change-to-tests matrix for Django and DRF, test level and tool selection, isolation decisions
  and a failure classification, linked to the testing skill and rule.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, pytest]
domain: [Software Engineering, Quality Assurance]
---

# Django Testing Strategy

## Purpose

Decide quickly and consistently which tests a Django change needs, at which level, with which
Django test tool, so that test plans written by different agents and reviews of them agree on
what "tested" means.

## Scope

Automated tests of Django and DRF code run by the project's own runner against the test
database. Browser end-to-end and load testing are mentioned only as escalation points.

## How to use

1. Find each kind of change in the matrix and take its minimum tests.
2. Choose the level and tool with the selection table.
3. Record the plan in the [test plan](../templates/test-plan.template.md) template.
4. A reviewer uses the same matrix to decide whether coverage of a change is sufficient.

## Minimum tests per change

| Change | Minimum tests | Usual level |
|---|---|---|
| New or changed model constraint | One test that violates it and expects `IntegrityError` or `ValidationError`; one that satisfies it | database |
| New manager or QuerySet method | Results for matching and non-matching rows; query count if used in lists | database |
| Data migration | Forward on representative rows; reverse if reversible | migration (database) |
| New or changed view or endpoint | Success path, validation failure, and the access matrix: anonymous, other user, owner, privileged | request (client) |
| Serializer change | Valid input, each invalid field, read-only fields ignored on write, output shape | unit or request |
| List endpoint or listing page | Pagination bounds and a query-count assertion | request |
| Permission or scoping change | Each role sees exactly the allowed objects; out-of-scope object returns 404 | request |
| Service or business rule | Each rule outcome and each error branch; side effects asserted | unit or database |
| Code with `transaction.on_commit` | Callback runs after commit and not on rollback | database |
| Background task | Task logic called directly; enqueuing asserted at the call site | unit or database |
| Outbound integration | Client mocked at the boundary: success, timeout, error response | unit |
| Bug fix | A regression test that fails before the fix | level of the defect |
| Settings or middleware change | Behaviour under `override_settings` for each relevant value | request |

The matrix is the minimum that `TST-M1` (tests with every behaviour change), `TST-M5` (the access
matrix) and `TST-M8` (commit-dependent code) imply for each change; the rule remains the authority.

## Choosing level and tool

| Need | Django tool (unittest style) | pytest-django equivalent |
|---|---|---|
| No database | `SimpleTestCase` | test without the `db` fixture |
| Database, rolled back per test | `TestCase` | `@pytest.mark.django_db` |
| Real commits, multiple connections, or database-level transaction behaviour | `TransactionTestCase` | `@pytest.mark.django_db(transaction=True)` |
| HTTP request through URLconf and middleware | `self.client`, DRF `APIClient` | `client` fixture, `APIClient` |
| Query count | `assertNumQueries` | `django_assert_num_queries` |
| On-commit callbacks | `captureOnCommitCallbacks` | `django_capture_on_commit_callbacks` |
| Settings variation | `override_settings` | `settings` fixture |
| Shared read-only data per class | `setUpTestData` | fixture with class or module scope and care with mutation |

Use the runner and style the project already uses. Mixing styles in one project needs a reason.

## Isolation and data decisions

| Decision | Default | Escalate when |
|---|---|---|
| Test data | Project factories, else small explicit objects | A shared fixture is already the project's convention |
| Time | Frozen or injected clock where results depend on time | — |
| External services | Mocked at the project's client wrapper | A contract test exists for that integration |
| Email and messages | Django's locmem email backend | — |
| Caches | Local memory cache in test settings, cleared between tests that use it | Behaviour depends on the production backend |
| Randomness | Seeded or asserted by property, not by value | — |

These defaults serve `TST-M2` (test database only), `TST-M3` (no real network or messages),
`TST-M4` (deterministic, independent tests) and `TST-M6` (synthetic data).

## Failure classification

| Class | Meaning | Owner |
|---|---|---|
| test | The test is wrong (bad setup, wrong expectation, order dependence) | the test author |
| code | The code violates the expected behaviour | the agent owning that code |
| env | The environment prevents running (imports, settings, test database) | the human or the project setup |
| flaky | Different outcomes on reruns without changes | the test author, with both outcomes recorded |

## Related skills and rules

- Skills: [django-testing](../skills/django-testing/SKILL.md)
  ([framework](../skills/django-testing/references/django-testing-framework.md),
  [pytest-django](../skills/django-testing/references/pytest-and-pytest-django.md),
  [database and transactions](../skills/django-testing/references/database-and-transaction-testing.md),
  [DRF testing](../skills/django-testing/references/django-rest-framework-testing.md),
  [mocking](../skills/django-testing/references/mocking-and-dependency-isolation.md)),
  [django-rest-framework](../skills/django-rest-framework/SKILL.md).
- Rules: [django-testing-standards](../rules/django-testing-standards/SKILL.md),
  [django-security-standards](../rules/django-security-standards/SKILL.md).
