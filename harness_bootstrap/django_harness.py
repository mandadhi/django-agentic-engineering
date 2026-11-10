"""Entry point the plugin commands run: ``python "${CLAUDE_PLUGIN_ROOT}/harness_bootstrap/django_harness.py" ...``

Standard library only. Runs with any Python 3.8+ so it can report what is missing; the harness
itself (installed by ``setup``) needs Python 3.11+. See ``harness_bootstrap/cli.py`` for the commands.
"""

import os
import sys

if sys.version_info < (3, 8):                                     # pragma: no cover - old interpreters
    sys.stderr.write("harness: Python 3.8+ is needed to run this script (the harness itself needs 3.11+)\n")
    sys.exit(5)

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
# Import this plugin's package, never a same-named module from the script folder or the project.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) not in (_HERE, os.getcwd())]
sys.path.insert(0, _PLUGIN_ROOT)

from harness_bootstrap.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
