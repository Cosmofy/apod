from dataclasses import dataclass
from datetime import date

from opentelemetry import trace
from opentelemetry.trace import SpanKind
import turso_serverless
from turso_serverless import Connection

from app.config import Settings
from app.embeddings import encode_embedding
from app.source import SourceApod

tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class DatabaseSearchMatch:
    apod: SourceApod
    metric: float


def database_span_attributes(operation: str, table: str) -> dict[str, str]:
    return {
        "db.system.name": "turso",
        "db.namespace": "apod",
        "db.operation.name": operation,
        "db.collection.name": table,
    }


def connect_database() -> Connection:
    settings = Settings()
    return turso_serverless.connect(
        settings.turso_database_url,
        auth_token=settings.turso_auth_token,
    )


def source_apod_from_row(row: tuple) -> SourceApod:
    return SourceApod(
        date=row[0],
        title=row[1],
        explanation=row[2],
        url=row[3],
        hdurl=row[4],
        media_type=row[5],
        credit=row[6],
        copyright=row[7],
    )

# Own the complete synchronous connection lifecycle so callers can run it safely in one worker thread.
def get_database_apod(apod_date: date) -> SourceApod | None:
    with tracer.start_as_current_span(
        "turso.apod.select",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("SELECT", "apods"),
    ) as span:
        connection = connect_database()
        try:
            row = connection.execute(
                """
                SELECT date, title, explanation, media_url, hd_media_url,
                       media_type, credit, copyright
                FROM apods
                WHERE date = ?
                """,
                (apod_date.isoformat(),),
            ).fetchone()
            span.set_attribute("db.response.returned_rows", 1 if row else 0)
            if row:
                return source_apod_from_row(row)
            return None
        finally:
            connection.close()

def save_database_apod(apod: SourceApod) -> None:
    with tracer.start_as_current_span(
        "turso.apod.upsert",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("INSERT", "apods"),
    ):
        connection = connect_database()
        try:
            connection.execute(
                """
                INSERT INTO apods (
                    date, title, explanation, media_url, hd_media_url,
                    media_type, credit, copyright
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    title = excluded.title,
                    explanation = excluded.explanation,
                    media_url = excluded.media_url,
                    hd_media_url = excluded.hd_media_url,
                    media_type = excluded.media_type,
                    credit = excluded.credit,
                    copyright = excluded.copyright
                """,
                (
                    apod.date.isoformat(),
                    apod.title,
                    apod.explanation,
                    apod.url,
                    apod.hdurl,
                    apod.media_type,
                    apod.credit,
                    apod.copyright,
                ),
            )
            connection.commit()
        finally:
            connection.close()


def save_database_embedding(apod_date: date, embedding: list[float]) -> None:
    encoded_embedding = encode_embedding(embedding)
    with tracer.start_as_current_span(
        "turso.embedding.upsert",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("INSERT", "apod_embeddings"),
    ):
        connection = connect_database()
        try:
            connection.execute(
                """
                INSERT INTO apod_embeddings (apod_date, embedding)
                VALUES (?, ?)
                ON CONFLICT(apod_date) DO UPDATE SET embedding = excluded.embedding
                """,
                (apod_date.isoformat(), encoded_embedding),
            )
            connection.commit()
        finally:
            connection.close()


def search_lexical_apods(
    fts_query: str,
    limit: int,
) -> list[DatabaseSearchMatch]:
    with tracer.start_as_current_span(
        "turso.apod.search.lexical",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("SELECT", "apods_fts"),
    ) as span:
        connection = connect_database()
        try:
            rows = connection.execute(
                """
                SELECT a.date, a.title, a.explanation, a.media_url,
                       a.hd_media_url, a.media_type, a.credit, a.copyright,
                       bm25(apods_fts, 8.0, 1.0, 3.0) AS lexical_score
                FROM apods_fts
                JOIN apods AS a ON a.rowid = apods_fts.rowid
                WHERE apods_fts MATCH ?
                ORDER BY lexical_score
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            span.set_attribute("db.response.returned_rows", len(rows))
            return [
                DatabaseSearchMatch(
                    apod=source_apod_from_row(row),
                    metric=float(row[8]),
                )
                for row in rows
            ]
        finally:
            connection.close()


def search_vector_apods(
    embedding: list[float],
    limit: int,
) -> list[DatabaseSearchMatch]:
    encoded_embedding = encode_embedding(embedding)
    with tracer.start_as_current_span(
        "turso.apod.search.vector",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("SELECT", "apod_embeddings"),
    ) as span:
        connection = connect_database()
        try:
            rows = connection.execute(
                """
                SELECT a.date, a.title, a.explanation, a.media_url,
                       a.hd_media_url, a.media_type, a.credit, a.copyright,
                       vector_distance_cos(embedding.embedding, ?) AS semantic_distance
                FROM vector_top_k(
                    'apod_embeddings_vector_idx',
                    ?,
                    ?
                ) AS matches
                JOIN apod_embeddings AS embedding
                  ON embedding.rowid = matches.id
                JOIN apods AS a
                  ON a.date = embedding.apod_date
                ORDER BY semantic_distance
                """,
                (encoded_embedding, encoded_embedding, limit),
            ).fetchall()
            span.set_attribute("db.response.returned_rows", len(rows))
            return [
                DatabaseSearchMatch(
                    apod=source_apod_from_row(row),
                    metric=float(row[8]),
                )
                for row in rows
            ]
        finally:
            connection.close()
