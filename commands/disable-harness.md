---
name: disable-harness
description: >-
  Switch the current repository back to the standalone plugin. The harness runtime, its
  configuration and the SQLite execution history are kept, so enable-harness can turn it on again.
argument-hint: "[--project <dir>] [--reset-unreadable-manifest]"
project: django-agentic-engineering
abstract: >-
  Explicit, non-destructive deactivation of the repository-local Agentic Harness.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, Claude Code]
domain: [Software Engineering, AI Agents]
---

# /django-engineering:disable-harness

## Purpose

Return this repository to standalone execution, for example while a harness problem is open.
Disabling deletes nothing.

## Arguments

`$ARGUMENTS`

- `--project <dir>`: the project directory, when it is not the current one.
- `--reset-unreadable-manifest`: only when the manifest cannot be read. It is kept as
  `installation.json.unreadable-<time>` and a disabled manifest is written.

## Runtime

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" disable [--project <dir>]
```

If `python` is not found, use `python3`, or `py -3` on Windows.

## Authorization

Changes only the manifest's enabled state. Pass `--reset-unreadable-manifest` only after telling the
user the manifest is unreadable and getting their agreement. Never delete `.django-engineering/`.

## Steps

1. Run the disable command with the user's arguments.
2. If it reports an unreadable manifest, explain the reset option and ask before using it.
3. Relay the result: `/orchestrate` now uses the standalone plugin here; the history is kept.

## Expected output

Confirmation that the repository uses the standalone plugin, and that runtime, configuration and
execution history were kept.

## Completion criteria

- [ ] No file other than the installation manifest was changed.
