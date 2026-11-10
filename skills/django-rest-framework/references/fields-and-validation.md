# Fields and Validation

> **Scope.** Owns: built-in serializer field types and core options (`required`, `allow_null`,
> `allow_blank`, `default`, `read_only`, `write_only`, `source`), field-level and object-level
> validation, validators (built-in, unique, custom), cross-field and conditional validation,
> custom fields, validation ordering and the validation error structure.
> Does not cover: serializer lifecycle and `save()` ([serializers-and-model-serializers.md](serializers-and-model-serializers.md)),
> relational fields ([nested-and-relational-serializers.md](nested-and-relational-serializers.md)),
> how errors are formatted in responses ([exception-handling-and-error-responses.md](exception-handling-and-error-responses.md)),
> model validation and database constraints
> ([django-orm model-design](../../django-orm/references/model-design.md),
> [django-orm database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md)).

## 1. Purpose and scope

Validation at the API boundary turns untrusted input into data the service can rely on. This
reference explains which DRF hook to use for which rule, the order DRF runs them in, and how
serializer validation relates to model validation and database constraints.

## 2. Core concepts

**Core field options.**

| Option | Default | Meaning |
|---|---|---|
| `required` | `True` (unless `default` set or `read_only`) | Key must be present on input |
| `allow_null` | `False` | `null` accepted |
| `allow_blank` | `False` (string fields) | `""` accepted |
| `default` | — | Used when the key is missing (not in partial updates); implies `required=False`; may be a callable |
| `read_only` / `write_only` | `False` | Output-only / input-only |
| `source` | field name | Attribute path (`"customer.name"`) or `"*"` for the whole object |
| `validators` | `[]` | Callables run after type conversion |
| `error_messages` | — | Override messages per error code |

**Common field types** (all in `rest_framework.serializers`): `BooleanField`, `CharField`
(`max_length`, `min_length`, `trim_whitespace=True`), `EmailField`, `RegexField`, `SlugField`,
`URLField`, `UUIDField`, `IntegerField`/`FloatField` (`min_value`, `max_value`), `DecimalField`
(`max_digits`, `decimal_places` required; `coerce_to_string`), `DateTimeField`, `DateField`,
`TimeField`, `DurationField`, `ChoiceField`, `MultipleChoiceField`, `FileField`, `ImageField`
(requires Pillow), `ListField` (`child`, `min_length`, `max_length`, `allow_empty`), `DictField`,
`JSONField`, `ReadOnlyField`, `HiddenField`, `SerializerMethodField`, `ModelField`.

**Three validation layers.**

| Layer | Runs | Good for | Not good for |
|---|---|---|---|
| Serializer validation | On `is_valid()` | Input shape, types, ranges, cross-field input rules, user-facing messages | Guaranteeing invariants under concurrency |
| Model validation (`clean()`, `full_clean()`) | Only when called (forms/admin call it; **DRF does not**) | Rules shared by admin, forms and services | Being relied on from DRF automatically |
| Database constraints | Every write, every code path | Invariants that must always hold (uniqueness, checks, FKs) | Friendly messages — they raise `IntegrityError` |

Rule of thumb: enforce invariants in the database; mirror the most common ones in the serializer
for a good error message; handle the remaining `IntegrityError` in the service.

## 3. Internal mechanics

Order for a single `is_valid()` call:

1. **Per field**, in declaration order (`Serializer.to_internal_value`):
   1. Empty-value handling: missing → `required`/`default`; `None` → `allow_null`.
   2. `field.to_internal_value()` — type coercion (`"12"` → `12`), raising e.g. `invalid`.
   3. `field.run_validators()` — `validators=[...]` and implicit ones (`MaxLengthValidator`,
      `UniqueValidator` from the model).
   4. `serializer.validate_<field_name>(value)` — returns the (possibly transformed) value.
2. If **any** field failed, raise now — object-level validation does **not** run.
3. **Serializer-level validators** (`Meta.validators`, e.g. `UniqueTogetherValidator`).
4. **`serializer.validate(attrs)`** — must return `attrs`.

