"""The HTTP side of the fleet UI: the request gates, the router, and every endpoint.

The gates (Host, Origin, Content-Type and the write token) run before any request body is
read. Endpoints call ``web_writes`` for anything that changes a unit or a backup, and
``web_state`` for what is shown.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, guided, hub, library, registration, registry, web_router, web_sets
from .backups import backup_space, backups_for
from .guided import WriteRefused
from .security import (_TOKEN_HEADER, Throttle, _host_name, _is_loopback_client, _origin_host,
                       host_allowed, token_file)
from .units import SLOTS, SLOT_LABELS
from .web_state import (APP, _PKG_CACHE, STATIC_DIR, _catalog_cached, _catalog_meta,
                        _sha256, _stat_key, _ui_build, getlist, update_for)
from . import clockstate, netctl, wifi_setup
from .web_writes import (_log_flash, backup_unit_now, delete_backup, finish_setup_by_id,
                         finish_setup_from_pi, flash_unit_by_id, recent_flashes,
                         restore_unit_by_id, setup_from_pi_possible)


# ------------------------------------------------------------------- request gates
#
# The API drives firmware writes and deletes backups, and the unit ids are no secret, so
# any web page an operator visits must not be able to drive it (audit §9 SEC-01, measured:
# a cross-origin text/plain POST was honoured, and a foreign Host got 200 on /api/fleet).
# Three hand-rolled, stdlib-only layers, applied in Handler._gate:
#   1. the Host header must name this server (DNS-rebinding pages must present the
#      attacker's name, so they can neither read nor write);
#   2. when a browser sends Origin, it must be one of this server's names;
#   3. mutation POSTs must carry Content-Type: application/json -- never a CORS-simple
#      type -- so a cross-origin POST forces a preflight, which this server never answers
#      with Access-Control-Allow-*: the browser itself blocks the request.
# Non-browser callers (curl, tests) send no Origin and a loopback Host, and pass.


#: Everything the UIs are made of. Source files are deliberately absent: the old static
#: check served this module's own source through a prefix confusion (audit §9 SEC-03,
#: measured: ``GET /../web.py`` returned 37,450 bytes of ``web.py``), and no page needs
#: it. Anything not in this set answers 404 even when it exists inside the web dir.
STATIC_FILES = frozenset({"fleet.html", "fleet.css", "fleet.js",
                          "index.html", "style.css", "app.js"})


def _json_ready(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


THROTTLE = Throttle()


def _clock_view() -> dict:
    """Is the Pi's clock trustworthy, and what time does it think it is? (clockstate.py)"""
    st = clockstate.status()
    return {"trusted": st.trusted, "synchronized": st.synchronized, "set_by_person": st.set_by_person,
            "reason": st.reason(), "now": st.now}


