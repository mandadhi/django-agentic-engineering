# HTMX with Django

## Setup

- Vendor `htmx.min.js` into `static/vendor/` (pin the version) or load it from the project's
  approved CDN with a Subresource Integrity hash.
- Optional: `django-htmx` adds `request.htmx` and response helpers; plain header checks work
  without it:

```python
def is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"
```

## CSRF

```django
<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
```

Every non-GET HTMX request then carries the token. Never `csrf_exempt` HTMX endpoints.

## One view, two responses

```python
@login_required
@vary_on_headers("HX-Request")
def contact_list(request):
    page = Paginator(contacts_for(request.user), 25).get_page(request.GET.get("page"))
    template = "contacts/list.html#rows" if is_htmx(request) else "contacts/list.html"
    return render(request, template, {"page": page})
```

Boosted navigation (`hx-boost`) sends `HX-Request` too but expects a full page; check
`HX-Boosted` if you use it.

## Patterns

**Live search**

```django
<input type="search" name="q" value="{{ request.GET.q }}"
       hx-get="{% url 'contacts:list' %}" hx-trigger="input changed delay:300ms, search"
       hx-target="#rows" hx-push-url="true" hx-indicator="#spinner">
```

**Inline edit**

```django
<tr id="contact-{{ c.pk }}">
  <td>{{ c.name }}</td>
  <td><button hx-get="{% url 'contacts:edit' c.pk %}" hx-target="closest tr" hx-swap="outerHTML">Edit</button></td>
</tr>
```

The edit view returns a row containing a form that `hx-post`s back; on success it returns the
display row, on error the form row with status 422.

**Delete with confirmation**

```django
<button hx-delete="{% url 'contacts:delete' c.pk %}" hx-target="closest tr" hx-swap="outerHTML swap:300ms"
        hx-confirm="Delete {{ c.name }}?">Delete</button>
```

Return `HttpResponse("")` (200) to remove the row; 204 means "do nothing" to HTMX.

**Infinite scroll / load more**

```django
{% if page.has_next %}
<tr hx-get="?page={{ page.next_page_number }}" hx-trigger="revealed" hx-swap="outerHTML">
  <td>Loading…</td></tr>
{% endif %}
```

**Out-of-band updates** (e.g. a counter or flash message alongside the main swap):

```django
<div id="messages" hx-swap-oob="true">{% include "components/_messages.html" %}</div>
```

## Response headers

| Header | Use |
|---|---|
| `HX-Redirect` | full-page redirect after an HTMX POST (e.g. after create) |
| `HX-Location` | client-side navigation without full reload |
| `HX-Trigger` | fire a client event (`{"contactSaved": {"id": 3}}`) other elements listen to |
| `HX-Retarget` / `HX-Reswap` | change where an error response goes |
| `HX-Push-Url` | update the URL from the server |

```python
response = HttpResponse(status=204)
response["HX-Trigger"] = json.dumps({"contactSaved": {"id": contact.pk}})
return response
```

## Errors

- By default HTMX does not swap 4xx/5xx responses. For validation errors either return 422 and
  configure `htmx.config.responseHandling` to swap 422, or retarget with `HX-Retarget`.
- Session expiry: return `HX-Redirect` to the login URL when an HTMX request is unauthenticated
  (a small middleware can do this for all HTMX requests).

## History and accessibility

- Use `hx-push-url` for navigational changes (filters, pages) so back/forward and bookmarking
  work; the view must render the full page for a non-HTMX GET to the same URL.
- Announce swapped regions with `aria-live="polite"`; move focus to the first error or the new
  content after a swap.

## Security

- HTMX endpoints are normal views: same authentication, authorisation and scoped querysets.
- Escape everything in fragments (auto-escaping covers this) — HTMX inserts HTML as-is.
- Avoid `hx-on`/inline handlers if CSP forbids inline script; use `htmx.config.allowEval = false`
  under a strict CSP.

## Official documentation

- https://htmx.org/docs/
- https://htmx.org/reference/#response_headers
- https://htmx.org/docs/#csrf-prevention
- https://django-htmx.readthedocs.io/
- https://docs.djangoproject.com/en/stable/ref/csrf/#setting-the-token-on-the-ajax-request
