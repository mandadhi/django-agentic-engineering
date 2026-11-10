# Data Synchronization and Reconciliation

**Scope:** Keeping data consistent between Django and external systems — source-of-truth identification, master
data concepts, mapping and transformation, full vs incremental sync, change data capture concepts, checkpoints,
conflict resolution, duplicate detection, external identifiers, failed-record recovery and generic
reconciliation. Money-specific reconciliation and settlement files are owned by
[payment-and-financial-integrations.md](payment-and-financial-integrations.md); bulk import/ETL pipeline
mechanics by [django-data-processing](../../django-data-processing/SKILL.md).

## 1. Purpose and scope

Use this reference when an external system and Django both hold a representation of the same business entity
(customers, products, employees, accounts, inventory) and the two must converge. It covers pull-based sync
(polling APIs, delta endpoints, files), push-based sync (webhooks, events) as inputs to the same apply logic,
and the reconciliation process that detects and repairs drift that every sync eventually accumulates.

Baseline: Python 3.12+, Django 5.2 LTS, DRF 3.16, PostgreSQL, Redis, Celery 5.x. Upsert examples use
`bulk_create(update_conflicts=True, …)` (Django ≥ 4.1; PostgreSQL, SQLite 3.24+, MariaDB/MySQL with caveats).

## 2. Core concepts

### 2.1 Source of truth

For **each attribute** (not just each entity) decide the system of record (SoR):

| Entity | Attribute | SoR | Django's role |
|---|---|---|---|
| Customer | legal name, tax id | ERP | read-only copy |
| Customer | marketing consent | Django | SoR; pushes to CRM |
| Product | price | PIM | copy, refreshed hourly |
| Product | stock | WMS | copy, event-driven |

Attribute-level ownership turns most "conflicts" into non-events: an incoming update simply does not touch
fields Django owns. Write the matrix into the integration decision record
([integration-engineering-fundamentals.md](integration-engineering-fundamentals.md)).

### 2.2 Master data management (MDM) concepts

- **Golden record:** the consolidated, de-duplicated version of an entity across systems.
- **Registry style:** MDM holds only cross-references between system ids; data stays in sources.
- **Consolidation style:** MDM builds golden records for reporting; sources unchanged.
- **Coexistence / centralised style:** MDM is authoring point and publishes to consumers.

A Django app is rarely the MDM hub; it is usually a consumer (copy) or the SoR for a narrow domain. Do not
build an MDM system accidentally inside a sync job.

### 2.3 Sync modes

| Mode | How | Pros | Cons |
|---|---|---|---|
| **Full sync** | Fetch everything, compare, apply | Simple, self-healing, detects deletions | Expensive, slow, provider rate limits |
| **Incremental (delta) by timestamp** | `updated_since=checkpoint` | Cheap | Clock skew, same-timestamp boundary misses, no deletes unless tombstones |
| **Incremental by cursor / sequence** | Provider change token, sequence number | Exact, resumable | Cursor expiry; provider-specific |
| **Event-driven (push)** | Webhooks/messages per change | Low latency | Lost/out-of-order events, needs backfill |
| **CDC** | Read the source DB's change log (WAL/binlog) | Complete, ordered per table | Needs DB access and platform tooling (e.g. Debezium [third-party: debezium]) |

Production systems combine them: **events for latency + incremental for completeness + periodic full
reconciliation for correctness**.

### 2.4 Mapping and transformation

- **Schema mapping:** field-to-field correspondence, including splits/merges (`full_name` → `first`, `last`).
- **Value mapping:** code tables (`"A"` → `ACTIVE`), units, currencies, time zones (store UTC, keep source TZ
  if business-relevant), enums with an explicit unknown policy.
- **Structural mapping:** nested ↔ flat, one-to-many (order header + lines).
- **Identity mapping:** external id ↔ internal pk via `ExternalReference` or a typed unique column.

### 2.5 Consistency vocabulary

- **Convergence:** with no new changes, both sides end up equal.
- **Staleness budget:** maximum acceptable age of a copy (drives sync frequency).
- **Drift:** divergence not explained by in-flight changes — found by reconciliation.
- **Tombstone:** an explicit deletion marker; without tombstones, incremental sync cannot see deletes.

## 3. Internal mechanics

### 3.1 Timestamp-based delta pitfalls

