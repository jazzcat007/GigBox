"""The dawless-hub routing core: what a route does to a MIDI message, and the saved patches.

This is the pure part of ``docs/ROADMAP-dawless-hub.md``: no ALSA, no hardware, no flash. It
answers two questions a router service will need, and nothing else:

1. **What does one route do to one message?** :meth:`Route.process` filters, remaps the
   channel, transposes and shapes velocity, and returns the message to send or ``None``.
2. **What is a saved patch?** A named set of routes between *ports*, validated on load and
   written atomically, so a half-written file never becomes a rig.

**Ports are keyed by the registered flash id, never by card number or port name.** The
survey found two units both called "Felucca" and card numbers that change on replug, so the
only stable key is the unit's registry id (``fm1-c``, or the flash id the registry binds).
A patch names ports by that key; resolving a key to a live ALSA client is the router
service's job and is out of scope here.

**Nothing here may hold a unit while it is flashed.** :func:`ports_to_release` takes the
patch and the units about to be flashed and says which routes must be torn down first, so
the flash path can call it and routing never blocks a write.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# ---------------------------------------------------------------------------- messages

NOTE_OFF, NOTE_ON, POLY_AT, CONTROL, PROGRAM, CHAN_AT, PITCH = (
    0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0,
)
SYSEX, CLOCK, START, CONTINUE, STOP = 0xF0, 0xF8, 0xFA, 0xFB, 0xFC

# Kinds a route can block. "realtime" is clock/start/continue/stop (0xF8..0xFF minus
# active sensing and reset, which are always dropped on a filtered route's behalf).
KINDS = ("note", "cc", "program", "pressure", "bend", "sysex", "clock", "transport")
ALL_CHANNELS = tuple(range(16))
_DATA_MAX = 127


def _kind(status: int) -> str | None:
    """The filter kind for a status byte, or None when it is not something a route carries."""
    if status >= 0xF0:
        if status == SYSEX:
            return "sysex"
        if status == CLOCK:
            return "clock"
        if status in (START, CONTINUE, STOP):
            return "transport"
        return None  # active sensing, reset, system common: never forwarded
    hi = status & 0xF0
    return {
        NOTE_OFF: "note", NOTE_ON: "note", POLY_AT: "pressure", CONTROL: "cc",
        PROGRAM: "program", CHAN_AT: "pressure", PITCH: "bend",
    }.get(hi)


class RouteError(ValueError):
    """A route or patch that cannot be used. The message says which field and why."""


def _int_in(name: str, value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise RouteError(f"{name} must be a whole number from {lo} to {hi}, got {value!r}")
    return value


# -------------------------------------------------------------------------------- route

@dataclass(frozen=True)
class Route:
    """One one-way link from a source port to a destination port, with its processing.

    Channels are numbered 1..16 in the file and API (what a musician sees) and 0..15 inside
    the message bytes; the conversion is confined to this class.
    """

    source: str
    dest: str
    # Channels let through, 1..16. Empty means all.
    channels: tuple[int, ...] = ()
    # Kinds blocked, any of KINDS.
    block: tuple[str, ...] = ()
    # Send every channel message out on this channel (1..16), or None to keep each one's own.
    remap_to: int | None = None
    # Move every channel message up or down by this many channels (-15..15), so a block of
    # source channels 5-8 can land on a unit that listens on 1-4 (shift -4). A message that
    # would leave 1..16 is dropped, never wrapped. Not combined with remap_to.
    shift: int = 0
    transpose: int = 0  # semitones, -48..48, notes only
    velocity_scale: float = 1.0  # 0.1..2.0, note-on velocity only
    low_note: int = 0  # note range let through, inclusive
    high_note: int = 127

    def __post_init__(self):
        if not self.source or not self.dest:
            raise RouteError("a route needs a source and a destination port")
        if self.source == self.dest:
            raise RouteError(f"a route from {self.source!r} to itself would feed back")
        for c in self.channels:
            _int_in("channels", c, 1, 16)
        for k in self.block:
            if k not in KINDS:
                raise RouteError(f"unknown kind {k!r} in block; choose from {', '.join(KINDS)}")
        if self.remap_to is not None:
            _int_in("remap_to", self.remap_to, 1, 16)
        _int_in("shift", self.shift, -15, 15)
        if self.shift and self.remap_to is not None:
            raise RouteError("use either remap_to or shift on one route, not both")
        _int_in("transpose", self.transpose, -48, 48)
        if isinstance(self.velocity_scale, bool) or not isinstance(
            self.velocity_scale, (int, float)
        ) or not 0.1 <= self.velocity_scale <= 2.0:
            raise RouteError(
                f"velocity_scale must be from 0.1 to 2.0, got {self.velocity_scale!r}"
            )
        _int_in("low_note", self.low_note, 0, 127)
        _int_in("high_note", self.high_note, 0, 127)
        if self.low_note > self.high_note:
            raise RouteError("low_note is above high_note: no note could pass")

    def process(self, msg: bytes) -> bytes | None:
        """Return the message to send for ``msg``, or None to drop it. Never raises on bad
        bytes: a malformed message is dropped, because a router must not die on a stray byte.
        """
        if not msg:
            return None
        status = msg[0]
        kind = _kind(status)
        if kind is None or kind in self.block:
            return None
        if status >= 0xF0:  # system messages pass through unchanged
            return bytes(msg)
        data = list(msg[1:])
        need = 1 if (status & 0xF0) in (PROGRAM, CHAN_AT) else 2
        if len(data) != need or any(b > _DATA_MAX or b < 0 for b in data):
            return None
        channel = (status & 0x0F) + 1
        if self.channels and channel not in self.channels:
            return None
        if kind == "note":
            note = data[0]
            if not self.low_note <= note <= self.high_note:
                return None
            note += self.transpose
            if not 0 <= note <= 127:
                return None  # transposed off the keyboard: drop, do not wrap
            data[0] = note
            is_on = (status & 0xF0) == NOTE_ON and data[1] > 0
            if is_on and self.velocity_scale != 1.0:
                data[1] = max(1, min(127, round(data[1] * self.velocity_scale)))
        out_channel = self.remap_to if self.remap_to is not None else channel + self.shift
        if not 1 <= out_channel <= 16:
            return None
        return bytes([(status & 0xF0) | (out_channel - 1), *data])

    def to_dict(self) -> dict:
        d: dict = {"source": self.source, "dest": self.dest}
        if self.channels:
            d["channels"] = list(self.channels)
        if self.block:
            d["block"] = list(self.block)
        if self.remap_to is not None:
            d["remap_to"] = self.remap_to
        if self.shift:
            d["shift"] = self.shift
        if self.transpose:
            d["transpose"] = self.transpose
        if self.velocity_scale != 1.0:
            d["velocity_scale"] = self.velocity_scale
        if (self.low_note, self.high_note) != (0, 127):
            d["low_note"], d["high_note"] = self.low_note, self.high_note
        return d

    @classmethod
    def from_dict(cls, raw) -> "Route":
        if not isinstance(raw, dict):
            raise RouteError("a route must be an object")
        known = {
            "source", "dest", "channels", "block", "remap_to", "shift", "transpose",
            "velocity_scale", "low_note", "high_note",
        }
        extra = set(raw) - known
        if extra:
            raise RouteError(f"unknown route field(s): {', '.join(sorted(extra))}")
        for key in ("channels", "block"):
            if key in raw and not isinstance(raw[key], list):
                raise RouteError(f"{key} must be a list")
        for key in ("source", "dest"):
            if not isinstance(raw.get(key), str):
                raise RouteError(f"{key} must be text")
        return cls(
            source=raw["source"], dest=raw["dest"],
            channels=tuple(raw.get("channels", ())), block=tuple(raw.get("block", ())),
            remap_to=raw.get("remap_to"), shift=raw.get("shift", 0), transpose=raw.get("transpose", 0),
            velocity_scale=raw.get("velocity_scale", 1.0),
            low_note=raw.get("low_note", 0), high_note=raw.get("high_note", 127),
        )


# -------------------------------------------------------------------------------- patch

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


@dataclass(frozen=True)
class Patch:
    """A named set of routes. Immutable: edit by building a new one."""

    name: str
    routes: tuple[Route, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise RouteError(
                "a patch name is 1-40 letters, digits, space, dot, dash or underscore, "
                "starting with a letter or digit"
            )
        seen = set()
        for r in self.routes:
            if r in seen:
                raise RouteError(f"the route {r.source} -> {r.dest} appears twice, identical")
            seen.add(r)

    def ports(self) -> set[str]:
        return {p for r in self.routes for p in (r.source, r.dest)}

    def routes_from(self, source: str) -> list[Route]:
        return [r for r in self.routes if r.source == source]

    def to_dict(self) -> dict:
        return {"name": self.name, "routes": [r.to_dict() for r in self.routes]}

    @classmethod
    def from_dict(cls, raw) -> "Patch":
        if not isinstance(raw, dict) or not isinstance(raw.get("routes", []), list):
            raise RouteError("a patch must be an object with a list of routes")
        extra = set(raw) - {"name", "routes"}
        if extra:
            raise RouteError(f"unknown patch field(s): {', '.join(sorted(extra))}")
        return cls(name=raw.get("name", ""),
                   routes=tuple(Route.from_dict(r) for r in raw.get("routes", [])))

    def deliver(self, source: str, msg: bytes) -> list[tuple[str, bytes]]:
        """What to send, and where, when ``msg`` arrives from ``source``."""
        out = []
        for r in self.routes_from(source):
            sent = r.process(msg)
            if sent is not None:
                out.append((r.dest, sent))
        return out


# ------------------------------------------------------------------------------ patch store

def store_path() -> Path:
    return config.path("patches")


@dataclass
class PatchStore:
    """All saved patches in one JSON file: ``{"patches": [...]}``, written atomically."""

    path: Path
    patches: dict[str, Patch] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "PatchStore":
        path = path or store_path()
        store = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as e:
            raise RouteError(f"{path} cannot be read as JSON ({e}); fix or move it") from e
        if not isinstance(raw, dict) or not isinstance(raw.get("patches"), list):
            raise RouteError(f"{path} must hold an object with a 'patches' list")
        for p in raw["patches"]:
            patch = Patch.from_dict(p)
            if patch.name in store.patches:
                raise RouteError(f"{path}: the patch name {patch.name!r} is used twice")
            store.patches[patch.name] = patch
        return store

    def save_patch(self, patch: Patch) -> None:
        self.patches[patch.name] = patch
        self._write()

    def delete(self, name: str) -> None:
        if name not in self.patches:
            raise RouteError(f"no patch named {name!r}")
        del self.patches[name]
        self._write()

    def _write(self) -> None:
        body = json.dumps(
            {"patches": [p.to_dict() for _, p in sorted(self.patches.items())]}, indent=2
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ----------------------------------------------------------------- panic and flash release

def panic_messages(channels=ALL_CHANNELS) -> list[bytes]:
    """The messages that clear hung notes: per channel, All Sound Off (CC 120), Reset All
    Controllers (CC 121) and All Notes Off (CC 123), in that order."""
    out = []
    for ch in channels:
        _int_in("channel", ch, 0, 15)
        for cc in (120, 121, 123):
            out.append(bytes([CONTROL | ch, cc, 0]))
    return out


def ports_to_release(patch: Patch, flashing: set[str]) -> list[Route]:
    """The routes that touch a unit about to be flashed: tear these down first and resume
    after. A route is released if either end is being flashed."""
    return [r for r in patch.routes if r.source in flashing or r.dest in flashing]


def letter_channel_ranges(letters: list[str], per_unit: int = 4) -> dict[str, tuple[int, ...]]:
    """The 'A = channels 1-4, B = 5-8 ...' split from the roadmap: each letter's own range."""
    if per_unit < 1 or per_unit * len(letters) > 16:
        raise RouteError(f"{len(letters)} units x {per_unit} channels does not fit in 16")
    if len(set(letters)) != len(letters):
        raise RouteError("a letter appears twice")
    return {
        letter: tuple(range(i * per_unit + 1, (i + 1) * per_unit + 1))
        for i, letter in enumerate(letters)
    }
