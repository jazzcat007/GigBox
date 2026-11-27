"""Channel layouts: slices of a controller's channels, shifted onto each unit's own channels."""

from __future__ import annotations

import pytest

from fm1_switch import layouts, router, router_service, web_router
from fm1_switch.guided import WriteRefused
from fm1_switch.layouts import Layout, LayoutStore, Slice
from fm1_switch.router import Patch, Route, RouteError

NOTE = lambda ch, n=60: bytes([0x90 | (ch - 1), n, 100])  # noqa: E731


def units(*counts, device=1):
    return {f"fm1-{chr(97 + i)}": Slice(1, c, device) for i, c in enumerate(counts)}


# ------------------------------------------------------------------------------ shift

def test_shift_moves_a_block_down_onto_the_unit_and_drops_what_would_leave_1_16():
    r = Route("oxi", "fm1-b", channels=(5, 6, 7, 8), shift=-4)
    assert r.process(NOTE(5)) == NOTE(1)
    assert r.process(NOTE(8)) == NOTE(4)
    assert r.process(NOTE(4)) is None  # outside the slice
    up = Route("oxi", "fm1-b", shift=10)
    assert up.process(NOTE(7)) is None  # 7 + 10 = 17: off the end, not wrapped
    assert up.process(NOTE(6)) == NOTE(16)


def test_shift_and_remap_cannot_be_combined_and_survive_a_round_trip():
    with pytest.raises(RouteError):
        Route("a", "b", shift=2, remap_to=3)
    r = Route("oxi", "fm1-b", channels=(5,), shift=-4)
    assert Route.from_dict(r.to_dict()) == r
    # system messages are untouched by a shift
    assert r.process(bytes([0xF8])) == bytes([0xF8])


# ----------------------------------------------------------------------------- presets

def test_packed_follows_the_counts_with_no_gaps():
    lay = layouts.preset("packed", "oxi", units(4, 4, 1, 1))
    assert [(k, s.start) for k, s in lay.slices] == [
        ("fm1-a", 1), ("fm1-b", 5), ("fm1-c", 9), ("fm1-d", 10)]


def test_blocks_keep_each_unit_in_its_own_block_and_use_the_first_channels_of_it():
    lay = layouts.preset("blocks", "oxi", units(4, 4, 1, 1))
    assert [(k, s.start, s.count) for k, s in lay.slices] == [
        ("fm1-a", 1, 4), ("fm1-b", 5, 4), ("fm1-c", 9, 1), ("fm1-d", 13, 1)]


def test_sixteen_single_channel_units_get_one_channel_each_in_either_layout():
    for kind in layouts.PRESETS:
        lay = layouts.preset(kind, "oxi", units(*([1] * 16)))
        assert [s.start for _, s in lay.slices] == list(range(1, 17))
        assert len(lay.to_patch().routes) == 16


def test_presets_refuse_what_does_not_fit_in_16_channels():
    with pytest.raises(RouteError):
        layouts.preset("packed", "oxi", units(4, 4, 4, 4, 1))
    with pytest.raises(RouteError):
        layouts.preset("blocks", "oxi", units(4, 4, 4, 4, 1))


def test_a_layout_cannot_give_one_channel_to_two_units_or_overrun_16():
    with pytest.raises(RouteError):
        Layout("x", "oxi", (("fm1-a", Slice(1, 4)), ("fm1-b", Slice(4, 2))))
    with pytest.raises(RouteError):
        Slice(15, 4)
    with pytest.raises(RouteError):
        Slice(1, 4, device=14)
    with pytest.raises(RouteError):
        Layout("x", "fm1-a", (("fm1-a", Slice(1, 1)),))


def test_the_patch_delivers_each_slice_to_its_unit_on_the_units_own_channels():
    patch = layouts.preset("blocks", "oxi", units(4, 4, 1, 1)).to_patch()
    assert patch.name == "auto-blocks"
    got = {m: patch.deliver("oxi", m) for m in (NOTE(2), NOTE(6), NOTE(9), NOTE(13), NOTE(10))}
    assert got[NOTE(2)] == [("fm1-a", NOTE(2))]
    assert got[NOTE(6)] == [("fm1-b", NOTE(2))]
    assert got[NOTE(9)] == [("fm1-c", NOTE(1))]
    assert got[NOTE(13)] == [("fm1-d", NOTE(1))]
    assert got[NOTE(10)] == []  # channel 10 belongs to nobody in this layout
    clock = patch.deliver("oxi", bytes([0xF8]))
    assert sorted(d for d, _ in clock) == ["fm1-a", "fm1-b", "fm1-c", "fm1-d"]


