"""Archive pending APOD/EO YouTube media as playable MKV objects in S3.

This is a separate worker from the FastAPI process. It is intended to run on a
trusted production host with yt-dlp, ffmpeg, and a protected worker-only AWS
credential environment. No credentials belong in the repository or API .env.

Examples:
    PYTHONPATH=. .venv/bin/python scripts/archive_youtube_media.py --date 2026-09-14
    PYTHONPATH=. .venv/bin/python scripts/archive_youtube_media.py --all-pending --limit 10
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from app.database import connect_database, connect_earth_observatory_database
from scripts.archive_earth_observatory_media import require_aws_config, upload_to_s3


YOUTUBE_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/", re.I)
MARKER_PREFIX = "cosmofy:youtube:"
DEFAULT_WORKERS = 1


@dataclass(frozen=True)
class VideoRow:
    source: str
    date: str
    title: str
    media_url: str
    explanation: str
    url_fallback: str | None
    s3_object_key: str | None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def canonical_youtube_url(value: str) -> str | None:
    return value.strip() if value and YOUTUBE_RE.search(value) else None


def marker(explanation: str, source_url: str) -> str:
    tag = f"{MARKER_PREFIX}{source_url}"
    return explanation if tag in explanation else f"{explanation.rstrip()}\n\n{tag}"


def select_rows(connection: Any, source: str, requested_date: str | None, limit: int | None) -> list[VideoRow]:
    where = "media_type='video' AND s3_object_key IS NULL"
    params: list[str | int] = []
    if requested_date:
        where += " AND date=?"
        params.append(requested_date)
    sql_limit = " LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    rows = connection.execute(
        f"SELECT date,title,media_url,explanation,url_fallback,s3_object_key FROM apods WHERE {where} ORDER BY date{sql_limit}",
        tuple(params),
    ).fetchall()
    return [VideoRow(source, row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows if canonical_youtube_url(row[2])]


def select_eo_rows(connection: Any, requested_date: str | None, limit: int | None) -> list[VideoRow]:
    where = "media_type='video' AND s3_object_key IS NULL"
    params: list[str | int] = []
    if requested_date:
        where += " AND date=?"
        params.append(requested_date)
    sql_limit = " LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    rows = connection.execute(
        f"SELECT date,title,media_url,explanation,url_fallback,s3_object_key FROM earth_observatory_pictures WHERE {where} ORDER BY date{sql_limit}",
        tuple(params),
    ).fetchall()
    return [VideoRow("earth_observatory", row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows if canonical_youtube_url(row[2])]


def download_video(row: VideoRow, directory: Path) -> tuple[Path, str]:
    output = directory / "%(upload_date>%Y-%m-%d)s - %(title).180B [%(id)s].%(ext)s"
    command = [
        os.environ.get("YTDLP_BIN", "yt-dlp"),
        "--force-ipv4", "--continue", "--no-overwrites", "--http-chunk-size", "10M",
        "--extractor-args", "youtube:player_client=default,ios,web",
        "-f", "bv*+ba/b", "--merge-output-format", "mkv",
        "-o", str(output), row.media_url,
    ]
    subprocess.run(command, check=True, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    files = sorted(directory.glob("*.mkv"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("yt-dlp completed without an MKV output")
    return files[0], row.media_url


def upload_row(row: VideoRow, aws: tuple[str, str, str | None, str, str, str]) -> dict[str, Any]:
    access_key, secret_key, session_token, region, bucket, base_url = aws
    source_url = canonical_youtube_url(row.media_url)
    if not source_url:
        raise RuntimeError("row is not YouTube-backed")
    digest = hashlib.sha256(source_url.encode()).hexdigest()
    prefix = "eo/video" if row.source == "earth_observatory" else "hd/video"
    key = f"{prefix}/{digest}.mkv"
    with tempfile.TemporaryDirectory(prefix="cosmofy-youtube-") as temp:
        video_path, _ = download_video(row, Path(temp))
        data = video_path.read_bytes()
    upload_to_s3(
        data=data, content_type="video/x-matroska", key=key,
        access_key=access_key, secret_key=secret_key, session_token=session_token,
        region=region, bucket=bucket,
    )
    return {
        "source": row.source, "date": row.date, "source_url": source_url,
        "key": key, "url": f"{base_url}/{key}", "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data), "explanation": marker(row.explanation, source_url),
        "fallback": row.url_fallback or source_url,
    }


def apply_mapping(connection: Any, result: dict[str, Any]) -> None:
    if result["source"] == "earth_observatory":
        connection.execute(
            """UPDATE earth_observatory_pictures
               SET explanation=?, media_url=?, url_fallback=?, s3_object_key=?
               WHERE date=? AND media_type='video' AND s3_object_key IS NULL""",
            (result["explanation"], result["url"], result["fallback"], result["key"], result["date"]),
        )
    else:
        connection.execute(
            """UPDATE apods
               SET explanation=?, media_url=?, hd_media_url=?, s3_object_key=?
               WHERE date=? AND media_type='video' AND s3_object_key IS NULL""",
            (result["explanation"], result["url"], result["fallback"], result["key"], result["date"]),
        )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuntimeError(f"mapping was not applied for {result['source']} {result['date']}")
    connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--source", choices=("apod", "earth-observatory", "all"), default="all")
    parser.add_argument("--all-pending", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if not args.date and not args.all_pending:
        raise SystemExit("provide --date for the daily worker or --all-pending for a backfill")

    aws = require_aws_config()
    apod_connection = connect_database() if args.source in ("apod", "all") else None
    eo_connection = connect_earth_observatory_database() if args.source in ("earth-observatory", "all") else None
    try:
        rows: list[VideoRow] = []
        if apod_connection:
            rows.extend(select_rows(apod_connection, "apod", args.date, args.limit))
        if eo_connection:
            rows.extend(select_eo_rows(eo_connection, args.date, args.limit))
        print(json.dumps({"event": "start", "date": args.date, "rows": len(rows), "timestamp": utc_now()}), flush=True)
        for index, row in enumerate(rows, 1):
            try:
                result = upload_row(row, aws)
                apply_mapping(eo_connection if row.source == "earth_observatory" else apod_connection, result)
                print(json.dumps({"event": "uploaded", **{k: result[k] for k in ("source", "date", "key", "bytes")}, "progress": f"{index}/{len(rows)}"}), flush=True)
            except Exception as exc:  # noqa: BLE001 - continue and report each independent video.
                print(json.dumps({"event": "failed", "source": row.source, "date": row.date, "error": type(exc).__name__, "detail": str(exc), "progress": f"{index}/{len(rows)}"}), flush=True)
        return 0
    finally:
        if apod_connection:
            apod_connection.close()
        if eo_connection:
            eo_connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
