"""Tests for the hermes plugin registration and tool handlers."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import dyvine_hermes.tools as tools_mod
from dyvine_hermes.plugin import TOOLSET, register
from dyvine_hermes.tools import TOOL_NAMES, TOOL_SPECS, jsonable


class FakeContext:
    """Minimal ``PluginContext`` double capturing ``register_tool`` calls."""

    def __init__(self) -> None:
        """Create an empty registration log."""
        self.calls: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []

    def register_cli_command(self, **kwargs: Any) -> None:
        """Record a Hermes plugin CLI registration."""
        self.cli_commands.append(kwargs)

    def register_skill(self, name: str, path: Path, **kwargs: Any) -> None:
        """Record one skill registration."""
        self.skills.append({"name": name, "path": path, **kwargs})

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Any,
        **kwargs: Any,
    ) -> None:
        """Record one tool registration."""
        self.calls.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                **kwargs,
            }
        )


def _repo_root() -> Path:
    """Return the repository root (parent of ``src/``)."""
    return Path(tools_mod.__file__).resolve().parents[2]


def test_manifest_matches_tool_table() -> None:
    """``plugin.yaml`` provides_tools mirrors TOOL_SPECS exactly."""
    manifest = yaml.safe_load((_repo_root() / "plugin.yaml").read_text())
    assert manifest["name"] == "dyvine"
    assert manifest["provides_tools"] == list(TOOL_NAMES)
    assert "DATABASE_URL" in manifest["requires_env"]
    assert "DOUYIN_COOKIE" in manifest["requires_env"]


def test_manifest_dependencies_pinned_and_complete() -> None:
    """Every runtime dep declared, each with an upper bound.

    ``hermes plugins doctor`` warns on unpinned entries; the set below
    includes lazy browser signing dependencies (plus ``alembic``
    deliberately excluded — migrations run operator-side).
    """
    import re

    manifest = yaml.safe_load((_repo_root() / "plugin.yaml").read_text())
    deps = manifest["python_dependencies"]
    assert all(re.search(r"<|==|~=", req) for req in deps), f"unpinned entries: {deps}"
    dists = {re.split(r"[<>=!~\[;\s]", req, maxsplit=1)[0].strip() for req in deps}
    assert dists == {
        "f2",
        "playwright",
        "sqlalchemy",
        "asyncpg",
        "pydantic",
        "pydantic-settings",
        "httpx",
        "boto3",
        "prometheus-client",
        "psutil",
        "python-dotenv",
    }


def test_register_registers_every_tool_async() -> None:
    """Every spec registers once, async, on the dyvine toolset."""
    ctx = FakeContext()
    register(ctx)  # type: ignore[arg-type]
    assert [call["name"] for call in ctx.calls] == list(TOOL_NAMES)
    assert {call["toolset"] for call in ctx.calls} == {TOOLSET}
    assert all(call["is_async"] is True for call in ctx.calls)
    assert all(call["schema"]["type"] == "object" for call in ctx.calls)
    # DB-only tools skip the Douyin cookie; f2 tools require it.
    by_name = {call["name"]: call for call in ctx.calls}
    assert by_name["dyvine.queue.list"]["requires_env"] == ["DATABASE_URL"]
    assert "DOUYIN_COOKIE" in by_name["dyvine.posts.download"]["requires_env"]


async def test_registered_handlers_return_json_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered handlers serialize (registry rejects non-strings)."""
    import json

    queue = MagicMock()
    queue.round_status = AsyncMock(return_value={"round": "r1", "total": 0})
    engine = MagicMock()
    engine.queue = queue
    monkeypatch.setattr(tools_mod, "get_engine", lambda: engine)
    ctx = FakeContext()
    register(ctx)  # type: ignore[arg-type]
    by_name = {call["name"]: call for call in ctx.calls}
    raw = await by_name["dyvine.queue.status"]["handler"]({"round": "r1"})
    assert isinstance(raw, str)
    assert json.loads(raw) == {"round": "r1", "total": 0}


