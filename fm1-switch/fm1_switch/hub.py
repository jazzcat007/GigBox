"""The Firmware Hub catalogue: the community's curated list of FM-1 firmwares.

Source: https://thefirmwarehub.com/catalog.json — 277 firmwares as of 2026-11-20,
each with identity, version, SHA-256, size and a chip-compatibility flag.

**This module is a client, not a mirror.** The hub publishes a machine-readable
catalogue explicitly for apps like this one, under terms we are obliged to honour
(https://thefirmwarehub.com/kb/#catalog):

1. Name The Firmware Hub with its address, visibly. See ATTRIBUTION below — it is
   rendered in the UI, not just buried here.
2. The makers' texts and pictures are theirs; name each firmware's author and
   link its repository. We show author + repo on every card.
3. **Read ``catalog.json`` at most once a day**, with a User-Agent naming us. Do not
   scrape the pages. Download packages from the makers' releases.
4. Keep the player safe: install only when the SHA-256 matches the catalogue, drop
   entries that have left it.

On the last point this module deliberately goes *further* than the hub asks: it never
downloads a package automatically. It resolves a URL and a checksum; the write happens
only after the user has asked for it and the hash has been re-checked. See fetch_package.

**Chip flag — read this before using ``chip`` for anything.**

The catalogue's ``chip`` field is ``new`` / ``old`` / ``any``. It is NOT what it looks
like. ``new`` does not mean "requires the newer flash chip"; it means **"knows the newer
chip"**, i.e. it is one of the firmwares that saves correctly on a UC25HQ80 unit. The
evidence, from the hub's own rescue page ("Firmwares that save on the newer chip"):

* Felucca 1.8.2, SLOOP 2.8.5 and OberVave 3.5.2 are all flagged ``new``,
* and all three run on this fleet's old-chip (0x856014) units.

So ``old`` means "does not know the new chip", not "will not run on an old one".
Consequently:

* on an **old-chip** unit, every firmware is fine — the flag is irrelevant;
* on a **new-chip** unit, an ``old`` firmware will not write the flash, so settings and
  saves are lost at power-off, and updates may be refused.

This module exposes that as :attr:`Entry.chip_note` rather than a boolean, so callers
cannot accidentally treat it as a hard gate. STALE_CAVEAT records the one thing we have
not verified: whether a new-chip firmware's fix survives being overwritten by an old-chip
firmware (m-flasher merges rather than replaces, so we believe it does, but our fleet has
only one new-chip unit and it is working — we are not going to break it to find out).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config

def hub_base() -> str:
    """The hub's address (config ``hub_url``; read-only on the website)."""
    return str(config.value("hub_url")).rstrip("/")


def catalog_url() -> str:
    return f"{hub_base()}/catalog.json"

#: Required by the hub's terms: they ask tools to identify themselves.
USER_AGENT = "fm1-fleet/0.1 (Raspberry Pi FM-1 firmware manager; personal use)"

def max_age_seconds() -> float:
    """The hub asks that the catalogue be read at most once a day (``hub_max_age_hours``)."""
    return float(config.value("hub_max_age_hours")) * 3600


def force_min_age_seconds() -> float:
    """A forced refresh is honoured only when the cache is at least this old (HUB-02), so a
    client looping the refresh button cannot hammer the hub (``hub_refresh_min_minutes``)."""
    return float(config.value("hub_refresh_min_minutes")) * 60

ATTRIBUTION = (
    "Firmware list: The Firmware Hub – https://thefirmwarehub.com. "
    "Compilation licensed CC BY 4.0; card texts and pictures belong to their makers."
)

STALE_CAVEAT = (
    "Not verified: whether a newer-chip firmware's flash support survives being "
    "overwritten by an older-chip one. m-flasher merges a package into flash rather "
    "than replacing it, so it probably does — but test on one unit before relying on it."
)

KINDS = ("synth", "groove", "drums", "chords", "tracker", "other", "dev")
KIND_LABELS = {
    "synth": "Synth",
    "groove": "Groovebox",
    "drums": "Drums / sampler",
    "chords": "Chords",
    "tracker": "Tracker",
    "other": "Other",
    "dev": "Dev tool",
}


def cache_dir() -> Path:
    """Where the catalogue is cached between runs."""
    return config.path("hub_cache")


def catalog_path() -> Path:
    return cache_dir() / "catalog.json"


class HubError(Exception):
    """The catalogue could not be read at all."""


class HubUnavailable(HubError):
    """No network, and no usable cached catalogue."""