**Error structure.** `serializer.errors` is a dict of field name → list of `ErrorDetail`
(a `str` subclass with a `.code`). Object-level errors go under `non_field_errors`
(`NON_FIELD_ERRORS_KEY` setting). Nested serializers produce nested dicts; `many=True` produces
a list aligned with the input list.

```json
{
  "quantity": ["Ensure this value is less than or equal to 1000."],
  "lines": [{}, {"product_id": ["Unknown product."]}],
  "non_field_errors": ["ends_at must be after starts_at."]
}
```

`exc.get_codes()` and `exc.get_full_details()` expose codes for machine-readable errors.

**Unique validators.** `UniqueValidator(queryset=...)` and `UniqueTogetherValidator(queryset=...,
fields=[...])` run a `SELECT ... EXISTS`. They are a race-prone pre-check: two concurrent
requests can both pass. The database constraint is the real guarantee. `UniqueTogetherValidator`
makes all its fields required.

## 4. Engineering decisions and trade-offs

| Rule | Put it in |
|---|---|
| Type, format, length, range | Field type and options |
| Single-field rule needing no other data (`sku` format) | `validators=[...]` (reusable) or `validate_<field>` |
| Single-field rule needing the DB or request (`product_id` exists and belongs to tenant) | `validate_<field>` or a scoped related field |
| Cross-field input rule (`ends_at > starts_at`) | `validate()` |
| Business rule depending on current state (stock available, order still open) | Service, raising a domain error — not the serializer |
| Invariant that must hold under concurrency (unique SKU per tenant) | DB constraint + serializer pre-check + `IntegrityError` handling |

Serializer validation should be **pure with respect to side effects**: it may read, never write.

## 5. Django/DRF implementation patterns

**Field-level and object-level:**

```python
class BookingInputSerializer(serializers.Serializer):
    room_id = serializers.IntegerField(min_value=1)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    attendees = serializers.ListField(child=serializers.EmailField(), max_length=50, required=False)

    def validate_room_id(self, value):
        org = self.context["request"].user.organization
        if not Room.objects.filter(pk=value, organization=org).exists():
            raise serializers.ValidationError("Unknown room.", code="unknown_room")
        return value

    def validate(self, attrs):
        if attrs["ends_at"] <= attrs["starts_at"]:
            raise serializers.ValidationError(
                {"ends_at": "Must be after starts_at."}, code="invalid_range"
            )
        return attrs
```

**Conditional validation with partial updates** — absent keys fall back to the instance:

```python
def validate(self, attrs):
    starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
    ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
    if starts and ends and ends <= starts:
        raise serializers.ValidationError({"ends_at": "Must be after starts_at."}, code="invalid_range")
    if attrs.get("status") == "cancelled" and not attrs.get("cancellation_reason"):
        raise serializers.ValidationError(
            {"cancellation_reason": "Required when cancelling."}, code="required"
        )
    return attrs
```

**Reusable validators:**

```python
from django.core.validators import RegexValidator

sku_validator = RegexValidator(r"^[A-Z0-9-]{4,32}$", "Invalid SKU.", code="invalid_sku")

class NotInPast:
    requires_context = True               # receives the field as second argument
    def __call__(self, value, serializer_field):
        if value < timezone.now():
            raise serializers.ValidationError("Must not be in the past.", code="in_past")
```

Django validators (`django.core.validators`) work in serializers; Django's `ValidationError`
raised from them is converted by DRF field validation.

**Unique validators with tenant scope** (mirror of a DB `UniqueConstraint(fields=["organization", "sku"])`):

```python
class ProductSerializer(serializers.ModelSerializer):
    organization = serializers.HiddenField(default=CurrentOrganizationDefault())

    class Meta:
        model = Product
        fields = ["id", "sku", "name", "organization"]
        validators = [
            UniqueTogetherValidator(queryset=Product.objects.all(), fields=["organization", "sku"],
                                    message="SKU already exists.")
        ]
```

(`CurrentOrganizationDefault` is a project class following the `CurrentUserDefault` pattern:
`requires_context = True`, `__call__(self, serializer_field)` returns
`serializer_field.context["request"].user.organization`.)

**Custom field:**

