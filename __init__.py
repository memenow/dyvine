"""Directory-plugin entry point (see plugin.yaml).

When hermes installs this repository from a Git URL it loads the repo
root as ``hermes_plugins.dyvine`` and calls :func:`register`. The
engine lives under ``src/`` (pip layout), so this shim puts ``src/``
on ``sys.path`` when the engine is not already importable (pip
installs need no path surgery), then re-exports ``register``.

The ``__path__`` extension below serves local tooling: pytest
imports the repo root as the top-level ``dyvine`` package during
collection (any ``__init__.py`` beside the test tree becomes a
``Package`` node), which would shadow the real engine at
``src/dyvine``. Pointing the package path at ``src/dyvine`` keeps
the canonical single spelling (``dyvine.*``) working no matter
which loader claims the ``dyvine`` name first.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

# Resolve ``dyvine.*`` submodules to the engine tree. Prepend (not
# replace) so sibling-plugin imports keep working if hermes ever
# places helper modules beside this file.
_ENGINE = str(_SRC / "dyvine")
if _ENGINE not in __path__:  # type: ignore[name-defined]
    __path__.insert(0, _ENGINE)  # type: ignore[name-defined]

try:
    import dyvine_hermes.plugin  # noqa: F401
except ImportError:
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    import dyvine_hermes.plugin  # noqa: F401

from dyvine_hermes.plugin import register  # noqa: E402,F401

__all__ = ["register"]
