# Reporting Security and Compliance

> **Scope:** Report security as a data-access boundary: the threat model for report requests, generation, artifacts and delivery; report-level, row-level and field-level authorisation; tenant isolation; signed-URL threat analysis; renderer SSRF, formula injection and path traversal as they apply to reports; temporary and cached artifacts; encryption; retention/deletion policy; audit requirements; and the line between technical controls and legal compliance. **Not here:** general Django security mechanics → [django-security-engineering](../../django-security-engineering/SKILL.md); signed-URL and download-view *implementation* → [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md); the `safe_cell()` implementation → [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md); the PDF `url_fetcher` implementation → [pdf-report-generation.md](pdf-report-generation.md); the cleanup task → [background-report-generation.md](background-report-generation.md).

## 1. Purpose and scope

A report is a **bulk read of data**. Often it reads the most sensitive data the system holds (payroll,
invoices, ledger entries, customer PII) and packages it into a portable file. That file leaves the
application's access controls the moment it is downloaded. Report security is therefore treated
as a **data-access boundary**, not as a file-hosting problem:

- Every row in a report must be a row the requester could see on screen *at the moment the data
  was read* — no more.
- Every artifact must be retrievable only by principals who may read that run — never by guessing
  or manipulating a run id, artifact id, storage name or download URL.
- Every copy of report data (temp files, caches, object storage, email attachments, logs) is
  inside the boundary and is covered by the same retention and deletion policy.

This reference tells an engineer which controls must exist at each stage of the report lifecycle
and how to prove them. It does not replace a legal compliance assessment.

## 2. Core concepts

### Report security boundaries

```text
 request ──► generation ──► artifact at rest ──► delivery ──► consumer's device
   │             │                │                  │
 authz #1     authz #2         private storage     authz #3 (download)
 params       scoped           encryption          signed URL / view
 validation   selectors        retention           audit
```

Three authorisation checkpoints are required because time passes between them:

| Checkpoint | Question | Why separate |
|---|---|---|
| Request | May this user request report `key` with these params in this tenant? | Rejects early with a useful error |
| Generation | Does the requester (still) hold the permission and scope? | Roles can be revoked between request and execution (minutes for queues, days for schedules) |
| Download | May *this* principal read *this* artifact now? | The downloader may differ from the requester; artifacts outlive role changes |

### Levels of authorisation

| Level | Example | Enforced in |
|---|---|---|
| Report-level | Only `payroll_admin` may run `payroll.monthly_register` | `ReportSpec.permission`, checked by `request_report` and the task |
| Row-level | A regional manager sees only their region's orders | The spec's `selector`, scoped by tenant + user |
| Field-level | Salary column masked for HR assistants; IBAN shown as last 4 digits | Column set chosen from the user's role at generation time |
| Artifact-level | Only requester and explicit recipients may download | `visible_runs` selector + download check |

### Classification

`ReportSpec.classification` (`internal` / `confidential` / `restricted`) drives the controls:
the TTL of download links, whether email attachments are allowed, whether downloads are audited,
the retention period, and whether a run may be cached or shared between users.

| Control | internal | confidential | restricted (payroll, PII, card data) |
|---|---|---|---|
| Email attachment | allowed if small | link only | link only, MFA-backed session |
| Signed URL TTL | ≤ 1 h | ≤ 15 min | ≤ 5 min or view-only streaming |
| Download audit | optional | required | required + alert on bulk |
| Cross-user artifact reuse | allowed within same scope | same user only | never |
| Retention | per spec | per spec | minimum necessary, legal-hold aware |

The values are illustrative defaults to adapt, not regulatory requirements.

## 3. Internal mechanics

### Where tenant and user scope come from

The tenant is resolved **server-side**: from the authenticated session, subdomain middleware or
API token. It is passed as a keyword argument and is never a report parameter. `request_report()`
([reporting-architecture-and-design.md](reporting-architecture-and-design.md)) checks the spec
permission, then binds the params form with `tenant=` and `user=`, so its cleaned data never
contains a tenant id.

