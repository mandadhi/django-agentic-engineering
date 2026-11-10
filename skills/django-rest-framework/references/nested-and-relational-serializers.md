# Nested and Relational Serializers

> **Scope.** Owns: DRF relational fields (`PrimaryKeyRelatedField`, `StringRelatedField`,
> `SlugRelatedField`, `HyperlinkedRelatedField`, `HyperlinkedIdentityField`), nested serializers
> (read-only and writable), FK/O2O/M2M and reverse relationship representation, nested create and
> update, relationship validation and authorisation of related IDs, and relationship query cost.
> Does not cover: relationship modelling (`on_delete`, through models —
> [django-orm model-relationships](../../django-orm/references/model-relationships.md)),
> `select_related`/`prefetch_related` mechanics
> ([django-orm query-optimization](../../django-orm/references/query-optimization.md)),
> or nested URL routing ([routers-and-url-design.md](routers-and-url-design.md)).

## 1. Purpose and scope

Relationships are where API design, authorisation and query performance collide. This reference
explains how to represent related objects, how to accept them on input safely, and why writable
nested serializers should be the exception.

## 2. Core concepts

| Field | Output | Input | Notes |
|---|---|---|---|
| `PrimaryKeyRelatedField(queryset=...)` | `42` | `42` | `ModelSerializer` default for FK/M2M; `queryset` required unless `read_only` |
| `SlugRelatedField(slug_field="sku", queryset=...)` | `"SKU-1"` | `"SKU-1"` | Slug must be unique (scoped) |
| `StringRelatedField()` | `str(obj)` | — | Always read-only |
| `HyperlinkedRelatedField(view_name="product-detail", queryset=...)` | URL | URL | Needs `request` in context |
| `HyperlinkedIdentityField(view_name=...)` | URL of the object itself | — | The `url` field of hyperlinked serializers |
| Nested serializer `ProductSerializer()` | Object | Dict (only with custom write code) | Read-only unless you implement writes |

`many=True` on a relational field produces a `ManyRelatedField` (list of values).

**Reverse relationships** (`customer.orders`) are not included by `ModelSerializer`
automatically; add the `related_name` to `fields` or declare the field explicitly.

**Choosing a representation.**

| Representation | Choose when |
|---|---|
| ID | Default for input; clients that cache related resources separately |
| Slug | Human-meaningful, stable natural keys (SKU, country code) |
| Hyperlink | Hypermedia-driven public APIs |
| Nested object (read) | Clients always need a small subset of the related data in the same view |
| Nested object (write) | A true aggregate the client creates as one unit (order + lines), and you own the write code |

## 3. Internal mechanics

- On input, `RelatedField.to_internal_value()` runs `self.get_queryset().get(pk=value)` (or the
  slug/URL equivalent): one query per value. For `many=True`, one query per element.
- `get_queryset()` defaults to the `queryset` argument; override it on a field subclass to scope
  by request.
- On output, `get_attribute()` follows the relation. `PrimaryKeyRelatedField` has an optimisation
  that reads `<fk>_id` without loading the related object; slug, string, hyperlinked and nested
  representations load the object — one query per row unless `select_related`/`prefetch_related`
  was applied.
- The browsable API and `OPTIONS` metadata may iterate a related field's queryset to build choices
  (bounded by `html_cutoff` in forms). Another reason to disable the browsable API in production.
- `ModelSerializer.create()`/`update()` refuse writable nested serializers with an assertion; M2M
  `PrimaryKeyRelatedField(many=True)` values are set via `.set()` after save.

## 4. Engineering decisions and trade-offs

**Writable nested serializers** look attractive and carry real costs:

- You must hand-write `create()`/`update()`: matching children by ID, deciding whether missing
  children are deleted, handling reordering.
- Partial updates of nested lists are ambiguous (does an omitted child mean "unchanged" or
  "delete"?).
- Errors and transactions span several models; the whole graph must be in one `atomic` block.
- Authorisation of each nested ID must be checked.

Prefer, in order:

1. **IDs on input, nested on output.** `product_id` in, `product: {...}` out.
2. **Nested input to a plain serializer, written by a service** in one transaction.
3. **Sub-resource endpoints** for children with their own lifecycle
   (`/orders/{id}/lines/`).
4. Writable nested `ModelSerializer` with custom `create()`/`update()` only for small,
   tightly-bound aggregates.

## 5. Django/DRF implementation patterns

**ID in, nested out:**

```python
class OrderLineOutputSerializer(serializers.ModelSerializer):
    product = ProductSummarySerializer(read_only=True)
    class Meta:
        model = OrderLine
        fields = ["id", "product", "quantity", "unit_price"]

class OrderLineInputSerializer(serializers.Serializer):
    product = TenantScopedPrimaryKeyRelatedField(queryset=Product.objects.filter(is_active=True))
    quantity = serializers.IntegerField(min_value=1, max_value=1000)
```

**Scoped related field** — prevents linking objects the caller cannot see:

```python
class TenantScopedPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(organization=self.context["request"].user.organization)
```

**Validating many IDs in one query** (avoid per-element lookups):

```python
class AssignTagsInputSerializer(serializers.Serializer):
    tag_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=100)

    def validate_tag_ids(self, ids):
        org = self.context["request"].user.organization
        found = set(Tag.objects.filter(organization=org, pk__in=ids).values_list("pk", flat=True))
        missing = sorted(set(ids) - found)
        if missing:
            raise serializers.ValidationError(f"Unknown tags: {missing}", code="unknown_tag")
        return ids
```