def test_entry_point_resolves_to_register() -> None:
    """The pip entry point loads the package and finds ``register``."""
    import importlib.metadata

    (entry_point,) = [
        ep
        for ep in importlib.metadata.entry_points(group="hermes_agent.plugins")
        if ep.name == "dyvine"
    ]
    assert entry_point.value == "dyvine_hermes"
    assert callable(entry_point.load().register)


def test_register_registers_bundled_skill() -> None:
    """The ops skill registers once with a valid name and file."""
    import re

    from dyvine_hermes.plugin import SKILL_NAME, skill_path

    ctx = FakeContext()
    register(ctx)  # type: ignore[arg-type]
    assert len(ctx.skills) == 1
    (skill,) = ctx.skills
    assert skill["name"] == SKILL_NAME
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", skill["name"])
    assert skill["path"] == skill_path()
    assert skill_path().is_file()
    assert skill["description"]


def test_register_exposes_weekly_cli_without_booting_engine() -> None:
    """The registered argparse tree accepts the exact cron command."""
    import argparse

    ctx = FakeContext()
    register(ctx)  # type: ignore[arg-type]
    assert len(ctx.cli_commands) == 1
    (command,) = ctx.cli_commands
    assert command["name"] == "dyvine"
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    parsed = parser.parse_args(
        ["weekly", "run-once", "--round", "weekly0913", "--dry-run"]
    )
    assert parsed.round_name == "weekly0913"
    assert parsed.dry_run is True
    assert parsed.func is command["handler_fn"]


def test_skill_manual_covers_every_tool() -> None:
    """The bundled manual names every registered tool (no drift)."""
    from dyvine_hermes.plugin import skill_path

    manual = skill_path().read_text()
    missing = [name for name in TOOL_NAMES if name not in manual]
    assert not missing, f"skill manual omits tools: {missing}"


def _schema_dummy(prop: dict[str, Any]) -> Any:
    """Type-correct dummy for one schema property."""
    return {
        "string": "x",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "array": [],
        "object": {},
    }[prop.get("type", "string")]


