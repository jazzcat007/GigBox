"""One config file and one schema, so nothing tunable is hardcoded (audit B13).

``~/.config/fm1/config.json`` (or the file named by ``FM1_CONFIG``)::

    {"units": 4, "library": "~/fm1-firmwares", "backups": "~/fm1-backups",
     "auto_backups_kept": 20, "flash_timeout": 300, "hub_max_age_hours": 24}

Every key is optional. An environment variable (FM1_LIBRARY, FM1_BACKUPS, ...) beats the
file, and the file beats the built-in default, so nothing that worked before changes. JSON,
not TOML: the stdlib reads it on every Python we support. A missing or broken file is
ignored, never fatal, and one bad value falls back to that setting's default.

**The schema (:data:`SPECS`) is the single source of truth.** Each setting has a key, a type,
a default, bounds, a label and help text. The code reads values through :func:`value` /
:func:`path` at the moment it needs them (not at import), and the web admin's Settings page
is generated from the same list and writes the same file through :func:`save`. So adding a
tunable is one entry here, and it shows up in the website without touching the UI.

Two kinds of setting are shown on the website but are **not editable from it**
(``editable=False``): ones that decide what code runs or who may connect (the flasher binary,
the maker's installer, the hub the firmware comes from). Those stay in the file or the
environment, where someone with a shell chose them.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_UNITS = 16
DEFAULT_UNITS = 4


class ConfigError(ValueError):
    """A setting the website (or CLI) asked to change is not acceptable."""


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str                     # "path" | "int" | "float" | "text" | "url" | "bool" | "choice"
    default: Any
    label: str
    group: str
    help: str = ""
    env: str | None = None        # an environment variable that beats the file
    min: float | None = None
    max: float | None = None
    unit: str = ""                # shown after a number: "s", "min", "MB"
    pattern: str | None = None    # a text value must match this (name rules)
    choices: tuple = ()           # kind "choice": the allowed values, as text
    restart: bool = False         # takes effect only after the service restarts
    editable: bool = True         # False: shown, but only the file/env can change it
    why_locked: str = ""          # shown when editable is False


GROUPS: tuple[tuple[str, str], ...] = (
    ("rig", "Rig"),
    ("files", "Folders and files"),
    ("hub", "Firmware hub"),
    ("flash", "Flashing and waiting"),
    ("backups", "Backups"),
    ("router", "MIDI router and clock"),
    ("network", "WiFi and hotspot"),
    ("advanced", "Advanced (runtime files)"),
)

_NAME = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$"

SPECS: tuple[Spec, ...] = (
    # -- rig ---------------------------------------------------------------------------
    Spec("units", "int", DEFAULT_UNITS, "Number of units", "rig",
         "How many FM-1s this bench drives. Sticker letters A, B, C... follow from it.",
         min=1, max=MAX_UNITS, restart=True),
    Spec("patch", "text", "default", "Default routing patch", "rig",
         "The patch the MIDI router follows when none has been chosen.",
         env="FM1_PATCH", pattern=_NAME),
    Spec("auto_reregister", "bool", True, "Re-register units after a power-up", "rig",
         "When units come back from a power cycle (or the cables moved) and the panel would ask "
         "to RE-REGISTER FLEET, do it automatically once the bus has settled. The units that "
         "need it restart, so sound stops for about half a minute."),
    Spec("auto_reregister_settle", "float", 20, "Wait for the units to settle", "rig",
         "How long the bus must stay unchanged before the automatic re-register starts, so "
         "units that power up one at a time are all there.", min=5, max=300, unit="s"),
    Spec("touch_device", "path", "/dev/input/event0", "Touch panel device", "rig",
         "The evdev node of the touchscreen, for the on-panel UI.", restart=True),
    # -- folders and files -------------------------------------------------------------
    Spec("library", "path", "~/fm1-firmwares", "Firmware library", "files",
         "Where firmware packages (.fwsc) are kept.", env="FM1_LIBRARY"),
    Spec("backups", "path", "~/fm1-backups", "Backups folder", "files",
         "Whole-flash images saved before every write, and the ones you take by hand.",
         env="FM1_BACKUPS"),
    Spec("registry", "path", "~/fm1-units.json", "Unit registry", "files",
         "Which flash id is which sticker letter.", env="FM1_UNITS"),
    Spec("portmap", "path", "~/fm1-portmap.txt", "USB port map", "files",
         "Which USB port each unit slot sits on (written by label-units.sh).",
         env="FM1_PORTMAP"),
    Spec("sets", "path", "~/fm1-sets.json", "Rig sets file", "files",
         "Saved rig sets and snapshots.", env="FM1_SETS"),
    Spec("patches", "path", "~/fm1-patches.json", "Routing patches file", "files",
         "The saved MIDI routing patches.", env="FM1_PATCHES"),
    Spec("layouts", "path", "~/fm1-layouts.json", "Channel layouts file", "files",
         "Which MIDI channels go to which unit (presets' unit settings and custom layouts).",
         env="FM1_LAYOUTS"),
    Spec("clock", "path", "~/fm1-clock.json", "Master clock file", "files",
         "Tempo and start/stop for the master clock.", env="FM1_CLOCK"),
    Spec("flashlog", "path", "~/fm1-flash.log", "Audit log", "files",
         "One JSON line per flash, restore, refusal and settings change.",
         env="FM1_FLASHLOG"),
    Spec("flasher", "path", "m-flasher", "m-flasher program", "files",
         "The program that does every write to a unit.", env="FM1_FLASHER",
         editable=False,
         why_locked="Decides what code runs with write access to the units; set it in "
                    "the config file or FM1_FLASHER."),
    Spec("maker_tools", "path", "~/fm1/vendor/jiant", "Maker installer folder", "files",
         "Where the maker's own installer for two-step firmwares lives.",
         env="FM1_MAKER_TOOLS", editable=False,
         why_locked="Code in this folder is run by the Pi; set it in the config file or "
                    "FM1_MAKER_TOOLS."),
    # -- hub -----------------------------------------------------------------------
    Spec("hub_url", "url", "https://thefirmwarehub.com", "Hub address", "hub",
         "Where the firmware catalogue and downloads come from.", editable=False,
         why_locked="It is the trust root for every firmware this Pi downloads; set it "
                    "in the config file."),
    Spec("hub_cache", "path", "~/fm1-hub", "Catalogue cache folder", "hub",
         "Where the catalogue is kept between runs.", env="FM1_HUB_CACHE"),
    Spec("inspect_cache", "path", "~/fm1-hub/inspect.json", "Inspection cache file", "hub",
         "Remembered verdicts on each package, so a page render stays fast.",
         env="FM1_INSPECT_CACHE", restart=True),
    Spec("hub_max_age_hours", "float", 24, "Catalogue stays fresh for", "hub",
         "The hub asks that the catalogue be read at most once a day; below that is "
         "your call.", min=1, max=24 * 30, unit="h"),
    Spec("hub_refresh_min_minutes", "float", 60, "Refresh button cool-down", "hub",
         "A forced refresh is honoured only when the cache is at least this old, so a "
         "looping client cannot hammer the hub.", min=1, max=24 * 60, unit="min"),
    Spec("hub_timeout", "float", 60, "Catalogue fetch timeout", "hub",
         "", min=5, max=600, unit="s"),
    Spec("download_timeout", "float", 180, "Package download timeout", "hub",
         "", min=10, max=1800, unit="s"),
    Spec("max_package_mb", "float", 4, "Largest package accepted", "hub",
         "Downloads bigger than this are refused. The largest catalogue entry is about "
         "1 MB.", min=1, max=64, unit="MB"),
    # -- flashing ------------------------------------------------------------------
    Spec("flash_timeout", "float", 300, "Firmware write timeout", "flash",
         "How long one m-flasher firmware write may run.", min=30, max=3600, unit="s"),
    Spec("restore_timeout", "float", 600, "Restore timeout", "flash",
         "How long a whole-flash restore may run.", min=30, max=3600, unit="s"),
    Spec("backup_timeout", "float", 300, "Backup timeout", "flash",
         "How long a whole-flash backup may run.", min=30, max=3600, unit="s"),
    Spec("restart_timeout", "float", 90, "Restart timeout", "flash",
         "", min=10, max=600, unit="s"),
    Spec("info_timeout", "float", 60, "Unit read timeout", "flash",
         "How long reading a unit's identity may take.", min=5, max=600, unit="s"),
    Spec("download_mode_wait", "float", 25, "Wait for download mode", "flash",
         "After the soft key, how long to wait for the unit to appear in download mode.",
         min=5, max=300, unit="s"),
    Spec("download_mode_settle", "float", 3, "Settle after it appears", "flash",
         "A pause once a unit shows up, because USB enumeration is not instant.",
         min=0, max=30, unit="s"),
    Spec("boot_wait", "float", 60, "Wait for a unit to boot", "flash",
         "After a write, how long to wait for the unit to come back running.",
         min=10, max=600, unit="s"),
    Spec("restart_boot_wait", "float", 30, "Wait after a restart", "flash",
         "After restarting a unit out of download mode, how long to wait for it to come "
         "back before re-identifying it.", min=5, max=300, unit="s"),
    Spec("register_wait", "float", 120, "Wait during registration", "flash",
         "How long registering a unit waits for you to power it.", min=10, max=900,
         unit="s"),
    # -- backups -------------------------------------------------------------------
    Spec("auto_backups_kept", "int", 20, "Automatic backups kept per unit", "backups",
         "Images saved automatically before a write. Older ones are deleted; backups "
         "you made by hand are never touched.", min=1, max=500),
    # -- router --------------------------------------------------------------------
    Spec("default_bpm", "float", 120, "Starting tempo", "router",
         "Master clock tempo before anyone sets one.", min=20, max=300, unit="BPM"),
    Spec("hold_ttl", "float", 300, "Flash hold lasts at most", "router",
         "While a unit is being flashed the router lets go of it; the hold always "
         "expires after this long, so a crashed flash cannot silence a unit forever.",
         min=30, max=3600, unit="s"),
    Spec("status_fresh", "float", 3, "Router counts as running for", "router",
         "The router's status file is trusted when newer than this.", min=1, max=60,
         unit="s"),
    # -- network: the Pi's own WiFi when no saved network connects ---------------------
    Spec("net_mode", "choice", "auto", "WiFi mode", "network",
         "Auto: join a saved network, and raise the Pi's own network when none connects. "
         "Home: never raise it. Field: always run it.", choices=("auto", "home", "field")),
    Spec("net_fallback_at_boot", "bool", True, "Fall back to the hotspot at boot", "network",
         "When no saved network connects after boot, raise the Pi's own network."),
    Spec("net_fallback_on_loss", "bool", True, "Fall back when the link is lost", "network",
         "When a connected network goes away, raise the Pi's own network."),
    Spec("net_fallback_after", "float", 60, "Give up on saved networks after", "network",
         "How long no saved network may be connected before the hotspot comes up.",
         min=10, max=600, unit="s"),
    Spec("net_retry_home_every", "float", 5, "Look for saved networks every", "network",
         "While the hotspot is up because of a fallback. 0 means never. A failed try waits "
         "twice as long next time, up to an hour.", min=0, max=120, unit="min"),
    Spec("net_retry_only_when_empty", "bool", True, "Go back only when nobody is joined",
         "network", "Never drop the hotspot while a phone is connected to it."),
    Spec("net_default_passphrase", "choice", "gate", "If the hotspot passphrase is still the default",
         "network",
         "gate: the hotspot works, but the first phone to open any page is made to choose a passphrase. "
         "refuse: the hotspot will not come up until it is changed at the Pi. allow: no restriction.",
         choices=("gate", "refuse", "allow")),
    Spec("net_ssid", "text", "FM-Pi", "Hotspot name", "network",
         "The network name phones see (up to 32 letters, digits, space . _ -).",
         pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$"),
    Spec("net_band", "choice", "2.4", "Hotspot band", "network",
         "2.4 GHz is what the fm1-radio units can join and reaches furthest; 5 GHz is "
         "quieter at a venue.", choices=("2.4", "5")),
    Spec("net_channel", "int", 6, "Hotspot channel", "network",
         "A fixed channel: 1-11 on 2.4 GHz, 36-165 on 5 GHz.", min=1, max=165),
    Spec("net_iface", "text", "wlan0", "WiFi adapter", "network",
         "The adapter that joins networks and runs the hotspot.",
         pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}$", restart=True),
    # -- advanced: internal state files ----------------------------------------------------
    Spec("router_active", "path", "~/fm1-router-active.json", "Active-patch file",
         "advanced", "", env="FM1_ROUTER_ACTIVE", restart=True),
    Spec("router_holds", "path", "~/fm1-router-holds.json", "Flash-holds file",
         "advanced", "", env="FM1_ROUTER_HOLDS", restart=True),
    Spec("router_status", "path", "~/fm1-router-status.json", "Router status file",
         "advanced", "", env="FM1_ROUTER_STATUS", restart=True),
    Spec("net_key", "path", "~/fm1-hotspot.key", "Hotspot passphrase file",
         "advanced", "Kept private (mode 0600); never shown on a page.",
         env="FM1_HOTSPOT_KEY", restart=True, editable=False,
         why_locked="It holds the hotspot passphrase; set it in the config file or "
                    "FM1_HOTSPOT_KEY."),
    Spec("time_set_marker", "path", "~/fm1-time-set.json", "Clock-set marker file",
         "advanced", "Remembers, for this boot only, that a person set the clock.",
         env="FM1_TIME_SET", restart=True),
    Spec("net_status", "path", "~/fm1-net-status.json", "WiFi status file",
         "advanced", "", env="FM1_NET_STATUS", restart=True),
)

BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}


# ------------------------------------------------------------------------------- file

def config_path() -> Path:
    raw = os.environ.get("FM1_CONFIG")
    return Path(raw).expanduser() if raw else Path("~/.config/fm1/config.json").expanduser()


def load() -> dict:
    """The raw file as a dict. Read on every call: it is tiny, and a change made on the
    website must reach the router process (a different one) without a restart."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce(spec: Spec, raw: Any) -> Any:
    """``raw`` as the spec's type, or raise ConfigError saying what is wrong."""
    if spec.kind in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"{spec.label}: needs a number")
        num = int(raw) if spec.kind == "int" and float(raw).is_integer() else raw
        if spec.kind == "int" and not isinstance(num, int):
            raise ConfigError(f"{spec.label}: needs a whole number")
        if spec.min is not None and num < spec.min or spec.max is not None and num > spec.max:
            raise ConfigError(f"{spec.label}: must be {spec.min:g} to {spec.max:g}"
                              f"{' ' + spec.unit if spec.unit else ''}")
        return float(num) if spec.kind == "float" else num
    if spec.kind == "bool":
        if not isinstance(raw, bool):
            raise ConfigError(f"{spec.label}: needs yes or no")
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{spec.label}: cannot be empty")
    text = raw.strip()
    if "\0" in text or "\n" in text or len(text) > 400:
        raise ConfigError(f"{spec.label}: not a usable value")
    if spec.kind == "choice" and text not in spec.choices:
        raise ConfigError(f"{spec.label}: choose one of {', '.join(spec.choices)}")
    if spec.kind == "url" and not re.match(r"^https?://[^\s/]+", text):
        raise ConfigError(f"{spec.label}: needs an http(s) address")
    if spec.pattern and not re.match(spec.pattern, text):
        raise ConfigError(f"{spec.label}: letters, digits, space, . _ - only, up to 40")
    return text


