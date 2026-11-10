# Reporting Architecture and Design

> **Scope:** The reporting subsystem's structure: components and dependency direction, the
> `ReportSpec` registry, the **canonical** `ReportRun` / `ReportArtifact` models, parameter validation
> and hashing, filter/sort allow-lists, the selector contract, the business status state machine,
> execution-mode comparison, multi-tenant design and the separation of generation from delivery.
> **Not here:** query techniques → [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md);
> task mechanics → [background-report-generation.md](background-report-generation.md); schedule and
> delivery models in depth → [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md);
> renderer abstraction → [report-templates-and-reusability.md](report-templates-and-reusability.md);
> endpoints → [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).

## 1. Purpose and scope

Every other reference in this skill assumes the design described here. It answers:

- Which components exist, which Django app owns them, and which way dependencies point.
- How a report is *defined* (code), *requested* (validated parameters), *executed* (a run with a
  business status) and *materialised* (an artifact with metadata).
- How the same design serves synchronous, asynchronous, scheduled and pre-generated reports.
- How tenant isolation, deduplication and versioning are built into the data model rather than
  bolted on.

Treat the models below as the canonical definitions; other references link here instead of
redefining them.

## 2. Core concepts

| Concept | Meaning | Lives in |
|---|---|---|
| `ReportSpec` | Immutable, code-defined description of a report: key, version, params form, formats, permission, selector, classification, retention | `reports/registry.py` + each domain app's `reports.py` |
| Parameters | Validated, normalised user inputs; stored as canonical JSON | `ReportRun.params` |
| `params_hash` | SHA-256 of canonical (key, version, format, scope, params) — identity of "the same report" | `ReportRun.params_hash` |
| Selector | Domain-owned, scoped read function returning a dataset | `billing/selectors.py`, `sales/selectors.py` … |
| `ReportRun` | One execution request and its **business** status | `reports/models.py` |
| `ReportArtifact` | The produced file: storage key, filename, type, size, checksum, expiry | `reports/models.py` |
| Renderer | Dataset → bytes for one format | `reports/renderers/` |
| Delivery | Getting an artifact to a person/system; separate from generation | `ReportDelivery`, `ReportDownload` |

Key ideas:

- **Definitions are code.** Reviewable, testable, versioned with the application. User-composed
  reports are restricted to choosing *parameters* of a fixed definition.
- **Domain apps own their data; the `reports` app owns the machinery.** Selectors live in the
  domain app; the platform never queries domain tables directly.
- **A run is a business record**, not a Celery artifact. Celery task state is transport detail
  ([background-report-generation.md](background-report-generation.md)).
- **Generation and delivery are separate steps** with separate statuses and retries.

## 3. Internal mechanics

### 3.1 Component and dependency diagram

```text
┌──────────────────────────── Interface layer ────────────────────────────┐
│  HTML views · DRF endpoints · admin actions · management commands       │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ calls services only
┌──────────────────────────────▼──── reports (platform app) ──────────────┐
│ registry: ReportSpec              services: request_report, cancel_run,  │
│ models: ReportRun, ReportArtifact,          issue_download_url           │
│   ReportSchedule, ReportRecipient,  tasks: generate_report, dispatch_due,│
│   ReportDelivery, ReportDownload           expire_artifacts, reap_stale  │
│ renderers: csv · xlsx · pdf · docx  storage: private report_storage      │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ ReportSpec.selector(tenant=, user=, params=)
┌──────────────────────────────▼──── domain apps (own their data) ────────┐
│ billing.selectors · sales.selectors · payroll.selectors · inventory ... │
│ each app's reports.py registers its ReportSpecs with reports.registry   │
└─────────────────────────────────────────────────────────────────────────┘
```

Dependency direction: domain apps import `reports.registry` to register specs (feature → platform);
`reports` never imports domain models. This keeps the platform reusable and avoids cycles
([django-architecture — django-application-boundaries](../../django-architecture/references/django-application-boundaries.md)).

### 3.2 Request-to-delivery flow

