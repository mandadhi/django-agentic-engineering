---
name: technical-documentation
description: >-
  Output template for code-derived Django technical documentation (architecture overview, app or
  module guide, API reference, developer guide, configuration reference, runbook) and the
  documentation report that accompanies it.
kind: output-template
agents: [django-documentation-agent]
commands: [django-document]
project: django-agentic-engineering
abstract: >-
  Standard structure for Django documentation in which every statement traces to code, plus a
  change report listing documents, unverified items and open questions.
sdlc_phase: Development
tech_stack: [Python, Django, Django REST Framework, Markdown, OpenAPI, Mermaid]
domain: [Software Engineering, Documentation]
---

# Technical Documentation Template

## Purpose

Give documentation produced from a Django codebase a predictable shape: who it is for, what it
covers, the content organised by the reader's task, where each fact comes from, and a report of
what was written and what could not be verified.

## When to use

- Writing or updating an architecture overview, app or module guide, API reference, developer
  guide, configuration reference or runbook for a Django project.
- Not for recording a new design decision; use the
  [architecture decision record](architecture-decision-record.template.md). Documentation may link to it.
- Not for an API change proposal; use the [API specification](api-specification.template.md).
- When the project already has a documentation layout, keep its file structure and use this
  template for the content of each page.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | What is being documented | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Apps, modules or endpoints covered | task |
| `{{prepared_by}}` | Agent or person producing the documentation | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `draft`, `complete` or `partial` | producer |
| `{{sources}}` | Revision documented, schema file if used | inputs |
| `{{document_type}}` | `architecture-overview`, `module-guide`, `api-reference`, `developer-guide`, `configuration-reference` or `runbook` | task |
| `{{audience}}` | The reader and the task they are trying to do | task |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Overview | required | What the subject is and does, in a few sentences | code |
| Audience and prerequisites | required | Who the document is for and what they need first | — |
| Content | required | The body, organised by the reader's task (see the type guide below) | `file:line` per fact |
| Configuration | optional | Settings and environment variables by name and purpose, never value | settings source |
| Diagrams | optional | Mermaid diagrams where the project renders them | matches code |
| Documents | required | Report: each file created or updated, its audience and sources | file paths |
| Unverified items | required | Statements that could not be traced to code, or `none` | — |
| Open questions | required | Unclear intent, code and documentation disagreements | — |

Content by document type:

| Type | Content subsections |
|---|---|
| architecture-overview | apps and responsibilities, layering, data flow, integrations |
| module-guide | models and relationships, services, tasks, signals, management commands |
| api-reference | per endpoint: method and path, authentication, permissions, request, response, errors, pagination |
| developer-guide | install, configure, run, test, common workflows |
| configuration-reference | one row per setting or variable: name, purpose, required, default behaviour |
| runbook | symptom, diagnosis steps, remedy, escalation, verification |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). State only behaviour the code
  implements (`DOC-M1`); anything not traceable goes under Unverified items.
- Examples use clearly fake placeholders such as `<your-api-token>`; no real hosts or personal data.
- The Documents, Unverified items and Open questions sections form the report returned with the
  documents; they are not written into the published page.

## Output structure

````markdown
# {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |
| Type | {{document_type}} |

## Overview

## Audience and prerequisites
{{audience}}

## Content

## Configuration
| Setting or variable | Purpose | Required | Source |
|---|---|---|---|

## Diagrams

# Documentation Report — {{subject}}

## Documents
| File | Action (created/updated) | Audience | Sources |
|---|---|---|---|

## Unverified items

## Open questions
````

## Completion checklist

- [ ] Every factual statement traces to code; untraceable statements are listed as unverified.
- [ ] Endpoints, settings, commands and paths were checked against the code.
- [ ] No secret, credential, internal hostname or personal data appears.
- [ ] Every created or updated file is listed in the report.
