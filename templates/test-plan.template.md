---
name: test-plan
description: >-
  Output template for a Django test plan and test report: behaviours and risks to cover, the test
  level and data for each, the run results, classified failures, coverage of changed code, files
  changed and follow-ups.
kind: output-template
agents: [django-testing-agent]
commands: [django-test]
project: django-agentic-engineering
abstract: >-
  Standard structure for planning Django tests and reporting their results, so that every
  behaviour maps to a test and every failure has an owner.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, pytest]
domain: [Software Engineering, Quality Assurance]
---

# Test Plan Template

## Purpose

Plan which behaviours and risks of a Django change are tested, at which level and with which data,
then report what ran, what failed and why. The plan part is filled before tests are written; the
results part after they run.

## When to use

- Writing tests for a new or changed behaviour, a bug fix or an untested area.
- Proposing a test plan for approval before test files are created.
- Reporting a test run, including a run of existing tests only.
- Not for load or benchmark campaigns; record those in a
  [performance assessment](performance-assessment.template.md).

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | Behaviour, change or area under test | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Code under test and what is excluded | task |
| `{{prepared_by}}` | Agent or person producing the plan | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `planned`, `executed`, `partial` or `blocked` | producer |
| `{{sources}}` | Requirements, change under test, revision | inputs |
| `{{test_runner}}` | Runner and settings module used | project configuration |
| `{{test_command}}` | Exact command used to run the tests | execution |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Strategy | required | Runner, settings, levels chosen and why, test data approach | project configuration |
| Plan | required | One row per behaviour or risk: level, test name, data | test names |
| Test data | optional | Factories or fixtures added or reused | file paths |
| Results | required | Command, passed, failed, skipped, duration | command output |
| Failures | required | Each failure classified as test, code or environment, with owner | evidence per row |
| Coverage | optional | Changed lines and branches covered, notable gaps | coverage output |
| Files changed | required | Test files created or edited; `none` for a plan | diff |
| Follow-ups | required | Defects found, flaky tests, gaps left | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Choose levels with the
  [testing strategy reference](../references/django-testing-strategy.md).
- Results come from an actual run; a plan without a run has Results `not-run`.
- A flaky test is reported with both outcomes; existing tests are never weakened to pass (`TST-M7`).
- Coverage numbers are reported only when the project measures coverage.

## Output structure

````markdown
# Test Report — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Strategy
- Runner and settings: {{test_runner}}

## Plan
| Behaviour / risk | Level | Test | Data |
|---|---|---|---|

## Test data

## Results
| Command | Passed | Failed | Skipped | Duration |
|---|---|---|---|---|
| {{test_command}} | | | | |

## Failures
| Test | Classification (test/code/env) | Evidence | Owner agent |
|---|---|---|---|

## Coverage

## Files changed

## Follow-ups
````

## Completion checklist

- [ ] Every behaviour and risk in scope maps to a planned test at a stated level.
- [ ] Results come from a real run with the exact command, or are marked `not-run`.
- [ ] Every failure is classified with evidence and an owner.
- [ ] No existing test was removed, skipped or weakened.
