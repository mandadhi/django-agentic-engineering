---
name: orchestrate
description: >-
  Hand a Django or DRF engineering request to the Django Orchestrator, which picks the relevant
  specialist agents (one or a short sequence), briefs them, reconciles their results and reports
  completion, verification performed and open issues. Use when you do not want to choose a
  specialist command yourself, or for end-to-end work. Runs the django-orchestrator agent.
argument-hint: "<what you want done> [--plan-only] [--category <id>]"
project: django-agentic-engineering
abstract: >-
  Deterministic entry point of the Django Engineering Plugin's orchestration: routes one request
  through django-orchestrator according to config/orchestration-policy.yaml.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework]
domain: [Software Engineering, AI Agents]
---

# /orchestrate

## Purpose

Use for a Django request when you would rather not pick the agent yourself, or when the work
needs several specialists in order (for example *add an invoices API with its model and tests*).
The orchestrator chooses the smallest set of specialists from the routing policy.

## Arguments

`$ARGUMENTS`

- The request, in plain language. `@file` mentions are passed on as context.
- `--plan-only`: return the routing plan without running any specialist.
- `--category <id>`: skip classification and use this category of the routing policy.

With no request, ask what to do. Do not guess.

## Agent

Delegate to the `django-orchestrator` subagent with the request, the arguments and any attached
files. The orchestrator owns classification, route selection, briefs, reconciliation and the
final report; the main session does not do specialist work itself.

When the orchestrator cannot spawn subagents from where it runs, it returns a **ROUTING PLAN**
instead of results. Then the main session executes that plan:

1. Invoke each step's specialist (`django-engineering:<agent id>`) with the `Agent` tool and
   the plan's brief, in order. Start a step only after the steps it runs after have finished;
   never run two writing specialists at the same time.
2. Respect the plan's limits: no more delegations than the policy allows, at most one retry
   of a failed step, no step that depends on a failed step.
3. Report in the orchestrator's output format (outcome, route, results, verification
   performed and not performed, assumptions, unresolved issues).

Delegate only to the nine Django specialists. Never invoke `django-orchestrator` again for the
same request.

## Skills and rules

The orchestrator preloads `django-security-standards`. Each specialist preloads its own
registered skills and rules; the orchestrator adds the routing policy's supplementary skills to
a brief only when the category lists them.

## Authorization

The orchestrator is read-only. Files change only through the specialists it delegates to, each
within its own write mode and approval gates, exactly as when you call that specialist's command
directly. Commands that propose first (`/django-model`, `/django-api`, ...) keep doing so: a
specialist that needs approval to write asks for it. Destructive, network or
production-affecting operations need your explicit approval.

## Steps

1. If the request is empty, ask for it.
2. Brief `django-orchestrator`:
   - **Objective:** the request, verbatim.
   - **Constraints:** `--plan-only` and `--category` if given; any limits the user stated.
   - **Inputs:** attached files and the current working directory.
   - **Expected outputs:** the orchestrator's final report, or a ROUTING PLAN.
3. If a ROUTING PLAN came back and `--plan-only` was not given, execute it as described under
   *Agent*.
4. Relay the final report.

## Expected output

The orchestrator's report: outcome (complete, partial or not started), the route and why each
specialist was chosen, per-step results, verification performed and not performed, assumptions,
unresolved issues and recommended follow-up commands. With `--plan-only`: the routing plan.

## Completion criteria

- [ ] The route came from the routing policy, and the report says why each specialist was used.
- [ ] Every routed step ran, or is reported as skipped or failed with the reason.
- [ ] No test or check is reported as passing unless it was run.
- [ ] The result was checked against the original request.
