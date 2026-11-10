# Cross-Site Scripting (XSS)

> Scope: reflected, stored and DOM-based XSS in Django applications; how template auto-escaping
> works and where it stops; context-specific output encoding (attributes, URLs, JavaScript, CSS);
> `mark_safe`, `|safe`, `{% autoescape %}`, `format_html`, `json_script`, `escapejs`; safe custom
> filters and tags; the Jinja2 backend; HTML sanitisation for rich text with nh3 (third-party);
> Markdown; API responses and `Content-Type`; CSP as defence in depth.
> Not covered here: CSP and header configuration ([cors-and-browser-security.md](./cors-and-browser-security.md));
> SVG/HTML uploads and serving user files ([file-upload-security.md](./file-upload-security.md));
> template injection (user-controlled template source) ([injection-prevention.md](./injection-prevention.md));
> frontend framework code and build tooling ([django-frontend](../../django-frontend/SKILL.md)).

## 1. Purpose and scope

XSS lets an attacker run script in a victim's browser **on your origin**: read the page and any
non-HttpOnly cookies, issue authenticated requests (CSRF tokens are readable to same-origin
script), change account email, exfiltrate data, or plant a fake login form. In a Django app it
undoes session security, CSRF protection and authorisation for the victim's session.

Django's template engine escapes variables by default — a **framework protection** that covers the
most common case (text in an HTML element body or a quoted attribute). Everything else — other
output contexts, deliberate opt-outs, rich text, JavaScript building DOM, non-template responses —
is **developer responsibility**. This reference maps those boundaries.

## 2. Core concepts

| Type | Source | Sink | Django example |
|---|---|---|---|
| Reflected | Request (query, path, form) | Response that echoes it | `HttpResponse(f"No results for {q}")` |
| Stored | Database, uploaded file, third-party feed | Any later rendering | `{{ comment.body|safe }}` |
| DOM-based | `location`, `postMessage`, API JSON | JS sink (`innerHTML`, `eval`, `href`) | `el.innerHTML = data.name` in a static JS file |

**Output encoding is context-specific.** The same string needs different encoding inside an HTML
body, a quoted attribute, a URL, a JavaScript string, or CSS. HTML escaping alone is correct only
for the first two.

**Trust markers**: Django distinguishes `str` (untrusted, escaped on output) from `SafeString`
(trusted, emitted verbatim). `mark_safe()` and `|safe` convert; `escape()` /
`conditional_escape()` produce safe output from untrusted input.

**Sanitisation** (allow-listing HTML tags/attributes) is a different operation from escaping and is
only for content that must remain HTML (rich text). Escaping is the default; sanitising is the
exception.

## 3. Internal mechanics

- Django Template Language (DTL) auto-escapes every `{{ variable }}` output unless the value is a
  `SafeData` instance or auto-escaping is off. `django.utils.html.escape` replaces
  `<` `>` `'` `"` `&` with entities (`'` → `&#x27;`).
- `conditional_escape(x)` escapes unless `x` is already safe — the right primitive in custom code.
- **Safe-ness propagation**: concatenating a `SafeString` with `str` yields a plain `str` (unsafe);
  f-strings and `%` formatting drop the marker too. `format_html()` escapes each argument and
  returns `SafeString`.
- **Filters**: a filter registered with `is_safe=True` tells Django that a safe input stays safe
  after filtering (only valid if the filter cannot introduce `<>&"'`). `needs_autoescape=True`
  passes the current `autoescape` flag so the filter can call `conditional_escape` itself.
- `{% autoescape off %}` disables escaping for the block; filters like `safe`, `safeseq` mark
  values safe. Built-ins such as `urlize`, `linebreaks`, `linebreaksbr` escape their input when
  auto-escaping is on and return safe HTML.
- String literals inside tags (`{% firstof "<b>" %}`) and the default argument of filters are
  treated as already-safe template source — they are *author* content, not user content.
- `json_script` serialises with `DjangoJSONEncoder`, escapes `<`, `>`, `&` as `<` etc., and
  wraps it in `<script type="application/json">`, making it safe to embed; read with
  `JSON.parse(el.textContent)`. Element id is optional (Django ≥ 4.1).
- `escapejs` escapes characters for use inside a quoted JavaScript string; the docs warn it does
  **not** make text safe for HTML, nor for JavaScript template literals.
