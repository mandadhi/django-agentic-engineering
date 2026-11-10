---
project: django-agentic-engineering
abstract: Convention for harness-local Django agents (added in Phase 2) and how they are registered.
sdlc_phase: Architecture/Planning
tech_stack: [Django]
domain: [Software Engineering, AI Agents]
---

# agents/

Harness-local agent definitions go here, one `<agent-id>.md` per agent. Each file has
Claude Code agent frontmatter (`name`, `description`, `tools`, `model`) plus the inventory
fields (`project`, `abstract`, `sdlc_phase`, `tech_stack`, `domain`).

## Django Engineering Agents (Phase 3B)

Each agent is a thin role definition. Django knowledge lives in the skills it lists, which
the registry preloads; the agent says only when and how to apply them.

| Agent | Role | Responsibility | Skills | Writes code |
|---|---|---|---|---|
| [django-project-analyzer](django-project-analyzer.md) | analyst | Profiles an existing project: versions, layout, apps, dependencies, architecture, risks | django-core, django-architecture, django-orm | no (read-only) |
| [django-architecture-agent](django-architecture-agent.md) | architect | App boundaries, layering, ADRs, implementation plan, optional scaffolding | django-core, django-architecture, django-security-engineering | design docs, scaffolding |
| [django-model-agent](django-model-agent.md) | engineer | Models, relationships, constraints, indexes, safe migrations | django-orm, postgresql-engineering, django-architecture | yes |
| [django-api-agent](django-api-agent.md) | engineer | DRF serializers, views, permissions, auth, pagination, API tests | django-rest-framework, django-security-engineering, django-orm | yes |
| [django-security-agent](django-security-agent.md) | reviewer | Security assessment; remediation only when asked | django-security-engineering, django-rest-framework, django-integrations | only in remediation mode |
| [django-testing-agent](django-testing-agent.md) | tester | Unit, integration and API tests; runs them and reports | django-testing, django-core, django-rest-framework | test code only |
| [django-performance-agent](django-performance-agent.md) | engineer | Measured ORM, database, caching and request-path optimisation | django-performance, django-orm, postgresql-engineering | yes |
| [django-documentation-agent](django-documentation-agent.md) | writer | Code-derived technical, API and developer documentation | django-core, django-architecture, django-rest-framework | docs only (no shell) |
| [django-code-review-agent](django-code-review-agent.md) | reviewer | Gate review of a change; applies skills per changed file | django-core, -architecture, -orm, -rest-framework, -security, -testing, -performance | no (read-only) |

Every agent file has the same sections: Purpose, Responsibilities, Scope and boundaries,
Required skills, Mandatory rules (Phase 3C, see [`rules/`](../rules/README.md)), Tools, Inputs,
Execution, Output, Templates and references (Phase 3E), Error handling, Security and Completion
criteria. The rules are appended to each agent's `skills` in the registry. They add no tools.
`tests/test_django_agents.py` enforces this, the registry wiring, the tool boundaries, and that no
agent copies text from a skill.

## Templates and shared references (Phase 3E)

Each agent's `## Templates and references` section names, by id, the
[output templates](../templates/README.md) that give its Output section the full standard
structure, and the [shared references](../references/README.md) it works from. The section holds
ids only: template structure stays in `templates/`, checklists in `references/`. It adds no tools,
so read-only agents stay read-only and the documentation agent still changes documentation only.

| Agent | Templates | Shared references |
|---|---|---|
| django-project-analyzer | project-analysis | django-project-discovery, django-version-compatibility |
| django-architecture-agent | architecture-design, architecture-decision-record, implementation-plan | django-project-discovery, django-version-compatibility |
| django-model-agent | django-model-specification, migration-plan | django-migration-safety, django-version-compatibility |
| django-api-agent | api-specification | django-api-design-checklist, django-security-checklist, django-testing-strategy |
| django-security-agent | security-assessment | django-security-checklist, django-version-compatibility |
| django-testing-agent | test-plan | django-testing-strategy |
| django-performance-agent | performance-assessment | django-performance-checklist, django-migration-safety |
| django-documentation-agent | technical-documentation | django-project-discovery |
| django-code-review-agent | code-review-report | django-security-checklist, django-api-design-checklist, django-performance-checklist, django-migration-safety, django-testing-strategy |

The agent's inline Output skeleton remains the minimum it always produces. In the Claude Code
plugin (Phase 4) the section's paths start at `${CLAUDE_PLUGIN_ROOT}`, which Claude Code fills in,
so templates and references resolve wherever the plugin is installed. In a harness run the pack
files are not readable yet, so the inline skeleton is what the agent follows there.
`tests/test_django_templates.py` checks that every inline heading also appears in the agent's
primary template.

**Runnable in Claude Code, not yet in the harness.** Installed as the `django-engineering`
Claude Code plugin, each agent preloads its registered skills and rules through the generated
`skills:` frontmatter line (`django-engineering:<id>`, written by `python -m plugin_packaging build`
from `config/agent-registry.yaml`; do not edit it by hand). The harness ignores that line and
reads skills from the registry. Harness integration is not implemented yet: the SDK loads skills
only through registered plugins, so a harness run of a Django agent still fails closed with
`plugin.unresolved` before the SDK starts (see the project README, Known limitations).

To add an agent:
1. Create `agents/<id>.md`. Projects Explorer lists it automatically, because it scans
   `projects/*/agents/*.md` and skips ALL-CAPS names such as this README.
2. Register it in `config/agent-registry.yaml`. Add only harness data; the description,
   tools, model and phase stay in the file.
   ```yaml
   - id: django-model-designer          # must equal the file's frontmatter `name`
     source: local
     path: projects/django-agentic-engineering/agents/django-model-designer.md
     skills: [db-schema-conventions]    # preloaded via AgentDefinition.skills
   ```
3. Run `python -m core.cli validate`. Loading fails if the path or any skill is missing,
   if the file has no frontmatter, or if its `name` differs from the registry id.

Agents stay Markdown; there are no Python agent classes. The Phase 2 runner turns each
file into an Agent SDK `AgentDefinition`: the body becomes the prompt, and `tools`,
`model` and `skills` come from the frontmatter and the registry.

Every agent receives an `AgentTask` (`contracts/agent-task.schema.json`) and returns an
`AgentResult` (`contracts/agent-result.schema.json`).
