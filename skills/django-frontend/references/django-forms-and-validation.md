# Django Forms and Validation (UI)

> **Scope:** How Django forms reach the browser and come back: rendering via form/field templates, widgets and custom widget templates, accessible error presentation, HTML5/client validation versus server validation, dynamic and dependent fields, HTMX/AJAX submissions, file upload UI, multi-step forms and formset UI. Form/ModelForm/validation mechanics (`clean_*`, `full_clean`, model constraints) belong to [forms-validation.md](../../django-core/references/forms-validation.md); serializer validation belongs to [fields-and-validation.md](../../django-rest-framework/references/fields-and-validation.md).

## 1. Purpose and scope

Use this reference when building or reviewing any form UI in a server-rendered Django app:
CRUD forms, filters, settings pages, uploads, wizards, editable line items. The invariants:

- **The server is the only validator that counts.** HTML5 attributes, JS checks, disabled inputs
  and hidden fields improve UX; they are never security or integrity controls.
- **One rendering path.** Every field renders through one field template, so labels, help text,
  errors and ARIA wiring are consistent and fixable in one place.
- **Errors are announced, linked and recoverable.** Invalid submit re-renders with values kept,
  an error summary, per-field messages programmatically associated, and HTTP 422.
- **Progressive enhancement.** The form works as a plain POST; HTMX/JS enhance it.

## 2. Core concepts

| Concept | Meaning for the UI |
|---|---|
| `BoundField` | Field + data + errors; exposes `label_tag`, `errors`, `help_text`, `auto_id`, `id_for_label`, `html_name`, `value()`, `widget_type`, `use_fieldset` |
| Widget | Renders the control(s) from a template with a `widget` context (`name`, `value`, `attrs`, `template_name`, `subwidgets`) |
| Form renderer | Object that loads form/field/widget templates (`FORM_RENDERER`) |
| Form template | `Form.template_name` — layout of the whole form (Django ≥ 4.0; `div` style default since 5.0) |
| Field template | `Field.template_name` / renderer `field_template_name`, used by `{{ field.as_field_group }}` (Django ≥ 5.0) |
| Non-field errors | `form.non_field_errors()` — errors from `clean()` or model constraints spanning fields |
| Management form | Hidden `TOTAL_FORMS`/`INITIAL_FORMS`/`MIN_NUM_FORMS`/`MAX_NUM_FORMS` inputs that let a formset parse POST data |
| Constraint Validation API | Browser API (`validity`, `checkValidity()`, `reportValidity()`, `setCustomValidity()`) behind HTML5 validation |

Built-in accessibility (Django ≥ 5.0): fields with `help_text` get `aria-describedby` pointing at
the help text element `<id>_helptext`, and invalid fields get `aria-invalid="true"` [VERIFY:
`aria-invalid` version and whether error lists are added to `aria-describedby` — on 5.2 assume
you must link error messages yourself, as below].

## 3. Internal mechanics

### Rendering chain

1. `{{ form }}` → `Form.render()` → renderer loads `form.template_name` (default
   `django/forms/div.html` on Django ≥ 5.0).
2. That template iterates fields; `{{ field.as_field_group }}` loads the field template
   (`django/forms/field.html` by default) with `field` in context.
3. `{{ field }}` → `BoundField.as_widget()` → `widget.get_context()` merges `widget.attrs`,
   `build_attrs()` (adds `required`, `id`, `maxlength`/`min`/`max`/`step` derived from the
   field), `aria-*` → renders `widget.template_name` (e.g. `django/forms/widgets/input.html`).
4. Errors render through `ErrorList` templates (`django/forms/errors/list/ul.html` etc.).

**Which templates are found depends on `FORM_RENDERER`.** The default `DjangoTemplates` renderer
uses its own engine that sees built-in form templates and installed apps' templates — but not
your project `DIRS`. `django.forms.renderers.TemplatesSetting` uses your `TEMPLATES` setting, so
project-level overrides of `django/forms/...` work; it requires `"django.forms"` in
`INSTALLED_APPS` (with `APP_DIRS=True`) so the built-in defaults remain findable.

