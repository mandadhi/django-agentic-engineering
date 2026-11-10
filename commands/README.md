---
project: django-agentic-engineering
abstract: Convention for the Django developer commands (Phase 3D) and how they reach the Django agents.
sdlc_phase: Development
tech_stack: [Django]
domain: [Software Engineering, AI Agents]
---

# commands/

Developer-facing slash commands for the Django Engineering Pack, one `<command>.md` per
command. A command is a thin entry point. It turns the developer's arguments into a brief for
one Django agent, applies an authorization gate where the agent can change code, and relays the
result. How the work is done stays in the agent; Django knowledge stays in the skills and rules.

## Django Developer Commands (Phase 3D)

| Command | Agent | Changes the workspace | Gate |
|---|---|---|---|
| [/django-analyze](django-analyze.md) | `django-project-analyzer` | never | read-only |
| [/django-design](django-design.md) | `django-architecture-agent` | design docs with `--out`; scaffolding with `--scaffold` | approval before scaffolding |
| [/django-model](django-model.md) | `django-model-agent` | models, migrations, tests | proposal → approval (or `--apply`) |
| [/django-api](django-api.md) | `django-api-agent` | API code and tests | proposal → approval (or `--apply`); breaking changes always asked |
| [/django-security](django-security.md) | `django-security-agent` | only with `--fix` | review → approval of the findings to fix |
| [/django-test](django-test.md) | `django-testing-agent` | test code only | plan → approval (or `--apply`); none with `--run-only` |
| [/django-optimize](django-optimize.md) | `django-performance-agent` | targeted optimisations and tests | baseline → approval (or `--apply`) → re-measure |
| [/django-document](django-document.md) | `django-documentation-agent` | documentation files only | none with `--dry-run` |
| [/django-review](django-review.md) | `django-code-review-agent` | never | read-only |
| [/orchestrate](orchestrate.md) (Phase 6) | `django-orchestrator`, then the specialists it selects | only through those specialists | each specialist's own gate |

`/orchestrate` is the deterministic entry point to the orchestrator. If the orchestrator cannot
spawn subagents where it runs, it returns a ROUTING PLAN. The command then has the main session
run that plan with the same limits (see [docs/orchestration.md](../docs/orchestration.md)).

## Optional Agentic Harness commands (Phase 5.6)

These commands manage the opt-in, repository-local Agentic Harness. None of them is needed for
the standalone plugin, and none runs an agent. They call the plugin's bootstrap script
(`harness_bootstrap/django_harness.py`, standard library only).

| Command | What it does | Changes the repository |
|---|---|---|
| [/django-engineering:setup-harness](setup-harness.md) | detect the project, check the environment, show a plan; after approval install `.django-engineering/`, validate, enable | only after you approve the exact plan |
| [/django-engineering:harness-status](harness-status.md) | execution mode, installation, versions, Python environment, database, profile, readiness | never |
| [/django-engineering:enable-harness](enable-harness.md) | validate the installation, then enable it for this repository | the manifest, after validation |
| [/django-engineering:disable-harness](disable-harness.md) | return this repository to the standalone plugin; runtime and history are kept | the manifest only |
| [/django-engineering:doctor](doctor.md) | read-only diagnosis with a remedy for every problem | never |

`/orchestrate` asks the execution-mode resolver first: standalone (no installation, or disabled)
works exactly as before; an enabled harness runs the same routing policy through the harness; an
invalid installation is reported and never bypassed. See [docs/harness.md](../docs/harness.md#repository-local-harness-phase-56).

## Output templates (Phase 3E)

Each command's Expected output section names the [output templates](../templates/README.md) its
agent's reply follows, and tells the session to name them in the brief. The templates are always
a subset of the agent's own templates. Naming a template changes the shape of the reply only, not
what the command or agent may do.

| Command | Templates |
|---|---|
| /django-analyze | project-analysis |
| /django-design | architecture-design, architecture-decision-record, implementation-plan |
| /django-model | django-model-specification, migration-plan |
| /django-api | api-specification |
| /django-security | security-assessment |
| /django-test | test-plan |
| /django-optimize | performance-assessment |
| /django-document | technical-documentation |
| /django-review | code-review-report |

## Format

Claude Code command files: frontmatter, then a Markdown body that receives `$ARGUMENTS`.

- Frontmatter: `name` (= file name), `description`, `argument-hint`, and the inventory fields
  used across this repository (`project`, `abstract`, `sdlc_phase`, `tech_stack`, `domain`).
  `sdlc_phase`, `tech_stack` and `domain` match the command's agent.
- Body sections, in this order: Purpose, Arguments, Agent, Skills and rules, Authorization,
  Steps, Expected output, Completion criteria.

## How a command reaches its agent

The same way the repository's orchestrator agents do it (for example
`plugins/security/agents/security-orchestrator.md`): the body names the subagent and says
what to brief it with. Claude Code dispatches the named subagent. The brief mirrors the
`AgentTask` fields (objective, constraints, inputs, expected outputs), so a command and a
harness stage ask the agent for the same thing. Outside the harness there is no output
directory, so the agent returns its report in its reply.

The Skills and rules section lists exactly the ids registered for that agent in
`config/agent-registry.yaml`. In the Claude Code plugin (Phase 4) the agent already has them
preloaded; the command still names them so the brief is complete wherever the agent runs.

In the plugin, commands are namespaced: `/django-engineering:django-security` and so on.
Claude Code also offers a skill as a slash command, and a command shadows a skill with the same
name (verified with CLI 2.1.319). That is why the security skill is named
`django-security-engineering`: `/django-security` is the command only.

## Security

- **No tools are granted.** Command frontmatter never declares `allowed-tools`, `tools`,
  `model`, `agent`, `context`, `hooks` or permission settings. The agent runs with its own
  `tools` frontmatter, and the session's normal permission prompts still apply.
- **The main session does not do the agent's work.** If the agent is unavailable, the command
  stops instead of falling back to the session's broader tools.
- **Code changes need explicit authorization:** a proposal first, then the user's approval, or
  an explicit `--apply` / `--fix` / `--scaffold` in the arguments.
- **Destructive or outward-facing operations are never run by a command**, even with `--apply`:
  migrations or SQL against a non-test database, data deletion, history rewrites, network
  calls, package installs (SEC-A3). The user gets the command to run instead.
- `/django-review` and `/django-analyze` are read-only. `/django-document` changes
  documentation only (SEC-A4, DOC-M8).

## Independence from the harness

Commands do not call `core.cli` or `agent_runtime`, and are not registered in `config/`. They
work wherever the Django agents are available to Claude Code; Phase 4 ships them in the
`django-engineering` plugin (see the project README). Wiring commands into the harness and
evaluation are later phases.

To add a command:
1. Create `commands/<name>.md` with the frontmatter and sections above. Name exactly one agent
   registered in `config/agent-registry.yaml`.
2. List that agent's skills and rules exactly as registered.
3. Name the agent's output template(s) in Expected output, and add the command to the template's
   `commands` frontmatter and to `COMMAND_TEMPLATES` in `tests/test_django_templates.py`.
4. Add it to `EXPECTED` in `tests/test_django_commands.py` and run the tests.
