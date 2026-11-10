# CI/CD Test Automation

> **Scope:** How a Django test suite runs in CI: job layout, database services, environment and
> secrets, parallelisation and sharding, fast/slow suite separation, artifacts, coverage
> publishing, failure reporting, pull-request gates and CI reliability.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · pytest-xdist 3.x ·
> **Depends on:** **[Django]** `manage.py check`/`makemigrations`, **[pytest]**,
> **[pytest-django]**, **[third-party: pytest-xdist, pytest-cov, pytest-split, pytest-timeout,
> pytest-randomly, diff-cover]**, CI platform (GitHub Actions / GitLab CI).

## 1. Purpose and scope

CI is where the test suite becomes a merge gate. This reference covers *how tests are executed
and reported in automation*. It does not own:

- Coverage configuration and thresholds → [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md).
- Making individual tests faster, flaky detection and quarantine → [performance-and-reliability-testing.md](performance-and-reliability-testing.md).
- Test DB isolation mechanics → [database-and-transaction-testing.md](database-and-transaction-testing.md).
- Diagnosing "fails only in CI" → [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md).
- Supply-chain hardening of the pipeline itself → [django-security-engineering dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md).

Use it when creating or changing a test workflow, adding a database or broker service, splitting
the suite, or deciding which checks block a merge.

## 2. Core concepts

| Concept | Meaning in a Django project |
|---|---|
| **Pipeline stage** | Ordered group of jobs: static checks → fast tests → slow/integration → E2E → deploy. Cheap, high-signal checks run first. |
| **Service container** | A DB/cache/broker container started next to the job (PostgreSQL, Redis). Must match production major versions. |
| **Test settings** | `config.settings.test` module; selected with `DJANGO_SETTINGS_MODULE`; reads connection details from env vars. |
| **Parallelisation** | Several processes in *one* job (`pytest -n auto` via pytest-xdist). Each worker gets its own test DB. |
| **Sharding** | Splitting the suite across *several jobs/machines* (matrix), each running a subset. |
| **Required check** | A status check branch protection demands before merge. |
| **Artifact** | Files kept after the job: JUnit XML, coverage XML/HTML, Playwright traces, logs. |
| **Fast vs slow suite** | Marker-based split: PRs run the fast suite on every push; slow/E2E/live-contract tests run on main, nightly, or on label. |
| **Merge queue** | Platform feature that re-tests the PR rebased on the latest main before merging; prevents "green PR, red main". |

## 3. Internal mechanics

### How a pytest-django run in CI creates databases

1. pytest-django reads `DJANGO_SETTINGS_MODULE` (env or `[tool.pytest.ini_options]`) and calls
   `django.setup()` **[pytest-django]**.
2. On first DB-requesting test, `django_db_setup` creates `test_<NAME>` and runs migrations
   (unless `--no-migrations`) **[pytest-django]** / **[Django]** `create_test_db`.
3. With pytest-xdist, each worker (`gw0`, `gw1`, …) gets a suffixed DB, e.g. `test_app_gw0`
   **[pytest-django]**. Migrations run *once per worker*, so migration time is paid N times.
4. `--reuse-db` keeps databases between runs; in ephemeral CI containers there is nothing to
   reuse, so it is harmless but useless. Caching a DB dump across CI runs is rarely worth the
   invalidation complexity — speed up migrations or use `--no-migrations` for the fast suite only
   if a separate job still runs migrations (see §4).

### How xdist distributes tests **[third-party: pytest-xdist]**

| `--dist` mode | Behaviour | Use when |
|---|---|---|
| `load` (default with `-n`) | Sends tests to idle workers | Tests are independent (should be the norm) |
| `loadscope` | Groups by module/class | Expensive class/module-scoped fixtures or `setUpTestData` |
| `loadfile` | Groups by file | File-level shared resources |
| `worksteal` | Idle workers steal from busy ones (xdist ≥ 3.2 `VERIFY`) | Very uneven test durations |
| `loadgroup` | `@pytest.mark.xdist_group("name")` pins tests to one worker | A few tests share an unshareable resource |

