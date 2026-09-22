"""Regression: importing the plugin must not perform network I/O.

The upstream f2 SDK executes a real HTTPS request while its modules are
imported (a msToken fetch in a pydantic model class body), which makes
plugin discovery slow, network-dependent, and prone to GC-time socket
warnings. Both plugin entries (the ``dyvine_hermes`` package for pip
installs and the repo-root ``__init__.py`` for directory installs)
must therefore never import f2 at module scope; the SDK may only load
lazily when a tool actually runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHILD_SCRIPT = (
    "import socket\n"
    "import sys\n"
    "attempts = []\n"
    "_connect = socket.socket.connect\n"
    "def _guard(self, address):\n"
    "    attempts.append(address)\n"
    "    return _connect(self, address)\n"
    "socket.socket.connect = _guard\n"
    "import dyvine_hermes\n"
    "import importlib.util\n"
    "spec = importlib.util.spec_from_file_location(\n"
    "    'dyvine_plugin_root_probe', '__init__.py')\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "assert callable(module.register)\n"
    "print(f'connects={len(attempts)}')\n"
    "sys.exit(1 if attempts else 0)\n"
)


def test_importing_plugin_performs_no_network_io() -> None:
    """Importing either plugin entry must not open any socket."""
    env = dict(os.environ)
    env["API_DEBUG"] = "true"
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        "importing the plugin attempted network I/O:\n" f"{proc.stdout}\n{proc.stderr}"
    )
