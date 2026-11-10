# Scheduled Reports and Distribution

> **Scope:** Recurring report generation and delivery: `ReportSchedule` configuration,
> time-zone-aware next-run computation (DST gaps/folds, month-end clamping), the dispatcher
> pattern, Celery beat vs django-celery-beat, idempotent run creation per schedule period,
> subscriptions and recipient management, re-authorisation at send time, links vs attachments,
> `ReportDelivery` status, retries, duplicate-notification prevention, delivery audit, failed
> delivery recovery, expiration and retention, and four worked examples. **Not here:** the
> generation job itself ([background-report-generation.md](background-report-generation.md)),
> download endpoints and signed links ([django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)),
> email provider mechanics ([django-integrations — email-sms-and-notification-integrations](../../django-integrations/references/email-sms-and-notification-integrations.md)),
> retention policy ([reporting-security-and-compliance.md](reporting-security-and-compliance.md)).

## 1. Purpose and scope

A scheduled report is two independent processes joined by a `ReportRun`: **generation** (produce
one correct artifact per schedule period) and **distribution** (deliver it once to each
currently-authorised recipient). Keeping them separate means a failed email is retried without
regenerating a payroll register, and a regenerated report does not spam recipients.

This reference covers everything between "the clock says it is time" and "each recipient has
received exactly one notification", using the canonical `ReportSchedule`, `ReportRecipient`,
`ReportDelivery` and `ReportRun` models from
[reporting-architecture-and-design.md](reporting-architecture-and-design.md).

## 2. Core concepts

| Concept | Definition |
|---|---|
| Schedule | Recurrence rule in the **owner's business time zone**: frequency, local time, weekday/day-of-month, report key, params, format |
| Period | The business interval the run covers (e.g. 2026-10-01 to 2026-11-01 for October) — distinct from the time it runs |
| Fire time | Instant (UTC) the dispatcher should create the run: `next_run_at` |
| Subscription / recipient | Who receives the run; internal user or explicitly approved external email |
| Delivery | One attempt-tracked transmission of one run to one recipient over one channel |
| Idempotency key | Deterministic identifier preventing duplicate runs (per schedule+period) and duplicate deliveries (per run+recipient+channel) |

### Schedules are data, beat entries are infrastructure

Hard-coding each customer report as a Celery beat entry does not scale and cannot express
per-tenant time zones. The standard design is **one beat entry** (`dispatch_due_schedules`,
every minute) plus **schedule rows** in the database.

| Mechanism | Owns | Fit |
|---|---|---|
| Celery beat static `beat_schedule` [third-party: celery] | Fixed platform jobs (dispatcher, reaper, cleanup) | Few, code-defined jobs; one `timezone` setting for all |
| django-celery-beat [third-party: django-celery-beat] | DB-stored periodic tasks editable in admin; per-crontab timezone [VERIFY: `CrontabSchedule.timezone` support in installed version] | Ops-managed jobs; not per-tenant report schedules with business params |
| `ReportSchedule` + dispatcher (this reference) | Business schedules, periods, recipients | Per-tenant, per-user report subscriptions |
| Platform cron + management command [external tool] | Dispatcher trigger without beat | Projects without Celery |

Run exactly **one** beat process; two beats double every dispatch (the dispatcher's locking makes
that harmless, but platform jobs without idempotency are not).

### Canonical schedule, recipient and delivery models

These complete the canonical model set; `ReportRun`/`ReportArtifact` live in
[reporting-architecture-and-design.md](reporting-architecture-and-design.md) and `ReportDownload`
in [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).