- **Jinja2 backend**: Django's `django.template.backends.jinja2.Jinja2` enables `autoescape=True` by
  default (raw Jinja2 `Environment` defaults to off). `Markup`/`|safe` are the opt-outs; Django
  `SafeString` implements `__html__`, so it interoperates.
- **Forms and admin**: widget rendering escapes values; `help_text` and `label` on model fields are
  **not** escaped in admin/forms output (they are treated as developer content — `VERIFY` per
  widget); never put user data there.
- `HttpResponse`/`JsonResponse` do no escaping. `JsonResponse` defaults to
  `Content-Type: application/json`, which browsers won't render as HTML (with `nosniff`).

## 4. Engineering decisions and trade-offs

**Escape vs sanitise vs restrict format**

| Requirement | Approach |
|---|---|
| Plain text (names, comments) | Store raw, auto-escape on render. Never pre-escape in the DB. |
| Light formatting (bold, links, lists) | Markdown with HTML disabled → render → sanitise output with nh3 |
| Full rich text (WYSIWYG) | Sanitise with a strict nh3 allow-list; also CSP |
| Arbitrary HTML/JS from customers (themes, widgets) | Serve from a separate sandbox origin or `<iframe sandbox>`, never the app origin |

**Sanitise on write or on read?** Sanitising on write keeps rendering cheap and makes stored data
"clean", but an allow-list fix later does not repair old rows. Sanitising on read always applies the
current policy but costs CPU per render. Production choice: **sanitise on write and store the
sanitised HTML; re-sanitise on read (or in a migration) when the policy changes**; keep the raw
source only if users must edit it again.

**nh3 vs bleach**: bleach (third-party) was deprecated in January 2023; nh3 (third-party, Rust
`ammonia` bindings) is the maintained replacement and is faster. Neither is built into Django.

**CSP as backstop**: a nonce-based CSP turns many injection bugs into blocked scripts. It does not
help with HTML injection that phishes (fake forms) or with DOM XSS in nonce-approved scripts.

## 5. Django implementation patterns

**Default: let the template escape.**

```django
<p>Results for “{{ query }}”</p>                         {# safe: element body #}
<input type="text" name="q" value="{{ query }}">          {# safe: quoted attribute #}
<input type="text" name="q" value={{ query }}>            {# UNSAFE: unquoted — "x onfocus=alert(1)" #}
```

**URLs from users** — HTML escaping doesn't stop `javascript:`:

```python
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

def validate_http_url(value: str) -> None:
    URLValidator(schemes=["http", "https"])(value)     # rejects javascript:, data:, vbscript:
    if urlsplit(value).scheme not in {"http", "https"}:
        raise ValidationError("Only http(s) links are allowed.")

class Profile(models.Model):
    website = models.URLField(blank=True, validators=[validate_http_url])
```

```django
<a href="{{ profile.website }}" rel="nofollow noopener noreferrer">{{ profile.website }}</a>
```

Note `URLField` already restricts schemes to http/https/ftp/ftps by default, but values set outside
forms (APIs, imports, admin `list_editable` bypasses) skip form validation — validate in the model
or serializer too.

**Data for JavaScript** — never interpolate into `<script>`:

```django
{# WRONG: </script> or a quote breaks out #}
<script>const user = "{{ user.name }}";</script>
<script>const cfg = {{ cfg|safe }};</script>

{# RIGHT #}
{{ cfg|json_script:"page-config" }}
<script nonce="{{ request.csp_nonce }}">
  const cfg = JSON.parse(document.getElementById("page-config").textContent);
</script>
```

**Building HTML in Python**

```python
from django.utils.html import format_html, format_html_join

def status_badge(order) -> str:
    return format_html('<span class="badge badge-{}">{}</span>', order.status, order.get_status_display())

def tag_list(tags) -> str:
    return format_html_join("", "<li>{}</li>", ((t.name,) for t in tags))

# WRONG: mark_safe(f'<span class="badge">{order.note}</span>')
```

Calling `format_html()` with only a string and no arguments was deprecated in Django 5.0 and is
expected to raise in Django 6.0 (`VERIFY`); use `mark_safe` for genuinely constant HTML.

**Safe custom filter**

```python
from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter(needs_autoescape=True)
def highlight(text, term, autoescape=True):
    esc = conditional_escape if autoescape else (lambda s: s)
    text, term = esc(text), esc(term)
    if not term:
        return mark_safe(text)
    return mark_safe(text.replace(term, f"<mark>{term}</mark>"))  # both inputs already escaped
```

