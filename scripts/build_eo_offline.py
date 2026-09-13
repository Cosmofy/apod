"""Copy EO records/vectors to local libSQL on Oracle and build the index there."""
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
import sqlite3
import subprocess
import turso_serverless
from dotenv import dotenv_values

def log(message):
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)

settings = dotenv_values(".env")
destination = Path("data/earth-observatory-built.db")
if destination.exists():
    raise SystemExit("destination exists; inspect before replacing")
remote = turso_serverless.connect(settings["TURSO_DATABASE_URL"], auth_token=settings["TURSO_AUTH_TOKEN"])
local = sqlite3.connect(str(destination))
started = time.monotonic()
try:
    local.execute("PRAGMA page_size=4096")
    local.execute("PRAGMA auto_vacuum=0")
    local.execute("PRAGMA journal_mode=WAL")
    local.execute("PRAGMA cache_size=-262144")
    for table, key in [("earth_observatory_pictures", "date"), ("earth_observatory_embeddings", "picture_date")]:
        schema = remote.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        local.execute(schema)
        columns = remote.execute(f"PRAGMA table_info({table})").fetchall()
        names = [r[1] for r in columns]
        key_index = names.index(key)
        insert = f"INSERT INTO {table} VALUES ({','.join('?' for _ in names)})"
        last = ""
        count = 0
        fingerprint = hashlib.sha256()
        while True:
            rows = remote.execute(f"SELECT * FROM {table} WHERE {key}>? ORDER BY {key} LIMIT 128", (last,)).fetchall()
            if not rows:
                break
            local.executemany(insert, rows)
            local.commit()
            for row in rows:
                if table.endswith("embeddings"):
                    fingerprint.update(row[0].encode())
                    fingerprint.update(row[1])
            count += len(rows)
            last = rows[-1][key_index]
            log(f"copy {table} {count}/7051 ({count/7051:.1%})")
        assert count == 7051, (table, count)
        if table.endswith("embeddings"):
            log(f"source_embeddings_sha256={fingerprint.hexdigest()}")
    records = [json.loads(line) for line in Path("data/eo-media-final-manifest.jsonl").read_text().splitlines()]
    for row in records:
        local.execute("""UPDATE earth_observatory_pictures SET s3_object_key=?, media_url=?
            WHERE date=? AND media_type='image' AND media_url IN (?,?)""",
            (row["key"], row.get("fetch_url",row["source_url"]), row["date"], row["source_url"], row.get("fetch_url",row["source_url"])))
    local.commit()
    mapped = local.execute("SELECT count(*) FROM earth_observatory_pictures WHERE media_type='image' AND s3_object_key IS NOT NULL").fetchone()[0]
    assert mapped == 7047, f"mapped {mapped}, expected7047"
    log("mapped 7047/7047 images")
    local.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    local.close()
    log("local index build starting")
    subprocess.run(["node", "scripts/build_eo_index.cjs", str(destination)], check=True)
    log(f"complete file={destination} bytes={destination.stat().st_size} elapsed_s={time.monotonic()-started:.3f}")
finally:
    local.close()
    remote.close()
