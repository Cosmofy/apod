"""Local HTTP contract checks; no live NASA, OpenAI, Redis, or Turso calls."""

from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAIError
from turso_serverless import OperationalError

from app.errors import Error, handle_error
from app.routers import earth_observatory as eo
from app.source import EarthObservatoryPicture


def match(day: int, score: float = 0.0) -> eo.EarthObservatorySearchMatch:
    return eo.EarthObservatorySearchMatch(
        picture=EarthObservatoryPicture(
            date=date(2024, 1, 1) + timedelta(days=day),
            title=f"Clouds {day}", explanation="Clouds over the ocean.",
            url="https://example.com/clouds.jpg",
            article_url="https://science.nasa.gov/example",
        ),
        score=score,
    )


@pytest.fixture
def harness(monkeypatch):
    # Mount the production router and error handler without dependency lifespan.
    app = FastAPI()
    app.include_router(eo.router)
    app.add_exception_handler(Error, handle_error)
    lexical = Mock(return_value=[match(1)])
    embedding = Mock(return_value=[0.1] * 3072)
    semantic = Mock(return_value=[match(2)])
    monkeypatch.setattr(eo, "search_earth_observatory_pictures", lexical)
    monkeypatch.setattr(eo, "create_query_embedding", embedding)
    monkeypatch.setattr(eo, "search_vector_earth_observatory_pictures", semantic)
    with TestClient(app) as client:
        yield client, lexical, embedding, semantic


@pytest.mark.parametrize("failure", [OpenAIError("offline"), ValueError("bad vector")])
def test_embedding_failure_falls_back_to_lexical(harness, failure):
    client, lexical, embedding, semantic = harness
    embedding.side_effect = failure
    response = client.get("/earth-observatory/search", params={"q": "clouds"})
    assert response.status_code == 200
    assert response.json()["search_mode"] == "lexical"
    assert [r["date"] for r in response.json()["results"]] == ["2024-01-02"]
    assert response.json()["results"][0]["relevance_score"] == 1.0
    lexical.assert_called_once()
    semantic.assert_not_called()


def test_vector_database_failure_falls_back_to_lexical(harness):
    client, _, _, semantic = harness
    semantic.side_effect = OperationalError("index unavailable")
    response = client.get("/earth-observatory/search", params={"q": "clouds"})
    assert response.status_code == 200
    assert response.json()["search_mode"] == "lexical"
    assert len(response.json()["results"]) == 1


def test_lexical_database_failure_falls_back_to_semantic(harness):
    client, lexical, _, _ = harness
    lexical.side_effect = OperationalError("lexical unavailable")
    response = client.get("/earth-observatory/search", params={"q": "clouds"})
    assert response.status_code == 200
    assert response.json()["search_mode"] == "semantic"
    assert [r["date"] for r in response.json()["results"]] == ["2024-01-03"]