### Field → HTML attribute derivation

`CharField(max_length=…)` → `maxlength`; `IntegerField(min_value, max_value)` → `min`/`max`;
`DecimalField(decimal_places)` → `step`; `EmailField` → `type="email"`; `URLField` →
`type="url"`; `required=True` → `required` (when `form.use_required_attribute`, default True).
These generate browser validation that mirrors, but does not replace, server validation.

### Browser validation mechanics

- On submit, the browser runs constraint validation on every listed element unless the form has
  `novalidate` or the submitter has `formnovalidate`. The first invalid control is focused and a
  browser-native bubble shown; `invalid` events fire (they do not bubble).
- `el.validity` exposes flags (`valueMissing`, `typeMismatch`, `patternMismatch`, `tooLong`,
  `rangeUnderflow`, `customError`, …). `setCustomValidity("msg")` marks invalid until reset with
  `""`.
- `:invalid` matches immediately (ugly on page load); `:user-invalid` matches only after
  interaction [VERIFY: `:user-invalid` Baseline status — newly available 2023].
- Client validation is bypassable trivially (DevTools, curl, disabled JS); `disabled` controls
  are not submitted at all, so a "disabled" field that the server reads from POST is a bug.

### HTMX and status codes

HTMX 2.x does not swap 4xx/5xx responses by default. Returning the re-rendered form with
**422** is semantically right but needs either `htmx.config.responseHandling` configured to swap
422, or an `htmx:beforeSwap` handler setting `evt.detail.shouldSwap = true` (and `isError =
false`) for 422. See §5 and [htmx-engineering.md](htmx-engineering.md).

## 4. Engineering decisions and trade-offs

### Rendering strategy

| Prefer | When |
|---|---|
| `FORM_RENDERER` subclass with project `form_template_name`/`field_template_name` | Default for any app: one consistent markup and a11y wiring |
| `{{ field.as_field_group }}` per field in a hand-written layout | Custom grid layouts, fieldsets grouping |
| Per-form `template_name` | One-off layouts (login, search bar) |
| django-crispy-forms / django-widget-tweaks (third-party) | Legacy codebases already using them; new code rarely needs them on Django ≥ 5.0 |

### Client vs server validation

| Prefer HTML5 attributes (default on) when | Prefer `novalidate` + server-only messages when |
|---|---|
| Simple constraints (required, type, length, range) | You need consistent, translated, styled messages and an error summary |
| Fast feedback with zero JS | Browser bubbles conflict with your design or are not announced well enough in your AT matrix |
| Low-stakes internal tools | Complex cross-field rules make partial client checks misleading |

A balanced default: keep attributes (they drive mobile keyboards, autofill and `:user-invalid`
styling), add `novalidate` to the `<form>` only when you render a full server error summary and
inline errors, and optionally run `checkValidity()` yourself to show inline hints.

### Submission style

| Prefer | When |
|---|---|
| Plain POST → redirect (PRG) | Default; works everywhere; correct history |
| HTMX `hx-post` swapping the form | Inline edit, modals, forms inside dashboards |
| `fetch` + JSON | Form lives in a React/JS island; API exists anyway |

### Multi-step

| Prefer | When |
|---|---|
| One long form with `<fieldset>` sections | ≤ ~15 fields; users benefit from seeing everything |
| Session-backed wizard (own code) | Few steps, custom flow, need full control |
| django-formtools `SessionWizardView` (third-party) | Many steps, conditional steps, standard behavior acceptable |
| Persisted draft model | Long processes, resume across devices, audit trail |

## 5. Django/frontend implementation patterns

### Project renderer and field template

