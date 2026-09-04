import asyncio
from datetime import date
from unittest.mock import AsyncMock

from app.cache import cache_key, get_cached_apod


def test_invalid_cached_apod_is_deleted_and_treated_as_a_miss() -> None:
    apod_date = date(2026, 9, 3)
    redis_client = AsyncMock()
    redis_client.get.return_value = b'{"date":"broken"}'

    result = asyncio.run(get_cached_apod(redis_client, apod_date))

    assert result is None
    redis_client.delete.assert_awaited_once_with(cache_key(apod_date))
