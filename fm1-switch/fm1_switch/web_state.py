"""The fleet UI's shared state: the unit views, the catalogue, the cached fleet answer, and
the package helpers (hashing, compatibility-ready data, the update check).

No HTTP lives here. ``web_http`` serves these; ``web_writes`` changes units through them.
The threading rules are in the ``web`` module docstring: ``APP.lock`` is held for a whole
write, ``APP.state_lock`` only for the short cached fields.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import hub, library, mflasher, packages, registry
from .config import path_setting
from .units import SLOTS, SLOT_LABELS, enumerate_units


STATIC_DIR = Path(__file__).parent / "web"


def port_map_path() -> Path:
    return path_setting("FM1_PORTMAP", "portmap", "~/fm1-portmap.txt")


# --------------------------------------------------------------------------- state

@dataclass
class UnitView:
    slot: str
    label: str
    present: bool
    #: Where this slot sits physically, from the port map ("1-1.1.3"), if known.
    port: str | None = None
    chip: str | None = None
    unit_id: str | None = None
    firmware: str | None = None
    identity: str | None = None
    error: str | None = None
    #: Catalogue entries whose identity pattern could be what this unit runs.
    maybe: list[str] = field(default_factory=list)


def read_port_map() -> dict[str, str]:
    """slot -> USB port path, from ``label-units.sh``'s file. Best effort."""
    out: dict[str, str] = {}
    try:
        for line in port_map_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0] in SLOTS:
                out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


