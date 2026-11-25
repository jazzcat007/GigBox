"""The only place that shells out to ``m-flasher``.

Every write goes through here so the argument shape is built in exactly one place.
``--device`` and ``--yes`` are always supplied: with several units connected we must
never let the tool pick "the one it finds", nor sit waiting on a prompt.

CLI reference (v0.12.1): https://github.com/jvitkauskas/m-flasher/blob/main/src/cli.rs

    m-flasher find | download-mode | restart | info
              backup [DIR] | restore BACKUP.bin [--keep-bootloader]
              firmware PACKAGE.fwsc
              bootloader [--stock PACKAGE.fwsc | --file UBOOT.boot]
    options:  --device PATH  --board fm1|smk37  --backup-dir DIR  --yes
              --restart (restore, firmware, bootloader: start the unit afterwards)
"""

from __future__ import annotations

import os

from .config import setting
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class MflasherError(Exception):
    """Base for every way an m-flasher invocation can fail."""


class NotFound(MflasherError):
    def __init__(self, binary: str) -> None:
        super().__init__(
            f"m-flasher not found at {binary!r} — install the ARM64 .deb on the Pi "
            "(sudo apt install ./m-flasher_*_arm64.deb), or set FM1_FLASHER"
        )
        self.binary = binary


class Exited(MflasherError):
    def __init__(self, code: int, stderr: str) -> None:
        detail = stderr.strip() or "(no stderr)"
        super().__init__(f"m-flasher exited {code}: {detail}")
        self.code = code
        self.stderr = stderr


class TimedOut(MflasherError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"m-flasher timed out after {seconds:g}s — the unit may be mid-write")
        self.seconds = seconds


class NothingWritten(MflasherError):
    """A writing command exited 0 but reported that it wrote nothing.

    Raised instead of returning success, because reporting "installed" when the unit
    still holds its old firmware is worse than reporting a failure.
    """


def binary() -> str:
    """Resolve the m-flasher executable. ``FM1_FLASHER`` wins; else PATH.

    An absolute ``FM1_FLASHER`` is used as given; a bare name is looked up on PATH. The
    first version of ``diagnose`` used ``shutil.which()`` on the result unconditionally,
    which reports "NOT FOUND" for a perfectly good absolute path that is not on PATH --
    exactly the setup the systemd unit uses. Resolve once, here, so callers agree.
    """
    import shutil
    wanted = setting("FM1_FLASHER", "flasher", "m-flasher")
    if os.path.isabs(wanted):
        return wanted
    # The systemd unit points FM1_FLASHER at ~/bin/m-flasher. A bare CLI run on the Pi does
    # not carry that env var, so fall back to the same absolute location before giving up
    # on PATH -- otherwise `diagnose` says "NOT FOUND" for a working install.
    default = Path("~/bin/m-flasher").expanduser()
    if default.is_file():
        return str(default)
    return shutil.which(wanted) or wanted


@dataclass
class Result:
    ok: bool
    stdout: str = ""
    stderr: str = ""


