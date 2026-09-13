from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from turso_serverless import OperationalError

from app import database
from app.config import Settings
from app.routers import health
from app.source import EarthObservatoryPicture, SourceApod


@pytest.fixture
def settings(monkeypatch):
    for key, value in {
        "TURSO_DATABASE_URL": "libsql://apod.example",
        "TURSO_AUTH_TOKEN": "apod-test-token",
        "OPENAI_API_KEY": "test", "NASA_API_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EO_DATABASE_URL", raising=False)
    monkeypatch.delenv("EO_AUTH_TOKEN", raising=False)
    factory = lambda: Settings(_env_file=None)
    monkeypatch.setattr(database, "Settings", factory)
    monkeypatch.setattr(health, "Settings", factory)
    return factory


def test_legacy_environment_keeps_eo_optional(settings):
    config = settings()
    assert config.eo_database_url is None
    assert config.eo_auth_token is None
    assert config.turso_database_url == "libsql://apod.example"


@pytest.mark.parametrize("url", [None, ""])
def test_no_eo_url_calls_existing_connection_even_with_token(settings, monkeypatch, url):
    if url is not None:
        monkeypatch.setenv("EO_DATABASE_URL", url)
    monkeypatch.setenv("EO_AUTH_TOKEN", "unused")
    shared = Mock()
    driver = Mock()
    monkeypatch.setattr(database, "connect_database", shared)
    monkeypatch.setattr(database.turso_serverless, "connect", driver)
    assert database.connect_earth_observatory_database() is shared.return_value
    shared.assert_called_once_with()
    driver.assert_not_called()


@pytest.mark.parametrize("token", [None, "", "eo-test-token"])
def test_explicit_eo_url_and_optional_token(settings, monkeypatch, token):
    monkeypatch.setenv("EO_DATABASE_URL", "libsql://eo.example")
    if token is not None:
        monkeypatch.setenv("EO_AUTH_TOKEN", token)
    driver, shared = Mock(), Mock()
    monkeypatch.setattr(database.turso_serverless, "connect", driver)
    monkeypatch.setattr(database, "connect_database", shared)
    assert database.connect_earth_observatory_database() is driver.return_value
    driver.assert_called_once_with("libsql://eo.example", auth_token=token or "apod-test-token")
    shared.assert_not_called()


def test_apod_connection_ignores_eo_settings(settings, monkeypatch):
    monkeypatch.setenv("EO_DATABASE_URL", "libsql://eo.example")
    monkeypatch.setenv("EO_AUTH_TOKEN", "eo-test-token")
    driver = Mock()
    monkeypatch.setattr(database.turso_serverless, "connect", driver)
    database.connect_database()
    driver.assert_called_once_with("libsql://apod.example", auth_token="apod-test-token")


DAY = date(2025, 1, 1)
VECTOR = [0.0] * 3072
EO = EarthObservatoryPicture(date=DAY, title="Earth", explanation="Earth", url="https://example.com/image.jpg", article_url="https://example.com/article")
APOD = SourceApod(date=DAY, title="Moon", explanation="Moon", media_type="image", url="https://example.com/moon.jpg")


@pytest.mark.parametrize("name,args", [
    ("get_earth_observatory_picture", (DAY,)),
    ("save_earth_observatory_picture", (EO,)),
    ("search_earth_observatory_pictures", ("moon", 10)),
    ("save_earth_observatory_embedding", (DAY, VECTOR)),
    ("search_vector_earth_observatory_pictures", (VECTOR, 10)),
    ("find_similar_earth_observatory_pictures", (DAY, 10)),
])
def test_every_eo_function_uses_eo_connection(monkeypatch, name, args):
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = []
    connection.execute.return_value.fetchone.return_value = (b"stored",) if name.startswith("find_similar") else None
    eo_connect = Mock(return_value=connection)
    apod_connect = Mock(side_effect=AssertionError("EO must not use APOD connection"))
    monkeypatch.setattr(database, "connect_earth_observatory_database", eo_connect)
    monkeypatch.setattr(database, "connect_database", apod_connect)
    getattr(database, name)(*args)
    eo_connect.assert_called_once_with()
    apod_connect.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("name,args", [
    ("get_database_apod", (DAY,)),
    ("save_database_apod", (APOD,)),
    ("save_database_embedding", (DAY, VECTOR)),
    ("search_lexical_apods", ("moon", 10)),
    ("search_vector_apods", (VECTOR, 10)),
    ("find_similar_database_apods", (DAY, 10)),
])
def test_every_apod_function_keeps_original_connection(monkeypatch, name, args):
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = []
    connection.execute.return_value.fetchone.return_value = (b"stored",) if name.startswith("find_similar") else None
    apod_connect = Mock(return_value=connection)
    eo_connect = Mock(side_effect=AssertionError("APOD must not use EO connection"))
    monkeypatch.setattr(database, "connect_database", apod_connect)
    monkeypatch.setattr(database, "connect_earth_observatory_database", eo_connect)
    getattr(database, name)(*args)
    apod_connect.assert_called_once_with()
    eo_connect.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("configured,eo_status,expected_status", [
    (False, "ok", 200), (True, "ok", 200), (True, "bad_result", 503),
    (True, "query_error", 503), (True, "connect_error", 503),
])
def test_readiness_only_adds_eo_when_configured(settings, monkeypatch, configured, eo_status, expected_status):
    if configured:
        monkeypatch.setenv("EO_DATABASE_URL", "libsql://eo.example")
    primary, eo = Mock(), Mock()
    primary.execute.return_value.fetchone.return_value = (67, 69)
    eo.execute.return_value.fetchone.return_value = (67, 69) if eo_status == "ok" else None
    if eo_status == "query_error":
        eo.execute.side_effect = OperationalError("offline")
    eo_connect = Mock(return_value=eo)
    if eo_status == "connect_error":
        eo_connect.side_effect = OperationalError("offline")
    monkeypatch.setattr(health, "connect_database", Mock(return_value=primary))
    monkeypatch.setattr(health, "connect_earth_observatory_database", eo_connect)
    app = FastAPI()
    app.state.redis_client = AsyncMock()
    app.state.redis_client.ping.return_value = True
    app.include_router(health.router)
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == expected_status
    expected = {"redis": "ok", "turso": "ok"}
    if configured:
        expected["earth_observatory_turso"] = "ok" if eo_status == "ok" else "unavailable"
        eo_connect.assert_called_once_with()
        if eo_status != "connect_error":
            eo.close.assert_called_once()
    else:
        eo_connect.assert_not_called()
    assert response.json() == {"status": "ready" if expected_status == 200 else "not_ready", "dependencies": expected}
    primary.close.assert_called_once()
