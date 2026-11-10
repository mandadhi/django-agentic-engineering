# Database and Transaction Testing

> **Scope:** How test isolation works at the database level and how to test `atomic` blocks, `on_commit` callbacks, locking, races, multi-database routing and backend-specific behaviour — choosing the cheapest isolation that still exercises the real semantics. Transaction and locking *design* lives in django-orm.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · PostgreSQL 15+ · **Depends on:** **[Django]** `TestCase`, `TransactionTestCase`, `django.db.transaction`, `connections`; **[pytest-django]** `django_db(transaction=…, databases=…)`, `django_capture_on_commit_callbacks`; **[Python]** `threading`; **[third-party: testcontainers]** optional ephemeral DBs.

## 1. Purpose and scope

Covers:

- The isolation mechanics behind `TestCase` / `django_db` and `TransactionTestCase` / `transaction=True`, and how to choose.
- Testing rollback on error, savepoints and `IntegrityError` handling.
- Testing `on_commit` side effects without transactional tests.
- Concurrency tests: races, `select_for_update`, optimistic locking, deadlock/serialization retries.
- Deferrable constraints, multi-database tests and routers.
- SQLite vs PostgreSQL behavioural differences and PostgreSQL test setup.

Not covered: which test class to pick for non-DB reasons (→ [django-testing-framework.md](django-testing-framework.md)), constraint test basics (→ [model-and-orm-testing.md](model-and-orm-testing.md)), async DB access (→ [async-testing.md](async-testing.md)), locking design (→ [transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md)).

## 2. Core concepts

| Mechanism | Isolation strategy | Commits? | `on_commit` fires? | Cost |
|---|---|---|---|---|
| **[Django]** `TestCase` / **[pytest-django]** `@django_db` | Class-level `atomic` + per-test savepoint, rolled back | Never | No (capture it) | Cheap |
| **[Django]** `TransactionTestCase` / `@django_db(transaction=True)` | Real commits; tables **flushed** (TRUNCATE) after each test | Yes | Yes | 10–100× slower per test on large schemas |
| `serialized_rollback=True` | TTC + reloads data serialized at DB creation (e.g. data-migration rows) | Yes | Yes | Slower still |
| `reset_sequences=True` | TTC + resets PK sequences | Yes | Yes | Slower; encourages PK-coupled tests |
| `available_apps` (Django attr / pytest-django marker arg) | Limits which apps' tables are flushed | — | — | Speeds TTC; risky if wrong (pytest-django marker arg `VERIFY` version ≥ 4.6) |

Rules: **Use rollback isolation unless the code under test needs a real commit, a second connection, or another thread.**

## 3. Internal mechanics

### `TestCase`

1. `setUpClass` opens `atomic()` on every database in `databases`; `setUpTestData` runs inside it.
2. Each test opens a nested `atomic()` (savepoint); teardown rolls it back.
3. Because the outermost transaction never commits: `on_commit` callbacks are queued and discarded; `DEFERRABLE INITIALLY DEFERRED` constraints are **never checked** unless forced; other connections/threads **cannot see** test data.
4. **[Django]** `TestCase` runs `connection.check_constraints()` at the end of each test on backends that support it, which surfaces deferred FK violations; other deferred constraints may still go unchecked (`VERIFY` exact scope per backend).

### `TransactionTestCase`

- No wrapping transaction; code commits for real. After each test Django runs `flush` (TRUNCATE … CASCADE on PostgreSQL), wiping **all** rows — including rows created by data migrations. `serialized_rollback=True` restores them from a JSON snapshot taken at test DB creation.
- Django runs all `TestCase` tests before `TransactionTestCase` tests to limit cross-pollution; pytest-django reorders similarly (`VERIFY` for pytest-django ordering details).

### `on_commit` capture

**[Django]** `TestCase.captureOnCommitCallbacks(using="default", execute=False)` (Django ≥ 3.2) records callbacks registered in the block; with `execute=True` it runs them on exit. Callbacks that register further callbacks are also captured (Django ≥ 4.0, `VERIFY`). **[pytest-django]** `django_capture_on_commit_callbacks` is the fixture equivalent. Callbacks registered inside a savepoint that rolls back are dropped — matching production.

### Threads and connections

Django connections are per-thread. A new thread gets a new connection, which is outside the test transaction → it sees only committed data and must be closed explicitly, or the test DB cannot be dropped at session end ("database is being accessed by other users").

## 4. Engineering decisions and trade-offs

