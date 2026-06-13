"""Tests for user service downloads, status polling, and cleanup."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dyvine.core.exceptions import OperationNotFoundError
from dyvine.core.operations import OperationStore
from dyvine.services.users import DownloadResponse, UserService


@pytest.mark.asyncio
async def test_start_download_tracks_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify start download tracks operation."""
    scheduled: list[asyncio.Future[None]] = []

    def fake_create_task(coro: object, **_kwargs: Any) -> asyncio.Future[None]:
        """Test helper for test_start_download_tracks_operation."""
        # ``spawn_or_fallback`` forwards a ``name=...`` kwarg to
        # ``asyncio.create_task``; absorb it so the mock can stand in for
        # the real factory.
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.Future()
        future.set_result(None)
        scheduled.append(future)
        return future

    monkeypatch.setattr("dyvine.core.background.asyncio.create_task", fake_create_task)

    service = UserService()
    monkeypatch.setattr(service, "get_user_info", AsyncMock(return_value=MagicMock()))

    response = await service.start_download("user-123")

    assert isinstance(response, DownloadResponse)
    assert response.status == "pending"
    assert response.operation_id
    assert response.task_id == response.operation_id
    assert scheduled, "Expected background task to be scheduled"


@pytest.mark.asyncio
async def test_get_download_status_returns_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get download status returns persisted state."""

    def resolved_future(coro: object, **_kwargs: Any) -> asyncio.Future[None]:
        """Test helper for test_get_download_status_returns_persisted_state."""
        if hasattr(coro, "close"):
            coro.close()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future

    service = UserService()
    monkeypatch.setattr(service, "get_user_info", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("dyvine.core.background.asyncio.create_task", resolved_future)

    start_response = await service.start_download("user-456")
    status_response = await service.get_download_status(start_response.operation_id)

    assert status_response.operation_id == start_response.operation_id
    assert status_response.status == "pending"
    assert status_response.progress == 0.0


@pytest.mark.asyncio
async def test_get_download_status_raises_for_unknown_task() -> None:
    """Verify get download status raises for unknown task."""
    service = UserService()
    with pytest.raises(OperationNotFoundError):
        await service.get_download_status("missing-task")


@pytest.mark.asyncio
async def test_get_download_status_rejects_non_user_operation(tmp_path) -> None:
    """Verify get download status rejects non user operation."""
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
    )
    service = UserService(operation_store=store)

    with pytest.raises(OperationNotFoundError):
        await service.get_download_status(operation.operation_id)


@pytest.mark.asyncio
async def test_get_user_info_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get user info success."""
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "TestUser"
    mock_user_data.avatar_url = "https://example.com/avatar.jpg"
    mock_user_data.signature = "test bio"
    mock_user_data.following_count = 10
    mock_user_data.follower_count = 20
    mock_user_data.total_favorited = 100
    mock_user_data.room_id = None
    mock_user_data._to_raw.return_value = {"user": {}}

    class FakeHandler:
        """Test double used by test_get_user_info_success."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    result = await service.get_user_info("test-user")
    assert result.nickname == "TestUser"
    assert result.user_id == "test-user"
    assert result.is_living is False


@pytest.mark.asyncio
async def test_get_user_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get user info not found."""
    from dyvine.core.exceptions import UserNotFoundError
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = ""

    class FakeHandler:
        """Test double used by test_get_user_info_not_found."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    with pytest.raises(UserNotFoundError):
        await service.get_user_info("missing-user")


@pytest.mark.asyncio
async def test_get_user_info_with_room_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get user info with room data."""
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "LiveUser"
    mock_user_data.avatar_url = "https://example.com/a.jpg"
    mock_user_data.signature = "sig"
    mock_user_data.following_count = 5
    mock_user_data.follower_count = 10
    mock_user_data.total_favorited = 50
    mock_user_data.room_id = 42
    mock_user_data._to_raw.return_value = {"user": {"room_data": '{"status":2}'}}

    class FakeHandler:
        """Test double used by test_get_user_info_with_room_data."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    result = await service.get_user_info("live-user")
    assert result.is_living is True
    assert result.room_id == 42
    # Schema now exposes ``room_data`` as a decoded dict so consumers do
    # not have to re-parse the upstream JSON payload.
    assert result.room_data == {"status": 2}


@pytest.mark.asyncio
async def test_process_download_no_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify process download no posts."""
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    mock_user_data = MagicMock()
    mock_user_data.nickname = "NoPostUser"
    mock_user_data.aweme_count = 0

    class FakeHandler:
        """Test double used by test_process_download_no_posts."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = AsyncMock(return_value=mock_user_data)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="empty-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="empty-user",
        include_posts=True,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.progress == 100.0


@pytest.mark.asyncio
async def test_process_download_sets_failed_on_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify process download sets failed on error."""
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    class FakeHandler:
        """Test double used by test_process_download_sets_failed_on_error."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = AsyncMock(side_effect=RuntimeError("api error"))

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="bad-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="bad-user",
        include_posts=True,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "failed"
    assert refreshed.error == "api error"


@pytest.mark.asyncio
async def test_process_download_skips_when_nothing_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A request with neither posts nor likes should complete immediately."""
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    fetch_profile = AsyncMock()

    class FakeHandler:
        """Test double used by test_process_download_skips_when_nothing_requested."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = fetch_profile

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="opt-out-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="opt-out-user",
        include_posts=False,
        include_likes=False,
        max_items=None,
    )

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.progress == 100.0
    assert refreshed.total_items == 0
    assert refreshed.completed_items == 0
    fetch_profile.assert_not_called()


@pytest.mark.asyncio
async def test_process_download_runs_when_only_likes_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``include_posts=False`` with ``include_likes=True`` routes to likes.

    Asserts the loop walks ``fetch_user_like_videos`` (not the post feed),
    that ``mode`` is tagged ``"like"`` on the handler kwargs, and that
    ``download_favorite`` stays off so f2 does not double-fetch through
    the posts feed.
    """
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    mock_user_data = MagicMock()
    mock_user_data.nickname = "likes-only-user"
    mock_user_data.aweme_count = 0

    fetch_profile = AsyncMock(return_value=mock_user_data)
    fetch_post_videos = MagicMock()
    captured_kwargs: dict[str, Any] = {}

    async def fake_fetch_likes(*_args: Any, **_kwargs: Any) -> Any:
        """Test helper for test_process_download_runs_when_only_likes_requested."""
        # Empty async generator; the loop treats this as "no likes yet"
        # and exits cleanly.
        if False:
            yield None

    class FakeHandler:
        """Test double used by test_process_download_runs_when_only_likes_requested."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            captured_kwargs.update(kwargs)

        fetch_user_profile = fetch_profile
        fetch_user_post_videos = fetch_post_videos
        fetch_user_like_videos = staticmethod(fake_fetch_likes)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="likes-only-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="likes-only-user",
        include_posts=False,
        include_likes=True,
        max_items=None,
    )

    fetch_profile.assert_awaited()
    fetch_post_videos.assert_not_called()
    assert captured_kwargs["mode"] == "like"
    assert captured_kwargs["download_favorite"] is False


@pytest.mark.asyncio
async def test_process_download_breaks_on_sticky_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sticky upstream cursor must terminate the loop immediately.

    The previous implementation incremented ``max_cursor`` by one when
    the upstream returned a value equal to the cursor we already issued,
    relying on the ``max_pages`` cap as a safety net. The synthetic
    increment was rejected by the upstream API, so the fetcher kept
    returning the same window and the loop downloaded duplicates until
    the cap fired. Treating a stuck cursor as the end of the feed is the
    correct termination condition: the test asserts the fetcher is
    called at most once per ``page_count`` round before the loop exits.
    """
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "sticky-cursor-user"
    mock_user_data.aweme_count = 100

    fetch_profile = AsyncMock(return_value=mock_user_data)

    sticky_batch = MagicMock()
    sticky_batch.has_aweme = True
    sticky_batch.aweme_id = ["aw"]
    sticky_batch.has_more = True
    sticky_batch.max_cursor = 0
    sticky_batch._to_list = MagicMock(return_value=[])

    call_count = 0

    async def fake_fetch_posts(*_args: Any, **_kwargs: Any) -> Any:
        """Test helper for test_process_download_breaks_on_sticky_cursor."""
        nonlocal call_count
        call_count += 1
        yield sticky_batch

    class FakeDownloader:
        """Test double used by test_process_download_breaks_on_sticky_cursor."""

        create_download_tasks = AsyncMock()

    class FakeHandler:
        """Test double used by test_process_download_breaks_on_sticky_cursor."""

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = fetch_profile
        fetch_user_post_videos = staticmethod(fake_fetch_posts)
        downloader = FakeDownloader()

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)
    # The production loop sleeps five seconds between pages. Skip that so
    # the test terminates in milliseconds.
    monkeypatch.setattr(users_mod.asyncio, "sleep", AsyncMock())
    monkeypatch.chdir(tmp_path)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="sticky-cursor-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="sticky-cursor-user",
        include_posts=True,
        include_likes=False,
        max_items=50,
    )

    assert (
        call_count == 1
    ), f"Sticky-cursor early-exit failed; loop ran {call_count} iterations"
    refreshed = await service.get_download_status(operation.operation_id)
    # ``has_aweme=True`` advanced ``downloaded_count`` once before the
    # sticky-cursor branch terminated the loop, so the run records as
    # ``partial`` with that single batch reflected in ``completed_items``.
    assert refreshed.status == "partial"
    assert refreshed.completed_items == 1


