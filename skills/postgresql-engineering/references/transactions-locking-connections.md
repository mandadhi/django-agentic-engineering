# Transactions, locking and connections

Django's transaction API (`atomic`, `on_commit`, `select_for_update`) is in
[django-orm](../../django-orm/references/querysets-and-transactions.md). This file covers the
engine behaviour behind it.

## Isolation levels

| Level | Behaviour | Use |
|---|---|---|
| Read committed (default) | each statement sees data committed before it started | almost everything; combine with row locks or atomic `UPDATE ... SET x = x - 1` |
| Repeatable read | snapshot for the whole transaction; concurrent update → serialization error | consistent multi-query reads (reports) |
| Serializable | as if transactions ran one at a time; may abort with `40001` | complex invariants across rows without explicit locks |

Set per connection in Django via `OPTIONS: {"isolation_level": IsolationLevel.SERIALIZABLE}`
(psycopg 3), or in a dedicated `DATABASES` alias for report jobs. Code under repeatable read or
serializable **must retry** on `40001`/`40P01`:

```python
from django.db import OperationalError, transaction
from psycopg import errors

def run_serializable(fn, attempts: int = 3):
    for attempt in range(attempts):
        try:
            with transaction.atomic(using="serializable"):
                return fn()
        except OperationalError as exc:
            if isinstance(exc.__cause__, (errors.SerializationFailure, errors.DeadlockDetected)) \
                    and attempt < attempts - 1:
                continue
            raise
```

## Row locks

| Django | SQL | Blocks |
|---|---|---|
| `select_for_update()` | `FOR UPDATE` | other `FOR UPDATE`, updates, deletes, FK inserts referencing the row |
| `select_for_update(no_key=True)` | `FOR NO KEY UPDATE` | other updates — but not FK inserts (prefer when not changing the key) |
| `select_for_update(skip_locked=True)` | `... SKIP LOCKED` | nothing — skips locked rows (job queues) |
| `select_for_update(nowait=True)` | `... NOWAIT` | errors immediately instead of waiting |
| `select_for_update(of=("self",))` | `FOR UPDATE OF t` | locks only the main table in a join |

Advisory locks (`pg_advisory_xact_lock(key)`) serialise work that isn't tied to one row (e.g.
"only one import per tenant at a time").

## Deadlocks

PostgreSQL detects them after `deadlock_timeout` and aborts one transaction. Prevent them:

- Lock rows in a deterministic order (`order_by("pk")` before `select_for_update`).
- Keep transactions short; don't mix bulk updates and row-by-row locks on the same table in
  different orders.
- Investigate with the server log (`log_lock_waits = on`) and `pg_locks` joined to `pg_stat_activity`.

## Timeouts

| Setting | Purpose | Typical web value |
|---|---|---|
| `statement_timeout` | cap any single statement | 5–30 s (longer on a reports alias) |
| `lock_timeout` | cap waiting for a lock | 2–5 s; always set for migrations |
| `idle_in_transaction_session_timeout` | kill sessions left in an open transaction | 30–60 s |
| `connect_timeout` (client) | fail fast when DB is unreachable | 5 s |

Set them on the database role (`ALTER ROLE app SET statement_timeout = '15s'`) or via
`OPTIONS["options"]`.

## Connections

Each Django process/thread holds its own connection. Sizing:

```
needed = web_processes × threads_per_process (+ async concurrency) + worker_processes × concurrency
needed ≤ max_connections − superuser/maintenance headroom
```

Options:

1. **`CONN_MAX_AGE = 60` + `CONN_HEALTH_CHECKS = True`** — persistent connection per worker;
   simple, fine for modest worker counts.
2. **Django's psycopg 3 pool** (Django ≥ 5.1, `OPTIONS["pool"]`) — per-process pool; requires
   `CONN_MAX_AGE = 0`. Good for threaded/async servers.
3. **PgBouncer in transaction mode** — when many processes or serverless instances share a DB.
   Then: `CONN_MAX_AGE = 0` (or short), `DISABLE_SERVER_SIDE_CURSORS = True` (otherwise
   `.iterator()` breaks), no session-level state (`SET`, advisory session locks, `LISTEN`).
   Prepared statements need PgBouncer ≥ 1.21 with `max_prepared_statements`.

Async views: each concurrently awaiting ORM call uses a thread and a connection; size the pool
for the concurrency you allow.

## Monitoring queries

```sql
-- long-running and idle-in-transaction sessions
SELECT pid, state, now() - xact_start AS xact_age, wait_event_type, left(query, 120)
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle'
ORDER BY xact_start NULLS LAST;

-- who blocks whom
SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid, left(blocked.query, 80) AS blocked_query
FROM pg_stat_activity blocked
JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
JOIN pg_stat_activity blocking ON blocking.pid = b.pid;

-- tables needing vacuum attention
SELECT relname, n_dead_tup, n_live_tup, last_autovacuum
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;
```

## Official documentation

- https://www.postgresql.org/docs/current/transaction-iso.html
- https://www.postgresql.org/docs/current/explicit-locking.html
- https://www.postgresql.org/docs/current/runtime-config-client.html
- https://www.postgresql.org/docs/current/monitoring-stats.html
- https://docs.djangoproject.com/en/stable/ref/databases/#connection-pool
- https://docs.djangoproject.com/en/stable/ref/databases/#transaction-pooling-server-side-cursors
- https://www.pgbouncer.org/features.html
