"""Tests for the deferred f2 import placeholder."""

from __future__ import annotations

import copy

import pytest

from dyvine.core._lazy_f2 import LazyF2Symbol


def test_dunder_probes_do_not_resolve() -> None:
    """Interpreter probes must not trigger the real import.

    ``copy``/``pickle``/``inspect`` machinery ``getattr``s dunders on
    arbitrary objects; answering by resolving would run f2's
    import-time HTTPS side effects. A bogus module coordinate proves
    no import was attempted: any resolution would raise
    ``ModuleNotFoundError`` instead of returning or raising
    ``AttributeError``.
    """
    lazy = LazyF2Symbol("no.such.module.anywhere", "Nope")
    for dunder in ("__copy__", "__deepcopy__", "__setstate__", "__wrapped__"):
        with pytest.raises(AttributeError):
            _ = getattr(lazy, dunder)
    # ``object`` supplies ``__getstate__`` (3.11+), so normal lookup
    # succeeds without reaching ``__getattr__`` -- the requirement is
    # only that no import happens.
    _ = lazy.__getstate__
    assert lazy.__dict__.get("_resolved") is None
    # copy.copy must survive the placeholder without importing.
    clone = copy.copy(lazy)
    assert clone.__dict__["_module_name"] == "no.such.module.anywhere"


def test_uninitialized_placeholder_raises_not_recurses() -> None:
    """A placeholder that skipped ``__init__`` fails fast.

    ``object.__new__``/copy/pickle intermediate states have no
    ``_resolved`` entry; resolving there must raise ``AttributeError``,
    not recurse through ``__getattr__`` into ``RecursionError``.
    """
    lazy = object.__new__(LazyF2Symbol)
    with pytest.raises(AttributeError, match="uninitialized"):
        lazy._resolve()
    with pytest.raises(AttributeError, match="uninitialized"):
        _ = lazy.anything


def test_resolve_caches_real_symbol() -> None:
    """First use imports once; later uses reuse the cached symbol."""
    lazy = LazyF2Symbol("os.path", "join")
    import os.path

    assert lazy("a", "b") == os.path.join("a", "b")
    assert lazy._resolve() is os.path.join
    assert lazy.__dict__["_resolved"] is os.path.join
