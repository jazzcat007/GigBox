"""Put ONE named FM-1 into ROM download mode, addressed by physical USB port.

Why this exists rather than just calling ``m-flasher download-mode``
--------------------------------------------------------------------
``m-flasher``'s soft key finds a unit **by name**. Every Felucca-family unit announces
itself as ``Felucca`` and every stock one as ``M-VAVE FM-1``, so with a fleet connected it
cannot tell which is which and refuses:

    Refused: more than one unit on USB MIDI: connect only one

That refusal is correct -- guessing would be worse -- but it makes the tool useless on a
bench where the whole point is that four units stay plugged in. Its documentation also
assumes you re-patch the target unit by hand.

The fix is to address the unit the same way flashing addresses it: **by physical port**.
A running FM-1 that exposes MIDI has exactly one ALSA raw MIDI node, reachable through a
``by-path`` symlink that names the USB port chain (``...usb-0:1.1.2:1.0``). Writing the
soft-key SysEx to that node reaches exactly one jack, so no unit has to be unplugged and
no decision is guessed: if the slot's port is empty, nothing is sent.

Measured on hardware (2026-11-21): sending ``F0 22 24 35 7D F7`` to
``...usb-0:1.1.2:1.0`` moved that port's unit from ``1209:0001 Felucca`` to
``4c4a:8057 WL80UBOOT1.00`` (note **WL80**, a spelling variant of ``WL82``) within about
four seconds, while the other three units stayed connected and running.

An important correction about what "works" means
------------------------------------------------
This helper's *channel* is per-port and therefore unambiguous. But it cannot know which
unit a returned successful SysEx was **acted on** by. The contract m-flasher documents is
that the soft key targets exactly one unit; the hardware appears not to enforce it.

Measured 2026-11-21, with three units running and one MIDI node present: one write to that
node put **all three** MIDI-capable units into download mode at once. A later run reported
success for a stock-V15 unit that exposes no MIDI interface at all -- and it really had
entered download mode, because a second unit that vanished from USB took the V15's port
number.

Later runs (every one since 2026-11-22, including a full four-unit bench on 2026-11-23)
moved only the addressed unit, so the behaviour varies and is not understood. So: **do not
rely on the soft key being pointed.** Treat :func:`soft_key` as "put this unit, and possibly
others, into download mode", never as "and only this one". Consequences that matter:

* Never send it while a unit is mid-write. You cannot scope it.
* After using it, re-read every unit rather than assuming which one moved.
* :func:`wait_for_download_mode` proves the port reached ROM boot; it does **not** prove
  nothing else did.

The safety property that *does* hold is that writes are addressed per slot: a flash goes to
``/dev/fm1-<letter>``, bound by USB port, and that has been verified on hardware.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

#: m-flasher's soft-key SysEx. Writes nothing to flash; a power cycle brings the unit back.
SYSEX_SOFT_KEY = bytes([0xF0, 0x22, 0x24, 0x35, 0x7D, 0xF7])

#: Where ALSA keeps the raw MIDI and by-path names.
SND_DIR = Path("/dev/snd")

#: Where sysfs exposes USB devices. Overridden in tests.
USB_SYSFS = "/sys/bus/usb/devices"

#: USB ids a unit may present while *running* firmware. Download mode is 4c4a:8057.
RUNNING_IDS = {"1209:0001", "4d4a:4155", "4c4a:c755"}
DOWNLOAD_IDS = {"4c4a:8057"}


class NoSuchPort(Exception):
    """The slot's port is not present at all (no cable, or nothing powered)."""


class NoMidiInterface(Exception):
    """The port is occupied but the kernel exposes no raw MIDI node for it.

    Raised when :func:`midi_node_for_port` finds nothing to write to. CORRECTION
    (measured 2026-11-22): this does *not* reliably mean the unit cannot take the soft
    key. Unit C presents as ``4d4a:4155`` with no MIDI node, and a write to its port still
    moved it into download mode -- because the soft key is delivered over this USB-C
    link, which carries MIDI for these units regardless of what the host enumerates as a
    sound card.

    So the caller should not treat this as "power-cycle it"; it means "the host found no
    node to write to", after which a power cycle is the fallback.
    """


@dataclass
class SoftKeyResult:
    port: str
    midi_node: str
    sent_at: float


def _by_path_names() -> list[str]:
    d = SND_DIR / "by-path"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir())


