from datetime import date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
import pytest
from turso_serverless import OperationalError, ProgrammingError

from app import database, similarity
from app.database import DatabaseSearchMatch
from app.errors import Code, Error, handle_error
from app.main import app
from app.observability import log_requests, request_id_context
from app.routers import vector
from app.source import SourceApod


SOURCE_DATE = date(2024, 1, 1)
SOURCE_VECTOR = b"stored-vector-test-double"


def apod(day: int) -> SourceApod:
    return SourceApod(
        date=date(2024, 1, day),
        title=f"Galaxy {day}",
        explanation="A spiral galaxy.",
        url=f"https://example.com/{day}.jpg",
        hdurl=f"https://example.com/{day}-hd.jpg",
        media_type="image",
        credit="Example credit",
        copyright="Example copyright",
    )


def match(day: int, distance: float) -> DatabaseSearchMatch:
    return DatabaseSearchMatch(apod=apod(day), metric=distance)


def database_row(day: int, distance: float) -> tuple:
    item = apod(day)
    return (
        item.date.isoformat(), item.title, item.explanation, item.url,
        item.hdurl, item.media_type, item.credit, item.copyright, distance,
    )


@pytest.fixture
def client():
    # Without the context manager, no application lifespan is started. These
    # tests mock the database and do not need external dependency clients.
    return TestClient(app)


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, Code.INVALID_DATE_FORMAT),
        ({"date": ""}, Code.INVALID_DATE_FORMAT),
        ({"date": "today"}, Code.INVALID_DATE_FORMAT),
        ({"date": "2024-1-01"}, Code.INVALID_DATE_FORMAT),
        ({"date": "2024-02-30"}, Code.INVALID_DATE_FORMAT),
        ({"date": "2024/01/01"}, Code.INVALID_DATE_FORMAT),
        ({"date": "1995-06-15"}, Code.DATE_TOO_EARLY),
        ({"date": "2999-01-01"}, Code.DATE_IN_FUTURE),
    ],
)
def test_date_validation_uses_existing_errors(client, monkeypatch, params, code):
    query = Mock(side_effect=AssertionError("must validate before reading database"))
    monkeypatch.setattr(similarity, "find_similar_database_apods", query)
    response = client.get("/vector/similar", params=params, headers={"x-request-id": "similar-invalid-date"})
    assert response.status_code == code.status
    assert response.json() == {"error": {"code": code.name, "message": code.message}}
    assert response.headers["x-request-id"] == "similar-invalid-date"
    query.assert_not_called()


@pytest.mark.parametrize("limit", ["0", "51", "-1", "no", "1.5", "", "1e1", "999999999999999999999"])
def test_invalid_limit_uses_similarity_error(client, monkeypatch, limit):
    query = Mock(side_effect=AssertionError("must validate before reading database"))
    monkeypatch.setattr(similarity, "find_similar_database_apods", query)
    response = client.get("/vector/similar", params={"date": "2024-01-01", "limit": limit})
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "INVALID_SIMILARITY_REQUEST",
            "message": Code.INVALID_SIMILARITY_REQUEST.message,
        }
    }
    query.assert_not_called()


@pytest.mark.parametrize("limit", [None, 1, 10, 50])
def test_default_and_boundary_limits(client, monkeypatch, limit):
    query = Mock(return_value=[match(2, 0.25)])
    monkeypatch.setattr(similarity, "find_similar_database_apods", query)
    params = {"date": "2024-01-01"}
    if limit is not None:
        params["limit"] = str(limit)
    response = client.get("/vector/similar", params=params)
    assert response.status_code == 200
    query.assert_called_once_with(SOURCE_DATE, limit if limit is not None else 10)
    assert response.json() == {
        "date": "2024-01-01",
        "results": [{**apod(2).model_dump(mode="json"), "relevance_score": 0.75}],
    }


