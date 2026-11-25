"""Request gates and the mutation token (audit SEC-01/02; split out of web.py, B14).

Pure helpers: no server state. web.Handler applies them in ``_gate``.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


#: Names the loopback clients (the panel and anything on the Pi itself) use.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Header a non-loopback client must present on mutations (audit §9 SEC-01 layer 4).
_TOKEN_HEADER = "X-FM1-Token"

#: Addresses serve() may be bound to while still counting as "this machine's own UI".
_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})

def _allowed_hosts() -> set[str]:
    """Loopback, plus any extra names this server is deliberately reachable as.

    The panel talks to its own loopback; if the operator binds the server to the LAN and
    reaches it by name, those names belong in FM1_WEB_HOSTS (comma separated).
    """
    names = set(_LOOPBACK_HOSTS)
    for raw in (os.environ.get("FM1_WEB_HOSTS") or "").split(","):
        name = raw.strip().lower()
        if name:
            names.add(name)
    return names

def _host_name(host_header: str) -> str:
    """The host part of a Host header, without its port. ``[::1]:8080`` -> ``::1``."""
    h = host_header.strip().lower()
    if h.startswith("["):                      # [::1]:8080
        return h[1:h.index("]")] if "]" in h else h[1:]
    if h.count(":") == 1:                      # name:port (bare IPv6 has many colons)
        return h.split(":", 1)[0]
    return h

def _origin_host(origin: str) -> str:
    """The host an Origin header claims to come from, lowercased."""
    try:
        return (urlparse(origin).hostname or "").lower()
    except ValueError:
        return ""

def _is_loopback_client(ip: str) -> bool:
    """True when the request came from this machine (the panel, a local curl)."""
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

def token_file() -> Path:
    """Where the mutation token lives (0600; it is the ability to write firmware)."""
    raw = os.environ.get("FM1_TOKEN_FILE")
    return Path(raw).expanduser() if raw else Path("~/.config/fm1/token").expanduser()

def _load_token() -> str | None:
    """A configured token: FM1_TOKEN wins, else the token file. None when neither."""
    from_env = os.environ.get("FM1_TOKEN")
    if from_env:
        return from_env
    try:
        return token_file().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

def _ensure_token() -> str:
    """The token off-Pi clients must present, creating the file mode 0600 if needed.

    Only serve() on a non-loopback bind calls this: on the Pi itself the panel is the
    trusted client and the token would be an unused second factor. The caller prints the
    value once (the journal, not the panel) so the operator can hand it to the expert UI.
    """
    found = _load_token()
    if found:
        return found
    token = f"{secrets.randbelow(1_000_000):06d}"
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)     # independent of umask: this file grants writes
    return token


def host_allowed(name: str) -> bool:
    """May a request addressed to ``name`` be answered? (headless mode, B16)

    Loopback and FM1_WEB_HOSTS as before, plus what a phone on the LAN naturally types:
    an IP literal, this machine's hostname, and ``hostname.local``. A DNS-rebinding page
    must present an attacker-controlled *name*, so none of these let one in.
    """
    name = name.strip().lower()
    if name in _allowed_hosts():
        return True
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
        return True
    except ValueError:
        pass
    mine = socket.gethostname().lower().split(".")[0]
    return bool(mine) and name in (mine, mine + ".local")


class Throttle:
    """Wrong-token guesses per client address: ``limit`` misses locks that address out for
    ``lockout`` seconds. The token is six digits; over a LAN that needs a speed limit."""

    def __init__(self, limit: int = 5, lockout: float = 300.0, clock=time.monotonic):
        self.limit, self.lockout, self._clock = limit, lockout, clock
        self._misses: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            n, at = self._misses.get(ip, (0, 0.0))
            if n >= self.limit and self._clock() - at < self.lockout:
                return True
            if n >= self.limit:
                del self._misses[ip]
            return False

    def miss(self, ip: str) -> None:
        with self._lock:
            n, _ = self._misses.get(ip, (0, 0.0))
            self._misses[ip] = (n + 1, self._clock())

    def ok(self, ip: str) -> None:
        with self._lock:
            self._misses.pop(ip, None)
