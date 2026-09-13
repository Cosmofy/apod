"""Resume missing embeddings remotely, preserving every existing vector."""
import time
import fcntl
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from openai import OpenAI
from app.config import Settings
from app.database import connect_database
from app.embeddings import encode_embedding, clean_text

def log(message):
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)

lock = open("data/eo-embeddings-recovery.lock", "a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
c = connect_database()
client = OpenAI(api_key=Settings().openai_api_key, max_retries=8, timeout=120)
started = time.monotonic()
tokens = 0

def embed(rows):
    api_started = time.monotonic()
    texts = [f"Title: {clean_text(r[1])}\nExplanation: {clean_text(r[2])}" +
             (f"\nCredit: {clean_text(r[3])}" if r[3] else "") for r in rows]
    response = client.embeddings.create(model="text-embedding-3-large", dimensions=3072, input=texts)
    vectors = sorted(response.data, key=lambda v: v.index)
    if [v.index for v in vectors] != list(range(len(rows))):
        raise RuntimeError("embedding response count/index mismatch")
    params = []
    for row, vector in zip(rows, vectors, strict=True):
        if not all(math.isfinite(value) for value in vector.embedding):
            raise RuntimeError("non-finite embedding")
        params.extend((row[0], encode_embedding(vector.embedding)))
    return tuple(params), response.usage.total_tokens, time.monotonic() - api_started

pool = ThreadPoolExecutor(max_workers=3)
try:
    total = c.execute("SELECT count(*) FROM earth_observatory_pictures").fetchone()[0]
    initial = c.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0]
    log(f"start {initial}/{total} parallel=3 api_batch=64 db_batch=64")
    while True:
        wave_started = time.monotonic()
        rows = c.execute("""SELECT p.date, p.title, p.explanation, p.credit
            FROM earth_observatory_pictures p
            LEFT JOIN earth_observatory_embeddings e ON p.date=e.picture_date
            WHERE e.picture_date IS NULL ORDER BY p.date DESC LIMIT 192""").fetchall()
        if not rows:
            log(f"complete {total}/{total} elapsed_s={time.monotonic()-started:.3f} run_tokens={tokens}")
            break
        # Disjoint batches; all database work stays on the main thread.
        futures = [pool.submit(embed, rows[i:i+64]) for i in range(0, len(rows), 64)]
        errors = []
        wave_tokens = 0
        for future in as_completed(futures):
            try:
                params, batch_tokens, api_seconds = future.result()
            except Exception as error:
                errors.append(error)
                continue  # Persist other successful API results before failing.
            tokens += batch_tokens
            wave_tokens += batch_tokens
            sql = "INSERT INTO earth_observatory_embeddings (picture_date,embedding) VALUES "
            sql += ",".join("(?,?)" for _ in range(len(params)//2)) + " ON CONFLICT(picture_date) DO NOTHING"
            db_started = time.monotonic()
            for attempt in range(3):
                try:
                    c.execute(sql, params)
                    c.commit()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            db_seconds = time.monotonic() - db_started
            done = c.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0]
            log(f"progress {done}/{total} ({done/total:.1%}) batch_tokens={batch_tokens} "
                f"run_tokens={tokens} api_s={api_seconds:.3f} db_s={db_seconds:.3f} "
                f"elapsed_s={time.monotonic()-started:.3f}")
        if errors:
            raise errors[0]
        # Leave TPM headroom for other consumers of the same OpenAI account.
        time.sleep(max(0, wave_tokens * 60 / 750000 - (time.monotonic()-wave_started)))
finally:
    pool.shutdown(wait=True, cancel_futures=True)
    c.close()
    client.close()
    lock.close()
