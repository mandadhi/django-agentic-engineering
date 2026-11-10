---
name: django-document
description: >-
  Generate or update technical documentation for a Django or DRF project from its actual code:
  architecture overview, app docs, API reference, setup and configuration guides, runbooks.
  Changes documentation files only. Runs the django-documentation-agent.
argument-hint: "<documents and audience> [--paths <code paths>] [--out <docs dir>] [--dry-run]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-documentation-agent to write code-derived documentation,
  restricted to documentation files and verified against the code.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, Markdown, OpenAPI, Mermaid]
domain: [Software Engineering, Documentation]
---

# /django-document

## Purpose

Use to create documentation that matches the code, or to bring existing documentation back in
line after a change. Every statement is traced to the code it describes.

## Arguments

`$ARGUMENTS`

- Free text (required): which documents, for which audience (for example "API reference for
  the orders app, for frontend developers").
- `--paths <code paths>`: the code to document. Default: inferred from the request.
- `--out <docs dir>`: where documentation goes. Default: the project's existing docs layout.
- `--dry-run`: return the documents in the reply and write nothing.
- `@file` mentions (existing docs, change reports) are passed as context.

## Agent

Delegate to the `django-documentation-agent` subagent. It owns fact gathering, structure and
verification; this command supplies the brief and the documentation-only boundary. The main
session does not write documentation itself.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-core` · `django-architecture` · `django-rest-framework`
- Rules: `django-security-standards` · `django-documentation-standards`

## Authorization

- **Documentation changes only.** The agent may create or edit documentation files
  (`docs/`, `README*`, other `*.md`, and the `--out` directory). It never changes application
  code, settings, migrations or tests. Docstrings change only if the user asks for them by name.
- A request that needs a code change is out of scope: say so and point to the right command
  (`/django-model`, `/django-api`, `/django-test`).
- `--dry-run`: nothing is written.
- If the result lists any changed file that is not documentation, report it to the user as a
  boundary violation. Do not revert anything automatically.
- No secret values, real credentials, internal hostnames or personal data in any document.

## Steps

1. Parse the documents, audience, code paths, output location and `--dry-run`.
2. Brief the agent:
   - **Objective:** the documents to create or update, and the audience.
   - **Constraints:** documentation files only; location; `--dry-run` if given.
   - **Inputs:** code paths and `@file` context.
   - **Expected outputs:** the documents and a documentation report.
3. Check the reported file list against the documentation-only boundary.
4. Relay the documentation report.

## Expected output

The documentation report: each document with its action (created or updated), audience and
code sources; statements that could not be verified; open questions. With `--dry-run`, the
document content itself.

Template: `technical-documentation` for each document and the report. Templates are in the pack
at `templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name them in the brief's expected outputs so the agent's reply
follows their structure.

## Completion criteria

- [ ] Every requested document exists (or is returned, with `--dry-run`).
- [ ] Only documentation files changed.
- [ ] Endpoints, settings, commands and paths in the documents were checked against the code.
- [ ] Unverified statements and open questions are listed.