The params form validates *related ids against the tenant*. A `customer` filter must be a
`ModelChoiceField(queryset=Customer.objects.filter(tenant=tenant))`, so a foreign tenant's
customer id is a validation error. It is never a silently empty report or, worse, a populated one.

### Generation-time re-check

```python
# reports/tasks.py (excerpt — full task in background-report-generation.md)
spec = get_spec(run.report_key)
user = run.requested_by
if user is not None and (not user.is_active or not user.has_perm(spec.permission)):
    mark_failed(run, code="permission_revoked")      # do not render
    return
rows = spec.selector(tenant=run.tenant, user=user, params=typed_params)
```

Scheduled runs (`requested_by=None`) execute as the schedule **owner**. Re-check the owner's
permission and fail closed if the owner has left the organisation. Never execute as a superuser
"system" identity that bypasses row scoping.

### Download-time check

```python
from django.db.models import Q
from django.shortcuts import get_object_or_404

def visible_runs(*, user, tenant):
    qs = ReportRun.objects.filter(tenant=tenant)
    if user.has_perm("reports.view_all_tenant_reports"):
        return qs
    return qs.filter(Q(requested_by=user) | Q(schedule__recipients__user=user)).distinct()

run = get_object_or_404(visible_runs(user=request.user, tenant=request.tenant), pk=run_id)
```

(`recipients` is the `related_name` of `ReportRecipient.schedule` —
[scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md).)
`get_object_or_404` on the **scoped** queryset returns 404 both when the run does not exist and
when it is not yours. That avoids confirming existence. See
[django-security-engineering — authorization-and-access-control](../../django-security-engineering/references/authorization-and-access-control.md).

### Signed URLs

A signed URL is a **bearer capability**. Whoever holds it can download until it expires, without
authentication. Choose its properties deliberately:

| Property | Requirement |
|---|---|
| Scope | Exactly one artifact (one storage object), never a prefix |
| Lifetime | Short (minutes); issued on demand by an authorised view, not stored in emails for days |
| Binding | Signature covers the artifact id and expiry; for app-signed links also the user id, re-checked on redemption |
| Issuance | Only after the download check; every issuance audited |
| Headers | Forces `Content-Disposition: attachment` and the correct `Content-Type` |

Provider presigned URLs (S3, Azure SAS) are `[external service]` features, exposed through
`[third-party: django-storages]`. They cannot be bound to a user: anyone holding the URL can use it
until expiry. App-signed tokens made with `django.core.signing` `[Django]` can be bound to a user
and re-checked on redemption. The cost is that bytes stream through Django (or an
`X-Accel-Redirect` hop). Implementation: [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md).

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Choose |
|---|---|---|---|
| Download mechanism | Authenticated view streaming the file (`FileResponse`) | Short-lived provider presigned URL issued by an authenticated view | A for `restricted` data or low volume; B for large files/high volume with TTL ≤ minutes |
| Email delivery | Attachment | Link to authenticated download page | Link for anything above `internal`; attachments leave your control forever |
| Field masking | Separate report specs per role | One spec, column set chosen by role at generation | One spec + role-driven columns, with tests per role; separate specs when the audiences differ legally |
| Artifact reuse | Share one artifact between users with identical params | Per-user artifacts | Share only if the selector output provably does not depend on user (no row-level scoping) and classification allows |
| Tenant isolation | Row-level `tenant` FK + scoped selectors | PostgreSQL RLS `[PostgreSQL]` in addition | Scoped selectors always; add RLS when a single missed filter is unacceptable (regulated, many developers) |
| Deletion | Hard delete artifact + run metadata | Delete artifact, keep run metadata as audit record | Delete bytes on expiry; keep minimal metadata (who, what, when, hash) for the audit retention period |
| Encryption at rest | Provider-managed keys (SSE) | Application-level encryption of artifacts | Provider SSE by default; app-level only when the threat model includes storage-admin access |

Each choice trades usability or cost against exposure. Record the choice, and the data
classification it applies to, in an ADR
([django-architecture — architecture-decision-records](../../django-architecture/references/architecture-decision-records.md)).

## 5. Implementation patterns

### Pattern: params cannot carry scope

