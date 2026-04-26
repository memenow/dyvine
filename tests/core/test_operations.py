from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        row = connection.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_operations_subject_type_updated'
            """).fetchone()

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


@pytest.mark.asyncio
async def test_operation_store_latest_missing_raises(tmp_path) -> None:
    """``get_latest_operation_for_subject`` signals absence as ``DownloadError``.

    Exercised directly so the missing-record branch in
    ``_get_latest_operation_for_subject_sync`` is covered without relying
    on integration paths.
    """
    store = OperationStore(str(tmp_path / "operations.db"))

    with pytest.raises(DownloadError, match="unknown-subject"):
        await store.get_latest_operation_for_subject("unknown-subject")


@pytest.mark.asyncio
async def test_operation_store_update_with_no_allowed_fields_returns_current(
    tmp_path,
) -> None:
    """Passing only unknown keys short-circuits to the current record."""
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-noop",
        status="pending",
        message="scheduled",
    )

    # ``subject_id`` is not in the allow-list, so ``updates`` ends up
    # empty and the sync helper returns the existing record unchanged.
    result = await store.update_operation(created.operation_id, subject_id="other")

    assert result.operation_id == created.operation_id
    assert result.status == "pending"
    assert result.subject_id == "user-noop"


def test_operation_store_set_executor_is_late_bindable(tmp_path) -> None:
    """``set_executor`` lets the container swap in its dedicated pool."""
    store = OperationStore(str(tmp_path / "operations.db"))
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        store.set_executor(executor)
        assert store._executor is executor
        store.set_executor(None)
        assert store._executor is None
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_operation_store_concurrent_reads_do_not_serialize(tmp_path) -> None:
    """Reads from multiple threads should run in parallel, not one at a time.

    With the old single-connection-plus-lock design, N concurrent reads
    serialized behind ``self._lock``. With per-thread reader connections,
    the wall-clock time of N simultaneous reads should be close to a
    single slow read, not N * slow read.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-parallel",
        status="pending",
        message="scheduled",
    )

    slow_read_delay = 0.05  # 50 ms
    reader_count = 8

    real_run = store._run

    async def slow_run(fn, /, *args, **kwargs):
        # Simulate a slow sqlite read inside the worker thread so the
        # overlap is observable. The sleep happens on the executor thread,
        # mirroring a real long-running SELECT.
        def slow_wrapper(*a, **kw):
            time.sleep(slow_read_delay)
            return fn(*a, **kw)

        return await real_run(slow_wrapper, *args, **kwargs)

    executor = ThreadPoolExecutor(max_workers=reader_count)
    try:
        store.set_executor(executor)
        # Patch ``_run`` per-task only for the reads below.
        original_run = store._run
        store._run = slow_run  # type: ignore[method-assign]
        try:
            start = time.perf_counter()
            results = await asyncio.gather(
                *(
                    store.get_operation(created.operation_id)
                    for _ in range(reader_count)
                )
            )
            elapsed = time.perf_counter() - start
        finally:
            store._run = original_run  # type: ignore[method-assign]
    finally:
        store.set_executor(None)
        executor.shutdown(wait=True)

    assert len(results) == reader_count
    # Serialized execution would take at least ``reader_count *
    # slow_read_delay`` seconds. A healthy concurrent run finishes in a
    # small multiple of one read. We allow generous slack so the test
    # does not flake on slow CI while still catching accidental
    # serialization (which would take >= 400 ms for 8 readers at 50 ms).
    assert elapsed < slow_read_delay * reader_count / 2


