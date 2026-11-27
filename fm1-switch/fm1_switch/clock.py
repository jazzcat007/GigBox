"""The master clock for the dawless hub: tempo, start and stop, sent to the units.

Pure logic and one small file, no ALSA and no unit contact here. The router service owns the
sequencer, so it also sends the ticks (one thread keeps ALSA single-threaded); this module only
decides *when* a tick is due and *what* the operator asked for.

**What the operator asks for** lives in ``~/fm1-clock.json`` (``bpm``, ``running``, ``targets``),
written by the CLI and the panel and followed by the running router, the same way the active
patch is. An empty ``targets`` list means every running unit.

**Timing.** MIDI clock is 24 pulses per quarter note. Ticks are scheduled against absolute
times (the next deadline is the previous one plus one interval), so a late tick does not push
the rest back and the average tempo does not drift. If the loop falls far behind (a stall of
more than :data:`MAX_CATCH_UP` ticks) it drops the backlog and restarts the grid rather than
send a burst of clocks.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config

CLOCK, START, STOP = 0xF8, 0xFA, 0xFC
PPQN = 24
BPM_MIN, BPM_MAX = 20.0, 300.0
#: More ticks than this overdue means the loop stalled; drop them instead of bursting.
MAX_CATCH_UP = 8


class ClockError(ValueError):
    """The requested clock setting is not usable."""


def clock_path() -> Path:
    return config.path("clock")


def check_bpm(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockError("tempo must be a number")
    bpm = round(float(value), 1)
    if not BPM_MIN <= bpm <= BPM_MAX:
        raise ClockError(f"tempo must be {BPM_MIN:g} to {BPM_MAX:g} BPM")
    return bpm


@dataclass(frozen=True)
class ClockSettings:
    bpm: float = field(default_factory=lambda: float(config.value("default_bpm")))
    running: bool = False
    targets: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"bpm": self.bpm, "running": self.running, "targets": list(self.targets)}

    @classmethod
    def from_dict(cls, raw) -> "ClockSettings":
        if not isinstance(raw, dict):
            raise ClockError("the clock file is not an object")
        targets = raw.get("targets", [])
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise ClockError("targets must be a list of unit keys")
        running = raw.get("running", False)
        if not isinstance(running, bool):
            raise ClockError("running must be true or false")
        return cls(check_bpm(raw.get("bpm", config.value("default_bpm"))), running,
                   tuple(sorted(set(targets))))


def load(path: Path | None = None) -> ClockSettings:
    """The saved settings. A missing file is the default (stopped, 120). A damaged file is
    an error: the router keeps what it had rather than guess."""
    path = path or clock_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ClockSettings()
    except OSError as e:
        raise ClockError(f"cannot read {path}: {e}") from e
    try:
        return ClockSettings.from_dict(json.loads(text))
    except ValueError as e:  # includes json.JSONDecodeError and ClockError
        raise ClockError(f"{path}: {e}") from e


def save(settings: ClockSettings, path: Path | None = None) -> None:
    path = path or clock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(*, bpm=None, running=None, targets=None, valid_targets=None,
           path: Path | None = None) -> ClockSettings:
    """Change some of the settings and save. ``valid_targets`` (unit keys) rejects an unknown
    target. A damaged file is replaced by the default plus this change."""
    try:
        cur = load(path)
    except ClockError:
        cur = ClockSettings()
    if bpm is not None:
        cur = ClockSettings(check_bpm(bpm), cur.running, cur.targets)
    if running is not None:
        if not isinstance(running, bool):
            raise ClockError("running must be true or false")
        cur = ClockSettings(cur.bpm, running, cur.targets)
    if targets is not None:
        if not isinstance(targets, (list, tuple)) or not all(isinstance(t, str) for t in targets):
            raise ClockError("targets must be a list of unit keys")
        bad = [t for t in targets if valid_targets is not None and t not in valid_targets]
        if bad:
            raise ClockError(f"not a unit: {', '.join(bad)}")
        cur = ClockSettings(cur.bpm, cur.running, tuple(sorted(set(targets))))
    save(cur, path)
    return cur


class MasterClock:
    """Turns settings and the passage of time into MIDI realtime bytes.

    ``set`` applies new settings and returns the transport byte to send (START or STOP) or
    ``None``; ``due`` returns how many clock ticks to send now. ``now`` is injected so tests
    need no real time.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self._now = now
        self.settings = ClockSettings()
        self._next: float | None = None  # when the next tick is due, while running
        self.ticks_sent = 0
        self.dropped = 0

    @property
    def running(self) -> bool:
        return self.settings.running

    @property
    def interval(self) -> float:
        return 60.0 / (self.settings.bpm * PPQN)

    def set(self, settings: ClockSettings) -> int | None:
        old = self.settings
        self.settings = settings
        if settings.running and not old.running:
            # START goes out now; the first clock follows one interval later, as a
            # sequencer that starts on the downbeat expects.
            self._next = self._now() + self.interval
            return START
        if old.running and not settings.running:
            self._next = None
            return STOP
        return None

    def deadline(self) -> float | None:
        """Monotonic time of the next tick, or ``None`` when stopped."""
        return self._next

    def wait(self, limit: float) -> float:
        """How long the caller may block before the next tick is due, at most ``limit``."""
        if self._next is None:
            return limit
        return max(0.0, min(limit, self._next - self._now()))

    def due(self) -> int:
        """Ticks due now. Each advances the grid by one interval from the last deadline."""
        if self._next is None:
            return 0
        now, n = self._now(), 0
        while self._next <= now:
            if n >= MAX_CATCH_UP:
                # Stalled: forget the backlog and restart the grid from here.
                self.dropped += int((now - self._next) / self.interval) + 1
                self._next = now + self.interval
                break
            n += 1
            self._next += self.interval
        self.ticks_sent += n
        return n
