"""Regression: importing dyvine must not perform network I/O.

The upstream f2 SDK executes a real HTTPS request while its modules are
imported (a msToken fetch in a pydantic model class body), which makes
collection slow, network-dependent, and prone to GC-time socket warnings
under ``-W error``. dyvine must therefore never import f2 at module
scope; the SDK may only load lazily when a real handler is constructed.
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
    "import dyvine.main\n"
    "print(f'connects={len(attempts)}')\n"
    "sys.exit(1 if attempts else 0)\n"
)


def test_importing_dyvine_main_performs_no_network_io() -> None:
    """Importing the app entry point must not open any socket."""
    env = dict(os.environ)
    env["API_DEBUG"] = "true"
    env["SECURITY_REQUIRE_API_KEY"] = "false"
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
        "importing dyvine.main attempted network I/O:\n" f"{proc.stdout}\n{proc.stderr}"
    )
