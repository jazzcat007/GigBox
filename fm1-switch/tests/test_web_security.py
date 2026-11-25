"""The request gates of the HTTP layer (audit §9 SEC-01/02; the TEST-02 cases).

Every test drives a real server on 127.0.0.1 through http.client so headers can be set
exactly as a browser -- or an attacker's page -- would set them. Nothing here touches a
device: the gates are proven before any handler runs, and the endpoints used to prove
"allowed" answers do no writes (GET /api/state, /api/library/remove of a name that does
not exist).
"""

from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from fm1_switch import web, web_http


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """A live server on a port, with an isolated library/backups/log and no network."""
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "backups"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap.txt"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "lib").mkdir()
    web.APP.catalog = None
    web.APP.catalog_tried = False
    web.APP.units = {}
    web.APP.last_event = None
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def raw(port: int, method: str, path: str, *, headers: dict | None = None,
        body: bytes | None = None) -> tuple[int, dict]:
    """One request with exactly the headers given (a browser would send no more)."""
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, {}


def json_post(port: int, path: str, payload: dict, **headers) -> tuple[int, dict]:
    return raw(port, "POST", path,
               headers={"Content-Type": "application/json", **headers},
               body=json.dumps(payload).encode())


def raw_text(port: int, method: str, path: str, **kw) -> tuple[int, bytes]:
    """Like raw(), but with undecoded bytes -- static files are not JSON."""
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=kw.get("body"), headers=kw.get("headers") or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


# --------------------------------------------------------------------- static serving

def test_the_ui_files_are_served(srv):
    code, html = raw_text(srv, "GET", "/fleet")
    assert code == 200 and b"FM-1 FLEET" in html
    code, html = raw_text(srv, "GET", "/")
    assert code == 200 and b"FM-1 FLEET" in html  # the root is the panel's own screen
    code, html = raw_text(srv, "GET", "/expert")
    assert code == 200 and b"app.js" in html      # the hub/library page, moved off the root
    for path in ("/fleet.css", "/fleet.js", "/app.js", "/style.css", "/index.html"):
        code, _ = raw_text(srv, "GET", path)
        assert code == 200, path


def test_source_files_are_not_served(srv):
    # Audit SEC-03: the old prefix check served this module's source ("GET /web.py" and
    # "GET /../web.py" both 200, 37,450 bytes). It will be public soon anyway -- it is a
    # correctness bug in a security check, and the fix is containment + an allowlist.
    for path in ("/web.py", "/guided.py"):
        code, _ = raw_text(srv, "GET", path)
        assert code == 404, path


def test_dotdot_traversal_is_refused(srv):
    code, _ = raw_text(srv, "GET", "/../web.py")
    assert code == 404


def test_encoded_traversal_is_refused(srv):
    code, _ = raw_text(srv, "GET", "/..%2fweb.py")
    assert code == 404


def test_traversal_out_of_the_package_is_refused(srv):
    code, _ = raw_text(srv, "GET", "/../../setup.sh")
    assert code == 404


# --------------------------------------------------------------------- content type

def test_text_plain_post_is_refused_415(srv):
    # The measured CSRF shape (audit G2): a cross-origin simple request. 415 without the
    # body being looked at, so the attack stops before the endpoint.
    code, body = raw(srv, "POST", "/api/flash",
                     headers={"Content-Type": "text/plain"},
                     body=json.dumps({"unit_id": "aaaaaaaa00000001", "confirm":
                                      "aaaaaaaa00000001", "package": "p.fwsc"}).encode())
    assert code == 415
    assert "application/json" in body["error"]


def test_post_without_content_type_is_refused_415(srv):
    code, _body = raw(srv, "POST", "/api/library/remove",
                      body=json.dumps({"name": "x.fwsc"}).encode())
    assert code == 415


def test_json_post_with_a_charset_parameter_is_accepted(srv):
    # The gate's job is to force a CORS preflight, not to police charset parameters: any
    # application/json POST is non-simple either way, so a browser still preflights.
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"},
                           **{"Content-Type": "application/json; charset=utf-8"})
    assert code == 200 and body["ok"] is True


# --------------------------------------------------------------------- origin

def test_foreign_origin_post_is_refused_403(srv):
    code, body = json_post(srv, "/api/library/remove", {"name": "x.fwsc"},
                           Origin="http://evil.example")
    assert code == 403
    assert "cross-origin" in body["error"]


