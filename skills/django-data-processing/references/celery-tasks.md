# Celery tasks

## Configuration

```python
# config/celery.py
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# settings.py
CELERY_BROKER_URL = os.environ["CELERY_BROKER_URL"]
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND")     # only if results are read
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]                                     # never pickle
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1                                # fair dispatch for long tasks
CELERY_TASK_TIME_LIMIT = 15 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 14 * 60
CELERY_TASK_ROUTES = {
    "apps.imports.tasks.*": {"queue": "bulk"},
    "apps.reports.tasks.*": {"queue": "bulk"},
    "apps.notifications.tasks.*": {"queue": "default"},
}
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}       # Redis: > longest task
```

- Use `ignore_result=True` (or no result backend) unless something reads results.
- Redis broker: `visibility_timeout` must exceed the longest task + retry delay or tasks are
  redelivered while still running.

## Task rules

```python
@shared_task(bind=True, autoretry_for=(TransientError,), retry_backoff=True, retry_backoff_max=600,
             retry_jitter=True, max_retries=5, acks_late=True)
def sync_customer(self, customer_id: int) -> None:
    customer = Customer.objects.filter(pk=customer_id).first()
    if customer is None:                       # deleted meanwhile — nothing to do
        return
    push_customer_to_crm(customer)             # service; idempotent (upsert by external id)
```

1. **Arguments are ids and primitives** (JSON-serialisable). Re-read state inside the task.
2. **Idempotent:** at-least-once delivery means duplicates happen. Check state before acting,
   use unique constraints/upserts, idempotency keys for external calls.
3. **Enqueue on commit:** `transaction.on_commit(lambda: sync_customer.delay(c.pk))`.
4. **Retry transient errors only** (network, 5xx, `OperationalError`); never validation errors.
5. **Time limits on every task**; catch `SoftTimeLimitExceeded` to clean up/mark the job.
6. **Thin tasks:** the body calls a service. The service is tested directly.
7. **No long transactions in tasks:** commit per batch.
8. Log task name, id, attempt and the domain ids (`self.request.id`, `self.request.retries`).

## Chunking large work

```python
@shared_task
def reprice_all(category_id: int) -> None:
    ids = list(Product.objects.filter(category_id=category_id).values_list("pk", flat=True))
    for start in range(0, len(ids), 500):
        reprice_batch.delay(ids[start:start + 500])

@shared_task(acks_late=True)
def reprice_batch(product_ids: list[int]) -> None:
    with transaction.atomic():
        for p in Product.objects.select_for_update().filter(pk__in=product_ids):
            ...
```

For millions of ids, page by pk ranges (`pk__gt=last_id ... [:500]`) instead of listing all ids.
Use `group`/`chord` only when you need a combined result; chords need a result backend.

## Concurrency control

- One job per key at a time: an advisory lock or cache lock
  (`cache.add(f"lock:import:{tenant_id}", 1, timeout)`), released in `finally`.
- Queue-like processing of DB rows: `select_for_update(skip_locked=True)`.

## Scheduling

```python
CELERY_BEAT_SCHEDULE = {
    "expire-unpaid-orders": {
        "task": "apps.orders.tasks.expire_unpaid_orders",
        "schedule": crontab(minute="*/15"),
        "options": {"expires": 600},        # drop if not started within 10 min
    },
}
```

- Run exactly **one** beat process. `django-celery-beat` if schedules must be edited at runtime.
- Periodic tasks must tolerate being skipped or run twice.

## Alternatives

- Django 6.0 adds `django.tasks` (`@task`, `.enqueue()`), a backend-agnostic interface; built-in
  backends are for development/testing, so production needs a third-party backend. Consider it for
  new projects that want a minimal API; existing Celery setups stay on Celery.
- Simpler stacks: RQ or Dramatiq — only if the project already uses them.

## Monitoring

- Queue length per queue, task runtime percentiles, failure/retry rates, worker heartbeats.
- Flower or the APM's Celery integration for visibility; alert on queue growth and stuck jobs.
- Dead-letter handling: after `max_retries`, record the failure on the domain object (job status)
  and alert — don't rely on logs alone.

## Testing

See [django-testing api-and-integration-tests](../../django-testing/references/api-and-integration-tests.md#celery-tasks).

## Official documentation

- https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
- https://docs.celeryq.dev/en/stable/userguide/tasks.html
- https://docs.celeryq.dev/en/stable/userguide/configuration.html
- https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- https://docs.celeryq.dev/en/stable/userguide/routing.html
- https://docs.djangoproject.com/en/stable/topics/tasks/
