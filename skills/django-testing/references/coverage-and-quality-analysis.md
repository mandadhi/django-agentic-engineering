# Coverage and quality analysis

> **Scope:** Measuring and interpreting test coverage of a Django codebase (coverage.py,
> pytest-cov, branch and diff coverage, template coverage), using it to find missing tests, and
> complementing it with mutation testing and static analysis so the suite's *strength* — not just
> its reach — is assessed.
> **Baseline:** Django 5.2 LTS · pytest ≥ 8 · pytest-django 4.x · coverage.py ≥ 7.2 ·
> **Depends on:** **[third-party: coverage]** (coverage.py) · **[third-party: pytest-cov]** ·
> optional **[third-party: django-coverage-plugin, diff-cover, mutmut, cosmic-ray, ruff, mypy,
> django-stubs]** · **[pytest]** `--strict-markers`, `filterwarnings`

## 1. Purpose and scope

Use this reference to:

- Configure coverage correctly for Django (sources, omits, branch mode, parallel processes).
- Report coverage and enforce thresholds without gaming them.
- Turn a coverage report into a prioritised list of **missing tests**.
- Decide when mutation testing is worth its cost and how to triage surviving mutants.
- Integrate static analysis that catches test-quality problems coverage cannot.

Out of scope: publishing reports and CI wiring → [ci-cd-test-automation.md](ci-cd-test-automation.md);
what to test and at which level → [testing-fundamentals.md](testing-fundamentals.md).

## 2. Core concepts

| Concept | Meaning |
|---|---|
| Line (statement) coverage | Which executable lines ran during the tests. |
| Branch coverage | Whether each possible jump from a line (both sides of `if`, loop entered/skipped, exception path) was taken. A "partial" line ran but not all its branches did. |
| Diff (patch) coverage | Coverage of only the lines changed in a PR — the metric reviewers can act on. |
| Dynamic context | Records *which test* executed each line (`dynamic_context = "test_function"`), shown in the HTML report. |
| Threshold / ratchet | A minimum percentage the build enforces; ratcheting raises it as coverage improves and never lowers it silently. |
| Mutation score | Share of deliberately injected bugs (mutants) the suite detects ("kills"). Measures assertion strength. |
| Exclusion | Code deliberately not counted (`pragma: no cover`, `exclude_also` patterns, `omit` files). |

Provenance:

| Tool | Provenance | Role |
|---|---|---|
| coverage.py | **[third-party: coverage]** | The measurement engine; config in `pyproject.toml` / `.coveragerc` |
| pytest-cov | **[third-party: pytest-cov]** | pytest integration; handles xdist workers and combining |
| django-coverage-plugin | **[third-party: django-coverage-plugin]** | Measures Django template execution |
| diff-cover | **[third-party: diff-cover]** | Diff coverage from a `coverage.xml` against a git branch |
| mutmut / cosmic-ray | **[third-party]** | Mutation testing |
| ruff, mypy + django-stubs | **[third-party]** | Static analysis incl. test-style rules |

Django and pytest provide **no** coverage measurement themselves.

## 3. Internal mechanics

- coverage.py installs a trace function (or, on Python ≥ 3.12, can use `sys.monitoring` via
  `COVERAGE_CORE=sysmon`, `VERIFY` for your coverage version) and records executed line/arc pairs
  per file into a `.coverage` SQLite data file.
- **Measurement must start before Django imports your code.** Module-level lines (imports, class
  bodies, model fields, URL patterns) execute at import; if coverage starts after
  `django.setup()`, they show as missed — or, more often with `source` configured, as covered by
  nobody's assertion. pytest-cov starts coverage early enough; with the Django runner use
  `coverage run manage.py test`.
- **Subprocesses and parallel workers** each write their own data file when `parallel = true`;
  `coverage combine` merges them. pytest-cov does this automatically for pytest-xdist. Django's
  `manage.py test --parallel` uses multiprocessing; it needs `concurrency = ["multiprocessing"]`
  plus `parallel = true` and a `coverage combine` step (`VERIFY` behaviour with the `spawn` start
  method on macOS/Windows).
- Branch coverage records arcs (line → line). An `if` with no `else` whose condition was always
  true shows as partial — the "condition false" path was never exercised.
- `omit` removes files from measurement; `exclude_lines`/`exclude_also` remove lines matching a
  regex (and the whole block they introduce). `exclude_also` (coverage ≥ 7.2) *adds* to the
  defaults rather than replacing them, so `pragma: no cover` keeps working.
- Template coverage requires the template engine option `"debug": True` in test settings for
  django-coverage-plugin to see template line numbers.

## 4. Engineering decisions and trade-offs

