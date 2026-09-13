"""Resumable one-time Earth Observatory metadata import.

Imports NASA EO Image of the Day metadata, article text, media URLs, and Explorer
coordinates into Turso. It deliberately does not download or archive media.

Run from a Pictures production node:
  PYTHONPATH=. .venv/bin/python scripts/import_earth_observatory.py
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.database import connect_database
from app.earth_observatory import _media_from_post, _parse_image_date, _parse_location, _text, _wordpress_editorial_content

EXPLORER_URL = "https://science.nasa.gov/earth/earth-observatory/explorer/"
WP_POST_URL = "https://science.nasa.gov/wp-json/wp/v2/posts"
PROGRESS_EVERY = 25


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}", flush=True)


def load_pins(html: str) -> list[dict[str, Any]]:
    marker = 'window["smdMapData_'
    start = html.rfind(marker)
    if start < 0:
        raise ValueError("NASA Explorer map payload not found")
    value_start = html.index("=", start) + 1
    payload, _ = json.JSONDecoder().raw_decode(html[value_start:].lstrip())
    pins = payload.get("pins")
    if not isinstance(pins, list):
        raise ValueError("NASA Explorer pin payload is invalid")
    # One date maps to one daily record. Keep the first stable pin if NASA has a duplicate.
    unique: dict[str, dict[str, Any]] = {}
    for pin in pins:
        published = pin.get("published_date")
        if not isinstance(published, str):
            continue
        try:
            day = datetime.strptime(published, "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        unique.setdefault(day, pin)
    return [unique[day] for day in sorted(unique)]


async def fetch_record(client: httpx.AsyncClient, pin: dict[str, Any], retries: int) -> dict[str, Any]:
    url = pin["permalink"]
    last_error = "unknown error"
    for attempt in range(retries):
        try:
            slug = url.rstrip("/").split("/")[-1]
            response = await client.get(WP_POST_URL, params={"slug": slug}, follow_redirects=True)
            response.raise_for_status()
            posts = response.json()
            if not isinstance(posts, list) or len(posts) != 1:
                raise ValueError("article post missing")
            editorial_html = _wordpress_editorial_content(posts[0].get("content", {}).get("rendered", ""))
            explanation = _text(editorial_html)
            media = await _media_from_post(client, posts[0], editorial_html)
            if not explanation or media is None:
                raise ValueError("missing article text or media")
            media_type, url_primary = media
            credit_match = re.search(r"(Astronaut photograph.*?)(?:Story by|NASA Earth Observatory/)", explanation, re.I | re.S)
            day = datetime.strptime(pin["published_date"], "%B %d, %Y").date()
            return {
                "date": day.isoformat(),
                "title": pin["title"],
                "explanation": explanation,
                "media_type": media_type,
                "media_url": url_primary,
                "url_fallback": pin.get("featured_image") if media_type == "image" else None,
                "credit": " ".join(credit_match.group(1).split()) if credit_match else None,
                "article_url": url,
                "image_date": _parse_image_date(explanation).isoformat() if _parse_image_date(explanation) else None,
                "location_name": _parse_location(explanation),
                "latitude": pin.get("lat"),
                "longitude": pin.get("lng"),
            }
        except (httpx.HTTPError, ValueError, KeyError) as error:
            last_error = str(error)
            if attempt + 1 < retries:
                await asyncio.sleep(0.75 * (attempt + 1))
    raise RuntimeError(last_error)


def save_batch(connection: Any, records: list[dict[str, Any]]) -> None:
    connection.execute("BEGIN")
    try:
        for item in records:
            connection.execute(
                """INSERT INTO earth_observatory_pictures
                   (date,title,explanation,media_type,media_url,url_fallback,credit,copyright,article_url,image_date,location_name,latitude,longitude)
                   VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     title=excluded.title, explanation=excluded.explanation, media_type=excluded.media_type, media_url=excluded.media_url,
                     url_fallback=excluded.url_fallback, credit=excluded.credit, article_url=excluded.article_url,
                     image_date=excluded.image_date, location_name=excluded.location_name,
                     latitude=excluded.latitude, longitude=excluded.longitude""",
                (item["date"], item["title"], item["explanation"], item["media_type"], item["media_url"], item["url_fallback"],
                 item["credit"], item["article_url"], item["image_date"], item["location_name"],
                 item["latitude"], item["longitude"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def write_checkpoint(path: Path, *, total: int, completed: int, imported: int, skipped: int, failed: int, failures: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "total": total, "completed": completed,
               "percent": round(completed * 100 / total, 2), "imported": imported, "skipped": skipped,
               "failed": failed, "failures": failures[-100:]}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--checkpoint", type=Path, default=Path("data/earth-observatory-import-progress.json"))
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 20:
        raise SystemExit("--concurrency must be between 1 and 20")

    async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=8), headers={"User-Agent": "Cosmofy Pictures importer/1.0"}) as client:
        response = await client.get(EXPLORER_URL, follow_redirects=True)
        response.raise_for_status()
        pins = load_pins(response.text)
        if args.limit:
            pins = pins[:args.limit]

        connection = connect_database()
        try:
            already_imported = {row[0] for row in connection.execute("SELECT date FROM earth_observatory_pictures").fetchall()}
            pending = []
            for pin in pins:
                day = datetime.strptime(pin["published_date"], "%B %d, %Y").date().isoformat()
                if day not in already_imported:
                    pending.append(pin)
            total = len(pins)
            skipped = total - len(pending)
            completed = skipped
            imported = 0
            failed = 0
            failures: list[dict[str, str]] = []
            log(f"start total={total} pending={len(pending)} skipped={skipped} concurrency={args.concurrency}")
            write_checkpoint(args.checkpoint, total=total, completed=completed, imported=imported, skipped=skipped, failed=failed, failures=failures)

            semaphore = asyncio.Semaphore(args.concurrency)
            async def guarded(pin: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
                async with semaphore:
                    try:
                        return pin, await fetch_record(client, pin, args.retries), None
                    except Exception as error:
                        return pin, None, str(error)

            batch: list[dict[str, Any]] = []
            for future in asyncio.as_completed([guarded(pin) for pin in pending]):
                pin, record, error = await future
                completed += 1
                if record:
                    batch.append(record)
                    imported += 1
                else:
                    failed += 1
                    failures.append({"title": str(pin.get("title")), "url": str(pin.get("permalink")), "error": error or "unknown"})
                if len(batch) >= PROGRESS_EVERY:
                    save_batch(connection, batch)
                    batch.clear()
                if completed % PROGRESS_EVERY == 0 or completed == total:
                    write_checkpoint(args.checkpoint, total=total, completed=completed, imported=imported, skipped=skipped, failed=failed, failures=failures)
                    log(f"progress {completed}/{total} ({completed * 100 / total:.1f}%) imported={imported} skipped={skipped} failed={failed}")
            if batch:
                save_batch(connection, batch)
            write_checkpoint(args.checkpoint, total=total, completed=completed, imported=imported, skipped=skipped, failed=failed, failures=failures)
            log(f"complete {completed}/{total} (100.0%) imported={imported} skipped={skipped} failed={failed}")
        finally:
            connection.close()


if __name__ == "__main__":
    asyncio.run(main())
