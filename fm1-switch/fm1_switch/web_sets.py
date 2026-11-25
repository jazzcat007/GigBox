"""The sets page's server side: saved rig sets and the one-button push.

Saving, listing and deleting a set only edits ``~/fm1-sets.json``. A *push* writes firmware
to units, so it goes through the same id-matched, backup-first paths as the single-unit panel
(``web_writes.flash_unit_by_id`` and ``restore_unit_by_id``). It runs in a background thread,
because four writes take minutes, and the panel polls the status in ``overview``.

A set is optional: nothing here changes how one unit is flashed on its own.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from . import backups, compat, library, registry, rigsets
from .guided import WriteRefused
from .rigsets import SetError
from .units import SLOT_LABELS, SLOTS
from .web_state import APP, _sha256
from .web_writes import _log_flash, flash_unit_by_id, restore_unit_by_id

_PUSH_LOCK = threading.Lock()
_PUSH: dict = {"running": False, "set": "", "steps": [], "started": 0.0, "finished": 0.0,
               "ok": None, "message": ""}


def push_status() -> dict:
    with _PUSH_LOCK:
        return {**_PUSH, "steps": [dict(s) for s in _PUSH["steps"]]}


def live_units() -> dict[str, dict]:
    """slot -> {unit_id, chip, firmware, name}, as the registry reports the fleet now."""
    out: dict[str, dict] = {}
    for u in registry.known():
        if u.slot in SLOTS and u.slot not in out:
            out[u.slot] = {"unit_id": u.unit_id, "chip": u.chip, "firmware": u.firmware,
                           "name": u.name}
    return out


def _backup_exists(unit_id: str, backup_id: str) -> bool:
    return any(b["id"] == backup_id for b in backups.backups_for(unit_id, limit=400))


def _store(path: Path | None) -> rigsets.SetStore:
    try:
        return rigsets.SetStore.load(path)
    except SetError as exc:
        raise WriteRefused(str(exc)) from exc


def overview(*, store_file: Path | None = None,
             units_fn: Callable[[], dict[str, dict]] | None = None) -> dict:
    error = ""
    try:
        sets = rigsets.SetStore.load(store_file).sets
    except SetError as exc:
        sets, error = {}, str(exc)
    units = (units_fn or live_units)()
    return {
        "slots": [{"slot": s, "label": SLOT_LABELS[s], **units.get(s, {})} for s in SLOTS],
        "library": [{"name": p.name, "sha256": _sha256(p.path)} for p in library.library()],
        "sets": [{**rs.to_dict(), "labels": {m.slot: m.label for m in rs.members}}
                 for _, rs in sorted(sets.items())],
        "push": push_status(),
        "error": error,
    }


def save_set(name: str, note: str, picks, *, store_file: Path | None = None,
             units_fn: Callable[[], dict[str, dict]] | None = None) -> dict:
    """Make or replace a set. ``picks`` is a list of ``{"slot", "package"}`` (a library file,
    pinned by its current sha256) or ``{"slot", "backup"}`` (a saved image id, or ``"newest"``
    for that unit's latest). Writes only the sets file."""
    if not isinstance(picks, list) or not picks:
        raise WriteRefused("pick at least one unit")
    units = (units_fn or live_units)()
    members: list[rigsets.Member] = []
    try:
        for pick in picks:
            if not isinstance(pick, dict):
                raise SetError("each pick must be an object")
            slot = str(pick.get("slot") or "")
            if pick.get("package"):
                pkg = library.library_dir() / str(pick["package"])
                if not pkg.is_file():
                    raise SetError(f"{pick['package']} is not in the library")
                members.append(rigsets.Member(slot, package=pkg.name, sha256=_sha256(pkg)))
            elif pick.get("backup"):
                backup = str(pick["backup"])
                if backup == "newest":
                    unit_id = (units.get(slot) or {}).get("unit_id", "")
                    found = backups.backups_for(unit_id, limit=1)
                    if not found:
                        raise SetError(f"{slot}: no saved image for this unit; take a backup first")
                    backup = found[0]["id"]
                members.append(rigsets.Member(slot, backup=backup))
            else:
                raise SetError(f"{slot}: choose a package or a backup")
        rs = rigsets.RigSet(name, tuple(members), note)
        _store(store_file).save_set(rs)
    except SetError as exc:
        raise WriteRefused(str(exc)) from exc
    return rs.to_dict()


def snapshot(name: str = "", *, store_file: Path | None = None,
             units_fn: Callable[[], dict[str, dict]] | None = None) -> dict:
    """Save "the rig as it is" as a restorable set: each unit's newest saved image. Optional,
    and it writes only the sets file. Units with no saved image yet are skipped and named in
    ``skipped``. Taking a fresh image is a separate, confirmed act (BACKUP on the unit)."""
    name = name or time.strftime("rig-%Y%m%d-%H%M")
    units = (units_fn or live_units)()
    picks, skipped = [], []
    for slot in SLOTS:
        unit_id = (units.get(slot) or {}).get("unit_id", "")
        if backups.backups_for(unit_id, limit=1):
            picks.append({"slot": slot, "backup": "newest"})
        else:
            skipped.append(slot)
    if not picks:
        raise WriteRefused("no saved images exist yet; back a unit up first")
    saved = save_set(name, "snapshot of each unit's newest saved image", picks,
                     store_file=store_file, units_fn=lambda: units)
    return {**saved, "skipped": skipped}


def delete_set(name: str, confirm: str, *, store_file: Path | None = None) -> None:
    if confirm != name:
        raise WriteRefused("confirmation does not match the set")
    try:
        _store(store_file).delete(name)
    except SetError as exc:
        raise WriteRefused(str(exc)) from exc


def preflight(name: str, only: list[str] | None = None, *, store_file: Path | None = None,
              units_fn: Callable[[], dict[str, dict]] | None = None) -> list[dict]:
    """What a push of this set (or a subset) would do and what blocks it. Writes nothing."""
    rs = _store(store_file).sets.get(name)
    if rs is None:
        raise WriteRefused(f"no set named {name!r}")
    try:
        steps = rigsets.plan(rs, units=(units_fn or live_units)(),
                             library_dir=library.library_dir(), sha_of=_sha256,
                             backup_exists=_backup_exists, compat=compat.compat_for, only=only)
    except SetError as exc:
        raise WriteRefused(str(exc)) from exc
    return [s.to_dict() for s in steps]


def start_push(name: str, only: list[str] | None, confirm: str, *,
               store_file: Path | None = None,
               units_fn: Callable[[], dict[str, dict]] | None = None,
               flash: Callable | None = None, restore: Callable | None = None,
               run_async: bool = True) -> dict:
    """Check everything, then write the units one at a time in the background.

    Returns as soon as the push has started (or been refused). Refusals are raised here,
    before any unit is touched: wrong confirm, a blocked unit, another push already running,
    or a flash in progress. The panel then polls ``overview()['push']``.
    """
    flash = flash or flash_unit_by_id
    restore = restore or restore_unit_by_id
    rs = _store(store_file).sets.get(name)
    if rs is None:
        raise WriteRefused(f"no set named {name!r}")
    if confirm != rs.name:
        raise WriteRefused("confirmation does not match the set")
    if APP.busy:
        raise WriteRefused("a write is running; wait for it to finish")
    try:
        steps = rigsets.plan(rs, units=(units_fn or live_units)(),
                             library_dir=library.library_dir(), sha_of=_sha256,
                             backup_exists=_backup_exists, compat=compat.compat_for, only=only)
        blocked = [f"{s.slot.upper()[-1]}: {s.blocking}" for s in steps if s.blocking]
        if blocked:
            raise SetError("not pushed, nothing was written: " + "; ".join(blocked))
    except SetError as exc:
        raise WriteRefused(str(exc)) from exc

    with _PUSH_LOCK:
        if _PUSH["running"]:
            raise WriteRefused(f"set {_PUSH['set']!r} is already being pushed")
        _PUSH.update(running=True, set=name, ok=None, message="", started=time.time(),
                     finished=0.0,
                     steps=[{"slot": s.slot, "label": s.label, "status": "pending",
                             "message": ""} for s in steps])

    def on_step(res: rigsets.StepResult) -> None:
        with _PUSH_LOCK:
            for row in _PUSH["steps"]:
                if row["slot"] == res.slot:
                    row.update(status=res.status, message=res.message)
        if res.status in ("done", "failed"):
            _log_flash(event="set-step", set=name, slot=res.slot, package=res.label,
                       status=res.status, message=res.message)

    def work() -> None:
        _log_flash(event="set-push", set=name, units=[s.unit_id for s in steps],
                   message="started")
        ok, message = False, ""
        try:
            results = rigsets.push(rs, steps, confirm, flash=flash, restore=restore,
                                   library_dir=library.library_dir(), on_step=on_step)
            ok = all(r.status == "done" for r in results)
            message = ("all units written" if ok else
                       next((f"{r.slot}: {r.message}" for r in results
                             if r.status == "failed"), "stopped"))
        except Exception as exc:  # noqa: BLE001 - report it; never leave "running" stuck
            message = str(exc)
        finally:
            with _PUSH_LOCK:
                _PUSH.update(running=False, ok=ok, message=message, finished=time.time())
            _log_flash(event="set-push", set=name, message="finished: " + message)

    if run_async:
        threading.Thread(target=work, name="fm1-set-push", daemon=True).start()
    else:
        work()
    return push_status()
