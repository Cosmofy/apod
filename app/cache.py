import logging
import json
from redis.asyncio import Redis
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from redis.asyncio.lock import Lock
from pydantic import ValidationError
from app.source import EarthObservatoryPicture, SourceApod

logger = logging.getLogger(__name__)

def cache_key(date: date) -> str:
    return f"apod:date:{date.isoformat()}"

def lock_key(date: date) -> str:
    return f"apod:lock:{date.isoformat()}"

def earth_observatory_cache_key() -> str:
    return "earth-observatory:latest"

def earth_observatory_lock_key() -> str:
    return "earth-observatory:latest:lock"

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
    payload = apod.model_dump(mode="json")
    # Internal source/archive metadata must survive Redis but is excluded from API JSON.
    payload["hdurl"] = apod.hdurl
    payload["s3_object_key"] = apod.s3_object_key
    await client.set(name=cache_key(apod.date), value=json.dumps(payload), ex=seconds_until_next_day())

async def get_cached_earth_observatory(client: Redis) -> EarthObservatoryPicture | None:
    cache = await client.get(name=earth_observatory_cache_key())
    if cache is None:
        return None
    try:
        return EarthObservatoryPicture.model_validate_json(json_data=cache)
    except ValidationError:
        logger.warning(msg="discarding invalid earth observatory redis cache entry", extra={"event": "earth_observatory.cache.invalid", "dependency": "earth-observatory:redis"})
        await client.delete(earth_observatory_cache_key())
        return None

async def cache_earth_observatory(client: Redis, picture: EarthObservatoryPicture) -> None:
    await client.set(
        name=earth_observatory_cache_key(),
        value=picture.model_dump_json(),
        ex=seconds_until_next_day(),
    )

async def acquire_earth_observatory_lock(client: Redis) -> Lock | None:
    lock = client.lock(name=earth_observatory_lock_key(), timeout=60)
    acquired = await lock.acquire(blocking=True, blocking_timeout=15)
    return lock if acquired else None

async def acquire_lock(client: Redis, date: date) -> Lock | None:
    lock = client.lock(name=lock_key(date), timeout=60)
    acquired = await lock.acquire(blocking=True, blocking_timeout=15)
    return lock if acquired else None

async def release_lock(lock: Lock) -> None:
    await lock.release()
