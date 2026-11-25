"""A local web UI for the FM-1 fleet, served by the Pi, used from a phone or tablet.

Why a web UI over HTTP rather than curses on the panel: the phone app, the hub browse
and the library all want real layout, images and scrolling, which a 99x30 terminal
cannot give. The Pi serves plain HTML/CSS/JS and a small JSON API; by default only the
panel and other local clients can drive it (loopback bind), and even when the operator
binds it onto the LAN the request gates below refuse cross-origin drivers and demand a
token for mutations. Nothing is installed on the Pi beyond the Python standard
library, so a 16 GB card and a 1 GB Pi 3B+ both stay comfortable.

Design rules, in order of importance:

1. **No write without a confirmation that names both sides.** A flash request carries
   the unit's slot and the package's sha256; the server refuses if either has changed
   since the confirmation was shown.
2. **Never guess which unit.** A unit is named by its flash id (read in download mode and
   bound to a sticker letter, see ``registry``/``registration``). The id in a request must
   match the device found now, and an unregistered or unmatched unit is shown as such,
   never as "the one it found".
3. **The catalogue is read once a day and cached.** Offline, the UI shows the cached
   list and says it is cached. It never silently degrades to "no firmwares".
4. **Say what is unknown.** Chip compatibility, save survival and resource payloads are
   reported as facts with their caveats attached, not smoothed into a green tick.

Threading: ``APP.lock`` is held for the whole of a write (flash, restore, backup), and a
second write is refused, not queued. While it is held nothing reads a device: fleet polls
are answered from the last cached state (``APP.last_fleet``), because m-flasher refuses to
act when several units are on USB and a read mid-write would interleave. ``APP.state_lock``
is a separate, short lock for the busy/last_event/last_fleet fields and is never held
across a device call. The request gates and token live in ``security``; saved-image
listing and pruning in ``backups``.

Where the code lives (B14): ``web_state`` holds the shared state (``APP``, unit views, the
catalogue, package hashing); ``web_writes`` holds every act that changes a unit or a saved
image, with its audit line; ``web_http`` holds the request gates, the router and the
endpoints. This module is the entry point: ``serve()`` starts the server, and the names
below are re-exported so ``web.<name>`` keeps working for callers and tests.
"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from . import guided, hub, library, mflasher, packages, registration, registry  # noqa: F401 (re-exported)
from .backups import prune_auto_backups  # noqa: F401 (re-exported)
from .security import (_LOOPBACK_BINDS, _LOOPBACK_HOSTS, _TOKEN_HEADER, _allowed_hosts,  # noqa: F401
                       _ensure_token, _host_name, _is_loopback_client, _load_token, _origin_host,
                       Throttle, host_allowed, token_file)
from .web_http import STATIC_FILES, THROTTLE, Handler, _json_ready  # noqa: F401
from .web_state import (APP, _IDENT_IN_LABEL, _PKG_CACHE, _SHA_CACHE, _VERSION, App,  # noqa: F401
                        STATIC_DIR, UnitView, _catalog_cached, _catalog_meta,
                        _identity_from_firmware, _sha256, _stat_key, _ui_build, _ver, getlist,
                        port_map_path, read_port_map, update_for)
from .web_writes import (_backup_by_id_path, _backup_before_write, _check_package,  # noqa: F401
                         _fresh_backup_ok, _log_flash, _run_guided, backup_unit_now,
                         delete_backup, flash_log_path, flash_unit_by_id, recent_flashes,
                         restore_unit_by_id)

__all__ = [
    "APP", "App", "Handler", "ThreadingHTTPServer", "THROTTLE", "STATIC_DIR", "STATIC_FILES",
    "UnitView", "backup_unit_now", "delete_backup", "flash_log_path", "flash_unit_by_id",
    "getlist", "port_map_path", "read_port_map", "recent_flashes", "restore_unit_by_id",
    "serve", "update_for",
]


def _harden_umask() -> None:
    """Everything this process creates is for the owner's eyes only (audit §9 SEC-05).

    The audit measured fresh state files at 0644: the flash log names units, the
    registry binds them, and whole-flash backups contain everything on a unit. New
    files are 0600 (directories 0700) from here on, and fm1-web.service gets UMask=0077
    for the same effect; this line covers every other way the server is started. Files
    that already exist keep their old bits -- one-time ``chmod 600`` is the owner's step.
    """
    os.umask(0o077)


def serve(host: str = "127.0.0.1", port: int = 8080) -> int:
    """Serve the UI. Loopback is the default (SEC-02): the panel and local clients are
    the whole expected audience. Reaching the UI from another machine is an explicit
    operator decision (--host 0.0.0.0 or an address/name) and turns on the mutation
    token for writes from off this Pi (SEC-01 layer 4)."""
    # Before anything on disk is touched: state files are owner-only from birth (SEC-05).
    _harden_umask()
    # The mutation token is a second factor for clients that are not on the Pi itself
    # (SEC-01 layer 4, with the SEC-02 loopback default). On loopback nobody needs it;
    # bound beyond loopback a token must exist -- generated, 0600 -- or writes would be
    # answerable to a whole LAN with no token at all.
    if host in _LOOPBACK_BINDS:
        APP.token = _load_token()
    else:
        APP.token = _ensure_token()
        print(f"Bound beyond loopback ({host}:{port}): writes from other machines need "
              f"the {_TOKEN_HEADER} header. Token: {APP.token}  "
              f"(also at {token_file()}, mode 0600)")
    APP.load_catalog()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"FM-1 fleet UI on http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0
