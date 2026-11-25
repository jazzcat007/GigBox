"""The panel's REGISTER flow: GET /api/register/scan, POST /api/register/bind.

A fake bus stands in for the hardware: the download-mode devices and the ids
``m-flasher info`` reports for them are scripted, on ports chosen so nothing here can
pass by accident of ordering. What these tests pin, above all:

* a binding is minted only from an id the SERVER has just read off a device in
  download mode -- never from a client-supplied id, a port, or a scan shown earlier;
* every refusal names its reason: none in download mode, two or more unclaimed
  (with the count), an id already registered (with its letter), no free letter,
  staleness between scan and confirm, a mismatched confirm;
* the endpoints sit behind the same request gates as every other mutation;
* nothing here writes to a unit: the fake bus records no write-side call, and the
  only state the flow touches is ``~/fm1-units.json``.

Unit ids in this file are deliberately fake 16-hex strings (the bench's real ids stay
out of every fixture).
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from fm1_switch import guided, mflasher, registry, web, web_http


class Bus:
    """A scripted bus of download-mode units: nodes, ports, and their ids."""

    def __init__(self) -> None:
        self.devices: list[guided.Device] = []
        self.ids: dict[str, object] = {}      # node -> mflasher.Identity | Exception

    def plug(self, unit_id: str, node: str = "/dev/sg0", port: str = "1-1.2") -> "Bus":
        self.devices.append(guided.Device(node, port))
        self.ids[node] = mflasher.Identity(flash_chip="0x856014", firmware="FM-1 953",
                                           unit_id=unit_id)
        return self

    def broken(self, node: str = "/dev/sg1", port: str = "1-1.3") -> "Bus":
        self.devices.append(guided.Device(node, port))
        self.ids[node] = mflasher.MflasherError("no answer from the flash")
        return self

    def unplug(self) -> None:
        self.devices.clear()
        self.ids.clear()


#: Obviously fake 16-hex ids. A is the unit under registration; the other four fill
#: every letter for the "no free letter" test.
A = "aaaaaaaa00000001"
B = "aaaaaaaa00000002"
C = "aaaaaaaa00000003"
D = "aaaaaaaa00000004"
E = "aaaaaaaa00000005"


@pytest.fixture()
def bus(tmp_path, monkeypatch):
    b = Bus()
    monkeypatch.setattr(guided, "download_devices", lambda: list(b.devices))

    def fake_info(node: str, timeout: float = 60.0):
        value = b.ids[node]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(mflasher, "info", fake_info)
    yield b


@pytest.fixture()
def srv(bus, tmp_path, monkeypatch):
    """A live server on loopback, with an isolated registry/library/log."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "backups"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap.txt"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    (tmp_path / "lib").mkdir()
    (tmp_path / "hub").mkdir()
    web.APP.catalog = None
    web.APP.catalog_tried = False
    web.APP.units = {}
    web.APP.last_event = None
    web.APP.last_fleet = None
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()
    return r.status, data


def post(port: int, path: str, payload: dict, **headers) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json", **headers})
    r = conn.getresponse()
    try:
        data = json.loads(r.read())
    except ValueError:
        data = {}
    conn.close()
    return r.status, data


def raw_post(port: int, path: str, payload: dict, ctype: str) -> tuple[int, str]:
    """One request with an arbitrary Content-Type, exactly as an attacker would send."""
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=json.dumps(payload).encode(),
                 headers={"Content-Type": ctype})
    r = conn.getresponse()
    body = r.read().decode()
    conn.close()
    return r.status, body


def prebind(unit_id: str, slot: str) -> None:
    """Record one binding the way the registry itself would: load, bind, atomic save."""
    reg = registry.load()
    reg.bind(unit_id, slot)
    registry.save(reg)


# ---------------------------------------------------------------------- GET scan

def test_scan_with_no_unit_in_download_mode_says_so(srv):
    code, body = get(srv, "/api/register/scan")
    assert code == 200
    assert body["units"] == [] and body["unbound"] == 0 and body["proposed"] is None
    assert "download mode" in body["refusal"]


