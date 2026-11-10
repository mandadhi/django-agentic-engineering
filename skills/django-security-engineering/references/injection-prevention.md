# Injection Prevention

> Scope: SQL injection and its Django-specific edges (`raw()`, `extra()`, `RawSQL`, cursors,
> user-controlled identifiers, field names and lookup keys); OS command and argument injection;
> server-side template injection; HTTP response and email header injection; unsafe
> deserialisation (`pickle`, YAML, `marshal`); XML attacks (XXE, entity expansion); ReDoS;
> `eval`/`exec`; LDAP/NoSQL briefly; secure input-handling principles.
> Not covered here: ORM raw-SQL mechanics in depth ([django-orm raw SQL](../../django-orm/references/raw-sql-and-database-access.md));
> XSS and output encoding ([cross-site-scripting.md](./cross-site-scripting.md)); log injection and
> redaction ([security-logging-and-auditing.md](./security-logging-and-auditing.md)); upload parsing
> ([file-upload-security.md](./file-upload-security.md)); SSRF ([ssrf-prevention.md](./ssrf-prevention.md));
> serializer/form validation mechanics ([django-rest-framework fields-and-validation](../../django-rest-framework/references/fields-and-validation.md),
> [django-core forms-validation](../../django-core/references/forms-validation.md)).

## 1. Purpose and scope

Injection happens when untrusted data crosses into an **interpreter** — SQL engine, shell,
template engine, header parser, deserialiser, XML parser — and is treated as code or structure
instead of data. Django removes the most common case (ORM queries bind values as parameters), but
every interpreter reached by hand-built strings, user-chosen identifiers, or untrusted serialised
objects is developer responsibility.

The rule across all interpreters: **separate code from data with the interpreter's own
parameterisation API; where only identifiers or names vary, map user input through an allow-list.**

## 2. Core concepts

| Interpreter | Safe mechanism | Unsafe pattern |
|---|---|---|
| SQL | ORM, or `params=` / `%s` placeholders | f-strings / `%` formatting into SQL |
| SQL identifiers | Allow-list → constant; `quote_name` only on allow-listed values | `order_by(request.GET["sort"])`, `filter(**request.GET.dict())` |
| Shell | `subprocess.run([...])` argument list | `shell=True`, `os.system`, `os.popen` |
| Templates | Fixed template files with context | `Template(user_string)`, user-chosen template names |
| HTTP/email headers | Django header validation, `EmailMessage` | Raw CR/LF in values via lower-level APIs |
| Deserialisation | JSON, schema-validated | `pickle.loads`, `yaml.load`, `marshal`, `jsonpickle` on untrusted data |
| XML | defusedxml | stdlib/lxml parsers with entity resolution on untrusted XML |
| Python | Literals (`ast.literal_eval`) or a DSL | `eval`, `exec`, `compile` |

**Validation vs encoding**: validation (type, length, format, allow-list) narrows input early;
interpreter-specific parameterisation/encoding at the **sink** is what actually prevents
injection. Validation alone is not a defence against injection; parameterisation alone does not
prevent logic abuse.

## 3. Internal mechanics

**ORM parameterisation**: the SQL compiler emits placeholders and passes values separately to the
DB-API driver (psycopg, mysqlclient, sqlite3). Values never become SQL text. What the compiler does
**not** parameterise:

- **Identifiers** — column/table names, aliases, `order_by()` field references, `values()` names,
  lookup paths in `filter()` keyword names. These come from Python code; if code passes user input
  there, the input controls query structure. Django validates many of these against the model
  (`FieldError` for unknown fields), which blocks most raw injection but still lets callers select
  **any** field or relation, including sensitive ones (`order_by("password")`,
  `filter(user__password__startswith="pbkdf2")` — data-leak oracles).
- **`extra()`**, **`RawSQL`**, **`raw()`**, **`cursor.execute()`** — SQL text supplied by you; only the
  `params` are bound.
- **Kwargs expansion of internal arguments** — historic CVEs exploited user-controlled dictionary
  keys expanded into ORM calls: column aliases in `annotate()`/`aggregate()`/`extra()`
  (CVE-2022-28346), `Trunc`/`Extract` `kind` (CVE-2022-34265), JSONField keys in
  `values()`/`values_list()` (CVE-2024-42005), and the `_connector` argument of `filter()`/`Q`
  (CVE-2025-64459) — `VERIFY` CVE numbers and fixed versions against the Django security archive.
  All are patched in current releases; the lasting lesson is **never `**`-expand user input into ORM
  methods**.

