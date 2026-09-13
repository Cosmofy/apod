"""Create an EO upload destination; retain the existing APOD database untouched."""
import json
import subprocess
from pathlib import Path
import httpx

name = "earth-observatory-us"
token = subprocess.check_output(["turso", "auth", "token"], text=True).strip()
headers = {"Authorization":f"Bearer {token}"}
with httpx.Client(timeout=60) as client:
    response = client.post("https://api.turso.tech/v1/organizations/bhatnag8/databases", headers=headers,
        json={"name":name,"group":"default","seed":{"type":"database_upload"}})
    response.raise_for_status()
    database = response.json()["database"]
hostname = database.get("Hostname") or database.get("hostname")
if not hostname:
    raise RuntimeError("database response lacks hostname")
database_token = subprocess.check_output(["turso","db","tokens","create",name],text=True).strip()
path = Path("/tmp/eo-upload-config.json")
path.touch(mode=0o600, exist_ok=False)
path.write_text(json.dumps({"hostname":hostname,"token":database_token}))
print(json.dumps({"database":name,"hostname":hostname,"upload_config":str(path)}))
