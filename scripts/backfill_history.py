import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from app.database import connect_database
from app.embeddings import build_embedding_text, clean_text, create_embeddings
from app.source import SourceApod, load_source_apod


DEFAULT_DATASET = Path("data/apod-api/extractor/extractedDailyData")


@dataclass
class PendingApod:
    apod: SourceApod
    embedding_text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill historical APOD records and embeddings."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="Directory containing historical APOD JSON files.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many JSON files.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of embedding inputs per OpenAI request.",
    )
    return parser.parse_args()


def flush_batch(connection, batch: list[PendingApod]) -> int:
    if not batch:
        return 0

    connection.executemany(
        """
        INSERT INTO apods (
            date, title, explanation, media_url, hd_media_url,
            media_type, credit, copyright
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO NOTHING
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
    connection.commit()

    embeddings = create_embeddings([item.embedding_text for item in batch])
    if len(embeddings) != len(batch):
        raise RuntimeError("OpenAI returned an unexpected number of embeddings")

    connection.executemany(
        """
        INSERT INTO apod_embeddings (apod_date, embedding)
        VALUES (?, vector32(?))
        ON CONFLICT(apod_date) DO NOTHING
        """,
        [
            (
                item.apod.date.isoformat(),
                json.dumps(embedding, separators=(",", ":")),
            )
            for item, embedding in zip(batch, embeddings, strict=True)
        ],
    )
    connection.commit()
    return len(batch)


def backfill(dataset: Path, limit: int | None, batch_size: int) -> None:
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if not dataset.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {dataset}")

    paths = sorted(dataset.rglob("*.json"))
    if limit is not None:
        paths = paths[:limit]

    connection = connect_database()
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
        failed = 0
        batch: list[PendingApod] = []

        for path in paths:
            processed += 1
            try:
                apod = load_source_apod(path)
                date = apod.date.isoformat()

                if date in embedded_dates:
                    skipped += 1
                    continue

                title = clean_text(apod.title)
                explanation = clean_text(apod.explanation)
                if not title and not explanation:
                    skipped += 1
                    continue

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
                    indexed += flush_batch(connection, batch)
                    embedded_dates.update(
                        item.apod.date.isoformat() for item in batch
                    )
                    batch.clear()
                    print(
                        f"processed={processed} indexed={indexed} "
                        f"skipped={skipped} failed={failed}",
                        flush=True,
                    )
            except Exception as error:
                failed += 1
                print(f"failed {path}: {error}", flush=True)

        indexed += flush_batch(connection, batch)
        print(
            f"complete processed={processed} indexed={indexed} "
            f"skipped={skipped} failed={failed}",
            flush=True,
        )
    finally:
        connection.close()


def main() -> None:
    args = parse_args()
    backfill(args.dataset, args.limit, args.batch_size)


if __name__ == "__main__":
    main()
