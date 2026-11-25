"""The routing core: what a route does to a message, and the saved patches. No hardware."""

from __future__ import annotations

import json

import pytest

from fm1_switch import router
from fm1_switch.router import Patch, PatchStore, Route, RouteError

NOTE_ON_C4_CH1 = bytes([0x90, 60, 100])


def r(**kw):
    return Route(source=kw.pop("source", "fm1-a"), dest=kw.pop("dest", "fm1-b"), **kw)


# --------------------------------------------------------------------------- processing

def test_a_plain_route_passes_a_message_unchanged():
    assert r().process(NOTE_ON_C4_CH1) == NOTE_ON_C4_CH1


def test_remap_moves_every_channel_message_to_one_channel():
    assert r(remap_to=5).process(NOTE_ON_C4_CH1) == bytes([0x94, 60, 100])
    assert r(remap_to=5).process(bytes([0xB9, 7, 90])) == bytes([0xB4, 7, 90])
    assert r(remap_to=2).process(bytes([0xC3, 12])) == bytes([0xC1, 12])  # program change


def test_channel_filter_drops_other_channels_and_keeps_listed_ones():
    route = r(channels=(1, 2))
    assert route.process(bytes([0x90, 60, 100])) is not None
    assert route.process(bytes([0x91, 60, 100])) is not None
    assert route.process(bytes([0x92, 60, 100])) is None


def test_block_drops_whole_kinds():
    assert r(block=("cc",)).process(bytes([0xB0, 1, 1])) is None
    assert r(block=("cc",)).process(NOTE_ON_C4_CH1) is not None
    assert r(block=("clock",)).process(bytes([0xF8])) is None
    assert r(block=("sysex",)).process(bytes([0xF0, 0x7D, 0x46, 0x4C, 0x01, 0xF7])) is None
    assert r().process(bytes([0xF8])) == bytes([0xF8])


def test_transpose_shifts_notes_and_drops_those_that_leave_the_keyboard():
    assert r(transpose=12).process(NOTE_ON_C4_CH1) == bytes([0x90, 72, 100])
    assert r(transpose=-12).process(bytes([0x80, 60, 0])) == bytes([0x80, 48, 0])
    assert r(transpose=12).process(bytes([0x90, 120, 100])) is None  # 132: no wrap
    assert r(transpose=-12).process(bytes([0x90, 5, 100])) is None


def test_transpose_leaves_controllers_alone():
    assert r(transpose=12).process(bytes([0xB0, 60, 64])) == bytes([0xB0, 60, 64])


def test_velocity_scale_applies_to_note_on_only_and_stays_in_range():
    assert r(velocity_scale=0.5).process(NOTE_ON_C4_CH1) == bytes([0x90, 60, 50])
    assert r(velocity_scale=2.0).process(NOTE_ON_C4_CH1) == bytes([0x90, 60, 127])
    assert r(velocity_scale=0.1).process(bytes([0x90, 60, 3])) == bytes([0x90, 60, 1])
    # note-on with velocity 0 is a note-off, and a note-off is never scaled up from zero
    assert r(velocity_scale=2.0).process(bytes([0x90, 60, 0])) == bytes([0x90, 60, 0])
    assert r(velocity_scale=2.0).process(bytes([0x80, 60, 40])) == bytes([0x80, 60, 40])


def test_note_range_lets_through_only_notes_inside_it():
    route = r(low_note=36, high_note=59)
    assert route.process(bytes([0x90, 36, 100])) is not None
    assert route.process(bytes([0x90, 59, 100])) is not None
    assert route.process(bytes([0x90, 60, 100])) is None
    assert route.process(bytes([0x80, 20, 0])) is None


def test_range_is_tested_before_the_transpose():
    # 60 is outside 36..59 even though 60-12 would be inside
    assert r(low_note=36, high_note=59, transpose=-12).process(NOTE_ON_C4_CH1) is None


