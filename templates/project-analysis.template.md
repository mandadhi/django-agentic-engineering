---
name: project-analysis
description: >-
  Output template for an evidence-backed profile of an existing Django project: stack and
  versions, settings, apps, interfaces, data model, background work, tests, architecture
  observations, ranked risks and unknowns.
kind: output-template
agents: [django-project-analyzer]
commands: [django-analyze]
project: django-agentic-engineering
abstract: >-
  Standard structure for a read-only Django project profile that downstream agents can start from,
  with every fact tied to evidence and every gap marked unknown.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, AI Agents]
---

# Project Analysis Template

## Purpose

Record what an existing Django codebase actually contains, so that design, model, API, security,
testing and performance work start from facts. The profile describes; it does not recommend a
redesign.

## When to use

- First contact with an unfamiliar Django repository, or after a large change to one.
- Before an architecture design, an upgrade assessment or a security assessment that needs a map.
- Not for a single focused question that needs one paragraph; answer that directly and cite the evidence.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | Project or repository being profiled | task objective |
| `{{project_name}}` | Name of the Django project | `manage.py` location, settings package |
| `{{scope}}` | Paths included and excluded, sampling used | task constraints |
| `{{prepared_by}}` | Agent or person producing the profile | producer |
| `{{date}}` | Date of the analysis (ISO 8601) | producer |
| `{{document_status}}` | `complete`, `partial` or `blocked` | producer |
| `{{sources}}` | Revision or commit analysed, if known | `git log -1` or `unknown` |
| `{{summary}}` | Three to five sentences on what the project is and its main risks | analysis |
| `{{settings_selection}}` | How the settings module is chosen | `manage.py`, `wsgi.py`, `asgi.py` |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Summary | required | What the project is, its shape, top risks | the sections below |
| Stack | required | Python, Django, DRF, database and key packages with versions | lock file or manifest line |
| Settings | required | Modules, selection mechanism, environment-sourced setting names | `file:line` |
| Apps | required | One row per project app | paths in the row |
| Interfaces | required | URL tree, APIs, authentication, admin, webhooks | URLconf `file:line` |
| Data model | optional | Key relationships; Mermaid `erDiagram` if useful | model `file:line` |
| Background work and integrations | required | Task queues, schedulers, caches, storage, outbound clients | settings and import evidence |
| Tests and tooling | required | Runner, test settings, factories, CI, linters, type checks | config files |
| Architecture observations | required | Layering, cross-app imports, where business logic lives | `file:line` |
| Risks | required | Ranked risks with evidence and the agent that should own each | `file:line` |
| Unknowns and limitations | required | What could not be established, sampling, unreadable paths | — |
| Machine-readable summary | optional | The fenced JSON summary defined by the producing agent | JSON parses |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Versions come from lock files
  before manifests; see the [project discovery reference](../references/django-project-discovery.md).
- Describe the code as it is. Rule deviations go under Risks with the requirement id (for example
  `CFG-M2`), not as fixes.
- Settings and credentials appear by name and location only.

## Output structure

````markdown
# Django Project Profile — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Summary
{{summary}}

## Stack
| Component | Version | Evidence |
|---|---|---|

## Settings
Selection: {{settings_selection}}

| Module | Selected by | Notes |
|---|---|---|

## Apps
| App | Models | Interfaces | Services/tasks | Tests | Depends on |
|---|---|---|---|---|---|

## Interfaces

## Data model

## Background work and integrations
| Kind | Technology | Configured in | Used by |
|---|---|---|---|

## Tests and tooling
| Concern | Tool | Evidence |
|---|---|---|

## Architecture observations
| Observation | Evidence |
|---|---|

## Risks
| # | Severity | Area | Finding | Evidence | Suggested agent |
|---|---|---|---|---|---|

## Unknowns and limitations

## Machine-readable summary
````

## Completion checklist

- [ ] Every required section is filled or explicitly marked `unknown` with the reason.
- [ ] Every version and every risk cites a file and line or a command result.
- [ ] Inferred facts are marked `inferred`; nothing is stated without evidence.
- [ ] No secret value, hostname or credential appears in the profile.