**Nested create via service:**

```python
class OrderCreateInputSerializer(serializers.Serializer):
    lines = OrderLineInputSerializer(many=True, allow_empty=False)
    shipping_address_id = serializers.IntegerField()

# view
payload = OrderCreateInputSerializer(data=request.data, context=self.get_serializer_context())
payload.is_valid(raise_exception=True)
order = place_order(customer=request.user.customer, **payload.validated_data)

# orders/services.py
@transaction.atomic
def place_order(*, customer, lines, shipping_address_id):
    order = Order.objects.create(customer=customer, shipping_address_id=shipping_address_id)
    OrderLine.objects.bulk_create(
        [OrderLine(order=order, product=l["product"], quantity=l["quantity"],
                   unit_price=l["product"].price) for l in lines]
    )
    return order
```

**Writable nested update (small aggregate, explicit replace semantics):**

```python
class RecipeSerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True)

    class Meta:
        model = Recipe
        fields = ["id", "title", "steps"]

    @transaction.atomic
    def update(self, instance, validated_data):
        steps = validated_data.pop("steps", None)
        instance = super().update(instance, validated_data)
        if steps is not None:                       # PUT/PATCH with steps => replace all
            instance.steps.all().delete()
            Step.objects.bulk_create(Step(recipe=instance, **s) for s in steps)
        return instance
```

Document the replace semantics in the schema description.

**M2M update** through the primary-key field:

```python
class ArticleSerializer(serializers.ModelSerializer):
    tags = TenantScopedPrimaryKeyRelatedField(many=True, queryset=Tag.objects.all())
    class Meta:
        model = Article
        fields = ["id", "title", "tags"]     # ModelSerializer.update() calls article.tags.set(...)
```

For M2M with a `through` model carrying extra fields, write through the through-model explicitly.

**Queryset matching the serializer:**

```python
def get_queryset(self):
    return (Order.objects.for_user(self.request.user)
            .select_related("customer")
            .prefetch_related(Prefetch("lines", queryset=OrderLine.objects.select_related("product"))))
```

## 6. Production use cases

- **Order with lines**: lines written atomically by a service; lines editable later via
  `/orders/{id}/lines/` sub-resource.
- **Product catalogue**: `SlugRelatedField(slug_field="code")` for category input, nested
  category summary on output.
- **Tagging**: list of tag IDs validated in one query, scoped to the tenant.
- **Public hypermedia API**: `HyperlinkedModelSerializer` with `HyperlinkedIdentityField`.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| IDs | Cheap, simple, cache-friendly | Client makes more requests to render names |
| Nested read | One request renders a view | Payload growth, prefetch required, coupling to related shape |
| Hyperlinks | Discoverable, decouples URL structure | Verbose, needs request context, harder for clients |
| Writable nested | One call creates an aggregate | Custom code, ambiguous partial updates, wider transactions |

## 8. Security and performance considerations

- **Every writable relation is an authorisation decision.** An unscoped
  `PrimaryKeyRelatedField(queryset=Project.objects.all())` lets a user attach another tenant's
  project — and the "does not exist" vs "invalid" error difference can disclose existence.
- Never expose nested serializers that include sensitive fields of related models (e.g.
  `UserSerializer` with `email` nested inside a public comment).
- One query per related object per row without prefetching; assert query counts on list
  endpoints.
- `StringRelatedField` calls `__str__`, which may itself touch further relations.
- Deep nesting multiplies payload size; cap depth at one or two levels.

## 9. Common mistakes and anti-patterns

- `depth = 2` on a `ModelSerializer` in a public API.
- Related field querysets that are not scoped to the user/tenant.
- Writable nested serializers without `transaction.atomic`.
- `SerializerMethodField` that calls `obj.related.filter(...)` (bypasses prefetch cache).
- Prefetching with a different filter than the serializer needs, then filtering in Python.
- Reverse relations declared without `read_only=True` and without a write strategy.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| `Relational field must provide a queryset argument` | Writable related field without `queryset`/`get_queryset()` |
| `Invalid pk "7" - object does not exist.` for an object that exists | Field queryset scoped (correctly) or filtered (`is_active`) |
| N queries for a list of N | Missing `select_related`/`prefetch_related` for a nested/slug/string field |
| `.create() does not support writable nested fields` | Nested serializer not read-only; implement explicit writes |
| Reverse relation missing from output | Not listed in `fields` |

Use `django.test.utils.CaptureQueriesContext` or `assertNumQueries` around a list request
([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [serializers-and-model-serializers.md](serializers-and-model-serializers.md)
- [fields-and-validation.md](fields-and-validation.md)
- [permissions-and-access-control.md](permissions-and-access-control.md) — writable relationship authorisation
- [routers-and-url-design.md](routers-and-url-design.md) — sub-resource URLs
- [django-orm model-relationships](../../django-orm/references/model-relationships.md)
- [django-orm query-optimization](../../django-orm/references/query-optimization.md)
- [django-orm transactions-and-concurrency](../../django-orm/references/transactions-and-concurrency.md)

## 12. Official documentation and external references

- Serializer relations: <https://www.django-rest-framework.org/api-guide/relations/>
- Writable nested serializers: <https://www.django-rest-framework.org/api-guide/serializers/#writable-nested-representations>
- Django `prefetch_related`: <https://docs.djangoproject.com/en/5.2/ref/models/querysets/#prefetch-related>
- Third-party (not part of DRF): drf-writable-nested — <https://github.com/beda-software/drf-writable-nested> (evaluate maintenance before adopting)
