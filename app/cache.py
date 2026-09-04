import logging
from redis.asyncio import Redis
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from redis.asyncio.lock import Lock
from pydantic import ValidationError
from app.source import SourceApod

logger = logging.getLogger(__name__)

def cache_key(date: date) -> str:
    return f"apod:date:{date.isoformat()}"

def lock_key(date: date) -> str:
    return f"apod:lock:{date.isoformat()}"

def seconds_until_next_day() -> int:
    now = datetime.now(ZoneInfo("America/Denver"))
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=ZoneInfo("America/Denver"))
    return max(1, int((tomorrow - now).total_seconds()))

async def get_cached_apod(client: Redis, date: date) -> SourceApod | None:
    key = cache_key(date)
    cache = await client.get(name=key)
    if cache is None:
        return None

    try:
        return SourceApod.model_validate_json(json_data=cache)
    except ValidationError:
        logger.warning(
            msg="discarding invalid redis cache entry",
            extra={"event": "apod.cache.invalid", "dependency": "apod:redis", "apod_date": date.isoformat()},
        )
        await client.delete(key)
        return None

async def cache_apod(client: Redis, apod: SourceApod) -> None:
    await client.set(name=cache_key(apod.date), value=apod.model_dump_json(), ex=seconds_until_next_day())

async def acquire_lock(client: Redis, date: date) -> Lock | None:
    lock = client.lock(name=lock_key(date), timeout=60)
    acquired = await lock.acquire(blocking=True, blocking_timeout=15)
    return lock if acquired else None

async def release_lock(lock: Lock) -> None:
    await lock.release()
