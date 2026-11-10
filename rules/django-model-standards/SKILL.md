---
name: django-model-standards
description: >-
  Mandatory Django engineering rule for model definitions: database-enforced invariants,
  relationships and on_delete, user-model references, field type choices, sensitive fields,
  save() overrides and keeping models and migrations in step. Apply when defining or changing
  Django models.
project: django-agentic-engineering
abstract: >-
  Enforceable model standards that make the database protect data integrity in any Django
  project, independent of business domain.
sdlc_phase: Development
tech_stack: [Python, Django]
domain: [Software Engineering, Data]
---

# Rule: Django Model Standards

**MUST / MUST NOT** are mandatory. **SHOULD** is a recommendation. A MUST may be broken only
for a reason recorded in an ADR or in the `AgentResult` summary, never silently. The project's
own conventions override SHOULD items, but never MUST items. Rules apply to code you write or
change. In existing code, report violations instead of rewriting code outside the task.

## Purpose

Models are the contract with the database. They must state the data's invariants so that every
write path (forms, APIs, admin, tasks, shell) is protected.

## Scope

Applies to model classes, fields, `Meta`, managers and model methods. Migration mechanics,
transactions and raw SQL are covered by `django-database-standards`.

## Mandatory requirements

- **MOD-M1** Every invariant the database can express is enforced there: `UniqueConstraint`
  for uniqueness, `CheckConstraint` for value rules, non-nullable foreign keys for required
  relations. Python validation alone is not enough.
- **MOD-M2** Every `ForeignKey` and `OneToOneField` sets `on_delete` deliberately. Use `CASCADE`
  only when the child cannot exist without its parent.
- **MOD-M3** When a model has two or more relations to the same model, each relation sets an
  explicit `related_name`.
- **MOD-M4** Relations to users use `settings.AUTH_USER_MODEL`.
- **MOD-M5** Monetary and other exact quantities use `DecimalField` with explicit
  `max_digits` and `decimal_places`, never `FloatField`.
- **MOD-M6** Text fields (`CharField`, `TextField`) do not use `null=True` unless the field is
  also unique and optional.
- **MOD-M7** Passwords are stored only through Django's password hashers. Tokens that only need
  verification are stored hashed. Secrets are never model defaults or fixture values.
- **MOD-M8** `__str__` and properties used in listings MUST NOT trigger extra queries or
  expose sensitive values.
- **MOD-M9** A `save()` override calls `super().save(*args, **kwargs)`. Logic that must run
  for every write is not placed only in `save()` or signals if bulk operations or
  `QuerySet.update()` also write that model.
- **MOD-M10** Every model change ships with its migration in the same change, and
  `makemigrations --check` reports no changes.

## Recommendations

- **MOD-R1** Put shared fields (for example timestamps) in abstract base models, and reusable
  filters in custom `QuerySet`/manager methods.
- **MOD-R2** Use `TextChoices`/`IntegerChoices` for enumerations. Add a `CheckConstraint` when
  invalid values must be impossible.
- **MOD-R3** Give constraints and indexes explicit, stable names. Use `clean()` for cross-field
  validation, backed by a constraint.
- **MOD-R4** Define a custom user model at project start, when the project can still choose.

## Anti-patterns

| Anti-pattern | Required instead | Rule |
|---|---|---|
| Uniqueness checked only in a form or serializer | `UniqueConstraint` | MOD-M1 |
| `FloatField` for money | `DecimalField` | MOD-M5 |
| `CharField(null=True, blank=True)` | `blank=True` with an empty-string default | MOD-M6 |
| `ForeignKey("auth.User", ...)` | `settings.AUTH_USER_MODEL` | MOD-M4 |
| `__str__` returning `self.parent.name` | Fields of the model itself | MOD-M8 |
| `GenericForeignKey` for an ordinary relation | A real `ForeignKey` | MOD-M1 |

## Security and maintainability

Constraints at the database level hold even when code paths bypass validation. Flag sensitive
fields (personal, credential, financial data) in the change report.

## Verification

`python manage.py makemigrations --check --dry-run`. Review `Meta.constraints`, the `on_delete`
values, and any `FloatField`, `null=True` text field or `save()` override in the diff.

## Related skills

[django-orm](../../skills/django-orm/SKILL.md) (model design, fields, relationships, constraints and indexes, managers) ·
[postgresql-engineering](../../skills/postgresql-engineering/SKILL.md) (backend-specific types and constraints).
