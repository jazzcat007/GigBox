"""Tests for fm1-switch. No hardware, no m-flasher binary required."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm1_switch import library, mflasher, units  # noqa: E402

# --------------------------------------------------------------------------- units


def test_enumerate_units_empty_when_no_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(units.os.path, "islink", lambda p: False)
    monkeypatch.setattr(units.os.path, "exists", lambda p: False)
    assert units.enumerate_units() == []


def test_units_only_appear_in_download_mode(tmp_path, monkeypatch):
    """A unit running normal firmware has no /dev/fm1-* link: absent, not an error."""
    present = {"/dev/fm1-a", "/dev/fm1-c"}
    monkeypatch.setattr(units.os.path, "islink", lambda p: p in present)
    monkeypatch.setattr(units.os.path, "exists", lambda p: p in present)
    found = units.enumerate_units()
    assert [u.slot for u in found] == ["fm1-a", "fm1-c"]
    assert found[0].device == "/dev/fm1-a"


def test_slot_labels_cover_every_slot():
    assert set(units.SLOT_LABELS) == set(units.SLOTS)


# --------------------------------------------------------------------------- mflasher


def test_argv_firmware_adds_restart_by_default():
    argv = mflasher.argv_firmware("/f/x.fwsc")
    assert argv == ["firmware", "/f/x.fwsc", "--restart"]


def test_argv_firmware_can_skip_restart():
    assert "--restart" not in mflasher.argv_firmware("/f/x.fwsc", restart=False)


def test_run_always_passes_device_and_yes(monkeypatch):
    """With four units connected the tool must never be left to guess or to prompt."""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(mflasher.subprocess, "run", fake_run)
    monkeypatch.setattr(mflasher.shutil, "which", lambda e: "/usr/bin/m-flasher")
    mflasher.run(["firmware", "x.fwsc"], "/dev/fm1-b")
    assert "--device" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--device") + 1] == "/dev/fm1-b"
    assert "--yes" in captured["argv"]


def test_run_without_device_omits_it(monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(mflasher.subprocess, "run", fake_run)
    monkeypatch.setattr(mflasher.shutil, "which", lambda e: "/usr/bin/m-flasher")
    mflasher.run(["find"], None)
    assert "--device" not in captured["argv"]
    assert "--yes" in captured["argv"]


def test_missing_binary_raises_notfound(monkeypatch):
    monkeypatch.setattr(mflasher.shutil, "which", lambda e: None)
    monkeypatch.setattr(mflasher.os.path, "isabs", lambda p: False)
    with pytest.raises(mflasher.NotFound):
        mflasher.run(["info"], "/dev/fm1-a")


# --------------------------------------------------------------------------- info parsing

# CAPTURED FROM THE HARDWARE, not invented. This is the verbatim stdout of
# `m-flasher info --device /dev/sg0 --yes` on the Pi, m-flasher 1.3, 2026-11-22 --
# EXCEPT the "unit id", which was this bench's real flash id and was replaced with an
# obviously fake one (aaaaaaaa00000002) when the repository was scrubbed for
# publication. Nothing in the parser depends on the id's value; every other byte is
# the capture.
#
# The previous fixture here used "model: FM-1" -- colon-separated -- which is what the
# parser expected but NOT what the tool prints. Every field silently came back None on
# real hardware for weeks: unit ids were never read, the web UI showed blank identities,
# and the tests stayed green because they encoded the same wrong assumption as the code.
# Keep these lines byte-exact apart from the scrubbed id.
REAL_INFO_V13 = """\
Connecting to /dev/sg0
M-Flasher loader 1.3 already running
Flash chip 0x856014 (old)
Reading the flash
Read in 2.2 s (459 KB/s)
Checked: the chip CRC32 of the flash matches the read
  model       FM-1
  bootloader  stock (M-VAVE)
  firmware    FM-1 953
  app         OK
  chip key    0x980F
  flash       0x856014
  unit id     aaaaaaaa00000002
  loader      M-Flasher 1.3, CPU at 96 MHz
    - the firmware uses the FM-1 flash layout
    - flash chip 0x856014 (old)