| Decision | Prefer | Why |
|---|---|---|
| Line vs branch | Branch always on | Line coverage hides untested `else`/error paths, exactly where bugs live |
| Global threshold | A realistic floor (current value, rounded down) + ratchet | A high aspirational number invites trivial tests and `no cover` abuse |
| Diff coverage | Gate PRs at e.g. 90 % of changed lines | Actionable for the author; prevents erosion without forcing legacy backfill |
| What to omit | Migrations, `tests/`, settings, `manage.py`, `asgi.py`/`wsgi.py` | Not behaviour you test with unit tests; migrations are tested separately ([model-and-orm-testing.md](model-and-orm-testing.md)) |
| Admin, generated code | Measure, don't gate per-file | Admin customisations can contain logic worth testing |
| Mutation testing | Scheduled, scoped to critical modules (pricing, permissions, billing) | Full-repo mutation runs take hours and produce noise |
| `pragma: no cover` | Defensive branches that cannot be reached in tests, platform guards, `if TYPE_CHECKING` | Every pragma is a reviewed claim, not an escape hatch |
| Coverage in local runs | Off by default (`addopts` without `--cov`) | Tracing slows tests ~20–40 %; CI owns measurement |

## 5. Django implementation patterns

### coverage.py configuration

```toml
# pyproject.toml
[tool.coverage.run]
branch = true
source = ["apps", "config"]
omit = [
  "*/migrations/*",
  "*/tests/*",
  "config/settings/*",
  "manage.py",
  "config/asgi.py",
  "config/wsgi.py",
]
dynamic_context = "test_function"         # which test hit which line (HTML report)
parallel = true                           # needed for xdist / Django --parallel / subprocesses
concurrency = ["thread", "multiprocessing"]
plugins = ["django_coverage_plugin"]      # only if installed (third-party)

[tool.coverage.report]
show_missing = true
skip_covered = true
skip_empty = true
precision = 1
fail_under = 84                           # current floor; ratchet upward
exclude_also = [                          # coverage >= 7.2
  "if TYPE_CHECKING:",
  "raise NotImplementedError",
  "@(abc\\.)?abstractmethod",
  "def __repr__",
  "if settings.DEBUG:",
]

[tool.coverage.html]
show_contexts = true

[tool.coverage.django_coverage_plugin]
template_extensions = "html, txt"
```

Template coverage also needs, in **test settings only**:

```python
TEMPLATES[0]["OPTIONS"]["debug"] = True
```

### Running with pytest-cov

```bash
pytest --cov --cov-branch --cov-report=term-missing:skip-covered \
       --cov-report=xml --cov-report=html --cov-fail-under=84
pytest -n auto --cov                     # pytest-cov combines xdist worker data automatically
pytest --cov --cov-context=test          # pytest-cov's own per-test contexts (alternative to dynamic_context)
```

`--cov` with no argument uses `source` from the coverage config. Don't set `--cov` in `addopts`
if developers run single tests often; put it in the CI command.

### Running with Django's runner

```bash
coverage erase
coverage run manage.py test --parallel 4
coverage combine
coverage report
coverage xml
```

### Diff coverage on pull requests **[third-party: diff-cover]**

```bash
pytest --cov --cov-branch --cov-report=xml
diff-cover coverage.xml --compare-branch=origin/main --fail-under=90 --markdown-report diff-cover.md
```

The diff gate is what keeps a large legacy codebase improving: new and modified lines must be
tested even if the global number is 60 %.

### From report to missing tests

1. Run with `term-missing` (or open the HTML report with contexts).
2. For each module with missed lines, classify misses by **risk**:

| Missed code | Risk | Action |
|---|---|---|
| Permission/tenant check `else` branch, `raise PermissionDenied` | High | Test now ([security-testing.md](security-testing.md)) |
| Service error paths: rollback, compensation, external timeout | High | Test failure scenario ([service-and-architecture-testing.md](service-and-architecture-testing.md)) |
| Validation branches (`clean()`, serializer `validate_*`) | Medium–high | Parametrised invalid-input tests |
| Task retry/backoff branch | Medium | Integration test with failing double |
| `__str__`, admin display helpers | Low | Leave or cover incidentally |
| Defensive "impossible" branch | Low | Consider removing code or `pragma` with justification |

3. Write behaviour tests that hit the branch **and assert its effect** — then re-run.
4. Check partial branches (`->` arcs in `term-missing`, yellow lines in HTML): these are often
   the `if` that is always true in tests.

### Why the percentage alone misleads — concrete example