@pytest.mark.parametrize("bad", [
    b"", b"\x90", b"\x90\x3c", bytes([0x90, 200, 1]), bytes([0xC0]), bytes([0xC0, 1, 2]),
    bytes([0xFE]), bytes([0xFF]), bytes([0xF1, 5]), bytes([0x40, 1, 2]),
])
def test_a_malformed_or_unrouted_message_is_dropped_not_raised(bad):
    assert r().process(bad) is None


# ---------------------------------------------------------------------------- validation

@pytest.mark.parametrize("kw", [
    dict(channels=(0,)), dict(channels=(17,)), dict(channels=(True,)),
    dict(block=("nope",)), dict(remap_to=0), dict(remap_to=17),
    dict(transpose=49), dict(velocity_scale=0.0), dict(velocity_scale=2.5),
    dict(velocity_scale=True), dict(low_note=100, high_note=10), dict(high_note=128),
])
def test_a_bad_route_field_is_refused(kw):
    with pytest.raises(RouteError):
        r(**kw)


def test_a_route_to_itself_or_with_no_port_is_refused():
    with pytest.raises(RouteError, match="itself"):
        Route(source="fm1-a", dest="fm1-a")
    with pytest.raises(RouteError):
        Route(source="", dest="fm1-a")


def test_from_dict_rejects_unknown_fields_and_wrong_shapes():
    with pytest.raises(RouteError, match="unknown route field"):
        Route.from_dict({"source": "a", "dest": "b", "chanel": 3})
    with pytest.raises(RouteError):
        Route.from_dict("fm1-a")
    with pytest.raises(RouteError, match="list"):
        Route.from_dict({"source": "a", "dest": "b", "channels": 3})
    with pytest.raises(RouteError, match="text"):
        Route.from_dict({"source": 1, "dest": "b"})


def test_a_route_round_trips_through_its_dict_form():
    route = r(channels=(1, 3), block=("clock",), remap_to=4, transpose=-5,
              velocity_scale=0.8, low_note=24, high_note=96)
    assert Route.from_dict(route.to_dict()) == route
    assert r().to_dict() == {"source": "fm1-a", "dest": "fm1-b"}


# ------------------------------------------------------------------------------ patches

@pytest.mark.parametrize("name", ["", " lead", "a" * 41, "x/y", "..", 5])
def test_a_bad_patch_name_is_refused(name):
    with pytest.raises(RouteError):
        Patch(name=name)


def test_an_identical_duplicate_route_is_refused():
    with pytest.raises(RouteError, match="twice"):
        Patch("p", (r(), r()))


def test_deliver_fans_out_merges_and_filters_per_route():
    patch = Patch("rig", (
        r(source="fm1-a", dest="fm1-b", remap_to=5),
        r(source="fm1-a", dest="fm1-c", block=("note",)),
        r(source="fm1-d", dest="fm1-b"),
    ))
    assert patch.deliver("fm1-a", NOTE_ON_C4_CH1) == [("fm1-b", bytes([0x94, 60, 100]))]
    assert patch.deliver("fm1-a", bytes([0xB0, 1, 1])) == [
        ("fm1-b", bytes([0xB4, 1, 1])), ("fm1-c", bytes([0xB0, 1, 1]))]
    assert patch.deliver("fm1-d", NOTE_ON_C4_CH1) == [("fm1-b", NOTE_ON_C4_CH1)]
    assert patch.deliver("fm1-b", NOTE_ON_C4_CH1) == []  # nothing routed from b
    assert patch.ports() == {"fm1-a", "fm1-b", "fm1-c", "fm1-d"}


# -------------------------------------------------------------------------------- store

def test_the_store_saves_and_reloads_patches_sorted_by_name(tmp_path):
    path = tmp_path / "patches.json"
    store = PatchStore.load(path)
    assert store.patches == {}
    store.save_patch(Patch("zed", (r(),)))
    store.save_patch(Patch("alpha", (r(remap_to=2),)))
    names = [p["name"] for p in json.loads(path.read_text())["patches"]]
    assert names == ["alpha", "zed"]
    again = PatchStore.load(path)
    assert again.patches["alpha"].routes[0].remap_to == 2
    assert not list(tmp_path.glob("patches.json.*")), "no temp file left behind"


