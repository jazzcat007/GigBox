"""The firmware library: which .fwsc packages are available to install.

Two sources, both plain directories — no network code:

* the **library** directory (``~/fm1-firmwares`` by default, or ``FM1_LIBRARY``),
  i.e. what lives on the Pi;
* an optional **staging** directory (a mounted SD card or USB stick), so packages
  arrive on the Pi by copying them off removable media.

Fetching over the network is deliberately NOT here. A firmware package is a binary
that gets written to a unit's flash; downloading one unattended, on a touchscreen,
is a supply-chain surface with no upside. Copy it onto the card, then import it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

#: Extensions the vendor and the community ship packages under.
PACKAGE_SUFFIXES: tuple[str, ...] = (".fwsc",)

from .config import path_setting

DEFAULT_LIBRARY = "~/fm1-firmwares"


def library_dir() -> Path:
    """The Pi's own firmware library directory."""
    return path_setting("FM1_LIBRARY", "library", DEFAULT_LIBRARY)


@dataclass(frozen=True)
class Package:
    path: Path
    size: int
    digest: str  # sha256 of the whole file, first 12 hex chars for display

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.size / 1024:.0f} KiB)"


_DIGESTS: dict[tuple, str] = {}


def _digest(path: Path) -> str:
    """SHA-256 of a file, remembered by (path, mtime, size): the panel lists the library
    every few seconds and hashing every package each time costs seconds on a Pi 3B+."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and key in _DIGESTS:
        return _DIGESTS[key]
    digest = _hash_file(path)
    if key is not None:
        if len(_DIGESTS) > 256:
            _DIGESTS.clear()
        _DIGESTS[key] = digest
    return digest


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(directory: Path) -> list[Package]:
    """Every package in one directory, sorted by name. Never raises on a bad file."""
    if not directory.is_dir():
        return []
    out: list[Package] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in PACKAGE_SUFFIXES:
            continue
        try:
            out.append(Package(path=entry, size=entry.stat().st_size, digest=_digest(entry)))
        except OSError:
            continue  # unreadable file: leave it out rather than break the menu
    return out


def library() -> list[Package]:
    return scan(library_dir())


def staging_dirs() -> list[Path]:
    """Removable media to import from: an explicit override, else the mount points."""
    override = os.environ.get("FM1_STAGING")
    if override:
        return [Path(override).expanduser()]
    found: list[Path] = []
    for base in ("/media", "/run/media", "/mnt"):
        root = Path(base)
        if not root.is_dir():
            continue
        # /media/<user>/<label> and /media/<label> and /run/media/<user>/<label>
        for depth in (1, 2):
            try:
                for d in root.glob("/".join(["*"] * depth)):
                    if d.is_dir():
                        found.append(d)
            except OSError:
                continue
    return found


def staged_packages() -> list[Package]:
    """Packages found on removable media, NOT yet imported."""
    out: list[Package] = []
    for d in staging_dirs():
        out.extend(scan(d))
    return out


def import_package(pkg: Package, *, overwrite: bool = False) -> Path:
    """Copy a staged package into the library. Refuses to clobber unless told to."""
    dest_dir = library_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pkg.path.name
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} already exists in the library")
    shutil.copy2(pkg.path, dest)
    return dest


def remove_package(pkg: Package) -> None:
    """Delete a package from the library."""
    if pkg.path.parent.resolve() != library_dir().resolve():
        raise ValueError(f"{pkg.path} is not in the library")
    pkg.path.unlink(missing_ok=True)
