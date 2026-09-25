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
    """Return the bundled skill file (ships in the wheel too).

    Fails fast with the resolved path when packaging dropped the file:
    surfacing it here beats a cryptic gateway error inside
    ``register_skill``.
    """
    path = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"bundled skill file is missing: {path}")
    return path


def _setup_cli(subparser: Any) -> None:
    """Install the bounded weekly command under ``hermes dyvine``."""
    sections = subparser.add_subparsers(dest="dyvine_section", required=True)
    weekly = sections.add_parser("weekly", help="Run weekly delivery steps")
    actions = weekly.add_subparsers(dest="dyvine_weekly_action", required=True)
    run = actions.add_parser("run-once", help="Advance one checkpointed weekly step")
    run.add_argument("--round", dest="round_name", help="Resume an existing round")
    run.add_argument("--dry-run", action="store_true", help="Inspect without writes")
    run.set_defaults(func=_handle_cli)


def _handle_cli(args: Any) -> None:
    """Boot the runner only when the operator invokes the CLI command.

    Tolerant of a bare namespace: the gateway may dispatch this handler
    without the ``weekly run-once`` subparser (where ``required=True``
    never runs), and that path must degrade to defaults, not
    ``AttributeError``.
    """
    from dyvine_hermes.weekly import run_cli

    run_cli(
        round_name=getattr(args, "round_name", None),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


#: Every tool needs the database; f2-backed tools additionally need the
#: Douyin cookie. Declared per tool (hermes surfaces these at install).
_DB_ENV = ["DATABASE_URL"]
_F2_ENV = ["DATABASE_URL", "DOUYIN_COOKIE"]

#: Tools that never touch the engine at all (pure subprocess/HTTPS).
_NO_ENV_TOOLS = frozenset(
    {
        # Only shells out to ``hermes send``: no DB, no cookie.
        "dyvine.notify.send",
    }
)

#: Tools that boot the engine (so they need the database) but never touch
#: Douyin itself: pure Postgres reads/writes, Feishu delivery, and plain
#: HTTPS share-link resolution.
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
        "dyvine.delivery.send_account",
        "dyvine.delivery.status",
        "dyvine.users.resolve",
        "dyvine.profiles.upsert",
        "dyvine.profiles.get",
        "dyvine.operation.get",
    }
)


def register(ctx: Any) -> None:
    """Register all dyvine tools on the gateway context."""
    known = {spec.name for spec in TOOL_SPECS}
    stale = (_NO_ENV_TOOLS | _DB_ONLY_TOOLS) - known
    if stale:
        # A rename/typo here would otherwise silently demote the tool to
        # the F2 branch and over-declare its env: fail the boot loudly.
        raise RuntimeError(
            f"stale env classification for unknown tools: {sorted(stale)}"
        )
    for spec in TOOL_SPECS:
        if spec.name in _NO_ENV_TOOLS:
            requires_env = []
        else:
            requires_env = (
                list(_DB_ENV) if spec.name in _DB_ONLY_TOOLS else list(_F2_ENV)
            )
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
    ctx.register_cli_command(
        name="dyvine",
        help="Run Dyvine maintenance commands",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
    )