def run(args: list[str], device: str | None, timeout: float = 300.0) -> Result:
    """Run one m-flasher invocation.

    ``device`` is required for anything that acts on a unit. ``--yes`` is always added
    so the GUI can never hang on an interactive prompt.
    """
    if args and args[0] in _NEEDS_DEVICE and not device:
        raise MflasherError(f"refusing to run 'm-flasher {args[0]}' without a --device")
    exe = binary()
    resolved = shutil.which(exe) or (
        exe if os.path.isabs(exe) and os.access(exe, os.X_OK) else None
    )
    if resolved is None:
        raise NotFound(exe)

    argv = [resolved, *args, "--yes"]
    if device:
        argv += ["--device", device]

    try:
        proc = subprocess.run(
            argv,
            # m-flasher 0.12.1 STILL asks "Type yes to write" for `firmware` even when
            # --yes is given, and with stdin closed it answers itself with nothing and
            # bails. Feeding "yes" makes the prompt succeed. Harmless for commands that
            # never read stdin: the text is simply never consumed.
            #
            # Discovered on hardware 2026-11-20: the first install attempt wrote nothing
            # while appearing to succeed. See _check_written() for the second half.
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimedOut(timeout) from exc
    except OSError as exc:  # pragma: no cover - environment specific
        raise MflasherError(f"failed to run {resolved}: {exc}") from exc

    if proc.returncode != 0:
        raise Exited(proc.returncode, proc.stderr or proc.stdout)

    _check_written(args, (proc.stdout or "") + "\n" + (proc.stderr or ""))
    return Result(ok=True, stdout=proc.stdout, stderr=proc.stderr)


#: Commands that change, or read, one specific unit. With no --device m-flasher would pick
#: for itself, so these refuse to run unaddressed (audit MF-01).
_NEEDS_DEVICE = frozenset({"firmware", "restore", "bootloader", "backup", "restart",
                           "download-mode"})

#: m-flasher prints this and exits 0 when its confirmation prompt went unanswered.
#: Exit code 0 is NOT proof that anything was written — this string is the real signal.
_NOTHING_WRITTEN_RE = re.compile(r"nothing\s+written", re.IGNORECASE)


def _check_written(args: list[str], stdout: str) -> None:
    """Raise if a writing command reported that it wrote nothing.

    Without this, ``firmware``/``restore``/``bootloader`` can exit 0 having changed
    nothing, and the caller would report a successful install of firmware that is not
    on the unit. Err loud rather than lie.
    """
    if not args:
        return
    if args[0] not in ("firmware", "restore", "bootloader"):
        return
    if _NOTHING_WRITTEN_RE.search(stdout or ""):
        raise NothingWritten(
            "m-flasher reported 'Nothing written' — the write did not happen. "
            "This usually means a confirmation prompt went unanswered; the unit is "
            "unchanged and safe. Re-run the install."
        )


# --------------------------------------------------------------------------- identity
#
# `m-flasher info` prints (label, value) pairs, but the separator is NOT stable across
# versions:
#
#   m-flasher <= 0.12.1   "flash: 0x856014"       -- colon
#   m-flasher 1.3         "  flash       0x856014" -- whitespace, two-space aligned
#
# The first parser here required ":" or "=", so against the loader actually installed on
# the Pi (1.3) it matched *nothing*: every field came back None, unit ids were never
# read, and the web UI showed blank identities for weeks while reporting success. The
# tests passed because the fixtures were written to match the parser's assumption rather
# than the tool's output. Accept both forms, and keep the real 1.3 output as a fixture.
#
# The label must still be a known word from the fixed set below, so "Connecting to /dev/sg0"
# and "M-Flasher loader 1.3 already running" can never be mistaken for a field.
_KNOWN_LABELS = ("model", "bootloader", "firmware", "app", "chip key", "flash",
                 "unit id", "loader")
_LINE_RE = re.compile(
    r"^\s*(?P<label>" + "|".join(_KNOWN_LABELS).replace(" ", r"\s+") + r")"
    r"\s*(?:[:=]\s*|\s+)(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_CHIP_HEX_RE = re.compile(r"0x([0-9A-Fa-f]{6,8})")


@dataclass
class Identity:
    model: str | None = None
    bootloader: str | None = None
    firmware: str | None = None
    app: str | None = None
    chip_key: str | None = None
    flash_chip: str | None = None  # normalised "0xB36014" / "0x856014"
    unit_id: str | None = None
    loader: str | None = None
    raw: str = ""

    @property
    def has_unit_id(self) -> bool:
        return bool(self.unit_id and self.unit_id.strip() not in ("", "-", "–"))


def parse_info(text: str) -> Identity:
    """Parse ``m-flasher info`` output into labelled fields."""
    ident = Identity(raw=text)
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        if label == "model":
            ident.model = value
        elif label == "bootloader":
            ident.bootloader = value
        elif label == "firmware":
            ident.firmware = value
        elif label == "app":
            ident.app = value
        elif label == "chip key":
            ident.chip_key = value
        elif label == "flash":
            cm = _CHIP_HEX_RE.search(value)
            ident.flash_chip = "0x" + cm.group(1).upper() if cm else value
        elif label == "unit id":
            ident.unit_id = None if value in ("", "-", "–") else value
        elif label == "loader":
            ident.loader = value
    return ident


def info(device: str, timeout: float = 60.0) -> Identity:
    """Read a unit's identity (reads the whole flash, so allow generous time)."""
    return parse_info(run(["info"], device, timeout).stdout or "")


# --------------------------------------------------------------------------- actions
#
# Each helper returns the argv it would run, so the UI can show a "dry" plan and the
# tests can assert the shape without hardware.

def argv_download_mode(device: str | None = None) -> list[str]:
    """Enter download mode via the MIDI soft key.

    NOTE: this refuses when more than one unit is on USB MIDI ("connect only one").
    With a fleet, units are re-patched to the Pi in download mode already, so this is
    the single-unit path only.
    """
    args = ["download-mode"]
    return args + (["--device", device] if device else [])


def argv_firmware(package: str, *, restart: bool = True) -> list[str]:
    """Install a package and (optionally) boot it, in one invocation."""
    args = ["firmware", package]
    if restart:
        args.append("--restart")
    return args


def argv_restart() -> list[str]:
    """Leave download mode: reset the unit and let it boot its application."""
    return ["restart"]


def argv_info() -> list[str]:
    return ["info"]


def install_firmware(device: str, package: str, *, restart: bool = True,
                     timeout: float = 300.0) -> Result:
    """Switch one unit's firmware. The heavy, writing operation."""
    return run(argv_firmware(package, restart=restart), device, timeout)


def backup_unit(device: str, directory: str, *, timeout: float = 300.0) -> Result:
    """Save the unit's whole flash (and a .json record with its flash id and sha256) into
    ``directory``. Reads only; nothing on the unit changes."""
    return run(["backup", directory], device, timeout)


def restore_image(device: str, image: str, *, keep_bootloader: bool = True,
                  restart: bool = True, timeout: float = 600.0) -> Result:
    """Write a whole-flash backup (``m-flasher backup``) back to one unit."""
    args = ["restore", image]
    if keep_bootloader:
        args.append("--keep-bootloader")
    if restart:
        args.append("--restart")
    return run(args, device, timeout)


def restart(device: str, timeout: float = 90.0) -> Result:
    return run(argv_restart(), device, timeout)


def download_mode(device: str | None = None, timeout: float = 60.0) -> Result:
    return run(argv_download_mode(device), device, timeout)
