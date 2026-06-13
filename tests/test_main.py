"""Tests for the FastAPI application entry point and health probes."""

import sqlite3
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dyvine.core.settings import settings
from dyvine.main import app


async def _async_noop() -> None:
    """Stand-in for ``OperationStore.healthcheck`` in readiness tests.

    ``/readyz`` now awaits the healthcheck, so the stub must also be a
    coroutine function; a plain ``lambda: None`` would raise ``TypeError:
    object NoneType can't be used in 'await' expression``.
    """
    return None


def _configure_r2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_id: str = "acc",
    access_key_id: str = "key",
    secret_access_key: str = "secret",
    bucket_name: str = "bucket",
    endpoint: str = "https://example.r2.cloudflarestorage.com",
) -> None:
    """Apply a fully populated R2 configuration for readiness checks."""
    monkeypatch.setattr(settings.r2, "account_id", account_id)
    monkeypatch.setattr(settings.r2, "access_key_id", access_key_id)
    monkeypatch.setattr(settings.r2, "secret_access_key", secret_access_key)
    monkeypatch.setattr(settings.r2, "bucket_name", bucket_name)
    monkeypatch.setattr(settings.r2, "endpoint", endpoint)


@pytest.fixture
def prime_ready_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Yield a TestClient with all readiness dependencies configured to pass.

    ``monkeypatch`` restores the original settings on exit and exposes the
    running client so individual tests can further tweak the environment.
    """
    monkeypatch.setattr(settings.douyin, "cookie", "dummy-cookie")
    _configure_r2(monkeypatch)

    with TestClient(app) as client:
        container = app.state.container
        monkeypatch.setattr(container.operation_store, "healthcheck", _async_noop)
        yield client


def test_read_main():
    """Verify read main."""
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
    """Verify readiness probe returns ready when all dependencies ok."""
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
    """Verify readiness probe returns ready with request ID."""
    request_id = str(uuid.uuid4())
    response = prime_ready_dependencies.get(
        "/readyz", headers={"X-Request-ID": request_id}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["correlation_id"] == request_id
    assert response.headers["X-Correlation-ID"] == request_id


def test_readiness_probe_returns_not_ready_when_douyin_cookie_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify readiness probe returns not ready when douyin cookie missing."""
    with TestClient(app) as client:
        monkeypatch.setattr(settings.douyin, "cookie", "")
        response = client.get("/readyz")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["dependencies"]["douyin_api"] == "missing_credentials"


def test_readiness_probe_returns_not_ready_when_operation_store_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify readiness probe returns not ready when operation store broken."""
    monkeypatch.setattr(settings.douyin, "cookie", "dummy-cookie")
    _configure_r2(monkeypatch)

    async def _raise() -> None:
        """Test helper for
        test_readiness_probe_returns_not_ready_when_operation_store_broken.
        """
        raise sqlite3.OperationalError("disk I/O error")

    with TestClient(app) as client:
        container = app.state.container
        monkeypatch.setattr(container.operation_store, "healthcheck", _raise)
        response = client.get("/readyz")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["dependencies"]["operation_store"] == "unavailable"


def test_readiness_probe_ready_when_r2_missing_uses_implicit_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 is optional when downloads implicitly retain local files."""
    monkeypatch.setattr(settings.douyin, "cookie", "dummy-cookie")
    _configure_r2(
        monkeypatch,
        account_id="",
        access_key_id="",
        secret_access_key="",
        bucket_name="",
        endpoint="",
    )

    with TestClient(app) as client:
        container = app.state.container
        monkeypatch.setattr(container.operation_store, "healthcheck", _async_noop)
        response = client.get("/readyz")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["dependencies"]["r2_storage"] == "disabled"


