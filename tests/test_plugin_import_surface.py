"""Regression: plugin registration must import without third-party deps.

``hermes plugins doctor`` never auto-installs a plugin's declared
``python_dependencies`` (it only warns), so importing either plugin
entry — the ``dyvine_hermes`` package for pip installs and the
repo-root ``__init__.py`` for directory installs — must succeed with
only the standard library plus this repo's ``src/`` importable. Any
module-scope edge from the registration chain (``plugin`` ->
``tools`` -> ``context``) into the engine (``dyvine.core`` /
``dyvine.services``) or a third-party SDK breaks ``doctor`` with
``No module named ...`` and zero registered tools.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHILD_SCRIPT = (
    "import sys\n"
    "# Scrub every third-party location: only the stdlib plus the repo\n"
    "# src tree below may satisfy imports, mirroring the doctor runtime.\n"
    "def _origin(mod):\n"
    "    spec = getattr(mod, '__spec__', None)\n"
    "    return getattr(spec, 'origin', '') or ''\n"
    "def _is_third_party(mod):\n"
    "    path = _origin(mod)\n"
    "    return 'site-packages' in path or 'dist-packages' in path\n"
    "sys.path = [p for p in sys.path if 'site-packages' not in p]\n"
    "sys.path = [p for p in sys.path if 'dist-packages' not in p]\n"
    "for _name, _mod in list(sys.modules.items()):\n"
    "    if _is_third_party(_mod):\n"
    "        del sys.modules[_name]\n"
    "sys.path.insert(0, 'src')\n"
    "import dyvine_hermes\n"
    "assert callable(dyvine_hermes.register)\n"
    "import importlib.util\n"
    "spec = importlib.util.spec_from_file_location(\n"
    "    'dyvine_plugin_root_probe', '__init__.py')\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "assert callable(module.register)\n"
    "third_party = sorted(\n"
    "    {n.split('.')[0] for n, m in sys.modules.items()\n"
    "     if _is_third_party(m)})\n"
    "print(f'third_party={third_party}')\n"
    "sys.exit(1 if third_party else 0)\n"
)


def test_plugin_registration_imports_without_third_party_deps() -> None:
    """Either plugin entry must import with stdlib + src only."""
    env = dict(os.environ)
    env["API_DEBUG"] = "true"
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-E", "-s", "-c", _CHILD_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        "plugin registration must import with stdlib + src only:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
