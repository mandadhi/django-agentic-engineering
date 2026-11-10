# Report Generation Troubleshooting

> **Scope:** A symptom → likely cause → diagnosis → fix catalogue for reporting failures in Django systems: wrong numbers, date and rounding errors, memory and timeout failures, stuck or duplicated runs, broken CSV/XLSX/PDF output, chart problems, download failures, and scheduling and delivery faults. **Not here:** instrumentation, dashboards, alerts and operational runbooks ([reporting-observability-and-operations.md](reporting-observability-and-operations.md)); the design patterns that prevent these faults live in the owning references linked from each entry.

## 1. Purpose and scope

When a report is wrong, late, missing or unreadable, an engineer needs to go from the symptom to
the cause quickly and fix it at the right layer. This reference is a catalogue, organised by
symptom. Each entry gives likely causes ranked by frequency, a concrete diagnostic step, the fix,
and the reference that owns the correct design. It assumes the canonical `reports` models
(`ReportRun`, `ReportArtifact`, `ReportSchedule`, `ReportDelivery`, `ReportDownload`) and the
`billing`/`sales`/`payroll` domain used throughout this skill.

## 2. Core concepts

- **Classify first.** Every reporting fault falls into one of five classes: *data correctness*
  (numbers), *execution* (never finishes, runs twice, OOM), *format* (file opens wrong),
  *access* (cannot download), *distribution* (not delivered / wrong time). The class tells you
  where to look.
- **Reproduce with the run's own inputs.** `ReportRun` stores `report_key`, `report_version`,
  `params`, `data_as_of` and `tenant`. Re-run the selector with exactly those inputs in a shell or
  test before changing anything.
- **Compare against an independent computation.** For wrong totals, compute the expected value
  with a different query path (raw SQL or a simpler ORM query) — never with the same selector.
- **Check the boring layers.** Many "rendering bugs" are encoding, time zone or Excel locale
  behaviour, not library defects.
- **Fix the cause and add the regression test** in the reference that owns the behaviour
  ([report-generation-testing.md](report-generation-testing.md)).

## 3. Internal mechanics

Where faults are introduced along the pipeline:

```text
params ──► validation ──► selector/query ──► transform ──► render ──► store ──► deliver/download
  │            │               │                 │            │          │            │
  │      wrong defaults   fan-out, status   tz, rounding,  encoding,   storage   permissions,
  │      inclusive end    filter, missing   nulls, float   fonts, CSS, perms,    expiry, email
  │                       tenant scope                     injection   size      provider, DST
  ▼
 execution layer: on_commit enqueue, claim, retries, visibility timeout, time limits, reaper
```

Diagnose left to right: if the selector output is already wrong, rendering is irrelevant.

## 4. Engineering decisions and trade-offs

| Situation | Quick mitigation | Proper fix | Trade-off of the quick path |
|---|---|---|---|
| Wrong totals shipped to customers | Disable the report / mark artifacts superseded | Fix query, bump `report_version`, regenerate affected periods | Users lose access temporarily |
| OOM in worker | Raise memory limit | Stream/iterate, write-only mode, smaller chunks | Hides growth until next data increase |
| Export times out in request | Raise Gunicorn/proxy timeout | Move to async generation | Ties up workers; other users suffer |
| Duplicates from redelivery | Raise visibility timeout | Idempotent claim + unique constraint | Long timeouts delay recovery after real crashes |
| Excel shows formulas as 0 | Tell users to press F9 | Write values, or provide cached values | Support burden remains |
| Schedule wrong after DST | Manually adjust times | Store local time + IANA zone, compute next run in zone | Recurs every DST change |

When you regenerate a wrong historical report, keep the original artifact and its audit rows
(superseded, not deleted) if the report was ever delivered — recipients acted on it.

## 5. Implementation patterns

