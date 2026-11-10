---
name: enable-harness
description: >-
  Turn the Django Agentic Harness back on for the current repository after it was disabled.
  Verifies the runtime snapshot, virtual environment, dependencies, configuration and execution
  database first, and enables it only if every check passes.
argument-hint: "[--project <dir>]"
project: django-agentic-engineering
abstract: >-
  Validated, explicit re-activation of an existing repository-local harness installation.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, Claude Code]
domain: [Software Engineering, AI Agents]
---

# /django-engineering:enable-harness

## Purpose

Route `/orchestrate` in this repository through the Agentic Harness again. For a repository that
has no installation yet, use `/django-engineering:setup-harness` instead.

## Arguments

`$ARGUMENTS`

- `--project <dir>`: the project directory, when it is not the current one.

## Runtime

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" enable [--project <dir>]
```

If `python` is not found, use `python3`, or `py -3` on Windows.

## Authorization

Enabling changes only the installation manifest of this repository, and only after validation
passed. Run it only when the user asked to enable the harness.

## Steps

1. Run the enable command with the user's arguments.
2. Relay every check. If it failed, nothing changed: give the remedies and suggest
   `/django-engineering:doctor`. If it succeeded, say that `/orchestrate` now uses the harness here.

## Expected output

The validation checks and either "harness enabled" or the reason it stayed disabled.

## Completion criteria

- [ ] The harness was enabled only when the script reported success.