**Rich text with nh3 (third-party)**

```python
import nh3

ALLOWED_TAGS = {"p", "br", "strong", "em", "ul", "ol", "li", "a", "blockquote", "code", "pre", "h2", "h3"}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}

def sanitize_rich_text(html: str) -> str:
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )

class Article(models.Model):
    body_html = models.TextField()

    def save(self, *args, **kwargs):
        self.body_html = sanitize_rich_text(self.body_html)
        super().save(*args, **kwargs)
```

Sanitising in `save()` misses `bulk_create`/`update()`; for strict guarantees put it in the
service/serializer that owns writes as well ([django-architecture service layer](../../django-architecture/references/service-layer.md)).
Render with `{{ article.body_html|safe }}` and a comment pointing to the sanitiser. Never allow
`style`, `on*` attributes, `<script>`, `<iframe>`, `<svg>`, `<math>` or `<form>` in user HTML.

**Markdown**: disable raw HTML in the renderer (library-specific option) **and** sanitise the
rendered output; Markdown links allow `javascript:` unless schemes are restricted.

**DOM XSS (frontend code)**: use `textContent`, `setAttribute` for vetted attributes, framework
binding (`{{ }}` in Vue/React JSX) instead of `innerHTML`, `v-html`, `dangerouslySetInnerHTML`,
`insertAdjacentHTML`, `document.write`, `eval`, `new Function`, or `location = userValue`. Consider
Trusted Types (`require-trusted-types-for 'script'` CSP directive) for large frontends — see
[django-frontend](../../django-frontend/SKILL.md).

**Non-template responses**

```python
# WRONG: reflected XSS (text/html by default)
return HttpResponse(f"Unknown report {request.GET['id']}")

# RIGHT
return JsonResponse({"detail": "Unknown report."}, status=404)
# or HttpResponse(escape(msg)), or content_type="text/plain; charset=utf-8"
```

DRF: the Browsable API (`BrowsableAPIRenderer`) renders data inside HTML and escapes it; disable it
in production if not needed. API JSON containing user HTML is safe *until a client inserts it with
`innerHTML`* — document which fields are sanitised HTML vs plain text.

## 6. Production use cases

- **Comments/reviews**: plain text, auto-escaped, `linebreaksbr`/`urlize` for formatting.
- **CMS / knowledge base**: WYSIWYG editor → nh3 sanitise on save → `|safe` with CSP nonce policy.
- **Email templates**: HTML emails are rendered by mail clients — still escape user data (phishing
  links, layout breaking) via DTL auto-escaping.
- **Admin `list_display` with HTML**: use `format_html` in the admin method; never `mark_safe` on
  model data.
- **Search pages**: reflected query values escaped; highlighting via the safe filter pattern above.
- **Customer-provided themes**: render on a sandbox domain (`usercontent.example.net`) with no app
  cookies, or in a sandboxed iframe.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Auto-escaping (built-in) | Default-on, zero effort, covers most output | Only HTML body/quoted attribute contexts; opt-outs are easy |
| `format_html` | Explicit, escapes arguments | Easy to bypass by pre-building strings |
| `json_script` | Correct JS data embedding | Requires reading via `JSON.parse` |
| nh3 sanitisation | Allows rich text safely | Policy maintenance; allow-list mistakes reopen XSS |
| CSP | Mitigates unknown bugs | Doesn't fix the bug; inline-script refactors |
| Sandbox origin | Strongest isolation for arbitrary HTML | Extra domain, infra, cookie scoping |

## 8. Security and performance considerations

- `HttpOnly` session cookies stop cookie theft, **not** XSS: injected script can still act as the
  user. XSS is an account-takeover bug.
- Pre-escaping before storage causes double-escaping and tempts `|safe` later; escape at output.
- Sanitisation is CPU work (nh3 is fast; bleach was slow) — sanitise on write and cache rendered
  Markdown.
- Error pages and `DEBUG=True` pages reflect request data — never run `DEBUG` in production.
- Mutation XSS (mXSS) exploits parser differences between sanitiser and browser; use a
  browser-grade parser (nh3/html5ever) and keep it updated.
- Uploaded HTML/SVG served from the app origin is stored XSS — see
  [file-upload-security.md](./file-upload-security.md).

