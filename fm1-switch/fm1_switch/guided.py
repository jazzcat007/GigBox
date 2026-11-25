"""Flash one unit, named by its flash id, from running firmware to done.

Why this exists
---------------
The touch panel names a unit by the sticker bound to its **flash id** (see ``registry``),
because the USB ports on this rig are random. The older write path (``web.flash_unit``)
finds its target through ``/dev/fm1-<slot>``, a udev symlink bound to a *port*. When a
cable moves, the dialog says "Unit A" and the write goes to whatever sits on A's old
port. This module closes that gap: **the unit that is written is the one whose flash
reports the id the operator confirmed**, whatever port or ``/dev/sgN`` it landed on.

The flow
--------
1. Refuse unless the confirm token equals the unit id, and the id looks like a real flash
   id (a ``port:`` or ``device:`` placeholder names nothing).
2. Take the one flash lock (never concurrent with another write).
3. If the unit is not already in download mode, send the MIDI soft key. On this hardware
   that has been measured both ways: a broadcast on 2026-11-21 (every MIDI-capable unit
   went to download mode), pointed in every run since 2026-11-22 (including the full
   four-unit bench on 2026-11-23, where only the target moved). It is never *treated* as
   pointed: the code re-reads every id and restarts whatever else moved.
4. Wait for the newly appeared download-mode devices to settle, then read each one's
   flash id. Nothing is assumed about which is which.
5. If the confirmed id is not among them: write nothing, restart what the soft key moved.
6. Otherwise restart every *other* unit the soft key moved (they are only in the way; a
   unit that was already in download mode before we began is left alone), then write the
   matching device and restart it into the new firmware.

A write never happens to a device whose id has not just been read and matched.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import mflasher, registry, softkey


class WriteRefused(Exception):
    """A write was asked for that the server will not perform, and why."""


class Busy(WriteRefused):
    """Another device operation holds the lock. Not a failure of *this* request's unit, so
    it must not overwrite the running write's ``last_event`` (audit R2)."""


class WriteFailed(WriteRefused):
    """The write itself was attempted and m-flasher failed part-way. Unlike a plain refusal,
    the unit may be partly written, so the log and the panel must not call this a refusal.
    The unit is left in download mode, and a backup taken just before is the way back."""


#: Flash ids seen on this fleet: 16 hex digits. ``port:1-1.2``, ``device:sg0`` and
#: ``slot:fm1-a`` are placeholders for "no readable id" and must never reach a write.
_FLASH_ID = re.compile(r"^[0-9a-fA-F]{8,}$")


def looks_like_flash_id(unit_id: str) -> bool:
    return bool(_FLASH_ID.match(unit_id or ""))


@dataclass(frozen=True)
class Device:
    node: str  # /dev/sgN -- an address for this moment, never an identity
    port: str  # USB port path, e.g. 1-1.2


@dataclass
class Outcome:
    unit_id: str
    device: str
    already_in_download: bool
    restarted: list[str] = field(default_factory=list)   # flash ids put back to running
    stuck: list[str] = field(default_factory=list)       # still in download mode: power-cycle
    message: str = ""
    relearned: str = ""   # registry.relearn_after_flash result


# --------------------------------------------------------------------------- discovery

def download_devices() -> list[Device]:
    """Every FM-1 in download mode right now, found structurally (not via udev symlinks,
    which are port-bound and absent for a unit on an unmapped port)."""
    out: list[Device] = []
    for path in sorted(glob.glob(os.path.join(registry._SCSI_GENERIC, "sg*"))):
        node = "/dev/" + os.path.basename(path)
        port = registry.port_of_sg(node)
        if port and softkey.port_ids(port) in softkey.DOWNLOAD_IDS:
            out.append(Device(node, port))
    return out


def read_id(dev: Device) -> str | None:
    """The flash id of the unit in ``dev``, or None if it cannot be read."""
    try:
        ident = mflasher.info(dev.node)
    except mflasher.MflasherError:
        return None
    return ident.unit_id if ident.has_unit_id and ident.unit_id else None