def test_saving_a_patch_with_an_existing_name_replaces_it(tmp_path):
    store = PatchStore.load(tmp_path / "p.json")
    store.save_patch(Patch("rig", (r(),)))
    store.save_patch(Patch("rig", (r(transpose=7),)))
    assert PatchStore.load(tmp_path / "p.json").patches["rig"].routes[0].transpose == 7


def test_delete_removes_a_patch_and_refuses_an_unknown_one(tmp_path):
    store = PatchStore.load(tmp_path / "p.json")
    store.save_patch(Patch("rig", (r(),)))
    store.delete("rig")
    assert PatchStore.load(tmp_path / "p.json").patches == {}
    with pytest.raises(RouteError, match="no patch named"):
        store.delete("rig")


@pytest.mark.parametrize("body", [
    "not json", "[]", '{"patches": {}}',
    '{"patches": [{"name": "a", "routes": []}, {"name": "a", "routes": []}]}',
    '{"patches": [{"name": "a", "routes": [{"source": "x"}]}]}',
])
def test_a_broken_store_file_is_refused_with_a_message_and_left_alone(tmp_path, body):
    path = tmp_path / "p.json"
    path.write_text(body)
    with pytest.raises(RouteError):
        PatchStore.load(path)
    assert path.read_text() == body


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    store = PatchStore.load(path)
    store.save_patch(Patch("keep", (r(),)))
    before = path.read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(router.os, "replace", boom)
    with pytest.raises(OSError):
        store.save_patch(Patch("new", (r(),)))
    assert path.read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.name != "p.json"], "temp file cleaned up"


# ------------------------------------------------------------- panic, release, channel split

def test_panic_sends_sound_off_reset_and_notes_off_on_every_channel():
    msgs = router.panic_messages()
    assert len(msgs) == 16 * 3
    assert msgs[:3] == [bytes([0xB0, 120, 0]), bytes([0xB0, 121, 0]), bytes([0xB0, 123, 0])]
    assert msgs[-1] == bytes([0xBF, 123, 0])
    assert router.panic_messages([2]) == [bytes([0xB2, 120, 0]), bytes([0xB2, 121, 0]),
                                          bytes([0xB2, 123, 0])]
    with pytest.raises(RouteError):
        router.panic_messages([16])


def test_a_unit_about_to_be_flashed_has_every_route_touching_it_released():
    patch = Patch("rig", (
        r(source="fm1-a", dest="fm1-b"),
        r(source="fm1-c", dest="fm1-d"),
        r(source="fm1-d", dest="fm1-a"),
    ))
    released = router.ports_to_release(patch, {"fm1-a"})
    assert [(x.source, x.dest) for x in released] == [("fm1-a", "fm1-b"), ("fm1-d", "fm1-a")]
    assert router.ports_to_release(patch, set()) == []
    assert router.ports_to_release(patch, {"fm1-z"}) == []


def test_each_letter_gets_its_own_channel_range():
    assert router.letter_channel_ranges(["A", "B", "C", "D"]) == {
        "A": (1, 2, 3, 4), "B": (5, 6, 7, 8), "C": (9, 10, 11, 12), "D": (13, 14, 15, 16)}
    assert router.letter_channel_ranges(["A", "B"], per_unit=8)["B"] == tuple(range(9, 17))
    with pytest.raises(RouteError, match="fit"):
        router.letter_channel_ranges(["A", "B", "C", "D", "E"])
    with pytest.raises(RouteError, match="twice"):
        router.letter_channel_ranges(["A", "A"])


def test_the_letter_ranges_work_as_route_channel_filters():
    ranges = router.letter_channel_ranges(["A", "B"])
    to_a = r(source="fm1-d", dest="fm1-a", channels=ranges["A"])
    assert to_a.process(bytes([0x93, 60, 100])) is not None  # channel 4
    assert to_a.process(bytes([0x94, 60, 100])) is None  # channel 5 belongs to B
