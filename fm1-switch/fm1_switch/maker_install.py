"""Finish a two-step firmware from the Pi with the maker's own installer (Phase D, option 2).

Some firmwares are only complete once the maker's installer has run: its update loader reads the
whole package over USB-MIDI and writes the extra region (JIANT's Bluetooth part). ``m-flasher``
cannot do that. The maker ships a Python tool for it; this module runs it as a separate process,
in its own venv (``mido`` and ``python-rtmidi`` live there, not here), against one unit.

Only the families listed in ``FAMILIES`` are offered, and only after a bench trial on a real unit
(JIANT: 2026-11-25, unit C, Bluetooth tag missing -> present). This module never writes to a
unit itself. The caller (``web_writes.finish_setup_from_pi``) owns the guards, the backup and the
log.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import path_setting


class MakerError(Exception):
    """The maker's tool could not be run, or reported a failure."""


@dataclass(frozen=True)
class Family:
    prefix: str          # the package name starts with this, lowercase
    reports: str         # what the running unit's INFO text must contain
    done_bt: int         # the Bluetooth tag value that means "finished"


#: Families whose maker's installer has been tried on a real unit. Do not add one untested.
FAMILIES = (Family(prefix="jiant", reports="JIANT", done_bt=1),)


def family_for(package: str) -> Family | None:
    name = package.lower()
    return next((f for f in FAMILIES if name.startswith(f.prefix)), None)


def tools_dir() -> Path:
    return path_setting("FM1_MAKER_TOOLS", "maker_tools", "~/fm1/vendor/jiant")


def _python() -> Path:
    return tools_dir() / "venv" / "bin" / "python"


def _installer() -> Path:
    return tools_dir() / "fm1_install.py"


def available() -> bool:
    return _python().is_file() and _installer().is_file()


def alsa_client_for_usb_path(usb_path: str, aconnect: str = "") -> int | None:
    """The ALSA sequencer client of the unit on this USB port path (``1-1.2``), or None.
    Matched by whole path component, so ``1-1.1`` never matches ``1-1.1.2``."""
    if not usb_path:
        return None
    if not aconnect:
        try:
            aconnect = subprocess.run(["aconnect", "-l"], capture_output=True, text=True,
                                      timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    found = []
    for client, card in re.findall(r"client (\d+): '[^']*' \[type=kernel,card=(\d+)\]", aconnect):
        try:
            parts = os.path.realpath(f"/sys/class/sound/card{card}/device").split("/")
        except OSError:
            continue
        if usb_path in parts:
            found.append(int(client))
    return found[0] if len(found) == 1 else None


def probe(client: int) -> dict:
    """Ask the running unit for its firmware text and Bluetooth tag. Read-only."""
    script = Path(__file__).with_name("maker_probe.py")
    try:
        out = subprocess.run([str(_python()), str(script), str(client)], capture_output=True,
                             text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MakerError(f"could not ask the unit: {exc}") from exc
    try:
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise MakerError("the unit's answer could not be read") from None
    if "error" in info:
        raise MakerError(f"the unit did not answer ({info['error']}). It is probably not running "
                         "this firmware (stock or another one?). Nothing was written; check the "
                         "unit's screen and write the package again first")
    return info


def run_install(package: Path, client: int, on_progress: Callable[[str], None],
                timeout: float = 900.0) -> None:
    """Run the maker's installer for ``package`` against the unit on ``client``. Raises
    ``MakerError`` unless it exits 0. ``on_progress`` gets short text for the panel."""
    cmd = [str(_python()), str(_installer()), str(package), "--port", f" {client}:0", "--yes"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise MakerError(f"could not start the maker's installer: {exc}") from exc
    tail = ""
    try:
        end = time.monotonic() + timeout
        assert proc.stdout is not None
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            tail = (tail + chunk.decode("utf-8", "replace"))[-2000:]
            m = re.findall(r"(writing|checking|loader|restarting)\s*(\d+)?%?", tail)
            if m:
                word, pct = m[-1]
                on_progress(f"Installing from the Pi… {word}{' ' + pct + '%' if pct else ''}")
            if time.monotonic() > end:
                proc.kill()
                raise MakerError("the maker's installer took too long and was stopped")
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise MakerError("the maker's installer did not finish") from None
    if code != 0:
        last = [ln.strip() for ln in re.split(r"[\r\n]+", tail) if ln.strip()]
        raise MakerError(f"the maker's installer failed (exit {code}): "
                         f"{last[-1] if last else 'no output'}")


__all__ = ["FAMILIES", "Family", "MakerError", "alsa_client_for_usb_path", "available",
           "family_for", "probe", "run_install", "tools_dir"]