class Handler(BaseHTTPRequestHandler):
    server_version = "fm1-fleet/0.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if os.environ.get("FM1_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(_json_ready(payload)).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- request gates (audit §9 SEC-01) -----------------------------------
    def _gate(self, *, mutation: bool, skip_token: bool = False) -> tuple[int, str] | None:
        """The refusal for a request that did not come from this server's own UI, or None
        when it may proceed. GETs get the Host and Origin layers; a POST additionally
        needs an application/json body. Non-browser callers (curl, the test suites) pass:
        they send no Origin and a loopback (or no) Host."""
        host = self.headers.get("Host")
        if host and not host_allowed(_host_name(host)):
            return (HTTPStatus.MISDIRECTED_REQUEST,
                    f"nothing is served here for host {_host_name(host)!r}; the names "
                    "this server answers to belong in FM1_WEB_HOSTS")
        origin = self.headers.get("Origin")
        if origin and not host_allowed(_origin_host(origin)):
            return (HTTPStatus.FORBIDDEN,
                    f"cross-origin request refused (Origin: {origin})")
        if not mutation:
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return (HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "POST bodies must be application/json")
        if THROTTLE.blocked(str(self.client_address[0])):
            return (HTTPStatus.TOO_MANY_REQUESTS,
                    "too many wrong tokens from this address; try again in a few minutes")
        if not skip_token and not self._token_ok():
            return (HTTPStatus.UNAUTHORIZED,
                    f"this write needs the {_TOKEN_HEADER} header; the panel on the Pi "
                    "needs no token, and the value is printed when the server is bound "
                    f"beyond loopback (file {token_file()})")
        return None

    def _token_ok(self) -> bool:
        """Mutations from off-Pi clients need the token (loopback: the panel, trusted).

        Fails closed: if a LAN surface is up without a configured token, every mutation
        is refused rather than answered. GETs stay open so the UIs can render; the gates
        above already keep a browser page from reading by rebinding.
        """
        if _is_loopback_client(str(self.client_address[0])):
            return True
        required = APP.token
        if not required:
            return False
        presented = self.headers.get(_TOKEN_HEADER) or ""
        ip = str(self.client_address[0])
        good = bool(presented) and secrets.compare_digest(presented, required)
        if good:
            THROTTLE.ok(ip)
        elif presented:
            THROTTLE.miss(ip)
        return good

    # -- first-run hotspot passphrase (wifi_setup.py) ------------------------
    def _hotspot_gated(self) -> bool:
        """Is this request from a phone on the hotspot while the passphrase is still the default?
        Then it may only reach the set-a-passphrase form. Never raises: a failure here must not
        take the UI down for the panel or the house network."""
        try:
            local = self.connection.getsockname()[0]
            return wifi_setup.gate_active(str(local), str(self.client_address[0]))
        except Exception:  # noqa: BLE001
            return False

    def _api_clock_set(self, body: dict) -> None:
        """Set the Pi's clock from a phone's time. Guarded in clockstate.set_time."""
        if str(body.get("confirm") or "") != "clock":
            raise WriteRefused("confirmation does not match the request")
        try:
            out = clockstate.set_time(
                body.get("epoch"),
                busy=lambda: "a write is running" if APP.lock.locked() else clockstate.holds_busy())
        except clockstate.ClockError as exc:
            raise WriteRefused(str(exc)) from exc
        if out["changed"]:
            _log_flash(event="clock-set", message=out["message"], before=int(out["before"]),
                       after=int(out["after"]))
        return self._json({"ok": True, **out, "clock": _clock_view()})

    def _api_wifi_setup(self, body: dict) -> None:
        try:
            out = wifi_setup.apply_passphrase(str(body.get("passphrase") or ""))
        except wifi_setup.Refused as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except netctl.NetError as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR,
                               f"could not update the hotspot: {exc}")
        _log_flash(event="wifi-passphrase",
                   message="hotspot passphrase changed from the published default")
        return self._json(out)

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        deny = self._gate(mutation=False)
        if deny:
            return self._error(*deny)
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        gated = self._hotspot_gated()
        if path == "/setup-wifi":
            return self._send(200, wifi_setup.page(gated=gated), "text/html; charset=utf-8")
        if gated:        # a phone on the hotspot with the default passphrase sees only the form
            if path.startswith("/api/"):
                return self._error(HTTPStatus.FORBIDDEN,
                                   "choose a hotspot passphrase first: open /setup-wifi")
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/setup-wifi")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        # The root is the SAME screen the HDMI panel shows, so a phone or tablet that opens
        # http://<pi>/ gets the touch UI. The hub/library page lives at /expert.
        if path in ("/", "", "/fleet", "/fleet/"):
            return self._static("fleet.html")
        if path in ("/expert", "/expert/"):
            return self._static("index.html")
        if path == "/api/viewport":
            # Log what the kiosk browser actually rendered into. A wrong viewport silently
            # rescales the whole design, and the symptom (from a photo) looks like a CSS
            # bug rather than a wrong canvas size.
            vw = (query.get("vw") or [""])[0]
            vh = (query.get("vh") or [""])[0]
            dpr = (query.get("dpr") or [""])[0]
            scr = (query.get("screen") or [""])[0]
            line = f"viewport {vw}x{vh} dpr={dpr} screen={scr}"
            try:
                with open("/tmp/fm1-viewport.log", "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
            return self._json({"ok": True, "logged": line})
        if path == "/api/backups":
            return self._json({"items": backups_for((query.get("unit_id") or [""])[0]),
                               "space": backup_space()})
        if path == "/api/getlist":
            items = getlist()
            return self._json({"items": items, "attribution": hub.ATTRIBUTION,
                               "unavailable": _catalog_cached() is None,
                               "online": hub.online(), "offline_note": hub.OFFLINE_NOTE})
        if path == "/api/register/scan":
            return self._api_register_scan()
        if path == "/api/flashlog":
            return self._json({"items": recent_flashes()})
        if path == "/api/settings":
            return self._json(config.describe())
        if path == "/api/router":
            return self._json(web_router.overview())
        if path == "/api/clock":
            return self._json(_clock_view())
        if path == "/api/router/layouts":
            return self._json(web_router.layouts_overview())
        if path == "/api/sets":
            return self._json(web_sets.overview())
        if path == "/api/sets/preflight":
            only = [x for x in query.get("only", [""])[0].split(",") if x] or None
            return self._json({"steps": web_sets.preflight(
                query.get("name", [""])[0], only)})
        if path == "/api/fleet":
            return self._api_fleet()
        if path == "/api/fleet/package":
            return self._api_fleet_package(query)
        if path == "/api/state":
            return self._api_state()
        if path == "/api/units":
            return self._api_units(read_identity=query.get("probe", ["0"])[0] not in ("0", "false"))
        if path == "/api/catalog":
            return self._api_catalog(query)
        if path == "/api/library":
            return self._api_library()
        if path == "/api/package":
            return self._api_package(query)
        if path == "/api/verify":
            return self._api_verify()
        return self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        # The gates run before the body is read: a refused request costs the attacker
        # nothing but a small answer, and none of its bytes reach the parsers below.
        path = unquote(urlparse(self.path).path)
        gated = self._hotspot_gated()
        # A phone on the hotspot has no write token yet: while the passphrase is the default,
        # choosing one is the single write it may make without it.
        deny = self._gate(mutation=True, skip_token=gated and path == "/api/wifi/setup")
        if deny:
            return self._error(*deny)
        if gated and path != "/api/wifi/setup":
            return self._error(HTTPStatus.FORBIDDEN,
                               "choose a hotspot passphrase first: open /setup-wifi")
        body = self._body_json()
        try:
            if path == "/api/wifi/setup":
                return self._api_wifi_setup(body)
            if path == "/api/clock/set":
                return self._api_clock_set(body)
            if path == "/api/flash":
                return self._api_flash(body)
            if path == "/api/restore":
                return self._api_restore(body)
            if path == "/api/restart":
                return self._api_restart(body)
            if path == "/api/reconcile":
                return self._api_reconcile(body)
            if path == "/api/backup":
                return self._json({"ok": True, "event": backup_unit_now(
                    str(body.get("unit_id") or ""), str(body.get("confirm") or ""))})
            if path == "/api/backup/delete":
                return self._json(delete_backup(
                    str(body.get("unit_id") or ""), str(body.get("backup") or ""),
                    str(body.get("confirm") or "")))
            if path == "/api/rename":
                return self._api_rename(body)
            if path == "/api/setup/finish":
                finished = finish_setup_by_id(str(body.get("unit_id") or ""),
                                              str(body.get("confirm") or ""))
                unit_id = str(body.get("unit_id") or "")
                with APP.state_lock:       # the cached fleet must not say "unfinished" any more
                    for u in (APP.last_fleet or {}).get("units", []):
                        if u.get("unit_id") == unit_id:
                            u["setup_pending"] = None
                return self._json(finished)
            if path == "/api/setup/install":
                return self._json(finish_setup_from_pi(str(body.get("unit_id") or ""),
                                                       str(body.get("confirm") or "")))
            if path == "/api/register/bind":
                return self._api_register_bind(body)
            if path == "/api/download":
                return self._api_download(body)
            if path == "/api/catalog/refresh":
                before = hub.catalog_path().stat().st_mtime if hub.catalog_path().is_file() else None
                APP.load_catalog(force=True)
                after = hub.catalog_path().stat().st_mtime if hub.catalog_path().is_file() else None
                _log_flash(event="catalog-refresh", fetched=after != before)
                return self._api_catalog({})
            if path == "/api/library/remove":
                return self._api_library_remove(body)
            if path == "/api/settings":
                return self._api_settings(body)
            if path.startswith("/api/router/"):
                return self._api_router(path, body)
            if path.startswith("/api/sets/"):
                return self._api_sets(path, body)
        except WriteRefused as exc:
            if not getattr(exc, "logged", False):
                # Refused before any write was attempted (bad confirm, sha drift, bad name,
                # wrong backup...). Every 409 leaves a line (audit SAFE-05).
                _log_flash(event="refused", stage="request", endpoint=path,
                           unit_id=str(body.get("unit_id") or "") or None,
                           package=str(body.get("package") or body.get("backup") or "") or None,
                           message=str(exc))
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except hub.HubOffline as exc:
            # Not a fault: the Pi has no internet. 503 with a flag the UIs show as a plain note.
            return self._json({"error": str(exc), "offline": True}, HTTPStatus.SERVICE_UNAVAILABLE)
        except hub.HubError as exc:
            return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    # -- endpoints ---------------------------------------------------------
    def _api_fleet(self) -> None:
        """The fleet in the registry's terms: identity by flash id, not by port.

        The older /api/state is port-map based and reports every running unit as absent,
        because it looks for a /dev/fm1-* symlink that only exists in download mode. This
        endpoint answers the question the touch UI actually asks: which units are here,
        what are they, and what can be done with each.
        """
        # While a write is running the scan would read whole flashes over the very device
        # being written. Serve the last answer instead; the UI is told it is busy.
        # The scan runs under the flash lock, taken without waiting: if a write (or a
        # restart, which sets no busy text) holds it we answer from the cache instead.
        if not APP.lock.acquire(blocking=False):
            return self._json(self._fleet_reply(cached=True))
        try:
            known = registry.known()
        finally:
            APP.lock.release()

        units = []
        for u in known:
            units.append({
                "slot": u.slot,
                "label": u.label,
                "name": u.name,
                "unit_id": u.unit_id,
                "running": u.running,
                "download": u.in_download_mode,
                "present": u.present,
                "provisional": u.provisional,
                "hint": u.hint,
                "chip": u.chip,
                "chip_kind": ("new" if "B36014" in (u.chip or "").upper()
                              else "old" if "856014" in (u.chip or "").upper()
                              else None),
                "firmware": u.firmware,
                "port": u.port,
                "device": u.device,
                "error": u.error,
                "update": update_for(u.firmware, _catalog_cached()),
                "setup_pending": u.setup_pending,
                "setup_from_pi": setup_from_pi_possible(u.setup_pending),
            })
        with APP.state_lock:
            APP.last_fleet = {"units": units, "labels": {s: SLOT_LABELS[s] for s in SLOTS},
                              "build": _ui_build()}
        self._json(self._fleet_reply())

    def _fleet_reply(self, *, cached: bool = False) -> dict:
        with APP.state_lock:
            fleet = APP.last_fleet
            if fleet is None:
                # Nothing cached and the device is busy: say so rather than read it.
                fleet = {"units": [], "labels": {s: SLOT_LABELS[s] for s in SLOTS},
                         "build": _ui_build()}
            return {**fleet, "busy": APP.busy or ("Working on a unit…" if cached else None),
                    "last_event": APP.last_event, "now": time.time(),
                    "log_error": APP.log_error, "clock": _clock_view()}

    def _api_fleet_package(self, query: dict) -> None:
        """One package, with its compatibility judged against a given unit's chip."""
        from .compat import compat_for

        name = (query.get("name") or [""])[0]
        chip = (query.get("chip") or [None])[0] or None
        path = library.library_dir() / name
        if not name or path.parent.resolve() != library.library_dir().resolve():
            return self._error(HTTPStatus.BAD_REQUEST, "bad package name")
        if not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "no such package")
        key = _stat_key(path) + (chip,)
        body = _PKG_CACHE.get(key)
        if body is None:
            ins = APP.inspect_package(path)
            severity, note = compat_for(path, chip)
            body = {
                "name": name, "sha256": _sha256(path), "identity": ins.identity,
                "severity": ins.severity, "headline": ins.headline,
                "details": list(ins.details),
                "compat": {"severity": severity, "note": note},
            }
            if len(_PKG_CACHE) > 400:
                _PKG_CACHE.clear()
            _PKG_CACHE[key] = body
        self._json(body)

    def _api_state(self) -> None:
        views = APP.unit_views_with_maybes()
        self._json({
            "units": [asdict(v) for v in views],
            "busy": APP.busy,
            "last_event": APP.last_event,
            "catalog": _catalog_meta(APP.catalog, APP.catalog_error),
            "clock": _clock_view(),
            "attribution": hub.ATTRIBUTION,
        })

    def _api_units(self, *, read_identity: bool) -> None:
        APP.refresh_units(read_identity=read_identity)
        self._api_state()

    def _api_catalog(self, query: dict) -> None:
        cat = APP.catalog or APP.load_catalog()
        if cat is None:
            return self._json(
                {"error": APP.catalog_error or "catalogue unavailable", "entries": []}, 200
            )
        q = (query.get("q") or [""])[0]
        kind = (query.get("kind") or [""])[0] or None
        chip = (query.get("chip") or [""])[0] or None
        entries = cat.search(q, kind)
        if chip == "saves-new":
            entries = [e for e in entries if e.chip == "new"]
        entries = sorted(entries, key=lambda e: (-e.stars, e.name.lower()))
        self._json({
            "entries": [asdict(e) | {"kind_label": e.kind_label,
                                     "installable_by_us": e.installable_by_us,
                                     "package_url": e.package_url} for e in entries],
            "total": len(cat.entries),
            "meta": _catalog_meta(cat, APP.catalog_error),
            "attribution": hub.ATTRIBUTION,
            "kinds": [{"id": k, "label": hub.KIND_LABELS[k]} for k in hub.KINDS],
        })

    def _api_library(self) -> None:
        items = []
        for pkg in library.library():
            ins = APP.inspect_package(pkg.path)
            entry = APP.catalog.by_id(pkg.path.stem.split("-")[0]) if APP.catalog else None
            items.append({
                "name": pkg.name,
                "path": str(pkg.path),
                "size": pkg.size,
                "sha256": pkg.digest,
                "severity": ins.severity,
                "headline": ins.headline,
                "details": ins.details,
                "identity": ins.identity,
                "files": list(ins.files),
                "catalog_id": entry.id if entry else None,
                "catalog_match": bool(entry and entry.sha256 == pkg.digest),
            })
        self._json({"items": items, "library_dir": str(library.library_dir())})

    def _api_package(self, query: dict) -> None:
        name = (query.get("name") or [""])[0]
        path = library.library_dir() / name
        if not name or path.parent.resolve() != library.library_dir().resolve():
            return self._error(HTTPStatus.BAD_REQUEST, "bad package name")
        if not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "no such package")
        ins = APP.inspect_package(path)
        self._json({
            "name": name, "sha256": _sha256(path), "size": path.stat().st_size,
            "severity": ins.severity, "headline": ins.headline, "details": ins.details,
            "identity": ins.identity,
            "files": list(ins.files),
        })

    def _api_verify(self) -> None:
        """Re-hash every library package against the catalogue. Slow, on demand."""
        cat = APP.catalog or APP.load_catalog()
        out = []
        for pkg in library.library():
            entry = None
            if cat:
                entry = next((e for e in cat.entries if e.sha256 == pkg.digest), None)
            out.append({
                "name": pkg.name, "sha256": pkg.digest,
                "catalog_id": entry.id if entry else None,
                "catalog_version": entry.version if entry else None,
                "verified": bool(entry),
            })
        self._json({"packages": out})

    def _api_flash(self, body: dict) -> None:
        name = str(body.get("package") or "")
        confirm = str(body.get("confirm") or "")
        sha = str(body.get("sha256") or "")
        if "/" in name or "\\" in name or not name:
            raise WriteRefused("bad package name")
        if body.get("unit_id"):
            # The touch panel: identity is the flash id.
            event = flash_unit_by_id(str(body["unit_id"]), library.library_dir() / name,
                                     sha, confirm)
            return self._json({"ok": True, "event": event})
        raise WriteRefused("a flash must name the unit by its flash id (unit_id)")

    def _api_restore(self, body: dict) -> None:
        unit_id = str(body.get("unit_id") or "")
        event = restore_unit_by_id(unit_id, str(body.get("backup") or ""),
                                   str(body.get("confirm") or ""))
        self._json({"ok": True, "event": event})

    def _api_rename(self, body: dict) -> None:
        unit_id = str(body.get("unit_id") or "")
        if str(body.get("confirm") or "") != unit_id:
            raise WriteRefused("confirmation does not match the selected unit")
        try:
            name = registry.rename(unit_id, str(body.get("name") or ""))
        except (ValueError, KeyError) as exc:
            raise WriteRefused(str(exc).strip("'\"")) from exc
        _log_flash(event="rename", unit_id=unit_id, message=name or "(cleared)")
        with APP.state_lock:       # keep the cache, with the new name: a write may be running
            for u in (APP.last_fleet or {}).get("units", []):
                if u.get("unit_id") == unit_id:
                    u["name"] = name or None
        self._json({"ok": True, "name": name})

    def _api_restart(self, body: dict) -> None:
        unit_id = str(body.get("unit_id") or "")
        if str(body.get("confirm") or "") != unit_id:
            raise WriteRefused("confirmation does not match the selected unit")
        def phase(text: str) -> None:
            with APP.state_lock:
                APP.busy = text
        try:
            out = guided.restart_unit(unit_id, lock=APP.lock, on_phase=phase)
        finally:
            if APP.lock.acquire(blocking=False):
                with APP.state_lock:
                    APP.busy = None
                APP.lock.release()
        _log_flash(event="restart", unit_id=unit_id, device=out.device, message=out.message)
        self._json({"ok": True, "message": out.message})

    def _api_reconcile(self, body: dict) -> None:
        """Match every registered sticker to its unit again after a reconnect: the units the
        registry cannot place are put into download mode, read by flash id, restarted and
        their port and fingerprint re-learned. Metadata only; never writes flash."""
        if str(body.get("confirm") or "") != "reconcile":
            raise WriteRefused("confirmation does not match the request")
        def phase(text: str) -> None:
            with APP.state_lock:
                APP.busy = text
        try:
            out = guided.reconcile(lock=APP.lock, on_phase=phase, everything=bool(body.get("all")))
        finally:
            if APP.lock.acquire(blocking=False):
                with APP.state_lock:
                    APP.busy = None
                APP.lock.release()
        _log_flash(event="reconcile", message=out.message,
                   matched=[s for s, _, _ in out.matched])
        self._json({"ok": True, "message": out.message,
                    "matched": [{"slot": s, "unit_id": i, "port": p} for s, i, p in out.matched],
                    "unregistered": out.unregistered, "missing": out.missing,
                    "stuck": out.stuck})

    def _api_settings(self, body: dict) -> None:
        """Change settings in the config file. ``{"values": {key: value-or-null}}``; null
        puts a key back to its default. All or nothing, and every change leaves an audit
        line. Nothing here touches a unit; what a setting can reach is decided in
        ``config`` (the flasher binary and the hub address are refused from here)."""
        values = body.get("values")
        try:
            result = config.save(values if isinstance(values, dict) else {})
        except config.ConfigError as exc:
            _log_flash(event="refused", stage="request", endpoint="/api/settings",
                       message=str(exc))
            return self._error(HTTPStatus.CONFLICT, str(exc))
        if result["changed"]:
            _log_flash(event="settings", message=", ".join(
                f"{k}: {old} -> {new}" for k, (old, new) in result["changed"].items()))
        return self._json({"ok": True, **result, **config.describe()})

    def _api_router(self, path: str, body: dict) -> None:
        """The routes page. Edits the saved patches and the choice of active patch (files the
        router follows); panic sends all-notes-off. Nothing here writes to a unit's flash."""
        text = lambda k: str(body.get(k) or "")  # noqa: E731
        if path == "/api/router/route":
            return self._json({"ok": True, "route": web_router.add_route(
                text("patch"), body.get("route"))})
        if path == "/api/router/route/delete":
            return self._json({"ok": True, "route": web_router.remove_route(
                text("patch"), body.get("index"), text("confirm"))})
        if path == "/api/router/patch/delete":
            web_router.delete_patch(text("name"), text("confirm"))
            return self._json({"ok": True})
        if path == "/api/router/activate":
            web_router.activate(text("name"), text("confirm"))
            return self._json({"ok": True})
        if path == "/api/router/layouts/units":
            web_router.save_units(body.get("units"))
            return self._json({"ok": True, **web_router.layouts_overview()})
        if path == "/api/router/layouts/save":
            web_router.save_layout(body.get("layout"))
            return self._json({"ok": True, **web_router.layouts_overview()})
        if path == "/api/router/layouts/delete":
            web_router.delete_layout(text("name"), text("confirm"))
            return self._json({"ok": True, **web_router.layouts_overview()})
        if path == "/api/router/layouts/apply":
            patch = web_router.apply_layout(text("name"), text("confirm"))
            return self._json({"ok": True, "patch": patch, **web_router.layouts_overview()})
        if path == "/api/router/clock":
            return self._json({"ok": True, "clock": web_router.set_clock(
                {**body, "confirm": text("confirm")})})
        if path == "/api/router/panic":
            # Not while a flash runs: the units are in download mode or about to be.
            if not APP.lock.acquire(blocking=False):
                raise WriteRefused("a write is running; panic is not sent during one")
            try:
                sent = web_router.panic(text("confirm"))
            finally:
                APP.lock.release()
            return self._json({"ok": True, "sent": sent})
        return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _api_sets(self, path: str, body: dict) -> None:
        """Rig sets. save/delete edit only the sets file; push writes firmware, one unit at a
        time, through the same id-matched, backup-first paths as a single-unit write, and
        returns at once (the panel polls GET /api/sets)."""
        text = lambda k: str(body.get(k) or "")  # noqa: E731
        if path == "/api/sets/save":
            return self._json({"ok": True, "set": web_sets.save_set(
                text("name"), text("note"), body.get("members"))})
        if path == "/api/sets/snapshot":
            return self._json({"ok": True, "set": web_sets.snapshot(text("name"))})
        if path == "/api/sets/delete":
            web_sets.delete_set(text("name"), text("confirm"))
            return self._json({"ok": True})
        if path == "/api/sets/push":
            only = body.get("only")
            if only is not None and (not isinstance(only, list)
                                     or not all(isinstance(x, str) for x in only)):
                raise WriteRefused("only must be a list of unit stickers")
            return self._json({"ok": True, "push": web_sets.start_push(
                text("name"), only, text("confirm"))})
        return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _api_register_scan(self) -> None:
        """What the REGISTER view shows: every download-mode unit, server-read, with the
        reason REGISTER cannot proceed right now (if it cannot). See registration."""
        self._json(registration.scan_download_mode(lock=APP.lock))

    def _api_register_bind(self, body: dict) -> None:
        """One deliberate act: bind the flash id to the letter REGISTER proposes.

        The identity is re-read by the server during this request (registration reads
        download mode under the flash lock); the client's ``confirm`` must equal the id
        being bound -- the same pattern every write uses -- but the id itself is never
        taken from the client: if no device on the bus reports it now, the request is
        refused. This records metadata only; the write path is untouched.
        """
        unit_id = str(body.get("unit_id") or "")
        try:
            binding = registration.bind_from_scan(unit_id, str(body.get("confirm") or ""),
                                                  lock=APP.lock)
        except registration.AttributionError as exc:
            raise WriteRefused(str(exc)) from exc
        _log_flash(event="bind", unit_id=unit_id, message=binding.label)
        # (no cache to clear: bind runs under the flash lock, so no write is running, and
        # the next poll scans afresh)
        self._json({"ok": True, "slot": binding.slot, "label": binding.label,
                    "unit_id": binding.unit_id})

    def _api_download(self, body: dict) -> None:
        if APP.catalog is None:
            APP.load_catalog()
        if APP.catalog is None:
            raise hub.HubUnavailable(APP.catalog_error or "no catalogue")
        entry = APP.catalog.by_id(str(body.get("id") or ""))
        if entry is None:
            raise hub.PackageDownloadError("no such firmware in the catalogue")
        path = hub.fetch_package(entry, library.library_dir())
        self._json({"ok": True, "path": str(path), "name": path.name,
                    "sha256": _sha256(path)})

    def _api_library_remove(self, body: dict) -> None:
        name = str(body.get("name") or "")
        path = library.library_dir() / name
        if not name or path.parent.resolve() != library.library_dir().resolve():
            raise WriteRefused("bad package name")
        path.unlink(missing_ok=True)
        self._json({"ok": True, "removed": name})

    # -- static ------------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        # Containment, the way _backup_by_id_path does it: the target must sit INSIDE the
        # web directory (be one of its parents' children), not merely start with its path
        # -- "…/fm1_switch/web.py".startswith("…/fm1_switch/web") is true, which is how
        # GET /../web.py served the source (audit SEC-03). The allowlist then keeps even
        # in-tree files that are not pages (this module's source) from being served.
        if (STATIC_DIR.resolve() not in target.parents
                or target.name not in STATIC_FILES
                or not target.is_file()):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)
