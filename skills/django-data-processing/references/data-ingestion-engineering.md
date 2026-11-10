# Data ingestion engineering

> **Scope:** Getting data *into* the system reliably: ingestion patterns (API, file, database,
> webhook, cloud-storage drop, scheduled), pull vs push, full vs incremental, CDC fundamentals,
> multi-source connectors, pagination, rate limits, ingestion retries, checkpoints and watermarks,
> duplicate detection at ingest, raw landing, ingestion metadata and audit (`IngestionBatch`,
> `RawRecord`). **Not here:** HTTP client design, auth flows and webhook signature mechanics
> ([../../django-integrations/references/http-and-rest-api-integrations.md](../../django-integrations/references/http-and-rest-api-integrations.md),
> [../../django-integrations/references/webhooks-and-inbound-integrations.md](../../django-integrations/references/webhooks-and-inbound-integrations.md));
> file-format parsing ([file-processing-engineering.md](file-processing-engineering.md));
> pipeline-level watermarks and run state ([etl-and-elt-pipelines.md](etl-and-elt-pipelines.md));
> validation ([data-validation-and-cleansing.md](data-validation-and-cleansing.md)).

## 1. Purpose and scope

Ingestion is the boundary where external, untrusted, possibly duplicated, out-of-order data
becomes an internal, attributable, replayable dataset. A good ingestion layer:

- captures **exactly what the source sent** (raw landing) before interpretation, so downstream
  bugs can be fixed by replay rather than re-fetching;
- records **who/what/when/how** for every batch (`IngestionBatch`) and every record (`RawRecord`);
- is **restartable** from a checkpoint and **idempotent** under re-fetch;
- detects **duplicates** at the earliest point;
- respects the source's **rate limits** and failure modes.

## 2. Core concepts

### 2.1 Ingestion patterns

| Pattern | Initiator | Typical source | Key concern |
|---|---|---|---|
| API pull | Us (scheduled/polled) | REST/GraphQL partner API | Pagination, rate limits, cursor stability |
| Webhook push | Source | Payment provider, CRM | Signature verification, at-least-once, ordering |
| File upload | User | CSV/XLSX from browser | Size limits, malicious content, user feedback |
| File drop (SFTP/object storage) | Source writes, we poll/are notified | Bank statements, supplier feeds | Completeness (partial uploads), re-drops |
| Database read | Us | Read replica, legacy DB, dump file | Load on source, consistent snapshot, CDC |
| Message/stream | Source publishes | Kafka/SQS/Event Hubs | Offsets, rebalances, DLQ |
| Scheduled extract | Us, on a timer | Any of the above | Overlap, missed runs, watermarks |

### 2.2 Pull vs push

| | Pull | Push |
|---|---|---|
| Who controls rate | We do | Source does — must absorb bursts |
| Freshness | Polling interval | Near-real-time |
| Missed data | Detected by watermark gaps | Silent unless source retries or we reconcile |
| Typical fix for gaps | Re-pull window | Periodic pull-based reconciliation sweep |

Robust designs combine both: **push for latency, scheduled pull for completeness**.

### 2.3 Full vs incremental

- **Full load:** fetch the entire dataset each run. Simple, self-healing (detects deletes by
  absence), expensive at scale.
- **Incremental:** fetch only changes since a watermark (timestamp, sequence id, cursor). Cheap,
  but blind to deletes and dependent on source timestamp correctness.
- Common hybrid: incremental every 15 minutes, full snapshot weekly to correct drift and detect
  deletes.

### 2.4 Change Data Capture (CDC) fundamentals

| Kind | How | Captures deletes | Ordering | Cost / caveats |
|---|---|---|---|---|
| Query-based (timestamp) | `WHERE updated_at > :wm` | No (unless soft-delete flag) | By timestamp — ties and clock skew | Needs reliable, indexed `updated_at`; misses rows whose timestamp is set in the past |
| Query-based (sequence) | `WHERE change_seq > :wm` | Only via tombstone rows | Strict if sequence is monotonic per commit | Sequences can commit out of order across transactions (VERIFY: sequence/commit ordering on the source DB) |
| Log-based | Read DB write-ahead/binlog (e.g. PostgreSQL logical decoding, Debezium) | Yes | Commit order | Needs DB privileges, replication slots, operational tooling |
| Trigger-based | Triggers write to a changelog table | Yes | Changelog id | Write amplification on the source |
| Snapshot diff | Compare full snapshots by key/hash | Yes | None | Expensive; good for small/medium tables |

