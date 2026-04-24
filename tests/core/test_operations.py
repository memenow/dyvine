from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from dyvine.core.exceptions import DownloadError
from dyvine.core.operations import OperationStore


@pytest.mark.asyncio
async def test_operation_store_create_and_get(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="pending",
        message="scheduled",
        progress=0.0,
        metadata={"include_likes": False},
    )

    loaded = await store.get_operation(created.operation_id)
    assert loaded.operation_id == created.operation_id
    assert loaded.operation_type == "user_content_download"
    assert loaded.metadata == {"include_likes": False}


@pytest.mark.asyncio
async def test_operation_store_update_operation(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
    )

    updated = await store.update_operation(
        created.operation_id,
        status="completed",
        message="done",
        progress=100.0,
        completed_items=1,
        download_path="/tmp/file.flv",
    )

    assert updated.status == "completed"
    assert updated.progress == 100.0
    assert updated.completed_items == 1
    assert updated.download_path == "/tmp/file.flv"


@pytest.mark.asyncio
async def test_operation_store_missing_operation_raises(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    with pytest.raises(DownloadError):
        await store.get_operation("missing")


@pytest.mark.asyncio
async def test_operation_store_marks_incomplete_operations_failed(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    pending = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-2",
        status="pending",
        message="scheduled",
    )
    running = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-2",
        status="running",
        message="running",
    )
    completed = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-3",
        status="completed",
        message="done",
    )

    updated = await store.mark_incomplete_operations_failed()

    assert updated == 2
    assert (await store.get_operation(pending.operation_id)).status == "failed"
    assert (await store.get_operation(running.operation_id)).status == "failed"
    assert (await store.get_operation(completed.operation_id)).status == "completed"


@pytest.mark.asyncio
async def test_operation_store_get_latest_operation_for_subject(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    first = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-4",
        status="pending",
        message="scheduled",
    )
    second = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-4",
        status="completed",
        message="done",
    )

    latest = await store.get_latest_operation_for_subject(
        "room-4",
        operation_type="livestream_download",
    )

    assert latest.operation_id == second.operation_id
    assert latest.operation_id != first.operation_id


def test_operation_store_enables_wal(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    # ``sqlite3.connect`` in a ``with`` block only commits/rollbacks — it does
    # not close the connection. Wrap it in ``closing`` so ``-W error`` does
    # not trip the ResourceWarning emitted when the connection is garbage
    # collected.
    with closing(sqlite3.connect(store.db_path)) as connection:
        mode = connection.execute("PRAGMA journal_mode;").fetchone()[0]

    assert mode.lower() == "wal"


def test_operation_store_creates_lookup_index(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    with closing(sqlite3.connect(store.db_path)) as connection:
        row = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_operations_subject_type_updated'
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "idx_operations_subject_type_updated"


@pytest.mark.asyncio
async def test_operation_store_healthcheck_succeeds(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    await store.healthcheck()


@pytest.mark.asyncio
async def test_operation_store_healthcheck_fails_when_path_unwritable(
    tmp_path,
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod-based permission enforcement has no effect as root")

    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    store = OperationStore(str(readonly_dir / "operations.db"))

    # Drop write permissions on both the database file and its parent directory
    # so BEGIN IMMEDIATE cannot acquire a RESERVED lock.
    os.chmod(store.db_path, 0o444)
    os.chmod(readonly_dir, 0o555)

    try:
        with pytest.raises(sqlite3.OperationalError):
            await store.healthcheck()
    finally:
        os.chmod(readonly_dir, 0o755)
        os.chmod(store.db_path, 0o644)


@pytest.mark.asyncio
async def test_operation_store_reads_back_empty_strings_as_empty(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-empty",
        status="pending",
        message="scheduled",
        download_path="",
        error="",
    )

    loaded = await store.get_operation(created.operation_id)
    assert loaded.download_path == ""
    assert loaded.error == ""


@pytest.mark.asyncio
async def test_operation_store_update_runs_in_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Writes must not block the event loop.

    Wrap ``OperationStore._run`` to record every sync helper that gets
    dispatched. Without this contract, per-page progress updates would
    stall the loop during bulk downloads.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="livestream_download",
        subject_id="room-99",
        status="pending",
        message="scheduled",
    )

    calls: list[str] = []
    original_run = store._run

    async def tracking_run(fn, /, *args, **kwargs):
        calls.append(fn.__name__)
        return await original_run(fn, *args, **kwargs)

    monkeypatch.setattr(store, "_run", tracking_run)

    await store.update_operation(
        created.operation_id, status="running", message="running"
    )
    await store.get_operation(created.operation_id)
    await store.get_latest_operation_for_subject(
        "room-99", operation_type="livestream_download"
    )
    await store.healthcheck()
    await store.mark_incomplete_operations_failed()

    assert calls == [
        "_update_operation_sync",
        "_get_operation_sync",
        "_get_latest_operation_for_subject_sync",
        "_healthcheck_sync",
        "_mark_incomplete_operations_failed_sync",
    ]
