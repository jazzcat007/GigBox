"""Automatic re-register after a power-up: the rules, the registry check, the loop, the hook.
No hardware and no real sleep."""

from __future__ import annotations

import fcntl
import threading

import pytest

from fm1_switch import autoreg, guided, registry, web
from fm1_switch.autoreg import GAVE_UP, RUN, AutoReregister, Policy, View

P = Policy(settle=20, retry_after=120, max_attempts=2, calm_reset=60)
SIG1 = (("1-1", "5"), ("1-2", "6"))
SIG2 = (("1-1", "7"), ("1-2", "8"))
NEED = lambda sig=SIG1: View(True, sig, False)  # noqa: E731
OK = View(False, SIG1, False)


# ------------------------------------------------------------------------------ the rules

def test_it_waits_for_the_bus_to_settle_before_acting():
    ar = AutoReregister(P)
    assert ar.step(0, NEED()) is None        # first sight starts the wait
    assert ar.step(19, NEED()) is None
    assert ar.step(20, NEED()) == RUN


def test_a_unit_powering_up_late_restarts_the_wait():
    ar = AutoReregister(P)
    ar.step(0, NEED(SIG1))
    assert ar.step(15, NEED(SIG2)) is None   # the bus changed: wait again from 15
    assert ar.step(34, NEED(SIG2)) is None
    assert ar.step(35, NEED(SIG2)) == RUN


def test_nothing_to_do_means_nothing_happens():
    ar = AutoReregister(P)
    for t in range(0, 300, 5):
        assert ar.step(t, OK) is None


def test_it_never_acts_while_a_write_or_download_mode_unit_is_there():
    ar = AutoReregister(P)
    busy = View(True, SIG1, True)
    for t in range(0, 200, 5):
        assert ar.step(t, busy) is None
    # and the wait starts only once the unit has gone
    assert ar.step(200, NEED()) is None
    assert ar.step(219, NEED()) is None
    assert ar.step(220, NEED()) == RUN


def test_it_does_not_loop_on_a_unit_it_cannot_match():
    ar = AutoReregister(P)
    runs = []
    t = 0
    for i in range(400):
        t += 5
        # every restart changes the bus, so the signature changes after each run
        sig = (("1-1", str(len(runs))),)
        a = ar.step(t, NEED(sig))
        if a == RUN:
            runs.append(t)
        elif a == GAVE_UP:
            runs.append("gave up")
    assert runs.count("gave up") == 1 and len([r for r in runs if r != "gave up"]) == 2


def test_the_attempts_come_back_after_a_calm_minute():
    ar = AutoReregister(Policy(settle=5, max_attempts=1, retry_after=10, calm_reset=60))
    ar.step(0, NEED())
    assert ar.step(5, NEED()) == RUN
    ar.step(10, NEED(SIG2))
    assert ar.step(20, NEED(SIG2)) == GAVE_UP
    for t in range(25, 90, 5):
        ar.step(t, OK)
    assert ar.attempts == 0
    ar.step(100, NEED())
    assert ar.step(105, NEED()) == RUN


def test_switched_off_does_nothing():
    ar = AutoReregister(Policy(enabled=False))
    assert ar.step(0, NEED()) is None and ar.step(1000, NEED()) is None


# ------------------------------------------------------------------ the registry's question

def _units(monkeypatch, bindings, runs):
    reg = registry.Registry()
    for slot, (uid, fp, port, dn) in bindings.items():
        reg.units[slot] = registry.Binding(slot=slot, unit_id=uid, fingerprint=fp, port=port,
                                           devnum=dn)
    monkeypatch.setattr(registry, "load", lambda: reg)
    monkeypatch.setattr(registry, "running", lambda: [
        registry.RunningUnit(port=p, vendor_product="1209:0001", product="Felucca",
                             fingerprint=fp, devnum=dn) for p, fp, dn in runs])


def test_a_power_cycle_of_look_alikes_asks_for_matching(monkeypatch):
    # same ports, same fingerprints, but every device number changed
    _units(monkeypatch,
           {"fm1-a": ("a" * 16, "0320", "1-1", "41"), "fm1-c": ("c" * 16, "0320", "1-2", "37")},
           [("1-1", "0320", "47"), ("1-2", "0320", "49")])
    assert registry.needs_auto_matching() is True


