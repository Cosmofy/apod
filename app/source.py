from datetime import date
from pathlib import Path
from pydantic import BaseModel, ConfigDict


# creating python object based off json structure
class SourceApod(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: date
    title: str = ""
    explanation: str = ""
    hdurl: str | None = None
    media_type: str | None = None
    url: str | None = None
    credit: str | None = None
    copyright: str | None = None


def load_source_apod(path: Path) -> SourceApod:
    return SourceApod.model_validate_json(
        path.read_text(encoding="utf-8")
    )