def _clamped(spec: Spec, raw: Any) -> Any:
    """Like _coerce, but forgiving: a bad value in the file becomes the default, and an
    out-of-range number is pulled to its bound (what the unit count always did)."""
    try:
        return _coerce(spec, raw)
    except ConfigError:
        pass
    if spec.kind in ("int", "float") and isinstance(raw, (int, float)) \
            and not isinstance(raw, bool):
        num = max(spec.min, min(spec.max, raw))
        return int(num) if spec.kind == "int" else float(num)
    return spec.default


def source(key: str) -> str:
    """Where the effective value comes from: ``env``, ``file`` or ``default``."""
    spec = BY_KEY[key]
    if spec.env and os.environ.get(spec.env):
        return "env"
    return "file" if key in load() else "default"


def value(key: str) -> Any:
    """The effective value of a setting: env, else the file, else the default."""
    spec = BY_KEY[key]
    if spec.env:
        raw = os.environ.get(spec.env)
        if raw:
            return raw
    data = load()
    if key in data:
        return _clamped(spec, data[key])
    return spec.default


def path(key: str) -> Path:
    return Path(str(value(key))).expanduser()


def unit_count() -> int:
    return value("units")


# Older callers: env var, file key, default as text. Kept so nothing that worked changes.
def setting(env: str, key: str, default: str) -> str:
    """env var, else the file's ``key``, else ``default`` (a path or name, as text)."""
    raw = os.environ.get(env)
    if raw:
        return raw
    val = load().get(key)
    return val if isinstance(val, str) and val else default