**Response headers**: Django's `HttpResponse` header handling rejects values containing newlines
(raises `BadHeaderError`, a `ValueError` subclass — `VERIFY` exact class in 5.2), blocking HTTP
response splitting via `response["X"] = user_value`. Email: `django.core.mail` raises
`BadHeaderError` for newlines in header values such as subject/recipients — but address *content*
(adding recipients via a comma list) is still your validation problem.

**Sessions and signing**: `django.core.signing.loads()` deserialises JSON by default; the session
framework uses `JSONSerializer` and `PickleSerializer` was removed (Django ≥ 5.0). Custom
serializers passed to `signing.loads(serializer=...)` or cache backends that pickle (Django's cache
framework pickles values) are safe only while their storage is not attacker-writable.

**Template engines** compile template *source* into executable nodes. DTL is sandbox-like (no
arbitrary Python calls with args, no underscore attributes) but exposes the whole context: a
user-authored template can read `{{ settings.SECRET_KEY }}` if a settings object is in context,
iterate querysets, or call zero-argument methods. Jinja2 without a `SandboxedEnvironment` allows
far more (attribute traversal to `__class__`… → code execution).

## 4. Engineering decisions and trade-offs

**Raw SQL**: prefer ORM expressions (`F`, `Q`, `Func`, `Subquery`, `Window`) — they keep
parameterisation and model validation. Use raw SQL for genuine ORM gaps, always parameterised; see
[django-orm raw SQL](../../django-orm/references/raw-sql-and-database-access.md) for when it is justified.

**Dynamic sorting/filtering**: map a public vocabulary to internal expressions. This costs a few
lines but prevents both injection and data-oracle leaks, and decouples the API contract from model
field names. DRF `OrderingFilter.ordering_fields` and django-filter (third-party) `FilterSet`s are
allow-lists — use them, never `"__all__"` (`OrderingFilter` with `ordering_fields="__all__"` exposes
every model field including sensitive ones).

**Shelling out vs libraries**: prefer a Python library (Pillow, `zipfile`, `pikepdf`) over invoking a
binary. If a binary is needed (ImageMagick, ffmpeg, LibreOffice), call it with an argument list,
fixed executable path, timeouts, and in a sandboxed worker — the parser itself is attack surface.

**User-defined templates** (email templates edited by customers): use a restricted language —
placeholder substitution (`string.Template.safe_substitute` with an allow-list of names) or Jinja2
`SandboxedEnvironment` with a minimal context — not DTL with the app's normal context.

**Serialization formats**: JSON for anything crossing a trust boundary. If a binary format is
required, sign it (`django.core.signing`, HMAC) so only your services can produce it — signing
protects integrity, it does not make pickle safe if the key leaks.

## 5. Django implementation patterns

**Parameterised raw SQL**

```python
from django.db import connection

def top_customers(tenant_id: int, since, limit: int = 20):
    sql = """
        SELECT c.id, c.name, SUM(o.total) AS revenue
        FROM billing_customer c JOIN billing_order o ON o.customer_id = c.id
        WHERE c.tenant_id = %s AND o.created_at >= %s
        GROUP BY c.id, c.name
        ORDER BY revenue DESC
        LIMIT %s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [tenant_id, since, min(limit, 100)])
        return cursor.fetchall()

# WRONG — any of these is injectable:
# cursor.execute(f"... WHERE c.tenant_id = {tenant_id}")
# Customer.objects.raw("SELECT * FROM billing_customer WHERE name = '%s'" % name)
# Customer.objects.raw("... WHERE name = '%s'", [name])   # quoted placeholder: never quote %s
# Customer.objects.extra(where=[f"name = '{name}'"])
```

`RawSQL("... = %s", (value,))` likewise; never format values into its SQL string.

**Allow-listed ordering and filtering**

