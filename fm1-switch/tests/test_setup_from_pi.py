"""Finish a two-step firmware from the Pi with the maker's installer (JIANT only for now).

The maker's tool and the backup are faked; these tests pin the guards and the one rule that
matters most: the flag is cleared only when the unit itself reports the missing part present.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from fm1_switch import guided, library, maker_install, registry, web_writes
from fm1_switch.guided import WriteFailed, WriteRefused
from fm1_switch.maker_install import MakerError
from fm1_switch.maker_probe import parse_info
from fm1_switch.web_state import APP

UNIT = "aaaaaaaa00000001"
OTHER = "aaaaaaaa00000002"
PKG = "jiant-V10.7.81+de2922f2.fwsc"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(library, "library_dir", lambda: lib)
    path = lib / PKG
    path.write_bytes(b"jiant" * 100)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    reg = registry.load()
    reg.bind(UNIT, "fm1-a", port="1-1.2", firmware="jiant", fingerprint="0329")
    registry.save(reg)
    registry.set_setup_pending(UNIT, PKG, sha)
    units = [
        {"unit_id": UNIT, "port": "1-1.2", "running": True, "download": False,
         "setup_pending": {"package": PKG, "sha256": sha, "since": 1.0}},
        {"unit_id": OTHER, "port": "1-1.3", "running": True, "download": False,
         "setup_pending": None},
    ]
    monkeypatch.setattr(APP, "last_fleet", {"units": units})
    monkeypatch.setattr(web_writes, "_sleep", lambda s: None)
    monkeypatch.setattr(maker_install, "available", lambda: True)
    monkeypatch.setattr(maker_install, "alsa_client_for_usb_path", lambda p: 20)
    state = SimpleNamespace(bt=2, firmware="JIANT V10.7.81", backups=[], installs=[],
                            install_error=None, bt_after=1, log=tmp_path / "flash.log",
                            units=units, lib=lib, sha=sha, mp=monkeypatch)

    def probe(client):
        return {"firmware": state.firmware, "bt": state.bt}

    def backup(unit_id, confirm):
        state.backups.append(unit_id)

    def run_install(package, client, on_progress):
        state.installs.append((package.name, client))
        if state.install_error:
            raise MakerError(state.install_error)
        state.bt = state.bt_after

    monkeypatch.setattr(maker_install, "probe", probe)
    monkeypatch.setattr(web_writes, "backup_unit_now", backup)
    monkeypatch.setattr(maker_install, "run_install", run_install)
    return state


def _log(env):
    return [json.loads(x) for x in env.log.read_text().splitlines()] if env.log.exists() else []


def _flag():
    return registry.load().binding_for_id(UNIT).setup_pending


def test_success_backs_up_first_then_clears_the_flag_when_the_unit_reports_the_part(env):
    out = web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert out["ok"] and out["cleared"]
    assert env.backups == [UNIT]
    assert env.installs == [(PKG, 20)]
    assert _flag() is None
    assert [r["event"] for r in _log(env)] == ["request", "setup-finished"]
    assert APP.last_fleet["units"][0]["setup_pending"] is None


def test_the_flag_stays_when_the_unit_still_reports_the_part_missing(env):
    env.bt_after = 2
    with pytest.raises(WriteFailed):
        web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert _flag() is not None
    assert any(r["event"] == "failed" for r in _log(env))


def test_the_flag_stays_when_the_installer_fails(env):
    env.install_error = "the maker's installer failed (exit 5): timeout"
    with pytest.raises(WriteFailed):
        web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert _flag() is not None


def test_an_already_complete_unit_is_not_written_to(env):
    env.bt = 1
    out = web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert out["ok"]
    assert env.backups == [] and env.installs == []
    assert _flag() is None


def test_a_failed_backup_stops_before_anything_is_installed(env):
    def boom(unit_id, confirm):
        raise WriteFailed("backup failed")
    env.mp.setattr(web_writes, "backup_unit_now", boom)
    with pytest.raises(WriteFailed):
        web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert env.installs == []
    assert _flag() is not None


@pytest.mark.parametrize("mutate,text", [
    (lambda e: e.units[0].update(setup_pending=None), "no unfinished setup"),
    (lambda e: e.units[0].update(setup_pending={"package": "melodee-1.6.1.fwsc", "sha256": "x"}),
     "cannot finish"),
    (lambda e: e.units[1].update(download=True), "every unit must be running"),
    (lambda e: e.units[1].update(running=False), "every unit must be running"),
    (lambda e: setattr(e, "firmware", "SLOOP 2.8.5"), "does not report JIANT"),
])
def test_refusals_install_nothing(env, mutate, text):
    mutate(env)
    with pytest.raises(WriteRefused, match=text):
        web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert env.backups == [] and env.installs == []


def test_a_wrong_confirm_is_refused(env):
    with pytest.raises(WriteRefused, match="confirmation"):
        web_writes.finish_setup_from_pi(UNIT, OTHER)
    assert env.installs == []


def test_a_changed_package_is_refused(env):
    (env.lib / PKG).write_bytes(b"changed")
    with pytest.raises(WriteRefused, match="changed"):
        web_writes.finish_setup_from_pi(UNIT, UNIT)
    assert env.installs == []


def test_the_tools_missing_is_refused(env):
    env.mp.setattr(maker_install, "available", lambda: False)
    with pytest.raises(WriteRefused, match="not set up"):
        web_writes.finish_setup_from_pi(UNIT, UNIT)


def test_no_port_found_is_refused(env):
    env.mp.setattr(maker_install, "alsa_client_for_usb_path", lambda p: None)
    with pytest.raises(WriteRefused, match="USB-MIDI port"):
        web_writes.finish_setup_from_pi(UNIT, UNIT)


def test_a_busy_lock_is_refused_after_the_backup(env):
    assert APP.lock.acquire(blocking=False)
    try:
        with pytest.raises(guided.Busy):
            web_writes.finish_setup_from_pi(UNIT, UNIT)
    finally:
        APP.lock.release()
    assert env.installs == [] and _flag() is not None


def test_only_tried_families_are_offered():
    assert maker_install.family_for("jiant-V10.7.81+x.fwsc") is not None
    assert maker_install.family_for("melodee-1.6.1.fwsc") is None
    assert maker_install.family_for("choralroot-0.42.fwsc") is None


def test_port_match_is_by_whole_path_component(tmp_path, monkeypatch):
    aconnect = ("client 16: 'A' [type=kernel,card=0]\n    0 'A MIDI 1'\n"
                "client 20: 'B' [type=kernel,card=1]\n    0 'B MIDI 1'\n")
    paths = {"/sys/class/sound/card0/device": "/sys/devices/usb1/1-1/1-1.1/1-1.1.2/1-1.1.2:1.0",
             "/sys/class/sound/card1/device": "/sys/devices/usb1/1-1/1-1.1/1-1.1.2:1.0"}
    monkeypatch.setattr(maker_install.os, "path", SimpleNamespace(
        realpath=lambda p: paths[p]))
    assert maker_install.alsa_client_for_usb_path("1-1.1.2", aconnect) == 16
    assert maker_install.alsa_client_for_usb_path("1-1.1", aconnect) is None   # not a component


def test_info_reply_is_parsed_like_the_bench_capture():
    reply = bytes.fromhex(
        "7d464c014a49414e54205631302e372e383100107b4d4073414e414c4f47002d00504841534500"
        "4d415448002d00564f494345002d00574845454c00535452494e47002d004452554d0057455354"
        "00464d360041444400414e494d414c004558540006105501094d014001420103460108005301"
        "035001034e01215201044c01015601204a01014f01075801014302010359013f")
    got = parse_info(list(reply))
    assert got == {"firmware": "JIANT V10.7.81", "bt": 1}
    assert parse_info(list(reply.replace(bytes.fromhex("580101"), bytes.fromhex("580102"))))["bt"] == 2
