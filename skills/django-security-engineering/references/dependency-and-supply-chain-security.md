# Dependency and Supply-Chain Security

> Scope: pinning and hash-locking Python dependencies; vulnerability scanning (pip-audit, OSV,
> Safety); automated updates; Django and DRF security releases and upgrade cadence; typosquatting
> and dependency confusion; evaluating new packages; package provenance (Trusted Publishing,
> attestations); SBOMs; container image hardening; CI/CD secrets and least-privilege tokens;
> secret scanning; SAST as CI gates.
> Not covered here: security test methodology, SAST/DAST interpretation and incident handling
> ([security-testing-and-incident-response.md](./security-testing-and-incident-response.md));
> runtime secret storage and rotation ([secrets-and-cryptography.md](./secrets-and-cryptography.md));
> production settings ([secure-django-configuration.md](./secure-django-configuration.md));
> frontend/npm build pipelines ([django-frontend](../../django-frontend/SKILL.md)).

## 1. Purpose and scope

A Django application is mostly other people's code: Django, DRF, database drivers, Pillow,
cryptography, Celery, the base image and the CI tooling. Each is a path for known
vulnerabilities (CVE in an outdated package) or for deliberate compromise (malicious release,
typosquat, hijacked maintainer account, poisoned CI). This reference defines how a Django
project keeps dependencies **known, pinned, verified, current and minimal**, and how the build
and deployment pipeline avoids becoming the weakest link.

None of this is provided by Django; it is project and platform engineering. Tools named below
are third-party unless stated.

## 2. Core concepts

- **Direct vs transitive dependencies** — you declare Django; you also ship `asgiref`, `sqlparse`
  and every transitive of every package. Vulnerabilities are mostly transitive.
- **Lock file** — the complete, resolved set with exact versions (and ideally hashes) for a
  given environment. Deploy what was tested.
- **Hash checking** — `pip install --require-hashes` refuses any artifact whose SHA-256 differs
  from the lock; defeats a tampered mirror or a re-uploaded file.
- **Vulnerability database** — PyPA Advisory Database / OSV (open), GitHub Advisory Database,
  Safety DB (commercial licensing for its full database and service — `VERIFY` current terms).
- **SCA (software composition analysis)** — matching your resolved dependencies against advisories.
- **SBOM** — machine-readable inventory (CycloneDX or SPDX) of what an artifact contains.
- **Provenance** — evidence of where and how a package was built: PyPI Trusted Publishing (OIDC
  from CI, no long-lived token) and PEP 740 digital attestations on PyPI (`VERIFY` current
  installer-side verification support; pip does not verify attestations by default).
- **Dependency confusion** — an installer picks a public package with the same name as an
  internal one, typically because of `--extra-index-url`.
- **Typosquatting** — `djnago`, `django-rest-framwork`, `requestes`: lookalike names with
  malicious install hooks.

## 3. Internal mechanics

- `pip install` of an sdist executes the package's build backend (arbitrary code) at install
  time; wheels do not run code at install but run code at import. Prefer wheels
  (`--only-binary=:all:` where feasible) and install in an isolated build stage.
- `--extra-index-url` makes pip consider **all** configured indexes and pick the highest version
  — an attacker publishing `internal-utils==99.0` on PyPI wins. `--index-url` pointing to a
  single proxy (devpi, Artifactory, Nexus, AWS CodeArtifact) that mirrors PyPI and hosts private
  packages under reserved names avoids this. uv's default index strategy stops at the first index
  that has the package (`first-index`), which mitigates confusion (`VERIFY` for the installed uv
  version).
- Hash mode: when any requirement has `--hash`, pip requires hashes for all, and requires exact
  `==` pins.
- Django security releases: announced in advance on the django-announce mailing list (date and
  severity), then published as patch releases for every supported series (e.g. 5.2.x, and the
  current feature release). Django 5.2 is LTS with security fixes until April 2028 (`VERIFY`).
  Feature releases are supported until roughly eight months after the next feature release.
- DRF 3.16 supports Django 4.2, 5.0, 5.1 and 5.2 and Python ≥ 3.9 (`VERIFY` against the DRF
  release notes before upgrading Django past 5.2).

