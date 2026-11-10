# Project structure

> Scope: how to lay out a Django codebase — project package, app boundaries, reusable apps, modular monolith, domain-oriented apps, shared code, config organisation and scaling a large codebase. Not covered here: settings content ([settings-configuration.md](./settings-configuration.md)), AppConfig mechanics ([models-application-lifecycle.md](./models-application-lifecycle.md)), deeper boundary/service-layer design ([django-architecture](../../django-architecture/SKILL.md)), test layout ([django-testing](../../django-testing/SKILL.md)).

## Purpose and overview

Structure decides how cheap future changes are. Django imposes very little: a project package and a list of installed apps. Everything else — where services live, how apps talk to each other, where shared code sits — is a team decision. Choose a structure that makes the *dependency direction* obvious and keeps each app describable in one sentence.

## How it works internally

Django discovers code by convention **inside installed apps**, not by directory scanning of the whole repo:

| Convention | Discovered when |
|---|---|
| `models.py` (or `models/__init__.py`) | App registry phase 2 |
| `migrations/` | `migrate`, `makemigrations` |
| `admin.py` | `admin.autodiscover()` (run by `django.contrib.admin`'s AppConfig) |
| `templates/<app_label>/` | `APP_DIRS: True` |
| `static/<app_label>/` | `AppDirectoriesFinder` |
| `management/commands/*.py` | `manage.py` command lookup |
| `templatetags/*.py` | `{% load %}` |
| `apps.py` AppConfig | Automatically if exactly one subclass exists (Django ≥ 3.2) |

Everything else (`services.py`, `selectors.py`, `api/`) is plain Python — Django does not care, which is exactly why you must pick a convention and apply it consistently.

The `AppConfig.name` must equal the dotted import path (`apps.orders`); the `label` (default: last component, `orders`) is what appears in migrations, `app_label`, permissions (`orders.add_order`) and table names (`orders_order`). **Changing a label after deployment requires data migration of tables, content types and permissions** — pick it once.

## Core concepts and APIs

- **Project package** (`config/`): settings, root URLconf, `asgi.py`, `wsgi.py`, cross-cutting middleware/logging. It contains no models.
- **Domain app**: owns a bounded area of the business (`orders`, `billing`, `accounts`) and its tables.
- **Reusable app**: no project-specific imports, configured through its own settings namespace, packaged with `pyproject.toml`.
- **Shared/common package**: framework-level helpers with no domain knowledge (`apps/common/` or `core/`): base models (timestamps), utilities, custom fields.
- **Dependency direction**: `config` → apps; domain apps → `common`; between domain apps only in one direction, through a public module (services/selectors), never through each other's internals.

## Implementation examples

### Recommended default (small to medium)

```text
project-root/
├── manage.py
├── pyproject.toml
├── config/
│   ├── __init__.py
│   ├── settings.py           # or settings/ package, see settings-configuration.md
│   ├── urls.py
│   ├── asgi.py  wsgi.py
│   ├── middleware.py         # RequestIdMiddleware
│   └── logging.py            # request_id_var, RequestIdFilter
├── apps/
│   ├── __init__.py
│   ├── common/               # base models, utils — no domain logic
│   ├── accounts/             # custom User (AUTH_USER_MODEL = "accounts.User")
│   ├── orders/
│   └── billing/
├── templates/                # project-wide templates (base.html, errors)
├── static/                   # project-wide assets
└── tests/                    # optional: cross-app/integration tests
```

### Inside a domain app

```text
apps/orders/
├── __init__.py
├── apps.py            # OrdersConfig(name="apps.orders", label="orders")
├── models.py          # split into models/ package when > ~500 lines
├── services.py        # write operations: place_order, expire_unpaid_orders
├── selectors.py       # read queries reused by views/APIs
├── forms.py
├── views.py  urls.py  # app_name = "orders"
├── api/               # DRF serializers/views if any
├── admin.py
├── signals.py         # only if needed; connected in ready()
├── management/commands/expire_orders.py
├── migrations/
├── templates/orders/
└── tests/
```

Why `services.py`/`selectors.py`: views, commands, tasks and API endpoints all need the same operations. Putting them in one importable place prevents four slightly different implementations. See [django-architecture](../../django-architecture/SKILL.md) for the full pattern.

### Cross-app communication

Good — call the other app's public service:

```python
# apps/billing/services.py
from apps.orders.selectors import get_payable_order

def charge_order(*, order_id: int) -> Invoice:
    order = get_payable_order(order_id=order_id)
    ...
```

Bad — reach into internals and mutate another app's state:

```python
from apps.orders.models import Order
Order.objects.filter(pk=order_id).update(status="paid")   # bypasses orders' invariants
```

### Splitting a large models module

```python
# apps/orders/models/__init__.py
from .order import Order
from .line import OrderLine
__all__ = ["Order", "OrderLine"]
```

Model classes must be importable from `apps.orders.models` so the registry finds them.

## Production patterns

**Modular monolith** — the default target for most teams: one deployable, one database, many apps with enforced boundaries. Gives most of the benefits of services (ownership, clear interfaces) without distributed transactions. Enforce boundaries with:

- A documented public surface per app (`services`, `selectors`, `api`), everything else private.
- An import linter (e.g. `import-linter` contracts) if already in the toolchain — don't add one silently.
- FKs across apps are fine (it's one DB), but writes go through the owning app's services.

**Domain-oriented apps** over layer-oriented ones: `orders/`, `billing/` — not `models/`, `views/`, `serializers/` top-level packages. Layer-first layouts force every feature change to touch every package and make ownership impossible.

**Reusable apps** — extract only when a second project actually needs it. Then:
- No imports from `config` or sibling apps; use `settings.ORDERS_*` with defaults and `apps.get_model()` / swappable settings for dependencies.
- Ship migrations, templates under `templates/<label>/`, a default `AppConfig`.

**Large codebases**:
- Keep `INSTALLED_APPS` grouped: Django contrib, third-party, local.
- One owner per app (CODEOWNERS).
- Watch for "god apps" (`core` with 60 models) — split by sub-domain, moving models with `SeparateDatabaseAndState` migrations when needed (see [django-orm](../../django-orm/SKILL.md)).
- Templates and static files namespaced by app label to avoid collisions (first match wins in `APP_DIRS` order).

## Decision guidance

| Situation | Structure |
|---|---|
| Prototype, one developer, < 10 models | Single app + `config/`; split later |
| Typical product, 2–15 devs | Modular monolith under `apps/` with services/selectors |
| Several products sharing auth/billing | Reusable apps as internal packages |
| Teams needing independent deploys and data stores | Consider separate services — but only after the monolith boundaries are clean |

| Should X be a new app? | Yes if… | No if… |
|---|---|---|
| Owns its own tables and rules | ✓ | |
| Only helpers without models | | Put in `common/` or the using app |
| Tightly coupled to another app's every change | | Keep in that app |
| Might be reused elsewhere | ✓ (keep it dependency-free) | |

Place the custom user model in its own `accounts` app **at project start** — switching `AUTH_USER_MODEL` later is a painful migration (see [authentication-sessions.md](./authentication-sessions.md)).

## Security considerations

- Keep secrets out of the tree; `config/` reads the environment ([settings-configuration.md](./settings-configuration.md)).
- Don't put uploaded media under `static/` or inside the repo; `MEDIA_ROOT` is outside source control ([file-handling.md](./file-handling.md)).
- Namespaced templates prevent a third-party app's `registration/login.html` from silently overriding yours (or vice versa) — order of `INSTALLED_APPS` decides which wins.

## Performance considerations

- Structure has no runtime cost per se, but import-time work does: heavy module-level code in many apps slows every process start, test run and management command.
- Avoid cross-app chatty calls in loops (`for o in orders: billing.get_invoice(o.id)`) — expose bulk selectors.

## Testing

- Tests live next to the code (`apps/orders/tests/`) so moving an app moves its tests; cross-app flows in `tests/integration/`.
- Test services directly; test views/URLs thinly via the test client.
- `python manage.py check` and `makemigrations --check --dry-run` catch broken app wiring. See [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- **Layer-first top-level packages** (`models/`, `views/`) instead of domain apps.
- **`utils.py` dumping ground** containing domain logic from several apps.
- **Circular app dependencies** (orders imports billing, billing imports orders). Break with string FK references, events/explicit callbacks, or moving the shared concept to its own app.
- **Changing `AppConfig.label`** or app path after migrations exist without a migration plan.
- **Project package containing models** (`config/models.py`) — it isn't an app.
- **Single giant app** for the whole product: migrations conflict constantly, ownership unclear.
- **Premature microservices** for problems a clean monolith boundary solves.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImproperlyConfigured: Cannot import 'orders'` | `AppConfig.name` is `"orders"` but package is `apps.orders` | Set `name = "apps.orders"` |
| Migrations reference `apps_orders` / wrong label | Label derived differently after a move | Set explicit `label = "orders"` |
| Wrong template rendered | Two apps provide same path | Namespace under `templates/<label>/`; check `INSTALLED_APPS` order |
| Management command not found | Missing `__init__.py` in `management/` or `commands/`, or app not installed | Add packages; install app |
| `ImportError` cycle at startup | Apps importing each other's models at module level | String FK refs, lazy imports inside functions |

## Official documentation

- https://docs.djangoproject.com/en/stable/ref/applications/
- https://docs.djangoproject.com/en/stable/intro/reusable-apps/
- https://docs.djangoproject.com/en/stable/ref/django-admin/#startapp
- https://docs.djangoproject.com/en/stable/topics/templates/#configuration
- https://docs.djangoproject.com/en/stable/howto/custom-management-commands/