```text
checkpoint = 10:00:00.000
provider row A updated 09:59:59.990 but its transaction commits at 10:00:00.200
our query at 10:00:00.100: updated_since=10:00:00.000 → A not visible yet
next run: updated_since=10:00:00.100 (max seen) → A has updated_at 09:59:59.990 < checkpoint → missed forever
```

Mitigations: overlap window (query `updated_since = checkpoint - overlap`, e.g. 5–15 minutes) plus idempotent
apply; use provider sequence/cursor tokens when available; never set the checkpoint to "now" — set it to the
max `updated_at` actually observed (minus overlap on the next query); use `(updated_at, id)` tie-breaking for
pagination over equal timestamps.

### 3.2 Idempotent apply

Every record apply must be an **upsert keyed by external id** with a **version guard**:

```text
apply(record):
  if local.source_version >= record.version: skip (stale or duplicate)
  else: update owned fields, set source_version = record.version
```

`version` may be a provider version number, ETag, or `updated_at`. Version guards make out-of-order delivery
and overlap windows harmless.

### 3.3 CDC concepts

CDC tails the database transaction log of the source (PostgreSQL logical decoding, MySQL binlog, SQL Server
CDC) and emits row-level change events in commit order per table. It captures deletes and intermediate
states, but exposes the source's **physical schema** — always put an ACL between CDC events and your domain.
For Django as a **source**, prefer the outbox ([integration-architecture-patterns.md](integration-architecture-patterns.md))
over exposing tables to CDC consumers; for PostgreSQL logical replication details see
[postgresql-engineering](../../postgresql-engineering/SKILL.md).

## 4. Engineering decisions and trade-offs

### 4.1 Choosing a sync strategy

| Situation | Strategy |
|---|---|
| < ~50k records, provider allows it, minutes of staleness OK | Scheduled full sync with diff |
| Large dataset, provider has `updated_since` or delta tokens | Incremental with checkpoint + weekly full reconciliation |
| Low-latency need and provider sends webhooks | Webhook-triggered refetch (fetch current state by id) + incremental safety net |
| Source is a database you are allowed to read | CDC or replicated read model — with ACL |
| Bidirectional edits on the same attributes | Avoid; if unavoidable, explicit conflict policy and human review |

**Webhook-triggered refetch** (treat the event as "id X changed", then GET the current resource) avoids most
ordering problems at the cost of one API call per event.

### 4.2 Conflict resolution policies

| Policy | Meaning | Use when | Risk |
|---|---|---|---|
| Attribute ownership | Each field has one writer | Default | Requires discipline in UI/admin (read-only fields) |
| Source wins | External always overwrites | Django is pure copy | Local edits silently lost — disallow them |
| Last-writer-wins (LWW) | Highest timestamp wins | Low-value data, synced clocks | Clock skew loses updates |
| Version vector / compare-and-set | Write only if base version matches | Bidirectional APIs with ETags | Rejections must be handled |
| Merge rules | Field-level merge (union of tags) | Sets/collections | Complex, domain-specific |
| Manual review | Park conflicting records | Regulated or high-value data | Operational load |

### 4.3 Duplicate detection

- **Deterministic:** same external id, or same normalised natural key (lower-cased email, E.164 phone, tax id
  without separators). Enforce with unique constraints — on functional indexes where needed
  (`UniqueConstraint(Lower("email"), name=...)`).
- **Probabilistic:** fuzzy name/address matching (trigram similarity — `pg_trgm`) produces *candidates*,
  never automatic merges for important entities; route to review.
- **Merges** must re-point external references and keep a `merged_into` pointer so late events for the
  losing id still resolve.

### 4.4 Pull vs push cost

Polling costs API quota proportional to frequency × pages, even when nothing changed. Push costs endpoint
operation and backfill logic. Budget both against provider rate limits
([integration-reliability-and-resilience.md](integration-reliability-and-resilience.md)).

## 5. Django implementation patterns

### 5.1 Checkpoint model

```python
# apps/integrations/models.py
class SyncCheckpoint(models.Model):
    integration = models.CharField(max_length=64, unique=True)     # "partner_crm.contacts"
    cursor = models.CharField(max_length=512, blank=True)          # opaque token or ISO timestamp
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_run_stats = models.JSONField(default=dict, blank=True)   # fetched/created/updated/skipped/failed
    updated_at = models.DateTimeField(auto_now=True)
```

### 5.2 Synced model with version guard