```python
from django import forms

class SalesReportParams(forms.Form):
    start = forms.DateField()
    end = forms.DateField()
    region = forms.ChoiceField(choices=[])          # filled per user
    customer = forms.ModelChoiceField(queryset=Customer.objects.none(), required=False)

    def __init__(self, *args, tenant, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(tenant=tenant)
        self.fields["region"].choices = [(r, r) for r in allowed_regions(user)]

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end"):
            if data["end"] <= data["start"]:
                raise forms.ValidationError("end must be after start")
            if (data["end"] - data["start"]).days > 366:
                raise forms.ValidationError("range too large")   # also a DoS control
        return data
```

The form drops unknown keys. If the client sends `tenant_id`, `user_id` or `is_admin`, they have
no effect.

### Pattern: field-level masking chosen at generation

```python
PAYROLL_COLUMNS = {
    "payroll_admin": ["employee_number", "full_name", "department", "gross", "deductions", "net", "iban"],
    "hr_assistant": ["employee_number", "full_name", "department"],
}

def payroll_columns(user) -> list[str]:
    for role, cols in PAYROLL_COLUMNS.items():
        if user.groups.filter(name=role).exists():
            return cols
    raise PermissionDenied
```

Mask by **not selecting** the column (`values_list(*cols)`), not by blanking it after the query.
The value then never reaches worker memory, logs or error reports.

### Pattern: safe artifact naming

```python
from django.utils.text import get_valid_filename   # [Django]

def storage_name_for(run: ReportRun) -> str:
    # Server-generated, no user input: tenant/uuid path.
    return f"reports/{run.tenant_id}/{run.id}.{run.format}"

def download_filename_for(run: ReportRun, spec) -> str:
    stem = get_valid_filename(f"{spec.key}-{run.data_as_of:%Y%m%d}")
    return f"{stem}.{run.format}"
```

`storage_name` is never derived from user input. That prevents path traversal and overwriting
another tenant's object. The user-friendly name only appears in `Content-Disposition`. See
[django-security-engineering — file-upload-security](../../django-security-engineering/references/file-upload-security.md)
for traversal mechanics.

### Pattern: renderer isolation

- HTML→PDF renderers fetch every `src`/`href` they see. Use a `url_fetcher` that serves only local
  static assets and rejects everything else. The implementation is in
  [pdf-report-generation.md](pdf-report-generation.md); SSRF background is in
  [django-security-engineering — ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md).
- Render templates with Django auto-escaping on. Never apply `|safe` to user-controlled text in PDF
  templates ([django-security-engineering — cross-site-scripting](../../django-security-engineering/references/cross-site-scripting.md)).
- Escape user text before passing it to ReportLab `Paragraph` (mini-markup), using
  `xml.sax.saxutils.escape` `[stdlib]`.
- Run renderers in workers that hold no credentials they don't need. Ideally the workers also have
  no network egress except to storage (network policy, platform-level).

### Pattern: spreadsheet formula injection

Every string that originates from a user or partner and is written to CSV/XLSX goes through
`safe_cell()` ([csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)).
With XlsxWriter, also disable `strings_to_formulas` or use `write_string()`
([excel-report-generation.md](excel-report-generation.md)). The risk falls on the *consumer* of the
report: DDE/command execution and data exfiltration via formulas. It therefore applies even to
"internal" reports.

### Pattern: temporary files

```python
import tempfile   # [stdlib]
from django.core.files import File

with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, dir=settings.REPORT_TMP_DIR) as tmp:
    renderer.render(document, tmp)
    tmp.seek(0)
    report_storage.save(storage_name_for(run), File(tmp))
# file removed on close, including on exception
```

`REPORT_TMP_DIR` should be per-worker (container-local, not a shared volume), mode 0700, on
encrypted disk where required, and swept on worker start to remove files left by crashes. Never
write to a predictable path like `/tmp/report.xlsx`.

### Pattern: logs, errors and notifications without data

- Log run id, report key, tenant id, row count, size and duration. Never log parameters that are
  PII (employee names, emails), and never log row values.
- `error_message` stores user-safe text. Stack traces go to error tracking with PII scrubbing
  ([django-core — logging-exceptions](../../django-core/references/logging-exceptions.md)).