## 4. Engineering decisions and trade-offs

| Decision | Options | Recommendation |
|---|---|---|
| Lock tool | pip-tools (`pip-compile --generate-hashes`), uv (`uv lock` / `uv pip compile --generate-hashes`), Poetry (`poetry.lock` with hashes) | Whatever the project already uses; require hashes and one lock per deploy target |
| Update cadence | Ad-hoc, scheduled batches, continuous bots | Bot PRs (Dependabot/Renovate) grouped weekly; security updates immediately; Django patch releases within days |
| Scanner | pip-audit (PyPA, OSV/PyPI advisories), OSV-Scanner (Google), Safety (commercial DB), GitHub Dependabot alerts, Snyk/Trivy/Grype | pip-audit in CI as the gate; platform alerts as a second signal |
| Index | Public PyPI directly vs a proxy/mirror | Proxy with `--index-url` when private packages exist; reserve internal names on PyPI if possible |
| Django version | LTS-to-LTS vs every feature release | LTS for most products (5.2 → next LTS 6.2, `VERIFY`); feature releases if the team can upgrade every ~8 months |
| Ignoring an advisory | Fix, mitigate, or ignore with reason | Ignore only with advisory id, justification and expiry (e.g. `pip-audit --ignore-vuln GHSA-… ` plus a tracking ticket) |

Trade-offs: aggressive updates reduce exposure but raise regression risk — offset with a good test
suite and deprecation-warning checks (`python -Wd manage.py test`). Very old pins are
cheaper today and expensive at the next forced security upgrade.

## 5. Django implementation patterns

**Declared vs locked requirements (pip-tools)**

```text
# requirements/base.in  — direct dependencies only, compatible ranges
Django>=5.2,<5.3
djangorestframework>=3.16,<3.17
psycopg[binary]>=3.2
argon2-cffi>=23.1
```

```bash
pip-compile --generate-hashes --allow-unsafe \
  --output-file requirements/base.txt requirements/base.in
pip install --require-hashes --no-deps -r requirements/base.txt
```

**uv equivalent**

```bash
uv lock                 # pyproject.toml -> uv.lock (includes hashes)
uv sync --frozen        # install exactly the lock; fail if it is out of date
```

**pip configuration avoiding dependency confusion**

```ini
# pip.conf / PIP_INDEX_URL in CI — a single index that proxies PyPI and hosts private packages
[global]
index-url = https://pypi.internal.example.com/simple/
require-hashes = true
```

**CI gates (GitHub Actions example)** — pin actions by commit SHA, least-privilege token:

```yaml
name: security
on: [pull_request]
permissions:
  contents: read            # least privilege for GITHUB_TOKEN
jobs:
  deps:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<full-commit-sha>        # pin by SHA, not tag
      - uses: actions/setup-python@<full-commit-sha>
        with: { python-version: "3.12" }
      - run: pip install pip-audit bandit
      - run: pip-audit --strict -r requirements/base.txt
      - run: bandit -r src -ll -x '*/tests/*'
      - run: python manage.py check --deploy --fail-level WARNING
        env: { DJANGO_SETTINGS_MODULE: config.settings.ci_production }
  secrets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<full-commit-sha>
        with: { fetch-depth: 0 }
      - uses: gitleaks/gitleaks-action@<full-commit-sha>
```

Semgrep (with Django/Python rulesets) is an alternative or complement to Bandit; both are
SAST gates here — interpreting and triaging their findings is covered in
[security-testing-and-incident-response.md](./security-testing-and-incident-response.md).

**Dependabot configuration**

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: pip
    directory: "/"
    schedule: { interval: weekly }
    groups:
      django-stack:
        patterns: ["Django", "djangorestframework", "django-*"]
  - package-ecosystem: github-actions
    directory: "/"
    schedule: { interval: weekly }
  - package-ecosystem: docker
    directory: "/"
    schedule: { interval: weekly }