```python
# core/forms/renderers.py
from django.forms.renderers import TemplatesSetting

class ProjectFormRenderer(TemplatesSetting):
    form_template_name = "forms/form.html"
    field_template_name = "forms/field.html"     # Django ≥ 5.0
```

```python
# config/settings/base.py
INSTALLED_APPS += ["django.forms"]               # built-in widget templates stay discoverable
FORM_RENDERER = "core.forms.renderers.ProjectFormRenderer"
```

```django
{# templates/forms/form.html #}
{% if form.errors %}{% include "forms/error_summary.html" with form=form only %}{% endif %}
{% for field in form.hidden_fields %}{{ field }}{% endfor %}
{% for field in form.visible_fields %}{{ field.as_field_group }}{% endfor %}
```

```django
{# templates/forms/field.html — rendered with `field` (BoundField) in context #}
{% with err_id=field.auto_id|add:"_error" help_id=field.auto_id|add:"_helptext" %}
{% if field.use_fieldset %}
  <fieldset class="field{% if field.errors %} field--invalid{% endif %}"
            {% if field.help_text or field.errors %}aria-describedby="{% if field.errors %}{{ err_id }}{% endif %}{% if field.help_text %} {{ help_id }}{% endif %}"{% endif %}>
    <legend>{{ field.label }}{% if field.field.required %} <span class="req">(required)</span>{% endif %}</legend>
{% else %}
  <div class="field{% if field.errors %} field--invalid{% endif %}">
    <label for="{{ field.id_for_label }}">{{ field.label }}{% if field.field.required %} <span class="req">(required)</span>{% endif %}</label>
{% endif %}
    {% if field.errors %}
      <ul class="field__errors" id="{{ err_id }}">
        {% for e in field.errors %}<li><span class="visually-hidden">Error:</span> {{ e }}</li>{% endfor %}
      </ul>
    {% endif %}
    {{ field }}
    {% if field.help_text %}<p class="field__help" id="{{ help_id }}">{{ field.help_text }}</p>{% endif %}
{% if field.use_fieldset %}</fieldset>{% else %}</div>{% endif %}
{% endwith %}
```

The error id must also appear on the control. Do it once in a form mixin rather than in
templates, so every widget (including subwidgets) gets it:

```python
# core/forms/mixins.py
class AccessibleErrorsMixin:
    def full_clean(self):
        super().full_clean()
        for name in self.errors:
            if name in self.fields:
                bf = self[name]
                attrs = self.fields[name].widget.attrs
                ids = [f"{bf.auto_id}_error"]
                if self.fields[name].help_text:
                    ids.append(f"{bf.auto_id}_helptext")
                attrs["aria-describedby"] = " ".join(ids)
                attrs["aria-invalid"] = "true"
```

(Field instances are deep-copied per form instance, so mutating `self.fields[...].widget.attrs`
does not leak across requests.) If your Django version already emits `aria-invalid`/
`aria-describedby`, drop the mixin parts it duplicates [VERIFY: compare against 5.2 rendered widget output].

### Error summary with links

```django
{# templates/forms/error_summary.html #}
<div class="error-summary" role="alert" tabindex="-1" id="error-summary" aria-labelledby="error-summary-title">
  <h2 id="error-summary-title">There {{ form.errors|length|pluralize:"is a problem,are problems" }}</h2>
  <ul>
    {% for e in form.non_field_errors %}<li>{{ e }}</li>{% endfor %}
    {% for field in form.visible_fields %}
      {% for e in field.errors %}<li><a href="#{{ field.id_for_label }}">{{ field.label }}: {{ e }}</a></li>{% endfor %}
    {% endfor %}
  </ul>
</div>
```

On a full-page re-render, prefix `<title>` with "Error: " and move focus to the summary
(`document.getElementById("error-summary")?.focus()` in a static module; the summary's
`tabindex="-1"` makes it programmatically focusable). `role="alert"` announces it when inserted by an HTMX swap.

