# Signals and management commands

> Scope: Django's signal dispatcher, built-in and custom signals, registration, timing relative to transactions, `transaction.on_commit()`; management command architecture, arguments, validation, errors and reuse. Not covered here: transaction internals and locking queries ([django-orm](../../django-orm/SKILL.md)); background task queues ([django-performance](../../django-performance/SKILL.md), [django-architecture](../../django-architecture/SKILL.md)); app startup phases ([models-application-lifecycle.md](./models-application-lifecycle.md)); test infrastructure ([django-testing](../../django-testing/SKILL.md)).

## Purpose and overview

**Signals** are an in-process observer mechanism: a sender announces "this happened" and any connected receivers run **synchronously, in the same thread and transaction**. They exist to decouple apps that must not import each other — mostly for *framework* events (user logged in, model saved by the admin, request finished).

**Management commands** are the CLI entry points into the application (`python manage.py <name>`): batch jobs, maintenance, data fixes, cron targets. Like views, they are adapters — parse input, call a service, report.

## How it works internally

**Signals.** A `Signal` holds a list of weak references to receivers keyed by `(receiver_id, sender_id)`. `send(sender, **kwargs)`:

1. Looks up receivers matching `sender` (or connected with `sender=None`), caching the lookup when `use_caching=True` (model signals).
2. Calls each receiver **in connection order**, synchronously.
3. Returns `[(receiver, response), ...]`. An exception in a receiver **propagates** and aborts the remaining receivers and the caller — unless you use `send_robust()`, which catches and returns exceptions instead.

Since Django 5.0, receivers may be `async def`, and signals have `asend()`/`asend_robust()`. Sync `send()` runs async receivers via `async_to_sync`; `asend()` runs sync receivers via `sync_to_async`. Mixing works but costs thread/loop hops.

Receivers are weakly referenced by default; a receiver defined inside a function (closure, lambda) may be garbage-collected and silently stop firing. Use module-level functions or `weak=False`.

**Timing.** `post_save` fires inside `save()`, **before** the surrounding transaction commits. If the receiver enqueues a Celery task or sends an email and the transaction later rolls back, the side effect refers to data that never existed — or the worker runs before commit and can't find the row. `transaction.on_commit(fn)` defers `fn` until the outermost atomic block commits (runs immediately in autocommit; discarded on rollback). `on_commit(..., robust=True)` (Django ≥ 4.2) logs and swallows exceptions so one failing callback doesn't block the others.

