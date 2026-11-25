"""The setup-unfinished flag for two-step firmwares (docs/ROADMAP-installer-firmwares.md).

A NEEDS INSTALLER package is written as before, but the unit is then "setup unfinished"
until the operator confirms the maker's installer step. These tests pin the lifecycle:
set by a two-step write (even a failed one), replaced by an ordinary firmware, cleared only
by a confirm that names the unit, and never invented from a damaged record.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from fm1_switch import guided, library, packages, registry, web_writes
from fm1_switch.guided import WriteFailed, WriteRefused

UNIT = "aaaaaaaa00000001"
OTHER = "aaaaaaaa00000002"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated registry, flash log and library, with unit UNIT bound to sticker A."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(library, "library_dir", lambda: lib)
    reg = registry.load()
    reg.bind(UNIT, "fm1-a", port="1-1.1.2", firmware="felucca", fingerprint="0329")
    registry.save(reg)
    return SimpleNamespace(lib=lib, log=tmp_path / "flash.log", tmp=tmp_path, monkeypatch=monkeypatch)


def _package(env, name: str, severity: str) -> tuple[object, str]:
    """A library package and the verdict the flash path will see for it."""
    path = env.lib / name
    path.write_bytes(name.encode() * 40)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    env.monkeypatch.setattr(packages, "inspect", lambda p: SimpleNamespace(severity=severity))
    return path, sha


def _stub_write(env, *, fail: bool = False):
    calls = []

    def fake(unit_id, name, sha, package, **kw):
        calls.append(name)
        if fail:
            raise WriteFailed("m-flasher stopped part way")
        return {"ok": True, "event": "done"}

    env.monkeypatch.setattr(web_writes, "_run_guided", fake)
    return calls


def _log(env) -> list[dict]:
    if not env.log.exists():
        return []
    return [json.loads(line) for line in env.log.read_text().splitlines()]


def _flag(unit=UNIT):
    return registry.load().binding_for_id(unit).setup_pending


# ------------------------------------------------------------------ the registry record

def test_flag_is_stored_and_cleared_in_the_registry_file(env):
    assert registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32) is True
    flag = _flag()
    assert flag["package"] == "melodee-1.6.1.fwsc"
    assert flag["sha256"] == "ab" * 32
    assert isinstance(flag["since"], float)

    cleared = registry.clear_setup_pending(UNIT)
    assert cleared["package"] == "melodee-1.6.1.fwsc"
    assert _flag() is None
    assert registry.clear_setup_pending(UNIT) is None      # nothing left to clear


def test_a_sticker_is_needed_to_hold_the_flag(env):
    assert registry.set_setup_pending(OTHER, "melodee-1.6.1.fwsc", "ab" * 32) is False


def test_a_damaged_flag_is_dropped_not_invented(env):
    raw = json.loads(env.tmp.joinpath("units.json").read_text())
    raw["units"]["fm1-a"]["setup_pending"] = {"package": 7, "sha256": "x", "since": 1}
    env.tmp.joinpath("units.json").write_text(json.dumps(raw))
    assert _flag() is None


def test_known_carries_the_flag_to_the_fleet_view(env, monkeypatch):
    monkeypatch.setattr(registry, "scan", lambda: [])
    monkeypatch.setattr(registry, "running", lambda: [])
    registry.set_setup_pending(UNIT, "jiant-V10.7.81.fwsc", "cd" * 32)
    rows = {u.unit_id: u for u in registry.known()}
    assert rows[UNIT].setup_pending["package"] == "jiant-V10.7.81.fwsc"


# ------------------------------------------------------------------ the write path

def test_a_two_step_write_sets_the_flag_and_logs_it(env):
    path, sha = _package(env, "melodee-1.6.1.fwsc", severity="warn")
    calls = _stub_write(env)
    web_writes.flash_unit_by_id(UNIT, path, sha, UNIT)
    assert calls == ["melodee-1.6.1.fwsc"]                 # the write still goes ahead
    assert _flag()["package"] == "melodee-1.6.1.fwsc"
    assert [e["event"] for e in _log(env)] == ["setup-pending"]


def test_an_ordinary_write_replaces_an_unfinished_setup(env):
    registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32)
    path, sha = _package(env, "felucca-1.8.2.fwsc", severity="ok")
    _stub_write(env)
    web_writes.flash_unit_by_id(UNIT, path, sha, UNIT)
    assert _flag() is None
    events = _log(env)
    assert events[-1]["event"] == "setup-replaced"
    assert events[-1]["package"] == "melodee-1.6.1.fwsc"


def test_an_ordinary_write_with_nothing_pending_logs_nothing_extra(env):
    path, sha = _package(env, "felucca-1.8.2.fwsc", severity="ok")
    _stub_write(env)
    web_writes.flash_unit_by_id(UNIT, path, sha, UNIT)
    assert _log(env) == []


def test_a_failed_two_step_write_still_leaves_the_unit_unfinished(env):
    path, sha = _package(env, "choralroot-0.42.fwsc", severity="warn")
    _stub_write(env, fail=True)
    with pytest.raises(WriteFailed):
        web_writes.flash_unit_by_id(UNIT, path, sha, UNIT)
    assert _flag()["package"] == "choralroot-0.42.fwsc"


def test_a_failed_ordinary_write_leaves_an_existing_flag_alone(env):
    registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32)
    path, sha = _package(env, "felucca-1.8.2.fwsc", severity="ok")
    _stub_write(env, fail=True)
    with pytest.raises(WriteFailed):
        web_writes.flash_unit_by_id(UNIT, path, sha, UNIT)
    assert _flag()["package"] == "melodee-1.6.1.fwsc"


def test_a_refused_write_changes_nothing(env):
    registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32)
    path, sha = _package(env, "melodee-1.6.1.fwsc", severity="warn")
    with pytest.raises(WriteRefused):
        web_writes.flash_unit_by_id(UNIT, path, sha, "not-the-unit")
    assert _flag()["package"] == "melodee-1.6.1.fwsc"


# ------------------------------------------------------------------ the operator's confirm

def test_finish_needs_a_confirm_that_names_the_unit(env):
    registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32)
    with pytest.raises(WriteRefused):
        web_writes.finish_setup_by_id(UNIT, OTHER)
    assert _flag() is not None


def test_finish_with_nothing_pending_is_refused(env):
    with pytest.raises(WriteRefused):
        web_writes.finish_setup_by_id(UNIT, UNIT)


def test_finish_clears_the_flag_logs_it_and_writes_nothing(env, monkeypatch):
    registry.set_setup_pending(UNIT, "melodee-1.6.1.fwsc", "ab" * 32)
    calls = _stub_write(env)
    out = web_writes.finish_setup_by_id(UNIT, UNIT)
    assert out == {"ok": True, "package": "melodee-1.6.1.fwsc"}
    assert _flag() is None
    assert calls == []                                       # a confirm is never a write
    assert _log(env)[-1]["event"] == "setup-finished"
