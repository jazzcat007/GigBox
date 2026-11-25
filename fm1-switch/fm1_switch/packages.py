"""What a local .fwsc actually contains, read without touching a unit.

This answers the question the screen cannot: **will this package install completely
from m-flasher, or does it need its maker's own installer?**

It parses the container rather than guessing from file size. The format is M-VAVE's
JieLi UFW package, documented in m-flasher's source (``src/formats.rs``):

    The file is cut into 48-byte blocks. Byte 47 of block i holds identity character i
    as (char + i + 1) & 0xFF, 0x7D once the identity has ended. Remove those bytes and
    what remains is a 64-byte header (CRC16 of bytes 2..63, CRC16 of the file list,
    total size, file count) followed by the file list, 80 bytes per file, both ENC
    0xFFFF. ``flash.bin`` (type 0) is the flash image: boot area as stored, then the
    app area, already encrypted with the chip key the package was made for.

**The finding that matters, from this fleet's own hardware.** m-flasher writes the
package's flash image, from 0x04000 to the end of the image, and nothing else. But a
package can carry *further files* beyond the flash image, and only the firmware's own
installer writes those. Measured on our packages:

    sloop-2.8.5   flash.bin, ota.bin             2 files   ← installs clean
    felucca-1.8.2 flash.bin, ota.bin             2 files   ← installs clean
    obervave-3.5.2 flash.bin, ota.bin            2 files   ← installs clean
    melodee-1.6.1 flash.bin, ota.bin, res.bin    3 files   ← "Setup unfinished"
    choralroot-0.42 flash.bin, ota.bin, res2.bin 3 files   ← "Presets Missing"

``res.bin`` is 57,344 bytes and ``res2.bin`` 16,384 — the factory sounds and presets
that went missing on the two firmwares that complained. The three that installed cleanly
carry no such file. So **"more than flash.bin + ota.bin" is the reliable warning sign**,
and it is a fact about the container, not a heuristic about its size.

Note which way round that is: Melodee's *image* is the same size as SLOOP's (602,112
bytes); the resource is a separate file after it. Size alone could never have told us.

Unverified, and flagged as such in the output: whether m-flasher leaves those extra files
unwritten, or writes them somewhere sensible anyway. The two failures on this fleet say
it does not complete them.
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

SECTOR = 0x1000
BOOT_AREA = 0x4000
FLASH_SIZE = 0x100000
FWSC_BLOCK = 0x30
FWSC_KEEP = 0x2F
FWSC_END = 0x7D
IDENTITY_MAX_BLOCKS = 20

#: Entry attribute bit for the app's own files (app.bin, cfg_tool.bin) rather than
#: data-region descriptors. Matches m-flasher's ``app_files_status`` test.
ATTR_FILE_BIT = 0x10

#: Files every package has. Anything beyond these is payload beyond the flash image.
BASE_FILES = {"flash.bin", "ota.bin"}


class PackageError(Exception):
    """The file is not a readable .fwsc package."""


# ------------------------------------------------------------------ primitives

def _make_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _make_table()


def crc16(data: bytes) -> int:
    """CRC16/XMODEM, as JieLi uses it.

    Table-driven: the bitwise version is ~8x slower, and recovering the chip key calls
    this tens of thousands of times, which made a single package take 31 s on the
    Pi 3B+ — long enough that the library page timed out.
    """
    crc = 0
    table = _CRC_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ b) & 0xFF]
    return crc


def enc(data: bytes, key: int) -> bytes:
    """JieLi's ENC cipher. Its own inverse."""
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ (key & 0xFF)
        key = ((key << 1) ^ (0x1021 if key & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def _u16(b: bytes, at: int) -> int:
    return struct.unpack_from("<H", b, at)[0]


def _u32(b: bytes, at: int) -> int:
    return struct.unpack_from("<I", b, at)[0]


def _cstr(b: bytes) -> str:
    end = b.find(b"\x00")
    return (b if end < 0 else b[:end]).decode("ascii", errors="replace")


# ------------------------------------------------------------------ structures

@dataclass(frozen=True)
class Region:
    """One entry in the app's file table, at an absolute flash address."""

    name: str
    addr: int
    length: int
    attr: int

    @property
    def end(self) -> int:
        return self.addr + self.length

    @property
    def address_text(self) -> str:
        return f"0x{self.addr:05X}-0x{self.end - 1:05X}"

    @property
    def is_app_file(self) -> bool:
        return bool(self.attr & ATTR_FILE_BIT)


@dataclass(frozen=True)
class PackageFile:
    name: str
    type: int
    offset: int
    size: int
    #: Kept in memory only (never cached) so the verdict can look inside the payload.
    data: bytes = field(default=b"", compare=False, repr=False)

    @property
    def is_extra_payload(self) -> bool:
        return self.name not in BASE_FILES


@dataclass
class Package:
    path: Path
    identity: str = ""
    files: list[PackageFile] = field(default_factory=list)
    flash: bytes = b""
    regions: list[Region] = field(default_factory=list)
    chip_key: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def model(self) -> str:
        return self.identity.rsplit("_", 1)[0] if "_" in self.identity else self.identity

    @property
    def image_bytes(self) -> int:
        return len(self.flash)

    @property
    def image_end(self) -> int:
        return len(self.flash) - 1

    @property
    def extra_payload(self) -> list[PackageFile]:
        return [f for f in self.files if f.is_extra_payload]


def _parse_app_table(image: bytes, key: int) -> list[Region]:
    """The app area's file table at 0x04000: up to 16 entries of 32 bytes."""
    out: list[Region] = []
    for i in range(16):
        off = 32 * i
        start = BOOT_AREA + off
        if start + 32 > len(image):
            break
        raw = enc(image[start:start + 32], key ^ ((off >> 2) & 0xFFFF))
        if crc16(raw[2:]) != _u16(raw, 0):
            break
        out.append(
            Region(
                name=_cstr(raw[16:32]) or f"entry{i}",
                addr=_u32(raw, 4),
                length=_u32(raw, 8),
                attr=raw[12],
            )
        )
    return out


def guess_chip_key(image: bytes) -> int | None:
    """Recover the chip key the image was encrypted with.

    A 16-bit key, so it is exhaustively recoverable: decrypt the first table entry with
    each candidate and keep those whose CRC, name and address are self-consistent. On
    our packages exactly one key survives (0x980F), so this is a parse, not a guess —
    but it returns None rather than a wrong key if the result is ambiguous.
    """
    survivors: list[int] = []
    for key in range(0x10000):
        raw = enc(image[BOOT_AREA:BOOT_AREA + 32], key)
        if crc16(raw[2:]) != _u16(raw, 0):
            continue
        name = _cstr(raw[16:32])
        if not name or not name.isascii() or not name.isprintable():
            continue
        if len(_parse_app_table(image, key)) < 2:
            continue
        survivors.append(key)
        if len(survivors) > 1:
            return None  # ambiguous: refuse rather than pick one
    return survivors[0] if survivors else None


def parse(path: Path) -> Package:
    """Parse a .fwsc into identity, files, flash image and regions."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackageError(f"cannot read {path}: {exc}") from exc
    if len(raw) < 0x1000:
        raise PackageError("too small to be a firmware package")

    pkg = Package(path=path)

    # The identity occupies whole 48-byte blocks at the start, padded with 0x7D. Block 0
    # always holds a character, so we continue while inside the first 20 blocks OR still
    # reading padding. (Inverting this stops at block 0, shifts every later offset by 47
    # bytes, and makes flash.bin fail its CRC — which looks like a corrupt download.)
    n = 0
    while n < IDENTITY_MAX_BLOCKS or raw[n * FWSC_BLOCK + FWSC_KEEP] == FWSC_END:
        n += 1
        if (n + 1) * FWSC_BLOCK > len(raw):
            raise PackageError("not a firmware package (.fwsc): identity not found")

    chars = []
    for i in range(n):
        m = raw[i * FWSC_BLOCK + FWSC_KEEP]
        if m == FWSC_END:
            break
        chars.append(chr((m - i - 1) & 0xFF))
    pkg.identity = "".join(chars)

    image = bytearray()
    for i in range(n):
        image += raw[i * FWSC_BLOCK:i * FWSC_BLOCK + FWSC_KEEP]
    image += raw[n * FWSC_BLOCK:]

    head = enc(bytes(image[:0x40]), 0xFFFF)
    if crc16(head[2:]) != _u16(head, 0):
        raise PackageError("not a firmware package, or a damaged one (header CRC)")
    count = _u16(head, 8)
    if 0x40 + 0x50 * count > len(image):
        raise PackageError("damaged firmware package (file list)")
    flist = bytes(image[0x40:0x40 + 0x50 * count])
    if crc16(flist) != _u16(head, 2):
        raise PackageError("damaged firmware package (file list CRC)")

    for i in range(count):
        e = enc(flist[i * 0x50:(i + 1) * 0x50], 0xFFFF)
        typ, dcrc, off, size = _u16(e, 0), _u16(e, 4), _u32(e, 8), _u32(e, 12)
        name = _cstr(e[0x40:])
        if off + size > len(image):
            raise PackageError("damaged firmware package (file outside the image)")
        keep_data = name not in BASE_FILES
        pkg.files.append(PackageFile(
            name=name, type=typ, offset=off, size=size,
            data=bytes(image[off:off + size]) if keep_data else b"",
        ))
        if typ == 0 and name == "flash.bin":
            data = bytes(image[off:off + size])
            if crc16(data) != dcrc:
                raise PackageError("damaged firmware package (flash.bin CRC)")
            pkg.flash = data

    if not pkg.flash:
        raise PackageError("the package has no flash image (flash.bin)")
    if len(pkg.flash) <= BOOT_AREA or len(pkg.flash) % SECTOR:
        raise PackageError(f"unexpected flash image size {len(pkg.flash)}")

    key = guess_chip_key(pkg.flash)
    if key is None:
        pkg.problems.append(
            "Could not decrypt the app's file table, so the data regions are unknown."
        )
    else:
        pkg.chip_key = key
        pkg.regions = _parse_app_table(pkg.flash, key)
    return pkg


# ------------------------------------------------------------------ the verdict

@dataclass(frozen=True)
class Inspection:
    path: Path
    severity: str  # "ok" | "warn"
    headline: str
    details: list[str]
    package: Package | None = None
    #: Kept separately so a cache hit can still report the identity without a re-parse.
    identity: str | None = None
    files: tuple[str, ...] = ()

    @property
    def complete_from_mflasher(self) -> bool:
        return self.severity == "ok"

    @property
    def name(self) -> str:
        return self.path.name


def inspect(path: Path) -> Inspection:
    """Judge whether this package will install completely via m-flasher."""
    return _inspect_cached(path)


class _Cache:
    """Parsed-package results, kept on disk and keyed by (path, size, mtime).

    Parsing costs a few tenths of a second even with the table-driven CRC, and the
    library page shows every package; the Pi has no speed to spare. One parse per file
    version, ever.
    """

    def __init__(self) -> None:
        raw = os.environ.get("FM1_INSPECT_CACHE")
        # expanduser() on BOTH branches. The default is a "~" string too, and leaving it
        # unexpanded made the cache land in a literal "./~" directory instead of $HOME,
        # so it never loaded and every request re-parsed every package (~8 s each on a
        # Pi 3B+, which turned the library page into a two-minute wait).
        default = Path("~/fm1-hub/inspect.json").expanduser()
        self.path = Path(raw).expanduser() if raw else default
        self.data: dict[str, dict] = {}
        self._loaded = False
        #: Guards ``data`` and the file write (audit PKG-01). Requests are threaded.
        self._lock = threading.RLock()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(path: Path) -> str | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return f"v{VERDICT_VERSION}|{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, path: Path) -> Inspection | None:
        k = self.key(path)
        with self._lock:
            self._load()
            rec = self.data.get(k) if k else None
        if not rec:
            return None
        return Inspection(
            path=path,
            severity=rec["severity"],
            headline=rec["headline"],
            details=list(rec["details"]),
            package=None,
            identity=rec.get("identity"),
            files=tuple(rec.get("files") or ()),
        )

    def put(self, ins: Inspection) -> None:
        k = self.key(ins.path)
        if not k:
            return
        with self._lock:
            self._put_locked(k, ins)

    def _put_locked(self, k: str, ins: Inspection) -> None:
        self._load()
        self.data[k] = {
            "path": str(ins.path),
            "severity": ins.severity,
            "headline": ins.headline,
            "details": ins.details,
            "identity": ins.identity,
            "files": list(ins.files),
        }
        # Keep it small: drop entries whose file no longer exists. Checked via the stored
        # path, not by parsing the key back apart — an earlier version split the key on
        # "|" and threw away everything, silently emptying the cache on the Pi.
        self.data = {
            kk: v for kk, v in self.data.items()
            if v.get("path") and Path(v["path"]).exists()
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(self.data))
                os.replace(name, self.path)
            except BaseException:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
        except OSError:
            pass


#: Readable name shapes that only a real sound bank produces. Measured on the fleet:
#: SHRUB 0, V15 1 (a stray "N PAD"), Melodee 47, ChoralRoot 9. The two firmwares that
#: reported missing content are the two above 3; the two that installed completely are
#: below it, by a wide margin.
SOUND_BANK_NAME_THRESHOLD = 3

#: Bump whenever the verdict logic changes, so cached verdicts from an older rule are
#: not served as if they were current. (Found the hard way: a tightened rule kept
#: returning the old answer because the cache key was path+size+mtime only.)
VERDICT_VERSION = 3


def _factory_sound_names(extra: list[PackageFile]) -> list[str]:
    """Readable factory sound-bank names inside a package's extra payload files.

    This is the signal that separated the firmwares which installed badly from those
    which installed well on this fleet. Melodee's ``res.bin`` holds 47 plaintext bank
    names ("BRASS 1", "STRINGS 2", ...) - its factory sounds, the very thing its screen
    complained was missing. ChoralRoot's ``res2.bin`` holds 9 ("TINE EP", "SOLID BASS").
    SHRUB's ``res.bin`` is *larger* (72 KiB) but yields none, and SHRUB installs
    completely; V15 yields one stray "N PAD" and installs completely.

    So neither presence nor size of an extra file decides it - readable name density
    does. The shape test is deliberately strict (upper-case words, optionally with a
    space and a number, vowel-bearing, not mixed-case soup) so binary noise such as
    ``sWs2``/``VbVZxb`` cannot pass.

    Returns the names found, so the UI can show its evidence rather than a bare verdict.
    """
    vowel = set("aeiouAEIOU")
    found: list[str] = []
    for f in extra:
        data = f.data or b""
        if not data:
            continue
        for run in re.findall(rb"[ -~]{4,40}", data):
            text = run.decode("ascii", "ignore").strip()
            if not (4 <= len(text) <= 40) or not text.isupper():
                continue
            letters = sum(c.isalpha() for c in text)
            if letters < 4 or not any(c in vowel for c in text):
                continue
            # Bank names are words, and the ones that matter are phrases: "BRASS 1",
            # "TINE EP", "SOLID BASS". Requiring a space is what keeps binary noise out
            # (QIIIII, UUUU, UTQA all pass a no-space test and none is a sound name).
            if " " not in text or not all(c.isalnum() or c == " " for c in text):
                continue
            if len({c for c in text if c.isalpha()}) < 3:
                continue
            if any(len(m) > 2 for m in re.findall(r"\d+", text)):
                continue
            found.append(text)
    return found


_CACHE = _Cache()


def _inspect_cached(path: Path) -> Inspection:
    hit = _CACHE.get(path)
    if hit is not None:
        return hit
    ins = _inspect_uncached(path)
    _CACHE.put(ins)
    return ins


def _inspect_uncached(path: Path) -> Inspection:
    try:
        pkg = parse(path)
    except PackageError as exc:
        return Inspection(path, "warn", f"Unreadable package: {exc}", [])

    extra = pkg.extra_payload
    details = [
        f"Identity {pkg.identity or 'unknown'} · flash image {pkg.image_bytes / 1024:.0f} KiB "
        f"(0x00000-0x{pkg.image_end:05X}) · files: {', '.join(f.name for f in pkg.files)}",
    ]
    if pkg.chip_key is not None:
        details.append(f"Chip key 0x{pkg.chip_key:04X}")
    if extra:
        details.append(
            "Extra files: " + ", ".join(f"{f.name} ({f.size / 1024:.0f} KiB)" for f in extra)
            + ". m-flasher writes the flash image only, so these are not written by it."
        )

    # The verdict is NOT "has extra files". That rule was wrong and the fleet proved it:
    # SHRUB carries res.bin (72 KiB) and installs completely; V15 carries a USR file
    # (71 KiB) and installs completely; Melodee's res.bin (56 KiB) produced "Factory
    # sounds missing". Presence and size do not separate them. Readable sound-bank
    # names do -- see _factory_sound_names and SOUND_BANK_NAME_THRESHOLD.
    sound_names = _factory_sound_names(extra)
    if len(sound_names) >= SOUND_BANK_NAME_THRESHOLD:
        listing = ", ".join(f"{f.name} ({f.size / 1024:.0f} KiB)" for f in extra)
        return Inspection(
            path,
            "warn",
            "Carries a factory sound bank that m-flasher cannot write.",
            details
            + [
                f"Sound-bank names found in the package ({listing}): "
                + ", ".join(sound_names[:6]) + ("…" if len(sound_names) > 6 else ""),
                "This is the payload behind 'Factory sounds missing / Setup unfinished'. "
                "Its own installer writes it; m-flasher does not. Use the maker's web "
                "installer for this one.",
            ],
            pkg,
            identity=pkg.identity,
            files=tuple(f.name for f in pkg.files),
        )

    return Inspection(
        path,
        "ok",
        "Installs completely from m-flasher.",
        details,
        pkg,
        identity=pkg.identity,
        files=tuple(f.name for f in pkg.files),
    )
