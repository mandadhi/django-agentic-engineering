# ASGI and WSGI

> Scope: the two server interfaces, Django's application callables and initialisation,
> server integration (gunicorn, uvicorn, daphne, granian, hypercorn — concepts only), concurrency
> models and worker sizing, WebSockets/Channels concepts, and choosing an interface.
> Not covered here: async coding semantics ([asynchronous-django.md](./asynchronous-django.md)),
> the per-request pipeline ([request-response-lifecycle.md](./request-response-lifecycle.md)),
> production settings ([production-configuration.md](./production-configuration.md)),
> containers/cloud, load testing ([django-performance](../../django-performance/SKILL.md)).

## Purpose and overview

Django never listens on a socket in production. A **server** speaks HTTP and calls Django
through a standard Python interface:

| | WSGI (PEP 3333) | ASGI |
|---|---|---|
| Callable shape | `application(environ, start_response) -> iterable` | `async application(scope, receive, send)` |
| Model | One request per call, blocking | Event-driven; one call per connection |
| Protocols | HTTP request/response | HTTP, HTTP streaming, WebSocket (lifespan not handled by Django) |
| Concurrency unit | Process × thread | Process × event loop (+ thread pool for sync code) |
| Django module | `config/wsgi.py` | `config/asgi.py` |
| Django entry | `get_wsgi_application()` | `get_asgi_application()` |

Both serve the same views, middleware and settings. The choice is about the **concurrency model**
you need, not about features of your views.

## How it works internally

```python
# config/wsgi.py
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()
```

```python
# config/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_asgi_application()
```

- `setdefault` means the environment wins: deployments override `DJANGO_SETTINGS_MODULE`
  without editing the file.
- Both `get_*_application()` functions call `django.setup(set_prefix=False)`, which configures
  logging, populates the app registry (imports every app's `AppConfig` and models, runs
  `ready()`), then return a `WSGIHandler` / `ASGIHandler`. This runs **once per worker process**
  at import time — heavy work in `ready()` multiplies by worker count and slows every restart.
- The handler's `__init__` calls `load_middleware()`, building the middleware chain once
  (and, under ASGI, inserting sync/async adapters).
- Per request, `WSGIHandler` converts `environ` into a `WSGIRequest`; `ASGIHandler` reads the
  body from `receive` (spooling large bodies to a temp file), builds an `ASGIRequest`, runs the
  chain, and sends the response via `send` (streaming chunks for `StreamingHttpResponse`).
- `request_started` / `request_finished` signals fire around each request; `request_finished`
  closes DB connections older than `CONN_MAX_AGE`.
- Django's `ASGIHandler` only handles `http` scopes. It rejects `lifespan` (raising
  "Django can only handle ASGI/HTTP connections, not lifespan"); servers like uvicorn in
  `--lifespan auto` mode log this and continue — use `--lifespan off` to silence it. WebSocket
  scopes need Channels or another ASGI router in front of Django.

## Core concepts and APIs

- **Application path** handed to the server: `config.wsgi:application` or `config.asgi:application`.
- `runserver` is WSGI by default; if `daphne` is first in `INSTALLED_APPS`, Daphne's
  `runserver` serves ASGI in development (requires `ASGI_APPLICATION` setting).
- `WSGI_APPLICATION` / `ASGI_APPLICATION` settings are only used by `runserver` (and Channels),
  not by production servers.
- Middleware wrapping: you may wrap `application` with other ASGI/WSGI middleware (e.g. a
  static-file app or an observability wrapper) — wrap **after** `get_*_application()`.
- Importing models in `asgi.py` before `get_asgi_application()` raises `AppRegistryNotReady`;
  Channels routing that imports consumers must come after it.

## Implementation examples

Servers (illustrative commands — confirm flags in each server's docs, they change):

```bash
# WSGI, sync workers — CPU/DB-bound apps
gunicorn config.wsgi:application --workers 4 --timeout 30

# WSGI, threaded workers — moderate I/O wait, lower memory than more processes
gunicorn config.wsgi:application --workers 2 --threads 8 --worker-class gthread

# ASGI with uvicorn (multi-process)
uvicorn config.asgi:application --workers 4 --lifespan off

# ASGI under gunicorn process management with a uvicorn worker class
# (worker class lives in uvicorn or the separate `uvicorn-worker` package depending on version)
gunicorn config.asgi:application -k uvicorn_worker.UvicornWorker --workers 4

# Daphne (Channels' reference server) / Granian (Rust-based, WSGI or ASGI)
daphne config.asgi:application
granian --interface asgi config.asgi:application
```

WebSockets with Django Channels (third-party; concepts only):

```python
# config/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()        # initialise Django FIRST

from channels.auth import AuthMiddlewareStack               # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from apps.orders.routing import websocket_urlpatterns       # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
})
```

Channels adds consumers (WebSocket handlers) and a **channel layer** (usually Redis) so that
messages can reach connections held by other worker processes — required for broadcast across
more than one process.

## Production patterns

- Put a reverse proxy (nginx, load balancer) in front: it buffers slow clients, terminates TLS,
  enforces body limits and serves static/media files. Configure `SECURE_PROXY_SSL_HEADER` and
  `USE_X_FORWARDED_HOST` only when the proxy sets those headers
  ([production-configuration.md](./production-configuration.md)).
- **Worker sizing (starting points, then measure):**
  - WSGI sync: ~`2 × CPU + 1` processes; each holds one request at a time.
  - WSGI gthread: fewer processes × threads; good for moderate I/O wait in sync code.
  - ASGI: ~1 process per CPU; concurrency comes from the event loop, so only async views benefit.
  - Total DB connections ≈ processes × (threads or ASGI thread pool) — keep below the DB/pooler
    limit ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).