#: Said wherever something needs the internet and the Pi has none (for example while it runs its own
#: hotspot). It names what still works and the way around it.
OFFLINE_NOTE = ("The Pi is offline (it has no internet connection), so it cannot reach the Firmware Hub. "
                "Everything already on the Pi still works: firmware in the library can be flashed, backed "
                "up and restored. To get a new firmware, connect the Pi to a network with internet and try "
                "again, or copy a .fwsc file onto it from a USB stick with `fm1-switch import`.")


@dataclass(frozen=True)
class Entry:
    """One firmware in the catalogue. Field names mirror the JSON, mostly."""

    id: str
    name: str
    version: str
    author: str
    repo: str
    ident: str
    what: str
    sha256: str
    size: int
    #: "new" | "old" | "any" — means "knows the new chip", see the module docstring.
    chip: str
    kind: tuple[str, ...] = ()
    beta: bool = False
    pkg: str | None = None
    date: str | None = None
    stars: int = 0
    #: Set when the hub considers the package installable by a USB installer;
    #: when true, ``pkg`` may be absent and there is nothing for us to flash.
    external_install: bool = False
    link_only: bool = False
    editor: str | None = None
    site: str | None = None
    release: str | None = None
    hid_keyboard: bool = False

    @property
    def package_url(self) -> str | None:
        """Absolute URL of the package, or None if the hub does not mirror one."""
        if not self.pkg:
            return None
        return f"{hub_base()}/{self.pkg.lstrip('/')}"

    @property
    def installable_by_us(self) -> bool:
        """Can this GUI fetch and flash it, or does it need its maker's installer?"""
        return bool(self.pkg) and not self.external_install and not self.link_only

    @property
    def kind_label(self) -> str:
        return " + ".join(KIND_LABELS.get(k, k.title()) for k in self.kind)

    def ident_matches(self, reported: str | None) -> bool:
        """Does a unit's reported identity fall inside this entry's pattern?

        The catalogue's ``ident`` is a REGEX (e.g. ``FM-1_95[3-9]\\d``), because some
        firmwares vary their last digits per version. Several firmwares share an
        identity, so a match is evidence, never proof of which one is installed.
        """
        if not reported:
            return False
        try:
            return re.fullmatch(self.ident, reported) is not None
        except re.error:
            return reported == self.ident

    def chip_note(self, unit_chip: str | None) -> tuple[str, str]:
        """(severity, text) describing chip compatibility for one unit.

        severity is "ok" | "warn" | "unknown". Never a boolean: an "ok" here does not
        mean installing is advisable, only that the chip is not the reason to hesitate.
        """
        chip = (unit_chip or "").upper()
        if not unit_chip:
            return ("unknown", "Chip not known — connect the unit to check.")
        if chip.endswith("B36014"):
            if self.chip == "new":
                return ("ok", "Knows this unit's newer flash chip.")
            if self.chip == "any":
                return ("ok", "Works with either flash chip.")
            return (
                "warn",
                "Does not know this unit's newer flash chip: settings and saves will "
                "not be kept across power-off, and updates may be refused.",
            )
        if chip.endswith("856014"):
            return ("ok", "Older flash chip — every firmware can write it.")
        return ("unknown", f"Unrecognised flash chip {unit_chip}.")


@dataclass
class Catalog:
    entries: list[Entry] = field(default_factory=list)
    updated: str = ""
    fetched_at: float = 0.0
    #: True when this came from the on-disk cache rather than the network.
    from_cache: bool = False
    #: True when the cache was served because the Pi is offline, not merely because it is fresh.
    offline: bool = False

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at) if self.fetched_at else 0.0

    @property
    def stale(self) -> bool:
        return self.age_seconds > max_age_seconds()

    def by_id(self, entry_id: str) -> Entry | None:
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None

    def matching_ident(self, reported: str | None) -> list[Entry]:
        """Entries whose identity pattern could be what this unit is running."""
        return [e for e in self.entries if e.ident_matches(reported)]

    def search(self, query: str = "", kind: str | None = None) -> list[Entry]:
        """Case-insensitive substring search over name, author and description."""
        q = (query or "").strip().lower()
        out = self.entries
        if kind:
            out = [e for e in out if kind in e.kind]
        if q:
            out = [
                e
                for e in out
                if q in e.name.lower()
                or q in e.author.lower()
                or q in e.what.lower()
                or q in e.id.lower()
            ]
        return out


def _kind_tuple(raw) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(str(k) for k in raw if k)
    if raw:
        return (str(raw),)
    return ()


