"""Report the raw touch coordinates for each corner, so the overlay ranges can be fixed.

Run this ON THE PI with the panel in front of you. It prints a prompt, reads the touch
device directly for a few seconds, then reports the extremes it saw. Do the four corners
in order.

Why raw coordinates rather than a mapped cell: the kernel's ads7846 driver maps its
reading through the xmin/xmax/ymin/ymax given in config.txt onto 0..4095. If those
ranges are wrong, the mapping is compressed or shifted, and the way to fix it is to see
the numbers BEFORE mapping -- which is what /sys exposes and what this reports.
"""

from __future__ import annotations

import os
import select
import struct
import sys
import time
from pathlib import Path

# The package lives in the checkout this tools/ directory belongs to; the README's
# install clones the repo to ~/fm1. Override with FM1_HOME if yours lives elsewhere.
sys.path.insert(0, os.environ.get("FM1_HOME", str(Path.home() / "fm1" / "fm1-switch")))

from fm1_switch import touch  # noqa: E402


def read_extremes(seconds: float) -> tuple[int, int, int, int] | None:
    """Collect (xmin, xmax, ymin, ymax) from presses during ``seconds``."""
    r = touch.TouchReader()
    if not r.open():
        print("  cannot open the touch device", file=sys.stderr)
        return None
    xs: list[int] = []
    ys: list[int] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not r.readable(0.1):
            continue
        try:
            data = r._read(r.fileno, touch.EVENT_SIZE * 64)
        except BlockingIOError:
            continue
        except OSError:
            break
        for off in range(0, len(data) - touch.EVENT_SIZE + 1, touch.EVENT_SIZE):
            _s, _us, etype, code, value = touch.EVENT.unpack_from(data, off)
            if etype == touch.EV_ABS:
                if code == touch.ABS_X:
                    xs.append(value)
                elif code == touch.ABS_Y:
                    ys.append(value)
    r.close()
    if not xs or not ys:
        return None
    # Trim the extreme 2% at each end: a resistive panel produces the odd wild sample and
    # a single outlier would otherwise set the range.
    xs.sort()
    ys.sort()
    k = max(0, len(xs) // 50)
    return xs[k], xs[-1 - k], ys[k], ys[-1 - k]


def main() -> int:
    print("Touch calibration. Press each corner firmly with a stylus or fingernail")
    print("and HOLD it for the whole 4 seconds. Do them in this order.\n")
    results = {}
    for name in ("TOP-LEFT", "TOP-RIGHT", "BOTTOM-LEFT", "BOTTOM-RIGHT"):
        print(f"  {name}: press and hold now…", end="", flush=True)
        got = read_extremes(4.0)
        if got is None:
            print(" no touch seen")
            continue
        x0, x1, y0, y1 = got
        print(f" x {x0}..{x1}  y {y0}..{y1}")
        results[name] = got

    if len(results) < 4:
        print("\nNot all four corners registered. Re-run, holding each longer.")
        return 1

    tl, tr, bl, br = (results["TOP-LEFT"], results["TOP-RIGHT"],
                      results["BOTTOM-LEFT"], results["BOTTOM-RIGHT"])
    # The overlay's ranges are the extremes the panel reaches at its physical edges.
    xmin, xmax = min(tl[0], bl[0]), max(tr[1], br[1])
    ymin, ymax = min(tl[2], tr[2]), max(bl[3], br[3])
    print("\nMeasured extremes across all four corners:")
    print(f"  xmin={xmin}  xmax={xmax}  ymin={ymin}  ymax={ymax}")
    print("\nIf the axes are swapped (you pressed left/right but X barely moved),")
    print("add swapxy=1 to the overlay instead of using these numbers.")
    print("\nconfig.txt line to use:")
    print(f"dtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=500000,"
          f"keep_vref_on=0,swapxy=0,pmax=255,xohms=150,"
          f"xmin={xmin},xmax={xmax},ymin={ymin},ymax={ymax}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
