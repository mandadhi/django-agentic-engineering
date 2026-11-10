---
name: django-documentation-standards
description: >-
  Mandatory Django engineering rule for documentation: docs that match the code and change with
  it, no secrets or personal data in documents, a configuration reference, endpoint-level API
  documentation, ADRs for hard-to-reverse decisions, and a reproducible setup guide. Apply when
  writing or reviewing documentation, or when a change alters documented behaviour.
project: django-agentic-engineering
abstract: >-
  Enforceable documentation standards that keep Django project documentation accurate, safe to
  share and useful to new developers.
sdlc_phase: Development
tech_stack: [Django, Django REST Framework, Markdown, OpenAPI]
domain: [Software Engineering, Documentation]
---

# Rule: Django Documentation Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Documentation must be correct, current and safe to share. Outdated documentation is worse than none.

## Scope

Applies to READMEs, `docs/`, API references, configuration references, ADRs, runbooks and
management-command help text. Docstring style follows `django-coding-standards`.

## Mandatory requirements

- **DOC-M1** Documentation states only behaviour that the code implements. Anything inferred
  or unverified is marked as such.
- **DOC-M2** A change to public behaviour updates its documentation in the same change. Public
  behaviour includes endpoints, settings, environment variables, management commands and the
  meaning of model fields.
- **DOC-M3** Documents and examples contain no secrets, real credentials, tokens, internal
  hostnames or real personal data. Use obvious placeholders such as `<your-api-token>`.
- **DOC-M4** Every environment variable or setting the application reads is listed once in a
  configuration reference, with name, purpose, whether it is required, its default, and a
  placeholder example.
- **DOC-M5** API documentation gives, for every endpoint: method and path, authentication,
  permissions, request and response shape, errors and pagination. If a schema is generated, the
  documentation is consistent with it.
- **DOC-M6** Hard-to-reverse decisions are recorded as ADRs with context, options, decision
  and consequences. Examples: architecture style, authentication model, tenancy, data ownership.
- **DOC-M7** The developer guide lets a new developer install, configure, run the project and
  run its tests by following the steps exactly.
- **DOC-M8** Documentation work changes documentation only. Behaviour changes found to be
  needed are reported, not made.

## Recommendations

- **DOC-R1** Keep documentation in the repository, next to the code it describes, in the
  project's existing tooling (Markdown, MkDocs or Sphinx).
- **DOC-R2** Use Mermaid diagrams for architecture and data flow where they render.
- **DOC-R3** Give every management command a meaningful `help` text, and keep an API changelog.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| A README describing endpoints that no longer exist | Docs updated with the change | DOC-M2 |
| A real API key in a curl example | `<your-api-token>` placeholder | DOC-M3 |
| Settings documented only in code comments | A configuration reference | DOC-M4 |
| A decision explained only in chat or ticket history | An ADR in the repository | DOC-M6 |
| "Install the usual dependencies" | Exact, reproducible steps | DOC-M7 |

## Security and maintainability

Documentation is shared more widely than code, so leaks through it are common. Keeping docs in
the repository and changing them with the code keeps them reviewable.

## Verification

Compare the documented endpoints, settings and commands against the URLconf, the settings
modules and the `management/commands` folders. Run the harness's secret patterns over the
changed documents.

## Related skills

[django-architecture](../../skills/django-architecture/SKILL.md) (architecture decision records) ·
[django-rest-framework](../../skills/django-rest-framework/SKILL.md) (API versioning and documentation) ·
[django-core](../../skills/django-core/SKILL.md) (settings, management commands).
