"""Private service-level renewal. Never expose this router through the public Caddy site."""
import asyncio
import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from openai import OpenAIError
from redis import RedisError
from turso_serverless import OperationalError

from app.cache import cache_apod, cache_earth_observatory
from app.config import Settings
from app.database import (
    save_apod_archive_key, save_database_apod, save_database_embedding,
    save_earth_observatory_archive_key, save_earth_observatory_embedding,
    save_earth_observatory_picture,
)
from app.earth_observatory import fetch_earth_observatory_picture
from app.embeddings import create_apod_embedding, create_earth_observatory_embedding
from app.media_archiver import MediaArchiverError, archive_media, is_youtube
from app.nasa import fetch_apod, resolve_date


router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def authorize(value: str | None) -> None:
    token = Settings().pictures_internal_token
    if token is None or value is None or not secrets.compare_digest(value, token.get_secret_value()):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED"})


def with_youtube_marker(picture, original_url: str):
    if not is_youtube(original_url):
        return picture
    marker = f"cosmofy:youtube:{original_url}"
    if marker in picture.explanation:
        return picture
    return picture.model_copy(update={"explanation": f"{picture.explanation}\n\n{marker}"})


def save_apod_embedding_for(apod) -> None:
    save_database_embedding(apod.date, create_apod_embedding(apod))


def save_earth_embedding_for(picture) -> None:
    save_earth_observatory_embedding(picture.date, create_earth_observatory_embedding(picture))


@router.post("/jobs/renew-pictures")
async def renew_pictures(
    request: Request,
    x_pictures_internal_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Run only from Warmer's durable job; it may wait for Slicer archiving."""
    authorize(x_pictures_internal_token)
    current_date = resolve_date()
    apod, earth = await asyncio.gather(
        fetch_apod(current_date, request.app.state.http_client),
        fetch_earth_observatory_picture(request.app.state.http_client),
    )
    try:
        original_apod_url, original_earth_url = apod.url, earth.url
        apod_key, earth_key = await asyncio.gather(
            archive_media(request.app.state.http_client, apod),
            archive_media(request.app.state.http_client, earth),
        )
        apod = with_youtube_marker(apod.model_copy(update={"s3_object_key": apod_key}), original_apod_url)
        earth = with_youtube_marker(earth.model_copy(update={"s3_object_key": earth_key}), original_earth_url)
        await asyncio.gather(
            asyncio.to_thread(save_database_apod, apod),
            asyncio.to_thread(save_earth_observatory_picture, earth),
        )
        await asyncio.gather(
            asyncio.to_thread(save_apod_archive_key, apod.date, apod_key),
            asyncio.to_thread(save_earth_observatory_archive_key, earth.date, earth_key),
        )
        await asyncio.gather(
            asyncio.to_thread(save_apod_embedding_for, apod),
            asyncio.to_thread(save_earth_embedding_for, earth),
        )
        await asyncio.gather(
            cache_apod(request.app.state.redis_client, apod),
            cache_earth_observatory(request.app.state.redis_client, earth),
        )
    except (MediaArchiverError, OpenAIError, OperationalError, RedisError, OSError, ValueError) as error:
        raise HTTPException(status_code=503, detail={"code": "RENEWAL_UNAVAILABLE"}) from error
    return {
        "status": "succeeded",
        "astronomy_date": apod.date.isoformat(), "astronomy_object_key": apod_key,
        "earth_observatory_date": earth.date.isoformat(), "earth_observatory_object_key": earth_key,
    }
