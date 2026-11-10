---
name: django-design
description: >-
  Design Django application boundaries and architecture for a feature, refactoring or new
  system: app ownership, layering, dependency and security boundaries, ADRs and an ordered
  implementation plan. Runs the django-architecture-agent; scaffolds only on request.
argument-hint: "<feature, refactoring or system to design> [--profile <file>] [--out <dir>] [--scaffold]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-architecture-agent to turn requirements into ADRs and an
  agent-by-agent implementation plan, with opt-in, confirmed structural scaffolding.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework, Mermaid]
domain: [Software Engineering, AI Agents]
---

# /django-design

## Purpose

Use when a change needs structural decisions before anyone writes code: which app owns what,
how layers depend on each other, where permissions, transactions and integrations sit, and in
what order the other Django commands should build it.

## Arguments

`$ARGUMENTS`

- Free text (required): the feature, refactoring or system, with its requirements and
  constraints.
- `--profile <file>`: an existing `/django-analyze` profile to start from.
- `--out <dir>`: write the design documents there. Without it, the design is returned in the
  reply and no file is written.
- `--scaffold`: also create the empty package structure, after the user approves the design.
- `@file` mentions (requirements, tickets, existing ADRs) are passed as context.

If no requirements are given, ask for them before delegating.

## Agent

Delegate to the `django-architecture-agent` subagent. It owns the design method, the ADR format
and the plan format; this command supplies the brief and the authorization gate. The main
session does not design or scaffold anything itself.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-core` · `django-architecture` · `django-security-engineering`
- Rules: `django-security-standards` · `django-project-structure` · `django-configuration-standards` · `django-logging-standards` · `django-documentation-standards` · `django-dependency-standards`

## Authorization

- **Default:** design only. The agent may read the code but must not change the workspace.
- `--out <dir>`: the agent may write design documents in that directory, nothing else.
- `--scaffold`: show the design first, list the paths that would be created, and ask the user
  to approve. Only then brief the agent to scaffold. Scaffolding is empty packages and modules
  with no business logic, and never overwrites an existing file.
- Models, migrations, APIs and tests are out of scope; the plan hands them to the other commands.

## Steps

1. Parse the requirements, the profile, the output directory and the scaffold flag.
2. Brief the agent:
   - **Objective:** the design subject.
   - **Constraints:** quality attributes and limits from the user; the authorization above.
   - **Inputs:** requirements, profile and `@file` context.
   - **Expected outputs:** the architecture design (in the reply, or in `--out`).
3. Relay the design. If the agent returns open questions, put them to the user before any
   scaffolding.
4. With `--scaffold` and the user's approval, brief the agent again to scaffold the approved
   structure and list every created path.
5. Map each plan item to the command that implements it (`/django-model`, `/django-api`,
   `/django-test`, `/django-document`).

## Expected output

The architecture design (requirements, current state, ADRs, target structure, dependency and
security boundaries, component diagram, implementation plan, risks and open questions), the
list of written or scaffolded paths if any, and the next command for each plan item.

Templates: `architecture-design` for the design, `architecture-decision-record` for each
decision written as its own file, and `implementation-plan` when the plan is delivered
separately. Templates are in the pack at `templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name them in the brief's
expected outputs so the agent's reply follows their structure.

## Completion criteria

- [ ] Every requirement maps to an owning app and a plan item.
- [ ] Each significant decision has an ADR with the alternatives considered.
- [ ] No file was written outside `--out`, and scaffolding happened only after approval.
- [ ] Each plan item names the command that will implement it.