def path_setting(env: str, key: str, default: str) -> Path:
    return Path(setting(env, key, default)).expanduser()


# ------------------------------------------------------------------------- the website

def describe() -> dict:
    """Everything the Settings page needs: the groups, and each setting with its value,
    default, where the value comes from, and whether this page may change it."""
    data = load()
    items = []
    for s in SPECS:
        src = source(s.key)
        items.append({
            "key": s.key, "group": s.group, "kind": s.kind, "label": s.label,
            "help": s.help, "unit": s.unit, "min": s.min, "max": s.max,
            "choices": list(s.choices),
            "value": value(s.key), "default": s.default,
            "in_file": s.key in data, "source": src,
            "env": s.env if src == "env" else None,
            "restart": s.restart, "editable": s.editable, "why_locked": s.why_locked,
        })
    return {"path": str(config_path()),
            "groups": [{"id": g, "label": label} for g, label in GROUPS],
            "settings": items}


def save(changes: dict) -> dict:
    """Apply ``{key: value}`` (``None`` resets a key to its default) to the file.

    All or nothing: if any entry is refused nothing is written. Unknown keys, settings
    this page may not edit, and settings an environment variable fixes are refused. Keys
    the schema does not know that are already in the file are left alone. Returns
    ``{"changed": {key: [old, new]}, "restart": [keys that need a restart]}``.
    """
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("nothing to change")
    data = load()
    new = dict(data)
    changed: dict[str, list] = {}
    for key, raw in changes.items():
        spec = BY_KEY.get(key)
        if spec is None:
            raise ConfigError(f"unknown setting {key!r}")
        if not spec.editable:
            raise ConfigError(f"{spec.label}: {spec.why_locked or 'not editable here'}")
        if spec.env and os.environ.get(spec.env):
            raise ConfigError(f"{spec.label}: fixed by the environment variable "
                              f"{spec.env}; unset it to change this")
        old = value(key)
        if raw is None:
            new.pop(key, None)
        else:
            new[key] = _coerce(spec, raw)
        now = new.get(key, spec.default)
        if now != old:
            changed[key] = [old, now]
    if new != data:
        _write(new)
    return {"changed": changed,
            "restart": [k for k in changed if BY_KEY[k].restart]}


def _write(data: dict) -> None:
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
