from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from redis import RedisError

from app.errors import Code, Error
from app.main import app
from app.routers import apod as apod_router
from app.source import SourceApod


TODAY = date(2026, 9, 3)


def sample_apod() -> SourceApod:
    return SourceApod(
        date=TODAY,
        title="Sample APOD",
        explanation="A sample explanation.",
        media_type="image",
        url="https://example.com/apod.jpg",
    )


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apod_router, "resolve_date", lambda _requested=None: TODAY)


def test_redis_hit_skips_turso_and_nasa(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    get_cached = AsyncMock(return_value=apod)
    acquire = AsyncMock()
    database_lookup = Mock()
    fetch = AsyncMock()

    monkeypatch.setattr(apod_router, "get_cached_apod", get_cached)
    monkeypatch.setattr(apod_router, "acquire_lock", acquire)
    monkeypatch.setattr(apod_router, "get_database_apod", database_lookup)
    monkeypatch.setattr(apod_router, "fetch_apod", fetch)

    response = client.get("/apod")

    assert response.status_code == 200
    assert response.json()["title"] == apod.title
    acquire.assert_not_awaited()
    database_lookup.assert_not_called()
    fetch.assert_not_awaited()


def test_turso_hit_refills_redis_and_releases_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    lock = object()
    get_cached = AsyncMock(side_effect=[None, None])
    acquire = AsyncMock(return_value=lock)
    database_lookup = Mock(return_value=apod)
    cache = AsyncMock()
    release = AsyncMock()
    fetch = AsyncMock()

    monkeypatch.setattr(apod_router, "get_cached_apod", get_cached)
    monkeypatch.setattr(apod_router, "acquire_lock", acquire)
    monkeypatch.setattr(apod_router, "get_database_apod", database_lookup)
    monkeypatch.setattr(apod_router, "cache_apod", cache)
    monkeypatch.setattr(apod_router, "release_lock", release)
    monkeypatch.setattr(apod_router, "fetch_apod", fetch)

    response = client.get("/apod")

    assert response.status_code == 200
    cache.assert_awaited_once_with(app.state.redis_client, apod)
    release.assert_awaited_once_with(lock)
    fetch.assert_not_awaited()


def test_turso_miss_fetches_nasa_and_saves_both_stores(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    lock = object()
    get_cached = AsyncMock(side_effect=[None, None])
    database_lookup = Mock(return_value=None)
    save_database = Mock()
    create_embedding = Mock(return_value=[0.0] * 3072)
    save_embedding = Mock()
    fetch = AsyncMock(return_value=apod)
    cache = AsyncMock()
    release = AsyncMock()

    monkeypatch.setattr(apod_router, "get_cached_apod", get_cached)
    monkeypatch.setattr(apod_router, "acquire_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(apod_router, "get_database_apod", database_lookup)
    monkeypatch.setattr(apod_router, "save_database_apod", save_database)
    monkeypatch.setattr(apod_router, "create_apod_embedding", create_embedding)
    monkeypatch.setattr(apod_router, "save_database_embedding", save_embedding)
    monkeypatch.setattr(apod_router, "fetch_apod", fetch)
    monkeypatch.setattr(apod_router, "cache_apod", cache)
    monkeypatch.setattr(apod_router, "release_lock", release)

    response = client.get("/apod")

    assert response.status_code == 200
    save_database.assert_called_once_with(apod)
    create_embedding.assert_called_once_with(apod)
    save_embedding.assert_called_once_with(apod.date, create_embedding.return_value)
    cache.assert_awaited_once_with(app.state.redis_client, apod)
    release.assert_awaited_once_with(lock)


def test_redis_failure_falls_back_to_turso(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    database_lookup = Mock(return_value=apod)
    cache = AsyncMock(side_effect=RedisError("Redis unavailable"))

    monkeypatch.setattr(
        apod_router,
        "get_cached_apod",
        AsyncMock(side_effect=RedisError("Redis unavailable")),
    )
    monkeypatch.setattr(apod_router, "get_database_apod", database_lookup)
    monkeypatch.setattr(apod_router, "cache_apod", cache)
    monkeypatch.setattr(apod_router, "fetch_apod", AsyncMock())

    response = client.get("/apod")

    assert response.status_code == 200
    assert response.json()["title"] == apod.title
    database_lookup.assert_called_once_with(TODAY)


def test_nasa_failure_still_releases_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = object()
    release = AsyncMock()

    monkeypatch.setattr(
        apod_router,
        "get_cached_apod",
        AsyncMock(side_effect=[None, None]),
    )
    monkeypatch.setattr(apod_router, "acquire_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(apod_router, "get_database_apod", Mock(return_value=None))
    monkeypatch.setattr(
        apod_router,
        "fetch_apod",
        AsyncMock(side_effect=Error(Code.NASA_UNAVAILABLE)),
    )
    monkeypatch.setattr(apod_router, "release_lock", release)

    response = client.get("/apod")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "NASA_UNAVAILABLE"
    release.assert_awaited_once_with(lock)


def test_lock_release_failure_does_not_replace_successful_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    lock = object()

    monkeypatch.setattr(
        apod_router,
        "get_cached_apod",
        AsyncMock(side_effect=[None, None]),
    )
    monkeypatch.setattr(apod_router, "acquire_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(apod_router, "get_database_apod", Mock(return_value=apod))
    monkeypatch.setattr(apod_router, "cache_apod", AsyncMock())
    monkeypatch.setattr(
        apod_router,
        "release_lock",
        AsyncMock(side_effect=RedisError("Redis unavailable")),
    )

    response = client.get("/apod")

    assert response.status_code == 200
    assert response.json()["title"] == apod.title


def test_non_owner_uses_result_populated_by_lock_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apod = sample_apod()
    database_lookup = Mock()
    fetch = AsyncMock()

    monkeypatch.setattr(
        apod_router,
        "get_cached_apod",
        AsyncMock(side_effect=[None, apod]),
    )
    monkeypatch.setattr(apod_router, "acquire_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(apod_router.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(apod_router, "get_database_apod", database_lookup)
    monkeypatch.setattr(apod_router, "fetch_apod", fetch)

    response = client.get("/apod")

    assert response.status_code == 200
    assert response.json()["title"] == apod.title
    database_lookup.assert_not_called()
    fetch.assert_not_awaited()
