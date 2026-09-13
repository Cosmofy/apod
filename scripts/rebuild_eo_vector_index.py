"""Restore only the EO vector index after the authorized bulk recovery."""
import time
from datetime import UTC, datetime

from app.database import connect_database

# Definition inspected on Toronto A before the index was dropped.
INDEX_SQL = """CREATE INDEX earth_observatory_embeddings_vector_idx
            ON earth_observatory_embeddings(
                libsql_vector_idx(
                    embedding,
                    'metric=cosine',
                    'max_neighbors=32',
                    'compress_neighbors=float8'
                )
            )
            """


def main():
    connection = connect_database()
    try:
        count = connection.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0]
        missing = connection.execute("""SELECT count(*) FROM earth_observatory_pictures p
            LEFT JOIN earth_observatory_embeddings e ON e.picture_date=p.date
            WHERE e.picture_date IS NULL""").fetchone()[0]
        assert count == 7051 and missing == 0, (count, missing)
        apod_before = connection.execute("SELECT sql FROM sqlite_master WHERE name='apod_embeddings_vector_idx'").fetchone()
        assert apod_before, "APOD index missing before EO rebuild"
        started = time.monotonic()
        print(f"{datetime.now(UTC).isoformat()} rebuild_start rows={count}", flush=True)
        connection.execute(INDEX_SQL)
        connection.commit()
        print(f"{datetime.now(UTC).isoformat()} rebuild_complete elapsed_s={time.monotonic()-started:.3f}", flush=True)
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name='apod_embeddings_vector_idx'").fetchone() == apod_before
        assert connection.execute("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='earth_observatory_embeddings_vector_idx'").fetchone()[0] == 'earth_observatory_embeddings'
    finally:
        connection.close()


if __name__ == "__main__":
    main()
