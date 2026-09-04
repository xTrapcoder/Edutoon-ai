from fastapi.testclient import TestClient

from edutoon.main import create_app


def test_health_returns_expected_shape() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    for key in ("status", "environment", "version", "database", "redis"):
        assert key in body
