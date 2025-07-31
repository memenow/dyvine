import sys
from pathlib import Path

from fastapi.testclient import TestClient

# Add project root to the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dyvine.core.settings import settings
from src.dyvine.main import app


def test_read_main():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == settings.project_name
        assert data["version"] == settings.version
        assert data["docs"] == "/docs"
        assert data["redoc"] == "/redoc"
