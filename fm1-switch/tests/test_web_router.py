"""The routes page's endpoints, the active-patch file the router follows, and panic.

No sequencer and no unit: panic runs against a fake backend, and every file lives in tmp_path.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from fm1_switch import router, router_service, web, web_router
from fm1_switch.guided import WriteRefused
from fm1_switch.router import Patch, Route


@pytest.fixture()
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_PATCHES", str(tmp_path / "patches.json"))
    monkeypatch.setenv("FM1_ROUTER_STATUS", str(tmp_path / "status.json"))
    monkeypatch.setenv("FM1_ROUTER_ACTIVE", str(tmp_path / "active.json"))
    monkeypatch.setenv("FM1_CLOCK", str(tmp_path / "clock.json"))
    monkeypatch.setenv("FM1_ROUTER_HOLDS", str(tmp_path / "holds.json"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    return tmp_path


@pytest.fixture()
def srv(files):
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()
    return r.status, data


def post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


A_TO_B = {"source": "fm1-a", "dest": "fm1-b"}


# ----------------------------------------------------------------------------- overview

def test_the_overview_of_an_empty_system_is_calm(srv):
    code, body = get(srv, "/api/router")
    assert code == 200
    assert body["router"] == {"running": False, "patch": None, "attached": [],
                              "missing": [], "held": []}
    assert body["patches"] == [] and body["active"] is None and body["error"] == ""
    assert [s["label"] for s in body["slots"]] == ["A", "B", "C", "D"]


def test_adding_routes_makes_the_patch_and_the_overview_describes_them(srv):
    code, body = post(srv, "/api/router/route", {"patch": "jam", "route": A_TO_B})
    assert (code, body["route"]) == (200, "A → B")
    post(srv, "/api/router/route", {"patch": "jam", "route": {
        "source": "fm1-b", "dest": "fm1-c", "remap_to": 5, "transpose": 12, "block": ["clock"]}})
    _, over = get(srv, "/api/router")
    jam = over["patches"][0]
    assert jam["name"] == "jam"
    assert [r["text"] for r in jam["routes"]] == ["A → B", "B → C · to ch 5 · +12 · no clock"]
    assert jam["routes"][1]["index"] == 1


@pytest.mark.parametrize("patch,route", [
    ("jam", {"source": "fm1-a", "dest": "fm1-a"}),                # to itself
    ("jam", {"source": "fm1-a", "dest": "fm1-z"}),                # not a sticker
    ("jam", {"source": "fm1-a", "dest": "fm1-b", "remap_to": 99}),
    ("jam", {"source": "fm1-a", "dest": "fm1-b", "bogus": 1}),
    ("bad/name", A_TO_B),
    ("jam", "fm1-a"),
])
def test_a_bad_route_is_refused_and_nothing_is_saved(srv, files, patch, route):
    code, body = post(srv, "/api/router/route", {"patch": patch, "route": route})
    assert code == 409 and body["error"]
    assert not (files / "patches.json").exists()


def test_removing_a_route_needs_the_patch_name_and_a_live_index(srv):
    post(srv, "/api/router/route", {"patch": "jam", "route": A_TO_B})
    post(srv, "/api/router/route", {"patch": "jam", "route": {"source": "fm1-c", "dest": "fm1-d"}})
    assert post(srv, "/api/router/route/delete", {"patch": "jam", "index": 0, "confirm": "x"})[0] == 409
    assert post(srv, "/api/router/route/delete", {"patch": "jam", "index": 5, "confirm": "jam"})[0] == 409
    assert post(srv, "/api/router/route/delete", {"patch": "jam", "index": True, "confirm": "jam"})[0] == 409
    code, body = post(srv, "/api/router/route/delete", {"patch": "jam", "index": 0, "confirm": "jam"})
    assert (code, body["route"]) == (200, "A → B")
    _, over = get(srv, "/api/router")
    assert [r["text"] for r in over["patches"][0]["routes"]] == ["C → D"]


def test_deleting_a_patch_needs_its_name(srv):
    post(srv, "/api/router/route", {"patch": "jam", "route": A_TO_B})
    assert post(srv, "/api/router/patch/delete", {"name": "jam", "confirm": "no"})[0] == 409
    assert post(srv, "/api/router/patch/delete", {"name": "jam", "confirm": "jam"})[0] == 200
    assert post(srv, "/api/router/patch/delete", {"name": "jam", "confirm": "jam"})[0] == 409
    assert get(srv, "/api/router")[1]["patches"] == []


def test_a_broken_patch_file_is_shown_not_overwritten(srv, files):
    (files / "patches.json").write_text("not json")
    _, over = get(srv, "/api/router")
    assert over["patches"] == [] and "cannot be read" in over["error"]
    assert post(srv, "/api/router/route", {"patch": "jam", "route": A_TO_B})[0] == 409
    assert (files / "patches.json").read_text() == "not json"


def test_the_overview_shows_a_running_router_and_ignores_a_stale_one(srv, files):
    import time
    status = {"patch": "jam", "attached": ["fm1-a"], "missing": ["fm1-b"], "held": [],
              "updated": time.time()}
    (files / "status.json").write_text(json.dumps(status))
    r = get(srv, "/api/router")[1]["router"]
    assert r["running"] and r["patch"] == "jam" and r["missing"] == ["fm1-b"]
    status["updated"] -= 60
    (files / "status.json").write_text(json.dumps(status))
    r = get(srv, "/api/router")[1]["router"]
    assert not r["running"] and r["attached"] == []


# ----------------------------------------------------------------------------- activate

def test_activate_chooses_a_saved_patch_and_stop_idles_the_router(srv, files):
    post(srv, "/api/router/route", {"patch": "jam", "route": A_TO_B})
    assert post(srv, "/api/router/activate", {"name": "nope", "confirm": "nope"})[0] == 409
    assert post(srv, "/api/router/activate", {"name": "jam", "confirm": "x"})[0] == 409
    assert post(srv, "/api/router/activate", {"name": "jam", "confirm": "jam"})[0] == 200
    assert get(srv, "/api/router")[1]["active"] == "jam"
    assert post(srv, "/api/router/activate", {"name": "", "confirm": "stop"})[0] == 200
    assert get(srv, "/api/router")[1]["active"] == ""


def test_unknown_router_endpoint_is_404(srv):
    assert post(srv, "/api/router/nope", {})[0] == 404


# ------------------------------------------------------------------------ the follower

def follower(files, explicit=None, default="default", log=None):
    return router_service.PatchFollower(
        explicit, default, log or (lambda s: None),
        store_file=files / "patches.json", active_file=files / "active.json")


def save(files, name, *routes):
    router.PatchStore.load(files / "patches.json").save_patch(Patch(name, tuple(routes)))


def test_the_follower_runs_the_default_then_the_panels_choice(files):
    f = follower(files)
    assert f().name == "waiting"           # no patch file yet: idle, units free
    assert f() is None                      # nothing changed
    save(files, "default", Route("fm1-a", "fm1-b"))
    assert f().name == "default"
    save(files, "jam", Route("fm1-c", "fm1-d"))
    router_service.write_active("jam", files / "active.json")
    assert f().name == "jam"
    router_service.write_active("", files / "active.json")
    assert f().name == "idle"
    assert f.name == ""


def test_stopping_from_the_panel_gives_an_empty_patch(files):
    save(files, "jam", Route("fm1-c", "fm1-d"))
    f = follower(files)
    router_service.write_active("", files / "active.json")
    patch = f()
    assert patch.routes == () and patch.ports() == set()


def test_a_name_given_on_the_command_line_beats_the_panel(files):
    save(files, "jam", Route("fm1-c", "fm1-d"))
    save(files, "fixed", Route("fm1-a", "fm1-b"))
    router_service.write_active("jam", files / "active.json")
    assert follower(files, explicit="fixed")().name == "fixed"


def test_an_unreadable_patch_file_keeps_the_running_patch(files):
    logs = []
    f = follower(files, log=logs.append)
    (files / "patches.json").write_text("{")
    assert f() is None and any("unusable" in m for m in logs)


# ------------------------------------------------------------------------------- panic

class FakeSeq:
    def __init__(self):
        self.connected, self.sent = [], []

    def connect(self, client, port=0): self.connected.append(client)
    def disconnect(self, client, port=0): self.connected.remove(client)
    def read(self, timeout=0.1): return []
    def send(self, client, data, port=0): self.sent.append((client, data))
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_panic_sounds_off_every_running_unit_and_lets_go(files):
    seq = FakeSeq()
    n = web_router.panic("panic", backend_factory=lambda: seq,
                         resolve=lambda: {"fm1-a": 20, "fm1-c": 22})
    assert n == 2 * 16 * 3
    assert {c for c, _ in seq.sent} == {20, 22}
    assert seq.connected == [], "every unit is released again"
    assert not (files / "status.json").exists(), "a one-shot panic never writes the router's status"


def test_panic_must_be_confirmed_and_reports_a_missing_sequencer():
    with pytest.raises(WriteRefused, match="confirmation"):
        web_router.panic("yes")

    def boom():
        raise OSError("no libasound")
    with pytest.raises(WriteRefused, match="could not send panic"):
        web_router.panic("panic", backend_factory=boom, resolve=lambda: {})


def test_panic_endpoint_is_refused_while_a_write_runs(srv, monkeypatch):
    assert web.APP.lock.acquire(blocking=False)
    try:
        code, body = post(srv, "/api/router/panic", {"confirm": "panic"})
    finally:
        web.APP.lock.release()
    assert code == 409 and "write is running" in body["error"]


# ------------------------------------------------------------------------------- clock

def test_the_overview_carries_a_calm_default_clock(srv):
    clock = get(srv, "/api/router")[1]["clock"]
    assert clock["bpm"] == 120.0 and clock["running"] is False and clock["targets"] == []
    assert clock["live"] is None and clock["error"] == ""


def test_tempo_and_targets_change_without_a_confirmation_and_are_saved(srv, files):
    code, body = post(srv, "/api/router/clock", {"bpm": 97.5, "targets": ["fm1-b", "fm1-a"]})
    assert code == 200 and body["clock"]["bpm"] == 97.5
    assert json.loads((files / "clock.json").read_text()) == {
        "bpm": 97.5, "running": False, "targets": ["fm1-a", "fm1-b"]}


def test_starting_the_clock_needs_the_confirmation_and_stopping_does_not(srv, files):
    assert post(srv, "/api/router/clock", {"running": True})[0] == 409
    assert post(srv, "/api/router/clock", {"running": True, "confirm": "nope"})[0] == 409
    assert not (files / "clock.json").exists()
    assert post(srv, "/api/router/clock", {"running": True, "confirm": "clock"})[0] == 200
    assert json.loads((files / "clock.json").read_text())["running"] is True
    assert post(srv, "/api/router/clock", {"running": False})[0] == 200
    assert json.loads((files / "clock.json").read_text())["running"] is False


@pytest.mark.parametrize("body", [
    {"bpm": 5}, {"bpm": "fast"}, {"bpm": True}, {"targets": ["fm1-z"]}, {"targets": "fm1-a"},
    {"running": "yes"},
])
def test_a_bad_clock_request_is_refused_and_nothing_is_saved(srv, files, body):
    code, resp = post(srv, "/api/router/clock", body)
    assert code == 409 and resp["error"]
    assert not (files / "clock.json").exists()


def test_the_overview_shows_what_a_running_router_reports(srv, files):
    (files / "status.json").write_text(json.dumps({
        "updated": __import__("time").time(), "patch": "idle", "attached": ["fm1-a"],
        "clock": {"bpm": 97.5, "running": True, "targets": [], "ticks": 123, "dropped": 0}}))
    live = get(srv, "/api/router")[1]["clock"]["live"]
    assert live == {"bpm": 97.5, "running": True, "targets": []}  # no tick counter: no churn
