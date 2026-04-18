import uuid

from fastapi.testclient import TestClient

from dyvine.core.settings import settings
from dyvine.main import app


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


def test_health_check_healthy_response():
    with TestClient(app) as client:
        original_cookie = settings.douyin.cookie
        original_r2 = {
            "account_id": settings.r2.account_id,
            "access_key_id": settings.r2.access_key_id,
            "secret_access_key": settings.r2.secret_access_key,
            "bucket_name": settings.r2.bucket_name,
        }

        try:
            settings.douyin.cookie = "dummy-cookie"
            settings.r2.account_id = "acc"
            settings.r2.access_key_id = "key"
            settings.r2.secret_access_key = "secret"
            settings.r2.bucket_name = "bucket"

            request_id = str(uuid.uuid4())
            response = client.get("/readyz", headers={"X-Request-ID": request_id})

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"
            assert data["correlation_id"] == request_id
            assert response.headers["X-Correlation-ID"] == request_id
        finally:
            settings.douyin.cookie = original_cookie
            settings.r2.account_id = original_r2["account_id"]
            settings.r2.access_key_id = original_r2["access_key_id"]
            settings.r2.secret_access_key = original_r2["secret_access_key"]
            settings.r2.bucket_name = original_r2["bucket_name"]


def test_health_check_missing_douyin_cookie_is_unhealthy():
    with TestClient(app) as client:
        original_cookie = settings.douyin.cookie
        try:
            settings.douyin.cookie = ""
            response = client.get("/readyz")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
        finally:
            settings.douyin.cookie = original_cookie


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
        }

        try:
            settings.douyin.cookie = "cookie"
            settings.r2.account_id = ""
            settings.r2.access_key_id = ""
            settings.r2.secret_access_key = ""
            settings.r2.bucket_name = ""

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