**Management commands.** `manage.py` → `ManagementUtility` finds the command module by name across `INSTALLED_APPS` (`<app>/management/commands/<name>.py`, later apps in `INSTALLED_APPS` do *not* override earlier ones — the first app wins, and app commands override Django's built-ins), calls `django.setup()`, creates `Command()`, builds an `argparse` parser via `create_parser()` + `add_arguments()`, runs system checks if `requires_system_checks` says so, then `execute()` → `handle()`. A `CommandError` raised in `handle()` prints the message to stderr and exits with `returncode` (default 1) instead of a traceback (pass `--traceback` to see it).

## Core concepts and APIs

| Signals API | Notes |
|---|---|
| `@receiver(signal, sender=Model, dispatch_uid="...")` | Decorator; always pass `sender` for model signals |
| `Signal()` | Custom signal; document the kwargs it sends (`providing_args` was removed in 4.0) |
| `send` / `send_robust` / `asend` / `asend_robust` | Async variants ≥ 5.0 |
| `pre_save`, `post_save`, `pre_delete`, `post_delete`, `m2m_changed` | Model signals; `post_save` has `created`, `update_fields`, `raw` |
| `request_started`, `request_finished`, `got_request_exception` | Request signals |
| `user_logged_in`, `user_logged_out`, `user_login_failed` | Auth signals |
| `post_migrate`, `class_prepared`, `setting_changed` | Framework/test signals |
| `transaction.on_commit(fn, using=None, robust=False)` | `robust` ≥ 4.2 |

| Command API | Notes |
|---|---|
| `BaseCommand` | Subclass as `Command` |
| `help`, `add_arguments(parser)`, `handle(*args, **options)` | Core |
| `self.stdout` / `self.stderr`, `self.style.SUCCESS/ERROR/WARNING` | Use instead of `print` so `call_command` can capture |
| `CommandError(msg, returncode=...)` | Clean failure |
| `requires_system_checks` | `"__all__"` (default), a list of tags, or `[]` |
| `requires_migrations_checks` | Warn if unapplied migrations |
| `call_command(name, *args, stdout=..., **options)` | Programmatic invocation |
| `LabelCommand`, `AppCommand` | Per-label / per-app iteration |

## Implementation examples

**Why explicit calls beat signals for business flow.**

```python
# Bad: order placement silently triggers billing via a post_save receiver in another app.
@receiver(post_save, sender=Order)
def create_invoice(sender, instance, created, **kwargs):
    if created:
        Invoice.objects.create(order=instance, amount=instance.total)   # hidden step
        charge_card(instance)                                           # before commit!
```

Problems: a reader of `place_order` cannot see invoicing; `bulk_create` skips it; fixtures (`raw=True`) and the admin trigger it; ordering between receivers is implicit; the card is charged even if the transaction rolls back; tests must mute signals.

```python
# Good: the service states the workflow; side effects run after commit.
# apps/orders/services.py
from django.db import transaction
from apps.billing.services import create_invoice_for, charge_invoice

@transaction.atomic
def place_order(*, customer, cart) -> Order:
    order = Order.objects.create(customer=customer, total=cart.total)
    invoice = create_invoice_for(order)                         # same transaction
    transaction.on_commit(lambda: charge_invoice.delay(invoice.pk), robust=True)
    return order
```

Rule of thumb: if removing the receiver would make the feature *wrong* (not just less informed), it should be an explicit call.

**Appropriate signal use — reacting to framework events.**

```python
# apps/accounts/signals.py
import logging
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

logger = logging.getLogger(__name__)

@receiver(user_logged_in, dispatch_uid="accounts.log_login")
def log_login(sender, request, user, **kwargs):
    logger.info("auth.login", extra={"user_id": user.pk})
```

Registered by importing the module in `AccountsConfig.ready()` (see [models-application-lifecycle.md](./models-application-lifecycle.md)).

**Custom signal as an extension point.** Use when a *reusable* app wants to let unknown consumers react:

```python
# apps/orders/signals.py
from django.dispatch import Signal

order_paid = Signal()   # kwargs: order, paid_at

# apps/orders/services.py
def mark_paid(order, *, paid_at):
    ...
    transaction.on_commit(lambda: order_paid.send_robust(sender=Order, order=order, paid_at=paid_at))
```

Sending after commit plus `send_robust` means a buggy optional consumer cannot break payment.

**Management command.**

```python
# apps/orders/management/commands/expire_unpaid_orders.py
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from apps.orders.services import expire_unpaid_orders


class Command(BaseCommand):
    help = "Cancel unpaid orders older than --hours. Idempotent; safe to run from cron."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, hours: int, batch_size: int, dry_run: bool, **options):
        if hours < 1:
            raise CommandError("--hours must be >= 1")
        count = expire_unpaid_orders(older_than=timedelta(hours=hours), batch_size=batch_size, dry_run=dry_run)
        verb = "would expire" if dry_run else "expired"
        self.stdout.write(self.style.SUCCESS(f"{verb}={count}"))
```

The service is idempotent (`filter(status=PENDING, created_at__lt=cutoff)` then transitions — re-running finds nothing new), processes in batches each in its own transaction, and supports `dry_run`.

## Production patterns

- **Commands are thin.** All logic in services so the same operation is callable from a task, an admin action or a test.
- **Idempotent and resumable.** Batch by primary key ranges, commit per batch, make re-runs safe. A crash at batch 40 of 100 should not require manual cleanup.
- **Concurrency.** Cron overlap is common. Prevent double execution with a DB advisory lock (PostgreSQL `pg_try_advisory_lock`), a cache lock (`cache.add(key, ...)` with timeout, best-effort), or `select_for_update(skip_locked=True)` when claiming rows. Details in django-orm / postgresql-engineering.
- **Exit codes matter.** Schedulers alert on non-zero exit; raise `CommandError` for expected failures; let unexpected exceptions crash (with logging).
- **Logging over stdout for ops.** `self.stdout` for human output; `logger.info(...)` with structured fields for monitoring.
- **Defaults are safe.** Destructive commands require explicit flags (`--confirm`) and default to dry-run where reasonable.
- **Scheduling.** Cron/Kubernetes CronJob/systemd timers call commands; Django 6.0 adds a built-in background tasks interface (`django.tasks`) — check its release notes and backend availability before relying on it; Celery/RQ remain common.
- **Signals.** Always `dispatch_uid`; handle `raw=True` in `post_save` (fixture loading) by returning early; keep receivers fast and side-effect-free until commit.

## Decision guidance

| Situation | Choose |
|---|---|
| Step required for correctness of a workflow | Explicit service call |
| Reacting to Django/contrib events (login, migrate, request finished) | Signal |
| Reusable app offering hooks to unknown consumers | Custom signal, sent after commit, `send_robust` |
| Cache invalidation on model change | Signal is acceptable, but remember bulk ops skip it; prefer invalidating in the service that writes |
| Side effect outside the DB (email, queue, webhook) | `transaction.on_commit` |
| Recurring/batch job | Management command calling a service |
| One-off data fix | Data migration if it must run everywhere once; command if operator-triggered |
| Long-running/user-triggered async work | Task queue, not a command spawned from a view |

## Security considerations

- Commands run with full DB privileges and bypass view-level auth; never shell out with interpolated user-provided strings (`subprocess` with lists, never `shell=True`).
- Don't print secrets or PII in command output; CI and cron logs are widely readable.
- Signals receiving `request` (auth signals) must not log credentials or raw request bodies.
- `post_save` receivers that grant permissions or create accounts are an audit blind spot — prefer explicit, logged service calls.

## Performance considerations

- Every `save()` pays for every connected model receiver; a slow receiver slows the admin, imports and all writes. Measure with logging/profiling (django-performance).
- `pre_save`/`post_save` receivers doing queries create N+1 patterns in loops.
- Connecting with `sender=None` to model signals makes the receiver run for **every** model.
- Commands loading full tables into memory: iterate with `.iterator(chunk_size=...)` and batch writes with `bulk_update`/`bulk_create` (note: these bypass signals).
- Disable `requires_system_checks` (`[]`) for very frequent, lightweight commands if checks are slow — only when CI runs checks separately.

## Testing

```python
from io import StringIO
from django.core.management import call_command, CommandError

def test_expire_dry_run_changes_nothing(stale_order):
    out = StringIO()
    call_command("expire_unpaid_orders", "--hours=1", "--dry-run", stdout=out)
    stale_order.refresh_from_db()
    assert stale_order.status == Order.Status.PENDING
    assert "would expire=1" in out.getvalue()

def test_invalid_hours():
    with pytest.raises(CommandError):
        call_command("expire_unpaid_orders", hours=0)

def test_charge_scheduled_after_commit(django_capture_on_commit_callbacks, customer, cart, mocker):
    charge = mocker.patch("apps.orders.services.charge_invoice.delay")
    with django_capture_on_commit_callbacks(execute=True):   # pytest-django; TestCase: self.captureOnCommitCallbacks
        place_order(customer=customer, cart=cart)
    charge.assert_called_once()
```

- `TestCase` wraps tests in a transaction that never commits, so `on_commit` callbacks don't run unless captured.
- Test receivers by triggering the event and asserting the effect; test that the receiver is connected (otherwise a broken `ready()` import goes unnoticed).
- Use `signal.disconnect()`/mocks sparingly; needing to mute signals in many tests is a sign the logic should be explicit.

## Common mistakes and anti-patterns

- Business-critical workflow chained through `post_save` receivers.
- Side effects (email, HTTP, queue) in receivers without `on_commit`.
- Defining receivers in `models.py` or importing signals modules from several places → duplicate registration; use `ready()` + `dispatch_uid`.
- Expecting signals on `QuerySet.update()`, `bulk_create()`, `bulk_update()`.
- Calling `instance.save()` inside its own `post_save` receiver → recursion.
- Commands with logic in `handle()`, `print()` output, no exit codes, no dry-run, not idempotent.
- `sys.exit()` in a command — breaks `call_command` in tests; raise `CommandError`.
- Using `call_command` from views for heavy work — blocks the request.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Receiver never fires | Module never imported; wrong `sender`; weak ref to local function GC'd | Import in `ready()`; check sender; module-level function |
| Receiver fires twice | Imported via two module paths or registered in two places | `dispatch_uid`; single import site |
| Worker can't find the row | Task enqueued before commit | `transaction.on_commit` |
| Side effects after rollback | Same | Same |
| `Unknown command` | App not in `INSTALLED_APPS`, missing `__init__.py` in `management/` or `commands/` | Add packages/app |
| Command output missing in tests | `print` used | `self.stdout.write` |
| Positional option ignored in `call_command` | Options passed by kwarg use `dest` names | Use `dest` names or CLI-style strings |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/signals/
- https://docs.djangoproject.com/en/stable/ref/signals/
- https://docs.djangoproject.com/en/stable/topics/db/transactions/#performing-actions-after-commit
- https://docs.djangoproject.com/en/stable/howto/custom-management-commands/
- https://docs.djangoproject.com/en/stable/ref/django-admin/#running-management-commands-from-your-code
