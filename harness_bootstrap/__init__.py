"""Opt-in, repository-local provisioning of the Django Agentic Harness (Phase 5.6).

The Django Engineering plugin works without any of this. Nothing here runs unless the user invokes
one of the harness commands (``/django-engineering:setup-harness``, ``harness-status``,
``enable-harness``, ``disable-harness``, ``doctor``) or ``/orchestrate`` asks the execution-mode
resolver which mode the current repository is in.

Modules
-------
``layout``        the repository-local layout (``<project>/.django-engineering/``) and its manifest
``project``       project-root detection (Git, Django markers, existing installations)
``mode``          the single execution-mode resolver: standalone | harness_enabled |
                  harness_disabled | harness_invalid
``distribution``  the versioned harness distribution shipped with the plugin, and its runtime snapshots
``environment``   read-only environment checks (Python, venv, pip, disk, paths, credentials)
``provision``     setup plan, consent binding, idempotent setup, upgrade, enable and disable
``adapter``       the runtime adapter: plan, run, status, approvals, recovery and results, delegated to
                  the existing harness (``python -m core.cli``) inside the repository's virtual environment
``probe``         helpers that run *inside* that virtual environment (config, database, readiness, results)
``cli``           the command line the plugin commands call (``django_harness.py``)

Everything except ``probe`` is standard library only and keeps Python 3.8 syntax, so it can run with
whatever interpreter Claude Code finds and explain what is missing. The harness itself needs 3.11+.
"""

from __future__ import annotations

from pathlib import Path

#: The plugin installation this package belongs to (the plugin root is the harness root).
PLUGIN_ROOT: Path = Path(__file__).resolve().parent.parent

__all__ = ["PLUGIN_ROOT"]
