# Parsers, Renderers, and Content Negotiation

> **Scope.** Owns: DRF parsers (`JSONParser`, `FormParser`, `MultiPartParser`,
> `FileUploadParser`, custom), file uploads at the API layer, renderers (`JSONRenderer`,
> `BrowsableAPIRenderer`, others, custom), content negotiation, `Accept`/`Content-Type` handling,
> media types, format suffixes, parse errors and file responses.
> Does not cover: storage backends, `FileField` and media serving ([django-core file-handling](../../django-core/references/file-handling.md)),
> large-file processing pipelines ([django-data-processing](../../django-data-processing/SKILL.md)),
> report generation (PDF/Excel) ([django-report-generation](../../django-report-generation/SKILL.md)),
> or Django template rendering for HTML pages ([django-core urls-views](../../django-core/references/urls-views.md)).

## 1. Purpose and scope

Parsers decide how request bodies become `request.data`; renderers decide how `Response.data`
becomes bytes. Content negotiation selects the renderer. DRF rendering is **not** Django template
rendering — a `Response` carries data, and a renderer serializes it per request.

## 2. Core concepts

**Parsers** (`rest_framework.parsers`) — selected by request `Content-Type`:

| Parser | Media type | `request.data` |
|---|---|---|
| `JSONParser` | `application/json` | dict/list |
| `FormParser` | `application/x-www-form-urlencoded` | `QueryDict` |
| `MultiPartParser` | `multipart/form-data` | `QueryDict` incl. files |
| `FileUploadParser` | `*/*` (raw body) | `{"file": UploadedFile}`; needs `filename` URL kwarg or `Content-Disposition` |

DRF default `DEFAULT_PARSER_CLASSES`: JSON, Form, MultiPart.

**Renderers** (`rest_framework.renderers`) — selected by negotiation:

| Renderer | Media type | Use |
|---|---|---|
| `JSONRenderer` | `application/json` | Default API output |
| `BrowsableAPIRenderer` | `text/html` | Developer HTML UI with forms |
| `TemplateHTMLRenderer` | `text/html` | Render a Django template with `Response.data` as context |
| `StaticHTMLRenderer` | `text/html` | Pre-rendered HTML strings |
| `HTMLFormRenderer`, `AdminRenderer` | `text/html` | Forms / admin-style views |

DRF default `DEFAULT_RENDERER_CLASSES`: JSON, Browsable API. Third-party renderers exist for
XML, YAML, CSV, MessagePack (e.g. `djangorestframework-csv`) — not built in.

## 3. Internal mechanics

**Parser selection** (lazy, on first `request.data` access): `Request._parse()` takes the
request's `Content-Type` and asks the negotiator's `select_parser()` for the first parser whose
`media_type` matches. No match → `UnsupportedMediaType` (415). A parser raising → `ParseError`
(400). An empty body yields empty data. Upload limits come from Django settings
(`DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`, `DATA_UPLOAD_MAX_NUMBER_FIELDS`)
— note `DATA_UPLOAD_MAX_MEMORY_SIZE` does not include file upload data.

**Renderer selection** (`DefaultContentNegotiation.select_renderer`, during `initial()`):

1. If a format override is present — URL format suffix (`/orders.json`) or the
   `URL_FORMAT_OVERRIDE` query param (default `?format=json`) — pick the renderer with that
   `format`; none → 404.
2. Otherwise parse `Accept` into precedence groups (more specific media types first, then by
   order given) and pick the first renderer, in `renderer_classes` order, that matches.
3. No `Accept`, or `*/*` → the first renderer in the list.
4. No match → `NotAcceptable` (406).

The response gets `Vary: Accept`; `JSONRenderer` uses `UNICODE_JSON`, `COMPACT_JSON` and
`STRICT_JSON` settings (strict rejects `NaN`/`Infinity`).

**Format suffixes.** `format_suffix_patterns(urlpatterns)` adds `\.(?P<format>[a-z0-9]+)/?$`
variants; `DefaultRouter` does this automatically. Views receive `format=None` kwarg.

## 4. Engineering decisions and trade-offs

| Decision | Guidance |
|---|---|
| Global parsers | JSON only; add `MultiPartParser`/`FormParser` per upload view |
| Global renderers | JSON only in production; add Browsable API under `DEBUG` or for internal APIs |
| Upload style | Multipart for files + metadata from browsers; raw `FileUploadParser` for single-file PUT; pre-signed direct-to-storage uploads for large files |
| CSV/Excel output | Third-party renderer for simple tabular data; a dedicated export view/job for large or formatted reports |
| Format suffixes | Disable unless clients rely on them — `Accept` is the HTTP mechanism |
| Custom media types (`application/vnd.acme.v2+json`) | Only with `AcceptHeaderVersioning` or real representation differences |

## 5. Django/DRF implementation patterns

**Upload endpoint with validation:**

```python
class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField(max_length=200, allow_empty_file=False)
    title = serializers.CharField(max_length=200)

    def validate_file(self, f):
        if f.size > 10 * 1024 * 1024:
            raise serializers.ValidationError("Max 10 MB.", code="file_too_large")
        if f.content_type not in {"application/pdf", "image/png", "image/jpeg"}:  # client-declared
            raise serializers.ValidationError("Unsupported type.", code="unsupported_type")
        return f

class DocumentUploadView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request):
        payload = DocumentUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        doc = store_document(owner=request.user, **payload.validated_data)   # service; storage API
        return Response(DocumentOutputSerializer(doc).data, status=status.HTTP_201_CREATED)
```

