"""The router service against a fake sequencer: attach, route, release for a flash, recover.

No ALSA and no unit is touched. The ctypes layer is checked on the Pi by
``python alsa_seq.py loopback``, which talks only to ALSA's Midi Through client.
"""

from __future__ import annotations

import threading
import time

import pytest

from fm1_switch import router, router_service
from fm1_switch.alsa_seq import SysexAssembler
from fm1_switch.router import Patch, Route
from fm1_switch.router_service import RouterService


class FakeSeq:
    def __init__(self):
        self.connected: set[int] = set()
        self.incoming: list[tuple[int, bytes]] = []
        self.sent: list[tuple[int, bytes]] = []
        self.fail_connect: set[int] = set()
        self.fail_send: set[int] = set()

    def connect(self, client, port=0):
        if client in self.fail_connect:
            raise OSError("busy")
        self.connected.add(client)

    def disconnect(self, client, port=0):
        self.connected.discard(client)

    def read(self, timeout=0.1):
        out, self.incoming = self.incoming, []
        return out

    def send(self, client, data, port=0):
        if client in self.fail_send:
            raise OSError("gone")
        assert client in self.connected, "sent to a unit that is not attached"
        self.sent.append((client, data))


def rt(a, b, **kw):
    return Route(a, b, **kw)


@pytest.fixture
def env(tmp_path):
    seq = FakeSeq()
    present = {"fm1-a": 20, "fm1-b": 24, "fm1-c": 28}
    holds: set[str] = set()
    logs: list[str] = []
    svc = RouterService(seq, lambda: dict(present), lambda: set(holds), logs.append,
                        status_file=tmp_path / "status.json")
    return seq, present, holds, logs, svc


NOTE = bytes([0x90, 60, 100])