Mechanics worth knowing: a timestamp watermark is unsafe if a long-running source transaction
commits a row whose `updated_at` is *older* than the watermark you already advanced past —
that row is never seen. Mitigate with an **overlap window** (re-read the last N minutes each run
and dedupe) or by using the source's commit-ordered change log. A PostgreSQL logical replication
slot retains WAL until consumed; an abandoned slot can fill the source's disk
(see [../../postgresql-engineering/SKILL.md](../../postgresql-engineering/SKILL.md)).

### 2.5 Pagination styles

| Style | Request | Stable under concurrent writes? | Notes |
|---|---|---|---|
| Offset/limit | `?offset=1000&limit=100` | No — inserts/deletes shift pages, causing skips/dupes | Cost grows with offset on the source |
| Page number | `?page=11` | No (same as offset) | Common in legacy APIs |
| Cursor (opaque) | `?cursor=abc` from previous response | Usually yes | Persist the cursor as a checkpoint; cursors may expire (VERIFY: cursor lifetime per API) |
| Keyset | `?after_id=123&limit=100` ordered by id | Yes for the ordering key | Requires a unique, indexed sort key |
| Time-window | `?from=t0&to=t1` | Yes if windows are closed and late data handled | Combine with inner pagination when a window is large |

### 2.6 Watermarks and late-arriving data

A **watermark** is the high-water position up to which the ingestion believes it has complete
data. It must be advanced only after the data up to it is durably landed. Late-arriving data
(source records whose event time is earlier than the watermark) is handled by:

1. **Overlap window:** query `updated_at > watermark - overlap`; duplicates removed by dedupe.
2. **Grace period:** only advance the watermark to `now() - grace` (e.g. 5 minutes), so in-flight
   source transactions have time to commit.
3. **Periodic re-sweep:** a daily/weekly wider window or full snapshot to catch stragglers.

Pipeline-wide watermark storage (`Watermark` model) is owned by
[etl-and-elt-pipelines.md](etl-and-elt-pipelines.md); ingestion records the *batch-level*
range it actually fetched on `IngestionBatch`.

### 2.7 Duplicate detection at ingest

| Technique | Identity | Catches | Misses |
|---|---|---|---|
| Natural key | `(source, external_id)` | Re-delivery of the same entity | Different ids for the same real entity |
| Content hash | SHA-256 of canonicalised payload | Identical re-sends, re-dropped files | Same entity with changed content (by design) |
| File hash | `hashlib.file_digest` of bytes | Same file uploaded twice | Re-exported file with a different timestamp header |
| Idempotency key | Sender-provided key (webhook event id) | Retries of the same event | Sender bugs that reuse keys |
| Version/sequence | `(external_id, source_version)` | Older updates arriving after newer | — |

Use natural key + content hash together: same key + same hash → skip; same key + new hash →
new version to process.

## 3. Internal mechanics

### 3.1 Ingestion flow with checkpointing

```text
create IngestionBatch(status=RUNNING, source, scope, cursor_start)
loop:
    page = connector.fetch(cursor)           # transient errors → retry with backoff
    BEGIN
      INSERT RawRecord rows (ON CONFLICT (source, natural_key, content_hash) DO NOTHING)
      UPDATE IngestionBatch SET cursor = page.next_cursor, fetched += n, new += inserted
    COMMIT                                   # checkpoint and data together
    if page.next_cursor is None: break
UPDATE IngestionBatch SET status=LANDED, cursor_end=..., completed_at=now()
advance pipeline Watermark (only now)
```

On crash, a restart finds the `RUNNING` batch, reads its committed `cursor`, and continues. Pages
re-fetched after the crash are absorbed by the `ON CONFLICT DO NOTHING` dedupe.