### 5.1 Data correctness

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| Totals too high | Join fan-out: aggregating `Invoice.total` while joining `InvoiceLine` or `Payment` multiplies rows | `print(qs.query)`; check for two multi-valued joins; compare `Count("id")` vs `Count("id", distinct=True)` | Aggregate each relation in its own `Subquery`, or aggregate at line level. See [django-orm — aggregation-and-annotation](../../django-orm/references/aggregation-and-annotation.md) |
| Totals too low / missing rows | Status filter excludes `issued` (only `paid`); soft-deleted/void handling; inner join drops rows with null FK | Count rows per status for the period; compare `filter(customer__isnull=True)` | Make status semantics explicit in the `ReportSpec`; use `LEFT JOIN` semantics (`Coalesce`, filter on the base model) |
| Totals include another tenant's data | Selector missing tenant scope; subquery not scoped | Inspect SQL for `tenant_id` in every subquery | Scope via the tenant-aware manager in every selector; add a cross-tenant test; treat as a security incident |
| Off by one day | Naive date boundaries in UTC instead of tenant time zone; `__date` lookup evaluated in the current time zone | Re-run with explicit `ZoneInfo(tenant.timezone)` boundaries; inspect `issued_at` values near midnight | Build half-open `[start, end)` aware datetimes in the tenant zone; `Trunc*(..., tzinfo=)` |
| Last day of month missing | Inclusive end `issued_at__lte=date(2026, 10, 31)` compares to midnight at start of the 31st | Check rows on the last day | `issued_at__lt=first_day_of_next_month` |
| Month totals shift around DST | Fixed UTC offsets instead of IANA zone | Compare periods spanning DST changes | Use `zoneinfo.ZoneInfo` `[stdlib]` for boundaries |
| Rounding mismatch (cents) | `FloatField`/float arithmetic; rounding each line vs rounding the total; `ROUND_HALF_EVEN` default vs business `ROUND_HALF_UP` | Recompute with `Decimal`; diff line-rounded vs total-rounded | `Decimal` end-to-end; document where rounding happens and with which mode; quantize once at presentation unless the business rule says per line |
| Percentages don't add up to 100 | Independent rounding of shares | Sum of rounded values | Largest-remainder allocation, or show a footnote; don't silently force |
| Screen and export differ | Different selectors; export uses pre-aggregated data with different `data_as_of`; screen applies default filters export doesn't | Diff the parameter sets; check `data_as_of` | One selector shared by screen and export; show `data_as_of` on both |
| Re-running last month's report gives different numbers | Late-arriving or edited data; current FX rates/prices joined instead of historical ones | Compare `data_as_of`; look for updates after period close | As-of queries on effective-dated data, snapshot tables for closed periods, version the report. See [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) |
| `None` instead of 0 in empty periods | `Sum` without `default=` (Django ≥ 4.0) | Check periods with no rows | `Sum("total", default=Decimal("0"))` and fill missing periods in Python |

