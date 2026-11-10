---
name: django-version-compatibility
description: >-
  Shared reference for establishing which Python, Django, DRF, database and third-party versions a
  project actually runs, and for making version-dependent recommendations without assuming a
  version.
kind: shared-reference
agents: [django-project-analyzer, django-architecture-agent, django-model-agent, django-security-agent]
project: django-agentic-engineering
abstract: >-
  How to detect versions from evidence, which compatibility pairs to check, how to treat
  version-dependent guidance, and how to report upgrade paths without inventing support dates.
sdlc_phase: Architecture/Planning
tech_stack: [Python, Django, Django REST Framework, PostgreSQL]
domain: [Software Engineering]
---

# Django Version Compatibility

## Purpose

Advice that is right for one Django version can be wrong for another. This reference sets out how
to find out which versions a project really uses, which pairs of components must be compatible,
and how to word a recommendation when an API or behaviour depends on the version.

## Scope

Python, Django, Django REST Framework, the database server and its driver, and the third-party
Django apps a project installs. It does not list version numbers or support dates: those change
and must be read from the official sources named below at the time of use.

## How to use

1. Detect each version with the evidence order below and record the source.
2. Check the compatibility pairs that apply to the task.
3. Before recommending a version-dependent API, confirm it exists in the detected version.
4. Report the result in the Stack section of the
   [project analysis](../templates/project-analysis.template.md) template, or in the Context of
   the template being filled.

## Evidence order for versions

Use the first source that answers the question, and record which one it was.

| Rank | Source | What it proves | Caveat |
|---|---|---|---|
| 1 | Lock file (`poetry.lock`, `uv.lock`, `Pipfile.lock`, `pdm.lock`, `requirements*.txt` produced by pip-compile) | The exact version that installs | A stale lock may differ from the manifest; report both |
| 2 | Installed environment (`python -m django --version`, `pip show <package>`) | What runs in this environment | Only when a project environment exists; never install to find out |
| 3 | Manifest (`pyproject.toml`, `setup.cfg`, unpinned `requirements*.txt`) | The allowed range | A range is not a version: report it as a range |
| 4 | Container and CI files (`Dockerfile`, CI workflow, `.python-version`, `runtime.txt`, `tox.ini`) | Python version and test matrix | May lag behind the manifest |
| 5 | Code signals (APIs used, settings present, migration file headers) | A lower bound only | Mark the result `inferred` |

Migration files record the Django version that generated them in their header comment. That
shows which version created the file, not which version runs today.

## Compatibility pairs to check

| Pair | Where the answer lives | Typical failure when wrong |
|---|---|---|
| Python ↔ Django | Django release notes and the "What Python version can I use" FAQ | Import errors, syntax errors at start-up |
| Django ↔ DRF | DRF release notes and its stated requirements | Removed Django APIs used inside DRF |
| Django ↔ each third-party app | The app's changelog, classifiers and CI matrix | `ImportError`, system check errors, silent behaviour changes |
| Django database backend ↔ database server | Django "Databases" documentation for the version in use | Unsupported-version error at connection or missing features |
| Database driver ↔ Django | Django "Databases" documentation (for PostgreSQL, which psycopg major versions are supported depends on the Django version) | Connection errors, missing features such as connection pooling options |
| Celery, channels, ASGI server ↔ Django | Each project's documentation | Async and worker start-up failures |

Never fill a compatibility cell from memory. Write the source you read, or `unverified`.

## Version-dependent guidance

- Skill references state the Django version they assume near their top. When the detected version
  differs, treat their version-specific statements as `unverified` and confirm them in the
  documentation for the detected version.
- Confirm that an API exists before recommending it: search the installed package
  (`grep -rn "def <name>" <site-packages>/django/`) when an environment is present, otherwise the
  versioned documentation page. Word the recommendation as "available in the detected version
  (source: ...)".
- When a recommendation needs a newer version than the project runs, say so and offer the
  alternative that works today.
- Settings defaults change between releases. Read an effective value from the project's settings
  or `python manage.py diffsettings` (read-only, needs importable settings), not from memory.

## Support status and upgrades

- Whether a release still receives security fixes is a fact to look up on the Django download
  page (supported versions table) at the time of the task, never to assume. Record the date you
  checked. This feeds `DEP-M2`.
- Django's release process labels some feature releases as long-term support (LTS); the download
  page shows which ones, with their end dates.
- Deprecations warn before removal. The Django deprecation timeline lists what each release
  removes. Running the test suite with warnings visible (`python -Wd manage.py test` or
  `python -W error::DeprecationWarning -m pytest`) shows what the next upgrade will break.
- Plan upgrades one feature release at a time, reading each release note crossed, and upgrade DRF
  and third-party apps to versions that state support for the target before moving Django.

## Reporting format

| Component | Detected version or range | Evidence | Compatible with | Status (verified/inferred/unknown) |
|---|---|---|---|---|

## Related skills and rules

- Skills: [django-core](../skills/django-core/SKILL.md) (version checks in
  [fundamentals](../skills/django-core/references/fundamentals.md)),
  [django-rest-framework](../skills/django-rest-framework/SKILL.md),
  [postgresql-engineering](../skills/postgresql-engineering/SKILL.md).
- Rules: [django-dependency-standards](../rules/django-dependency-standards/SKILL.md)
  (`DEP-M1`, `DEP-M2`, `DEP-R2`, `DEP-R3`),
  [django-configuration-standards](../rules/django-configuration-standards/SKILL.md).
