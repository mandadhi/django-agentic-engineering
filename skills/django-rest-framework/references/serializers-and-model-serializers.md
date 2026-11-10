# Serializers and ModelSerializers

> **Scope.** Owns: serializer architecture and lifecycle, `Serializer`, `ModelSerializer`,
> `HyperlinkedModelSerializer`, `ListSerializer`/`many=True`, `partial=True`, `save()`/`create()`/
> `update()`, `to_representation()`/`to_internal_value()`, context, `SerializerMethodField`,
> dynamic and inherited serializers, and input vs output serializer design.
> Does not cover: individual field types and validation hooks in depth
> ([fields-and-validation.md](fields-and-validation.md)), relationship fields and nested writes
> ([nested-and-relational-serializers.md](nested-and-relational-serializers.md)), or how views
> instantiate serializers ([views-and-viewsets.md](views-and-viewsets.md)).

## 1. Purpose and scope

A serializer has two jobs: **deserialization** (untrusted primitive data → validated Python data,
optionally persisted) and **serialization** (Python/model objects → primitive data for a
renderer). This reference explains how serializers do both, how `ModelSerializer` automates the
common case, and where that automation stops being helpful.

## 2. Core concepts

| Class | Use |
|---|---|
| `serializers.Serializer` | Explicit fields; input for a service, non-model output, command payloads |
| `serializers.ModelSerializer` | Fields, validators and default `create()`/`update()` derived from a model |
| `serializers.HyperlinkedModelSerializer` | As `ModelSerializer`, but relations and identity are URLs (`url` field); needs `request` in context |
| `serializers.ListSerializer` | Created automatically for `many=True`; validates/serializes a list |

Instance state after construction:

| Attribute | Available when | Contains |
|---|---|---|
| `initial_data` | `data=` passed | The raw input |
| `validated_data` | After `is_valid()` returned `True` | Cleaned Python data |
| `errors` | After `is_valid()` | Field → list of `ErrorDetail`; `non_field_errors` for object-level |
| `data` | Instance passed, or after `save()`/validation | Primitive representation |
| `instance` | `instance=` passed or after `save()` | The model object |

**Read-only / write-only.** `read_only=True` fields appear only in output; `write_only=True`
fields only in input (e.g. `password`). `Meta.read_only_fields` marks auto-generated fields
read-only in a `ModelSerializer`; `Meta.extra_kwargs` adjusts generated fields.

## 3. Internal mechanics

**Lifecycle (write path):**

1. `Serializer(instance=None, data=..., partial=False, context={...}, many=False)`. With
   `many=True`, `__new__` returns a `ListSerializer` wrapping a child serializer.
2. `is_valid(raise_exception=False)` → `run_validation(initial_data)`:
   1. `validate_empty_values()` (handles missing/`None`).
   2. `to_internal_value()` — for each writable field: `field.run_validation()` (type
      conversion + field validators), then `validate_<field_name>()`. Errors are collected for all
      fields; if any exist a `ValidationError` is raised and object-level validation is skipped.
   3. `run_validators()` — serializer-level validators (`Meta.validators`, e.g. unique-together).
   4. `validate(attrs)` — object-level rules.
3. `save(**kwargs)` — merges `kwargs` into `validated_data`, then calls `create(validated_data)`
   if `instance` is `None`, otherwise `update(instance, validated_data)`. Asserts `is_valid()`
   was called and that there were no errors.
4. `.data` — calls `to_representation(instance)`: for each readable field, `field.get_attribute()`
   (follows `source`) then `field.to_representation()`.

Details of ordering and error structure: [fields-and-validation.md](fields-and-validation.md).

**`ModelSerializer` generation.** At first access to `.fields`, `get_fields()` maps each model
field via `serializer_field_mapping`, builds relational fields
(`PrimaryKeyRelatedField` by default), copies `max_length`, `choices`, `null`→`allow_null`,
`blank`→`allow_blank`/`required=False`, `editable=False`→read-only, and adds `UniqueValidator`/
`UniqueTogetherValidator` from `unique`/`unique_together` — and, (DRF ≥ 3.15), from
`UniqueConstraint`s (**VERIFY** coverage of conditional/expression constraints for your release).
Inspect the result with `print(repr(OrderSerializer()))`.

