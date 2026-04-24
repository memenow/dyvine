from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from dyvine.services.lifecycle import LifecycleError, LifecycleManager

CONFIG_JSON = json.dumps(
    {
        "version": "1.0",
        "rules": [
            {
                "content_type": "livestream",
                "retention_days": 180,
                "transition": {"days": 30, "storage_class": "ARCHIVE"},
            }
        ],
        "audit": {
            "enabled": True,
            "log_format": (
                "{timestamp} user={user} action={action} "
                "object_key={object_key} "
                "metadata_size={metadata_size} status={status}"
            ),
            "log_retention_days": 90,
        },
    }
)


def _build_manager(
    rules: dict | None = None,
    audit_config: dict | None = None,
) -> LifecycleManager:
    """Create LifecycleManager without calling __init__."""
    mgr = object.__new__(LifecycleManager)
    mgr.storage = MagicMock()  # type: ignore[attr-defined]
    mgr.storage.list_objects = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    mgr.storage.delete_object = AsyncMock()  # type: ignore[attr-defined]
    # The container normally injects a dedicated audit executor; ``None``
    # falls back to the default asyncio executor, which is enough for tests.
    mgr._executor = None  # type: ignore[attr-defined]
    mgr.rules = rules or {}  # type: ignore[attr-defined]
    mgr.audit_config = audit_config or {  # type: ignore[attr-defined]
        "enabled": False,
        "log_format": (
            "{timestamp} user={user} action={action} "
            "object_key={object_key} "
            "metadata_size={metadata_size} status={status}"
        ),
        "log_retention_days": 90,
    }
    return mgr


# ── _apply_rule_to_object ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_rule_deletion() -> None:
    mgr = _build_manager()
    now = datetime.now(UTC)
    obj = {"Key": "old.mp4", "LastModified": now - timedelta(days=200)}
    rule = {"retention_days": 180}

    action = await mgr._apply_rule_to_object(obj, rule)
    assert action is not None
    assert action["action"] == "delete"
    mgr.storage.delete_object.assert_awaited_once_with("old.mp4")


@pytest.mark.asyncio
async def test_apply_rule_transition() -> None:
    mgr = _build_manager()
    now = datetime.now(UTC)
    obj = {
        "Key": "stream.mp4",
        "LastModified": now - timedelta(days=40),
        "StorageClass": "STANDARD",
    }
    rule = {"transition": {"days": 30, "storage_class": "ARCHIVE"}}

    action = await mgr._apply_rule_to_object(obj, rule)
    assert action is not None
    assert action["action"] == "transition"
    assert action["to_class"] == "ARCHIVE"


@pytest.mark.asyncio
async def test_apply_rule_no_action() -> None:
    mgr = _build_manager()
    now = datetime.now(UTC)
    obj = {"Key": "new.mp4", "LastModified": now - timedelta(days=1)}
    rule = {
        "retention_days": 180,
        "transition": {"days": 30, "storage_class": "ARCHIVE"},
    }

    action = await mgr._apply_rule_to_object(obj, rule)
    assert action is None


# ── apply_lifecycle_rules ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_lifecycle_rules_deletes_old() -> None:
    now = datetime.now(UTC)
    old_obj = {"Key": "livestream/old.mp4", "LastModified": now - timedelta(days=200)}

    mgr = _build_manager(
        rules={"livestream": {"retention_days": 180, "content_type": "livestream"}},
    )
    mgr.storage.list_objects = AsyncMock(return_value=[old_obj])

    summary = await mgr.apply_lifecycle_rules()
    assert summary["deleted"] == 1


@pytest.mark.asyncio
async def test_apply_lifecycle_rules_transitions() -> None:
    now = datetime.now(UTC)
    obj = {
        "Key": "livestream/rec.mp4",
        "LastModified": now - timedelta(days=35),
        "StorageClass": "STANDARD",
    }
    mgr = _build_manager(
        rules={
            "livestream": {
                "content_type": "livestream",
                "transition": {"days": 30, "storage_class": "ARCHIVE"},
            }
        },
    )
    mgr.storage.list_objects = AsyncMock(return_value=[obj])

    summary = await mgr.apply_lifecycle_rules()
    assert summary["transitioned"] == 1


@pytest.mark.asyncio
async def test_apply_lifecycle_rules_skips_current() -> None:
    now = datetime.now(UTC)
    obj = {"Key": "livestream/new.mp4", "LastModified": now - timedelta(days=1)}
    mgr = _build_manager(
        rules={
            "livestream": {
                "content_type": "livestream",
                "retention_days": 180,
                "transition": {"days": 30, "storage_class": "ARCHIVE"},
            }
        },
    )
    mgr.storage.list_objects = AsyncMock(return_value=[obj])

    summary = await mgr.apply_lifecycle_rules()
    assert summary["deleted"] == 0
    assert summary["transitioned"] == 0


# ── _write_audit_log ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_audit_log_format() -> None:
    mgr = _build_manager(
        audit_config={
            "enabled": True,
            "log_format": (
                "{timestamp} user={user} action={action} "
                "object_key={object_key} "
                "metadata_size={metadata_size} status={status}"
            ),
            "log_retention_days": 90,
        }
    )
    summary = {"details": [{"action": "delete", "object_key": "test/obj.mp4"}]}
    m = mock_open()
    with patch("builtins.open", m), patch.object(mgr, "_rotate_audit_logs"):
        await mgr._write_audit_log(summary)
    written = m().write.call_args[0][0]
    assert "delete" in written
    assert "test/obj.mp4" in written


# ── _load_config ─────────────────────────────────────────────────────────


def test_load_config_success() -> None:
    mgr = object.__new__(LifecycleManager)
    mgr.storage = MagicMock()

    with patch("builtins.open", mock_open(read_data=CONFIG_JSON)):
        mgr._load_config()

    assert "livestream" in mgr.rules
    assert mgr.audit_config["enabled"] is True


def test_load_config_file_not_found_raises() -> None:
    mgr = object.__new__(LifecycleManager)
    mgr.storage = MagicMock()

    with patch("builtins.open", side_effect=FileNotFoundError("missing")):
        with pytest.raises(LifecycleError, match="Failed to load"):
            mgr._load_config()


# ── _rotate_audit_logs ──────────────────────────────────────────────────


def test_rotate_audit_logs_removes_old_files() -> None:
    mgr = _build_manager(
        audit_config={"enabled": True, "log_format": "", "log_retention_days": 30}
    )

    old_file = MagicMock(spec=Path)
    old_file.stem = "r2_lifecycle_audit.20230101"
    old_file.unlink = MagicMock()

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", return_value=[old_file]),
    ):
        mgr._rotate_audit_logs()

    old_file.unlink.assert_called_once()