| Need | Use | Not |
|---|---|---|
| Assert side effect scheduled after commit | `django_db` + capture `execute=True` | `transaction=True` |
| Assert behaviour when commit fails / deferred constraint at COMMIT | `transaction=True` or `connection.check_constraints()` / `SET CONSTRAINTS ALL IMMEDIATE` inside `TestCase` | Ignoring deferred constraints |
| Race between two requests | `transaction=True` + threads, on PostgreSQL | SQLite; single-thread simulation only |
| Code that opens its own connection (Celery worker, `live_server`, `sync_to_async` thread) | `transaction=True` | Rollback isolation (data invisible) |
| Data from data migrations needed | `serialized_rollback=True`, or recreate via factories | Assuming rows exist after a TTC test |
| Lock semantics (`nowait`, `skip_locked`) | PostgreSQL + threads | Mocking `select_for_update` |

Concurrency tests are slow and prone to flakiness; write a few for the critical invariants (stock, balances, idempotency keys), not for every service.

## 5. Django implementation patterns

### Rollback on error inside a service

```python
@pytest.mark.django_db
def test_transfer_stock_rolls_back_when_destination_invalid():
    src = StockItemFactory(quantity=10)
    with pytest.raises(WarehouseClosed):
        transfer_stock(source_id=src.pk, destination_id=closed_warehouse().pk, quantity=4)
    src.refresh_from_db()
    assert src.quantity == 10
    assert not StockMovement.objects.exists()
```

### Catching `IntegrityError` and continuing

```python
@pytest.mark.django_db
def test_register_payment_is_idempotent_on_duplicate_key():
    first = register_payment(idempotency_key="k-1", amount=Decimal("50"))
    second = register_payment(idempotency_key="k-1", amount=Decimal("50"))  # service catches IntegrityError in atomic()
    assert second.pk == first.pk
    assert Payment.objects.count() == 1
```

If the service catches `IntegrityError` **without** its own `atomic()` savepoint, this test fails on PostgreSQL with "current transaction is aborted" — a real production bug the test correctly exposes. SQLite may not.

### `on_commit` side effects

```python
@pytest.mark.django_db
def test_invoice_issue_enqueues_pdf_after_commit(django_capture_on_commit_callbacks, mocker):
    delay = mocker.patch("apps.billing.tasks.render_invoice_pdf.delay")  # pytest-mock (third-party)
    invoice = InvoiceFactory(status="draft")

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        issue_invoice(invoice_id=invoice.pk)

    assert len(callbacks) == 1
    delay.assert_called_once_with(invoice.pk)
```

Django `TestCase` equivalent:

```python
class IssueInvoiceTests(TestCase):
    def test_enqueues_pdf(self):
        with mock.patch("apps.billing.tasks.render_invoice_pdf.delay") as delay, \
             self.captureOnCommitCallbacks(execute=True):
            issue_invoice(invoice_id=self.invoice.pk)
        delay.assert_called_once_with(self.invoice.pk)
```

Also test the negative: an exception after scheduling → `callbacks == []`.

### Race condition with threads (PostgreSQL)

```python
import threading
from django.db import connection, connections

@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_never_oversell():
    item = StockItemFactory(quantity=1)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=5)               # maximise overlap
            results.append(reserve_stock(item_id=item.pk, quantity=1))
        except OutOfStock:
            results.append("out_of_stock")
        except Exception as exc:                  # surface unexpected errors
            errors.append(exc)
        finally:
            connection.close()                    # per-thread connection

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)

    assert errors == []
    assert sorted(map(str, results)).count("out_of_stock") == 1
    item.refresh_from_db()
    assert item.quantity == 0
```

Notes:
- `transaction=True` is mandatory: threads use separate connections and must see committed rows.
- The barrier makes the race likely, not certain. To prove the *lock* works, also run the test against a deliberately unsafe version during development — it should fail.
- For deterministic interleaving, hold a lock explicitly in the test thread:

```python
@pytest.mark.django_db(transaction=True)
def test_reserve_with_nowait_raises_when_row_locked():
    item = StockItemFactory(quantity=5)
    locked = threading.Event(); release = threading.Event()

    def holder():
        with transaction.atomic():
            StockItem.objects.select_for_update().get(pk=item.pk)
            locked.set(); release.wait(timeout=5)
        connection.close()

    t = threading.Thread(target=holder); t.start()
    locked.wait(timeout=5)
    try:
        with pytest.raises(StockBusy):            # service uses select_for_update(nowait=True)
            reserve_stock(item_id=item.pk, quantity=1)
    finally:
        release.set(); t.join(timeout=5)
```

`select_for_update()` outside `atomic()` raises `TransactionManagementError` in autocommit — a cheap test catches services that forgot the transaction. On SQLite `select_for_update` is a no-op, so lock tests must be skipped there:

```python
requires_pg = pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL")
```

### Optimistic locking

```python
@pytest.mark.django_db
def test_stale_version_update_rejected():
    doc = DocumentFactory(version=3)
    update_document(doc_id=doc.pk, expected_version=3, title="A")
    with pytest.raises(StaleWrite):
        update_document(doc_id=doc.pk, expected_version=3, title="B")
```

