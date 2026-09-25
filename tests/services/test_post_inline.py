"""Awaited full-download operation contract for the weekly runner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import ServiceError
from dyvine.services import posts as posts_module
from dyvine.services.posts import PostService
from tests.fake_repos import FakeOperationRepository


async def _service() -> tuple[PostService, FakeOperationRepository, str]:
    store = FakeOperationRepository()
    handler = MagicMock()
    handler.kwargs = {"mode": "all"}
    handler.fetch_user_profile = AsyncMock(return_value=MagicMock(nickname="Account"))
    service = PostService(handler=handler, operation_store=store)
    operation = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="sec_1",
        status="pending",
        message="Scheduled",
    )
    return service, store, operation.operation_id


async def test_inline_bulk_awaits_completion_without_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, operation_id = await _service()

    async def finish(*args: object, **kwargs: object) -> None:
        await store.update_operation(
            operation_id,
            status="completed",
            message="Complete",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": None},
        )

    runner = AsyncMock(side_effect=finish)
    monkeypatch.setattr(service, "_run_bulk_download", runner)
    result = await service.download_bulk_inline(
        "sec_1", operation_id=operation_id, max_cursor=42
    )
    assert result.operation_id == operation_id
    assert result.status.value == "completed"
    runner.assert_awaited_once()
    assert runner.await_args.args[:3] == (operation_id, "sec_1", 42)


async def test_inline_bulk_rejects_mismatched_operation() -> None:
    service, store, operation_id = await _service()
    with pytest.raises(ServiceError, match="does not match"):
        await service.download_bulk_inline("wrong_sec", operation_id=operation_id)
    assert (await store.get_operation(operation_id)).status == "pending"


async def test_inline_bulk_marks_cancelled_operation_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, operation_id = await _service()
    monkeypatch.setattr(
        service,
        "_run_bulk_download",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await service.download_bulk_inline("sec_1", operation_id=operation_id)
    operation = await store.get_operation(operation_id)
    assert operation.status == "failed"
    assert operation.error == "cancelled"


async def test_full_download_checkpoints_next_complete_page_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, operation_id = await _service()
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    service.handler.get_or_add_user_data = AsyncMock(return_value=user_dir)

    class FakeUserDB:
        async def __aenter__(self) -> FakeUserDB:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(posts_module, "AsyncUserDB", lambda *_args: FakeUserDB())
    monkeypatch.setattr(
        posts_module, "relative_to_download_root", lambda _path: "Account"
    )
    service._fetch_posts_batch = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"aweme_list": [{"aweme_id": "one"}], "has_more": True, "max_cursor": 123},
            RuntimeError("upstream unavailable"),
        ]
    )
    service._process_posts_batch = AsyncMock(return_value=0)  # type: ignore[method-assign]
    await service._run_bulk_download(
        operation_id,
        "sec_1",
        0,
        profile=SimpleNamespace(nickname="Account", aweme_count=2),
    )
    saved = await store.get_operation(operation_id)
    assert saved.status == "failed"
    assert saved.metadata["resume_cursor"] == 123


async def test_inline_bulk_preserves_original_error() -> None:
    """Inline failures persist the root cause, not a canned string."""
    from unittest.mock import patch

    service, store, operation_id = await _service()
    with patch.object(
        service,
        "_run_bulk_download",
        new=AsyncMock(side_effect=RuntimeError("profile exploded")),
    ):
        with pytest.raises(RuntimeError, match="profile exploded"):
            await service.download_bulk_inline("sec_1", operation_id=operation_id)
    operation = await store.get_operation(operation_id)
    assert operation.status == "failed"
    assert operation.error == "profile exploded"