@pytest.mark.parametrize("failed_stage", ["embedding", "vector"])
def test_both_channels_unavailable_returns_service_error(harness, failed_stage):
    client, lexical, embedding, semantic = harness
    lexical.side_effect = OperationalError("lexical unavailable")
    if failed_stage == "embedding":
        embedding.side_effect = OpenAIError("offline")
    else:
        semantic.side_effect = OperationalError("index unavailable")
    response = client.get("/earth-observatory/search", params={"q": "clouds"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EARTH_OBSERVATORY_SEARCH_UNAVAILABLE"


@pytest.mark.parametrize("stage", ["lexical", "embedding", "semantic"])
def test_unexpected_errors_are_not_disguised_as_degraded_success(harness, stage):
    client, lexical, embedding, semantic = harness
    {"lexical": lexical, "embedding": embedding, "semantic": semantic}[stage].side_effect = RuntimeError("bug")
    with pytest.raises(RuntimeError, match="bug"):
        client.get("/earth-observatory/search", params={"q": "clouds"})


@pytest.mark.parametrize("mode", ["hybrid", "lexical", "semantic"])
def test_successful_empty_search_is_not_unavailable(harness, mode):
    client, lexical, embedding, semantic = harness
    lexical.return_value = []
    semantic.return_value = []
    if mode == "lexical":
        embedding.side_effect = OpenAIError("offline")
    elif mode == "semantic":
        lexical.side_effect = OperationalError("offline")
    response = client.get("/earth-observatory/search", params={"q": "clouds"})
    assert response.status_code == 200
    assert response.json() == {"query": "clouds", "search_mode": mode, "results": []}


@pytest.mark.parametrize("limit,candidates", [(None, 50), (1, 30), (7, 35), (20, 100), (50, 100)])
def test_normalization_candidate_budget_and_final_limit(harness, limit, candidates):
    client, lexical, embedding, semantic = harness
    lexical.return_value = [match(day) for day in range(100)]
    semantic.return_value = list(reversed(lexical.return_value))
    params = {"q": "  ocean\t clouds\n "}
    if limit is not None:
        params["limit"] = limit
    response = client.get("/earth-observatory/search", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "ocean clouds"
    assert len(body["results"]) == (10 if limit is None else limit)
    assert len({r["date"] for r in body["results"]}) == len(body["results"])
    scores = [r["relevance_score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0
    assert all(0 <= score <= 1 for score in scores)
    lexical.assert_called_once_with("ocean clouds", candidates)
    embedding.assert_called_once_with("ocean clouds")
    semantic.assert_called_once_with(embedding.return_value, candidates)


@pytest.mark.parametrize("params,custom_error", [
    ({}, False), ({"q": ""}, False), ({"q": "x" * 201}, False),
    ({"q": " \t\n "}, True), ({"q": "___!?"}, True),
    ({"q": "clouds", "limit": 0}, False),
    ({"q": "clouds", "limit": 51}, False),
    ({"q": "clouds", "limit": "1.5"}, False),
    ({"q": "clouds", "limit": "abc"}, False),
])
def test_invalid_input_is_rejected_before_dependencies(harness, params, custom_error):
    client, lexical, embedding, semantic = harness
    response = client.get("/earth-observatory/search", params=params)
    assert response.status_code == 422
    if custom_error:
        assert response.json()["error"]["code"] == "INVALID_SEARCH_QUERY"
    else:
        assert "detail" in response.json()
    for dependency in (lexical, embedding, semantic):
        dependency.assert_not_called()


@pytest.mark.parametrize("query", ["x" * 200, "地球", "123"])
def test_searchable_boundary_and_unicode_queries(harness, query):
    client, lexical, _, _ = harness
    assert client.get("/earth-observatory/search", params={"q": query}).status_code == 200
    lexical.assert_called_once_with(query, 50)


def test_fusion_deduplicates_dates_and_uses_weighted_ranks_not_raw_scores():
    shared, lexical_only, semantic_only = match(0, 999), match(1, 1000), match(2, -100)
    results = eo.fuse_earth_observatory_matches(
        [lexical_only, shared], [semantic_only, shared], 3,
    )
    assert [r.date for r in results] == [shared.picture.date, semantic_only.picture.date, lexical_only.picture.date]
    assert [r.relevance_score for r in results] == pytest.approx([
        1.0, round((0.55 / 61) / (1 / 62), 6), round((0.45 / 61) / (1 / 62), 6),
    ])
    assert eo.fuse_earth_observatory_matches([], [], 10) == []


def test_equal_fusion_scores_use_newest_date_as_tiebreaker():
    # These ranks tie exactly, including floating-point representation.
    lexical = [match(day) for day in range(12)]
    semantic = [match(day + 100) for day in range(28)]
    assert 0.45 / 72 == 0.55 / 88
    results = eo.fuse_earth_observatory_matches(lexical, semantic, 50)
    dates = [r.date for r in results]
    assert dates.index(semantic[-1].picture.date) < dates.index(lexical[-1].picture.date)
