"""Presign verified large-media replacements without downloading any media."""
import json
from pathlib import Path
import boto3

repairs = json.loads(Path("data/eo-source-repairs-research.json").read_text())
original = {r["date"]: r for r in map(json.loads, Path("/tmp/eo_s3_manifest.jsonl").read_text().splitlines())}
s3 = boto3.client("s3", region_name="eu-west-2")
rows = []
for repair in repairs:
    fetch_url = repair.get("fetch_url") or repair.get("verified_archive_fetch_url")
    if not fetch_url:
        continue
    row = dict(original[repair["date"]])
    row["fetch_url"] = fetch_url
    row["put_url"] = s3.generate_presigned_url("put_object", Params={
        "Bucket":"cosmofy-apod-hd-010025084205-eu-west-2", "Key":row["key"],
        "ContentType":row["content_type"], "ServerSideEncryption":"AES256"}, ExpiresIn=86400)
    rows.append(row)
path = Path("/tmp/eo-repairs-manifest.jsonl")
path.write_text("".join(json.dumps(r)+"\n" for r in rows))
path.chmod(0o600)
print(f"prepared {len(rows)} verified large-image repairs")
