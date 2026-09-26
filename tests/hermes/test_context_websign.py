"""Hermes engine signing initialization and cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import dyvine.services.websign as websign_mod
import dyvine_hermes.context as context_mod


def _settings(*, enabled: bool = True, retry_once: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        websign_enabled=enabled,
        websign_page_url="https://www.douyin.com/",
        user_agent="test-user-agent",
        websign_init_timeout_seconds=120.0,
        websign_sign_timeout_seconds=30.0,
        websign_retry_once=retry_once,
    )


def test_enabled_signing_installs_patch_and_optional_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MagicMock()
    make_provider = MagicMock(return_value=provider)
    install_patch = MagicMock()
    install_retry = MagicMock()
    uninstall = MagicMock()
    monkeypatch.setattr(websign_mod, "WebSignProvider", make_provider)
    monkeypatch.setattr(websign_mod, "install_websign_patch", install_patch)
    monkeypatch.setattr(websign_mod, "install_fetch_retry", install_retry)
    monkeypatch.setattr(websign_mod, "uninstall_websign_patch", uninstall)

    assert context_mod._install_websign(_settings()) is provider
    make_provider.assert_called_once_with(
        page_url="https://www.douyin.com/",
        user_agent="test-user-agent",
        init_timeout_seconds=120.0,
        sign_timeout_seconds=30.0,
    )
    install_patch.assert_called_once_with(provider)
    install_retry.assert_called_once_with(provider)
    provider.close.assert_not_called()

    context_mod._stop_websign(provider)
    uninstall.assert_called_once()
    provider.close.assert_called_once()


def test_disabled_signing_never_creates_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_provider = MagicMock()
    monkeypatch.setattr(websign_mod, "WebSignProvider", make_provider)
    assert context_mod._install_websign(_settings(enabled=False)) is None
    make_provider.assert_not_called()


def test_retry_patch_respects_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MagicMock()
    install_retry = MagicMock()
    monkeypatch.setattr(websign_mod, "WebSignProvider", lambda **_: provider)
    monkeypatch.setattr(websign_mod, "install_websign_patch", MagicMock())
    monkeypatch.setattr(websign_mod, "install_fetch_retry", install_retry)
    assert context_mod._install_websign(_settings(retry_once=False)) is provider
    install_retry.assert_not_called()
    context_mod._stop_websign(provider)


def test_partial_install_failure_uninstalls_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MagicMock()
    uninstall = MagicMock()
    monkeypatch.setattr(websign_mod, "WebSignProvider", lambda **_: provider)
    monkeypatch.setattr(websign_mod, "install_websign_patch", MagicMock())
    monkeypatch.setattr(
        websign_mod,
        "install_fetch_retry",
        MagicMock(side_effect=RuntimeError("retry install failed")),
    )
    monkeypatch.setattr(websign_mod, "uninstall_websign_patch", uninstall)
    with pytest.raises(RuntimeError, match="retry install failed"):
        context_mod._install_websign(_settings())
    uninstall.assert_called_once()
    provider.close.assert_called_once()


async def test_close_engine_cleans_signer_even_if_pool_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MagicMock()
    uninstall = MagicMock()
    monkeypatch.setattr(websign_mod, "uninstall_websign_patch", uninstall)
    sessions = SimpleNamespace(
        aclose=AsyncMock(side_effect=RuntimeError("pool failure"))
    )
    context_mod._ENGINE = SimpleNamespace(
        sessions=sessions,
        websign_provider=provider,
        r2_executor=None,
        r2_head_executor=None,
    )
    with pytest.raises(RuntimeError, match="pool failure"):
        await context_mod.close_engine()
    assert context_mod._ENGINE is None
    uninstall.assert_called_once()
    provider.close.assert_called_once()
    await context_mod.close_engine()
