"""Who is who: bind each FM-1 to its sticker by its flash's factory unit id.

Why the port map was the wrong key
----------------------------------
The first design bound units to physical USB ports (``fm1-a`` -> ``1-1.1.2``) and the
operator recorded that mapping once. It works only while every cable stays in the same
jack. This fleet's brief is explicit: the USB ports are **random every time** the
dawless rig is set up, so a port map is wrong by default and silently wrong the moment
two units swap jacks -- which is exactly the case the port map was supposed to catch.

Why an identity read from USB is not enough either
--------------------------------------------------
A *running* FM-1 presents as either ``1209:0001`` (community firmwares that expose MIDI,
announcing "Felucca") or ``4d4a:4155`` (stock-style). Measured on this fleet, three of
four running units present identically, and one of them has no MIDI interface at all.
So while a unit is running, the Pi genuinely cannot tell it from its neighbour. That is
not a limitation to work around with cleverness -- it is a fact, and the UI says so.

What identity actually survives
-------------------------------
Each flash chip carries a factory-unique id that ``m-flasher info`` reports as "unit id".
The id is per-unit, so on any working fleet the four ids are distinct from one another.
The examples used throughout this repository's fixtures are obviously fake ids in that
shape (the bench's real ids belong in no published file):

    aaaaaaaa00000001   aaaaaaaa00000002   aaaaaaaa00000003   aaaaaaaa00000004

That id does not depend on which port the unit is in, which firmware it runs, or whether
the Pi rebooted. So the id is the key and the sticker is the value. The port becomes
*where the unit happens to be right now* -- a cache, refreshed on every read.

Binding, and why it is a deliberate act
---------------------------------------
The id can only be read in download mode. Entering download mode is itself a deliberate
act (soft key, or unplug/replug after a failed boot), and it is the moment the operator
can physically see which sticker they just moved. So:

    the Pi sees a unit in download mode with id X
    the operator says "that one is fm1-a"
    we record X -> fm1-a

Never the reverse: a sticker is never inferred from a port, because that inference is
the thing that breaks. An unbound unit is shown as ``unassigned`` and can still be
flashed on purpose -- named by its id, never by a guess.

A second, weaker signal: ``bcdDevice``
--------------------------------------
A *running* unit cannot be asked for its flash id, so the only thing that separates two
Felucca units on the bench is their ``bcdDevice``. Measured on this fleet (2026-11-22):

* it is per-unit, not per-port: after swapping every cable so each unit moved to a
  different jack, each ``bcdDevice`` travelled with its own unit;
* it is stable across re-enumeration: one unit power-cycled back to the same value;
* it is *not* a firmware version: the same physical unit reported ``0300`` in download
  mode and ``0328`` while running SLOOP.

Two Felucca units read ``0328`` and ``0329``; the stock-style one reads ``0104``. So the
low byte looks like a per-chip ordinal.

It is still treated as a HINT, not an identity, for two honest reasons: it has been
observed on four units over one morning, and nothing has confirmed the value survives a
firmware flash or a different hub. A hint may *suggest* which sticker a running unit
wears; it may never authorise a write. Writes are addressed to the flash id, which is
confirmed by reading the chip every time.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import mflasher
from . import config
from .units import SLOT_LABELS, SLOTS

#: A sysfs chain looks like
#: /sys/devices/platform/soc/3f980000.usb/usb1/1-1/1-1.2/1-1.2:1.0/host0/.../sg0
#: The USB device node is the last component before the interface ("1-1.2:1.0").
_USB_DEV_ROOT = "/sys/bus/usb/devices"
_SCSI_GENERIC = "/sys/class/scsi_generic"


def registry_path() -> Path:
    return config.path("registry")


@dataclass
class Binding:
    """One unit: its sticker, its durable id, and what was true when it was bound.

    ``unit_id`` is empty for a *provisional* binding: one taught by watching the unit
    power on, before its flash has ever been read. A provisional binding is enough to
    label a row on the panel and to tell the operator that unit B is probably this one,
    but it carries no identity and can never authorise a write.
    """

    slot: str
    unit_id: str
    port: str | None = None  # where it was last seen; a hint, never an identity
    chip: str | None = None
    firmware: str | None = None
    bound_at: float | None = None
    #: ``bcdDevice`` while *running*, e.g. "0328". A weak per-unit hint -- see below.
    fingerprint: str | None = None
    #: A short name the operator gave this unit ("BASS"), shown beside the sticker letter.
    name: str | None = None
    #: Set when the last write was a two-step firmware (a NEEDS INSTALLER package): the
    #: unit is not finished until the operator confirms the maker's installer step.
    #: ``{"package": name, "sha256": hex, "since": epoch seconds}`` or None.
    setup_pending: dict | None = None
    #: USB device number of the running unit when its flash id was last read and its port
    #: learned (MATCH UNITS). The kernel gives a unit a new number every time it
    #: re-enumerates, so a port pairing between look-alike units is PROVEN only while the
    #: number still matches: a power cycle or a moved cable invalidates it.
    devnum: str | None = None

    @property
    def label(self) -> str:
        return SLOT_LABELS.get(self.slot, self.slot)

    @property
    def provisional(self) -> bool:
        """True when this was taught by power-on and its flash id is still unknown."""
        return not self.unit_id


@dataclass
class Registry:
    units: dict[str, Binding] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.units is None:
            self.units = {}

    # -- lookup ------------------------------------------------------------
    def binding_for_id(self, unit_id: str | None) -> Binding | None:
        # A provisional binding has no id; matching "" against a real unit would be a
        # false positive, so an empty id never resolves.
        if not unit_id:
            return None
        for b in self.units.values():
            if b.unit_id and b.unit_id == unit_id:
                return b
        return None

    def slot_for_id(self, unit_id: str | None) -> str | None:
        b = self.binding_for_id(unit_id)
        return b.slot if b else None

    def free_slots(self) -> list[str]:
        return [s for s in SLOTS if s not in self.units]

    def by_slot(self, slot: str) -> Binding | None:
        return self.units.get(slot)

    # -- mutation ----------------------------------------------------------
    def bind(self, unit_id: str, slot: str, *, port: str | None = None,
             chip: str | None = None, firmware: str | None = None,
             fingerprint: str | None = None) -> Binding:
        """Record that ``unit_id`` is the unit wearing ``slot``.

        An id belongs to exactly one sticker and a sticker to exactly one id, so both
        sides are cleared of stale claims first: rebinding a slot to a different unit
        must not leave the old id claiming that slot as well.
        """
        if slot not in SLOTS:
            raise ValueError(f"{slot!r} is not one of {', '.join(SLOTS)}")
        if not unit_id or unit_id.strip() in ("", "-"):
            raise ValueError("cannot bind a unit that reports no unit id")
        # A unit keeps its name when it is re-bound to the sticker it already wore.
        kept = next((b.name for b in self.units.values() if b.unit_id == unit_id), None)
        self.units.pop(slot, None)
        for other, b in list(self.units.items()):
            if b.unit_id == unit_id:
                del self.units[other]
        self.units[slot] = Binding(slot=slot, unit_id=unit_id, port=port, chip=chip,
                                   firmware=firmware, bound_at=time.time(),
                                   fingerprint=fingerprint, name=kept)
        return self.units[slot]

    def unbind(self, slot: str) -> None:
        self.units.pop(slot, None)


# --------------------------------------------------------------------------- storage

#: Held across every load-modify-save of the registry file (audit CONC-01 / R6): a rename
#: during a write's re-learn would otherwise lose one of the two updates. Re-entrant so a
#: caller may hold it across calls that take it again. Never held while waiting on a device.
_io_lock = threading.RLock()


def load(path: Path | None = None) -> Registry:
    """Read the registry. A missing or damaged file yields an empty one, never an error.

    A corrupt store must not stop the panel showing the units that are present. The
    worst case is re-binding, which is cheap and explicit.
    """
    reg = Registry()
    try:
        data = json.loads((path or registry_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return reg
    raw = data.get("units") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return reg
    for slot, rec in raw.items():
        if slot not in SLOTS or not isinstance(rec, dict):
            continue
        uid = rec.get("unit_id")
        # A provisional binding stores no id. Anything else that is not a string is a
        # damaged record and is dropped rather than half-loaded.
        if uid == "":
            uid = ""
        elif not isinstance(uid, str) or not uid or uid == "-":
            if rec.get("fingerprint") is None:
                continue
            uid = ""
        reg.units[slot] = Binding(
            slot=slot, unit_id=uid, port=rec.get("port"), chip=rec.get("chip"),
            firmware=rec.get("firmware"), bound_at=rec.get("bound_at"),
            fingerprint=rec.get("fingerprint"),
            name=rec.get("name") if isinstance(rec.get("name"), str) else None,
            setup_pending=_clean_pending(rec.get("setup_pending")),
            devnum=rec.get("devnum") if isinstance(rec.get("devnum"), str) else None,
        )
    return reg


def _clean_pending(raw) -> dict | None:
    """A stored setup flag, or None. A damaged one is dropped, never half-kept: a missing
    flag shows the unit as it is, so a bad record must not invent a pending setup."""
    if not isinstance(raw, dict):
        return None
    package, sha, since = raw.get("package"), raw.get("sha256"), raw.get("since")
    if not isinstance(package, str) or not package or not isinstance(sha, str):
        return None
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        return None
    return {"package": package, "sha256": sha, "since": float(since)}


#: Longest name the panel can show on a card, and the characters it allows.
NAME_MAX = 12
_NAME_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .-_+")


def clean_name(raw: str) -> str:
    """A display name: trimmed, upper-cased, at most NAME_MAX of letters, digits and
    ``space . - _ +``. Raises ValueError otherwise. Empty means "no name"."""
    name = " ".join((raw or "").split()).upper()
    if len(name) > NAME_MAX:
        raise ValueError(f"a name is at most {NAME_MAX} characters")
    if any(c not in _NAME_OK for c in name):
        raise ValueError("a name may use letters, digits, space and . - _ +")
    return name


def rename(unit_id: str, raw: str) -> str:
    """Name the unit with this flash id ("" clears it). Only a registered unit can be
    named: the name lives on its sticker binding. Returns the stored name."""
    name = clean_name(raw)
    with _io_lock:
        reg = load()
        b = reg.binding_for_id(unit_id)
        if b is None:
            raise KeyError("that unit is not registered")
        b.name = name or None
        save(reg)
    return name


def set_setup_pending(unit_id: str, package: str, sha256: str) -> bool:
    """Record that this unit's last write was a two-step firmware. False if no sticker owns
    the flash id (nothing to attach the flag to)."""
    with _io_lock:
        reg = load()
        b = reg.binding_for_id(unit_id)
        if b is None:
            return False
        b.setup_pending = {"package": package, "sha256": sha256, "since": time.time()}
        save(reg)
    return True


def clear_setup_pending(unit_id: str) -> dict | None:
    """Clear the flag and return what it held, or None if there was nothing to clear."""
    with _io_lock:
        reg = load()
        b = reg.binding_for_id(unit_id)
        if b is None or b.setup_pending is None:
            return None
        cleared, b.setup_pending = b.setup_pending, None
        save(reg)
    return cleared


def relearn_after_flash(unit_id: str, port: str, firmware: str | None = None) -> str:
    """A flash changes ``bcdDevice`` (measured 2026-11-22: 0329 -> 0322 on unit A), so the
    running fingerprint the registry holds goes stale and the unit shows as not connected.
    Re-read it from the port the unit came back on and store it against the unit's id.

    Returns "ok", "unbound" (no sticker owns this id: nothing to update), "not-running"
    (nothing on that port yet) or "shared" (stored, but another unit wears the same
    fingerprint, so the two are told apart only by port -- see ``_match_running``).
    """
    run = next((r for r in running() if r.port == port), None)   # a bus read: outside the lock
    with _io_lock:
        reg = load()
        b = reg.binding_for_id(unit_id)
        if b is None:
            return "unbound"
        if run is None or not run.fingerprint:
            return "not-running"
        shared = any(o.fingerprint == run.fingerprint and o.slot != b.slot
                     for o in reg.units.values())
        b.fingerprint = run.fingerprint
        b.devnum = run.devnum
        b.port = port
        if firmware:
            b.firmware = firmware
        save(reg)
    # Shared fingerprints are resolved by port in ``_match_running``; say so honestly.
    return "shared" if shared else "ok"


def save(reg: Registry, path: Path | None = None) -> None:
    """Write atomically, so a power cut cannot leave a half-written store."""
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "units": {
            slot: {"unit_id": b.unit_id, "port": b.port, "chip": b.chip,
                   "firmware": b.firmware, "bound_at": b.bound_at,
                   "fingerprint": b.fingerprint, "name": b.name,
                   "setup_pending": b.setup_pending, "devnum": b.devnum}
            for slot, b in sorted(reg.units.items())
        },
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


# --------------------------------------------------------------------------- live view

@dataclass
class LiveUnit:
    """One unit the Pi can see, or one it knows about but cannot see right now."""

    unit_id: str
    port: str | None = None
    device: str | None = None  # /dev/sgN while in download mode
    present: bool = False
    chip: str | None = None
    firmware: str | None = None
    slot: str | None = None
    error: str | None = None
    #: This row is labelled by a *hint* (a running fingerprint), not by a flash id.
    provisional: bool = False
    #: The unit is powered on and running firmware, matched by fingerprint.
    running: bool = False
    #: How a *running* unit got its sticker when that was not certain: "port" means two
    #: units looked identical and the last-seen USB port broke the tie (audit UX-05).
    hint: str | None = None
    #: The operator's short name for it, from its binding.
    name: str | None = None
    #: The unfinished two-step setup on this unit, from its binding (see Binding).
    setup_pending: dict | None = None

    @property
    def label(self) -> str:
        """The sticker, or "?" when this unit wears none we have recorded."""
        return SLOT_LABELS.get(self.slot, None) or "?"

    @property
    def in_download_mode(self) -> bool:
        return self.present and self.device is not None


def port_of_sg(node: str) -> str | None:
    """The USB port path for a ``sgN`` device, from its sysfs chain.

    ``/sys/class/scsi_generic/sg0`` resolves through ``host0/target0:0:0/...`` up to
    ``.../usb1/1-1/1-1.2/1-1.2:1.0``; the device node is the last component *before* the
    interface. Purely structural -- no /dev/sgN number is ever read as an identity.
    """
    name = os.path.basename(node)
    if not name.startswith("sg"):
        return None
    real = os.path.realpath(f"{_SCSI_GENERIC}/{name}")
    parts = [p for p in Path(real).parts if p]
    # Walk from the SCSI node back up towards the root, stopping at the usbN hub. The
    # first "<port>:<cfg>" interface we meet names the device we want.
    for part in reversed(parts):
        if ":" not in part:
            continue
        device = part.split(":", 1)[0]
        # Only a real port path qualifies: "1-1.2", not "usb1" or "1-0".
        if "-" in device and os.path.exists(os.path.join(_USB_DEV_ROOT, device)):
            return device
    return None


def _download_devices() -> list[tuple[str, str | None]]:
    """(device node, USB port) for every unit in download mode.

    Found structurally through sysfs, the same way ``guided`` finds them, so a unit on a
    port no udev rule knows about is still listed with its flash id (audit ARCH-02). The
    older ``/dev/fm1-*`` symlinks are consulted too, only to catch a device sysfs could not
    place; the two are merged by real device node."""
    from . import softkey
    from .units import enumerate_units

    found: dict[str, str | None] = {}
    for path in sorted(Path(_SCSI_GENERIC).glob("sg*")) if Path(_SCSI_GENERIC).is_dir() else []:
        node = "/dev/" + path.name
        port = port_of_sg(node)
        if port and softkey.port_ids(port) in softkey.DOWNLOAD_IDS:
            found[node] = port
    for unit in enumerate_units():
        found.setdefault(os.path.realpath(unit.device), port_of_sg(unit.device))
    return list(found.items())


# Identity of each download-mode device, read from flash once and reused. The touch UI asks
# for the fleet every 2 s, and each ask used to run `m-flasher info` (a whole 1 MiB read) on
# every download-mode unit. A device's identity cannot change while it stays the same
# enumeration, so it is keyed by (node, port, USB device number): the kernel gives a unit a
# new device number on every connect, so a different unit or a restart is always re-read.
# Only successful reads are kept; an error is retried on the next scan.
_ID_CACHE: dict[tuple, "mflasher.Identity"] = {}
_ID_LOCK = threading.Lock()


def _id_key(device: str, port: str | None) -> tuple:
    devnum = None
    if port:
        try:
            devnum = (Path(_USB_DEV_ROOT) / port / "devnum").read_text().strip()
        except OSError:
            devnum = None
    return (device, port, devnum)


def _cached_info(device: str, key: tuple):
    """``m-flasher info`` for a device, read at most once per enumeration. Raises
    ``MflasherError`` (and caches nothing) when the read fails."""
    with _ID_LOCK:
        hit = _ID_CACHE.get(key)
    if hit is not None:
        return hit
    ident = mflasher.info(device)
    with _ID_LOCK:
        _ID_CACHE[key] = ident
    return ident


def forget_identities() -> None:
    """Drop every cached identity. Called after a flash or restart, which changes what the
    unit reports even though it stays on the same USB port."""
    with _ID_LOCK:
        _ID_CACHE.clear()


def scan() -> list[LiveUnit]:
    """Every unit currently in download mode, with its id read from flash.

    Running units cannot be enumerated by identity at all (see the module docstring), so
    they are deliberately absent here rather than guessed at. ``known()`` adds the
    registered units that are not visible, so the panel can still show the whole fleet.
    """
    reg = load()
    out: list[LiveUnit] = []
    live: set[tuple] = set()
    for device, port in _download_devices():
        node = os.path.basename(device)
        key = _id_key(device, port)
        live.add(key)
        lu = LiveUnit(unit_id="", port=port, device=device, present=True)
        try:
            ident = _cached_info(device, key)
        except mflasher.MflasherError as exc:
            lu.error = str(exc)
            lu.unit_id = f"device:{node}"
            out.append(lu)
            continue
        lu.chip = ident.flash_chip
        lu.firmware = ident.firmware
        if ident.has_unit_id and ident.unit_id:
            lu.unit_id = ident.unit_id
            lu.slot = reg.slot_for_id(ident.unit_id)
            bound = reg.binding_for_id(ident.unit_id)
            lu.name = bound.name if bound else None
        else:
            # A flash with no factory id cannot be told apart from its neighbour, and
            # inventing an identity here is how the wrong unit gets flashed.
            lu.unit_id = f"device:{node}"
            lu.error = "this flash reports no unit id, so it cannot be told apart"
        out.append(lu)
    # Forget devices that have gone, so the cache cannot grow without limit.
    with _ID_LOCK:
        for stale in [k for k in _ID_CACHE if k not in live]:
            del _ID_CACHE[stale]
    return out


def known() -> list[LiveUnit]:
    """The whole fleet as the panel should show it.

    Three kinds of row, and the panel is expected to distinguish them:

    * a unit in download mode -- identity read from flash, trustworthy;
    * a unit matched to a sticker by its running fingerprint -- labelled, but a *hint*;
    * a registered unit not visible at all -- may be running, off, or unplugged.
    """
    reg = load()
    present = scan()
    seen = {u.unit_id for u in present}
    out = list(present)

    # Labels for units we can only see running, matched by fingerprint.
    #
    # A binding whose flash id HAS been read once is already proven; the fingerprint is
    # then just how we recognise it while it runs, and the row is a confirmed sticker,
    # not a guess. Only a binding with no flash id (taught purely by power-on) stays
    # provisional -- hence provisional=b.provisional rather than a flat True.
    runs = running()
    paired = _match_running(reg, runs, seen)
    for run, b in paired:
        out.append(LiveUnit(unit_id=b.unit_id or f"port:{run.port}", slot=b.slot,
                            port=run.port, present=False, chip=b.chip,
                            firmware=b.firmware, provisional=b.provisional,
                            running=True, name=b.name, setup_pending=b.setup_pending,
                            hint="port" if _unproven_port_pairing(reg, runs, run, b) else None))
    # A running unit nothing could be paired with is still a unit on the bus: show it as
    # "?" rather than leave it out of the count (audit REG-01). Its id is a placeholder
    # that can never reach a write (guided.looks_like_flash_id refuses it).
    paired_runs = {id(r) for r, _ in paired}
    for run in runs:
        if id(run) not in paired_runs:
            out.append(LiveUnit(unit_id=f"port:{run.port}", port=run.port, running=True,
                                firmware=run.product))

    for slot in SLOTS:
        b = reg.units.get(slot)
        if b is None:
            continue
        if b.unit_id and b.unit_id in seen:
            continue
        if any(u.slot == slot for u in out):
            continue
        out.append(LiveUnit(unit_id=b.unit_id or f"slot:{slot}", slot=slot, port=b.port,
                            chip=b.chip, firmware=b.firmware, present=False,
                            provisional=b.provisional, name=b.name,
                            setup_pending=b.setup_pending))
    # Sort by sticker so rows stay still and in A-B-C-D order; unbound units go last.
    out.sort(key=lambda u: (SLOTS.index(u.slot) if u.slot in SLOTS else len(SLOTS),
                            u.unit_id))
    return out


def _match_running(reg: Registry, runs: list["RunningUnit"], seen: set[str]):
    """Pair running units with bindings, one-to-one.

    ``bcdDevice`` is set by the *firmware*, not the chip (measured 2026-11-22: two units
    flashed with ChoralRoot both report 0322), so after a flash two units can share a
    fingerprint. Matching every unit to the first binding with that fingerprint showed
    one sticker twice and left the other unit "disconnected". Rules:

    * a fingerprint worn by exactly one binding and one running unit is a match;
    * when it is shared, the unit's last-seen PORT breaks the tie -- weak (ports are
      random between rig setups), but it only ever chooses between units that already
      look identical, and it never leaves one sticker on two units;
    * whatever still cannot be paired is left unlabelled rather than guessed.
    """
    used: set[str] = set()
    pairs: list[tuple] = []
    # A running unit whose USB serial equals a binding's flash id is that unit, whatever its
    # fingerprint says. The stored fingerprint goes stale when the firmware changes (unit C:
    # stored 0420, JIANT reports 0100), which showed C as "not running" plus a phantom "?".
    by_id = {b.unit_id: b for b in reg.units.values() if b.unit_id and b.unit_id not in seen}
    rest = []
    for r in runs:
        b = by_id.get(r.serial or "")
        if b is not None and b.slot not in used:
            used.add(b.slot)
            pairs.append((r, b))
        else:
            rest.append(r)
    runs = rest
    bindings = [b for b in reg.units.values()
                if b.fingerprint and b.slot not in used
                and not (b.unit_id and b.unit_id in seen)]
    runs = [r for r in runs if r.fingerprint]
    for r in runs:
        cands = [b for b in bindings if b.fingerprint == r.fingerprint]
        same_fp_runs = [x for x in runs if x.fingerprint == r.fingerprint]
        if len(cands) == 1 and len(same_fp_runs) == 1 and cands[0].slot not in used:
            used.add(cands[0].slot)
            pairs.append((r, cands[0]))
    matched = {id(r) for r, _ in pairs}
    for r in runs:
        if id(r) in matched:
            continue
        cands = [b for b in bindings
                 if b.fingerprint == r.fingerprint and b.slot not in used and b.port == r.port]
        if len(cands) == 1:
            used.add(cands[0].slot)
            pairs.append((r, cands[0]))
    return pairs


def running_slots() -> dict[str, str]:
    """``{sticker slot: USB port}`` for the powered-on units the registry can name.

    Reads sysfs only. It never runs ``m-flasher``, so it is safe to call every few seconds
    from a long-running service, even while another unit is being flashed (``known()`` asks
    each download-mode unit for its id, which a service must not do on a timer).
    """
    reg = load()
    return {b.slot: r.port for r, b in _match_running(reg, running(), set())}


def live_ports() -> dict[str, str]:
    """``{sticker slot: USB port}`` for every unit the registry can place right now.

    Running units are placed by fingerprint, from sysfs (``running_slots``). A unit already
    in download mode is placed by its flash id, which costs one ``m-flasher`` read per such
    unit, so this is for one-shot commands (``download``), not for timers. A saved port map
    is never consulted: ports move when cables move, so a map goes stale (it did on
    2026-11-25 after the move to the USB hub).
    """
    ports = running_slots()
    for u in scan():
        if u.slot and u.port and u.slot not in ports:
            ports[u.slot] = u.port
    return ports


def _tie_broken_by_port(reg: Registry, runs: list["RunningUnit"], run: "RunningUnit") -> bool:
    """True when this run shares its fingerprint with another, so only its last-seen port
    could have chosen the sticker."""
    return sum(1 for x in runs if x.fingerprint == run.fingerprint) > 1


def _unproven_port_pairing(reg: Registry, runs: list["RunningUnit"], run: "RunningUnit",
                           b: Binding) -> bool:
    """A sticker chosen only by last-seen port, where nothing proves the unit has not moved.

    Proof is the USB device number recorded when the flash id was last read on this port:
    it changes whenever the unit re-enumerates (power cycle, replug), so an equal number
    means the same unit has sat on that port since. Without it, two look-alike units that
    swap jacks would swap letters silently."""
    if not _tie_broken_by_port(reg, runs, run):
        return False
    return not (b.devnum and b.devnum == run.devnum)


def unproven_runs(reg: Registry, runs: list["RunningUnit"], paired) -> list["RunningUnit"]:
    """Running units whose letter rests on an unproven port pairing (see above)."""
    return [r for r, b in paired if _unproven_port_pairing(reg, runs, r, b)]


def unassigned(present: list[LiveUnit] | None = None) -> list[LiveUnit]:
    """Units in download mode that no sticker claims."""
    units = present if present is not None else scan()
    return [u for u in units if u.slot is None]


# --------------------------------------------------------------- running units

@dataclass
class RunningUnit:
    """A powered-on unit presenting normal firmware.

    It has no readable id, so this is *description*, not identity: what it looks like on
    USB and, via ``fingerprint``, a hint at which physical unit it might be.
    """

    port: str
    vendor_product: str  # "1209:0001"
    product: str | None  # "Felucca", "M-VAVE FM-1"
    fingerprint: str | None  # bcdDevice, e.g. "0328"
    slot_hint: str | None = None  # sticker this fingerprint was bound to, if any
    #: USB serial string. JIANT reports the unit's flash id here (bench 2026-11-25, unit C:
    #: serial == flash id). Other firmwares may report nothing or something else, so it is
    #: only ever used to *match* a binding whose flash id is equal, never as an identity.
    serial: str | None = None
    #: USB device number: new on every enumeration, so it proves "same connection as before".
    devnum: str | None = None

    @property
    def has_midi(self) -> bool:
        return self.vendor_product in RUNNING_MIDI_IDS


#: USB ids a unit presents while running firmware that exposes MIDI. Only these can
#: receive the soft key; a unit without MIDI must be power-cycled instead.
RUNNING_MIDI_IDS = {"1209:0001", "4c4a:c755"}

#: VIDs that are FM-1s. Filtering on the vendor keeps hubs and the Pi's own NIC out.
FM1_VENDORS = {"1209", "4d4a", "4c4a"}

#: ...but 1209 is pid.codes, shared by every open-hardware gadget (audit REG-02), so a
#: device counts only if its vid:pid is a pair an FM-1 is known to present, or its USB
#: product string says it is one.
FM1_PRODUCTS = ("felucca", "m-vave fm-1", "fm-1")


def _is_fm1(vid: str, pid: str, product: str | None) -> bool:
    from . import softkey
    if f"{vid}:{pid}" in softkey.RUNNING_IDS | softkey.DOWNLOAD_IDS:
        return True
    return vid in FM1_VENDORS and any(k in (product or "").lower() for k in FM1_PRODUCTS)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def running() -> list[RunningUnit]:
    """Every powered-on FM-1 on the bus, newest information read from sysfs each call.

    Deliberately structural: it walks the USB device tree rather than trusting a saved
    port map, because ports are random on this rig.
    """
    reg = load()
    hints = {b.fingerprint: s for s, b in reg.units.items() if b.fingerprint}
    out: list[RunningUnit] = []
    root = Path(_USB_DEV_ROOT)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for dev in entries:
        vid = _read(str(dev / "idVendor"))
        pid = _read(str(dev / "idProduct"))
        if not vid or not pid or vid not in FM1_VENDORS:
            continue
        product = _read(str(dev / "product"))
        if not _is_fm1(vid, pid, product):
            continue
        from . import softkey
        if f"{vid}:{pid}" in softkey.DOWNLOAD_IDS:
            # The ROM loader is a unit in download mode, which scan() already reports with
            # its flash id. Listing it here too showed one unit twice -- as itself and as a
            # phantom "?" (found by replaying the bench capture, 2026-11-25).
            continue
        fp = _read(str(dev / "bcdDevice"))
        out.append(RunningUnit(
            port=dev.name,
            vendor_product=f"{vid}:{pid}",
            product=product,
            fingerprint=fp,
            slot_hint=hints.get(fp or ""),
            serial=_read(str(dev / "serial")),
            devnum=_read(str(dev / "devnum")),
        ))
    return out

