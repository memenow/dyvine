"""Directory-plugin entry point (see plugin.yaml).

When hermes installs this repository from a Git URL it loads the repo
root as ``hermes_plugins.dyvine`` and calls :func:`register`. The
engine lives under ``src/`` (pip layout), so this shim puts ``src/``
on ``sys.path`` when the plugin package itself is not already
importable (pip installs need no path surgery), then re-exports
``register``.

The ``__path__`` extension below serves exactly one loader: local
tooling that imports the repo root as the top-level ``dyvine``
package, which would otherwise shadow the real engine at
``src/dyvine``. It stays off for every other loader name (notably
``hermes_plugins.dyvine``), where it would create a second module
identity for the same files and break singletons and ``isinstance``
checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

if __name__ == "dyvine":
    # Only the top-level shadow spelling needs rescuing: resolve
    # ``dyvine.*`` submodules to the engine tree. Prepend (not
    # replace) so sibling modules beside this file keep working.
    _ENGINE = str(_SRC / "dyvine")
    if _ENGINE not in __path__:  # type: ignore[name-defined]
        __path__.insert(0, _ENGINE)  # type: ignore[name-defined]

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