### 5.2 Execution

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| Worker killed (OOM, exit 137 / SIGKILL) | `list(qs)` of all rows; openpyxl normal mode; whole PDF/HTML string in memory; pandas DataFrame of full dataset; `prefetch_related` on huge sets | Peak RSS by `report_key`; `tracemalloc` snapshot in staging with production-sized data | `.iterator(chunk_size=…)`/`values_list`; `Workbook(write_only=True)` or XlsxWriter `constant_memory`; stream CSV to a temp file; split huge PDFs. See [report-generation-performance.md](report-generation-performance.md) |
| 502/504 or connection reset during export in request | Gunicorn worker timeout (default 30 s [VERIFY: Gunicorn default --timeout]); proxy read timeout; synchronous Excel/PDF build | Proxy and app-server logs show worker timeout/abort | Async generation for anything not reliably fast; for CSV, `StreamingHttpResponse` plus `X-Accel-Buffering: no` and adequate proxy timeouts |
| Streaming CSV arrives all at once / still times out | Proxy buffering; GZip middleware buffering; worker class not suited to long streams | Inspect response timing with `curl -N` `[external tool]` | Disable buffering for that location; exclude from compression; async generation for very large exports |
| Run stays `pending` forever | Task enqueued inside the transaction and executed before commit (row not visible → task exits), or enqueue never happened because the transaction rolled back after `.delay()`; broker unavailable at enqueue; wrong queue with no consumers | Look for `report.requested` without `report.started`; check broker for the message; check the task's routing | Enqueue via `transaction.on_commit`; dispatcher sweep re-enqueues old `pending` runs; ensure a worker consumes the route. See [background-report-generation.md](background-report-generation.md) |
| Run stays `running` forever | Worker crashed / OOM / deploy restart without a reaper; hard `time_limit` kill skipped the `except` block | `heartbeat_at` stale; worker logs for the task id | Heartbeat + reaper that requeues or fails stale runs; `acks_late` with `reject_on_worker_lost` where safe |
| Report generated twice / duplicate artifacts | Redis broker `visibility_timeout` shorter than task duration → redelivery (default 1 h [VERIFY: Redis transport visibility_timeout default]); user double-click; retry after timeout of an ambiguous step | Two `report.started` events with the same `run_id` or two runs with the same `params_hash` | Atomic claim (`filter(status="pending").update(status="running")`), partial unique constraint `uniq_active_report_run`, visibility timeout > max task duration, deterministic storage name per run |
| Retries never stop / retry storm | `autoretry_for=(Exception,)` including permanent errors; no `max_retries` | Retry counts by `error_code` | Retry only transient errors; classify `row_limit_exceeded`, validation, permission errors as terminal |
| Cancelled report keeps running | Relying on `revoke()`; no cooperative check | Run continues after `cancel_requested=True` | Check `cancel_requested` between chunks; stop and mark `cancelled` |
| `ValueError` using `iterator()` with prefetch | `prefetch_related` + `iterator()` without `chunk_size` (Django ≥ 5.0 [VERIFY: exact Django version where this became an error]) | Traceback | Pass `chunk_size` |
| Server-side cursor errors behind PgBouncer | Transaction-pooling mode breaks named cursors | Error mentions cursor does not exist | `DISABLE_SERVER_SIDE_CURSORS = True` for that alias, or session pooling for the reporting alias |
| `canceling statement due to statement timeout` | Reporting query exceeds `statement_timeout` | Query plan with `EXPLAIN (ANALYZE, BUFFERS)` on a replica | Index for filter/group columns, pre-aggregate, use a reporting alias with a longer timeout. See [postgresql-engineering](../../postgresql-engineering/SKILL.md) |

### 5.3 CSV and structured output

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| Accented characters garbled when opened in Excel | UTF-8 without BOM; Excel guesses a legacy code page | Open in a text editor — the bytes are correct | Write `utf-8-sig` (BOM) for Excel-targeted exports; keep plain UTF-8 for machine consumers. See [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md) |
| All data in column A | Excel locale uses `;` as list separator (many European locales) | Same file splits correctly in another locale | Offer XLSX for spreadsheet users; or a `;` dialect option per tenant locale; don't guess per request |
| Blank lines between rows | File opened without `newline=""` on Windows, producing `\r\r\n` | Hex view shows `0D 0D 0A` | Open files with `newline=""`; the csv module writes `\r\n` itself |
| Leading zeros lost (`00123` → `123`), long IDs shown as `1.23E+15` | Excel type inference on CSV | Text editor shows correct values | Use XLSX with text-formatted cells; for CSV there is no reliable fix — document it |
| Cell shows a computed value or warning instead of text | Formula injection: value starts with `=`, `+`, `-`, `@`, tab, CR | Inspect raw value | Apply `reports.formatting.safe_cell()` to every untrusted string |
| Decimal values like `1234.5000000001` or `1,234.50` mismatch | Float conversion; locale formatting in machine exports | Inspect serializer | Serialize `Decimal` with `str()`/fixed `quantize`; machine formats use `.` and no thousands separator |
| JSON output differs between runs with same data | Dict ordering, float formatting, timestamps in payload | Diff two outputs | `sort_keys=True`, stable `separators`, `DjangoJSONEncoder` for Decimal/datetime, `data_as_of` instead of `now()` |

