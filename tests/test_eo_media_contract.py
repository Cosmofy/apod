"""EO archive projection, local SQL, and latest-cache regression coverage."""

import asyncio
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis import RedisError

from app import database
from app.cache import cache_earth_observatory, get_cached_earth_observatory
from app.errors import Error, handle_error
from app.media import MEDIA_BASE_URL, with_earth_observatory_media_urls
from app.routers import earth_observatory as eo
from app.source import EarthObservatoryPicture


def picture(**overrides):
    return EarthObservatoryPicture(**{
        "date": "2024-01-01", "title": "Cloud streets", "explanation": "Over the ocean.",
        "url": "https://nasa.example/clouds.jpg", "article_url": "https://science.nasa.gov/example",
        "image_date": "2023-12-20", "latitude": 10.0, "longitude": -20.0,
        "s3_object_key": "eo/image/" + "a" * 64 + ".jpg", **overrides,
    })


@pytest.mark.parametrize("media_type,extension", [("image", "jpg"), ("video", "mp4")])
def test_verified_projection_preserves_source_and_hides_key(media_type, extension):
    original = picture(media_type=media_type, s3_object_key=f"eo/{media_type}/" + "a" * 64 + f".{extension}")
    before = original.model_dump()
    result = with_earth_observatory_media_urls(original)
    assert result is not original
    assert original.model_dump() == before
    assert result.url == f"{MEDIA_BASE_URL}/{original.s3_object_key}"
    assert result.url_fallback == original.url
    assert result.image_date == original.image_date
    assert (result.latitude, result.longitude) == (10.0, -20.0)
    assert "s3_object_key" not in result.model_dump()
    assert "s3_object_key" not in result.model_dump_json()


@pytest.mark.parametrize("key", [
    None, "", "../secret", "eo/image/bad.jpg", "eo/image/" + "a" * 63 + ".jpg",
    "eo/image/" + "A" * 64 + ".jpg", "eo/image/" + "a" * 64 + ".jpg?x=1",
    "hd/image/" + "a" * 64 + ".jpg", "eo/video/" + "a" * 64 + ".mp4",
    "eo/image/" + "a" * 64 + ".jpg/../../secret",
])
def test_invalid_or_mismatched_keys_leave_source_urls_unchanged(key):
    original = picture(s3_object_key=key, url_fallback="https://nasa.example/fallback.jpg")
    assert with_earth_observatory_media_urls(original) is original


def test_unarchived_video_keeps_embed_url():
    original = picture(media_type="video", s3_object_key=None, url="https://www.youtube.com/embed/example")
    result = with_earth_observatory_media_urls(original)
    assert result.url == original.url
    assert result.url_fallback is None


def test_projection_uses_existing_fallback_when_source_url_is_empty():
    result = with_earth_observatory_media_urls(picture(url="", url_fallback="https://nasa.example/original.jpg"))
    assert result.url_fallback == "https://nasa.example/original.jpg"


def test_projected_urls_survive_real_cache_serialization():
    original = picture()
    projected = with_earth_observatory_media_urls(original)
    redis = AsyncMock()
    asyncio.run(cache_earth_observatory(redis, projected))
    redis.get.return_value = redis.set.call_args.kwargs["value"]
    restored = asyncio.run(get_cached_earth_observatory(redis))
    assert restored.url == projected.url
    assert restored.url_fallback == original.url
    assert restored.model_dump() == projected.model_dump()


@pytest.fixture
def local_database(tmp_path, monkeypatch):
    path = tmp_path / "eo.sqlite"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with sqlite3.connect(path) as connection:
        for name in ("006_create_earth_observatory_pictures.sql", "007_add_earth_observatory_media_type.sql", "008_add_earth_observatory_archive_fields.sql"):
            connection.executescript((migrations / name).read_text())
    monkeypatch.setattr(database, "connect_database", lambda: sqlite3.connect(path))
    return path


def test_real_lexical_sql_weights_title_orders_ties_and_limits(local_database):
    for day, title, explanation in [
        ("2024-01-01", "Clouds", "Ocean"),
        ("2024-01-02", "Clouds", "Ocean"),
        ("2024-01-03", "Ocean", "Clouds"),
        ("2024-01-04", "Mountains", "Rock"),
    ]:
        database.save_earth_observatory_picture(picture(date=day, title=title, explanation=explanation, s3_object_key=None))
    matches = database.search_earth_observatory_pictures("CLOUDS", 2)
    assert [m.picture.date.isoformat() for m in matches] == ["2024-01-02", "2024-01-01"]
    assert [m.score for m in matches] == [3.0, 3.0]
    assert [m.score for m in database.search_earth_observatory_pictures("clouds", 50)] == [3.0, 3.0, 1.0]
    assert database.search_earth_observatory_pictures("absent", 10) == []


