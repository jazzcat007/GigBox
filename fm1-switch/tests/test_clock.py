"""The master clock: timing logic, settings file, service integration and CLI.

No ALSA and no unit is touched; the service runs against the fake sequencer.
"""

from __future__ import annotations

import json
import threading

import pytest

from fm1_switch import __main__ as cli
from fm1_switch import clock as clocklib
from fm1_switch import router_service
from fm1_switch.clock import CLOCK, START, STOP, ClockError, ClockSettings, MasterClock
from fm1_switch.router import Patch
from fm1_switch.router_service import ClockFollower, RouterService
from test_router_service import FakeSeq


class Time:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ------------------------------------------------------------------------------ timing

def test_120_bpm_is_48_ticks_per_second():
    t = Time()
    c = MasterClock(t)
    assert c.set(ClockSettings(120.0, True)) == START
    total = 0
    for _ in range(1000):  # one second in 1 ms steps
        t.t += 0.001
        total += c.due()
    assert total in (47, 48)


def test_ticks_are_scheduled_on_a_fixed_grid_so_a_late_poll_does_not_drift():
    t = Time()
    c = MasterClock(t)
    c.set(ClockSettings(125.0, True))  # interval 20 ms
    start = t.t
    total = 0
    for i in range(1, 51):  # poll late and unevenly for 1 s
        t.t = start + i * 0.02 + (0.007 if i % 2 else 0.0)
        total += c.due()
    assert total == 50


def test_nothing_is_due_while_stopped_and_stop_is_returned_once():
    t = Time()
    c = MasterClock(t)
    assert c.due() == 0 and c.deadline() is None
    assert c.set(ClockSettings(100.0, True)) == START
    assert c.set(ClockSettings(100.0, True)) is None
    assert c.set(ClockSettings(100.0, False)) == STOP
    assert c.set(ClockSettings(100.0, False)) is None
    t.t += 10
    assert c.due() == 0


def test_a_long_stall_drops_the_backlog_instead_of_bursting():
    t = Time()
    c = MasterClock(t)
    c.set(ClockSettings(120.0, True))
    t.t += 5.0
    assert c.due() == clocklib.MAX_CATCH_UP and c.dropped > 0
    assert c.due() == 0  # the grid restarted from now


def test_a_tempo_change_applies_from_the_next_tick():
    t = Time()
    c = MasterClock(t)
    c.set(ClockSettings(60.0, True))
    first = c.deadline()
    c.set(ClockSettings(120.0, True))
    assert c.deadline() == first  # the pending tick is not moved
    t.t = first
    assert c.due() == 1
    assert c.deadline() == pytest.approx(first + 60.0 / (120 * 24))


def test_wait_never_exceeds_the_limit_or_goes_negative():
    t = Time()
    c = MasterClock(t)
    assert c.wait(0.05) == 0.05
    c.set(ClockSettings(120.0, True))
    assert 0 < c.wait(0.05) <= 0.05
    t.t += 1
    assert c.wait(0.05) == 0.0


# --------------------------------------------------------------------------- settings

@pytest.mark.parametrize("bad", [True, "120", None, 19.9, 300.1, float("nan")])
def test_a_bad_tempo_is_refused(bad):
    with pytest.raises(ClockError):
        clocklib.check_bpm(bad)


def test_settings_round_trip_and_defaults(tmp_path):
    p = tmp_path / "clock.json"
    assert clocklib.load(p) == ClockSettings()
    st = clocklib.update(bpm=133.33, running=True, targets=["fm1-b", "fm1-a", "fm1-a"],
                         valid_targets={"fm1-a", "fm1-b"}, path=p)
    assert st == ClockSettings(133.3, True, ("fm1-a", "fm1-b"))
    assert clocklib.load(p) == st


def test_an_unknown_target_is_refused_and_nothing_is_saved(tmp_path):
    p = tmp_path / "clock.json"
    with pytest.raises(ClockError):
        clocklib.update(targets=["fm1-z"], valid_targets={"fm1-a"}, path=p)
    assert not p.exists()