### 5.4 Excel

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| "We found a problem with some content…" repair prompt | Invalid XML characters (control chars) in strings; sheet name > 31 chars or containing `[]:*?/\`; overlapping merged ranges; invalid defined names or table refs; formula syntax errors | Unzip the `.xlsx` and inspect the referenced part; bisect by removing features | Strip illegal control characters; sanitize sheet names; validate merges; write formulas in English syntax with commas |
| Formulas show 0 or blank in previews/other apps | XlsxWriter/openpyxl write formulas without cached results; non-Excel viewers don't recalculate [VERIFY: XlsxWriter stores 0 as the default formula result unless value= is given] | Open in Excel (recalculates) vs viewer | Write computed values for reports (formulas only where users need to edit); or supply the computed value alongside the formula |
| Dates shifted by hours or wrong day | Stripping tz without converting first; openpyxl rejecting aware datetimes [VERIFY: openpyxl behaviour for timezone-aware datetimes] | Compare a known timestamp | `timezone.localtime(value, ZoneInfo(tenant.timezone)).replace(tzinfo=None)`, and state the zone in the header |
| Numbers stored as text (green triangle) | Writing `str(decimal)` | Cell type inspection with openpyxl | Write `Decimal`/numeric types, set `number_format` |
| Workbook huge / slow to open | Per-cell styles, many unique formats, conditional formatting on whole columns | Unzip and check `styles.xml` size | Reuse named/format objects; limit ranges |
| Row limit error or truncated data | > 1,048,576 rows per sheet | Row count | Split across sheets or deliver CSV |

See [excel-report-generation.md](excel-report-generation.md).

### 5.5 PDF

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| Missing glyphs / black boxes / "?" for non-Latin text | ReportLab standard fonts lack Unicode coverage; WeasyPrint container lacks fonts | List fonts in container (`fc-list` `[external tool]`); check font registration | Register a TTF (ReportLab `TTFont`); install/bundle fonts in the image and reference them via `@font-face` with local files |
| Images or CSS missing in WeasyPrint output | Missing or wrong `base_url`; restrictive `url_fetcher` blocks the path; relative `STATIC_URL` not resolvable in a worker | Enable WeasyPrint logging; log fetcher denials | Pass a `base_url` pointing at local static files; allow-list only those paths in the fetcher |
| Layout broken / columns overlap | CSS unsupported by the engine (xhtml2pdf supports a CSS 2.1 subset; WeasyPrint has no JavaScript and partial support for some modern layout features [VERIFY: WeasyPrint flex/grid support in the installed version]) | Render a minimal template with the failing rule | Use table/block layout and paged-media CSS the engine supports; choose ReportLab for precise layout |
| Table header not repeated / rows split across pages | Missing `thead` (WeasyPrint) or `repeatRows` (ReportLab) | Inspect page 2 | `thead { display: table-header-group }`; `Table(..., repeatRows=1)`; `tr { break-inside: avoid }` |
| "Page X of Y" wrong or missing in ReportLab | Single-pass build cannot know total pages | — | Two-pass build or a canvas subclass that counts pages before saving |
| PDF generation suddenly fails after deploy | System library change (Pango/cairo) or WeasyPrint major version API change | Error at import or render | Pin library and base-image versions; render smoke test in CI |
| PDF takes minutes | Thousands of table rows in HTML; large images; per-page font subsetting | Profile render vs query | Paginate/summarise; pre-scale images; async generation. See [pdf-report-generation.md](pdf-report-generation.md) |

### 5.6 Charts

| Symptom | Likely causes | Fix |
|---|---|---|
| Blank chart image | `savefig` after `plt.close`; buffer not rewound (`buf.seek(0)`); empty data after filtering | Save before closing; seek; show "no data" state explicitly |
| `TclError` / display errors on server | Interactive backend selected | Use Agg backend or the `matplotlib.figure.Figure` OO API |
| Memory grows per chart | pyplot figures never closed | Use `Figure` directly or `plt.close(fig)` |
| Wrong values / labels misaligned | Missing periods not filled; unsorted categories; dates plotted in UTC | Fill periods, sort, convert to tenant zone before plotting |
| Interactive Plotly chart empty in email/PDF | No JavaScript in email clients or WeasyPrint | Static image export for those channels. See [charts-and-data-visualization.md](charts-and-data-visualization.md) |

### 5.7 Access and downloads

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| 404 for the requester's own report | Selector scoped by `requested_by` but the user now has a different tenant/role; artifact expired and deleted; run not `succeeded` | Check run status, `expires_at`, `deleted_at`, the user's tenant | Show status and expiry in UI; offer regenerate. 404 is correct for runs outside the user's scope |
| 403 after permissions changed | Download re-checks permission (correct) | Audit role change | Expected behaviour; communicate |
| Signed URL "expired" / `SignatureDoesNotMatch` | Short expiry and user clicked later; clock skew; URL altered by an email client | Compare issue time vs click time | Links in emails point to an authenticated app view that issues a fresh short-lived URL |
| Filename garbled or download opens inline | Non-ASCII filename without `filename*`; wrong `Content-Type` | Inspect response headers | `FileResponse(..., as_attachment=True, filename=...)`; correct content type. See [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md) |
| Large download restarts from zero | Django does not serve Range requests [VERIFY: Django FileResponse Range support] | Response lacks `Accept-Ranges` | Serve via object-storage URL or `X-Accel-Redirect` |
| Another user's report retrievable | IDOR: lookup by id without scope; predictable storage names served publicly | Security test with second user/tenant | Scoped selector lookup, private storage — and treat as an incident. See [reporting-security-and-compliance.md](reporting-security-and-compliance.md) |

### 5.8 Scheduling and distribution

| Symptom | Likely causes | Diagnosis | Fix |
|---|---|---|---|
| Report ran twice | Two beat instances; dispatcher not using an atomic claim of `next_run_at`; retry after an ambiguous enqueue | Two runs with the same `schedule_id` and period | Single beat (or DB-backed lock); claim with `UPDATE … WHERE next_run_at = old_value`; unique constraint on (schedule, period) |
| Report didn't run | Beat not running; schedule `is_active=False`; `next_run_at` not advanced after a failure; exception in dispatcher aborting the loop | `report_schedules_overdue`; dispatcher logs | Per-schedule try/except; catch-up logic for missed windows |
| Runs an hour early/late after DST | Schedule stored as UTC time or fixed offset | Compare `next_run_at` before/after the DST change | Store `run_at_local` + IANA `timezone`; compute next run in the zone; define behaviour for non-existent/ambiguous local times |
| Monthly report on 31st skipped in short months | `day_of_month=31` not clamped | — | Clamp to the last day of the month or use "last day" semantics |
| Email not received | Provider bounce/suppression; attachment too large; spam filtering; delivery marked `sent` at enqueue time instead of provider acceptance | `ReportDelivery.status`, provider event logs | Send links, not large attachments; handle bounce webhooks; mark `sent` on provider acceptance. See [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md) |
| Recipient received the same email twice | Delivery retried without idempotency key | Two provider message IDs | Unique `idempotency_key` per (run, recipient, channel) |

## 6. Production use cases

- **Monthly revenue 3 % too high after adding a "paid amount" column** — the selector joined
  `Payment`, fanning out invoices with split payments. Fix: `Subquery` for payments, bump
  `report_version`, regenerate October, mark old artifacts superseded.
- **Payroll register emailed at 05:00 instead of 06:00 in late October** — schedule stored as a UTC
  time. Fix: local time + IANA zone; regression test across the DST boundary.
- **Sales export OOM at quarter end** — `list()` over 2 M `OrderLine` rows into openpyxl normal mode.
  Fix: iterator + write-only workbook; added memory test at 10× data.
- **Finance sees `#NAME?` in XLSX** — formulas written with locale-specific function names and `;`
  separators. Fix: English function names and commas in the file.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Catalogue-driven diagnosis | Fast triage, consistent fixes | Needs upkeep as the system changes |
