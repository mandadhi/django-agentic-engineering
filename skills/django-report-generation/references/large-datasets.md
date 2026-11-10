# Reporting queries and large datasets

> **Superseded (legacy, retained for reference).** Do not load for new work. Its content is now
> owned by [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md),
> [background-report-generation.md](background-report-generation.md),
> [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) and
> [report-generation-performance.md](report-generation-performance.md).

## Reporting queries

```python
from django.db.models import Count, F, Q, Sum, Window
from django.db.models.functions import Coalesce, Rank, TruncMonth

# Grouped totals with conditional aggregates
summary = (Order.objects.filter(tenant_id=t, created_at__gte=start, created_at__lt=end)
           .values("region")
           .annotate(
               orders=Count("id"),
               paid=Sum("total", filter=Q(status="paid"), default=0),
               refunded=Sum("total", filter=Q(status="refunded"), default=0))
           .order_by("region"))

# Window functions over rows: rank within a partition, running total per customer
ranked = (Invoice.objects.filter(tenant_id=t, status="paid", issued_at__gte=start, issued_at__lt=end)
          .annotate(
              rank_in_region=Window(Rank(), partition_by=F("customer__region"), order_by=F("total").desc()),
              running_total=Window(Sum("total"), partition_by=F("customer_id"),
                                   order_by=[F("issued_at").asc(), F("id").asc()]))
          .values("number", "customer__region", "total", "rank_in_region", "running_total"))

# Running total over *grouped* results: group in SQL, accumulate the short list in Python
monthly = list(Invoice.objects.filter(tenant_id=t, status="paid")
               .annotate(month=TruncMonth("issued_at", tzinfo=REPORT_TZ))
               .values("month").annotate(revenue=Sum("total")).order_by("month"))
running = Decimal("0")
for row in monthly:
    running += row["revenue"]
    row["cumulative"] = running
```

Filtering on a window annotation (e.g. top 3 per region: `.filter(rank_in_region__lte=3)`) is
supported from Django 4.2.

- `default=` on aggregates (Django ≥ 4.0) avoids `None` for empty groups.
- Fill missing periods (months with no rows) in Python after the query — it's a small list.
- Half-open ranges `[start, end)` in the report time zone; convert to UTC for the filter.
- Ratios and percentages: compute from summed numerators/denominators, not averages of averages.
- Avoid joining two multi-valued relations in one aggregate query (fan-out double counting);
  use `Subquery` per measure or separate queries.

## Consistency

A report built from several queries must see one snapshot:

```python
with transaction.atomic(using="reports"):        # alias configured with REPEATABLE READ
    header = report_header(...)
    lines = list(report_lines(...))
```

Or read from a replica alias (`.using("replica")`) with a longer `statement_timeout`; accept
replication lag explicitly in the report's "as of" timestamp.

## Choosing the delivery mode

| Size / cost | Mode |
|---|---|
| < ~2 s, small output | synchronous `HttpResponse` |
| CSV of any size, simple rows | `StreamingHttpResponse` + `.iterator()` |
| Excel/PDF with many rows, or > ~10 s | background task → stored file → notify with link |
| Viewed often, changes slowly | pre-aggregated table or materialised view refreshed on a schedule |

## Background generation

```python
class ReportRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; RUNNING = "running"; READY = "ready"; FAILED = "failed"

    kind = models.CharField(max_length=40)
    params = models.JSONField()
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    file = models.FileField(upload_to="reports/%Y/%m/", blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(null=True, blank=True)

@shared_task(bind=True, acks_late=True, soft_time_limit=1800, time_limit=1900)
def generate_report(self, run_id: int) -> None:
    run = ReportRun.objects.get(pk=run_id)
    if run.status == ReportRun.Status.READY:
        return
    ReportRun.objects.filter(pk=run_id).update(status=ReportRun.Status.RUNNING)
    try:
        with tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024) as tmp:
            build_orders_xlsx(report_rows(run), tmp)            # streams from .iterator()
            tmp.seek(0)
            run.file.save(report_filename(run), File(tmp), save=False)
        run.status, run.expires_at = ReportRun.Status.READY, timezone.now() + timedelta(days=7)
        run.save(update_fields=["file", "status", "expires_at"])
        notify_report_ready.delay(run.pk)
    except SoftTimeLimitExceeded:
        ReportRun.objects.filter(pk=run_id).update(status=ReportRun.Status.FAILED, error="timeout")
        raise
```

- Re-check the requester's permissions inside the task (they may have changed since the request).
- Route to a `bulk` queue; see [django-background-processing](../../django-data-processing/references/django-background-processing.md).
- Deduplicate identical concurrent requests (same kind + params + user within N minutes).
- A scheduled task deletes expired files and rows.

## Download links

- Private storage + short-lived signed URLs (S3 presigned via `django-storages`
  `querystring_auth=True`, `querystring_expire=300`), generated by a view that checks the user
  may read this `ReportRun`.
- Or stream through a permission-checking view with `FileResponse(run.file.open("rb"),
  as_attachment=True, filename=...)` — fine for moderate traffic.
- Audit downloads of reports containing personal or financial data.

## Pre-aggregation

For dashboards over large fact tables:

- A summary table (`DailyRevenue(tenant, day, revenue, orders)`) maintained incrementally by a
  task (upsert yesterday + today), or
- A PostgreSQL materialised view created in a migration (`RunSQL`), refreshed with
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` (needs a unique index) from a scheduled task, and
  mapped with an unmanaged model (`managed = False`).

Record "data as of" on every pre-aggregated report.

## Memory checklist

- [ ] Rows come from `.iterator(chunk_size=…)` or `values_list()`.
- [ ] Excel uses `write_only=True`; CSV is streamed or written to a temp file.
- [ ] No list of all rows, no `pandas` DataFrame of the full dataset unless the worker is sized for it.
- [ ] Output written to `SpooledTemporaryFile`/storage, not held as `bytes` for large files.

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/db/aggregation/
- https://docs.djangoproject.com/en/stable/ref/models/expressions/#window-functions
- https://docs.djangoproject.com/en/stable/ref/models/database-functions/#trunc
- https://docs.djangoproject.com/en/stable/ref/request-response/#fileresponse-objects
- https://www.postgresql.org/docs/current/rules-materializedviews.html
- https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html
