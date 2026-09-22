"""Hermes-native plugin entry for Dyvine (Douyin download automation).

Importing this package is side-effect free: the engine (services,
database pools, f2 SDK) boots lazily inside :func:`register` via
:mod:`dyvine_hermes.context`, so ``hermes plugins list`` and ``doctor``
never open network connections or database pools.

The ``hermes_agent.plugins`` entry point resolves to this package;
hermes calls :func:`register` with a ``PluginContext``.
"""

from __future__ import annotations

from dyvine_hermes.plugin import TOOLSET, register

__version__ = "0.1.0"

__all__ = ["TOOLSET", "__version__", "register"]
