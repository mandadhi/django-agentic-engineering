"""Runtime adapter: the stable interface between the plugin's commands and the existing harness.

It translates a small, fixed set of operations into ``python -m core.cli`` calls of the repository's
own runtime snapshot and virtual environment (see :func:`provision.harness_argv`):

==============  =====================================================================
plan            ``orchestrate plan``     route from the canonical policy (runs nothing)
run             ``orchestrate run``      the route as one workflow execution: ExecutionManager,
                                          AgentRunner, ToolGuard, approvals, checkpoints, RunRecords
list / status   ``execution list|status``
resume          ``execution resume``     recovery from the last checkpoint
approve/reject  ``execution approve|reject``  stage approval gates (a named human approver)
cancel          ``execution cancel``
result          probe ``results``        stage summaries, output artifacts, guard decisions
==============  =====================================================================

It owns no routing, retry, checkpoint, approval or security logic: those stay in the harness. Every
operation first asks the mode resolver. Executing operations need ``harness_enabled``; read-only ones
(``plan``, ``list``, ``status``, ``result``) also work while the harness is disabled, so history stays
visible. Nothing ever falls back to the standalone plugin: a missing or broken harness is an error
the user sees.

Unattended approval policy: ``run`` and ``resume`` always pass ``--approve deny``. A Claude Code Bash
call has no terminal, so a tool call that the harness sends to its approver is denied and recorded
(``result`` lists those decisions); stage-level approval gates wait (exit 3) for ``approve``.
No MCP server is needed for this: every operation is a bounded, synchronous command.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, TextIO

from .environment import Runner
from .mode import HARNESS_DISABLED, HARNESS_ENABLED, Resolution
from .provision import _common, _probe, harness_argv, harness_env

READ_ONLY = frozenset({"plan", "list", "status", "result"})
EXIT_NOT_ENABLED = 14
EXIT_INVALID = 13


class AdapterError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _gate(resolution: Resolution, operation: str) -> Path:
    allowed = (HARNESS_ENABLED, HARNESS_DISABLED) if operation in READ_ONLY else (HARNESS_ENABLED,)
    if resolution.mode not in allowed:
        if resolution.mode == HARNESS_DISABLED:
            raise AdapterError(f"the harness is disabled for {resolution.project}; '{operation}' needs it enabled "
                               "(/django-engineering:enable-harness). Nothing was run.", EXIT_NOT_ENABLED)
        if resolution.mode == "standalone":
            raise AdapterError(f"the harness is not set up for {resolution.project} (standalone mode). Use the "
                               "standalone plugin, or /django-engineering:setup-harness.", EXIT_NOT_ENABLED)
        details = "; ".join(f"[{p.code}] {p.message}" for p in resolution.problems)
        raise AdapterError(f"the harness installation for {resolution.project} is invalid: {details}. Nothing was "
                           "run, and the standalone plugin is not used in its place.", EXIT_INVALID)
    runtime_dir = resolution.runtime_dir
    layout = resolution.layout
    if runtime_dir is None or layout is None or not layout.python.is_file() or not runtime_dir.is_dir():
        raise AdapterError("the harness runtime or its virtual environment is missing; run "
                           "/django-engineering:doctor", EXIT_INVALID)
    return runtime_dir


def _selection(category: Optional[str], include: Sequence[str], exclude: Sequence[str]) -> List[str]:
    args: List[str] = []
    if category:
        args += ["--category", category]
    for agent in include:
        args += ["--with", agent]
    for agent in exclude:
        args += ["--without", agent]
    return args


def build(resolution: Resolution, operation: str, *, task: Optional[str] = None, category: Optional[str] = None,
          include: Sequence[str] = (), exclude: Sequence[str] = (), as_json: bool = False,
          execution_id: Optional[str] = None, stage_id: Optional[str] = None, person: Optional[str] = None,
          reason: Optional[str] = None, status: Optional[str] = None, limit: Optional[int] = None,
          timeout: Optional[int] = None) -> List[str]:
    """The exact argv the adapter runs for ``operation`` (also used by tests)."""
    runtime_dir = _gate(resolution, operation)
    layout, profile = resolution.layout, str(resolution.profile)
    assert layout is not None
    if operation in ("plan", "run"):
        if not task or not task.strip():
            raise AdapterError("the task is empty; describe the Django work to do", 4)
        args = ["orchestrate", operation, task, *_selection(category, include, exclude)]
        if operation == "plan" and as_json:
            args.append("--json")
        if operation == "run":
            args += ["--workspace", str(resolution.project), "--approve", "deny"]
            if timeout:
                args += ["--timeout", str(timeout)]
        return harness_argv(layout, runtime_dir, profile, *args)
    if operation == "list":
        args = ["execution", "list"] + (["--status", status] if status else []) + \
            (["--limit", str(limit)] if limit else [])
        return harness_argv(layout, runtime_dir, profile, *args)
    if not execution_id:
        raise AdapterError(f"'{operation}' needs an execution id", 4)
    if operation == "status":
        return harness_argv(layout, runtime_dir, profile, "execution", "status", execution_id,
                            *(["--json"] if as_json else []))
    if operation == "resume":
        return harness_argv(layout, runtime_dir, profile, "execution", "resume", execution_id, "--approve", "deny",
                            *(["--timeout", str(timeout)] if timeout else []))
    if operation == "cancel":
        return harness_argv(layout, runtime_dir, profile, "execution", "cancel", execution_id,
                            "--reason", reason or "cancelled from Claude Code")
    if operation in ("approve", "reject"):
        if not stage_id or not person:
            raise AdapterError(f"'{operation}' needs the stage id and the name of the person who decided", 4)
        if operation == "approve":
            return harness_argv(layout, runtime_dir, profile, "execution", "approve", execution_id, stage_id,
                                "--approver", person)
        if not reason:
            raise AdapterError("'reject' needs a reason", 4)
        return harness_argv(layout, runtime_dir, profile, "execution", "reject", execution_id, stage_id,
                            "--reviewer", person, "--reason", reason)
    raise AdapterError(f"unknown operation '{operation}'", 2)


def execute(resolution: Resolution, operation: str, *, runner: Optional[Runner] = None, out: TextIO = sys.stdout,
            **kwargs) -> int:
    """Run ``operation`` through the harness. Output streams through; the harness's exit code is returned."""
    runner = runner or Runner()
    if operation == "result":
        runtime_dir = _gate(resolution, operation)
        layout = resolution.layout
        assert layout is not None
        execution_id = kwargs.get("execution_id")
        if not execution_id:
            raise AdapterError("'result' needs an execution id", 4)
        import json
        doc = _probe(runner, layout, runtime_dir, "results", *_common(layout, str(resolution.profile), runtime_dir),
                     execution_id)
        code = doc.pop("returncode", 1)
        print(json.dumps(doc, indent=2), file=out)
        return code
    argv = build(resolution, operation, **kwargs)
    runtime_dir = resolution.runtime_dir
    try:
        result = runner.run(argv, cwd=runtime_dir, env=harness_env(), capture=False, timeout=None)
    except KeyboardInterrupt:
        return 130
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"could not start the harness runtime: {exc}", 5) from exc
    return result.returncode
