# Static Assets and Build Engineering

> **Scope:** How CSS/JS/fonts/images get from source to the browser in a Django project: the staticfiles pipeline (finders, `collectstatic`, `STORAGES`, `ManifestStaticFilesStorage`), WhiteNoise and CDN delivery, fingerprinting and cache invalidation, source maps, Vite and Tailwind CLI pipelines, dev vs prod, npm/lockfiles, Docker and CI ordering. Browser-side performance budgets belong to [frontend-performance.md](frontend-performance.md); HTTP/CDN tuning from the server side belongs to [http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md); user uploads (`MEDIA_*`) belong to [file-handling.md](../../django-core/references/file-handling.md).

## 1. Purpose and scope

Use this reference when setting up or debugging asset delivery: adding a bundler, choosing
between "vendored files only", Tailwind standalone CLI and Vite, wiring hashed filenames into
templates, configuring WhiteNoise or a CDN, writing the Dockerfile/CI steps, or fixing 404s and
stale caches after deploys.

Principles:

- **Start without a bundler.** Vendored `htmx.min.js` + hand-written ES modules + one CSS file
  served by Django staticfiles is a complete, production-grade setup.
- **Every production asset URL is content-hashed** and served with a one-year immutable cache;
  HTML is never cached long.
- **Builds are reproducible:** lockfile + `npm ci`, pinned tool versions, build in CI/Docker,
  never commit build output unless deliberately chosen.
- **Everything bundled is public.** No secrets in `VITE_*` or any env var a bundler inlines.

## 2. Core concepts

| Setting / term | Meaning |
|---|---|
| `STATIC_URL` | URL prefix for static files (`"static/"` or a CDN URL `https://cdn.example.com/static/`) |
| `STATIC_ROOT` | Single directory `collectstatic` writes to; served in prod. Never a source dir |
| `STATICFILES_DIRS` | Extra source dirs (project `assets/`, build output dir) |
| App `static/` dirs | Per-app sources, namespaced `app/static/<app>/…` |
| Finders | Locate sources: `FileSystemFinder` (`STATICFILES_DIRS`), `AppDirectoriesFinder` (app `static/`) |
| `STORAGES["staticfiles"]` | Backend that stores collected files and computes URLs (Django ≥ 4.2; `STATICFILES_STORAGE` removed in 5.1) |
| Fingerprinting | Content hash in filename (`app.3f9a1c2b7d4e.css`) → safe infinite caching |
| Manifest | JSON map logical name → hashed name (Django's `staticfiles.json`; Vite's `manifest.json`) |
| `{% static %}` | Template tag asking the storage for the URL (hashed in prod) |

## 3. Internal mechanics

### Finders and `collectstatic`

1. `collectstatic` asks each finder in `STATICFILES_FINDERS` order for all files. For duplicate
   relative paths, **the first found wins** (`STATICFILES_DIRS` before apps, apps in
   `INSTALLED_APPS` order) — the reason app files are namespaced under `static/<app>/`.
2. Files are copied (or symlinked with `--link`) into `STATIC_ROOT` via the staticfiles storage.
   Unchanged files (by modification time) are skipped unless `--clear`.
3. If the storage implements `post_process()` (manifest storages do), it runs after copying.

`python manage.py findstatic css/app.css --verbosity 2` shows which file wins and where it searched.

### `ManifestStaticFilesStorage` post-processing

- For each file, computes an MD5-based hash of the content (12 hex chars) and saves
  `name.<hash>.ext` **in addition to** the original.
- For CSS, rewrites `url(...)` and `@import` references to hashed names; this is iterative
  (`max_post_process_passes`) because hashing a referenced file changes the referencing file's
  hash. References that cannot be resolved raise an error during `collectstatic`.
- Also rewrites `sourceMappingURL` comments in CSS and JS [VERIFY: versions — CSS ≥ 4.0/4.1, JS ≥
  4.1]. A referenced-but-missing `.map` file fails `collectstatic`.
- JS `import` statements are rewritten only if `support_js_module_import_aggregation = True` on a
  subclass (Django ≥ 4.2, off by default, experimental).
- Writes `STATIC_ROOT/staticfiles.json`. At runtime `{% static "css/app.css" %}` looks up this
  manifest — **only when `DEBUG = False`**; with `DEBUG = True` unhashed names are returned.
