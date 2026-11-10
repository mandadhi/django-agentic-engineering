# Raw SQL and Database Access

> Scope: when hand-written SQL is justified; `Manager.raw()`, `RawSQL`, `connection.cursor()`
> and `connections[alias]`; parameterised queries and SQL-injection prevention; safe dynamic
> SQL (identifiers, sort keys); result mapping; stored procedures; database-specific SQL;
> transactions with raw SQL; ORM/raw interoperability.
> Not covered here: expression-based alternatives
> ([advanced-query-expressions.md](./advanced-query-expressions.md)); `RunSQL` in migrations
> ([migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md)); transaction
> semantics ([transactions-and-concurrency.md](./transactions-and-concurrency.md)); general
> application security and threat modelling ([django-security-engineering](../../django-security-engineering/SKILL.md));
> PostgreSQL features and tuning ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

## 1. Purpose and scope

The ORM is the default data-access layer. Raw SQL is a tool for the cases where the ORM cannot
express the operation, expresses it with a materially worse plan, or where a database feature
(recursive CTEs, `LATERAL`, `DISTINCT ON` variants, `COPY`, advisory locks, vendor functions)
is the right solution. This reference defines when that line is crossed and how to cross it
without losing safety, transactions or maintainability.

SQL examples use PostgreSQL syntax unless marked; they were not executed.

## 2. Core concepts

| Tool | Returns | Use when |
|---|---|---|
| ORM + expressions | QuerySet | Default — almost always |
| `RawSQL(sql, params, output_field=)` | Expression inside a QuerySet | One fragment the ORM lacks; keep the rest ORM |
| `Model.objects.raw(sql, params, translations=)` | `RawQuerySet` of model instances | Full custom SELECT that maps to one model |
| `connection.cursor()` | DB-API cursor, tuples | Non-model results, DDL, vendor commands, writes the ORM can't express |
| `connections["alias"].cursor()` | Same, on another configured DB | Replica/reporting/legacy database |

**Parameter binding**: SQL text and values travel separately (`cursor.execute(sql, params)`).
The driver (psycopg, mysqlclient, sqlite3, oracledb) sends values as data; they can never change
the statement's structure. Django uses the `%s` placeholder style on all backends (it converts
for SQLite's `?` and Oracle's `:arg`). Placeholders must **not** be quoted: `'%s'` is wrong.

## 3. Internal mechanics

- `Manager.raw()` returns a lazy `RawQuerySet`. On iteration Django executes the SQL, maps
  columns to model fields **by name** (use `translations={"sql_col": "field"}` or `AS` aliases),
  and creates instances. The primary key column is mandatory; omitted fields become deferred and
  load lazily on access (one query each). Extra columns become attributes. It does not support
  `filter()`, `exclude()` or chaining; indexing/slicing happens in Python, not as `LIMIT`.
- `RawSQL` is compiled verbatim into the ORM's SQL, its params are merged into the query's
  params. Django does not parse it: aliases, joins and quoting inside are your responsibility.
- `connection.cursor()` returns a `CursorWrapper` (or `CursorDebugWrapper` when `DEBUG=True`,
  which records statements in `connection.queries`). It uses the **same connection** as the ORM
  in that thread/alias, so it participates in any open `transaction.atomic()` block.