```python
# apps/crm/models.py
class Contact(models.Model):
    crm_id = models.CharField(max_length=64, unique=True)          # external identifier
    email = models.EmailField()
    full_name = models.CharField(max_length=200)
    marketing_consent = models.BooleanField(default=False)         # owned by Django — never overwritten by sync
    crm_version = models.BigIntegerField(default=0)                # source version for guards
    crm_deleted_at = models.DateTimeField(null=True, blank=True)   # tombstone (soft delete)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="contact_email_ci_uniq",
                                    condition=Q(crm_deleted_at__isnull=True)),
        ]
```

### 5.3 Incremental sync job

```python
# apps/crm/sync.py
import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.errors import ContractViolationError
from apps.integrations.models import SyncCheckpoint

from .integrations.partner_crm.gateway import PartnerCrmGateway
from .models import Contact, SyncFailure

logger = logging.getLogger(__name__)
OVERLAP = timedelta(minutes=10)
INTEGRATION = "partner_crm.contacts"


def sync_contacts(*, gateway: PartnerCrmGateway | None = None, page_size: int = 200) -> dict[str, int]:
    gateway = gateway or PartnerCrmGateway()
    checkpoint, _ = SyncCheckpoint.objects.get_or_create(integration=INTEGRATION)
    since = (datetime.fromisoformat(checkpoint.cursor) - OVERLAP) if checkpoint.cursor else None
    stats = {"fetched": 0, "applied": 0, "skipped": 0, "failed": 0}
    max_seen = since

    for page in gateway.iter_changed_contacts(updated_since=since, page_size=page_size):  # (updated_at, id) order
        for dto in page:
            stats["fetched"] += 1
            try:
                applied = apply_contact(dto)
            except ContractViolationError as exc:            # bad record: park it, keep going
                SyncFailure.record(INTEGRATION, external_id=dto.crm_id, error=exc, payload=dto.raw)
                stats["failed"] += 1
                continue
            stats["applied" if applied else "skipped"] += 1
            max_seen = max(filter(None, [max_seen, dto.updated_at]))
        # Persist progress per page so a crash resumes near where it stopped.
        if max_seen:
            SyncCheckpoint.objects.filter(pk=checkpoint.pk).update(cursor=max_seen.isoformat())

    SyncCheckpoint.objects.filter(pk=checkpoint.pk).update(
        last_success_at=timezone.now(), last_run_stats=stats)
    logger.info("sync.completed", extra={"integration": INTEGRATION, **stats})
    return stats


def apply_contact(dto) -> bool:
    """Idempotent, version-guarded upsert of CRM-owned fields only. Returns True if changed."""
    with transaction.atomic():
        updated = (Contact.objects
                   .filter(crm_id=dto.crm_id)
                   .filter(Q(crm_version__lt=dto.version))
                   .update(email=dto.email.lower(), full_name=dto.full_name,
                           crm_version=dto.version, synced_at=timezone.now(),
                           crm_deleted_at=dto.deleted_at))
        if updated:
            return True
        _, created = Contact.objects.get_or_create(
            crm_id=dto.crm_id,
            defaults={"email": dto.email.lower(), "full_name": dto.full_name,
                      "crm_version": dto.version, "synced_at": timezone.now(),
                      "crm_deleted_at": dto.deleted_at},
        )
        return created            # existing row with newer/equal version → stale event, skipped
```

Notes: the job runs from a periodic Celery beat task **or** a cron-driven management command
([signals-management-commands.md](../../django-core/references/signals-management-commands.md)) — guard it
with a lock (`cache.add(lock_key, …, timeout)` or a PostgreSQL advisory lock) so two runs never overlap. No
HTTP call occurs inside `atomic()`: the gateway fetches, then each record applies in its own short transaction.

### 5.4 Bulk upsert for full sync

```python
Contact.objects.bulk_create(
    [Contact(crm_id=d.crm_id, email=d.email.lower(), full_name=d.full_name, crm_version=d.version)
     for d in batch],
    update_conflicts=True,
    unique_fields=["crm_id"],
    update_fields=["email", "full_name", "crm_version"],     # never Django-owned fields
    batch_size=500,
)
```

`bulk_create(update_conflicts=…)` bypasses `save()`, signals and the version guard (the `ON CONFLICT DO UPDATE`
overwrites unconditionally). Use it for full loads where the source is authoritative, or filter batches against
current versions first. Detect deletions in full sync by marking rows not seen in this run
(`synced_at < run_started_at` → tombstone), never by hard-deleting before the run completed successfully.