- Notification emails contain the report title, period and a link to the authenticated page. They
  do not contain totals, names, or (for `restricted` reports) the signed URL itself.
- Never log full signed URLs: the signature is the credential. Strip query strings from access logs
  for the download path.

### Pattern: audit records

The report audit trail has three parts: `ReportRun` (who requested what, with which params, when,
data as-of time), `ReportDownload` (who downloaded which artifact, when, from where, via the view or
a signed URL), and `ReportDelivery` records. Requirements:

- Append-only from the application's perspective: no update or delete API, and a restricted DB role
  if required.
- Retained per the audit retention policy, which is usually longer than artifact retention.
- Includes failed and denied attempts (403/404 on download, permission-revoked generation). These
  are the security signal.

General audit design: [django-security-engineering — security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md).

### Pattern: retention, deletion and legal hold

The canonical `ReportArtifact` already has `expires_at` and `deleted_at`. Where legal holds exist,
add an extension field such as `legal_hold = models.BooleanField(default=False)`, set only by
compliance tooling.

- Set `expires_at = created_at + spec.retention` when the artifact is created.
- The cleanup task deletes the bytes of expired artifacts that are not on legal hold, sets
  `deleted_at`, and marks the run `EXPIRED`. Downloads of expired artifacts consistently return
  either 410 or 404.
- Storage-level lifecycle rules (`[external service]`) act as a backstop. Their TTL must be longer
  than the application's, never shorter.
- Deletion must reach every copy: object storage (including versioned objects and the provider's
  soft-delete features), temp dirs and caches. Email attachments already sent cannot be recalled,
  which is another reason to send links.
- Data-subject erasure requests: decide whether generated reports containing the subject are
  deleted, regenerated or retained under another legal basis. That is a legal decision; record it.

## 6. Production use cases

| Scenario | Primary threats | Key controls |
|---|---|---|
| Monthly payroll register | Salary disclosure to wrong employees, forwarded links, leftover temp files | Report-level permission `payroll_admin`, re-check at generation, link-only delivery, 5-min TTL, download audit, restricted classification |
| Customer invoice PDFs in a SaaS portal | IDOR on invoice/run id, cross-tenant access | Scoped `visible_runs`, 404 on miss, storage path by tenant/uuid, tests per tenant |
| Sales export by regional managers | Over-broad rows, CSV formula injection from customer names | Row-level selector by region, `safe_cell()`, audit of bulk exports |
| Financial statement for auditors (external) | External recipients, long-lived access | Named external recipients approved per schedule, expiring links, watermark with recipient + timestamp, delivery audit |
| Regulatory submission file | Integrity and provenance | SHA-256 recorded on artifact, immutable storage, report version + data_as_of in metadata |
| Self-service "export everything" | Data exfiltration by insider | Size caps, rate limits per user, alert on volume anomalies, approval workflow for restricted datasets |

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Three-checkpoint authorisation | Closes revocation gap; downloader checked independently | More code paths to test; scheduled runs need an owner identity |
| Scoped selectors as single choke point | Same rules for screen, API, export and task | One missing scope = leak; mitigate with tests and optionally RLS |
| Provider presigned URLs | Bytes bypass app servers; scales | Bearer token; cannot revoke before expiry; leaks via logs/referrers |
| Streaming through an authenticated view | Per-request authz, revocable, auditable per download | Ties up app workers; needs `X-Accel-Redirect` for scale |
| Link-only email delivery | Revocable, auditable, no data at rest in mailboxes | Extra click; recipients need accounts or a secure external flow |
| Short retention | Smaller breach blast radius, lower cost | Users must regenerate; reproducibility depends on historical data |

## 8. Security considerations

### Threat model

