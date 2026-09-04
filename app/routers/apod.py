import asyncio
from redis import RedisError
import redis.asyncio as redis
from fastapi import APIRouter, Request
from openai import OpenAIError
from turso_serverless import OperationalError
import logging
from app.cache import acquire_lock, cache_apod, get_cached_apod, release_lock
from app.database import get_database_apod, save_database_apod, save_database_embedding
from app.embeddings import create_apod_embedding
from app.errors import Code, Error
from app.nasa import fetch_apod, resolve_date
from app.source import SourceApod

router = APIRouter(prefix="/apod", tags=["apod"])
logger = logging.getLogger(name=__name__)

# get picture by date (default = today, otherwise has data argument)
@router.get(
    path="",
    status_code=200,
    summary="retrieve an astronomy Picture of the Day",
    response_model=SourceApod,
)
async def get_apod(request: Request, date: str | None = None) -> SourceApod:

    """
    if today:
        check redis:
            if hit: return apod
            else:
                acquire lock
                if not gotten lock:
                    wait
                    if redis: return apod

    # at this point redis/lock doesnt matter, jump to db
    check turso:
        if hit:
            if today: save to redis
            return apod

    check nasa:
        if 200:
            save to turso
            if today: save to redis
            return apod

    check ellanan:
        if 200:
            save to turso
            if today: save to redis
            return apod
    raise Error
    release lock
    """

    get_lock = False

    try:
        # 0. check input
        date = resolve_date(date) # format changes from str to Date
        is_today = date == resolve_date() # is the requested date today?
        logger.info(
            msg="apod request date resolved",
            extra={"event": "apod.request.resolved", "apod_date": date.isoformat(), "is_today": is_today},
        )

        # 1. check redis cache

        try:
            redis_client = request.app.state.redis_client
            if is_today:
                cached_apod = await get_cached_apod(redis_client, date)
                if cached_apod:
                    logger.info(msg="apod retrieved", extra={"event": "apod.retrieved", "source": "redis", "apod_date": date.isoformat()})
                    return cached_apod
                logger.info(msg="redis cache miss", extra={"event": "apod.cache.miss", "source": "redis", "apod_date": date.isoformat()})
                get_lock = await acquire_lock(redis_client, date)

                if get_lock:
                    logger.info(msg="redis lock acquired", extra={"event": "apod.lock.acquired", "dependency": "apod:redis", "apod_date": date.isoformat()})
                    # Another lock owner may have populated Redis while this request waited.
                    cached_apod = await get_cached_apod(redis_client, date)
                    if cached_apod:
                        logger.info(msg="apod retrieved", extra={"event": "apod.retrieved", "source": "redis", "apod_date": date.isoformat()})
                        return cached_apod
                else: # Another request still owns the lock, so wait for its cached result.
                    logger.info(msg="waiting for redis lock owner", extra={"event": "apod.lock.wait", "dependency": "apod:redis", "apod_date": date.isoformat()})
                    for _ in range(15):
                        await asyncio.sleep(1)
                        cached_apod = await get_cached_apod(redis_client, date)
                        if cached_apod:
                            logger.info(msg="apod retrieved", extra={"event": "apod.retrieved", "source": "redis", "apod_date": date.isoformat()})
                            return cached_apod
                    # Do not continue to Turso or NASA without owning the lock.
                    raise Error(Code.APOD_REQUEST_IN_PROGRESS)
        except RedisError:
            logger.exception(msg="redis cache operation failed", extra={"dependency": "apod:redis"})




        # 2. check database (update redis if today)
        try:
            # The synchronous Turso connection, query, and close all run in one worker thread.
            database_apod = await asyncio.to_thread(get_database_apod, date)
            if database_apod:
                logger.info(msg="apod retrieved", extra={"event": "apod.retrieved", "source": "turso", "apod_date": date.isoformat()})
                if is_today:
                    try:
                        await cache_apod(redis_client, database_apod)
                    except RedisError:
                        logger.exception(msg="redis cache write failed", extra={"dependency": "apod:redis"})

                return database_apod
        except OperationalError:
            logger.exception(msg="turso lookup failed", extra={"dependency": "apod:turso"})


        # 3. check nasa api (update database and redis if today)
        logger.info(msg="fetching apod from nasa", extra={"event": "apod.nasa.fetch", "source": "nasa", "apod_date": date.isoformat()})
        apod = await fetch_apod(date, request.app.state.http_client)
        logger.info(msg="apod retrieved", extra={"event": "apod.retrieved", "source": "nasa", "apod_date": date.isoformat()})

        try:
            # Persist through a worker thread because the Turso driver is synchronous.
            await asyncio.to_thread(save_database_apod, apod)
        except OperationalError:
            logger.exception(msg="turso write failed", extra={"dependency": "apod:turso"})

        try:
            embedding = await asyncio.to_thread(create_apod_embedding, apod)
            await asyncio.to_thread(save_database_embedding, apod.date, embedding)
            logger.info(msg="apod embedding saved", extra={"event": "apod.embedding.saved", "dependency": "apod:turso", "apod_date": date.isoformat()})
        except (OpenAIError, OperationalError, ValueError):
            # Exact-date retrieval remains available even if semantic indexing fails.
            logger.exception(msg="apod embedding failed", extra={"event": "apod.embedding.failed", "dependency": "apod:turso", "apod_date": date.isoformat()})

        if is_today:
            try:
                await cache_apod(redis_client, apod)
            except RedisError:
                logger.exception(msg="redis cache write failed", extra={"dependency": "apod:redis"})

        return apod
        # get picture by keywords (has to search vector database)
    finally:
        if get_lock:
            try:
                await release_lock(get_lock)
            except RedisError:
                logger.exception(msg="redis lock release failed", extra={"dependency": "apod:redis"})