### Widgets and attrs

```python
# billing/forms.py
from django import forms
from django.utils import timezone
from core.forms.mixins import AccessibleErrorsMixin
from .models import Invoice, Customer

class InvoiceForm(AccessibleErrorsMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["customer", "due_date", "amount", "notes"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),  # type=date needs ISO value
            "amount": forms.NumberInput(attrs={"inputmode": "decimal", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {"due_date": "Must be today or later."}

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.for_tenant(tenant)   # scope choices
        self.fields["customer"].widget.attrs["autocomplete"] = "off"

    def clean_due_date(self):
        due = self.cleaned_data["due_date"]
        if due < timezone.localdate():
            raise forms.ValidationError("Due date cannot be in the past.", code="past_date")
        return due
```

Use `autocomplete` tokens (`email`, `given-name`, `street-address`, `one-time-code`,
`new-password`) on user-data fields — it drives autofill and supports WCAG 1.3.5 and 3.3.7.

### Custom widget template

```python
# core/forms/widgets.py
from django import forms

class CurrencyInput(forms.NumberInput):
    template_name = "widgets/currency_input.html"

    def __init__(self, currency="EUR", attrs=None):
        super().__init__(attrs={"inputmode": "decimal", "step": "0.01", **(attrs or {})})
        self.currency = currency

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        ctx["widget"]["currency"] = self.currency
        return ctx
```

```django
{# templates/widgets/currency_input.html #}
<div class="input-group">
  {% include "django/forms/widgets/input.html" %}
  <span class="input-group__suffix" aria-hidden="true">{{ widget.currency }}</span>
</div>
```

Put the currency in the label too ("Amount (EUR)") since the suffix is `aria-hidden`.
Custom widget templates require `TemplatesSetting` (or an app-level template) to be found.

### View: PRG with 422 on invalid

```python
# billing/views.py
@login_required
def invoice_create(request):
    form = InvoiceForm(request.POST or None, tenant=request.tenant)
    if request.method == "POST":
        if form.is_valid():
            invoice = create_invoice(tenant=request.tenant, by=request.user, **form.cleaned_data)
            messages.success(request, f"Invoice {invoice.number} created.")
            if request.htmx:
                return HttpResponseClientRedirect(invoice.get_absolute_url())   # django-htmx (third-party)
            return redirect(invoice)
        template = "billing/invoice_form.html#form" if request.htmx else "billing/invoice_form.html"
        return render(request, template, {"form": form}, status=422)
    return render(request, "billing/invoice_form.html", {"form": form})
```

```django
{# billing/templates/billing/invoice_form.html (partials via django-template-partials on 5.2) #}
{% extends "layouts/app.html" %}
{% load partials %}
{% block title %}{% if form.errors %}Error: {% endif %}New invoice · {{ block.super }}{% endblock %}
{% block page %}
  {% partialdef form inline %}
  <form id="invoice-form" method="post" action="{% url 'billing:invoice_create' %}" novalidate
        hx-post="{% url 'billing:invoice_create' %}" hx-target="this" hx-swap="outerHTML"
        hx-disabled-elt="find button[type=submit]">
    {% csrf_token %}
    {{ form }}
    <button type="submit" class="btn btn--primary">Create invoice</button>
  </form>
  {% endpartialdef %}
{% endblock %}
```

```js
// static/js/htmx-config.js — let 422 responses swap (HTMX 2.x)
htmx.config.responseHandling = [
  { code: "204", swap: false },
  { code: "[23]..", swap: true },
  { code: "422", swap: true, error: false },
  { code: "[45]..", swap: false, error: true },
];  // [VERIFY: responseHandling entry shape against your htmx 2.x version]
document.body.addEventListener("htmx:afterSwap", (e) => {
  e.detail.target.querySelector?.("#error-summary")?.focus();
});
```

`hx-disabled-elt` prevents double-submits client-side; idempotency on the server (unique
constraints, idempotency keys) is still required.

