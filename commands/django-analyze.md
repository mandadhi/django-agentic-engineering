---
name: django-analyze
description: >-
  Profile an existing Django project without changing it: Python, Django, DRF and database
  versions, settings, apps, models, dependencies, interfaces and ranked architectural risks.
  Runs the django-project-analyzer agent.
argument-hint: "[project path | focus question] [--paths <glob,...>] [--exclude <glob,...>]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-project-analyzer to produce an evidence-backed, read-only
  profile of a Django codebase for the other Django commands to start from.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, AI Agents]
---

# /django-analyze

## Purpose

Run this first on an unfamiliar or recently changed Django codebase, or before `/django-design`,
so that later work starts from facts. It answers either "profile the whole project" or one
focused question such as "how is authentication wired?".

## Arguments

`$ARGUMENTS`

- Free text: the project root (default: the current directory) or a focused question.
- `--paths <glob,...>` / `--exclude <glob,...>`: limit what is profiled.
- `@file` mentions are passed to the agent as starting context.

With no arguments, profile the Django project in the current directory.

## Agent

Delegate to the `django-project-analyzer` subagent. The agent owns the method and the profile
format; this command only builds its brief and relays the result. Do not do the analysis in the
main session, and do not fall back to it if the agent is unavailable: stop and say the agent
is not installed.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-core` · `django-architecture` · `django-orm`
- Rules: `django-security-standards` · `django-project-structure` · `django-configuration-standards` · `django-dependency-standards`

Rule deviations are reported as risks, not fixed.

## Authorization

Read-only. Neither this command nor the agent creates, edits or deletes files. No migrations,
servers, database connections, package installs or network calls. Secret values are never
shown; settings and credentials are named with their location only.

## Steps

1. Parse the arguments into the scope (root, paths, exclusions) and the question, if any.
2. Brief the agent:
   - **Objective:** full profile, or the focused question.
   - **Constraints:** the scope above; read-only.
   - **Inputs:** the project root and any `@file` context.
   - **Expected outputs:** the project profile, returned in the agent's reply.
3. Relay the profile. Name the follow-up command each high risk points to (`/django-design`,
   `/django-security`, `/django-optimize`).

## Expected output

The agent's Django project profile (stack, settings, apps, interfaces, data model, background
work, tests and tooling, architecture observations, ranked risks, unknowns) with its JSON
summary, followed by a short list of suggested next commands.

Template: `project-analysis`. Templates are in the pack at `templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name
them in the brief's expected outputs so the agent's reply follows their structure.

## Completion criteria

- [ ] The profile covers every section, or marks it `unknown` with a reason.
- [ ] Versions and risks cite `file:line` evidence.
- [ ] The workspace is unchanged and no secret value appears in the output.
- [ ] Suggested next commands are listed for the high-severity risks.
