"""Archive Earth Observatory image media to S3 from the production host.

This script is intentionally stdlib-only for AWS uploads. It can run on the
deployed Oracle host even when boto3 and the AWS CLI are not installed.

Run on the production host from the repository root:

    PYTHONPATH=. .venv/bin/python scripts/archive_earth_observatory_media.py --workers 75

Required remote environment, either exported or present in .env:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY

Optional:
    AWS_SESSION_TOKEN
    AWS_REGION / AWS_DEFAULT_REGION
    EO_ARCHIVE_BUCKET
    EO_ARCHIVE_BASE_URL
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from app.database import connect_earth_observatory_database
from app.media import MEDIA_BASE_URL


IMAGE_PREFIX = "eo/image"
DEFAULT_WORKERS = 75
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class MediaRow:
    date: str
    media_type: str
    media_url: str
    url_fallback: str | None
    s3_object_key: str | None


@dataclass(frozen=True)
class UploadResult:
    date: str
    source_url: str
    s3_object_key: str
    s3_url: str
    sha256: str
    bytes_count: int
    content_type: str
    status: str
    error: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def parse_s3_base_url(base_url: str) -> tuple[str, str]:
    host = urlparse(base_url).netloc
    match = re.fullmatch(r"(?P<bucket>.+)\.s3\.(?P<region>[a-z0-9-]+)\.amazonaws\.com", host)
    if not match:
        raise RuntimeError(f"cannot derive bucket/region from MEDIA_BASE_URL host {host!r}")
    return match.group("bucket"), match.group("region")


def require_aws_config() -> tuple[str, str, str | None, str, str, str]:
    load_dotenv(Path(".env"))
    default_bucket, default_region = parse_s3_base_url(os.environ.get("EO_ARCHIVE_BASE_URL", MEDIA_BASE_URL))
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or default_region
    bucket = os.environ.get("EO_ARCHIVE_BUCKET") or default_bucket
    base_url = os.environ.get("EO_ARCHIVE_BASE_URL") or f"https://{bucket}.s3.{region}.amazonaws.com"
    if not access_key or not secret_key:
        raise RuntimeError("missing AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY on the remote host")
    return access_key, secret_key, session_token, region, bucket, base_url.rstrip("/")


def ensure_columns(connection: Any) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(earth_observatory_pictures)").fetchall()}
    additions = {
        "s3_object_key": "TEXT",
        "s3_url": "TEXT",
        "archive_sha256": "TEXT",
        "archive_content_length": "INTEGER",
        "archive_content_type": "TEXT",
    }
    for name, kind in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE earth_observatory_pictures ADD COLUMN {name} {kind}")
    connection.commit()


def fetch_rows(connection: Any) -> list[MediaRow]:
    rows = connection.execute(
        """SELECT date, media_type, media_url, url_fallback, s3_object_key
           FROM earth_observatory_pictures
           ORDER BY date"""
    ).fetchall()
    return [MediaRow(row[0], row[1], row[2], row[3], row[4]) for row in rows]


def source_url_for(row: MediaRow, base_url: str) -> str:
    if row.media_url.startswith(base_url + "/") and row.url_fallback:
        return row.url_fallback
    return row.media_url


def content_extension(source_url: str, content_type: str) -> str:
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower() or "")
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    path_suffix = Path(urlparse(source_url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{2,5}", path_suffix):
        return path_suffix
    return ".jpg"


def signed_headers(
    *,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    region: str,
    bucket: str,
    key: str,
    payload_hash: str,
    content_type: str,
) -> dict[str, str]:
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = f"{bucket}.s3.{region}.amazonaws.com"
    canonical_uri = "/" + quote(key, safe="/")
    headers = {
        "content-type": content_type,
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_header_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",
        canonical_headers,
        signed_header_names,
        payload_hash,
    ])
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def sign(key_bytes: bytes, message: str) -> bytes:
        return hmac.new(key_bytes, message.encode(), hashlib.sha256).digest()

    signing_key = sign(("AWS4" + secret_key).encode(), date_stamp)
    signing_key = sign(signing_key, region)
    signing_key = sign(signing_key, "s3")
    signing_key = sign(signing_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, "
        f"Signature={signature}"
    )
    return headers


def download_source(source_url: str) -> tuple[bytes, str]:
    request = Request(source_url, headers={"User-Agent": "cosmofy-eo-archiver/1.0"})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("content-type", "application/octet-stream")
        data = response.read()
    if not data:
        raise RuntimeError("empty response body")
    return data, content_type


def upload_to_s3(
    *,
    data: bytes,
    content_type: str,
    key: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    region: str,
    bucket: str,
) -> None:
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = signed_headers(
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        region=region,
        bucket=bucket,
        key=key,
        payload_hash=payload_hash,
        content_type=content_type,
    )
    request = Request(
        f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key, safe='/')}",
        data=data,
        headers=headers,
        method="PUT",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"s3 put returned HTTP {response.status}")


def process_row(row: MediaRow, aws: tuple[str, str, str | None, str, str, str]) -> UploadResult:
    access_key, secret_key, session_token, region, bucket, base_url = aws
    source_url = source_url_for(row, base_url)
    source_digest = hashlib.sha256(source_url.encode()).hexdigest()
    try:
        data, content_type = download_source(source_url)
        if not content_type.lower().startswith("image/"):
            raise RuntimeError(f"not an image content-type: {content_type}")
        ext = content_extension(source_url, content_type)
        key = f"{IMAGE_PREFIX}/{source_digest}{ext}"
        file_hash = hashlib.sha256(data).hexdigest()
        upload_to_s3(
            data=data,
            content_type=content_type,
            key=key,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            region=region,
            bucket=bucket,
        )
        return UploadResult(
            date=row.date,
            source_url=source_url,
            s3_object_key=key,
            s3_url=f"{base_url}/{key}",
            sha256=file_hash,
            bytes_count=len(data),
            content_type=content_type,
            status="uploaded",
        )
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        return UploadResult(
            date=row.date,
            source_url=source_url,
            s3_object_key=f"{IMAGE_PREFIX}/{source_digest}",
            s3_url="",
            sha256="",
            bytes_count=0,
            content_type="",
            status="failed",
            error=str(exc),
        )


def update_mapping(connection: Any, result: UploadResult) -> None:
    connection.execute(
        """UPDATE earth_observatory_pictures
           SET s3_object_key = ?,
               s3_url = ?,
               archive_sha256 = ?,
               archive_content_length = ?,
               archive_content_type = ?,
               url_fallback = CASE
                   WHEN media_url = ? THEN ?
                   ELSE COALESCE(url_fallback, ?)
               END,
               media_url = ?
           WHERE date = ?
             AND media_type = 'image'
             AND (media_url = ? OR url_fallback = ?)""",
        (
            result.s3_object_key,
            result.s3_url,
            result.sha256,
            result.bytes_count,
            result.content_type,
            result.source_url,
            result.source_url,
            result.source_url,
            result.s3_url,
            result.date,
            result.source_url,
            result.source_url,
        ),
    )
    connection.commit()


def verify_public_url(url: str) -> tuple[int, int]:
    request = Request(url, method="HEAD", headers={"User-Agent": "cosmofy-eo-archiver/1.0"})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        length = int(response.headers.get("content-length", "0"))
        return response.status, length


def print_progress(done: int, total: int, uploaded: int, skipped: int, failed: int) -> None:
    percent = (done / total * 100) if total else 100
    print(
        f"{utc_now()} progress {done}/{total} ({percent:.1f}%) "
        f"uploaded={uploaded} skipped={skipped} failed={failed}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.workers < DEFAULT_WORKERS:
        raise SystemExit(f"--workers must be at least {DEFAULT_WORKERS}")

    aws = require_aws_config()
    _, _, _, _, bucket, base_url = aws
    log_dir = Path("data")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"earth-observatory-s3-archive-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"

    connection = connect_earth_observatory_database()
    try:
        if not args.dry_run:
            ensure_columns(connection)
        rows = fetch_rows(connection)
        videos = [row for row in rows if row.media_type == "video"]
        images = [row for row in rows if row.media_type == "image"]
        pending = [row for row in images if not row.s3_object_key]
        if args.limit is not None:
            pending = pending[: args.limit]
        total = len(pending)
        print(json.dumps({
            "event": "start",
            "bucket": bucket,
            "base_url": base_url,
            "prefix": IMAGE_PREFIX,
            "image_rows": len(images),
            "pending_images": total,
            "video_rows_skipped": len(videos),
            "workers": args.workers,
            "log_path": str(log_path),
            "dry_run": args.dry_run,
        }), flush=True)
        if args.dry_run:
            return 0

        uploaded = 0
        failed = 0
        samples: list[UploadResult] = []
        with log_path.open("a", encoding="utf-8") as log_file:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(process_row, row, aws): row for row in pending}
                for done, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    log_file.write(json.dumps(result.__dict__, sort_keys=True) + "\n")
                    log_file.flush()
                    if result.status == "uploaded":
                        update_mapping(connection, result)
                        uploaded += 1
                        if len(samples) < 5:
                            samples.append(result)
                    else:
                        failed += 1
                    print_progress(done, total, uploaded, len(videos), failed)

        verified = []
        for sample in samples:
            try:
                status, length = verify_public_url(sample.s3_url)
                verified.append({"date": sample.date, "status": status, "content_length": length, "url": sample.s3_url})
            except Exception as exc:  # noqa: BLE001 - report verification failures without hiding completed uploads.
                verified.append({"date": sample.date, "error": str(exc), "url": sample.s3_url})

        mapped = connection.execute(
            "SELECT count(*) FROM earth_observatory_pictures WHERE media_type='image' AND s3_object_key IS NOT NULL"
        ).fetchone()[0]
        print(json.dumps({
            "event": "complete",
            "bucket": bucket,
            "prefix": IMAGE_PREFIX,
            "image_rows": len(images),
            "mapped_image_rows": mapped,
            "video_rows_skipped": len(videos),
            "uploaded_this_run": uploaded,
            "failed_this_run": failed,
            "log_path": str(log_path),
            "sample_url_checks": verified,
        }), flush=True)
        return 1 if failed else 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
