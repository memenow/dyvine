"""Tests for service container dependency wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import fake_repos
import pytest
from fake_repos import (
    FakeOperationRepository,
    FakeOperationState,
    FakeWatchRepository,
)
from fastapi import HTTPException

from dyvine.core import dependencies


class DummyHandler:
    """Test double used by this test."""

    def __init__(self, kwargs: dict[str, object]) -> None:
        """Test helper for DummyHandler."""
        self.kwargs = kwargs


def _fake_stores() -> tuple[FakeOperationRepository, FakeWatchRepository]:
    """Return an operation/watch fake pair for container injection."""
    return FakeOperationRepository(), FakeWatchRepository()


@pytest.fixture(autouse=True)
def reset_container_cache() -> None:
    """Test helper for this module."""
    dependencies.get_service_container.cache_clear()
    yield
    dependencies.get_service_container.cache_clear()


def _stub_douyin_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the f2 handler with a construction double."""
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )


@pytest.mark.asyncio
async def test_service_container_initializes_with_douyin_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Verify service container initializes with douyin handler."""
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(dependencies.settings.douyin, "download_root", str(tmp_path))

    def build_handler(kwargs: dict[str, object]) -> DummyHandler:
        """Test helper for test_service_container_initializes_with_douyin_handler."""
        captured_kwargs.update(kwargs)
        return DummyHandler(kwargs)

    monkeypatch.setattr(dependencies, "DouyinHandler", build_handler)

    operation_store, watch_store = _fake_stores()
    container = dependencies.ServiceContainer()
    await container.initialize(operation_store=operation_store, watch_store=watch_store)
    try:
        handler = container.douyin_handler
        assert isinstance(handler, DummyHandler)
        assert captured_kwargs.get("mode") == "all"
        assert captured_kwargs.get("interval") == "all"
        assert captured_kwargs.get("path") == str(tmp_path)
        assert captured_kwargs.get("max_tasks") == 3
    finally:
        await container.shutdown()


def test_get_service_container_returns_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get service container returns singleton."""
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    container_one = dependencies.get_service_container()
    container_two = dependencies.get_service_container()

    assert container_one is container_two


