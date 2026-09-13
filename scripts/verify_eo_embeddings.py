"""Read-only recovery verification; never calls OpenAI or fetches media."""
import json
import time

from app.database import connect_earth_observatory_database


def main():
    connection = connect_earth_observatory_database()
    started = time.monotonic()
    try:
        pictures = connection.execute("SELECT count(*) FROM earth_observatory_pictures").fetchone()[0]
        embeddings = connection.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0]
        missing = connection.execute("""SELECT count(*) FROM earth_observatory_pictures p
            LEFT JOIN earth_observatory_embeddings e ON e.picture_date=p.date
            WHERE e.picture_date IS NULL""").fetchone()[0]
        dimensions = connection.execute("""SELECT json_array_length(vector_extract(embedding)), count(*)
            FROM earth_observatory_embeddings GROUP BY 1""").fetchall()
        sample = connection.execute("SELECT picture_date, embedding FROM earth_observatory_embeddings ORDER BY picture_date LIMIT 1").fetchone()
        query_started = time.monotonic()
        matches = connection.execute("""SELECT e.picture_date, vector_distance_cos(e.embedding, ?) AS distance
            FROM vector_top_k('earth_observatory_embeddings_vector_idx', ?, 5) AS m
            JOIN earth_observatory_embeddings e ON e.rowid=m.id ORDER BY distance""",
            (sample[1], sample[1])).fetchall()
        result = dict(pictures=pictures, embeddings=embeddings, missing=missing,
                      dimensions=dimensions, query_sample=sample[0], matches=matches,
                      vector_top_k_ms=round((time.monotonic()-query_started)*1000, 3),
                      verification_ms=round((time.monotonic()-started)*1000, 3))
        print(json.dumps(result), flush=True)
        assert pictures == embeddings == 7051 and missing == 0, "recovery incomplete"
        assert [tuple(row) for row in dimensions] == [(3072, 7051)], "invalid dimensions"
        assert len(matches) == 5, "vector_top_k did not return five results"
    finally:
        connection.close()


if __name__ == "__main__":
    main()
