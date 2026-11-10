---
name: harness-status
description: >-
  Show whether the current repository uses the standalone plugin or the Django Agentic Harness:
  execution mode, installation status, harness and plugin versions, Python environment, execution
  database, profile and runtime readiness. Read-only.
argument-hint: "[--project <dir>]"
project: django-agentic-engineering
abstract: >-
  Read-only status of the repository-local Agentic Harness integration and the execution mode the
  resolver selects for this repository.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, Claude Code]
domain: [Software Engineering, AI Agents]
---

# /django-engineering:harness-status

## Purpose

Answer "what will `/orchestrate` use here, and is it healthy?" without changing anything.

## Arguments

`$ARGUMENTS`

- `--project <dir>`: inspect another directory than the current one.

## Runtime

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" status [--project <dir>]
```

If `python` is not found, use `python3`, or `py -3` on Windows. If no interpreter runs at all,
report that the harness status cannot be determined; do not guess.

## Authorization

Read-only. It never creates, repairs or enables anything.

## Steps

1. Run the status command with the user's arguments.
2. Relay the result. For each listed problem, give the remedy it names. In standalone mode, say
   that setup is optional (`/django-engineering:setup-harness`).

## Expected output

Repository, execution mode (`standalone`, `harness_enabled`, `harness_disabled` or
`harness_invalid`) and what `/orchestrate` routes to, installation status, harness version,
Python environment, database, profile and runtime readiness.

## Completion criteria

- [ ] The status shown is the script's output, not an inference.
- [ ] Every problem is reported with its remedy.