def test_readiness_probe_ready_in_local_retention_mode_without_r2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-retention mode makes R2 optional for readiness.

    When ``DOUYIN_RETAIN_LOCAL_DOWNLOADS`` is enabled the service archives to
    the local volume, so absent R2 credentials are expected and must not hold
    the Pod out of the load balancer. ``/readyz`` must report ready with R2
    marked ``disabled`` instead of failing with ``missing_credentials``.
    """
    monkeypatch.setattr(settings.douyin, "cookie", "dummy-cookie")
    monkeypatch.setattr(settings.douyin, "retain_local_downloads", True)
    _configure_r2(
        monkeypatch,
        account_id="",
        access_key_id="",
        secret_access_key="",
        bucket_name="",
        endpoint="",
    )

    with TestClient(app) as client:
        container = app.state.container
        monkeypatch.setattr(container.operation_store, "healthcheck", _async_noop)
        response = client.get("/readyz")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["dependencies"]["r2_storage"] == "disabled"


def test_readiness_probe_ready_when_r2_endpoint_missing_uses_implicit_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness mirrors the download path when R2 is not fully configured."""
    monkeypatch.setattr(settings.douyin, "cookie", "dummy-cookie")
    _configure_r2(monkeypatch, endpoint="")

    with TestClient(app) as client:
        container = app.state.container
        monkeypatch.setattr(container.operation_store, "healthcheck", _async_noop)
        response = client.get("/readyz")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["dependencies"]["r2_storage"] == "disabled"


def test_invalid_request_id_is_regenerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify invalid request ID is regenerated."""
    with TestClient(app) as client:
        monkeypatch.setattr(settings.douyin, "cookie", "")
        response = client.get("/livez", headers={"X-Request-ID": "invalid"})

        assert response.headers["X-Correlation-ID"] != "invalid"
        uuid.UUID(response.headers["X-Correlation-ID"])


def test_health_check_returns_200_when_r2_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` must stay informational and never gate on dependencies.

    The dependency-aware gate lives on ``/readyz``; ``/health`` is a
    metrics aggregator for ops dashboards and is required to return
    ``200 OK`` with ``status == "ok"`` even when optional dependency
    credentials (Douyin cookie, R2 settings) are not configured.
    """
    with TestClient(app) as client:
        monkeypatch.setattr(settings.douyin, "cookie", "cookie")
        _configure_r2(
            monkeypatch,
            account_id="",
            access_key_id="",
            secret_access_key="",
            bucket_name="",
            endpoint="",
        )

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        # Dependency snapshot is informational only; downloads retain locally
        # when no R2 configuration is available.
        assert data["dependencies"]["r2_storage"] == "disabled"
        correlation_id = data["correlation_id"]
        assert correlation_id == response.headers["X-Correlation-ID"]
        uuid.UUID(correlation_id)


def test_health_check_reports_r2_disabled_in_local_retention_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` reports intentionally disabled R2 as informational."""
    with TestClient(app) as client:
        monkeypatch.setattr(settings.douyin, "cookie", "cookie")
        monkeypatch.setattr(settings.douyin, "retain_local_downloads", True)
        _configure_r2(
            monkeypatch,
            account_id="",
            access_key_id="",
            secret_access_key="",
            bucket_name="",
            endpoint="",
        )

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["dependencies"]["r2_storage"] == "disabled"


def test_health_check_returns_200_when_douyin_cookie_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` must remain a metrics aggregator that returns 200.

    Even with no Douyin cookie configured (which previously flipped the
    legacy status to ``"unhealthy"`` and forced a 503), the endpoint now
    surfaces the missing credential through the informational
    ``dependencies`` map while keeping the HTTP contract at ``200 OK``.
    """
    with TestClient(app) as client:
        monkeypatch.setattr(settings.douyin, "cookie", "")
        _configure_r2(monkeypatch)

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["dependencies"]["douyin_api"] == "missing_credentials"
        assert data["memory_pressure"] in {"normal", "high"}


def test_metrics_endpoint_exposed() -> None:
    """Verify metrics endpoint exposed."""
    with TestClient(app) as client:
        response = client.get("/metrics/")
        assert response.status_code == 200
        assert "dyvine_http_requests_total" in response.text


def test_metrics_uses_bounded_label_for_unmatched_routes() -> None:
    """Verify metrics uses bounded label for unmatched routes."""
    with TestClient(app) as client:
        client.get("/definitely-not-a-real-route")
        response = client.get("/metrics/")

        assert response.status_code == 200
        assert 'route="unmatched"' in response.text
        assert "/definitely-not-a-real-route" not in response.text
