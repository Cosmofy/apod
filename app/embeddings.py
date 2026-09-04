import re
import sys
from array import array
from html import unescape
from openai import OpenAI
from app.config import Settings
from app.source import SourceApod


EMBEDDING_DIMENSIONS = 3072

def clean_text(value: str | None) -> str:
    text = re.compile(r"<[^>]+>").sub(" ", value or "")
    return " ".join(unescape(text).split())

def build_embedding_text(title: str | None, explanation: str | None, credit: str | None) -> str:
    parts = [
        f"Title: {clean_text(title)}",
        f"Explanation: {clean_text(explanation)}",
    ]
    if credit: parts.append(f"Credit: {clean_text(credit)}")
    return "\n".join(parts)


def create_embeddings(texts: list[str]) -> list[list[float]]:
    settings = Settings()
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(
        model="text-embedding-3-large",
        dimensions=3072,
        input=texts,
    )
    return [
        item.embedding
        for item in sorted(response.data, key=lambda item: item.index)
    ]


def create_apod_embedding(apod: SourceApod) -> list[float]:
    text = build_embedding_text(apod.title, apod.explanation, apod.credit)
    return create_embeddings([text])[0]


def create_query_embedding(query: str) -> list[float]:
    return create_embeddings([clean_text(query)])[0]


def encode_embedding(embedding: list[float]) -> bytes:
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(embedding)}"
        )
    values = array("f", embedding)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()