- `manifest_strict = True` (default): a name missing from the manifest raises `ValueError` at
  render time (often surfacing as a 500 on a page that referenced a typo'd path). Set
  `manifest_strict = False` on a subclass only if you accept silently-uncached URLs.

### Runtime serving

- **Development:** with `django.contrib.staticfiles` installed and `DEBUG = True`, `runserver`
  serves files directly from finders (no `collectstatic`). `runserver --nostatic` disables it;
  `--insecure` forces it with `DEBUG = False` (testing only).
- **Production:** Django does not serve static files itself. Options: WhiteNoise (third-party)
  middleware in the app process, reverse proxy (nginx/Caddy) serving `STATIC_ROOT`, or object
  storage/CDN.
- **WhiteNoise:** at startup scans `STATIC_ROOT`, serves files with correct headers; files it
  recognizes as hashed by Django's manifest get `Cache-Control: max-age=315360000, public,
  immutable`-style headers, others get a short max-age (`WHITENOISE_MAX_AGE`) [VERIFY: exact
  defaults]. `CompressedManifestStaticFilesStorage` precompresses `.gz` and `.br` (if `Brotli`
  is installed) and WhiteNoise negotiates `Accept-Encoding`.

### Vite build output

- Vite fingerprints its own outputs (`assets/main-BxY12z.js`) and, with `build.manifest: true`,
  writes `outDir/.vite/manifest.json` (Vite ≥ 5; `outDir/manifest.json` before)
  [VERIFY: for your Vite major]. Each manifest entry for a source path has `file`, `css[]`,
  `imports[]`, `dynamicImports[]`, `isEntry`.
- Chunks import each other with **relative URLs**. Because Django's manifest storage keeps the
  original filenames as well as Django-hashed copies, Vite's internal relative imports still
  resolve after `collectstatic`.
- Vite-hashed files are not recognized as immutable by WhiteNoise unless you configure
  `WHITENOISE_IMMUTABLE_FILE_TEST` (see §5).
- In dev, Vite serves source modules over its own HTTP server with HMR via `/@vite/client`; no
  files are written to disk.

### Tailwind build

Tailwind scans source files for class-name tokens and emits only matching CSS. v4 detects
sources automatically (respecting `.gitignore`) relative to the CSS entry; add Django templates
outside detection with `@source "../templates";`. v3 uses `content` globs in
`tailwind.config.js`. Classes built dynamically (`"bg-" + color`) are never generated.

## 4. Engineering decisions and trade-offs

### Pipeline choice

| Prefer | When |
|---|---|
| No build: vendored libs + native ES modules + plain CSS | HTMX/Alpine apps, small JS, team without Node expertise |
| Tailwind standalone CLI (no Node runtime) | Tailwind wanted, little/no npm JS; simplest CI/Docker |
| esbuild (single command) | A few JS entry points need bundling/minification, no HMR need |
| Vite | Many modules, TypeScript, React islands, npm dependencies, want HMR |
| Separate SPA deployment | Full React SPA on its own origin — see [django-react-integration.md](django-react-integration.md) |

### Serving

| Prefer | When |
|---|---|
| WhiteNoise (third-party) | PaaS/containers, no separate web server, moderate traffic; pair with a CDN for scale |
| nginx/Caddy serving `STATIC_ROOT` | You already run a reverse proxy on the same host/volume |
| Object storage + CDN (django-storages, third-party) | Multiple app servers without shared volume, very high traffic, global users |
| CDN in front of WhiteNoise (origin pull) | Easiest CDN: no upload step; hashed URLs make the CDN cache trivially correct |

### Double hashing (Vite + Django manifest)

| Option | Trade-off |
|---|---|
| Keep Django `ManifestStaticFilesStorage` over Vite output (default) | Consistent `{% static %}` for all assets; Vite files get a second hash; ensure manifest-tag resolves through `static()` |
| Plain `StaticFilesStorage` + rely on Vite hashes | No double hash, but non-Vite assets (images, vendored libs) lose fingerprinting |

### Source maps

| Policy | When |
|---|---|
| `sourcemap: "hidden"` + upload to error tracker, do not deploy `.map` | Default for closed-source apps |
| Public source maps | Open source or internal tools; easier debugging |
| None | Tiny unbundled JS (source is already readable) |

Source maps are not a security boundary — minified code is still public; never rely on
obscurity, and never put secrets in client code.

## 5. Django/frontend implementation patterns

### Settings

```python
# config/settings/base.py
STATIC_URL = "static/"                      # or env("STATIC_URL", "https://cdn.example.com/static/")
STATIC_ROOT = BASE_DIR / "staticfiles"      # collectstatic target (gitignored)
STATICFILES_DIRS = [
    BASE_DIR / "assets" / "static",         # hand-written CSS/JS, vendored htmx/alpine, images
    BASE_DIR / "assets" / "dist",           # build output (Vite / Tailwind CLI), gitignored
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    # Without WhiteNoise: "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",    # directly after SecurityMiddleware
    # ...
]

# Treat Vite-hashed filenames as immutable too (WhiteNoise, third-party)
import re
_VITE_HASHED = re.compile(r"^.+[.-][0-9a-zA-Z_-]{8,12}\..+$")
def _immutable_file_test(path, url):
    return bool(_VITE_HASHED.match(url))
WHITENOISE_IMMUTABLE_FILE_TEST = _immutable_file_test   # [VERIFY: Vite hash length/charset for your version]
```

```python
# config/settings/dev.py
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]   # dev serves like prod (optional)
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