def test_nothing_changed_means_no_matching(monkeypatch):
    _units(monkeypatch,
           {"fm1-a": ("a" * 16, "0320", "1-1", "41"), "fm1-c": ("c" * 16, "0320", "1-2", "37")},
           [("1-1", "0320", "41"), ("1-2", "0320", "37")])
    assert registry.needs_auto_matching() is False


def test_a_unique_fingerprint_needs_no_proof(monkeypatch):
    _units(monkeypatch, {"fm1-b": ("b" * 16, "03a0", "1-3", "31")}, [("1-3", "03a0", "99")])
    assert registry.needs_auto_matching() is False


def test_a_lone_new_unit_is_registers_job_not_ours(monkeypatch):
    _units(monkeypatch, {"fm1-b": ("b" * 16, "03a0", "1-3", "31")},
           [("1-3", "03a0", "31"), ("1-4", "0111", "5")])
    assert registry.needs_auto_matching() is False   # a stray, but no letter is missing


def test_moved_cables_with_a_missing_letter_asks_for_matching(monkeypatch):
    _units(monkeypatch, {"fm1-a": ("a" * 16, "0320", "1-1", "41")}, [("1-9", "0555", "5")])
    assert registry.needs_auto_matching() is True


def test_the_signature_changes_when_a_unit_re_enumerates(monkeypatch):
    _units(monkeypatch, {}, [("1-1", "0320", "47")])
    a = registry.running_signature()
    _units(monkeypatch, {}, [("1-1", "0320", "52")])
    assert registry.running_signature() != a


# ------------------------------------------------------------------------- loop and server

def test_the_loop_runs_reconcile_once_the_bus_settles_and_survives_a_failing_check():
    t = [0.0]
    stop = threading.Event()
    calls, logs, ticks = [], [], [0]

    class Stop:
        def is_set(self):
            return stop.is_set()

        def wait(self, _):
            t[0] += 5
            ticks[0] += 1
            if ticks[0] >= 12:
                stop.set()

    def view():
        if ticks[0] == 1:
            raise OSError("sysfs hiccup")
        return NEED()

    autoreg.watch(Stop(), policy_fn=lambda: P, view_fn=view,
                  reconcile_fn=lambda: calls.append(t[0]) or "matched A C",
                  log=logs.append, clock=lambda: t[0])
    assert len(calls) == 1
    assert any("check failed" in l for l in logs) and any("matched A C" in l for l in logs)


def test_when_switched_off_the_loop_reads_nothing():
    stop = threading.Event()
    n = [0]

    class Stop:
        def is_set(self):
            return stop.is_set()

        def wait(self, _):
            n[0] += 1
            stop.set() if n[0] > 3 else None

    def boom():
        raise AssertionError("must not look at the bus when off")

    autoreg.watch(Stop(), policy_fn=lambda: Policy(enabled=False), view_fn=boom,
                  reconcile_fn=boom)


def test_the_server_hook_logs_the_result_and_frees_the_busy_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    out = guided.ReconcileOutcome()
    out.message, out.matched = "matched A C", [("fm1-a", "x", "1-1"), ("fm1-c", "y", "1-2")]
    monkeypatch.setattr(guided, "reconcile", lambda **kw: out)
    assert web._auto_reconcile() == "matched A C"
    assert web.APP.busy is None
    assert "auto: matched A C" in (tmp_path / "flash.log").read_text()


def test_the_server_hook_steps_aside_when_the_other_server_is_doing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    monkeypatch.setattr(guided, "reconcile", lambda **kw: pytest.fail("must not run"))
    with open(tmp_path / "fm1-autoreg.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert "already doing it" in web._auto_reconcile()


def test_a_busy_lock_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))

    def refuse(**kw):
        raise guided.Busy("a flash is running; wait for it to finish")
    monkeypatch.setattr(guided, "reconcile", refuse)
    assert web._auto_reconcile().startswith("not done:")


def test_the_view_says_busy_while_a_unit_is_in_download_mode(monkeypatch):
    monkeypatch.setattr(guided, "download_devices", lambda: [guided.Device("/dev/sg0", "1-1")])
    monkeypatch.setattr(registry, "needs_auto_matching", lambda: pytest.fail("must not look"))
    assert web._auto_view().busy is True
