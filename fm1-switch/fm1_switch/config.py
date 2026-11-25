"""One small config file, so a second bench needs no code edits (audit B13).

``~/.config/fm1/config.json`` (or the file named by ``FM1_CONFIG``)::

    {"units": 4, "library": "~/fm1-firmwares", "backups": "~/fm1-backups",
     "registry": "~/fm1-units.json", "portmap": "~/fm1-portmap.txt",
     "flasher": "/usr/bin/m-flasher"}

Every key is optional. An environment variable (FM1_LIBRARY, FM1_BACKUPS, FM1_UNITS,
FM1_PORTMAP, FM1_FLASHER) beats the file, and the file beats the built-in default, so
nothing that worked before changes. JSON, not TOML: the stdlib reads it on every Python
we support. A missing or broken file is ignored, never fatal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_UNITS = 8
DEFAULT_UNITS = 4


def config_path() -> Path:
    raw = os.environ.get("FM1_CONFIG")
    return Path(raw).expanduser() if raw else Path("~/.config/fm1/config.json").expanduser()


def load() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def unit_count() -> int:
    n = load().get("units", DEFAULT_UNITS)
    if isinstance(n, bool) or not isinstance(n, int):
        return DEFAULT_UNITS
    return max(1, min(MAX_UNITS, n))


def setting(env: str, key: str, default: str) -> str:
    """env var, else the file's ``key``, else ``default`` (a path or name, as text)."""
    raw = os.environ.get(env)
    if raw:
        return raw
    val = load().get(key)
    return val if isinstance(val, str) and val else default


def path_setting(env: str, key: str, default: str) -> Path:
    return Path(setting(env, key, default)).expanduser()
