"""EO stored-vector similarity: HTTP contract, read-only SQL, and tracing."""

from datetime import date
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from turso_serverless import OperationalError, ProgrammingError

from app import database, embeddings
from app.errors import Code, Error, handle_error
from app.media import MEDIA_BASE_URL
from app.observability import log_requests, request_id_context
from app.routers import earth_observatory as eo
from app.source import EarthObservatoryPicture


SOURCE_DATE = date(2024, 1, 1)
SOURCE_VECTOR = b"eo-stored-vector-test-double"
ARCHIVE_KEY = "eo/image/" + "a" * 64 + ".jpg"


def picture(day):
    return EarthObservatoryPicture(
        date=date(2024, 1, day), title=f"Clouds {day}", explanation="Over the ocean.",
        url=f"https://nasa.example/{day}.jpg", article_url="https://science.nasa.gov/example",
        image_date=date(2023, 12, 20), latitude=10.0, longitude=-20.0,
    )


def match(day, distance):
    return database.EarthObservatorySearchMatch(picture=picture(day), score=distance)


def row(day=2, distance=0.25):
    item = picture(day)
    return (item.date.isoformat(), item.title, item.explanation, item.media_type, item.url,
            None, "NASA", None, item.article_url, item.image_date.isoformat(), None,
            item.latitude, item.longitude, ARCHIVE_KEY, distance)


def connection_with_rows(source=(SOURCE_VECTOR,), rows=None):
    connection = Mock()
    source_cursor, neighbors = Mock(), Mock()
    source_cursor.fetchone.return_value = source
    neighbors.fetchall.return_value = [row()] if rows is None else rows
    connection.execute.side_effect = [source_cursor, neighbors]
    return connection


@pytest.fixture(autouse=True)
def forbid_openai(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("similarity must not generate embeddings"))
    monkeypatch.setattr(eo, "create_query_embedding", forbidden)
    for name in ("create_query_embedding", "create_earth_observatory_embedding", "create_embeddings"):
        monkeypatch.setattr(embeddings, name, forbidden)
    yield
    forbidden.assert_not_called()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(eo.router)
    app.add_exception_handler(Error, handle_error)
    app.middleware("http")(log_requests)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("params,code", [
    ({}, Code.INVALID_DATE_FORMAT),
    ({"date": ""}, Code.INVALID_DATE_FORMAT),
    ({"date": "today"}, Code.INVALID_DATE_FORMAT),
    ({"date": "2024-1-01"}, Code.INVALID_DATE_FORMAT),
    ({"date": "2024-02-30"}, Code.INVALID_DATE_FORMAT),
    ({"date": "2024/01/01"}, Code.INVALID_DATE_FORMAT),
    *[({"date": "2024-01-01", "limit": limit}, Code.INVALID_SIMILARITY_REQUEST)
      for limit in ("0", "51", "-1", "no", "1.5", "", "1e1", "999999999999999999999")],
])
def test_validation_uses_existing_errors_before_database(client, monkeypatch, params, code):
    query = Mock()
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", query)
    response = client.get("/earth-observatory/similar", params=params)
    assert response.status_code == 422
    assert response.json() == {"error": {"code": code.name, "message": code.message}}
    query.assert_not_called()


@pytest.mark.parametrize("limit", [None, 1, 10, 50])
def test_default_and_boundary_limits(client, monkeypatch, limit):
    query = Mock(return_value=[match(2, 0.25)])
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", query)
    params = {"date": "2024-01-01"}
    if limit is not None:
        params["limit"] = limit
    response = client.get("/earth-observatory/similar", params=params)
    assert response.status_code == 200
    query.assert_called_once_with(SOURCE_DATE, 10 if limit is None else limit)
    assert response.json() == {
        "date": "2024-01-01",
        "results": [{**picture(2).model_dump(mode="json"), "relevance_score": 0.75}],
    }