def midi_node_for_port(port: str) -> Path | None:
    """Resolve a USB port path (``1-1.1.2``) to its raw MIDI node, if it has one.

    ALSA's by-path name embeds the kernel path *without* the leading bus number and with
    the interface appended, e.g. port ``1-1.1.2`` -> ``...usb-0:1.1.2:1.0``. The ``usbv2``
    aliases describe the same device and are ignored so a port cannot resolve twice.
    """
    if not port or "-" not in port or not port[0].isdigit():
        # Reject "" and "1" as malformed, and also "fm1-a", which would otherwise
        # "parse" as bus "fm1" and then silently resolve to nothing.
        raise ValueError(f"not a USB port path: {port!r}")
    # Compare by CARD NUMBER, not by resolved path: macOS resolves /var -> /private/var,
    # so path comparison gives false mismatches; the number is what actually matters.
    real_card = _card_number_of_port(port)

    # Path 1: sysfs already tells us this port's card. Then the by-path naming is
    # irrelevant -- go straight to the node. (ALSA names the chain WITHOUT the leading bus
    # number, "usb-0:1.1.3", not "usb-1:1.1.3"; matching could therefore miss entirely.)
    if real_card is not None:
        node = SND_DIR / f"midiC{real_card}D0"
        return node if node.exists() else None

    # Path 2: no sysfs card (port may be mid-enumeration). Fall back to the by-path name,
    # which is still a sound match because it embeds the port chain.
    bus, _, rest = port.partition("-")
    suffix = f"usb-{bus}:{rest}:1.0"
    for name in _by_path_names():
        if "usbv2" in name:
            continue
        if not name.endswith(suffix):
            continue
        ctl = SND_DIR / "by-path" / name
        try:
            card = os.path.basename(os.readlink(ctl))
        except OSError:
            continue
        if not card.startswith("controlC"):
            continue
        node = SND_DIR / f"midiC{card[len('controlC'):]}D0"
        if node.exists():
            return node
    return None


def _card_number_of_port(port: str) -> int | None:
    """The ALSA card number the kernel assigned to this USB port, per sysfs.

    This is the authority. ``/dev/snd/by-path`` names are *not*: after an unplug they can
    linger and point at a card number that has since been reused by another port.
    """
    d = Path(f"{USB_SYSFS}/{port}")
    try:
        for entry in d.iterdir():
            # Interface entries are named "<port>:<config>.<iface>", e.g. "1-1.1.3:1.0".
            # (An earlier filter excluded anything starting with "1-0", which also threw
            # away every real interface on this bus -- the check is the colon, not a prefix.)
            if ":" not in entry.name or not (entry / "sound").is_dir():
                continue
            for card in (entry / "sound").iterdir():
                if card.name.startswith("card"):
                    return int(card.name[4:])
    except (OSError, ValueError):
        return None
    return None


def port_ids(port: str) -> str | None:
    """``vendor:product`` currently on this port, or None when the port is empty."""
    base = Path(USB_SYSFS)
    dev = base / port
    if not dev.exists():
        return None
    try:
        vendor = (dev / "idVendor").read_text().strip()
        product = (dev / "idProduct").read_text().strip()
    except OSError:
        return None
    return f"{vendor}:{product}"


def soft_key(port: str, *, settle: float = 0.0) -> SoftKeyResult:
    """Send the soft key to the unit on ``port``. Never guesses another unit.

    Raises :class:`NoSuchPort` when the port is empty and
    :class:`NoMidiInterface` when the firmware exposes no MIDI node.
    """
    ids = port_ids(port)
    if ids is None:
        raise NoSuchPort(port)
    node = midi_node_for_port(port)
    if node is None:
        raise NoMidiInterface(
            f"the host exposes no MIDI node for the unit on {port} "
            f"(it presents as {ids}), so there is nothing to write the soft key to; "
            "power-cycle that unit to reach ROM download mode"
        )
    # A running router (the dawless hub) holds the units' MIDI ports open; it is asked to let
    # go first. With no router running this does nothing at all.
    from .router_service import released_for_soft_key
    with released_for_soft_key():
        with open(node, "wb", buffering=0) as fh:
            fh.write(SYSEX_SOFT_KEY)
    if settle:
        time.sleep(settle)
    return SoftKeyResult(port=port, midi_node=str(node), sent_at=time.time())


def wait_for_download_mode(port: str, timeout: float = 20.0,
                           interval: float = 0.5) -> bool:
    """Poll until ``port`` reports a download-mode id, or the timeout expires.

    Deliberately not a sleep-and-hope: the caller gets an exact yes/no about whether the
    unit actually re-entered ROM boot, which is the thing worth knowing before a write.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_ids(port) in DOWNLOAD_IDS:
            return True
        time.sleep(interval)
    return False
