"""Every act that changes a unit or a saved image: a firmware write, a restore, a hand backup,
and a delete. Each one must be confirmed by flash id (``confirm`` equals the unit id), and each
leaves an audit line (``_log_flash``) for the request, the refusal or the outcome.

The HTTP side is ``web_http``; the device work is ``guided`` and ``mflasher``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import clockstate, config, guided, library, maker_install, mflasher, packages, registry
from .backups import backups_dir, prune_auto_backups
from .guided import WriteFailed, WriteRefused
from .web_state import APP, _sha256


def _check_package(detail: Path, expected_sha: str) -> Path:
    """Refuse unless the package is in the library and still has the sha256 the user saw."""
    if not detail.is_file():
        raise WriteRefused("package file is gone")
    if detail.parent.resolve() != library.library_dir().resolve():
        raise WriteRefused("package is not in the library")
    if not expected_sha:
        raise WriteRefused("no sha256 was sent, so there is nothing to confirm the package "
                           "against; refresh the page and confirm again")
    actual = _sha256(detail)
    if actual != expected_sha:
        raise WriteRefused(
            "the package changed since you confirmed it (sha256 differs); refresh and "
            "confirm again"
        )
    return detail


def flash_log_path() -> Path:
    return config.path("flashlog")


def recent_flashes(limit: int = 8) -> list[dict]:
    """The newest finished flash/restart/refusal records, newest first. A request line with
    no outcome is skipped; unreadable lines are ignored."""
    try:
        lines = flash_log_path().read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") in ("done", "refused", "failed", "restart",
                                                          "bind"):
            out.append({k: rec.get(k) for k in ("at", "event", "unit_id", "package", "message")})
            if len(out) >= limit:
                break
    return out


def _log_flash(**fields) -> None:
    """One JSON line per flash request and outcome. The Pi's journal does not persist, and
    "it flashed the wrong thing" needs a record of what was actually asked."""
    try:
        with flash_log_path().open("a", encoding="utf-8") as fh:
            line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
            if not clockstate.status().trusted:     # say so: the date on this line may be wrong
                line["clock"] = "unsynced"
            fh.write(json.dumps(line) + "\n")
        APP.log_error = None
    except OSError as exc:
        APP.log_error = f"the audit log could not be written: {exc}"


def _run_guided(unit_id: str, name: str, sha: str, package: str, **kw) -> dict:
    """One id-matched write (firmware, restore or backup) with the shared bookkeeping:
    the log, the panel's busy phase, and ``last_event``. ``kw`` goes to ``guided.flash``."""
    _log_flash(event="request", unit_id=unit_id, package=name, sha256=sha)

    def phase(text: str) -> None:
        with APP.state_lock:
            APP.busy = text

    try:
        out = guided.flash(unit_id, package, lock=APP.lock, on_phase=phase, **kw)
    except (WriteRefused, mflasher.MflasherError) as exc:
        failed = isinstance(exc, WriteFailed)
        if not isinstance(exc, guided.Busy):
            # "Another flash is running" is about the *other* request. Recording it here
            # would overwrite the running write's context (audit R2).
            with APP.state_lock:
                APP.last_event = {"at": time.time(), "unit_id": unit_id, "package": name,
                                  "ok": False, "message": str(exc),
                                  **({"stage": "write"} if failed else {})}
        _log_flash(event="failed" if failed else "refused", unit_id=unit_id, package=name,
                   message=str(exc), stage="write" if failed else "guided")
        err = (WriteFailed if failed else guided.Busy if isinstance(exc, guided.Busy)
               else WriteRefused)(str(exc))
        err.logged = True            # do_POST must not log this refusal a second time
        raise err from exc
    finally:
        if APP.lock.acquire(blocking=False):   # only clear busy if no other write holds it
            with APP.state_lock:
                APP.busy = None
            APP.lock.release()
    with APP.state_lock:
        APP.last_event = {"at": time.time(), "unit_id": unit_id, "package": name,
                          "ok": True, "message": out.message, "restarted": out.restarted,
                          "stuck": out.stuck, "relearned": out.relearned}
    _log_flash(event="done", unit_id=unit_id, package=name, device=out.device,
               message=out.message, restarted=out.restarted, stuck=out.stuck)
    return dict(APP.last_event)


