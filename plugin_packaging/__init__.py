"""Phase 4: packaging of the Django Engineering Pack as a standalone Claude Code plugin.

``python -m plugin_packaging build`` regenerates the derived plugin files from their canonical
sources; ``python -m plugin_packaging check`` fails on drift. See ``build.py`` and the README.
Nothing in the harness (core/, runtime/, agent_runtime/) imports this package.
"""

from .build import PLUGIN_NAME, PackagingError, Report, build_policy, run

__all__ = ["PLUGIN_NAME", "PackagingError", "Report", "build_policy", "run"]
