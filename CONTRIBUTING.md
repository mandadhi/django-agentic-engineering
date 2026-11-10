# Contributing

Thank you for improving Django Agentic Engineering. This repository holds three things that must
stay consistent:

- the canonical **Django Engineering Pack**: `agents/`, `skills/`, `rules/`, `commands/`,
  `templates/` and `references/`;
- the **Django Engineering Plugin** packaging: `.claude-plugin/`, `hooks/` and `plugin_packaging/`;
- the **Django Agentic Harness**: `config/`, `contracts/`, `core/`, `runtime/`, `agent_runtime/`
  and `workflows/`.

## Ground rules

1. **One source of truth.** Each agent, skill, rule, command, template and reference exists once,
   in its canonical folder. Do not copy definitions into plugin-specific or harness-specific
   locations, and do not copy text between layers: skills explain, rules require, references turn
   both into checks, and templates shape the output. The tests reject copied lines.
2. **The agent-to-skill mapping lives only in `config/agent-registry.yaml`.** Do not edit the
   generated `skills:` line in an agent's frontmatter by hand.
3. **Generated files are regenerated, not edited:** `hooks/security-policy.json`, each agent's
   `skills:` line and `hooks/shell_syntax.py`, which is a copy of `core/shell_syntax.py`.
4. **No permission escalation.** Commands declare no tools. An agent's `tools` frontmatter is its
   whole tool set, and the security policies only narrow it.
5. **Keep the plugin independent of the harness.** Nothing under `hooks/` may import harness code.
   The guard uses only the Python standard library (3.8+).
6. **Never commit secrets**, `.env` files, local Claude settings or `var/` (runtime database and
   artifacts).

## Development setup

```bash
git clone https://github.com/<GITHUB_USERNAME>/<REPOSITORY_NAME>.git
cd <REPOSITORY_NAME>
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt      # Linux/macOS: .venv/bin/python
claude --plugin-dir .                                            # try your changes in Claude Code
```

## Before you open a pull request

```bash
.venv/Scripts/python -m plugin_packaging build                   # after editing a source of a generated file
.venv/Scripts/python -m plugin_packaging check                   # must report no drift
claude plugin validate .claude-plugin/plugin.json
claude plugin validate .claude-plugin/marketplace.json
DAE_TEST_PRUNE_UNAVAILABLE=1 .venv/Scripts/python -m pytest -q tests/test_plugin_packaging.py \
    tests/test_django_harness_integration.py tests/test_django_agents.py tests/test_django_rules.py \
    tests/test_django_commands.py tests/test_django_templates.py
```

The full suite (`.venv/Scripts/python -m pytest -q`) and `python -m core.cli validate` need the
parent-repository layout described in [docs/harness.md](docs/harness.md#parent-repository-layout).
In a standalone clone, the tests that load the reused parent-repository agents error or fail. Say
in your pull request which suites you ran and what they reported.

## Changing the pack

| Change | Also update |
|---|---|
| Add or rename an agent | `.claude-plugin/plugin.json` `agents`, `config/agent-registry.yaml`, `plugin_packaging/standalone-security.yaml`, the expected counts in `plugin_packaging/build.py`, and the tests' expected sets. See [agents/README.md](agents/README.md). |
| Add or rename a command | `.claude-plugin/plugin.json` `commands` and the tests. See [commands/README.md](commands/README.md). |
| Change routing (categories, routes, limits) | Edit only `config/orchestration-policy.yaml`, then `plugin_packaging build` (it regenerates the orchestrator's routing section and delegation allowlist) and `core.cli --profile pack validate`. Never edit the generated section of `agents/django-orchestrator.md`. See [docs/orchestration.md](docs/orchestration.md). |
| Add a skill or rule | `config/skill-registry.yaml`, the agents' `skills` lists in `config/agent-registry.yaml`, then `plugin_packaging build`. See [skills/README.md](skills/README.md) and [rules/README.md](rules/README.md). |
| Add a template or reference | The consuming agents' `## Templates and references` sections and the tests. See [templates/README.md](templates/README.md) and [references/README.md](references/README.md). |
| Change the security policy | `config/security-config.yaml` or `plugin_packaging/standalone-security.yaml`, then `plugin_packaging build`. |
| Release the plugin | Bump `version` in `.claude-plugin/plugin.json` so that `claude plugin update` picks up the change, and add an entry to [CHANGELOG.md](CHANGELOG.md). |

Component names must be unique across agents, commands, skills and rules. Claude Code exposes
skills as slash commands, and a command shadows a skill with the same name.

## Reporting security issues

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
