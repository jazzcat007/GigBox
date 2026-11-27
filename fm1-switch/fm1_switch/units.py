"""Unit mapping: stable slot names (fm1-a..d) to download-mode SCSI devices.

The FM-1 appears in download mode as a SCSI generic device (``/dev/sgN``) whose number
RENUMBERS on every replug, so ``/dev/sg0`` is not a stable identity. We bind units to
*physical USB port paths* with UDEV SYMLINKs (fm1-a..d), which stay fixed while a unit
stays in the same hub port. This module reads those symlinks and the fallback
``/dev/sg*`` scan without guessing which unit is meant.

Nothing here shells out to hardware: :func:`enumerate_units` only lists what is present,
and the ``/dev/fm1-*`` symlinks are emitted by the udev rules in ``fm1-switch/udev/``.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

from . import config
from .config import unit_count

# Standard slot names, in cabling order. fm1-a..d map to hub/port paths via udev.
# The count comes from the config file (default 4, at most 16): see config.py.
SLOTS: tuple[str, ...] = tuple(f"fm1-{chr(97 + i)}" for i in range(unit_count()))

# A nice human label per slot, so the TUI can say "unit C" instead of "fm1-c".
SLOT_LABELS: dict[str, str] = {s: s[-1].upper() for s in SLOTS}


@dataclass
class Unit:
    """One addressed FM-1. Only ``device`` is required; the rest is discovered later."""

    slot: str
    device: str  # the /dev/... path m-flasher --device takes
    flash_chip: str | None = None  # "0x856014" / "0xB36014", read by `info`
    unit_id: str | None = None  # per-unit factory id, read by `info`
    firmware: str | None = None  # text label of what is currently installed


def _slot_symlink(slot: str) -> str:
    return f"/dev/{slot}"


def enumerate_units(slots: tuple[str, ...] = SLOTS) -> list[Unit]:
    """Return the units whose download-mode symlink currently exists.

    A unit running normal firmware is NOT a SCSI device, so its symlink does not
    exist and it is simply absent here — expected, not an error. The caller decides
    whether absence matters.
    """
    units: list[Unit] = []
    for slot in slots:
        link = _slot_symlink(slot)
        target = os.path.realpath(link) if os.path.islink(link) else link
        if os.path.exists(target):
            units.append(Unit(slot=slot, device=target))
    return units


def list_download_devices() -> list[str]:
    """All /dev/sg* nodes, so a unit can be found before udev symlinks are set up."""
    return sorted(glob.glob("/dev/sg*"))


def candidates(slots: tuple[str, ...] = SLOTS) -> list[str]:
    """Slots that could exist (for a TUI to show all four even when absent)."""
    return list(slots)


# ---------------------------------------------------------------- port map (slot -> port)

def port_map_path() -> Path:
    """Where ``label-units.sh`` / the operator records slot -> USB port. Best effort."""
    return config.path("portmap")


def read_port_map() -> dict[str, str]:
    """slot -> USB port path (e.g. ``fm1-a`` -> ``1-1.1.2``), from the port map file.

    The file is tab-separated and written by ``label-units.sh``. A malformed line is
    skipped rather than raising: the map is a convenience for the UI and for addressing
    the soft key, never the authority on which unit is which (the sticker is).
    """
    out: dict[str, str] = {}
    try:
        text = port_map_path().read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] in SLOTS:
            out[parts[0]] = parts[1]
    return out


def port_for_slot(slot: str) -> str | None:
    return read_port_map().get(slot)

