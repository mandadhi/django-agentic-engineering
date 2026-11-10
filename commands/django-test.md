---
name: django-test
description: >-
  Plan, write and run Django and DRF tests for a change, module, endpoint or regression, then
  report results, failures and coverage gaps. Changes test code only, after approval.
  Runs the django-testing-agent.
argument-hint: "<what to test: path, module, endpoint or change> [--base <git ref>] [--run-only] [--apply]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-testing-agent to plan risk-based tests, write them once
  authorized, run them on the test database and report results and gaps.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, pytest, pytest-django]
domain: [Software Engineering, Quality Assurance]
---

# /django-test

## Purpose

Use to get evidence that Django code works: new tests for a change or module, a regression
test for a bug, or just a run of the existing suite with a classified failure report.

## Arguments

`$ARGUMENTS`

- Free text: what to test (paths, a module, an endpoint, a bug description). Default: the
  uncommitted changes in the current repository.
- `--base <git ref>`: test what changed since that ref.
- `--run-only`: run the existing tests for the scope and report; write nothing.
- `--apply`: the user authorizes writing the planned tests without a second confirmation.
- `@file` mentions (code under test, bug reports) are passed as context.

## Agent

Delegate to the `django-testing-agent` subagent. It owns the test plan, the test style and the
failure triage; this command supplies the brief and the authorization gate. The main session
does not write or run tests itself.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-testing` · `django-core` · `django-rest-framework`
- Rules: `django-security-standards` · `django-testing-standards` · `django-coding-standards`

## Authorization

- **`--run-only`:** the agent runs existing tests and writes nothing.
- **Otherwise, plan first.** The agent returns the test plan and may run the existing tests for
  the scope; it must not change files. Show the plan and ask the user to approve it, unless
  `--apply` was given. Then brief the agent to write and run the approved tests.
- Only test code and test support files change. Application code is never changed: code
  defects are reported with the command that should fix them.
- Tests run only on the runner's test database. If the test settings point at a shared or
  non-local database, stop and report it. No package installs or network calls.

## Steps

1. Parse the scope, base ref and mode.
2. Brief the agent:
   - **Objective:** test plan for the scope (or a run of existing tests with `--run-only`).
   - **Constraints:** the mode above; runner, time budget or coverage target from the user.
   - **Inputs:** scope, base ref, `@file` context.
   - **Expected outputs:** test plan, or test report for `--run-only`.
3. Apply the authorization gate, then brief the agent to write and run the approved tests.
4. Relay the test report.

## Expected output

The test report: plan, results (command, passed, failed, skipped, duration), classified
failures with owner, coverage gaps, test files changed, follow-ups. `--run-only` returns the
results and failures only.

Template: `test-plan` for the plan and the report. Templates are in the pack at
`templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name them in the brief's expected outputs so the agent's reply
follows their structure.

## Completion criteria

- [ ] Tests were executed and the exact commands and results are reported.
- [ ] Test files changed only after approval or `--apply`, and never with `--run-only`.
- [ ] No application code changed and no existing test was weakened, skipped or removed.
- [ ] Every failure is classified (test, code or environment) with evidence.
