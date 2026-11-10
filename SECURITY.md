# Security policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities in public issues, pull requests or discussions.

Report them privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub private vulnerability reporting). Include:

- the affected component: the standalone plugin guard (`hooks/`), the harness (`core/`,
  `agent_runtime/`, `runtime/`), or an agent, command or rule definition;
- the Claude Code CLI version and, for the harness, the `claude-agent-sdk` version;
- your operating system;
- steps to reproduce, and the expected and actual guard decision or behaviour.

Do not include real secrets or credentials in a report. Use obviously fake values.

## Scope

In scope:

- bypasses of the plugin guard (`hooks/django_pack_guard.py`), for example a write by a read-only
  agent, a read of a denied path, or a shell command that escapes the project;
- bypasses of the harness ToolGuard, workspace isolation, approval binding, environment filtering
  or secret redaction;
- agent, command or rule definitions that grant more access than documented.

The documented limitations are known and are not treated as vulnerabilities unless you show a
practical bypass beyond them. Examples: static shell analysis cannot bound scripts an agent writes
and then runs, failed commands are not redacted in the plugin, and the macOS and Linux sandbox has
not been exercised. See [docs/plugin.md](docs/plugin.md#plugin-guard-limitations) and
[docs/harness.md](docs/harness.md#known-limitations).

## Supported versions

There are no published releases yet. Fixes are made on the default branch.