```text
client ──POST /reports/runs──► view/DRF ──► request_report()
                                              │ authorise spec permission
                                              │ validate params (form) → canonical JSON → params_hash
                                              │ INSERT ReportRun(PENDING)  (dedupe via partial unique)
                                              │ on_commit → enqueue generate_report(run_id)
                                              ▼
worker: generate_report ──► claim (PENDING→RUNNING) ──► re-validate + re-authorise
        ──► spec.selector(...) ──► renderer ──► report_storage.save()
        ──► ReportArtifact + ReportRun(SUCCEEDED, row_count, data_as_of)
        ──► on_commit → notify / deliveries
client ──GET /reports/runs/<id>/download──► visible_runs() scope ──► signed URL or FileResponse
        ──► ReportDownload audit row
```

### 3.3 Business status state machine

```text
                request_report()
                       │
                       ▼
                 ┌──────────┐   cancel (not started)   ┌───────────┐
                 │ PENDING  │─────────────────────────►│ CANCELLED │
                 └────┬─────┘                          └───────────┘
       claim (atomic  │      ▲                                ▲
        conditional   │      │ reaper: stale heartbeat,       │ cancel_requested
        UPDATE)       ▼      │ attempts remaining             │ observed between chunks
                 ┌──────────┐┘                               │
                 │ RUNNING  │─────────────────────────────────┘
                 └────┬─────┘──── permanent error / attempts exhausted ──► FAILED
                      │ artifact stored, metadata committed
                      ▼
                 ┌───────────┐   expires_at passed (cleanup task)   ┌─────────┐
                 │ SUCCEEDED │─────────────────────────────────────►│ EXPIRED │
                 └───────────┘                                      └─────────┘
```

| From | Allowed to | Who |
|---|---|---|
| PENDING | RUNNING, CANCELLED, FAILED | worker claim; user cancel; validation failure at execution |
| RUNNING | SUCCEEDED, FAILED, CANCELLED, PENDING | worker; reaper requeue after crash |
| SUCCEEDED | EXPIRED | cleanup task |
| FAILED, CANCELLED, EXPIRED | — (terminal) | a retry is a **new** run |

Every transition is a conditional `UPDATE … WHERE status IN (…)`, so two actors can never both win
([django-orm — transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)).

## 4. Engineering decisions and trade-offs

### 4.1 Execution modes

| Mode | Trigger | User waits? | Practical size | Retry | Audit trail | Infra | Best for |
|---|---|---|---|---|---|---|---|
| Synchronous | Request | Yes, whole render | Small (seconds, few k rows) | User re-clicks | Optional run row | None | Single invoice PDF, small CSV |
| Synchronous streaming | Request | Yes, while downloading | Large CSV / JSONL only | None mid-stream | Log line or run row | Proxy/timeouts tuned | Big flat exports |
| Asynchronous on-demand | Request → task | No; notified | Large, any format | Task retries | `ReportRun` | Broker, workers, storage | XLSX/PDF reports, heavy queries |
| Scheduled | Dispatcher | No | Any | Task + delivery retries | `ReportRun` + `ReportDelivery` | + scheduler | Daily/weekly/monthly distribution |
| Pre-generated | Business event or batch | No; served instantly | Many small artifacts | Regenerate | `ReportArtifact` per entity | + storage lifecycle | Statements, invoices, payslips |
| Precomputed aggregates | Refresh job | No; fast reads | Dashboards | Refresh retry | Refresh timestamp | + refresh job | KPIs over large fact tables |

Rule of thumb: the moment a report could exceed the request timeout for the **largest** tenant,
it is asynchronous for **every** tenant. Two code paths for the same report diverge.

### 4.2 Other design decisions

| Decision | Prefer | Alternative | Trade-off |
|---|---|---|---|
| Definition storage | Code registry | DB rows with constrained filter config | DB definitions enable self-service but need their own validation, permissions, versioning |
| Parameter validation | Django `forms.Form` [Django] per spec | DRF serializer [DRF] | Forms work in views, tasks and commands without DRF; serializers fit if the API is the only entry point |
| Artifact file field | Explicit `storage_name` + named storage | `FileField` | `FileField` is convenient but couples to `upload_to` and default storage; explicit keys make tenant-prefixed paths and lifecycle rules obvious |
| Dedupe scope | Tenant + key + params_hash + format, active runs only | Global cache key | DB constraint survives races; cache can be evicted |
| Retry of a failed run | New `ReportRun` linked by params_hash | Mutate the failed run | History stays truthful; audit shows both |
| Run primary key | UUID | Integer | UUIDs are not enumerable in URLs (authorisation is still required) |
| Versioning | `version` on spec, recorded on every run | Implicit (git) | Explicit version explains old artifacts without archaeology |

