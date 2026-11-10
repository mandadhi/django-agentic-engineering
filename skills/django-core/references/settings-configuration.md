# Settings and configuration

> Scope: how settings load, `settings.py` architecture, environment variables, secrets, core settings (`INSTALLED_APPS`, `MIDDLEWARE`, `DATABASES`, `TEMPLATES`, static/media URLs), custom settings, validation and per-environment configuration. Not covered here: production security flags and checklist ([production-configuration.md](./production-configuration.md)), middleware ordering ([middleware.md](./middleware.md)), `LOGGING` dict ([logging-exceptions.md](./logging-exceptions.md)), `CACHES` ([caching.md](./caching.md)), `STORAGES`/media ([file-handling.md](./file-handling.md)), ASGI/WSGI entry points ([asgi-wsgi.md](./asgi-wsgi.md)), hardening ([django-security-engineering](../../django-security-engineering/SKILL.md)), DB tuning ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## Purpose and overview

Settings are the single place where code meets environment. Goal: **the same code artifact runs in dev, CI, staging and production; only environment variables differ**. Values that vary per deployment never live in code; values that don't vary never live in the environment.

## How it works internally

- `django.conf.settings` is a `LazySettings` proxy. On first attribute access it imports the module named by `DJANGO_SETTINGS_MODULE`, copies every **UPPERCASE** module-level name onto a `Settings` object, and fills the rest from `django.conf.global_settings`. Lowercase names are ignored — that's why helpers like `env_bool` don't leak into settings.
- Settings are read once per process. Changing `os.environ` after startup has no effect; changing `settings.X` at runtime is unsupported outside tests.
- `settings.configure(**kwargs)` is the alternative for standalone use of Django components (no settings module); call it before `django.setup()`, and only once.
- Tests override with `override_settings` / `self.settings()`, which emit the `setting_changed` signal so cached consumers (e.g. template engines, `get_user_model` caches) reset.
- `python manage.py diffsettings` prints settings that differ from defaults (`--all` for everything) — the fastest way to see what a process actually runs with.

## Core concepts and APIs

| Setting | What to know |
|---|---|
| `INSTALLED_APPS` | Order matters for template/static/command override resolution and `ready()` order. Group: contrib, third-party, local. Use dotted app paths (`"apps.orders"`); Django picks the AppConfig automatically. |
| `MIDDLEWARE` | Ordered list; ordering rules in [middleware.md](./middleware.md). |
| `DATABASES` | `default` required. `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS` (≥ 4.1), `ATOMIC_REQUESTS`, `OPTIONS` (driver args; `"pool"` for psycopg 3 pooling, Django ≥ 5.1) — production tuning in [production-configuration.md](./production-configuration.md). |
| `TEMPLATES` | List of engine configs. `DIRS` for project templates, `APP_DIRS: True` for app templates, `context_processors` run on every `render()` with a request. |
| `STATIC_URL` / `STATIC_ROOT` / `STATICFILES_DIRS` | URL prefix, `collectstatic` target, extra source dirs. |
| `MEDIA_URL` / `MEDIA_ROOT` | User uploads; must differ from static. See [file-handling.md](./file-handling.md). |
| `STORAGES` | Django ≥ 4.2; `DEFAULT_FILE_STORAGE` / `STATICFILES_STORAGE` were removed in 5.1. |
| `DEFAULT_AUTO_FIELD` | Set to `"django.db.models.BigAutoField"` explicitly; the global default changed in Django 6.0 — check release notes for your version. |
| `AUTH_USER_MODEL` | Set before the first migration (`"accounts.User"`). |
| `SECRET_KEY` / `SECRET_KEY_FALLBACKS` | Fallbacks (≥ 4.1) enable rotation without logging everyone out. |

Check what the project runs on before relying on any version-gated setting: `python -m django --version`.

## Implementation examples

### Environment helpers and the required/optional rule