@pytest.mark.asyncio
async def test_process_download_likes_reports_progress_as_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A likes-only run must not report a bogus 100% progress mid-flight.

    The profile endpoint cannot expose a total number of liked items, so
    the in-loop update must leave ``progress`` as ``None`` rather than
    pegging it to 100% after the first batch. Construct a fake likes
    generator that yields one page (one aweme), then verify the stored
    record still carries ``progress is None`` while ``completed_items``
    reflects the real count.
    """
    from dyvine.services import users as users_mod

    mock_user_data = MagicMock()
    mock_user_data.nickname = "likes-progress-user"
    mock_user_data.aweme_count = 0

    fetch_profile = AsyncMock(return_value=mock_user_data)

    aweme_batch = MagicMock()
    aweme_batch.has_aweme = True
    aweme_batch.aweme_id = ["aw1"]
    aweme_batch.has_more = False
    aweme_batch.max_cursor = 0
    aweme_batch._to_list = MagicMock(return_value=[])

    async def fake_fetch_likes(*_args: Any, **_kwargs: Any) -> Any:
        """Test helper for
        test_process_download_likes_reports_progress_as_indeterminate.
        """
        yield aweme_batch

    class FakeDownloader:
        """Test double used by
        test_process_download_likes_reports_progress_as_indeterminate.
        """

        create_download_tasks = AsyncMock()

    class FakeHandler:
        """Test double used by
        test_process_download_likes_reports_progress_as_indeterminate.
        """

        def __init__(self, kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            pass

        fetch_user_profile = fetch_profile
        fetch_user_like_videos = staticmethod(fake_fetch_likes)
        downloader = FakeDownloader()

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)

    # Redirect the working directory so ``downloads`` is created
    # inside ``tmp_path`` and we never touch the repo tree.
    monkeypatch.chdir(tmp_path)

    service = UserService()
    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="likes-progress-user",
        status="pending",
        message="scheduled",
    )

    progress_samples: list[float | None] = []
    original_update = service.operation_store.update_operation

    async def capture_update(operation_id: str, **fields: Any) -> Any:
        """Test helper for
        test_process_download_likes_reports_progress_as_indeterminate.
        """
        # Only capture in-loop updates (they carry ``completed_items`` but
        # never a ``status`` change; the ``running`` bootstrap update does).
        if "status" not in fields and "completed_items" in fields:
            progress_samples.append(fields.get("progress"))
        return await original_update(operation_id, **fields)

    monkeypatch.setattr(service.operation_store, "update_operation", capture_update)

    await service._process_download(
        operation.operation_id,
        user_id="likes-progress-user",
        include_posts=False,
        include_likes=True,
        max_items=None,
    )

    # No in-loop update should carry a numeric progress value because the
    # total is unknown. The initial ``running`` update also omits progress
    # for the same reason, so the captured list must be empty.
    assert all(sample is None for sample in progress_samples), progress_samples

    refreshed = await service.get_download_status(operation.operation_id)
    assert refreshed.status == "completed"
    assert refreshed.completed_items == 1


@pytest.mark.asyncio
async def test_upload_directory_to_r2_counts_partial_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three files, one upload error: helper must report (2 uploaded, 1 failed).

    The new ``_upload_directory_to_r2`` helper centralises R2 upload
    accounting so ``_finalize_status`` can downgrade to ``partial`` when
    the loop completes with any failed uploads. Verifies the return tuple
    matches the success/failure split and that failed local files stay on
    disk while successful ones are unlinked.
    """
    from dyvine.services import users as users_mod

    user_dir = tmp_path / "nick"
    user_dir.mkdir()
    file_ok_one = user_dir / "video1.mp4"
    file_ok_one.write_bytes(b"a")
    file_failing = user_dir / "video2.mp4"
    file_failing.write_bytes(b"b")
    file_ok_two = user_dir / "image1.jpg"
    file_ok_two.write_bytes(b"c")

    # Force the storage layer to raise on the second file only.
    failing_target = file_failing.resolve()

    async def fake_upload_file(
        local_path: Path,
        _r2_path: str,
        _metadata: dict[str, str],
        _content_type: str,
    ) -> None:
        """Test helper for test_upload_directory_to_r2_counts_partial_failures."""
        if Path(local_path).resolve() == failing_target:
            raise users_mod.ServiceError("simulated upload outage")

    service = UserService()
    service.storage.generate_ugc_path = MagicMock(return_value="r2/path")
    service.storage.generate_metadata = MagicMock(return_value={"category": "posts"})
    monkeypatch.setattr(service.storage, "upload_file", fake_upload_file)

    user_data = MagicMock()
    user_data.nickname = "nick"

    uploaded, failed = await service._upload_directory_to_r2(
        user_dir, user_id="user-123", user_data=user_data
    )

    assert (uploaded, failed) == (2, 1)
    # Successfully uploaded files are unlinked; the failed one remains so
    # the cleanup step can pick it up.
    assert not file_ok_one.exists()
    assert not file_ok_two.exists()
    assert file_failing.exists()