### 4.3 Report configuration vs parameters

Presentation configuration (logo, legal address, colour theme) may be tenant-level settings. Anything
that **changes the numbers** — fiscal year start, revenue recognition rule, base currency, inclusion
of refunds — must be a **parameter** (stored in `params`, part of `params_hash`) or a **version**
change. Otherwise changing a tenant setting silently changes historical reports.

## 5. Implementation patterns

### 5.1 The registry

```python
# reports/registry.py
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class ReportSpec:
    key: str                 # "sales.weekly_revenue"
    version: int             # bump when output semantics change
    title: str
    params_form: type        # django.forms.Form subclass validating parameters
    formats: tuple[str, ...] # ("csv", "xlsx", "pdf")
    permission: str          # "reports.view_sales_reports"
    selector: Callable       # (*, tenant, user, params) -> ReportDataset
    classification: str      # "internal" | "confidential" | "restricted"
    retention: timedelta
    max_sync_rows: int = 5_000
    user_scoped: bool = False  # True when the selector applies per-user row restrictions


_REGISTRY: dict[str, ReportSpec] = {}


def register(spec: ReportSpec) -> ReportSpec:
    if spec.key in _REGISTRY:
        raise ImproperlyConfigured(f"Duplicate report key: {spec.key}")
    _REGISTRY[spec.key] = spec
    return spec


def get_spec(key: str) -> ReportSpec:
    return _REGISTRY[key]   # KeyError → caller maps to 404 / validation error
```

Domain apps import their `reports` module in `AppConfig.ready()` so registration happens once at
startup ([django-core — models-application-lifecycle](../../django-core/references/models-application-lifecycle.md)).
If regulatory reports must be reproducible with an *old* version, key the registry by
`(key, version)` and keep old selectors callable.

### 5.2 Canonical models

```python
# reports/models.py
import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class ReportRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    ACTIVE = (Status.PENDING, Status.RUNNING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="report_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")  # null = scheduled/system
    schedule = models.ForeignKey("reports.ReportSchedule", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="runs")
    report_key = models.CharField(max_length=100)
    report_version = models.PositiveIntegerField()
    format = models.CharField(max_length=10)
    params = models.JSONField(encoder=DjangoJSONEncoder)
    params_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    attempt = models.PositiveSmallIntegerField(default=0)
    celery_task_id = models.CharField(max_length=155, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    requested_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    data_as_of = models.DateTimeField(null=True, blank=True)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=50, blank=True)
    error_message = models.CharField(max_length=500, blank=True)   # safe to show users
    cancel_requested = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "report_key", "params_hash", "format"],
                condition=Q(status__in=["pending", "running"]),
                name="uniq_active_report_run",
            ),
            models.UniqueConstraint(                   # one scheduled run per schedule period
                fields=["schedule", "params_hash", "format"],
                condition=Q(schedule__isnull=False),
                name="uniq_scheduled_run_per_period",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "requested_by", "-requested_at"], name="reportrun_user_recent"),
            models.Index(fields=["status", "heartbeat_at"], name="reportrun_status_heartbeat"),
        ]


class ReportArtifact(models.Model):
    run = models.OneToOneField(ReportRun, on_delete=models.PROTECT, related_name="artifact")
    storage_name = models.CharField(max_length=500)   # internal key; never exposed or user-derived
    filename = models.CharField(max_length=255)       # sanitised download name
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, blank=True)
```

Notes:

- `choices=Status` (passing the enum class) and `db_default` require Django ≥ 5.0
  [VERIFY: Django 5.0 release notes for `choices` accepting `TextChoices` directly and `db_default`].
- The partial unique constraint is enforced by PostgreSQL [PostgreSQL] and SQLite; Django does not
  create conditional unique constraints on MySQL/MariaDB
  [VERIFY: Django 5.2 `UniqueConstraint.condition` backend support notes].
- `ReportArtifact.run` uses `PROTECT` and artifacts are soft-deleted via `deleted_at`, so expiring
  a file never erases the audit trail that references it.