No threads needed — staleness is simulated by sequencing.

### Retry on serialization failure / deadlock

Test the retry wrapper with a deterministic injected failure, not a real deadlock:

```python
from django.db import OperationalError

@pytest.mark.django_db
def test_settle_retries_serialization_failure(mocker):
    real = ledger.post_entries
    calls = {"n": 0}
    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("could not serialize access")  # VERIFY: match psycopg exception type your retry checks
        return real(*a, **kw)
    mocker.patch("apps.ledger.services.post_entries", side_effect=flaky)
    settle_account(account_id=AccountFactory().pk)
    assert calls["n"] == 2
```

Match what the retry decorator inspects (`pgcode` `40001`/`40P01` on the underlying `psycopg` error, `VERIFY`); a fake exception that lacks the attribute tests the wrong branch.

### Deferrable constraints (PostgreSQL)

```python
from django.db import connection

@pytest.mark.django_db
def test_position_swap_respects_deferred_unique():
    a = ItemFactory(position=1)
    b = ItemFactory(board=a.board, position=2)
    swap_positions(a.pk, b.pk)  # temporarily duplicates position mid-transaction
    with connection.cursor() as c:
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")   # raises IntegrityError now if the final state violates it
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.position, b.position) == (2, 1)
```

Without forcing, a `TestCase` never reaches COMMIT, so a deferred violation is silently accepted.

### Multi-database

```python
@pytest.mark.django_db(databases=["default", "analytics"])
def test_event_written_to_analytics_db():
    record_page_view(path="/pricing")
    assert PageView.objects.using("analytics").count() == 1

# Django: class AnalyticsTests(TestCase): databases = {"default", "analytics"}
```

Queries to a database not listed raise an assertion error — a useful guard. Router tests can be pure:

```python
def test_router_sends_reporting_reads_to_replica():
    assert ReportingRouter().db_for_read(ReportRow) == "replica"
```

For `TEST: {"MIRROR": "default"}` replicas, the replica alias reuses the default connection in tests, so replica-lag bugs are **not** exercised — test lag handling explicitly at the routing layer.

### SQLite vs PostgreSQL differences that change test outcomes

| Behaviour | SQLite (test default) | PostgreSQL |
|---|---|---|
| `select_for_update` | No-op | Row locks, `nowait`/`skip_locked`/`of` |
| Error after `IntegrityError` | Transaction usually still usable | Transaction aborted until savepoint rollback |
| String comparison / `LIKE` | Case-insensitive `LIKE` for ASCII | Case-sensitive; `ILIKE` for `icontains` |
| `JSONField` lookups | JSON1 extension; some lookups differ | Full `jsonb` operators |
| `ArrayField`, `HStoreField`, full-text, `ExclusionConstraint` | Unavailable | Available |
| Decimal precision / `max_digits` | Not enforced (stored as REAL/TEXT, `VERIFY`) | Enforced |
| Varchar length | Not enforced | Enforced |
| Deferrable constraints | Limited | Supported |
| Transactional DDL | Partly | Yes |
| Concurrency | Database-level write lock | MVCC, row locks |

Conclusion: run the suite on the production engine in CI; SQLite-only suites routinely pass with bugs.

### PostgreSQL test setup

- **Extensions:** create them in migrations (`django.contrib.postgres.operations.CreateExtension("pg_trgm")`) so the test DB gets them; or point `DATABASES["default"]["TEST"]["TEMPLATE"]` at a template DB that has them (requires superuser for some extensions).
- **CI-only speed settings** on a disposable server: `fsync=off`, `synchronous_commit=off`, `full_page_writes=off`. Never on any persistent DB. CI wiring → [ci-cd-test-automation.md](ci-cd-test-automation.md).
- **Ephemeral containers locally:** testcontainers **[third-party: testcontainers]**:

```python
# conftest.py (session-scoped; only if the project opts in)
@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from testcontainers.postgres import PostgresContainer
    pg = PostgresContainer("postgres:17").start()
    settings.DATABASES["default"].update(HOST=pg.get_container_host_ip(),
                                         PORT=pg.get_exposed_port(5432), USER=pg.username,
                                         PASSWORD=pg.password, NAME=pg.dbname)
```

(`VERIFY` hook choice and teardown; `django_db_modify_db_settings` is a pytest-django fixture intended for adjusting settings before DB setup.)

## 6. Production use cases

