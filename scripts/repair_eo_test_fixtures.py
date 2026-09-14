"""Guarded repair of the confirmed accidental EO test writes on the new DB."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import turso_serverless

EXPECTED_HASH = "8cac25b57e2706c1c05fe991da8a168e3dcf21bdc197c06ba60d6833c6dbc87d"
DATES = {"2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2026-09-11"}


def vector_hash(c):
    digest = hashlib.sha256()
    last = ""
    count = 0
    while True:
        rows = c.execute("SELECT picture_date,embedding FROM earth_observatory_embeddings WHERE picture_date>? ORDER BY picture_date LIMIT 128", (last,)).fetchall()
        if not rows:
            return count, digest.hexdigest()
        for date, blob in rows:
            assert len(blob) == 12288
            digest.update(date.encode())
            digest.update(blob)
        count += len(rows)
        last = rows[-1][0]


def metadata(c):
    return {r[0]: tuple(r) for r in c.execute("SELECT * FROM earth_observatory_pictures").fetchall()}


def counts(c):
    return {"pictures": c.execute("SELECT count(*) FROM earth_observatory_pictures").fetchone()[0],
            "vectors": c.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0],
            "mappings": c.execute("SELECT count(*) FROM earth_observatory_pictures WHERE media_type='image' AND s3_object_key IS NOT NULL").fetchone()[0]}


def main():
    started = time.monotonic()
    cfg = json.loads(Path("data/eo-upload-config.json").read_text())
    c = turso_serverless.connect("https://" + cfg["hostname"], auth_token=cfg["token"])
    snapshot = sqlite3.connect("file:data/earth-observatory-built.db?mode=ro", uri=True)
    transaction = False
    try:
        columns = [r[1] for r in snapshot.execute("PRAGMA table_info(earth_observatory_pictures)")]
        assert columns == [r[1] for r in c.execute("PRAGMA table_info(earth_observatory_pictures)").fetchall()]
        baseline, current = metadata(snapshot), metadata(c)
        differences = {d for d in baseline.keys() | current.keys() if baseline.get(d) != current.get(d)}
        assert differences == DATES, sorted(differences)
        assert "2024-01-01" not in baseline
        assert c.execute("SELECT count(*) FROM earth_observatory_embeddings WHERE picture_date='2024-01-01'").fetchone()[0] == 0
        for date in differences:
            record = dict(zip(columns, current[date]))
            assert record["article_url"] == "https://science.nasa.gov/example"
            assert record["media_url"] == ("https://www.youtube.com/embed/example" if date == "2026-09-11" else "https://nasa.example/clouds.jpg")
        before_counts = counts(c)
        before_hash = vector_hash(c)
        assert before_hash == (7051, EXPECTED_HASH)
        audit = {"before_counts": before_counts, "before_vector_hash": before_hash,
                 "columns": columns, "before_rows": {d: current[d] for d in sorted(differences)}}
        # Preserve the exact pre-repair metadata for recovery/audit, no credentials.
        with Path("data/eo-fixture-repair-audit.json").open("x") as stream:
            json.dump(audit, stream)
        print("before", json.dumps({"counts": before_counts, "vector_hash": before_hash}), flush=True)
        c.execute("BEGIN IMMEDIATE")
        transaction = True
        guard = " AND ".join(f'"{col}" IS ?' for col in columns)
        assignments = ",".join(f'"{col}"=?' for col in columns)
        for date in sorted(differences):
            if date in baseline:
                c.execute(f"UPDATE earth_observatory_pictures SET {assignments} WHERE {guard}", baseline[date] + current[date])
            else:
                c.execute(f"DELETE FROM earth_observatory_pictures WHERE {guard}", current[date])
            assert c.execute("SELECT changes()").fetchone()[0] == 1, date
        c.commit()
        transaction = False
        after = metadata(c)
        remaining = sorted(d for d in baseline.keys() | after.keys() if baseline.get(d) != after.get(d))
        after_hash = vector_hash(c)
        after_counts = counts(c)
        result = {"restored_dates": sorted(differences - {"2024-01-01"}),
                  "deleted_fixture_date": "2024-01-01", "after_counts": after_counts,
                  "metadata_differences": remaining, "after_vector_hash": after_hash,
                  "elapsed_s": round(time.monotonic() - started, 3)}
        print("after", json.dumps(result), flush=True)
        assert not remaining
        assert after_hash == before_hash
        assert after_counts == {"pictures": 7051, "vectors": 7051, "mappings": 7047}
        print("REPAIR_VERIFIED", flush=True)
    finally:
        if transaction:
            c.rollback()
        c.close()
        snapshot.close()


if __name__ == "__main__":
    main()
