import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.embeddings import EMBEDDING_DIMENSIONS, build_embedding_text, clean_text, create_embeddings, encode_embedding
from app.source import SourceApod, load_source_apod


DEFAULT_DATASET = Path("data/apod-api/extractor/extractedDailyData")
DEFAULT_DATABASE = Path("data/apod-local.db")


@dataclass
class PendingApod:
    apod: SourceApod
    embedding_text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a complete local APOD database for Turso import."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--batch-size", type=int, default=100)
    return parser.parse_args()


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size = 4096")
    connection.execute("PRAGMA auto_vacuum = NONE")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS apods (
            date TEXT PRIMARY KEY,
            title TEXT,
            explanation TEXT,
            media_url TEXT,
            hd_media_url TEXT,
            media_type TEXT,
            credit TEXT,
            copyright TEXT
        );

        CREATE TABLE IF NOT EXISTS apod_embeddings (
            apod_date TEXT PRIMARY KEY
                REFERENCES apods(date) ON DELETE CASCADE,
            embedding F32_BLOB(3072) NOT NULL
        );
        """
    )
    return connection


def write_batch(
    connection: sqlite3.Connection,
    batch: list[PendingApod],
) -> int:
    if not batch:
        return 0

    embeddings = create_embeddings([item.embedding_text for item in batch])
    if len(embeddings) != len(batch):
        raise RuntimeError("OpenAI returned an unexpected number of embeddings")

    connection.executemany(
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
        [
            (
                item.apod.date.isoformat(),
                item.apod.title,
                item.apod.explanation,
                item.apod.url,
                item.apod.hdurl,
                item.apod.media_type,
                item.apod.credit,
                item.apod.copyright,
            )
            for item in batch
        ],
    )
    connection.executemany(
        """
        INSERT INTO apod_embeddings (apod_date, embedding)
        VALUES (?, ?)
        ON CONFLICT(apod_date) DO NOTHING
        """,
        [
            (item.apod.date.isoformat(), encode_embedding(embedding))
            for item, embedding in zip(batch, embeddings, strict=True)
        ],
    )
    connection.commit()
    return len(batch)


def build_local_database(
    dataset: Path,
    database: Path,
    batch_size: int,
) -> None:
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    if not dataset.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {dataset}")

    paths = sorted(dataset.rglob("*.json"))
    connection = open_database(database)
    try:
        embedded_dates = {
            row[0]
            for row in connection.execute(
                "SELECT apod_date FROM apod_embeddings"
            ).fetchall()
        }

        processed = 0
        indexed = 0
        skipped = 0
        batch: list[PendingApod] = []

        for path in paths:
            processed += 1
            apod = load_source_apod(path)
            date = apod.date.isoformat()
            if date in embedded_dates:
                skipped += 1
                continue

            title = clean_text(apod.title)
            explanation = clean_text(apod.explanation)
            if not title and not explanation:
                raise ValueError(f"APOD has no embeddable text: {path}")

            batch.append(
                PendingApod(
                    apod=apod,
                    embedding_text=build_embedding_text(
                        apod.title,
                        apod.explanation,
                        apod.credit,
                    ),
                )
            )

            if len(batch) >= batch_size:
                indexed += write_batch(connection, batch)
                embedded_dates.update(
                    item.apod.date.isoformat() for item in batch
                )
                batch.clear()
                print(
                    f"processed={processed} embedded={indexed} skipped={skipped}",
                    flush=True,
                )

        indexed += write_batch(connection, batch)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        apod_count = connection.execute("SELECT COUNT(*) FROM apods").fetchone()[0]
        embedding_count = connection.execute(
            "SELECT COUNT(*) FROM apod_embeddings"
        ).fetchone()[0]
        invalid_vectors = connection.execute(
            "SELECT COUNT(*) FROM apod_embeddings WHERE length(embedding) != ?",
            (EMBEDDING_DIMENSIONS * 4,),
        ).fetchone()[0]
        print(
            f"complete files={len(paths)} apods={apod_count} "
            f"embeddings={embedding_count} invalid_vectors={invalid_vectors}",
            flush=True,
        )
    finally:
        connection.close()


def main() -> None:
    args = parse_args()
    build_local_database(args.dataset, args.database, args.batch_size)


if __name__ == "__main__":
    main()
