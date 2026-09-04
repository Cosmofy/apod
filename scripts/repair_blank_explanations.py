import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.database import connect_database
from app.embeddings import build_embedding_text, create_embeddings, encode_embedding


DEFAULT_REPAIR_DIRECTORY = Path("data/apod-repair")
FORBIDDEN_TEXT = (
    "tomorrow's picture:",
    "authors & editors:",
    "nasa technical rep.:",
    "archive | index | search",
)


@dataclass(frozen=True)
class Repair:
    apod_date: date
    explanation: str
    source_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair blank APOD explanations and regenerate their embeddings."
    )
    parser.add_argument("--directory", type=Path, default=DEFAULT_REPAIR_DIRECTORY)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write validated repairs to the configured Turso database.",
    )
    return parser.parse_args()


def load_repairs(directory: Path) -> list[Repair]:
    paths = sorted(directory.glob("explanations-*.jsonl"))
    if not paths:
        raise FileNotFoundError(f"no repair files found in {directory}")

    repairs: list[Repair] = []
    seen_dates: set[date] = set()
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if set(value) != {"date", "explanation", "source_url"}:
                raise ValueError(f"unexpected fields in {path}:{line_number}")

            apod_date = date.fromisoformat(value["date"])
            explanation = " ".join(value["explanation"].split())
            source_url = value["source_url"]
            expected_url = f"https://apod.nasa.gov/apod/ap{apod_date:%y%m%d}.html"

            if apod_date in seen_dates:
                raise ValueError(f"duplicate repair date: {apod_date}")
            if not explanation:
                raise ValueError(f"blank repair explanation: {apod_date}")
            if source_url != expected_url:
                raise ValueError(f"unexpected source URL: {apod_date}")
            lowered = explanation.lower()
            if any(text in lowered for text in FORBIDDEN_TEXT):
                raise ValueError(f"footer boilerplate detected: {apod_date}")
            if "<" in explanation or ">" in explanation:
                raise ValueError(f"possible HTML detected: {apod_date}")

            seen_dates.add(apod_date)
            repairs.append(Repair(apod_date, explanation, source_url))

    return sorted(repairs, key=lambda item: item.apod_date)


def validate_database_targets(connection, repairs: list[Repair]) -> list[tuple[str, str | None]]:
    blank_rows = connection.execute(
        """
        SELECT date
        FROM apods
        WHERE trim(coalesce(explanation, '')) = ''
        ORDER BY date
        """
    ).fetchall()
    blank_dates = {row[0] for row in blank_rows}
    repair_dates = {item.apod_date.isoformat() for item in repairs}
    if blank_dates != repair_dates:
        missing = sorted(blank_dates - repair_dates)
        unexpected = sorted(repair_dates - blank_dates)
        raise RuntimeError(
            "repair files do not exactly match blank database rows: "
            f"missing={len(missing)} unexpected={len(unexpected)}"
        )

    placeholders = ",".join("?" for _ in repairs)
    rows = connection.execute(
        f"""
        SELECT date, title, credit
        FROM apods
        WHERE date IN ({placeholders})
        ORDER BY date
        """,
        tuple(sorted(repair_dates)),
    ).fetchall()
    if len(rows) != len(repairs):
        raise RuntimeError("not every repair date exists in apods")
    return [(row[1], row[2]) for row in rows]


def create_repair_embeddings(
    repairs: list[Repair],
    titles_and_credits: list[tuple[str, str | None]],
    batch_size: int,
) -> list[bytes]:
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")

    texts = [
        build_embedding_text(title, repair.explanation, credit)
        for repair, (title, credit) in zip(
            repairs,
            titles_and_credits,
            strict=True,
        )
    ]
    encoded: list[bytes] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors = create_embeddings(batch)
        if len(vectors) != len(batch):
            raise RuntimeError("OpenAI returned an unexpected embedding count")
        encoded.extend(encode_embedding(vector) for vector in vectors)
        print(
            f"embedded={len(encoded)}/{len(texts)}",
            flush=True,
        )
    return encoded


def apply_repairs(connection, repairs: list[Repair], embeddings: list[bytes]) -> None:
    table_rows = connection.execute(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table'
          AND name IN ('apod_embeddings', 'apod_embeddings_reindexed')
        """
    ).fetchall()
    embedding_tables = {row[0] for row in table_rows}
    expected_tables = {"apod_embeddings", "apod_embeddings_reindexed"}
    if embedding_tables != expected_tables:
        raise RuntimeError(f"unexpected embedding tables: {sorted(embedding_tables)}")

    explanation_rows = [
        (repair.explanation, repair.apod_date.isoformat())
        for repair in repairs
    ]
    embedding_rows = [
        (repair.apod_date.isoformat(), embedding)
        for repair, embedding in zip(repairs, embeddings, strict=True)
    ]

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """
            UPDATE apods
            SET explanation = ?
            WHERE date = ?
              AND trim(coalesce(explanation, '')) = ''
            """,
            explanation_rows,
        )
        for table in sorted(embedding_tables):
            connection.executemany(
                f"""
                INSERT INTO {table} (apod_date, embedding)
                VALUES (?, ?)
                ON CONFLICT(apod_date) DO UPDATE SET embedding = excluded.embedding
                """,
                embedding_rows,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def verify_repairs(connection, repairs: list[Repair]) -> None:
    repaired_dates = tuple(item.apod_date.isoformat() for item in repairs)
    placeholders = ",".join("?" for _ in repaired_dates)
    remaining_blank = connection.execute(
        f"""
        SELECT count(*)
        FROM apods
        WHERE date IN ({placeholders})
          AND trim(coalesce(explanation, '')) = ''
        """,
        repaired_dates,
    ).fetchone()[0]
    counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("apod_embeddings", "apod_embeddings_reindexed")
    }
    if remaining_blank:
        raise RuntimeError(f"repairs still blank: {remaining_blank}")
    if len(set(counts.values())) != 1:
        raise RuntimeError(f"embedding table counts differ: {counts}")
    print(
        f"verified repaired={len(repairs)} remaining_blank={remaining_blank} "
        f"embedding_rows={next(iter(counts.values()))}",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    repairs = load_repairs(args.directory)
    connection = connect_database()
    try:
        titles_and_credits = validate_database_targets(connection, repairs)
        print(f"validated repairs={len(repairs)} database_targets={len(titles_and_credits)}")
        if not args.apply:
            print("dry run complete; pass --apply to generate embeddings and write repairs")
            return

        embeddings = create_repair_embeddings(
            repairs,
            titles_and_credits,
            args.batch_size,
        )
        apply_repairs(connection, repairs, embeddings)
        verify_repairs(connection, repairs)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