@pytest.mark.asyncio
async def test_upload_directory_to_r2_keeps_files_when_delete_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``delete_after_upload=False`` keeps local copies after a clean upload.

    Local-retention mode still archives to R2 when it is configured, but must
    leave the file on the local volume so a durable copy survives. Verifies
    the helper reports every file as uploaded while none are unlinked.
    """
    user_dir = tmp_path / "nick"
    user_dir.mkdir()
    file_one = user_dir / "video1.mp4"
    file_one.write_bytes(b"a")
    file_two = user_dir / "image1.jpg"
    file_two.write_bytes(b"c")

    uploaded_paths: list[Path] = []

    async def fake_upload_file(
        local_path: Path,
        _r2_path: str,
        _metadata: dict[str, str],
        _content_type: str,
    ) -> None:
        """Record the upload without unlinking the local file."""
        uploaded_paths.append(Path(local_path))

    service = UserService()
    service.storage.generate_ugc_path = MagicMock(return_value="r2/path")
    service.storage.generate_metadata = MagicMock(return_value={"category": "posts"})
    monkeypatch.setattr(service.storage, "upload_file", fake_upload_file)

    user_data = MagicMock()
    user_data.nickname = "nick"

    uploaded, failed = await service._upload_directory_to_r2(
        user_dir,
        user_id="user-123",
        user_data=user_data,
        delete_after_upload=False,
    )

    assert (uploaded, failed) == (2, 0)
    # Retention mode keeps the local copies despite the successful upload.
    assert file_one.exists()
    assert file_two.exists()
    assert len(uploaded_paths) == 2


@pytest.mark.asyncio
async def test_process_download_uploads_only_files_changed_in_current_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retained files from earlier batches must not be re-uploaded."""
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    mock_user_data = MagicMock()
    mock_user_data.nickname = "retain-r2-user"
    mock_user_data.aweme_count = 2

    first_batch = MagicMock()
    first_batch.has_aweme = True
    first_batch.aweme_id = ["aw1"]
    first_batch.has_more = True
    first_batch.max_cursor = 1
    first_batch._to_list = MagicMock(return_value=[])

    second_batch = MagicMock()
    second_batch.has_aweme = True
    second_batch.aweme_id = ["aw2"]
    second_batch.has_more = False
    second_batch.max_cursor = 2
    second_batch._to_list = MagicMock(return_value=[])

    async def fake_fetch_posts(*_args: Any, max_cursor: int, **_kwargs: Any) -> Any:
        """Yield one batch per cursor so the loop performs two uploads."""
        if max_cursor == 0:
            yield first_batch
        elif max_cursor == 1:
            yield second_batch

    class FakeDownloader:
        """Downloader double that leaves prior batch files on disk."""

        def __init__(self) -> None:
            self.calls = 0

        async def create_download_tasks(
            self, _kwargs: dict[str, Any], _items: list[Any], user_dir: Path
        ) -> None:
            """Write one new file per batch into the retained workspace."""
            self.calls += 1
            filename = "first.mp4" if self.calls == 1 else "second.mp4"
            (Path(user_dir) / filename).write_bytes(filename.encode("utf-8"))

    class FakeHandler:
        """Handler double exposing the fake profile, fetch, and downloader."""

        def __init__(self, _kwargs: dict[str, Any]) -> None:
            self.downloader = FakeDownloader()

        fetch_user_profile = AsyncMock(return_value=mock_user_data)
        fetch_user_post_videos = staticmethod(fake_fetch_posts)

    uploaded_names: list[str] = []

    async def fake_upload_file(
        local_path: Path,
        _r2_path: str,
        _metadata: dict[str, str],
        _content_type: str,
    ) -> None:
        """Record the file names selected for upload."""
        uploaded_names.append(Path(local_path).name)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)
    monkeypatch.setattr(users_mod.asyncio, "sleep", AsyncMock())

    service = UserService()
    monkeypatch.setattr(service, "_r2_upload_enabled", lambda: True)
    monkeypatch.setattr(service, "_should_retain_workspace", lambda: True)
    service.storage.generate_ugc_path = MagicMock(return_value="r2/path")
    service.storage.generate_metadata = MagicMock(return_value={"category": "posts"})
    monkeypatch.setattr(service.storage, "upload_file", fake_upload_file)

    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="retain-r2-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="retain-r2-user",
        include_posts=True,
        include_likes=False,
        max_items=None,
    )

    assert uploaded_names == ["first.mp4", "second.mp4"]
    task_dir = tmp_path / users_mod.TEMP_DOWNLOAD_ROOT / operation.operation_id
    assert (task_dir / "retain-r2-user" / "first.mp4").exists()
    assert (task_dir / "retain-r2-user" / "second.mp4").exists()


