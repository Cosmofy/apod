"""Upload a prebuilt EO database directly from Oracle to Turso."""
import json
from pathlib import Path
import httpx

config = json.loads(Path("data/eo-upload-config.json").read_text())
database = Path("data/earth-observatory-built.db")
size = database.stat().st_size
if size > 2_000_000_000:
    raise RuntimeError("built database exceeds documented import size limit")
def body():
    with database.open("rb") as stream:
        while chunk := stream.read(1024*1024):
            yield chunk
with httpx.Client(timeout=httpx.Timeout(1800,connect=30)) as client:
    response = client.post(f"https://{config['hostname']}/v1/upload",content=body(),
        headers={"Authorization":f"Bearer {config['token']}","Content-Length":str(size),"Content-Type":"application/octet-stream"})
    print(json.dumps({"status":response.status_code,"bytes":size}),flush=True)
    response.raise_for_status()