- `ReportSchedule`, `ReportRecipient` and `ReportDelivery` are defined in
  [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md); `ReportDownload`
  in [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).

### 5.3 Parameter form, canonicalisation and hashing

```python
# sales/report_params.py
from datetime import datetime
from zoneinfo import ZoneInfo

from django import forms

SORTS = {                                   # public key → ORM ordering (allow-list)
    "revenue_desc": ("-revenue", "customer__region"),
    "region": ("customer__region",),
}


class WeeklyRevenueParams(forms.Form):
    week_start = forms.DateField()
    region = forms.ChoiceField(required=False)
    sort = forms.ChoiceField(choices=[(k, k) for k in SORTS], required=False)
    include_refunds = forms.BooleanField(required=False)

    def __init__(self, *args, tenant, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        regions = visible_regions(tenant=tenant, user=user)          # scoped selector
        self.fields["region"].choices = [("", "All")] + [(r, r) for r in regions]

    def clean_week_start(self):
        value = self.cleaned_data["week_start"]
        today = datetime.now(ZoneInfo(self.tenant.timezone)).date()
        if value.weekday() != 0:
            raise forms.ValidationError("Week must start on a Monday.", code="not_monday")
        if value > today:
            raise forms.ValidationError("Week cannot be in the future.", code="future")
        return value

    def clean_sort(self):
        return self.cleaned_data["sort"] or "revenue_desc"            # explicit default → stable hash
```

```python
# reports/params.py
import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder


def canonical_params(cleaned: dict) -> dict:
    """Typed cleaned_data → JSON-safe dict (dates as ISO strings, Decimals as strings)."""
    return json.loads(json.dumps(cleaned, cls=DjangoJSONEncoder, sort_keys=True))


def params_hash(*, key: str, version: int, fmt: str, scope: str, params: dict) -> str:
    payload = json.dumps(
        {"k": key, "v": version, "f": fmt, "s": scope, "p": params},
        sort_keys=True, separators=(",", ":"), cls=DjangoJSONEncoder,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`scope` matters: if the selector applies **row-level** restrictions per user (a regional manager sees
only their regions), two users with the same parameters get different outputs. Use
`scope=f"user:{user.pk}"` for user-scoped specs and `scope="tenant"` for tenant-wide specs, or
deduplication will hand one user another user's report.

At execution time the task re-binds `run.params` to the same form (`data=run.params`) to recover typed
values **and re-check visibility** — permissions may have changed since the request. A validation
failure at that point ends the run as `FAILED` with `error_code="params_no_longer_valid"`
[VERIFY: confirm this policy with product owners; the alternative is to run with original
authorisation, which is rarely acceptable].

### 5.4 Requesting a run (deduplicated)

```python
# reports/services.py
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from .models import ReportRun
from .params import canonical_params, params_hash
from .registry import get_spec


def request_report(*, user, tenant, key, params, fmt) -> ReportRun:
    from .tasks import generate_report               # local import avoids a services↔tasks cycle

    spec = get_spec(key)
    if fmt not in spec.formats:
        raise ValidationError({"format": "Unsupported format."})
    if not user.has_perm(spec.permission):           # or a tenant-aware role check
        raise PermissionDenied
    form = spec.params_form(data=params, tenant=tenant, user=user)
    if not form.is_valid():
        raise ValidationError(form.errors)
    clean = canonical_params(form.cleaned_data)
    scope = f"user:{user.pk}" if spec.user_scoped else "tenant"
    digest = params_hash(key=key, version=spec.version, fmt=fmt, scope=scope, params=clean)
    try:
        with transaction.atomic():
            run = ReportRun.objects.create(
                tenant=tenant, requested_by=user, report_key=key, report_version=spec.version,
                format=fmt, params=clean, params_hash=digest,
            )
    except IntegrityError:
        # An identical run is already pending/running: return it instead of starting another.
        return ReportRun.objects.get(tenant=tenant, report_key=key, params_hash=digest,
                                     format=fmt, status__in=ReportRun.ACTIVE)
    transaction.on_commit(lambda: generate_report.delay(str(run.pk)))
    return run
