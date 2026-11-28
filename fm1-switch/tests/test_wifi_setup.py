"""The first-run hotspot passphrase gate: who is gated, what they may reach, how the form saves."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from fm1_switch import netctl, web, web_http, wifi_setup
from tests.test_netwatch import FakeNm   # the same stand-in nmcli the other WiFi tests use

HOTSPOT = "10.42.0.1"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setenv("FM1_HOTSPOT_KEY", str(tmp_path / "key"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "bk"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "lib").mkdir()
    wifi_setup._CACHE.update(at=-1e9, ip=None)
    web.APP.token = "654321"
    return tmp_path


def set_policy(tmp, policy):
    (tmp / "c.json").write_text(json.dumps({"net_default_passphrase": policy}))


# ------------------------------------------------------------------------- who is gated

def test_a_phone_on_the_hotspot_is_gated_while_the_default_is_in_use(isolated):
    on = lambda ip=HOTSPOT: wifi_setup.gate_active(ip, "10.42.0.23", ip_fn=lambda: HOTSPOT)  # noqa: E731
    assert on() is True
    assert wifi_setup.gate_active("192.168.1.20", "192.168.1.9", ip_fn=lambda: HOTSPOT) is False   # house LAN
    assert wifi_setup.gate_active(HOTSPOT, "127.0.0.1", ip_fn=lambda: HOTSPOT) is False            # the panel
    assert wifi_setup.gate_active("127.0.0.1", "127.0.0.1", ip_fn=lambda: HOTSPOT) is False


def test_the_gate_is_off_once_the_passphrase_is_changed_or_the_policy_says_so(isolated):
    args = (HOTSPOT, "10.42.0.23")
    assert wifi_setup.gate_active(*args, ip_fn=lambda: HOTSPOT) is True
    for policy in ("refuse", "allow"):
        set_policy(isolated, policy)
        assert wifi_setup.gate_active(*args, ip_fn=lambda: HOTSPOT) is False, policy
    set_policy(isolated, "gate")
    netctl.write_passphrase("my-own-phrase", isolated / "key")
    assert wifi_setup.gate_active(*args, ip_fn=lambda: HOTSPOT) is False


def test_when_the_hotspot_address_cannot_be_read_the_shared_range_counts(isolated):
    assert wifi_setup.gate_active("10.42.3.1", "10.42.3.7", ip_fn=lambda: None) is True
    assert wifi_setup.gate_active("192.168.1.2", "192.168.1.9", ip_fn=lambda: None) is False


def test_the_hotspot_address_comes_from_the_adapter_while_the_hotspot_is_up(isolated):
    nm = FakeNm(state="connected", conn=netctl.HOTSPOT)
    nm_ip = lambda args: (0, "10.42.0.1/24\n") if "IP4.ADDRESS" in args else nm(args)   # noqa: E731
    assert wifi_setup.hotspot_ip(nm_ip, "wlan0", clock=lambda: 1000.0) == "10.42.0.1"
    wifi_setup._CACHE.update(at=-1e9, ip=None)
    assert wifi_setup.hotspot_ip(FakeNm(state="connected", conn="Home"), "wlan0",
                                 clock=lambda: 1000.0) is None


# ---------------------------------------------------------------------------- the form

def test_the_page_has_the_form_and_the_already_set_note(isolated):
    gated = wifi_setup.page("FM-Pi", gated=True).decode()
    assert "Choose your hotspot passphrase" in gated and 'id="p1"' in gated and "/api/wifi/setup" in gated
    assert "FM-Pi" in gated
    done = wifi_setup.page("FM-Pi", gated=False, is_default=False).decode()
    assert "already set" in done.lower() and "<form" not in done
    still = wifi_setup.page("FM-Pi", gated=False, is_default=True).decode()
    assert "still uses the default" in still and "already set" not in still.lower() and "<form" not in still


def test_an_odd_network_name_is_escaped_in_the_page(isolated):
    assert "<script>x" not in wifi_setup.page("<script>x</script>").decode().split("<style>")[0]
    assert "&lt;script&gt;" in wifi_setup.page("<script>x</script>").decode()


def test_the_new_passphrase_is_checked(isolated):
    for bad in ("short", "x" * 64, netctl.DEFAULT_PASSPHRASE, "bad\nline-phrase"):
        with pytest.raises(wifi_setup.Refused):
            wifi_setup.check_new_passphrase(bad)
    assert wifi_setup.check_new_passphrase("a-good-phrase") == "a-good-phrase"


def test_saving_writes_the_key_and_updates_the_profile_without_returning_it(isolated):
    nm = FakeNm(has_hotspot=True)
    out = wifi_setup.apply_passphrase("a-good-phrase", nm, restart_after=-1)
    assert out["ok"] is True and "a-good-phrase" not in json.dumps(out)
    assert (isolated / "key").read_text().strip() == "a-good-phrase"
    modify = [c for c in nm.calls if c[:2] == ["connection", "modify"]][-1]
    assert modify[modify.index("wifi-sec.psk") + 1] == "a-good-phrase"
    assert netctl.is_default_passphrase() is False


# ------------------------------------------------------------------ through the real server

@pytest.fixture
def srv(isolated):
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    h = {"Content-Type": "application/json", **(headers or {})} if method == "POST" else (headers or {})
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), data


@pytest.fixture
def on_hotspot(monkeypatch):
    """Make the live server see every request as a phone on the hotspot."""
    monkeypatch.setattr(wifi_setup, "gate_active", lambda local, client, ip_fn=None:
                        wifi_setup.policy() == "gate" and netctl.is_default_passphrase())
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)


def test_a_gated_phone_is_sent_to_the_form_from_every_page_and_refused_on_the_api(srv, on_hotspot):
    for path in ("/", "/fleet", "/expert", "/index.html", "/app.js"):
        status, headers, _ = req(srv, "GET", path)
        assert status == 302 and headers["Location"] == "/setup-wifi", path
    status, _, body = req(srv, "GET", "/api/fleet")
    assert status == 403 and b"/setup-wifi" in body
    # any other write is refused: without a token by the token check, with one by the gate
    status, _, _ = req(srv, "POST", "/api/flash", {"unit_id": "x", "confirm": "x"})
    assert status == 401
    status, _, body = req(srv, "POST", "/api/flash", {"unit_id": "x", "confirm": "x"},
                          headers={"X-FM1-Token": "654321"})
    assert status == 403 and b"/setup-wifi" in body
    status, headers, body = req(srv, "GET", "/setup-wifi")
    assert status == 200 and "text/html" in headers["Content-Type"] and b"Choose your hotspot" in body


def test_a_gated_phone_can_set_the_passphrase_without_the_write_token_and_is_then_let_in(
        srv, on_hotspot, isolated, monkeypatch):
    monkeypatch.setattr(netctl, "nmcli", FakeNm(has_hotspot=True))
    monkeypatch.setattr(wifi_setup, "_restart_hotspot", lambda run: None)
    status, _, body = req(srv, "POST", "/api/wifi/setup", {"passphrase": "short"})
    assert status == 400 and b"8 to 63" in body
    status, _, body = req(srv, "POST", "/api/wifi/setup", {"passphrase": netctl.DEFAULT_PASSPHRASE})
    assert status == 400 and b"published default" in body
    status, _, body = req(srv, "POST", "/api/wifi/setup", {"passphrase": "a-good-phrase"})
    assert status == 200 and json.loads(body)["ok"] is True and b"a-good-phrase" not in body
    assert (isolated / "key").read_text().strip() == "a-good-phrase"
    assert "wifi-passphrase" in (isolated / "flash.log").read_text()
    assert "a-good-phrase" not in (isolated / "flash.log").read_text()
    # the gate is gone: the UI now answers
    status, _, _ = req(srv, "GET", "/")
    assert status == 200


def test_once_the_passphrase_is_set_changing_it_again_needs_the_write_token(srv, isolated, monkeypatch):
    netctl.write_passphrase("already-changed", isolated / "key")
    monkeypatch.setattr(netctl, "nmcli", FakeNm(has_hotspot=True))
    monkeypatch.setattr(wifi_setup, "_restart_hotspot", lambda run: None)
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    status, _, _ = req(srv, "POST", "/api/wifi/setup", {"passphrase": "another-phrase"})
    assert status == 401
    status, _, _ = req(srv, "POST", "/api/wifi/setup", {"passphrase": "another-phrase"},
                       headers={"X-FM1-Token": "654321"})
    assert status == 200
    status, _, body = req(srv, "GET", "/setup-wifi")
    assert status == 200 and b"already set" in body.lower()
    # the panel (not gated) sees the right note while the default is in use
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, isolated / "key")
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: ip == "127.0.0.1")
    status, _, body = req(srv, "GET", "/setup-wifi")
    assert status == 200 and b"still uses the default" in body


def test_the_panel_and_the_house_network_are_never_gated(srv):
    # no monkeypatching: the test client is loopback, like the touch panel
    for path in ("/", "/api/state"):
        status, _, _ = req(srv, "GET", path)
        assert status == 200, path