def test_require_api_key_allows_missing_header_when_gate_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the dependency is a no-op behind another auth layer."""
    monkeypatch.setattr(dependencies.settings.security, "require_api_key", False)
    monkeypatch.setattr(dependencies.settings.security, "api_key", "expected")

    assert dependencies.require_api_key(None) is None


def test_require_api_key_rejects_missing_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify missing API keys are rejected when the gate is enabled."""
    monkeypatch.setattr(dependencies.settings.security, "require_api_key", True)
    monkeypatch.setattr(dependencies.settings.security, "api_key", "expected")

    with pytest.raises(HTTPException) as exc_info:
        dependencies.require_api_key(None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing API key"
    assert exc_info.value.headers == {"WWW-Authenticate": "ApiKey"}


def test_require_api_key_rejects_wrong_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify non-matching API keys are rejected."""
    monkeypatch.setattr(dependencies.settings.security, "require_api_key", True)
    monkeypatch.setattr(dependencies.settings.security, "api_key", "expected")

    with pytest.raises(HTTPException) as exc_info:
        dependencies.require_api_key("wrong")

    assert exc_info.value.status_code == 401


def test_require_api_key_accepts_matching_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify matching API keys pass the dependency."""
    monkeypatch.setattr(dependencies.settings.security, "require_api_key", True)
    monkeypatch.setattr(dependencies.settings.security, "api_key", "expected")

    assert dependencies.require_api_key("expected") is None


@pytest.mark.asyncio
async def test_service_container_sweeps_orphans_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot fails rows orphaned by dead replicas, nothing else."""
    _stub_douyin_handler(monkeypatch)
    state = FakeOperationState()
    dead_owner = FakeOperationRepository(owner_id="dead-replica", state=state)

    old = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
    monkeypatch.setattr(fake_repos, "_now_iso", lambda: old)
    orphan = await dead_owner.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="running",
        message="running",
    )
    monkeypatch.undo()

    operation_store = FakeOperationRepository(owner_id="boot-owner", state=state)
    container = dependencies.ServiceContainer()
    await container.initialize(
        operation_store=operation_store,
        watch_store=FakeWatchRepository(),
    )
    try:
        refreshed = await container.operation_store.get_operation(orphan.operation_id)
        assert refreshed.status == "failed"
        assert "stopped heartbeating" in refreshed.message
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_service_container_rejects_mixed_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing exactly one repository override is a loud error."""
    _stub_douyin_handler(monkeypatch)
    container = dependencies.ServiceContainer()
    with pytest.raises(ValueError, match="both .* or neither"):
        await container.initialize(operation_store=FakeOperationRepository())


@pytest.mark.asyncio
async def test_service_container_boot_failure_unwinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable database fails boot without leaking threads."""
    _stub_douyin_handler(monkeypatch)
    monkeypatch.setattr(
        dependencies.settings.database,
        "url",
        "postgresql+asyncpg://u:p@127.0.0.1:1/db",
    )
    container = dependencies.ServiceContainer()
    # asyncpg surfaces a refused connection as a raw ``OSError`` before
    # SQLAlchemy can wrap it; either way boot must fail loudly.
    with pytest.raises(OSError):
        await container.initialize()

    assert container._initialized is False
    assert container._r2_executor is None
    assert container._r2_head_executor is None
    assert container._audit_executor is None
    assert container._db is None
    assert container._janitor_task is None


def test_service_container_requires_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accessing a service before awaiting ``initialize`` must fail loudly.

    ``initialize`` is a coroutine because boot recovery awaits database
    IO. Synchronous property access has no safe way to bootstrap, so
    the container should raise rather than block the caller.

    """
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    container = dependencies.ServiceContainer()
    with pytest.raises(RuntimeError, match="ServiceContainer has not been initialized"):
        _ = container.douyin_handler


@pytest.mark.asyncio
async def test_service_container_exposes_post_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container must expose the bulk-download-aware ``PostService``.

    The post service shares the operation store with the user and
    livestream services so the lifespan can drain in-flight bulk
    downloads alongside the other long-running workflows. The
    behavioural check below — writing through the post service's store
    and reading the same row through the container — replaces the prior
    ``is`` assertions on private ``_task_registry`` / ``_background_tasks``
    attributes that broke on every internal rename.
    """
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    operation_store, watch_store = _fake_stores()
    container = dependencies.ServiceContainer()
    await container.initialize(operation_store=operation_store, watch_store=watch_store)
    try:
        post_service = container.post_service
        assert isinstance(post_service, dependencies.PostService)

        operation = await post_service.operation_store.create_operation(
            operation_type="user_posts_bulk_download",
            subject_id="user-shared-store",
            status="pending",
            message="scheduled",
        )
        fetched = await container.operation_store.get_operation(operation.operation_id)
        assert fetched.operation_id == operation.operation_id
        assert dependencies.get_post_service.__name__ == "get_post_service"
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_service_container_survives_watch_resume_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watch resume failure must not abort startup (watch is non-critical)."""
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )
    monkeypatch.setattr(
        dependencies.WatchService,
        "resume_persisted",
        AsyncMock(side_effect=RuntimeError("resume boom")),
    )

    operation_store, watch_store = _fake_stores()
    container = dependencies.ServiceContainer()
    await container.initialize(
        operation_store=operation_store, watch_store=watch_store
    )  # must not raise despite the resume failure
    try:
        # The container still came up; non-watch services remain usable.
        assert container._initialized is True
        assert isinstance(container.post_service, dependencies.PostService)
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_service_container_survives_watch_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watch-service construction failure leaves the rest intact."""
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    class _BoomWatchService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("watch build boom")

    monkeypatch.setattr(dependencies, "WatchService", _BoomWatchService)

    operation_store, watch_store = _fake_stores()
    container = dependencies.ServiceContainer()
    await container.initialize(
        operation_store=operation_store, watch_store=watch_store
    )  # must not raise or leak the executors above
    try:
        assert container._initialized is True
        assert isinstance(container.post_service, dependencies.PostService)
        assert "watch_service" not in container._services
    finally:
        await container.shutdown()  # clean teardown even without watch