def test_scan_reports_the_server_read_id_and_the_letter_the_unit_would_get(srv, bus):
    bus.plug(A, node="/dev/sg3", port="1-1.9")     # an unmapped port, on purpose
    code, body = get(srv, "/api/register/scan")
    assert code == 200
    assert body["refusal"] is None and body["unbound"] == 1
    row = body["units"][0]
    assert row["unit_id"] == A                     # read by the server in this scan
    assert row["slot"] is None and row["label"] == "?"
    assert row["port"] == "1-1.9" and row["device"] == "/dev/sg3"
    assert body["free"] == ["A", "B", "C", "D"]
    assert body["proposed"] == {"slot": "fm1-a", "letter": "A"}


def test_scan_counts_two_unclaimed_units_and_refuses_with_the_count(srv, bus):
    bus.plug(A, node="/dev/sg0", port="1-1.2")
    bus.plug(B, node="/dev/sg1", port="1-1.4")
    _code, body = get(srv, "/api/register/scan")
    assert body["unbound"] == 2
    assert "2 units" in body["refusal"] and "one" in body["refusal"]
    assert body["proposed"] is None
    assert [u["unit_id"] for u in body["units"]] == [A, B]   # shown, not filtered


def test_scan_marks_the_registered_unit_and_names_its_letter(srv, bus):
    prebind(A, "fm1-a")
    bus.plug(A)
    bus.plug(B, node="/dev/sg1", port="1-1.4")
    _code, body = get(srv, "/api/register/scan")
    assert body["unbound"] == 1
    first, second = body["units"]
    assert first["unit_id"] == A and first["slot"] == "fm1-a" and first["label"] == "A"
    assert second["unit_id"] == B and second["slot"] is None
    assert body["free"] == ["B", "C", "D"]
    assert body["proposed"] == {"slot": "fm1-b", "letter": "B"}


def test_an_unreadable_unit_is_shown_and_counts_as_unclaimed(srv, bus):
    """Nothing silently filtered: a device whose id cannot be read is a row with its
    error, and it counts as unclaimed -- it must not narrow the choice to one unit."""
    bus.plug(A, node="/dev/sg0", port="1-1.2")
    bus.broken(node="/dev/sg1", port="1-1.3")
    _code, body = get(srv, "/api/register/scan")
    assert body["unbound"] == 2
    assert "2 units" in body["refusal"]
    broken = next(u for u in body["units"] if u["unit_id"].startswith("device:"))
    assert "no answer" in broken["error"]


def test_a_single_unreadable_unit_cannot_be_registered(srv, bus):
    bus.broken()
    _code, body = get(srv, "/api/register/scan")
    assert body["unbound"] == 1 and body["proposed"] is None
    assert "no readable unit id" in body["refusal"]


def test_scan_refuses_while_a_write_holds_the_lock(srv, bus):
    bus.plug(A)
    with web.APP.lock:                              # a flash "is running"
        code, body = get(srv, "/api/register/scan")
    assert code == 200
    assert "another flash is already running" in body["refusal"]
    assert body["units"] == []


# ---------------------------------------------------------------------- POST bind

def test_bind_write_a_confirmed_binding_from_the_server_read(srv, bus, tmp_path):
    bus.plug(A)
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 200
    assert body["ok"] is True and body["slot"] == "fm1-a" and body["label"] == "A"
    back = registry.load()
    binding = back.binding_for_id(A)
    assert binding is not None and binding.slot == "fm1-a"
    assert binding.provisional is False             # a real binding, not provisional
    lines = (tmp_path / "flash.log").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l).get("event") == "bind" for l in lines)


def test_bind_refuses_a_confirm_that_does_not_name_the_id(srv, bus):
    bus.plug(A)
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": B})
    assert code == 409
    assert "confirmation" in body["error"]
    assert registry.load().units == {}


def test_bind_refuses_placeholder_ids(srv, bus):
    bus.plug(A)
    for bad in ("device:sg0", "port:1-1.2", "slot:fm1-a"):
        code, body = post(srv, "/api/register/bind", {"unit_id": bad, "confirm": bad})
        assert code == 409, bad
        assert "flash id" in body["error"], bad
    assert registry.load().units == {}