### Inline validation on blur (HTMX)

```django
<input … hx-post="{% url 'accounts:validate_field' %}" hx-trigger="change delay:300ms"
       hx-target="closest .field" hx-swap="outerHTML" hx-include="closest form" name="email">
```

```python
@require_POST
def validate_field(request):
    form = SignupForm(request.POST)
    name = request.headers.get("HX-Trigger-Name")
    if name not in form.fields:
        return HttpResponseBadRequest()
    form.is_valid()   # runs everything; render only the triggering field
    return render(request, "forms/field.html", {"field": form[name]})
```

Only show errors for the field the user just left; do not validate untouched fields. Respond 200
here (it is a validation preview, not a failed submit). Rate-limit if it reveals data (e.g.
"email already registered" is an enumeration oracle — see
[authentication-security.md](../../django-security-engineering/references/authentication-security.md)).

### Dependent fields (country → region)

```python
class AddressForm(forms.Form):
    country = forms.ModelChoiceField(Country.objects.all())
    region = forms.ModelChoiceField(Region.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        country_id = self.data.get(self.add_prefix("country")) or getattr(self.initial.get("country"), "pk", None)
        if country_id:
            self.fields["region"].queryset = Region.objects.filter(country_id=country_id)  # server enforces the dependency

def region_options(request):
    form = AddressForm(request.GET)
    return render(request, "addresses/_region_field.html", {"field": form["region"]})
```

```django
{{ form.country.as_field_group }}   {# widget attrs: hx-get=region_options url, hx-target="#region-field", hx-trigger="change", hx-include="this" #}
<div id="region-field" aria-live="polite">{{ form.region.as_field_group }}</div>
```

Set the `hx-*` attrs on the country widget in `__init__`. Without JS, the user submits, gets a
422 with the region list populated — still functional.

### Conditional fields

Show/hide with native `<details>`, `hidden`, or Alpine `x-show` bound to a radio/checkbox; the
server decides requiredness in `clean()`:

```python
def clean(self):
    data = super().clean()
    if data.get("billing_type") == "company" and not data.get("vat_number"):
        self.add_error("vat_number", "VAT number is required for companies.")
    if data.get("billing_type") != "company":
        data["vat_number"] = ""      # ignore hidden-field values; never trust them
    return data
```

Hidden fields keep focus out of the tab order (`hidden`/`display:none`), but their values are
still submitted if not `disabled` — sanitize server-side as above.

### File upload UI

```django
<form method="post" enctype="multipart/form-data" action="{% url 'projects:attach' project.pk %}">
  {% csrf_token %}
  <label for="id_file">Attachment</label>
  <input type="file" id="id_file" name="file" accept=".pdf,.png,.jpg,image/png,image/jpeg"
         aria-describedby="id_file_helptext" data-max-bytes="{{ max_upload_bytes }}">
  <p id="id_file_helptext">PDF, PNG or JPG, up to {{ max_upload_bytes|filesizeformat }}.</p>
  <progress id="upload-progress" max="100" value="0" hidden>0%</progress>
  <button type="submit">Upload</button>
</form>
```

```js
// static/js/upload.js — early feedback only; the server re-checks everything
const input = document.getElementById("id_file");
input?.addEventListener("change", () => {
  const f = input.files[0];
  input.setCustomValidity(f && f.size > Number(input.dataset.maxBytes) ? "File is too large." : "");
  input.reportValidity();
});
// HTMX uses XHR, so upload progress is available:
document.body.addEventListener("htmx:xhr:progress", (e) => {
  const p = document.getElementById("upload-progress");
  if (p && e.detail.lengthComputable) { p.hidden = false; p.value = (e.detail.loaded / e.detail.total) * 100; }
});
```

