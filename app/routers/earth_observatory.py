import asyncio
import logging

from fastapi import APIRouter, Request
from opentelemetry import trace
from redis import RedisError
from turso_serverless import OperationalError

from app.cache import (
    acquire_earth_observatory_lock,
    cache_earth_observatory,
    get_cached_earth_observatory,
    release_lock,
)
from app.database import get_earth_observatory_picture, save_earth_observatory_picture
from app.earth_observatory import fetch_earth_observatory_picture
from app.errors import Code, Error
from app.source import EarthObservatoryPicture

router = APIRouter(prefix="/earth-observatory", tags=["earth-observatory"])
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@router.get("", response_model=EarthObservatoryPicture, summary="retrieve the current Earth Observatory Image of the Day")
async def get_earth_observatory(request: Request) -> EarthObservatoryPicture:
    lock = None
    with tracer.start_as_current_span("earth_observatory.pipeline") as span:
        try:
            redis_client = request.app.state.redis_client
            try:
                cached = await get_cached_earth_observatory(redis_client)
                if cached:
                    span.set_attribute("earth_observatory.result.source", "redis")
                    return cached
                lock = await acquire_earth_observatory_lock(redis_client)
                if lock:
                    cached = await get_cached_earth_observatory(redis_client)
                    if cached:
                        span.set_attribute("earth_observatory.result.source", "redis")
                        return cached
                else:
                    for _ in range(15):
                        await asyncio.sleep(1)
                        cached = await get_cached_earth_observatory(redis_client)
                        if cached:
                            span.set_attribute("earth_observatory.result.source", "redis")
                            return cached
                    raise Error(Code.EARTH_OBSERVATORY_REQUEST_IN_PROGRESS)
            except RedisError:
                logger.exception("earth observatory redis operation failed", extra={"dependency": "earth-observatory:redis"})

            # The database is keyed by NASA's publication date. The feed supplies
            # that date, so obtain it before checking persistence on a cache miss.
            picture = await fetch_earth_observatory_picture(request.app.state.http_client)
            try:
                stored = await asyncio.to_thread(get_earth_observatory_picture, picture.date)
                if stored:
                    span.set_attribute("earth_observatory.result.source", "turso")
                    return stored
                await asyncio.to_thread(save_earth_observatory_picture, picture)
                span.set_attribute("earth_observatory.result.source", "nasa")
            except OperationalError:
                logger.exception("earth observatory turso operation failed", extra={"dependency": "earth-observatory:turso"})

            try:
                await cache_earth_observatory(redis_client, picture)
            except RedisError:
                logger.exception("earth observatory redis write failed", extra={"dependency": "earth-observatory:redis"})
            return picture
        finally:
            if lock:
                try:
                    await release_lock(lock)
                except RedisError:
                    logger.exception("earth observatory lock release failed", extra={"dependency": "earth-observatory:redis"})