```python
# reports/models.py (continued)
class ReportSchedule(models.Model):
    class Frequency(models.TextChoices):
        DAILY = "daily"; WEEKLY = "weekly"; MONTHLY = "monthly"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="report_schedules")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    report_key = models.CharField(max_length=100)
    params = models.JSONField(encoder=DjangoJSONEncoder)        # relative period params, e.g. {"period": "previous_month"}
    format = models.CharField(max_length=10)
    frequency = models.CharField(max_length=10, choices=Frequency)
    run_at_local = models.TimeField()                           # wall-clock time in `timezone`
    weekday = models.PositiveSmallIntegerField(null=True, blank=True)       # 0 = Monday (weekly)
    day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)  # 1–31, clamped (monthly)
    timezone = models.CharField(max_length=64)                  # IANA name, validated with zoneinfo
    next_run_at = models.DateTimeField(db_index=True)           # aware UTC
    is_active = models.BooleanField(default=True)


class ReportRecipient(models.Model):
    schedule = models.ForeignKey(ReportSchedule, on_delete=models.CASCADE, related_name="recipients")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    email = models.EmailField(blank=True)                       # approved external recipients only
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=Q(user__isnull=False) | ~Q(email=""), name="recipient_user_or_email")]


class ReportDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"; SENDING = "sending"; SENT = "sent"; FAILED = "failed"; BOUNCED = "bounced"

    run = models.ForeignKey("reports.ReportRun", on_delete=models.PROTECT, related_name="deliveries")
    recipient = models.ForeignKey(ReportRecipient, on_delete=models.PROTECT, related_name="deliveries")
    channel = models.CharField(max_length=10)                   # "email" | "webhook" | "sftp"
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    attempt = models.PositiveSmallIntegerField(default=0)
    idempotency_key = models.CharField(max_length=200, unique=True)   # f"{run_id}:{recipient_id}:{channel}"
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)   # error code, not provider payloads
```

`CheckConstraint(condition=...)` is the Django ≥ 5.1 spelling (earlier versions use `check=`)
[VERIFY: Django 5.1 release notes for the `check` → `condition` rename]. Scheduled runs are made
unique per period by the `uniq_scheduled_run_per_period` constraint on `ReportRun` (below and in
the architecture reference).

## 3. Internal mechanics

### Time-zone-aware next run [stdlib]

Store `run_at_local` (wall-clock time) and `timezone` (IANA name), compute `next_run_at` as an
aware UTC instant. Never store "06:00 UTC" for a schedule a user defined as "06:00 Berlin" — it
drifts by an hour at every DST change.

```python
import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

def _local_to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    naive = datetime.combine(day, at)
    aware = naive.replace(tzinfo=tz, fold=0)                    # fold=0: first occurrence in an overlap
    if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        # Non-existent local time (spring-forward gap): run at the first valid instant after it.
        aware = naive.replace(tzinfo=tz, fold=1)                # fold=1 maps to the post-transition offset
    return aware.astimezone(UTC)

def _clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))

def _add_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)

def compute_next_run(schedule, *, after: datetime) -> datetime:
    tz = ZoneInfo(schedule.timezone)
    local_after = after.astimezone(tz)
    day = local_after.date()
    for _ in range(400):                                        # bounded search
        if schedule.frequency == "daily":
            candidate_day = day
        elif schedule.frequency == "weekly":
            candidate_day = day + timedelta(days=(schedule.weekday - day.weekday()) % 7)
        else:  # monthly
            candidate_day = _clamp_day(day.year, day.month, schedule.day_of_month)
        fire = _local_to_utc(candidate_day, schedule.run_at_local, tz)
        if fire > after:
            return fire
        day = (candidate_day + timedelta(days=1)) if schedule.frequency != "monthly" \
            else _clamp_day(*_add_month(candidate_day.year, candidate_day.month), 1)
    raise ValueError("no next run found")
```

[VERIFY: zoneinfo `fold=1` on a non-existent time yields the post-transition offset, i.e. 02:30 in a spring-forward gap becomes 03:30 local — confirm against PEP 495 semantics and test with `Europe/Berlin` and `America/New_York`]

Policy decisions to state explicitly (and test):