### 5.5 Failed-record recovery

```python
class SyncFailure(models.Model):
    integration = models.CharField(max_length=64)
    external_id = models.CharField(max_length=128)
    error = models.TextField()
    payload = models.JSONField()                          # redact PII if not needed for replay
    attempts = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["integration", "external_id"], condition=Q(resolved_at__isnull=True),
            name="syncfailure_open_uniq")]
```

Provide a management command `replay_sync_failures --integration partner_crm.contacts` that re-fetches each
record by id (fresh data, not the stored payload) and applies it; mark resolved on success. Alert when open
failures exceed a threshold or age.

### 5.6 Reconciliation job

```python
# apps/crm/reconcile.py — compare hashes, not whole objects, to keep memory bounded
import hashlib
import json


def fingerprint(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()


def reconcile_contacts(gateway) -> dict[str, list[str]]:
    remote = {c.crm_id: fingerprint(c.email.lower(), c.full_name)
              for c in gateway.iter_all_contacts_summary()}             # streamed; ids + compared fields
    local = {crm_id: fingerprint(email, name) for crm_id, email, name in
             Contact.objects.filter(crm_deleted_at__isnull=True)
             .values_list("crm_id", "email", "full_name").iterator(chunk_size=2000)}
    report = {
        "missing_locally": sorted(remote.keys() - local.keys()),
        "missing_remotely": sorted(local.keys() - remote.keys()),
        "mismatched": sorted(k for k in remote.keys() & local.keys() if remote[k] != local[k]),
    }
    return report
```

Reconciliation outputs a **report**, then a separate, auditable repair step (refetch-and-apply for copies;
push for Django-owned data; human review for conflicts). For very large sets, reconcile per partition (id range,
date bucket) and compare partition-level hashes first, then drill down.

## 6. Production use cases

| Case | Design |
|---|---|
| CRM contacts into Django (CRM is SoR for identity, Django for consent) | Attribute ownership; webhook-triggered refetch; hourly incremental; weekly full reconcile |
| Product catalogue from PIM (200k SKUs) | Delta API with cursor; bulk upsert batches; nightly hash reconciliation per category |
| Employee directory from Entra ID | Microsoft Graph delta query (`deltaLink` as cursor) [provider-specific] `VERIFY` token expiry behaviour; deletions via `@removed` markers |
| Inventory from WMS | Events (stock changed) with version guard; reconciliation every 15 minutes for top SKUs |
| Django orders pushed to ERP | Outbox → SOAP adapter; ERP acknowledges with its document number stored as external reference; daily reconcile by order id |

Failure scenarios to rehearse: provider resets delta tokens (forces a full resync — must be safe and
throttled); a mapping bug corrupts a field for 3 days (reconciliation detects; re-apply from source); a
provider merges two records (late events for the absorbed id); a sync job runs twice concurrently after a
deploy (lock prevents it).

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Full sync | Simple, detects deletes, self-healing | Cost and duration grow with data; rate limits |
| Incremental | Efficient, low latency | Misses (skew, deletes) without overlap and reconciliation |
| Event-driven | Near-real-time | Loss/reordering; needs backfill and dedupe |
| CDC | Complete change history | Couples to source schema; infra-heavy |
| Local copy (any sync) | Fast reads, availability independent of provider | Staleness, storage, privacy duties for copied data |
| Live API composition (no copy) | Always fresh | Latency and availability coupled to provider |

## 8. Security and performance considerations

- Copying data copies **obligations**: PII retention, erasure requests (propagate deletes/anonymisation to
  copies), data residency. Sync only fields you need. See
  [integration-security.md](integration-security.md).
- Payloads stored for debugging (`SyncFailure.payload`) need redaction and retention limits.
- Scope sync credentials read-only where Django is a pure consumer.
- Performance: stream pages (generators), apply in bounded batches, use `iterator(chunk_size=…)` and
  `values_list` for comparisons, avoid per-record `save()` loops for full loads; index external id and
  `synced_at`. Respect provider rate limits with a limiter, not sleeps. See
  [file-and-data-processing-performance.md](../../django-performance/references/file-and-data-processing-performance.md).
- Long sync runs hold no long transactions: one short transaction per record or batch.

## 9. Common mistakes and anti-patterns

- **No declared SoR**, so both sides overwrite each other in a loop ("ping-pong sync"). Also: echoing back
  changes you just received — tag the origin and skip self-originated changes.
