"""The EDID is the only thing on the Pi that decides the display mode.

It is not shipped with the repo (it lives in /lib/firmware on the Pi), so make-edid.py is
the only record of it.  These tests pin the layout rules the kernel actually enforces,
because an EDID with a valid checksum still parses into a *different mode list* -- that is
exactly how the 1224x765 family got generated, and a checksum-only test would have passed.
"""
import hashlib
import importlib.machinery
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "make-edid.py"


@pytest.fixture(scope="module")
def mk():
    loader = importlib.machinery.SourceFileLoader("make_edid", str(SCRIPT))
    spec = importlib.util.spec_from_loader("make_edid", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def edid(mk):
    return mk.build()


def test_checksum_and_length(edid):
    assert len(edid) == 128
    assert sum(edid) % 256 == 0


def test_standard_timings_are_padding(edid):
    # Bytes 38..53.  Anything else here is decoded as *standard timings* and the kernel
    # generates extra modes from it -- the original bug.
    assert edid[38:54] == b"\x01" * 16


def test_first_descriptor_is_the_detailed_timing(edid):
    # Byte 54 onwards.  A 0x0000 pixel clock would make this a monitor descriptor instead.
    dtd = edid[54:72]
    assert dtd[0:2] != b"\x00\x00"
    assert dtd[0:2] == (2938).to_bytes(2, "little")          # 29.38 MHz
    assert dtd[2] | ((dtd[4] >> 4) << 8) == 800              # h active
    assert dtd[5] | ((dtd[7] >> 4) << 8) == 480              # v active
    assert dtd[17] == 0x18                                   # digital separate sync


def test_monitor_descriptors_are_terminated(edid):
    # Byte 17 of a *monitor descriptor* must be 0x00.  The text is padded into 5..17, so
    # padding to 13 silently overwrites the terminator with 0x20 and the descriptor is
    # malformed.  The kernel tolerated this, but it is not a valid EDID.
    for i in range(4):
        s = edid[54 + 18 * i:72 + 18 * i]
        if s[0:2] == b"\x00\x00":
            assert s[17] == 0, f"descriptor {i} terminator is 0x{s[17]:02x}"


def test_reports_no_extensions_and_no_range_limits(edid):
    # byte 126 counts extension blocks; claiming one that is absent is a parse error.
    assert edid[126] == 0
    # A range-limits descriptor makes the kernel generate GTF modes.
    for i in range(4):
        s = edid[54 + 18 * i:72 + 18 * i]
        assert not (s[0:3] == b"\x00\x00\x00" and s[3] == 0xFD)


def test_problems_flags_the_historical_bug(mk, edid):
    """The checker must actually catch the defect it was written for."""
    broken = bytearray(edid)
    broken[72 + 17] = 0x20                       # text spilled over the terminator byte
    broken[127] = 0
    broken[127] = (-sum(broken[:127])) & 0xFF                            # fix checksum only
    found = mk.problems(bytes(broken))
    assert any("terminator" in f for f in found), found


def test_problems_flags_a_stray_standard_timing(mk, edid):
    broken = bytearray(edid)
    broken[38:40] = bytes([0x7A, 0x0B])          # this decodes as 1224x765
    broken[127] = 0
    broken[127] = (-sum(broken[:127])) & 0xFF
    assert any("standard timing" in f for f in mk.problems(bytes(broken)))


def test_problems_flags_missing_extension_block(mk, edid):
    broken = bytearray(edid)
    broken[126] = 1
    broken[127] = 0
    broken[127] = (-sum(broken[:127])) & 0xFF
    assert any("extension" in f for f in mk.problems(bytes(broken)))


def test_clean_edid_reports_no_problems(mk, edid):
    assert mk.problems(edid) == []


def test_installed_digest_is_documented(edid):
    """The Pi copy is not in the repo; record the digest so drift is detectable.

    If this fails, either the generator changed (update the digest and reinstall) or the
    Pi is running something the repo cannot reproduce.
    """
    assert hashlib.md5(edid).hexdigest() == "9f502e27456585fa744d95cf5c043868"