```

**Hardened container image (multi-stage)**

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build              # pin by digest in production: python:3.12-slim@sha256:...
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements/base.txt .
RUN pip install --require-hashes --no-deps --prefix=/install -r base.txt
COPY src/ ./src/
# collectstatic at build time with a build-only settings module that needs no real secrets
RUN PYTHONPATH=/install/lib/python3.12/site-packages:/app/src \
    DJANGO_SETTINGS_MODULE=config.settings.build python src/manage.py collectstatic --noinput

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /install /usr/local
COPY --from=build --chown=app:app /app /app
USER app                                    # never run as root
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
```

Image rules: no compilers or build tools in the final stage; no `.env`, `.git`, tests or
fixtures (use `.dockerignore`); secrets injected at runtime, never `ARG`/`ENV`/`COPY` (they
persist in layers — use BuildKit `--mount=type=secret` if a build needs one); read-only root
filesystem where the platform allows; scan the image (Trivy/Grype) in CI.

**SBOM generation** (third-party `cyclonedx-bom` / `cyclonedx-py`):

```bash
cyclonedx-py requirements requirements/base.txt -o sbom.cdx.json   # VERIFY CLI syntax for the installed version
```

Attach the SBOM to the release artifact so a future advisory can be matched to deployed builds.

## 6. Production use cases

- **Django security release announced**: read the advisory, check which issues affect used
  features, upgrade the patch version (`Django>=5.2.N,<5.3`), run tests, deploy within the
  agreed SLA (e.g. high severity ≤ 72 hours).
- **pip-audit fails on a transitive package**: identify the parent (`pipdeptree -r -p <pkg>`,
  third-party), upgrade the parent or add a constraint; if no fix exists, assess reachability and
  record a time-boxed ignore.
- **Adding a new package**: evaluate before installing (checklist in section 10), add to `.in`,
  re-lock, review the lock diff for unexpected transitive additions.
- **Private packages**: publish to the internal index only; reserve the name on PyPI or use a
  scoped prefix; CI uses `--index-url` to the proxy.
- **Releasing an internal package to PyPI**: use Trusted Publishing from CI (OIDC), not a
  long-lived API token; enable 2FA for all maintainers.

## 7. Advantages and disadvantages

| Practice | Advantages | Disadvantages |
|---|---|---|
| Hash-pinned locks | Reproducible, tamper-evident installs | Platform-specific wheels need per-platform hashes; more lock churn |
| Automated update bots | Small, frequent, reviewable upgrades | PR noise; requires reliable CI |
| pip-audit gate | Free, PyPA-maintained, OSV-backed | Only known vulnerabilities; reachability not analysed |
| Private index proxy | Stops dependency confusion; caches; audit trail | Operational dependency; must be kept available |
| Slim, non-root images | Smaller attack surface, fewer CVEs | Debugging tools absent; native deps need build stage |
| SBOMs | Fast impact analysis for new CVEs | Only useful if stored and queryable per release |

## 8. Security and performance considerations

- Never install in production from a mutable reference (`git+https://…@main`, unpinned sdists).
- CI is production-adjacent: it holds deploy credentials. Use short-lived OIDC credentials to
  the cloud instead of stored keys; restrict who can modify workflows (`CODEOWNERS`); don't run
  privileged workflows on untrusted fork PRs (`pull_request_target` with checkout of PR code is
  a known foot-gun).
- Pin third-party CI actions by full commit SHA; tags can be moved by a compromised maintainer.
- Secret scanning must cover history, not only the diff — a secret removed in a later commit is
  still leaked; rotate it ([secrets-and-cryptography.md](./secrets-and-cryptography.md)).
- Enable platform push protection (GitHub secret scanning push protection, GitLab equivalents)
  to block secrets before they land.
- Keep debug/dev packages (django-debug-toolbar, django-extensions, Werkzeug debugger) in a
  separate dev requirements file; Werkzeug's interactive debugger in production is remote code
  execution.
- Hash-checked, wheel-only installs are also faster and cache-friendly.

## 9. Common mistakes and anti-patterns

- `requirements.txt` with unpinned or `>=` entries used directly for deployment.
- `--extra-index-url` for private packages.
- Ignoring Django patch releases because "we're on 5.2 LTS" — LTS means *receiving* patches,
  which still have to be applied.