| Reproducing from `ReportRun` inputs | Deterministic reproduction | Data may have changed since; needs as-of data or snapshots |
| Independent cross-check queries | Catches selector bugs | Extra effort; second query can share the same misconception |
| Quick mitigations (timeouts, memory) | Restore service fast | Mask root causes; must be followed by the proper fix |

## 8. Security considerations

- Reproduction happens with production data: use a read-only replica or a restricted shell, scope
  every query to the affected tenant, and do not copy report files to personal machines or tickets.
- Never paste signed URLs, report contents or PII into issue trackers or chat.
- A cross-tenant or cross-user access symptom is a security incident, not a bug ticket — follow
  [django-security-engineering — security-testing-and-incident-response](../../django-security-engineering/references/security-testing-and-incident-response.md).
- Debug flags that disable the PDF `url_fetcher` allow-list or formula escaping must never reach
  production.

## 9. Performance considerations

- Profile query time and render time separately before optimising; a slow PDF is often a slow query.
- Test fixes at production data volume — a fix that works for 1 000 rows may not for 1 000 000.
- Reproducing heavy reports against the primary database during an incident adds load; use a
  replica or a scaled-down tenant.
- Quick mitigations such as raising time limits reduce worker throughput for everyone; revert them
  after the proper fix.

## 10. Common mistakes and anti-patterns