| Kind | Rule | Example |
|---|---|---|
| Required secret | `os.environ["X"]` — crash at import if absent | `DJANGO_SECRET_KEY`, DB password |
| Optional flag | explicit, **safe** default | `DJANGO_DEBUG` → `False` |
| List | comma-separated, empty items dropped | `DJANGO_ALLOWED_HOSTS` |
| Per-env tuning | default suited to production | `DJANGO_CONN_MAX_AGE` → `60` |

```python
# config/settings.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))

def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
SECRET_KEY_FALLBACKS = env_list("DJANGO_SECRET_KEY_FALLBACKS")
DEBUG = env_bool("DJANGO_DEBUG")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("DJANGO_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    }
}
```

Why crash on missing secrets: a process that starts with an empty `SECRET_KEY` or a default DB password fails later, in production, in a confusing way. Import-time `KeyError` fails the deploy instead.

Bad:

```python
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret")   # silently insecure in prod
DEBUG = os.environ.get("DJANGO_DEBUG", True)                     # any non-empty string is truthy: "False" → True
```

If the team already uses `django-environ` or `pydantic-settings`, keep it; don't add a dependency for a handful of variables. A local `.env` may be loaded by the shell/process manager; the app only reads `os.environ`.

### Templates

```python
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
```

Keep context processors cheap — they run for every template rendered with a request. When `DEBUG=False` and no `loaders` are set, Django uses the cached loader automatically.

### Static and media URLs

```python
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"           # collectstatic output, not in git
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))
```

### Custom settings

Namespace them, give defaults at the point of use, and document them:

```python
# config/settings.py
ORDERS_UNPAID_EXPIRY_HOURS = env_int("ORDERS_UNPAID_EXPIRY_HOURS", 24)

# apps/orders/services.py
from django.conf import settings

def expiry_hours() -> int:
    return getattr(settings, "ORDERS_UNPAID_EXPIRY_HOURS", 24)   # read at call time, not import time
```

Reading `settings` inside functions (not module level) keeps `override_settings` working in tests. Always `from django.conf import settings` — never `from config import settings`, which bypasses defaults and overrides.

### Configuration validation with system checks

```python
# apps/orders/checks.py
from django.conf import settings
from django.core.checks import Error, register, Tags

@register(Tags.compatibility)
def check_orders_settings(app_configs, **kwargs):
    value = getattr(settings, "ORDERS_UNPAID_EXPIRY_HOURS", 24)
    if not isinstance(value, int) or value <= 0:
        return [Error("ORDERS_UNPAID_EXPIRY_HOURS must be a positive int.", id="orders.E001")]
    return []

# apps/orders/apps.py → OrdersConfig.ready(): from . import checks  # noqa: F401
```

Pass `deploy=True` to `@register` for checks that should only run with `check --deploy`. Checks run on `runserver`, `migrate`, `check` and test startup, so misconfiguration is caught before traffic.

### Per-environment settings

Default: one `settings.py` driven by env vars. Split only when environments genuinely diverge:

```text
config/settings/
├── __init__.py
├── base.py      # everything shared
├── dev.py       # from .base import * ; debug tooling, console email
├── test.py      # fast hasher, locmem cache/email
└── prod.py      # from .base import * ; security flags (production-configuration.md)
```

Select with `DJANGO_SETTINGS_MODULE=config.settings.prod`. Child modules override or mutate `base` values (`LOGGING["root"]["level"] = "DEBUG"`); never copy whole dicts. Note `BASE_DIR` gains one `.parent` when moving into a package.

```python
# config/settings/test.py
from .base import *  # noqa: F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]   # tests only — speed
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
```

## Production patterns

- **Secrets**: injected by the platform (Kubernetes secrets, AWS/GCP secret managers, Vault) as env vars or mounted files. For file-mounted secrets read the file in settings (`Path(os.environ["DJANGO_SECRET_KEY_FILE"]).read_text().strip()`). Never commit `.env` files with real values; commit `.env.example` with names only.
- **Rotation**: put the new key in `SECRET_KEY`, the old in `SECRET_KEY_FALLBACKS`, deploy, wait for sessions/tokens to expire, remove the fallback.
- **Immutability**: settings are configuration, not state. Feature flags that change at runtime belong in the DB/cache or a flag service, not settings.
- **CI**: run checks with production-like env vars:

