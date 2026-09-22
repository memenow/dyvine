"""Hermes plugin registration for Dyvine.

:func:`register` is the entry point the gateway calls with a
:class:`PluginContext`: it registers every tool in
:data:`TOOL_SPECS <dyvine_hermes.tools.TOOL_SPECS>` as async tools of
the ``dyvine`` toolset. Registration is side-effect free beyond the
registry itself: no database pools, no network, no f2 import. The
engine boots on the first tool call (see
:mod:`dyvine_hermes.context`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dyvine_hermes.tools import TOOL_SPECS, as_tool_handler

TOOLSET = "dyvine"

#: Bundled ops skill (SOP + tool manual), resolvable as
#: ``dyvine:dyvine-ops``. Lives beside the code so both the
#: directory plugin and the pip install find it.
SKILL_NAME = "dyvine-ops"
SKILL_DESCRIPTION = (
    "Douyin batch delivery SOP (paired batches, verification, "
    "cleanup) plus the dyvine tool manual."
)


def skill_path() -> Path:
    """Return the bundled skill file (ships in the wheel too)."""
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"


#: Every tool needs the database; f2-backed tools additionally need the
#: Douyin cookie. Declared per tool (hermes surfaces these at install).
_DB_ENV = ["DATABASE_URL"]
_F2_ENV = ["DATABASE_URL", "DOUYIN_COOKIE"]

#: Tools that never touch Douyin (pure Postgres reads/writes).
_DB_ONLY_TOOLS = frozenset(
    {
        "dyvine.queue.import_seeds",
        "dyvine.queue.enqueue",
        "dyvine.queue.claim",
        "dyvine.queue.update",
        "dyvine.queue.list",
        "dyvine.queue.status",
        "dyvine.queue.release",
        "dyvine.rounds.create",
        "dyvine.rounds.list",
        "dyvine.delivery.status",
        "dyvine.profiles.upsert",
        "dyvine.profiles.get",
        "dyvine.operation.get",
    }
)


def register(ctx: Any) -> None:
    """Register all dyvine tools on the gateway context."""
    for spec in TOOL_SPECS:
        requires_env = list(_DB_ENV) if spec.name in _DB_ONLY_TOOLS else list(_F2_ENV)
        ctx.register_tool(
            spec.name,
            TOOLSET,
            spec.schema,
            as_tool_handler(spec.handler),
            is_async=True,
            description=spec.description,
            requires_env=requires_env,
        )
    ctx.register_skill(SKILL_NAME, skill_path(), description=SKILL_DESCRIPTION)
