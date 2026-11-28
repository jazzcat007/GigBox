"""The Pi's WiFi, through NetworkManager's ``nmcli``, as a small set of fixed operations.

This is the only place that runs ``nmcli``. The web server never passes text to it: it can
ask for these operations by name and nothing else (see ``docs/ROADMAP-field-network.md``).

* **Saved networks** are the WiFi profiles NetworkManager already has with auto-connect on.
  They are read here, never created, changed or deleted: adding a network (and its password)
  is not this module's job.
* **The hotspot** is one profile, ``fm1-hotspot``, made and updated by :func:`ensure_hotspot`.
  It is created with auto-connect **off**: the supervisor (``netwatch.py``) is the only thing
  that raises it, so there is one decider and no race with NetworkManager's own choice.
  The netplan-generated profiles are never touched.
* The passphrase lives in a 0600 file and is generated on first use.

Everything takes a ``run`` function (``args -> (exit code, stdout)``) so the tests use a fake
and no test touches a network.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

HOTSPOT = "fm1-hotspot"
Runner = Callable[[list[str]], "tuple[int, str]"]

#: The shared default passphrase. It is published in the docs, so ANYONE who has read them can join
#: a hotspot that still uses it: change it (``fm1-switch net passphrase``). The status commands say so
#: for as long as it is in use. Written to the key file on first use.
DEFAULT_PASSPHRASE = "g1gb0x-fm1"

# No 0/O or 1/l/I: a random passphrase is read off a screen and typed on a phone.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CHANNELS = {"2.4": range(1, 12), "5": (36, 40, 44, 48, 149, 153, 157, 161, 165)}


class NetError(RuntimeError):
    """An nmcli call failed or a setting cannot be used. The message says what."""


class DefaultPassphraseRefused(NetError):
    """The hotspot would use the published default passphrase, which is not allowed."""


def nmcli(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """Run ``nmcli`` with ``args`` (a list, never a shell string). Returns (exit code, stdout)."""
    # FM1_NMCLI points at another program: a stand-in for tests, set by whoever has the shell.
    exe = os.environ.get("FM1_NMCLI") or shutil.which("nmcli") or "nmcli"
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                             check=False)
    except FileNotFoundError as e:
        raise NetError("nmcli is not installed here") from e
    except subprocess.TimeoutExpired as e:
        raise NetError(f"nmcli {' '.join(args[:3])} did not finish in {timeout:g} s") from e
    return out.returncode, out.stdout


def _split(line: str) -> list[str]:
    """Split one ``nmcli -t`` line on unescaped colons, undoing the escapes."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _rows(text: str) -> list[list[str]]:
    return [_split(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------- reading

@dataclass(frozen=True)
class WifiState:
    iface: str
    present: bool          # the adapter exists
    state: str             # "connected", "disconnected", "connecting", ...
    connection: str        # the active profile name, or ""
    hotspot: bool          # the active profile is ours
    client: bool           # connected to some other (saved) network


def wifi_state(run: Runner, iface: str) -> WifiState:
    code, out = run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    if code != 0:
        raise NetError(f"could not read the network devices (nmcli exit {code})")
    for dev, typ, state, conn in (r + [""] * (4 - len(r)) for r in _rows(out)):
        if dev == iface and typ == "wifi":
            connected = state.startswith("connected")
            return WifiState(iface, True, state, conn if connected else "",
                             connected and conn == HOTSPOT,
                             connected and bool(conn) and conn != HOTSPOT)
    return WifiState(iface, False, "missing", "", False, False)


def saved_networks(run: Runner) -> list[str]:
    """Saved WiFi networks with auto-connect on, highest priority first, the hotspot
    excluded. Only networks that join something (not access-point profiles)."""
    code, out = run(["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"])
    if code != 0:
        raise NetError(f"could not read the saved connections (nmcli exit {code})")
    found: list[tuple[int, str]] = []
    for r in _rows(out):
        if len(r) < 3 or r[1] != "802-11-wireless" or r[2] != "yes" or r[0] == HOTSPOT:
            continue
        name = r[0]
        _, mode = run(["-g", "802-11-wireless.mode", "connection", "show", name])
        if mode.strip() == "ap":
            continue
        _, prio = run(["-g", "connection.autoconnect-priority", "connection", "show", name])
        try:
            found.append((int(prio.strip() or 0), name))
        except ValueError:
            found.append((0, name))
    return [n for _, n in sorted(found, key=lambda t: -t[0])]


def hotspot_exists(run: Runner) -> bool:
    code, out = run(["-t", "-f", "NAME", "connection", "show"])
    return code == 0 and HOTSPOT in [r[0] for r in _rows(out)]


def _iw() -> str | None:
    for cand in (os.environ.get("FM1_IW"), shutil.which("iw"), "/sbin/iw", "/usr/sbin/iw"):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def joined_phones(iface: str, iw_run: Callable[[list[str]], str] | None = None) -> int | None:
    """How many stations are joined to the hotspot on ``iface``; None when it cannot be
    told (no ``iw``). None must be treated as "maybe somebody": never switch away on it."""
    if iw_run is None:
        exe = _iw()
        if exe is None:
            return None

        def iw_run(args: list[str]) -> str:
            res = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10,
                                 check=False)
            if res.returncode != 0:
                raise NetError(f"iw exited {res.returncode}")
            return res.stdout
    try:
        text = iw_run(["dev", iface, "station", "dump"])
    except (OSError, subprocess.SubprocessError, NetError):
        return None
    return sum(1 for ln in text.splitlines() if ln.startswith("Station "))


def address(run: Runner, iface: str) -> str:
    code, out = run(["-g", "IP4.ADDRESS", "device", "show", iface])
    return out.strip().splitlines()[0].split("/")[0] if code == 0 and out.strip() else ""


# --------------------------------------------------------------------------- passphrase

def key_path() -> Path:
    return config.path("net_key")


def passphrase(path: Path | None = None) -> str:
    """The hotspot passphrase. On first use the key file is made (0600) holding the shared
    default, :data:`DEFAULT_PASSPHRASE`; it is the owner's to change."""
    path = path or key_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
        if 8 <= len(text) <= 63:
            return text
    except FileNotFoundError:
        pass
    except OSError as e:
        raise NetError(f"cannot read {path}: {e}") from e
    write_passphrase(DEFAULT_PASSPHRASE, path)
    return DEFAULT_PASSPHRASE


def is_default_passphrase(path: Path | None = None) -> bool:
    """Is the hotspot still using the published default? (True when no key file exists yet.)"""
    try:
        return passphrase(path) == DEFAULT_PASSPHRASE
    except NetError:
        return False


def require_changed_passphrase(path: Path | None = None) -> None:
    """Refuse to raise the hotspot while it still uses the published default, but only under the
    ``refuse`` policy (``net_default_passphrase``). Under ``gate`` (the default) the hotspot comes up
    and the first phone to open a page is made to choose a passphrase (wifi_setup.py); ``allow`` lets
    the default stand. The default is in the public docs, so a hotspot that keeps it is open to
    anyone who has read them."""
    if str(config.value("net_default_passphrase")) != "refuse":
        return
    if is_default_passphrase(path):
        raise DefaultPassphraseRefused(
            "the hotspot passphrase is still the published default, and net_default_passphrase is "
            "'refuse', so the hotspot will not come up. Change it with `fm1-switch net passphrase "
            "--prompt` (`fm1-switch net up --yes --allow-default` allows it for one timed run)")


def random_passphrase() -> str:
    """12 characters that are easy to read off a screen and type on a phone."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(12))


def write_passphrase(text: str, path: Path | None = None) -> None:
    path = path or key_path()
    if not 8 <= len(text) <= 63 or not text.isprintable() or "\n" in text:
        raise NetError("a WiFi passphrase is 8 to 63 printable characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------- hotspot

def check_radio(band: str, channel: int) -> None:
    if band not in CHANNELS:
        raise NetError(f"band must be one of {', '.join(CHANNELS)}")
    if channel not in CHANNELS[band]:
        raise NetError(f"channel {channel} is not valid on {band} GHz "
                       f"({'1-11' if band == '2.4' else '36-48, 149-165'})")


def ensure_hotspot(run: Runner, *, iface: str, ssid: str, band: str, channel: int,
                   psk: str) -> str:
    """Make or update the ``fm1-hotspot`` profile. Returns ``"created"`` or ``"updated"``.
    Auto-connect stays off: only the supervisor raises it. Nothing is brought up here."""
    check_radio(band, channel)
    if not ssid or len(ssid.encode()) > 32:
        raise NetError("the hotspot name is 1 to 32 characters")
    if not 8 <= len(psk) <= 63:
        raise NetError("a WiFi passphrase is 8 to 63 characters")
    props = [
        "802-11-wireless.ssid", ssid, "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg" if band == "2.4" else "a",
        "802-11-wireless.channel", str(channel),
        "ipv4.method", "shared", "ipv6.method", "disabled",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk,
        "connection.autoconnect", "no", "connection.interface-name", iface,
    ]
    if hotspot_exists(run):
        code, _ = run(["connection", "modify", HOTSPOT, *props])
        verb = "updated"
    else:
        code, _ = run(["connection", "add", "type", "wifi", "con-name", HOTSPOT, *props])
        verb = "created"
    if code != 0:
        raise NetError(f"nmcli could not {'update' if verb == 'updated' else 'create'} "
                       f"the {HOTSPOT} profile (exit {code})")
    return verb


def hotspot_up(run: Runner) -> None:
    code, out = run(["-w", "30", "connection", "up", HOTSPOT])
    if code != 0:
        raise NetError(f"the hotspot did not come up (nmcli exit {code})")


def hotspot_down(run: Runner) -> None:
    code, _ = run(["connection", "down", HOTSPOT])
    if code not in (0, 10):  # 10: it was not active
        raise NetError(f"the hotspot did not go down (nmcli exit {code})")


def join(run: Runner, name: str, wait: int = 30) -> bool:
    """Try to bring one saved network up. True when it connected."""
    code, _ = run(["-w", str(wait), "connection", "up", name])
    return code == 0


def qr_payload(ssid: str, psk: str) -> str:
    """The text a phone's camera reads to join the network."""
    esc = lambda t: re.sub(r"([\\;,:\"])", r"\\\1", t)  # noqa: E731
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(psk)};;"
