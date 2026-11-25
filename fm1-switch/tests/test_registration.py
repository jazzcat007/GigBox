"""Tests for registration: teaching the Pi which sticker a unit wears.

The whole point of this module is that a unit's arrival on USB is NOT by itself proof of
*which* unit arrived. So these tests lean on the cases where attribution fails: two units
appearing at once, and a unit appearing while another leaves. In both, nothing may be
bound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm1_switch import registration, registry  # noqa: E402

FP_A = "0328"
FP_B = "0320"
FP_D = "0329"


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))


def _run(port, fp, vp="1209:0001", product="Felucca"):
    return registry.RunningUnit(port=port, vendor_product=vp, product=product,
                                fingerprint=fp)


# ------------------------------------------------------------------ diffing

def test_diff_reports_one_arrival():
    after = {"1-1.2": _run("1-1.2", FP_A)}
    res = registration.diff({}, after)
    assert len(res.arrived) == 1
    assert res.single_arrival.port == "1-1.2"
    assert res.departed == []


def test_diff_reports_no_arrival_when_nothing_changed():
    snap = {"1-1.2": _run("1-1.2", FP_A)}
    res = registration.diff(snap, dict(snap))
    assert res.arrived == []
    assert res.single_arrival is None


def test_diff_reports_two_arrivals_as_ambiguous():
    after = {"1-1.2": _run("1-1.2", FP_A), "1-1.3": _run("1-1.3", FP_B)}
    res = registration.diff({}, after)
    assert len(res.arrived) == 2
    assert res.single_arrival is None


def test_diff_detects_a_departure():
    before = {"1-1.2": _run("1-1.2", FP_A)}
    res = registration.diff(before, {})
    assert res.departed == ["1-1.2"]


def test_arrival_reports_whether_the_soft_key_can_reach_it():
    res = registration.diff({}, {"1-1.3": _run("1-1.3", "0104", vp="4d4a:4155",
                                               product="M-VAVE FM-1")})
    assert res.single_arrival.has_midi is False

    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    assert res.single_arrival.has_midi is True


# ------------------------------------------------------------------ waiting

def test_wait_finds_a_single_arrival(monkeypatch):
    seq = [[], [], {"1-1.2": _run("1-1.2", FP_A)}]
    calls = {"n": 0}

    def fake_snapshot():
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return dict(seq[i])

    monkeypatch.setattr(registration, "snapshot", fake_snapshot)
    monkeypatch.setattr(registration.time, "sleep", lambda s: None)
    res = registration.wait_for_arrival({}, timeout=5, poll=0.0, settle=0.0)
    assert res.single_arrival.port == "1-1.2"


def test_wait_refuses_two_units_appearing_together(monkeypatch):
    after = {"1-1.2": _run("1-1.2", FP_A), "1-1.3": _run("1-1.3", FP_B)}
    monkeypatch.setattr(registration, "snapshot", lambda: dict(after))
    monkeypatch.setattr(registration.time, "sleep", lambda s: None)
    with pytest.raises(registration.AttributionError) as exc:
        registration.wait_for_arrival({}, timeout=5, poll=0.0, settle=0.0)
    assert "one unit at a time" in str(exc.value)


def test_wait_refuses_an_arrival_while_something_left(monkeypatch):
    """A unit appearing as another vanishes cannot be attributed to either."""
    before = {"1-1.1": _run("1-1.1", "0011")}
    after = {"1-1.2": _run("1-1.2", FP_A)}
    states = iter([dict(after), dict(after)])
    monkeypatch.setattr(registration, "snapshot", lambda: dict(next(states)))
    monkeypatch.setattr(registration.time, "sleep", lambda s: None)
    with pytest.raises(registration.AttributionError) as exc:
        registration.wait_for_arrival(before, timeout=5, poll=0.0, settle=0.0)
    assert "cannot be attributed" in str(exc.value)


def test_wait_catches_a_second_unit_that_joins_a_moment_late(monkeypatch):
    """Enumeration is not atomic: the second unit may show up after the settle pause."""
    first = {"1-1.2": _run("1-1.2", FP_A)}
    later = {"1-1.2": _run("1-1.2", FP_A), "1-1.3": _run("1-1.3", FP_B)}
    states = iter([first, later, later])
    monkeypatch.setattr(registration, "snapshot", lambda: dict(next(states)))
    monkeypatch.setattr(registration.time, "sleep", lambda s: None)
    with pytest.raises(registration.AttributionError):
        registration.wait_for_arrival({}, timeout=5, poll=0.0, settle=0.1)


def test_wait_times_out_when_nothing_appears(monkeypatch):
    monkeypatch.setattr(registration, "snapshot", lambda: {})
    monkeypatch.setattr(registration.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        registration.wait_for_arrival({}, timeout=0, poll=0.0)


# ------------------------------------------------------------------ binding

def test_register_records_the_sticker_provisionally():
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    b = registration.register(res, "fm1-b")
    assert b.provisional is True
    assert b.fingerprint == FP_A
    assert b.port == "1-1.2"

    back = registry.load()
    assert back.units["fm1-b"].provisional is True
    # A provisional binding must never resolve a flash id, because it has none.
    assert back.slot_for_id("") is None
    assert back.binding_for_id("") is None


def test_register_refuses_ambiguous_arrivals():
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A),
                                 "1-1.3": _run("1-1.3", FP_B)})
    with pytest.raises(registration.AttributionError):
        registration.register(res, "fm1-a")
    assert registry.load().units == {}


def test_register_refuses_a_unit_with_no_fingerprint():
    """Without bcdDevice there is nothing to recognise it by next time."""
    res = registration.diff({}, {"1-1.2": _run("1-1.2", None)})
    with pytest.raises(registration.AttributionError) as exc:
        registration.register(res, "fm1-a")
    assert "bcdDevice" in str(exc.value)


def test_register_rejects_an_unknown_sticker():
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    with pytest.raises(ValueError):
        registration.register(res, "fm1-z")


def test_rerunning_registration_for_a_sticker_replaces_the_old_fingerprint():
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    registration.register(res, "fm1-a")
    res2 = registration.diff({}, {"1-1.3": _run("1-1.3", FP_D)})
    registration.register(res2, "fm1-a")
    back = registry.load()
    assert back.units["fm1-a"].fingerprint == FP_D


# ------------------------------------------------------------------ confirmation

def test_confirm_upgrades_a_provisional_binding_to_a_real_id(monkeypatch):
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    registration.register(res, "fm1-a")

    live = registry.LiveUnit(unit_id="aaaaaaaa00000001", port="1-1.2",
                             device="/dev/sg0", present=True, chip="0x856014",
                             firmware="Felucca")
    monkeypatch.setattr(registration, "fingerprint_for_port", lambda p: FP_A)
    b = registration.confirm(live, "fm1-a")
    assert b.unit_id == "aaaaaaaa00000001"
    assert b.provisional is False
    assert b.chip == "0x856014"

    back = registry.load()
    assert back.slot_for_id("aaaaaaaa00000001") == "fm1-a"


def test_confirm_refuses_a_unit_whose_id_could_not_be_read():
    live = registry.LiveUnit(unit_id="device:sg0", port="1-1.2", device="/dev/sg0",
                             present=True)
    with pytest.raises(registration.AttributionError):
        registration.confirm(live, "fm1-a")


# ------------------------------------------------------------------ wording

def test_guidance_tells_the_operator_the_one_rule_that_matters():
    text = " ".join(registration.guidance()).lower()
    assert "power on only that one unit" in text
    assert "exactly one" in text


def test_describe_arrival_is_human_readable():
    res = registration.diff({}, {"1-1.2": _run("1-1.2", FP_A)})
    text = registration.describe_arrival(res)
    assert "1-1.2" in text
    assert FP_A in text
    assert "MIDI" in text


# ------------------------------------------------------------------ fingerprint safety

def test_fingerprint_ignores_a_port_in_download_mode(monkeypatch):
    """Measured on hardware: every unit reports bcdDevice=0300 in download mode.

    Recording that would give the whole fleet one fingerprint, so a download-mode port
    must yield no fingerprint at all rather than a shared, misleading one.
    """
    monkeypatch.setattr(registration.registry, "running", lambda: [
        registry.RunningUnit(port="1-1.2", vendor_product="4c4a:8057",
                             product="WL80UBOOT1.00", fingerprint="0300"),
    ])
    assert registration.fingerprint_for_port("1-1.2") is None


def test_fingerprint_comes_from_the_running_unit(monkeypatch):
    monkeypatch.setattr(registration.registry, "running", lambda: [
        registry.RunningUnit(port="1-1.2", vendor_product="1209:0001",
                             product="Felucca", fingerprint=FP_A),
    ])
    assert registration.fingerprint_for_port("1-1.2") == FP_A


def test_fingerprint_is_none_for_an_empty_port(monkeypatch):
    monkeypatch.setattr(registration.registry, "running", lambda: [])
    assert registration.fingerprint_for_port("1-1.9") is None
    assert registration.fingerprint_for_port(None) is None
