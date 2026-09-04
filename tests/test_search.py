import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from turso_serverless import OperationalError

from app.database import DatabaseSearchMatch
from app.errors import Code, Error
from app.main import app
from app.routers import vector
from app import search
from app.source import SourceApod


def apod(day: int, title: str) -> SourceApod:
    return SourceApod(
        date=date(2024, 1, day),
        title=title,
        explanation=f"Explanation for {title}",
        media_type="image",
        url=f"https://example.com/{day}.jpg",
    )


def match(item: SourceApod, metric: float = 0.1) -> DatabaseSearchMatch:
    return DatabaseSearchMatch(apod=item, metric=metric)


def test_normalize_search_query_collapses_whitespace() -> None:
    assert search.normalize_search_query("  black   hole \n") == "black hole"


@pytest.mark.parametrize("query", ["", "   ", "___", "!!!", "x" * 201])
def test_normalize_search_query_rejects_invalid_input(query: str) -> None:
    with pytest.raises(Error) as raised:
        search.normalize_search_query(query)

    assert raised.value.code is Code.INVALID_SEARCH_QUERY


def test_build_fts_query_uses_safe_prefix_terms() -> None:
    assert search.build_fts_query("Black holes") == '"black"* AND "holes"*'


def test_rank_fusion_prioritizes_result_found_by_both_methods() -> None:
    lexical_only = apod(1, "Lexical")
    overlap = apod(2, "Overlap")
    semantic_only = apod(3, "Semantic")

    results = search.fuse_search_results(
        [match(lexical_only), match(overlap)],
        [match(overlap), match(semantic_only)],
        limit=3,
    )

    assert results[0].date == overlap.date
    assert results[0].match_types == ["lexical", "semantic"]
    assert results[0].relevance_score == 1.0
    assert {result.date for result in results} == {
        lexical_only.date,
        overlap.date,
        semantic_only.date,
    }


def test_hybrid_search_combines_lexical_and_semantic_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = apod(1, "Solar Eclipse")
    semantic = apod(2, "Moon Shadow")
    monkeypatch.setattr(search, "search_lexical_apods", lambda _query, _limit: [match(lexical)])
    monkeypatch.setattr(search, "create_query_embedding", lambda _query: [0.0] * 3072)
    monkeypatch.setattr(search, "search_vector_apods", lambda _embedding, _limit: [match(semantic)])

    response = asyncio.run(search.hybrid_search_apods("solar eclipse", 10))

    assert response.search_mode == "hybrid"
    assert response.query == "solar eclipse"
    assert {result.date for result in response.results} == {lexical.date, semantic.date}


def test_search_degrades_to_lexical_when_embedding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = apod(1, "Solar Eclipse")
    monkeypatch.setattr(search, "search_lexical_apods", lambda _query, _limit: [match(lexical)])

    def fail_embedding(_query: str) -> list[float]:
        raise ValueError("embedding unavailable")

    monkeypatch.setattr(search, "create_query_embedding", fail_embedding)

    response = asyncio.run(search.hybrid_search_apods("solar", 10))

    assert response.search_mode == "lexical"
    assert [result.date for result in response.results] == [lexical.date]


def test_search_degrades_to_semantic_when_fts_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic = apod(2, "Moon Shadow")

    def fail_lexical(_query: str, _limit: int) -> list[DatabaseSearchMatch]:
        raise OperationalError("fts unavailable")

    monkeypatch.setattr(search, "search_lexical_apods", fail_lexical)
    monkeypatch.setattr(search, "create_query_embedding", lambda _query: [0.0] * 3072)
    monkeypatch.setattr(search, "search_vector_apods", lambda _embedding, _limit: [match(semantic)])

    response = asyncio.run(search.hybrid_search_apods("moon", 10))

    assert response.search_mode == "semantic"
    assert [result.date for result in response.results] == [semantic.date]


def test_search_returns_structured_error_when_both_methods_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_lexical(_query: str, _limit: int) -> list[DatabaseSearchMatch]:
        raise OperationalError("fts unavailable")

    def fail_embedding(_query: str) -> list[float]:
        raise ValueError("embedding unavailable")

    monkeypatch.setattr(search, "search_lexical_apods", fail_lexical)
    monkeypatch.setattr(search, "create_query_embedding", fail_embedding)

    with pytest.raises(Error) as raised:
        asyncio.run(search.hybrid_search_apods("moon", 10))

    assert raised.value.code is Code.SEARCH_UNAVAILABLE


def test_vector_search_endpoint_returns_typed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = search.ApodSearchResponse(
        query="solar",
        search_mode="hybrid",
        results=[
            search.ApodSearchResult(
                **apod(1, "Solar Eclipse").model_dump(),
                relevance_score=1.0,
                match_types=["lexical", "semantic"],
            )
        ],
    )
    mocked_search = AsyncMock(return_value=response)
    monkeypatch.setattr(vector, "hybrid_search_apods", mocked_search)

    with TestClient(app) as client:
        result = client.get("/vector/search", params={"q": "solar", "limit": 5})

    assert result.status_code == 200
    assert result.json()["results"][0]["title"] == "Solar Eclipse"
    mocked_search.assert_awaited_once_with("solar", 5)


def test_vector_search_endpoint_rejects_limit_above_fifty() -> None:
    with TestClient(app) as client:
        result = client.get("/vector/search", params={"q": "solar", "limit": 51})

    assert result.status_code == 422
