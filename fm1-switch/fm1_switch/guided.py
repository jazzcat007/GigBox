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

from . import config, mflasher, registry, softkey
from .units import SLOT_LABELS


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
          timeout: float | None = None, settle: float | None = None, poll: float = 0.5,
          boot_timeout: float | None = None,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Outcome:
    if not looks_like_flash_id(unit_id):
        raise WriteRefused(
            "this unit has no readable flash id, so it cannot be told apart from its "
            "neighbours. Register it first (power it off, then on alone).")
    # None means "what the settings say" (download_mode_wait, download_mode_settle, boot_wait).
    if timeout is None:
        timeout = float(config.value("download_mode_wait"))
    if settle is None:
        settle = float(config.value("download_mode_settle"))
    if boot_timeout is None:
        boot_timeout = float(config.value("boot_wait"))
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
                 boot_timeout: float | None = None, poll: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Return a unit that is sitting in download mode to running firmware.

    Found by flash id, like a write: every download-mode device is read and only the one
    reporting ``unit_id`` is restarted. Nothing is written to flash.

    The unit's running fingerprint and port are re-learned once it is back, exactly as after
    a flash: firmware changed by anything else (another tool, another developer) leaves the
    registry's fingerprint stale, and without this the unit stays "?" however often it is
    restarted. Metadata only; a failed re-learn does not fail the restart."""
    if boot_timeout is None:
        boot_timeout = float(config.value("restart_boot_wait"))
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


@dataclass
class ReconcileOutcome:
    """What a reconcile found. Metadata only: nothing was written to any unit's flash."""

    matched: list[tuple[str, str, str]] = field(default_factory=list)  # (slot, unit_id, port)
    unregistered: list[str] = field(default_factory=list)   # flash ids no sticker owns
    missing: list[str] = field(default_factory=list)        # slots whose unit was not found
    stuck: list[str] = field(default_factory=list)          # ids/nodes still in download mode
    no_midi: list[str] = field(default_factory=list)        # ports needing a power cycle
    message: str = ""


