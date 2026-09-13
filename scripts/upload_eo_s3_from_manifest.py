import asyncio
import httpx
import json
import os
import time
import tempfile
import hashlib
from pathlib import Path


MANIFEST = Path(os.environ.get("EO_S3_MANIFEST", "data/eo-s3-manifest.jsonl"))
DONE = Path(os.environ.get("EO_S3_DONE", "data/eo-s3-upload-done.jsonl"))
FAIL = Path(os.environ.get("EO_S3_FAIL", "data/eo-s3-upload-failed.jsonl"))
CONCURRENCY = int(os.environ.get("EO_S3_CONCURRENCY", "80"))


items = [json.loads(line) for line in MANIFEST.read_text().splitlines() if line.strip()]
done_dates = set()
if DONE.exists():
    for line in DONE.read_text().splitlines():
        if line.strip():
            try:
                done_dates.add(json.loads(line)["date"])
            except Exception:
                pass

done_dates.intersection_update(item["date"] for item in items)
pending = [item for item in items if item["date"] not in done_dates]
lock = asyncio.Lock()
counts = {"ok": len(done_dates), "failed": 0, "seen": 0}
start = time.time()


async def append(path: Path, obj: dict) -> None:
    async with lock:
        with path.open("a") as file:
            file.write(json.dumps(obj, separators=(",", ":")) + "\n")


async def progress(force: bool = False) -> None:
    total = len(items)
    done = counts["ok"] + counts["failed"]
    if force or done % 25 == 0:
        elapsed = max(1.0, time.time() - start)
        rate = counts["seen"] / elapsed
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"progress {done}/{total} ({done / total * 100:.1f}%) "
            f"uploaded={counts['ok']} failed={counts['failed']} rate={rate:.2f}/s",
            flush=True,
        )


async def worker(queue: asyncio.Queue, client: httpx.AsyncClient) -> None:
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            for attempt in range(3):
                try:
                    with tempfile.TemporaryFile() as media:
                        size = 0
                        digest = hashlib.sha256()
                        async with client.stream("GET", item.get("fetch_url", item["source_url"])) as source:
                            source.raise_for_status()
                            if "text/html" in source.headers.get("content-type", ""):
                                raise ValueError("source returned HTML instead of media")
                            async for chunk in source.aiter_bytes():
                                media.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        if not size:
                            raise ValueError("source returned empty media")
                        media.seek(0)
                        async def body():
                            while chunk := media.read(262144):
                                yield chunk
                        upload = await client.put(
                            item["put_url"], content=body(),
                            headers={"Content-Type": item["content_type"],
                                     "Content-Length": str(size),
                                     "x-amz-server-side-encryption": "AES256"},
                        )
                        upload.raise_for_status()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)
            await append(
                DONE,
                {
                    "date": item["date"],
                    "key": item["key"],
                    "public_url": item["public_url"],
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "source_url": item["source_url"],
                    "fetch_url": item.get("fetch_url", item["source_url"]),
                },
            )
            async with lock:
                counts["ok"] += 1
                counts["seen"] += 1
        except Exception as exc:
            await append(
                FAIL,
                {
                    "date": item.get("date"),
                    "key": item.get("key"),
                    "source_url": item.get("source_url"),
                    "error": type(exc).__name__,
                    "status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                },
            )
            async with lock:
                counts["failed"] += 1
                counts["seen"] += 1
        finally:
            queue.task_done()
            await progress()


async def main() -> None:
    print(
        f"start total={len(items)} already_done={len(done_dates)} "
        f"pending={len(pending)} concurrency={CONCURRENCY}",
        flush=True,
    )
    queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    limits = httpx.Limits(max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY)
    timeout = httpx.Timeout(120.0, connect=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, limits=limits) as client:
        tasks = [asyncio.create_task(worker(queue, client)) for _ in range(CONCURRENCY)]
        await queue.join()
        for task in tasks:
            task.cancel()

    await progress(force=True)
    print("complete", flush=True)


asyncio.run(main())
