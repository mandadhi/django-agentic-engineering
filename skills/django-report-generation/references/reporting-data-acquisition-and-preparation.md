# Reporting Data Acquisition and Preparation

> **Scope:** How to obtain correct, consistent, reproducible report data with Django: aggregation
> query shapes, join fan-out, DB-side calculation, iteration and keyset chunking for exports,
> snapshot isolation and transaction boundaries, read models / materialized views / pre-aggregated
> tables, time zones and period boundaries, Decimal and currency, nulls and missing periods,
> historical (as-of) reporting and reproducibility metadata. **Not here:** general QuerySet and
> expression mechanics → [django-orm](../../django-orm/SKILL.md); generic ETL/transformation →
> [django-data-processing](../../django-data-processing/SKILL.md); query/index tuning method →
> [django-performance — database-performance](../../django-performance/references/database-performance.md).

## 1. Purpose and scope

Rendering a wrong number beautifully is the most common reporting failure. This reference covers the
part of the pipeline that decides whether numbers are right:

```text
scoped queryset ─► period filter (tz) ─► join without fan-out ─► aggregate in DB
   ─► consistent snapshot ─► zero-fill / null policy ─► Decimal rounding policy ─► dataset + data_as_of
```

It applies the ORM and data-processing skills to reporting; follow the links for the underlying
mechanics.

## 2. Core concepts

### 2.1 Report query shapes

| Shape | Example | Django tools |
|---|---|---|
| Grouped aggregate | Revenue per region per month | `values(...).annotate(Sum, Count)` [Django] |
| Detail listing | Every invoice in the period | `values_list(...).iterator(chunk_size=)` [Django] |
| Header + lines | Invoice + `InvoiceLine`s, payslip + lines | Two queries in one snapshot |
| Time series | Daily orders for 90 days | `Trunc*` with `tzinfo` + zero-fill in Python |
| Running totals / ranks | Cumulative revenue, top 10 customers per region | `Window` [Django]; filter on window (Django ≥ 4.2) |
| Multiple measures across relations | Billed vs paid per invoice | `Subquery` per measure (avoids fan-out) |
| Point-in-time | AR aging as of a past date | As-of filters on immutable/effective-dated data |

### 2.2 Correctness vocabulary

- **Fan-out**: joining two one-to-many relations multiplies rows; `SUM` then double counts.
- **Snapshot**: all queries of one report see the same committed database state.
- **Half-open period**: `[start, end)` — includes `start`, excludes `end`; no `23:59:59.999` hacks.
- **Data-as-of**: the moment the data reflects (transaction start, replica replay time, read-model
  refresh time). Printed on every report.
- **Reproducibility**: same definition version + same params + same data-as-of ⇒ same output.

## 3. Internal mechanics

### 3.1 GROUP BY in the ORM

`values("a", "b").annotate(total=Sum("x"))` groups by `a, b`. Any field in a later `order_by()` or a
model `Meta.ordering` can leak into the GROUP BY in some cases and split groups — always call
`order_by()` explicitly on report queries
([django-orm — aggregation-and-annotation](../../django-orm/references/aggregation-and-annotation.md)).

### 3.2 Why fan-out happens

```text
Invoice 1 ─┬─ line A 60   ─┬─ payment P1 50        JOIN produces 2×2 = 4 rows:
           └─ line B 40   ─┘─ payment P2 50        SUM(line_total) = 200 (true 100)
                                                   SUM(amount)     = 200 (true 100)
```

Each `Sum` across a second multi-valued relation is multiplied by the cardinality of the other.
`Count(..., distinct=True)` fixes counts, but `Sum(distinct=True)` is **wrong** (it collapses equal
amounts). Use one `Subquery` per measure.

### 3.3 Iteration and server-side cursors

`QuerySet.iterator(chunk_size=N)` [Django] streams results without filling the QuerySet cache. On
PostgreSQL it uses a server-side (named) cursor, which:

- holds a snapshot for its lifetime (consistent but long-running);
- breaks under PgBouncer **transaction** pooling unless `DISABLE_SERVER_SIDE_CURSORS = True`, which
  turns iteration into client-side fetching of the whole result
  ([django-orm — queryset-internals](../../django-orm/references/queryset-internals.md));
- with `prefetch_related`, requires `chunk_size` (Django ≥ 5.0 raises without it)
  [VERIFY: Django 5.0 release notes — removal of the deprecation started in 4.1].

### 3.4 Snapshot isolation in PostgreSQL

