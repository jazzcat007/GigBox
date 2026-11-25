"""Registration: learn which physically-labelled unit is which, by watching it arrive.

Why this exists
---------------
Two facts, measured on this fleet, make "just ask the Pi" impossible:

* A **running** unit exposes no per-unit id: no USB serial, and three of the four present
  identically as ``1209:0001``.
* Its **flash id** -- the one durable identity -- can only be read in download mode.

So identity cannot be discovered. It has to be *taught*, once per unit, at a moment when
the operator can see which physical unit they are teaching. That moment is power-on: you
switch on the unit wearing sticker B, and the Pi sees a unit appear.

The one thing that proves *which* appearance is the operator's
-------------------------------------------------------------
An arrival on its own proves "a unit appeared", not "unit B appeared". So this module
never binds on arrival alone. It:

1. snapshots the bus,
2. asks the operator to power on exactly one unit,
3. waits for exactly ONE new FM-1 to appear,
4. if anything else changes at the same time, refuses and says what it saw.

Step 3 is what makes the power-on-one-at-a-time rule load-bearing rather than cosmetic:
if two units arrive, or one arrives while another leaves, the arrival cannot be
attributed and the registration is abandoned. There is no "probably it was that one".

What is recorded, and what is merely a hint
-------------------------------------------
The arrival gives a **port** and a ``bcdDevice`` fingerprint -- both weak, both subject to
change. Neither is allowed to authorise a write. They are recorded so the panel can *show*
a running unit's likely sticker; the binding becomes authoritative only when the flash id
is read in download mode and matches. See ``registry.py``.

The other way in: confirm from download mode (the panel's REGISTER)
-------------------------------------------------------------------
Power-on attribution is not the only way a binding is taught. A unit sitting in download
mode lets the Pi read its flash id directly -- the one trustworthy identity -- so
``scan_download_mode`` + ``bind_from_scan`` serve the panel's REGISTER flow: the server
reads every download-mode unit's id itself, and a binding is minted only from an id THIS
call has just read. Nothing else (a port, a USB order, a client-sent id) can ever create
one, and a bind carries a ``confirm`` that must equal the id being bound, exactly like
every deliberate write. A bind records metadata only; it never touches the write path.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field

from . import guided, mflasher, registry, softkey
from .units import SLOT_LABELS, SLOTS


class AttributionError(Exception):
    """The change on USB cannot be attributed to a single unit, so nothing is bound."""


@dataclass
class Arrival:
    """One unit's appearance, as far as it can be established."""

    port: str
    vendor_product: str
    product: str | None
    fingerprint: str | None
    has_midi: bool


@dataclass
class WatchResult:
    #: Units present before the operator acted, keyed by port.
    before: dict[str, registry.RunningUnit] = field(default_factory=dict)
    #: Units present after.
    after: dict[str, registry.RunningUnit] = field(default_factory=dict)
    #: Ports that appeared.
    arrived: list[Arrival] = field(default_factory=list)
    #: Ports that disappeared during the watch.
    departed: list[str] = field(default_factory=list)

    @property
    def single_arrival(self) -> Arrival | None:
        """The one arrival, or None when zero or several arrived."""
        return self.arrived[0] if len(self.arrived) == 1 else None


def snapshot() -> dict[str, registry.RunningUnit]:
    """Current running units, keyed by port. Cheap enough to poll."""
    return {u.port: u for u in registry.running()}


def diff(before: dict[str, registry.RunningUnit],
         after: dict[str, registry.RunningUnit]) -> WatchResult:
    """Compare two snapshots into arrivals and departures."""
    arrived = [
        Arrival(port=p, vendor_product=u.vendor_product, product=u.product,
                fingerprint=u.fingerprint, has_midi=u.has_midi)
        for p, u in sorted(after.items()) if p not in before
    ]
    departed = sorted(p for p in before if p not in after)
    return WatchResult(before=before, after=after, arrived=arrived, departed=departed)


