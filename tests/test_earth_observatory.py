from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import database
from app.earth_observatory import _article_body, _media_from_editorial_content, _parse_image_date, _parse_location, _wordpress_editorial_content
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


def test_parses_migrated_wordpress_video_content() -> None:
    content = '<nav>navigation</nav><iframe src="https://www.youtube.com/embed/example"></iframe><p>A NASA video.</p>'
    editorial = _wordpress_editorial_content(content)
    assert _media_from_editorial_content(editorial) == ("video", "https://www.youtube.com/embed/example")


def test_parses_migrated_wordpress_image_content() -> None:
    content = '<nav>breadcrumbs</nav><div class="hds-secondary-navigation-menu-items"><nav>navigation</nav></div><a href="https://images.nasa.gov/earth_lrg.jpg">download</a><p>NASA image.</p><div class="hds-content-lists-inner"><img src="https://example.com/related.jpg">'
    editorial = _wordpress_editorial_content(content)

    assert _media_from_editorial_content(editorial) == ("image", "https://images.nasa.gov/earth_lrg.jpg")
    assert "related.jpg" not in editorial


def test_earth_observatory_database_round_trip_preserves_video_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    video = picture().model_copy(update={"media_type": "video", "url": "https://www.youtube.com/embed/example"})
    stored: tuple[object, ...] | None = None

    class Cursor:
        def fetchone(self) -> tuple[object, ...] | None:
            return stored

    class Connection:
        def execute(self, statement: str, parameters: tuple[object, ...]) -> Cursor:
            nonlocal stored
            if statement.lstrip().startswith("INSERT"):
                stored = parameters
            return Cursor()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    connection = Connection()
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    database.save_earth_observatory_picture(video)
    result = database.get_earth_observatory_picture(video.date)

    assert result is not None
    assert result.media_type == "video"
    assert result.url == video.url


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


def test_current_endpoint_returns_video_when_nasa_publishes_one(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    video = picture().model_copy(update={"media_type": "video", "url": "https://www.youtube.com/embed/example"})
    lock = object()
    monkeypatch.setattr(router, "get_cached_earth_observatory", AsyncMock(side_effect=[None, None]))
    monkeypatch.setattr(router, "acquire_earth_observatory_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(router, "get_earth_observatory_picture", Mock(return_value=None))
    monkeypatch.setattr(router, "save_earth_observatory_picture", Mock())
    monkeypatch.setattr(router, "fetch_earth_observatory_picture", AsyncMock(return_value=video))
    monkeypatch.setattr(router, "cache_earth_observatory", AsyncMock())
    monkeypatch.setattr(router, "release_lock", AsyncMock())

    response = client.get("/earth-observatory")

    assert response.status_code == 200
    assert response.json()["media_type"] == "video"
    assert response.json()["url"] == video.url