def _soft_key_ports(unit_id: str) -> list[str]:
    """Ports to try the soft key on: the unit's own last-seen port first, then any other
    running unit with MIDI. Because the soft key may be a broadcast (see the module docstring), the port is only a
    way to deliver it; identity is established afterwards by reading flash ids."""
    ports: list[str] = []
    for lu in registry.known():
        if lu.unit_id == unit_id and lu.running and lu.port:
            ports.append(lu.port)
    for run in registry.running():
        if run.has_midi and run.port not in ports:
            ports.append(run.port)
    return ports


# --------------------------------------------------------------------------- the flow

def flash(unit_id: str, package: str, *, lock, on_phase: Callable[[str], None] = lambda s: None,
          firmware_label: str | None = None, writer=None, before_write=None,
          timeout: float = 25.0, settle: float = 3.0, poll: float = 0.5,
          boot_timeout: float = 60.0,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Outcome:
    if not looks_like_flash_id(unit_id):
        raise WriteRefused(
            "this unit has no readable flash id, so it cannot be told apart from its "
            "neighbours. Register it first (power it off, then on alone).")
    if not lock.acquire(blocking=False):
        raise Busy("another flash is already running")
    try:
        return _flash_locked(unit_id, package, on_phase, timeout, settle, poll, sleep, clock,
                             firmware_label, writer, before_write, boot_timeout)
    finally:
        registry.forget_identities()   # the flash changed what the unit reports
        lock.release()


def _flash_locked(unit_id, package, on_phase, timeout, settle, poll, sleep, clock,
                  firmware_label=None, writer=None, before_write=None,
                  boot_timeout=60.0) -> Outcome:
    before = download_devices()
    before_ports = {d.port for d in before}

    on_phase("Reading units already in download mode…")
    ids = {d: read_id(d) for d in before}
    target = next((d for d, i in ids.items() if i == unit_id), None)
    moved: dict[Device, str | None] = {}

    already = target is not None
    if target is None:
        on_phase("Putting units into download mode…")
        _send_soft_key(unit_id)
        new = _wait_for_new(before_ports, timeout, settle, poll, sleep, clock)
        if not new:
            raise WriteRefused(
                "no unit entered download mode. Nothing was written; power-cycle the unit "
                "and try again.")
        on_phase("Identifying units…")
        moved = {d: read_id(d) for d in new}
        target = next((d for d, i in moved.items() if i == unit_id), None)

    others = [(d, i) for d, i in moved.items() if d != target]
    restarted: list[str] = []
    stuck: list[str] = []

    def restart_others() -> None:
        if others:
            on_phase("Restarting the other units…")
        for dev, ident in others:
            try:
                mflasher.restart(dev.node)
                restarted.append(ident or dev.node)
            except mflasher.MflasherError:
                stuck.append(ident or dev.node)

    if target is None:
        restart_others()
        extra = f" Could not restart: {', '.join(stuck)} — power-cycle them." if stuck else ""
        raise WriteRefused(
            f"unit {unit_id[-4:]} did not appear in download mode, so nothing was written. "
            f"Units that did were restarted.{extra}")

    restart_others()

    # The restarts above change the bus. Confirm the matched device is still the one we
    # identified (same node on the same port) before the write.
    if target not in download_devices():
        raise WriteRefused("the unit moved while the others restarted; nothing was written. "
                           "Try again.")

    if before_write is not None:
        # A copy of what is on the unit now, taken from the device just matched by id. If it
        # cannot be saved, nothing is written and the unit is sent back to running firmware.
        on_phase("Saving a backup…")
        try:
            before_write(target.node)
        except (mflasher.MflasherError, OSError) as exc:
            try:
                mflasher.restart(target.node)
            except mflasher.MflasherError:
                pass
            raise WriteRefused(f"could not save a backup first ({exc}); nothing was written "
                               "and the unit was restarted") from exc

    on_phase(f"Writing to unit {unit_id[-4:]}…")
    try:
        (writer or mflasher.install_firmware)(target.node, package)
    except mflasher.MflasherError as exc:
        raise WriteFailed(f"{exc} — the unit may be partly written and is still in "
                          "download mode; restart it, or restore it from the backup "
                          "taken just before this write") from exc

    # The new firmware changes the unit's running fingerprint; re-learn it or the panel
    # will report the unit as not connected.
    on_phase("Waiting for the unit to restart…")
    relearned = _relearn(unit_id, target.port, firmware_label, boot_timeout, poll, sleep, clock)

    msg = "installed"
    if stuck:
        msg += f"; could not restart {', '.join(stuck)} — power-cycle them"
    if relearned not in ("ok", "unbound", "shared"):
        msg += f"; could not re-identify it afterwards ({relearned})"
    return Outcome(unit_id, target.node, already, restarted, stuck, msg, relearned)


def restart_unit(unit_id: str, *, lock, on_phase: Callable[[str], None] | None = None,
                 boot_timeout: float = 30.0, poll: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Return a unit that is sitting in download mode to running firmware.

    Found by flash id, like a write: every download-mode device is read and only the one
    reporting ``unit_id`` is restarted. Nothing is written to flash.

    The unit's running fingerprint and port are re-learned once it is back, exactly as after
    a flash: firmware changed by anything else (another tool, another developer) leaves the
    registry's fingerprint stale, and without this the unit stays "?" however often it is
    restarted. Metadata only; a failed re-learn does not fail the restart."""
    if not looks_like_flash_id(unit_id):
        raise WriteRefused("this unit has no readable flash id.")
    if not lock.acquire(blocking=False):
        raise Busy("a flash is running; wait for it to finish")
    try:
        if on_phase:
            on_phase("Restarting…")
        target = next((d for d in download_devices() if read_id(d) == unit_id), None)
        if target is None:
            raise WriteRefused("that unit is not in download mode (or could not be read)")
        try:
            mflasher.restart(target.node)
        except mflasher.MflasherError as exc:
            raise WriteRefused(f"restart failed: {exc}") from exc
        if on_phase:
            on_phase("Waiting for the unit to restart…")
        relearned = _relearn(unit_id, target.port, None, boot_timeout, poll, sleep, clock)
        msg = "restarted"
        if relearned not in ("ok", "unbound", "shared"):
            msg += f"; could not re-identify it afterwards ({relearned})"
        return Outcome(unit_id, target.node, True, [unit_id], [], msg, relearned)
    finally:
        registry.forget_identities()
        lock.release()


def _relearn(unit_id, port, label, timeout, poll, sleep, clock) -> str:
    deadline = clock() + timeout
    while True:
        if softkey.port_ids(port) in softkey.RUNNING_IDS:
            break
        if clock() >= deadline:
            return "not-running"
        sleep(poll)
    try:
        return registry.relearn_after_flash(unit_id, port, label)
    except OSError:
        return "not-saved"


def _send_soft_key(unit_id: str) -> None:
    ports = _soft_key_ports(unit_id)
    if not ports:
        raise WriteRefused(
            "no running unit has a MIDI link to carry the download-mode key. Power-cycle "
            "the unit you want to flash and try again.")
    last: Exception | None = None
    for port in ports:
        try:
            softkey.soft_key(port)
            return
        except (softkey.NoSuchPort, softkey.NoMidiInterface) as exc:
            last = exc
    raise WriteRefused(f"could not send the download-mode key: {last}")


def _wait_for_new(before_ports: set[str], timeout, settle, poll, sleep, clock) -> list[Device]:
    """Newly appeared download-mode devices, once the set has stopped changing for
    ``settle`` seconds (if the soft key broadcasts, several units move a moment apart)."""
    deadline = clock() + timeout
    last: tuple[Device, ...] = ()
    stable_since = clock()
    while clock() < deadline:
        now = tuple(d for d in download_devices() if d.port not in before_ports)
        if now != last:
            last, stable_since = now, clock()
        elif now and clock() - stable_since >= settle:
            return list(now)
        sleep(poll)
    return list(last)