@pytest.mark.asyncio
async def test_operation_store_writes_serialize_cleanly(tmp_path) -> None:
    """Concurrent writes must not corrupt state.

    Two concurrent update calls on the same operation should both commit
    without raising, and the final record must reflect one of the two
    updates (the one that committed last). SQLite's guarantees plus the
    writer lock give us consistency; this test just ensures the new
    writer-connection code path does not break that contract.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-writer",
        status="pending",
        message="scheduled",
        progress=0.0,
    )

    executor = ThreadPoolExecutor(max_workers=4)
    try:
        store.set_executor(executor)

        async def bump(target_progress: float) -> None:
            for _ in range(10):
                await store.update_operation(
                    created.operation_id, progress=target_progress
                )

        await asyncio.gather(bump(25.0), bump(75.0))
    finally:
        store.set_executor(None)
        executor.shutdown(wait=True)

    final = await store.get_operation(created.operation_id)
    assert final.progress in {25.0, 75.0}


def test_operation_store_shutdown_closes_reader_connections(tmp_path) -> None:
    """``shutdown`` must close every per-thread reader connection."""
    store = OperationStore(str(tmp_path / "operations.db"))
    errors: list[BaseException] = []
    connections: list[sqlite3.Connection] = []

    def open_reader() -> None:
        try:
            conn = store._reader_connection()
            conn.execute("SELECT 1").fetchone()
            connections.append(conn)
        except BaseException as exc:  # pragma: no cover - captured for debugging
            errors.append(exc)

    threads = [threading.Thread(target=open_reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(connections) == 4
    assert len(store._reader_connections) == 4

    store.shutdown()

    assert store._reader_connections == {}
    assert store._writer_slot.connection is None
    # After shutdown, each reader connection must be closed. Using the
    # handle should raise ``ProgrammingError`` -- ``sqlite3`` reports
    # closed connections that way.
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1").fetchone()


@pytest.mark.asyncio
async def test_operation_store_shutdown_closes_writer_connection(tmp_path) -> None:
    """After a write the writer slot is populated, and shutdown clears it."""
    store = OperationStore(str(tmp_path / "operations.db"))
    await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-shutdown",
        status="pending",
        message="scheduled",
    )
    # The write above lazy-opens the writer connection.
    writer = store._writer_slot.connection
    assert writer is not None

    store.shutdown()

    assert store._writer_slot.connection is None
    with pytest.raises(sqlite3.ProgrammingError):
        writer.execute("SELECT 1").fetchone()


def test_operation_store_shutdown_is_idempotent(tmp_path) -> None:
    """Calling ``shutdown`` twice must not raise."""
    store = OperationStore(str(tmp_path / "operations.db"))
    store._reader_connection()  # ensure at least one reader exists
    store.shutdown()
    store.shutdown()


@pytest.mark.asyncio
async def test_operation_store_reader_sees_writer_commits(tmp_path) -> None:
    """The per-thread reader must see rows the writer already committed.

    This guards against a WAL visibility regression: if readers ran in an
    implicit transaction they would keep returning the old snapshot even
    after the writer commits.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-visibility",
        status="pending",
        message="scheduled",
        progress=0.0,
    )

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        store.set_executor(executor)
        await store.update_operation(created.operation_id, progress=42.0)
        refreshed = await store.get_operation(created.operation_id)
        assert refreshed.progress == 42.0
    finally:
        store.set_executor(None)
        executor.shutdown(wait=True)


def test_operation_record_to_response_includes_expected_fields() -> None:
    """``OperationRecord.to_response`` exposes the public API shape."""
    from dyvine.core.operations import OperationRecord

    record = OperationRecord(
        operation_id="op-1",
        operation_type="user_content_download",
        subject_id="user-1",
        status="pending",
        message="scheduled",
        progress=10.0,
        total_items=5,
        completed_items=1,
        download_path="/tmp/f",
        error=None,
        metadata={"k": "v"},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )

    payload = record.to_response()

    assert payload["operation_id"] == "op-1"
    assert payload["task_id"] == "op-1"
    assert payload["downloaded_items"] == 1
    assert payload["status"] == "pending"