"""

# The colon form, still accepted for older m-flasher builds.
REAL_INFO_LEGACY = """\
model: FM-1
bootloader: stock
firmware: FM-1_015
app: ok
chip key: 0x980F
flash: 0xB36014
unit id: 1A2B3C4D
loader: 2
"""


def test_parse_info_reads_real_mflasher_1_3_output():
    """Whitespace-separated, two-space aligned. This is what the Pi actually prints."""
    ident = mflasher.parse_info(REAL_INFO_V13)
    assert ident.model == "FM-1"
    assert ident.bootloader == "stock (M-VAVE)"
    assert ident.firmware == "FM-1 953"
    assert ident.app == "OK"
    assert ident.chip_key == "0x980F"
    assert ident.flash_chip == "0x856014"
    assert ident.unit_id == "aaaaaaaa00000002"
    assert ident.has_unit_id
    assert ident.loader.startswith("M-Flasher 1.3")


def test_parse_info_ignores_prose_and_bullets():
    """'Connecting to /dev/sg0' and '- flash chip ...' must not become fields."""
    ident = mflasher.parse_info(REAL_INFO_V13)
    for field in ("model", "bootloader", "firmware", "app", "chip_key",
                  "flash_chip", "unit_id", "loader"):
        value = getattr(ident, field)
        assert value is None or "Connecting" not in str(value)


def test_parse_info_still_accepts_the_colon_form():
    ident = mflasher.parse_info(REAL_INFO_LEGACY)
    assert ident.model == "FM-1"
    assert ident.bootloader == "stock"
    assert ident.firmware == "FM-1_015"
    assert ident.chip_key == "0x980F"
    assert ident.flash_chip == "0xB36014"
    assert ident.unit_id == "1A2B3C4D"
    assert ident.has_unit_id


def test_parse_info_normalises_chip_case_and_width():
    """flash is printed 0x%06X; we must compare it case-insensitively upstream."""
    assert mflasher.parse_info("flash: 0x856014").flash_chip == "0x856014"
    assert mflasher.parse_info("flash: 0xb36014").flash_chip == "0xB36014"


def test_parse_info_handles_absent_unit_id():
    """The loader reports '-' when it has no per-unit id: that is not an id."""
    for dash in ("-", "–", ""):
        ident = mflasher.parse_info(f"flash: 0xB36014\nunit id: {dash}\n")
        assert ident.unit_id is None
        assert not ident.has_unit_id


def test_parse_info_tolerates_junk_and_missing_lines():
    ident = mflasher.parse_info("garbage\n\nflash: 0xB36014\n!")
    assert ident.flash_chip == "0xB36014"
    assert ident.unit_id is None


def test_parse_info_does_not_confuse_unit_id_with_flash():
    """Both are hex; parsing by LABEL is what keeps them apart."""
    ident = mflasher.parse_info("flash: 0x856014\nunit id: 856014\n")
    assert ident.flash_chip == "0x856014"
    assert ident.unit_id == "856014"


# --------------------------------------------------------------------------- library


def test_scan_finds_only_fwsc_case_insensitively(tmp_path):
    (tmp_path / "a.fwsc").write_bytes(b"x" * 100)
    (tmp_path / "b.FWSC").write_bytes(b"y" * 50)
    (tmp_path / "c.txt").write_bytes(b"nope")
    names = [p.name for p in library.scan(tmp_path)]
    assert names == ["a.fwsc", "b.FWSC"]


def test_scan_missing_dir_is_empty_not_error(tmp_path):
    assert library.scan(tmp_path / "nope") == []


def test_scan_digest_is_sha256(tmp_path):
    import hashlib

    f = tmp_path / "a.fwsc"
    f.write_bytes(b"hello")
    pkg = library.scan(tmp_path)[0]
    assert pkg.digest == hashlib.sha256(b"hello").hexdigest()


def test_import_refuses_to_clobber(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.fwsc").write_bytes(b"new")
    pkg = library.scan(src)[0]
    library.import_package(pkg)
    with pytest.raises(FileExistsError):
        library.import_package(pkg)


def test_import_then_visible_in_library(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.fwsc").write_bytes(b"data")
    library.import_package(library.scan(src)[0])
    assert [p.name for p in library.library()] == ["x.fwsc"]


def test_remove_refuses_outside_library(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.fwsc").write_bytes(b"data")
    pkg = library.scan(other)[0]
    with pytest.raises(ValueError):
        library.remove_package(pkg)


def test_staged_finds_packages_on_removable_media(tmp_path, monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    (card / "v15.fwsc").write_bytes(b"z")
    monkeypatch.setenv("FM1_STAGING", str(card))
    assert [p.name for p in library.staged_packages()] == ["v15.fwsc"]


# ---------------------------------------------- the "--yes" bug (found on hardware 2026-11-20)
#
# m-flasher 0.12.1 still asks "Type yes to write" for `firmware` DESPITE --yes, and prints
# "Nothing written." while exiting 0. Two separate defects followed from that:
#   1. stdin was DEVNULL, so the prompt got no answer and the write never happened;
#   2. exit 0 made run() report success, so the TUI would claim "installed" falsely.

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_run(monkeypatch, proc, captured):
    import subprocess
    monkeypatch.setattr(mflasher.shutil, "which", lambda _: "/usr/bin/m-flasher")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_run_feeds_yes_to_stdin_for_the_write_prompt(monkeypatch):
    """stdin must supply "yes": DEVNULL silently cancels a real install."""
    captured = {}
    _capture_run(monkeypatch, _FakeProc(stdout="Done: everything written is verified"), captured)
    mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")
    assert captured["kwargs"].get("input") == "yes\n"
    assert captured["kwargs"].get("stdin") is None, "stdin=DEVNULL would defeat the input"


def test_nothing_written_is_an_error_not_a_success(monkeypatch):
    """Exit 0 + 'Nothing written.' must raise — reporting success would be a lie."""
    captured = {}
    _capture_run(
        monkeypatch,
        _FakeProc(returncode=0, stdout='Installs FM-1_920.\nType "yes" to write: Nothing written.\n'),
        captured,
    )
    with pytest.raises(mflasher.NothingWritten) as exc:
        mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")
    assert "Nothing written" in str(exc.value)


def test_nothing_written_detection_is_case_insensitive(monkeypatch):
    captured = {}
    _capture_run(monkeypatch, _FakeProc(stdout="NOTHING WRITTEN"), captured)
    with pytest.raises(mflasher.NothingWritten):
        mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")


def test_nothing_written_check_applies_to_restore_and_bootloader(monkeypatch):
    for cmd in ("restore", "bootloader"):
        captured = {}
        _capture_run(monkeypatch, _FakeProc(stdout="Nothing written."), captured)
        with pytest.raises(mflasher.NothingWritten):
            mflasher.run([cmd, "x"], "/dev/sg0")


def test_read_only_commands_ignore_the_phrase(monkeypatch):
    """`info`/`backup` never prompt; a stray phrase must not turn them into failures."""
    for cmd in ("info", "backup"):
        captured = {}
        _capture_run(monkeypatch, _FakeProc(stdout="Nothing written."), captured)
        mflasher.run([cmd], "/dev/sg0")  # must not raise


def test_successful_write_still_succeeds(monkeypatch):
    captured = {}
    _capture_run(
        monkeypatch,
        _FakeProc(stdout="Installs FM-1_920.\nDone: everything written is verified\n"),
        captured,
    )
    res = mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")
    assert res.ok is True


def test_nothing_to_write_is_NOT_a_failure(monkeypatch):
    """Observed on hardware 2026-11-21: re-installing the firmware already on the unit
    makes m-flasher print "Nothing to write: the flash already holds this." and succeed.
    That must NOT be treated as the "Nothing written" failure — different words, different
    meaning. The unit genuinely holds the requested firmware either way.
    """
    captured = {}
    _capture_run(
        monkeypatch,
        _FakeProc(stdout="Install firmware FM-1_920\n"
                         "Nothing to write: the flash already holds this.\n"
                         "Running: the ChoralRoot FM-1 is on USB MIDI\n"),
        captured,
    )
    res = mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")
    assert res.ok is True


def test_the_two_real_mflasher_wordingd_are_distinguished():
    """Pin the distinction between the failure and the no-op-success phrasings."""
    failure = "Type \"yes\" to write: Nothing written."
    benign = "Nothing to write: the flash already holds this."
    assert mflasher._NOTHING_WRITTEN_RE.search(failure)
    assert not mflasher._NOTHING_WRITTEN_RE.search(benign)


# ---- B6: MF-01 (a device is required) and MF-03 (stderr is read too) ----------------

def test_writing_commands_refuse_to_run_without_a_device(monkeypatch):
    captured = {}
    _capture_run(monkeypatch, _FakeProc(stdout="ok"), captured)
    for cmd in ("firmware", "restore", "bootloader", "backup", "restart", "download-mode"):
        with pytest.raises(mflasher.MflasherError, match="without a --device"):
            mflasher.run([cmd, "x"], None)
    assert "argv" not in captured, "nothing may be launched without a device"


def test_nothing_written_on_stderr_is_still_nothing_written(monkeypatch):
    captured = {}
    _capture_run(monkeypatch, _FakeProc(stdout="", stderr="Nothing written."), captured)
    with pytest.raises(mflasher.NothingWritten):
        mflasher.run(["firmware", "x.fwsc"], "/dev/sg0")


# Captured 2026-11-25 on the bench Pi, m-flasher 0.12.1 (see tests/captures/README.md): the
# real `info` output includes a progress line of "%" ticks that the older capture lacked.
def test_info_parses_the_captured_0_12_1_output_with_its_progress_line():
    from pathlib import Path
    raw = (Path(__file__).parent / "captures" / "scsi-and-info-fm1-a.raw").read_text(encoding="utf-8")
    text = raw[raw.index("Connecting to"):]
    ident = mflasher.parse_info(text)
    assert ident.unit_id == "aaaaaaaa00000001"
    assert ident.flash_chip == "0x856014"
    assert "966" in (ident.firmware or "")