| # | Threat | Example | Control | Test |
|---|---|---|---|---|
| T1 | IDOR on run id | `GET /reports/runs/<uuid>/download` with another user's UUID | Scoped `visible_runs` + `get_object_or_404`; UUIDs are not authorisation | Other user / other tenant → 404 |
| T2 | IDOR on artifact or storage name | Download view takes `?file=reports/7/…` | Never accept storage names from clients; resolve via run | Parameter ignored / 404 |
| T3 | Cross-tenant via params | `customer=<id from tenant B>`, `tenant_id=B` in body | Tenant from request context; related-id querysets scoped | Validation error; no rows from B |
| T4 | Privilege drift | Role revoked after request, before generation; scheduled report for departed owner | Re-check at generation; fail closed | Revoke then run task → failed with `permission_revoked` |
| T5 | Signed URL leakage | URL in logs, browser history, `Referer`, forwarded email, chat previews | Short TTL, one object, `Referrer-Policy`, no URL in logs, link to authenticated page instead | URL expires; access log redaction verified |
| T6 | SSRF via HTML→PDF | User-supplied `<img src="http://169.254.169.254/...">` or CSS `@import` | Restrictive `url_fetcher`, escaped templates, no egress from renderers | Malicious URL in field → no outbound request, render succeeds or fails safely |
| T7 | Formula injection | Customer name `=HYPERLINK(...)` in CSV/XLSX | `safe_cell()`, typed numeric cells, `write_string` | Exported cell starts with `'` / is a string |
| T8 | Path traversal / unsafe filenames | Filename param `../../settings.py` or header injection via `\r\n` in filename | Server-generated storage names; `get_valid_filename`; `FileResponse(filename=)` encodes header | Traversal input → sanitised name, same storage path |
| T9 | Cached report leakage | Shared cache key without user/tenant; CDN caching downloads; browser cache on shared PCs | Cache key includes tenant + scope; `Cache-Control: private, no-store`; no CDN on download paths | Response headers asserted |
| T10 | Temp file exposure | Files left on shared volume after crash | Per-worker temp dir, `SpooledTemporaryFile`/context managers, startup sweep | Inject failure mid-render → no file remains |
| T11 | PII in logs/emails/errors | Params with employee names logged; totals in notification email | Log ids not values; email contains link only; error-tracker scrubbing | Log capture test asserts no PII |
| T12 | DoS via expensive reports | Unbounded date range, 50 concurrent exports | Param bounds, per-user/tenant concurrency caps, throttles | Range > max rejected; cap returns 429 |
| T13 | Tampering with artifacts | Overwriting an artifact in storage | Write-once naming (uuid), hash recorded, least-privilege storage credentials | SHA-256 verified on download for regulated reports |
| T14 | Enumeration via errors | 403 vs 404 differences, timing | Uniform 404 for not-visible | Same status/body for missing and foreign |

Further reading: [django-security-engineering — threat-modeling](../../django-security-engineering/references/threat-modeling.md),
[django-security-engineering — multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md),
[django-security-engineering — api-security](../../django-security-engineering/references/api-security.md).

### Encryption

- **In transit:** HTTPS for all download and API traffic (`SECURE_SSL_REDIRECT`, HSTS — see
  [django-security-engineering — secure-django-configuration](../../django-security-engineering/references/secure-django-configuration.md));
  TLS to object storage, the database and the SMTP relay; SFTP rather than FTP for file drops.
- **At rest:** object storage server-side encryption is an `[external service]` feature (S3 SSE,
  Azure Storage Service Encryption), enabled on the bucket or account, not by Django. Database
  encryption at rest is platform-level ([postgresql-engineering](../../postgresql-engineering/SKILL.md)).
  Application-level encryption of artifacts (e.g. `[third-party: cryptography]` Fernet) protects
  against storage-admin access; it adds key management and prevents provider-side direct download
  of plaintext.
- **PDF passwords and permissions** are not an access-control mechanism. Permission flags are
  advisory and only compliant readers honour them. User-password encryption just moves the problem
  to password distribution. Use them only when a counterparty requires them
  ([pdf-report-generation.md](pdf-report-generation.md)).

### Technical controls vs legal compliance

Engineering provides **controls**. Whether they satisfy a regulation is decided by legal and
compliance owners, for the specific jurisdiction, contract and data. Never write "this makes the
report GDPR/SOX/PCI compliant" in code, docs or PRs.