def reconcile(*, lock, everything: bool = False,
              on_phase: Callable[[str], None] | None = None,
              timeout: float | None = None, settle: float | None = None,
              boot_timeout: float | None = None, poll: float = 0.5,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> ReconcileOutcome:
    """Put every registered sticker back on the unit that wears it, after a reconnect.

    After the cables move (or a hub is swapped) the registry's saved ports and running
    fingerprints are stale, and running units that look alike (same firmware, same
    ``bcdDevice``) cannot be told apart from USB alone: the letters show "not connected"
    and the units show as "?". The one thing that CAN tell them apart is the flash id, so:

    1. work out which running units the registry cannot place (the rest are left alone);
    2. put those into download mode (the soft key; it may take others with it, so every
       unit that moves is handled, not just the ones asked for);
    3. read each unit's flash id and match it to its sticker BY ID;
    4. restart them all and re-learn each one's port and fingerprint, which is what lets
       the panel place identical-looking units from then on.

    Never binds a new sticker (a flash id nobody owns is reported, and REGISTER stays the
    deliberate act it is) and never writes flash. A unit already sitting in download mode
    is not touched: it may be there on purpose.

    ``everything=True`` is the SCAN FLEET button: every running unit is scanned, whether or
    not the registry thinks it has placed it, so the answer never rests on a guess.
    """
    if boot_timeout is None:
        boot_timeout = float(config.value("restart_boot_wait"))
    if timeout is None:
        timeout = float(config.value("download_mode_wait"))
    if settle is None:
        settle = float(config.value("download_mode_settle"))
    if not lock.acquire(blocking=False):
        raise Busy("a flash is running; wait for it to finish")
    say = on_phase or (lambda s: None)
    try:
        reg = registry.load()
        runs = registry.running()
        paired = registry._match_running(reg, runs, set())
        # A look-alike unit placed only by its last-seen port is a guess once it has
        # re-enumerated (power cycle, moved cable): treat those as unmatched too.
        doubtful = {id(r) for r in registry.unproven_runs(reg, runs, paired)}
        paired = [(r, b) for r, b in paired if id(r) not in doubtful]
        placed = {id(r) for r, _ in paired}
        lost = list(runs) if everything else [r for r in runs if id(r) not in placed]
        out = ReconcileOutcome()
        if not lost:
            out.message = ("every running unit is already matched to its sticker"
                           if runs else "no running units to match")
            return out
        out.no_midi = [r.port for r in lost if not r.has_midi]
        targets = [r.port for r in lost if r.has_midi]
        if not targets:
            raise WriteRefused(
                "no unmatched unit has a MIDI link to carry the download-mode key. "
                "Power-cycle the units, or put them into download mode and use REGISTER.")

        say("Putting the unmatched units into download mode…")
        before_ports = {d.port for d in download_devices()}
        moved: list[Device] = []
        key_error: Exception | None = None
        for port in targets:
            if any(d.port == port for d in moved) or softkey.port_ids(port) in softkey.DOWNLOAD_IDS:
                continue                 # an earlier key (it may be a broadcast) took this one
            try:
                softkey.soft_key(port)
            except (softkey.NoSuchPort, softkey.NoMidiInterface) as exc:
                key_error = exc
                continue
            moved += _wait_for_new(before_ports | {d.port for d in moved},
                                   timeout, settle, poll, sleep, clock)
        if not moved:
            raise WriteRefused(
                "no unit entered download mode, so nothing was matched"
                + (f" ({key_error})" if key_error else "")
                + ". Power-cycle the units and try again.")

        say("Identifying units…")
        ids = {d: read_id(d) for d in moved}
        bound = {b.unit_id: b.slot for b in reg.units.values() if b.unit_id}

        say("Restarting the units…")
        back: list[tuple[Device, str | None]] = []
        for dev, ident in ids.items():
            try:
                mflasher.restart(dev.node)
                back.append((dev, ident))
            except mflasher.MflasherError:
                out.stuck.append(ident or dev.node)

        say("Re-registering the units…")
        found: set[str] = set()
        for dev, ident in back:
            if not ident or not looks_like_flash_id(ident):
                out.unregistered.append(f"port {dev.port}")
                continue
            if ident not in bound:
                out.unregistered.append(ident)
                continue
            result = _relearn(ident, dev.port, None, boot_timeout, poll, sleep, clock)
            if result in ("ok", "shared"):
                out.matched.append((bound[ident], ident, dev.port))
                found.add(bound[ident])
            else:
                out.stuck.append(ident)
        already = set() if everything else {b.slot for _, b in paired}      # placed before we started: not "missing"
        out.missing = [s for s in sorted(reg.units) if s not in found and s not in already
                       and reg.units[s].unit_id not in set(out.stuck)]
        out.matched.sort()
        out.message = _reconcile_message(out)
        return out
    finally:
        registry.forget_identities()
        lock.release()


def _reconcile_message(out: ReconcileOutcome) -> str:
    def letter(slot: str) -> str:
        return SLOT_LABELS.get(slot, slot)
    parts = []
    if out.matched:
        parts.append("matched " + " ".join(letter(s) for s, _, _ in out.matched))
    if out.missing:
        parts.append("not found: " + " ".join(letter(s) for s in out.missing)
                     + " (off, unplugged, or not reachable)")
    if out.unregistered:
        parts.append(f"{len(out.unregistered)} unit(s) with no sticker; use REGISTER")
    if out.stuck:
        parts.append("could not restart or re-identify: " + ", ".join(out.stuck)
                     + " (power-cycle them)")
    if out.no_midi:
        parts.append("power-cycle the unit(s) on " + ", ".join(out.no_midi))
    return "; ".join(parts) or "nothing matched"


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
