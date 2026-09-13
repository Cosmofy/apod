from datetime import date
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


# creating python object based off json structure
class SourceApod(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: date
    title: str
    explanation: str
    # Kept internally for NASA ingestion, storage, and choosing the public URL.
    # It must never leak through this service's HTTP contract.
    hdurl: str | None = Field(default=None, exclude=True)
    media_type: str
    url: str = ""
    credit: str | None = None
    copyright: str | None = None
    url_fallback: str | None = None
    source: Literal["astronomy", "earth_observatory"] = "astronomy"
    s3_object_key: str | None = Field(default=None, exclude=True)
    article_url: str | None = Field(default=None, exclude=True)


class EarthObservatoryPicture(BaseModel):
    """The daily Earth Observatory record, normalized from NASA's RSS and article."""

    date: date
    title: str
    explanation: str
    media_type: Literal["image"] = "image"
    url: str
    url_fallback: str | None = None
    credit: str | None = None
    copyright: str | None = None
    source: Literal["earth_observatory"] = "earth_observatory"
    image_date: date | None = None
    location_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    article_url: str


def load_source_apod(path: Path) -> SourceApod:
    return SourceApod.model_validate_json(
        path.read_text(encoding="utf-8")
    )