### 3.2 Rate limits and retries

- Honour `Retry-After` (seconds or HTTP date) on 429/503; otherwise exponential backoff with
  jitter, capped. Respect documented quotas proactively with a token bucket shared across workers
  (e.g. in Redis) when several workers hit the same API.
- Classify: **transient** (timeouts, connection reset, 429, 502/503/504) → retry; **permanent**
  (400, 401/403 after token refresh, 404 for a required resource, schema mismatch) → fail the
  batch with a clear error; **data** errors (a single malformed record) → land it raw and let
  validation reject it — never fail ingestion for one bad payload.
- Bound total retry time so a run does not overlap the next scheduled run indefinitely.

Client-level retry/circuit-breaker mechanics: [../../django-integrations/references/integration-reliability-and-resilience.md](../../django-integrations/references/integration-reliability-and-resilience.md).

### 3.3 Raw landing zone

Two layers, used together or separately:

- **Object storage** (S3/Azure Blob via django-storages): the original bytes of files and API
  responses, immutable, keyed by `source/yyyy/mm/dd/batch_id/...`. Cheap, ideal for replay.
- **`RawRecord` table**: one row per source record with JSONB payload, natural key, hash, and
  batch FK. Queryable, dedupe-enforcing, the input to validation.

Never mutate landed raw data. Corrections happen downstream; replay starts from raw.

## 4. Engineering decisions and trade-offs

| Situation | Prefer | Avoid / escalate when |
|---|---|---|
| Partner API with `updated_since` filter, < 100k changes/day | Scheduled incremental pull, timestamp watermark + overlap window | Source timestamps unreliable → periodic full snapshot diff |
| Source pushes webhooks | Accept, verify, land raw, return 2xx fast; process async | Volume spikes beyond web tier → put a queue in front |
| Need deletes from a source DB you control | Log-based CDC or soft-delete flags | Team cannot operate replication slots → nightly snapshot diff |
| Read-only access to legacy DB | Read replica, keyset pagination, off-peak window | Replica lag matters → record replica lag in batch metadata |
| Daily file drop on SFTP/S3 | Poll prefix, require completion marker or stable size, file-hash dedupe | Producer can't write markers → wait for object age > N minutes |
| User uploads | Store to object storage, create batch, background ingest | — (never parse large uploads in the request) |
| Many heterogeneous sources | `SourceConnector` protocol + per-source config | Dozens of sources with complex scheduling → external orchestrator |
| Offset-only API on a mutating dataset | Sort by stable key if API allows; else overlap + dedupe; full reconcile | Accept occasional misses only if business tolerates and reconcile catches |

| Raw landing choice | Gain | Cost |
|---|---|---|
| Object storage only | Cheap, exact bytes, unlimited size | Not queryable; re-parse on replay |
| `RawRecord` table only | Queryable, DB-enforced dedupe | Table growth, JSONB storage cost, loses original bytes |
| Both | Exact replay + queryable dedupe | Two writes; retention on both |

## 5. Django/Python implementation patterns

### 5.1 Models: `IngestionBatch` and `RawRecord`

```python
from django.db import models  # [Django]


class IngestionBatch(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING"
        LANDED = "LANDED"          # raw data complete; downstream may start
        FAILED = "FAILED"
        SUPERSEDED = "SUPERSEDED"  # replaced by a re-ingest of the same scope

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    source = models.CharField(max_length=64)                 # "acme_crm", "bank_sftp", "upload"
    kind = models.CharField(max_length=16)                   # api | file | db | webhook | storage
    mode = models.CharField(max_length=12, default="incremental")   # full | incremental
    status = models.CharField(max_length=12, choices=Status, default=Status.RUNNING)
    scope_start = models.DateTimeField(null=True)            # window actually requested
    scope_end = models.DateTimeField(null=True)
    cursor = models.TextField(blank=True)                    # checkpoint: last committed page cursor
    source_uri = models.CharField(max_length=1024, blank=True)   # storage key / endpoint / file
    content_sha256 = models.CharField(max_length=64, blank=True) # whole-file hash (file kinds)
    fetched_count = models.PositiveIntegerField(default=0)
    new_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=64)           # "beat", "user:42", "webhook"
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "source", "-started_at"])]
        constraints = [
            models.UniqueConstraint(                         # same file landed once per tenant/source
                fields=["tenant", "source", "content_sha256"],
                condition=~models.Q(content_sha256=""),
                name="uniq_ingested_file",
            ),
        ]
```

