"""Rig sets: model, saved file, preflight and the sequential push. Fakes only: no unit, no Pi."""

from __future__ import annotations

import json

import pytest

from fm1_switch import rigsets
from fm1_switch.rigsets import Member, RigSet, SetError, SetStore

SHA = "a" * 64
OTHER = "b" * 64
IDS = {"fm1-a": "1111aaaa00000001", "fm1-b": "1111aaaa00000002",
       "fm1-c": "1111aaaa00000003", "fm1-d": "1111aaaa00000004"}


def units():
    return {s: {"unit_id": i, "chip": "0x856014"} for s, i in IDS.items()}


def full_set():
    return RigSet("jam", tuple(Member(s, package=f"{s[-1]}.fwsc", sha256=SHA) for s in IDS))


@pytest.fixture()
def lib(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    for s in "abcd":
        (d / f"{s}.fwsc").write_bytes(b"x")
    return d


def make_plan(rs, lib, *, sha=lambda p: SHA, backup=lambda u, b: True, only=None, compat=None):
    return rigsets.plan(rs, units=units(), library_dir=lib, sha_of=sha, backup_exists=backup,
                        compat=compat, only=only)


# ------------------------------------------------------------------------------ model

def test_member_needs_exactly_one_source():
    with pytest.raises(SetError):
        Member("fm1-a")
    with pytest.raises(SetError):
        Member("fm1-a", package="a.fwsc", sha256=SHA, backup="x/y.bin")


@pytest.mark.parametrize("bad", ["../a.fwsc", "dir/a.fwsc", ".hidden.fwsc"])
def test_package_must_be_a_plain_name(bad):
    with pytest.raises(SetError):
        Member("fm1-a", package=bad, sha256=SHA)


def test_package_needs_a_real_sha():
    with pytest.raises(SetError):
        Member("fm1-a", package="a.fwsc", sha256="nope")


@pytest.mark.parametrize("bad", ["/etc/x.bin", "../x.bin", "a/../../x.bin", "x.json"])
def test_backup_must_stay_inside(bad):
    with pytest.raises(SetError):
        Member("fm1-a", backup=bad)


def test_unknown_slot_and_duplicates_and_names_refused():
    with pytest.raises(SetError):
        Member("fm1-z", package="a.fwsc", sha256=SHA)
    m = Member("fm1-a", package="a.fwsc", sha256=SHA)
    with pytest.raises(SetError):
        RigSet("x", (m, m))
    with pytest.raises(SetError):
        RigSet("../evil", (m,))
    with pytest.raises(SetError):
        RigSet("empty", ())


def test_a_set_may_cover_a_single_unit():
    rs = RigSet("just-b", (Member("fm1-b", package="b.fwsc", sha256=SHA),))
    assert [m.slot for m in rs.members] == ["fm1-b"]


# ------------------------------------------------------------------------------ store

def test_store_round_trip_and_sticker_order(tmp_path):
    path = tmp_path / "sets.json"
    st = SetStore.load(path)
    rs = RigSet("jam", (Member("fm1-d", package="d.fwsc", sha256=SHA),
                        Member("fm1-a", backup="auto/x.bin")), note="friday")
    st.save_set(rs)
    again = SetStore.load(path).sets["jam"]
    assert again.to_dict() == rs.to_dict()
    assert [m["slot"] for m in json.loads(path.read_text())["sets"][0]["members"]] == [
        "fm1-a", "fm1-d"]


def test_broken_store_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "sets.json"
    path.write_text("{ not json")
    with pytest.raises(SetError):
        SetStore.load(path)
    assert path.read_text() == "{ not json"


def test_delete_unknown_set(tmp_path):
    with pytest.raises(SetError):
        SetStore.load(tmp_path / "s.json").delete("nope")


# ---------------------------------------------------------------------------- preflight

def test_plan_all_ready_in_sticker_order(lib):
    steps = make_plan(full_set(), lib)
    assert [s.slot for s in steps] == list(IDS)
    assert not any(s.blocking for s in steps)


def test_plan_subset(lib):
    steps = make_plan(full_set(), lib, only=["fm1-c", "fm1-a"])
    assert [s.slot for s in steps] == ["fm1-a", "fm1-c"]


def test_plan_rejects_unit_not_in_set_or_empty_selection(lib):
    rs = RigSet("two", (Member("fm1-a", package="a.fwsc", sha256=SHA),))
    with pytest.raises(SetError):
        make_plan(rs, lib, only=["fm1-b"])
    with pytest.raises(SetError):
        make_plan(rs, lib, only=[])


def test_plan_blocks_missing_changed_and_unknown_unit(lib):
    (lib / "b.fwsc").unlink()
    steps = make_plan(full_set(), lib, sha=lambda p: OTHER if p.name == "c.fwsc" else SHA)
    by = {s.slot: s.blocking for s in steps}
    assert by["fm1-a"] == ""
    assert "not in the library" in by["fm1-b"]
    assert "changed" in by["fm1-c"]
    u = units()
    u["fm1-d"]["unit_id"] = "port:1-1.3"
    steps = rigsets.plan(full_set(), units=u, library_dir=lib, sha_of=lambda p: SHA,
                         backup_exists=lambda u, b: True)
    assert "not known" in [s for s in steps if s.slot == "fm1-d"][0].blocking


def test_plan_backup_member_checks_the_image(lib):
    rs = RigSet("back", (Member("fm1-a", backup="auto/x.bin"),))
    assert not make_plan(rs, lib)[0].blocking
    assert "gone" in make_plan(rs, lib, backup=lambda u, b: False)[0].blocking


def test_plan_carries_chip_warning_without_blocking(lib):
    steps = make_plan(full_set(), lib, compat=lambda p, chip: ("warn", "will not save"))
    assert all(s.warning == "will not save" and not s.blocking for s in steps)


# -------------------------------------------------------------------------------- push

class Writer:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def flash(self, unit_id, path, sha, confirm):
        self.calls.append(("flash", unit_id, path.name, sha, confirm))
        if unit_id == self.fail_on:
            raise RuntimeError("write failed")
        return {"message": "installed"}

    def restore(self, unit_id, backup, confirm):
        self.calls.append(("restore", unit_id, backup, confirm))
        return {"message": "restored"}


def run_push(rs, lib, w, confirm="jam", only=None, seen=None):
    steps = make_plan(rs, lib, only=only)
    return rigsets.push(rs, steps, confirm, flash=w.flash, restore=w.restore, library_dir=lib,
                        on_step=(lambda r: seen.append((r.slot, r.status))) if seen is not None
                        else (lambda r: None))


def test_push_writes_each_unit_in_order_confirmed_by_its_own_id(lib):
    w = Writer()
    results = run_push(full_set(), lib, w)
    assert [r.status for r in results] == ["done"] * 4
    assert [c[1] for c in w.calls] == list(IDS.values())
    assert all(c[-1] == c[1] for c in w.calls)      # confirm is the unit's flash id


def test_push_mixes_packages_and_backups(lib):
    rs = RigSet("jam", (Member("fm1-a", package="a.fwsc", sha256=SHA),
                        Member("fm1-b", backup="auto/b.bin")))
    w = Writer()
    run_push(rs, lib, w)
    assert [c[0] for c in w.calls] == ["flash", "restore"]


def test_wrong_confirm_writes_nothing(lib):
    w = Writer()
    with pytest.raises(SetError):
        run_push(full_set(), lib, w, confirm="wrong")
    assert w.calls == []


def test_blocked_step_refuses_before_any_write(lib):
    (lib / "c.fwsc").unlink()
    w = Writer()
    with pytest.raises(SetError, match="nothing was written"):
        run_push(full_set(), lib, w)
    assert w.calls == []


def test_blocked_unit_can_be_deselected_and_the_rest_pushed(lib):
    (lib / "c.fwsc").unlink()
    w = Writer()
    results = run_push(full_set(), lib, w, only=["fm1-a", "fm1-b", "fm1-d"])
    assert [r.slot for r in results] == ["fm1-a", "fm1-b", "fm1-d"]
    assert len(w.calls) == 3


def test_first_failure_stops_and_leaves_the_rest_untouched(lib):
    w = Writer(fail_on=IDS["fm1-b"])
    seen = []
    results = run_push(full_set(), lib, w, seen=seen)
    assert [r.status for r in results] == ["done", "failed", "not started", "not started"]
    assert "write failed" in results[1].message
    assert len(w.calls) == 2
    assert ("fm1-a", "writing") in seen and ("fm1-d", "not started") in seen


def test_nothing_selected_is_refused(lib):
    with pytest.raises(SetError):
        rigsets.push(full_set(), [], "jam", flash=lambda *a: {}, restore=lambda *a: {},
                     library_dir=lib)
