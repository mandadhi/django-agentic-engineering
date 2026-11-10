"""Shared fixtures.

Tests never touch the shipped configuration: ``harness_root`` copies config/,
contracts/ and workflows/ into a temp dir (pointing repo_root back at the real
repository so registry paths still resolve), and tests mutate that copy.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yaml

from core import HARNESS_HOME
from core.context import ContextManager
from core.registry import HarnessRegistry
from runtime.execution import ExecutionManager
from runtime.sqlite_store import SQLiteExecutionStore

REPO_ROOT = HARNESS_HOME.parents[1]


def sandboxed_isolation():
    """The Bash isolation of a macOS/Linux host with a working OS sandbox, independent of the test machine.

    Runner fixtures use it so guard behaviour is tested deterministically on every OS. The configuration
    of hosts without a sandbox (Windows: Bash disabled or approval-gated) is tested explicitly.
    """
    from agent_runtime.sandbox import shell_isolation
    from core.config import ShellPolicy
    return shell_isolation(ShellPolicy(), platform="linux", which=lambda name: f"/usr/bin/{name}")


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def harness_root(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    for folder in ("config", "contracts", "workflows"):
        shutil.copytree(HARNESS_HOME / folder, root / folder)
    framework = root / "config" / "framework-config.yaml"
    data = read_yaml(framework)
    data["paths"]["repo_root"] = str(REPO_ROOT)
    write_yaml(framework, data)
    if os.environ.get("DAE_TEST_PRUNE_UNAVAILABLE") == "1":   # opt-in: outside the full repository layout
        prune_unavailable(root)
    return root


@pytest.fixture
def edit_yaml(harness_root: Path) -> Callable[[str, Callable[[Any], Any]], None]:
    """edit_yaml("config/agent-registry.yaml", lambda d: ...) — mutate a copied file in place."""

    def _edit(relative: str, mutate: Callable[[Any], Any]) -> None:
        path = harness_root / relative
        data = read_yaml(path)
        mutate(data)  # mutates in place; return value ignored (lambdas often return list.pop() results)
        write_yaml(path, data)

    return _edit


@pytest.fixture
def registry(harness_root: Path) -> HarnessRegistry:
    return HarnessRegistry.load(harness_root)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteExecutionStore]:
    s = SQLiteExecutionStore(tmp_path / "state" / "harness.db")
    yield s
    s.close()


@pytest.fixture
def manager(store: SQLiteExecutionStore, registry: HarnessRegistry) -> ExecutionManager:
    return ExecutionManager(store, registry)


@pytest.fixture
def context_manager(registry: HarnessRegistry) -> ContextManager:
    return ContextManager(registry.config.framework.context,
                          max_inline_bytes=registry.config.security.max_inline_bytes)


# ---------------------------------------------------------------------- Phase 5 (layout-independent)


def prune_unavailable(root: Path, repo_root: Path = REPO_ROOT) -> list[str]:
    """Drop registry entries whose files are absent from this checkout, and what references them.

    The shipped registries reuse plugins from the parent repository (``plugins/security`` ...),
    which only exist when this project sits at ``projects/django-agentic-engineering``. This
    derives a loadable copy from the canonical registries anywhere: nothing is hand-maintained,
    and in the full repository layout nothing is pruned. Returns the pruned ids.
    """
    registry_path, skills_path = root / "config" / "agent-registry.yaml", root / "config" / "skill-registry.yaml"
    agents, skills = read_yaml(registry_path), read_yaml(skills_path)
    bases = {"repo_root": repo_root, "harness": HARNESS_HOME}
    plugins = {p["id"]: (bases[p.get("base", "repo_root")] / p["path"]).resolve() for p in agents.get("plugins", [])}
    kept_plugins = {pid for pid, path in plugins.items() if (path / ".claude-plugin" / "plugin.json").is_file()}

    def located(entry: dict) -> Path:
        return ((plugins[entry["plugin"]] if entry.get("plugin") else repo_root) / entry["path"]).resolve()

    def keep_agent(entry: dict) -> bool:
        return (entry.get("plugin") in (None, *kept_plugins)) and located(entry).is_file()

    def keep_skill(entry: dict) -> bool:
        return (entry.get("plugin") in (None, *kept_plugins)) and (located(entry) / "SKILL.md").is_file()

    pruned = sorted(set(plugins) - kept_plugins)
    agents["plugins"] = [p for p in agents.get("plugins", []) if p["id"] in kept_plugins]
    pruned += [a["id"] for a in agents["agents"] if not keep_agent(a)]
    pruned += [s["id"] for s in skills["skills"] if not keep_skill(s)]
    skills["skills"] = [s for s in skills["skills"] if keep_skill(s)]
    skill_ids = {s["id"] for s in skills["skills"]}
    agents["agents"] = [a for a in agents["agents"] if keep_agent(a) and set(a.get("skills", ())) <= skill_ids]
    agent_ids = {a["id"] for a in agents["agents"]}
    write_yaml(registry_path, agents)
    write_yaml(skills_path, skills)

    workflows = set()
    for path in sorted((root / "workflows").glob("*.workflow.yaml")):
        doc = read_yaml(path)
        if {s["agent_id"] for s in doc.get("stages", [])} <= agent_ids:
            workflows.add(doc["id"])
        else:
            path.unlink()
            pruned.append(f"workflow:{doc['id']}")
    security = read_yaml(root / "config" / "security-config.yaml")
    security["tools"]["agents"] = [o for o in security["tools"].get("agents", []) if o["agent_id"] in agent_ids]
    write_yaml(root / "config" / "security-config.yaml", security)
    evaluation = read_yaml(root / "config" / "evaluation-config.yaml")
    evaluation["targets"] = [t for t in evaluation.get("targets", [])
                             if (t["type"] == "agent" and t["id"] in agent_ids)
                             or (t["type"] != "agent" and t["id"].split("/")[0] in workflows)]
    write_yaml(root / "config" / "evaluation-config.yaml", evaluation)
    return pruned


@pytest.fixture
def pack_root(harness_root: Path) -> Path:
    """``harness_root`` with registry entries absent from this checkout pruned (see prune_unavailable)."""
    prune_unavailable(harness_root)                       # idempotent
    return harness_root


@pytest.fixture
def pack_registry(pack_root: Path) -> HarnessRegistry:
    return HarnessRegistry.load(pack_root)


@pytest.fixture
def pack_runner(store: SQLiteExecutionStore, pack_registry: HarnessRegistry, tmp_path: Path):
    """pack_runner(adapter, **kwargs) -> AgentRunner over ``pack_registry``."""
    from agent_runtime.runner import AgentRunner

    def _make(adapter, **kwargs):
        context = ContextManager(pack_registry.config.framework.context,
                                 max_inline_bytes=pack_registry.config.security.max_inline_bytes)
        kwargs.setdefault("artifacts_root", tmp_path / "artifacts")
        kwargs.setdefault("cancel_poll_seconds", 0.02)
        kwargs.setdefault("shell_isolation", sandboxed_isolation())
        return AgentRunner(ExecutionManager(store, pack_registry), context, adapter, **kwargs)

    return _make


# ---------------------------------------------------------------------- Phase 2 (agent runtime)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A small stand-in for the Django project an agent works on."""
    ws = tmp_path / "workspace"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=never-read\n", encoding="utf-8")
    return ws


@pytest.fixture
def make_runner(manager: ExecutionManager, context_manager: ContextManager, tmp_path: Path):
    """make_runner(FakeSDKAdapter(...), approver=..., timeout_override=...) -> AgentRunner."""
    from agent_runtime.runner import AgentRunner

    def _make(adapter, **kwargs):
        kwargs.setdefault("artifacts_root", tmp_path / "artifacts")
        kwargs.setdefault("cancel_poll_seconds", 0.02)
        kwargs.setdefault("shell_isolation", sandboxed_isolation())
        return AgentRunner(manager, context_manager, adapter, **kwargs)

    return _make