def test_sorting_exclusion_unique_dates_tiebreak_and_limit(client, monkeypatch):
    query = Mock(return_value=[match(4, 0.8), match(1, 0.0), match(2, 0.6),
                              match(3, 0.1), match(2, 0.2), match(5, 0.2)])
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", query)
    response = client.get("/earth-observatory/similar?date=2024-01-01&limit=3")
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["date"] for item in results] == ["2024-01-03", "2024-01-05", "2024-01-02"]
    assert [item["relevance_score"] for item in results] == pytest.approx([0.9, 0.8, 0.8])
    public_fields = {name for name, field in EarthObservatoryPicture.model_fields.items() if not field.exclude}
    assert all(set(item) == public_fields | {"relevance_score"} for item in results)


@pytest.mark.parametrize("distance,score", [(-0.000001, 1.0), (0, 1.0), (0.25, 0.75), (1, 0), (1.8, 0)])
def test_score_is_bounded_one_minus_cosine_distance(client, monkeypatch, distance, score):
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", Mock(return_value=[match(2, distance)]))
    response = client.get("/earth-observatory/similar?date=2024-01-01")
    assert response.status_code == 200
    assert response.json()["results"][0]["relevance_score"] == score


@pytest.mark.parametrize("matches", [[], [match(1, 0.0)]])
def test_empty_success(client, monkeypatch, matches):
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", Mock(return_value=matches))
    response = client.get("/earth-observatory/similar?date=2024-01-01")
    assert response.status_code == 200
    assert response.json() == {"date": "2024-01-01", "results": []}


@pytest.mark.parametrize("failure,code", [
    (Error(Code.EARTH_OBSERVATORY_NOT_FOUND), Code.EARTH_OBSERVATORY_NOT_FOUND),
    (Error(Code.SIMILARITY_UNAVAILABLE), Code.SIMILARITY_UNAVAILABLE),
    (OperationalError("offline"), Code.SIMILARITY_UNAVAILABLE),
    (ProgrammingError("index missing"), Code.SIMILARITY_UNAVAILABLE),
    (TimeoutError("timeout"), Code.SIMILARITY_UNAVAILABLE),
    (ValueError("invalid vector"), Code.SIMILARITY_UNAVAILABLE),
    (TypeError("invalid distance"), Code.SIMILARITY_UNAVAILABLE),
])
def test_errors_and_request_correlation(client, monkeypatch, failure, code):
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", Mock(side_effect=failure))
    response = client.get("/earth-observatory/similar?date=2024-01-01", headers={"x-request-id": "eo-similarity"})
    assert response.status_code == code.status
    assert response.json() == {"error": {"code": code.name, "message": code.message}}
    assert response.headers["x-request-id"] == "eo-similarity"