- Set request **timeouts** at the server and proxy; recycle workers periodically
  (e.g. gunicorn `--max-requests` with jitter) to contain memory leaks.
- Graceful shutdown: servers stop accepting, finish in-flight requests, then exit — keep request
  durations below the shutdown grace period.
- Keep a single codebase able to run under both: never put business logic in `wsgi.py`/`asgi.py`.

## Decision guidance

| Situation | Choose | Why |
|---|---|---|
| Classic server-rendered or REST app, sync code | WSGI (gunicorn sync/gthread) | Mature, simple, no adapter overhead |
| Some views fan out to slow external APIs | ASGI + async views for those | Waiting doesn't occupy a thread |
| Streaming, SSE, long-polling | ASGI | Holds many open connections cheaply |
| WebSockets, real-time push | ASGI + Channels (or a separate realtime service) | WSGI cannot do WebSockets |
| Mostly sync app, sync-only middleware | WSGI | ASGI would add thread hops for every request |
| Mixed needs | Two deployments of the same code: WSGI for the bulk, ASGI for real-time paths | Isolates failure modes and scaling |

Moving to ASGI does not speed up sync code; it can make it slightly slower. Choose it when you
have concurrency-shaped requirements.

## Security considerations

- Never run `runserver` in production — it is not security-audited or performance-tuned.
- `ALLOWED_HOSTS` applies to both interfaces; Channels WebSockets need
  `AllowedHostsOriginValidator` (or equivalent) to check `Origin`, since browsers don't apply CSRF
  to WebSocket handshakes.
- Don't trust `X-Forwarded-*` headers unless your proxy overwrites them.
- Run workers as an unprivileged user; the server, not Django, binds privileged ports or sits
  behind the proxy. Deep hardening → [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- App start-up cost (`django.setup()`) is paid per worker; gunicorn `--preload` loads the app
  once before forking (saves memory) but anything opened at import time (DB connections,
  sockets) is then shared across forks — don't open connections at import.
- Under ASGI, sync views and sync middleware run in a thread pool; heavy sync traffic on ASGI can
  bottleneck on that pool.
- Long requests tie up WSGI workers; move them to background tasks or ASGI streaming.
- Static files served by Python (e.g. WhiteNoise) are fine with a CDN in front; otherwise let the
  proxy serve them.

## Testing

- `Client` exercises the WSGI-style handler; `AsyncClient` exercises the ASGI handler — test
  async paths with the latter ([asynchronous-django.md](./asynchronous-django.md)).
- Smoke-test entry points in CI: `python -c "import config.wsgi, config.asgi"` with
  production-like environment variables catches import-time and settings errors.
- Channels provides `WebsocketCommunicator` for consumer tests.
- Load and concurrency testing → [django-performance](../../django-performance/SKILL.md).

## Common mistakes and anti-patterns

- Importing models or app code in `asgi.py` before `get_asgi_application()` → `AppRegistryNotReady`.
- Choosing ASGI "because it's newer" while all views and middleware are sync.
- Sizing workers without accounting for DB connections → exhausted connection limits.
- Expecting ASGI lifespan startup/shutdown hooks to run Django code — Django doesn't implement them.
- In-process WebSocket broadcast (a Python list of connections) in a multi-worker deployment —
  needs a channel layer.
- Hard-coding `DJANGO_SETTINGS_MODULE` with `os.environ[...] =` instead of `setdefault`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Workers killed with timeout | Slow upstream/DB; raise timeout only after finding the cause |
| `AppRegistryNotReady` at start | Import order in `asgi.py`/`wsgi.py` or code querying at import time |
| "Django can only handle ASGI/HTTP connections, not lifespan" | Harmless with lifespan auto; use `--lifespan off` |
| WebSocket 404/close on connect | No ASGI router for `websocket` scope, or Origin rejected |
| Async views behave like sync | Server is WSGI; check the command uses `config.asgi:application` |
| Memory grows per worker | Leak or large caches; enable worker recycling, then profile |
| `DisallowedHost` behind proxy | `ALLOWED_HOSTS` / forwarded host configuration |

## Official documentation

- https://docs.djangoproject.com/en/stable/howto/deployment/wsgi/
- https://docs.djangoproject.com/en/stable/howto/deployment/asgi/
- https://docs.djangoproject.com/en/stable/ref/applications/#how-applications-are-loaded
- https://docs.djangoproject.com/en/stable/topics/async/
- https://peps.python.org/pep-3333/
- https://asgi.readthedocs.io/en/latest/
- https://channels.readthedocs.io/
