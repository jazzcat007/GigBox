"""The router service: runs a :class:`router.Patch` over a sequencer backend.

The backend is a small interface (``connect``, ``disconnect``, ``read``, ``send``) so the
service is tested with a fake and runs on the Pi with :class:`alsa_seq.AlsaSeq`.

**Releasing units for a flash is a cross-process rule.** The flash and soft-key paths run in
the web server, the router runs in its own process. A flash path calls :func:`hold_and_wait`
before it touches a unit: it writes a *hold* (a unit key and an expiry) to a small file and
waits until the router reports that unit released. The router checks the holds on every
refresh and detaches held units, and takes them back when the hold is cleared or expires.
If no router is running, :func:`hold_and_wait` returns at once, so the flash path never waits
on a service that is not there. A hold always expires, so a crashed flash cannot silence a
unit forever.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import clock as clocklib
from . import router
from .alsa_seq import SysexAssembler
from . import config



#: Devices that are not FM-1s but can be routed: ``{route key: ALSA client name}``. They are
#: found by the sequencer client's name, not a sticker, and are never flashed, so they have
#: no registry entry. They are left out of the master clock's default targets and out of
#: panic: the router only sends them what a route explicitly names.
EXTERNAL_DEVICES: dict[str, str] = {"oxi": "OXI ONE"}


def is_external(key: str) -> bool:
    return key in EXTERNAL_DEVICES


class Backend(Protocol):
    def connect(self, client: int, port: int = 0) -> None: ...
    def disconnect(self, client: int, port: int = 0) -> None: ...
    def read(self, timeout: float = 0.1) -> list[tuple[int, bytes]]: ...
    def send(self, client: int, data: bytes, port: int = 0) -> None: ...


# ------------------------------------------------------------------------------ files

def holds_path() -> Path:
    return config.path("router_holds")


def status_path() -> Path:
    return config.path("router_status")


def _write_json(path: Path, body: dict) -> None:
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


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def active_holds(now: float | None = None, path: Path | None = None) -> set[str]:
    now = time.time() if now is None else now
    out = set()
    for key, expiry in _read_json(path or holds_path()).items():
        if isinstance(key, str) and isinstance(expiry, (int, float)) and expiry > now:
            out.add(key)
    return out


def _locked_update(path: Path, change: Callable[[dict], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = {k: v for k, v in _read_json(path).items()
                if isinstance(v, (int, float)) and v > time.time()}
        change(data)
        _write_json(path, data)


def place_hold(keys, ttl: float | None = None, path: Path | None = None) -> None:
    """Ask the router to let go of these unit keys for ``ttl`` seconds."""
    if ttl is None:
        ttl = float(config.value("hold_ttl"))
    expiry = time.time() + ttl

    def change(d: dict) -> None:
        for k in keys:
            d[k] = expiry
    _locked_update(path or holds_path(), change)


def clear_hold(keys, path: Path | None = None) -> None:
    def change(d: dict) -> None:
        for k in keys:
            d.pop(k, None)
    _locked_update(path or holds_path(), change)


def router_running(now: float | None = None, path: Path | None = None) -> bool:
    now = time.time() if now is None else now
    stamp = _read_json(path or status_path()).get("updated")
    return isinstance(stamp, (int, float)) and now - stamp < float(config.value("status_fresh"))


def hold_and_wait(keys, *, ttl: float | None = None, timeout: float = 5.0,
                  poll: float = 0.1, hpath: Path | None = None,
                  spath: Path | None = None) -> bool:
    """Place a hold and wait until the router has released every key.

    True when the units are free (released, or no router is running); False when a running
    router had not let go within ``timeout``. The hold stays placed either way: the caller
    decides whether to go on, and calls :func:`clear_hold` afterwards.
    """
    keys = list(keys)
    place_hold(keys, ttl, hpath)
    deadline = time.monotonic() + timeout
    while True:
        if not router_running(path=spath):
            return True
        attached = set(_read_json(spath or status_path()).get("attached", []))
        if not (attached if ALL_UNITS in keys else set(keys) & attached):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


#: A hold on this key frees every unit. The soft key has been seen to reach more than the
#: unit it was aimed at, so it holds the whole fleet.
ALL_UNITS = "*"


class released_for_soft_key:
    """Context manager for the soft-key path: frees every unit the router holds, then takes
    the hold off again. It does nothing, and writes nothing, when no router is running, and
    it never raises: a router problem must not stand between the operator and a unit."""

    def __init__(self, ttl: float | None = None, timeout: float = 5.0):
        self.ttl, self.timeout, self.active = ttl, timeout, False

    def __enter__(self):
        try:
            if router_running():
                self.active = True
                hold_and_wait([ALL_UNITS], ttl=self.ttl, timeout=self.timeout)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.active:
            try:
                clear_hold([ALL_UNITS])
            except Exception:
                pass
        return False


# ------------------------------------------------------------------------ active patch

def active_path() -> Path:
    return config.path("router_active")


def read_active(path: Path | None = None) -> str | None:
    """The patch the panel chose to run. ``None``: nothing chosen (the router falls back to
    its configured default). ``""``: routing was stopped on purpose (the router idles)."""
    value = _read_json(path or active_path()).get("patch")
    return value if isinstance(value, str) else None


def write_active(name: str | None, path: Path | None = None) -> None:
    _write_json(path or active_path(), {"patch": name})


class PatchFollower:
    """What ``route run`` asks for a changed patch. The patch it runs is the one named on the
    command line, else the one chosen on the panel (the active file), else ``default``.
    Editing either file takes effect while running. A missing patch idles (units stay free);
    an unreadable patch file keeps the patch that is running."""

    def __init__(self, explicit: str | None, default: str,
                 log: Callable[[str], None] = lambda s: None,
                 store_file: Path | None = None, active_file: Path | None = None):
        self.explicit, self.default, self.log = explicit, default, log
        self.store_file, self.active_file = store_file, active_file
        self._seen: object = object()
        self.name = explicit or default

    def _stamp(self, path: Path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def __call__(self) -> router.Patch | None:
        store_file = self.store_file or router.store_path()
        stamp = (self._stamp(store_file), self._stamp(self.active_file or active_path()))
        if stamp == self._seen:
            return None
        self._seen = stamp
        chosen = None if self.explicit else read_active(self.active_file)
        if chosen == "":
            self.name = ""
            self.log("routing stopped from the panel; units are free")
            return router.Patch("idle")
        self.name = self.explicit or chosen or self.default
        try:
            found = router.PatchStore.load(store_file).patches.get(self.name)
        except router.RouteError as e:
            self.log(f"patch file unusable, keeping the current patch: {e}")
            return None
        if found is None:
            self.log(f"waiting for a patch named {self.name!r}")
            return router.Patch("waiting")
        return found


class ClockFollower:
    """What ``route run`` asks for changed master-clock settings: the file is read when it
    changes. An unreadable file keeps the settings that are running."""

    def __init__(self, log: Callable[[str], None] = lambda s: None, path: Path | None = None):
        self.log, self.path = log, path
        self._seen: object = object()

    def _file(self) -> Path:
        return self.path or clocklib.clock_path()

    def start_stopped(self) -> None:
        """A router that starts does not start the units' sequencers: if the file says the
        clock was running, it is switched off first."""
        try:
            st = clocklib.load(self.path)
            if st.running:
                clocklib.save(clocklib.ClockSettings(st.bpm, False, st.targets), self.path)
                self.log("clock was left running; starting stopped")
        except (clocklib.ClockError, OSError) as e:
            self.log(f"clock file unusable at start: {e}")

    def __call__(self) -> clocklib.ClockSettings | None:
        try:
            stamp = self._file().stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self._seen:
            return None
        self._seen = stamp
        try:
            return clocklib.load(self.path)
        except clocklib.ClockError as e:
            self.log(f"clock file unusable, keeping the current clock: {e}")
            return None


def panic_all(backend: Backend, resolve: Callable[[], dict[str, int]],
              log: Callable[[str], None] = lambda s: None) -> int:
    """Send every channel sound-off, reset and notes-off to every running unit the registry
    can name, whatever the patch. Attaches and lets go again; returns the messages sent."""
    svc = RouterService(backend, resolve, lambda: set(), log)
    svc.publish = False
    svc.always = {k for k in resolve() if not is_external(k)}
    svc.refresh()
    try:
        return svc.panic()
    finally:
        for key in list(svc.attached):
            svc._detach(key)


# ---------------------------------------------------------------------------- service

class RouterService:
    """Runs one patch. ``resolve`` returns ``{unit key: ALSA client}`` for the units present
    right now; it is called on every refresh, because client numbers change on replug."""

    def __init__(self, backend: Backend, resolve: Callable[[], dict[str, int]],
                 holds: Callable[[], set[str]] = active_holds,
                 log: Callable[[str], None] = lambda s: None,
                 status_file: Path | None = None):
        self.backend = backend
        self.resolve = resolve
        self.holds = holds
        self.log = log
        self.status_file = status_file
        self.patch = router.Patch("empty")
        self.attached: dict[str, int] = {}
        self.missing: set[str] = set()
        self.held: set[str] = set()
        self._asm = SysexAssembler()
        self._present: dict[str, int] | None = None
        #: Units to attach even when no route names them (used by `route panic`).
        self.always: set[str] = set()
        self.dropped_to_missing = 0
        #: False for a one-shot helper (panic): it must not overwrite a running router's status.
        self.publish = True
        #: The master clock (tempo and transport sent to the units). Idle until applied.
        self.clock = clocklib.MasterClock()
        self._clock_errors: set[str] = set()

    def set_patch(self, patch: router.Patch) -> None:
        self.patch = patch
        self.refresh()

    def refresh(self, rescan: bool = True) -> None:
        """Attach the patch's units that are present and not held; detach the rest.
        ``rescan=False`` reuses the last look at what is plugged in (it is the costly part)
        and only re-reads the holds."""
        if rescan or self._present is None:
            self._present = self.resolve()
        present = self._present
        holds = self.holds()
        clock_ports = self._clock_ports(present)
        used = self.patch.ports() | clock_ports
        self.held = set(used) if ALL_UNITS in holds else holds & used
        wanted = (used | self.always) - self.held
        for key in list(self.attached):
            client = self.attached[key]
            if key not in wanted or present.get(key) != client:
                self._detach(key)
        self.missing = set()
        for key in sorted(wanted):
            if key in self.attached:
                continue
            client = present.get(key)
            if client is None:
                self.missing.add(key)
                continue
            try:
                self.backend.connect(client, 0)
            except Exception as e:  # a failed attach is reported, never fatal
                self.log(f"could not attach {key} (client {client}): {e}")
                self.missing.add(key)
                continue
            self.attached[key] = client
            self.log(f"attached {key} (client {client})")
        self._write_status()

    def _clock_ports(self, present: dict[str, int]) -> set[str]:
        """The units the running clock must reach: its targets, or every unit present."""
        st = self.clock.settings
        if not st.running:
            return set()
        return set(st.targets) if st.targets else {k for k in present if not is_external(k)}

    def apply_clock(self, settings: clocklib.ClockSettings) -> None:
        """Follow new clock settings. START goes to the targets once they are attached; STOP
        goes out before they are let go. A unit added to or dropped from the targets while the
        clock runs is started or stopped on its own."""
        old = self.clock.settings
        byte = self.clock.set(settings)
        if byte == clocklib.START:
            self.refresh(rescan=False)
            self._send_realtime(byte, self._clock_keys(settings))
        elif byte == clocklib.STOP:
            self._send_realtime(byte, self._clock_keys(old))
            self.refresh(rescan=False)
        elif settings.running and settings.targets != old.targets:
            before = self._clock_keys(old)
            after = self._clock_keys(settings)
            self._send_realtime(clocklib.STOP, before - after)  # still attached: stop first
            self.refresh(rescan=False)
            after = self._clock_keys(settings)  # newly attached units count now
            self._send_realtime(clocklib.START, after - before)
        else:
            self.refresh(rescan=False)
        self.log(f"clock: {settings.bpm:g} BPM, {'running' if settings.running else 'stopped'}"
                 + (f", to {', '.join(settings.targets)}" if settings.targets else ""))

    def _clock_keys(self, settings: clocklib.ClockSettings) -> set[str]:
        keys = set(self.attached)
        return keys & set(settings.targets) if settings.targets else {
            k for k in keys if not is_external(k)}

    def _send_realtime(self, byte: int, keys) -> None:
        for key in sorted(keys):
            client = self.attached.get(key)
            if client is None:
                continue
            try:
                self.backend.send(client, bytes([byte]))
            except Exception as e:
                if key not in self._clock_errors:
                    self._clock_errors.add(key)
                    self.log(f"clock to {key} failed: {e}")

    def tick(self) -> int:
        """Send the clock ticks that are due. Returns how many ticks went out."""
        n = self.clock.due()
        if n:
            keys = self._clock_keys(self.clock.settings)
            for _ in range(n):
                self._send_realtime(clocklib.CLOCK, keys)
        return n

    def _detach(self, key: str) -> None:
        client = self.attached.pop(key)
        try:
            self.backend.disconnect(client, 0)
        except Exception as e:
            self.log(f"could not detach {key} (client {client}): {e}")
        self.log(f"released {key}")

    def release_all(self) -> None:
        for key in list(self.attached):
            self._detach(key)
        self._write_status()

    def pump(self, timeout: float = 0.1) -> int:
        """Route what has arrived. Returns the number of messages sent on."""
        by_client = {c: k for k, c in self.attached.items()}
        sent = 0
        for client, chunk in self.backend.read(timeout):
            key = by_client.get(client)
            if key is None:
                continue
            for msg in self._asm.feed(client, chunk):
                for dest, out in self.patch.deliver(key, msg):
                    target = self.attached.get(dest)
                    if target is None:
                        self.dropped_to_missing += 1
                        continue
                    try:
                        self.backend.send(target, out)
                        sent += 1
                    except Exception as e:
                        self.log(f"send to {dest} failed: {e}")
        return sent

    def panic(self) -> int:
        n = 0
        for key, client in self.attached.items():
            if is_external(key):
                continue
            for msg in router.panic_messages():
                try:
                    self.backend.send(client, msg)
                    n += 1
                except Exception as e:
                    self.log(f"panic to {key} failed: {e}")
                    break
        return n

    def status(self) -> dict:
        return {"patch": self.patch.name, "attached": sorted(self.attached),
                "missing": sorted(self.missing), "held": sorted(self.held),
                "clock": {**self.clock.settings.to_dict(), "ticks": self.clock.ticks_sent,
                          "dropped": self.clock.dropped},
                "updated": time.time()}

    def _write_status(self) -> None:
        if not self.publish:
            return
        try:
            _write_json(self.status_file or status_path(), self.status())
        except OSError as e:
            self.log(f"could not write the status file: {e}")

    def run(self, stop: threading.Event, rescan_every: float = 2.0,
            holds_every: float = 0.2,
            reload: Callable[[], router.Patch | None] | None = None,
            clock: Callable[[], clocklib.ClockSettings | None] | None = None) -> None:
        """Route until ``stop`` is set. ``reload`` is asked on every rescan for a changed
        patch (None means unchanged), so editing the patch file takes effect while running.
        ``clock`` is asked on every holds check for changed master-clock settings, and the
        ticks are sent from this loop, so ALSA is only ever used from one thread."""
        last_scan = last_holds = 0.0
        try:
            while not stop.is_set():
                now = time.monotonic()
                new_clock = clock() if clock and now - last_holds >= holds_every else None
                if new_clock is not None:
                    self.apply_clock(new_clock)
                if now - last_scan >= rescan_every:
                    new = reload() if reload else None
                    if new is not None:
                        self.log(f"patch {new.name!r}: {len(new.routes)} route(s)")
                        self.patch = new
                    self.refresh()
                    last_scan = last_holds = now
                elif now - last_holds >= holds_every:
                    self.refresh(rescan=False)
                    last_holds = now
                self.tick()
                self.pump(self.clock.wait(0.05))
        finally:
            if self.clock.running:  # leave no unit waiting on a clock that has gone
                self._send_realtime(clocklib.STOP, self._clock_keys(self.clock.settings))
            self.release_all()
            try:
                (self.status_file or status_path()).unlink()
            except OSError:
                pass


def external_clients(aconnect: str) -> dict[str, int]:
    """``{key: ALSA client}`` for each external device (:data:`EXTERNAL_DEVICES`) that is
    plugged in. A name seen twice is skipped: two of one device cannot be told apart by name,
    and guessing would send notes to the wrong box."""
    import re

    found: dict[str, list[int]] = {}
    for client, name in re.findall(r"client (\d+): '([^']*)' \[type=kernel", aconnect):
        for key, wanted in EXTERNAL_DEVICES.items():
            if name.strip() == wanted:
                found.setdefault(key, []).append(int(client))
    return {k: v[0] for k, v in found.items() if len(v) == 1}


def registry_resolver() -> Callable[[], dict[str, int]]:
    """``{sticker slot: ALSA client}`` for the running units the registry can name.

    A unit is keyed by its sticker slot (``fm1-c``), whose binding is the registered flash
    id. A running unit with no sticker, or whose USB port has no sequencer client, is absent.
    This looks only at units that are running: it never talks to a unit in download mode.
    """
    import subprocess

    from . import maker_install, registry

    def resolve() -> dict[str, int]:
        try:
            text = subprocess.run(["aconnect", "-l"], capture_output=True, text=True,
                                  timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        out: dict[str, int] = {}
        for slot, port in registry.running_slots().items():
            client = maker_install.alsa_client_for_usb_path(port, text)
            if client is not None:
                out[slot] = client
        out.update(external_clients(text))
        return out

    return resolve