def flash_unit_by_id(unit_id: str, package_path: Path, expected_sha: str, confirm: str) -> dict:
    """The touch panel's write path: the unit is named by its FLASH ID and the write goes
    to the device whose flash reports that id (see ``guided``). Puts the unit into
    download mode itself, and restarts any other unit that went with it."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")
    detail = _check_package(package_path, expected_sha)
    two_step = packages.inspect(detail).severity != "ok"
    try:
        event = _run_guided(unit_id, detail.name, expected_sha, str(detail),
                            firmware_label=detail.stem,
                            before_write=lambda node: _backup_before_write(node, unit_id))
    except WriteFailed:
        # A partial write of a two-step package leaves the unit unfinished too.
        if two_step:
            _note_setup(unit_id, detail.name, expected_sha)
        raise
    if two_step:
        _note_setup(unit_id, detail.name, expected_sha)
    else:
        _clear_setup(unit_id)
    return event


def _note_setup(unit_id: str, package: str, sha: str) -> None:
    """The write was a two-step firmware: the unit is unfinished until the operator says so.
    Recorded even when the sticker is unknown to the registry, so the log still shows it."""
    recorded = registry.set_setup_pending(unit_id, package, sha)
    _log_flash(event="setup-pending", unit_id=unit_id, package=package, sha256=sha,
               message=("setup unfinished: finish with the maker's installer" if recorded
                        else "setup unfinished, but this unit has no sticker to record it on"))


def _clear_setup(unit_id: str) -> None:
    """An ordinary firmware replaced whatever was on the unit, so an earlier unfinished
    setup no longer applies to it. Logged only when there was something to clear."""
    cleared = registry.clear_setup_pending(unit_id)
    if cleared is not None:
        _log_flash(event="setup-replaced", unit_id=unit_id, package=cleared["package"],
                   message="replaced by another firmware")


def finish_setup_by_id(unit_id: str, confirm: str) -> dict:
    """The operator confirms that the maker's installer has finished on this unit. Only
    clears a flag that exists; the confirm must name the unit's flash id. Nothing is written
    to the unit."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")
    cleared = registry.clear_setup_pending(unit_id)
    if cleared is None:
        raise WriteRefused("this unit has no unfinished setup to finish")
    _log_flash(event="setup-finished", unit_id=unit_id, package=cleared["package"],
               message="maker's installer step confirmed by the operator")
    return {"ok": True, "package": cleared["package"]}


def setup_from_pi_possible(pending: dict | None) -> bool:
    """Can the Pi run the maker's installer for this unfinished setup? (family tried on a real
    unit, tools present). The panel shows the button only when true."""
    return bool(pending) and maker_install.family_for(str(pending.get("package") or "")) is not None \
        and maker_install.available()


def _sleep(seconds: float) -> None:        # one place to stub in tests
    time.sleep(seconds)


def _wait_for_unit(client: int, tries: int = 20) -> dict:
    """The unit answering again after a backup restart or the install's restart."""
    last = "no answer"
    for _ in range(tries):
        try:
            return maker_install.probe(client)
        except maker_install.MakerError as exc:
            last = str(exc)
            _sleep(3)
    raise WriteFailed(f"the unit did not come back: {last}")


