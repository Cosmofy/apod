#!/usr/bin/env python3
"""Back up an existing .env and atomically set only the two EO database keys.

Reads {"hostname": "...", "token": "..."} from the upload configuration.
Uses python-dotenv (installed with pydantic-settings). No networking, restarts,
deployment, or secret output. Run only after the uploaded database is verified.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from dotenv import dotenv_values, set_key


EO_KEYS = {"EO_DATABASE_URL", "EO_AUTH_TOKEN"}


class ConfigurationError(Exception):
    """Only static, secret-free error codes may be passed to this exception."""


def read_regular_file(path):
    # Do not follow a symlink substituted between validation and open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("regular_file_required")
        return stream.read(), metadata


def configure(config_path, env_path):
    config_bytes, _ = read_regular_file(config_path)
    try:
        config = json.loads(config_bytes)
    except (ValueError, UnicodeError):
        raise ConfigurationError("invalid_config_json") from None
    if not isinstance(config, dict):
        raise ConfigurationError("config_object_required")
    hostname, token = config.get("hostname"), config.get("token")
    if not isinstance(hostname, str) or not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", hostname
    ):
        raise ConfigurationError("invalid_config_hostname")
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
        raise ConfigurationError("invalid_config_token")

    original, metadata = read_regular_file(env_path)
    try:
        original.decode("utf-8")
    except UnicodeError:
        raise ConfigurationError("env_must_be_utf8") from None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = env_path.with_name(f"{env_path.name}.before-eo-{stamp}.bak")
    # Exclusive creation and mode at open time avoid any world-readable window.
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())

    staged = None
    try:
        fd, filename = tempfile.mkstemp(prefix=f".{env_path.name}.eo-", dir=env_path.parent)
        staged = Path(filename)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
        before = dotenv_values(staged, interpolate=False)
        values = {"EO_DATABASE_URL": f"libsql://{hostname}", "EO_AUTH_TOKEN": token}
        for key, value in values.items():
            if set_key(str(staged), key, value, quote_mode="always", encoding="utf-8")[0] is not True:
                raise ConfigurationError("dotenv_update_failed")
        after = dotenv_values(staged, interpolate=False)
        if any(after.get(key) != value for key, value in values.items()):
            raise ConfigurationError("eo_value_verification_failed")
        if {k: v for k, v in before.items() if k not in EO_KEYS} != {k: v for k, v in after.items() if k not in EO_KEYS}:
            raise ConfigurationError("unrelated_env_change_detected")
        # dotenv rewrites its own temporary file, so set metadata after set_key.
        os.chmod(staged, 0o600)
        staged_stat = staged.stat()
        if (staged_stat.st_uid, staged_stat.st_gid) != (metadata.st_uid, metadata.st_gid):
            os.chown(staged, metadata.st_uid, metadata.st_gid)
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        current, current_metadata = read_regular_file(env_path)
        if current != original or (current_metadata.st_dev, current_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ConfigurationError("env_changed_during_update")
        os.replace(staged, env_path)
        staged = None
        return {"ok": True, "backup_path": str(backup), "env_path": str(env_path),
                "updated_keys": sorted(EO_KEYS), "backup_mode": "0600", "env_mode": "0600",
                "restart_performed": False}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("data/eo-upload-config.json"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        summary = configure(args.config, args.env)
    except ConfigurationError as error:
        summary = {"ok": False, "error": str(error), "restart_performed": False}
    except (OSError, ValueError, UnicodeError):
        # Never print exception text, config values, or a traceback containing them.
        summary = {"ok": False, "error": "configuration_io_failed", "restart_performed": False}
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