class _AsyncStub:
    """Service double whose every method returns fresh JSON-safe data."""

    def __init__(self, value: Any) -> None:
        """Capture the per-call payload template."""
        self._value = value

    def __getattr__(self, name: str) -> Any:
        """Return an async method yielding a copy of the template."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            import copy

            return copy.deepcopy(self._value)

        return _call


def _stub_engine() -> Any:
    """Build an Engine-shaped namespace of async stubs."""
    from types import SimpleNamespace

    return SimpleNamespace(
        users=_AsyncStub({"ok": True}),
        posts=_AsyncStub({"ok": True}),
        livestreams=_AsyncStub({"ok": True}),
        queue=_AsyncStub({"ok": True}),
        queue_repo=SimpleNamespace(
            list_entries=_AsyncStub(
                [
                    SimpleNamespace(
                        chat_id="x", nickname="x", round="r1", sec_user_id="s1"
                    )
                ]
            ).list_entries
        ),
        profiles=_AsyncStub({"ok": True}),
        send_status=_AsyncStub({"ok": True}),
        delivery_ledger=_AsyncStub(set()),
        operations=_AsyncStub({"ok": True}),
        round_repo=_AsyncStub([{"round": "r1"}]),
    )


# Handlers needing extra arg sets beyond required-only (branch coverage).
_EXTRA_HANDLER_RUNS: dict[str, list[dict[str, Any]]] = {
    "dyvine.delivery.status": [{"nickname": "n"}, {"sec_user_id": "s1"}],
}
# Required-only calls that must fail closed (clean error, no KeyError).
_EXPECTED_FAILURES: dict[str, type[Exception]] = {
    "dyvine.delivery.status": ValueError,
}


@pytest.mark.parametrize(
    "spec", list(TOOL_SPECS), ids=[spec.name for spec in TOOL_SPECS]
)
async def test_every_handler_runs_with_schema_args(
    spec: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every raw handler runs on schema-shaped args (plumbing lock).

    Builds the required args from each tool's own JSON schema and runs
    the handler against stub services: a wrong service-method name, a
    misspelled kwarg, or a schema/required mismatch fails loudly here
    instead of at 3 AM in production.
    """
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import dyvine.services.delivery as delivery_mod

    monkeypatch.setattr(tools_mod, "get_engine", lambda: _stub_engine())

    channel_result = SimpleNamespace(
        nickname="n",
        chat_id="c",
        total_files=0,
        sent_files=0,
        failed_files=0,
        status="completed",
    )

    class _FakeChannel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Ignore constructor args (no credentials needed)."""

        send_account = AsyncMock(return_value=channel_result)

    monkeypatch.setattr(delivery_mod, "FeishuGroupChannel", _FakeChannel)
    monkeypatch.setattr(
        delivery_mod.FeishuCredentials,
        "from_hermes_default",
        classmethod(lambda cls, env_path=None: MagicMock()),
    )
    monkeypatch.setattr(
        delivery_mod, "send_via_hermes", AsyncMock(return_value={"sent": True})
    )

    required = spec.schema.get("required", [])
    properties = spec.schema.get("properties", {})
    base = {name: _schema_dummy(properties[name]) for name in required}
    if spec.name in _EXPECTED_FAILURES:
        with pytest.raises(_EXPECTED_FAILURES[spec.name]):
            await spec.handler(base)
    else:
        result = await spec.handler(base)
        # Must survive the registry's JSON serialization.
        json.dumps(jsonable(result))
    for args in _EXTRA_HANDLER_RUNS.get(spec.name, []):
        result = await spec.handler({**base, **args})
        json.dumps(jsonable(result))


def test_get_engine_builds_offline_and_caches() -> None:
    """The engine constructs without I/O and caches per process."""
    import dyvine_hermes.context as context_mod

    context_mod._ENGINE = None
    try:
        assert context_mod.get_engine() is context_mod.get_engine()
        engine = context_mod.get_engine()
        assert engine.owner_id
        assert engine.users is not None
        assert engine.posts is not None
        assert engine.livestreams is not None
        assert engine.queue is not None
    finally:
        asyncio.run(context_mod.close_engine())


def test_root_shim_exposes_register() -> None:
    """The repo-root ``__init__.py`` re-exports ``register``."""
    root = _repo_root()
    spec = importlib.util.spec_from_file_location(
        "dyvine_plugin_root", root / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.register)


def test_jsonable_converts_service_shapes() -> None:
    """Enums, paths, dataclasses, and pydantic models all normalize."""
    import dataclasses
    from enum import Enum

    class _Kind(Enum):
        A = "a"

    @dataclasses.dataclass
    class _Row:
        kind: _Kind
        path: Path

    assert jsonable({"k": _Kind.A, "p": Path("/x")}) == {"k": "a", "p": "/x"}
    assert jsonable(_Row(_Kind.A, Path("/x"))) == {"kind": "a", "path": "/x"}


async def test_queue_status_handler_uses_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers delegate to the engine service and normalize output."""
    from dyvine_hermes.tools import _queue_status

    queue = MagicMock()
    queue.round_status = AsyncMock(
        return_value={"round": "r1", "total": 2, "by_status": {"pending": 2}}
    )
    engine = MagicMock()
    engine.queue = queue
    monkeypatch.setattr(tools_mod, "get_engine", lambda: engine)
    assert await _queue_status({"round": "r1"}) == {
        "round": "r1",
        "total": 2,
        "by_status": {"pending": 2},
    }
    queue.round_status.assert_awaited_once_with("r1")


async def test_queue_update_rejects_non_object_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict ``fields`` fail before touching the repository."""
    from dyvine_hermes.tools import _queue_update

    engine = MagicMock()
    monkeypatch.setattr(tools_mod, "get_engine", lambda: engine)
    with pytest.raises(ValueError, match="fields must be an object"):
        await _queue_update({"key": "r1:s1", "fields": ["nope"]})
    engine.queue.report_progress.assert_not_called()
