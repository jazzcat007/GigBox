"""Chip compatibility judgement for library packages."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm1_switch import compat, library  # noqa: E402


def test_old_chip_unit_can_write_every_firmware():
    """On 0x856014 the catalogue flag is irrelevant: every firmware can write it."""
    severity, note = compat.compat_for(Path("x.fwsc"), "0x856014")
    assert severity == "ok"
    assert "every firmware" in note


def test_running_unit_with_no_chip_known_says_so():
    severity, note = compat.compat_for(Path("x.fwsc"), None)
    assert severity == "unknown"
    assert "download mode" in note


def test_new_chip_unit_flags_a_firmware_the_catalogue_says_is_unaware(monkeypatch):
    class Entry:
        chip = "old"
        sha256 = "abc"

        def chip_note(self, chip):
            return ("warn", "Does not know this unit's newer flash chip.")

    class Cat:
        entries = [Entry()]

    monkeypatch.setattr(library, "_digest", lambda p: "abc")
    monkeypatch.setattr("fm1_switch.hub.load", lambda *a, **k: Cat())
    severity, note = compat.compat_for(Path("x.fwsc"), "0xB36014")
    assert severity == "warn"
    assert "newer flash chip" in note
