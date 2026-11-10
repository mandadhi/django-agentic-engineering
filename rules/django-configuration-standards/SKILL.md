---
name: django-configuration-standards
description: >-
  Mandatory Django engineering rule for settings and configuration: environment-sourced values
  and secrets, production hardening settings, a single settings selection mechanism, typed and
  validated values, fail-fast on missing configuration, immutability at runtime, and keeping
  credential files out of version control. Apply when reading, adding or changing settings.
project: django-agentic-engineering
abstract: >-
  Enforceable configuration standards that keep Django settings secure, environment-independent
  and verifiable in any deployment model.
sdlc_phase: Deployment/Maintenance
tech_stack: [Python, Django]
domain: [Software Engineering, Security]
---

# Rule: Django Configuration Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Run the same code in every environment, with configuration supplied from outside and production
safe by default.

## Scope

Applies to settings modules, environment variables, `.env` handling and production security
settings. Which secrets exist, and how they are rotated, is covered by `django-security-engineering`.

## Mandatory requirements

- **CFG-M1** Values that vary by environment, and all secrets, are read from environment
  variables or a secret manager. A secret has no fallback default. Production start-up fails
  if `SECRET_KEY` or a credential is missing.
- **CFG-M2** Production settings set: `DEBUG = False`, an explicit `ALLOWED_HOSTS`,
  `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE`, HTTPS redirect and HSTS (at the app or the
  proxy, as documented), and `CSRF_TRUSTED_ORIGINS` where cross-origin forms or HTTPS proxies need it.
  `SECURE_PROXY_SSL_HEADER` is set only behind a proxy that always overwrites that header.
- **CFG-M3** There is one settings selection mechanism (`DJANGO_SETTINGS_MODULE`). Each
  environment overrides a shared base instead of copying it.
- **CFG-M4** Boolean and numeric values are parsed explicitly. A non-empty string such as
  `"False"` MUST NOT be treated as true.
- **CFG-M5** Missing or invalid required configuration fails at start-up with an error that
  names the variable and never shows its value.
- **CFG-M6** Settings are not changed at runtime. Code reads them through
  `django.conf.settings`. Tests change them only with `override_settings` or the `settings` fixture.
- **CFG-M7** `.env` files, key files and local settings that hold credentials are never
  committed. A committed example file holds variable names and placeholders only.
- **CFG-M8** `USE_TZ = True` is set in new projects. Existing projects change it only through a planned migration.

## Recommendations

- **CFG-R1** Use one typed configuration helper (for example django-environ or environs)
  consistently.
- **CFG-R2** Keep a configuration reference that lists every variable (see `django-documentation-standards`).
- **CFG-R3** Give tests their own settings (fast password hasher, in-memory email, local cache).

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| `DEBUG = os.environ.get("DEBUG", True)` | Safe default with explicit parsing | CFG-M1, CFG-M4 |
| `bool(os.getenv("FEATURE"))` | Explicit boolean parsing | CFG-M4 |
| `ALLOWED_HOSTS = ["*"]` in production | Explicit host list | CFG-M2 |
| `prod.py` as a full copy of `dev.py` | Shared base plus overrides | CFG-M3 |
| Committed `local_settings.py` with passwords | Environment or secret manager | CFG-M7 |
| `settings.FEATURE = True` inside a view | Configuration read at start-up | CFG-M6 |

## Security and maintainability

Misconfiguration is among the most common Django production weaknesses. Failing fast turns a
silent insecure deployment into a visible error.

## Verification

`python manage.py check --deploy` with production settings. Grep settings for literal secrets,
`os.environ.get(` defaults, and `bool(os.getenv`. Confirm that `.env` is git-ignored.

## Related skills

[django-core](../../skills/django-core/SKILL.md) (settings configuration, production configuration) ·
[django-security-engineering](../../skills/django-security-engineering/SKILL.md) (secure configuration, secrets and cryptography).
