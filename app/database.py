import turso_serverless as typeshit
from turso_serverless import Connection
from datetime import date
from app.config import Settings
from app.embeddings import encode_embedding
from app.source import SourceApod

def connect_database() -> Connection:
    settings = Settings()
    return typeshit.connect(settings.turso_database_url, auth_token=settings.turso_auth_token)

# Own the complete synchronous connection lifecycle so callers can run it safely in one worker thread.
def get_database_apod(apod_date: date) -> SourceApod | None:
    connection = connect_database()
    try:
        row = connection.execute("select * from apods where date = ?", (apod_date.isoformat(),)).fetchone()
        if row:
            return SourceApod(
                date = row[0],
                title = row[1],
                explanation = row[2],
                url = row[3],
                hdurl = row[4],
                media_type = row[5],
                credit = row[6],
                copyright = row[7],
            )
        return None
    finally:
        connection.close()

def save_database_apod(apod: SourceApod) -> None:
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
    connection = connect_database()
    try:
        connection.execute(
            """
            INSERT INTO apod_embeddings (apod_date, embedding)
            VALUES (?, ?)
            ON CONFLICT(apod_date) DO UPDATE SET embedding = excluded.embedding
            """,
            (apod_date.isoformat(), encode_embedding(embedding)),
        )
        connection.commit()
    finally:
        connection.close()