def test_a_null_origin_is_refused(srv):
    code, _ = json_post(srv, "/api/library/remove", {"name": "x.fwsc"}, Origin="null")
    assert code == 403


def test_the_panel_origin_is_accepted(srv):
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"},
                           Origin=f"http://127.0.0.1:{srv}")
    assert code == 200 and body["ok"]


def test_origin_with_a_configured_lan_name_is_accepted(srv, monkeypatch):
    monkeypatch.setenv("FM1_WEB_HOSTS", "fm1.local, bench.local")
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"},
                           Origin="https://fm1.local")
    assert code == 200 and body["ok"]


# --------------------------------------------------------------------- host

def test_foreign_host_is_refused_421(srv):
    # What a DNS-rebinding page must send (its own name, after rebinding resolves to the
    # Pi). The measured hole (200 on /api/fleet) is closed for reads as well as writes.
    code, body = raw(srv, "GET", "/api/fleet", headers={"Host": "evil.example:8080"})
    assert code == 421
    assert "evil.example" in body["error"]


def test_foreign_host_post_is_refused_even_with_a_good_body(srv):
    code, _ = json_post(srv, "/api/library/remove", {"name": "x.fwsc"},
                        Host="evil.example")
    assert code == 421


def test_loopback_host_is_accepted(srv):
    code, _ = raw(srv, "GET", "/api/state", headers={"Host": "127.0.0.1:8080"})
    assert code == 200


def test_ipv6_loopback_host_is_accepted(srv):
    code, _ = raw(srv, "GET", "/api/state", headers={"Host": "[::1]:8080"})
    assert code == 200


def test_lan_host_names_come_from_fm1_web_hosts(srv, monkeypatch):
    monkeypatch.setenv("FM1_WEB_HOSTS", "FM1.BENCH.home")   # case-insensitive
    code, _ = raw(srv, "GET", "/api/state", headers={"Host": "fm1.bench.home:8080"})
    assert code == 200
    code, _ = raw(srv, "GET", "/api/state", headers={"Host": "other.bench.home:8080"})
    assert code == 421


# --------------------------------------------------------------------- loopback default

def test_serve_binds_loopback_by_default():
    # Audit SEC-02: 0.0.0.0 must not be the default anywhere. serve() itself is not
    # started here (it would block); its default is the security surface.
    import inspect
    assert inspect.signature(web.serve).parameters["host"].default == "127.0.0.1"


def test_the_loopback_bind_set_covers_the_panel_paths():
    assert web._LOOPBACK_BINDS == {"127.0.0.1", "::1", "localhost"}


# --------------------------------------------------------------------- file permissions

def test_harden_umask_sets_0077():
    old = os.umask(0o022)
    try:
        web._harden_umask()
        assert os.umask(0o022) == 0o077    # umask() returns what was set
    finally:
        os.umask(old)


def test_a_fresh_flash_log_is_owner_only(monkeypatch, tmp_path):
    # The chain serve() relies on: umask 0077 -> a new 0o666-mode open lands 0600. The
    # audit measured it 0644 on this Mac before the fix.
    old = os.umask(0o022)
    try:
        web._harden_umask()
        monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
        web._log_flash(event="request", unit_id="0" * 16, package="x.fwsc")
        assert (tmp_path / "flash.log").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


# --------------------------------------------------------------------- gate plumbing

def test_host_name_strips_the_port_and_brackets():
    assert web._host_name("127.0.0.1:8080") == "127.0.0.1"
    assert web._host_name("[::1]:8080") == "::1"
    assert web._host_name("::1") == "::1"
    assert web._host_name("FM1.BENCH.home:80") == "fm1.bench.home"
    assert web._host_name(" [::1] ") == "::1"


def test_allowed_hosts_default_to_loopback_plus_fm1_web_hosts(monkeypatch):
    monkeypatch.delenv("FM1_WEB_HOSTS", raising=False)
    assert web._allowed_hosts() == set(web._LOOPBACK_HOSTS)
    monkeypatch.setenv("FM1_WEB_HOSTS", "a.local, b.local")
    assert web._allowed_hosts() == set(web._LOOPBACK_HOSTS) | {"a.local", "b.local"}


# --------------------------------------------------------------------- mutation token

