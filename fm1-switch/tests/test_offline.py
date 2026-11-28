"""Things that need the internet fail gracefully when the Pi has none (for example on its own hotspot):
quickly, in plain words, with what still works, and without waiting for a network timeout."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from fm1_switch import hub, web
from tests.test_web_hub import REAL_CATALOG, _entry


@pytest.fixture
def offline(monkeypatch):
    """The Pi has no internet (conftest says online by default)."""
    monkeypatch.setattr(hub, "online", lambda **kw: False)


@pytest.fixture
def no_network_calls(monkeypatch):
    """Any attempt to open a URL fails the test: offline code must not even try."""
    def boom(*a, **k):
        raise AssertionError("tried to use the network while offline")
    monkeypatch.setattr(hub.urllib.request, "urlopen", boom)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    hub._ONLINE.update(at=-1e9, ok=True)
    monkeypatch.delenv("FM1_OFFLINE", raising=False)


# ----------------------------------------------------------- the connectivity check itself

def test_no_default_route_means_offline_without_trying_to_connect(monkeypatch):
    monkeypatch.setattr(hub, "has_default_route", lambda: False)
    monkeypatch.setattr(hub, "_probe", lambda *a: pytest.fail("probed with no route"))
    assert hub.check_online(force=True) is False


def test_a_route_and_a_reachable_hub_means_online_and_the_answer_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "has_default_route", lambda: True)
    monkeypatch.setattr(hub, "_probe", lambda host, port, timeout: calls.append((host, port)) or True)
    t = [100.0]
    assert hub.check_online(force=True, clock=lambda: t[0]) is True
    t[0] += 20                                   # inside the 30 s cache
    assert hub.check_online(clock=lambda: t[0]) is True and len(calls) == 1
    t[0] += 20                                   # past it: asks again
    hub.check_online(clock=lambda: t[0])
    assert len(calls) == 2 and calls[0] == ("thefirmwarehub.com", 443)


def test_a_route_but_an_unreachable_hub_is_offline_and_is_rechecked_sooner(monkeypatch):
    monkeypatch.setattr(hub, "has_default_route", lambda: True)
    answers = iter([False, True])
    monkeypatch.setattr(hub, "_probe", lambda *a: next(answers))
    t = [0.0]
    assert hub.check_online(force=True, clock=lambda: t[0]) is False
    t[0] += 5
    assert hub.check_online(clock=lambda: t[0]) is False          # cached for 10 s
    t[0] += 6
    assert hub.check_online(clock=lambda: t[0]) is True           # rechecked after 10 s


def test_where_the_routing_table_cannot_be_read_the_probe_decides(monkeypatch):
    monkeypatch.setattr(hub, "has_default_route", lambda: None)
    monkeypatch.setattr(hub, "_probe", lambda *a: True)
    assert hub.check_online(force=True) is True


def test_the_environment_can_force_offline(monkeypatch):
    monkeypatch.setenv("FM1_OFFLINE", "1")
    monkeypatch.setattr(hub, "_probe", lambda *a: pytest.fail("probed while forced offline"))
    assert hub.check_online(force=True) is False


def test_a_probe_that_hangs_is_abandoned_at_its_deadline(monkeypatch):
    def slow_connect(addr, timeout=None):
        time.sleep(1.5)
        raise OSError("too slow")        # what an abandoned attempt ends with, quietly
    monkeypatch.setattr(hub.socket, "create_connection", slow_connect)
    start = time.monotonic()
    assert hub._probe("example.invalid", 443, 0.3) is False
    assert time.monotonic() - start < 2


# ------------------------------------------------------------------------ the catalogue

def test_a_stale_cache_is_served_at_once_when_offline_with_no_network_attempt(
        tmp_path, monkeypatch, offline, no_network_calls):
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path))
    (tmp_path / "catalog.json").write_bytes(REAL_CATALOG.read_bytes())
    import os
    old = time.time() - 10 * 86400
    os.utime(tmp_path / "catalog.json", (old, old))          # a week and more old: stale
    start = time.monotonic()
    cat = hub.load()
    assert cat.stale and cat.offline and cat.from_cache and len(cat.entries) == 322
    assert time.monotonic() - start < 1


def test_offline_with_no_saved_catalogue_says_so_plainly(tmp_path, monkeypatch, offline, no_network_calls):
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "empty"))
    with pytest.raises(hub.HubUnavailable) as e:
        hub.load()
    assert "offline" in str(e.value).lower() and "fm1-switch import" in str(e.value)


def test_online_still_fetches_when_the_cache_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path))
    (tmp_path / "catalog.json").write_bytes(REAL_CATALOG.read_bytes())
    import os
    old = time.time() - 10 * 86400
    os.utime(tmp_path / "catalog.json", (old, old))
    asked = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return REAL_CATALOG.read_bytes()

    monkeypatch.setattr(hub.urllib.request, "urlopen", lambda *a, **k: asked.append(a) or Resp())
    cat = hub.load()
    assert asked and not cat.offline


# --------------------------------------------------------------------------- downloads

def test_a_download_offline_is_refused_at_once_leaving_nothing_behind(tmp_path, offline, no_network_calls):
    entry = _entry(sha256="a" * 64, pkg="fw/x.fwsc")
    with pytest.raises(hub.HubOffline) as e:
        hub.fetch_package(entry, tmp_path / "lib")
    assert "offline" in str(e.value).lower() and "library can be flashed" in str(e.value)
    assert not (tmp_path / "lib").exists() or not list((tmp_path / "lib").iterdir())


def test_the_cli_says_it_plainly_and_exits_1(tmp_path, monkeypatch, offline, no_network_calls, capsys):
    from fm1_switch import __main__ as cli
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "catalog.json").write_bytes(REAL_CATALOG.read_bytes())
    assert cli.main(["fetch", "sloop"]) == 1
    assert "offline" in capsys.readouterr().err.lower()


# ------------------------------------------------------------------------------ the web API

@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "bk"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "hub" / "catalog.json").write_bytes(REAL_CATALOG.read_bytes())
    web.APP.catalog = None
    web.APP.catalog_tried = False
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


def test_the_catalogue_api_says_it_is_offline_but_still_lists_the_saved_catalogue(srv, offline):
    status, data = call(srv, "GET", "/api/catalog")
    assert status == 200 and len(data["entries"]) > 100
    assert data["meta"]["online"] is False and "offline" in data["meta"]["offline_note"].lower()


def test_the_download_endpoint_answers_503_offline_with_a_flag_and_the_plain_words(srv, offline, no_network_calls):
    status, data = call(srv, "POST", "/api/download", {"id": "sloop"})
    assert status == 503 and data["offline"] is True
    assert "offline" in data["error"].lower() and "library can be flashed" in data["error"]


def test_refreshing_the_catalogue_offline_returns_the_saved_one_without_a_network_attempt(
        srv, offline, no_network_calls):
    status, data = call(srv, "POST", "/api/catalog/refresh", {})
    assert status == 200 and data["meta"]["online"] is False and data["entries"]


def test_getlist_reports_offline_for_the_touch_screen(srv, offline):
    status, data = call(srv, "GET", "/api/getlist")
    assert status == 200 and data["online"] is False and "offline_note" in data


def test_when_online_nothing_changes(srv):
    status, data = call(srv, "GET", "/api/catalog")
    assert status == 200 and data["meta"]["online"] is True
