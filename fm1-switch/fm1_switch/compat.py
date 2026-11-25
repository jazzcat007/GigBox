"""Package/chip compatibility, shared by the web API."""

from __future__ import annotations


def compat_for(pkg_path, unit_chip: str | None) -> tuple[str, str]:
    """(severity, note) for one library package on one unit's flash chip.

    Two independent facts decide this, and neither is in the .fwsc itself:

    * the unit's chip, read from flash in download mode (``0xB36014`` new, ``0x856014``
      old), or unknown while the unit is merely running;
    * the *catalogue's* chip flag, which means "knows the newer chip" -- NOT "requires
      it". So an old-chip unit can run anything, and a new-chip unit can run an old
      firmware but will not keep saves across power-off.

    The catalogue is keyed by sha256, so a package is matched to its flag by content, not
    by filename.
    """
    if not unit_chip:
        return ("unknown", "chip unknown — read it in download mode")
    up = unit_chip.upper()
    if up.endswith("856014"):
        return ("ok", "old chip — every firmware can write it")

    # New chip: the catalogue flag matters. Look it up by content.
    from . import hub, library

    try:
        cat = hub.load(network=False)   # a page render never reaches the network (HUB-01)
    except Exception:  # noqa: BLE001 - offline must not block the panel
        cat = None
    if cat is None:
        return ("unknown", "catalogue offline — cannot check the new chip")
    try:
        digest = library._digest(pkg_path)
    except OSError:
        return ("unknown", "cannot read package")
    entry = next((e for e in cat.entries if e.sha256 == digest), None)
    if entry is None:
        return ("unknown", "not in the catalogue")
    severity, text = entry.chip_note(unit_chip)
    return (severity, text)
