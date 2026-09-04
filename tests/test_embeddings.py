from datetime import date

import pytest

from app import embeddings
from app.source import SourceApod


def test_create_one_apod_embedding_uses_its_searchable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_create_embeddings(texts: list[str]) -> list[list[float]]:
        captured.extend(texts)
        return [[0.25] * embeddings.EMBEDDING_DIMENSIONS]

    monkeypatch.setattr(embeddings, "create_embeddings", fake_create_embeddings)
    apod = SourceApod(
        date=date(2026, 9, 3),
        title="Saturn's Rings",
        explanation="A view of Saturn.",
        media_type="image",
        url="https://example.com/saturn.jpg",
        credit="Example Observatory",
    )

    result = embeddings.create_apod_embedding(apod)

    assert len(result) == embeddings.EMBEDDING_DIMENSIONS
    assert captured == [
        "Title: Saturn's Rings\n"
        "Explanation: A view of Saturn.\n"
        "Credit: Example Observatory"
    ]


def test_encode_embedding_produces_3072_float32_values() -> None:
    encoded = embeddings.encode_embedding(
        [0.0] * embeddings.EMBEDDING_DIMENSIONS
    )

    assert len(encoded) == embeddings.EMBEDDING_DIMENSIONS * 4


def test_encode_embedding_rejects_wrong_dimensions() -> None:
    with pytest.raises(ValueError, match="expected 3072 dimensions"):
        embeddings.encode_embedding([0.0])