def test_a_unit_set_to_another_channel_is_followed():
    # the unit was set by hand to listen from channel 10
    lay = layouts.preset("packed", "oxi", {"fm1-a": Slice(1, 1, 10)})
    assert lay.to_patch().deliver("oxi", NOTE(1)) == [("fm1-a", NOTE(10))]


# ------------------------------------------------------------------------------- store

def test_store_round_trips_units_and_custom_layouts(tmp_path):
    f = tmp_path / "l.json"
    st = LayoutStore.load(f)
    assert st.unit("fm1-a") == Slice(1, 4, 1)  # defaults
    st.set_units({"fm1-c": {"count": 1, "device": 10}})
    st.save_custom(Layout("mine", "oxi", (("fm1-a", Slice(3, 2, 1)),)))
    again = LayoutStore.load(f)
    assert again.unit("fm1-c") == Slice(1, 1, 10)
    assert again.custom["mine"].slices[0][1] == Slice(3, 2, 1)
    with pytest.raises(RouteError):
        again.save_custom(Layout("packed", "oxi", ()))
    f.write_text("not json")
    with pytest.raises(RouteError):
        LayoutStore.load(f)


# ---------------------------------------------------------------------------- web layer

@pytest.fixture
def files(tmp_path):
    return {"layout_file": tmp_path / "l.json", "store_file": tmp_path / "p.json",
            "active_file": tmp_path / "a.json"}


def test_apply_writes_the_patch_and_makes_it_active(files):
    web_router.save_units({"fm1-c": {"count": 1}, "fm1-d": {"count": 1}}, **files)
    name = web_router.apply_layout("blocks", "blocks", **files)
    assert name == "auto-blocks"
    patch = router.PatchStore.load(files["store_file"]).patches["auto-blocks"]
    assert [r.dest for r in patch.routes] == ["fm1-a", "fm1-b", "fm1-c", "fm1-d"]
    assert router_service.read_active(files["active_file"]) == "auto-blocks"
    assert web_router.layouts_overview(layout_file=files["layout_file"])["applied"] == "blocks"


def test_changing_a_units_channels_rebuilds_the_applied_patch(files):
    web_router.apply_layout("packed", "packed", **files)
    web_router.save_units({"fm1-a": {"count": 2}}, **files)
    patch = router.PatchStore.load(files["store_file"]).patches["auto-packed"]
    assert patch.routes[0].channels == (1, 2)
    assert patch.routes[1].channels[0] == 3  # B moved up behind A


def test_apply_and_delete_need_the_confirmation_and_the_layout_to_exist(files):
    with pytest.raises(WriteRefused):
        web_router.apply_layout("packed", "nope", **files)
    with pytest.raises(WriteRefused):
        web_router.apply_layout("ghost", "ghost", **files)
    with pytest.raises(WriteRefused):
        web_router.delete_layout("packed", "packed", layout_file=files["layout_file"])


def test_save_layout_checks_the_unit_keys_and_the_controller(files):
    ok = {"name": "mine", "source": "oxi",
          "slices": {"fm1-a": {"start": 1, "count": 2, "device": 1}}}
    assert web_router.save_layout(ok, **files) == "mine"
    for bad in ({**ok, "name": "m2", "slices": {"toaster": {"start": 1, "count": 1}}},
                {**ok, "name": "m3", "source": "toaster"}):
        with pytest.raises(WriteRefused):
            web_router.save_layout(bad, **files)
    view = web_router.layouts_overview(layout_file=files["layout_file"])
    assert [l["name"] for l in view["layouts"]] == ["packed", "blocks", "mine"]
    web_router.delete_layout("mine", "mine", layout_file=files["layout_file"])


def test_the_oxi_and_sixteen_units_fit_the_unit_cap():
    from fm1_switch import config
    assert config.MAX_UNITS >= 16