def test_operation_store_shutdown_swallows_sqlite_errors_on_close(tmp_path) -> None:
    """Failing close calls during shutdown must not bubble up."""

    class FailingConnection:
        def close(self) -> None:
            raise sqlite3.Error("already closed")

    store = OperationStore(str(tmp_path / "operations.db"))
    # Inject a fake reader that raises on close so we hit the except branch.
    fake_reader = FailingConnection()
    with store._reader_lock:
        store._reader_connections[99] = fake_reader  # type: ignore[assignment]
    # And a fake writer connection that also raises on close.
    store._writer_slot.connection = FailingConnection()  # type: ignore[assignment]

    # Must not raise.
    store.shutdown()
    assert store._reader_connections == {}
    assert store._writer_slot.connection is None


def test_operation_store_healthcheck_swallows_rollback_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``rollback`` raises, the healthcheck still finishes cleanly."""
    store = OperationStore(str(tmp_path / "operations.db"))

    real_connect = store._connect

    class RollbackFailingConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # First execute fails to force the finally branch.
            raise sqlite3.OperationalError("boom")

        def rollback(self) -> None:
            raise sqlite3.Error("rollback failed")

        def close(self) -> None:
            self._real.close()

    def fake_connect(**kwargs):  # type: ignore[no-untyped-def]
        return RollbackFailingConnection(real_connect(**kwargs))

    monkeypatch.setattr(store, "_connect", fake_connect)

    with pytest.raises(sqlite3.OperationalError, match="boom"):
        store._healthcheck_sync()


def test_operation_store_is_closed_property_reflects_shutdown(tmp_path) -> None:
    """``is_closed`` lets callers probe the store without provoking ``_run``."""
    store = OperationStore(str(tmp_path / "operations.db"))
    assert store.is_closed is False

    store.shutdown()
    assert store.is_closed is True

    # Idempotent shutdown keeps the flag set.
    store.shutdown()
    assert store.is_closed is True


@pytest.mark.asyncio
async def test_operation_store_async_calls_after_shutdown_raise(tmp_path) -> None:
    """Every async dispatch must reject calls once ``shutdown`` has run.

    The container drains background tasks before calling ``shutdown``, so
    any post-shutdown coroutine call is a programming error. Surfacing a
    ``RuntimeError`` makes that error loud rather than returning a closed
    sqlite handle that would later raise ``sqlite3.ProgrammingError`` deep
    inside the executor pool.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    created = await store.create_operation(
        operation_type="user_content_download",
        subject_id="user-shutdown-guard",
        status="pending",
        message="scheduled",
    )

    store.shutdown()

    with pytest.raises(RuntimeError, match="shut down"):
        await store.healthcheck()
    with pytest.raises(RuntimeError, match="shut down"):
        await store.get_operation(created.operation_id)
    with pytest.raises(RuntimeError, match="shut down"):
        await store.get_latest_operation_for_subject("user-shutdown-guard")
    with pytest.raises(RuntimeError, match="shut down"):
        await store.update_operation(created.operation_id, status="failed")
    with pytest.raises(RuntimeError, match="shut down"):
        await store.create_operation(
            operation_type="user_content_download",
            subject_id="another",
            status="pending",
            message="m",
        )
    with pytest.raises(RuntimeError, match="shut down"):
        await store.mark_incomplete_operations_failed()


def test_operation_store_finalizer_closes_connections_on_gc(tmp_path) -> None:
    """GC'ing the store without calling shutdown must still close handles."""
    import gc

    store = OperationStore(str(tmp_path / "operations.db"))
    reader = store._reader_connection()
    assert reader is not None

    # Snapshot the mutable containers so we can inspect them after the
    # store itself is garbage collected.
    readers = store._reader_connections
    writer_slot = store._writer_slot

    del store
    gc.collect()

    assert readers == {}
    assert writer_slot.connection is None
    with pytest.raises(sqlite3.ProgrammingError):
        reader.execute("SELECT 1").fetchone()
