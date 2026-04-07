"""Pytest configuration shared across all test modules.

Adds ``src/`` to ``sys.path`` so that imports like ``from dyvine.core import ...``
resolve to the local source tree rather than an installed package. This ensures a
single canonical import path (``dyvine.*``) — using the project root instead would
expose a second path (``src.dyvine.*``), causing double-registration errors in
modules with side effects at import time (e.g. Prometheus metrics in storage.py).
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