- **Gap** (02:30 doesn't exist): run at the shifted instant, or at the next valid wall-clock time.
- **Fold** (01:30 occurs twice): run once, at the first occurrence (`fold=0`).
- **Day 31 in a 30-day month**: clamp to month end (`_clamp_day`), or skip the month — clamp is
  the usual business expectation for "last day" style schedules; better still, offer an explicit
  "last day of month" option.
- Avoid scheduling business reports between 00:00 and 03:00 local; most DST transitions fall there.

### Period derivation

The period is computed from the **fire time in the schedule's time zone**, not from `now()` when
the task happens to run (late dispatch must not shift the period):

| Frequency | Period covered (half-open, local) |
|---|---|
| Daily | previous local day `[D-1 00:00, D 00:00)` |
| Weekly | previous ISO week `[Mon-7, Mon)` |
| Monthly | previous calendar month `[first of prev month, first of this month)` |

Period boundaries are converted to aware datetimes per
[reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md).

### Dispatcher [Django] + [third-party: celery]

```python
@shared_task(queue="reports-maintenance", ignore_result=True)
def dispatch_due_schedules(batch: int = 200) -> None:
    now = timezone.now()
    with transaction.atomic():
        due = list(ReportSchedule.objects
                   .select_for_update(skip_locked=True)
                   .filter(is_active=True, next_run_at__lte=now)
                   .order_by("next_run_at")[:batch])
        for schedule in due:
            fire_at = schedule.next_run_at
            create_scheduled_run(schedule, fire_at=fire_at)      # idempotent, enqueues on commit
            schedule.next_run_at = compute_next_run(schedule, after=fire_at)
            schedule.save(update_fields=["next_run_at"])
```

- `select_for_update(skip_locked=True)` [Django] + [PostgreSQL] lets several dispatchers (or an
  accidental second beat) share work without blocking or double-processing.
- `next_run_at` advances from `fire_at`, not from `now`, so a dispatcher outage produces one
  catch-up run per missed period — or, by policy, only the latest (set `after=now` and record
  skipped periods). Financial and payroll reports usually require every period; operational
  dashboards usually only the latest.

### Idempotent run creation per schedule period

```python
def create_scheduled_run(schedule, *, fire_at) -> ReportRun:
    period = period_for(schedule, fire_at)                       # (start, end, label)
    params = {**schedule.params, "period_start": period.start.isoformat(),
              "period_end": period.end.isoformat(), "period_label": period.label}
    spec = get_spec(schedule.report_key)
    digest = params_hash(key=spec.key, version=spec.version, fmt=schedule.format,
                         scope="tenant", params=params)
    run, created = ReportRun.objects.get_or_create(
        schedule=schedule, params_hash=digest, format=schedule.format,
        defaults=dict(tenant=schedule.tenant, requested_by=None, report_key=spec.key,
                      report_version=spec.version, params=params))
    if created:
        transaction.on_commit(lambda: generate_report.delay(str(run.pk)))
    return run
```

Back `get_or_create` with a `UniqueConstraint(fields=["schedule", "params_hash", "format"],
condition=Q(schedule__isnull=False), name="uniq_scheduled_run_per_period")` so a race cannot
create two runs for one period.

### Distribution after generation

```text
ReportRun SUCCEEDED ──on_commit──▶ fan_out_deliveries(run_id)
        for each active ReportRecipient:
            authorised now?  ── no ──▶ ReportDelivery(status=failed, last_error="not_authorised") + audit
                │ yes
                ▼
            get_or_create ReportDelivery(idempotency_key=f"{run_id}:{recipient_id}:{channel}")
                │ created or status in (pending, failed & attempts left)
                ▼
            send_delivery.delay(delivery_id)  ──▶ provider ──▶ sent / failed / bounced (webhook)
```

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Alternative | Trade-off |
|---|---|---|---|
| Schedule storage | `ReportSchedule` rows + 1-minute dispatcher | beat entry per report | Rows scale and carry tz/params; dispatcher adds ≤ 1 min latency |
| Time representation | Local time + IANA tz → computed UTC `next_run_at` | Fixed UTC time | UTC drifts by DST relative to business hours |
| Missed periods | Catch up every period for financial/payroll | Latest only for operational | Catch-up may burst load after outages |
| Content delivery | Link to authenticated download page | Attachment | Attachments can't be revoked, leak via forwarding, hit size limits (often 10–25 MB [VERIFY: provider limits]) |
| Recipients | Internal users resolved at send time | Static email lists | Static lists keep sending to leavers |
| Authorisation | Re-check permission and row scope per recipient at send time | Check at subscription time only | Role changes otherwise leak data |
| Shared vs per-recipient run | One run when output is identical for all recipients | Per-recipient runs when row-level security differs | Per-recipient runs multiply cost; shared runs must not exceed the least-privileged recipient's scope |
| Delivery retries | Bounded retries for transient provider errors; no retry for hard bounces | Unbounded | Hard bounces retried hurt sender reputation |

## 5. Implementation patterns

### Recipient management and send-time re-authorisation

```python
def recipient_is_authorised(recipient, run) -> bool:
    spec = get_spec(run.report_key)
    if recipient.user_id:
        u = recipient.user
        return (u.is_active and is_member(u, run.tenant) and u.has_perm(spec.permission)
                and scope_covers(u, run))                       # row-level scope ⊇ run scope
    # External email recipients: only for specs explicitly allowing it, with an approval record.
    return spec.classification == "internal" and recipient.email in approved_external_emails(run.tenant)
```

- Adding a recipient requires the same permission as viewing the report, plus an approval flow
  for external addresses.
- Deactivate recipients on user deactivation/tenant removal (signal or periodic sync), and still
  re-check at send time.
- Unsubscribe links resolve to a signed token that sets `ReportRecipient` inactive — not to a
  delete of the schedule.

### Fan-out with idempotency keys

```python
@shared_task(queue="reports-delivery", ignore_result=True)
def fan_out_deliveries(run_id: str) -> None:
    run = ReportRun.objects.select_related("schedule", "tenant").get(pk=run_id)
    if run.status != ReportRun.Status.SUCCEEDED or run.schedule_id is None:
        return
    for recipient in run.schedule.recipients.filter(is_active=True):
        key = f"{run.pk}:{recipient.pk}:email"
        delivery, created = ReportDelivery.objects.get_or_create(
            idempotency_key=key, defaults=dict(run=run, recipient=recipient, channel="email"))
        if not recipient_is_authorised(recipient, run):
            ReportDelivery.objects.filter(pk=delivery.pk, status="pending").update(
                status="failed", last_error="not_authorised")
            continue
        if created:
            transaction.on_commit(lambda d=delivery.pk: send_delivery.delay(d))
```

Re-running `fan_out_deliveries` (redelivery, manual replay) creates nothing new for recipients
already handled.

### Sending one delivery

```python
@shared_task(bind=True, queue="reports-delivery", acks_late=True, max_retries=5, ignore_result=True)
def send_delivery(self, delivery_id: int) -> None:
    claimed = ReportDelivery.objects.filter(pk=delivery_id, status__in=["pending", "failed"]) \
        .update(status="sending", attempt=F("attempt") + 1)
    if not claimed:
        return
    d = ReportDelivery.objects.select_related("run", "recipient__user").get(pk=delivery_id)
    try:
        send_report_ready_email(to=d.recipient, run=d.run,
                                link=run_page_url(d.run),       # login + permission re-check
                                idempotency_key=d.idempotency_key)
    except ProviderTransientError as exc:
        ReportDelivery.objects.filter(pk=d.pk).update(status="failed", last_error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=60 * 2 ** d.attempt)
    except ProviderPermanentError as exc:
        ReportDelivery.objects.filter(pk=d.pk).update(status="failed", last_error=type(exc).__name__)
        return
    ReportDelivery.objects.filter(pk=d.pk).update(status="sent", sent_at=timezone.now())
```

- `"sending"` is part of the canonical `ReportDelivery.Status` above; a stale `sending` row is recovered by a reaper like the one in
  [background-report-generation.md](background-report-generation.md).
- Pass the idempotency key to providers that support one; most email APIs don't dedupe, so the
  **claim is what prevents duplicate emails**, not the provider. A crash after the provider accepted
  the message but before `sent` is written can still duplicate — this is the irreducible
  at-least-once window; keep it small and document it.
- Bounces arrive asynchronously via provider webhooks → `status="bounced"`, deactivate the recipient
  after repeated hard bounces ([webhooks-and-inbound-integrations](../../django-integrations/references/webhooks-and-inbound-integrations.md)).

### Links vs attachments

| Choose links when | Choose attachments when |
|---|---|
| Classification is `confidential`/`restricted` | Recipient cannot log in (approved external), report is `internal` and small |
| File > a few MB | Contractual requirement to deliver the document itself |
| Access must be revocable and audited | Offline access is the business need |

If attachments are mandatory for sensitive data, encrypt the file (e.g. password-protected PDF
via a post-processing library with the password sent out of band) and record the decision in an
ADR — PDF permission flags are not access control ([pdf-report-generation.md](pdf-report-generation.md)).

### Delivery audit

`ReportDelivery` *is* the delivery audit: recipient, channel, attempts, status, timestamps,
last error. Never delete it when the artifact expires — the artifact row is soft-deleted
(`deleted_at`) and the delivery rows remain, proving who was sent what and when. Downloads after
delivery are audited separately by `ReportDownload`.

### Failed delivery recovery

- Operator action "resend failed deliveries for run X": reset `status` to `pending` for rows in
  `failed` (not `bounced`, not `not_authorised`) and enqueue — the claim keeps it idempotent.
- If the artifact has expired, regenerating is a **new** run for the same period. For
  regulated reports, prefer extending retention over regenerating, because regenerated output may
  differ when source data was corrected since ([reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md)).
- A schedule whose runs fail N times consecutively is auto-paused (`is_active=False`) with an
  owner notification rather than failing silently every day.

### Expiration and retention

- Artifact `expires_at = finished_at + spec.retention`; links in emails stop working after it
  (endpoint returns 410 with "request a new copy" guidance).
- Email link validity ≤ artifact retention; state the expiry date in the email body.
- Cleanup implementation: [background-report-generation.md](background-report-generation.md);
  policy and legal retention: [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 6. Production use cases

### Monthly payroll report

```text
ReportSchedule: report_key="payroll.monthly_register", format="xlsx",
  frequency="monthly", day_of_month=2, run_at_local=07:00, timezone="Europe/Berlin"
Precondition: PayrollRun for previous month has status="approved"; else run FAILED(code="not_ready")
             and dispatcher retries daily until day 5, then alerts payroll owner.
Recipients: payroll managers (users with reports.view_payroll_reports), re-checked at send.
Delivery: link only (classification="restricted"); downloads audited; no external recipients.
Missed periods: catch up every period.
```

### Daily operational report

```text
ReportSchedule: report_key="inventory.daily_stock_exceptions", format="pdf",
  frequency="daily", run_at_local=06:30, timezone=tenant.timezone
Period: previous local day; data from pre-aggregated table refreshed at 05:00.
Missed periods: latest only (yesterday's ops report is useless a week later).
Recipients: warehouse leads per site; per-site params → one schedule per site.
```

### Weekly sales report

```text
ReportSchedule: report_key="sales.weekly_revenue", format="xlsx",
  frequency="weekly", weekday=0 (Monday), run_at_local=08:00, timezone=tenant.timezone
Period: previous ISO week [Mon 00:00, Mon 00:00) local.
Row scope: regional managers see only their region → per-region schedules, or per-recipient runs.
Includes chart sheet (native Excel chart) — see excel-report-generation.md.
```

### Monthly financial statement

```text
ReportSchedule: report_key="ledger.monthly_statement", format="pdf",
  frequency="monthly", day_of_month=5, run_at_local=09:00, timezone="America/New_York"
Precondition: period closed in ledger (no postings after close); data_as_of recorded.
Reproducibility: report_version + params + period close snapshot stored; artifact retained per
financial retention policy [VERIFY: jurisdiction-specific retention with finance/legal].
Corrections: never overwrite — a restated statement is a new run labelled "restated" and
re-delivered with an explanation.
```

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| DB schedules + dispatcher | Per-tenant tz, params, recipients; auditable; scales | Must build next-run logic and tests |
| Beat entry per report | Trivial for a few platform reports | Single timezone, redeploy to change, no per-tenant params |
| Links | Revocable, audited, small emails | Recipient must log in; friction |
| Attachments | Offline, simple for recipients | Unrevocable copies, size limits, data leakage |
| Shared run per schedule | One generation for N recipients | Only valid when all recipients have identical scope |

## 8. Security considerations

- Scheduled runs have `requested_by=None`; the **schedule owner and each recipient** are the
  principals. Re-check at generation (owner) and at send (each recipient).
- A report run shared across recipients must be generated under the **intersection** of their
  scopes, or split per scope; never under the owner's broader scope.
- External recipients require explicit approval, are tenant-scoped, and are limited to
  classifications policy allows.
- Email bodies contain no report data beyond title, period and link — subject lines leak to
  mail logs and lock screens.
- Unsubscribe and link tokens are signed, salted per purpose, and expire.
- Payroll and financial distribution changes (new recipient, format change to attachment) are
  audit-logged with actor and timestamp.
- Details: [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Many tenants share "Monday 08:00" — spread load with jitter (± a few minutes) for operational
  reports, or a per-tenant offset; keep exact times only where the business needs them.
- Dispatcher batches (`[:batch]`) and `skip_locked` keep each tick short.
- Month-start bursts (payroll, finance, sales) compete for the `reports` queue — prioritise
  regulated reports on a separate queue or with reserved concurrency.
- Delivery uses its own queue so slow email providers do not block generation.
- Reuse a single run for identical schedules across recipients when scope allows.
- See [report-generation-performance.md](report-generation-performance.md).

## 10. Common mistakes and anti-patterns

- Storing schedule times in UTC; computing next run with `timedelta(days=30)` for "monthly".
- Deriving the period from `timezone.now()` inside the task (late runs cover the wrong period).
- Advancing `next_run_at` from `now` and silently skipping missed payroll periods.
- Sending the email from inside the generation task — a send failure retries the whole report.
- No idempotency key per run+recipient — every retry of the fan-out re-emails everyone.
- Static email distribution lists that outlive employment.
- Attaching restricted spreadsheets to email.
- Running two Celery beat processes for redundancy.
- Retrying hard bounces.

## 11. Troubleshooting and validation

| Symptom | Likely cause | Check |
|---|---|---|
| Report arrives an hour early/late after DST | UTC-stored schedule or naive datetime math | `timezone` field; `compute_next_run` tests at DST edges |
| No report on the 31st-scheduled monthly job in some months | No clamping | `_clamp_day` |
| Recipients got two emails | Fan-out not keyed; claim missing; two beats | `ReportDelivery` rows per key; beat process count |
| Schedule silently stopped | Exception in dispatcher loop for one schedule blocks the batch | Per-schedule try/except with logging; `next_run_at` age alert |
| Former employee still receives reports | No send-time re-authorisation | `recipient_is_authorised` tests |
| Missing period after outage | `next_run_at` advanced from `now` | Catch-up policy per report |

Validation checklist:

- [ ] `compute_next_run` tested for DST gap, fold, Feb 28/29, day 31, year boundary, and several tz.
- [ ] Unique constraints for run-per-period and delivery idempotency key exist.
- [ ] Dispatcher uses `select_for_update(skip_locked=True)` and is the only beat-scheduled report job.
- [ ] Recipients re-authorised at send; external recipients approved.
- [ ] Links, not attachments, for confidential/restricted reports.
- [ ] Delivery retries bounded; bounces handled; failed-delivery replay is idempotent.
- [ ] Alert when any active schedule's `next_run_at` is older than its frequency allows
      ([reporting-observability-and-operations.md](reporting-observability-and-operations.md)).

## 12. Related references and official documentation

- [background-report-generation.md](background-report-generation.md) — the generation job and reaper.
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) — signed links, 410 handling.
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — period boundaries.
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — recipient authorisation, retention.
- [production-reporting-architectures.md](production-reporting-architectures.md) — payroll and financial blueprints.
- [report-generation-testing.md](report-generation-testing.md) — schedule and delivery tests.
- [django-integrations — email-sms-and-notification-integrations](../../django-integrations/references/email-sms-and-notification-integrations.md)
- [django-integrations — celery-and-background-integrations](../../django-integrations/references/celery-and-background-integrations.md)
- [django-data-processing — django-background-processing](../../django-data-processing/references/django-background-processing.md)
- [django-orm — transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)
- <https://docs.python.org/3/library/zoneinfo.html>
- <https://peps.python.org/pep-0495/>
- <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update>
- <https://docs.djangoproject.com/en/5.2/topics/email/>
- <https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html>
- <https://django-celery-beat.readthedocs.io/>