def parse_catalog(payload: dict, *, fetched_at: float | None = None) -> Catalog:
    """Build a :class:`Catalog` from the hub's JSON, skipping unusable rows.

    Rows without a package, or without a checksum, are kept (they are still useful to
    browse and link to) but will report ``installable_by_us`` as False.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("firmwares"), list):
        raise HubError("catalogue is not the shape we expect (no 'firmwares' list)")

    entries: list[Entry] = []
    for row in payload["firmwares"]:
        if not isinstance(row, dict):
            continue
        try:
            eid = str(row["id"])
            name = str(row.get("name") or eid)
        except (KeyError, TypeError):
            continue
        hid = row.get("hid") or {}
        entries.append(
            Entry(
                id=eid,
                name=name,
                version=str(row.get("version") or ""),
                author=str(row.get("author") or ""),
                repo=str(row.get("repo") or ""),
                ident=str(row.get("ident") or ""),
                what=str(row.get("what") or ""),
                sha256=str(row.get("sha256") or ""),
                size=int(row.get("size") or 0),
                chip=str(row.get("chip") or "any"),
                kind=_kind_tuple(row.get("kind")),
                beta=bool(row.get("beta")),
                pkg=row.get("pkg") or None,
                date=row.get("date"),
                stars=int(row.get("stars") or 0),
                external_install=bool(row.get("externalInstall")),
                link_only=bool(row.get("linkOnly")),
                editor=row.get("editor") or None,
                site=row.get("site") or None,
                release=row.get("release") or None,
                hid_keyboard=bool(isinstance(hid, dict) and hid.get("keyboard")),
            )
        )
    return Catalog(
        entries=entries,
        updated=str(payload.get("updated") or ""),
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


# ---------------------------------------------------------------------------- connectivity
#
# Away from home the Pi may run its own hotspot, with no internet behind it. Anything that needs the
# hub must find that out in a moment, not after the 60 s catalogue timeout or the 180 s download
# timeout, and must say so in plain words. online() is a cheap, cached check: no default route means
# offline at once; otherwise the hub host is resolved and connected to with a short deadline.

_ONLINE: dict = {"at": -1e9, "ok": True}


def has_default_route() -> bool | None:
    """True/False from the kernel's routing table, or None where it cannot be read (not Linux)."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        cols = row.split()
        if len(cols) >= 4 and cols[1] == "00000000" and int(cols[3], 16) & 1:
            return True
    try:                                   # an IPv6-only default route also counts
        with open("/proc/net/ipv6_route", encoding="ascii") as f:
            for row in f:
                cols = row.split()
                # dest, prefix length, ..., flags (col 8), device (col 9): an up, non-reject default route
                if (len(cols) >= 10 and cols[0] == "0" * 32 and cols[1] == "00000000"
                        and int(cols[8], 16) & 0x1 and not int(cols[8], 16) & 0x200
                        and cols[9] != "lo"):
                    return True
    except OSError:
        pass
    return False


def _probe(host: str, port: int, timeout: float) -> bool:
    """Resolve and connect within ``timeout`` seconds. getaddrinfo ignores timeouts, so this runs in a
    thread that is simply abandoned when the deadline passes."""
    result: list[bool] = []

    def attempt() -> None:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                result.append(True)
        except OSError:
            result.append(False)

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(timeout + 0.5)
    return bool(result and result[0])


def online(*, force: bool = False) -> bool:
    """Can this Pi reach the Firmware Hub right now? (A thin wrapper so tests can stub it.)"""
    return check_online(force=force)


def check_online(*, force: bool = False, clock=time.monotonic) -> bool:
    """The real check, cached for 30 s when yes and 10 s when no. The environment variable
    FM1_OFFLINE=1 forces "no" (for testing, or a rig that must never call out)."""
    if os.environ.get("FM1_OFFLINE"):
        return False
    now = clock()
    ttl = 30.0 if _ONLINE["ok"] else 10.0
    if not force and now - _ONLINE["at"] < ttl:
        return bool(_ONLINE["ok"])
    ok = False
    route = has_default_route()
    if route is not False:
        url = urlparse(catalog_url())
        host = url.hostname or ""
        port = url.port or (443 if url.scheme == "https" else 80)
        ok = bool(host) and _probe(host, port, 3.0)
    _ONLINE["at"], _ONLINE["ok"] = now, ok
    return ok


