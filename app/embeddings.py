import re
from html import unescape
from openai import OpenAI
from app.config import Settings

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