@pytest.mark.asyncio
async def test_process_download_retains_workspace_when_r2_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With R2 unconfigured the per-task workspace is kept, not swept.

    R2 is the archival target; when it is not configured a finished download
    has nowhere to go, so the ``finally`` branch must retain the local files
    instead of deleting the only copy. Verifies the task directory and its
    downloaded file survive the run.
    """
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    mock_user_data = MagicMock()
    mock_user_data.nickname = "retain-user"
    mock_user_data.aweme_count = 1

    aweme_batch = MagicMock()
    aweme_batch.has_aweme = True
    aweme_batch.aweme_id = ["aw1"]
    aweme_batch.has_more = False
    aweme_batch.max_cursor = 0
    aweme_batch._to_list = MagicMock(return_value=[])

    async def fake_fetch_posts(*_args: Any, **_kwargs: Any) -> Any:
        """Yield a single post batch for the retention test."""
        yield aweme_batch

    class FakeDownloader:
        """Downloader double that writes one file into the workspace."""

        async def create_download_tasks(
            self, _kwargs: dict[str, Any], _items: list[Any], user_dir: Path
        ) -> None:
            """Test helper for FakeDownloader."""
            (Path(user_dir) / "clip.mp4").write_bytes(b"x")

    class FakeHandler:
        """Handler double exposing the fake profile, fetch, and downloader."""

        def __init__(self, _kwargs: dict) -> None:
            """Test helper for FakeHandler."""
            self.downloader = FakeDownloader()

        fetch_user_profile = AsyncMock(return_value=mock_user_data)
        fetch_user_post_videos = staticmethod(fake_fetch_posts)

    monkeypatch.setattr(users_mod, "DouyinHandler", FakeHandler)
    monkeypatch.setattr(users_mod.asyncio, "sleep", AsyncMock())

    service = UserService()
    # Pin the unconfigured-R2 retain path so a developer ``.env`` with real R2
    # credentials cannot flip this test onto the cleanup branch.
    monkeypatch.setattr(service, "_r2_upload_enabled", lambda: False)
    monkeypatch.setattr(service, "_should_retain_workspace", lambda: True)

    operation = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="retain-user",
        status="pending",
        message="scheduled",
    )

    await service._process_download(
        operation.operation_id,
        user_id="retain-user",
        include_posts=True,
        include_likes=False,
        max_items=1,
    )

    workspace_root = (tmp_path / users_mod.TEMP_DOWNLOAD_ROOT).resolve()
    task_dir = workspace_root / operation.operation_id
    assert task_dir.exists(), "retain mode must keep the per-task workspace"
    assert list(task_dir.glob("**/*.mp4")), "the downloaded file must survive"


def test_prune_retained_workspace_evicts_oldest_over_cap(tmp_path: Path) -> None:
    """Pruning removes the oldest task dirs until the tree fits the cap.

    A positive ``retain_max_gb`` must bound the workspace by evicting the
    least-recently-modified directories first, while never touching the
    ``protect`` directory (the task that just finished) even when it is the
    oldest entry.
    """
    from dyvine.services import users as users_mod

    root = tmp_path / "downloads"
    root.mkdir()

    def make_task(name: str, size: int, mtime: float) -> Path:
        """Test helper for test_prune_retained_workspace_evicts_oldest_over_cap."""
        task = root / name
        task.mkdir()
        (task / "blob.bin").write_bytes(b"x" * size)
        os.utime(task, (mtime, mtime))
        return task

    one_mib = 1024 * 1024
    oldest = make_task("oldest", one_mib, mtime=1_000.0)
    newer = make_task("newer", one_mib, mtime=2_000.0)
    newest = make_task("newest", one_mib, mtime=3_000.0)

    # ~3 MiB total against a ~2 MiB cap: one directory must be evicted. The
    # oldest is protected, so the next-oldest (``newer``) is taken instead.
    cap_gb = (2 * one_mib) / 1024**3
    users_mod.UserService._prune_retained_workspace(root, cap_gb, protect=oldest)

    assert oldest.exists(), "the protected current task must never be evicted"
    assert not newer.exists(), "the next-oldest unprotected workspace is evicted"
    assert newest.exists(), "newer workspaces survive once the tree fits the cap"


def test_prune_retained_workspace_noop_when_cap_zero(tmp_path: Path) -> None:
    """A zero cap disables pruning: every retained workspace survives."""
    from dyvine.services import users as users_mod

    root = tmp_path / "downloads"
    root.mkdir()
    task = root / "task-1"
    task.mkdir()
    (task / "blob.bin").write_bytes(b"x" * 4096)

    users_mod.UserService._prune_retained_workspace(root, 0.0)

    assert task.exists(), "pruning must be disabled when the cap is zero"


@pytest.mark.asyncio
async def test_finalize_status_clamps_progress_when_downloaded_exceeds_total(
    tmp_path: Path,
) -> None:
    """``_finalize_status`` must clamp ``progress`` at 100 even when the
    download counter overshoots the profile's ``aweme_count``.

    A run that mixes posts and likes accumulates both into ``downloaded``
    while ``total_posts`` only tracks ``aweme_count``. Combined with an R2
    upload failure (which forces the ``partial`` branch), the percentage
    can climb above 100. The persisted ``progress`` field must remain
    inside the documented 0..100 range so dashboards do not render
    nonsense values.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = await store.create_operation(
        operation_type="user_content_download",
        subject_id="overflow-user",
        status="running",
        message="scheduled",
        progress=0.0,
        metadata={},
    )

    service = UserService(operation_store=store)
    await service._finalize_status(
        task_id=operation.operation_id,
        downloaded=130,
        total_posts=100,
        failed_uploads=1,
        downloading_likes_only=False,
    )

    refreshed = await store.get_operation(operation.operation_id)
    assert refreshed.status == "partial"
    assert refreshed.progress is not None
    assert refreshed.progress <= 100.0
    # ``failed_uploads`` should still surface in the error string so the
    # clamp does not mask the underlying R2 failure.
    assert refreshed.error and "R2 upload failed" in refreshed.error


