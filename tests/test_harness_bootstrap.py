"""Phase 5.6: opt-in, repository-local Agentic Harness integration (harness_bootstrap/).

Covers the default standalone behaviour (nothing is created, imported or started), project
detection, the execution-mode resolver, consent-bound idempotent setup and upgrade with their
failure modes, enable/disable, the runtime adapter, and harness execution from a repository-local
installation through the existing ExecutionManager, AgentRunner, ToolGuard and SQLite store.

Setup tests use :class:`FakeRunner`: the virtual environment's interpreter is this test interpreter
(which has the pinned dependencies), venv creation writes a placeholder, and pip is simulated. The
runtime snapshot, configuration, database migrations, ``core.cli validate`` and the readiness probe
run for real, from the copied snapshot, as subprocesses.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from core import HARNESS_HOME
from harness_bootstrap import PLUGIN_ROOT, adapter, distribution, mode, project
from harness_bootstrap.environment import Runner, check_paths
from harness_bootstrap.layout import (ENCLAVE_NAME, GITIGNORE_TEXT, Layout, ManifestError, manifest_problems,
                                      new_manifest, path_problem, read_manifest, venv_python, write_manifest)
from harness_bootstrap.provision import (_PINS_CHECK, Options, build_plan, disable, enable, harness_argv,
                                         redact_text, run_setup)

ROOT = HARNESS_HOME
SCRIPT = PLUGIN_ROOT / "harness_bootstrap" / "django_harness.py"
HARNESS_COMMANDS = ("setup-harness", "harness-status", "enable-harness", "disable-harness", "doctor")


# ---------------------------------------------------------------------- helpers


class FakeRunner(Runner):
    """Real subprocesses, except: venv creation, pip install and (optionally) interpreter discovery."""

    def __init__(self, *, python_ok: bool = True, venv_ok: bool = True, pip_ok: bool = True, pip_rc: int = 0,
                 pip_output: str = "", preinstalled: bool = False) -> None:
        self.python_ok, self.venv_ok, self.pip_ok = python_ok, venv_ok, pip_ok
        self.pip_rc, self.pip_output = pip_rc, pip_output
        self.installed = preinstalled
        self.calls: list[list[str]] = []

    @staticmethod
    def _done(argv, code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, code, out, err)

    def run(self, argv, *, cwd=None, env=None, capture=True, timeout=120):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        rest = argv[1:]
        if rest[:3] == ["-I", "-m", "venv"]:
            target = Path(rest[3])
            python = venv_python(target)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("placeholder", encoding="utf-8")
            return self._done(argv)
        in_venv = ENCLAVE_NAME in argv[0]
        if not in_venv:
            if not self.python_ok:
                return self._done(argv, 1, "", "python: not found")
            if "import venv, ensurepip" in rest and not self.venv_ok:
                return self._done(argv, 1, "", "No module named ensurepip")
            if rest[:4] == ["-I", "-m", "pip", "--version"] and not self.pip_ok:
                return self._done(argv, 1, "", "No module named pip")
        else:
            if rest[:4] == ["-I", "-m", "pip", "install"]:
                self.installed = self.pip_rc == 0
                return self._done(argv, self.pip_rc, "Collecting PyYAML", self.pip_output)
            if rest[:3] == ["-I", "-c", _PINS_CHECK]:
                return self._done(argv, 0 if self.installed else 1)
        return subprocess.run([sys.executable, *rest], cwd=str(cwd) if cwd else None, env=env,
                              capture_output=capture, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)


def make_project(base: Path, name: str = "proj", *, git: bool = True) -> Path:
    path = base / name
    (path / "app").mkdir(parents=True)
    (path / "manage.py").write_text("import sys\n", encoding="utf-8")
    (path / "app" / "models.py").write_text("x = 1\n", encoding="utf-8")
    if git:
        (path / ".git").mkdir()
    return path


def snapshot(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def plan(proj: Path, runner: Runner | None = None, **options: Any) -> dict:
    return build_plan(Options(project=proj, **options), runner=runner or FakeRunner())


def setup(proj: Path, runner: FakeRunner | None = None, *, plugin_root: Path = PLUGIN_ROOT, fault=None,
          **options: Any) -> tuple[int, str, dict]:
    runner = runner or FakeRunner()
    approved = build_plan(Options(project=proj, **options), runner=runner, plugin_root=plugin_root)
    out = io.StringIO()
    code = run_setup(Options(project=proj, **options), approved["plan_id"], runner=runner, out=out,
                     plugin_root=plugin_root, fault=fault)
    return code, out.getvalue(), approved


def plugin_copy(base: Path, version: str, *, change: bool = True) -> Path:
    """A plugin distribution with another version (and different content), for upgrade tests."""
    dest = base / f"plugin-{version}"
    for rel, path in distribution.distribution_files(PLUGIN_ROOT):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    manifest = json.loads((dest / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if change:
        with (dest / "references" / "django-testing-strategy.md").open("a", encoding="utf-8") as handle:
            handle.write("\n<!-- upgraded -->\n")
    return dest


def harness_cli(argv: list[str], **kwargs) -> tuple[int, str]:
    from core.cli import main

    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv, **kwargs)
    return code, out.getvalue()


def cli_args(proj: Path) -> list[str]:
    """What the adapter passes to ``core.cli`` for this repository (without the interpreter)."""
    resolution = mode.resolve(proj)
    argv = harness_argv(resolution.layout, resolution.runtime_dir, resolution.profile)
    return argv[argv.index("core.cli") + 1:]


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> Path:
    """One repository set up once (read-only tests share it)."""
    proj = make_project(tmp_path_factory.mktemp("installed"))
    code, out, _ = setup(proj)
    assert code == 0, out
    return proj


# ====================================================================== default: standalone


def test_fresh_repository_is_standalone_and_nothing_is_created(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    before = snapshot(proj)
    resolution = mode.resolve(proj / "app")
    assert resolution.mode == mode.STANDALONE and resolution.project == proj.resolve()
    assert resolution.to_dict()["routes_to"] == "standalone plugin"
    assert snapshot(proj) == before and not (proj / ENCLAVE_NAME).exists()


def test_planning_changes_nothing(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    before = snapshot(proj)
    doc = plan(proj)
    assert doc["state"] == "fresh" and doc["actions"] and doc["plan_id"]
    assert snapshot(proj) == before and not (proj / ENCLAVE_NAME).exists()


def test_mode_resolution_never_imports_or_starts_the_harness(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    code = (
        "import json, subprocess, sys\n"
        f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
        "def refuse(*a, **k): raise AssertionError('a subprocess was started')\n"
        "subprocess.run = subprocess.Popen = refuse\n"
        "from harness_bootstrap import mode\n"
        f"r = mode.resolve(__import__('pathlib').Path({str(proj)!r}))\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'core', 'runtime', 'agent_runtime', 'claude_agent_sdk', 'yaml', 'jsonschema'})\n"
        "print(json.dumps({'mode': r.mode, 'heavy': heavy}))\n")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"mode": "standalone", "heavy": []}


def test_cli_mode_in_a_fresh_repository(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), "mode", "--json"], cwd=str(proj), capture_output=True,
                            text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "standalone"
    assert not (proj / ENCLAVE_NAME).exists()


def test_bootstrap_is_standard_library_only() -> None:
    allowed = set(sys.stdlib_module_names) | {"harness_bootstrap", "__future__"}
    for path in sorted((PLUGIN_ROOT / "harness_bootstrap").glob("*.py")):
        if path.name == "probe.py":                 # runs inside the harness environment, by design
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
        assert imported <= allowed, (path.name, imported - allowed)


def test_plugin_never_sets_up_the_harness_by_itself() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse"}                        # no SessionStart / setup hook
    assert all("django_pack_guard.py" in h["args"][0] for event in hooks.values() for m in event for h in m["hooks"])
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "hooks" not in manifest and "mcpServers" not in manifest
    for path in (ROOT / "commands").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if path.stem != "setup-harness":
            assert " setup --approve-plan" not in text, path.name


# ====================================================================== commands and packaging


def test_harness_commands_are_registered_and_documented() -> None:
    from core.frontmatter import read_markdown

    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    for name in HARNESS_COMMANDS:
        assert f"./commands/{name}.md" in manifest["commands"]
        meta, body = read_markdown(ROOT / "commands" / f"{name}.md")
        assert meta["name"] == name and meta["description"].strip()
        assert not {"allowed-tools", "tools", "model", "hooks", "permissionMode"} & set(meta)   # grants nothing
        assert '"${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py"' in body
        for section in ("## Purpose", "## Arguments", "## Authorization", "## Steps", "## Expected output",
                        "## Completion criteria"):
            assert section in body, (name, section)
    readme = (ROOT / "commands" / "README.md").read_text(encoding="utf-8")
    assert all(f"/django-engineering:{name}" in readme for name in HARNESS_COMMANDS)


def test_setup_command_requires_approval_of_the_exact_plan() -> None:
    body = (ROOT / "commands" / "setup-harness.md").read_text(encoding="utf-8")
    assert "--approve-plan <plan_id>" in body and "approve" in body.lower()
    assert "never pass `--approve-plan` with a plan id the user has not approved" in body


def test_orchestrate_resolves_the_mode_and_never_falls_back() -> None:
    body = (ROOT / "commands" / "orchestrate.md").read_text(encoding="utf-8")
    assert "django_harness.py\" mode --json" in body
    assert "Do not fall back to\n  standalone execution" in body
    assert "Never retry a failed harness execution in standalone mode" in body
    assert "--task-stdin" in body and "<<'DJANGO_HARNESS_TASK'" in body       # request never shell-interpreted
    assert "--by \"<their name>\"" in body                                     # a human approver, by name


def test_staged_bundle_ships_the_bootstrap_but_reports_no_distribution(tmp_path: Path) -> None:
    import plugin_packaging.build as packaging

    report = packaging.stage(tmp_path / "bundle")
    assert report.ok, report.problems
    bundle = tmp_path / "bundle"
    assert (bundle / "harness_bootstrap" / "django_harness.py").is_file()
    assert not (bundle / "core").exists()
    assert "does not include the harness distribution" in distribution.availability(bundle)
    assert distribution.availability(PLUGIN_ROOT) is None


def test_standalone_guard_protects_the_installation_directory(tmp_path: Path) -> None:
    from test_orchestration import decide, verdict

    proj = make_project(tmp_path)
    (proj / ENCLAVE_NAME / "config").mkdir(parents=True)
    target = proj / ENCLAVE_NAME / "config" / "framework-config.yaml"
    target.write_text("x: 1\n", encoding="utf-8")
    for tool, tool_input in (("Write", {"file_path": str(target), "content": "x"}),
                             ("Edit", {"file_path": str(target), "old_string": "1", "new_string": "2"}),
                             ("Read", {"file_path": str(proj / ENCLAVE_NAME / "installation.json")}),
                             ("Bash", {"command": "rm -rf .django-engineering"})):
        assert verdict(decide("django-model-agent", tool, tool_input, proj)) == "deny", tool
    # the project itself stays writable for a workspace-write agent
    assert verdict(decide("django-model-agent", "Write", {"file_path": str(proj / "app" / "x.py"), "content": "1"},
                          proj)) != "deny"


# ====================================================================== project detection


def test_detects_the_git_root_from_a_subdirectory(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    found = project.detect_project(proj / "app")
    assert found.root == proj.resolve() and found.confidence == "high" and found.confirmed


def test_monorepo_with_nested_manage_py_needs_confirmation(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    (repo / ".git").mkdir(parents=True)
    backend = repo / "backend"
    backend.mkdir()
    (backend / "manage.py").write_text("", encoding="utf-8")
    found = project.detect_project(backend)
    assert not found.confirmed and found.confidence == "ambiguous"
    assert found.candidates == [repo.resolve(), backend.resolve()]
    doc = build_plan(Options(), runner=FakeRunner(), cwd=backend)
    assert doc["next"] == "confirm-project" and not (repo / ENCLAVE_NAME).exists()
    assert project.detect_project(None, explicit=backend).root == backend.resolve()


def test_non_git_projects(tmp_path: Path) -> None:
    proj = make_project(tmp_path, git=False)
    found = project.detect_project(proj / "app")
    assert found.root == proj.resolve() and found.confidence == "medium" and not found.to_dict()["is_git_repository"]
    empty = tmp_path / "empty"
    empty.mkdir()
    lost = project.detect_project(empty)
    assert lost.confidence == "low" and not lost.confirmed


def test_never_targets_home_filesystem_root_plugin_or_installation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert project.detect_project(None, explicit=tmp_path).confidence == "refused"
    anchor = Path(tmp_path.anchor)
    assert "filesystem root" in project.refusal(anchor)
    assert "plugin installation" in project.refusal(PLUGIN_ROOT.resolve())
    assert "plugin installation" in project.refusal((PLUGIN_ROOT / "agents").resolve())
    proj = make_project(tmp_path)
    inner = proj / ENCLAVE_NAME / "runtime"
    inner.mkdir(parents=True)
    assert project.detect_project(None, explicit=inner).root == proj.resolve()       # owner of the enclave


def test_installations_in_home_are_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ENCLAVE_NAME).mkdir()
    (tmp_path / ENCLAVE_NAME / "installation.json").write_text("{}", encoding="utf-8")
    proj = make_project(tmp_path, git=False)
    assert project.find_installation(proj / "app") is None


def test_paths_with_spaces_and_separators(tmp_path: Path) -> None:
    proj = make_project(tmp_path, "my project")
    spelled = str(proj / "app" / "..") + os.sep
    assert project.detect_project(None, explicit=Path(spelled)).root == proj.resolve()
    assert path_problem("runtime/0.6.0-abc") is None
    for bad in ("..\\x", "C:/x", "/etc/passwd", "runtime/../../x", "runtime\\x", ""):
        assert path_problem(bad), bad


def test_windows_path_length_check() -> None:
    long_project = Path("C:/" + "d" * 150)
    checks = {c.name: c for c in check_paths(long_project, 120, windows=True, long_paths=False)}
    assert checks["path-length"].status == "fail" and "LongPathsEnabled" in checks["path-length"].remediation
    assert {c.name: c for c in check_paths(long_project, 120, windows=True, long_paths=True)}["path-length"].status == "ok"
    short = {c.name: c for c in check_paths(Path("C:/p"), 120, windows=True, long_paths=False)}
    assert short["path-length"].status == "ok"
    assert "path-length" not in {c.name for c in check_paths(Path("/srv/p"), 120, windows=False)}


# ====================================================================== mode resolution


def fake_install(proj: Path, **fields: Any) -> Layout:
    """A structurally complete installation without running setup (resolver tests)."""
    layout = Layout(proj.resolve())
    described = distribution.describe(PLUGIN_ROOT)
    name = distribution.snapshot_name(described["version"], described["digest"])
    runtime = layout.runtime(name)
    runtime.mkdir(parents=True)
    (runtime / distribution.RECORD).write_text(json.dumps(described), encoding="utf-8")
    for path in (layout.python, layout.config_file, layout.database):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    manifest = new_manifest(project=layout.project, plugin_version=described["version"], profile="pack")
    manifest.update(status="installed", enabled=True, harness_version=described["version"],
                    runtime={"path": f"runtime/{name}", "version": described["version"],
                             "digest": described["digest"], "files": len(described["files"])})
    manifest.update(fields)
    write_manifest(layout.manifest, manifest)
    return layout


def test_valid_enabled_installation_selects_the_harness(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    fake_install(proj)
    resolution = mode.resolve(proj / "app")
    assert resolution.mode == mode.HARNESS_ENABLED, resolution.problems
    assert resolution.to_dict()["routes_to"] == "Agentic Harness"


def test_disabled_installation_selects_standalone(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    fake_install(proj, enabled=False, disabled_by_user=True)
    assert mode.resolve(proj).mode == mode.HARNESS_DISABLED
    assert mode.resolve(proj).to_dict()["routes_to"] == "standalone plugin"


@pytest.mark.parametrize("fields, code", [
    ({"status": "provisioning", "enabled": False}, "setup-interrupted"),
    ({"status": "failed", "enabled": False, "last_error": "pip"}, "setup-failed"),
    ({"enabled": False}, "not-enabled"),
    ({"paths": {"venv": "../evil"}}, "manifest-invalid"),
    ({"project_root": "/somewhere/else"}, "relocated"),
])
def test_invalid_installations_are_reported(tmp_path: Path, fields: dict, code: str) -> None:
    proj = make_project(tmp_path)
    fake_install(proj, **fields)
    resolution = mode.resolve(proj)
    assert resolution.mode == mode.HARNESS_INVALID
    assert code in {p.code for p in resolution.problems}
    assert all(p.remediation for p in resolution.problems)


def test_unreadable_manifest_and_missing_files_are_invalid(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    layout = fake_install(proj)
    layout.database.unlink()
    assert {p.code for p in mode.resolve(proj).problems} == {"missing-file"}
    layout.manifest.write_text("{not json", encoding="utf-8")
    resolution = mode.resolve(proj)
    assert resolution.mode == mode.HARNESS_INVALID and resolution.problems[0].code == "manifest-unreadable"


def test_version_mismatch_and_drift_are_never_silently_accepted(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    layout = fake_install(proj)
    newer = plugin_copy(tmp_path, "9.9.9")
    assert {p.code for p in mode.resolve(proj, plugin_root=newer).problems} == {"version-mismatch"}
    same_version = plugin_copy(tmp_path / "x", distribution.plugin_version(PLUGIN_ROOT))
    assert {p.code for p in mode.resolve(proj, plugin_root=same_version).problems} == {"runtime-drift"}
    assert mode.resolve(proj).mode == mode.HARNESS_ENABLED
    assert layout.manifest.is_file()


def test_repositories_resolve_independently(tmp_path: Path) -> None:
    first, second = make_project(tmp_path, "first"), make_project(tmp_path, "second")
    fake_install(first)
    assert mode.resolve(first).mode == mode.HARNESS_ENABLED
    assert mode.resolve(second).mode == mode.STANDALONE
    nested = make_project(first, "vendored")                     # its own Git work tree inside the first one
    assert mode.resolve(nested).mode == mode.STANDALONE


def test_manifest_never_holds_secrets(tmp_path: Path) -> None:
    doc = new_manifest(project=tmp_path, plugin_version="1.0.0", profile="pack")
    assert not manifest_problems(doc)
    with pytest.raises(ManifestError):
        write_manifest(tmp_path / "installation.json", {**doc, "api_key": "x"})
    assert "credential" in " ".join(manifest_problems({**doc, "auth_token": "x"}))


# ====================================================================== setup


def test_clean_setup(installed: Path) -> None:
    layout = Layout(installed.resolve())
    manifest = read_manifest(layout.manifest)
    assert manifest["status"] == "installed" and manifest["enabled"] is True and manifest["profile"] == "pack"
    assert not manifest_problems(manifest)
    assert manifest["harness_version"] == distribution.plugin_version(PLUGIN_ROOT)
    runtime = layout.inside(manifest["runtime"]["path"])
    assert distribution.verify_snapshot(runtime) == []
    assert distribution.read_record(runtime)["digest"] == distribution.describe(PLUGIN_ROOT)["digest"]
    for rel in ("agents/django-testing-agent.md", "config/orchestration-policy.yaml", "skills/django-testing/SKILL.md"):
        assert (runtime / rel).read_bytes() == (PLUGIN_ROOT / rel).read_bytes()   # the same canonical files
    assert not (runtime / "tests").exists() and not (runtime / "plugins").exists()
    assert (layout.enclave / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_TEXT
    config = layout.config_file.read_text(encoding="utf-8")
    assert "enclave: ../.." in config and "../../database/harness.db" in config
    conn = sqlite3.connect(str(layout.database))
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
    conn.close()
    assert sorted(p.name for p in installed.iterdir()) == [".django-engineering", ".git", "app", "manage.py"]
    assert mode.resolve(installed).mode == mode.HARNESS_ENABLED
    history = [h["event"] for h in manifest["history"]]
    assert history == ["installed"]
    assert list(layout.logs.glob("setup-*.log"))


def test_repeated_setup_validates_and_changes_nothing(installed: Path) -> None:
    layout = Layout(installed.resolve())
    before = layout.manifest.read_bytes(), layout.config_file.read_bytes(), layout.database.stat().st_size
    doc = plan(installed)
    assert doc["state"] == "installed" and doc["actions"] == ["validate the existing installation (nothing is changed)"]
    code, out, _ = setup(installed)
    assert code == 0 and "already installed" in out and "[fail]" not in out, out
    assert (layout.manifest.read_bytes(), layout.config_file.read_bytes(), layout.database.stat().st_size) == before


def test_setup_runs_only_the_approved_plan(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    out = io.StringIO()
    assert run_setup(Options(project=proj), "0000000000000000", runner=FakeRunner(), out=out) == 12
    assert "changed since it was approved" in out.getvalue() and not (proj / ENCLAVE_NAME).exists()


def test_python_unavailable(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    doc = plan(proj, FakeRunner(python_ok=False), python="python9.9")
    assert any(b.startswith("python:") for b in doc["blocked"]) and doc["next"] == "fix-blockers"
    code, out, _ = setup(proj, FakeRunner(python_ok=False), python="python9.9")
    assert code == 11 and not (proj / ENCLAVE_NAME).exists()


def test_venv_and_pip_unavailable(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    doc = plan(proj, FakeRunner(venv_ok=False, pip_ok=False))
    assert any(b.startswith("venv:") for b in doc["blocked"]) and any(b.startswith("pip:") for b in doc["blocked"])
    assert not (proj / ENCLAVE_NAME).exists()


def test_permission_denied_is_a_blocker(tmp_path: Path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    doc = plan(proj)
    assert any(b.startswith("permissions:") for b in doc["blocked"])


def test_dependency_failure_is_recorded_redacted_and_never_enabled(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    runner = FakeRunner(pip_rc=1, pip_output="ERROR: https://alice:s3cr3t-token@pypi.example/simple unreachable")
    code, out, _ = setup(proj, runner)
    assert code == 1 and "s3cr3t-token" not in out and "***@" in out
    layout = Layout(proj.resolve())
    manifest = read_manifest(layout.manifest)
    assert manifest["status"] == "failed" and manifest["enabled"] is False and "dependencies" in manifest["last_error"]
    assert all("s3cr3t-token" not in p.read_text(encoding="utf-8") for p in layout.logs.glob("*.log"))
    resolution = mode.resolve(proj)
    assert resolution.mode == mode.HARNESS_INVALID and resolution.problems[0].code == "setup-failed"
    assert not layout.lock.exists()
    assert redact_text("pip https://u:p@h/x") == "pip https://***@h/x"


def test_database_failure_leaves_the_harness_disabled(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    (proj / ENCLAVE_NAME / "database" / "harness.db").mkdir(parents=True)          # not a file: cannot open
    code, out, _ = setup(proj)
    assert code == 1 and "database" in out
    manifest = read_manifest(Layout(proj.resolve()).manifest)
    assert manifest["status"] == "failed" and manifest["enabled"] is False
    assert mode.resolve(proj).mode == mode.HARNESS_INVALID


def test_interrupted_setup_resumes(tmp_path: Path) -> None:
    proj = make_project(tmp_path)

    def interrupt(step: str) -> None:
        if step == "dependencies":
            raise KeyboardInterrupt

    code, out, _ = setup(proj, fault=interrupt)
    layout = Layout(proj.resolve())
    assert code == 130 and read_manifest(layout.manifest)["status"] == "provisioning"
    assert not layout.lock.exists()
    assert {p.code for p in mode.resolve(proj).problems} == {"setup-interrupted"}
    assert plan(proj)["state"] == "resume"
    code, out, _ = setup(proj)
    assert code == 0, out
    assert mode.resolve(proj).mode == mode.HARNESS_ENABLED


def test_foreign_files_are_a_conflict_and_never_touched(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    (proj / ENCLAVE_NAME).mkdir()
    (proj / ENCLAVE_NAME / "notes.txt").write_text("mine", encoding="utf-8")
    doc = plan(proj)
    assert doc["state"] == "conflict" and "notes.txt" in doc["blocked"][0]
    assert sorted(p.name for p in (proj / ENCLAVE_NAME).iterdir()) == ["notes.txt"]


def test_a_committed_team_configuration_is_adopted_unchanged(tmp_path: Path, installed: Path) -> None:
    proj = make_project(tmp_path)
    shared = Layout(installed.resolve()).config_file.read_text(encoding="utf-8") + "# team settings\n"
    (proj / ENCLAVE_NAME / "config").mkdir(parents=True)
    (proj / ENCLAVE_NAME / "config" / "framework-config.yaml").write_text(shared, encoding="utf-8")
    assert plan(proj)["state"] == "adopt-config"
    code, out, _ = setup(proj)
    assert code == 0, out
    assert Layout(proj.resolve()).config_file.read_text(encoding="utf-8") == shared


def test_missing_database_is_never_recreated_silently(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    assert setup(proj)[0] == 0
    layout = Layout(proj.resolve())
    layout.database.unlink()
    doc = plan(proj)
    assert doc["state"] == "repair" and any("never" in b and "recreated" in b for b in doc["blocked"])
    code, out, _ = setup(proj, new_database=True)
    assert code == 0, out
    assert layout.database.is_file()


# ====================================================================== upgrade


def test_upgrade_preserves_configuration_and_history(tmp_path: Path) -> None:
    from runtime.models import Execution, utcnow
    from runtime.sqlite_store import SQLiteExecutionStore
    from runtime.state import ExecutionStatus

    proj = make_project(tmp_path)
    assert setup(proj)[0] == 0
    layout = Layout(proj.resolve())
    with layout.config_file.open("a", encoding="utf-8") as handle:
        handle.write("# my edit\n")
    config_before = layout.config_file.read_bytes()
    store = SQLiteExecutionStore(layout.database)
    store.create_execution(Execution(execution_id="exec-1", workflow_id="orchestrate.testing",
                                     workflow_version="1.0.0", status=ExecutionStatus.SUCCEEDED,
                                     created_at=utcnow(), updated_at=utcnow()), [])
    store.close()
    old_runtime = read_manifest(layout.manifest)["runtime"]["path"]

    newer = plugin_copy(tmp_path, "0.99.0")
    blocked = build_plan(Options(project=proj), runner=FakeRunner(), plugin_root=newer)
    assert blocked["state"] == "upgrade-available" and blocked["next"] == "upgrade"     # never silent
    assert {p.code for p in mode.resolve(proj, plugin_root=newer).problems} == {"version-mismatch"}
    code, out, approved = setup(proj, FakeRunner(preinstalled=True), plugin_root=newer, upgrade=True)
    assert approved["state"] == "upgrade" and code == 0, out

    manifest = read_manifest(layout.manifest)
    assert manifest["harness_version"] == "0.99.0" and manifest["enabled"] is True
    assert manifest["runtime"]["path"] != old_runtime and layout.inside(old_runtime).is_dir()   # old one kept
    assert layout.config_file.read_bytes() == config_before
    conn = sqlite3.connect(str(layout.database))
    assert conn.execute("SELECT execution_id FROM executions").fetchall() == [("exec-1",)]
    conn.close()
    backups = list(layout.backups.glob("harness-*-before-0.99.0-*.db"))
    assert len(backups) == 1
    assert [h["event"] for h in manifest["history"]][-1] == "upgraded"
    assert mode.resolve(proj, plugin_root=newer).mode == mode.HARNESS_ENABLED
    # the older plugin never downgrades it
    assert build_plan(Options(project=proj), runner=FakeRunner())["state"] == "installed-newer"


def test_upgrade_keeps_an_explicitly_disabled_harness_disabled(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    assert setup(proj)[0] == 0
    layout = Layout(proj.resolve())
    disable(layout, out=io.StringIO())
    newer = plugin_copy(tmp_path, "0.98.0")
    code, out, _ = setup(proj, FakeRunner(preinstalled=True), plugin_root=newer, upgrade=True)
    assert code == 0, out
    manifest = read_manifest(layout.manifest)
    assert manifest["enabled"] is False and manifest["disabled_by_user"] is True


# ====================================================================== enable / disable


def test_disable_keeps_everything_and_enable_validates(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    assert setup(proj)[0] == 0
    layout = Layout(proj.resolve())
    kept = {p: p.read_bytes() for p in (layout.config_file, layout.database)}
    out = io.StringIO()
    assert disable(layout, out=out) == 0 and "kept" in out.getvalue()
    assert mode.resolve(proj).mode == mode.HARNESS_DISABLED
    assert {p: p.read_bytes() for p in kept} == kept and layout.python.is_file()
    assert enable(layout, runner=FakeRunner(preinstalled=True), out=io.StringIO()) == 0
    assert mode.resolve(proj).mode == mode.HARNESS_ENABLED
    events = [h["event"] for h in read_manifest(layout.manifest)["history"]]
    assert events[-2:] == ["disabled", "enabled"]


def test_enable_refuses_a_broken_installation(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    assert setup(proj)[0] == 0
    layout = Layout(proj.resolve())
    disable(layout, out=io.StringIO())
    runtime = layout.inside(read_manifest(layout.manifest)["runtime"]["path"])
    with (runtime / "config" / "security-config.yaml").open("a", encoding="utf-8") as handle:
        handle.write("\n# tampered\n")
    out = io.StringIO()
    assert enable(layout, runner=FakeRunner(preinstalled=True), out=out) == 1
    assert "changed config/security-config.yaml" in out.getvalue()
    assert mode.resolve(proj).mode == mode.HARNESS_DISABLED                            # unchanged


def test_disable_with_an_unreadable_manifest_needs_explicit_reset(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    layout = fake_install(proj)
    layout.manifest.write_text("{broken", encoding="utf-8")
    assert disable(layout, out=io.StringIO()) == 13
    assert layout.manifest.read_text(encoding="utf-8") == "{broken"
    assert disable(layout, reset_unreadable=True, out=io.StringIO()) == 0
    assert mode.resolve(proj).mode == mode.HARNESS_DISABLED
    kept = list(layout.enclave.glob("installation.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{broken"
    assert "unreadable" not in " ".join(plan(proj)["blocked"])          # a kept manifest is not a conflict


# ====================================================================== adapter


def test_adapter_runs_the_repository_runtime_unattended(installed: Path) -> None:
    resolution = mode.resolve(installed)
    layout = resolution.layout
    argv = adapter.build(resolution, "run", task="Write tests; rm -rf / $(whoami)", category="testing")
    assert argv[0] == str(layout.python) and argv[1:6] == ["-X", "utf8", "-E", "-s", "-m"] and argv[6] == "core.cli"
    assert argv[argv.index("--root") + 1] == str(resolution.runtime_dir)
    assert argv[argv.index("--framework-file") + 1] == str(layout.config_file)
    assert argv[argv.index("--profile") + 1] == "pack"
    tail = argv[argv.index("orchestrate"):]
    assert tail[:3] == ["orchestrate", "run", "Write tests; rm -rf / $(whoami)"]          # one argv element
    assert tail[tail.index("--workspace") + 1] == str(resolution.project)
    assert tail[tail.index("--approve") + 1] == "deny" and "interactive" not in argv
    resume = adapter.build(resolution, "resume", execution_id="e1")
    assert resume[-3:] == ["e1", "--approve", "deny"]
    approve = adapter.build(resolution, "approve", execution_id="e1", stage_id="s1", person="Dana")
    assert approve[-5:] == ["approve", "e1", "s1", "--approver", "Dana"]
    with pytest.raises(adapter.AdapterError):
        adapter.build(resolution, "approve", execution_id="e1", stage_id="s1")          # nobody approved


def test_adapter_refuses_without_an_enabled_harness(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    with pytest.raises(adapter.AdapterError) as err:
        adapter.build(mode.resolve(proj), "run", task="x")
    assert err.value.code == adapter.EXIT_NOT_ENABLED
    layout = fake_install(proj, status="failed", enabled=False)
    with pytest.raises(adapter.AdapterError) as err:
        adapter.build(mode.resolve(proj), "plan", task="x")
    assert err.value.code == adapter.EXIT_INVALID and "not used in its place" in str(err.value)
    write_manifest(layout.manifest, {**read_manifest(layout.manifest), "status": "installed", "enabled": False,
                                     "disabled_by_user": True})
    adapter.build(mode.resolve(proj), "plan", task="x")                                    # read-only: allowed
    with pytest.raises(adapter.AdapterError) as err:
        adapter.build(mode.resolve(proj), "run", task="x")
    assert err.value.code == adapter.EXIT_NOT_ENABLED


def test_adapter_plan_through_the_runtime_subprocess(installed: Path) -> None:
    resolution = mode.resolve(installed)
    runner = FakeRunner(preinstalled=True)
    argv = adapter.build(resolution, "plan", task="Write tests for the billing app", as_json=True)
    result = runner.run(argv, cwd=resolution.runtime_dir)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["category"] == "testing" and [s["agent"] for s in doc["steps"]] == ["django-testing-agent"]


# ====================================================================== harness execution (repository-local)


def test_workspace_rule_allows_only_the_project_owning_the_enclave(tmp_path: Path) -> None:
    from agent_runtime.workspace import WorkspaceError, validate_workspace

    proj = make_project(tmp_path)
    enclave = proj / ENCLAVE_NAME
    (enclave / "runtime").mkdir(parents=True)
    protected = [enclave.resolve(), (enclave / "runtime").resolve()]
    assert validate_workspace(proj, protected, enclave=enclave) == proj.resolve()
    with pytest.raises(WorkspaceError):
        validate_workspace(proj, protected)                                  # SEC-04 unchanged without an enclave
    with pytest.raises(WorkspaceError):
        validate_workspace(enclave / "runtime", protected, enclave=enclave)  # inside the enclave
    with pytest.raises(WorkspaceError):
        validate_workspace(tmp_path, protected, enclave=enclave)            # a parent of the project
    other = proj / "other-trusted"
    other.mkdir()
    with pytest.raises(WorkspaceError):
        validate_workspace(proj, [*protected, other.resolve()], enclave=enclave)   # any other protected root
    with pytest.raises(WorkspaceError):
        validate_workspace(proj, [(enclave / "runtime").resolve()], enclave=enclave)   # enclave not protected


def test_orchestrated_run_uses_the_existing_engine_and_store(installed: Path) -> None:
    from fakes import FakeSDKAdapter, succeed

    fake = FakeSDKAdapter(succeed())
    code, out = harness_cli([*cli_args(installed), "orchestrate", "run", "Write tests for the billing app",
                             "--workspace", str(installed), "--approve", "deny"], adapter=fake)
    assert code == 0, out
    assert "orchestrate.testing" in out and "succeeded" in out
    assert [c.options.extra_args["agent"] for c in fake.calls] == ["dae-django-testing-agent"]
    assert fake.calls[0].options.cwd == str(installed.resolve()) or Path(fake.calls[0].options.cwd) == installed.resolve()
    execution_id = out.split("started ", 1)[1].split(" ", 1)[0]
    layout = Layout(installed.resolve())
    conn = sqlite3.connect(str(layout.database))
    row = conn.execute("SELECT workflow_id, status, metadata FROM executions WHERE execution_id = ?",
                       (execution_id,)).fetchone()
    runs = conn.execute("SELECT agent_id, status FROM agent_runs WHERE execution_id = ?", (execution_id,)).fetchall()
    conn.close()
    assert row[0] == "orchestrate.testing" and row[1] == "succeeded"
    assert json.loads(row[2])["dae.orchestration"]["orchestrator"] == "django-orchestrator"
    assert runs == [("django-testing-agent", "succeeded")]
    out = io.StringIO()
    assert adapter.execute(mode.resolve(installed), "result", runner=FakeRunner(preinstalled=True), out=out,
                           execution_id=execution_id) == 0
    result = json.loads(out.getvalue())
    assert result["status"] == "succeeded" and result["stages"][0]["agent_id"] == "django-testing-agent"
    assert result["stages"][0]["outputs"][0]["available"] is True


def test_toolguard_keeps_agents_out_of_the_installation(installed: Path) -> None:
    from fakes import FakeSDKAdapter, tool_then_succeed

    target = installed / ENCLAVE_NAME / "config" / "framework-config.yaml"
    before = target.read_bytes()
    fake = FakeSDKAdapter(tool_then_succeed("Write", {"file_path": str(target), "content": "weakened"}))
    code, out = harness_cli([*cli_args(installed), "orchestrate", "run", "Write tests for the billing app",
                             "--workspace", str(installed)], adapter=fake)
    decision = fake.calls[0].decisions[0][1]["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "trusted harness area" in decision["permissionDecisionReason"]
    assert target.read_bytes() == before
    fake = FakeSDKAdapter(tool_then_succeed("Read", {"file_path": str(installed / ENCLAVE_NAME / "installation.json")}))
    harness_cli([*cli_args(installed), "orchestrate", "run", "Write tests for the billing app",
                 "--workspace", str(installed)], adapter=fake)
    assert fake.calls[0].decisions[0][1]["hookSpecificOutput"]["permissionDecision"] == "deny"
    fake = FakeSDKAdapter(tool_then_succeed("Write", {"file_path": str(installed / "app" / "test_x.py"),
                                                      "content": "def test(): pass\n"}))
    harness_cli([*cli_args(installed), "orchestrate", "run", "Write tests for the billing app",
                 "--workspace", str(installed)], adapter=fake)
    assert fake.calls[0].decisions[0][1].get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_approval_requests_are_recorded_and_denied_unattended(installed: Path) -> None:
    from fakes import FakeSDKAdapter, tool_then_succeed

    settings = installed / "app" / "models.py"
    fake = FakeSDKAdapter(tool_then_succeed("Write", {"file_path": str(settings), "content": "x = 2\n"}))
    code, out = harness_cli([*cli_args(installed), "orchestrate", "run", "Assess the security of the app",
                             "--category", "security-assessment", "--workspace", str(installed)], adapter=fake)
    execution_id = out.split("started ", 1)[1].split(" ", 1)[0]
    assert fake.calls[0].options.extra_args["agent"] == "dae-django-security-agent"
    assert fake.calls[0].decisions[0][1]["hookSpecificOutput"]["permissionDecision"] == "deny"   # confirm-writes
    assert settings.read_text(encoding="utf-8") == "x = 1\n"
    out = io.StringIO()
    adapter.execute(mode.resolve(installed), "result", runner=FakeRunner(preinstalled=True), out=out,
                    execution_id=execution_id)
    events = [g["event"] for g in json.loads(out.getvalue())["guard_decisions"]]
    assert "tool.approval_requested" in events and "tool.denied" in events


def test_interrupted_execution_resumes_from_its_checkpoint(installed: Path) -> None:
    from fakes import FakeSDKAdapter, crash_after_init, succeed

    code, out = harness_cli([*cli_args(installed), "orchestrate", "run", "Write tests for the billing app",
                             "--workspace", str(installed)], adapter=FakeSDKAdapter(crash_after_init()))
    assert code == 130
    layout = Layout(installed.resolve())
    conn = sqlite3.connect(str(layout.database))
    execution_id = conn.execute("SELECT execution_id FROM executions ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    checkpoints = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE execution_id = ?", (execution_id,)).fetchone()[0]
    conn.close()
    assert checkpoints >= 1
    code, out = harness_cli([*cli_args(installed), "execution", "resume", execution_id, "--approve", "deny"],
                            adapter=FakeSDKAdapter(succeed()))
    assert code == 0 and "succeeded" in out, out


def test_sandbox_rules_cover_the_installation(installed: Path) -> None:
    from agent_runtime.runner import AgentRunner
    from core.context import ContextManager
    from core.registry import HarnessRegistry
    from runtime.execution import ExecutionManager
    from runtime.sqlite_store import SQLiteExecutionStore

    resolution = mode.resolve(installed)
    registry = HarnessRegistry.load(resolution.runtime_dir, str(resolution.layout.config_file), profile="pack")
    store = SQLiteExecutionStore(resolution.layout.database)
    try:
        runner = AgentRunner(ExecutionManager(store, registry),
                             ContextManager(registry.config.framework.context,
                                            max_inline_bytes=registry.config.security.max_inline_bytes),
                             adapter=object())
        enclave = resolution.layout.enclave.resolve()
        read_deny = {p for p in runner._protected_read_deny() if enclave in p.parents}
        assert {enclave / "installation.json", enclave / "config", enclave / "database", enclave / "logs"} <= read_deny
        assert not {enclave / "runtime", enclave / "artifacts"} & read_deny     # pack read roots, run outputs
        write_deny = set(runner._enclave_write_deny())
        assert {enclave / "runtime", enclave / "config", enclave / "database"} <= write_deny
        assert enclave / "artifacts" not in write_deny
        assert enclave in {p.resolve() for p in runner.protected_roots}
    finally:
        store.close()


def test_status_and_doctor_report_without_changing_anything(installed: Path) -> None:
    layout = Layout(installed.resolve())
    before = layout.manifest.read_bytes()
    for argv in (["status", "--json"], ["doctor", "--json"]):
        result = subprocess.run([sys.executable, str(SCRIPT), *argv, "--project", str(installed)],
                                capture_output=True, text=True, encoding="utf-8", timeout=600)
        doc = json.loads(result.stdout)
        if argv[0] == "status":
            assert doc["execution_mode"] == "harness_enabled" and doc["database"]["status"] == "ok"
            assert doc["profile"] == "pack" and doc["runtime_ready"] is True
        else:
            names = {c["name"] for c in doc["checks"]}
            assert {"distribution", "python", "manifest", "runtime", "compatibility", "credentials"} <= names
        assert "ANTHROPIC_API_KEY=" not in result.stdout
    assert layout.manifest.read_bytes() == before