Session-scoped fixtures run **once per worker**, not once per session. Anything assuming a single
process (a fixed port, a file path, a global counter) breaks under `-n`.

### Django's own runner

`manage.py test --parallel auto` **[Django]** forks processes and clones the test DB per worker
(`test_app_1`, `test_app_2`…). Since Django 4.1 the spawn start method (macOS/Windows) is
supported `VERIFY`. Use either Django's `--parallel` or xdist — not both.

## 4. Engineering decisions and trade-offs

| Decision | Option A | Option B | Guidance |
|---|---|---|---|
| Parallelism | `-n auto` in one job | Shard across N jobs | Start with `-n auto`; shard when one job exceeds ~10 min even with all cores. |
| Sharding method | Hash/round-robin by file | Duration-based (`pytest-split` + `.test_durations`) | Duration-based gives balanced shards; refresh the durations file periodically. |
| Migrations in tests | Run migrations | `--no-migrations` (syncdb-style) | Fast suite may skip; **one** job must run real migrations plus `makemigrations --check`. Skipping hides broken data migrations and DB-level defaults. |
| DB engine | PostgreSQL service | SQLite | PostgreSQL whenever production is PostgreSQL. SQLite only for pure libraries. |
| When slow tests run | Every PR | main + nightly + `run-slow` label | Keep PR feedback < ~10 min; anything not on PRs must alert an owner when it fails. |
| Retries | `--reruns` on all | No reruns | Never auto-pass via reruns; use reruns only to *classify* flakes (report, don't hide). |
| Version matrix | Single pinned version | Python × Django matrix | Apps: one pinned version (+ a scheduled "next Django" job). Reusable libraries: full matrix. |
| Coverage per shard | Combine in a final job | Skip | Combine: per-shard coverage % is meaningless. |

## 5. Django implementation patterns

### Test settings driven by environment **[Django]**

```python
# config/settings/test.py
from .base import *  # noqa: F403
import os

DEBUG = False
SECRET_KEY = "test-only-not-secret"  # never a real key
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "app"),
        "USER": os.environ.get("POSTGRES_USER", "app"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "app"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},  # Django ≥ 4.2
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
CELERY_TASK_ALWAYS_EAGER = False  # tests call tasks explicitly
```

Rules: test settings never reference production hosts, buckets or API keys; any credential in CI
is a sandbox/test credential with least privilege.

### pytest configuration for CI **[pytest]** / **[pytest-django]**

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"
addopts = "--strict-markers --strict-config -ra --tb=short"
markers = [
  "slow: > 1s or needs extra services",
  "e2e: browser tests (Playwright)",
  "contract: talks to a real third-party sandbox",
]
timeout = 120          # pytest-timeout [third-party]
junit_family = "xunit2"
```

`--strict-markers` turns a typo (`@pytest.mark.slwo`) into an error instead of a silently
unselected test.

### Complete GitHub Actions workflow

```yaml
# .github/workflows/tests.yml
name: tests
on:
  pull_request:
  push:
    branches: [main]
  merge_group:            # merge queue

concurrency:
  group: tests-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  static:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6        # third-party action; pin by SHA in hardened repos
        with: { enable-cache: true }
      - run: uv sync --frozen --group dev
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run mypy apps

  tests:
    runs-on: ubuntu-24.04
    timeout-minutes: 25
    needs: static
    strategy:
      fail-fast: false
      matrix:
        group: [1, 2, 3, 4]
    services:
      postgres:
        image: postgres:17            # same major as production
        env:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app
          POSTGRES_DB: app
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U app"
          --health-interval 5s --health-timeout 5s --health-retries 10
      redis:
        image: redis:7
        ports: ["6379:6379"]
        options: --health-cmd "redis-cli ping" --health-interval 5s --health-retries 10
    env:
      DJANGO_SETTINGS_MODULE: config.settings.test
      POSTGRES_HOST: localhost
      REDIS_URL: redis://localhost:6379/0
      PYTHONHASHSEED: "0"
      TZ: UTC
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with: { enable-cache: true }
      - run: uv sync --frozen --group dev
      - name: Django system checks and migration drift
        run: |
          uv run python manage.py check --fail-level WARNING
          uv run python manage.py makemigrations --check --dry-run
      - name: Tests (shard ${{ matrix.group }}/4)
        run: >
          uv run pytest -m "not e2e and not contract"
          -n auto --dist loadscope
          --splits 4 --group ${{ matrix.group }} --durations-path .test_durations
          --cov --cov-branch --cov-report=xml:coverage-${{ matrix.group }}.xml
          --junitxml=junit-${{ matrix.group }}.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results-${{ matrix.group }}
          path: |
            junit-*.xml
            coverage-*.xml
          retention-days: 14

  deploy-checks:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    env:
      DJANGO_SETTINGS_MODULE: config.settings.production
      DJANGO_SECRET_KEY: ci-dummy-${{ github.run_id }}-not-used-anywhere-real-000000000000
      DJANGO_ALLOWED_HOSTS: example.test
      DATABASE_URL: postgres://u:p@localhost:5432/none   # never connected to
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen
      - run: uv run python manage.py check --deploy --fail-level WARNING

  coverage:
    runs-on: ubuntu-24.04
    needs: tests
    if: always()
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/download-artifact@v4
        with: { pattern: test-results-*, merge-multiple: true }
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen --group dev
      - name: Diff coverage gate on changed lines
        if: github.event_name == 'pull_request'
        run: >
          uv run diff-cover coverage-*.xml
          --compare-branch=origin/${{ github.base_ref }} --fail-under=90
```

Notes:

- `--splits/--group/--durations-path` come from **[third-party: pytest-split]**; refresh the
  durations file with `pytest --store-durations` on a schedule.
- `pytest-cov` combines xdist workers automatically within a job; across shards, diff-cover
  accepts several XML reports, or run `coverage combine` on raw `.coverage.*` data files
  (see [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md)).
- `check --deploy` needs production settings importable without real secrets; dummy values
  prove the settings module enforces HTTPS, HSTS, secure cookies, etc. It does not connect to
  the DB.
- Pin third-party actions by commit SHA in security-sensitive repos.

### GitLab CI variant (brief)

```yaml
test:
  image: python:3.13
  services:
    - name: postgres:17
      alias: postgres
  variables:
    POSTGRES_DB: app
    POSTGRES_USER: app
    POSTGRES_PASSWORD: app
    POSTGRES_HOST: postgres
    DJANGO_SETTINGS_MODULE: config.settings.test
  parallel: 4
  script:
    - pip install -r requirements/dev.txt --require-hashes
    - python manage.py makemigrations --check --dry-run
    - pytest -n auto --splits $CI_NODE_TOTAL --group $CI_NODE_INDEX --junitxml=junit.xml --cov --cov-report=xml
  artifacts:
    when: always
    reports:
      junit: junit.xml
      coverage_report: { coverage_format: cobertura, path: coverage.xml }
```

GitLab's `services` are reachable by alias, not `localhost`; that is the most common port/host
mistake when porting from GitHub Actions.

### Fast vs slow suites

```text
PR push        → static + fast suite (-m "not slow and not e2e and not contract") + diff coverage
PR label/merge → + slow + e2e (Playwright)
main / nightly → full suite, contract tests against sandboxes, next-Django job, durations refresh
```

```yaml
  e2e:
    if: github.event_name == 'push' || contains(github.event.pull_request.labels.*.name, 'run-e2e')
    # ... same services ...
    steps:
      # ...
      - run: uv run playwright install --with-deps chromium
      - run: uv run pytest -m e2e --tracing=retain-on-failure --output=test-results
      - uses: actions/upload-artifact@v4
        if: failure()
        with: { name: playwright-traces, path: test-results/ }
```

Live contract tests that need real sandbox credentials run only on trusted branches (secrets are
not exposed to fork PRs on GitHub by default).

### Failure reporting

- `-ra` lists skips/xfails/errors with reasons; `--tb=short` keeps logs readable.
- Publish JUnit XML so the platform shows failed test names (GitLab `reports: junit`; on
  GitHub a test-report action or the job summary — **[third-party]**).
- Print the random-order seed (pytest-randomly prints `Using --randomly-seed=…` in the header) so
  a CI failure can be replayed locally.
- `pytest --reruns 2` **[third-party: pytest-rerunfailures]** only in a *detection* job that
  reports reruns; the merge gate should not turn a flaky failure green. Policy:
  [performance-and-reliability-testing.md](performance-and-reliability-testing.md).

## 6. Production use cases

- **Monorepo-sized Django app (8k tests, 25 min serial).** `-n auto` on 4 cores → ~9 min;
  `pytest-split` into 4 shards → ~3 min wall clock; diff coverage gate replaces a brittle 90 %
  global threshold.
- **SaaS with PostgreSQL-specific constraints.** Moving CI from SQLite to a `postgres:17` service
  surfaced exclusion-constraint and `JSONField` lookup bugs that SQLite never executed.
- **Payment integration.** Unit tests mock the provider transport on every PR; a nightly
  `contract` job hits the provider sandbox with a test key and opens an issue on failure.
- **Library supporting Django 4.2/5.2/6.0.** Matrix of Python × Django with `tox`/`nox`
  **[third-party]**; the app repository, by contrast, pins one version.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| xdist in one job | Simple, near-linear speed-up for isolated suites | Exposes hidden shared state; migrations repeat per worker |
| Sharding | Wall-clock scales with machines | More runner minutes, coverage must be combined, durations file upkeep |
| Fast/slow split | Quick PR feedback | Slow failures discovered after merge unless gated in merge queue |
| Merge queue | main stays green with concurrent merges | Extra CI run per merge, queue latency |
| Diff coverage | Focuses on new code | Can be gamed with trivial tests; needs full history (`fetch-depth: 0`) |
| PostgreSQL service | Production-faithful | ~5–10 s start-up, health check needed |

## 8. Security and performance considerations

**Security**

- CI secrets: sandbox-only, least privilege, scoped to environments; never production DB URLs
  or keys. `permissions: contents: read` by default on GitHub.
- `pull_request_target` workflows that check out PR code run untrusted code with secrets —
  avoid for test jobs.
- Don't upload artifacts containing secrets (debug dumps, `.env`, HTML coverage of settings with
  secrets embedded).
- Lock files with hashes (`uv sync --frozen`, `pip --require-hashes`) make test environments
  reproducible and resist dependency substitution.
- Block outbound network in tests (`pytest-socket` **[third-party]** `--disable-socket
  --allow-hosts=localhost,127.0.0.1,postgres`) so a missing mock fails loudly instead of calling
  a real service.

**Performance**

- Order jobs by cost; `needs:` lets lint fail before expensive services start.
- Cache the dependency installer (uv/pip cache), not the virtualenv across OS images.
- Tune throwaway PostgreSQL for speed (CI only): `-c fsync=off -c synchronous_commit=off
  -c full_page_writes=off` via a custom command — never in production. GitHub service
  containers can't pass a command easily; run Postgres via `docker run` in a step if needed
  `VERIFY`.
- `timeout-minutes` on every job and `pytest-timeout` per test stop hung tests from burning an
  hour of runner time.

## 9. Common mistakes and anti-patterns

- SQLite in CI while production runs PostgreSQL.
- Postgres service without a health check → intermittent "connection refused" on the first test.
- Using `localhost` on GitLab (services are on their alias) or the alias on GitHub (when the job
  runs on the host, services are on `localhost`).
- No `makemigrations --check` → model changes merged without migrations.
- Global `--reruns` hiding race conditions.
- Slow/E2E suites that nobody watches after they were moved out of PRs.
- `-n auto` on a runner with 2 vCPUs and 4 shards → oversubscription; benchmark worker count.
- Per-shard coverage thresholds (each shard fails because it only sees part of the code).
- Unpinned dependencies: CI breaks on someone else's release, not on your change.
- Committing `.test_durations` once and never refreshing → shard imbalance creeps back.
- Running `check --deploy` against test settings (it will always warn) instead of production.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| `could not connect to server` at first DB test | Service not ready / wrong host | Health check; correct host for platform |
| `database "test_app_gw1" already exists` | Aborted previous run with persistent DB | Ephemeral services or `--create-db` |
| Passes serially, fails with `-n` | Shared state (ports, files, caches) | [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) |
| One shard always slowest | Stale durations | Re-run `--store-durations` |
| Coverage drops only in CI | Not combining shards/workers, wrong `source` | Combine; check config in [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) |
| Tests pass on PR, main red | Concurrent merges | Merge queue / require branch up to date |
| Random failure unreproducible | Seed not recorded | Read the seed from the log header; replay with `pytest --randomly-seed=<seed>` |

Validation checklist for a pipeline:

- [ ] Lint/type → tests → E2E ordered by cost; all jobs have timeouts.
- [ ] DB service matches production engine and major version, with a health check.
- [ ] `manage.py check`, `makemigrations --check --dry-run`, `check --deploy` (prod settings) run.
- [ ] Test settings contain no real credentials; fork PRs need no secrets for required checks.
- [ ] JUnit and coverage artifacts uploaded `if: always()`.
- [ ] Required checks set in branch protection; merge queue if many concurrent merges.
- [ ] Slow/nightly jobs notify an owner on failure.

## 11. Related references and skills

- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — markers, `--reuse-db`, configuration.
- [django-testing-framework.md](django-testing-framework.md) — Django `--parallel`, test DB lifecycle.
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — suite speed, flaky detection/quarantine.
- [coverage-and-quality-analysis.md](coverage-and-quality-analysis.md) — coverage config, thresholds, mutation testing.
- [browser-and-end-to-end-testing.md](browser-and-end-to-end-testing.md) — Playwright job details.
- [test-maintenance-and-troubleshooting.md](test-maintenance-and-troubleshooting.md) — CI-only failures.
- [django-security-engineering dependency-and-supply-chain-security](../../django-security-engineering/references/dependency-and-supply-chain-security.md) — pipeline hardening.
- [django-security-engineering secure-django-configuration](../../django-security-engineering/references/secure-django-configuration.md) — what `check --deploy` verifies.
- [postgresql-engineering](../../postgresql-engineering/SKILL.md) — PostgreSQL configuration.

## 12. Official documentation and external references

- Django testing overview (running tests, `--parallel`): <https://docs.djangoproject.com/en/5.2/topics/testing/overview/>
- Django `check --deploy`: <https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/>
- Django `makemigrations --check`: <https://docs.djangoproject.com/en/5.2/ref/django-admin/#makemigrations>
- pytest-django database / xdist: <https://pytest-django.readthedocs.io/en/latest/database.html>
- pytest-xdist: <https://pytest-xdist.readthedocs.io/en/stable/distribution.html>
- pytest JUnit XML / `-ra`: <https://docs.pytest.org/en/stable/how-to/output.html>
- pytest-split: <https://jerry-git.github.io/pytest-split/>
- pytest-timeout: <https://github.com/pytest-dev/pytest-timeout>
- diff-cover: <https://github.com/Bachmann1234/diff_cover>
- GitHub Actions service containers (PostgreSQL): <https://docs.github.com/en/actions/use-cases-and-examples/using-containerized-services/creating-postgresql-service-containers>
- GitHub merge queue: <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue>
- GitLab CI services: <https://docs.gitlab.com/ci/services/>
- GitLab parallel jobs: <https://docs.gitlab.com/ci/yaml/#parallel>
- uv in GitHub Actions: <https://docs.astral.sh/uv/guides/integration/github/>
