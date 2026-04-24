import sqlite3
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dyvine.core.settings import settings
from dyvine.main import app


@pytest.fixture
def prime_ready_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Yield a TestClient with all readiness dependencies configured to pass.

    The fixture restores the original settings on exit and exposes the
    running client so individual tests can further tweak the environment.
    """
    original_cookie = settings.douyin.cookie
    original_r2 = {
        "account_id": settings.r2.account_id,
        "access_key_id": settings.r2.access_key_id,
        "secret_access_key": settings.r2.secret_access_key,
        "bucket_name": settings.r2.bucket_name,
        "endpoint": settings.r2.endpoint,
    }

    settings.douyin.cookie = "dummy-cookie"
    settings.r2.account_id = "acc"
    settings.r2.access_key_id = "key"
    settings.r2.secret_access_key = "secret"
    settings.r2.bucket_name = "bucket"
    settings.r2.endpoint = "https://example.r2.cloudflarestorage.com"

    try:
        with TestClient(app) as client:
            container = app.state.container
            monkeypatch.setattr(container.operation_store, "healthcheck", lambda: None)
            yield client
    finally:
        settings.douyin.cookie = original_cookie
        settings.r2.account_id = original_r2["account_id"]
        settings.r2.access_key_id = original_r2["access_key_id"]
        settings.r2.secret_access_key = original_r2["secret_access_key"]
        settings.r2.bucket_name = original_r2["bucket_name"]
        settings.r2.endpoint = original_r2["endpoint"]


def test_read_main():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == settings.project_name
        assert data["version"] == settings.version
        assert data["docs"] == "/docs"
        assert data["redoc"] == "/redoc"
        assert data["status"] == "operational"
        assert data["api_prefix"] == settings.prefix
        assert data["features"] == ["users", "posts", "livestreams"]


def test_readiness_probe_returns_ready_when_all_dependencies_ok(
    prime_ready_dependencies: TestClient,
) -> None:
    response = prime_ready_dependencies.get("/readyz")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["dependencies"] == {
        "douyin_api": "configured",
        "service_container": "initialized",
        "operation_store": "available",
        "r2_storage": "configured",
    }


def test_readiness_probe_returns_ready_with_request_id(
    prime_ready_dependencies: TestClient,
) -> None:
    request_id = str(uuid.uuid4())
    response = prime_ready_dependencies.get(
        "/readyz", headers={"X-Request-ID": request_id}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["correlation_id"] == request_id
    assert response.headers["X-Correlation-ID"] == request_id


def test_readiness_probe_returns_not_ready_when_douyin_cookie_missing() -> None:
    with TestClient(app) as client:
        original_cookie = settings.douyin.cookie
        try:
            settings.douyin.cookie = ""
            response = client.get("/readyz")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["dependencies"]["douyin_api"] == "missing_credentials"
        finally:
            settings.douyin.cookie = original_cookie


def test_readiness_probe_returns_not_ready_when_operation_store_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cookie = settings.douyin.cookie
    original_r2 = {
        "account_id": settings.r2.account_id,
        "access_key_id": settings.r2.access_key_id,
        "secret_access_key": settings.r2.secret_access_key,
        "bucket_name": settings.r2.bucket_name,
        "endpoint": settings.r2.endpoint,
    }

    settings.douyin.cookie = "dummy-cookie"
    settings.r2.account_id = "acc"
    settings.r2.access_key_id = "key"
    settings.r2.secret_access_key = "secret"
    settings.r2.bucket_name = "bucket"
    settings.r2.endpoint = "https://example.r2.cloudflarestorage.com"

    def _raise() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    try:
        with TestClient(app) as client:
            container = app.state.container
            monkeypatch.setattr(container.operation_store, "healthcheck", _raise)
            response = client.get("/readyz")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["dependencies"]["operation_store"] == "unavailable"
    finally:
        settings.douyin.cookie = original_cookie
        settings.r2.account_id = original_r2["account_id"]
        settings.r2.access_key_id = original_r2["access_key_id"]
        settings.r2.secret_access_key = original_r2["secret_access_key"]
        settings.r2.bucket_name = original_r2["bucket_name"]
        settings.r2.endpoint = original_r2["endpoint"]


def test_readiness_probe_returns_not_ready_when_r2_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cookie = settings.douyin.cookie
    original_r2 = {
        "account_id": settings.r2.account_id,
        "access_key_id": settings.r2.access_key_id,
        "secret_access_key": settings.r2.secret_access_key,
        "bucket_name": settings.r2.bucket_name,
        "endpoint": settings.r2.endpoint,
    }

    settings.douyin.cookie = "dummy-cookie"
    settings.r2.account_id = ""
    settings.r2.access_key_id = ""
    settings.r2.secret_access_key = ""
    settings.r2.bucket_name = ""
    settings.r2.endpoint = ""

    try:
        with TestClient(app) as client:
            container = app.state.container
            monkeypatch.setattr(container.operation_store, "healthcheck", lambda: None)
            response = client.get("/readyz")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["dependencies"]["r2_storage"] == "missing_credentials"
    finally:
        settings.douyin.cookie = original_cookie
        settings.r2.account_id = original_r2["account_id"]
        settings.r2.access_key_id = original_r2["access_key_id"]
        settings.r2.secret_access_key = original_r2["secret_access_key"]
        settings.r2.bucket_name = original_r2["bucket_name"]
        settings.r2.endpoint = original_r2["endpoint"]


def test_readiness_probe_returns_not_ready_when_r2_endpoint_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the bug where /readyz passed while ``settings.r2.endpoint`` was
    empty, even though ``R2StorageService`` disables itself in that state.
    """
    original_cookie = settings.douyin.cookie
    original_r2 = {
        "account_id": settings.r2.account_id,
        "access_key_id": settings.r2.access_key_id,
        "secret_access_key": settings.r2.secret_access_key,
        "bucket_name": settings.r2.bucket_name,
        "endpoint": settings.r2.endpoint,
    }

    settings.douyin.cookie = "dummy-cookie"
    settings.r2.account_id = "acc"
    settings.r2.access_key_id = "key"
    settings.r2.secret_access_key = "secret"
    settings.r2.bucket_name = "bucket"
    settings.r2.endpoint = ""

    try:
        with TestClient(app) as client:
            container = app.state.container
            monkeypatch.setattr(container.operation_store, "healthcheck", lambda: None)
            response = client.get("/readyz")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["dependencies"]["r2_storage"] == "missing_credentials"
    finally:
        settings.douyin.cookie = original_cookie
        settings.r2.account_id = original_r2["account_id"]
        settings.r2.access_key_id = original_r2["access_key_id"]
        settings.r2.secret_access_key = original_r2["secret_access_key"]
        settings.r2.bucket_name = original_r2["bucket_name"]
        settings.r2.endpoint = original_r2["endpoint"]


