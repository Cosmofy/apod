"""Validate public archived media signatures remotely with bounded range reads."""
import asyncio
import json
import sys
from pathlib import Path
import httpx

async def main():
    rows = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line]
    if len(sys.argv) > 3:
        rows = rows[int(sys.argv[3]):]
    semaphore = asyncio.Semaphore(75)
    failures = []
    completed = 0
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=75)) as client:
        async def check(row):
            nonlocal completed
            async with semaphore:
                try:
                    async with client.stream("GET", row["public_url"], headers={"Range":"bytes=0-63"}) as r:
                        r.raise_for_status()
                        prefix = b""
                        async for chunk in r.aiter_bytes():
                            prefix += chunk[:64-len(prefix)]
                            if len(prefix) >= 64:
                                break
                        signatures = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")
                        valid = prefix.startswith(signatures) or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP") or b"ftypavif" in prefix
                        if not valid:
                            raise ValueError("invalid image signature")
                except Exception as exc:
                    failures.append({"date":row["date"],"key":row["key"],"error":type(exc).__name__})
                completed += 1
                if completed % 250 == 0:
                    print(f"verified {completed}/{len(rows)} failures={len(failures)}",flush=True)
        await asyncio.gather(*(check(row) for row in rows))
    Path(sys.argv[2]).write_text("".join(json.dumps(row)+"\n" for row in failures))
    print(json.dumps({"checked":len(rows),"passed":len(rows)-len(failures),"failed":len(failures)}),flush=True)

asyncio.run(main())
