from __future__ import annotations

import pytest

from dyvine.core import dependencies


class DummyHandler:
    def __init__(self, kwargs: dict[str, object]) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def reset_container_cache() -> None:
    dependencies.get_service_container.cache_clear()
    yield
    dependencies.get_service_container.cache_clear()


@pytest.mark.asyncio
async def test_service_container_initializes_with_douyin_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}

    def build_handler(kwargs: dict[str, object]) -> DummyHandler:
        captured_kwargs.update(kwargs)
        return DummyHandler(kwargs)

    monkeypatch.setattr(dependencies, "DouyinHandler", build_handler)

    container = dependencies.ServiceContainer()
    await container.initialize()

    handler = container.douyin_handler
    assert isinstance(handler, DummyHandler)
    assert captured_kwargs.get("mode") == "all"
    assert captured_kwargs.get("max_tasks") == 3


def test_get_service_container_returns_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    container_one = dependencies.get_service_container()
    container_two = dependencies.get_service_container()

    assert container_one is container_two


@pytest.mark.asyncio
async def test_service_container_reconciles_incomplete_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )
    monkeypatch.setattr(
        dependencies.settings.api,
        "operation_db_path",
        str(tmp_path / "operations.db"),
    )

    preexisting = dependencies.OperationStore(str(tmp_path / "operations.db"))
    operation = await preexisting.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="running",
        message="running",
    )

    container = dependencies.ServiceContainer()
    await container.initialize()

    refreshed = await container.operation_store.get_operation(operation.operation_id)
    assert refreshed.status == "failed"
    assert refreshed.error == "Operation interrupted during process restart"


def test_service_container_requires_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accessing a service before awaiting ``initialize`` must fail loudly.

    ``initialize`` is a coroutine because it awaits sqlite recovery via
    ``asyncio.to_thread``. Synchronous property access has no safe way to
    bootstrap, so the container should raise rather than block the caller.
    """
    monkeypatch.setattr(
        dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs)
    )

    container = dependencies.ServiceContainer()
    with pytest.raises(RuntimeError, match="ServiceContainer has not been initialized"):
        _ = container.douyin_handler