```python
SORTS = {
    "newest": ("-created_at", "-id"),
    "oldest": ("created_at", "id"),
    "total": ("-total", "-id"),
}
FILTERABLE = {"status": "status", "customer": "customer_id"}

def list_orders(user, params):
    qs = Order.objects.for_user(user)
    filters = {FILTERABLE[k]: v for k, v in params.items() if k in FILTERABLE}
    return qs.filter(**filters).order_by(*SORTS.get(params.get("sort"), SORTS["newest"]))

# WRONG: Order.objects.filter(**request.GET.dict())   # caller picks lookups, relations, fields
# WRONG: qs.order_by(request.GET["sort"])               # sorts/oracles on any field
# WRONG: qs.values(*request.GET.getlist("fields"))      # exposes any column
```

**Dynamic identifiers in raw SQL** (rare): allow-list first, then quote with the driver:

```python
from psycopg import sql   # psycopg 3; PostgreSQL only

ALLOWED_REPORT_COLUMNS = {"created_at", "total", "status"}

def report(column: str):
    if column not in ALLOWED_REPORT_COLUMNS:
        raise ValueError("invalid column")
    query = sql.SQL("SELECT {col}, count(*) FROM billing_order GROUP BY {col}").format(
        col=sql.Identifier(column)
    )
    with connection.cursor() as cursor:
        cursor.execute(query.as_string(cursor.connection))  # VERIFY: as_string signature for installed psycopg
        return cursor.fetchall()
```

`connection.ops.quote_name()` quotes identifiers but is an internal helper that does not escape
embedded quote characters on every backend (`VERIFY`) — use it only on allow-listed values.

**Command execution**

```python
import subprocess
from pathlib import Path

CONVERT = "/usr/bin/convert"

def make_thumbnail(src: Path, dst: Path) -> None:
    subprocess.run(
        [CONVERT, "--", str(src), "-resize", "256x256", str(dst)],  # "--" ends option parsing (if the tool supports it)
        check=True,
        timeout=30,
        capture_output=True,
        env={"PATH": "/usr/bin"},
    )

# WRONG: subprocess.run(f"convert {filename} out.png", shell=True)
# WRONG: os.system("tar xf " + upload_name)
```

Argument injection survives argument lists: a filename `-delete` or `--output=/etc/x` becomes an
option. Use server-generated filenames ([file-upload-security.md](./file-upload-security.md)), the
`--` separator where supported, and absolute paths. `shlex.quote` is POSIX-shell-only and is a last
resort when a shell truly cannot be avoided.

**Templates**

```python
from django.template.loader import render_to_string

TEMPLATES_BY_KIND = {"invoice": "emails/invoice.html", "receipt": "emails/receipt.html"}

def render_email(kind: str, context: dict) -> str:
    return render_to_string(TEMPLATES_BY_KIND[kind], context)   # KeyError → 400 upstream

# WRONG: Template(request.POST["body"]).render(Context({...}))
# WRONG: render_to_string(request.GET["template"], ...)  # loads any template, e.g. admin internals

# Customer-editable placeholders:
from string import Template as StrTemplate

ALLOWED_VARS = {"first_name", "order_number"}

def render_customer_text(source: str, values: dict) -> str:
    return StrTemplate(source).safe_substitute({k: values[k] for k in ALLOWED_VARS})
```

**Headers and redirects**

```python
from django.http import HttpResponse
from django.utils.http import content_disposition_header   # Django ≥ 4.2

def download(request, doc):
    response = HttpResponse(doc.read_bytes(), content_type="application/pdf")
    # Correctly encodes/escapes the filename instead of f'attachment; filename="{name}"'
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=doc.name)
    return response
```

`FileResponse(..., as_attachment=True, filename=...)` does the same. Redirect targets are an
open-redirect problem, owned by [ssrf-prevention.md](./ssrf-prevention.md).

Email: build messages with `EmailMessage`/`send_mail`, validate addresses with `EmailValidator`,
and never let a user supply a raw recipient list or extra headers.

**Deserialisation and XML**

```python
import json

import yaml
from defusedxml import ElementTree as SafeET   # third-party defusedxml

payload = json.loads(request.body)                    # data only
config = yaml.safe_load(uploaded_text)                # never yaml.load / yaml.unsafe_load
tree = SafeET.fromstring(xml_bytes)                   # blocks entity expansion, external entities

# WRONG: pickle.loads(request.body), pickle.loads(base64.b64decode(cookie))
# WRONG: xml.etree / lxml.etree.fromstring on untrusted XML without hardening
# WRONG: numpy.load(..., allow_pickle=True), torch.load(untrusted) (pickle-based)
```