```

The `get()` after `IntegrityError` can race with the other run finishing; on `DoesNotExist`, retry the
create once. Returning a *succeeded* recent run instead of generating again (artifact reuse) is a
separate, explicit policy — see [report-generation-performance.md](report-generation-performance.md).

### 5.5 Filter and sort allow-lists

- Public parameter values map to **server-defined** ORM lookups/orderings (`SORTS` above). Never pass
  user strings to `order_by()`, `filter(**kwargs)` keys or `values()` field lists.
- Every ordering ends with a unique tiebreaker (`pk`, or the group key for aggregates) so paginated
  and chunked output is deterministic.
- Bound ranges (max period length, max ids in an `in` filter) in the form, not in the selector.
- Free-text filters use fixed lookups (`name__icontains`) with length limits.

### 5.6 Selector contract

```python
# reports/datasets.py
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str            # "text" | "int" | "decimal" | "money" | "date" | "datetime"


@dataclass
class ReportDataset:
    columns: list[Column]
    rows: Iterable[tuple]                  # lazy; values in column order
    data_as_of: datetime
    totals: dict[str, Decimal] = field(default_factory=dict)
```

A selector registered on a `ReportSpec` must:

1. Take keyword-only `tenant`, `user`, `params` (typed cleaned data) and nothing request-shaped.
2. Start from the domain's **scoped** queryset (`visible_invoices(tenant=, user=)`).
3. Push filtering, grouping and arithmetic to the database; return rows lazily
   (`values_list(...).iterator(chunk_size=...)` for detail data).
4. Return deterministic ordering and typed values (`Decimal`, aware `datetime`, `date`) — no
   formatting, no rounding for display, no file I/O.
5. Report `data_as_of` (snapshot or refresh time) so renderers can print it.
6. Have no side effects; it must be safe to call twice.

Renderers and the format-agnostic document model built on datasets are owned by
[report-templates-and-reusability.md](report-templates-and-reusability.md).

### 5.7 Snapshots, read models and pre-computed aggregates (architectural placement)

Treat each as a **data product** with an owner, a refresh task and a recorded refresh timestamp.
Selectors read them like any table and report their refresh time as `data_as_of`. Design and SQL
details: [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md).

### 5.8 Separating generation from delivery

```text
generate once ──► ReportArtifact ──┬──► download (ReportDownload audit)
                                   ├──► email link (ReportDelivery: email)
                                   └──► SFTP push  (ReportDelivery: sftp)