`content_type` and file name are client-supplied; verify content (magic bytes, image decoding via
`ImageField`/Pillow, antivirus) where it matters — see [django-security-engineering](../../django-security-engineering/SKILL.md).

**Raw single-file upload:**

```python
class AvatarView(APIView):
    parser_classes = [FileUploadParser]

    def put(self, request, filename):               # path("avatar/<str:filename>/", ...)
        update_avatar(user=request.user, upload=request.data["file"])
        return Response(status=status.HTTP_204_NO_CONTENT)
```

**Custom parser:**

```python
class PlainTextParser(parsers.BaseParser):
    media_type = "text/plain"

    def parse(self, stream, media_type=None, parser_context=None):
        return stream.read().decode((parser_context or {}).get("encoding", "utf-8"))
```

**Custom renderer:**

```python
class NDJSONRenderer(renderers.BaseRenderer):
    media_type = "application/x-ndjson"
    format = "ndjson"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        rows = data if isinstance(data, list) else [data]
        return "\n".join(json.dumps(r, cls=encoders.JSONEncoder) for r in rows).encode()
```

**File download** — bypass renderers with Django's `FileResponse`:

```python
class InvoicePdfView(APIView):
    def get(self, request, pk):
        invoice = get_object_or_404(invoice_list(user=request.user), pk=pk)
        return FileResponse(invoice.pdf.open("rb"), as_attachment=True,
                            filename=f"invoice-{invoice.number}.pdf", content_type="application/pdf")
```

Returning `HttpResponseBase` subclasses from DRF views is supported; negotiation still runs (a
strict `Accept` may 406 first — set `renderer_classes` to include a matching renderer or a
pass-through renderer for such views; **VERIFY** behaviour for your clients). For large files,
redirect to a signed storage URL instead of streaming through Python.

## 6. Production use cases

- **Mobile photo upload**: `MultiPartParser` on one view, `ImageField` validation, storage via
  the project's storage backend.
- **Large media**: pre-signed URL issued by the API; client uploads directly to object storage;
  API receives a completion callback.
- **CSV export for small result sets**: third-party CSV renderer on a filtered list endpoint.
- **Partner integration requiring XML**: third-party XML parser/renderer on the partner views only.

## 7. Advantages and disadvantages

| Advantages | Disadvantages |
|---|---|
| One view can serve several representations | Each extra renderer is surface area and test burden |
| Parsers make `request.data` uniform | Multipart uploads go through the Python process |
| Pluggable custom media types | Format suffixes/`?format=` add alternative, less visible URLs |

## 8. Security and performance considerations

- JSON-only parsers in production reduce attack surface (form/multipart only where needed).
- The browsable API renders forms (and may evaluate related-field querysets) — disable in
  production or restrict.
- Enforce upload size in Django settings and the reverse proxy (`client_max_body_size`), not only
  in serializers (the body is read before serializer validation).
- Never trust `Content-Type` or file names; sanitise names and store under generated keys.
- `TemplateHTMLRenderer` escapes via Django templates; custom HTML renderers must escape output.

## 9. Common mistakes and anti-patterns

- Expecting files in `request.data` with `JSONParser` only (415 or empty).
- Sending JSON with `Content-Type: text/plain` (415) or forgetting the header in tests (use
  `format="json"`).
- Setting `Content-Type: multipart/form-data` manually without a boundary.
- Confusing `Response(template_name=...)` with Django's `render()`.
- Base64-encoding large files inside JSON payloads.

## 10. Troubleshooting and validation

| Symptom | Cause |
|---|---|
| 415 Unsupported Media Type | No parser for `Content-Type` on this view |
| 400 "JSON parse error" | Malformed body, wrong encoding |
| 406 Not Acceptable | `Accept` not satisfiable by `renderer_classes` |
| HTML returned to an API client | Browsable API first in list and client sends `Accept: text/html` |
| `RequestDataTooBig` / 400 on upload | `DATA_UPLOAD_MAX_MEMORY_SIZE`/field-count limits |

Test with `APIClient.post(url, {"file": fp, "title": "x"}, format="multipart")` and assert
415/406 paths ([drf-testing-and-debugging.md](drf-testing-and-debugging.md)).

## 11. Related references and skills

- [request-response-lifecycle.md](request-response-lifecycle.md) — when parsing/negotiation run
- [fields-and-validation.md](fields-and-validation.md) — `FileField`/`ImageField`
- [api-versioning-and-documentation.md](api-versioning-and-documentation.md) — media-type versioning
- [django-core file-handling](../../django-core/references/file-handling.md)
- [django-security-engineering](../../django-security-engineering/SKILL.md), [django-data-processing](../../django-data-processing/SKILL.md),
  [django-report-generation](../../django-report-generation/SKILL.md)

## 12. Official documentation and external references

- Parsers: <https://www.django-rest-framework.org/api-guide/parsers/>
- Renderers: <https://www.django-rest-framework.org/api-guide/renderers/>
- Content negotiation: <https://www.django-rest-framework.org/api-guide/content-negotiation/>
- Format suffixes: <https://www.django-rest-framework.org/api-guide/format-suffixes/>
- Django file uploads: <https://docs.djangoproject.com/en/5.2/topics/http/file-uploads/>
