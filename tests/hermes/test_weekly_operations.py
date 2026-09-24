"""Operation checkpoints and recovery for the weekly runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.services.posts import IncrementalDownloadResult
from dyvine.services.queue import QueueService
from dyvine_hermes.weekly import WeeklyConfig, run_once
from tests.fake_repos import FakeOperationRepository, FakeQueueRepository


def _config(root: Path) -> WeeklyConfig:
    return WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=root,
        first_auto_date=date(2026, 9, 27),
    )


def _engine(repo: FakeQueueRepository) -> SimpleNamespace:
    return SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock()),
        delivery_ledger=SimpleNamespace(
            get_group=AsyncMock(return_value=None),
            list_files=AsyncMock(return_value=[]),
        ),
        posts=SimpleNamespace(download_new_posts=AsyncMock()),
        operations=FakeOperationRepository(),
    )


async def _entry(repo: FakeQueueRepository) -> None:
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        extra={},
    )


async def test_empty_cutover_directory_ignores_old_incremental_zero_result(
    tmp_path: Path,
) -> None:
    """A stale 0/0 result cannot close an account without a fresh feed check."""
    (tmp_path / "Account").mkdir()
    repo = FakeQueueRepository()
    engine = _engine(repo)
    previous = await engine.operations.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id="sec_1",
        status="completed",
        message="No new posts found",
        download_path="Account",
        metadata={"new_count": 0, "failed_count": 0},
    )
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        operation_id=previous.operation_id,
        extra={"since_aweme_id": "old-anchor"},
    )

    async def complete(
        sec_user_id: str,
        *,
        since_aweme_id: str | None,
        operation_id: str,
        posted_after: datetime | None = None,
    ) -> IncrementalDownloadResult:
        assert operation_id != previous.operation_id
        assert since_aweme_id is None
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            message="Fresh check found no posts",
            download_path="Account",
            metadata={"new_count": 0, "failed_count": 0},
        )
        return IncrementalDownloadResult(
            operation_id=operation_id,
            new_count=0,
            newest_aweme_id=None,
            seen_aweme_ids=[],
            failed_count=0,
        )

    engine.posts.download_new_posts = AsyncMock(side_effect=complete)
    config = replace(_config(tmp_path), cutover_round="weekly0913")
    outcome = await run_once(engine=engine, config=config, round_name="weekly0913")
    assert outcome.status == "completed"
    engine.posts.download_new_posts.assert_awaited_once()
    saved = await repo.get_entry("weekly0913:sec_1")
    assert saved.operation_id != previous.operation_id
    assert saved.extra["weekly"]["fresh_download_confirmed"] is True


@pytest.mark.parametrize(
    "action", ["release_pending_group_attested", "release_pending_window_attested"]
)
async def test_attested_incremental_rechecks_nonempty_dir_without_old_anchor(
    tmp_path: Path, action: str
) -> None:
    """An attested release ignores old media and a stale completed checkpoint."""
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"previously sent")
    repo = FakeQueueRepository()
    engine = _engine(repo)
    old = await engine.operations.create_operation(
        operation_type="user_posts_incremental_download",
        subject_id="sec_1",
        status="completed",
        message="Old check found nothing",
        download_path="Account",
        metadata={"new_count": 0, "failed_count": 0},
    )
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        operation_id=old.operation_id,
        extra={
            "since_aweme_id": "old-anchor",
            "weekly": {"download_complete": True, "user_dir": str(user_dir)},
            "reconciliation": {"action": action},
        },
    )
    historical = SimpleNamespace(
        relative_path=video.relative_to(user_dir).as_posix(),
        status="legacy_confirmed_sent",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        return [historical] if kwargs.get("status") == "legacy_confirmed_sent" else []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)

    async def complete(
        sec_user_id: str,
        *,
        since_aweme_id: str | None,
        operation_id: str,
        posted_after: datetime | None = None,
    ) -> IncrementalDownloadResult:
        assert sec_user_id == "sec_1"
        assert since_aweme_id is None
        assert operation_id != old.operation_id
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            message="Fresh check found no posts",
            download_path="Account",
            metadata={"new_count": 0, "failed_count": 0},
        )
        return IncrementalDownloadResult(
            operation_id=operation_id,
            new_count=0,
            newest_aweme_id=None,
            seen_aweme_ids=[],
            failed_count=0,
        )

    engine.posts.download_new_posts = AsyncMock(side_effect=complete)
    channel = SimpleNamespace(ensure_group=AsyncMock(), deliver_file=AsyncMock())
    config = replace(_config(tmp_path), cutover_round="weekly0913")
    first = await run_once(
        engine=engine, config=config, round_name="weekly0913", channel=channel
    )
    assert first.status == "completed"
    assert (await repo.get_entry("weekly0913:sec_1")).extra["weekly"][
        "fresh_download_confirmed"
    ]
    channel.deliver_file.assert_not_called()
    await repo.update_entry("weekly0913:sec_1", status="pending")
    second = await run_once(
        engine=engine, config=config, round_name="weekly0913", channel=channel
    )
    assert second.status == "completed"
    engine.posts.download_new_posts.assert_awaited_once()


async def test_full_post_mode_is_not_misreported_as_incremental(tmp_path: Path) -> None:
    tmp_path.joinpath("Account").mkdir()
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="post",
        status="pending",
    )
    engine = _engine(repo)

    async def finish_full(
        _sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        assert max_cursor == 0
        assert mode == "post"
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            completed_items=0,
            download_path="Account",
            metadata={"failed_count": 0, "resume_cursor": None},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=finish_full)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    engine.posts.download_bulk_inline.assert_awaited_once()
    engine.posts.download_new_posts.assert_not_called()


async def test_full_post_resumes_from_saved_page(tmp_path: Path) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    engine = _engine(repo)
    previous = await engine.operations.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="sec_1",
        status="partial",
        message="Bulk download completed with missing items",
        download_path="Account",
        metadata={"resume_cursor": 2800, "failed_count": 0},
    )
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="post",
        status="pending",
        operation_id=previous.operation_id,
    )

    async def finish_full(
        _sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        assert max_cursor == 2800
        assert mode == "post"
        saved = await repo.get_entry("weekly0913:sec_1")
        assert saved.operation_id == operation_id
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            completed_items=0,
            download_path="Account",
            metadata={"failed_count": 0, "resume_cursor": None},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=finish_full)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    assert (
        await repo.get_entry("weekly0913:sec_1")
    ).operation_id != previous.operation_id


async def test_full_post_stalled_cursor_requires_review(tmp_path: Path) -> None:
    repo = FakeQueueRepository()
    engine = _engine(repo)
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="post",
        status="pending",
    )

    async def stall(
        _sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        await engine.operations.update_operation(
            operation_id,
            status="partial",
            completed_items=1,
            download_path="Account",
            metadata={"failed_count": 0, "resume_cursor": None, "cursor_stalled": True},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=stall)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "op_issue"
    assert (await repo.get_entry("weekly0913:sec_1")).status == "op_issue"


async def test_incremental_operation_is_persisted_before_download(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Account"
    user_dir.mkdir()
    repo = FakeQueueRepository()
    await _entry(repo)
    engine = _engine(repo)

    async def finish_incremental(
        _sec_user_id: str,
        *,
        since_aweme_id: str | None,
        operation_id: str,
        posted_after: datetime | None = None,
    ) -> IncrementalDownloadResult:
        saved = await repo.get_entry("weekly0913:sec_1")
        assert saved.operation_id == operation_id
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            download_path="Account",
            metadata={"new_count": 0, "failed_count": 0, "truncated": False},
        )
        return IncrementalDownloadResult(operation_id, 0, None, [], 0)

    engine.posts.download_new_posts = AsyncMock(side_effect=finish_incremental)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    engine.posts.download_new_posts.assert_awaited_once()


async def test_incremental_download_is_bounded_by_the_queue_cutoff(
    tmp_path: Path,
) -> None:
    """The runner hands delivery's cutoff to the download, in its timezone."""
    (tmp_path / "Account").mkdir()
    repo = FakeQueueRepository()
    engine = _engine(repo)
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="incremental",
        status="pending",
        cutoff="2026-09-06T00:00:00Z",
        extra={},
    )
    seen: dict[str, object] = {}

    async def complete(
        sec_user_id: str,
        *,
        since_aweme_id: str | None,
        operation_id: str,
        posted_after: datetime | None = None,
    ) -> IncrementalDownloadResult:
        seen["posted_after"] = posted_after
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            message="No new posts found",
            download_path="Account",
            metadata={"new_count": 0, "failed_count": 0},
        )
        return IncrementalDownloadResult(
            operation_id=operation_id,
            new_count=0,
            newest_aweme_id=None,
            seen_aweme_ids=[],
            failed_count=0,
        )

    engine.posts.download_new_posts = AsyncMock(side_effect=complete)
    await run_once(engine=engine, config=_config(tmp_path), round_name="weekly0913")
    assert seen["posted_after"] == datetime(2026, 9, 6, 8, 0)