PostgreSQL's default `READ COMMITTED` gives each **statement** a fresh snapshot. A report that runs
"header totals" then "detail lines" can see different data in each. `REPEATABLE READ` [PostgreSQL]
gives the whole **transaction** one snapshot; read-only transactions at this level do not raise
serialization failures. `now()` [PostgreSQL] returns the transaction start time — a natural
`data_as_of`.

Costs: a long transaction on the primary holds back vacuum (table bloat); on a hot-standby replica,
long queries can be cancelled by replication conflicts depending on `max_standby_streaming_delay` /
`hot_standby_feedback` ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).

### 3.5 Time-zone truncation

`TruncMonth("issued_at", tzinfo=ZoneInfo(tenant.timezone))` [Django] makes PostgreSQL convert to the
tenant zone before truncating, so a 23:30 local invoice on 31 October lands in October even though
its UTC timestamp is in November. Without `tzinfo`, the current time zone (usually UTC in workers)
is used.

## 4. Engineering decisions and trade-offs

### 4.1 Where to read from

| Source | Freshness | Load on OLTP | Reproducible | Use when |
|---|---|---|---|---|
| Primary, live tables | Real time | Yes | No (data mutates) | Operational/transactional reports, small ranges |
| Read replica | Seconds of lag | No | No | Analytical reports that hurt the primary; print lag-aware data_as_of |
| Materialized view [PostgreSQL] | Last refresh | Refresh cost only | Per refresh | Medium aggregates, full recompute acceptable |
| Pre-aggregated table (incremental) | Last increment | Small, continuous | Per increment | Large fact tables, frequent dashboards |
| Snapshot / closed-period table | Fixed | None after close | Yes | Financial and regulatory reports |
| Stored artifact | Fixed | None | Yes (it *is* the record) | Issued documents, submitted filings |

### 4.2 Iteration strategy for detail exports

| Strategy | Snapshot | Pooler-safe | Memory | Notes |
|---|---|---|---|---|
| `.iterator(chunk_size=)` (server-side cursor) | One statement | No (transaction pooling) | Bounded | Simplest; long cursor |
| Keyset pagination (`pk > last`) | Per batch, unless wrapped in REPEATABLE READ | Yes | Bounded | Resumable/checkpointable |
| `OFFSET/LIMIT` | Per batch | Yes | Bounded | O(n²) cost, rows skipped/duplicated under writes — avoid |
| `list(qs)` | One statement | Yes | Unbounded | Small reports only |

### 4.3 ORM vs raw SQL

Prefer the ORM (`Subquery`, `Window`, filtered aggregates `Sum(..., filter=Q(...))`, `Coalesce`).
Use raw SQL when it is clearly simpler (recursive CTEs, `GROUPING SETS`/`ROLLUP` for subtotals),
always parameterised, with identifiers from allow-lists
([django-orm — raw-sql-and-database-access](../../django-orm/references/raw-sql-and-database-access.md)).

### 4.4 Database vs Python computation

| Compute in the database | Compute in Python |
|---|---|
| Sums, counts, group-bys, windows, filters | Zero-filling a short list of periods |
| Currency conversion via joined rate table | Cumulative totals over an already-grouped short list |
| Anything over many rows | Presentation rounding and labels |

Moving rows to Python to sum them costs memory, network and time, and invites float bugs.

## 5. Implementation patterns

### 5.1 Period boundaries in the tenant's time zone

```python
# reports/periods.py
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def local_period(start: date, end: date, tz_name: str) -> tuple[datetime, datetime]:
    """Half-open [start, end) of local calendar dates → aware datetimes (compared in UTC by Django)."""
    tz = ZoneInfo(tz_name)
    return (datetime.combine(start, time.min, tzinfo=tz),
            datetime.combine(end, time.min, tzinfo=tz))


def month_bounds(year: int, month: int, tz_name: str) -> tuple[datetime, datetime]:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return local_period(date(year, month, 1), nxt, tz_name)
```

- DST days are 23 or 25 hours long; building bounds from local dates handles that, adding
  `timedelta(hours=24)` does not.
- Filter by **range** on the raw column (`issued_at__gte`, `issued_at__lt`) so the index is usable;
  use `Trunc*`/`Extract*` only for grouping, not in `WHERE`.
- For `DateField` facts (`PayrollRun.period_end`, `Invoice.due_date`) no time-zone conversion applies;
  for `DateTimeField` facts it always does.
