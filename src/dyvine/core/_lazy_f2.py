"""Deferred-import placeholder for the upstream f2 SDK.

Importing any ``f2.*`` module performs real HTTPS requests as a side
effect (a token fetch runs in a pydantic model class body at import
time), so dyvine modules must not import f2 at module scope: doing so
makes collection slow, network-dependent, and prone to GC-time socket
warnings under ``-W error``.

:class:`LazyF2Symbol` is a module-global stand-in with the same name as
the real SDK symbol. It lives in the consumer's ``__dict__`` from
import time, so :func:`getattr`, :mod:`unittest.mock.patch`, and
:mod:`pytest.monkeypatch` all see a plain global without triggering an
import; the real symbol loads on first *use* (call or attribute
access) instead.

Limitations (all verified absent from the codebase before adopting
this): the placeholder cannot serve ``except`` clauses (use a helper
that returns the resolved exception type), ``isinstance`` checks, or
base-class positions.
"""

from __future__ import annotations

import importlib
from typing import Any

# Dunder probes issued by copy/pickle/inspect against arbitrary objects.
# ``__getattr__`` must answer these with ``AttributeError`` instead of
# resolving f2: none of them can be satisfied without the real symbol,
# and resolving would run import-time HTTPS as a side effect. Any other
# missing attribute still resolves, since only the real symbol can say
# whether it exists.
_NON_RESOLVING_DUNDERS = frozenset(
    {
        "__copy__",
        "__deepcopy__",
        "__getstate__",
        "__setstate__",
        "__reduce__",
        "__reduce_ex__",
        "__getnewargs__",
        "__getnewargs_ex__",
        "__getinitargs__",
        "__wrapped__",
    }
)


class LazyF2Symbol:
    """Stand-in for one f2 SDK symbol, resolved on first use.

    Args:
        module_name: Fully qualified module to import on first use.
        symbol_name: Attribute to fetch from the imported module.
    """

    def __init__(self, module_name: str, symbol_name: str) -> None:
        """Store the deferred import coordinates without importing."""
        self._module_name = module_name
        self._symbol_name = symbol_name
        self._resolved: Any = None

    def _resolve(self) -> Any:
        """Import and cache the real SDK symbol.

        Reads coordinates from ``self.__dict__`` instead of attribute
        access: during ``__new__``/copy/pickle intermediate states the
        instance may not have ``_resolved`` set yet, and going through
        ``__getattr__`` there would recurse forever.
        """
        state = self.__dict__
        resolved = state.get("_resolved")
        if resolved is None:
            try:
                module_name = state["_module_name"]
                symbol_name = state["_symbol_name"]
            except KeyError as exc:
                raise AttributeError(
                    "LazyF2Symbol is uninitialized; cannot resolve"
                ) from exc
            module = importlib.import_module(module_name)
            resolved = getattr(module, symbol_name)
            state["_resolved"] = resolved
        return resolved

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Construct/call the resolved SDK symbol."""
        return self._resolve()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Serve class-attribute access (e.g. classmethods) post-resolve.

        Interpreter probes (copy/pickle/inspect dunders) are rejected
        without resolving: answering them would run f2's import-time
        HTTPS side effects from an innocent ``copy.copy``.
        """
        if name in _NON_RESOLVING_DUNDERS:
            raise AttributeError(name)
        return getattr(self._resolve(), name)
