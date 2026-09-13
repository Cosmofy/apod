"""Project verified archive locations onto responses, never onto source records."""
import re

from app.source import SourceApod

MEDIA_BASE_URL = "https://cosmofy-apod-hd-010025084205-eu-west-2.s3.eu-west-2.amazonaws.com"


def with_media_urls(apod: SourceApod) -> SourceApod:
    key = apod.s3_object_key
    verified_archive = (
        key
        and re.fullmatch(r"hd/(image|video)/[0-9a-f]{64}\.[a-z0-9]+", key)
        and key.startswith(f"hd/{apod.media_type}/")
    )

    # Public contract: one primary URL and one optional fallback. `hdurl` remains
    # private source metadata, so consumers never need to choose between three URLs.
    if verified_archive:
        url = f"{MEDIA_BASE_URL}/{key}"
        fallback = apod.hdurl or apod.url or None
    elif apod.media_type == "image" and apod.hdurl and apod.hdurl != apod.url:
        url = apod.hdurl
        fallback = apod.url or None
    else:
        # Embedded/video APOD hdurl values may be a thumbnail, not playable media.
        url = apod.url
        fallback = None

    return apod.model_copy(update={
        "url": url,
        "url_fallback": fallback if fallback != url else None,
    })