```python
class RawRecord(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    batch = models.ForeignKey(IngestionBatch, on_delete=models.PROTECT, related_name="records")
    source = models.CharField(max_length=64)
    natural_key = models.CharField(max_length=255)          # e.g. external id
    content_sha256 = models.CharField(max_length=64)        # hash of canonical payload
    source_version = models.CharField(max_length=64, blank=True)  # etag/version/updated_at
    source_position = models.PositiveIntegerField(null=True)      # row/line/page offset
    payload = models.JSONField()                            # exactly as received (parsed)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)          # set by downstream stage

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "source", "natural_key", "content_sha256"],
                name="uniq_raw_version",
            ),
        ]
        indexes = [models.Index(fields=["batch", "processed_at"])]
```

The unique constraint makes re-fetching idempotent: identical content is ignored, changed
content lands as a new version. Partition or purge `RawRecord` by `received_at` per retention.

### 5.2 Canonical content hash

```python
import hashlib  # [stdlib]
import json     # [stdlib]


def content_hash(payload: dict) -> str:
    """Stable across key order and whitespace; excludes volatile fields."""
    volatile = {"fetched_at", "_links", "request_id"}
    canonical = {k: v for k, v in payload.items() if k not in volatile}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

### 5.3 `SourceConnector` protocol

```python
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SourceRecord:
    natural_key: str
    payload: dict
    source_version: str = ""
    position: int | None = None


@dataclass(frozen=True, slots=True)
class Page:
    records: list[SourceRecord]
    next_cursor: str | None          # None → exhausted


class SourceConnector(Protocol):
    source_name: str

    def fetch_page(self, *, cursor: str | None, since: str | None, until: str | None) -> Page:
        """Return one page. Raise TransientSourceError / PermanentSourceError only."""
        ...


class TransientSourceError(Exception): ...
class PermanentSourceError(Exception): ...
```

Connectors know the source's paging/auth quirks and nothing about Django models; the ingestion
service knows models and nothing about the source. Each connector is unit-tested against recorded
responses. Credentials come from settings/secret stores, never from `IngestionBatch` rows
([../../django-security-engineering/references/secrets-and-cryptography.md](../../django-security-engineering/references/secrets-and-cryptography.md)).

### 5.4 Generic landing service

```python
from django.db import transaction
from django.db.models import F
from django.utils import timezone


def land(connector: SourceConnector, batch: IngestionBatch, *, since: str | None,
         until: str | None) -> IngestionBatch:
    cursor = batch.cursor or None                       # resume from checkpoint
    while True:
        page = connector.fetch_page(cursor=cursor, since=since, until=until)
        rows = {}                                        # dedupe within the page
        for r in page.records:
            h = content_hash(r.payload)
            rows[(r.natural_key, h)] = RawRecord(
                tenant_id=batch.tenant_id, batch=batch, source=connector.source_name,
                natural_key=r.natural_key, content_sha256=h, source_version=r.source_version,
                source_position=r.position, payload=r.payload)
        with transaction.atomic():
            # ignore_conflicts → PKs not set and the return value can't tell new from duplicate;
            # authoritative counts are recomputed from the DB after landing.
            RawRecord.objects.bulk_create(rows.values(), ignore_conflicts=True)
            IngestionBatch.objects.filter(pk=batch.pk).update(
                cursor=page.next_cursor or "", fetched_count=F("fetched_count") + len(page.records))
        cursor = page.next_cursor
        if cursor is None:
            break
    batch.refresh_from_db()
    batch.new_count = RawRecord.objects.filter(batch=batch).count()
    batch.duplicate_count = batch.fetched_count - batch.new_count
    batch.status, batch.completed_at = IngestionBatch.Status.LANDED, timezone.now()
    batch.save(update_fields=["new_count", "duplicate_count", "status", "completed_at"])
    return batch