- With `params` supplied, a literal `%` in SQL must be written `%%` (e.g. `LIKE 'abc%%'`); with no
  params, the docs' example uses a single `%`
  ([docs](https://docs.djangoproject.com/en/5.2/topics/db/sql/#executing-custom-sql-directly)).
  VERIFY: the docs do not state that every backend/driver leaves `%` untouched when no params
  are passed — test on your backend.
- `QuerySet.query` printed as `str()` interpolates params for display only — it is **not** safe
  or valid SQL to execute.

## 4. Engineering decisions and trade-offs

Raw SQL is justified when at least one holds — state which one in a code comment:

1. The ORM cannot express it: recursive CTE (category trees), `LATERAL` joins, `UNNEST`-based
   bulk updates, `INSERT ... SELECT`, advisory locks, `COPY`.
2. The ORM's SQL has a measured, materially worse plan and no expression rewrite fixes it
   (show the `EXPLAIN ANALYZE` before/after).
3. A vendor feature is the right tool and the project has committed to that backend.
4. Legacy/external database whose schema doesn't fit models.

Not justified: "I know SQL better", avoiding learning `Subquery`/`Window`, premature performance.

| Concern | ORM | Raw SQL |
|---|---|---|
| Schema changes (renamed columns) | Caught at query build | Breaks at runtime; grep needed |
| Portability | Backend-neutral | Usually vendor-specific |
| Injection risk | Bound by construction | Safe only if parameterised |
| Composition (filters, pagination) | Native | Manual |
| Signals/model hooks | Fired by `save()`/`delete()` | Never |
| Plan control | Indirect | Direct |

Prefer the smallest escape hatch: `Func`/`Lookup` → `RawSQL` fragment → `raw()` → cursor.

## 5. Django implementation patterns

### Parameterised cursor query with dict mapping

```python
# apps/inventory/selectors.py
from django.db import connection

def dictfetchall(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def low_stock_report(*, warehouse_id: int, threshold: int) -> list[dict]:
    sql = """
        SELECT p.sku, p.name, s.quantity_on_hand - s.quantity_reserved AS available
        FROM inventory_stockitem s
        JOIN catalog_product p ON p.id = s.product_id
        WHERE s.warehouse_id = %s
          AND s.quantity_on_hand - s.quantity_reserved < %s
        ORDER BY available
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [warehouse_id, threshold])
        return dictfetchall(cursor)
```

(This particular query is expressible in the ORM — shown for mechanics. Prefer the ORM version.)
Hard-coded table names couple to `db_table`; use `Model._meta.db_table` and
`connection.ops.quote_name()` if the names may change.

### Recursive CTE: a justified case

```python
def category_descendant_ids(root_id: int) -> list[int]:
    sql = f"""
        WITH RECURSIVE tree AS (
            SELECT id FROM {Category._meta.db_table} WHERE id = %s
            UNION ALL
            SELECT c.id FROM {Category._meta.db_table} c JOIN tree t ON c.parent_id = t.id
        )
        SELECT id FROM tree
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [root_id])
        return [row[0] for row in cursor.fetchall()]

Product.objects.filter(category_id__in=category_descendant_ids(root_id), is_active=True)
```

The f-string inserts only a value from model metadata (trusted, not user input); the user value
goes through `%s`. Alternatives: a materialized path / `ltree` column or a tree library, if tree
queries are frequent.

### Manager.raw() mapping to models

```python
products = Product.objects.raw(
    """
    SELECT p.id, p.sku, p.name, p.price, ts_rank(p.search_vector, q) AS rank
    FROM catalog_product p, plainto_tsquery('english', %s) q
    WHERE p.search_vector @@ q AND p.is_active
    ORDER BY rank DESC
    LIMIT 50
    """,
    [term],
)
for p in products:
    p.rank          # extra column exposed as attribute
    p.category      # deferred/related access → extra query per row (N+1)
```

(`search_vector` is a hypothetical column; `django.contrib.postgres.search` usually removes the
need for raw SQL here.) Put `LIMIT` in SQL — slicing a `RawQuerySet` does not add one.

### RawSQL fragment inside a QuerySet

```python
from django.db.models import BooleanField
from django.db.models.expressions import RawSQL

Order.objects.annotate(
    in_business_hours=RawSQL(
        "EXTRACT(HOUR FROM orders_order.placed_at AT TIME ZONE %s) BETWEEN 9 AND 17",
        (tz_name,),
        output_field=BooleanField(),
    )
).filter(status=Order.Status.PAID)
```

`RawSQL` breaks if the table is aliased (e.g. in subqueries) — prefer `Func`/`ExtractHour`.

### Safe dynamic SQL: identifiers and sort keys

Values → parameters. Identifiers (tables, columns, sort direction) cannot be parameters; they
must come from an **allow-list** and be quoted.

```python
SORTABLE = {"sku": "p.sku", "name": "p.name", "available": "available"}

def build_order_by(sort: str, direction: str) -> str:
    column = SORTABLE.get(sort)
    if column is None:
        raise ValueError("unsupported sort")
    return f"{column} {'DESC' if direction == 'desc' else 'ASC'}"
```

For PostgreSQL with psycopg 3, `psycopg.sql.SQL`/`sql.Identifier` compose identifiers safely —
but only for allow-listed names; quoting doesn't make an arbitrary user-chosen table acceptable.
In ORM code the same rule applies to `order_by(user_value)` and `F(user_value)`.

Never:

```python
cursor.execute(f"SELECT * FROM orders_order WHERE customer_id = {customer_id}")      # injection
cursor.execute("... WHERE email = '%s'" % email)                                      # injection
cursor.execute("... WHERE email = '%s'", [email])                                      # quoted placeholder: broken
Order.objects.raw("SELECT * FROM orders_order WHERE status = '" + status + "'")        # injection
```

`IN` lists: generate placeholders, not values — `",".join(["%s"] * len(ids))` — or on PostgreSQL
use `= ANY(%s)` with a list parameter.

### Writes and transactions

```python
from django.db import transaction

@transaction.atomic
def release_expired_reservations(cutoff):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE inventory_stockitem s
            SET quantity_reserved = s.quantity_reserved - r.qty
            FROM (SELECT stock_item_id, SUM(quantity) AS qty
                  FROM inventory_reservation WHERE expires_at < %s GROUP BY stock_item_id) r
            WHERE s.id = r.stock_item_id
            """,
            [cutoff],
        )
        released = cursor.rowcount
    Reservation.objects.filter(expires_at__lt=cutoff).delete()   # same transaction
    return released
```

(`inventory_reservation` is illustrative.) Raw writes skip `save()`, signals, `auto_now`, and
cache invalidation; trigger side effects explicitly and via `transaction.on_commit`.

### Stored procedures and vendor features

```python
with connection.cursor() as cursor:
    cursor.callproc("recalculate_customer_ltv", [customer_id])   # DB-API callproc; backend support varies
```

`callproc` support depends on the driver (not available on SQLite; `kparams` only on Oracle —
[docs](https://docs.djangoproject.com/en/5.2/topics/db/sql/#calling-stored-procedures)). On
PostgreSQL ≥ 11 procedures are invoked with `CALL proc(...)` via `execute`. psycopg 3 does
**not** implement `cursor.callproc()` — use `execute("SELECT fn(%s)", ...)` or
`execute("CALL proc(%s)", ...)` ([psycopg 3](https://www.psycopg.org/psycopg3/docs/basic/from_pg2.html));
psycopg2's `callproc` calls functions, not procedures. Keep procedures in migrations (`RunSQL`) so schema and
code stay in sync.

### Multiple databases

```python
from django.db import connections

with connections["reporting"].cursor() as cursor:
    cursor.execute("SELECT ...", params)
```

`transaction.atomic(using="reporting")` controls that alias; transactions never span aliases.

## 6. Production use cases

- **Hierarchies**: recursive CTE for category descendants (above).
- **Bulk set-based updates** (`UPDATE ... FROM`, `UNNEST`) where `bulk_update`'s `CASE` statement
  is measured to be slow — see [bulk-operations-and-batching.md](./bulk-operations-and-batching.md).
- **Advisory locks** (PostgreSQL `pg_advisory_xact_lock`) to serialise a job per tenant.
- **Reporting queries** on a replica with window/CTE combinations that are clearer as SQL.
- **Legacy databases**: `managed = False` models plus raw SQL for awkward joins.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Full access to database features and plan shape | Bypasses model hooks, signals, validation |
| Can be dramatically faster for set-based work | Vendor lock-in; fails on other backends in tests |
| Clear for complex analytics | Not refactor-safe; schema renames break silently |
| No ORM object overhead with cursors | Injection risk if discipline lapses; harder review |

## 8. Security and performance considerations

- **Never interpolate user-controlled values into SQL.** String formatting lets input become
  syntax (`' OR 1=1 --`), bypassing tenant filters, leaking data or modifying rows. Parameters
  are the only defence for values; allow-lists for identifiers. Escaping by hand is not a
  substitute.
- Tenant scoping in raw SQL is manual — every query needs the tenant predicate; review for it.
- Raw SQL used in tests on SQLite while production is PostgreSQL hides breakage; run tests on
  the production engine ([django-testing](../../django-testing/SKILL.md)).
- `DEBUG=True` logs SQL with values; don't log raw SQL with params containing personal data
  ([django-core logging](../../django-core/references/logging-exceptions.md)).
- Database role privileges limit blast radius — application roles should not own the schema.
- `fetchall()` on huge results loads everything; iterate with `fetchmany(n)` or a named
  server-side cursor (PostgreSQL) for streaming.

## 9. Common mistakes and anti-patterns

- Raw SQL for queries the ORM expresses fine (adds maintenance with no gain).
- f-strings / `%` formatting / concatenation with any external value.
- Quoting placeholders (`'%s'`).
- `extra()` for new code — legacy API; use expressions or `RawSQL`.
- Omitting the primary key from `raw()` (raises `FieldDoesNotExist: Raw query must include the primary key`).
- Accessing related objects on `raw()` results in a loop (N+1).
- Hard-coding table names that differ from `db_table`.
- Running raw writes outside `atomic` when they must commit with ORM writes.
- Swallowing `DatabaseError` from cursor code inside `atomic` without a nested savepoint.

## 10. Troubleshooting and validation

| Symptom | Cause | Fix |
|---|---|---|
| `IndexError: tuple index out of range` / "not all arguments converted" | Mismatched placeholders/params or unescaped `%` | Count `%s`; use `%%` for literals |
| `raw()` instance fields load lazily | Column missing or misnamed | Alias columns or `translations=` |
| Works on PostgreSQL, fails in tests | SQLite test DB | Test on the production engine; gate with `connection.vendor` |
| `TransactionManagementError` after a failed statement | Error inside `atomic` | Nested `atomic()` around the risky statement |
| Raw write not visible in cached views | Bypassed invalidation | Invalidate explicitly on commit |

Validation: code review checklist — every value via params; every identifier from an allow-list;
justification comment present; tests cover empty and malicious input (`"'; DROP TABLE"` must be
treated as data). Static checks: Bandit `B608` (hardcoded_sql_expressions, string-built SQL),
`B610` (django_extra_used), `B611` (django_rawsql_used)
([Bandit plugins](https://bandit.readthedocs.io/en/latest/plugins/index.html)).

## 11. Related references and skills

- [advanced-query-expressions.md](./advanced-query-expressions.md) — expression alternatives.
- [bulk-operations-and-batching.md](./bulk-operations-and-batching.md) — set-based writes.
- [transactions-and-concurrency.md](./transactions-and-concurrency.md) — locks, `atomic`, advisory locks context.
- [migrations-and-schema-evolution.md](./migrations-and-schema-evolution.md) — `RunSQL`.
- [orm-anti-patterns.md](./orm-anti-patterns.md) — unnecessary raw SQL; unsafe dynamic SQL.
- [django-security-engineering](../../django-security-engineering/SKILL.md) · [postgresql-engineering](../../postgresql-engineering/SKILL.md)

## 12. Official documentation and external references

- Performing raw SQL queries: https://docs.djangoproject.com/en/stable/topics/db/sql/
- `RawSQL`: https://docs.djangoproject.com/en/stable/ref/models/expressions/#raw-sql-expressions
- Multiple databases: https://docs.djangoproject.com/en/stable/topics/db/multi-db/
- Security — SQL injection: https://docs.djangoproject.com/en/stable/topics/security/#sql-injection-protection
- psycopg 3 SQL composition: https://www.psycopg.org/psycopg3/docs/api/sql.html
- PostgreSQL WITH queries: https://www.postgresql.org/docs/current/queries-with.html
- OWASP SQL Injection Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html
