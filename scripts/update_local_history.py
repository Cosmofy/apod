import argparse
import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.nasa import fetch_apod, resolve_date
from scripts.build_local_history import DEFAULT_DATABASE, PendingApod, open_database, write_batch
from app.embeddings import build_embedding_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and embed APOD dates missing from the local database."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--concurrency", type=int, default=5)
    return parser.parse_args()


def dates_after_latest(connection: sqlite3.Connection, through: date) -> list[date]:
    latest_value = connection.execute("SELECT MAX(date) FROM apods").fetchone()[0]
    if latest_value is None:
        raise RuntimeError("local APOD database is empty")

    current = date.fromisoformat(latest_value) + timedelta(days=1)
    missing: list[date] = []
    while current <= through:
        missing.append(current)
        current += timedelta(days=1)
    return missing


async def fetch_dates(dates: list[date], concurrency: int) -> list[PendingApod]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        async def fetch_one(apod_date: date) -> PendingApod:
            async with semaphore:
                apod = await fetch_apod(apod_date, client)
                print(f"fetched={apod_date.isoformat()}", flush=True)
                return PendingApod(
                    apod=apod,
                    embedding_text=build_embedding_text(
                        apod.title,
                        apod.explanation,
                        apod.credit,
                    ),
                )

        return list(await asyncio.gather(*(fetch_one(item) for item in dates)))


async def update_local_database(database: Path, concurrency: int) -> None:
    connection = open_database(database)
    try:
        missing_dates = dates_after_latest(connection, resolve_date())
        if not missing_dates:
            print("local APOD database is already current", flush=True)
            return

        print(
            f"missing={len(missing_dates)} from={missing_dates[0]} through={missing_dates[-1]}",
            flush=True,
        )
        batch = await fetch_dates(missing_dates, concurrency)
        embedded = write_batch(connection, batch)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"updated={embedded}", flush=True)
    finally:
        connection.close()


def main() -> None:
    args = parse_args()
    asyncio.run(update_local_database(args.database, args.concurrency))


if __name__ == "__main__":
    main()