@pytest.mark.parametrize("source_date", ["1995-06-16", datetime.now(ZoneInfo("America/Denver")).date().isoformat()])
def test_archive_start_and_today_are_valid(client, monkeypatch, source_date):
    query = Mock(return_value=[])
    monkeypatch.setattr(similarity, "find_similar_database_apods", query)
    response = client.get("/vector/similar", params={"date": source_date})
    assert response.status_code == 200
    assert response.json() == {"date": source_date, "results": []}
    query.assert_called_once_with(date.fromisoformat(source_date), 10)


def test_ranking_excludes_source_deduplicates_dates_and_applies_limit(client, monkeypatch):
    monkeypatch.setattr(
        similarity, "find_similar_database_apods",
        lambda *_: [match(4, 0.8), match(1, 0.0), match(2, 0.6), match(3, 0.1), match(2, 0.2), match(5, 0.3)],
    )
    response = client.get("/vector/similar", params={"date": "2024-01-01", "limit": 3})
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["date"] for item in results] == ["2024-01-03", "2024-01-02", "2024-01-05"]
    assert [item["relevance_score"] for item in results] == pytest.approx([0.9, 0.8, 0.7])
    assert all(set(item) == set(SourceApod.model_fields) | {"relevance_score"} for item in results)


@pytest.mark.parametrize(("distance", "score"), [(-0.000001, 1.0), (0.0, 1.0), (0.25, 0.75), (1.0, 0.0), (1.8, 0.0)])
def test_cosine_score_formula(client, monkeypatch, distance, score):
    monkeypatch.setattr(similarity, "find_similar_database_apods", lambda *_: [match(2, distance)])
    response = client.get("/vector/similar", params={"date": "2024-01-01"})
    assert response.status_code == 200
    assert response.json()["results"][0]["relevance_score"] == score


@pytest.mark.parametrize("matches", [[], [match(1, 0.0)]])
def test_empty_results_are_successful(client, monkeypatch, matches):
    monkeypatch.setattr(similarity, "find_similar_database_apods", lambda *_: matches)
    response = client.get("/vector/similar", params={"date": "2024-01-01"})
    assert response.status_code == 200
    assert response.json() == {"date": "2024-01-01", "results": []}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (Error(Code.NOT_FOUND), Code.NOT_FOUND),
        (Error(Code.SIMILARITY_UNAVAILABLE), Code.SIMILARITY_UNAVAILABLE),
        (OperationalError("database offline"), Code.SIMILARITY_UNAVAILABLE),
        (ProgrammingError("index missing"), Code.SIMILARITY_UNAVAILABLE),
        (TimeoutError("database timed out"), Code.SIMILARITY_UNAVAILABLE),
        (ValueError("invalid stored vector"), Code.SIMILARITY_UNAVAILABLE),
    ],
)
def test_failures_preserve_error_envelope_and_request_id(client, monkeypatch, failure, code):
    monkeypatch.setattr(similarity, "find_similar_database_apods", Mock(side_effect=failure))
    response = client.get("/vector/similar", params={"date": "2024-01-01"}, headers={"x-request-id": "similar-failure"})
    assert response.status_code == code.status
    assert response.json() == {"error": {"code": code.name, "message": code.message}}
    assert response.headers["x-request-id"] == "similar-failure"


