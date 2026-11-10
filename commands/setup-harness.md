---
name: setup-harness
description: >-
  Opt in to the Django Agentic Harness for the current repository. Detects the project, checks
  Python 3.11+, venv, pip, disk space and credentials, shows a setup plan and, only after you
  approve it, installs a repository-local runtime in .django-engineering/ (own virtual environment,
  pinned dependencies, configuration, execution database), validates it and enables it here.
argument-hint: "[--project <dir>] [--profile pack|full] [--python <exe>] [--upgrade] [--new-database]"
project: django-agentic-engineering
abstract: >-
  Opt-in, consent-bound initialisation of the existing Python Agentic Harness inside the user's
  repository; the standalone plugin never needs it.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django, Claude Code]
domain: [Software Engineering, AI Agents]
---

# /django-engineering:setup-harness

## Purpose

The plugin works without this command. Run it only if you want `/orchestrate` in this repository
to execute through the Agentic Harness (persistent executions, checkpoints and recovery, approval
gates, run records, the harness's own tool guard). Setup is per repository and never global.

## Arguments

`$ARGUMENTS`

- `--project <dir>`: the project directory, when it cannot be detected with confidence.
- `--profile pack|full`: `pack` (default) runs the Django Engineering Pack alone; `full` also
  requires the optional parent-repository plugins, which are never copied into your project.
- `--python <exe>`: the interpreter that creates the harness environment (3.11+).
- `--upgrade`: approve upgrading an installed runtime to this plugin's version.
- `--new-database`: approve starting an empty execution history when a previous database is missing.

## Runtime

Every step runs the plugin's bootstrap script; call it exactly like this (if `python` is not
found, use `python3`, or `py -3` on Windows):

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" <subcommand> [arguments]
```

## Authorization

The plan step changes nothing. Setup modifies the repository only after the user approves the
exact plan shown: never pass `--approve-plan` with a plan id the user has not approved in this
conversation, never add `--upgrade` or `--new-database` unless the user asked for it, and never
edit, delete or commit anything in `.django-engineering/` yourself.

## Steps

1. Run `plan` with the arguments the user gave (add `--json` if you need to parse it).
   - Exit 10 (project not confirmed): show the candidates and ask the user which directory is the
     project, then plan again with `--project <dir>`.
   - Exit 11 (blocked): show every blocker with its remedy and stop. When the plan says `upgrade`,
     explain that the runtime would be upgraded and ask whether to plan again with `--upgrade`.
   - Exit 0: continue.
2. Show the plan to the user: project, harness version, profile, Python, checks, warnings and
   every action. Ask for explicit approval (for example with AskUserQuestion). If the user declines,
   stop; nothing was changed.
3. Run `setup --approve-plan <plan_id>` with the same arguments. Installing the pinned
   dependencies can take several minutes: run it as a background command and wait for it to finish.
   - Exit 12 (plan changed): show the new plan and ask again.
   - Exit 1: relay the failing step and the setup log path; suggest `/django-engineering:doctor`.
4. Run `status` and report the result.

## Expected output

The approved plan, the outcome of each setup step, and the final status: execution mode
`harness_enabled` for this repository, or the step that failed and how to fix it. When the
repository was already set up, the validation result (nothing changed).

## Completion criteria

- [ ] Nothing was written before the user approved the plan shown.
- [ ] The outcome reported is the script's actual result; failures are not described as success.
- [ ] The user knows how to check (`harness-status`), disable (`disable-harness`) and diagnose (`doctor`).
