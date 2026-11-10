# Forms and validation

> Scope: `Form`, `ModelForm`, field and cross-field validation, `ValidationError`, the form lifecycle, formsets, validators, normalisation. Not covered here: model-level validation internals ([models-application-lifecycle.md](./models-application-lifecycle.md)); upload storage and file security ([file-handling.md](./file-handling.md)); API serializers ([django-rest-framework](../../django-rest-framework/SKILL.md)); CSRF/XSS hardening ([django-security-engineering](../../django-security-engineering/SKILL.md)); test infrastructure ([django-testing](../../django-testing/SKILL.md)).

## Purpose and overview

A form is Django's boundary between untrusted input and trusted Python values. It does three jobs: **parse** raw strings into typed values, **validate** them (per field, then across fields), and **report** errors in a structure templates or APIs can render. Treat `form.cleaned_data` as the only data that may flow into services; `request.POST` never should.

Forms are useful outside HTML too: as input validators for management commands, webhooks or JSON views when DRF isn't in use.

## How it works internally

The lifecycle is lazy. Nothing is validated until something asks:

1. **Construction** — `Form(data=None, files=None, initial=..., prefix=...)`. With `data` the form is *bound*; without it, *unbound* (renders initial values, never valid).
2. **Trigger** — `is_valid()`, `.errors` or `full_clean()` starts validation once and caches the result.
3. **`_clean_fields()`** — for each field in declaration order:
   - `field.clean(value)` → `to_python()` (coerce, raise on type errors) → `validate()` (required, choices) → `run_validators()` (field `validators=[...]`).
   - Then `clean_<fieldname>()` on the form if defined; its return value replaces the cleaned value.
   - A field that fails is **absent** from `cleaned_data`.
4. **`_clean_form()`** — calls `form.clean()` for cross-field rules; returns `cleaned_data`.
5. **`_post_clean()`** — no-op on `Form`; on `ModelForm` it builds the instance (`construct_instance`) and calls `instance.full_clean(exclude=...)`, mapping model errors back to form fields. Fields not on the form are excluded from model validation.
6. **`save()`** (ModelForm only) — writes the instance; `commit=False` returns it unsaved and defers M2M via `form.save_m2m()`.

`ValidationError` can carry a message, a `code`, `params`, a list, or a dict keyed by field. Use `code` so callers and tests can match errors without parsing text.

## Core concepts and APIs

| API | Purpose |
|---|---|
| `forms.Form` / `forms.ModelForm` | Plain input vs model-backed input |
| `Meta.fields` | Explicit whitelist (never `"__all__"` on user-facing forms) |
| `clean_<field>()` | Single-field rule needing form context; must **return** the value |
| `clean()` | Cross-field rules; call `super().clean()` first |
| `add_error(field, error)` | Attach an error to a field from `clean()`; removes field from `cleaned_data` |
| `non_field_errors()` | Errors raised from `clean()` without a field |
| `validators=[...]` | Reusable callables (`MinValueValidator`, custom functions) |
| `has_changed()` / `changed_data` | Detect edits relative to `initial` |
| `formset_factory`, `modelformset_factory`, `inlineformset_factory` | Repeated forms with management form |
| `errors.as_json()` / `errors.get_json_data()` | Machine-readable errors |

Rendering since 5.0 uses the div-based template and field groups (`{{ form.field.as_field_group }}`); 4.x defaulted to table rendering. Check the project version before advising template changes.

## Implementation examples

**Form with field and cross-field validation.**

```python
# apps/orders/forms.py
from decimal import Decimal
from django import forms
from django.core.exceptions import ValidationError
from .models import Order


class CheckoutForm(forms.Form):
    email = forms.EmailField()
    coupon = forms.CharField(required=False, max_length=32)
    quantity = forms.IntegerField(min_value=1, max_value=100)
    ship_date = forms.DateField(required=False)
    express = forms.BooleanField(required=False)

    def clean_coupon(self) -> str:
        code = self.cleaned_data["coupon"].strip().upper()   # normalise
        if code and not Coupon.objects.active().filter(code=code).exists():
            raise ValidationError("Unknown or expired coupon.", code="invalid_coupon")
        return code                                          # must return

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("express") and not cleaned.get("ship_date"):   # .get(): field may have failed
            self.add_error("ship_date", ValidationError("Required for express shipping.", code="required_for_express"))
        return cleaned
```