def load(*, network: bool = True, force: bool = False,
         timeout: float | None = None) -> Catalog:
    """Return the catalogue, from cache when fresh, else from the hub.

    ``network=False`` never touches the network (offline mode, and tests).
    Raises :class:`HubUnavailable` only when there is neither network nor cache.
    """
    cache = catalog_path()
    cached: Catalog | None = None

    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            cached = parse_catalog(payload, fetched_at=cache.stat().st_mtime)
            cached.from_cache = True
        except (OSError, ValueError, HubError):
            cached = None

    if force and cached is not None and cached.entries and cached.age_seconds < force_min_age_seconds():
        force = False
    if cached is not None and cached.entries and not force and not cached.stale:
        return cached
    if not network:
        if cached is not None and cached.entries:
            return cached
        raise HubUnavailable(
            "No cached catalogue and network access is disabled. "
            "Connect to Wi-Fi and refresh, or run `fm1-hub --refresh`."
        )

    if not online():
        # No internet: do not wait for a timeout. Serve what is saved, or say plainly that nothing is.
        if cached is not None and cached.entries:
            cached.offline = True
            return cached
        raise HubUnavailable("No saved catalogue yet, and the Pi is offline. " + OFFLINE_NOTE)

    if timeout is None:
        timeout = float(config.value("hub_timeout"))
    try:
        req = urllib.request.Request(
            catalog_url(), headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed URL
            raw = resp.read()
        payload = json.loads(raw)
        fresh = parse_catalog(payload)
    except (urllib.error.URLError, OSError, ValueError, HubError) as exc:
        if cached is not None and cached.entries:
            # Serving yesterday's list is better than an empty screen, as long as the
            # UI says so — which it does, from Catalog.stale and from_cache.
            return cached
        raise HubUnavailable(f"Could not read the catalogue: {exc}") from exc

    # Write atomically: a power cut mid-write must not leave a truncated catalogue
    # that refuses to load next boot.
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        pass  # read-only filesystem is not a reason to fail the refresh

    return fresh


# ------------------------------------------------------------------- fetching packages
#
# A package is a firmware image. Downloading one is the one place this program trusts
# the network, so the rules are stated once, here:
#
#   * never fetch without an explicit call (the UI asks first, and says so);
#   * verify SHA-256 against the catalogue *before* the file enters the library —
#     a mismatch deletes the download and raises, it does not "keep it anyway";
#   * verify size too, because a truncated body that happens to hash-verify is
#     impossible but a truncated body that never finished is common;
#   * write via a temp file and rename, so a killed download cannot leave a
#     half-written .fwsc where the library scan will pick it up.

def max_package_bytes() -> int:
    """Largest download accepted (``max_package_mb``; the largest catalogue entry is ~1 MiB)."""
    return int(float(config.value("max_package_mb")) * 1024 * 1024)


class HubOffline(HubError):
    """A download was asked for while the Pi has no internet. The message says what still works."""


class PackageDownloadError(HubError):
    """The package could not be fetched or did not match the catalogue."""


class ChecksumMismatch(PackageDownloadError):
    """The bytes on the wire are not the bytes the catalogue promised."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"checksum mismatch: catalogue says {expected[:16]}…, "
            f"downloaded file is {actual[:16]}…. The file was deleted and NOT installed."
        )
        self.expected = expected
        self.actual = actual


def fetch_package(
    entry: Entry,
    dest_dir: Path,
    *,
    timeout: float | None = None,
    allow_unpinned: bool = False,
) -> Path:
    """Download one catalogue entry into ``dest_dir`` and verify it.

    Returns the written path. Raises :class:`PackageDownloadError` on any problem,
    and in that case leaves nothing behind.
    """
    if not entry.installable_by_us:
        raise PackageDownloadError(
            f"{entry.name} is not distributed as a downloadable package "
            "(its maker's installer is required)."
        )
    url = entry.package_url
    if not url:
        raise PackageDownloadError(f"{entry.name} has no package URL in the catalogue.")
    if not entry.sha256 and not allow_unpinned:
        raise PackageDownloadError(
            f"{entry.name} has no checksum in the catalogue; refusing to install an "
            "unverifiable file (pass allow_unpinned to override)."
        )

    if not online():
        raise HubOffline(OFFLINE_NOTE)

    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = entry.pkg.rsplit("/", 1)[-1] if entry.pkg else f"{entry.id}.fwsc"
    if not filename.lower().endswith(".fwsc"):
        filename += ".fwsc"
    dest = dest_dir / filename
    tmp = dest.with_name(dest.name + ".part")

    if timeout is None:
        timeout = float(config.value("download_timeout"))
    limit = max_package_bytes()
    digest = hashlib.sha256()
    written = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise PackageDownloadError(
                    f"refusing {int(declared)} bytes — larger than any FM-1 package"
                )
            with tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise PackageDownloadError("download exceeded the size limit")
                    digest.update(chunk)
                    fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise PackageDownloadError(f"download failed: {exc}") from exc
    except PackageDownloadError:
        tmp.unlink(missing_ok=True)
        raise

    actual = digest.hexdigest()
    problems: list[str] = []
    if entry.sha256 and actual != entry.sha256.lower():
        problems.append("sha256")
    if entry.size and written != entry.size:
        problems.append(f"size {written} != {entry.size}")
    if problems:
        tmp.unlink(missing_ok=True)
        raise ChecksumMismatch(entry.sha256, actual)

    tmp.replace(dest)
    return dest