def finish_setup_from_pi(unit_id: str, confirm: str) -> dict:
    """Run the maker's own installer for an unfinished two-step firmware, from the Pi.

    Guards, in order (each refusal leaves an audit line): the confirm names the unit's flash id;
    the unit has an unfinished setup; its family was tried on real hardware; the package is the
    one that was written (sha256); every unit is running (none in download or update mode); the
    unit's ALSA port is resolved from its USB path; and the running unit reports that firmware.
    Then a whole-flash backup is saved, the maker's installer runs, and the flag is cleared
    **only** when the unit reports the part present afterwards. Otherwise the flag stays."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")
    with APP.state_lock:
        units = [dict(u) for u in (APP.last_fleet or {}).get("units", [])]
    unit = next((u for u in units if u.get("unit_id") == unit_id), None)
    if unit is None:
        raise WriteRefused("this unit is not in the fleet right now; refresh and try again")
    pending = unit.get("setup_pending")
    if not pending:
        raise WriteRefused("this unit has no unfinished setup to finish")
    package = str(pending.get("package") or "")
    family = maker_install.family_for(package)
    if family is None:
        raise WriteRefused("the Pi cannot finish this firmware's setup yet; use the maker's installer")
    if not maker_install.available():
        raise WriteRefused("the maker's installer is not set up on this Pi")
    path = _check_package(library.library_dir() / package, str(pending.get("sha256") or ""))
    if any(u.get("download") or not u.get("running") for u in units):
        raise WriteRefused("every unit must be running before this: one is off or in download mode")
    client = maker_install.alsa_client_for_usb_path(str(unit.get("port") or ""))
    if client is None:
        raise WriteRefused("could not find this unit's USB-MIDI port")
    try:
        info = maker_install.probe(client)
    except maker_install.MakerError as exc:
        raise WriteRefused(str(exc)) from exc
    if family.reports not in str(info.get("firmware") or "").upper():
        raise WriteRefused(f"this unit does not report {family.reports} "
                           f"(it says {info.get('firmware') or 'nothing'})")
    if info.get("bt") == family.done_bt:
        return _setup_finished(unit_id, package, "already complete: the unit reports its part present")
    _log_flash(event="request", unit_id=unit_id, package=package, sha256=pending.get("sha256"),
               message="finish setup from the Pi")
    backup_unit_now(unit_id, unit_id)                 # raises, and nothing is installed, if it fails
    _wait_for_unit(client)
    if not APP.lock.acquire(blocking=False):
        raise guided.Busy("another device operation is running")
    try:
        def phase(text: str) -> None:
            with APP.state_lock:
                APP.busy = text
        phase("Installing from the Pi…")
        try:
            maker_install.run_install(path, client, phase)
        except maker_install.MakerError as exc:
            _log_flash(event="failed", unit_id=unit_id, package=package, message=str(exc),
                       stage="maker-install")
            with APP.state_lock:
                APP.last_event = {"at": time.time(), "unit_id": unit_id, "package": package,
                                  "ok": False, "message": str(exc), "stage": "write"}
            err = WriteFailed(str(exc))
            err.logged = True
            raise err from exc
        phase("Checking the result…")
        after = _wait_for_unit(client)
    finally:
        with APP.state_lock:
            APP.busy = None
        APP.lock.release()
    if after.get("bt") != family.done_bt:
        msg = "installed, but the unit still reports its part missing; setup stays unfinished"
        _log_flash(event="failed", unit_id=unit_id, package=package, message=msg,
                   stage="maker-verify")
        err = WriteFailed(msg)
        err.logged = True
        raise err
    return _setup_finished(unit_id, package, "installed from the Pi; the unit reports its part present")


def _setup_finished(unit_id: str, package: str, why: str) -> dict:
    cleared = registry.clear_setup_pending(unit_id)
    _log_flash(event="setup-finished", unit_id=unit_id, package=package, message=why)
    with APP.state_lock:
        for u in (APP.last_fleet or {}).get("units", []):
            if u.get("unit_id") == unit_id:
                u["setup_pending"] = None
        APP.last_event = {"at": time.time(), "unit_id": unit_id, "package": package,
                          "ok": True, "message": why, "restarted": [], "stuck": [], "relearned": None}
    return {"ok": True, "package": package, "cleared": cleared is not None, "message": why}


def restore_unit_by_id(unit_id: str, backup_id: str, confirm: str) -> dict:
    """Write a saved whole-flash image back to the unit whose flash id is ``unit_id``.

    Same id-matched path as a firmware write (see ``guided``). Refused unless the image's
    own sidecar names this unit and its checksum still matches, so a backup of one unit can
    never be written to another."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")
    image = _backup_by_id_path(unit_id, backup_id)
    info = json.loads(image.with_suffix(".json").read_text(encoding="utf-8"))
    if str(info.get("sha256", "")) != _sha256(image):
        raise WriteRefused("that backup does not match its checksum; it may be damaged")
    label = str(info.get("firmware", "")) or image.stem
    return _run_guided(
        unit_id, f"restore {image.name} ({label})", info["sha256"], str(image),
        firmware_label=label,
        before_write=lambda node: _backup_before_write(node, unit_id),
        writer=lambda node, pkg: mflasher.restore_image(node, pkg, keep_bootloader=True))


