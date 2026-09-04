import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app import database
from app.routers import apod as apod_router
from app.source import SourceApod


APOD_DATE = date(2026, 9, 3)
APOD_ROW = (
    APOD_DATE.isoformat(),
    "The Eclipse and the Stork",
    "sensitive full explanation that must not become a span attribute",
    "https://example.com/apod.jpg",
    "https://example.com/apod-hd.jpg",
    "image",
    "Example credit",
    "Example copyright",
)


class FakeCursor:
    def __init__(self, row: tuple[str, ...] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[str, ...] | None:
        return self.row


class FakeConnection:
    def __init__(
        self,
        row: tuple[str, ...] | None = APOD_ROW,
        execute_error: Exception | None = None,
    ) -> None:
        self.row = row
        self.execute_error = execute_error
        self.committed = False
        self.closed = False

    def execute(self, statement: str, parameters: tuple[object, ...]) -> FakeCursor:
        if self.execute_error:
            raise self.execute_error
        return FakeCursor(self.row if statement.lstrip().lower().startswith("select") else None)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("tests.tracing")
    monkeypatch.setattr(database, "tracer", test_tracer)
    monkeypatch.setattr(apod_router, "tracer", test_tracer)
    yield exporter
    provider.shutdown()


def sample_apod() -> SourceApod:
    return SourceApod(
        date=APOD_DATE,
        title=APOD_ROW[1],
        explanation=APOD_ROW[2],
        url=APOD_ROW[3],
        hdurl=APOD_ROW[4],
        media_type=APOD_ROW[5],
        credit=APOD_ROW[6],
        copyright=APOD_ROW[7],
    )


def test_turso_operations_emit_stable_client_spans_without_apod_content(
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [FakeConnection(), FakeConnection(), FakeConnection()]
    monkeypatch.setattr(database, "connect_database", lambda: connections.pop(0))

    database.get_database_apod(APOD_DATE)
    database.save_database_apod(sample_apod())
    database.save_database_embedding(APOD_DATE, [0.0] * 3072)

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == [
        "turso.apod.select",
        "turso.apod.upsert",
        "turso.embedding.upsert",
    ]
    assert all(span.kind is SpanKind.CLIENT for span in finished)
    assert [span.attributes["db.collection.name"] for span in finished] == [
        "apods",
        "apods",
        "apod_embeddings",
    ]
    assert [span.attributes["db.operation.name"] for span in finished] == [
        "SELECT",
        "INSERT",
        "INSERT",
    ]
    assert finished[0].attributes["db.response.returned_rows"] == 1

    recorded_attributes = repr([dict(span.attributes) for span in finished])
    assert APOD_ROW[2] not in recorded_attributes
    assert APOD_DATE.isoformat() not in recorded_attributes
    assert "db.query.text" not in recorded_attributes


def test_turso_span_records_failures_and_still_closes_connection(
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(execute_error=RuntimeError("database unavailable"))
    monkeypatch.setattr(database, "connect_database", lambda: connection)

    with pytest.raises(RuntimeError, match="database unavailable"):
        database.get_database_apod(APOD_DATE)

    span = spans.get_finished_spans()[0]
    assert span.name == "turso.apod.select"
    assert span.status.status_code is StatusCode.ERROR
    assert connection.closed is True


def test_apod_pipeline_keeps_turso_thread_span_in_the_same_trace(
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical_date = date(2026, 9, 2)
    historical_row = (historical_date.isoformat(), *APOD_ROW[1:])
    monkeypatch.setattr(database, "connect_database", lambda: FakeConnection(historical_row))
    monkeypatch.setattr(
        apod_router,
        "resolve_date",
        lambda value=None: historical_date if value else APOD_DATE,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis_client=object()))
    )

    result = asyncio.run(apod_router.get_apod(request, historical_date.isoformat()))

    assert result.date == historical_date
    finished = {span.name: span for span in spans.get_finished_spans()}
    pipeline_span = finished["apod.pipeline"]
    turso_span = finished["turso.apod.select"]

    assert turso_span.context.trace_id == pipeline_span.context.trace_id
    assert turso_span.parent is not None
    assert turso_span.parent.span_id == pipeline_span.context.span_id
    assert pipeline_span.attributes["apod.request.has_explicit_date"] is True
    assert pipeline_span.attributes["apod.request.is_today"] is False
    assert pipeline_span.attributes["apod.cache.outcome"] == "skipped"
    assert pipeline_span.attributes["apod.database.outcome"] == "hit"
    assert pipeline_span.attributes["apod.result.source"] == "turso"
    assert historical_date.isoformat() not in repr(dict(pipeline_span.attributes))
