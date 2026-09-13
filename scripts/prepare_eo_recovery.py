"""Prepare remote upload manifest using S3 metadata only; no media downloads."""
import hashlib
import json
from pathlib import Path
import boto3

bucket = "cosmofy-apod-hd-010025084205-eu-west-2"
s3 = boto3.client("s3", region_name="eu-west-2")
existing = {}
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="eo/image/"):
    existing.update({obj["Key"]: obj["Size"] for obj in page.get("Contents", [])})
manifest = [json.loads(line) for line in Path("/tmp/eo_s3_manifest.jsonl").read_text().splitlines()]
pending, verified = [], []
for item in manifest:
    url_key = "eo/image/" + hashlib.sha256(item["source_url"].encode()).hexdigest() + Path(item["key"]).suffix
    if not existing.get(item["key"], 0) and existing.get(url_key, 0):
        item["key"] = url_key
        item["public_url"] = f"https://{bucket}.s3.eu-west-2.amazonaws.com/{url_key}"
    if existing.get(item["key"], 0) > 0:
        verified.append({k: item[k] for k in ("date", "key", "source_url", "public_url")})
    else:
        item["put_url"] = s3.generate_presigned_url("put_object", Params={
            "Bucket": bucket, "Key": item["key"], "ContentType": item["content_type"],
            "ServerSideEncryption": "AES256"}, ExpiresIn=86400)
        pending.append(item)
for name, records in [("pending", pending), ("verified", verified)]:
    path = Path(f"/tmp/eo-recovery-{name}.jsonl")
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    path.chmod(0o600)
print(json.dumps({"total": len(manifest), "verified": len(verified), "pending": len(pending)}))
