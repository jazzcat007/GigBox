"""The Pi's clock: when it cannot be trusted, how a phone sets it, and every guard on doing so.
timedatectl is replaced by tests/tools/fake_timedatectl.py, so no test moves a clock."""

from __future__ import annotations

import http.client
import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from fm1_switch import clockstate, web, web_http

# conftest stubs clockstate.status for every test (trusted); keep the real function from import time.
REAL_STATUS = clockstate.status

FAKE = Path(__file__).parent / "tools" / "fake_timedatectl.py"
NOW = 1_800_000_000.0                      # an arbitrary believable "now" (2027-01)


@pytest.fixture
def td(tmp_path, monkeypatch):
    """A fake timedatectl, a private marker file, and a REAL clockstate.status (conftest stubs it)."""
    wrapper = tmp_path / "timedatectl"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ntp": True, "synced": False, "time": ""}))
    monkeypatch.setenv("FM1_TIMEDATECTL", str(wrapper))
    monkeypatch.setenv("FAKE_TD_STATE", str(state))
    monkeypatch.setenv("FAKE_TD_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FM1_TIME_SET", str(tmp_path / "marker.json"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    (tmp_path / "calls.log").write_text("")
    monkeypatch.setattr(clockstate, "boot_id", lambda: "boot-A")
    # undo conftest's trusted stub with the real implementation
    monkeypatch.setattr(clockstate, "status", REAL_STATUS)
    clockstate.forget()

    class Td:
        path = state
        calls = tmp_path / "calls.log"

        def get(self):
            return json.loads(state.read_text())

        def put(self, **kw):
            d = self.get()
            d.update(kw)
            state.write_text(json.dumps(d))

        def ran(self, text):
            return sum(1 for ln in self.calls.read_text().splitlines() if ln.startswith(text))
    return Td()


# ------------------------------------------------------------------------------- status

def test_a_clock_the_network_set_is_trusted_and_an_unsynced_one_is_not(td):
    assert clockstate.status(force=True).trusted is False
    td.put(synced=True)
    st = clockstate.status(force=True)
    assert st.trusted and st.synchronized and "network" in st.reason()


def test_a_clock_that_cannot_be_read_is_untrusted(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_TIMEDATECTL", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(clockstate, "status", REAL_STATUS)
    clockstate.forget()
    assert clockstate.status(force=True).trusted is False


def test_the_status_is_cached_briefly(td):
    clockstate.status(force=True)
    n = td.ran("show")
    clockstate.status()
    assert td.ran("show") == n


# ---------------------------------------------------------------------------- setting it

def test_setting_the_clock_switches_network_time_off_sets_it_and_always_switches_it_back_on(td):
    out = clockstate.set_time(NOW, now=lambda: NOW - 86400)
    assert out["changed"] and td.get()["time"] and td.get()["ntp"] is True
    log = td.calls.read_text().splitlines()
    assert log.index("set-ntp false") < [i for i, l in enumerate(log) if l.startswith("set-time")][0] \
        < len(log) - 1 and log[-1] == "set-ntp true"
    assert clockstate.status(force=True).set_by_person            # trusted for this boot


def test_network_time_is_switched_back_on_even_when_setting_fails(td):
    td.put(fail_set=True)
    with pytest.raises(clockstate.ClockError):
        clockstate.set_time(NOW, now=lambda: NOW - 86400)
    assert td.get()["ntp"] is True and td.calls.read_text().splitlines()[-1] == "set-ntp true"
    assert not clockstate.status(force=True).set_by_person        # nothing was recorded


def test_it_never_overrides_a_clock_the_network_set(td):
    td.put(synced=True)
    with pytest.raises(clockstate.ClockError, match="already set by the network"):
        clockstate.set_time(NOW, now=lambda: NOW - 86400)
    assert td.ran("set-") == 0


@pytest.mark.parametrize("bad", [0, 1e9, 5e9, "yesterday", None, True, float("nan")])
def test_an_unbelievable_time_is_refused(td, bad):
    with pytest.raises(clockstate.ClockError):
        clockstate.set_time(bad, now=lambda: NOW)
    assert td.ran("set-") == 0


def test_a_difference_under_two_minutes_changes_nothing(td):
    out = clockstate.set_time(NOW + 30, now=lambda: NOW)
    assert out["changed"] is False and td.ran("set-") == 0


def test_it_refuses_while_something_is_running(td):
    with pytest.raises(clockstate.ClockError, match="a unit is being flashed"):
        clockstate.set_time(NOW, now=lambda: NOW - 86400, busy=lambda: "a unit is being flashed")
    assert td.ran("set-") == 0


def test_a_permission_failure_says_what_to_install(td):
    td.put(deny=True)
    with pytest.raises(clockstate.ClockError, match="polkit rule"):
        clockstate.set_time(NOW, now=lambda: NOW - 86400)


def test_the_person_set_flag_ends_with_the_boot(td, monkeypatch):
    clockstate.set_time(NOW, now=lambda: NOW - 86400)
    assert clockstate.status(force=True).set_by_person
    monkeypatch.setattr(clockstate, "boot_id", lambda: "boot-B")  # a reboot: the clock reset
    assert clockstate.status(force=True).set_by_person is False


# ----------------------------------------------------------------- the audit log and the API

def test_the_audit_log_marks_lines_written_on_an_untrusted_clock(td, tmp_path):
    web._log_flash(event="test", message="x")
    assert json.loads((tmp_path / "flash.log").read_text().splitlines()[-1])["clock"] == "unsynced"
    td.put(synced=True)
    clockstate.forget()
    web._log_flash(event="test", message="y")
    assert "clock" not in json.loads((tmp_path / "flash.log").read_text().splitlines()[-1])


@pytest.fixture
def srv(td, tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "bk"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap"))
    monkeypatch.setenv("FM1_ROUTER_HOLDS", str(tmp_path / "holds.json"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "lib").mkdir()
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def test_the_clock_api_reports_trust_and_the_fleet_response_carries_it(srv, td):
    status, data = call(srv, "GET", "/api/clock")
    assert status == 200 and data["trusted"] is False and data["now"] > 0
    status, fleet = call(srv, "GET", "/api/fleet")
    assert status == 200 and fleet["clock"]["trusted"] is False


def test_a_phone_can_set_the_clock_through_the_api_and_it_is_logged(srv, td, tmp_path):
    epoch = int(time.time()) + 3 * 86400
    status, data = call(srv, "POST", "/api/clock/set", {"confirm": "clock", "epoch": epoch})
    assert status == 200 and data["changed"] is True and data["clock"]["set_by_person"] is True
    last = json.loads((tmp_path / "flash.log").read_text().splitlines()[-1])
    assert last["event"] == "clock-set" and last["after"] > last["before"]


def test_the_api_refuses_without_confirmation_a_synced_clock_and_a_running_write(srv, td, monkeypatch):
    epoch = int(time.time()) + 86400
    assert call(srv, "POST", "/api/clock/set", {"epoch": epoch})[0] == 409
    td.put(synced=True)
    status, data = call(srv, "POST", "/api/clock/set", {"confirm": "clock", "epoch": epoch})
    assert status == 409 and "network" in data["error"]
    td.put(synced=False)
    assert web.APP.lock.acquire(blocking=False)
    try:
        status, data = call(srv, "POST", "/api/clock/set", {"confirm": "clock", "epoch": epoch})
        assert status == 409 and "a write is running" in data["error"]
    finally:
        web.APP.lock.release()


def test_the_cli_shows_and_sets_the_time(td, capsys):
    from fm1_switch import __main__ as cli
    assert cli.main(["time", "status"]) == 0 and "NOT TRUSTED" in capsys.readouterr().out
    assert cli.main(["time", "set", "next tuesday"]) == 1
    assert cli.main(["time", "set", "2090-01-01 10:00"]) == 0
    assert "clock set to 2090-01-01 10:00:00" in capsys.readouterr().out
    assert cli.main(["time", "bogus"]) == 2