# config/settings/test.py — avoid needing collectstatic in tests
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
```

### Option A: no bundler

```text
assets/static/
├── css/app.css
├── js/
│   ├── main.js              # type="module", imports ./table-sort.js
│   └── table-sort.js
└── vendor/
    ├── htmx-2.x.min.js      # pinned version in filename; record source URL + checksum
    └── alpine-3.x.min.js
```

```django
<link rel="stylesheet" href="{% static 'css/app.css' %}">
<script src="{% static 'vendor/htmx-2.x.min.js' %}" defer></script>
<script type="module" src="{% static 'js/main.js' %}"></script>
```

Caveat: relative `import "./table-sort.js"` inside `main.js` points to the **unhashed** name, so
that module is served with a short cache. Acceptable for small apps; otherwise use an import map
built from `{% static %}` or a bundler.

### Option B: Tailwind standalone CLI (no Node runtime)

```css
/* assets/css/input.css — Tailwind v4 */
@import "tailwindcss";
@source "../../templates";
@source "../../**/templates";
@theme {
  --color-brand-500: oklch(0.62 0.17 255);
}
```

```bash
# Download a pinned standalone binary (platform-specific asset name) once, verify checksum
curl -sLo bin/tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.0/tailwindcss-linux-x64
chmod +x bin/tailwindcss          # [VERIFY: release tag and asset name]

# Dev (watch) and prod builds
bin/tailwindcss -i assets/css/input.css -o assets/dist/css/app.css --watch
bin/tailwindcss -i assets/css/input.css -o assets/dist/css/app.css --minify
```

Template: `{% static 'css/app.css' %}` (from `assets/dist`). Details on Tailwind config in
[tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md).

### Option C: Vite

```json
{
  "private": true,
  "type": "module",
  "scripts": { "dev": "vite", "build": "vite build" },
  "devDependencies": { "vite": "^6.0.0", "@tailwindcss/vite": "^4.0.0", "tailwindcss": "^4.0.0" }
}
```

```js
// vite.config.js
import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig(({ command }) => ({
  plugins: [tailwindcss()],
  base: command === "build" ? "/static/" : "/",      // must match STATIC_URL (if CDN, the CDN path)
  root: resolve("./frontend"),
  build: {
    manifest: true,                                   // writes outDir/.vite/manifest.json (Vite ≥ 5)
    outDir: resolve("./assets/dist"),                 // a STATICFILES_DIRS entry
    emptyOutDir: true,
    sourcemap: "hidden",
    rollupOptions: { input: { main: resolve("./frontend/src/main.js"), charts: resolve("./frontend/src/charts.js") } },
  },
  server: {
    port: 5173,
    strictPort: true,
    origin: "http://localhost:5173",                  // asset URLs inside CSS point at the dev server
    cors: { origin: "http://localhost:8000" },        // [VERIFY: default dev CORS was restricted in recent Vite]
  },
}));
```

With `base: "/static/"` and output at the root of a `STATICFILES_DIRS` dir, Vite's chunk URLs
(`/static/assets/main-xyz.js`) match Django's static URL layout.

#### Manifest-reading template tag (instead of a third-party package)

```python
# core/templatetags/vite.py
import json
from functools import lru_cache
from django import template
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.utils.html import format_html, format_html_join

