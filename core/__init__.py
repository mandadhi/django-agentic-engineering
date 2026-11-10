"""Core of the Django Agentic Engineering Harness.

Configuration loading and validation, JSON-Schema contracts, the agent/skill/workflow
registry, context management and security helpers. Nothing in here executes an
agent; execution state lives in :mod:`runtime`.
"""

from pathlib import Path

#: Directory that ships the framework-owned schemas (config/schemas, contracts).
HARNESS_HOME: Path = Path(__file__).resolve().parent.parent

__all__ = ["HARNESS_HOME"]
