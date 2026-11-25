"""The routes page's server side: what it shows and the few things it may change.

Everything here edits only the Pi's own files (the saved patches and the choice of the
active patch) or sends the panic messages. Nothing writes to a unit's flash. The router
process follows both files, so a change here takes effect within its two-second rescan.

Every function takes the paths and the panic backend as arguments with defaults, so the
tests never touch a real home directory or a real sequencer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import config, router, router_service
from .guided import WriteRefused
from .units import SLOT_LABELS, SLOTS

KEYS = tuple(SLOTS)


def describe(route: router.Route) -> str:
    """One short line for a small screen: ``A → B · ch 1-4 · to ch 5 · +12 · no clock``."""
    label = lambda k: SLOT_LABELS.get(k, k)  # noqa: E731
    parts = [f"{label(route.source)} → {label(route.dest)}"]
    if route.channels:
        parts.append("ch " + ",".join(str(c) for c in route.channels))
    if route.remap_to is not None:
        parts.append(f"to ch {route.remap_to}")
    if route.transpose:
        parts.append(f"{route.transpose:+d}")
    if route.velocity_scale != 1.0:
        parts.append(f"vel x{route.velocity_scale:g}")
    if (route.low_note, route.high_note) != (0, 127):
        parts.append(f"notes {route.low_note}-{route.high_note}")
    parts.extend(f"no {k}" for k in route.block)
    return " · ".join(parts)


def _store(path: Path | None) -> router.PatchStore:
    try:
        return router.PatchStore.load(path)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc


def overview(*, store_file: Path | None = None, status_file: Path | None = None,
             active_file: Path | None = None) -> dict:
    error = ""
    try:
        patches = router.PatchStore.load(store_file).patches
    except router.RouteError as exc:
        patches, error = {}, str(exc)
    status = router_service._read_json(status_file or router_service.status_path())
    running = router_service.router_running(path=status_file)
    return {
        "router": {
            "running": running,
            "patch": status.get("patch") if running else None,
            "attached": list(status.get("attached", [])) if running else [],
            "missing": list(status.get("missing", [])) if running else [],
            "held": list(status.get("held", [])) if running else [],
        },
        "active": router_service.read_active(active_file),
        "default": config.setting("FM1_PATCH", "patch", "default"),
        "slots": [{"slot": s, "label": SLOT_LABELS[s]} for s in KEYS],
        "patches": [
            {"name": name,
             "routes": [{"index": i, "text": describe(r), "route": r.to_dict()}
                        for i, r in enumerate(p.routes)]}
            for name, p in sorted(patches.items())],
        "error": error,
    }


def add_route(patch_name: str, route_body, *, store_file: Path | None = None) -> str:
    """Add a route to a patch, making the patch if it is new. Returns the route's text."""
    try:
        route = router.Route.from_dict(route_body)
        for key in (route.source, route.dest):
            if key not in KEYS:
                raise router.RouteError(f"{key!r} is not a unit sticker ({', '.join(KEYS)})")
        store = _store(store_file)
        old = store.patches.get(patch_name)
        store.save_patch(router.Patch(patch_name, (old.routes if old else ()) + (route,)))
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc
    return describe(route)


def remove_route(patch_name: str, index, confirm: str, *,
                 store_file: Path | None = None) -> str:
    if confirm != patch_name:
        raise WriteRefused("confirmation does not match the patch")
    store = _store(store_file)
    patch = store.patches.get(patch_name)
    if patch is None:
        raise WriteRefused(f"no patch named {patch_name!r}")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(patch.routes):
        raise WriteRefused("no such route (the list changed; look again)")
    gone = patch.routes[index]
    try:
        store.save_patch(router.Patch(patch_name, patch.routes[:index] + patch.routes[index + 1:]))
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc
    return describe(gone)


def delete_patch(name: str, confirm: str, *, store_file: Path | None = None) -> None:
    if confirm != name:
        raise WriteRefused("confirmation does not match the patch")
    try:
        _store(store_file).delete(name)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc


def activate(name: str, confirm: str, *, store_file: Path | None = None,
             active_file: Path | None = None) -> None:
    """Choose the patch the router runs. ``name == ""`` stops routing (the units are freed)."""
    if confirm != (name or "stop"):
        raise WriteRefused("confirmation does not match")
    if name and name not in _store(store_file).patches:
        raise WriteRefused(f"no patch named {name!r}")
    router_service.write_active(name, active_file)


def panic(confirm: str, *, backend_factory: Callable | None = None,
          resolve: Callable[[], dict[str, int]] | None = None) -> int:
    """All sound off on every running unit. The caller holds the flash lock, so this never
    runs under a write. Returns the number of messages sent."""
    if confirm != "panic":
        raise WriteRefused("confirmation does not match")
    try:
        if backend_factory is None:
            from .alsa_seq import AlsaSeq
            backend_factory = lambda: AlsaSeq("fm1-router-panic")  # noqa: E731
        if resolve is None:
            resolve = router_service.registry_resolver()
        with backend_factory() as seq:
            return router_service.panic_all(seq, resolve)
    except WriteRefused:
        raise
    except Exception as exc:  # the sequencer missing, a library error: say it, do not crash
        raise WriteRefused(f"could not send panic: {exc}") from exc