```bash
python manage.py check --deploy --fail-level WARNING
python manage.py makemigrations --check --dry-run
```

## Decision guidance

| Choice | Prefer | When the alternative fits |
|---|---|---|
| Single `settings.py` vs `settings/` package | Single file + env vars | Package when dev/test/prod differ by many non-scalar values (tooling apps, middleware) |
| `os.environ` vs settings library | `os.environ` + 3 helpers | Library already in use, or need typed nested config |
| Env var vs file-mounted secret | Whatever the platform provides | Files avoid secrets in `/proc/<pid>/environ` and crash dumps |
| Custom setting vs constant | Setting if it varies per deployment | Constant in the app module otherwise |

## Security considerations

- Safe defaults: `DEBUG` false, `ALLOWED_HOSTS` empty, secrets required. An unset env var must never produce an insecure config.
- Never log settings or render them in error pages; Django's debug page masks names containing `API`, `KEY`, `PASS`, `SECRET`, `SIGNATURE`, `TOKEN` — name secrets accordingly.
- Keep `local_settings.py`/`from .local import *` patterns out: they produce configs nobody reviewed.
- Hardening flags → [production-configuration.md](./production-configuration.md), [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- Settings import runs on every process start; avoid network calls (secret fetches, HTTP) at import unless cached by the platform.
- Accessing `settings.X` is a cheap attribute lookup after the first access; no need to copy into module globals (and doing so breaks `override_settings`).
- `CONN_MAX_AGE`, cached template loader and `CACHES` choice are the settings with the largest performance impact.

## Testing

- `@override_settings(ORDERS_UNPAID_EXPIRY_HOURS=1)` on tests/classes; `self.settings(...)` as a context manager.
- Test custom checks by calling the function directly and asserting the returned messages' `id`.
- Run the suite against `config.settings.test` (or env vars) — set via `pytest.ini` / `DJANGO_SETTINGS_MODULE`. See [django-testing](../../django-testing/SKILL.md).

## Common mistakes and anti-patterns

- `os.environ.get("DJANGO_DEBUG", False)` — returns the **string** `"False"`, which is truthy.
- Defaults that are only safe for development (`DEBUG=True`, `ALLOWED_HOSTS=["*"]`, sample secret).
- `from .local_settings import *` or committed `.env` files with real values.
- Copy-pasted dicts across `dev.py`/`prod.py` that drift apart.
- Reading settings at module import into constants (breaks overrides; stale values).
- Using deprecated/removed settings (`DEFAULT_FILE_STORAGE`, `STATICFILES_STORAGE` — removed in 5.1).
- Runtime mutation of `settings` in application code.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImproperlyConfigured: The SECRET_KEY setting must not be empty` | Env var missing/empty | Provide it; check the process manager passes env |
| `KeyError: 'DJANGO_SECRET_KEY'` at startup | Intended fail-fast | Set the variable for that environment |
| Setting change has no effect | Wrong `DJANGO_SETTINGS_MODULE`, or value cached at import | `manage.py diffsettings`; `print(settings.SETTINGS_MODULE)`; read at call time |
| `override_settings` ignored | Code copied the setting into a module constant | Read `settings.X` inside the function |
| Tests slow | Default PBKDF2 hasher | MD5 hasher in test settings |
| `ModuleNotFoundError: config.settings.prod` | Missing `__init__.py` or wrong path | Fix package; check `BASE_DIR` depth |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/settings/
- https://docs.djangoproject.com/en/stable/ref/settings/
- https://docs.djangoproject.com/en/stable/ref/checks/
- https://docs.djangoproject.com/en/stable/topics/checks/
- https://docs.djangoproject.com/en/stable/ref/django-admin/#diffsettings
- https://docs.djangoproject.com/en/stable/topics/testing/tools/#overriding-settings
- https://docs.python.org/3/library/os.html#os.environ