```python
# apps/billing/services.py
def apply_discount(invoice, code):
    discount = Discount.objects.get(code=code)
    if discount.expires_at < timezone.now():
        raise DiscountExpired(code)
    invoice.total = invoice.total - invoice.total * discount.percent / 100
    invoice.save(update_fields=["total"])
    return invoice
```

```python
def test_apply_discount(invoice_factory, discount_factory):
    invoice = invoice_factory(total=Decimal("100"))
    discount_factory(code="X", percent=10, expires_at=far_future)
    apply_discount(invoice, "X")          # no assertion
```

Line coverage: ~86 % (only the `raise` missed). Branch coverage reports the partial `if`. Yet
the test asserts nothing: a bug computing `percent / 10` passes. A mutation tool changing `/ 100`
to `* 100` would survive — that's the signal. Correct tests assert `invoice.total == Decimal("90")`,
the persisted value, and the expired path.

### Mutation testing **[third-party: mutmut]** / **[third-party: cosmic-ray]**

```toml
# pyproject.toml — mutmut 3.x style config (VERIFY keys for installed version)
[tool.mutmut]
paths_to_mutate = ["apps/billing/"]
tests_dir = ["apps/billing/tests/"]
pytest_add_cli_args = ["-p", "no:randomly", "-x", "--no-cov"]
```

```bash
mutmut run                 # scheduled/nightly or before a risky refactor
mutmut results             # list survivors
mutmut show <mutant-id>    # inspect the diff of a surviving mutant
```

Make it workable with Django:

- Scope to one critical app; run tests that cover it only.
- Use `--reuse-db`/`--keepdb` and rollback isolation — each mutant reruns the selected tests.
- Disable coverage during mutation runs.

Triage survivors: **missing assertion** (add one), **equivalent mutant** (no observable
difference — ignore/mark), **dead code** (delete it), **untested branch** (add test). Track the
mutation score of critical modules over time rather than gating on a global number.

### Static analysis that improves test quality

```toml
# pyproject.toml
[tool.ruff.lint]
extend-select = ["PT", "B", "DJ"]          # flake8-pytest-style, bugbear, flake8-django rules in ruff

[tool.pytest.ini_options]
addopts = "--strict-markers --strict-config -ra"
filterwarnings = [
  "error",                                  # warnings fail tests …
  "ignore::DeprecationWarning:some_third_party_pkg.*",   # … with explicit, reviewed exceptions
]
xfail_strict = true                         # an xfail that passes is a failure
```

- **PT rules** catch `assert` misuse in `pytest.raises` blocks without `match`, broad
  `pytest.raises(Exception)`, fixture style issues.
- **mypy + django-stubs** (and `djangorestframework-stubs`) catch wrong factory kwargs, wrong
  service signatures used in tests, and `Optional` misuse — run on `tests/` too.
- **Warnings as errors** surfaces Django `RemovedInDjangoXXWarning` deprecations before the
  upgrade breaks you.
- `--strict-markers` prevents typo'd markers (`@pytest.mark.djangodb`) silently doing nothing.

## 6. Production use cases

- **Legacy app at 48 %**: global floor set to 48 with ratchet, diff-cover at 85 % on PRs; within
  two quarters global coverage reached 71 % with tests concentrated on touched (riskiest) code.
- **Permission regression**: coverage HTML with contexts showed `HasProjectRole.has_object_permission`
  `False` branch never executed by any test → added cross-tenant 404 tests, which found a
  missing check in one ViewSet.
- **Billing refactor**: mutmut on `apps/billing/` showed 23 surviving mutants in rounding logic;
  parametrised boundary tests killed 21, two were equivalent.
- **Django upgrade prep**: `filterwarnings = ["error"]` surfaced every deprecated API in the
  suite before upgrading from 4.2 to 5.2.

## 7. Advantages and disadvantages

| Technique | Advantages | Disadvantages |
|---|---|---|
| Line coverage | Cheap, universal | Misses untested branches; says nothing about assertions |
| Branch coverage | Finds untested decision paths | Slightly slower; partials need interpretation |
| Diff coverage | Actionable per PR; fair to legacy code | Needs git history in CI; ignores tests deleted elsewhere |
| Global threshold | Prevents erosion | Encourages gaming if set aspirationally |
| Template coverage | Finds untested template branches | Requires template `debug`; plugin dependency |
| Mutation testing | Measures assertion strength | Slow; equivalent mutants; tool/Django friction |
| Static analysis | Catches test smells and type errors cheaply | Config upkeep; false positives |

## 8. Security and performance considerations

- Prioritise coverage of **authorisation, validation and tenant scoping branches**; 100 % of a
  permission class's deny paths is worth more than any global percentage.
