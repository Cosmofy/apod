"""Combine verified/resumed/repaired records by publication date on Oracle."""
import json
from pathlib import Path

records = {}
for filename in ("eo-recovery-verified.jsonl", "eo-s3-upload-done.jsonl", "eo-repairs-done.jsonl"):
    for line in Path("data", filename).read_text().splitlines():
        row = json.loads(line)
        records[row["date"]] = row
if len(records) != 7047:
    raise RuntimeError(f"expected 7047 image dates, found {len(records)}")
output = Path("data/eo-media-final-manifest.jsonl")
output.write_text("".join(json.dumps(records[day])+"\n" for day in sorted(records)))
print(json.dumps({"image_dates":len(records),"unique_keys":len({r['key'] for r in records.values()}),"manifest":str(output)}),flush=True)
