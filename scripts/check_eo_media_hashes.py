"""Run on Oracle: compare three archived images against their source bytes."""
import asyncio
import hashlib
import json
from pathlib import Path
import httpx

async def main():
    rows = [json.loads(x) for x in Path("data/eo-s3-upload-done.jsonl").read_text().splitlines()]
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        async def digest(url):
            value = hashlib.sha256()
            size = 0
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    value.update(chunk)
                    size += len(chunk)
            return value.hexdigest(), size
        for position in (0, len(rows)//2, len(rows)-1):
            row = rows[position]
            source, archived = await asyncio.gather(digest(row.get("fetch_url", row["source_url"])), digest(row["public_url"]))
            print(json.dumps({"date":row["date"],"source_sha256":source[0],"s3_sha256":archived[0],"bytes":archived[1],"match":source==archived}),flush=True)
            if source != archived:
                raise RuntimeError("archive differs from source")

asyncio.run(main())