| Regime (examples) | Typical reporting concern | Technical controls that *support* it | Needs legal/compliance decision |
|---|---|---|---|
| GDPR / UK GDPR (personal data) | Minimisation, purpose limitation, erasure, access requests, transfers | Field-level masking, retention-driven deletion, audit of exports, data residency of storage | Lawful basis, retention periods, whether generated reports are in scope of erasure [VERIFY: with DPO for the deployment's jurisdiction] |
| SOX (US public-company financial reporting) | Integrity and reproducibility of financial reports, change control | Report versioning, data_as_of, immutable artifacts with hashes, access audit, segregation of duties on report definitions | Which reports are in scope of ICFR controls [VERIFY: with internal audit] |
| PCI DSS (cardholder data) | Card numbers must not appear in reports in clear | Never select PAN; masked/truncated values only; reports excluded from cardholder data environment | Scope of CDE and truncation rules [VERIFY: against the current PCI DSS version] |
| HIPAA (US health data) | PHI disclosure, minimum necessary, access logs | Role-based column selection, audit, encryption, BAAs with storage/email providers | Covered-entity status, BAA requirements [VERIFY: with compliance] |
| Payroll / tax / labour law | Payslip and payroll register content, retention, employee access | Versioned templates, reproducible runs, long-term immutable archive, per-employee access | Mandatory content and retention durations per country [VERIFY: local legislation] |

Retention durations in particular vary by jurisdiction and record type. Encode them as
configuration (`ReportSpec.retention`) owned by compliance, not as constants chosen by engineers.

## 9. Performance considerations

- Authorisation checks should be **set-based**: scope the queryset once rather than checking each
  row in Python. A per-row `has_perm` call over 1M rows is slow, and it signals that the row-level
  rules are not in the selector.
- RLS `[PostgreSQL]` adds per-row predicate evaluation. With an indexed `tenant_id` this is usually
  negligible, but measure it on large reports
  ([django-performance — database-performance](../../django-performance/references/database-performance.md)).
- Streaming downloads through Django costs worker time. `X-Accel-Redirect` keeps the authorisation
  check in Django and moves the byte transfer to nginx.
- Audit writes are one insert per download, which is cheap. Write them synchronously so they can't
  be lost, or through a durable queue if download volume is very high.
- Short TTLs mean more link-issuance requests. Issuance is cheap (one signature), so prefer short TTLs.
- Param bounds (max range, max rows) are both a security control and a capacity control. See
  [report-generation-performance.md](report-generation-performance.md).

## 10. Common mistakes and anti-patterns

- Treating an unguessable UUID as access control ("nobody can guess it"). URLs leak.
- Accepting `tenant_id` or `user_id` as a report parameter, or deriving scope from a hidden form field.
- Checking permission only in the view that *requests* the report, then generating as a privileged
  system user in the task.
- A download view that does an unscoped `ReportRun.objects.get(pk=...)` and then
  `if run.requested_by != user: raise PermissionDenied`. It returns 403 for foreign runs and 404 for
  missing ones, which allows enumeration, and the check is easy to forget on new endpoints.
- Public-read buckets, or long-lived (days/weeks) presigned URLs in scheduled emails.
- Sending `restricted` reports as email attachments.
- Logging full presigned URLs, or report parameters that contain names/emails.
- Sharing a cached artifact between users when the selector applies row-level scoping.
- `storage.save(user_supplied_filename, ...)`.
- HTML→PDF with the default URL fetcher and user-controlled HTML/URLs.
- Masking columns by overwriting values after loading them. The data is still in memory, logs and
  crash dumps.
- No deletion path for temp files, or for artifacts referenced by failed runs.
- Claiming compliance from technical controls alone.

## 11. Troubleshooting and validation

### Security validation checklist

- [ ] Request, generation and download each perform an authorisation check; tests cover revocation between request and generation.
- [ ] Tenant is taken from request context; params forms scope every related-id field to the tenant.
- [ ] `visible_runs` (or equivalent) is the only way views and APIs look up runs/artifacts; missing and foreign return identical 404s.
- [ ] Storage names are server-generated (`reports/<tenant_id>/<uuid>.<ext>`); user input only appears in a sanitised `Content-Disposition` filename.
- [ ] Signed URLs: single object, TTL by classification, issued only after authz, never logged; downloads force `attachment`.
- [ ] Downloads send `Cache-Control: private, no-store`; no CDN or shared cache on download paths.
- [ ] HTML→PDF uses an allow-list fetcher; templates auto-escape; ReportLab text escaped.
- [ ] CSV/XLSX strings pass through `safe_cell()`; XlsxWriter does not auto-convert strings to formulas.
- [ ] Temp files are context-managed in a private per-worker dir and swept on start.
- [ ] Logs, errors and notifications contain ids and codes, not row data or PII.
- [ ] Every run and download (including denials) is audited; audit retention ≥ artifact retention.
- [ ] Retention per classification, legal hold respected, cleanup task monitored; storage lifecycle backstop configured.
- [ ] Encryption in transit everywhere; at-rest encryption enabled at provider level and documented.
- [ ] Compliance requirements documented by their owners; `[VERIFY: ...]` items resolved.

### Diagnosing incidents

| Symptom | Likely cause | Check |
|---|---|---|
| User reports seeing another tenant's data in an export | Unscoped selector path (e.g. a new filter that joins without tenant) or shared cache key | Diff the selector SQL (`str(qs.query)`), cache key composition, run params |
| Download links work for people who were never recipients | Links forwarded; presigned TTL too long | TTL config, audit of downloads by IP/user, move to authenticated page links |
| Outbound requests from report workers to internal IPs | SSRF via PDF renderer | Fetcher configuration, template escaping, egress logs |
| Artifacts present in storage after expiry | Cleanup task failing or legal hold misapplied | Cleanup metrics ([reporting-observability-and-operations.md](reporting-observability-and-operations.md)) |
| 403 for some, 404 for others on the same foreign run | Inconsistent lookup paths | Route all lookups through the scoped selector |

Security test patterns: [report-generation-testing.md](report-generation-testing.md) and
[django-security-engineering — security-testing-and-incident-response](../../django-security-engineering/references/security-testing-and-incident-response.md).

## 12. Related references and official documentation

- In this skill: [reporting-architecture-and-design.md](reporting-architecture-and-design.md) ·
  [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) ·
  [background-report-generation.md](background-report-generation.md) ·
  [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) ·
  [pdf-report-generation.md](pdf-report-generation.md) ·
  [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) ·
  [excel-report-generation.md](excel-report-generation.md) ·
  [report-generation-testing.md](report-generation-testing.md) ·
  [reporting-observability-and-operations.md](reporting-observability-and-operations.md)
- django-security-engineering: [authorization-and-access-control](../../django-security-engineering/references/authorization-and-access-control.md) ·
  [multi-tenant-security](../../django-security-engineering/references/multi-tenant-security.md) ·
  [ssrf-prevention](../../django-security-engineering/references/ssrf-prevention.md) ·
  [file-upload-security](../../django-security-engineering/references/file-upload-security.md) ·
  [secrets-and-cryptography](../../django-security-engineering/references/secrets-and-cryptography.md) ·
  [security-logging-and-auditing](../../django-security-engineering/references/security-logging-and-auditing.md) ·
  [threat-modeling](../../django-security-engineering/references/threat-modeling.md) ·
  [injection-prevention](../../django-security-engineering/references/injection-prevention.md)
- Other skills: [django-data-processing — data-processing-security](../../django-data-processing/references/data-processing-security.md) ·
  [django-integrations — file-transfer-and-object-storage](../../django-integrations/references/file-transfer-and-object-storage.md) ·
  [django-integrations — email-sms-and-notification-integrations](../../django-integrations/references/email-sms-and-notification-integrations.md) ·
  [django-rest-framework — permissions-and-access-control](../../django-rest-framework/references/permissions-and-access-control.md)
- Official documentation:
  <https://docs.djangoproject.com/en/5.2/topics/security/> ·
  <https://docs.djangoproject.com/en/5.2/topics/signing/> ·
  <https://docs.djangoproject.com/en/5.2/ref/request-response/> ·
  <https://docs.python.org/3/library/tempfile.html> ·
  <https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html> ·
  <https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html> ·
  <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html> ·
  <https://owasp.org/www-community/attacks/CSV_Injection> ·
  <https://doc.courtbouillon.org/weasyprint/stable/>