@pytest.mark.parametrize("distance", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_distances_fail_closed(client, monkeypatch, distance):
    monkeypatch.setattr(eo, "find_similar_earth_observatory_pictures", Mock(return_value=[match(2, distance)]))
    response = client.get("/earth-observatory/similar?date=2024-01-01")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SIMILARITY_UNAVAILABLE"


def test_openapi_contract(client):
    operation = client.get("/openapi.json").json()["paths"]["/earth-observatory/similar"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["date"]["required"] is True
    assert params["limit"]["schema"]["default"] == 10
    assert params["limit"]["schema"]["minimum"] == 1
    assert params["limit"]["schema"]["maximum"] == 50


@pytest.mark.parametrize("limit", [1, 10, 50])
def test_database_stored_vector_overfetch_exclusion_projection_and_read_only(monkeypatch, limit):
    connection = connection_with_rows()
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    results = database.find_similar_earth_observatory_pictures(SOURCE_DATE, limit)
    source_sql, source_params = connection.execute.call_args_list[0].args
    match_sql, match_params = connection.execute.call_args_list[1].args
    assert "LEFT JOIN earth_observatory_embeddings" in source_sql
    assert source_params == ("2024-01-01",)
    assert "vector_top_k('earth_observatory_embeddings_vector_idx'" in match_sql
    assert "vector_distance_cos" in match_sql
    assert "WHERE p.date != ?" in match_sql
    assert "ORDER BY distance ASC, p.date DESC" in match_sql
    assert "LIMIT ?" in match_sql
    assert match_params == (SOURCE_VECTOR, SOURCE_VECTOR, limit + 1, "2024-01-01", limit)
    assert results[0].score == 0.25
    assert results[0].picture.url == f"{MEDIA_BASE_URL}/{ARCHIVE_KEY}"
    assert results[0].picture.url_fallback == picture(2).url
    connection.close.assert_called_once()
    connection.commit.assert_not_called()


@pytest.mark.parametrize("source,code", [(None, Code.EARTH_OBSERVATORY_NOT_FOUND), ((None,), Code.SIMILARITY_UNAVAILABLE)])
def test_missing_source_and_missing_vector_through_http(client, monkeypatch, source, code):
    connection = connection_with_rows(source=source)
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    response = client.get("/earth-observatory/similar?date=2024-01-01")
    assert response.status_code == code.status
    assert response.json()["error"]["code"] == code.name
    assert connection.execute.call_count == 1
    connection.close.assert_called_once()


def test_database_empty_candidates(monkeypatch):
    connection = connection_with_rows(rows=[])
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    assert database.find_similar_earth_observatory_pictures(SOURCE_DATE, 10) == []
    connection.close.assert_called_once()


@pytest.mark.parametrize("stage", ["source", "neighbors", "mapping"])
def test_connection_closes_on_failures(monkeypatch, stage):
    connection = connection_with_rows()
    if stage == "source":
        connection.execute.side_effect = OperationalError("offline")
    elif stage == "neighbors":
        connection.execute.side_effect = [Mock(**{"fetchone.return_value": (SOURCE_VECTOR,)}), OperationalError("offline")]
    else:
        connection = connection_with_rows(rows=[row(distance="bad distance")])
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    with pytest.raises((OperationalError, ValueError)):
        database.find_similar_earth_observatory_pictures(SOURCE_DATE, 10)
    connection.close.assert_called_once()


def test_database_to_http_preserves_s3_and_eo_metadata(client, monkeypatch):
    connection = connection_with_rows()
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    response = client.get("/earth-observatory/similar?date=2024-01-01")
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["source"] == "earth_observatory"
    assert item["url"] == f"{MEDIA_BASE_URL}/{ARCHIVE_KEY}"
    assert item["url_fallback"] == picture(2).url
    assert item["article_url"] == picture(2).article_url
    assert item["image_date"] == "2023-12-20"
    assert (item["latitude"], item["longitude"]) == (10.0, -20.0)
    assert item["relevance_score"] == 0.75
    assert "s3_object_key" not in item


def test_trace_and_request_context_reach_database_thread(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tests.eo.similarity")
    monkeypatch.setattr(eo, "tracer", tracer)
    monkeypatch.setattr(database, "tracer", tracer)
    observed = []

    def connect():
        observed.append(request_id_context.get())
        return connection_with_rows()

    monkeypatch.setattr(database, "connect_database", connect)
    app = FastAPI()
    app.include_router(eo.router)
    app.add_exception_handler(Error, handle_error)
    app.middleware("http")(log_requests)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        with TestClient(app) as client:
            response = client.get("/earth-observatory/similar?date=2024-01-01", headers={
                "x-request-id": "eo-similar-trace",
                "traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01",
            })
        assert response.status_code == 200
        assert observed == ["eo-similar-trace"]
        spans = exporter.get_finished_spans()
        server = next(s for s in spans if s.kind is SpanKind.SERVER)
        pipeline = next(s for s in spans if s.name == "earth_observatory.similar")
        db_span = next(s for s in spans if s.name == "turso.earth_observatory.similar")
        assert server.parent.span_id == int("1234567890abcdef", 16)
        assert pipeline.parent.span_id == server.context.span_id
        assert db_span.parent.span_id == pipeline.context.span_id
        assert db_span.kind is SpanKind.CLIENT
        assert all(s.context.trace_id == int("1234567890abcdef1234567890abcdef", 16) for s in (server, pipeline, db_span))
        assert SOURCE_VECTOR.decode() not in repr([dict(s.attributes) for s in spans])
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