**Default `create()`/`update()`.** `ModelSerializer.create()` pops many-to-many data, calls
`Model._default_manager.create(**validated_data)`, then sets M2M values. `update()` sets
attributes, `save()`s, then sets M2M. Neither calls `Model.full_clean()`. Both raise an assertion
error if the serializer has writable nested serializers or writable dotted-`source` fields.

**`partial=True`.** Fields missing from input are skipped (`required` ignored); field defaults
are not applied to missing fields in partial updates. `validate()` receives only supplied keys.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Because |
|---|---|---|
| `ModelSerializer` vs `Serializer` for input | `ModelSerializer` for single-model CRUD with no side effects; `Serializer` + service once a write spans models, has side effects or is a command | Generated `create()` is convenient until the use case outgrows one `INSERT` |
| One serializer vs input/output pair | Separate input and output serializers when shapes differ (write `product_id`, read nested `product`) | Avoids `read_only`/`write_only` sprawl and conditional fields |
| `fields` list vs `__all__`/`exclude` | Always an explicit `fields` list | New model columns must not leak automatically |
| `depth = n` | Avoid; declare nested serializers explicitly | `depth` exposes every field of related models and is read-only |
| `SerializerMethodField` | Only for data already loaded (annotation/prefetch) | It runs per object; a query inside it is an N+1 |
| `HyperlinkedModelSerializer` | Public, discoverable APIs that commit to hypermedia | Requires request context and URL names; IDs are simpler for most clients |

## 5. Django/DRF implementation patterns

**Output (list/detail) serializer:**

```python
class OrderOutputSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    line_count = serializers.IntegerField(read_only=True)        # from .annotate()

    class Meta:
        model = Order
        fields = ["id", "reference", "status", "total", "customer_name", "line_count", "created_at"]
        read_only_fields = fields

class OrderDetailOutputSerializer(OrderOutputSerializer):       # inheritance extends Meta.fields
    lines = OrderLineOutputSerializer(many=True, read_only=True)

    class Meta(OrderOutputSerializer.Meta):
        fields = OrderOutputSerializer.Meta.fields + ["lines", "shipping_address"]
        read_only_fields = fields
```

**Create (ModelSerializer is enough — single model, no side effects):**

```python
class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "sku", "name", "price", "is_active"]
        read_only_fields = ["id"]

serializer = ProductSerializer(data=request.data, context={"request": request})
serializer.is_valid(raise_exception=True)
product = serializer.save(organization=request.user.organization)   # server-owned field
return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)
```

**Create via service (plain input serializer):**

```python
class OrderCreateInputSerializer(serializers.Serializer):
    class LineInput(serializers.Serializer):
        product_id = serializers.IntegerField(min_value=1)
        quantity = serializers.IntegerField(min_value=1, max_value=1000)

    lines = LineInput(many=True, allow_empty=False, max_length=200)   # max_length: DRF ≥ 3.15, VERIFY
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)

payload = OrderCreateInputSerializer(data=request.data)
payload.is_valid(raise_exception=True)
order = place_order(customer=request.user.customer, **payload.validated_data)
```

**Update and partial update:**

```python
serializer = ProductSerializer(product, data=request.data)                 # PUT
serializer = ProductSerializer(product, data=request.data, partial=True)   # PATCH
serializer.is_valid(raise_exception=True)
serializer.save()
```

**List:**

```python
ProductSerializer(page_of_products, many=True).data   # ListSerializer of ProductSerializer
```

**Context** — views pass `{"request", "format", "view"}` via `get_serializer_context()`; add
your own by overriding it. Read with `self.context["request"]`. Never rely on context keys being
present in a serializer used outside a view (tests, tasks) without a guard.

**`SerializerMethodField` reading prefetched data:**

```python
class CustomerOutputSerializer(serializers.ModelSerializer):
    open_order_count = serializers.SerializerMethodField()

    def get_open_order_count(self, obj) -> int:   # return annotation -> schema tools infer type
        return obj.open_order_count                 # annotated in the selector, no query

    class Meta:
        model = Customer
        fields = ["id", "name", "open_order_count"]
```

**Custom representation** — adjust output without new fields:

```python
def to_representation(self, instance):
    data = super().to_representation(instance)
    if not self.context["request"].user.has_perm("orders.view_margin"):
        data.pop("margin", None)
    return data
```

