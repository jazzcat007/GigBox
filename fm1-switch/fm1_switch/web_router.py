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

from . import clock as clocklib
from . import config, layouts, router, router_service
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
    if route.shift:
        parts.append(f"ch {route.shift:+d}")
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


def clock_view(*, clock_file: Path | None = None, status_file: Path | None = None) -> dict:
    """The master clock as the panel shows it: what was asked for, and what the router says it
    is doing (``live`` is ``None`` when no router is running)."""
    error = ""
    try:
        st = clocklib.load(clock_file)
    except clocklib.ClockError as exc:
        st, error = clocklib.ClockSettings(), str(exc)
    live = None
    if router_service.router_running(path=status_file):
        raw = router_service._read_json(status_file or router_service.status_path()).get("clock")
        # Only what the panel shows: a tick counter would make every poll look like a change.
        if isinstance(raw, dict):
            live = {k: raw.get(k) for k in ("bpm", "running", "targets")}
    return {**st.to_dict(), "live": live, "error": error,
            "min": clocklib.BPM_MIN, "max": clocklib.BPM_MAX}


def set_clock(body, *, clock_file: Path | None = None) -> dict:
    """Change the master clock's tempo, targets or running state. Only ``~/fm1-clock.json``
    is written; the router sends the MIDI. Starting needs ``confirm == "clock"`` because it
    makes every target unit's sequencer run; stopping and tempo need none."""
    if not isinstance(body, dict):
        raise WriteRefused("the clock request must be an object")
    running = body.get("running")
    if running is True and body.get("confirm") != "clock":
        raise WriteRefused("confirmation does not match")
    if running is not None and not isinstance(running, bool):
        raise WriteRefused("running must be true or false")
    try:
        clocklib.update(bpm=body.get("bpm"), running=running, targets=body.get("targets"),
                        valid_targets=set(KEYS), path=clock_file)
    except clocklib.ClockError as exc:
        raise WriteRefused(str(exc)) from exc
    except OSError as exc:
        raise WriteRefused(f"could not save the clock: {exc}") from exc
    return clock_view(clock_file=clock_file)


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
        "clock": clock_view(status_file=status_file),
        "active": router_service.read_active(active_file),
        "default": config.value("patch"),
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
            if key not in KEYS and not router_service.is_external(key):
                raise router.RouteError(
                    f"{key!r} is not a unit sticker or a known device "
                    f"({', '.join(KEYS + tuple(router_service.EXTERNAL_DEVICES))})")
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


# ------------------------------------------------------------------------------- layouts

def _layout_store(path: Path | None) -> layouts.LayoutStore:
    try:
        return layouts.LayoutStore.load(path)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc


def _layout_view(layout: layouts.Layout) -> dict:
    return {"name": layout.name, "source": layout.source, "slices": [
        {"key": k, "label": SLOT_LABELS.get(k, k), **s.to_dict()} for k, s in layout.slices]}


def layouts_overview(*, layout_file: Path | None = None) -> dict:
    """The channel-layout page: per-unit settings, the two built-in layouts, the saved ones."""
    error = ""
    try:
        ls = layouts.LayoutStore.load(layout_file)
    except router.RouteError as exc:
        ls, error = layouts.LayoutStore(layout_file or layouts.store_path()), str(exc)
    shown = []
    for name in layouts.PRESETS:
        try:
            shown.append({**_layout_view(ls.layout(name, list(KEYS))), "builtin": True})
        except router.RouteError as exc:
            shown.append({"name": name, "builtin": True, "slices": [], "error": str(exc)})
    shown += [{**_layout_view(l), "builtin": False} for _, l in sorted(ls.custom.items())]
    return {
        "source": ls.source,
        "sources": list(router_service.EXTERNAL_DEVICES),
        "units": [{"key": k, "label": SLOT_LABELS[k], **{
            "count": ls.unit(k).count, "device": ls.unit(k).device}} for k in KEYS],
        "layouts": shown, "applied": ls.applied, "error": error,
    }


def _reapply(ls: layouts.LayoutStore, store_file, active_file) -> None:
    """Rebuild the running layout's patch after a setting under it changed, so the router
    follows the edit. Does nothing when no layout has been applied."""
    if ls.applied:
        _apply(ls, ls.applied, store_file, active_file)


def _apply(ls: layouts.LayoutStore, name: str, store_file, active_file) -> str:
    layout = ls.layout(name, list(KEYS))
    patch = layout.to_patch()
    _store(store_file).save_patch(patch)
    router_service.write_active(patch.name, active_file)
    ls.mark_applied(name)
    return patch.name


def save_units(units, *, layout_file: Path | None = None, store_file: Path | None = None,
               active_file: Path | None = None) -> None:
    try:
        ls = _layout_store(layout_file)
        for key in (units if isinstance(units, dict) else {}):
            if key not in KEYS:
                raise router.RouteError(f"{key!r} is not a unit sticker ({', '.join(KEYS)})")
        ls.set_units(units)
        _reapply(ls, store_file, active_file)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc


def save_layout(body, *, layout_file: Path | None = None, store_file: Path | None = None,
                active_file: Path | None = None) -> str:
    try:
        layout = layouts.Layout.from_dict(body)
        for key, _ in layout.slices:
            if key not in KEYS:
                raise router.RouteError(f"{key!r} is not a unit sticker ({', '.join(KEYS)})")
        if layout.source not in router_service.EXTERNAL_DEVICES and layout.source not in KEYS:
            raise router.RouteError(f"{layout.source!r} is not a known controller")
        ls = _layout_store(layout_file)
        ls.save_custom(layout)
        if ls.applied == layout.name:
            _reapply(ls, store_file, active_file)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc
    return layout.name


def delete_layout(name: str, confirm: str, *, layout_file: Path | None = None) -> None:
    if confirm != name:
        raise WriteRefused("confirmation does not match the layout")
    try:
        _layout_store(layout_file).delete_custom(name)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc


def apply_layout(name: str, confirm: str, *, layout_file: Path | None = None,
                 store_file: Path | None = None, active_file: Path | None = None) -> str:
    """Write the layout's patch and make it the one the router runs. Returns the patch name."""
    if confirm != name:
        raise WriteRefused("confirmation does not match the layout")
    try:
        return _apply(_layout_store(layout_file), name, store_file, active_file)
    except router.RouteError as exc:
        raise WriteRefused(str(exc)) from exc