- **Checkpoint set to `now()`** instead of the max observed source timestamp; no overlap window.
- **Hard deletes on absence** in a partial or failed run.
- **Trusting events alone** — no periodic reconciliation.
- **Comparing naïve and aware datetimes**, or mixing source local time with UTC.
- **Using internal pks as the external key** (or vice versa); no unique constraint on external id.
- **One giant transaction** wrapping the whole sync, holding locks for minutes and losing all progress on error.
- **Auto-merging fuzzy duplicates** for customers or accounts.
- **`bulk_create(update_conflicts=True)` overwriting Django-owned fields** because `update_fields` was copied
  from the model's full field list.
- **Retrying a poison record forever** and blocking the rest of the run.

## 10. Troubleshooting and validation

| Symptom | Check |
|---|---|
| Records missing locally | Checkpoint vs provider max `updated_at`; overlap window; pagination stability (sort key); permission scope of the sync user |
| Values revert after edit | Attribute ownership; another sync direction overwriting; LWW with skewed clocks |
| Duplicates | Unique constraint on external id and normalised natural key; merges not re-pointed |
| Sync slower every week | Full sync on growing data; missing index on external id; per-row queries |
| Deleted upstream, still visible | No tombstones in delta; reconciliation not detecting `missing_remotely` |
| Concurrent runs | Lock missing/expired before run finished (lock TTL < run time) |

Validation checklist:

- [ ] SoR matrix per attribute documented; Django-owned fields excluded from inbound updates.
- [ ] External ids stored with unique constraints; natural-key duplicates prevented or detected.
- [ ] Apply is idempotent and version-guarded; overlap window on timestamp deltas.
- [ ] Checkpoint advances only after successful apply; progress persisted per page.
- [ ] Deletions handled (tombstones, absence-in-full-sync with safety checks).
- [ ] Failed records parked, alerted and replayable; poison records do not block the run.
- [ ] Runs cannot overlap (lock); runs and lag are monitored
  ([integration-observability-and-operations.md](integration-observability-and-operations.md)).
- [ ] Periodic reconciliation exists, with an auditable repair path.
- [ ] Tests cover overlap duplicates, out-of-order versions, deletes and resume after crash
  ([integration-testing-and-contracts.md](integration-testing-and-contracts.md)).

## 11. Related references and skills

- [integration-engineering-fundamentals.md](integration-engineering-fundamentals.md) — data ownership and SoR
- [integration-architecture-patterns.md](integration-architecture-patterns.md) — outbox, inbox, ACL, strangler
- [http-and-rest-api-integrations.md](http-and-rest-api-integrations.md) — pagination and response validation for pull sync
- [webhooks-and-inbound-integrations.md](webhooks-and-inbound-integrations.md) — push triggers
- [payment-and-financial-integrations.md](payment-and-financial-integrations.md) — monetary reconciliation, settlement files
- [file-transfer-and-object-storage.md](file-transfer-and-object-storage.md) — file-based sync inputs
- [celery-and-background-integrations.md](celery-and-background-integrations.md) — scheduling sync jobs
- Bulk import/ETL mechanics: [django-data-processing](../../django-data-processing/SKILL.md)
- Upserts, constraints, functional indexes: [django-orm](../../django-orm/SKILL.md);
  logical replication and `pg_trgm`: [postgresql-engineering](../../postgresql-engineering/SKILL.md)

## 12. Official documentation and external references

- Django `bulk_create(update_conflicts=…)`: <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create>
- Django `UniqueConstraint` with expressions/conditions: <https://docs.djangoproject.com/en/5.2/ref/models/constraints/#uniqueconstraint>
- Django `QuerySet.iterator()`: <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator>
- PostgreSQL `INSERT … ON CONFLICT`: <https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT>
- PostgreSQL logical decoding: <https://www.postgresql.org/docs/current/logicaldecoding.html>
- PostgreSQL `pg_trgm`: <https://www.postgresql.org/docs/current/pgtrgm.html>
- PostgreSQL advisory locks: <https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS>
- Microsoft Graph delta query: <https://learn.microsoft.com/en-us/graph/delta-query-overview>
- Debezium documentation [third-party]: <https://debezium.io/documentation/>
- RFC 9110 §8.8 (ETag / validators) and §13.1 (conditional requests): <https://www.rfc-editor.org/rfc/rfc9110>
