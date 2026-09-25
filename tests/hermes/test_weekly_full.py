"""Full-feed weekly download checkpoints and recovery."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dyvine.schemas.users import AuthorState
from dyvine.services.queue import QueueService
from dyvine_hermes.weekly import WeeklyConfig, run_once
from tests.fake_repos import FakeOperationRepository, FakeQueueRepository


def _config(root: Path) -> WeeklyConfig:
    return WeeklyConfig(
        timezone="Asia/Shanghai",
        owner_open_id="ou_recipient",
        download_root=root,
        first_auto_date=date(2026, 9, 27),
        cutover_round="weekly0913",
    )


def _engine(repo: FakeQueueRepository) -> SimpleNamespace:
    return SimpleNamespace(
        queue=QueueService(queue=repo, seeds=AsyncMock(), rounds=AsyncMock()),
        delivery_ledger=SimpleNamespace(list_files=AsyncMock(return_value=[])),
        users=SimpleNamespace(
            get_author_state=AsyncMock(
                return_value=AuthorState(available=True, aweme_count=1)
            )
        ),
        posts=SimpleNamespace(
            download_bulk_inline=AsyncMock(), download_new_posts=AsyncMock()
        ),
        operations=FakeOperationRepository(),
    )


async def test_full_mode_persists_operation_before_awaited_download(
    tmp_path: Path,
) -> None:
    (tmp_path / "Account").mkdir()
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="full",
        status="pending",
    )
    engine = _engine(repo)
    engine.operations = FakeOperationRepository()

    async def complete(
        sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        saved = await repo.get_entry("weekly0913:sec_1")
        assert saved.operation_id == operation_id
        assert saved.status == "downloading"
        assert (sec_user_id, max_cursor, mode) == ("sec_1", 0, "post")
        await engine.operations.update_operation(
            operation_id,
            status="completed",
            message="Full download complete",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": None, "failed_count": 0},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=complete)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    engine.posts.download_bulk_inline.assert_awaited_once()
    engine.posts.download_new_posts.assert_not_called()


async def test_full_mode_resumes_after_a_persisted_complete_page(
    tmp_path: Path,
) -> None:
    earlier_post = tmp_path / "Account" / "2026-09-13 09-00-00 post"
    earlier_post.mkdir(parents=True)
    (earlier_post / "clip.mp4").write_bytes(b"previous page")
    store = FakeOperationRepository()
    prior = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="sec_1",
        status="partial",
        message="Page cap reached",
        metadata={"resume_cursor": 123, "failed_count": 0},
        download_path="Account",
    )
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="full",
        status="pending",
        operation_id=prior.operation_id,
        cutoff="2026-09-14T08:00:00",
    )
    engine = _engine(repo)
    engine.operations = store

    async def complete(
        sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        assert max_cursor == 123
        await store.update_operation(
            operation_id,
            status="completed",
            message="Full download complete",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": None, "failed_count": 0},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=complete)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    assert engine.posts.download_bulk_inline.await_args.kwargs["max_cursor"] == 123


@pytest.mark.parametrize(
    ("legacy_checkpoint", "old_cursor"),
    [(False, None), (False, 123), (True, None)],
)
async def test_empty_legacy_directory_forces_fresh_full_download(
    tmp_path: Path, legacy_checkpoint: bool, old_cursor: int | None
) -> None:
    """An old 0/0 operation or checkpoint cannot silently close pending work."""
    (tmp_path / "Account").mkdir()
    store = FakeOperationRepository()
    old_operation_id = None
    extra = None
    if legacy_checkpoint:
        extra = {
            "weekly": {"download_complete": True, "user_dir": str(tmp_path / "Account")}
        }
    else:
        old = await store.create_operation(
            operation_type="user_posts_bulk_download",
            subject_id="sec_1",
            status="completed",
            message="Old 0/0 operation",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": old_cursor, "failed_count": 0},
        )
        old_operation_id = old.operation_id
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="full",
        status="pending",
        operation_id=old_operation_id,
        extra=extra,
    )
    engine = _engine(repo)
    engine.operations = store

    async def complete(
        sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        assert operation_id != old_operation_id
        assert max_cursor == 0
        await store.update_operation(
            operation_id,
            status="completed",
            message="Fresh full download complete",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": None, "failed_count": 0},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=complete)
    outcome = await run_once(
        engine=engine, config=_config(tmp_path), round_name="weekly0913"
    )
    assert outcome.status == "completed"
    engine.posts.download_bulk_inline.assert_awaited_once()
    saved = await repo.get_entry("weekly0913:sec_1")
    assert saved.operation_id != old_operation_id
    assert saved.extra["weekly"]["fresh_download_confirmed"] is True


async def test_group_attested_release_rechecks_full_from_zero_once_and_skips_history(
    tmp_path: Path,
) -> None:
    """Existing media and a completed old operation cannot bypass attestation."""
    user_dir = tmp_path / "Account"
    post_dir = user_dir / "2026-09-13 09-00-00 post"
    post_dir.mkdir(parents=True)
    video = post_dir / "clip.mp4"
    video.write_bytes(b"previously sent")
    store = FakeOperationRepository()
    old = await store.create_operation(
        operation_type="user_posts_bulk_download",
        subject_id="sec_1",
        status="completed",
        message="Old completed download",
        download_path="Account",
        metadata={"resume_cursor": 123, "failed_count": 0},
    )
    repo = FakeQueueRepository()
    await repo.upsert_entry(
        key="weekly0913:sec_1",
        round="weekly0913",
        nickname="Account",
        sec_user_id="sec_1",
        mode="full",
        status="pending",
        operation_id=old.operation_id,
        extra={
            "weekly": {"download_complete": True, "user_dir": str(user_dir)},
            "reconciliation": {"action": "release_pending_group_attested"},
        },
    )
    engine = _engine(repo)
    engine.operations = store
    historical = SimpleNamespace(
        relative_path=video.relative_to(user_dir).as_posix(),
        status="legacy_confirmed_sent",
    )

    async def list_files(**kwargs: object) -> list[SimpleNamespace]:
        if kwargs.get("offset"):
            return []
        return [historical] if kwargs.get("status") == "legacy_confirmed_sent" else []

    engine.delivery_ledger.list_files = AsyncMock(side_effect=list_files)

    async def complete(
        sec_user_id: str, *, operation_id: str, max_cursor: int, mode: str
    ) -> None:
        assert (sec_user_id, max_cursor, mode) == ("sec_1", 0, "post")
        assert operation_id != old.operation_id
        await store.update_operation(
            operation_id,
            status="completed",
            message="Fresh full download complete",
            completed_items=0,
            total_items=0,
            download_path="Account",
            metadata={"resume_cursor": None, "failed_count": 0},
        )

    engine.posts.download_bulk_inline = AsyncMock(side_effect=complete)
    channel = SimpleNamespace(ensure_group=AsyncMock(), deliver_file=AsyncMock())
    first = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert first.status == "completed"
    saved = await repo.get_entry("weekly0913:sec_1")
    assert saved.extra["weekly"]["fresh_download_confirmed"] is True
    assert saved.extra["reconciliation"]["action"] == "release_pending_group_attested"
    channel.deliver_file.assert_not_called()
    await repo.update_entry("weekly0913:sec_1", status="pending")
    second = await run_once(
        engine=engine,
        config=_config(tmp_path),
        round_name="weekly0913",
        channel=channel,
    )
    assert second.status == "completed"
    engine.posts.download_bulk_inline.assert_awaited_once()
    channel.deliver_file.assert_not_called()