def test_bind_never_trusts_a_client_supplied_id(srv, bus):
    """The identity authority: an id no device on the bus reports cannot be bound,
    even with a well-formed matching confirm."""
    bus.plug(B)                                     # the bus does NOT report A
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 409
    assert "download mode" in body["error"]
    assert registry.load().units == {}


def test_bind_refuses_a_unit_that_left_download_mode_after_the_scan(srv, bus):
    """The scan the view displayed is not an authority: the bind re-reads the bus, and
    a unit that left download mode between the two is refused, not bound. (With the
    bus now empty the refusal is the no-unit one; the point is that it refuses and the
    registry grows no binding.)"""
    bus.plug(A)
    scan = get(srv, "/api/register/scan")[1]
    assert scan["proposed"] is not None             # the view offered the binding
    bus.unplug()
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 409
    assert "download mode" in body["error"]
    assert registry.load().units == {}


def test_bind_refuses_a_unit_that_is_already_registered(srv, bus):
    prebind(A, "fm1-b")
    bus.plug(A)
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 409
    assert "B" in body["error"] and "rename" in body["error"]


def test_bind_refuses_when_every_letter_is_taken(srv, bus):
    for unit_id, slot in ((A, "fm1-a"), (B, "fm1-b"), (C, "fm1-c"), (D, "fm1-d")):
        prebind(unit_id, slot)
    bus.plug(E)
    code, body = post(srv, "/api/register/bind", {"unit_id": E, "confirm": E})
    assert code == 409
    assert "every letter" in body["error"]


def test_bind_refuses_while_a_write_holds_the_lock(srv, bus):
    bus.plug(A)
    with web.APP.lock:
        code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 409
    assert "another flash is already running" in body["error"]
    assert registry.load().units == {}


def test_bind_upgrades_a_provisional_binding_to_a_confirmed_one(srv, bus):
    """The provisional -> confirmed machinery, wired end to end: a binding taught by
    power-on does not hold its letter hostage, and confirming the real id onto it is
    exactly the upgrade it waits for."""
    reg = registry.Registry()
    reg.units["fm1-a"] = registry.Binding(slot="fm1-a", unit_id="", fingerprint="0328",
                                          port="1-1.2")
    registry.save(reg)
    bus.plug(A, port="1-1.2")
    scan = get(srv, "/api/register/scan")[1]
    assert scan["proposed"] == {"slot": "fm1-a", "letter": "A"}
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 200 and body["slot"] == "fm1-a"
    back = registry.load()
    assert back.units["fm1-a"].unit_id == A and not back.units["fm1-a"].provisional


def test_a_scan_after_the_bind_shows_the_unit_registered(srv, bus):
    bus.plug(A)
    assert post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})[0] == 200
    _code, body = get(srv, "/api/register/scan")
    assert body["unbound"] == 0 and body["proposed"] is None
    assert body["units"][0]["slot"] == "fm1-a" and body["units"][0]["label"] == "A"
    assert "already has a sticker" in body["refusal"]


# ---------------------------------------------------------------------- the gates

def test_bind_post_with_a_text_plain_body_is_refused_415(srv, bus):
    """Same shape as every other mutation: only application/json gets in."""
    bus.plug(A)
    code, body = raw_post(srv, "/api/register/bind", {"unit_id": A, "confirm": A},
                          "text/plain")
    assert code == 415
    assert "application/json" in body


def test_bind_post_from_off_pi_needs_the_token(srv, bus, monkeypatch):
    bus.plug(A)
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A})
    assert code == 401
    assert "X-FM1-Token" in body["error"]
    assert registry.load().units == {}

    code, body = post(srv, "/api/register/bind", {"unit_id": A, "confirm": A},
                      **{web._TOKEN_HEADER: "654321"})
    assert code == 200 and body["ok"] is True


def test_register_scan_get_passes_the_loopback_gates_untouched(srv, monkeypatch):
    """GETs stay open to the UIs (the Host/Origin gates still cover them); the token
    gates mutations only."""
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, body = get(srv, "/api/register/scan")
    assert code == 200 and body["refusal"]         # no unit in download mode: say so