class App:
    """Holds the catalogue, the lock, and the last known state of each unit."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        #: Guards busy / last_event / last_fleet (audit CONC-01). Short critical sections
        #: only, never held across a device call, so it can block without queueing writes.
        self.state_lock = threading.RLock()
        self.catalog: hub.Catalog | None = None
        self.catalog_error: str | None = None
        self.catalog_tried = False
        self.units: dict[str, UnitView] = {}
        self.busy: str | None = None
        self.last_event: dict | None = None
        #: Set when the audit log could not be written (audit SAFE-05); cleared by the next
        #: successful line. Surfaced in /api/fleet so a lost record is not silent.
        self.log_error: str | None = None
        self.last_fleet: dict | None = None
        # Off-Pi clients must present this on mutations (SEC-01). None on a loopback
        # bind: the panel is this machine's own UI and needs no second factor. serve()
        # fills it (and the tests set it directly).
        self.token: str | None = None
        self._inspect_cache: dict[str, tuple[float, packages.Inspection]] = {}

    # -- catalogue ---------------------------------------------------------
    def load_catalog(self, *, force: bool = False, network: bool = True) -> hub.Catalog | None:
        try:
            self.catalog = hub.load(network=network, force=force)
            self.catalog_error = None
        except hub.HubUnavailable as exc:
            self.catalog_error = str(exc)
        return self.catalog

    # -- units -------------------------------------------------------------
    def refresh_units(self, *, read_identity: bool = True) -> list[UnitView]:
        """Enumerate slots and, when a unit is in download mode, read its identity.

        Reading identity means `m-flasher info` reads the whole 1 MiB flash, so this is
        deliberately not called on every page load — see the /api/units handler.
        """
        if read_identity and not self.lock.acquire(blocking=False):
            # A write holds the flash lock: reading identity would run m-flasher info on
            # the very device being written (audit SAFE-03). Show what we last knew.
            return list(self.units.values())
        try:
            return self._refresh_units_locked(read_identity)
        finally:
            if read_identity:
                self.lock.release()

    def _refresh_units_locked(self, read_identity: bool) -> list[UnitView]:
        # Where each running unit is now, from sysfs (no m-flasher). Not the saved map: it
        # goes stale when cables move. A unit already in download mode shows no port here.
        ports = registry.running_slots()
        views: dict[str, UnitView] = {}
        present = {u.slot: u for u in enumerate_units()}
        for slot in SLOTS:
            u = present.get(slot)
            v = UnitView(
                slot=slot,
                label=SLOT_LABELS.get(slot, slot),
                present=u is not None,
                port=ports.get(slot),
            )
            if u is not None and read_identity:
                try:
                    ident = mflasher.info(u.device)
                    v.chip = ident.flash_chip
                    v.unit_id = ident.unit_id
                    v.firmware = ident.firmware
                    v.identity = _identity_from_firmware(ident.firmware, ident.app)
                except mflasher.MflasherError as exc:
                    v.error = str(exc)
            views[slot] = v
        self.units = views
        return list(views.values())

    def unit_views_with_maybes(self) -> list[UnitView]:
        views = list(self.units.values()) or self.refresh_units(read_identity=False)
        for v in views:
            if self.catalog and v.identity:
                v.maybe = [e.id for e in self.catalog.matching_ident(v.identity)]
        return views

    # -- packages ----------------------------------------------------------
    def inspect_package(self, path: Path) -> packages.Inspection:
        # packages.inspect keeps a durable, mtime-keyed cache of parsed results: a full
        # parse is ~0.3 s here and the library page shows every package, so doing it
        # per request made the page take minutes on a Pi 3B+.
        return packages.inspect(path)


_IDENT_IN_LABEL = re.compile(r"FM-1_[0-9A-Za-z]+")


def _identity_from_firmware(firmware: str | None, app: str | None) -> str | None:
    """Pull ``FM-1_NNN`` out of whatever m-flasher reports as the firmware label."""
    for text in (firmware, app):
        if text:
            m = _IDENT_IN_LABEL.search(text)
            if m:
                return m.group(0)
    return None


APP = App()


# --------------------------------------------------------------------------- writes

def _ui_build() -> int:
    """Changes whenever a panel file is deployed; the page reloads itself when it does."""
    try:
        return max(int(p.stat().st_mtime) for p in STATIC_DIR.glob("fleet.*"))
    except (OSError, ValueError):
        return 0


_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def _ver(text: str | None) -> tuple[int, ...] | None:
    m = _VERSION.match((text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _catalog_cached() -> hub.Catalog | None:
    """The catalogue if it is loaded; otherwise one try at the on-disk cache (never the
    network: this runs on every fleet poll)."""
    if APP.catalog is None and not APP.catalog_tried:
        APP.catalog_tried = True
        APP.load_catalog(network=False)
    return APP.catalog


def update_for(firmware: str | None, cat: hub.Catalog | None) -> dict | None:
    """Is a newer version of the firmware a unit runs in the catalogue?

    A unit's firmware label is the package stem it was flashed from ("grimm-2.0.2"), so it
    reads as <catalogue id>-<version>. Anything else (stock firmware, an unknown label, a
    version that does not parse) answers None: no claim is made."""
    if not firmware or cat is None or "-" not in firmware:
        return None
    fid, _, current = firmware.rpartition("-")
    entry = cat.by_id(fid)
    cur, new = _ver(current), _ver(entry.version if entry else None)
    if entry is None or cur is None or new is None or new <= cur or not entry.installable_by_us:
        return None
    return {"id": entry.id, "name": entry.name, "current": current, "latest": entry.version,
            "package": entry.pkg.rsplit("/", 1)[-1] if entry.pkg else None,
            "have": bool(entry.pkg) and (library.library_dir() / entry.pkg.rsplit("/", 1)[-1]).is_file()}


def getlist(limit: int = 24) -> list[dict]:
    """Catalogue firmwares the panel can fetch: installable here, not already on the Pi,
    best-rated first (stable releases before betas)."""
    cat = _catalog_cached()
    if cat is None:
        return []
    have = {p.name for p in library.library()}
    rows = [e for e in cat.entries if e.installable_by_us
            and e.pkg.rsplit("/", 1)[-1] not in have]
    rows.sort(key=lambda e: (e.beta, -e.stars, e.name.lower()))
    return [{"id": e.id, "name": e.name, "version": e.version, "stars": e.stars,
             "chip": e.chip, "beta": e.beta, "size": e.size,
             "author": e.author, "repo": e.repo} for e in rows[:limit]]


_SHA_CACHE: dict[tuple, str] = {}
_PKG_CACHE: dict[tuple, dict] = {}


def _stat_key(path: Path) -> tuple:
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def _sha256(path: Path) -> str:
    """SHA-256 of a file, remembered by (path, mtime, size): the panel asks for every
    package on each unit screen and hashing them all each time is slow on a Pi."""
    try:
        key = _stat_key(path)
    except OSError:
        key = None
    if key is not None and key in _SHA_CACHE:
        return _SHA_CACHE[key]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if key is not None:
        _SHA_CACHE[key] = digest
    return digest


# --------------------------------------------------------------------------- HTTP

def _catalog_meta(cat: hub.Catalog | None, error: str | None) -> dict:
    if cat is None:
        return {"available": False, "error": error, "attribution": hub.ATTRIBUTION}
    return {
        "available": True,
        "count": len(cat.entries),
        "updated": cat.updated,
        "age_hours": round(cat.age_seconds / 3600, 1),
        "stale": cat.stale,
        "from_cache": cat.from_cache,
        "error": error,
        "attribution": hub.ATTRIBUTION,
        "stale_caveat": hub.STALE_CAVEAT,
    }
