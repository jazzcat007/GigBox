"""Read the panel's touchscreen directly, so taps work without console mouse support.

Why not ncurses mouse events
----------------------------
The obvious route is ``gpm`` or the kernel's console mouse, which turns pointer motion
into escape sequences ncurses understands. On this device that route has two problems:

* it needs ``gpm`` installed and running against the right device, and
* it silently delivers *nothing* when it is missing or pointed elsewhere -- the UI looks
  perfect and ignores every tap, which is indistinguishable from broken hardware.

Measured on this panel (2026-11-22): the touch controller is a real input device
(``ADS7846 Touchscreen``, ``/dev/input/event0``) that the operator can read, while no
console-mouse daemon exists. So the screen reads the device itself. One less moving part.

What this module deliberately does NOT do
-----------------------------------------
It does not calibrate. The kernel's ``ads7846`` overlay already maps raw 0..4095 readings
onto the range given by ``xmin``/``xmax``/``ymin``/``ymax`` in ``config.txt``, and reports
``ABS_X``/``ABS_Y`` in 0..4095 regardless. Turning that into a screen cell is done here,
against the range the device advertises, so a better calibration in config.txt improves
the result without touching this code.

Resistive panels are noisy when a stylus rests, so a tap is only reported on the *press*
edge and then suppressed until release. Otherwise one firm press would fire the button
under the stylus several times a second.
"""

from __future__ import annotations

import os
import select
import struct
from dataclasses import dataclass

#: Linux evdev structures. struct input_event is 24 bytes on 64-bit ARM/x86:
#: struct timeval time (16 bytes) + __u16 type + __u16 code + __s32 value.
EVENT = struct.Struct("llHHi")
EVENT_SIZE = EVENT.size

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
ABS_X = 0x00
ABS_Y = 0x01
ABS_PRESSURE = 0x18
BTN_TOUCH = 0x14A
SYN_REPORT = 0


@dataclass
class Touch:
    """One completed press, in screen cell coordinates.

    ``raw_x``/``raw_y`` are the values the driver reported, kept so calibration can work
    from the sensor range rather than from the coarse cell it mapped onto (one cell is
    ~50 raw units across, which is far too coarse to calibrate against).
    """

    col: int
    row: int
    raw_x: int = 0
    raw_y: int = 0


class TouchReader:
    """Turns raw evdev events into cell coordinates for a given console size.

    ``path`` is opened lazily so that constructing this on a machine with no touchscreen
    (a laptop, the test suite) is harmless.
    """

    def __init__(self, path: str = "/dev/input/event0", *,
                 reader=None) -> None:
        self.path = path
        #: Where bytes come from. Injectable so the event parsing can be tested without a
        #: real device -- and so a busy-loop never mistakes stdout for an event node.
        self._read = reader or os.read
        self._fd: int | None = None
        self._x = 0
        self._y = 0
        self._pressed = False
        #: True while the stylus is down, so the UI can highlight on press.
        self.down = False
        self.x_range = (0, 4095)
        self.y_range = (0, 4095)

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> bool:
        """Open the device non-blocking. Returns False when unavailable."""
        if self._fd is not None:
            return True
        try:
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self._fd = None
            return False
        return True

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    @property
    def fileno(self) -> int | None:
        return self._fd

    # -- geometry ----------------------------------------------------------
    def _axis_ranges(self) -> None:
        """Read ABS_X/ABS_Y ranges from sysfs if we can, else keep 0..4095."""
        node = os.path.basename(self.path)
        base = f"/sys/class/input/{node}/device"
        for axis, name in ((0, "x"), (1, "y")):
            try:
                lo = int(open(f"{base}/abs_{name}_min").read())
                hi = int(open(f"{base}/abs_{name}_max").read())
            except (OSError, ValueError):
                continue
            if hi > lo:
                if axis == 0:
                    self.x_range = (lo, hi)
                else:
                    self.y_range = (lo, hi)

    def to_cell(self, cols: int, rows: int) -> tuple[int, int]:
        """Map the last raw reading onto a screen cell, clamped to the screen."""
        (x0, x1), (y0, y1) = self.x_range, self.y_range
        fx = (self._x - x0) / max(1, x1 - x0)
        fy = (self._y - y0) / max(1, y1 - y0)
        col = int(round(fx * (cols - 1)))
        row = int(round(fy * (rows - 1)))
        return max(0, min(cols - 1, col)), max(0, min(rows - 1, row))

    # -- reading -----------------------------------------------------------
    def readable(self, timeout: float) -> bool:
        """Whether the device has data within ``timeout`` seconds."""
        if self._fd is None:
            return False
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def read_taps(self, cols: int, rows: int) -> list[Touch]:
        """Drain pending events and return the taps they complete.

        A tap is emitted on the press edge and then latched until the stylus is lifted,
        so holding a finger down cannot fire the same button repeatedly.
        """
        if self._fd is None:
            return []
        taps: list[Touch] = []
        while True:
            try:
                data = self._read(self._fd, EVENT_SIZE * 64)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                break
            if not data:
                break
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _us, etype, code, value = EVENT.unpack_from(data, off)
                if etype == EV_ABS:
                    if code == ABS_X:
                        self._x = value
                    elif code == ABS_Y:
                        self._y = value
                elif etype == EV_KEY and code == BTN_TOUCH:
                    if value and not self._pressed:
                        self._pressed = True
                        self.down = True
                        col, row = self.to_cell(cols, rows)
                        taps.append(Touch(col=col, row=row,
                                          raw_x=self._x, raw_y=self._y))
                    elif not value:
                        self._pressed = False
                        self.down = False
        return taps
