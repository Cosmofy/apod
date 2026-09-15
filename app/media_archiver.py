"""Client for Slicer's private, durable S3 media-archiver queue."""
import asyncio
import httpx

from app.config import Settings
from app.source import EarthObservatoryPicture, SourceApod


class MediaArchiverError(RuntimeError):
    pass


def source_name(picture: SourceApod | EarthObservatoryPicture) -> str:
    return "apod" if isinstance(picture, SourceApod) else "earth_observatory"


def source_url(picture: SourceApod | EarthObservatoryPicture) -> str:
    # Archive APOD's genuine high-definition image while retaining its normal URL
    # as the client fallback in Turso.
    if isinstance(picture, SourceApod) and picture.media_type == "image" and picture.hdurl:
        return picture.hdurl
    return picture.url


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


async def archive_media(
    client: httpx.AsyncClient,
    picture: SourceApod | EarthObservatoryPicture,
    *,
    timeout_seconds: int = 1_800,
) -> str:
    """Submit then poll the sole Slicer worker; return its verified object key."""
    settings = Settings()
    if not settings.media_archiver_url or not settings.media_archiver_api_token:
        raise MediaArchiverError("media archiver is not configured")
    origin = settings.media_archiver_url.rstrip("/")
    url = source_url(picture)
    body = {"source": source_name(picture), "date": picture.date.isoformat(), "source_url": url}
    endpoint = "/jobs/youtube" if is_youtube(url) else "/jobs/media"
    if endpoint == "/jobs/media":
        body["media_type"] = picture.media_type
    headers = {"Authorization": f"Bearer {settings.media_archiver_api_token.get_secret_value()}"}
    try:
        response = await client.post(f"{origin}{endpoint}", json=body, headers=headers, timeout=15)
        response.raise_for_status()
        job = response.json()
        location = response.headers.get("Location") or f"/jobs/{job['id']}"
        job_url = f"{origin}{location}"
        for _ in range(max(1, timeout_seconds // 3)):
            if job["status"] == "succeeded":
                return str(job["object_key"])
            if job["status"] == "failed":
                raise MediaArchiverError(str(job.get("error") or "archiver job failed"))
            await asyncio.sleep(3)
            poll = await client.get(job_url, headers=headers, timeout=15)
            poll.raise_for_status()
            job = poll.json()
    except httpx.HTTPError as error:
        raise MediaArchiverError("media archiver request failed") from error
    raise MediaArchiverError("media archiver job timed out")