```

Transient errors from `fetch_page` propagate to the caller (Celery retry or command loop), which
resumes from the committed `cursor`. `ignore_conflicts=True` is acceptable *here* because duplicates are the
expected, measured outcome — not a silent loss.

### 5.5 Example: external paginated REST API (cursor + time window)

```python
import httpx  # [third-party: httpx] — or requests; client design lives in django-integrations


class AcmeCrmConnector:
    source_name = "acme_crm"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client                              # base_url, auth, timeouts preconfigured

    def fetch_page(self, *, cursor, since, until) -> Page:
        params = {"limit": 200, "updated_since": since, "updated_before": until}
        if cursor:
            params = {"cursor": cursor, "limit": 200}     # cursor encodes the filter (VERIFY: per API)
        try:
            resp = self.client.get("/v2/customers", params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientSourceError(str(exc)) from exc
        if resp.status_code in (429, 502, 503, 504):
            raise TransientSourceError(f"{resp.status_code} retry_after={resp.headers.get('Retry-After')}")
        if resp.status_code >= 400:
            raise PermanentSourceError(f"{resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        return Page(
            records=[SourceRecord(natural_key=str(c["id"]), payload=c,
                                  source_version=c.get("updated_at", "")) for c in body["data"]],
            next_cursor=body.get("next_cursor"),
        )
```

Scheduler computes `since = watermark - overlap`, `until = now - grace`; after `LANDED`, the
pipeline advances the watermark to `until`.

### 5.6 Example: uploaded files and cloud-storage drop folders

```python
from django.core.files.storage import storages  # [Django] (Django ≥ 4.2)
import hashlib


def register_file(*, tenant_id: int, source: str, storage_key: str, triggered_by: str) -> IngestionBatch | None:
    storage = storages["ingest"]   # e.g. storages.backends.s3.S3Storage / azure_storage.AzureStorage
    with storage.open(storage_key, "rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()      # (Python ≥ 3.11)
    batch, created = IngestionBatch.objects.get_or_create(
        tenant_id=tenant_id, source=source, content_sha256=digest,
        defaults={"kind": "file", "mode": "full", "source_uri": storage_key,
                  "triggered_by": triggered_by},
    )
    return batch if created else None        # None → same bytes already ingested; report duplicate
```

- **Uploads:** the view stores the file under a generated key (never the user's filename) and
  calls `register_file` in `transaction.on_commit` before enqueuing parsing. Upload security:
  [data-processing-security.md](data-processing-security.md).
- **Drop folders (S3 / Azure Blob via [third-party: django-storages]):** a beat task lists the
  inbound prefix (`storage.listdir("inbound/")`), skips objects younger than N minutes or without
  a `.done` marker (protects against partial uploads), registers each, then moves processed
  objects to `processed/` (copy + delete — object stores have no atomic rename). Event-driven
  alternatives (S3 event notifications → SQS, Event Grid) reduce polling
  ([../../django-integrations/references/file-transfer-and-object-storage.md](../../django-integrations/references/file-transfer-and-object-storage.md)).
  `listdir` on very large prefixes may be slow/paginated (VERIFY: backend listing behaviour).
- Parsing the file into `RawRecord`s uses the streaming readers in
  [file-processing-engineering.md](file-processing-engineering.md).

### 5.7 Example: database exports (read replica / dump files)

```python
from django.db import connections  # [Django]


class LegacyErpConnector:
    """Keyset pagination over a read-only replica configured as DATABASES['erp_replica']."""
    source_name = "legacy_erp"

    def fetch_page(self, *, cursor, since, until) -> Page:
        last_id = int(cursor or 0)
        with connections["erp_replica"].cursor() as cur:
            cur.execute(
                "SELECT id, code, name, updated_at FROM customers "
                "WHERE id > %s AND updated_at >= %s ORDER BY id LIMIT 1000",
                [last_id, since],
            )
            rows = cur.fetchall()
        recs = [SourceRecord(str(r[0]), {"id": r[0], "code": r[1], "name": r[2],
                                         "updated_at": r[3].isoformat()}) for r in rows]
        return Page(recs, str(rows[-1][0]) if len(rows) == 1000 else None)
```

- Use a dedicated DB alias with a **read-only role**, a statement timeout, and route nothing else
  to it. Parameterised SQL only.
- Record replica lag at batch start in metadata if freshness matters (VERIFY: lag query for the
  source engine, e.g. `pg_last_xact_replay_timestamp()` on PostgreSQL).
- **Dump files** (`pg_dump` custom format, CSV exports from a vendor): restore into an isolated
  schema/database, never the application schema, then ingest with the same connector over that
  alias; or, for CSV exports, treat as a file drop.

## 6. Production use cases

| Scenario | Pattern | Dedupe / watermark | Notes |
|---|---|---|---|
| ERP → CRM customer sync | API pull every 10 min + nightly full | `updated_at` watermark, 15-min overlap, `(source, id, hash)` | Nightly full detects deletes by absence |
| Payment provider events | Webhook push | Provider event id as idempotency key | Verify signature, land, 200 fast; daily pull of settlement report to reconcile |
| Bank statement files | SFTP → S3 drop | File SHA-256 + statement number | Re-dropped corrected file → new batch, old `SUPERSEDED` |
| Hospital doctor roster | Read replica of HR DB | Keyset on id + `updated_at` | Off-peak window; statement timeout 60 s |
| Supplier product catalog | Daily Azure Blob drop (Parquet) | File hash; SKU + hash per record | 2M records; land to object storage, stream into staging |
| Employee attendance | Manager CSV upload | File hash per tenant; `(employee_no, date)` | User sees duplicate-file warning immediately |

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Raw landing before validation | Replay without re-fetch; audit evidence; decouples source outages from processing | Storage cost; retention and PII obligations |
| Natural key + content hash | Cheap idempotency; detects real changes | Hash must exclude volatile fields or every fetch looks "changed" |
| Timestamp CDC | Easy, no source privileges | Misses deletes and late commits without overlap |
| Log-based CDC | Complete, ordered, captures deletes | Operational burden, privileges, slot management |
| Connector protocol | Uniform service code, testable per source | Abstraction can leak for exotic sources |

## 8. Security and performance considerations

**Security**

- Treat every source as untrusted: size-cap API responses and files, enforce JSON depth/size
  limits, never `eval`/unpickle source data, and validate before persisting into domain tables.
- Webhooks: verify signatures and timestamps before landing; reject replays outside a tolerance
  window ([../../django-integrations/references/webhooks-and-inbound-integrations.md](../../django-integrations/references/webhooks-and-inbound-integrations.md)).
- Credentials per source in a secret store; least-privilege, read-only DB roles; separate DB alias.
- Raw payloads often contain PII/financial data: encrypt storage, restrict admin access to
  `RawRecord`, mask in logs, apply retention and deletion ([data-processing-security.md](data-processing-security.md)).
- Tenant isolation: every `IngestionBatch`/`RawRecord` query filters by `tenant`; storage keys
  are prefixed by tenant and generated server-side (no path traversal from filenames).

**Performance**

- Batch `RawRecord` inserts per page (hundreds–thousands); one transaction per page.
- JSONB payloads inflate tables; for very large feeds land bytes to object storage and keep only
  keys + hashes in the DB.
- Index `(tenant, source, natural_key)` for dedupe lookups; partition `RawRecord` by time when it
  exceeds tens of millions of rows (see [../../postgresql-engineering/SKILL.md](../../postgresql-engineering/SKILL.md)).
- Parallelise by independent scope (time window, tenant, partition), not by page of a single
  cursor — cursors are sequential.
- Throttle against source limits; a faster ingest that triggers 429 storms is slower overall.

## 9. Common mistakes and anti-patterns

- Advancing the watermark before data is committed → crash loses the window forever → advance
  only after `LANDED`, in the same transaction as the final checkpoint.
- Watermark = `max(updated_at)` with no overlap/grace → late-committing rows skipped → overlap
  window + grace period + periodic full sweep.
- Offset pagination over a live, mutating dataset → skipped and duplicated records → keyset or
  cursor pagination; overlap + dedupe if unavoidable.
- Hashing the payload including `fetched_at`/ETag headers → every record looks new → canonicalise
  and drop volatile fields.
- Processing webhooks synchronously in the request → provider timeouts and retries cause
  duplicates → land + 2xx + async processing.
- Polling a drop folder and reading files still being written → truncated imports → completion
  markers or age/size-stability checks.
- Failing the whole ingest on one malformed record → one bad row blocks a day's data → land raw,
  reject in validation.
- Using the user's filename as storage key → overwrites and path traversal → generated keys.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Records missing that exist in source | Watermark advanced past late commits; offset pagination skips | Compare source counts per window; add overlap; re-ingest window |
| Duplicate count ~100% every run | Hash includes volatile fields; overlap window too large | Inspect two `RawRecord` payloads with the same key |
| Ingest never finishes | Cursor loop (API returns same cursor); retries unbounded | Detect repeated cursor; cap pages and total retry time |
| 429 storms | Parallel workers sharing one quota | Shared token bucket; reduce concurrency; honour `Retry-After` |
| Same file ingested twice | Hash not computed or constraint missing | `uniq_ingested_file` constraint; check `content_sha256` populated |
| Source DB slowed during ingest | Unindexed `updated_at`, large pages, peak hours | Keyset on indexed key; smaller pages; replica; off-peak schedule |
| Replication slot disk growth on source | CDC consumer stopped | Monitor slot lag; drop abandoned slots |

**Validation checklist**

- [ ] Every run creates an `IngestionBatch` with source, scope, trigger and counts.
- [ ] Raw data is landed immutably before transformation.
- [ ] Checkpoint (cursor) and records commit in the same transaction.
- [ ] Watermark advanced only after `LANDED`; overlap and grace configured.
- [ ] Dedupe enforced by a DB unique constraint, not only by application checks.
- [ ] Transient vs permanent errors classified; retries bounded.
- [ ] fetched = new + duplicate per batch; reconciliation against source counts scheduled.

## 11. Related references and skills

- In this skill: [fundamentals](data-processing-fundamentals.md) · [ETL/ELT pipelines](etl-and-elt-pipelines.md) · [file formats](file-processing-engineering.md) · [columnar/archives](columnar-archive-and-document-files.md) · [validation](data-validation-and-cleansing.md) · [background jobs](django-background-processing.md) · [reliability](processing-reliability-and-recovery.md) · [security](data-processing-security.md) · [reference architectures](production-architectures-imports-and-pipelines.md)
- django-integrations: [HTTP/REST clients](../../django-integrations/references/http-and-rest-api-integrations.md) · [webhooks](../../django-integrations/references/webhooks-and-inbound-integrations.md) · [object storage](../../django-integrations/references/file-transfer-and-object-storage.md) · [resilience](../../django-integrations/references/integration-reliability-and-resilience.md) · [messaging](../../django-integrations/references/messaging-and-event-driven-integrations.md) · [AWS](../../django-integrations/references/aws-and-cloud-integrations.md) · [Azure](../../django-integrations/references/azure-integrations.md)
- [django-orm bulk operations](../../django-orm/references/bulk-operations-and-batching.md) · [django-security-engineering file uploads](../../django-security-engineering/references/file-upload-security.md)

## 12. Official documentation and external references

- Django multiple databases (read-only alias): https://docs.djangoproject.com/en/5.2/topics/db/multi-db/
- Django file storage / `storages`: https://docs.djangoproject.com/en/5.2/ref/files/storage/
- Django `bulk_create`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#bulk-create
- django-storages (S3, Azure): https://django-storages.readthedocs.io/en/latest/
- Python `hashlib.file_digest`: https://docs.python.org/3/library/hashlib.html#hashlib.file_digest
- PostgreSQL logical decoding: https://www.postgresql.org/docs/current/logicaldecoding.html
- PostgreSQL replication slots: https://www.postgresql.org/docs/current/warm-standby.html#STREAMING-REPLICATION-SLOTS
- AWS S3 event notifications: https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html
- Azure Blob Storage events: https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-event-overview
- OWASP Input Validation Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