- ISO weeks, fiscal years and 4-4-5 calendars: compute boundaries in Python from the calendar
  definition, then filter by range.

### 5.2 Grouped time series with zero-fill

```python
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth


def monthly_revenue(*, tenant, user, year: int) -> list[dict]:
    start, end = local_period(date(year, 1, 1), date(year + 1, 1, 1), tenant.timezone)
    rows = (visible_invoices(tenant=tenant, user=user)
            .filter(status__in=["issued", "paid"], issued_at__gte=start, issued_at__lt=end)
            .annotate(month=TruncMonth("issued_at", tzinfo=ZoneInfo(tenant.timezone)))
            .values("month")
            .annotate(invoices=Count("id"), revenue=Sum("total", default=Decimal("0")))
            .order_by("month"))
    found = {r["month"].date(): r for r in rows}
    return [
        {"month": m,
         "invoices": found[m]["invoices"] if m in found else 0,
         "revenue": found[m]["revenue"] if m in found else Decimal("0.00")}
        for m in (date(year, i, 1) for i in range(1, 13))
    ]
```

`Sum(..., default=)` requires Django ≥ 4.0. Zero-fill in Python is fine: the list is at most a few
hundred periods.

### 5.3 Multiple measures without fan-out

```python
from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

MONEY = DecimalField(max_digits=14, decimal_places=2)

billed = (InvoiceLine.objects.filter(invoice=OuterRef("pk"))
          .values("invoice").annotate(s=Sum("line_total")).values("s"))
paid = (Payment.objects.filter(invoice=OuterRef("pk"), received_at__lt=OuterRef("as_of_cutoff"))
        .values("invoice").annotate(s=Sum("amount")).values("s"))

invoices = (visible_invoices(tenant=tenant, user=user)
            .filter(status="issued")
            .annotate(as_of_cutoff=Value(as_of_end))
            .annotate(
                billed=Coalesce(Subquery(billed, output_field=MONEY), Value(Decimal("0")), output_field=MONEY),
                paid=Coalesce(Subquery(paid, output_field=MONEY), Value(Decimal("0")), output_field=MONEY),
            ))
```

(Assumes `InvoiceLine.invoice` and `Payment.invoice` FKs; the payment cutoff makes the balance an
**as-of** figure.) Subqueries are evaluated per outer row; check the plan on large ranges and
consider a pre-aggregated per-invoice balance table if it is slow.

### 5.4 Consistent multi-query snapshot

Option A — a dedicated read alias whose connections are always `REPEATABLE READ` and read-only:

```python
# settings.py (PostgreSQL with psycopg 3)
from psycopg import IsolationLevel

DATABASES["reports"] = {
    **DATABASES["default"],                       # or the replica's HOST
    "OPTIONS": {
        "isolation_level": IsolationLevel.REPEATABLE_READ,
        "options": "-c statement_timeout=300000 -c default_transaction_read_only=on",
    },
}
```

Option B — per transaction on an existing alias:

```python
from contextlib import contextmanager

from django.db import connections, transaction


@contextmanager
def report_snapshot(alias: str = "default"):
    with transaction.atomic(using=alias):
        with connections[alias].cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SELECT now()")
            (data_as_of,) = cur.fetchone()
        yield data_as_of


with report_snapshot() as as_of:
    header = invoice_totals(tenant=t, start=s, end=e)          # all queries see one snapshot
    lines = list(invoice_lines(tenant=t, start=s, end=e))
```

`SET TRANSACTION` must be the first statement in the transaction
[VERIFY: no query is issued by Django/psycopg inside `atomic()` before the first `execute()` on this
connection — test with `connection.queries` on the target Django version]. Route queries explicitly
with `.using("reports")` (Option A) or a database router; never send writes to a read-only alias.
In tests, configure the alias with `TEST: {"MIRROR": "default"}` where appropriate.

### 5.5 Keyset iteration for large detail exports

```python
def iter_invoice_rows(qs, *, batch: int = 2_000):
    qs = qs.order_by("pk")
    last = None
    while True:
        page = qs if last is None else qs.filter(pk__gt=last)
        rows = list(page.values_list("pk", "number", "customer__name", "issued_at", "total")[:batch])
        if not rows:
            return
        yield from rows
        last = rows[-1][0]
```

Run inside `report_snapshot()` when the export must be internally consistent; otherwise rows changed
mid-export appear in their latest state. Keyset ordering needs an index whose leading columns match
the filter + order (`(tenant_id, id)` or `(tenant_id, issued_at, id)`).
Deep dive: [django-data-processing — large-dataset-processing](../../django-data-processing/references/large-dataset-processing.md).

