"""The fallback supervisor: raise the Pi's own WiFi when no saved network connects.

The rules live in :class:`Fallback`, a plain state machine that is given the time and what it
sees and answers with at most one action. It touches nothing, so every rule is tested without
a network. :func:`watch` is the loop that observes, asks, acts through ``netctl`` and writes a
status file for the panel.

Rules (``docs/ROADMAP-field-network.md``), in ``auto`` mode:

* No saved network connected for ``after`` seconds: raise the hotspot. Before the first
  connection that is the *boot* rule, after one it is the *link lost* rule; each can be
  switched off.
* While the hotspot is up, every ``retry_every`` seconds try the saved networks, but only when
  nobody is joined to the hotspot (unless that guard is off). If the number of joined phones
  cannot be read, the Pi assumes somebody is using it and stays put.
* A failed try waits twice as long next time, up to an hour. The hotspot is raised again at
  once after a failed try, so a phone never loses it for long.

``home`` never raises the hotspot. ``field`` keeps it up and never joins anything.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, netctl

MAX_BACKOFF = 3600.0

HOTSPOT_UP, TRY_HOME = "hotspot_up", "try_home"


@dataclass(frozen=True)
class Policy:
    mode: str = "auto"
    at_boot: bool = True
    on_loss: bool = True
    after: float = 60.0
    retry_every: float = 300.0   # seconds; 0 = never
    only_when_empty: bool = True

    @classmethod
    def from_config(cls) -> "Policy":
        v = config.value
        return cls(mode=v("net_mode"), at_boot=v("net_fallback_at_boot"),
                   on_loss=v("net_fallback_on_loss"), after=float(v("net_fallback_after")),
                   retry_every=float(v("net_retry_home_every")) * 60,
                   only_when_empty=v("net_retry_only_when_empty"))


@dataclass(frozen=True)
class Obs:
    present: bool
    client: bool          # connected to a saved network
    hotspot: bool         # our hotspot is up
    phones: int | None    # stations joined to the hotspot; None = cannot tell


class Fallback:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.seen_client = False
        self.down_since: float | None = None
        self.fallback = False      # the hotspot is up because we raised it for a fallback
        self.fails = 0
        self.next_try = 0.0

    # -- the decision --------------------------------------------------------------------
    def step(self, now: float, obs: Obs) -> str | None:
        p = self.policy
        if not obs.present:
            self.down_since = None
            return None
        if p.mode == "field":  # always the hotspot, even over a joined network
            return None if obs.hotspot or now < self.next_try else HOTSPOT_UP
        if obs.client:
            self.seen_client = True
            self.down_since = None
            self.fallback = False
            self.fails = 0
            return None
        if p.mode == "home":
            return None
        # auto
        if obs.hotspot:
            if not self.fallback:  # up when we started, or raised by hand: look after it
                self.fallback = True
                self.next_try = now + p.retry_every
            if p.retry_every <= 0 or now < self.next_try:
                return None
            if p.only_when_empty and obs.phones != 0:
                return None  # somebody is on it, or we cannot tell: stay
            return TRY_HOME
        # neither a saved network nor the hotspot
        if self.down_since is None:
            self.down_since = now
        rule = p.on_loss if self.seen_client else p.at_boot
        if rule and now - self.down_since >= p.after and now >= self.next_try:
            return HOTSPOT_UP
        return None

    # -- what happened -------------------------------------------------------------------
    def hotspot_raised(self, now: float, ok: bool) -> None:
        if ok:
            self.down_since = None
            self.fallback = self.policy.mode == "auto"
            self.next_try = now + self.policy.retry_every
        else:  # try again after another wait, not on every tick
            self.next_try = now + max(self.policy.after, 10.0)

    def hotspot_refused(self, now: float) -> None:
        """The hotspot may not come up (default passphrase): look again in 5 minutes, not every tick."""
        self.next_try = now + 300.0

    def home_tried(self, now: float, joined: bool) -> None:
        if joined:
            self.fails, self.fallback, self.down_since = 0, False, None
            self.seen_client = True
            return
        self.fails += 1
        self.next_try = now + min(self.policy.retry_every * 2 ** self.fails, MAX_BACKOFF)


# ------------------------------------------------------------------------------- the loop

def status_path() -> Path:
    return config.path("net_status")


def _write_status(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def raise_hotspot(run: netctl.Runner, iface: str, *, allow_default: bool = False) -> None:
    """Make sure the profile matches the settings, then bring it up. Refuses (DefaultPassphraseRefused)
    while the passphrase is still the published default."""
    if not allow_default:
        netctl.require_changed_passphrase()
    netctl.ensure_hotspot(
        run, iface=iface, ssid=str(config.value("net_ssid")),
        band=str(config.value("net_band")), channel=int(config.value("net_channel")),
        psk=netctl.passphrase())
    netctl.hotspot_up(run)


def try_saved(run: netctl.Runner, iface: str, log: Callable[[str], None], *,
              allow_default: bool = False) -> bool:
    """Try each saved network in priority order. When none connects, put the hotspot back
    (joining drops it on a single radio) and say so. With no saved network, touch nothing."""
    names = netctl.saved_networks(run)
    if not names:
        log("no saved network to try")
        return False
    for name in names:
        log(f"trying {name}")
        if netctl.join(run, name):
            log(f"joined {name}")
            return True
    log("no saved network connected; the hotspot goes back up")
    try:
        raise_hotspot(run, iface, allow_default=allow_default)
    except netctl.NetError as e:
        log(f"could not raise the hotspot again: {e}")
    return False


def watch(run: netctl.Runner, stop: threading.Event, *,
          iface: str | None = None, interval: float = 5.0,
          policy_fn: Callable[[], Policy] = Policy.from_config,
          phones_fn: Callable[[str], int | None] = netctl.joined_phones,
          log: Callable[[str], None] = lambda s: None,
          clock: Callable[[], float] = time.monotonic,
          status_file: Path | None = None) -> None:
    """Observe, decide, act, until ``stop`` is set. A failing tick is logged and the loop
    goes on: this must not die because one ``nmcli`` call did."""
    iface = iface or str(config.value("net_iface"))
    fb = Fallback(policy_fn())
    blocked = False
    while not stop.is_set():
        now = clock()
        try:
            fb.policy = policy_fn()
            st = netctl.wifi_state(run, iface)
            phones = phones_fn(iface) if st.hotspot else None
            action = fb.step(now, Obs(st.present, st.client, st.hotspot, phones))
            if action == HOTSPOT_UP:
                log(f"no saved network for {fb.policy.after:g} s: raising the hotspot")
                try:
                    raise_hotspot(run, iface)
                    fb.hotspot_raised(clock(), True)
                    blocked = False
                    log("hotspot is up")
                except netctl.DefaultPassphraseRefused as e:
                    fb.hotspot_refused(clock())
                    if not blocked:        # say it once, not on every look
                        log(f"hotspot refused: {e}")
                    blocked = True
                except netctl.NetError as e:
                    fb.hotspot_raised(clock(), False)
                    log(f"hotspot failed: {e}")
            elif action == TRY_HOME:
                joined = try_saved(run, iface, log)
                fb.home_tried(clock(), joined)
            try:
                _write_status(status_file or status_path(), {
                    "mode": fb.policy.mode, "iface": iface, "present": st.present,
                    "state": ("hotspot" if st.hotspot else "client" if st.client
                              else st.state),
                    "connection": st.connection, "fallback": fb.fallback,
                    "phones": phones, "blocked": "default-passphrase" if blocked else "",
                    "updated": time.time()})
            except OSError as e:
                log(f"could not write the status file: {e}")
        except netctl.NetError as e:
            log(f"network check failed: {e}")
        except Exception as e:  # noqa: BLE001 - the supervisor must keep running
            log(f"unexpected error, carrying on: {e!r}")
        stop.wait(interval)
