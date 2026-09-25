"""Directory-plugin entry point (see plugin.yaml).

When hermes installs this repository from a Git URL it loads the repo
root as ``hermes_plugins.dyvine`` and calls :func:`register`. The
engine lives under ``src/`` (pip layout), so this shim puts ``src/``
on ``sys.path`` when the plugin package itself is not already
importable (pip installs need no path surgery), then re-exports
``register``.

The ``__path__`` extension below serves local tooling: pytest
imports the repo root as the top-level ``dyvine`` package during
collection (any ``__init__.py`` beside the test tree becomes a
``Package`` node), which would shadow the real engine at
``src/dyvine``. Pointing the package path at ``src/dyvine`` keeps
the canonical single spelling (``dyvine.*``) working no matter
which loader claims the ``dyvine`` name first.

pytest's importlib mode only resolves the root as a package when the
checkout directory name is a valid identifier. Elsewhere
(``dyvine-main``, hyphenated worktree names) it imports this file as
a plain ``__init__`` module: that module has no ``__path__`` and
cannot shadow the engine, so the extension is skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

# Resolve ``dyvine.*`` submodules to the engine tree. Prepend (not
# replace) so sibling-plugin imports keep working if hermes ever
# places helper modules beside this file. Only package loads define
# ``__path__``; a plain-module load has nothing to extend.
_ENGINE = str(_SRC / "dyvine")
_PACKAGE_PATH: list[str] | None = globals().get("__path__")
if _PACKAGE_PATH is not None and _ENGINE not in _PACKAGE_PATH:
    _PACKAGE_PATH.insert(0, _ENGINE)

try:
    import dyvine_hermes.plugin  # noqa: F401
except ModuleNotFoundError as exc:
    # Retry with ``src/`` on the path only when the plugin package
    # itself is missing. Any other missing module (a third-party
    # dependency of the plugin internals, a typo deeper inside)
    # re-raises immediately: retrying would fail again with a
    # misleading traceback while pointlessly polluting ``sys.path``.
    if exc.name not in {"dyvine_hermes", "dyvine_hermes.plugin"}:
        raise
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    import dyvine_hermes.plugin  # noqa: F401

from dyvine_hermes.plugin import register  # noqa: E402,F401

__all__ = ["register"]
