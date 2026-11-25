"""Phase D investigation findings, pinned as tests (docs/ROADMAP-installer-firmwares.md).

These record what was measured, not what should be built. Each one skips when the bytes it
needs are not on this machine: realtest/ packages and archives/ backups are not in the
repository (see .gitignore).
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from fm1_switch import packages, web_state, web_writes

ROOT = Path(__file__).resolve().parent.parent.parent
REAL_DIR = ROOT / "fm1-switch" / "realtest"
ARCHIVE_DIR = ROOT / "archives" / "units"

needs_melodee = pytest.mark.skipif(
    not (REAL_DIR / "melodee-1.6.1.fwsc").exists(),
    reason="realtest/melodee-1.6.1.fwsc not present (run tools/fetch-realtest.sh)",
)
needs_choralroot = pytest.mark.skipif(
    not (REAL_DIR / "choralroot-0.42.fwsc").exists(),
    reason="realtest/choralroot-0.42.fwsc not present",
)
needs_pair = pytest.mark.skipif(
    not (ARCHIVE_DIR / "fm1-20261120-171537.bin").exists()
    or not (ARCHIVE_DIR / "fm1-20261120-173052.bin").exists(),
    reason="archives/units backups not present (kept on the bench, not in the repository)",
)

# A synthetic flash id. The write path is stubbed below, so only the guards run.
FAKE_ID = "aaaaaaaa00000002"


@needs_melodee
def test_needs_installer_package_is_not_refused_by_the_flash_path(tmp_path, monkeypatch):
    """Finding (ROADMAP-installer-firmwares, point 1): a NEEDS INSTALLER package is only
    warned about. The write path checks the library location and the sha256 the operator
    confirmed; it never reads the install verdict, so it goes on to the write."""
    lib = tmp_path / "library"
    lib.mkdir()
    pkg = lib / "melodee-1.6.1.fwsc"
    shutil.copy(REAL_DIR / "melodee-1.6.1.fwsc", pkg)

    assert packages.inspect(pkg).severity == "warn"  # the premise: NEEDS INSTALLER

    monkeypatch.setattr(web_writes.library, "library_dir", lambda: lib)
    reached: dict = {}

    def fake_run_guided(unit_id, name, sha, package, **kw):
        reached.update(unit_id=unit_id, name=name, sha=sha)
        return {"ok": True}

    monkeypatch.setattr(web_writes, "_run_guided", fake_run_guided)
    sha = web_state._sha256(pkg)
    result = web_writes.flash_unit_by_id(FAKE_ID, pkg, sha, FAKE_ID)

    assert result == {"ok": True}
    assert reached == {"unit_id": FAKE_ID, "name": "melodee-1.6.1.fwsc", "sha": sha}


@needs_melodee
def test_melodee_resource_region_is_the_mrs1_format():
    """Finding (point 2): Melodee's res.bin is a self-describing resource file. Its header is
    magic 'MRS1', a payload length and a CRC32, which is the file Melodee's own
    tools/fm1_resources.py checks before it pushes the region over SysEx command 82."""
    pkg = packages.parse(REAL_DIR / "melodee-1.6.1.fwsc")
    res = next(f for f in pkg.files if f.name == "res.bin")
    assert res.size == 57344
    magic, length, crc = struct.unpack_from("<3I", res.data)
    assert res.data[:4] == b"MRS1"
    assert length == 53844
    assert crc == 0x2035D335
    assert [f.name for f in pkg.extra_payload] == ["res.bin"]


@needs_choralroot
def test_choralroot_resource_region_has_its_own_format():
    """Finding (point 2): ChoralRoot's res2.bin carries a different magic ('CRE2'). Nothing
    in the Melodee writer applies to it, and its writer is not identified."""
    pkg = packages.parse(REAL_DIR / "choralroot-0.42.fwsc")
    res = next(f for f in pkg.files if f.name == "res2.bin")
    assert res.size == 16384
    assert res.data[:4] == b"CRE2"
    assert [f.name for f in pkg.extra_payload] == ["res2.bin"]


@needs_pair
def test_melodee_install_changes_only_the_app_region_and_runtime_record():
    """Finding (point 2, observed on the bench): the Melodee install on unit C changed the
    app region (0x04000-0x92FFF) and a tail record at 0xFC000. The per-unit data regions
    (VM 0x93000-0xE8FFF, BTIF 0xE9000-0xE9FFF, USR 0xEA000-0xFBFFF) and the boot area are
    byte-identical before and after, so an install does not touch per-unit data."""
    before = (ARCHIVE_DIR / "fm1-20261120-171537.bin").read_bytes()  # stock V15, unit C
    after = (ARCHIVE_DIR / "fm1-20261120-173052.bin").read_bytes()   # after Melodee
    assert len(before) == len(after) == 0x100000
    assert before[0x0000:0x4000] == after[0x0000:0x4000]            # boot area
    assert before[0x93000:0xFC000] == after[0x93000:0xFC000]        # VM, BTIF, USR
    assert before[0x4000:0x93000] != after[0x4000:0x93000]          # the app region
    assert before[0xFC000:0xFD430] != after[0xFC000:0xFD430]        # the tail record