@pytest.mark.parametrize("distance", [float("nan"), float("inf"), float("-inf")])
def test_invalid_distances_return_503(client, monkeypatch, distance):
    monkeypatch.setattr(similarity, "find_similar_database_apods", lambda *_: [match(2, distance)])
    response = client.get("/vector/similar", params={"date": "2024-01-01"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SIMILARITY_UNAVAILABLE"


def test_openapi_requires_date_and_documents_limit_range():
    schema = app.openapi()["paths"]["/vector/similar"]["get"]
    params = {param["name"]: param for param in schema["parameters"]}
    assert params["date"]["required"] is True
    assert params["limit"]["schema"]["default"] == 10
    assert params["limit"]["schema"]["minimum"] == 1
    assert params["limit"]["schema"]["maximum"] == 50


def connection_with_rows(source=(SOURCE_VECTOR,), rows=None):
    connection = Mock()
    source_cursor = Mock()
    source_cursor.fetchone.return_value = source
    match_cursor = Mock()
    match_cursor.fetchall.return_value = rows if rows is not None else [database_row(2, 0.25)]
    connection.execute.side_effect = [source_cursor, match_cursor]
    return connection


@pytest.mark.parametrize("limit", [1, 10, 50])
def test_database_uses_stored_vector_excludes_source_and_overfetches(monkeypatch, limit):
    connection = connection_with_rows()
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    results = database.find_similar_database_apods(SOURCE_DATE, limit)
    source_sql, source_params = connection.execute.call_args_list[0].args
    match_sql, match_params = connection.execute.call_args_list[1].args
    assert "LEFT JOIN apod_embeddings" in source_sql
    assert source_params == ("2024-01-01",)
    assert "vector_top_k('apod_embeddings_vector_idx'" in match_sql
    assert "vector_distance_cos" in match_sql
    assert "WHERE a.date != ?" in match_sql
    assert "ORDER BY distance ASC, a.date DESC" in match_sql
    assert match_params == (SOURCE_VECTOR, SOURCE_VECTOR, limit + 1, "2024-01-01", limit)
    assert results == [match(2, 0.25)]
    connection.close.assert_called_once()
    connection.commit.assert_not_called()


@pytest.mark.parametrize(("source", "code"), [(None, Code.NOT_FOUND), ((None,), Code.SIMILARITY_UNAVAILABLE)])
def test_database_distinguishes_missing_apod_from_missing_vector(monkeypatch, source, code):
    connection = connection_with_rows(source=source)
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    with pytest.raises(Error) as raised:
        database.find_similar_database_apods(SOURCE_DATE, 10)
    assert raised.value.code is code
    assert connection.execute.call_count == 1
    connection.close.assert_called_once()


def test_database_no_candidates_is_successful(monkeypatch):
    connection = connection_with_rows(rows=[])
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    assert database.find_similar_database_apods(SOURCE_DATE, 10) == []
    connection.close.assert_called_once()


def test_database_failure_closes_connection(monkeypatch):
    connection = connection_with_rows()
    connection.execute.side_effect = OperationalError("database offline")
    monkeypatch.setattr(database, "connect_database", lambda: connection)
    with pytest.raises(OperationalError):
        database.find_similar_database_apods(SOURCE_DATE, 10)
    connection.close.assert_called_once()


def test_w3c_context_and_request_id_reach_similarity_and_database_threads(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tests.similarity")
    monkeypatch.setattr(similarity, "tracer", tracer)
    monkeypatch.setattr(database, "tracer", tracer)
    observed_request_ids = []

    def connect():
        observed_request_ids.append(request_id_context.get())
        return connection_with_rows()

    monkeypatch.setattr(database, "connect_database", connect)
    traced_app = FastAPI()
    traced_app.add_exception_handler(Error, handle_error)
    traced_app.include_router(vector.router)
    traced_app.middleware("http")(log_requests)
    FastAPIInstrumentor.instrument_app(traced_app, tracer_provider=provider)
    try:
        with TestClient(traced_app) as traced_client:
            response = traced_client.get(
                "/vector/similar?date=2024-01-01",
                headers={
                    "x-request-id": "livia-similarity-test",
                    "traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01",
                },
            )
        assert response.status_code == 200
        assert response.headers["x-request-id"] == "livia-similarity-test"
        assert observed_request_ids == ["livia-similarity-test"]
        spans = exporter.get_finished_spans()
        server = next(span for span in spans if span.kind is SpanKind.SERVER)
        pipeline = next(span for span in spans if span.name == "apod.similar")
        db_span = next(span for span in spans if span.name == "turso.apod.similar")
        assert server.parent.span_id == int("1234567890abcdef", 16)
        assert all(span.context.trace_id == int("1234567890abcdef1234567890abcdef", 16) for span in (server, pipeline, db_span))
        assert pipeline.parent.span_id == server.context.span_id
        assert db_span.parent.span_id == pipeline.context.span_id
        assert db_span.kind is SpanKind.CLIENT
        assert SOURCE_VECTOR.decode() not in repr([dict(span.attributes) for span in spans])
    finally:
        FastAPIInstrumentor.uninstrument_app(traced_app)
        provider.shutdown()