```

- A delivery failure retries **the delivery**, never the generation.
- Regenerating a report does not automatically re-send it; resend is an explicit action creating new
  `ReportDelivery` rows with new idempotency keys.
- The artifact's `expires_at` bounds every delivery's link lifetime.

## 6. Production use cases

- **Weekly revenue** (`sales.weekly_revenue` v3): user-requested XLSX; deduped so a manager clicking
  five times gets one run; the scheduled Monday run uses the same spec with `requested_by=None`.
- **Invoice PDF**: pre-generated at issue time into `ReportArtifact` (one run per invoice); downloads
  serve the stored file, so the customer always sees exactly what was issued.
- **Payroll register**: restricted classification; selector reads approved `PayrollRun` only;
  `scope="tenant"`; retention set by policy; every download audited.
- **Multi-tenant SaaS**: thousands of tenants requesting the same definition; per-tenant
  concurrency limits enforced when claiming runs
  ([background-report-generation.md](background-report-generation.md)).

## 7. Advantages and disadvantages

| Design element | Advantage | Disadvantage |
|---|---|---|
| Code registry | Type-checked, reviewable, testable | Deploy needed for a new report |
| Run model with business status | Audit, history, UI progress independent of Celery | One more table and lifecycle to maintain |
| Partial unique dedupe | Race-proof, no locks | PostgreSQL/SQLite-specific |
| Canonical params hash | Stable identity for dedupe, reuse, caching | Must include version, format and scope or it lies |
| Separate artifact model | Explicit lifecycle, checksum, expiry | Join for listings |
| Generation/delivery split | Independent retries; no duplicate generation | More statuses to present to users |

## 8. Security considerations

- Authorise at **request** (spec permission + parameter visibility) and again at **execution** and
  **download**; any of the three can be the bypass.
- Every report model carries `tenant`; every lookup goes through a tenant-scoped selector
  (`visible_runs(user=, tenant=)`). A run id from another tenant must produce 404, not 403
  ([django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md)).
- `storage_name` is server-generated (`reports/<tenant_id>/<yyyy>/<mm>/<run_uuid>.<ext>`) — never
  derived from user input or the display filename.
- `params` may contain personal data (an employee filter); treat run rows with the report's
  classification and exclude raw params from logs.
- `error_message` is user-visible: never store exception text or SQL there; put details in logs with
  the correlation id.

Full threat model: [reporting-security-and-compliance.md](reporting-security-and-compliance.md).

## 9. Performance considerations

- Indexes support the three hot lookups: a user's recent runs, active-run dedupe (the partial unique
  index itself), and the reaper's `status + heartbeat_at` scan.
- `ReportRun.params` is small JSON; never store datasets or rendered bytes in the database.
- Dedupe and artifact reuse are the cheapest performance wins in a reporting platform — they remove
  whole executions.
- Per-tenant concurrency caps prevent one large tenant from monopolising report workers.

## 10. Common mistakes and anti-patterns

- Using Celery's result backend or `AsyncResult.state` as the report status shown to users.
- A `ReportRun` without `tenant`, relying on `requested_by` to imply tenancy.
- Hashing raw request parameters (unordered, untyped, defaults missing) — identical reports get
  different hashes and different ones collide when defaults change.
- Omitting `version` or `scope` from the hash.
- Selectors that format values (`"1,204.50"`) — renderers can no longer type cells correctly.
- Reports that change meaning because a tenant setting changed.
- Retrying a failed run by flipping it back to `PENDING`, losing the failure history.
- Generating a report inside the delivery step ("email job renders the PDF").
- One code path for small tenants (sync) and another for large ones (async).

## 11. Troubleshooting and validation

| Symptom | Likely design cause | Check |
|---|---|---|
| Two identical runs executing concurrently | Constraint missing, wrong fields or not on this backend | `\d reports_reportrun` shows the partial unique index |
| User saw another user's figures | Dedupe hash lacks user scope for a row-scoped spec | Spec `user_scoped` flag and hash inputs |
| Old report cannot be explained | No `report_version` / params recorded, or version not bumped | Run row fields; changelog of the spec |
| Runs stuck in RUNNING forever | No heartbeat/reaper | See [background-report-generation.md](background-report-generation.md) |
| Report changed after a settings change | Semantics in tenant config, not params/version | §4.3 |

Validation checklist:

- [ ] Each report is a registered `ReportSpec` with version, permission, classification, retention.
- [ ] Parameters validated by a form with bounds and scoped choices; canonicalised before hashing.
- [ ] Hash includes key, version, format, scope and params.
- [ ] `uniq_active_report_run` exists in the database (verify via migration SQL).
- [ ] Status transitions use conditional updates; terminal states are never revived.
- [ ] Selectors are scoped, side-effect free, deterministic and return typed values + `data_as_of`.
- [ ] Generation and delivery have separate records and retries.
- [ ] Tests prove cross-tenant and cross-user access to runs/artifacts returns 404
      ([report-generation-testing.md](report-generation-testing.md)).

## 12. Related references and official documentation

- [reporting-engineering-fundamentals.md](reporting-engineering-fundamentals.md) — taxonomy and architecture selection.
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — selector query design.
- [background-report-generation.md](background-report-generation.md) — claim, retries, reaper, cleanup.
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) — schedules, recipients, deliveries.
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) — request/download endpoints.
- [report-templates-and-reusability.md](report-templates-and-reusability.md) — renderers and document model.
- [reporting-security-and-compliance.md](reporting-security-and-compliance.md) — authorisation and isolation.
- [django-architecture — service-layer](../../django-architecture/references/service-layer.md)
- [django-architecture — django-application-boundaries](../../django-architecture/references/django-application-boundaries.md)
- [django-orm — database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md)
- [django-orm — transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)
- [django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md)
- [django-data-processing — django-background-processing](../../django-data-processing/references/django-background-processing.md)
- <https://docs.djangoproject.com/en/5.2/ref/models/constraints/>
- <https://docs.djangoproject.com/en/5.2/ref/forms/api/>
- <https://docs.djangoproject.com/en/5.2/topics/db/transactions/>
- <https://docs.djangoproject.com/en/5.2/ref/models/fields/#jsonfield>
