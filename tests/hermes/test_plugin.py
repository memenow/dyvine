"""Tests for the hermes plugin registration and tool handlers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import dyvine_hermes.tools as tools_mod
from dyvine_hermes.plugin import TOOLSET, register
from dyvine_hermes.tools import TOOL_NAMES, jsonable


class FakeContext:
    """Minimal ``PluginContext`` double capturing ``register_tool`` calls."""

    def __init__(self) -> None:
        """Create an empty registration log."""
        self.calls: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []

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
    is the import-hook-traced ``get_engine`` closure (plus ``alembic``
    deliberately excluded — migrations run operator-side).
    """
    import re

    manifest = yaml.safe_load((_repo_root() / "plugin.yaml").read_text())
    deps = manifest["python_dependencies"]
    assert all(re.search(r"<|==|~=", req) for req in deps), f"unpinned entries: {deps}"
    dists = {re.split(r"[<>=!~\[;\s]", req, maxsplit=1)[0].strip() for req in deps}
    assert dists == {
        "f2",
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


def test_skill_manual_covers_every_tool() -> None:
    """The bundled manual names every registered tool (no drift)."""
    from dyvine_hermes.plugin import skill_path

    manual = skill_path().read_text()
    missing = [name for name in TOOL_NAMES if name not in manual]
    assert not missing, f"skill manual omits tools: {missing}"


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