### 5.6 Avoiding N+1 in report rows

- Prefer `values_list()` with `__` lookups (`customer__name`) — one joined query, no model instances.
- Never call per-row properties that query (`invoice.balance` doing `payments.aggregate`); annotate.
- For nested detail (invoice → lines in one PDF), `prefetch_related` with `.iterator(chunk_size=)`
  or fetch lines per batch of invoice ids.
- Assert query counts in tests ([report-generation-testing.md](report-generation-testing.md)).

### 5.7 Materialized view mapped to an unmanaged model

```python
# reports/migrations/0007_daily_revenue_mv.py
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("reports", "0006_previous"), ("billing", "0042_previous")]
    operations = [migrations.RunSQL(
        sql="""
        CREATE MATERIALIZED VIEW reports_daily_revenue AS
        SELECT i.tenant_id,
               (i.issued_at AT TIME ZONE t.timezone)::date AS day,
               i.currency,
               COUNT(*)      AS invoices,
               SUM(i.total)  AS revenue
        FROM billing_invoice i
        JOIN tenants_tenant t ON t.id = i.tenant_id
        WHERE i.status IN ('issued', 'paid')
        GROUP BY 1, 2, 3;
        CREATE UNIQUE INDEX reports_daily_revenue_uniq
            ON reports_daily_revenue (tenant_id, day, currency);
        """,
        reverse_sql="DROP MATERIALIZED VIEW IF EXISTS reports_daily_revenue;",
    )]
```

```python
class DailyRevenue(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "day", "currency")   # Django ≥ 5.2
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.DO_NOTHING)
    day = models.DateField()
    currency = models.CharField(max_length=3)
    invoices = models.IntegerField()
    revenue = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        managed = False
        db_table = "reports_daily_revenue"
```

[VERIFY: `CompositePrimaryKey` (Django 5.2) on an unmanaged model backed by a materialized view;
alternative on older versions: expose a synthetic text key column in the view and mark it
`primary_key=True`.] Refresh from a scheduled task with
`REFRESH MATERIALIZED VIEW CONCURRENTLY reports_daily_revenue` (requires the unique index; does not
block readers) and record the refresh time in a small `ReadModelRefresh(name, refreshed_at)` table
that selectors return as `data_as_of`. A materialized view recomputes **everything** on refresh —
fine for thousands of groups, costly for hundreds of millions of source rows.

### 5.8 Incremental pre-aggregated table

```python
from django.db import transaction

def refresh_daily_sales(*, tenant, days: list[date]) -> None:
    """Recompute whole days (idempotent). Include a late-arrival window, e.g. the last 3 days."""
    rows = compute_daily_sales(tenant=tenant, days=days)        # grouped ORM query → model instances
    with transaction.atomic():
        DailySalesSummary.objects.filter(tenant=tenant, day__in=days).delete()
        DailySalesSummary.objects.bulk_create(rows)
```

Delete-and-insert per day handles days whose orders were all voided (an upsert with
`bulk_create(update_conflicts=True, unique_fields=..., update_fields=...)` (Django ≥ 4.1) leaves
stale rows for groups that disappeared). Back-dated corrections need a wider recompute window or a
change-driven trigger ([django-orm — bulk-operations-and-batching](../../django-orm/references/bulk-operations-and-batching.md)).

### 5.9 Decimal, rounding and currency

```python
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

def money(value: Decimal, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    return value.quantize(CENT, rounding=rounding)

def share(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if not whole else (part / whole * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
```

- `DecimalField` → `Decimal` [stdlib] end to end; never `float()` money, never `FloatField` for amounts.
- The rounding mode is a **business decision** (`ROUND_HALF_UP` is common in invoicing; Python's
  default context uses `ROUND_HALF_EVEN`); record it in the definition.
- Round at presentation; compute totals from unrounded values — **except** documents that must sum
  visibly (invoices): print stored, already-rounded line totals and the stored invoice total, never
  recompute.
- Ratios: sum numerators and denominators, then divide; never average percentages.
- Never add amounts in different currencies. Group by `currency`, or convert with a dated rate table
  (rate date = transaction date or period end — a business rule recorded in params/metadata).
- Exact type mapping into each format is owned by the format references
  ([excel-report-generation.md](excel-report-generation.md),
  [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)).

### 5.10 Nulls and missing values