def test_a_damaged_file_is_an_error_to_load_but_update_replaces_it(tmp_path):
    p = tmp_path / "clock.json"
    p.write_text("{not json")
    with pytest.raises(ClockError):
        clocklib.load(p)
    clocklib.update(bpm=90, path=p)
    assert clocklib.load(p).bpm == 90.0


# ---------------------------------------------------------------------------- service

@pytest.fixture
def env(tmp_path):
    seq = FakeSeq()
    present = {"fm1-a": 20, "fm1-b": 24, "fm1-c": 28}
    holds: set[str] = set()
    logs: list[str] = []
    svc = RouterService(seq, lambda: dict(present), lambda: set(holds), logs.append,
                        status_file=tmp_path / "status.json")
    t = Time()
    svc.clock = MasterClock(t)
    svc.refresh()
    return seq, present, holds, logs, svc, t


def bytes_to(seq, client):
    return [d for c, d in seq.sent if c == client]


def test_start_attaches_every_unit_and_sends_start(env):
    seq, present, holds, logs, svc, t = env
    assert seq.connected == set()  # no patch routes anything
    svc.apply_clock(ClockSettings(120.0, True))
    assert seq.connected == {20, 24, 28}
    for client in (20, 24, 28):
        assert bytes_to(seq, client) == [bytes([START])]


def test_ticks_go_only_to_the_targets(env):
    seq, present, holds, logs, svc, t = env
    svc.apply_clock(ClockSettings(120.0, True, ("fm1-b",)))
    assert seq.connected == {24}
    seq.sent.clear()
    t.t += 0.1
    n = svc.tick()
    assert n >= 4 and bytes_to(seq, 24) == [bytes([CLOCK])] * n
    assert bytes_to(seq, 20) == []


def test_stop_is_sent_before_the_units_are_let_go(env):
    seq, present, holds, logs, svc, t = env
    svc.apply_clock(ClockSettings(120.0, True, ("fm1-a",)))
    seq.sent.clear()
    svc.apply_clock(ClockSettings(120.0, False, ("fm1-a",)))
    assert seq.sent == [(20, bytes([STOP]))]
    assert seq.connected == set()  # nothing else needs unit A, so its port is free again


def test_a_unit_a_route_needs_stays_attached_after_stop(env):
    seq, present, holds, logs, svc, t = env
    from fm1_switch.router import Route
    svc.set_patch(Patch("p", (Route("fm1-a", "fm1-b"),)))
    svc.apply_clock(ClockSettings(120.0, True))
    svc.apply_clock(ClockSettings(120.0, False))
    assert seq.connected == {20, 24}


def test_changing_targets_while_running_starts_and_stops_the_right_units(env):
    seq, present, holds, logs, svc, t = env
    svc.apply_clock(ClockSettings(120.0, True, ("fm1-a",)))
    seq.sent.clear()
    svc.apply_clock(ClockSettings(120.0, True, ("fm1-b",)))
    assert bytes_to(seq, 24) == [bytes([START])]
    assert seq.sent[0] == (20, bytes([STOP]))  # A was released only after it was stopped


def test_a_held_unit_gets_nothing_and_comes_back_when_the_hold_ends(env):
    seq, present, holds, logs, svc, t = env
    svc.apply_clock(ClockSettings(120.0, True, ("fm1-a", "fm1-b")))
    holds.add(router_service.ALL_UNITS)
    svc.refresh(rescan=False)
    assert seq.connected == set() and svc.held == {"fm1-a", "fm1-b"}
    seq.sent.clear()
    t.t += 0.2
    svc.tick()  # nothing attached: nothing sent, nothing raised
    assert seq.sent == []
    holds.clear()
    svc.refresh(rescan=False)
    assert seq.connected == {20, 24}