- **Inventory oversell:** threaded reservation test against PostgreSQL proves `select_for_update` or atomic `F()` decrement prevents negative stock.
- **Payment idempotency:** duplicate key path tested to return the original payment without aborting the transaction.
- **Outbox:** `on_commit` publishes only on commit; rollback path asserts no outbox dispatch.
- **Reporting replica:** router unit tests + one multi-DB integration test.
- **Search:** trigram extension created by migration so CI test DB matches production.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Rollback isolation | Fast, automatic cleanup | Hides commit-time behaviour, invisible to other connections |
| Transactional tests | Real commit semantics, threads work | Slow; flush wipes migration data; sequence/state leakage risks |
| Commit-callback capture | Fast proof of after-commit behaviour | Doesn't prove commit itself succeeds |
| Threaded race tests | Catch real concurrency bugs | Non-deterministic; PostgreSQL only; slow |
| Injected failure for retries | Deterministic | Must mirror real exception shape |

## 8. Security and performance considerations

- Concurrency bugs are security bugs when they involve money, quotas or one-time tokens (double-spend, reused reset token) — test those invariants under concurrency.
- Test credentials: CI DB users need `CREATEDB`; never run tests against a shared or production server — Django creates `test_<NAME>` on the same server and `--keepdb`/`--reuse-db` keep data around.
- Performance: each `transaction=True` test truncates every table; use `available_apps` or isolate them in a marked group. Keep thread tests few and with timeouts so a deadlock fails fast instead of hanging CI.

## 9. Common mistakes and anti-patterns

- Switching to `TransactionTestCase` "to make `on_commit` work".
- Thread tests under rollback isolation → threads don't see test data, test "passes" vacuously or errors.
- Forgetting `connection.close()` in threads → "database is being accessed by other users" at teardown.
- Lock tests on SQLite.
- `time.sleep()` to sequence threads instead of `Event`/`Barrier`.
- Assuming data-migration seed rows exist after a transactional test ran earlier.
- Asserting exact PKs (`id == 1`) — breaks with `--reuse-db`, ordering and parallelism.
- Deferred constraints never checked in `TestCase`.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| `current transaction is aborted, commands ignored` | Swallowed DB error without savepoint | `atomic()` around the failing statement (test or code) |
| `on_commit` callback never runs | Rollback isolation | Capture fixture with `execute=True` |
| Thread sees no rows | Test not transactional | `transaction=True` |
| Hang at session end / DB drop fails | Unclosed thread connections | `connection.close()` in `finally`; `connections.close_all()` |
| Test passes alone, fails after a TTC test | Flush removed seed data | `serialized_rollback=True` or factory-created data |
| `AssertionError: Database queries to 'x' are not allowed` | Missing `databases` | Add alias to `databases` / marker |
| Race test flaky | Overlap not forced | Barrier/Event; explicit lock holder |
| Lock test passes on SQLite falsely | No-op locks | Skip unless PostgreSQL |

Validation: grep for `transaction=True` / `TransactionTestCase` and justify each; ensure CI DB engine and major version match production; run concurrency tests repeatedly (`pytest --count=50`, pytest-repeat **[third-party]**) before trusting them.

## 11. Related references and skills

- Siblings: [django-testing-framework.md](django-testing-framework.md) · [model-and-orm-testing.md](model-and-orm-testing.md) · [service-and-architecture-testing.md](service-and-architecture-testing.md) · [async-testing.md](async-testing.md) · [ci-cd-test-automation.md](ci-cd-test-automation.md) · [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md)
- django-orm: [transactions-and-concurrency.md](../../django-orm/references/transactions-and-concurrency.md) · [database-constraints-and-indexes.md](../../django-orm/references/database-constraints-and-indexes.md) · [orm-fundamentals.md](../../django-orm/references/orm-fundamentals.md)
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — locking, isolation levels, configuration.

## 12. Official documentation and external references

- Django — The test database / order of test execution: <https://docs.djangoproject.com/en/5.2/topics/testing/overview/#the-test-database>
- Django — `TransactionTestCase`, `TestCase`, `captureOnCommitCallbacks`, multi-database support: <https://docs.djangoproject.com/en/5.2/topics/testing/tools/>
- Django — Advanced testing (`TEST` settings, `MIRROR`, `serialized_rollback`): <https://docs.djangoproject.com/en/5.2/topics/testing/advanced/>
- Django — Database transactions, `on_commit`: <https://docs.djangoproject.com/en/5.2/topics/db/transactions/>
- Django — Database-specific notes: <https://docs.djangoproject.com/en/5.2/ref/databases/>
- pytest-django — Database access and fixtures: <https://pytest-django.readthedocs.io/en/latest/database.html>
- PostgreSQL — Explicit locking: <https://www.postgresql.org/docs/current/explicit-locking.html>
- PostgreSQL — `SET CONSTRAINTS`: <https://www.postgresql.org/docs/current/sql-set-constraints.html>
- PostgreSQL — Non-durable settings: <https://www.postgresql.org/docs/current/non-durability.html>
- testcontainers-python (third-party): <https://testcontainers-python.readthedocs.io/>
