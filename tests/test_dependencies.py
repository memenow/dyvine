from __future__ import annotations

import pytest

from src.dyvine.core import dependencies


class DummyHandler:
    def __init__(self, kwargs: dict[str, object]) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def reset_container_cache() -> None:
    dependencies.get_service_container.cache_clear()
    yield
    dependencies.get_service_container.cache_clear()


def test_service_container_initializes_with_douyin_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    def build_handler(kwargs: dict[str, object]) -> DummyHandler:
        captured_kwargs.update(kwargs)
        return DummyHandler(kwargs)

    monkeypatch.setattr(dependencies, "DouyinHandler", build_handler)

    container = dependencies.ServiceContainer()
    container.initialize()

    handler = container.douyin_handler
    assert isinstance(handler, DummyHandler)
    assert captured_kwargs.get("mode") == "all"
    assert captured_kwargs.get("max_tasks") == 3


def test_get_service_container_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "DouyinHandler", lambda kwargs: DummyHandler(kwargs))

    container_one = dependencies.get_service_container()
    container_two = dependencies.get_service_container()

    assert container_one is container_two
