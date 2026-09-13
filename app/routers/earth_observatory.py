import asyncio
import logging
import math
import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from opentelemetry import trace
from redis import RedisError
from turso_serverless import OperationalError
from turso_serverless import Error as DatabaseError
from openai import OpenAIError

from app.cache import (
    acquire_earth_observatory_lock,
    cache_earth_observatory,
    get_cached_earth_observatory,
    release_lock,
)
from app.database import (
    EarthObservatorySearchMatch,
    find_similar_earth_observatory_pictures,
    get_earth_observatory_picture,
    save_earth_observatory_picture,
    search_earth_observatory_pictures,
    search_vector_earth_observatory_pictures,
)
from app.earth_observatory import fetch_earth_observatory_picture
from app.embeddings import create_query_embedding
from app.errors import Code, Error
from app.source import EarthObservatoryPicture
from app.routers.vector import SimilarityRoute
from pydantic import BaseModel, Field

router = APIRouter(prefix="/earth-observatory", tags=["earth-observatory"])
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class EarthObservatorySearchResult(EarthObservatoryPicture):
    relevance_score: float = Field(ge=0.0, le=1.0)


class EarthObservatorySearchResponse(BaseModel):
    query: str
    search_mode: str
    results: list[EarthObservatorySearchResult]


class EarthObservatorySimilarityResponse(BaseModel):
    date: date
    results: list[EarthObservatorySearchResult]


def resolve_earth_observatory_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Error(Code.INVALID_DATE_FORMAT) from None


def fuse_earth_observatory_matches(
    lexical_matches: list[EarthObservatorySearchMatch],
    semantic_matches: list[EarthObservatorySearchMatch],
    limit: int,
) -> list[EarthObservatorySearchResult]:
    fused: dict[str, tuple[EarthObservatoryPicture, float]] = {}

    for rank, match in enumerate(lexical_matches, 1):
        key = match.picture.date.isoformat()
        picture, score = fused.get(key, (match.picture, 0.0))
        fused[key] = (picture, score + 0.45 / (60 + rank))

    for rank, match in enumerate(semantic_matches, 1):
        key = match.picture.date.isoformat()
        picture, score = fused.get(key, (match.picture, 0.0))
        fused[key] = (picture, score + 0.55 / (60 + rank))

    ordered = sorted(
        fused.values(),
        key=lambda item: (item[1], item[0].date),
        reverse=True,
    )[:limit]
    maximum_score = ordered[0][1] if ordered else 1.0
    return [
        EarthObservatorySearchResult(
            **picture.model_dump(),
            relevance_score=round(score / maximum_score, 6),
        )
        for picture, score in ordered
    ]


@router.get("/search", response_model=EarthObservatorySearchResponse, summary="search Earth Observatory Image of the Day archive")
async def search_earth_observatory(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> EarthObservatorySearchResponse:
    normalized_query = " ".join(q.split())
    if not normalized_query or not re.search(r"[^\W_]", normalized_query, re.UNICODE):
        raise Error(Code.INVALID_SEARCH_QUERY)
    candidate_limit = min(100, max(30, limit * 5))
    lexical_result, embedding_result = await asyncio.gather(
        asyncio.to_thread(search_earth_observatory_pictures, normalized_query, candidate_limit),
        asyncio.to_thread(create_query_embedding, normalized_query),
        return_exceptions=True,
    )

    lexical_matches: list[EarthObservatorySearchMatch] = []
    semantic_matches: list[EarthObservatorySearchMatch] = []
    lexical_available = not isinstance(lexical_result, BaseException)
    semantic_available = not isinstance(embedding_result, BaseException)

    if lexical_available:
        lexical_matches = lexical_result
    elif isinstance(lexical_result, OperationalError):
        logger.exception("earth observatory lexical search failed", extra={"event": "earth_observatory.search.lexical.failed", "dependency": "earth-observatory:turso"})
    else:
        raise lexical_result

    if semantic_available:
        try:
            semantic_matches = await asyncio.to_thread(search_vector_earth_observatory_pictures, embedding_result, candidate_limit)
        except OperationalError:
            semantic_available = False
            logger.exception("earth observatory semantic search failed", extra={"event": "earth_observatory.search.semantic.failed", "dependency": "earth-observatory:turso"})
    elif isinstance(embedding_result, (OpenAIError, ValueError)):
        logger.exception("earth observatory query embedding failed", extra={"event": "earth_observatory.search.embedding.failed", "dependency": "earth-observatory:openai"})
    else:
        raise embedding_result

    if not lexical_available and not semantic_available:
        raise Error(Code.EARTH_OBSERVATORY_SEARCH_UNAVAILABLE)

    if lexical_available and semantic_available:
        search_mode = "hybrid"
    elif lexical_available:
        search_mode = "lexical"
    else:
        search_mode = "semantic"

    return EarthObservatorySearchResponse(
        query=normalized_query,
        search_mode=search_mode,
        results=fuse_earth_observatory_matches(lexical_matches, semantic_matches, limit),
    )


similarity_router = APIRouter(route_class=SimilarityRoute)


@similarity_router.get(
    "/similar",
    response_model=EarthObservatorySimilarityResponse,
    summary="find Earth Observatory pictures similar to a stored publication",
)
async def similar_earth_observatory(
    date: Annotated[str, Query(description="Source EO publication date in YYYY-MM-DD format.")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> EarthObservatorySimilarityResponse:
    requested_date = resolve_earth_observatory_date(date)
    with tracer.start_as_current_span("earth_observatory.similar") as span:
        span.set_attribute("earth_observatory.similarity.requested_limit", limit)
        try:
            matches = await asyncio.to_thread(find_similar_earth_observatory_pictures, requested_date, limit)
            if any(not math.isfinite(match.score) for match in matches):
                raise ValueError("invalid cosine distance")

            seen = {requested_date}
            results = []
            for match in sorted(matches, key=lambda item: (item.score, -item.picture.date.toordinal())):
                if match.picture.date in seen:
                    continue
                seen.add(match.picture.date)
                results.append(EarthObservatorySearchResult(
                    **match.picture.model_dump(),
                    relevance_score=max(0.0, min(1.0, 1.0 - match.score)),
                ))
                if len(results) == limit:
                    break
        except (DatabaseError, OSError, ValueError, TypeError) as error:
            logger.exception(
                "earth observatory similarity unavailable",
                extra={"event": "earth_observatory.similarity.failed", "dependency": "earth-observatory:turso"},
            )
            raise Error(Code.SIMILARITY_UNAVAILABLE) from error

        span.set_attribute("earth_observatory.similarity.result_count", len(results))
        logger.info(
            "earth observatory similarity completed",
            extra={"event": "earth_observatory.similarity.completed", "source": "turso"},
        )
        return EarthObservatorySimilarityResponse(date=requested_date, results=results)


router.include_router(similarity_router)


@router.get("", response_model=EarthObservatoryPicture, summary="retrieve an Earth Observatory Image of the Day")
async def get_earth_observatory(request: Request, date: str | None = None) -> EarthObservatoryPicture:
    if date is not None:
        requested_date = resolve_earth_observatory_date(date)
        try:
            stored = await asyncio.to_thread(get_earth_observatory_picture, requested_date)
        except OperationalError:
            logger.exception("earth observatory historical lookup failed", extra={"event": "earth_observatory.lookup.failed", "dependency": "earth-observatory:turso"})
            raise Error(Code.EARTH_OBSERVATORY_UNAVAILABLE) from None
        if stored is None:
            raise Error(Code.EARTH_OBSERVATORY_NOT_FOUND)
        return stored

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
                    picture = stored
                else:
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
