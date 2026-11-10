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

## Execution mode

Before routing, ask the plugin's execution-mode resolver which mode this repository is in. It
only reads `.django-engineering/installation.json`; it never starts or installs the harness:

```
python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" mode --json
```

- `standalone` or `harness_disabled`: continue with *Agent* below (standalone plugin, unchanged).
- `harness_enabled`: follow *Harness mode* instead.
- `harness_invalid` (exit 13): stop. Report each problem and its remedy (`/django-engineering:doctor`,
  `setup-harness`, or `disable-harness` to use the standalone plugin). Do not fall back to
  standalone execution: the user enabled the harness for this repository.
- The resolver cannot run (no Python): use standalone mode only if
  `.django-engineering/installation.json` does not exist in the project; otherwise report the problem.

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

## Harness mode

The same routing policy and the same canonical specialist definitions, executed by the existing
harness from the repository's own runtime. Run every call as
`python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" <subcommand>`, passing the request
on standard input with a quoted heredoc so it is never interpreted by the shell:

```
... orchestrate plan --task-stdin [--category <id>] <<'DJANGO_HARNESS_TASK'
<the request, verbatim>
DJANGO_HARNESS_TASK
```

1. `orchestrate plan`: show the route. On `CANNOT ROUTE` (ambiguous or unclassified), show the
   candidate categories and ask the user to choose; never pick one yourself. With `--plan-only`, stop.
2. `orchestrate run` with the same arguments, as a background command; wait for it to finish.
3. `result <execution id>`: relay each stage's status, summary and outputs, and every guard decision
   (denied tool calls and approval requests).
4. Exit 3 (a stage awaits approval): show what waits and ask the user. Only on their explicit
   approval run `execution approve <id> <stage> --by "<their name>"`, then `execution resume <id>`;
   on rejection `execution reject <id> <stage> --by "<their name>" --reason "<why>"`.
5. Exit 130 or an interrupted run: `execution resume <id>` recovers from the last checkpoint.

Any other failure is reported as it is. Never retry a failed harness execution in standalone mode
and never run the specialists yourself in its place.

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

1. If the request is empty, ask for it. Resolve the execution mode; in harness mode follow
   *Harness mode* and skip steps 2 and 3.
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

- [ ] The execution mode came from the resolver; an invalid harness was reported, not bypassed.
- [ ] The route came from the routing policy, and the report says why each specialist was used.
- [ ] Every routed step ran, or is reported as skipped or failed with the reason.
- [ ] No test or check is reported as passing unless it was run.
- [ ] The result was checked against the original request.
