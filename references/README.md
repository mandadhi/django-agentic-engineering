---
project: django-agentic-engineering
abstract: >-
  Convention for the Django Engineering Pack's shared references (Phase 3E): reusable checklists
  and lookup tables that several agents consume, linked to skills and rules instead of copying them.
sdlc_phase: Architecture/Planning
tech_stack: [Django]
domain: [Software Engineering, AI Agents]
---

# references/

Shared references are reusable working material that more than one Django agent needs: lookup
tables, verification checklists, decision matrices and classification tables. They answer "what
do I check, where do I look, how do I rate it". They do not teach a topic and they do not set
requirements:

| Component | Answers | Example |
|---|---|---|
| Skill (`skills/<id>/`) | How does this work and how is it done well? | How `select_related` works |
| Rule (`rules/<id>/`) | What must every result satisfy? | `PRF-M1`: relations read in loops are loaded up front |
| Shared reference (`references/<id>.md`) | What do I check, where, and how do I rate it? | Search pattern and measurement for N+1 queries, citing `PRF-M1` and the skill reference |
| Template (`templates/<id>.template.md`) | What shape does the report take? | Performance assessment sections |

Skill references (`skills/<id>/references/`) belong to one skill. A shared reference belongs to no
single skill and is used by several agents.

## References

| Reference | Covers | Agents |
|---|---|---|
| [django-version-compatibility](django-version-compatibility.md) | Version evidence order, compatibility pairs, version-dependent guidance, support and upgrades | project-analyzer, architecture, model, security |
| [django-project-discovery](django-project-discovery.md) | Where each project fact lives, safe commands, architecture signals, sampling | project-analyzer, architecture, documentation |
| [django-security-checklist](django-security-checklist.md) | Security checks per area with search patterns, rule ids and skill references | security, api, code-review |
| [django-testing-strategy](django-testing-strategy.md) | Minimum tests per change, level and tool choice, isolation, failure classes | testing, api, code-review |
| [django-migration-safety](django-migration-safety.md) | Pre-merge migration checks, operation risk, red flags, escalation | model, performance, code-review |
| [django-api-design-checklist](django-api-design-checklist.md) | Per-endpoint DRF checklist and contract-change classification | api, code-review |
| [django-performance-checklist](django-performance-checklist.md) | Symptom to cause, search patterns, measurement toolkit | performance, code-review |

## Format

- **Frontmatter:** `name` (= file name), `description`, `kind: shared-reference`, `agents` (the
  consumers), and the inventory fields (`project`, `abstract`, `sdlc_phase`, `tech_stack`, `domain`).
- **Sections:** Purpose, Scope and How to use first; topic sections next; Related skills and
  rules last. The last section links to at least one local skill and one rule with relative links.
- **Linked, not copied.** A reference cites rule requirement ids (for example `SEC-M2`) and links
  skill references for the explanation. It never repeats a skill or rule line;
  `tests/test_django_templates.py` checks this.
- **No version assumptions.** References name no Django version numbers or support dates. Where
  behaviour depends on the version or the database backend, they say what to check and where.
- **Django-specific.** Each item concerns Django, DRF or the Django ecosystem.

## How agents find them

Each Django agent's `## Templates and references` section lists the references it uses, by id.
The files are part of the pack, not of the workspace. In the Claude Code plugin the agents read
them at `${CLAUDE_PLUGIN_ROOT}/references/<id>.md`; the plugin's guard lets the pack's agents read
these files without a permission prompt. Since Phase 5 a harness run resolves the same ids against
the plugin root and offers the files as task-context references, read on demand (see
[docs/harness.md](../docs/harness.md#harness-integration-phase-5)).

## Adding a reference

1. Create `references/<id>.md` with the frontmatter and sections above.
2. Add the id to the `References` line of each consuming agent's `## Templates and references`
   section, and to `EXPECTED_REFERENCES` in `tests/test_django_templates.py`.
3. Add it to the table above and run the tests.
