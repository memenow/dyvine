"""Unit tests for the livestreams router."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from dyvine.routers.livestreams import router, get_livestream_service
from dyvine.services.livestreams import (
    LivestreamError,
    UserNotFoundError,
    DownloadError
)

@pytest.fixture
def app(mock_livestream_service):
    """Fixture for FastAPI test application."""
    app = FastAPI()
    
    def get_test_service():
        return mock_livestream_service
        
    app.dependency_overrides[get_livestream_service] = get_test_service
    app.include_router(router)
    return app

@pytest.fixture
def client(app):
    """Fixture for FastAPI test client."""
    return TestClient(app)

def test_download_livestream_success(client, mock_livestream_service):
    """Test successful livestream download request."""
    user_id = "test123"
    expected_path = "/path/to/downloaded/stream.flv"
    
    mock_livestream_service.download_stream = AsyncMock(return_value=("success", expected_path))
    
    response = client.post(
        f"/livestreams/users/{user_id}/stream:download",
        json={"output_path": None}
    )
    
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "download_path": expected_path,
        "error": None
    }

def test_download_livestream_user_not_found(client, mock_livestream_service):
    """Test download request for non-existent user."""
    user_id = "nonexistent123"
    
    mock_livestream_service.download_stream = AsyncMock(
        side_effect=UserNotFoundError("User not found")
    )
    
    response = client.post(
        f"/livestreams/users/{user_id}/stream:download",
        json={"output_path": None}
    )
    
    assert response.status_code == 404
    assert "User not found" in response.json()["detail"]

def test_download_livestream_not_live(client, mock_livestream_service):
    """Test download request when user is not live."""
    user_id = "test123"
    
    mock_livestream_service.download_stream = AsyncMock(
        side_effect=LivestreamError("User is not streaming")
    )
    
    response = client.post(
        f"/livestreams/users/{user_id}/stream:download",
        json={"output_path": None}
    )
    
    assert response.status_code == 404
    assert "User is not streaming" in response.json()["detail"]

def test_download_livestream_download_error(client, mock_livestream_service):
    """Test download request with download failure."""
    user_id = "test123"
    
    mock_livestream_service.download_stream = AsyncMock(
        side_effect=DownloadError("Download failed")
    )
    
    response = client.post(
        f"/livestreams/users/{user_id}/stream:download",
        json={"output_path": None}
    )
    
    assert response.status_code == 500
    assert "Download failed" in response.json()["detail"]

def test_download_livestream_unexpected_error(client, mock_livestream_service):
    """Test download request with unexpected error."""
    user_id = "test123"
    
    mock_livestream_service.download_stream = AsyncMock(
        side_effect=Exception("Unexpected error")
    )
    
    response = client.post(
        f"/livestreams/users/{user_id}/stream:download",
        json={"output_path": None}
    )
    
    assert response.status_code == 500
    assert "Unexpected error" in response.json()["detail"]

def test_get_download_status_success(client, mock_livestream_service):
    """Test successful download status request."""
    operation_id = "op123"
    expected_path = "/path/to/downloaded/stream.flv"
    
    mock_livestream_service.get_download_status = AsyncMock(return_value=expected_path)
    
    response = client.get(f"/livestreams/operations/{operation_id}")
    
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "download_path": expected_path,
        "error": None
    }

def test_get_download_status_not_found(client, mock_livestream_service):
    """Test download status request for non-existent operation."""
    operation_id = "nonexistent123"
    
    mock_livestream_service.get_download_status = AsyncMock(
        side_effect=DownloadError("Operation not found")
    )
    
    response = client.get(f"/livestreams/operations/{operation_id}")
    
    assert response.status_code == 404
    assert "Operation not found" in response.json()["detail"]

def test_get_download_status_unexpected_error(client, mock_livestream_service):
    """Test download status request with unexpected error."""
    operation_id = "op123"
    
    mock_livestream_service.get_download_status = AsyncMock(
        side_effect=Exception("Unexpected error")
    )
    
    response = client.get(f"/livestreams/operations/{operation_id}")
    
    assert response.status_code == 500
    assert "Unexpected error" in response.json()["detail"]
