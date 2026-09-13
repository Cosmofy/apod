from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.earth_observatory import _article_body, _parse_image_date, _parse_location
from app.main import app
from app.routers import earth_observatory as router
from app.source import EarthObservatoryPicture


def picture() -> EarthObservatoryPicture:
    return EarthObservatoryPicture(
        date=date(2026, 9, 11), title="Monterrey Amid Mountains", explanation="An astronaut photographed this while orbiting over Monterrey, Mexico.",
        url="https://example.com/monterrey_lrg.jpg", article_url="https://science.nasa.gov/example",
        location_name="over Monterrey, Mexico", latitude=25.6867, longitude=-100.3162,
    )


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_parses_editorial_content_without_downloads() -> None:
    html = '<div class="usa-article-content"><div class="entry-content"><p>First paragraph.</p><p>Second paragraph.</p><div class="hds-featured-file-list"><p>Download.</p>'
    assert _article_body(html) == "First paragraph.\n\nSecond paragraph."
    assert _parse_image_date("Photographed on November 25, 2024.") == date(2024, 11, 25)
    assert _parse_location("while orbiting over Quebec, Canada.") == "over Quebec, Canada"


def test_redis_hit_returns_cached_picture(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = picture()
    lookup = AsyncMock(return_value=cached)
    monkeypatch.setattr(router, "get_cached_earth_observatory", lookup)
    monkeypatch.setattr(router, "fetch_earth_observatory_picture", AsyncMock())

    response = client.get("/earth-observatory")

    assert response.status_code == 200
    assert response.json()["source"] == "earth_observatory"
    assert response.json()["latitude"] == 25.6867
    lookup.assert_awaited_once()


def test_fetches_persists_and_caches_on_miss(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = picture()
    lock = object()
    monkeypatch.setattr(router, "get_cached_earth_observatory", AsyncMock(side_effect=[None, None]))
    monkeypatch.setattr(router, "acquire_earth_observatory_lock", AsyncMock(return_value=lock))
    database = Mock(return_value=None)
    save = Mock()
    fetch = AsyncMock(return_value=fetched)
    cache = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(router, "get_earth_observatory_picture", database)
    monkeypatch.setattr(router, "save_earth_observatory_picture", save)
    monkeypatch.setattr(router, "fetch_earth_observatory_picture", fetch)
    monkeypatch.setattr(router, "cache_earth_observatory", cache)
    monkeypatch.setattr(router, "release_lock", release)

    response = client.get("/earth-observatory")

    assert response.status_code == 200
    database.assert_called_once_with(fetched.date)
    save.assert_called_once_with(fetched)
    cache.assert_awaited_once_with(app.state.redis_client, fetched)
    release.assert_awaited_once_with(lock)
