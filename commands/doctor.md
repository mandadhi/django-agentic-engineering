---
name: doctor
description: >-
  Diagnose the Django Agentic Harness integration for the current repository: Python, virtual
  environment, pinned dependencies, installation manifest, runtime snapshot and version
  compatibility, configuration, execution database, credentials prerequisites, OS sandbox and
  optional plugins. Read-only; every problem comes with the action that fixes it.
argument-hint: "[--project <dir>] [--python <exe>]"
project: django-agentic-engineering
abstract: >-
  Read-only diagnosis of the opt-in harness integration with actionable remedies; it never repairs.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, Claude Code]
domain: [Software Engineering, AI Agents]
---

# /django-engineering:doctor

## Purpose

Find out why setup failed, why `/orchestrate` reports the harness as invalid, or whether an
installation is healthy. Use the namespaced form; the bare `/doctor` is Claude Code's own command.

## Arguments

`$ARGUMENTS`

- `--project <dir>`: the project directory, when it is not the current one.
- `--python <exe>`: check this interpreter instead of the one that would be detected.

## Runtime

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" doctor [--project <dir>]
```

If `python` is not found, use `python3`, or `py -3` on Windows; if none runs, report that Python
3.11+ is missing (the harness needs it; the standalone plugin's guard needs a `python` command too).

## Authorization

Read-only. Do not repair, delete or overwrite anything on your own. Offer the remedy the report
names (usually a harness command); run it only if the user agrees.

## Steps

1. Run the doctor command with the user's arguments.
2. Group the findings: failures first, then warnings. Give each one's remedy.
3. Say whether the repository is healthy, and which mode `/orchestrate` will use.

## Expected output

Each check with `ok`, `warn` or `fail`, the remedies, and an overall verdict.

## Completion criteria

- [ ] Nothing was changed.
- [ ] Every failure is reported with an actionable remedy.