**ModelForm — bad vs good.**

```python
# Bad: exposes every field, including status and customer.
class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

# Good: whitelist; server-controlled fields are set by the view/service.
class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["total", "discount"]
```

```python
# apps/orders/views.py
@login_required
def create_order(request):
    form = OrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        order = form.save(commit=False)
        order.customer = request.user         # set before model validation? see note
        order.full_clean()
        order.save()
        return redirect("orders:detail", pk=order.pk)
    return render(request, "orders/create.html", {"form": form}, status=400 if form.errors else 200)
```

Note: `ModelForm` validation excludes `customer` because it isn't a form field, so rules in `Order.clean()` depending on `customer` don't see it during `is_valid()`. Either pass `instance=Order(customer=request.user)` when constructing the form (preferred — then `clean()` sees it), or re-run `full_clean()` as above.

```python
form = OrderForm(request.POST or None, instance=Order(customer=request.user))
```

**Reusable validator.**

```python
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

@deconstructible            # required if used on a model field (migrations serialize it)
class MaxDecimalPlacesValidator:
    def __init__(self, places: int):
        self.places = places

    def __call__(self, value):
        if value.as_tuple().exponent < -self.places:
            raise ValidationError("Too many decimal places.", code="decimal_places", params={"places": self.places})

    def __eq__(self, other):
        return isinstance(other, MaxDecimalPlacesValidator) and other.places == self.places
```

**Inline formset.**

```python
OrderLineFormSet = forms.inlineformset_factory(
    Order, OrderLine, fields=["sku", "quantity"], extra=1, can_delete=True,
    max_num=50, validate_max=True, absolute_max=100,
)

formset = OrderLineFormSet(request.POST or None, instance=order)
if request.method == "POST" and formset.is_valid():
    with transaction.atomic():
        formset.save()
```

Override `BaseInlineFormSet.clean()` for rules across rows (e.g. duplicate SKUs); skip forms in `self.deleted_forms` and forms with errors. The management form (`TOTAL_FORMS`, `INITIAL_FORMS`) must be rendered — missing it raises `ValidationError` ("ManagementForm data is missing") rather than a field error.

**File field validation (brief).** Validate size and type in `clean_<field>()`; do not trust `content_type` or the filename. Storage, size limits and secure handling: [file-handling.md](./file-handling.md).

## Production patterns

- **Forms validate, services act.** The view calls `form.is_valid()`, then passes `cleaned_data` to a service (`place_order(**form.cleaned_data, customer=request.user)`). The form never sends emails or charges cards.
- Use forms for non-HTML input too: `form = WebhookForm(json.loads(request.body)); if not form.is_valid(): return JsonResponse(form.errors.get_json_data(), status=400)`.
- Return HTTP 400 (or 422 for APIs, by convention) for invalid submissions — monitoring and tests depend on it.
- Pass request-scoped context explicitly: `def __init__(self, *args, user, **kwargs)` then `super().__init__(*args, **kwargs)`; restrict querysets of choice fields per user (`self.fields["address"].queryset = user.addresses.all()`).
- Set `error_messages` and `code` on errors; frontends and translations key off codes.
- Normalise in `clean_<field>()` (strip, case-fold, canonicalise phone numbers) so the database stores one representation.

## Decision guidance

| Situation | Use |
|---|---|
| Input maps 1:1 to a model's editable fields | `ModelForm` with explicit `fields` |
| Input spans models or isn't persisted directly | `Form` + service |
| Rule involves one field only, no context | Validator on the field (reusable) |
| Rule involves one field + DB lookup/context | `clean_<field>()` |
| Rule involves several fields | `clean()` with `add_error` |
| Invariant must hold for every write path | Model constraint + `Model.clean()` |
| Many rows of the same shape | Formset (cap with `max_num`/`absolute_max`) |
| JSON API with DRF available | Serializer (django-rest-framework skill) |