| Situation | Policy |
|---|---|
| `Sum` over no rows | `default=Decimal("0")` or `Coalesce`; decide whether the output shows `0.00` or "no data" |
| NULL group key (customer without region) | Keep as its own group labelled "Unassigned" — never drop silently |
| `Count("field")` | Skips NULLs; use `Count("id")` to count rows |
| `Avg` | Ignores NULLs; document whether missing means zero |
| Missing period | Zero-fill (5.2) for time series; omit for detail listings |
| NULL in CSV/XLSX | Empty cell, not the string `"None"` |

### 5.11 Historical and as-of reporting

| Technique | How | Strength | Cost |
|---|---|---|---|
| Immutable facts | Ledger entries / payments never updated; corrections are new entries | Any as-of is a filter on time | Requires append-only domain design |
| Snapshot on the fact | Store `customer_region`, `tax_rate` on `Invoice` at issue | Reports reflect what was true then | Denormalisation must be set at write time |
| Effective-dated rows | `valid_from` / `valid_to` on assignments, prices | Point-in-time joins | Every query needs the as-of predicate |
| Periodic snapshot tables | Month-end balances copied at close | Fast, simple queries | Only at snapshot granularity |
| History tables | django-simple-history [third-party: django-simple-history] or triggers | Retrofit onto mutable models | Storage; awkward aggregate queries |

```python
active_on = (Q(valid_from__lte=as_of) & (Q(valid_to__gt=as_of) | Q(valid_to__isnull=True)))
assignments = EmployeeAssignment.objects.filter(active_on, tenant=tenant)
```

Closed periods: once a `PayrollRun` is approved or an accounting period is closed, its reports read
only closed data; later corrections are adjustments in an open period, not edits to closed rows.

### 5.12 Reproducibility record

Record on `ReportRun` (canonical model in
[reporting-architecture-and-design.md](reporting-architecture-and-design.md)):

- `report_key`, `report_version`, `params`, `params_hash`;
- `data_as_of` (snapshot `now()`, replica replay time, or read-model refresh time);
- `row_count` and control totals (e.g. sum of `total`) for reconciliation;
- the application build identifier (e.g. an `APP_VERSION` environment value) in run metadata or logs.

Re-running a **live-data** report later will legitimately differ; reproducibility then depends on
immutable sources with as-of filters, or on keeping the artifact itself.

## 6. Production use cases

- **AR aging as of month-end**: `Invoice` + as-of payment subquery (5.3), buckets via `Case/When` on
  `due_date`, one snapshot, totals reconciled to the ledger control account.
- **Monthly payroll register**: reads `Payslip` for an approved `PayrollRun` (closed period) —
  reproducible by construction; department taken from the payslip snapshot, not the current `Employee`.
- **Weekly sales by region**: reads `DailySalesSummary` (5.8) for 7 days; `data_as_of` = summary refresh time.
- **Inventory valuation**: `StockMovement` summed up to the as-of date per `StockItem`, valued with the
  cost method's dated unit cost — never current stock × current price for a past date.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| DB-side aggregation | Fast, exact, low memory | Complex ORM expressions; backend-specific behaviour |
| REPEATABLE READ snapshot | Internally consistent reports | Long transactions: bloat on primary, conflicts on replicas |
| Server-side cursor iteration | Simple, consistent | Pooler incompatibility; long-lived cursor |
| Keyset iteration | Resumable, pooler-safe | Needs snapshot for consistency; index required |
| Materialized views | Declarative, fast reads | Full refresh cost; refresh lag |
| Incremental summary tables | Scales to large facts | Late-arriving data and deletions need care |
| As-of designs | Reproducible history | Must be designed into the domain early |

## 8. Security considerations

- Every report query starts from the scoped selector; a materialized view or summary table contains
  **all tenants** — give its model a tenant-required manager and test that no query omits the filter
  ([django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md)).
- Replicas and reporting aliases carry all data; credentials for them are as sensitive as the primary's.
  Prefer a read-only database role for the reporting alias.
- Raw SQL in `RunSQL` is static; raw SQL in selectors takes parameters only — never interpolated
  filters or sort keys ([django-orm — raw-sql-and-database-access](../../django-orm/references/raw-sql-and-database-access.md)).
- Row-level restrictions (regions, departments, cost centres) belong in the selector, not the renderer.

## 9. Performance considerations

- Index the leading filter columns of report queries, typically `(tenant_id, issued_at)` or
  `(tenant_id, status, issued_at)`; verify with `QuerySet.explain(analyze=True)` on production-sized
  data ([django-performance — database-performance](../../django-performance/references/database-performance.md)).