def test_a_note_from_one_unit_is_routed_and_remapped_to_another(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b", remap_to=5),)))
    assert seq.connected == {20, 24}
    seq.incoming = [(20, NOTE)]
    assert svc.pump() == 1
    assert seq.sent == [(24, bytes([0x94, 60, 100]))]


def test_a_message_from_an_unattached_client_is_ignored(env):
    seq, *_, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    seq.incoming = [(99, NOTE), (24, NOTE)]  # 24 is attached but sends nowhere in this patch
    assert svc.pump() == 0 and seq.sent == []


def test_only_units_in_the_patch_are_attached(env):
    seq, *_, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    assert 28 not in seq.connected  # unit C is not in the patch, so its port stays free


def test_a_missing_unit_is_reported_and_the_rest_still_route(env):
    seq, present, holds, logs, svc = env
    del present["fm1-b"]
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"), rt("fm1-a", "fm1-c"))))
    assert svc.missing == {"fm1-b"} and svc.attached == {"fm1-a": 20, "fm1-c": 28}
    seq.incoming = [(20, NOTE)]
    assert svc.pump() == 1 and seq.sent == [(28, NOTE)]


def test_a_unit_that_arrives_later_is_attached_on_the_next_refresh(env):
    seq, present, holds, logs, svc = env
    del present["fm1-b"]
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    present["fm1-b"] = 24
    svc.refresh()
    assert svc.attached["fm1-b"] == 24 and not svc.missing


def test_a_replugged_unit_with_a_new_client_number_is_reattached(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    present["fm1-b"] = 40
    svc.refresh()
    assert 24 not in seq.connected and 40 in seq.connected
    seq.incoming = [(20, NOTE)]
    svc.pump()
    assert seq.sent == [(40, NOTE)]


def test_a_failed_attach_is_logged_and_does_not_stop_the_others(env):
    seq, present, holds, logs, svc = env
    seq.fail_connect.add(24)
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"), rt("fm1-a", "fm1-c"))))
    assert "fm1-b" in svc.missing and "fm1-c" in svc.attached
    assert any("could not attach fm1-b" in m for m in logs)


def test_a_failed_send_is_logged_and_the_service_keeps_going(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"), rt("fm1-a", "fm1-c"))))
    seq.fail_send.add(24)
    seq.incoming = [(20, NOTE)]
    assert svc.pump() == 1  # the send to C still went
    assert any("send to fm1-b failed" in m for m in logs)


def test_a_held_unit_is_released_at_once_and_taken_back_when_the_hold_ends(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    holds.add("fm1-b")
    svc.refresh(rescan=False)
    assert 24 not in seq.connected and "fm1-b" in svc.held
    assert 20 in seq.connected, "units that are not held keep routing"
    seq.incoming = [(20, NOTE)]
    assert svc.pump() == 0 and seq.sent == []
    holds.clear()
    svc.refresh(rescan=False)
    assert 24 in seq.connected and not svc.held


def test_a_hold_on_a_unit_outside_the_patch_changes_nothing(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    holds.add("fm1-c")
    svc.refresh(rescan=False)
    assert svc.held == set() and seq.connected == {20, 24}


def test_panic_goes_to_every_attached_unit(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    assert svc.panic() == 2 * 48
    assert {c for c, _ in seq.sent} == {20, 24}


def test_release_all_lets_go_of_every_unit(env):
    seq, *_, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    svc.release_all()
    assert seq.connected == set() and svc.attached == {}


def test_sysex_split_across_events_is_joined_before_routing(env):
    seq, *_, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    seq.incoming = [(20, bytes([0xF0, 0x7D, 0x01])), (20, bytes([0x02, 0xF7]))]
    assert svc.pump() == 1
    assert seq.sent == [(24, bytes([0xF0, 0x7D, 0x01, 0x02, 0xF7]))]


def test_run_attaches_routes_and_lets_go_on_stop(env):
    seq, present, holds, logs, svc = env
    svc.patch = Patch("p", (rt("fm1-a", "fm1-b"),))
    stop = threading.Event()
    seq.incoming = []
    t = threading.Thread(target=svc.run, args=(stop,), kwargs=dict(rescan_every=0.05,
                                                                    holds_every=0.01))
    t.start()
    deadline = time.time() + 2
    while 20 not in seq.connected and time.time() < deadline:
        time.sleep(0.01)
    assert seq.connected == {20, 24}
    stop.set()
    t.join(2)
    assert not t.is_alive() and seq.connected == set()
    assert not (svc.status_file).exists(), "the status file goes when the router stops"


# ------------------------------------------------------------------------- the codec glue

def test_the_assembler_passes_channel_messages_and_realtime_through_a_open_sysex():
    a = SysexAssembler()
    assert a.feed(1, bytes([0x90, 60, 100])) == [bytes([0x90, 60, 100])]
    assert a.feed(1, bytes([0xF0, 1, 2])) == []
    assert a.feed(1, bytes([0xF8])) == [bytes([0xF8])]  # clock inside a sysex
    assert a.feed(1, bytes([3, 0xF7])) == [bytes([0xF0, 1, 2, 3, 0xF7])]


def test_a_new_status_byte_abandons_an_unfinished_sysex():
    a = SysexAssembler()
    a.feed(1, bytes([0xF0, 1, 2]))
    assert a.feed(1, bytes([0x90, 60, 100])) == [bytes([0x90, 60, 100])]
    assert a.feed(1, bytes([5, 0xF7])) == [bytes([5, 0xF7])]  # not a sysex any more


def test_sysex_from_two_sources_do_not_mix_and_too_long_ones_are_dropped():
    a = SysexAssembler(limit=8)
    a.feed(1, bytes([0xF0, 1]))
    a.feed(2, bytes([0xF0, 9]))
    assert a.feed(1, bytes([2, 0xF7])) == [bytes([0xF0, 1, 2, 0xF7])]
    assert a.feed(2, bytes([8, 0xF7])) == [bytes([0xF0, 9, 8, 0xF7])]
    a.feed(3, bytes([0xF0, 1, 2, 3, 4]))
    assert a.feed(3, bytes([5, 6, 7, 8])) == []
    assert a.feed(3, bytes([0xF7])) == [bytes([0xF7])]  # the long one is gone


# ------------------------------------------------------------------- holds across processes

def test_holds_expire_and_clear(tmp_path):
    h = tmp_path / "holds.json"
    router_service.place_hold(["fm1-a", "fm1-b"], ttl=60, path=h)
    assert router_service.active_holds(path=h) == {"fm1-a", "fm1-b"}
    router_service.clear_hold(["fm1-a"], path=h)
    assert router_service.active_holds(path=h) == {"fm1-b"}
    assert router_service.active_holds(now=time.time() + 120, path=h) == set()
    router_service.place_hold(["fm1-c"], ttl=-1, path=h)  # already expired
    assert "fm1-c" not in router_service.active_holds(path=h)


def test_a_broken_holds_file_means_no_holds(tmp_path):
    h = tmp_path / "holds.json"
    h.write_text("{not json")
    assert router_service.active_holds(path=h) == set()
    router_service.place_hold(["fm1-a"], path=h)  # and a new hold repairs it
    assert router_service.active_holds(path=h) == {"fm1-a"}


def test_hold_and_wait_returns_at_once_when_no_router_is_running(tmp_path):
    t0 = time.monotonic()
    assert router_service.hold_and_wait(["fm1-a"], hpath=tmp_path / "h.json",
                                        spath=tmp_path / "s.json", timeout=5)
    assert time.monotonic() - t0 < 1
    assert router_service.active_holds(path=tmp_path / "h.json") == {"fm1-a"}


def test_hold_and_wait_waits_for_a_running_router_to_release_the_unit(tmp_path):
    h, s = tmp_path / "h.json", tmp_path / "s.json"
    seq = FakeSeq()
    svc = RouterService(seq, lambda: {"fm1-a": 20, "fm1-b": 24},
                        lambda: router_service.active_holds(path=h), status_file=s)
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    assert router_service.router_running(path=s)
    stop = threading.Event()
    t = threading.Thread(target=svc.run, args=(stop,),
                         kwargs=dict(rescan_every=0.05, holds_every=0.01))
    t.start()
    try:
        assert router_service.hold_and_wait(["fm1-b"], hpath=h, spath=s, timeout=3)
        assert 24 not in seq.connected, "the unit was released before the call returned"
        router_service.clear_hold(["fm1-b"], path=h)
        deadline = time.time() + 2
        while 24 not in seq.connected and time.time() < deadline:
            time.sleep(0.01)
        assert 24 in seq.connected, "and taken back after the hold was cleared"
    finally:
        stop.set()
        t.join(2)


def test_hold_and_wait_gives_up_and_says_so_when_the_router_does_not_let_go(tmp_path):
    h, s = tmp_path / "h.json", tmp_path / "s.json"
    router_service._write_json(s, {"updated": time.time(), "attached": ["fm1-a"]})
    assert router_service.hold_and_wait(["fm1-a"], hpath=h, spath=s, timeout=0.3,
                                        poll=0.05) is False
    # a status file that stopped updating means the router is gone
    router_service._write_json(s, {"updated": time.time() - 60, "attached": ["fm1-a"]})
    assert router_service.hold_and_wait(["fm1-a"], hpath=h, spath=s, timeout=0.3) is True


def test_patches_name_units_by_key_so_the_registry_resolver_keys_match(tmp_path):
    # the resolver keys units by sticker slot, which is what a patch's ports are
    assert Patch("p", (rt("fm1-a", "fm1-b"),)).ports() == {"fm1-a", "fm1-b"}
    assert router.letter_channel_ranges(["A", "B"])["A"] == (1, 2, 3, 4)


# --------------------------------------------------------------- the soft key lets go first

def test_the_all_units_hold_frees_every_unit_in_the_patch(env):
    seq, present, holds, logs, svc = env
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    holds.add(router_service.ALL_UNITS)
    svc.refresh(rescan=False)
    assert seq.connected == set() and svc.held == {"fm1-a", "fm1-b"}
    holds.clear()
    svc.refresh(rescan=False)
    assert seq.connected == {20, 24}


def test_soft_key_with_no_router_writes_no_hold_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_ROUTER_HOLDS", str(tmp_path / "holds.json"))
    monkeypatch.setenv("FM1_ROUTER_STATUS", str(tmp_path / "status.json"))
    with router_service.released_for_soft_key():
        pass
    assert not (tmp_path / "holds.json").exists()


def test_soft_key_waits_for_the_router_and_clears_its_hold_after(tmp_path, monkeypatch):
    h, s = tmp_path / "holds.json", tmp_path / "status.json"
    monkeypatch.setenv("FM1_ROUTER_HOLDS", str(h))
    monkeypatch.setenv("FM1_ROUTER_STATUS", str(s))
    seq = FakeSeq()
    svc = RouterService(seq, lambda: {"fm1-a": 20, "fm1-b": 24},
                        lambda: router_service.active_holds(), status_file=s)
    svc.set_patch(Patch("p", (rt("fm1-a", "fm1-b"),)))
    stop = threading.Event()
    t = threading.Thread(target=svc.run, args=(stop,),
                         kwargs=dict(rescan_every=0.05, holds_every=0.01))
    t.start()
    try:
        with router_service.released_for_soft_key():
            assert seq.connected == set(), "every unit is free while the soft key is written"
        deadline = time.time() + 2
        while seq.connected != {20, 24} and time.time() < deadline:
            time.sleep(0.01)
        assert seq.connected == {20, 24}, "and routing resumes afterwards"
        assert router_service.active_holds() == set()
    finally:
        stop.set()
        t.join(2)


def test_a_router_problem_never_blocks_the_soft_key(tmp_path, monkeypatch):
    monkeypatch.setattr(router_service, "router_running",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ran = []
    with router_service.released_for_soft_key():
        ran.append(True)
    assert ran == [True]


def test_units_in_always_are_attached_without_any_route(env):
    seq, *_, svc = env
    svc.always = {"fm1-c"}
    svc.refresh()
    assert seq.connected == {28}
    assert svc.panic() == 48


def test_run_follows_a_reloaded_patch_without_a_restart(env):
    seq, present, holds, logs, svc = env
    patches = iter([Patch("waiting"), None, None, Patch("rig", (rt("fm1-a", "fm1-b"),))])
    stop = threading.Event()
    t = threading.Thread(target=svc.run, args=(stop,), kwargs=dict(
        rescan_every=0.03, holds_every=0.01, reload=lambda: next(patches, None)))
    t.start()
    deadline = time.time() + 3
    while seq.connected != {20, 24} and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(2)
    assert svc.patch.name == "rig"
    assert any("patch 'rig': 1 route(s)" in m for m in logs)
    assert seq.connected == set(), "and it still lets go on stop"