def test_cleanup_temp_dir_only_removes_target_subdirectory(tmp_path: Path) -> None:
    """Cleanup must isolate per-task workspaces from sibling tasks.

    The helper is invoked from a ``finally`` block so it has to be safe
    against concurrent tasks owning their own siblings under the shared
    ``downloads`` parent. Verifies that wiping one task's directory
    leaves the parent and the other task's workspace untouched.
    """
    from dyvine.services import users as users_mod

    parent = tmp_path / "downloads"
    parent.mkdir()
    target = parent / "task-target"
    target.mkdir()
    (target / "nested").mkdir()
    (target / "nested" / "file.bin").write_bytes(b"x")

    sibling = parent / "task-sibling"
    sibling.mkdir()
    sibling_file = sibling / "keep.bin"
    sibling_file.write_bytes(b"y")

    users_mod.UserService._cleanup_temp_dir(target)

    assert not target.exists()
    assert parent.exists(), "shared parent must survive cleanup"
    assert sibling.exists(), "sibling task workspace must not be touched"
    assert sibling_file.read_bytes() == b"y"


def test_cleanup_temp_dir_logs_failures_without_propagating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup failures must surface in logs but never raise.

    The helper is invoked from a ``finally`` block, so a permission or
    transient FS error during cleanup must not mask the original failure
    by raising. At the same time, silently swallowing the error would
    leave production volumes accumulating orphaned files with no signal
    — so the ``onexc`` callback must record a warning per failed entry.
    """
    import logging
    import types

    from dyvine.services import users as users_mod

    target = tmp_path / "doomed-task"
    target.mkdir()
    (target / "leftover.bin").write_bytes(b"x")

    def fake_rmtree(path: Path, *, onexc: Any) -> None:
        """Test helper for test_cleanup_temp_dir_logs_failures_without_propagating."""
        # Simulate ``shutil.rmtree`` encountering a permission error on a
        # nested file; the helper must invoke ``onexc`` with the offending
        # callable, the path, and the exception itself.
        onexc(Path.unlink, str(target / "leftover.bin"), PermissionError("EACCES"))

    # Replace only the ``shutil`` reference inside ``users_mod`` to avoid
    # mutating the global ``shutil`` module for the duration of the test.
    fake_shutil = types.SimpleNamespace(rmtree=fake_rmtree)
    monkeypatch.setattr(users_mod, "shutil", fake_shutil)

    with caplog.at_level(logging.WARNING, logger="dyvine.services.users"):
        # Must not raise even though the patched ``rmtree`` reports an error.
        users_mod.UserService._cleanup_temp_dir(target)

    matching = [
        record
        for record in caplog.records
        if "workspace cleanup" in record.getMessage()
    ]
    assert matching, "cleanup failure must produce a warning log entry"
    assert matching[0].levelno == logging.WARNING
    assert getattr(matching[0], "path", None) == str(target / "leftover.bin")
    assert getattr(matching[0], "error", None) == "EACCES"


@pytest.mark.asyncio
async def test_concurrent_downloads_use_isolated_temp_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two concurrent ``_process_download`` runs must not share files.

    Schedules two downloads through ``asyncio.gather`` with distinct
    ``user_id`` values and a stub fetcher that records which directory
    f2 was asked to write into. Both invocations must see workspaces
    rooted at ``downloads/<task_id>`` and the cleanup branch must
    leave neither task's files behind.
    """
    from dyvine.services import users as users_mod

    monkeypatch.chdir(tmp_path)

    seen_dirs: list[Path] = []

    aweme_batch = MagicMock()
    aweme_batch.has_aweme = True
    aweme_batch.aweme_id = ["aw1"]
    aweme_batch.has_more = False
    aweme_batch.max_cursor = 0
    aweme_batch._to_list = MagicMock(return_value=[])

    async def fake_fetch_posts(*_args: Any, **_kwargs: Any) -> Any:
        """Test helper for test_concurrent_downloads_use_isolated_temp_directories."""
        yield aweme_batch

    class FakeDownloader:
        """Test double used by
        test_concurrent_downloads_use_isolated_temp_directories.
        """

        async def create_download_tasks(
            self,
            _kwargs: dict[str, Any],
            _items: list[Any],
            user_dir: Path,
        ) -> None:
            """Test helper for FakeDownloader."""
            # Record which workspace we were handed and drop a sentinel
            # file so the test can later assert per-task isolation.
            seen_dirs.append(Path(user_dir).parent)
            (Path(user_dir) / f"file-{Path(user_dir).parent.name}.txt").write_text("x")

    def make_handler(user_data: MagicMock) -> type:
        """Test helper for test_concurrent_downloads_use_isolated_temp_directories."""

        class FakeHandler:
            """Test double used by make_handler."""

            def __init__(self, _kwargs: dict) -> None:
                """Test helper for FakeHandler."""
                self.downloader = FakeDownloader()

            fetch_user_profile = AsyncMock(return_value=user_data)
            fetch_user_post_videos = staticmethod(fake_fetch_posts)

        return FakeHandler

    user_data_a = MagicMock()
    user_data_a.nickname = "alpha"
    user_data_a.aweme_count = 1

    user_data_b = MagicMock()
    user_data_b.nickname = "bravo"
    user_data_b.aweme_count = 1

    handlers = iter([make_handler(user_data_a), make_handler(user_data_b)])

    def handler_factory(kwargs: dict) -> Any:
        """Test helper for test_concurrent_downloads_use_isolated_temp_directories."""
        return next(handlers)(kwargs)

    monkeypatch.setattr(users_mod, "DouyinHandler", handler_factory)
    monkeypatch.setattr(users_mod.asyncio, "sleep", AsyncMock())

    service = UserService()
    # Exercise the R2-archival cleanup branch deterministically: force
    # archival mode (not local retention) so each per-task workspace is
    # removed after the run regardless of whether R2 is configured in the
    # test environment.
    monkeypatch.setattr(service, "_r2_upload_enabled", lambda: True)
    monkeypatch.setattr(service, "_should_retain_workspace", lambda: False)
    # Avoid contacting R2 in the concurrency test; the helper is unit
    # tested directly above.
    monkeypatch.setattr(
        service,
        "_upload_directory_to_r2",
        AsyncMock(return_value=(0, 0)),
    )

    op_a = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="alpha",
        status="pending",
        message="scheduled",
    )
    op_b = await service.operation_store.create_operation(
        operation_type="user_content_download",
        subject_id="bravo",
        status="pending",
        message="scheduled",
    )

    await asyncio.gather(
        service._process_download(
            op_a.operation_id,
            user_id="alpha",
            include_posts=True,
            include_likes=False,
            max_items=1,
        ),
        service._process_download(
            op_b.operation_id,
            user_id="bravo",
            include_posts=True,
            include_likes=False,
            max_items=1,
        ),
    )

    # The downloader was handed two distinct per-task workspaces, each
    # rooted at ``downloads/<task_id>``. ``TEMP_DOWNLOAD_ROOT`` is a
    # relative path, so resolve both sides against the cwd before
    # comparing.
    assert len(seen_dirs) == 2
    assert seen_dirs[0] != seen_dirs[1]
    workspace_root = (tmp_path / users_mod.TEMP_DOWNLOAD_ROOT).resolve()
    for directory in seen_dirs:
        assert directory.resolve().parent == workspace_root
        assert directory.name in {op_a.operation_id, op_b.operation_id}

    # Cleanup ran for both tasks: each per-task workspace is gone, but
    # the shared parent is preserved.
    assert workspace_root.exists()
    assert not (workspace_root / op_a.operation_id).exists()
    assert not (workspace_root / op_b.operation_id).exists()
