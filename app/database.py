from dataclasses import dataclass
from datetime import date

from opentelemetry import trace
from opentelemetry.trace import SpanKind
import turso_serverless
from turso_serverless import Connection

from app.config import Settings
from app.embeddings import encode_embedding
from app.errors import Code, Error
from app.source import EarthObservatoryPicture, SourceApod

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
        url=row[3] or "",
        hdurl=row[4],
        media_type=row[5],
        credit=row[6],
        copyright=row[7],
        s3_object_key=row[9] if len(row) > 9 else None,
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
                       media_type, credit, copyright, NULL, s3_object_key
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

def get_earth_observatory_picture(picture_date: date) -> EarthObservatoryPicture | None:
    with tracer.start_as_current_span(
        "turso.earth_observatory.select",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("SELECT", "earth_observatory_pictures"),
    ) as span:
        connection = connect_database()
        try:
            row = connection.execute(
                """SELECT date, title, explanation, media_type, media_url, url_fallback, credit, copyright,
                          article_url, image_date, location_name, latitude, longitude
                   FROM earth_observatory_pictures WHERE date = ?""",
                (picture_date.isoformat(),),
            ).fetchone()
            span.set_attribute("db.response.returned_rows", 1 if row else 0)
            if not row:
                return None
            return EarthObservatoryPicture(
                date=row[0], title=row[1], explanation=row[2], media_type=row[3], url=row[4], url_fallback=row[5],
                credit=row[6], copyright=row[7], article_url=row[8], image_date=row[9],
                location_name=row[10], latitude=row[11], longitude=row[12],
            )
        finally:
            connection.close()

def save_earth_observatory_picture(picture: EarthObservatoryPicture) -> None:
    with tracer.start_as_current_span(
        "turso.earth_observatory.upsert",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("INSERT", "earth_observatory_pictures"),
    ):
        connection = connect_database()
        try:
            connection.execute(
                """INSERT INTO earth_observatory_pictures
                   (date,title,explanation,media_type,media_url,url_fallback,credit,copyright,article_url,image_date,location_name,latitude,longitude)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     title=excluded.title, explanation=excluded.explanation, media_type=excluded.media_type, media_url=excluded.media_url,
                     url_fallback=excluded.url_fallback, credit=excluded.credit, copyright=excluded.copyright,
                     article_url=excluded.article_url, image_date=excluded.image_date,
                     location_name=excluded.location_name, latitude=excluded.latitude, longitude=excluded.longitude""",
                (picture.date.isoformat(), picture.title, picture.explanation, picture.media_type, picture.url,
                 picture.url_fallback, picture.credit, picture.copyright, picture.article_url,
                 picture.image_date.isoformat() if picture.image_date else None, picture.location_name,
                 picture.latitude, picture.longitude),
            )
            connection.commit()
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
                    copyright = excluded.copyright,
                    s3_object_key = CASE
                        WHEN apods.hd_media_url IS excluded.hd_media_url
                         AND apods.media_url IS excluded.media_url
                         AND apods.media_type IS excluded.media_type
                        THEN apods.s3_object_key ELSE NULL END
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
                       bm25(apods_fts, 8.0, 1.0, 3.0) AS lexical_score,
                       a.s3_object_key
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
                       vector_distance_cos(embedding.embedding, ?) AS semantic_distance,
                       a.s3_object_key
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


def find_similar_database_apods(
    apod_date: date,
    limit: int,
) -> list[DatabaseSearchMatch]:
    """Read the source vector and its neighbors without generating embeddings."""
    with tracer.start_as_current_span(
        "turso.apod.similar",
        kind=SpanKind.CLIENT,
        attributes=database_span_attributes("SELECT", "apod_embeddings"),
    ) as span:
        connection = connect_database()
        try:
            source = connection.execute(
                """
                SELECT embedding.embedding
                FROM apods AS a
                LEFT JOIN apod_embeddings AS embedding
                  ON embedding.apod_date = a.date
                WHERE a.date = ?
                """,
                (apod_date.isoformat(),),
            ).fetchone()
            if source is None:
                raise Error(Code.NOT_FOUND)
            if source[0] is None:
                raise Error(Code.SIMILARITY_UNAVAILABLE)

            # Ask for one extra candidate because the index can include the source.
            rows = connection.execute(
                """
                SELECT a.date, a.title, a.explanation, a.media_url,
                       a.hd_media_url, a.media_type, a.credit, a.copyright,
                       vector_distance_cos(embedding.embedding, ?) AS distance,
                       a.s3_object_key
                FROM vector_top_k('apod_embeddings_vector_idx', ?, ?) AS matches
                JOIN apod_embeddings AS embedding ON embedding.rowid = matches.id
                JOIN apods AS a ON a.date = embedding.apod_date
                WHERE a.date != ?
                ORDER BY distance ASC, a.date DESC
                LIMIT ?
                """,
                (source[0], source[0], limit + 1, apod_date.isoformat(), limit),
            ).fetchall()
            span.set_attribute("db.response.returned_rows", len(rows))
            return [
                DatabaseSearchMatch(apod=source_apod_from_row(row), metric=float(row[8]))
                for row in rows
            ]
        finally:
            connection.close()