- Coverage of security controls must be by tests that **assert denial** — executing a
  `PermissionDenied` line in a test that expects 200 is not coverage of the control.
- HTML coverage reports include source code; publish them as private CI artifacts, not public pages.
- Tracing overhead is 20–40 % (less with `sysmon`, `VERIFY`); measure in CI, not in every local run.
- Mutation testing multiplies suite runtime by the number of mutants — schedule it, scope it.
- Don't let `DEBUG`-style template settings or coverage plugins leak into production settings.

## 9. Common mistakes and anti-patterns

- Tests written to execute lines without asserting outcomes ("coverage tests").
- `# pragma: no cover` on error-handling paths because they're "hard to test" — those are the
  highest-value tests.
- Including `migrations/` and `tests/` in the denominator, inflating or distorting the number.
- Replacing default exclusions with `exclude_lines` and accidentally dropping `pragma: no cover`
  support — use `exclude_also`.
- Starting coverage after Django setup (e.g. `coverage start` in `conftest.py`), so module-level
  code shows as missed.
- Forgetting `coverage combine` with parallel runs → reported coverage far lower than reality.
- Setting a 100 % target for the whole project.
- Running mutation testing on the whole repository once, getting thousands of survivors, and
  abandoning it.
- Ignoring deprecation warnings until the Django upgrade.

## 10. Troubleshooting and validation

| Symptom | Likely cause | Fix |
|---|---|---|
| Coverage much lower in CI than locally | Missing `coverage combine` / parallel data not merged | `parallel = true`; let pytest-cov combine, or run `coverage combine` |
| Model fields/imports reported as missed | Coverage started after imports | Use pytest-cov or `coverage run manage.py test` |
| "No data was collected" | `source` doesn't match package paths, or tests run in a subprocess without coverage | Fix `source`; enable subprocess measurement (`VERIFY` method for your coverage version) |
| Templates not in report | Plugin not loaded or template `debug` false | Add plugin to `plugins`; `debug: True` in test settings |
| Celery/thread code missed | Code ran in threads/greenlets not traced | `concurrency = ["thread", ...]` (or `gevent`/`eventlet`) |
| `fail_under` passes locally, fails in CI | Different test selection (`-m "not slow"`) | Measure the same suite in both, or combine shard data before reporting |
| diff-cover reports 0 lines | Shallow clone, compare branch missing | `fetch-depth: 0`; fetch `origin/main` |
| Mutation run extremely slow | Whole suite per mutant; DB recreation | Scope tests; `--reuse-db`; `-x`; disable coverage |

Validation: branch coverage on; migrations/tests omitted; parallel data combined; diff gate on
PRs; every `pragma: no cover` reviewed; critical modules' deny/error paths covered by asserting tests.

## 11. Related references and skills

- [testing-fundamentals.md](testing-fundamentals.md) — risk-based test selection.
- [ci-cd-test-automation.md](ci-cd-test-automation.md) — publishing coverage, PR gates, sharded coverage combine.
- [pytest-and-pytest-django.md](pytest-and-pytest-django.md) — pytest configuration and markers.
- [security-testing.md](security-testing.md) — testing deny paths.
- [model-and-orm-testing.md](model-and-orm-testing.md) — migration tests (why migrations are omitted from coverage).
- [performance-and-reliability-testing.md](performance-and-reliability-testing.md) — suite speed cost of coverage.
- [django-security-engineering](../../django-security-engineering/SKILL.md) — which controls must be proven.

## 12. Official documentation and external references

- coverage.py: <https://coverage.readthedocs.io/>
- coverage.py configuration (`exclude_also`, `dynamic_context`, `parallel`): <https://coverage.readthedocs.io/en/latest/config.html>
- coverage.py branch coverage: <https://coverage.readthedocs.io/en/latest/branch.html>
- coverage.py subprocesses: <https://coverage.readthedocs.io/en/latest/subprocess.html>
- Django — integration with coverage.py: <https://docs.djangoproject.com/en/5.2/topics/testing/advanced/#integration-with-coverage-py>
- pytest-cov: <https://pytest-cov.readthedocs.io/>
- django-coverage-plugin: <https://github.com/nedbat/django_coverage_plugin>
- diff-cover: <https://github.com/Bachmann1234/diff_cover>
- mutmut: <https://mutmut.readthedocs.io/> · cosmic-ray: <https://cosmic-ray.readthedocs.io/>
- Ruff rules (flake8-pytest-style `PT`, flake8-django `DJ`): <https://docs.astral.sh/ruff/rules/>
- django-stubs: <https://github.com/typeddjango/django-stubs>
- pytest — warnings and strict options: <https://docs.pytest.org/en/stable/how-to/capture-warnings.html>
