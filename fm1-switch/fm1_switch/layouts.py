"""Channel layouts: which slice of a controller's 16 MIDI channels goes to which unit.

A *layout* turns "the Oxi plays channels 1-4, 5-8, 9 ..." into routes, so nobody sets a
channel on a unit by hand. Each unit has two settings, kept in one file:

* ``count``: how many channels it uses (a four-track firmware 4, a one-voice one 1);
* ``device``: the first channel the unit itself listens on (its factory channel, normally 1).

The layout decides where each unit's slice starts *on the controller*. The route for a unit
passes only that slice and shifts it down onto the unit's own channels, so the unit stays
at its own settings. The code works for any number of units: 4 x 4, or 16 x 1 one day.

Two layouts are always there, computed from the units' counts:

* ``packed``: slices follow each other with no gaps (A 1-4, B 5-8, C 9, D 10);
* ``blocks``: every unit owns a block (A 1-4, B 5-8, C 9-12, D 13-16) and uses the first
  channels of it, so a unit's channels never move when another unit changes. The block is 4
  wide, or narrower when that many units would not fit in 16 (16 units: 1 wide).

Custom layouts are saved by name with a start channel and a device channel per unit.

Applying a layout writes an ordinary patch (``auto-<layout>``) and makes it the active one;
the router needs nothing new. Nothing here touches a unit.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .router import Patch, Route, RouteError

PRESETS = ("packed", "blocks")
DEFAULT_COUNT = 4
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$")
PATCH_PREFIX = "auto-"


def store_path() -> Path:
    return config.path("layouts")


@dataclass(frozen=True)
class Slice:
    start: int   # first controller channel for this unit, 1..16
    count: int   # channels it uses
    device: int = 1  # first channel the unit listens on

    def __post_init__(self):
        for name, v in (("start", self.start), ("count", self.count), ("device", self.device)):
            if isinstance(v, bool) or not isinstance(v, int):
                raise RouteError(f"{name} must be a whole number, got {v!r}")
        if self.count < 1:
            raise RouteError("a unit uses at least 1 channel")
        if self.start < 1 or self.start + self.count - 1 > 16:
            raise RouteError(
                f"controller channels {self.start}-{self.start + self.count - 1} do not fit in 1-16")
        if self.device < 1 or self.device + self.count - 1 > 16:
            raise RouteError(
                f"unit channels {self.device}-{self.device + self.count - 1} do not fit in 1-16")

    @property
    def channels(self) -> tuple[int, ...]:
        return tuple(range(self.start, self.start + self.count))

    def to_dict(self) -> dict:
        return {"start": self.start, "count": self.count, "device": self.device}


@dataclass(frozen=True)
class Layout:
    name: str
    source: str
    slices: tuple[tuple[str, Slice], ...]  # (unit key, slice), in unit order

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise RouteError("a layout name is 1-32 letters, digits, space, dot, dash or "
                             "underscore, starting with a letter or digit")
        if not self.source:
            raise RouteError("a layout needs the controller it listens to")
        used: dict[int, str] = {}
        seen = set()
        for key, sl in self.slices:
            if key == self.source:
                raise RouteError(f"{key!r} cannot be both the controller and a unit")
            if key in seen:
                raise RouteError(f"{key!r} appears twice")
            seen.add(key)
            for ch in sl.channels:
                if ch in used:
                    raise RouteError(f"channel {ch} is given to both {used[ch]} and {key}")
                used[ch] = key

    def to_patch(self) -> Patch:
        routes = tuple(
            Route(self.source, key, channels=sl.channels, block=("sysex",),
                  shift=sl.device - sl.start)
            for key, sl in self.slices)
        return Patch(PATCH_PREFIX + self.name, routes)

    def to_dict(self) -> dict:
        return {"name": self.name, "source": self.source,
                "slices": {k: s.to_dict() for k, s in self.slices}}

    @classmethod
    def from_dict(cls, raw) -> "Layout":
        if not isinstance(raw, dict) or not isinstance(raw.get("slices"), dict):
            raise RouteError("a layout must be an object with a 'slices' object")
        extra = set(raw) - {"name", "source", "slices"}
        if extra:
            raise RouteError(f"unknown layout field(s): {', '.join(sorted(extra))}")
        out = []
        for key, s in raw["slices"].items():
            if not isinstance(s, dict) or set(s) - {"start", "count", "device"}:
                raise RouteError(f"the slice for {key!r} needs start, count and device")
            out.append((key, Slice(s.get("start"), s.get("count", 1), s.get("device", 1))))
        return cls(raw.get("name", ""), raw.get("source", ""), tuple(out))


def preset(kind: str, source: str, units: dict[str, Slice]) -> Layout:
    """``packed`` or ``blocks`` for ``units`` (``{key: Slice}``: only count and device are
    used, in the order given). Raises RouteError when they do not fit in 16 channels."""
    if kind not in PRESETS:
        raise RouteError(f"unknown preset {kind!r}")
    if kind == "packed":
        pos, out = 1, []
        for k, s in units.items():
            if pos + s.count - 1 > 16:
                raise RouteError("the units use more than 16 channels together; the most is 16")
            out.append((k, Slice(pos, s.count, s.device)))
            pos += s.count
    else:
        n = max(len(units), 1)
        block = max(max((s.count for s in units.values()), default=1),
                    min(DEFAULT_COUNT, 16 // n))
        if block * len(units) > 16:
            raise RouteError(
                f"{len(units)} units in blocks of {block} need {block * len(units)} "
                f"channels; the most is 16. Use the packed layout")
        out = [(k, Slice(i * block + 1, s.count, s.device))
               for i, (k, s) in enumerate(units.items())]
    return Layout(kind, source, tuple(out))


# -------------------------------------------------------------------------------- store

@dataclass
class LayoutStore:
    """``{"source": "oxi", "units": {key: {count, device}}, "layouts": [...], "applied": name}``,
    written atomically. ``units`` holds the per-unit settings the presets are built from."""

    path: Path
    source: str = "oxi"
    units: dict[str, Slice] = field(default_factory=dict)
    custom: dict[str, Layout] = field(default_factory=dict)
    applied: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "LayoutStore":
        path = path or store_path()
        st = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return st
        except (OSError, ValueError) as e:
            raise RouteError(f"{path} cannot be read as JSON ({e}); fix or move it") from e
        if not isinstance(raw, dict):
            raise RouteError(f"{path} must hold an object")
        st.source = raw.get("source") or "oxi"
        for key, u in (raw.get("units") or {}).items():
            st.units[key] = _unit_slice(key, u)
        for lay in raw.get("layouts") or []:
            layout = Layout.from_dict(lay)
            st.custom[layout.name] = layout
        st.applied = raw.get("applied") or ""
        return st

    def unit(self, key: str) -> Slice:
        return self.units.get(key) or Slice(1, DEFAULT_COUNT, 1)

    def layout(self, name: str, keys: list[str]) -> Layout:
        """``name`` for the units ``keys``: a preset covers exactly ``keys``; a custom layout
        is as it was saved."""
        if name in PRESETS:
            return preset(name, self.source, {k: self.unit(k) for k in keys})
        if name in self.custom:
            return self.custom[name]
        raise RouteError(f"no layout named {name!r}")

    def set_units(self, units) -> None:
        if not isinstance(units, dict):
            raise RouteError("units must be an object")
        new = {key: _unit_slice(key, u) for key, u in units.items()}
        self.units.update(new)
        self._write()

    def save_custom(self, layout: Layout) -> None:
        if layout.name in PRESETS:
            raise RouteError(f"{layout.name!r} is a built-in layout; pick another name")
        self.custom[layout.name] = layout
        self._write()

    def delete_custom(self, name: str) -> None:
        if name not in self.custom:
            raise RouteError(f"no custom layout named {name!r}")
        del self.custom[name]
        if self.applied == name:
            self.applied = ""
        self._write()

    def mark_applied(self, name: str) -> None:
        self.applied = name
        self._write()

    def _write(self) -> None:
        body = json.dumps({
            "source": self.source,
            "units": {k: {"count": s.count, "device": s.device}
                      for k, s in sorted(self.units.items())},
            "layouts": [l.to_dict() for _, l in sorted(self.custom.items())],
            "applied": self.applied,
        }, indent=2) + "\n"
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


def _unit_slice(key: str, u) -> Slice:
    if not isinstance(u, dict) or set(u) - {"count", "device"}:
        raise RouteError(f"the settings for {key!r} must be an object with count and device")
    return Slice(1, u.get("count", DEFAULT_COUNT), u.get("device", 1))