**Dynamic field selection** (only when clients really need sparse fieldsets; allow-list it):

```python
class DynamicFieldsMixin:
    def __init__(self, *args, fields=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fields is not None:
            for name in set(self.fields) - set(fields):
                self.fields.pop(name)
```

Dynamic fields complicate schemas and caching; prefer a dedicated list serializer.

## 6. Production use cases

- **List vs detail**: `get_serializer_class()` returns a slim serializer for `list` and a
  richer one for `retrieve`.
- **Write-then-read**: accept `OrderCreateInputSerializer`, respond with
  `OrderDetailOutputSerializer` of the created order (re-fetched via the selector so prefetching
  applies).
- **Permission-dependent fields**: drop sensitive fields in `to_representation()` based on
  the request user — and test it.
- **Large exports**: bypass serializers with `values()` and a streaming response when
  measurement shows serializer overhead dominates.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| One declarative place for input shape and output shape | Easy to accumulate business logic in `create()`/`validate()` |
| `ModelSerializer` removes boilerplate and derives validators | Generated behaviour is implicit; inspect with `repr()` |
| Structured, field-keyed error output | Per-object, per-field Python overhead |
| Works with schema generators for documentation | Dynamic serializers defeat static schema generation |

## 8. Security and performance considerations

- **Mass assignment:** explicit `fields`; server-owned attributes (`organization`, `owner`,
  `status`, `is_staff`) are read-only and set via `save(**kwargs)` or the service.
- `HiddenField(default=serializers.CurrentUserDefault())` sets the owner from context without
  exposing a field; requires `request` in context.
- Bound every input: `max_length`, `max_value`, list `max_length`, `allow_empty=False`.
- Each `source="a.b"` and nested serializer requires `select_related`/`prefetch_related` in the
  view queryset; check with query-count tests
  ([django-orm query-optimization](../../django-orm/references/query-optimization.md)).
- `serializer.data` on an unsaved, validated serializer represents `validated_data`, not the DB
  state — re-serialize the saved instance for the response.

## 9. Common mistakes and anti-patterns

- `fields = "__all__"` or `exclude` — leaks new columns.
- Accessing `serializer.data` before `is_valid()` when `data=` was passed (assertion error).
- Calling `serializer.save()` without `is_valid()`; or `is_valid()` without
  `raise_exception=True` and then ignoring `errors`.
- Emails, HTTP calls or multi-model writes inside `create()`/`update()`.
- `SerializerMethodField` that runs a query per row.
- Overriding `save()` on the serializer instead of `create()`/`update()`.
- Re-using an output serializer as input, then adding `read_only`/`write_only` flags until it
  is unreadable.
- Expecting `ModelSerializer` to run `Model.clean()`/`full_clean()` — it does not.

## 10. Troubleshooting and validation

| Symptom | Cause / check |
|---|---|
| `AssertionError: You must call .is_valid()` | `.data`/`.save()` before validation |
| `The .create() method does not support writable nested fields` | Nested serializer not `read_only`; implement explicit create or use a service |
| Field missing from output | Not in `Meta.fields`, or `write_only` |
| Field silently ignored on input | It is `read_only` (or in `read_only_fields`) |
| `KeyError: 'request'` | Serializer instantiated without context |
| `ImproperlyConfigured` for hyperlinked field | `request` missing from context or wrong `view_name` |

Inspect generated fields: `python manage.py shell -c "from orders.api import *; print(repr(OrderOutputSerializer()))"`.
Unit-test serializers directly ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [fields-and-validation.md](fields-and-validation.md) — field options, validators, ordering
- [nested-and-relational-serializers.md](nested-and-relational-serializers.md) — relations, nested writes
- [views-and-viewsets.md](views-and-viewsets.md) — `get_serializer_class()`, `perform_create()`
- [django-architecture service-layer](../../django-architecture/references/service-layer.md)
- [django-orm model-design](../../django-orm/references/model-design.md) — model validation

## 12. Official documentation and external references

- Serializers: <https://www.django-rest-framework.org/api-guide/serializers/>
- Serializer fields: <https://www.django-rest-framework.org/api-guide/fields/>
- Validators: <https://www.django-rest-framework.org/api-guide/validators/>
- DRF 3.15 announcement (UniqueConstraint support): <https://www.django-rest-framework.org/community/3.15-announcement/>
