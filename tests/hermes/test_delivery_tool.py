"""The Hermes delivery tool cannot bypass the persistent ledger."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dyvine.core.exceptions import DeliveryError
from dyvine.services.delivery import FeishuCredentials
from dyvine_hermes import tools as tools_mod


class MissingGroupLedger:
    async def get_group(self, **kwargs: Any) -> None:
        return None


async def test_tool_refuses_unverified_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dyvine.core.settings import settings as live_settings

    monkeypatch.setattr(live_settings.douyin, "download_root", str(tmp_path))
    monkeypatch.setattr(
        tools_mod,
        "get_engine",
        lambda: SimpleNamespace(delivery_ledger=MissingGroupLedger()),
    )
    monkeypatch.setattr(
        FeishuCredentials,
        "from_hermes_default",
        staticmethod(lambda: FeishuCredentials("id", "secret")),
    )
    user_dir = tmp_path / "nick"
    user_dir.mkdir()
    with pytest.raises(DeliveryError, match="not verified"):
        await tools_mod._delivery_send_account(
            {
                "round": "r1",
                "sec_user_id": "sec-1",
                "nickname": "nick",
                "chat_id": "chat-1",
                "homepage": "https://example.com",
                "user_dir": str(user_dir),
            }
        )


async def test_tool_refuses_unimported_legacy_skip_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools_mod, "get_engine", lambda: SimpleNamespace())
    with pytest.raises(DeliveryError, match="require import"):
        await tools_mod._delivery_send_account(
            {
                "round": "r1",
                "sec_user_id": "sec-1",
                "nickname": "nick",
                "chat_id": "chat-1",
                "homepage": "https://example.com",
                "user_dir": str(tmp_path),
                "already_sent": ["/old/path.mp4"],
            }
        )
