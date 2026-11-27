"""Saved whole-flash images: listing, space and pruning (split out of web.py, B14)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import guided
from . import config


#: Saved per-unit flash images live here (used for region comparison and restore).
def backups_dir() -> Path:
    return config.path("backups")

def backups_for(unit_id: str, limit: int = 40) -> list[dict]:
    """Saved whole-flash images that belong to ``unit_id`` (matched on the flash id inside
    each image's .json sidecar), newest first. ``id`` is the path under the backups dir."""
    root = backups_dir()
    if not guided.looks_like_flash_id(unit_id) or not root.is_dir():
        return []
    out: list[dict] = []
    for meta in root.rglob("*.json"):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        image = meta.with_suffix(".bin")
        if (not isinstance(info, dict) or str(info.get("unit id", "")).lower() != unit_id.lower()
                or not image.is_file()):
            continue
        out.append({"id": str(image.relative_to(root)), "saved": str(info.get("saved", "")),
                    "firmware": str(info.get("firmware", "")),
                    "bootloader": str(info.get("bootloader", ""))})
    out.sort(key=lambda b: (b["saved"], b["id"]), reverse=True)
    return out[:limit]

def backup_space() -> dict:
    """How much the saved images take, and how much room is left where they go."""
    root = backups_dir()
    used = 0
    if root.is_dir():
        for f in root.rglob("*"):
            try:
                if f.is_file():
                    used += f.stat().st_size
            except OSError:
                pass
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = None
    return {"used_kb": used // 1024, "free_mb": None if free is None else free // (1024 * 1024)}

def prune_auto_backups(directory: Path, keep: int | None = None) -> list[str]:
    """Delete all but the newest ``keep`` images per unit in ``directory`` (the auto/ dir).
    Returns the names removed. Only .bin/.json pairs found in this one directory.
    ``keep`` defaults to the ``auto_backups_kept`` setting; backups made by hand live
    outside auto/ and are never touched."""
    if keep is None:
        keep = int(config.value("auto_backups_kept"))
    by_unit: dict[str, list[tuple[str, Path]]] = {}
    for meta in directory.glob("*.json"):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get("unit id"):
            by_unit.setdefault(str(info["unit id"]).lower(), []).append(
                (str(info.get("saved", "")), meta))
    removed: list[str] = []
    for items in by_unit.values():
        items.sort(key=lambda t: (t[0], t[1].name), reverse=True)
        for _saved, meta in items[keep:]:
            for f in (meta.with_suffix(".bin"), meta):
                f.unlink(missing_ok=True)
            removed.append(meta.stem)
    return removed
