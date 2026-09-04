from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_live() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "Cosmofy APOD API",
        "version": "1.0.0",
    }
