"""Re-register the fleet by itself after a power-up, so the panel stops asking.

After a power cycle every unit re-enumerates, and look-alike units (same firmware, same USB
description) can only be told apart by their port plus the USB device number they had. The
device numbers all changed, so the registry cannot prove which is which and the panel offers
RE-REGISTER FLEET. This module runs that same operation unprompted. It never writes flash and
never gives a new letter (``guided.reconcile``); the units that need it restart.

The rules are a state machine that touches nothing, so each is tested without hardware:

* **Wait for the bus to settle.** Units power up one at a time. It acts only after the set of
  ``(port, device number)`` pairs has stayed the same for ``settle`` seconds.
* **Never interrupt someone.** Not while a flash or other write holds the lock, and not while a
  unit sits in download mode (it may be there on purpose, or mid-REGISTER).
* **Never loop.** At most ``max_attempts`` runs per episode, ``retry_after`` seconds apart. A
  unit that cannot be matched (no MIDI link, not found) would otherwise be restarted for ever.
  After the last attempt it gives up and says so; the button is still there. An episode ends
  when nothing has needed matching for ``calm_reset`` seconds.
* **Switchable.** The ``auto_reregister`` setting turns it off.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    settle: float = 20.0
    retry_after: float = 120.0
    max_attempts: int = 2
    calm_reset: float = 60.0


@dataclass(frozen=True)
class View:
    needs: bool        # the panel would ask to RE-REGISTER FLEET
    signature: tuple   # ((port, devnum), ...) on the bus now
    busy: bool         # a write is running, or a unit is in download mode


RUN, GAVE_UP = "run", "gave_up"


class AutoReregister:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.attempts = 0
        self._sig: tuple | None = None
        self._stable_since: float | None = None
        self._calm_since: float | None = None
        self._next_ok = 0.0
        self._gave_up_said = False

    def step(self, now: float, v: View) -> str | None:
        p = self.policy
        if not p.enabled or v.busy:
            self._sig = self._stable_since = None
            return None
        if not v.needs:
            self._sig = self._stable_since = None
            if self._calm_since is None:
                self._calm_since = now
            if now - self._calm_since >= p.calm_reset:
                self.attempts, self._gave_up_said = 0, False
            return None
        self._calm_since = None
        if v.signature != self._sig:  # something changed on the bus: start the wait again
            self._sig, self._stable_since = v.signature, now
            return None
        if now - (self._stable_since if self._stable_since is not None else now) < p.settle:
            return None
        if self.attempts >= p.max_attempts:
            if not self._gave_up_said:
                self._gave_up_said = True
                return GAVE_UP
            return None
        if now < self._next_ok:
            return None
        self.attempts += 1
        self._next_ok = now + p.retry_after
        self._sig = self._stable_since = None  # the restart changes the bus: settle afresh
        return RUN


def watch(stop: threading.Event, *, policy_fn: Callable[[], Policy],
          view_fn: Callable[[], View], reconcile_fn: Callable[[], str],
          log: Callable[[str], None] = lambda s: None,
          clock: Callable[[], float] = time.monotonic, interval: float = 5.0) -> None:
    """Observe, decide, act until ``stop``. A failing tick is logged and the loop goes on."""
    ar = AutoReregister(policy_fn())
    while not stop.is_set():
        try:
            ar.policy = policy_fn()
            if not ar.policy.enabled:  # off: read nothing, touch nothing
                stop.wait(interval)
                continue
            action = ar.step(clock(), view_fn())
            if action == RUN:
                log(f"units need matching: re-registering (attempt {ar.attempts} of "
                    f"{ar.policy.max_attempts})")
                log(f"result: {reconcile_fn()}")
            elif action == GAVE_UP:
                log("still not matched after the automatic attempts; use RE-REGISTER FLEET "
                    "on the fleet screen")
        except Exception as e:  # noqa: BLE001 - a failing check must not end the watcher
            log(f"auto re-register check failed: {e!r}")
        stop.wait(interval)
