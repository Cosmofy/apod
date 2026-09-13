"""Backfill Earth Observatory embeddings with progress and resume safety.

Run from the service root after applying migration 009:

    PYTHONPATH=. .venv/bin/python scripts/backfill_earth_observatory_embeddings.py --estimate
    PYTHONPATH=. .venv/bin/python scripts/backfill_earth_observatory_embeddings.py --batch-size 32 --yes
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sys

from app.database import connect_earth_observatory_database, save_earth_observatory_embedding
from app.embeddings import build_embedding_text, create_embeddings
from app.source import EarthObservatoryPicture


PRICE_PER_MILLION_TOKENS = 0.13
SAFE_BALANCE_USD = 1.50


@dataclass(frozen=True)
class PendingPicture:
    date: str
    title: str
    explanation: str
    media_type: str
    media_url: str
    url_fallback: str | None
    credit: str | None
    copyright: str | None
    article_url: str

    def embedding_text(self) -> str:
        return build_embedding_text(self.title, self.explanation, self.credit)

    def picture(self) -> EarthObservatoryPicture:
        return EarthObservatoryPicture(
            date=self.date,
            title=self.title,
            explanation=self.explanation,
            media_type=self.media_type,
            url=self.media_url,
            url_fallback=self.url_fallback,
            credit=self.credit,
            copyright=self.copyright,
            article_url=self.article_url,
        )


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def ensure_schema(connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS earth_observatory_embeddings (
               picture_date TEXT PRIMARY KEY
                   REFERENCES earth_observatory_pictures(date) ON DELETE CASCADE,
               embedding F32_BLOB(3072) NOT NULL
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS earth_observatory_embeddings_vector_idx
           ON earth_observatory_embeddings(
               libsql_vector_idx(
                   embedding,
                   'metric=cosine',
                   'max_neighbors=32',
                   'compress_neighbors=float8'
               )
           )"""
    )
    connection.commit()


def fetch_pending(connection, limit: int | None) -> list[PendingPicture]:
    sql_limit = "" if limit is None else "LIMIT ?"
    parameters = () if limit is None else (limit,)
    rows = connection.execute(
        f"""SELECT p.date, p.title, p.explanation, p.media_type, p.media_url,
                   p.url_fallback, p.credit, p.copyright, p.article_url
            FROM earth_observatory_pictures AS p
            LEFT JOIN earth_observatory_embeddings AS e
              ON e.picture_date = p.date
            WHERE e.picture_date IS NULL
            ORDER BY p.date
            {sql_limit}""",
        parameters,
    ).fetchall()
    return [PendingPicture(*row) for row in rows]


def count_corpus(connection) -> tuple[int, int, int]:
    total_rows = connection.execute("SELECT count(*) FROM earth_observatory_pictures").fetchone()[0]
    embedded_rows = connection.execute("SELECT count(*) FROM earth_observatory_embeddings").fetchone()[0]
    row = connection.execute(
        """SELECT COALESCE(sum(length(title) + length(explanation) + length(COALESCE(credit, ''))), 0)
           FROM earth_observatory_pictures"""
    ).fetchone()
    return int(total_rows), int(embedded_rows), int(row[0])


def estimate_cost(characters: int) -> tuple[float, float, float, float]:
    low_tokens = characters / 4
    high_tokens = characters / 3
    low_cost = low_tokens / 1_000_000 * PRICE_PER_MILLION_TOKENS
    high_cost = high_tokens / 1_000_000 * PRICE_PER_MILLION_TOKENS
    return low_tokens, high_tokens, low_cost, high_cost


def print_estimate(connection) -> bool:
    total_rows, embedded_rows, characters = count_corpus(connection)
    low_tokens, high_tokens, low_cost, high_cost = estimate_cost(characters)
    safe = high_cost < SAFE_BALANCE_USD
    print(
        f"{utc_now()} estimate rows={total_rows} embedded={embedded_rows} "
        f"chars={characters} tokens={low_tokens:,.0f}-{high_tokens:,.0f} "
        f"cost=${low_cost:.2f}-${high_cost:.2f} safe={str(safe).lower()}",
        flush=True,
    )
    return safe


def chunks(values: list[PendingPicture], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.batch_size < 1 or args.batch_size > 128:
        raise SystemExit("--batch-size must be between 1 and 128")

    log_dir = Path("data")
    log_dir.mkdir(exist_ok=True)
    connection = connect_earth_observatory_database()
    try:
        ensure_schema(connection)
        safe = print_estimate(connection)
        if args.estimate:
            return 0 if safe else 1
        if not args.yes:
            print("refusing to run without --yes", file=sys.stderr)
            return 2
        if not safe:
            print("refusing to run because estimated cost exceeds safety balance", file=sys.stderr)
            return 3

        pending = fetch_pending(connection, args.limit)
    finally:
        connection.close()

    total = len(pending)
    print(f"{utc_now()} start pending={total} batch_size={args.batch_size}", flush=True)
    done = 0
    failed = 0
    for batch in chunks(pending, args.batch_size):
        texts = [item.embedding_text() for item in batch]
        try:
            embeddings = create_embeddings(texts)
            for item, embedding in zip(batch, embeddings, strict=True):
                save_earth_observatory_embedding(item.picture().date, embedding)
                done += 1
        except Exception as exc:  # noqa: BLE001 - keep backfill resumable and visible.
            failed += len(batch)
            print(
                f"{utc_now()} batch_failed first_date={batch[0].date} "
                f"count={len(batch)} error={type(exc).__name__}",
                flush=True,
            )
        percent = (done + failed) / total * 100 if total else 100
        print(
            f"{utc_now()} progress {done + failed}/{total} ({percent:.1f}%) "
            f"embedded={done} failed={failed}",
            flush=True,
        )

    print(f"{utc_now()} complete embedded={done} failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