def test_a_send_failure_is_logged_once_and_does_not_stop_the_others(env):
    seq, present, holds, logs, svc, t = env
    seq.fail_send.add(20)
    svc.apply_clock(ClockSettings(120.0, True))
    t.t += 0.5
    svc.tick()
    assert sum("clock to fm1-a failed" in m for m in logs) == 1
    assert bytes_to(seq, 24)[-1] == bytes([CLOCK])


def test_status_reports_the_clock(env):
    seq, present, holds, logs, svc, t = env
    svc.apply_clock(ClockSettings(98.5, True, ("fm1-a",)))
    assert svc.status()["clock"]["bpm"] == 98.5 and svc.status()["clock"]["running"] is True


def test_run_loop_follows_the_clock_file_and_stops_the_clock_on_exit(tmp_path):
    seq = FakeSeq()
    path = tmp_path / "clock.json"
    svc = RouterService(seq, lambda: {"fm1-a": 20}, lambda: set(), lambda m: None,
                        status_file=tmp_path / "status.json")
    follower = ClockFollower(path=path)
    stop = threading.Event()
    clocklib.save(ClockSettings(300.0, True, ("fm1-a",)), path)
    original = svc.pump

    def pump_then_stop(timeout=0.1):
        out = original(0.0)
        if svc.clock.ticks_sent >= 5:
            stop.set()
        return out

    svc.pump = pump_then_stop
    svc.run(stop, rescan_every=0.05, holds_every=0.01, clock=follower)
    sent = bytes_to(seq, 20)
    assert sent[0] == bytes([START]) and sent.count(bytes([CLOCK])) >= 5
    assert sent[-1] == bytes([STOP])  # the router going away stops the clock


def test_a_router_start_does_not_start_the_units(tmp_path):
    path = tmp_path / "clock.json"
    clocklib.save(ClockSettings(110.0, True, ("fm1-a",)), path)
    f = ClockFollower(path=path)
    f.start_stopped()
    assert clocklib.load(path) == ClockSettings(110.0, False, ("fm1-a",))


def test_the_follower_reports_a_change_once_and_keeps_going_on_a_damaged_file(tmp_path):
    path = tmp_path / "clock.json"
    logs: list[str] = []
    f = ClockFollower(logs.append, path)
    assert f() == ClockSettings()  # no file: the default, once
    assert f() is None
    clocklib.update(bpm=140, path=path)
    assert f().bpm == 140.0 and f() is None
    path.write_text("{oops")
    assert f() is None and any("unusable" in m for m in logs)


# -------------------------------------------------------------------------------- CLI

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_CLOCK", str(tmp_path / "clock.json"))
    monkeypatch.setenv("FM1_ROUTER_STATUS", str(tmp_path / "status.json"))
    return tmp_path / "clock.json"


def test_cli_set_start_stop_and_targets(cli_env, capsys):
    assert cli.main(["clock", "set", "128"]) == 0
    assert cli.main(["clock", "targets", "fm1-a", "fm1-c"]) == 0
    assert cli.main(["clock", "start"]) == 0
    assert json.loads(cli_env.read_text()) == {"bpm": 128.0, "running": True,
                                               "targets": ["fm1-a", "fm1-c"]}
    capsys.readouterr()
    assert cli.main(["clock"]) == 0
    out = capsys.readouterr().out
    assert "128 BPM, running, to fm1-a, fm1-c" in out and "router is not running" in out
    assert cli.main(["clock", "stop"]) == 0
    assert json.loads(cli_env.read_text())["running"] is False


@pytest.mark.parametrize("argv, code", [
    (["clock", "set", "fast"], 1), (["clock", "set", "5"], 1),
    (["clock", "targets", "fm1-z"], 1), (["clock", "set"], 2), (["clock", "dance"], 2),
])
def test_cli_refuses_bad_input(cli_env, argv, code):
    assert cli.main(argv) == code
    assert not cli_env.exists()
