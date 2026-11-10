---
name: django-api
description: >-
  Design and implement Django REST Framework endpoints: serializers, views and routes,
  authentication, permissions, scoped querysets, pagination and API tests. Proposes the
  endpoint contract first and writes only after approval. Runs the django-api-agent.
argument-hint: "<resources and operations> [--design <file>] [--version <api version>] [--apply]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-api-agent to define endpoint contracts and, once
  authorized, implement and test default-deny DRF endpoints in the project's API style.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, OpenAPI]
domain: [Software Engineering, APIs]
---

# /django-api

## Purpose

Use to add or change DRF endpoints. The contract (paths, auth, permissions, payloads, errors,
compatibility) is agreed before code is written, and the result is proven by tests that cover
the access matrix.

## Arguments

`$ARGUMENTS`

- Free text (required): the resources, operations and consumers.
- `--design <file>`: a `/django-design` result to follow.
- `--version <api version>`: the target API version, when the project versions its API.
- `--apply`: the user authorizes writing the proposed changes without a second confirmation.
- `@file` mentions (existing views, serializers, specs) are passed as context.

If the auth scheme or the permission rules are unclear, ask before delegating.

## Agent

Delegate to the `django-api-agent` subagent. It owns the contract, the implementation order and
the tests; this command supplies the brief and the authorization gate. The main session does
not write API code itself. Missing models go to `/django-model` first.

## Skills and rules

Tell the agent to apply these, by id. In the Django Engineering Pack they live at
`skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-rest-framework` · `django-security-engineering` · `django-orm`
- Rules: `django-security-standards` · `django-api-standards` · `django-coding-standards` · `django-testing-standards` · `django-performance-standards` · `django-logging-standards`

## Authorization

Code changes need explicit approval.

1. **Proposal (always first).** The agent studies the existing API and returns the endpoint
   contract, the access matrix and the compatibility impact. It must not change files.
2. **Approval.** Show the proposal and ask the user to approve it, unless `--apply` was given.
   A breaking change always needs the user's explicit approval, even with `--apply`.
3. **Implementation.** Brief the agent with the approved contract. It writes API code and
   tests in the workspace and runs them.

Never: package installs, network calls, or migrations against a non-test database.

## Steps

1. Parse the request, design, version and `--apply`.
2. Brief the agent for the proposal:
   - **Objective:** the requested endpoints.
   - **Constraints:** proposal only; auth scheme, versioning and compatibility from the user.
   - **Inputs:** design, profile and `@file` context.
   - **Expected outputs:** endpoint contract, access matrix, compatibility impact.
3. Apply the authorization gate above.
4. Brief the agent to implement the approved contract and verify it.
5. Relay the API change report.

## Expected output

Proposal: the endpoint table, access matrix and compatibility notes. After implementation: the
API change report (endpoints, access matrix, compatibility, files changed, test and schema
validation results, open questions).

Template: `api-specification` for the proposal and the report. Templates are in the pack at
`templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name them in the brief's expected outputs so the agent's reply
follows their structure.

## Completion criteria

- [ ] The contract was shown before any file changed.
- [ ] Files changed only after the user's approval or `--apply`; no silent breaking change.
- [ ] Every endpoint has explicit authentication, permission and a scoped queryset.
- [ ] The access-matrix tests pass, or each failure is reported with its owner.
