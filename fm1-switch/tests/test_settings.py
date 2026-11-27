"""The settings schema: one list that the code, the config file and the website all share.

Everything lives in tmp_path; FM1_CONFIG points the module at a throwaway file.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from fm1_switch import backups, clock, config, hub, mflasher, router_service, web


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    monkeypatch.setenv("FM1_CONFIG", str(f))
    for spec in config.SPECS:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    return f


# --------------------------------------------------------------------- the schema itself

def test_keys_are_unique_and_groups_exist():
    keys = [s.key for s in config.SPECS]
    assert len(keys) == len(set(keys))
    groups = {g for g, _ in config.GROUPS}
    assert {s.group for s in config.SPECS} <= groups


def test_every_default_is_valid_for_its_own_spec():
    for s in config.SPECS:
        assert config._coerce(s, s.default) == s.default or s.kind == "float", s.key


def test_defaults_are_what_the_code_used_before(cfg):
    assert hub.max_age_seconds() == 24 * 3600
    assert hub.force_min_age_seconds() == 3600
    assert hub.max_package_bytes() == 4 * 1024 * 1024
    assert mflasher.default_timeout("firmware") == 300
    assert mflasher.default_timeout("restore") == 600
    assert mflasher.default_timeout("restart") == 90
    assert mflasher.default_timeout("info") == 60
    assert clock.ClockSettings().bpm == 120.0
    assert config.value("auto_backups_kept") == 20
    assert config.value("hold_ttl") == 300
    assert config.value("units") == 4


# ----------------------------------------------------------------- the code reads it live

def test_changing_the_file_changes_behaviour_without_a_restart(cfg):
    cfg.write_text(json.dumps({"hub_max_age_hours": 2, "flash_timeout": 111,
                               "default_bpm": 90, "max_package_mb": 8}))
    assert hub.max_age_seconds() == 2 * 3600
    assert mflasher.default_timeout("firmware") == 111
    assert clock.ClockSettings().bpm == 90.0
    assert hub.max_package_bytes() == 8 * 1024 * 1024


def test_backup_retention_follows_the_setting(cfg, tmp_path):
    d = tmp_path / "auto"
    d.mkdir()
    for i in range(5):
        (d / f"u{i}.json").write_text(json.dumps({"unit id": "abc", "saved": f"2026-01-0{i}"}))
        (d / f"u{i}.bin").write_bytes(b"x")
    cfg.write_text(json.dumps({"auto_backups_kept": 2}))
    assert len(backups.prune_auto_backups(d)) == 3
    assert len(list(d.glob("*.json"))) == 2


def test_hold_ttl_follows_the_setting(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_ROUTER_HOLDS", str(tmp_path / "holds.json"))
    cfg.write_text(json.dumps({"hold_ttl": 77}))
    import time
    router_service.place_hold(["fm1-a"])
    held = json.loads((tmp_path / "holds.json").read_text())["fm1-a"]
    assert 70 < held - time.time() <= 77


def test_bad_values_in_the_file_fall_back(cfg):
    cfg.write_text(json.dumps({"flash_timeout": "soon", "hub_max_age_hours": True,
                               "units": 99, "library": ""}))
    assert config.value("flash_timeout") == 300
    assert config.value("hub_max_age_hours") == 24
    assert config.value("units") == 16                # an out-of-range number is clamped
    assert str(config.path("library")).endswith("fm1-firmwares")


def test_env_beats_file_beats_default(cfg, monkeypatch, tmp_path):
    cfg.write_text(json.dumps({"backups": str(tmp_path / "fromfile")}))
    assert config.source("backups") == "file"
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "fromenv"))
    assert config.source("backups") == "env"
    assert config.path("backups") == tmp_path / "fromenv"


# ------------------------------------------------------------------------------- saving

def test_save_writes_only_what_changed_and_keeps_unknown_keys(cfg):
    cfg.write_text(json.dumps({"somebody_elses_key": 1}))
    res = config.save({"flash_timeout": 400, "auto_backups_kept": 5})
    assert res["changed"] == {"flash_timeout": [300, 400.0], "auto_backups_kept": [20, 5]}
    assert json.loads(cfg.read_text()) == {"somebody_elses_key": 1, "flash_timeout": 400.0,
                                           "auto_backups_kept": 5}
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def test_null_resets_to_default(cfg):
    config.save({"flash_timeout": 400})
    res = config.save({"flash_timeout": None})
    assert res["changed"] == {"flash_timeout": [400.0, 300]}
    assert "flash_timeout" not in json.loads(cfg.read_text())


def test_save_is_all_or_nothing(cfg):
    with pytest.raises(config.ConfigError):
        config.save({"flash_timeout": 400, "auto_backups_kept": 0})
    assert not cfg.exists()


@pytest.mark.parametrize("key,bad", [
    ("flash_timeout", "fast"), ("flash_timeout", 1), ("flash_timeout", 99999),
    ("flash_timeout", True), ("units", 2.5), ("units", 17), ("library", ""),
    ("library", "a\nb"), ("patch", "../etc"), ("nope", 1),
])
def test_save_refuses_bad_input(cfg, key, bad):
    with pytest.raises(config.ConfigError):
        config.save({key: bad})


def test_the_website_cannot_change_what_code_runs_or_where_firmware_comes_from(cfg):
    for key in ("flasher", "maker_tools", "hub_url"):
        assert not config.BY_KEY[key].editable
        with pytest.raises(config.ConfigError, match="config file|FM1_|trust"):
            config.save({key: "/tmp/evil"})
    assert not cfg.exists()


def test_env_fixed_setting_is_refused(cfg, monkeypatch):
    monkeypatch.setenv("FM1_LIBRARY", "/somewhere")
    with pytest.raises(config.ConfigError, match="FM1_LIBRARY"):
        config.save({"library": "/else"})


def test_restart_flag_is_reported(cfg):
    res = config.save({"units": 6, "flash_timeout": 400})
    assert res["restart"] == ["units"]


def test_security_settings_are_not_in_the_schema():
    # Bind address, port, token and the allowed host names decide who may write to the
    # units; they stay on the command line / environment by design.
    for key in ("host", "port", "token", "web_hosts", "throttle", "throttle_limit"):
        assert key not in config.BY_KEY


def test_every_hardcoded_tunable_of_the_audit_is_in_the_schema():
    # The numbers and paths that used to be constants in the code.
    for key in ("hub_max_age_hours", "hub_refresh_min_minutes", "max_package_mb",
                "hub_timeout", "download_timeout", "flash_timeout", "restore_timeout",
                "backup_timeout", "restart_timeout", "info_timeout", "download_mode_wait",
                "boot_wait", "auto_backups_kept", "default_bpm", "hold_ttl", "status_fresh",
                "touch_device", "flashlog", "hub_cache", "inspect_cache"):
        assert key in config.BY_KEY


def test_no_module_still_hardcodes_a_default_path_or_timeout():
    """A guard against a tunable creeping back: the old constants and the old literal
    defaults must not reappear in the package."""
    src = Path(config.__file__).parent
    banned = re.compile(r"HOLD_DEFAULT_TTL|STATUS_FRESH|AUTO_BACKUPS_KEPT|MAX_PACKAGE_BYTES|"
                        r"MAX_AGE_SECONDS|FORCE_MIN_AGE_SECONDS|DEFAULT_BPM|CATALOG_URL|"
                        r"HUB_BASE|path_setting\(|\"~/fm1-")
    hits = []
    for f in src.glob("*.py"):
        if f.name == "config.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if banned.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{f.name}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)


# -------------------------------------------------------------------------- the website

@pytest.fixture()
def srv(cfg):
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=None if payload is None else json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def test_api_lists_every_setting_with_its_source(srv):
    status, data = call(srv, "GET", "/api/settings")
    assert status == 200
    assert {s["key"] for s in data["settings"]} == set(config.BY_KEY)
    flasher = next(s for s in data["settings"] if s["key"] == "flasher")
    assert flasher["editable"] is False and flasher["why_locked"]
    assert data["groups"][0]["id"] == "rig"


def test_api_saves_and_the_change_is_live(srv, cfg):
    status, data = call(srv, "POST", "/api/settings", {"values": {"hub_max_age_hours": 3}})
    assert status == 200 and data["ok"]
    assert data["changed"] == {"hub_max_age_hours": [24, 3.0]}
    assert hub.max_age_seconds() == 3 * 3600
    log = Path(web.flash_log_path()).read_text()
    assert '"event": "settings"' in log and "hub_max_age_hours" in log


def test_api_refuses_a_locked_setting_and_logs_it(srv, cfg):
    status, data = call(srv, "POST", "/api/settings", {"values": {"flasher": "/bin/sh"}})
    assert status == 409 and "config file" in data["error"]
    assert not cfg.exists()
    assert '"event": "refused"' in Path(web.flash_log_path()).read_text()


def test_api_refuses_an_empty_request(srv):
    status, _ = call(srv, "POST", "/api/settings", {})
    assert status == 409