- Running an end-of-life Django (e.g. 4.1, 5.0, 5.1 after their support ended).
- Copying `.env` into the image or passing secrets as Docker build args.
- Silencing scanner findings permanently without justification or expiry.
- Installing a package because it appeared in a code suggestion without checking it exists and is
  the intended project (AI-suggested non-existent names are a known squatting vector, "slopsquatting").
- Running containers as root with a writable application directory.
- Giving the CI `GITHUB_TOKEN` default write permissions on all workflows.

## 10. Troubleshooting and validation

**New-package review checklist**

- [ ] Exact name verified on PyPI and in the project's docs/repository link (no typo).
- [ ] Maintained: recent releases, responsive issues, more than one maintainer if critical.
- [ ] Supports the project's Django/Python versions (classifiers, CI matrix).
- [ ] Licence compatible.
- [ ] No known advisories (`pip-audit`, OSV query).
- [ ] Transitive dependencies acceptable (lock diff reviewed).
- [ ] Need justified — the functionality isn't already in Django/DRF or the stdlib.
- [ ] Wheels available; no surprising install-time behaviour in `setup.py`/build hooks for sdists.

**Diagnostics**

- `pip install --require-hashes` fails with "hashes do not match": the artifact changed or a
  different platform wheel was chosen — re-lock on the target platform; never just delete hashes.
- `pip-audit` reports a vuln with no fix: check affected versions/functions; mitigate or record an
  expiring ignore.
- Verify the deployed image matches the lock: `pip freeze` inside the container vs the lock.
- Confirm supported versions: `python -m django --version`; compare with the supported-versions
  table; check DRF release notes for compatibility.
- `git log -p | gitleaks detect --pipe` or `gitleaks detect` over full history after onboarding
  a legacy repository.

## 11. Related references and skills

- [security-testing-and-incident-response.md](./security-testing-and-incident-response.md) — SAST/DAST methodology, triage, incident handling.
- [secrets-and-cryptography.md](./secrets-and-cryptography.md) — rotating a leaked secret.
- [secure-django-configuration.md](./secure-django-configuration.md) — `check --deploy` gate content.
- [security-fundamentals.md](./security-fundamentals.md) — supported versions and release policy.
- [django-core production-configuration](../../django-core/references/production-configuration.md) — settings modules and `collectstatic`.
- [django-architecture dependency-management](../../django-architecture/references/dependency-management.md) — code-level dependency design (distinct from packages).
- [django-testing](../../django-testing/SKILL.md) — CI test layout.

## 12. Official documentation and external references

- Django supported versions: https://www.djangoproject.com/download/#supported-versions
- Django security policies and release process: https://docs.djangoproject.com/en/dev/internals/security/
- Django security archive: https://docs.djangoproject.com/en/dev/releases/security/
- DRF release notes: https://www.django-rest-framework.org/community/release-notes/
- pip secure installs / hash-checking mode: https://pip.pypa.io/en/stable/topics/secure-installs/
- pip-audit: https://github.com/pypa/pip-audit
- PyPA Advisory Database: https://github.com/pypa/advisory-database · OSV: https://osv.dev/
- pip-tools: https://pip-tools.readthedocs.io/ · uv: https://docs.astral.sh/uv/
- PyPI Trusted Publishing: https://docs.pypi.org/trusted-publishers/
- PEP 740 (index support for digital attestations): https://peps.python.org/pep-0740/
- CycloneDX: https://cyclonedx.org/ · SPDX: https://spdx.dev/
- OWASP Software Supply Chain Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Software_Supply_Chain_Security_Cheat_Sheet.html
- OWASP Vulnerable Dependency Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Vulnerable_Dependency_Management_Cheat_Sheet.html
- OWASP Docker Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- OWASP CI/CD Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/CI_CD_Security_Cheat_Sheet.html
- SLSA framework: https://slsa.dev/
- GitHub Actions security hardening: https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
- gitleaks: https://github.com/gitleaks/gitleaks · TruffleHog: https://github.com/trufflesecurity/trufflehog
- Bandit: https://bandit.readthedocs.io/ · Semgrep: https://semgrep.dev/docs/
