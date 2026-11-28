"""The WiFi fallback end to end, synthetically: the real ``fm1-switch net`` commands (the service
loop, the timed hotspot, the status report) run against a stand-in ``nmcli`` and ``iw``
(tests/tools/fake_nm.py) whose "world" the test changes while they run. No network is touched.

Slow (about two minutes, it waits in real time), so it only runs with ``FM1_SLOW=1``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fm1_switch import netctl

pytestmark = pytest.mark.skipif(not os.environ.get("FM1_SLOW"),
                                reason="slow synthetic test: set FM1_SLOW=1")

FAKE = Path(__file__).parent / "tools" / "fake_nm.py"
HOT = "fm1-hotspot"


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state_file = tmp / "state.json"
        self.calls = tmp / "calls.log"
        self.calls.write_text("")
        for tool in ("nmcli", "iw"):
            w = tmp / tool
            w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {tool} "$@"\n')
            w.chmod(w.stat().st_mode | stat.S_IEXEC)
        self.set(state="connected", conn="Home", in_range=["Home"], saved=["Home", "Home-2"],
                 phones=0, has_hotspot=False, fail_hotspot=False)
        self.env = {
            **os.environ,
            "FM1_NMCLI": str(tmp / "nmcli"), "FM1_IW": str(tmp / "iw"),
            "FAKE_NM_STATE": str(self.state_file), "FAKE_NM_LOG": str(self.calls),
            "FM1_CONFIG": str(tmp / "config.json"), "FM1_HOTSPOT_KEY": str(tmp / "key"),
            "FM1_NET_STATUS": str(tmp / "status.json"), "FM1_NET_INTERVAL": "0.5",
            "PYTHONPATH": str(Path(__file__).parent.parent),
        }
        self.config(net_fallback_after=10, net_retry_home_every=0.2, net_mode="auto")
        netctl.write_passphrase("a-changed-phrase", tmp / "key")   # the published default is refused

    def config(self, **kw):
        cfg = {"net_iface": "wlan0", **kw}
        (self.tmp / "config.json").write_text(json.dumps(cfg))

    def get(self) -> dict:
        return json.loads(self.state_file.read_text())

    def set(self, **kw):
        with open(str(self.state_file) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            st.update(kw)
            self.state_file.write_text(json.dumps(st))

    def ran(self, text: str) -> int:
        return sum(1 for ln in self.calls.read_text().splitlines() if text in ln)

    def cli(self, *args, **kw):
        return subprocess.Popen([sys.executable, "-m", "fm1_switch", "net", *args],
                                env=self.env, stdout=kw.pop("stdout", subprocess.PIPE),
                                stderr=subprocess.STDOUT, text=True, **kw)


def wait_for(pred, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.returncode


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_connected_pi_is_left_alone_then_falls_back_and_returns_when_nobody_is_on_it(world):
    w = world
    st = w.cli("status")
    out = st.communicate(timeout=30)[0]
    assert "adapter wlan0: present, connected, on Home" in out and "saved networks: Home" in out

    watcher = w.cli("watch", stdout=subprocess.DEVNULL)
    try:
        time.sleep(11)  # longer than the wait: a connected Pi must not raise the hotspot
        assert w.ran(f"up {HOT}") == 0

        w.set(state="disconnected", conn="", in_range=[])          # the house WiFi vanishes
        wait_for(lambda: w.get()["conn"] == HOT, 30, "the hotspot after the link was lost")
        assert w.get()["has_hotspot"] is True
        status = json.loads((w.tmp / "status.json").read_text())
        assert status["state"] in ("hotspot", "client", "disconnected")
        wait_for(lambda: json.loads((w.tmp / "status.json").read_text())["state"] == "hotspot",
                 5, "the status file to say hotspot")

        w.set(phones=1, in_range=["Home"])                         # a phone joins, house returns
        time.sleep(16)                                             # past the retry interval
        assert w.get()["conn"] == HOT and w.ran("up Home") == 0, "kicked a joined phone off"

        w.set(phones=0)                                            # the phone leaves
        wait_for(lambda: w.get()["conn"] == "Home", 30, "going back to the saved network")
        time.sleep(3)
        assert w.get()["conn"] == "Home" and w.ran(f"up {HOT}") == 1
    finally:
        assert stop(watcher) in (0, -signal.SIGTERM)
    assert not (w.tmp / "nonexistent").exists()


def test_a_failed_return_puts_the_hotspot_straight_back(world):
    w = world
    w.set(state="connected", conn=HOT, in_range=[], has_hotspot=True, phones=0)
    watcher = w.cli("watch", stdout=subprocess.DEVNULL)
    try:
        wait_for(lambda: w.ran("up Home") >= 1, 30, "the first try at a saved network")
        wait_for(lambda: w.get()["conn"] == HOT, 10, "the hotspot back up after the failed try")
    finally:
        stop(watcher)


def test_field_mode_raises_the_hotspot_even_while_connected(world):
    w = world
    w.config(net_mode="field", net_fallback_after=10)
    watcher = w.cli("watch", stdout=subprocess.DEVNULL)
    try:
        wait_for(lambda: w.get()["conn"] == HOT, 15, "the hotspot in field mode")
    finally:
        stop(watcher)


def test_the_published_default_passphrase_keeps_the_hotspot_down_under_refuse(world):
    w = world
    w.config(net_mode="auto", net_fallback_after=10, net_default_passphrase="refuse")
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, w.tmp / "key")
    w.set(state="disconnected", conn="", in_range=[])
    watcher = w.cli("watch", stdout=subprocess.DEVNULL)
    try:
        wait_for(lambda: (w.tmp / "status.json").exists() and
                 json.loads((w.tmp / "status.json").read_text()).get("blocked") == "default-passphrase",
                 30, "the supervisor to report that it is blocked")
        assert w.ran(f"up {HOT}") == 0 and w.get()["state"] == "disconnected"
    finally:
        stop(watcher)


def test_home_mode_never_raises_it(world):
    w = world
    w.config(net_mode="home", net_fallback_after=10)
    w.set(state="disconnected", conn="", in_range=[])
    watcher = w.cli("watch", stdout=subprocess.DEVNULL)
    try:
        time.sleep(14)
        assert w.ran(f"up {HOT}") == 0 and w.get()["state"] == "disconnected"
    finally:
        stop(watcher)


def test_the_timed_hotspot_restores_the_network_even_when_the_terminal_is_gone(world):
    """`net up` is what the owner runs over SSH. The SSH session dies with the WiFi: the hangup and a
    closed terminal must not stop the saved network coming back."""
    w = world
    p = w.cli("up", "--yes", "--for", "20")
    wait_for(lambda: w.get()["conn"] == HOT, 20, "the timed hotspot")
    p.send_signal(signal.SIGHUP)       # what a dropped SSH session sends
    p.stdout.close()                   # and the terminal goes away
    wait_for(lambda: p.poll() is not None, 45, "the command to finish")
    assert w.get()["conn"] == "Home", "the saved network was not put back"
    assert w.ran("up Home") >= 1
    assert p.returncode == 0


def test_the_timed_hotspot_raises_the_hotspot_again_if_no_saved_network_answers(world):
    w = world
    w.set(in_range=[])
    p = w.cli("up", "--yes", "--for", "20")
    out = p.communicate(timeout=60)[0]
    assert p.returncode == 1
    assert "NO saved network connected" in out and "the hotspot goes back up" in out
    assert w.get()["conn"] == HOT       # the Pi stays reachable


# ------------------------------------------------------------- the bench script, dry run

BENCH = Path(__file__).parent.parent / "tools" / "bench_wifi.py"


def run_bench(w: World, *extra, phone_joins=True):
    w.set(has_hotspot=True)                       # `net setup` has been run
    p = subprocess.Popen(
        [sys.executable, str(BENCH), "--no-service", "--after", "10", "--retry-min", "0.2",
         "--hold", "20", "--phone-wait", "30", "--return-wait", "60", "--interval", "0.5",
         "--timeout", "240", *extra],
        env=w.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in p.stdout:
        lines.append(line.rstrip())
        if "NOW: join" in line and phone_joins:
            w.set(phones=1)
        if "NOW: disconnect the phone" in line:
            w.set(phones=0)
    p.wait(timeout=30)
    return p.returncode, "\n".join(lines)


def test_the_bench_script_passes_every_check_against_the_stand_in(world):
    before = (world.tmp / "config.json").read_text()
    code, out = run_bench(world)
    assert "FAIL" not in out.replace("0 FAILED", ""), out
    for name in ("connected Pi is left alone past the wait", "hotspot appears after the link is lost",
                 "a phone can join the hotspot", "a joined phone is not kicked off",
                 "the Pi returns when nobody is on it"):
        assert f"PASS: {name}" in out, f"{name}\n{out}"
    assert "ALL PASSED" in out and code == 0
    assert world.get()["conn"] == "Home"          # it always puts the saved network back
    assert (world.tmp / "config.json").read_text() == before     # and the settings it changed


def test_the_bench_script_skips_the_phone_checks_when_no_phone_joins_and_still_returns(world):
    code, out = run_bench(world, phone_joins=False)
    assert "SKIP: a phone can join the hotspot" in out
    assert "PASS: the Pi returns when nobody is on it" in out and world.get()["conn"] == "Home"


def test_the_bench_script_restores_everything_even_when_it_fails(world):
    world.set(state="disconnected", conn="")      # not connected to start with: it must refuse
    before = (world.tmp / "config.json").read_text()
    code, out = run_bench(world)
    assert code == 1 and "FAIL: starts connected to a saved network" in out
    assert "CLEANUP" in out and "settings restored" in out
    assert (world.tmp / "config.json").read_text() == before                  # settings put back


def test_net_up_can_use_the_default_passphrase_once_with_a_flag_and_only_then(world):
    w = world
    w.set(has_hotspot=True)
    w.config(net_default_passphrase="refuse")
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, w.tmp / "key")
    # without the flag it refuses before touching the WiFi
    p = w.cli("up", "--yes", "--for", "20")
    out = p.communicate(timeout=30)[0]
    assert p.returncode == 1 and "published default" in out and w.get()["conn"] == "Home"
    assert w.ran(f"up {HOT}") == 0
    # with the flag it raises the hotspot for this run, then puts the saved network back
    p = w.cli("up", "--yes", "--allow-default", "--for", "20")
    out = p.communicate(timeout=60)[0]
    assert p.returncode == 0 and "for this run only" in out, out
    assert w.ran(f"up {HOT}") == 1 and w.get()["conn"] == "Home"
    # and the default is still refused afterwards
    assert netctl.is_default_passphrase(w.tmp / "key")
    p = w.cli("up", "--yes", "--for", "20")
    assert p.communicate(timeout=30)[0] and p.returncode == 1


def test_a_one_off_run_keeps_the_hotspot_reachable_if_no_saved_network_answers(world):
    w = world
    w.set(has_hotspot=True, in_range=[])
    w.config(net_default_passphrase="refuse")
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, w.tmp / "key")
    p = w.cli("up", "--yes", "--allow-default", "--for", "20")
    out = p.communicate(timeout=60)[0]
    assert p.returncode == 1 and "NO saved network connected" in out
    assert w.get()["conn"] == HOT, "the Pi was left unreachable"