- Range filters on raw columns, not `__year`/`__month` lookups or truncations in `WHERE`.
- Set `statement_timeout` on the reporting alias so a bad report cannot run for hours.
- `values_list()` rows cost far less memory and CPU than model instances.
- Escalate to replica → summary table → materialized view only on measured need; each adds staleness.

## 10. Common mistakes and anti-patterns

- `Sum` across two multi-valued joins (fan-out), or "fixing" it with `Sum(distinct=True)`.
- `issued_at__date__range=(start, end)` with server-time-zone dates, or `<= end 23:59:59`.
- `TruncDay` without `tzinfo` in a Celery worker running in UTC.
- Float arithmetic, `round()` on floats, or rounding each line and then summing for an analytical total.
- Summing in Python over a full QuerySet.
- Header and lines read in separate `READ COMMITTED` statements for a financial document.
- `OFFSET` pagination for large exports.
- Reporting a past period from mutable current-state columns without disclosure.
- Dropping NULL-keyed groups so totals don't add up.
- Mixing currencies in one `Sum`.
- No `data_as_of` printed or stored.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Totals 2×–N× too high | Join fan-out | Inspect SQL (`str(qs.query)`); move measures to subqueries |
| First/last day missing or extra | Period in wrong tz or closed range | Print computed UTC bounds; use `local_period` |
| Monthly totals shift between months | `Trunc*` without `tzinfo` | Pass tenant `ZoneInfo` |
| Header total ≠ sum of lines | No snapshot | Wrap in `report_snapshot()` |
| `None` instead of 0 | Aggregate over empty set | `default=` / `Coalesce` |
| Cents off vs finance | Rounding mode or round-then-sum | Align rounding policy with definition |
| Different results each run for a closed period | Mutable sources | As-of design or stored artifact |
| Cursor errors behind PgBouncer | Server-side cursors + transaction pooling | `DISABLE_SERVER_SIDE_CURSORS` or keyset |
| Query slow only for big tenants | Missing/unused composite index | `explain(analyze=True)` |

Validation:

- [ ] Hand-computed fixture covering partial payments, voids, refunds, a DST day and a midnight-boundary record.
- [ ] Control totals reconcile with the source (count and sum) for the same snapshot.
- [ ] Query count bounded and independent of row count.
- [ ] Empty period, NULL keys and multi-currency cases tested.
- [ ] `data_as_of`, version and `params_hash` stored on the run.

## 12. Related references and official documentation

- [reporting-architecture-and-design.md](reporting-architecture-and-design.md) — selector contract, canonical models.
- [report-generation-performance.md](report-generation-performance.md) — when to pre-aggregate, budgets.
- [report-generation-testing.md](report-generation-testing.md) — calculation and reconciliation tests.
- [report-generation-troubleshooting.md](report-generation-troubleshooting.md) — symptom catalogue.
- [django-orm — aggregation-and-annotation](../../django-orm/references/aggregation-and-annotation.md)
- [django-orm — advanced-query-expressions](../../django-orm/references/advanced-query-expressions.md)
- [django-orm — queryset-internals](../../django-orm/references/queryset-internals.md)
- [django-orm — transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)
- [django-orm — query-optimization](../../django-orm/references/query-optimization.md)
- [django-data-processing — large-dataset-processing](../../django-data-processing/references/large-dataset-processing.md)
- [django-data-processing — data-transformation-and-mapping](../../django-data-processing/references/data-transformation-and-mapping.md)
- [django-data-processing — data-reconciliation-engineering](../../django-data-processing/references/data-reconciliation-engineering.md)
- [django-performance — database-performance](../../django-performance/references/database-performance.md)
- [postgresql-engineering](../../postgresql-engineering/SKILL.md)
- <https://docs.djangoproject.com/en/5.2/topics/db/aggregation/>
- <https://docs.djangoproject.com/en/5.2/ref/models/database-functions/#trunc>
- <https://docs.djangoproject.com/en/5.2/ref/models/expressions/#window-functions>
- <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator>
- <https://docs.djangoproject.com/en/5.2/ref/databases/#isolation-level>
- <https://docs.djangoproject.com/en/5.2/topics/db/optimization/>
- <https://docs.python.org/3/library/decimal.html>
- <https://www.postgresql.org/docs/16/transaction-iso.html>
- <https://www.postgresql.org/docs/16/sql-refreshmaterializedview.html>
