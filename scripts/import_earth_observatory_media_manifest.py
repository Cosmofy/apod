import json
import sys
from pathlib import Path

from app.database import connect_earth_observatory_database


def ensure_column(connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(earth_observatory_pictures)").fetchall()}
    if "s3_object_key" not in columns:
        connection.execute("ALTER TABLE earth_observatory_pictures ADD COLUMN s3_object_key TEXT")
        connection.commit()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/import_earth_observatory_media_manifest.py data/eo-s3-upload-done.jsonl")

    manifest_path = Path(sys.argv[1])
    records = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
    connection = connect_earth_observatory_database()
    try:
        ensure_column(connection)
        updated = 0
        skipped = 0
        valid = [r for r in records if r["key"].startswith("eo/image/")]
        skipped = len(records) - len(valid)
        for offset in range(0, len(valid), 100):
            batch = valid[offset:offset + 100]
            placeholders = ",".join("(?,?,?,?)" for _ in batch)
            params = tuple(value for r in batch for value in (r["date"], r["key"], r["source_url"], r.get("fetch_url", r["source_url"])))
            connection.execute(
                f"""WITH uploaded(date, object_key, source_url, fetch_url) AS (VALUES {placeholders})
                UPDATE earth_observatory_pictures AS p
                SET s3_object_key = (SELECT object_key FROM uploaded u WHERE u.date=p.date),
                    media_url = (SELECT fetch_url FROM uploaded u WHERE u.date=p.date)
                WHERE p.media_type='image' AND EXISTS
                    (SELECT 1 FROM uploaded u WHERE u.date=p.date AND p.media_url IN (u.source_url,u.fetch_url))""",
                params,
            )
            updated += connection.execute("SELECT changes()").fetchone()[0]
            connection.commit()
            print(f"mapped batch {min(offset+100,len(valid))}/{len(valid)}", flush=True)
        count = connection.execute(
            "SELECT count(*) FROM earth_observatory_pictures WHERE s3_object_key IS NOT NULL"
        ).fetchone()[0]
        print(json.dumps({"updated": updated, "skipped": skipped, "mapped_total": count}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