## Security considerations

- Always whitelist `Meta.fields`; `exclude` silently exposes fields added later.
- Scope `ModelChoiceField` querysets to the current user — the default `Model.objects.all()` lets users select other tenants' objects by ID.
- Form errors are auto-escaped in templates; don't mark user input `safe` in error messages.
- Formsets: set `absolute_max` (and `validate_max`) to bound memory/CPU from forged `TOTAL_FORMS`.
- `cleaned_data` is not authorisation. Check permissions in the view/service before acting.
- CSRF protection applies to the view, not the form; see [django-security-engineering](../../django-security-engineering/SKILL.md).

## Performance considerations

- `ModelChoiceField` evaluates its queryset when rendering — large tables produce huge `<select>`s and N+1 label queries. Use `select_related` in the queryset, autocomplete widgets, or `ModelChoiceIterator` customisation.
- DB lookups in `clean_<field>()` run per submit; acceptable, but avoid them inside formset per-row clean when one aggregated query in `formset.clean()` will do.
- `inlineformset_factory` saves row by row; for bulk imports use a service with `bulk_create`.

## Testing

Test forms directly — no client, no templates:

```python
def test_express_requires_ship_date():
    form = CheckoutForm(data={"email": "a@example.com", "quantity": 1, "express": True})
    assert not form.is_valid()
    assert form.has_error("ship_date", code="required_for_express")

def test_coupon_is_normalised(active_coupon):
    form = CheckoutForm(data={"email": "a@example.com", "quantity": 1, "coupon": "  save10 "})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["coupon"] == "SAVE10"
```

- Assert on error **codes** (`has_error(field, code=...)`), not message text.
- For formsets, build POST data including the management form fields with the correct prefix.
- File uploads: `SimpleUploadedFile` passed via `files=`.
- View-level tests assert status codes (400 on invalid) and that no side effects happened.

## Common mistakes and anti-patterns

- `clean_<field>()` that forgets to `return` → field becomes `None`.
- `cleaned_data["x"]` in `clean()` → `KeyError` when `x` already failed; use `.get()`.
- Using `request.POST` after validation instead of `cleaned_data`.
- Raising `ValidationError` for a specific field from `clean()` without `add_error` → shows as non-field error.
- `fields = "__all__"` or `exclude = [...]` on user-facing ModelForms.
- Expecting `ModelForm` to validate model fields not included in the form.
- Business side effects in `form.save()` or `clean()`.
- Validating only in JavaScript.
- Rendering `{{ form }}` without the formset management form.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `is_valid()` always False, no visible errors | Unbound form (`data` not passed) or errors only in `non_field_errors` | Pass `request.POST`; render `form.non_field_errors` |
| Field value becomes `None` | `clean_<field>()` missing `return` | Return the value |
| File field always "required" | Missing `request.FILES` or `enctype="multipart/form-data"` | Pass `files=request.FILES`, fix template |
| "ManagementForm data is missing or has been tampered with" | Management form not rendered or wrong prefix | Render `{{ formset.management_form }}`, align prefixes |
| Model `clean()` error not shown | Field excluded from form, so error is non-field or skipped | Pass `instance=` with server values; render non-field errors |
| Unique error not raised on update | Instance not passed (`instance=`), so it's treated as insert | Pass the instance |

## Official documentation

- https://docs.djangoproject.com/en/stable/topics/forms/
- https://docs.djangoproject.com/en/stable/ref/forms/api/
- https://docs.djangoproject.com/en/stable/ref/forms/validation/
- https://docs.djangoproject.com/en/stable/topics/forms/modelforms/
- https://docs.djangoproject.com/en/stable/topics/forms/formsets/
- https://docs.djangoproject.com/en/stable/ref/validators/