- Fixing the renderer when the selector output is already wrong.
- Validating a fix only with a fresh small dataset instead of the failing run's parameters.
- Regenerating and overwriting delivered artifacts, destroying evidence of what recipients saw.
- Raising timeouts and memory as the "fix" for unbounded work.
- Telling users to change Excel settings instead of producing a correct XLSX.
- Retrying permanent failures (validation, permission, row limit) automatically.
- Closing a "duplicate email" bug by adding a sleep.
- Changing report semantics without bumping `report_version`.

## 11. Troubleshooting and validation

Generic triage order:

1. Identify the run(s): `ReportRun` by id, tenant, `report_key`, time window.
2. Read status, `error_code`, `attempt`, timestamps, `data_as_of`, `report_version`.
3. Follow `correlation_id` in logs across request, task(s), delivery and download.
4. Classify (data / execution / format / access / distribution) and use the tables above.
5. Reproduce with the stored parameters at the stored `data_as_of`, outside production if possible.
6. Fix at the owning layer; bump `report_version` if output semantics changed.
7. Add a regression test that fails before the fix ([report-generation-testing.md](report-generation-testing.md)).
8. Decide on remediation for already-delivered artifacts (regenerate, notify, supersede).

Validation of the fix:

- Totals reconcile with an independent query for the failing parameters and boundary dates.
- The artifact opens in the target application (Excel, PDF reader) without warnings.
- Memory and duration measured at production-like volume.
- Cross-user and cross-tenant download tests still return 404.

## 12. Related references and official documentation

- [reporting-observability-and-operations.md](reporting-observability-and-operations.md) — signals and runbooks
- [reporting-data-acquisition-and-preparation.md](reporting-data-acquisition-and-preparation.md) — queries, time zones, Decimal
- [background-report-generation.md](background-report-generation.md) — claim, retries, reaper
- [csv-and-structured-report-generation.md](csv-and-structured-report-generation.md)
- [excel-report-generation.md](excel-report-generation.md)
- [pdf-report-generation.md](pdf-report-generation.md)
- [charts-and-data-visualization.md](charts-and-data-visualization.md)
- [django-report-endpoints-and-delivery.md](django-report-endpoints-and-delivery.md)
- [scheduled-reports-and-distribution.md](scheduled-reports-and-distribution.md)
- [report-generation-performance.md](report-generation-performance.md)
- [django-orm — aggregation-and-annotation](../../django-orm/references/aggregation-and-annotation.md)
- [django-orm — query-optimization](../../django-orm/references/query-optimization.md)
- [django-performance — production-performance-troubleshooting](../../django-performance/references/production-performance-troubleshooting.md)
- [django-data-processing — processing-reliability-and-recovery](../../django-data-processing/references/processing-reliability-and-recovery.md)
- [django-testing — test-maintenance-and-troubleshooting](../../django-testing/references/test-maintenance-and-troubleshooting.md)
- <https://docs.djangoproject.com/en/5.2/topics/i18n/timezones/>
- <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#iterator>
- <https://docs.python.org/3/library/csv.html>
- <https://docs.python.org/3/library/decimal.html>
- <https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html>
- <https://openpyxl.readthedocs.io/>
- <https://xlsxwriter.readthedocs.io/working_with_formulas.html>
- <https://doc.courtbouillon.org/weasyprint/stable/>
- <https://owasp.org/www-community/attacks/CSV_Injection>