`fetch()` has no upload-progress events; use XHR (`xhr.upload.onprogress`) or HTMX. Server side:
validate size in `clean_file`, extension with `FileExtensionValidator`, and actual content type
by sniffing; cap request size at the proxy. `FILE_UPLOAD_MAX_MEMORY_SIZE` is a memory→disk
threshold, not a size limit. Details: [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md),
[file-handling.md](../../django-core/references/file-handling.md).

### Multi-step form (session-backed)

```python
STEPS = [("details", ProjectDetailsForm), ("team", TeamForm), ("billing", BillingForm)]
SESSION_KEY = "project_wizard"

@login_required
def project_wizard(request, step):
    names = [s for s, _ in STEPS]
    if step not in names:
        raise Http404
    idx = names.index(step)
    data = request.session.setdefault(SESSION_KEY, {})
    if any(prev not in data for prev in names[:idx]):            # no skipping ahead via URL
        return redirect("projects:wizard", step=names[0])
    FormClass = dict(STEPS)[step]
    form = FormClass(request.POST or None, initial=data.get(step))
    if request.method == "POST" and form.is_valid():
        data[step] = form.cleaned_data_for_session()             # your helper: JSON-serializable pks, ISO dates
        request.session.modified = True
        if idx + 1 < len(names):
            return redirect("projects:wizard", step=names[idx + 1])
        project = create_project_from_wizard(request.user, request.tenant, data)  # re-validates all steps
        del request.session[SESSION_KEY]
        return redirect(project)
    return render(request, "projects/wizard.html", {"form": form, "step": idx + 1, "total": len(names), "names": names},
                  status=422 if form.errors else 200)
```

UI: a step indicator as an ordered list with `aria-current="step"`, a "Back" link (GET, not a
submit that loses data), the step number in `<title>` and `<h1>` ("Step 2 of 3: Team"). The final
commit re-validates all steps because session data may be stale (e.g. a team member removed).
django-formtools (third-party) `SessionWizardView` implements the same with `get_form_list`/
`condition_dict`.

### Formset UI (add/remove rows)

```python
LineFormSet = forms.inlineformset_factory(
    Invoice, InvoiceLine, fields=["description", "quantity", "unit_price"],
    extra=0, can_delete=True, max_num=50, validate_max=True, min_num=1, validate_min=True,
)
```

```django
<form method="post">
  {% csrf_token %}
  {{ formset.management_form }}
  {% if formset.non_form_errors %}<div role="alert">{{ formset.non_form_errors }}</div>{% endif %}
  <table>
    <caption>Invoice lines</caption>
    <thead><tr><th scope="col">Description</th><th scope="col">Qty</th><th scope="col">Unit price</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
    <tbody id="lines">
      {% for f in formset %}{% include "billing/_line_row.html" with f=f only %}{% endfor %}
    </tbody>
  </table>
  <template id="line-template">{% include "billing/_line_row.html" with f=formset.empty_form only %}</template>
  <button type="button" id="add-line">Add line</button>
  <p id="lines-status" class="visually-hidden" aria-live="polite"></p>
  <button type="submit">Save</button>
</form>
```