def test_the_panel_on_loopback_needs_no_token(srv):
    # The default surface: the kiosk posts to 127.0.0.1 with no token header at all.
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"})
    assert code == 200 and body["ok"] is True


def test_off_pi_post_without_a_token_is_401(srv, monkeypatch):
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"})
    assert code == 401
    assert "X-FM1-Token" in body["error"]


def test_off_pi_post_with_the_wrong_token_is_401(srv, monkeypatch):
    # compare_digest, not ==: the check is not the kind that leaks via timing.
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, _ = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"},
                        **{web._TOKEN_HEADER: "111111"})
    assert code == 401


def test_off_pi_post_with_the_right_token_passes_the_gate(srv, monkeypatch):
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"},
                           **{web._TOKEN_HEADER: "654321"})
    assert code == 200 and body["ok"] is True


def test_off_pi_reads_stay_open_so_the_uis_can_render(srv, monkeypatch):
    # The token gates mutations only; GETs are what draws the pages. The Host/Origin
    # gates above are what keep a rebinding page from reading.
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", "654321")
    code, _ = raw(srv, "GET", "/api/state")
    assert code == 200


def test_a_lan_surface_without_any_token_fails_closed(srv, monkeypatch):
    # Nothing configured: every off-Pi mutation is refused, never answered leniently.
    monkeypatch.setattr(web_http, "_is_loopback_client", lambda ip: False)
    monkeypatch.setattr(web.APP, "token", None)
    code, body = json_post(srv, "/api/library/remove", {"name": "nope.fwsc"})
    assert code == 401


# --------------------------------------------------------------------- token file

def test_token_file_is_created_0600_and_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_TOKEN_FILE", str(tmp_path / "cfg" / "token"))
    monkeypatch.delenv("FM1_TOKEN", raising=False)
    token = web._ensure_token()
    assert len(token) == 6 and token.isdigit()
    path = tmp_path / "cfg" / "token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert web._ensure_token() == token              # idempotent: same value, one file
    assert web._load_token() == token


def test_fm1_token_env_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_TOKEN", "123456")
    monkeypatch.setenv("FM1_TOKEN_FILE", str(tmp_path / "cfg" / "token"))
    assert web._load_token() == "123456"
    assert not (tmp_path / "cfg" / "token").exists()


def test_load_token_reads_a_stripped_file(tmp_path, monkeypatch):
    f = tmp_path / "token"
    f.write_text("  090909  \n", encoding="utf-8")
    monkeypatch.setenv("FM1_TOKEN_FILE", str(f))
    monkeypatch.delenv("FM1_TOKEN", raising=False)
    assert web._load_token() == "090909"


def test_loopback_client_test_helper():
    assert web._is_loopback_client("127.0.0.1")
    assert web._is_loopback_client("::1")
    # Any non-loopback address must fail; this one is an RFC 5737 documentation range,
    # deliberately not a LAN-shaped one.
    assert not web._is_loopback_client("203.0.113.20")



# --------------------------------------------------------------------- headless (B16)

def test_lan_hosts_a_phone_types_are_allowed_but_foreign_names_are_not():
    import socket
    from fm1_switch.security import host_allowed
    assert host_allowed("192.168.1.50") and host_allowed("fe80::1")
    mine = socket.gethostname().lower().split(".")[0]
    assert host_allowed(mine) and host_allowed(mine + ".local")
    assert not host_allowed("evil.example") and not host_allowed("rebind.attacker.net")


def test_throttle_locks_an_address_after_five_misses_then_releases():
    from fm1_switch.security import Throttle
    now = [0.0]
    t = Throttle(limit=5, lockout=300, clock=lambda: now[0])
    for _ in range(5):
        assert not t.blocked("10.0.0.9")
        t.miss("10.0.0.9")
    assert t.blocked("10.0.0.9") and not t.blocked("10.0.0.10")
    now[0] = 301
    assert not t.blocked("10.0.0.9")


def test_headless_flag_binds_the_lan_on_port_80(monkeypatch):
    from fm1_switch import __main__ as cli
    seen = {}
    monkeypatch.setattr("fm1_switch.web.serve", lambda host, port: seen.update(host=host, port=port) or 0)
    assert cli.cmd_web(["--headless"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 80}
    assert cli.cmd_web(["--headless", "--port", "8081"]) == 0 and seen["port"] == 8081
    assert cli.cmd_web([]) == 0 and seen == {"host": "127.0.0.1", "port": 8080}
