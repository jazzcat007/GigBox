"""Rig sets on the web side: save, preflight, the background push and its HTTP endpoints.

The writers are replaced by recorders everywhere: nothing here can reach a unit.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time

import pytest

from fm1_switch import web, web_sets
from fm1_switch.guided import WriteRefused

IDS = {"fm1-a": "1111aaaa00000001", "fm1-b": "1111aaaa00000002",
       "fm1-c": "1111aaaa00000003", "fm1-d": "1111aaaa00000004"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    for s in "abcd":
        (lib / f"{s}.fwsc").write_bytes(f"fw-{s}".encode())
    monkeypatch.setenv("FM1_LIBRARY", str(lib))
    monkeypatch.setenv("FM1_SETS", str(tmp_path / "sets.json"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "backups"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    units = {s: {"unit_id": i, "chip": "0x856014", "firmware": "x", "name": None}
             for s, i in IDS.items()}
    monkeypatch.setattr(web_sets, "live_units", lambda: units)
    with web_sets._PUSH_LOCK:
        web_sets._PUSH.update(running=False, set="", steps=[], ok=None, message="")
    return tmp_path


def picks(*slots):
    return [{"slot": f"fm1-{s}", "package": f"{s}.fwsc"} for s in slots]


class Rec:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def flash(self, unit_id, path, expected_sha, confirm):
        self.calls.append(("flash", unit_id, path.name, expected_sha, confirm))
        if unit_id == self.fail_on:
            raise WriteRefused("the unit did not answer")
        return {"message": "installed"}

    def restore(self, unit_id, backup, confirm):
        self.calls.append(("restore", unit_id, backup, confirm))
        return {"message": "restored"}


def test_save_pins_the_package_hash_and_lists_it(env):
    web_sets.save_set("jam", "friday", picks("a", "b", "c", "d"))
    ov = web_sets.overview()
    assert [s["name"] for s in ov["sets"]] == ["jam"]
    first = ov["sets"][0]["members"][0]
    assert first["sha256"] == sha(b"fw-a")
    assert [s["slot"] for s in ov["slots"]] == list(IDS)
    assert {p["name"] for p in ov["library"]} == {"a.fwsc", "b.fwsc", "c.fwsc", "d.fwsc"}


def test_save_refuses_missing_package_and_empty_pick(env):
    with pytest.raises(WriteRefused):
        web_sets.save_set("jam", "", [{"slot": "fm1-a", "package": "nope.fwsc"}])
    with pytest.raises(WriteRefused):
        web_sets.save_set("jam", "", [])
    with pytest.raises(WriteRefused):
        web_sets.save_set("jam", "", [{"slot": "fm1-a"}])


def test_save_backup_newest_uses_the_units_latest_image(env):
    d = env / "backups" / "auto"
    d.mkdir(parents=True)
    for stamp in ("20261125-090000", "20261125-100000"):
        (d / f"fm1-{stamp}.bin").write_bytes(b"img")
        (d / f"fm1-{stamp}.json").write_text(json.dumps(
            {"unit id": IDS["fm1-a"], "saved": stamp, "firmware": "grimm"}))
    web_sets.save_set("before", "", [{"slot": "fm1-a", "backup": "newest"}])
    member = web_sets.overview()["sets"][0]["members"][0]
    assert member["backup"] == "auto/fm1-20261125-100000.bin"
    with pytest.raises(WriteRefused, match="no saved image"):
        web_sets.save_set("b2", "", [{"slot": "fm1-b", "backup": "newest"}])


def test_snapshot_pins_each_units_newest_image_and_names_the_skipped(env):
    d = env / "backups" / "auto"
    d.mkdir(parents=True)
    for slot in ("fm1-a", "fm1-c"):
        (d / f"{slot}.bin").write_bytes(b"img")
        (d / f"{slot}.json").write_text(json.dumps(
            {"unit id": IDS[slot], "saved": "20261125-100000", "firmware": "x"}))
    out = web_sets.snapshot("before-friday")
    assert [m["slot"] for m in out["members"]] == ["fm1-a", "fm1-c"]
    assert out["skipped"] == ["fm1-b", "fm1-d"]
    assert web_sets.overview()["sets"][0]["name"] == "before-friday"
    assert web_sets.snapshot()["name"].startswith("rig-")


def test_snapshot_with_no_images_is_refused(env):
    with pytest.raises(WriteRefused, match="no saved images"):
        web_sets.snapshot("x")


def test_delete_needs_the_name(env):
    web_sets.save_set("jam", "", picks("a"))
    with pytest.raises(WriteRefused):
        web_sets.delete_set("jam", "wrong")
    web_sets.delete_set("jam", "jam")
    assert web_sets.overview()["sets"] == []


def test_push_all_four_in_order_through_the_id_matched_writers(env):
    web_sets.save_set("jam", "", picks("a", "b", "c", "d"))
    rec = Rec()
    out = web_sets.start_push("jam", None, "jam", flash=rec.flash, restore=rec.restore,
                              run_async=False)
    assert out["running"] is False and out["ok"] is True
    assert [c[1] for c in rec.calls] == list(IDS.values())
    assert all(c[4] == c[1] for c in rec.calls)           # confirm = the unit's own flash id
    assert rec.calls[0][3] == sha(b"fw-a")                # pinned hash is what is sent
    assert [s["status"] for s in out["steps"]] == ["done"] * 4
    log = (env / "flash.log").read_text()
    assert "set-push" in log and "set-step" in log


def test_push_a_subset_leaves_other_units_alone(env):
    web_sets.save_set("jam", "", picks("a", "b", "c", "d"))
    rec = Rec()
    web_sets.start_push("jam", ["fm1-b", "fm1-d"], "jam", flash=rec.flash,
                        restore=rec.restore, run_async=False)
    assert [c[1] for c in rec.calls] == [IDS["fm1-b"], IDS["fm1-d"]]


def test_push_stops_at_the_first_failure(env):
    web_sets.save_set("jam", "", picks("a", "b", "c", "d"))
    rec = Rec(fail_on=IDS["fm1-b"])
    out = web_sets.start_push("jam", None, "jam", flash=rec.flash, restore=rec.restore,
                              run_async=False)
    assert out["ok"] is False
    assert [s["status"] for s in out["steps"]] == ["done", "failed", "not started",
                                                   "not started"]
    assert len(rec.calls) == 2
    assert "did not answer" in out["message"]


def test_wrong_confirm_changed_package_or_missing_unit_write_nothing(env):
    web_sets.save_set("jam", "", picks("a", "b"))
    rec = Rec()
    with pytest.raises(WriteRefused, match="confirmation"):
        web_sets.start_push("jam", None, "nope", flash=rec.flash, restore=rec.restore,
                            run_async=False)
    (env / "lib" / "b.fwsc").write_bytes(b"changed")
    with pytest.raises(WriteRefused, match="nothing was written"):
        web_sets.start_push("jam", None, "jam", flash=rec.flash, restore=rec.restore,
                            run_async=False)
    with pytest.raises(WriteRefused, match="no set named"):
        web_sets.start_push("ghost", None, "ghost", flash=rec.flash, restore=rec.restore,
                            run_async=False)
    assert rec.calls == []
    # The unchanged unit can still go on its own, which is the point of subsets.
    web_sets.start_push("jam", ["fm1-a"], "jam", flash=rec.flash, restore=rec.restore,
                        run_async=False)
    assert len(rec.calls) == 1


def test_preflight_reports_what_blocks_without_writing(env):
    web_sets.save_set("jam", "", picks("a", "b"))
    (env / "lib" / "a.fwsc").unlink()
    steps = web_sets.preflight("jam")
    assert "not in the library" in steps[0]["blocking"] and steps[1]["blocking"] == ""


def test_a_second_push_is_refused_while_one_runs(env):
    web_sets.save_set("jam", "", picks("a"))
    release = threading.Event()

    def slow(unit_id, path, expected_sha, confirm):
        release.wait(5)
        return {"message": "installed"}

    rec = Rec()
    web_sets.start_push("jam", None, "jam", flash=slow, restore=rec.restore)
    try:
        assert web_sets.push_status()["running"] is True
        with pytest.raises(WriteRefused, match="already being pushed"):
            web_sets.start_push("jam", None, "jam", flash=rec.flash, restore=rec.restore,
                                run_async=False)
    finally:
        release.set()
    for _ in range(100):
        if not web_sets.push_status()["running"]:
            break
        time.sleep(0.02)
    assert web_sets.push_status()["ok"] is True
    assert rec.calls == []


def test_broken_sets_file_is_reported_not_overwritten(env):
    (env / "sets.json").write_text("{ nope")
    assert "cannot be read" in web_sets.overview()["error"]
    with pytest.raises(WriteRefused):
        web_sets.save_set("jam", "", picks("a"))
    assert (env / "sets.json").read_text() == "{ nope"


# ------------------------------------------------------------------------------ HTTP

@pytest.fixture()
def srv(env, monkeypatch):
    rec = Rec()
    monkeypatch.setattr(web_sets, "flash_unit_by_id", rec.flash)
    monkeypatch.setattr(web_sets, "restore_unit_by_id", rec.restore)
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], rec
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    body = json.dumps(payload).encode() if payload is not None else None
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()
    return r.status, data


def test_http_save_preflight_push(srv):
    port, rec = srv
    st, d = call(port, "POST", "/api/sets/save",
                 {"name": "jam", "members": picks("a", "b", "c", "d")})
    assert st == 200 and d["ok"]
    st, d = call(port, "GET", "/api/sets/preflight?name=jam&only=fm1-a,fm1-b")
    assert st == 200 and [s["slot"] for s in d["steps"]] == ["fm1-a", "fm1-b"]
    st, d = call(port, "POST", "/api/sets/push", {"name": "jam", "confirm": "x"})
    assert st == 409 and rec.calls == []
    st, d = call(port, "POST", "/api/sets/push",
                 {"name": "jam", "confirm": "jam", "only": ["fm1-c"]})
    assert st == 200
    for _ in range(100):
        if not web_sets.push_status()["running"]:
            break
        time.sleep(0.02)
    assert [c[1] for c in rec.calls] == [IDS["fm1-c"]]
    st, d = call(port, "GET", "/api/sets")
    assert st == 200 and d["push"]["ok"] is True


def test_http_push_rejects_bad_only(srv):
    port, rec = srv
    call(port, "POST", "/api/sets/save", {"name": "jam", "members": picks("a")})
    st, _ = call(port, "POST", "/api/sets/push", {"name": "jam", "confirm": "jam", "only": "a"})
    assert st == 409 and rec.calls == []
