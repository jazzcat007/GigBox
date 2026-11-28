"""Can the Pi's clock be trusted, and setting it from a phone when it cannot.

The Pi has no battery-backed clock. Online, systemd-timesyncd sets the time from the internet. Offline (on its
own hotspot, at a gig) a Pi that was powered off wakes up believing it is still the moment it shut down, so the
audit log, backup names and set names carry the wrong date until it is next online. Nothing here hides that:

* :func:`status` says whether the clock is trustworthy (the network set it, or a person did since this boot);
* the audit log marks lines written while it is not (see ``web_writes._log_flash``);
* a phone that opens the UI is offered "set the clock from this phone" when it is not, and the Pi's time
  and the phone's disagree.

Setting the clock is a deliberate act, so it is guarded:

* only while the network has NOT set the clock (a clock the network set is never overridden);
* the time must be plausible (not before 2026, not after 2100) and differ by at least two minutes;
* never during a write or a router hold: the router's holds and the flash lock expire on wall-clock time, and a
  jump in the middle could release a unit that is being flashed;
* ``timedatectl`` refuses to set the time while network time is on, so it is switched off for the moment and
  ALWAYS switched back on afterwards, so the clock is set by the network again as soon as there is one;
* what was changed (old time, new time, who asked) is written to the audit log.

The program that does it is ``timedatectl``; ``FM1_TIMEDATECTL`` points at a stand-in for tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

#: Nothing earlier than this is a believable "now" for this software (2026-01-01 UTC), or later than 2100.
FLOOR, CEILING = 1767225600, 4102444800
MIN_STEP = 120.0          # a difference smaller than this (seconds) is not worth a jump
_CACHE: dict = {"at": -1e9, "st": None}

Runner = Callable[[list[str]], "tuple[int, str]"]


class ClockError(RuntimeError):
    """The clock cannot be set (or read). The message says why, in words for a person."""


def timedatectl(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    exe = os.environ.get("FM1_TIMEDATECTL") or shutil.which("timedatectl") or "timedatectl"
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise ClockError("timedatectl is not installed here") from e
    except subprocess.TimeoutExpired as e:
        raise ClockError("timedatectl did not answer in time") from e
    return out.returncode, (out.stdout + out.stderr if out.returncode else out.stdout)


def marker_path() -> Path:
    return config.path("time_set_marker")


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Status:
    synchronized: bool       # the network has set the clock
    ntp: bool                # network time is switched on
    set_by_person: bool      # a person set it since this boot
    now: float

    @property
    def trusted(self) -> bool:
        return self.synchronized or self.set_by_person

    def reason(self) -> str:
        if self.synchronized:
            return "set by the network"
        if self.set_by_person:
            return "set by hand since this boot"
        return "not set by the network or by hand: the date may be wrong"


def _person_set(path: Path | None = None) -> bool:
    try:
        data = json.loads((path or marker_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    bid = boot_id()
    return bool(bid) and data.get("boot_id") == bid


def status(run: Runner = None, *, force: bool = False, clock: Callable[[], float] = time.monotonic) -> Status:
    """The clock's trust. Cached for 15 s so a busy log does not run ``timedatectl`` per line. If it cannot be
    read, the clock is reported untrusted (the safe answer)."""
    now = clock()
    if not force and _CACHE["st"] is not None and now - _CACHE["at"] < 15.0:
        return _CACHE["st"]
    synced = ntp = False
    try:
        code, out = (run or timedatectl)(["show", "-p", "NTP", "-p", "NTPSynchronized"])
        if code == 0:
            kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            synced = kv.get("NTPSynchronized", "no").strip() == "yes"
            ntp = kv.get("NTP", "no").strip() == "yes"
    except ClockError:
        pass
    st = Status(synced, ntp, _person_set(), time.time())
    _CACHE["at"], _CACHE["st"] = now, st
    return st


def forget() -> None:
    _CACHE.update(at=-1e9, st=None)


def check_epoch(epoch) -> float:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or epoch != epoch:
        raise ClockError("the time must be a number of seconds since 1970")
    if not FLOOR <= epoch <= CEILING:
        raise ClockError("that time is not believable (before 2026 or after 2100)")
    return float(epoch)


def set_time(epoch, *, run: Runner = None, busy: Callable[[], str | None] = lambda: None,
             now: Callable[[], float] = time.time, marker: Path | None = None) -> dict:
    """Set the system clock to ``epoch`` (seconds since 1970). ``busy`` returns a reason when something that
    depends on the clock is running (a write, a router hold), else None. Returns what changed."""
    run = run or timedatectl
    epoch = check_epoch(epoch)
    st = status(run, force=True)
    if st.synchronized:
        raise ClockError("the clock is already set by the network, so it is not changed by hand")
    reason = busy()
    if reason:
        raise ClockError(f"not now: {reason}")
    before = now()
    if abs(epoch - before) < MIN_STEP:
        return {"changed": False, "before": before, "after": before,
                "message": "the clock is already within two minutes of that"}
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    code, out = run(["set-ntp", "false"])
    if code != 0:
        raise ClockError(_why(out))
    try:
        code, out = run(["set-time", stamp])
        if code != 0:
            raise ClockError(_why(out))
    finally:
        run(["set-ntp", "true"])          # always: the network sets it again as soon as there is one
    path = marker or marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"boot_id": boot_id(), "set_at": epoch}, f)
    os.replace(tmp, path)
    forget()
    return {"changed": True, "before": before, "after": epoch, "stamp": stamp,
            "message": f"clock set to {stamp}"}


def _why(out: str) -> str:
    text = (out or "").strip().splitlines()[-1:] or ["timedatectl failed"]
    msg = text[0]
    if "access denied" in msg.lower() or "authorization" in msg.lower() or "not authorized" in msg.lower():
        return ("the Pi's user is not allowed to set the time yet: install the updated polkit rule "
                "(see docs/GUIDE-wifi-hotspot.md, The clock)")
    return msg


def holds_busy() -> str | None:
    """A reason not to move the clock, from the router's flash holds (a unit is being flashed)."""
    try:
        from . import router_service
        if router_service.active_holds():
            return "a unit is being flashed or restored"
    except Exception:  # noqa: BLE001
        pass
    return None
