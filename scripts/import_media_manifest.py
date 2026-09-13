"""Import a trusted checksum-verified backfill manifest; no AWS credentials needed.

Run from the repository: PYTHONPATH=. uv run python scripts/import_media_manifest.py manifest.json
This never changes NASA source URLs or embeddings. Safe to rerun.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

from app.database import connect_database


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    rows = {}
    mismatched = {}
    for record in manifest["records"]:
        if record["status"] != "uploaded":
            continue
        key, source, kind = record["key"], record["source_url"], record["media_type"]
        digest = hashlib.sha256(source.encode()).hexdigest()
        assert re.fullmatch(rf"hd/{kind}/{digest}\.[a-z0-9]+", key)
        assert kind in ("image", "video")
        assert record["bytes"] > 0 and re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
        if not record.get("content_type", "").startswith(kind + "/"):
            mismatched.update({day: key for day in record["dates"]})
            continue
        for day in record["dates"]:
            assert day not in rows or rows[day] == (day, source, kind, key)
            rows[day] = (day, source, kind, key)
    conn = connect_database()
    try:
        def originals_hash():
            originals = conn.execute("SELECT date,media_url,hd_media_url,media_type FROM apods ORDER BY date").fetchall()
            return hashlib.sha256(json.dumps(originals).encode()).hexdigest()
        before = originals_hash()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(apods)").fetchall()}
        if "s3_object_key" not in columns:
            conn.execute("ALTER TABLE apods ADD COLUMN s3_object_key TEXT")
            conn.commit()
        current = {r[0]: r for r in conn.execute(
            "SELECT date,media_url,hd_media_url,media_type,s3_object_key FROM apods"
        ).fetchall()}
        eligible = {}
        for day, item in rows.items():
            stored = current.get(day)
            if stored:
                source = stored[2] if stored[3] == "image" else (stored[2] or stored[1])
                if source == item[1] and stored[3] == item[2]:
                    eligible[day] = item
        for day, key in mismatched.items():
            if current.get(day) and current[day][4] == key:
                conn.execute("UPDATE apods SET s3_object_key=NULL WHERE date=? AND s3_object_key=?", (day,key))
        conn.commit()
        entries = [item for day, item in eligible.items() if current[day][4] != item[3]]
        for start in range(0, len(entries), 200):
            batch = entries[start:start+200]
            values = ",".join("(?,?,?,?)" for _ in batch)
            # Match the original source as well as the date: no stale/wrong mappings.
            conn.execute(f"""WITH verified(day,source,kind,object_key) AS (VALUES {values})
                UPDATE apods SET s3_object_key = (
                    SELECT object_key FROM verified WHERE day = apods.date
                ) WHERE EXISTS (
                    SELECT 1 FROM verified WHERE day = apods.date AND kind = apods.media_type
                    AND source = CASE WHEN apods.media_type = 'image' THEN apods.hd_media_url
                        ELSE COALESCE(NULLIF(apods.hd_media_url,''), apods.media_url) END
                )""", tuple(value for row in batch for value in row))
            conn.commit()
            print(json.dumps({"processed_dates": min(start+200, len(entries))}), flush=True)
        assert before == originals_hash(), "original media changed"
        count = conn.execute("SELECT count(*) FROM apods WHERE s3_object_key IS NOT NULL").fetchone()[0]
        assert count == len(eligible), (count, len(eligible))
        print(json.dumps({"mapped_dates": count, "skipped_media_type_dates": len(mismatched) + len(rows) - len(eligible),
            "original_urls_sha256": before}), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
