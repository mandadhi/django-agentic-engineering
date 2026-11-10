# Templates, forms and static files

## Template structure

```text
templates/
├── base.html                 # <html>, head, nav, {% block content %}, messages
├── components/               # _button.html, _pagination.html, _field.html
└── <app>/                    # app pages: list.html, detail.html, form.html
```

- `TEMPLATES[0]["DIRS"] = [BASE_DIR / "templates"]`, `APP_DIRS = True`.
- In production, the cached template loader is on by default when `DEBUG = False`.
- Keep context processors cheap — they run for every `render`.

## Includes, partials and components

```django
{% include "components/_pagination.html" with page=page_obj only %}
```

`only` keeps includes from depending on ambient context. Template partials (Django ≥ 6.0
`{% partialdef name %}`, render as `"template.html#name"`) let one file define both the page and
its HTMX fragments. Before 6.0, `django-template-partials` provides the same API.

## Custom tags and filters

```python
# apps/core/templatetags/money.py
register = template.Library()

@register.filter
def money(value: Decimal, currency: str = "EUR") -> str:
    return format_currency(value, currency)        # pure formatting, no queries
```

Use `simple_tag` / `inclusion_tag` for small components. Tags never query the database or check
permissions.

## Passing data to JavaScript

```django
{{ chart_data|json_script:"chart-data" }}
<script nonce="{{ request.csp_nonce }}">
  const data = JSON.parse(document.getElementById("chart-data").textContent);
</script>
```

(`request.csp_nonce` with Django ≥ 6.0 CSP; otherwise use a separate static JS file.)

## Forms

```python
class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["customer", "due_date", "notes"]
        widgets = {"due_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.for_user(user)   # scoped choices

    def clean_due_date(self):
        due = self.cleaned_data["due_date"]
        if due < timezone.localdate():
            raise forms.ValidationError("Due date cannot be in the past.", code="past_date")
        return due
```

```python
@login_required
def invoice_create(request):
    form = InvoiceForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        invoice = create_invoice(created_by=request.user, **form.cleaned_data)
        messages.success(request, "Invoice created.")
        return redirect(invoice)
    status = 422 if form.errors else 200
    return render(request, "billing/invoice_form.html", {"form": form}, status=status)
```

- `ModelForm.is_valid()` runs model validation, including `Meta.constraints`
  (`validate_constraints`), so users see constraint errors as form errors.
- Scope every `ModelChoiceField` queryset to the user/tenant.
- Render fields consistently with a field template (`{{ field.as_field_group }}`, Django ≥ 5.0,
  customisable via `FORM_RENDERER` / `field_template_name`).
- Formsets: `inlineformset_factory(Parent, Child, fields=[...], extra=0, can_delete=True,
  max_num=50, validate_max=True)` — always bound the count.

## Messages

`messages.success/info/warning/error` + a block in `base.html`; with HTMX, render messages in an
out-of-band swap (see [htmx.md](htmx.md)).

## Static files

```python
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},  # or Django's ManifestStaticFilesStorage
}
```

- Hashed filenames → cache forever (`Cache-Control: max-age=31536000, immutable`).
- If a JS/CSS build tool is used (Vite, esbuild), output to `static/` and let `collectstatic`
  pick it up; don't introduce a bundler just for a few scripts. HTMX can be vendored as a single
  static file.
- User uploads (`MEDIA_*`) are never served by WhiteNoise — see
  [django-data-processing](../../django-data-processing/SKILL.md).

## Official documentation

- https://docs.djangoproject.com/en/stable/ref/templates/language/
- https://docs.djangoproject.com/en/stable/ref/templates/builtins/
- https://docs.djangoproject.com/en/stable/howto/custom-template-tags/
- https://docs.djangoproject.com/en/stable/topics/forms/
- https://docs.djangoproject.com/en/stable/topics/forms/modelforms/
- https://docs.djangoproject.com/en/stable/topics/forms/formsets/
- https://docs.djangoproject.com/en/stable/ref/contrib/staticfiles/
- https://whitenoise.readthedocs.io/