## 9. Common mistakes and anti-patterns

- `{{ value|safe }}`, `mark_safe(user_input)`, `{% autoescape off %}` on user data.
- `mark_safe(f"...{x}...")` / `mark_safe("<b>%s</b>" % x)` instead of `format_html`.
- Interpolating into `<script>`, `<style>`, `on*=` handlers, or unquoted attributes.
- Trusting `escapejs` for HTML or template literals.
- `href="{{ url }}"` without scheme validation; `data:` and `javascript:` URLs.
- Filters registered `is_safe=True` that can add markup from input.
- Treating admin-entered data as trusted (staff accounts get phished; least privilege).
- Using regex or `strip_tags` as a sanitiser — `strip_tags` output must still be escaped; it is
  not a security boundary.
- Raw Jinja2 `Environment()` without autoescape.
- Frontend `innerHTML = apiResponse.field`.

## 10. Troubleshooting and validation

- **Grep for opt-outs** in review/CI: `mark_safe(`, `|safe`, `autoescape off`, `safeseq`,
  `is_safe=True`, `SafeString(`, `innerHTML`, `v-html`, `dangerouslySetInnerHTML`. Bandit flags
  `mark_safe` (B308) and Semgrep has Django XSS rules.
- **Payload tests**: render with `<script>alert(1)</script>`, `"><img src=x onerror=alert(1)>`,
  `javascript:alert(1)`, `</script><script>alert(1)</script>` and assert they appear escaped.

```python
from django.test import TestCase
from django.urls import reverse

PAYLOAD = '"><script>alert(1)</script>'

class XSSTests(TestCase):
    def test_search_reflection_is_escaped(self):
        r = self.client.get(reverse("search"), {"q": PAYLOAD})
        self.assertNotContains(r, PAYLOAD)
        self.assertContains(r, "&lt;script&gt;")

    def test_rich_text_strips_scripts_and_handlers(self):
        html = sanitize_rich_text('<p onclick="x()">hi</p><script>bad()</script><a href="javascript:x">l</a>')
        self.assertNotIn("script", html)
        self.assertNotIn("onclick", html)
        self.assertNotIn("javascript:", html)

    def test_json_script_breakout(self):
        r = self.client.get(reverse("dashboard"))  # context contains user-controlled "</script>"
        self.assertNotContains(r, "</script><script>")
```

- Double-escaped output (`&amp;lt;`) means data was escaped before storage or a value was escaped
  twice — remove the pre-escape, don't add `|safe`.
- CSP report-only violations from inline scripts reveal places where user data may reach script
  contexts.

## 11. Related references and skills

- [cors-and-browser-security.md](./cors-and-browser-security.md) — CSP rollout and nonces.
- [injection-prevention.md](./injection-prevention.md) — template injection, header injection.
- [file-upload-security.md](./file-upload-security.md) — SVG/HTML uploads, `Content-Disposition`.
- [session-and-cookie-security.md](./session-and-cookie-security.md) — `HttpOnly`, session impact.
- [api-security.md](./api-security.md) — Browsable API, response content types.
- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — XSS regression tests.
- [django-core forms-validation](../../django-core/references/forms-validation.md) · [django-frontend](../../django-frontend/SKILL.md) · [django-testing](../../django-testing/SKILL.md)

## 12. Official documentation and external references

- Django security — XSS: https://docs.djangoproject.com/en/5.2/topics/security/#cross-site-scripting-xss-protection
- Automatic HTML escaping: https://docs.djangoproject.com/en/5.2/ref/templates/language/#automatic-html-escaping
- `django.utils.html` (`format_html`, `conditional_escape`): https://docs.djangoproject.com/en/5.2/ref/utils/#module-django.utils.html
- Custom filters and auto-escaping: https://docs.djangoproject.com/en/5.2/howto/custom-template-tags/#filters-and-auto-escaping
- `json_script`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#json-script
- `escapejs`: https://docs.djangoproject.com/en/5.2/ref/templates/builtins/#escapejs
- Jinja2 backend: https://docs.djangoproject.com/en/5.2/topics/templates/#django.template.backends.jinja2.Jinja2
- nh3 (third-party): https://nh3.readthedocs.io/
- OWASP XSS Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- OWASP DOM-based XSS Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html
- MDN Trusted Types: https://developer.mozilla.org/en-US/docs/Web/API/Trusted_Types_API
