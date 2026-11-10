---
name: django-review
description: >-
  Review changed Django or DRF code against the applicable engineering rules and skills before
  merge. Returns severity-ranked findings with file:line references and an APPROVE,
  APPROVE-WITH-COMMENTS or CHANGES-REQUESTED verdict. Read-only. Runs the django-code-review-agent.
argument-hint: "[--base <git ref>] [paths...] [--focus <areas>] [--threshold <severity>]"
project: django-agentic-engineering
abstract: >-
  Developer command that briefs django-code-review-agent for a read-only gate review of a Django
  change, with rule-cited findings and a merge verdict.
sdlc_phase: Testing
tech_stack: [Python, Django, Django REST Framework, Git]
domain: [Software Engineering, Quality Assurance]
---

# /django-review

## Purpose

Use before merging a Django change. The review covers the changed lines and what they affect,
cites the rule each finding violates, and ends with a verdict. It never edits anything.

## Arguments

`$ARGUMENTS`

- `--base <git ref>`: review the changes from that ref to `HEAD` (for example `main`).
- `paths...`: review these files or directories instead of, or in addition to, a diff.
- `--focus <areas>`: emphasise some dimensions (correctness, maintainability, security,
  performance, tests).
- `--threshold <severity>`: omit findings below this severity from the summary.
- `@file` mentions (PR description, acceptance criteria) are passed as context.

With no base and no paths, review the uncommitted changes in the current repository. If there
are none, ask what to review.

## Agent

Delegate to the `django-code-review-agent` subagent. It owns the review method, the per-file
choice of skills and rules, and the verdict rule; this command supplies the brief. The main
session does not review the code itself.

## Skills and rules

Tell the agent to apply these, by id, choosing per changed file as its instructions describe.
In the Django Engineering Pack they live at `skills/<id>/SKILL.md` and `rules/<id>/SKILL.md`
(installed plugin: under `${CLAUDE_PLUGIN_ROOT}`, and preloaded into the agent).

- Skills: `django-core` · `django-architecture` · `django-orm` · `django-rest-framework` · `django-security-engineering` · `django-testing` · `django-performance`
- Rules: `django-security-standards` · `django-project-structure` · `django-coding-standards` · `django-model-standards` · `django-api-standards` · `django-database-standards` · `django-testing-standards` · `django-performance-standards` · `django-configuration-standards` · `django-logging-standards` · `django-documentation-standards` · `django-dependency-standards`

## Authorization

Read-only, always. This command has no fix mode. Neither the command nor the agent edits,
commits, pushes, resets or deletes anything. To find the changed files, the main session may
run only read-only git commands (`git status --porcelain`, `git diff --stat`). Fixes are
handed to the command named by each finding's owner agent.

## Steps

1. Determine the scope: the base ref, the paths, or the uncommitted changes.
2. Brief the agent:
   - **Objective:** gate review of the scope.
   - **Constraints:** read-only; focus areas and severity threshold.
   - **Inputs:** base ref, paths, PR description or acceptance criteria.
   - **Expected outputs:** the review with verdict and findings, in the reply.
3. Relay the review. For each finding, name the command that would fix it (`/django-model`,
   `/django-api`, `/django-test`, `/django-optimize`, `/django-security`, `/django-document`).

## Expected output

The code review: verdict with a one-line reason, scope (files and the skills and rules applied
to each), mechanical check results, findings ordered by severity (id, severity, dimension,
`file:line`, issue, why, suggestion, rule id where one applies, owner), and recommended
follow-up commands.

Template: `code-review-report`. Templates are in the pack at `templates/<id>.template.md`
(installed plugin: `${CLAUDE_PLUGIN_ROOT}/templates/<id>.template.md`). Name
them in the brief's expected outputs so the agent's reply follows their structure.

## Completion criteria

- [ ] Every changed file was reviewed, or listed as not reviewed with a reason.
- [ ] Every finding has a severity, a `file:line` reference and a suggestion.
- [ ] The verdict is one of APPROVE, APPROVE-WITH-COMMENTS, CHANGES-REQUESTED.
- [ ] The workspace is unchanged.