register = template.Library()
MANIFEST = settings.BASE_DIR / "assets" / "dist" / ".vite" / "manifest.json"
DEV_SERVER = "http://localhost:5173"

@lru_cache(maxsize=1)
def _manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)

def _collect(entry: str, seen: set) -> tuple[list[str], list[str]]:
    chunk = _manifest()[entry]               # KeyError → loud failure for a typo'd entry
    css, preloads = list(chunk.get("css", [])), []
    for imp in chunk.get("imports", []):
        if imp not in seen:
            seen.add(imp)
            sub_css, sub_pre = _collect(imp, seen)
            css += sub_css
            preloads += [_manifest()[imp]["file"], *sub_pre]
    return css, preloads

@register.simple_tag
def vite_entry(entry: str):
    if settings.VITE_DEV_MODE:
        return format_html(
            '<script type="module" src="{0}/@vite/client"></script>'
            '<script type="module" src="{0}/{1}"></script>', DEV_SERVER, entry)
    css, preloads = _collect(entry, set())
    url = staticfiles_storage.url                 # resolves through Django's manifest in prod
    return format_html(
        "{}{}{}",
        format_html_join("", '<link rel="stylesheet" href="{}">', ((url(c),) for c in dict.fromkeys(css))),
        format_html_join("", '<link rel="modulepreload" href="{}">', ((url(p),) for p in dict.fromkeys(preloads))),
        format_html('<script type="module" src="{}"></script>', url(_manifest()[entry]["file"])),
    )
```

```django
{% load vite %}
<head>
  …
  {% vite_entry "src/main.js" %}
</head>
```

`VITE_DEV_MODE = env.bool("VITE_DEV_MODE", default=False)` — never derive it from request
data. Alternative: django-vite (third-party) provides `{% vite_hmr_client %}` and
`{% vite_asset "src/main.js" %}` with a `DJANGO_VITE` settings dict [VERIFY: tag names and
settings structure for the installed major version].

Vite env vars: only `VITE_*` are exposed via `import.meta.env` and are inlined into public
bundles. Runtime config (API base, feature flags) for the browser comes from the server via
`json_script`, not from build-time env.

### Dev workflow

```bash
# Terminal 1
python manage.py runserver            # serves Django + static from finders (DEBUG=True)
# Terminal 2 (Vite projects)
npm run dev                           # VITE_DEV_MODE=1 in Django env → tags point at :5173
# Terminal 2 (Tailwind CLI projects)
bin/tailwindcss -i assets/css/input.css -o assets/dist/css/app.css --watch
```

### Lockfiles and npm

- Commit `package-lock.json`; CI and Docker use `npm ci` (fails if lock and `package.json`
  disagree, installs exact versions, deletes `node_modules` first).
- Pin Node via `.nvmrc`/`engines` and the Docker base image tag.
- Keep build tools in `devDependencies`; the runtime image needs none of them.
- `npm audit` / Dependabot for advisories; review new transitive deps
  ([dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md)).

### Dockerfile (multi-stage)

```dockerfile
# syntax=docker/dockerfile:1
FROM node:22-slim AS assets
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY vite.config.js ./
COPY frontend ./frontend
COPY templates ./templates
COPY apps ./apps
RUN npm run build                                   # → /app/assets/dist

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=assets /app/assets/dist ./assets/dist
# collectstatic must not need real secrets/DB: use a build-only settings value
RUN DJANGO_SETTINGS_MODULE=config.settings.prod SECRET_KEY=build-only-not-secret \
    python manage.py collectstatic --noinput
