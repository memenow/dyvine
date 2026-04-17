from __future__ import annotations

import pytest

from dyvine.core.exceptions import DownloadError
from dyvine.core.operations import OperationStore


def test_operation_store_create_and_get(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))

    created = store.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="pending",
        message="scheduled",
        progress=0.0,
        metadata={"include_likes": False},
    )

    loaded = store.get_operation(created.operation_id)
    assert loaded.operation_id == created.operation_id
    assert loaded.operation_type == "user_content_download"
    assert loaded.metadata == {"include_likes": False}


def test_operation_store_update_operation(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    created = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
    )

    updated = store.update_operation(
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


def test_operation_store_missing_operation_raises(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    with pytest.raises(DownloadError):
        store.get_operation("missing")


def test_operation_store_marks_incomplete_operations_failed(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    pending = store.create_operation(
        operation_type="user_content_download",
        subject_id="user-2",
        status="pending",
        message="scheduled",
    )
    running = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-2",
        status="running",
        message="running",
    )
    completed = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-3",
        status="completed",
        message="done",
    )

    updated = store.mark_incomplete_operations_failed()

    assert updated == 2
    assert store.get_operation(pending.operation_id).status == "failed"
    assert store.get_operation(running.operation_id).status == "failed"
    assert store.get_operation(completed.operation_id).status == "completed"
