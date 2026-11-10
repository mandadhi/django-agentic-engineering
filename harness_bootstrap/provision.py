"""Setup plan, consent binding, idempotent setup and upgrade, validation, enable and disable.

Consent: ``plan`` changes nothing and returns a ``plan_id`` (a digest of the target directory, its
current state, the plugin distribution, the interpreter, the profile and every planned action).
``setup`` recomputes the plan and refuses unless ``--approve-plan`` equals the recomputed id, so it
only ever performs the exact plan the user approved in Claude Code; anything that changed in
between (a new plugin version, another interpreter, files created meanwhile) needs a new approval.

Safety rules (every one is tested):

* nothing global: the virtual environment is ``.django-engineering/.venv``; pip runs with
  ``PIP_REQUIRE_VIRTUALENV=1`` and only the pinned ``requirements-harness.txt`` of the snapshot;
  no optional dependency is ever installed;
* nothing overwritten: configuration, ``.gitignore`` and runtime snapshots are created only when
  absent; an existing virtual environment is reused or reported, never deleted;
* the database is created once, by the harness's own migrations (``runtime.store.open_store``),
  never recreated: once a database existed, a missing one stops setup (``--new-database`` is the
  explicit way to start an empty history); an upgrade first copies it to ``database/backups/``;
* the harness is enabled only after validation passed; a failed step leaves ``status: failed``
  (or, for an upgrade, the previous runtime in place) and an interrupted one ``provisioning``, and
  the resolver reports both instead of running anything;
* idempotent: setup on a valid installation validates it and changes nothing; an upgrade happens
  only with ``--upgrade``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from . import PLUGIN_ROOT, distribution
from .environment import (Check, Runner, check_credentials, check_disk, check_paths, check_shell_sandbox,
                          check_venv_support, find_python, python_info)
from .layout import (COMMITTABLE, CONFIG_FILE, DEFAULT_PROFILE, GITIGNORE_TEXT, KNOWN_ENTRIES, MANIFEST_NAME,
                     Layout, ManifestError, add_history, manifest_problems, new_manifest, read_manifest, utcnow,
                     write_manifest)
from .project import Detection, detect_project

LOCK_STALE_SECONDS = 2 * 3600
PIP_TIMEOUT = 1800
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def redact_text(text: str) -> str:
    """Credentials embedded in URLs (``https://user:token@index/``) never reach logs or output."""
    return _URL_CREDENTIALS.sub(r"\1***@", text)


def version_tuple(value: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.+-]", value)[:3])


@dataclass
class Options:
    project: Optional[Path] = None
    profile: Optional[str] = None          # None: keep the installation's profile, else "pack"
    python: Optional[str] = None
    upgrade: bool = False
    new_database: bool = False


class SetupFailure(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


# ---------------------------------------------------------------------- invoking the harness


def harness_argv(layout: Layout, runtime_dir: Path, profile: str, *args: str) -> List[str]:
    """``python -m core.cli`` of the repository's runtime snapshot, in its own virtual environment.

    ``-E -s``: no PYTHON* variables and no user site-packages can change what is imported;
    ``-X utf8``: output encoding is independent of the console code page. The working directory is
    the snapshot (see :func:`harness_env`), so ``core``/``runtime``/``agent_runtime`` come from it.
    """
    return [str(layout.python), "-X", "utf8", "-E", "-s", "-m", "core.cli", "--root", str(runtime_dir),
            "--framework-file", str(layout.config_file), "--profile", profile, *args]


def probe_argv(layout: Layout, runtime_dir: Path, command: str, *args: str) -> List[str]:
    return [str(layout.python), "-X", "utf8", "-E", "-s", "-m", "harness_bootstrap.probe", command, *args]


def harness_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.pop("DAE_HARNESS_PROFILE", None)        # the manifest's profile is passed explicitly
    return env


def _probe(runner: Runner, layout: Layout, runtime_dir: Path, command: str, *args: str,
           timeout: float = 300) -> Dict[str, Any]:
    result = runner.run(probe_argv(layout, runtime_dir, command, *args), cwd=runtime_dir, env=harness_env(),
                        timeout=timeout)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = {"ok": False, "error": redact_text((result.stderr or result.stdout or "no output").strip()[-2000:])}
    doc["returncode"] = result.returncode
    return doc


def _common(layout: Layout, profile: str, runtime_dir: Path) -> List[str]:
    return ["--root", str(runtime_dir), "--framework-file", str(layout.config_file), "--profile", profile]


def validate_runtime(runner: Runner, layout: Layout, runtime_dir: Path, profile: str) -> List[Check]:
    """``core.cli validate`` plus the readiness probe, both from the repository's runtime."""
    checks: List[Check] = []
    result = runner.run(harness_argv(layout, runtime_dir, profile, "validate"), cwd=runtime_dir, env=harness_env(),
                        timeout=300)
    text = redact_text((result.stdout + result.stderr).strip())
    if result.returncode == 0:
        checks.append(Check("harness-validate", "ok", text.splitlines()[0] if text else "configuration valid"))
    else:
        hint = ("the full profile requires the optional parent-repository plugins under .django-engineering/plugins/; "
                "use the pack profile" if profile == "full" else "fix .django-engineering/config/framework-config.yaml "
                "or run /django-engineering:setup-harness again")
        checks.append(Check("harness-validate", "fail", text[-3000:] or f"exit code {result.returncode}", hint))
    ready = _probe(runner, layout, runtime_dir, "readiness", *_common(layout, profile, runtime_dir))
    if "checks" in ready:
        checks += [Check(c["name"], c["status"], c["detail"], c.get("remediation", "")) for c in ready["checks"]]
    else:
        checks.append(Check("readiness", "fail", ready.get("error", "readiness probe failed"),
                            "run /django-engineering:setup-harness to repair the environment"))
    return checks


# ---------------------------------------------------------------------- plan


def _lock_state(layout: Layout) -> Optional[Dict[str, Any]]:
    if not layout.lock.is_file():
        return None
    try:
        info = json.loads(layout.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    age = time.time() - layout.lock.stat().st_mtime
    return {"pid": info.get("pid"), "started_at": info.get("started_at"), "age_seconds": int(age),
            "stale": age > LOCK_STALE_SECONDS}


def _foreign_entries(layout: Layout) -> List[str]:
    if not layout.enclave.is_dir():
        return []
    return sorted(name for name in os.listdir(layout.enclave) if name not in KNOWN_ENTRIES
                  and not name.startswith((".installation.", f"{MANIFEST_NAME}.unreadable-")))


def build_plan(options: Options, *, plugin_root: Path = PLUGIN_ROOT, runner: Optional[Runner] = None,
               cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Everything setup would do, and every reason it cannot. Changes nothing."""
    runner = runner or Runner()
    detection = detect_project(cwd, explicit=options.project, plugin_root=plugin_root)
    plan: Dict[str, Any] = {"project": detection.to_dict(), "checks": [], "actions": [], "blocked": [],
                            "warnings": [], "state": None, "profile": None, "plugin_version": None,
                            "distribution": None, "python": None, "will_enable": False, "next": "setup"}
    blocked, actions, checks = plan["blocked"], plan["actions"], plan["checks"]

    def check(item: Check) -> None:
        checks.append(item.to_dict())
        if item.status == "fail":
            blocked.append(f"{item.name}: {item.detail}" + (f" -> {item.remediation}" if item.remediation else ""))
        elif item.status == "warn":
            plan["warnings"].append(f"{item.name}: {item.detail}")

    if not detection.confirmed:
        plan["state"] = detection.confidence
        plan["next"] = "confirm-project"
        blocked.append(f"project: {detection.reason}")
        return _finish(plan, options)
    root = detection.root
    assert root is not None
    layout = Layout(root)

    unavailable = distribution.availability(plugin_root)
    if unavailable:
        plan["state"] = "unavailable"
        blocked.append(f"distribution: {unavailable}")
        return _finish(plan, options)
    described = distribution.describe(plugin_root)
    snapshot = distribution.snapshot_name(described["version"], described["digest"])
    plan["plugin_version"] = described["version"]
    plan["distribution"] = {"version": described["version"], "digest": described["digest"],
                            "files": len(described["files"]), "snapshot": f"runtime/{snapshot}"}

    manifest: Optional[Dict[str, Any]] = None
    if layout.manifest.is_file():
        try:
            manifest = read_manifest(layout.manifest)
        except ManifestError as exc:
            plan["state"] = "conflict"
            blocked.append(f"manifest: {exc}. Setup never overwrites it; fix it, or reset it with "
                           "/django-engineering:disable-harness")
            return _finish(plan, options)
        structural = [p for p in manifest_problems(manifest) if "runtime" not in p and "harness_version" not in p]
        if structural:
            plan["state"] = "conflict"
            blocked.append("manifest: " + "; ".join(structural))
            return _finish(plan, options)
    foreign = _foreign_entries(layout)
    if foreign:
        plan["state"] = "conflict"
        blocked.append(f"{layout.enclave} holds files setup did not create ({', '.join(foreign)}); nothing there is "
                       "overwritten. Move them, or choose another project directory.")
        return _finish(plan, options)
    lock = _lock_state(layout)
    if lock and not lock["stale"]:
        blocked.append(f"another setup holds {layout.lock} (pid {lock['pid']}, started {lock['started_at']}). If none "
                       "is running, delete that file and plan again.")

    profile = options.profile or (manifest or {}).get("profile") or DEFAULT_PROFILE
    plan["profile"] = profile
    if manifest is not None and options.profile and options.profile != manifest.get("profile"):
        actions.append(f"switch the profile from {manifest.get('profile')} to {options.profile}")
    if profile == "full":
        plan["warnings"].append("profile full: the optional parent-repository plugins are required and are never "
                                "copied into your project; validation fails unless you provide them yourself")

    venv_ok = False
    if layout.python.is_file():
        info = python_info([str(layout.python)], runner)
        if info is None or tuple(info["version"][:2]) < (3, 11):
            blocked.append(f"existing virtual environment {layout.venv} does not run Python 3.11+; it is never deleted. "
                           "Remove or repair it yourself, then plan again.")
        else:
            venv_ok = True
            plan["python"] = {"venv": str(layout.python), "version": ".".join(map(str, info["version"]))}
            checks.append(Check("venv", "ok", f"reusing {layout.venv} (Python {plan['python']['version']})").to_dict())
    elif layout.venv.exists():
        blocked.append(f"{layout.venv} exists but is not a virtual environment; it is never overwritten")

    state: str
    installed = manifest is not None and manifest.get("status") == "installed"
    if manifest is None:
        state = "adopt-config" if layout.enclave.exists() else "fresh"
    elif not installed:
        state = "resume"
    else:
        runtime = manifest.get("runtime") or {}
        current_version = str(runtime.get("version") or manifest.get("harness_version") or "0")
        if runtime.get("digest") == described["digest"]:
            try:
                runtime_present = layout.inside(str(runtime.get("path"))).is_dir()
            except ValueError:
                runtime_present = False
            complete = all(p.is_file() for p in (layout.python, layout.config_file, layout.database))
            # A damaged installation of the current version is repaired in place (same rules as a resume).
            state = "installed" if (complete and runtime_present) else "repair"
        elif version_tuple(current_version) > version_tuple(described["version"]):
            state = "installed-newer"
            blocked.append(f"the repository runs harness {current_version}, newer than this plugin "
                           f"({described['version']}); update the plugin. Downgrades are not performed.")
        elif options.upgrade:
            state = "upgrade"
        else:
            state = "upgrade-available"
            plan["next"] = "upgrade"
            blocked.append(f"the repository runs harness {current_version} ({runtime.get('path')}); this plugin ships "
                           f"{described['version']} ({snapshot}). Runtimes are never upgraded silently: approve an "
                           "upgrade with --upgrade (database and configuration are kept).")
    plan["state"] = state

    if not venv_ok and state not in ("installed", "installed-newer"):
        base, found = find_python(options.python, runner)
        check(found)
        if base is not None:
            plan["python"] = {"base": base["executable"], "version": ".".join(map(str, base["version"]))}
            for item in check_venv_support([base["executable"]], runner):
                check(item)
    for item in check_paths(root, distribution.deepest_path(described)):
        check(item)
    check(check_disk(root, needs_environment=not venv_ok))
    check(check_credentials())
    checks.append(check_shell_sandbox().to_dict())

    database_existed = bool(manifest and manifest.get("database_created_at")) or layout.database.is_file()
    if state == "installed":
        plan["next"] = "validate"
        actions.append("validate the existing installation (nothing is changed)")
    elif state in ("fresh", "adopt-config", "resume", "repair", "upgrade"):
        if state == "repair":
            actions.append("repair the installation: recreate only what is missing (nothing existing is replaced)")
        if state == "upgrade":
            old = (manifest or {}).get("runtime") or {}
            actions.append(f"upgrade the runtime from {old.get('path')} to runtime/{snapshot} (the old snapshot is kept)")
        else:
            actions.append(f"create {layout.enclave} (config/, runtime/, database/, artifacts/, logs/) and "
                           f"{MANIFEST_NAME} (status provisioning, harness disabled until validated)")
            if not (layout.enclave / ".gitignore").exists():
                actions.append("write .django-engineering/.gitignore (ignores everything except "
                               f"{', '.join(COMMITTABLE)})")
        actions.append(f"copy the harness distribution ({len(described['files'])} files, plugin {described['version']}) "
                       f"to runtime/{snapshot} unless it already exists and verifies")
        if venv_ok:
            actions.append(f"reuse the virtual environment {layout.venv}")
        else:
            actions.append(f"create the virtual environment {layout.venv} with "
                           f"{(plan['python'] or {}).get('base', 'Python 3.11+')}")
        actions.append("install the pinned packages of requirements-harness.txt into it, only if they are not "
                       "already installed (" + ", ".join(_pins(plugin_root)) + ")")
        if layout.config_file.is_file():
            actions.append(f"keep the existing {CONFIG_FILE}")
        else:
            actions.append(f"create {CONFIG_FILE} from the canonical framework configuration")
        if state == "upgrade" and layout.database.is_file():
            actions.append("copy database/harness.db to database/backups/ before migrating it")
        if database_existed and not layout.database.is_file():
            if options.new_database:
                actions.append("create a NEW, empty execution database (--new-database; the previous history is gone)")
            else:
                blocked.append(f"the execution database {layout.database} existed before and is missing; it is never "
                               "recreated silently. Restore it (database/backups/) or approve an empty history with "
                               "--new-database.")
        else:
            actions.append("open or create database/harness.db through the harness's own migrations "
                           "(existing history is kept)")
        actions.append("validate: core configuration, registries, orchestration policy, dependencies, database")
        disabled = bool(manifest and manifest.get("disabled_by_user"))
        plan["will_enable"] = not disabled
        actions.append("enable the harness for this repository only after validation passes" if not disabled else
                       "keep the harness disabled (it was disabled explicitly; enable it afterwards if you want)")
    return _finish(plan, options)


def _pins(plugin_root: Path) -> List[str]:
    lines = (plugin_root / "requirements-harness.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _finish(plan: Dict[str, Any], options: Options) -> Dict[str, Any]:
    if plan["blocked"] and plan["next"] == "setup":
        plan["next"] = "fix-blockers"
    key = {"root": plan["project"]["root"], "state": plan["state"], "distribution": plan["distribution"],
           "python": plan["python"], "profile": plan["profile"], "actions": plan["actions"],
           "blocked": plan["blocked"], "upgrade": options.upgrade, "new_database": options.new_database}
    plan["plan_id"] = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return plan


# ---------------------------------------------------------------------- setup


class _Lock:
    def __init__(self, layout: Layout) -> None:
        self.path = layout.lock
        self.held = False

    def __enter__(self) -> "_Lock":
        state = _lock_state_path(self.path)
        if state == "stale":
            self.path.unlink()
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SetupFailure("lock", f"another setup holds {self.path}; if none is running, delete it") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": utcnow()}, handle)
        self.held = True
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def _lock_state_path(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return "stale" if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS else "held"


class _Log:
    def __init__(self, layout: Layout, out: TextIO) -> None:
        self.out = out
        self.path: Optional[Path] = None
        self.layout = layout

    def open(self) -> None:
        self.layout.logs.mkdir(parents=True, exist_ok=True)
        self.path = self.layout.logs / f"setup-{time.strftime('%Y%m%d-%H%M%S')}.log"

    def __call__(self, message: str, *, quiet: bool = False) -> None:
        text = redact_text(message)
        if not quiet:
            print(text, file=self.out, flush=True)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utcnow()} {text}\n")


_PINS_CHECK = """\
import importlib.metadata as m, sys
bad = []
for line in open(sys.argv[1], encoding="utf-8"):
    spec = line.split("#")[0].strip()
    if "==" not in spec:
        continue
    name, version = [s.strip() for s in spec.split("==", 1)]
    try:
        found = m.version(name)
    except m.PackageNotFoundError:
        found = None
    if found != version:
        bad.append(name)
sys.exit(1 if bad else 0)
"""


def _pins_satisfied(runner: Runner, python: Path, requirements: Path) -> bool:
    return runner.run([str(python), "-I", "-c", _PINS_CHECK, str(requirements)], timeout=60).returncode == 0


def _backup_database(layout: Layout, label: str) -> Optional[Path]:
    if not layout.database.is_file():
        return None
    layout.backups.mkdir(parents=True, exist_ok=True)
    target = layout.backups / f"harness-{time.strftime('%Y%m%d-%H%M%S')}-before-{label}.db"
    source = sqlite3.connect(f"file:{layout.database.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)                 # consistent copy, WAL included
        finally:
            dest.close()
    finally:
        source.close()
    return target


def run_setup(options: Options, approved_plan: str, *, plugin_root: Path = PLUGIN_ROOT,
              runner: Optional[Runner] = None, out: TextIO = sys.stdout, cwd: Optional[Path] = None,
              fault: Optional[Callable[[str], None]] = None) -> int:
    """Perform the approved plan. Returns 0 ok, 1 failed, 11 blocked, 12 plan changed, 130 interrupted.

    ``fault`` is a test seam called with each step name before it runs.
    """
    runner = runner or Runner()
    plan = build_plan(options, plugin_root=plugin_root, runner=runner, cwd=cwd)
    if plan["blocked"]:
        print("setup is blocked:", file=out)
        for reason in plan["blocked"]:
            print(f"  - {reason}", file=out)
        return 11
    if plan["plan_id"] != approved_plan:
        print(f"the setup plan changed since it was approved (approved {approved_plan}, now {plan['plan_id']}); "
              "nothing was changed. Review the new plan and approve it again.", file=out)
        return 12
    layout = Layout(Path(plan["project"]["root"]))
    if plan["state"] == "installed":
        return validate_installation(layout, runner=runner, out=out, plugin_root=plugin_root)

    upgrading = plan["state"] == "upgrade"
    log = _Log(layout, out)
    layout.enclave.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(layout.manifest) if layout.manifest.is_file() else \
        new_manifest(project=layout.project, plugin_version=plan["plugin_version"], profile=plan["profile"])
    previous_runtime = manifest.get("runtime")
    try:
        with _Lock(layout):
            log.open()
            log(f"setup {plan['plan_id']} for {layout.project} (state {plan['state']}, profile {plan['profile']})")

            def step(name: str, message: str) -> None:
                if fault is not None:
                    fault(name)
                log(f"- {message}")

            if not upgrading:
                manifest.update(status="provisioning", enabled=False, profile=plan["profile"],
                                plugin_version=plan["plugin_version"], last_error=None)
                write_manifest(layout.manifest, manifest)
            step("directories", "creating config/, runtime/, database/, artifacts/, logs/")
            for path in (layout.config_file.parent, layout.runtime_root, layout.database.parent, layout.artifacts,
                         layout.logs):
                path.mkdir(parents=True, exist_ok=True)
            gitignore = layout.enclave / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(GITIGNORE_TEXT, encoding="utf-8")

            step("runtime", f"provisioning {plan['distribution']['snapshot']}")
            try:
                runtime_dir, record, created = distribution.copy_snapshot(plugin_root, layout.runtime_root)
            except distribution.DistributionError as exc:
                raise SetupFailure("runtime", str(exc)) from exc
            log(f"  {'copied' if created else 'verified existing'} {runtime_dir.name} ({len(record['files'])} files)")

            step("venv", "preparing the virtual environment")
            if not layout.python.is_file():
                base = plan["python"]["base"]
                result = runner.run([base, "-I", "-m", "venv", str(layout.venv)], timeout=600)
                if result.returncode != 0 or not layout.python.is_file():
                    raise SetupFailure("venv", redact_text((result.stderr or result.stdout or "").strip()[-1500:])
                                       or "venv creation failed")
                log(f"  created {layout.venv}")
            info = python_info([str(layout.python)], runner)
            if info is None or tuple(info["version"][:2]) < (3, 11):
                raise SetupFailure("venv", f"{layout.python} does not run Python 3.11+")

            step("dependencies", "checking the pinned harness dependencies")
            requirements = runtime_dir / "requirements-harness.txt"
            if _pins_satisfied(runner, layout.python, requirements):
                log("  already installed")
            else:
                log("  installing with pip (this downloads about 100 MB and can take a few minutes)")
                env = harness_env()
                env.update(PIP_REQUIRE_VIRTUALENV="1", PIP_USER="0", PIP_NO_INPUT="1",
                           PIP_DISABLE_PIP_VERSION_CHECK="1")
                result = runner.run([str(layout.python), "-I", "-m", "pip", "install", "-r", str(requirements)],
                                    env=env, timeout=PIP_TIMEOUT)
                log(redact_text((result.stdout or "") + (result.stderr or "")), quiet=True)
                if result.returncode != 0:
                    tail = redact_text((result.stderr or result.stdout or "").strip())[-1500:]
                    raise SetupFailure("dependencies", f"pip could not install the pinned dependencies:\n{tail}\n"
                                       "Check your network or package index; the environment is kept as it is.")
                if not _pins_satisfied(runner, layout.python, requirements):
                    raise SetupFailure("dependencies", "pip finished but the pinned versions are not installed")

            step("config", "repository configuration")
            written = _probe(runner, layout, runtime_dir, "write-config", "--root", str(runtime_dir), "--out",
                             str(layout.config_file))
            if not written.get("ok"):
                raise SetupFailure("config", written.get("error", "could not write the configuration"))
            log(f"  {'created' if written.get('written') else 'kept existing'} {CONFIG_FILE}")

            step("database", "execution database")
            if upgrading:
                backup = _backup_database(layout, distribution.snapshot_name(record["version"], record["digest"]))
                if backup:
                    log(f"  backed up to {backup.relative_to(layout.enclave).as_posix()}")
            must_exist = (bool(manifest.get("database_created_at")) or layout.database.is_file()) \
                and not options.new_database
            args = _common(layout, plan["profile"], runtime_dir) + (["--must-exist"] if must_exist else [])
            db = _probe(runner, layout, runtime_dir, "init-db", *args)
            if not db.get("ok"):
                raise SetupFailure("database", db.get("error") or
                                   f"schema {db.get('schema_version')} != {db.get('expected_schema_version')}")
            log(f"  {'created' if db.get('created') else 'opened'} database (schema {db['schema_version']})")
            if db.get("created"):
                manifest["database_created_at"] = utcnow()

            step("validate", "validating the installation")
            failed = [c for c in validate_runtime(runner, layout, runtime_dir, plan["profile"]) if c.status == "fail"]
            for item in failed:
                log(f"  FAILED {item.name}: {item.detail}")
            if failed:
                raise SetupFailure("validate", "; ".join(f"{c.name}: {c.detail.splitlines()[0] if c.detail else ''}"
                                                         for c in failed))

            step("manifest", "recording and enabling")
            now = utcnow()
            manifest.update(
                status="installed", harness_version=record["version"], plugin_version=plan["plugin_version"],
                profile=plan["profile"], last_error=None,
                runtime={"path": f"runtime/{runtime_dir.name}", "version": record["version"], "digest": record["digest"],
                         "files": len(record["files"])},
                python={"version": ".".join(map(str, info["version"])),
                        "base_executable": (plan.get("python") or {}).get("base")
                        or (manifest.get("python") or {}).get("base_executable")},
            )
            if not manifest.get("installed_at"):
                manifest["installed_at"] = now
            if not manifest.get("disabled_by_user"):
                manifest["enabled"] = True
            if upgrading:
                add_history(manifest, "upgraded", **{"from": (previous_runtime or {}).get("path"),
                                                     "to": manifest["runtime"]["path"]})
            else:
                add_history(manifest, "installed", runtime=manifest["runtime"]["path"], profile=plan["profile"])
            write_manifest(layout.manifest, manifest)
            log(f"harness {'enabled' if manifest['enabled'] else 'installed (kept disabled)'} for {layout.project}")
            return 0
    except SetupFailure as exc:
        log(f"setup failed at step '{exc.step}': {exc.message}")
        _record_failure(layout, manifest, upgrading, previous_runtime, f"{exc.step}: {exc.message.splitlines()[0]}")
        if log.path:
            print(f"setup log: {log.path}", file=out)
        return 1
    except KeyboardInterrupt:
        print("setup interrupted; run /django-engineering:setup-harness again to resume (nothing is lost)", file=out)
        return 130
    except OSError as exc:
        log(f"setup failed: {exc}")
        _record_failure(layout, manifest, upgrading, previous_runtime, f"{type(exc).__name__}: {exc}")
        return 1


def _record_failure(layout: Layout, manifest: Dict[str, Any], upgrading: bool, previous_runtime: Any,
                    message: str) -> None:
    if not layout.enclave.is_dir():
        return
    if upgrading:                                       # the previous runtime stays in place
        manifest["runtime"] = previous_runtime
        add_history(manifest, "upgrade-failed", error=message[:300])
        manifest["last_error"] = message[:500]
    else:
        manifest.update(status="failed", enabled=False, last_error=message[:500])
        add_history(manifest, "setup-failed", error=message[:300])
    try:
        write_manifest(layout.manifest, manifest)
    except OSError:
        pass


# ---------------------------------------------------------------------- validate, enable, disable


def deep_checks(layout: Layout, manifest: Dict[str, Any], *, runner: Runner, plugin_root: Path) -> List[Check]:
    """Runtime integrity, version compatibility and the harness's own validation (read-only)."""
    checks: List[Check] = []
    runtime = manifest.get("runtime") or {}
    try:
        runtime_dir = layout.inside(str(runtime.get("path")))
    except ValueError as exc:
        return [Check("runtime", "fail", f"manifest runtime path: {exc}", "run /django-engineering:setup-harness")]
    problems = distribution.verify_snapshot(runtime_dir) if runtime_dir.is_dir() else [f"{runtime_dir} not found"]
    checks.append(Check("runtime", "fail" if problems else "ok",
                        "; ".join(problems[:5]) if problems else f"{runtime_dir.name} verifies ({runtime.get('files')} files)",
                        "the snapshot was modified or damaged; run /django-engineering:setup-harness --upgrade, which "
                        "provisions a fresh snapshot without touching the database" if problems else ""))
    if distribution.availability(plugin_root) is None:
        current = distribution.describe(plugin_root)
        if current["digest"] != runtime.get("digest"):
            checks.append(Check("compatibility", "fail", f"runtime {runtime.get('version')} differs from this plugin's "
                                f"distribution {current['version']}", "run /django-engineering:setup-harness --upgrade"))
        else:
            checks.append(Check("compatibility", "ok", f"runtime matches plugin {current['version']}"))
    if not layout.python.is_file():
        checks.append(Check("venv", "fail", f"{layout.python} not found", "run /django-engineering:setup-harness"))
        return checks
    info = python_info([str(layout.python)], runner)
    if info is None or tuple(info["version"][:2]) < (3, 11):
        checks.append(Check("venv", "fail", f"{layout.python} does not run Python 3.11+",
                            "repair or remove .django-engineering/.venv yourself, then run setup again"))
        return checks
    checks.append(Check("venv", "ok", f"Python {'.'.join(map(str, info['version']))} ({layout.venv})"))
    if not problems:
        checks += validate_runtime(runner, layout, runtime_dir, str(manifest.get("profile")))
    return checks


def validate_installation(layout: Layout, *, runner: Optional[Runner] = None, out: TextIO = sys.stdout,
                          plugin_root: Path = PLUGIN_ROOT) -> int:
    runner = runner or Runner()
    manifest = read_manifest(layout.manifest)
    print(f"the harness is already installed in {layout.enclave} (runtime {manifest['runtime']['path']}, "
          f"profile {manifest['profile']}, {'enabled' if manifest['enabled'] else 'disabled'}); validating it, "
          "nothing is changed", file=out)
    checks = deep_checks(layout, manifest, runner=runner, plugin_root=plugin_root)
    for item in checks:
        print(f"  [{item.status}] {item.name}: {item.detail.splitlines()[0] if item.detail else ''}", file=out)
    return 1 if any(c.status == "fail" for c in checks) else 0


def enable(layout: Layout, *, runner: Optional[Runner] = None, out: TextIO = sys.stdout,
           plugin_root: Path = PLUGIN_ROOT) -> int:
    runner = runner or Runner()
    if not layout.manifest.is_file():
        print(f"no harness installation in {layout.project}; run /django-engineering:setup-harness", file=out)
        return 11
    try:
        manifest = read_manifest(layout.manifest)
    except ManifestError as exc:
        print(f"cannot enable: {exc}", file=out)
        return 13
    structural = manifest_problems(manifest)
    if manifest.get("status") != "installed" or structural:
        print("cannot enable: setup has not completed (" + "; ".join(structural or [f"status {manifest.get('status')}"])
              + "); run /django-engineering:setup-harness", file=out)
        return 13
    checks = deep_checks(layout, manifest, runner=runner, plugin_root=plugin_root)
    failed = [c for c in checks if c.status == "fail"]
    for item in checks:
        print(f"  [{item.status}] {item.name}: {item.detail.splitlines()[0] if item.detail else ''}", file=out)
    if failed:
        print("not enabled: validation failed (see above); nothing was changed", file=out)
        return 1
    manifest.update(enabled=True, disabled_by_user=False)
    add_history(manifest, "enabled")
    write_manifest(layout.manifest, manifest)
    print(f"harness enabled for {layout.project}; /orchestrate now runs through the Agentic Harness here", file=out)
    return 0


def disable(layout: Layout, *, reset_unreadable: bool = False, out: TextIO = sys.stdout) -> int:
    if not layout.manifest.is_file():
        print(f"no harness installation in {layout.project}; it already uses the standalone plugin", file=out)
        return 0
    try:
        manifest = read_manifest(layout.manifest)
    except ManifestError as exc:
        if not reset_unreadable:
            print(f"cannot disable: {exc}\nThe manifest is never overwritten silently. Re-run with "
                  "--reset-unreadable-manifest to keep it as installation.json.unreadable-<time> and write a disabled "
                  "manifest (runtime, database and history are kept).", file=out)
            return 13
        kept = layout.manifest.with_name(f"{MANIFEST_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(layout.manifest, kept)
        manifest = new_manifest(project=layout.project, plugin_version="unknown", profile=DEFAULT_PROFILE)
        manifest.update(status="failed", last_error=f"unreadable manifest kept as {kept.name}")
        print(f"kept the unreadable manifest as {kept.name}", file=out)
    manifest.update(enabled=False, disabled_by_user=True)
    add_history(manifest, "disabled")
    write_manifest(layout.manifest, manifest)
    print(f"harness disabled for {layout.project}; /orchestrate uses the standalone plugin here. The runtime, "
          "configuration and execution history are kept; /django-engineering:enable-harness turns it back on.", file=out)
    return 0