def wait_for_arrival(before: dict[str, registry.RunningUnit], *,
                     timeout: float = 120.0, poll: float = 0.4,
                     settle: float = 1.5) -> WatchResult:
    """Block until exactly one new FM-1 appears, then confirm nothing else changed.

    ``settle`` is a second look after the first change: USB enumeration is not atomic, so
    a unit can briefly appear as a device and only later gain its interfaces. Re-reading
    after a short pause catches a second unit joining late, which would otherwise be
    mistaken for a clean single arrival.

    Raises :class:`AttributionError` when the change cannot be pinned to one unit, or
    :class:`TimeoutError` when nothing appears in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = snapshot()
        if set(now) - set(before):
            time.sleep(settle)
            settled = snapshot()
            result = diff(before, settled)
            if len(result.arrived) > 1:
                ports = ", ".join(a.port for a in result.arrived)
                raise AttributionError(
                    f"{len(result.arrived)} units appeared at once ({ports}). "
                    "Power on one unit at a time so the Pi can tell which is which."
                )
            if result.departed:
                raise AttributionError(
                    "a unit disappeared while one arrived "
                    f"({', '.join(result.departed)}), so the arrival cannot be "
                    "attributed. Check the cables and try again."
                )
            return result
        time.sleep(poll)
    raise TimeoutError(f"no unit appeared within {timeout:g}s")


# --------------------------------------------------------------------------- binding

def register(result: WatchResult, slot: str) -> registry.Binding:
    """Bind the single arrived unit to ``slot`` on a *provisional* basis.

    Only the weak signals are available at power-on -- a port and a fingerprint -- so
    this records a binding whose flash id is not yet known. It is provisional: the panel
    shows the sticker as a guess until a download-mode read confirms the id.

    Deliberately separate from :meth:`registry.Registry.bind`, which requires a real id.
    """
    if slot not in SLOTS:
        raise ValueError(f"{slot!r} is not one of {', '.join(SLOTS)}")
    arrival = result.single_arrival
    if arrival is None:
        raise AttributionError("registration needs exactly one arrival")
    if not arrival.fingerprint:
        raise AttributionError(
            "this unit reported no bcdDevice, so there is nothing to recognise it by "
            "next time; label it from download mode instead"
        )
    # A provisional binding has no flash id yet. Record it under the sticker with a
    # marker id so `known()` can show it as unconfirmed rather than pretending.
    with registry._io_lock:
        reg = registry.load()
        reg.units[slot] = registry.Binding(
            slot=slot, unit_id="", port=arrival.port, fingerprint=arrival.fingerprint,
            bound_at=time.time(),
        )
        registry.save(reg)
    return reg.units[slot]


def confirm(result: registry.LiveUnit, slot: str) -> registry.Binding:
    """Upgrade a provisional binding to a real one once the flash id is readable."""
    if not result.unit_id or result.unit_id.startswith("device:"):
        raise AttributionError("this unit reports no flash id, so it cannot be confirmed")
    fp = fingerprint_for_port(result.port)          # a bus read: outside the registry lock
    with registry._io_lock:
        reg = registry.load()
        b = reg.bind(result.unit_id, slot, port=result.port, chip=result.chip,
                     firmware=result.firmware, fingerprint=fp)
        # Saving is the whole point: a confirmation that is not written down is lost at the
        # next reboot, which is exactly the failure this module exists to prevent.
        registry.save(reg)
    return b


def fingerprint_for_port(port: str | None) -> str | None:
    """The running ``bcdDevice`` on a port, or None when no *running* unit is there.

    This must never borrow a value from a port that is in download mode. Measured on the
    hardware 2026-11-22: a download-mode unit reports ``bcdDevice=0300`` (the bootloader's
    own version), identical for every unit. Recording that would give the whole fleet one
    fingerprint, which is worse than recording none -- it would make every unit look like
    every other one.

    ``registry.running()`` already filters to FM-1 vendors, but a download-mode unit is
    ALSO an FM-1 vendor id, so the filter here is on the vendor:product pair.
    """
    if not port:
        return None
    for u in registry.running():
        if u.port != port:
            continue
        # 4c4a:8057 is the ROM bootloader. Its bcdDevice is not a unit fingerprint.
        if u.vendor_product in softkey.DOWNLOAD_IDS:
            return None
        return u.fingerprint
    return None


def describe_arrival(result: WatchResult) -> str:
    """A one-line, operator-facing summary of what was seen."""
    a = result.single_arrival
    if a is None:
        return f"{len(result.arrived)} units appeared"
    kind = "Felucca-style MIDI" if a.has_midi else "no MIDI"
    return f"unit on port {a.port} ({a.vendor_product}, {kind}, fp {a.fingerprint})"


def guidance() -> list[str]:
    """The steps to show an operator, in order."""
    return [
        f"Power OFF every FM-1. The Pi should list no units.",
        f"Choose which sticker this unit wears ({', '.join(SLOT_LABELS[s] for s in SLOTS)}).",
        "Power ON ONLY that one unit, and wait.",
        "The Pi watches for exactly one unit to appear.",
        "If two appear at once, nothing is recorded and you are asked again.",
    ]


# ------------------------------------------------------------------ bind from download mode
#
# The panel's REGISTER flow (roadmap B4, audit finding FEAT-01). Everything here reads
# identity and records metadata; nothing here writes to a unit.

#: Operator-facing reason REGISTER cannot proceed while a flash runs. The device reads
#: below must not race a write (audit SAFE-03), so the flash lock is taken non-blocking
#: and the flow refuses rather than queues -- the same rule restart_unit follows.
_BUSY = "another flash is already running; try again when it finishes"

_NO_UNIT = ("no unit is in download mode — put the new unit into download mode and "
            "scan again")

_ALREADY = ("every unit in download mode already has a sticker; "
            "use rename to change a unit's name")

_NO_FREE = ("every letter (A-D) already has a unit — moving a binding is not possible "
            "from the panel yet")


def free_letters(reg: registry.Registry | None = None) -> list[str]:
    """Letters the REGISTER flow may still hand out, in A-D order.

    A letter is free when no CONFIRMED binding wears it. A provisional binding (taught
    by power-on, flash id still unknown) does not hold a letter hostage: confirming a
    real id onto that letter is exactly the upgrade a provisional binding waits for,
    and :func:`registration.confirm` replaces it through ``Registry.bind``.
    """
    reg = reg if reg is not None else registry.load()
    return [s for s in SLOTS
            if not (reg.units.get(s) is not None and not reg.units[s].provisional)]


def _download_units() -> list[registry.LiveUnit]:
    """Units in download mode right now, identity read fresh from flash by THIS scan.

    Found structurally through sysfs (``guided.download_devices``), like every write is,
    so the register flow does not inherit the udev port map's limitation (audit ARCH-02;
    that cleanup is roadmap B5 and this module does not wait for it). One
    ``m-flasher info`` per device, the same read ``registry.scan`` does. A device that
    cannot be read keeps its row with the error: it is shown, never filtered
    (invariant 5), and it counts as unclaimed -- its identity contributes NOTHING the
    flow could act on, so it must not silently narrow the choice to one unit.
    """
    reg = registry.load()
    out: list[registry.LiveUnit] = []
    for dev in guided.download_devices():
        lu = registry.LiveUnit(unit_id=f"device:{os.path.basename(dev.node)}",
                               port=dev.port, device=dev.node, present=True)
        try:
            ident = mflasher.info(dev.node)
        except mflasher.MflasherError as exc:
            lu.error = str(exc)
            out.append(lu)
            continue
        lu.chip = ident.flash_chip
        lu.firmware = ident.firmware
        if ident.has_unit_id and ident.unit_id and guided.looks_like_flash_id(ident.unit_id):
            lu.unit_id = ident.unit_id
            bound = reg.binding_for_id(ident.unit_id)
            lu.slot = bound.slot if bound else None
            lu.name = bound.name if bound else None
        else:
            lu.error = ("this flash reports no unit id, so it cannot be registered")
        out.append(lu)
    return out


def scan_download_mode(lock=None) -> dict:
    """What the REGISTER view needs to show, from one fresh look at the bus.

    Returns a plain, JSON-ready dict:

    * ``units``   -- every download-mode unit, bound or not, readable or not, each with
      its server-read id, chip, firmware, port and its current sticker (``slot``/``label``).
    * ``unbound`` -- how many wear no sticker at all. A device whose id cannot be read
      counts as unclaimed (see ``_download_units``): it can contribute nothing, and say-
      so beats silently filtering it out of the count.
    * ``free``    -- the letters REGISTER could still hand out (:func:`free_letters`).
    * ``proposed``-- the letter the ONE unbound unit would get, or None. The letter is
      proposed by the server and re-derived again at bind time; the panel never chooses.
    * ``refusal`` -- why REGISTER cannot proceed right now, or None. The bind endpoint
      re-derives all of this and refuses again on its own (a scan is a display, not an
      authority); this dict exists so the panel can state the reason honestly.
    """
    if lock is not None and not lock.acquire(blocking=False):
        return {"units": [], "unbound": 0, "free": _labels(free_letters()),
                "proposed": None, "refusal": _BUSY}
    try:
        units = _download_units()
        reg = registry.load()
        free = free_letters(reg)
        unbound = [u for u in units if u.slot is None]
        refusal = None
        proposed = None
        if not units:
            refusal = _NO_UNIT
        elif len(unbound) > 1:
            refusal = (f"{len(unbound)} units are in download mode without a sticker — "
                       "leave only one connected and scan again")
        elif not unbound:
            refusal = _ALREADY
        elif not guided.looks_like_flash_id(unbound[0].unit_id):
            refusal = "that unit reports no readable unit id, so it cannot be registered"
        elif not free:
            refusal = _NO_FREE
        else:
            slot = free[0]
            proposed = {"slot": slot, "letter": SLOT_LABELS.get(slot, slot)}
        return {
            "units": [{**asdict(u), "label": u.label} for u in units],
            "unbound": len(unbound),
            "free": _labels(free),
            "proposed": proposed,
            "refusal": refusal,
        }
    finally:
        if lock is not None:
            lock.release()


def _labels(slots: list[str]) -> list[str]:
    return [SLOT_LABELS.get(s, s) for s in slots]


def bind_from_scan(unit_id: str, confirm_id: str, *, lock=None) -> registry.Binding:
    """Bind one unit to its letter, from an id the Pi itself reads RIGHT NOW.

    The refusals, in the order they are checked (each names its reason):

    * the confirmation must equal the id being bound -- the same ``confirm == unit_id``
      pattern every deliberate act uses, so the dialog's act names its subject (the
      parameter is ``confirm_id`` so it does not shadow the module's ``confirm()``
      function, which is the machinery that does the binding);
    * the id must look like a real flash id; ``device:``/``port:``/``slot:`` placeholders
      name nothing and are refused, exactly as the write path refuses them;
    * no flash may be running: the reads below touch the bus, the lock is taken
      non-blocking, and REGISTER refuses rather than queues (invariant 7);
    * the same fresh read refuses the two scan states that make a binding impossible or
      ambiguous: an empty bus (no unit is in download mode), and two or more unclaimed
      units -- with the count, never picking one (invariant 5);
    * the id must be read again NOW, from a device currently in download mode. A
      binding is minted only from an id THIS call has just read -- never from the scan
      the view displayed (a unit can leave download mode between the two), never from a
      client-supplied id, a USB port or an order. If the id matches nothing on the bus,
      the unit left download mode and the operator is told to scan again;
    * the id may not already be registered (rename is the existing remedy, and the
      refusal names its letter);
    * a free letter must exist; the binding goes to :func:`free_letters`' first one.

    The binding itself is written by :func:`registration.confirm` -- the existing
    provisional->confirmed machinery -- so a provisional binding taught by power-on is
    upgraded by the same path that creates a fresh one. This records metadata only:
    nothing here calls ``guided.flash`` or any other writer.
    """
    if confirm_id != unit_id:
        raise AttributionError("confirmation does not match the unit being registered")
    if not guided.looks_like_flash_id(unit_id):
        raise AttributionError(
            "this unit has no readable flash id, so it cannot be registered")
    if lock is not None and not lock.acquire(blocking=False):
        raise AttributionError(_BUSY)
    try:
        units = _download_units()
        if not units:
            raise AttributionError(_NO_UNIT)
        unbound = [u for u in units if u.slot is None]
        if len(unbound) > 1:
            raise AttributionError(
                f"{len(unbound)} units are in download mode without a sticker — leave "
                "only one connected and try again")
        live = next((u for u in units if u.unit_id == unit_id), None)
        if live is None:
            raise AttributionError(
                "that unit is not in download mode anymore — it left between the scan "
                "and the confirm. Scan again and confirm once more.")
        reg = registry.load()
        bound = reg.binding_for_id(unit_id)
        if bound is not None:
            raise AttributionError(
                f"that unit is already registered as letter {bound.label} — use rename "
                "to change its name")
        free = free_letters(reg)
        if not free:
            raise AttributionError(_NO_FREE)
        return confirm(live, free[0])
    finally:
        if lock is not None:
            lock.release()
