---
name: security-assessment
description: >-
  Output template for a Django or DRF security assessment: risk verdict, scope and attack surface,
  findings with evidence, impact and recommendation, configuration checklist results, fixes
  applied in remediation mode, and accepted risks.
kind: output-template
agents: [django-security-agent]
commands: [django-security]
project: django-agentic-engineering
abstract: >-
  Standard structure for an evidence-based Django security assessment in which every finding is
  located, confirmed and actionable, and no secret value is reproduced.
sdlc_phase: Security
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, Security]
---

# Security Assessment Template

## Purpose

Report the security posture of a Django project, app or change: what is exposed, which weaknesses
were confirmed, how severe they are, how to fix them and what was checked without findings.

## When to use

- A focused or full security review of a Django or DRF codebase, a release candidate or a change.
- Remediation work, to record which findings were fixed and the test proving each fix.
- Not for a general merge review; use the [code review report](code-review-report.template.md),
  which can recommend a full assessment.

## Placeholders

| Placeholder | Meaning | Source |
|---|---|---|
| `{{subject}}` | System, app or change assessed | task objective |
| `{{project_name}}` | Name of the Django project | project profile |
| `{{scope}}` | Apps, paths and vulnerability classes in scope and excluded | task |
| `{{prepared_by}}` | Agent or person producing the assessment | producer |
| `{{date}}` | Date (ISO 8601) | producer |
| `{{document_status}}` | `complete`, `partial` or `blocked` | producer |
| `{{sources}}` | Revision assessed, project profile | inputs |
| `{{risk_verdict}}` | Overall verdict with one-line reason | findings |
| `{{assessment_mode}}` | `review` or `remediation` | task |

## Sections

| Section | Required | Content | Verifiable by |
|---|---|---|---|
| Summary and risk verdict | required | Overall risk, counts by severity, the most important actions | Findings table |
| Scope and attack surface | required | Entry points, trust boundaries, roles, data classes, exclusions | URLconf, settings |
| Findings | required | Confirmed findings ordered by severity | `file:line` and evidence per finding |
| Configuration checklist results | required | Result per configuration check, including passes | `check --deploy` output or settings `file:line` |
| Checks without findings | optional | Vulnerability classes examined that produced no finding | — |
| Fixes applied | optional | Remediation mode only: change and proving test per fixed finding | test name and result |
| Accepted risks and limitations | required | Risks accepted by the owner, unreadable paths, checks not run | — |

## Filling rules

- Apply the [filling conventions](README.md#filling-conventions). Work through the
  [security checklist](../references/django-security-checklist.md) and record a result per row.
- A finding is reported only after tracing input to sink or request to object access; anything
  unconfirmed is `needs-verification`, not a finding.
- Hard-coded secrets are reported by location and type only, with rotation recommended (`SEC-A2`).
- Cite the violated requirement id where one applies (for example `SEC-M4`, `API-M2`).

## Output structure

````markdown
# Security Assessment — {{subject}}

| Field | Value |
|---|---|
| Project | {{project_name}} |
| Scope | {{scope}} |
| Prepared by | {{prepared_by}} |
| Date | {{date}} |
| Status | {{document_status}} |
| Sources | {{sources}} |

## Summary and risk verdict
- Mode: {{assessment_mode}}
- Verdict: {{risk_verdict}}

| Severity | Count |
|---|---|
| critical | |
| high | |
| medium | |
| low | |
| info | |

## Scope and attack surface
| Entry point | Authentication | Trust boundary | Data handled |
|---|---|---|---|

## Findings
| Id | Severity | Category | Location | Evidence | Impact | Recommendation | Rule | Status |
|---|---|---|---|---|---|---|---|---|

## Configuration checklist results
| Check | Result (pass/fail/not-run/n/a) | Evidence |
|---|---|---|

## Checks without findings

## Fixes applied
| Finding | Change | Test | Result |
|---|---|---|---|

## Accepted risks and limitations
````

## Completion checklist

- [ ] Every vulnerability class in scope was checked, or explicitly excluded with a reason.
- [ ] Every finding has a location, evidence, impact, severity, recommendation and status.
- [ ] Configuration checks record passes as well as failures.
- [ ] In remediation mode, every fixed finding has a passing test that failed before the fix.
- [ ] No secret value appears anywhere in the report.