```js
// static/js/formset.js
const prefix = "lines";   // must match LineFormSet(request.POST or None, instance=invoice, prefix="lines")
const total = document.getElementById(`id_${prefix}-TOTAL_FORMS`);
const max = Number(document.getElementById(`id_${prefix}-MAX_NUM_FORMS`).value);
document.getElementById("add-line").addEventListener("click", () => {
  const n = Number(total.value);
  if (n >= max) return;
  const html = document.getElementById("line-template").innerHTML.replaceAll("__prefix__", String(n));
  document.getElementById("lines").insertAdjacentHTML("beforeend", html);
  total.value = String(n + 1);
  document.querySelector(`#id_${prefix}-${n}-description`)?.focus();
  document.getElementById("lines-status").textContent = `Line ${n + 1} added.`;
});
```

Remove rows by checking the `DELETE` checkbox and hiding the row (for existing rows) — never
renumber existing forms; removing unsaved extra rows from the DOM is fine only if you renumber
or let them stay empty (empty extra forms are skipped when unchanged). The server bounds count
with `max_num`/`validate_max` and `absolute_max`; TOTAL_FORMS is attacker-controlled.

## 6. Production use cases

- **Invoice editor:** ModelForm + line-item formset, error summary, HTMX partial re-render with
  422, PRG on success.
- **Tenant settings page:** grouped `<fieldset>`s, one field template, per-section save with
  HTMX and a toast on success.
- **Signup:** autocomplete tokens, inline validation on change for email/password strength,
  server-side enumeration-safe messages.
- **Report filters:** GET form, `{% querystring %}` for links, no CSRF needed, values echoed
  back into the form.
- **Document uploads:** `accept` + size hint + progress, async virus scan, status polling.
- **Onboarding wizard:** session-backed 3 steps; final atomic create re-validating all data.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| Server-rendered Django forms | Single source of validation, works without JS, accessible baseline | Full round-trip for feedback |
| HTML5 attributes | Instant feedback, mobile keyboards, zero JS | Browser-styled messages, inconsistent AT announcements, bypassable |
| HTMX form swaps | Partial updates, keeps server validation | Must handle 422 swap config, focus management |
| JS-heavy client validation | Rich UX | Duplicated rules drift from server; more code to test |
| Formsets | Built-in multi-row parsing and validation | Prefix/management-form complexity in JS |

## 8. Security and performance considerations

### Security

- Validate and authorize on the server for every field, including hidden, readonly, disabled
  and "derived" fields. Exclude non-editable fields from `Meta.fields` rather than hiding them.
- Scope every `ModelChoiceField`/`ModelMultipleChoiceField` queryset to tenant/user; a forged
  pk outside the queryset is then a validation error, not an IDOR.
- `{% csrf_token %}` in every POST form; HTMX sends the token via `hx-headers` on `<body>` or the
  form field (see [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md)).
- Never echo raw input with `|safe` in error messages; `ValidationError` params are escaped.
- Do not leak existence (accounts, emails) through inline validation endpoints.
- Uploads: server-side size/type/content checks, storage outside web root, no user-controlled
  filenames in paths.
- Formsets: `max_num`, `validate_max`, `absolute_max` bound memory and DB work.

### Performance

- Choice fields with large querysets render every option on every request → use an
  autocomplete endpoint (HTMX `hx-get` search) or a `TextInput` + pk validation.
- `ModelChoiceField` in formsets re-queries per form unless you share a cached choices iterator
  (set `field.choices` once from a list in the formset's `__init__`/`get_form_kwargs`).
- Inline validation: debounce (`delay:300ms`), and avoid expensive `clean()` logic on each call.
- Large uploads: stream to object storage, not through app memory; show progress.

## 9. Common mistakes and anti-patterns

1. **Status 200 on invalid POST** → return 422 (and configure HTMX to swap it).
2. **Redirect-less success** (render after POST) → PRG to avoid duplicate submits on refresh.
3. **Errors only shown by color/border** → text messages linked via `aria-describedby`,
   "Error:" prefix, summary with links.
4. **Placeholder used as label** → real `<label for>`; placeholder only for format examples.
5. **`DateInput(attrs={"type":"date"})` without ISO `format`** → value doesn't show on re-render.
6. **Relying on `disabled`/`readonly` to protect fields** → exclude them from the form; set in
   the view/service.
7. **Dependent dropdown filtered only in JS** → filter queryset in `__init__` from `self.data`.
8. **Formset rows renumbered or TOTAL_FORMS not updated** → use `empty_form` + `__prefix__` and
   increment TOTAL_FORMS; DELETE existing rows.
9. **`accept` treated as validation** → server validates content.
10. **Wizard trusting earlier-step session data at commit** → re-validate everything in the
    final service call.
11. **Custom widget templates not found** → `FORM_RENDERER = TemplatesSetting` subclass +
    `"django.forms"` in `INSTALLED_APPS`.
12. **Focus lost after HTMX swap** → focus error summary or first invalid field after swap.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| Form override templates ignored | Default renderer doesn't use project `DIRS` | `FORM_RENDERER` setting; `django.forms` in apps |
| HTMX invalid submit shows nothing | 422 not swapped in HTMX 2.x | `responseHandling` / `htmx:beforeSwap`; Network tab |
| "ManagementForm data is missing or has been tampered with" | Missing `{{ formset.management_form }}` or prefix mismatch | Hidden inputs in DOM; `prefix` in view and JS |
| Extra row ignored on save | TOTAL_FORMS not incremented | Inspect POST payload |
| Select options empty after error | Dependent queryset built only from `initial` | Use `self.data` in `__init__` |
| `request.FILES` empty | Missing `enctype="multipart/form-data"` or form not passed `request.FILES` | Form tag; `Form(request.POST, request.FILES)` |
| CSRF 403 on HTMX post | Token not sent | `hx-headers` / hidden input; cookie present |
| Required field never errors in browser | `novalidate` or `use_required_attribute=False` | Rendered HTML |
| Screen reader doesn't read error | No `aria-describedby`, error not in live region after swap | Accessibility tree in DevTools |

Validation steps:

1. Django test client: invalid POST → `status_code == 422`, `assertFormError`, error text in
   HTML; valid POST → 302 (see [views-forms-and-middleware-testing.md](../../django-testing/references/views-forms-and-middleware-testing.md)).
2. Tamper tests: forged choice pk from another tenant, extra fields, TOTAL_FORMS > max.
3. Playwright: submit empty form, assert focus on summary, links move focus to fields, axe passes;
   repeat with JS disabled (see [frontend-testing.md](frontend-testing.md)).
4. Keyboard-only run through wizard and formset add/remove.

## 11. Related references and skills

- [django-template-engineering.md](django-template-engineering.md) — components, partials
- [html-and-semantic-ui.md](html-and-semantic-ui.md) — labels, fieldset/legend, input types
- [htmx-engineering.md](htmx-engineering.md) — swap/error handling, CSRF headers
- [alpine-js-engineering.md](alpine-js-engineering.md) — local show/hide state
- [frontend-state-and-interaction-design.md](frontend-state-and-interaction-design.md) — multi-step, optimistic UI
- [frontend-accessibility.md](frontend-accessibility.md) — error announcement, focus
- [frontend-security.md](frontend-security.md) — upload UI, client-side controls
- [frontend-testing.md](frontend-testing.md)
- [forms-validation.md](../../django-core/references/forms-validation.md), [file-handling.md](../../django-core/references/file-handling.md)
- [csrf-protection.md](../../django-security-engineering/references/csrf-protection.md), [file-upload-security.md](../../django-security-engineering/references/file-upload-security.md)
- [fields-and-validation.md](../../django-rest-framework/references/fields-and-validation.md)

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/topics/forms/
- https://docs.djangoproject.com/en/5.2/ref/forms/api/
- https://docs.djangoproject.com/en/5.2/ref/forms/renderers/
- https://docs.djangoproject.com/en/5.2/ref/forms/widgets/
- https://docs.djangoproject.com/en/5.2/topics/forms/formsets/
- https://docs.djangoproject.com/en/5.2/topics/forms/modelforms/
- https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/
- https://developer.mozilla.org/en-US/docs/Web/HTML/Constraint_validation
- https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/setCustomValidity
- https://developer.mozilla.org/en-US/docs/Web/HTML/Attributes/autocomplete
- https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#constraints
- https://www.w3.org/WAI/tutorials/forms/
- https://www.w3.org/WAI/WCAG22/Understanding/error-identification.html
- https://htmx.org/docs/#response-handling
- https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
