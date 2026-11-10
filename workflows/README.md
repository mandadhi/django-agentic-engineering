---
project: django-agentic-engineering
abstract: How workflow definitions are written, discovered and validated.
sdlc_phase: Architecture/Planning
tech_stack: [YAML]
domain: [Software Engineering, AI Agents]
---

# workflows/

Every file matching `discovery.pattern` (`*.workflow.yaml`) in `discovery.definitions_dir`
(both set in `config/workflow-config.yaml`) is loaded as a `WorkflowDefinition`. Adding a
file registers the workflow; no code change is needed.

Each file must:
- validate against `contracts/workflow-definition.schema.json` (the stages use
  `workflow-stage.schema.json`);
- have a unique workflow `id` and a semantic `version` (the version is recorded on every
  execution);
- reference only registered, enabled agents (`agent_id`) and existing stages
  (`depends_on`), with no cycles.

A stage that omits `max_attempts`, `timeout_seconds` or `approval_required` inherits the
value from `workflow-config.yaml` `defaults`.

`security-review.workflow.yaml` is a real `draft` workflow built from reused security
agents: threat model, then secrets scan and SAST in parallel, then an OWASP audit with an
approval gate. Full SDLC workflows come in a later phase.