DRF's built-in parsers handle JSON/form/multipart; an XML parser (`djangorestframework-xml`,
third-party) must use defusedxml (`VERIFY` for the installed version).

**ReDoS** — bound input length before regex; avoid nested quantifiers (`(a+)+`, `(.*,)*`) in
validators; Python's `re` has no timeout (third-party `regex` module supports `timeout=`). Django's
own validators have had ReDoS fixes in security releases — stay on supported versions.

**`eval`/`exec`**: replace with `ast.literal_eval` for literals, `operator`-based tiny evaluators, or
a parsing library for formulas; never evaluate user expressions in-process.

**NoSQL/LDAP (brief)**: same principle — use the driver's filter builders (`ldap3` escape
helpers such as `escape_filter_chars`; MongoDB queries built from typed values, rejecting dict
input where a scalar is expected, e.g. `{"$ne": null}` operator injection).

## 6. Production use cases

- **Reporting endpoints** with user-chosen dimensions: public vocabulary → ORM expressions;
  raw SQL only for window/CTE gaps, parameterised.
- **Search with sort/filter query params**: DRF `ordering_fields` allow-list + django-filter
  `FilterSet` (third-party) with explicit fields.
- **Media pipelines** (thumbnails, PDF rendering, video transcoding): argument-list subprocesses in
  a worker container with timeouts, no network, and server-side filenames.
- **Customer-customisable notifications**: placeholder substitution, not a template engine.
- **Config/feature import** (YAML/CSV/XML uploads): `safe_load`, defusedxml, schema validation,
  size limits ([django-data-processing](../../django-data-processing/SKILL.md)).
- **Inter-service messages**: JSON with schema validation; Celery configured for JSON serializer,
  not pickle ([django-integrations](../../django-integrations/SKILL.md)).

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| ORM (default) | Parameterised, model-validated identifiers | Some queries awkward; identifier injection still possible via user-chosen names |
| Parameterised raw SQL | Full SQL power, safe values | Identifiers need allow-lists; no model validation; backend-specific |
| Allow-listed vocabularies | Prevents injection and data oracles; stable API | Must be maintained as fields evolve |
| Argument-list subprocess | No shell parsing | Argument injection and binary parser bugs remain |
| Sandboxed/restricted templates | Safe customer customisation | Limited expressiveness |
| JSON + schema | Data-only, interoperable | No rich types; schema upkeep |
| defusedxml | Blocks XXE/expansion | Third-party; must be used at every parse site |

## 8. Security and performance considerations

- Injection is high-impact: SQLi bypasses authorisation and tenant scoping entirely; command
  injection and unsafe deserialisation are remote code execution.
- Least-privilege DB roles limit SQLi blast radius (no DDL, no superuser, per-service roles) —
  see [postgresql-engineering](../../postgresql-engineering/SKILL.md).
- Database errors leaking into responses (`DEBUG`, verbose API errors) make blind SQLi easy; keep
  generic 500s ([api-security.md](./api-security.md)).
- Bound sizes before expensive parsing (`DATA_UPLOAD_MAX_MEMORY_SIZE`, explicit max lengths) to
  limit ReDoS and XML/YAML bombs.
- Subprocess calls are slow and resource-heavy; move them off the request path to workers with
  concurrency limits.
- Cache backends pickle values: an attacker who can write to Redis/Memcached can achieve code
  execution in Django — authenticate and network-isolate cache servers.

## 9. Common mistakes and anti-patterns

- Quoting placeholders (`'%s'`) or formatting params into SQL "because the ORM can't do it".
- `extra()` in new code (docs: "use as a last resort"; slated for deprecation).
- `filter(**request.GET.dict())`, `order_by(user_value)`, `values(*user_fields)`, DRF
  `ordering_fields = "__all__"`.
- Trusting `FieldError` as a security control — it blocks syntax, not sensitive-field access.
- `shell=True` with any interpolated value; `os.system`; building ImageMagick/ffmpeg command lines
  from original filenames.
- `Template(user_input)` or user-chosen template paths.
- Hand-built `Content-Disposition`/`Location` headers from user data.
- `pickle`/`yaml.load`/`jsonpickle`/`torch.load` on uploads, cookies, cache entries or messages
  from less-trusted services.