@pytest.mark.parametrize("change,preserve_key", [
    ({"title": "New title"}, True),
    ({"url": "https://nasa.example/replaced.jpg"}, False),
    ({"media_type": "video"}, False),
])
def test_upsert_preserves_verified_key_only_for_unchanged_media(local_database, change, preserve_key):
    original = picture()
    database.save_earth_observatory_picture(original)
    with sqlite3.connect(local_database) as connection:
        connection.execute("UPDATE earth_observatory_pictures SET s3_object_key = ?", (original.s3_object_key,))
    database.save_earth_observatory_picture(original.model_copy(update=change))
    with sqlite3.connect(local_database) as connection:
        url, key = connection.execute("SELECT media_url, s3_object_key FROM earth_observatory_pictures").fetchone()
    assert url == change.get("url", original.url)
    assert key == (original.s3_object_key if preserve_key else None)


@pytest.mark.parametrize("endpoint", ["historical", "lexical", "semantic"])
def test_database_projection_reaches_http_without_private_metadata(monkeypatch, endpoint):
    original = picture()
    row = (original.date.isoformat(), original.title, original.explanation, original.media_type,
           original.url, None, None, None, original.article_url, original.image_date.isoformat(),
           None, original.latitude, original.longitude, original.s3_object_key)
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = row
    connection.execute.return_value.fetchall.return_value = [(*row, 0.25)]
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    monkeypatch.setattr(eo, "create_query_embedding", Mock(return_value=[0.0] * 3072))
    if endpoint == "lexical":
        monkeypatch.setattr(eo, "search_vector_earth_observatory_pictures", Mock(return_value=[]))
    elif endpoint == "semantic":
        monkeypatch.setattr(eo, "search_earth_observatory_pictures", Mock(return_value=[]))
    app = FastAPI()
    app.include_router(eo.router)
    with TestClient(app) as client:
        response = client.get("/earth-observatory?date=2024-01-01" if endpoint == "historical" else "/earth-observatory/search?q=clouds")
    assert response.status_code == 200
    body = response.json() if endpoint == "historical" else response.json()["results"][0]
    assert body["url"] == f"{MEDIA_BASE_URL}/{original.s3_object_key}"
    assert body["url_fallback"] == original.url
    assert body["article_url"] == original.article_url
    assert body["image_date"] == "2023-12-20"
    assert body["latitude"] == 10.0
    assert "s3_object_key" not in body
    if endpoint != "historical":
        assert body["relevance_score"] == 1.0
    connection.close.assert_called_once()


@pytest.mark.parametrize("cache_failure", [False, True])
def test_latest_stored_hit_repopulates_redis_and_releases_lock(monkeypatch, cache_failure):
    stored = with_earth_observatory_media_urls(picture())
    fetched = picture(title="Fresh feed title", s3_object_key=None)
    lock = object()
    cache = AsyncMock(side_effect=RedisError("write failed") if cache_failure else None)
    save = Mock()
    release = AsyncMock()
    fetch = AsyncMock(return_value=fetched)
    lookup = Mock(return_value=stored)
    cached = AsyncMock(side_effect=[None, None])
    monkeypatch.setattr(eo, "get_cached_earth_observatory", cached)
    monkeypatch.setattr(eo, "acquire_earth_observatory_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(eo, "fetch_earth_observatory_picture", fetch)
    monkeypatch.setattr(eo, "get_earth_observatory_picture", lookup)
    monkeypatch.setattr(eo, "save_earth_observatory_picture", save)
    monkeypatch.setattr(eo, "cache_earth_observatory", cache)
    monkeypatch.setattr(eo, "release_lock", release)
    app = FastAPI()
    app.state.redis_client = AsyncMock()
    app.state.http_client = object()
    app.include_router(eo.router)
    app.add_exception_handler(Error, handle_error)
    with TestClient(app) as client:
        response = client.get("/earth-observatory")
        assert response.status_code == 200
        assert response.json() == stored.model_dump(mode="json")
        lookup.assert_called_once_with(date(2024, 1, 1))
        cache.assert_awaited_once_with(app.state.redis_client, stored)
        save.assert_not_called()
        release.assert_awaited_once_with(lock)
        if not cache_failure:
            cached.side_effect = None
            cached.return_value = stored
            second = client.get("/earth-observatory")
            assert second.json() == response.json()
            fetch.assert_awaited_once()
            lookup.assert_called_once()