def test_invalid_request_id_is_regenerated():
    with TestClient(app) as client:
        original_cookie = settings.douyin.cookie
        try:
            settings.douyin.cookie = ""
            response = client.get("/livez", headers={"X-Request-ID": "invalid"})

            assert response.headers["X-Correlation-ID"] != "invalid"
            uuid.UUID(response.headers["X-Correlation-ID"])
        finally:
            settings.douyin.cookie = original_cookie


def test_health_check_degraded_when_r2_missing():
    with TestClient(app) as client:
        original_cookie = settings.douyin.cookie
        original_r2 = {
            "account_id": settings.r2.account_id,
            "access_key_id": settings.r2.access_key_id,
            "secret_access_key": settings.r2.secret_access_key,
            "bucket_name": settings.r2.bucket_name,
            "endpoint": settings.r2.endpoint,
        }

        try:
            settings.douyin.cookie = "cookie"
            settings.r2.account_id = ""
            settings.r2.access_key_id = ""
            settings.r2.secret_access_key = ""
            settings.r2.bucket_name = ""
            settings.r2.endpoint = ""

            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"
            correlation_id = data["correlation_id"]
            assert correlation_id == response.headers["X-Correlation-ID"]
            uuid.UUID(correlation_id)
        finally:
            settings.douyin.cookie = original_cookie
            settings.r2.account_id = original_r2["account_id"]
            settings.r2.access_key_id = original_r2["access_key_id"]
            settings.r2.secret_access_key = original_r2["secret_access_key"]
            settings.r2.bucket_name = original_r2["bucket_name"]
            settings.r2.endpoint = original_r2["endpoint"]


def test_metrics_endpoint_exposed() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics/")
        assert response.status_code == 200
        assert "dyvine_http_requests_total" in response.text


def test_metrics_uses_bounded_label_for_unmatched_routes() -> None:
    with TestClient(app) as client:
        client.get("/definitely-not-a-real-route")
        response = client.get("/metrics/")

        assert response.status_code == 200
        assert 'route="unmatched"' in response.text
        assert "/definitely-not-a-real-route" not in response.text