- Parsing SAML/SVG/DOCX/XML uploads with non-hardened parsers.
- `eval` for "simple formulas".

## 10. Troubleshooting and validation

- **Static analysis**: Bandit (B608 SQL string building, B602/B605 shell, B301 pickle, B506 yaml
  load, B314–B320 XML), Semgrep Django rules (raw SQL, `extra`, `mark_safe`, user-controlled
  `order_by`). Grep: `.raw(`, `.extra(`, `RawSQL(`, `cursor.execute(`, `shell=True`, `os.system`,
  `pickle.load`, `yaml.load(`, `Template(`, `eval(`, `**request.`.
- **Payload tests** for each dynamic query surface:

```python
from django.test import TestCase
from django.urls import reverse

class InjectionTests(TestCase):
    def test_sort_param_is_allow_listed(self):
        r = self.client.get(reverse("orders"), {"sort": "customer__user__password"})
        self.assertEqual(r.status_code, 200)       # falls back to default ordering, no FieldError/500

    def test_unknown_filter_keys_ignored(self):
        r = self.client.get(reverse("orders"), {"customer__tenant_id": 999, "_connector": "OR"})
        self.assertQuerySetEqual(r.context["orders"], Order.objects.for_user(self.user), ordered=False)

    def test_sql_metacharacters_are_data(self):
        r = self.client.get(reverse("customer-search"), {"q": "x' OR '1'='1"})
        self.assertEqual(len(r.context["results"]), 0)
```

- A `ProgrammingError`/`DataError` triggered by a user-supplied value is a red flag — it means input
  reached SQL structure or wasn't validated; investigate rather than catching it.
- Inspect generated SQL with `str(qs.query)` (approximate; params are interpolated for display only)
  or `connection.queries` in tests ([django-orm query-optimization](../../django-orm/references/query-optimization.md)).
- DAST (OWASP ZAP active scan, sqlmap against staging only with authorisation) for regression; see
  [security-testing-and-incident-response.md](./security-testing-and-incident-response.md).

## 11. Related references and skills

- [cross-site-scripting.md](./cross-site-scripting.md) — HTML/JS output encoding.
- [file-upload-security.md](./file-upload-security.md) — parsing uploaded documents/images, filenames.
- [ssrf-prevention.md](./ssrf-prevention.md) — URL inputs, open redirects.
- [security-logging-and-auditing.md](./security-logging-and-auditing.md) — log/CRLF injection.
- [api-security.md](./api-security.md) — filter/ordering abuse in DRF, error disclosure.
- [multi-tenant-security.md](./multi-tenant-security.md) — tenant scoping in raw SQL.
- [django-orm raw SQL](../../django-orm/references/raw-sql-and-database-access.md) · [django-orm advanced expressions](../../django-orm/references/advanced-query-expressions.md) · [django-rest-framework filtering](../../django-rest-framework/references/filtering-search-and-ordering.md)

## 12. Official documentation and external references

- Django security — SQL injection: https://docs.djangoproject.com/en/5.2/topics/security/#sql-injection-protection
- Performing raw SQL queries: https://docs.djangoproject.com/en/5.2/topics/db/sql/
- `QuerySet.extra()`: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#extra
- Django security archive (CVEs): https://docs.djangoproject.com/en/dev/releases/security/
- Signing: https://docs.djangoproject.com/en/5.2/topics/signing/
- Email header injection: https://docs.djangoproject.com/en/5.2/topics/email/#preventing-header-injection
- Python `subprocess` security: https://docs.python.org/3/library/subprocess.html#security-considerations
- Python `pickle` warning: https://docs.python.org/3/library/pickle.html
- Python XML vulnerabilities: https://docs.python.org/3/library/xml.html#xml-vulnerabilities
- defusedxml (third-party): https://github.com/tiran/defusedxml
- psycopg 3 SQL composition: https://www.psycopg.org/psycopg3/docs/api/sql.html
- OWASP SQL Injection Prevention: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html
- OWASP OS Command Injection Defense: https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html
- OWASP Deserialization: https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html
- OWASP XXE Prevention: https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html
- OWASP Injection Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Injection_Prevention_Cheat_Sheet.html