USER 1000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
```

Templates/app dirs are copied into the asset stage because Tailwind scans them for classes. Make
sure `.dockerignore` excludes `node_modules`, `assets/dist`, `staticfiles`, `.env`.

### CI ordering

```bash
npm ci
npm run build                                   # Vite / Tailwind → assets/dist
test -f assets/dist/.vite/manifest.json         # fail fast if the build silently produced nothing
python manage.py collectstatic --noinput --clear
python manage.py check --deploy
pytest                                          # template tests can render {% vite_entry %} against the real manifest
```

Build once, deploy the same image to every environment. For a CDN-uploaded setup,
`collectstatic` runs in the release step (it uploads), before switching traffic; keep old hashed
files for at least one deploy so in-flight pages still load (do not `--clear` a shared bucket).

### CDN

- Origin pull: set `STATIC_URL = "https://cdn.example.com/static/"`, CDN origin = app (WhiteNoise)
  host. Hashed files are cached forever; no purge needed on deploy.
- Fonts from a different origin need CORS (`Access-Control-Allow-Origin`) and `crossorigin` on
  `<link rel="preload" as="font">`.
- Third-party CDN scripts (not your own) need SRI `integrity` + `crossorigin` and a CSP entry
  (see [frontend-security.md](frontend-security.md)); prefer self-hosting.

## 6. Production use cases

- **HTMX + Alpine business app:** vendored libs, plain CSS or Tailwind CLI, WhiteNoise with
  `CompressedManifestStaticFilesStorage`, CDN origin pull later.
- **Tailwind dashboard without Node in prod:** standalone CLI in the Docker build stage, output
  into `assets/dist`, `collectstatic` in the image.
- **Hybrid templates + React islands:** Vite multi-entry (`main`, `charts`, `editor`), manifest
  tag per page, `modulepreload` for shared chunks, hidden source maps uploaded to the error tracker.
- **Multi-server deployment:** django-storages (third-party) S3 manifest storage, `collectstatic`
  in a release job, CDN in front of the bucket.

## 7. Advantages and disadvantages

| Approach | Advantages | Disadvantages |
|---|---|---|
| No bundler | Zero toolchain, instant reload, auditable | Unhashed nested ES module imports; manual vendoring |
| Tailwind standalone CLI | One binary, no `node_modules` | Only Tailwind; plugins limited [VERIFY: v4 standalone plugin support] |
| Vite | HMR, TS/JSX, code splitting, tree shaking | Node toolchain, manifest integration, two dev servers |
| WhiteNoise | Simple, correct caching headers, compression | Python process serves bytes (fine behind CDN) |
| Object storage + CDN | Scales, offloads app | Upload step, credentials in CI, eventual consistency |

## 8. Security and performance considerations

### Security

- No secrets in any bundled env var (`VITE_*`, `process.env.*` replaced at build). Treat all
  JS/CSS/source maps as public.
- `STATIC_ROOT` must contain only static assets — never point it at the project root or media.
- Self-host third-party libraries or use SRI; pin versions; review lockfile diffs.
- Do not serve user uploads via WhiteNoise/static storage (different trust, no hashing).
- `runserver --insecure` and `DEBUG=True` static serving are never production.
- CSP: bundled module scripts from `'self'` (or the CDN origin) need no `unsafe-inline`; Vite dev
  HMR needs dev-only CSP relaxations — keep them out of prod settings.

### Performance

- Hashed URLs + `Cache-Control: max-age=31536000, immutable`; HTML `no-cache` or short max-age.
- Serve Brotli/gzip (WhiteNoise precompression or the CDN).
- `modulepreload` for entry imports; avoid one giant bundle — split per page entry.
- Tailwind production CSS is minified and purged automatically; verify size in CI.
- Measure in the browser (see [frontend-performance.md](frontend-performance.md)).

## 9. Common mistakes and anti-patterns

1. **`STATIC_ROOT` listed in `STATICFILES_DIRS`** → `ImproperlyConfigured`; keep them distinct.
2. **Hard-coded `/static/...` paths** → always `{% static %}` / `staticfiles_storage.url()`.
3. **Using `STATICFILES_STORAGE`** on Django ≥ 5.1 → `STORAGES["staticfiles"]`.
4. **Tests failing with "Missing staticfiles manifest entry"** → plain `StaticFilesStorage` in
   test settings, or run `collectstatic` in CI before tests.
5. **CSS referencing a missing image/font or `.map`** → `collectstatic` fails in post-process;
   fix the path or ship the file.
6. **Vite `base` not matching `STATIC_URL`** → chunk 404s; align both (including CDN prefix).
7. **`npm install` in CI** → `npm ci` with committed lockfile.
8. **Committing `assets/dist` and building in CI too** → pick one; usually build in CI, gitignore.
9. **Dynamic Tailwind classes** (`bg-{{ color }}-500`) → map to complete class names in Python.
10. **`collectstatic --clear` against a shared CDN bucket** → old pages break mid-deploy.
11. **Caching the manifest across deploys in a long-lived process** → restart workers on deploy
    (the `lru_cache` above assumes immutable images).
12. **Exposing an "API key" via `VITE_API_KEY`** → proxy through Django; keep secrets server-side.

## 10. Troubleshooting and validation

| Symptom | Cause | Check |
|---|---|---|
| 404 for static in dev | `django.contrib.staticfiles` missing, file not under a finder path, wrong namespace | `findstatic --verbosity 2 path` |
| 404 for static in prod | `collectstatic` not run, WhiteNoise not in middleware, proxy path mismatch | `ls $STATIC_ROOT`, response headers, `STATIC_URL` |
| `ValueError: Missing staticfiles manifest entry for 'x'` | Typo, file added after `collectstatic`, `manifest_strict` | `staticfiles.json` contents; rerun `collectstatic` |
| `collectstatic` fails "file … could not be found" in post-process | CSS `url()`/`sourceMappingURL` to missing file | Path in the error; ship `.map` or strip the comment |
| `KeyError` in Vite tag | Entry name differs from manifest key (must be path relative to Vite `root`) | Open `.vite/manifest.json` |
| Old CSS after deploy | Non-hashed URL cached, CDN caching HTML, manifest not regenerated | View source: hashed name? `Cache-Control` on HTML |
| Works in dev, blank styles in prod | Tailwind didn't scan templates in build stage | Templates copied into asset stage; `@source` paths |
| Vite dev assets blocked | Dev CORS/origin config, HTTPS mixed content | Browser console; `server.cors`, `server.origin` |
| Fonts fail from CDN | Missing CORS header | Response `Access-Control-Allow-Origin` |
| Huge JS bundle | Library imported wholesale, no splitting | `vite build` report; rollup visualizer |

Validation steps:

1. CI: `npm ci && npm run build && collectstatic --noinput` must pass; assert manifest exists.
2. Smoke test against the built image with `DEBUG=False`: fetch `/`, parse `<link>/<script>` URLs,
   assert 200, hashed names, `immutable` cache header, `content-encoding: br|gzip`.
3. Template test rendering the base layout with prod storage to catch manifest-strict errors.
4. Lighthouse/bundle-size budget check (see [frontend-performance.md](frontend-performance.md)).

## 11. Related references and skills

- [tailwind-and-ui-frameworks.md](tailwind-and-ui-frameworks.md) — Tailwind config, v3 vs v4
- [django-react-integration.md](django-react-integration.md) — SPA builds, dev proxy
- [javascript-engineering.md](javascript-engineering.md) — module organization, dynamic imports
- [frontend-performance.md](frontend-performance.md) — budgets, caching from the browser
- [frontend-security.md](frontend-security.md) — SRI, CSP, client-side secrets
- [django-template-engineering.md](django-template-engineering.md) — `{% static %}` in layouts
- [production-frontend-architectures.md](production-frontend-architectures.md)
- [settings-configuration.md](../../django-core/references/settings-configuration.md), [production-configuration.md](../../django-core/references/production-configuration.md), [file-handling.md](../../django-core/references/file-handling.md)
- [http-and-static-delivery-performance.md](../../django-performance/references/http-and-static-delivery-performance.md)
- [dependency-and-supply-chain-security.md](../../django-security-engineering/references/dependency-and-supply-chain-security.md)
- [ci-cd-test-automation.md](../../django-testing/references/ci-cd-test-automation.md)

## 12. Official documentation and external references

- https://docs.djangoproject.com/en/5.2/howto/static-files/
- https://docs.djangoproject.com/en/5.2/ref/contrib/staticfiles/
- https://docs.djangoproject.com/en/5.2/ref/settings/#std-setting-STORAGES
- https://docs.djangoproject.com/en/5.2/howto/static-files/deployment/
- https://vite.dev/guide/backend-integration
- https://vite.dev/config/build-options
- https://vite.dev/config/server-options
- https://vite.dev/guide/env-and-mode
- https://tailwindcss.com/docs/installation/tailwind-cli
- https://tailwindcss.com/docs/detecting-classes-in-source-files
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control
- https://developer.mozilla.org/en-US/docs/Web/HTML/Attributes/rel/modulepreload
- https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity
- https://web.dev/articles/http-cache