```python
class MoneyField(serializers.Field):
    default_error_messages = {"invalid": "Expected {\"amount\": str, \"currency\": str}."}

    def to_representation(self, value: Money):
        return {"amount": str(value.amount), "currency": value.currency}

    def to_internal_value(self, data):
        try:
            return Money(Decimal(data["amount"]), data["currency"])
        except (KeyError, TypeError, InvalidOperation):
            self.fail("invalid")
```

**Integrity errors as a fallback** (in the service):

```python
try:
    product = Product.objects.create(organization=org, sku=sku, name=name)
except IntegrityError as exc:
    raise DuplicateSku(sku) from exc          # mapped to 400/409 by the exception handler
```

## 6. Production use cases

- **Tenant-scoped uniqueness**: DB `UniqueConstraint`, serializer `UniqueTogetherValidator` for
  the message, service catches the race.
- **State transitions**: serializer validates the requested target status is a known choice;
  the service checks the transition is legal from the current state under a row lock.
- **Bulk payloads**: `ListField(max_length=...)`/`many=True` with a bound, so a 1 MB array
  cannot cause thousands of validation queries.
- **Machine-readable errors**: every custom `ValidationError` has a stable `code`.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| Declarative, field-keyed errors clients can map to UI inputs | Model `clean()` is skipped unless you call it |
| Collects all field errors in one response | Object-level `validate()` is skipped while any field is invalid |
| Validators are reusable across serializers | Per-field DB lookups multiply on list inputs |
| Codes enable stable client handling | Unique validators are not race-safe |

## 8. Security and performance considerations

- Validate every input bound (lengths, numeric ranges, list sizes, file sizes) — it caps both
  abuse and work.
- Validate ownership/tenancy of every referenced ID; existence alone is insufficient
  ([permissions-and-access-control.md](permissions-and-access-control.md)).
- Avoid per-item queries in `validate_<field>` of a `many=True` child; validate the set of IDs
  once in the parent's `validate()` with one `filter(pk__in=...)`.
- Do not echo raw input back in error messages where it may contain secrets or markup.
- `RegexField`/`RegexValidator` with catastrophic backtracking patterns are a DoS vector.

## 9. Common mistakes and anti-patterns

- `validate()` forgetting to `return attrs` (validated data becomes `None`).
- Mixing Django's and DRF's `ValidationError` inconsistently. DRF converts a Django
  `ValidationError` raised during validation, but raising `serializers.ValidationError` with an
  explicit `code` keeps error structures predictable.
- Assuming `blank=True` on the model means the serializer accepts `null` (it maps to
  `allow_blank`/`required=False`; `null=True` maps to `allow_null`).
- Relying on `Model.clean()` for API rules.
- Side effects (writes, emails) inside validation hooks.
- `required=False` without `default` and then indexing `attrs["field"]`.

## 10. Troubleshooting and validation

| Symptom | Check |
|---|---|
| `validate()` never runs | A field-level error occurred first |
| Unique error not raised | Field is read-only or excluded, so the validator is not generated; or `HiddenField` missing |
| Unexpected `This field is required` on PATCH | `partial=True` not passed |
| Whitespace stripped from a password | `CharField(trim_whitespace=True)` default — set `False` |
| `IntegrityError` → 500 | DB constraint without serializer mirror or service handling |

Test each rule directly against the serializer and assert on `errors` and codes
([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [serializers-and-model-serializers.md](serializers-and-model-serializers.md)
- [nested-and-relational-serializers.md](nested-and-relational-serializers.md) — relationship validation
- [exception-handling-and-error-responses.md](exception-handling-and-error-responses.md) — error format
- [django-orm database-constraints-and-indexes](../../django-orm/references/database-constraints-and-indexes.md)
- [django-orm model-design](../../django-orm/references/model-design.md) — `clean()`/`full_clean()`
- [django-core forms-validation](../../django-core/references/forms-validation.md) — Django form validation (comparison)

## 12. Official documentation and external references

- Fields: <https://www.django-rest-framework.org/api-guide/fields/>
- Validators: <https://www.django-rest-framework.org/api-guide/validators/>
- Serializer validation: <https://www.django-rest-framework.org/api-guide/serializers/#validation>
- Django validators: <https://docs.djangoproject.com/en/5.2/ref/validators/>