def _backup_by_id_path(unit_id: str, backup_id: str) -> Path:
    """The .bin for a backup of THIS unit, or a refusal. Shared by delete and restore-style
    checks: inside the backups dir, with a record that names the unit."""
    root = backups_dir().resolve()
    image = (root / backup_id).resolve()
    if root not in image.parents or image.suffix != ".bin" or not image.is_file():
        raise WriteRefused("that backup does not exist")
    try:
        info = json.loads(image.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WriteRefused("that backup has no readable record, so it cannot be checked")
    if str(info.get("unit id", "")).lower() != unit_id.lower():
        raise WriteRefused("that backup belongs to a different unit")
    return image


def backup_unit_now(unit_id: str, confirm: str) -> dict:
    """Save the unit's whole flash by hand: put it in download mode (matched by id), copy it
    to <backups>/manual/, and restart it. Nothing is written to the unit."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")

    def save_and_restart(node: str, _pkg: str) -> None:
        with APP.state_lock:
            APP.busy = "Saving a backup…"
        target = backups_dir() / "manual"
        target.mkdir(parents=True, exist_ok=True)
        try:
            mflasher.backup_unit(node, str(target))
        finally:
            try:
                mflasher.restart(node)     # a failed copy must not strand it in download mode
            except mflasher.MflasherError:
                pass

    return _run_guided(unit_id, "backup", "", "", writer=save_and_restart)


def delete_backup(unit_id: str, backup_id: str, confirm: str) -> dict:
    """Remove one saved image (and its record). Only an image whose record names this unit."""
    if confirm != unit_id:
        raise WriteRefused("confirmation does not match the selected unit")
    image = _backup_by_id_path(unit_id, backup_id)
    image.unlink(missing_ok=True)
    image.with_suffix(".json").unlink(missing_ok=True)
    _log_flash(event="backup-deleted", unit_id=unit_id, package=image.name)
    return {"ok": True, "removed": image.name}


def _backup_before_write(node: str, unit_id: str | None = None) -> None:
    """Pre-write safety copy, kept under <backups>/auto/ where RESTORE finds it.

    m-flasher's exit code is not trusted (audit SAFE-04): a new .bin with a .json record
    must have appeared, the record must carry the sha256 of that image and, when
    ``unit_id`` is given, name that unit. Otherwise this raises and nothing is written."""
    target = backups_dir() / "auto"
    target.mkdir(parents=True, exist_ok=True)
    before = {p: p.stat().st_mtime_ns for p in target.glob("*.json")}
    mflasher.backup_unit(node, str(target))
    if not _fresh_backup_ok(target, before, unit_id):
        raise mflasher.MflasherError(
            "m-flasher finished but no matching, checksummed backup file appeared")
    try:
        prune_auto_backups(target)
    except OSError:
        pass   # a failed tidy must never stop a write


def _fresh_backup_ok(target: Path, before: dict, unit_id: str | None) -> bool:
    for meta in target.glob("*.json"):
        if before.get(meta) == meta.stat().st_mtime_ns:
            continue                                   # not written by this backup
        image = meta.with_suffix(".bin")
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            if not isinstance(info, dict) or not image.is_file() or image.stat().st_size == 0:
                continue
            if str(info.get("sha256", "")) != _sha256(image):
                continue
            if unit_id and str(info.get("unit id", "")).lower() != unit_id.lower():
                continue
            return True
        except (OSError, ValueError):
            continue
    return False
