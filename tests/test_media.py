import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.cache import cache_apod, get_cached_apod
from app.database import source_apod_from_row
from app.media import MEDIA_BASE_URL, with_media_urls
from app.source import SourceApod


def source(**overrides):
    return SourceApod(**dict({"date": "2024-01-01", "title": "test",
        "explanation": "test", "media_type": "image",
        "url": "https://nasa.example/small.jpg", "hdurl": "https://nasa.example/hd.jpg",
        "s3_object_key": "hd/image/" + "a" * 64 + ".jpg"}, **overrides))


def test_verified_image_projection_preserves_original():
    original = source()
    result = with_media_urls(original)
    assert result.url == MEDIA_BASE_URL + "/" + original.s3_object_key
    assert result.url_fallback == original.hdurl
    assert original.url == "https://nasa.example/small.jpg"
    assert "s3_object_key" not in result.model_dump()
    assert "hdurl" not in result.model_dump()


@pytest.mark.parametrize("key", [None, "../secret", "hd/image/bad.jpg", "hd/video/" + "a"*64 + ".mp4"])
def test_unverified_or_invalid_key_uses_nasa_hd_with_sd_fallback(key):
    original = source(s3_object_key=key)
    result = with_media_urls(original)
    assert result.url == original.hdurl
    assert result.url_fallback == original.url


def test_verified_video_and_missing_hd():
    original = source(media_type="video", hdurl=None, s3_object_key="hd/video/"+"a"*64+".mp4")
    result = with_media_urls(original)
    assert result.url.startswith(MEDIA_BASE_URL)
    assert result.url_fallback == original.url


def test_unarchived_embed_keeps_player_url():
    original = source(media_type="video", s3_object_key=None)
    result = with_media_urls(original)
    assert result.url == original.url
    assert result.url_fallback is None


def test_unarchived_image_prefers_nasa_hd_with_sd_fallback():
    original = source(s3_object_key=None)
    result = with_media_urls(original)
    assert result.url == original.hdurl
    assert result.url_fallback == original.url


def test_database_mapping():
    key = source().s3_object_key
    result = source_apod_from_row(("2024-01-01", "t", "e", "u", "h", "image", None, None, 0.1, key))
    assert result.s3_object_key == key


def test_database_mapping_normalizes_null_media_url():
    result = source_apod_from_row(
        ("2007-05-22", "Orange Sun Oozing", "e", None, None, "other", None, None)
    )

    assert result.url == ""


def test_cache_preserves_internal_key():
    import asyncio
    client = AsyncMock()
    original = source()
    asyncio.run(cache_apod(client, original))
    payload = client.set.call_args.kwargs["value"]
    assert json.loads(payload)["s3_object_key"] == original.s3_object_key
    assert json.loads(payload)["hdurl"] == original.hdurl
    client.get.return_value = payload
    restored = asyncio.run(get_cached_apod(client, date(2024, 1, 1)))
    assert with_media_urls(restored).url.startswith(MEDIA_BASE_URL)


def test_search_keeps_media_and_scores():
    from app.database import DatabaseSearchMatch
    from app.search import fuse_search_results
    matches = [DatabaseSearchMatch(source(), 0.1)]
    result = fuse_search_results(matches, matches, 1)[0]
    assert result.url.startswith(MEDIA_BASE_URL)
    assert result.url_fallback == source().hdurl
    assert result.relevance_score == 1.0
    assert result.match_types == ["lexical", "semantic"]


def test_similarity_keeps_media_and_scores(monkeypatch):
    import asyncio
    from app import similarity
    from app.database import DatabaseSearchMatch
    monkeypatch.setattr(similarity, "find_similar_database_apods",
        lambda *_: [DatabaseSearchMatch(source(), 0.1)])
    result = asyncio.run(similarity.similar_apods(date(2023, 1, 1), 1)).results[0]
    assert result.url.startswith(MEDIA_BASE_URL)
    assert result.url_fallback == source().hdurl
    assert result.relevance_score == pytest.approx(0.9)
