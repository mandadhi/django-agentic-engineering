---
project: django-agentic-engineering
abstract: Convention for evaluation suites and scorers (Phase 2) driven by evaluation-config.yaml.
sdlc_phase: Testing
tech_stack: [Python]
domain: [Software Engineering, AI Agents]
---

# evaluations/

Evaluation suites (fixtures plus expected results) and scorer implementations go here.

**Phase 1 defines the contract and the configuration, not the scorers:**
- `config/evaluation-config.yaml` lists the criteria (`rule`, `llm-judge` or `human`,
  each with a threshold and weight) and the targets they apply to (an agent, a workflow,
  or `<workflow>/<stage>`). Both are validated when the config loads.
- `contracts/evaluation-result.schema.json` defines the record a scorer produces, with a
  score and pass/fail per criterion and an overall result.

Phase 2 will add the rule scorers (`contract-compliance`, `expected-outputs-present`,
`no-secrets-in-artifacts`) and an LLM-judge scorer, and will persist each
`EvaluationResult` against its execution.
