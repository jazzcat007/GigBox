"""Tests for the hub catalogue client, the package inspector and the web API.

The package tests are the important ones: they assert the *known* verdict for real
packages we have installed on hardware, so a future change to the parser cannot
silently start calling a resource-carrying package "safe".

Fixtures: tests build synthetic .fwsc files so the suite runs offline and without the
612 KB downloads. ``test_real_packages`` is skipped unless the real files are present.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from fm1_switch import hub, packages, web


# --------------------------------------------------------------------- builder
#
# A minimal .fwsc writer, following the format the parser reads. Deliberately written
# independently of packages.py so a bug in the parser is not mirrored in the fixture.

def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _enc(data: bytes, key: int) -> bytes:
    out = bytearray()
    for b in data:
        out.append(b ^ (key & 0xFF))
        key = ((key << 1) ^ (0x1021 if key & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def _table_entry(name: str, addr: int, length: int, attr: int, data_crc: int = 0) -> bytes:
    raw = bytearray(32)
    struct.pack_into("<H", raw, 2, data_crc)
    struct.pack_into("<I", raw, 4, addr)
    struct.pack_into("<I", raw, 8, length)
    raw[12] = attr
    nb = name.encode("ascii")[:15]
    raw[16:16 + len(nb)] = nb
    struct.pack_into("<H", raw, 0, _crc16(bytes(raw[2:])))
    return bytes(raw)


def build_fwsc(
    path: Path,
    *,
    identity: str = "FM-1_900",
    image_size: int = 0x93000,
    chip_key: int = 0x980F,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    """Write a synthetic package that the parser should read back."""
    image = bytearray(b"\xFF" * image_size)
    # app area: a file table with one file entry and one region descriptor
    entries = [
        _table_entry("app.bin", 0x120, 0x100, 0x82),
        _table_entry("USR", 0xEA000, 0x100, 0x92),
    ]
    for i, e in enumerate(entries):
        plain = _enc(e, chip_key ^ (32 * i >> 2))
        image[0x4000 + 32 * i:0x4000 + 32 * i + 32] = plain

    files = {"flash.bin": bytes(image)}
    files["ota.bin"] = b"OTA" * 10
    for name, data in (extra_files or {}).items():
        files[name] = data

    # header + file list
    count = len(files)
    list_len = 0x50 * count
    head = bytearray(64)
    struct.pack_into("<H", head, 8, count)
    flist = bytearray(list_len)
    offset = 0x40 + list_len
    body = bytearray()
    for i, (name, data) in enumerate(files.items()):
        e = bytearray(0x50)
        struct.pack_into("<H", e, 0, 0 if name == "flash.bin" else 100)
        struct.pack_into("<H", e, 4, _crc16(data))
        struct.pack_into("<I", e, 8, offset)
        struct.pack_into("<I", e, 12, len(data))
        nb = name.encode("ascii")
        e[0x40:0x40 + len(nb)] = nb
        flist[i * 0x50:(i + 1) * 0x50] = _enc(bytes(e), 0xFFFF)
        body += data
        offset += len(data)
    struct.pack_into("<H", head, 2, _crc16(bytes(flist)))
    struct.pack_into("<H", head, 0, _crc16(bytes(head[2:])))
    container = bytes(_enc(bytes(head), 0xFFFF)) + bytes(flist) + bytes(body)

    # Weave the identity INTO the container: byte 47 of every 48-byte block is one
    # identity character, starting at block 0. It is not a prefix — the container's
    # first byte is byte 0 of block 0. Verified against sloop/obervave/choralroot:
    # stripping byte 0x2F from each block leaves a container whose header CRC passes
    # and whose first file entry reads "flash.bin", and the stripped column decodes to
    # exactly "FM-1_900" etc, one char per block.
    ident = identity.encode("ascii")
    nblocks = packages.IDENTITY_MAX_BLOCKS
    assert len(ident) < nblocks
    out = bytearray()
    ci = 0
    for b in range(nblocks):
        chunk = container[ci:ci + 0x2F]
        ci += len(chunk)
        out += chunk.ljust(0x2F, b"\x00")
        out.append((ident[b] + b + 1) & 0xFF if b < len(ident) else 0x7D)
    out += container[ci:]
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------------- packages

def test_clean_package_is_ok(tmp_path):
    p = build_fwsc(tmp_path / "clean.fwsc")
    ins = packages.inspect(p)
    assert ins.severity == "ok", ins.headline
    assert ins.complete_from_mflasher


def test_extra_payload_alone_is_not_a_warning(tmp_path):
    # Regression, found on hardware: SHRUB carries a 72 KiB res.bin and installs
    # completely, and V15 carries a 71 KiB USR file and installs completely. An extra
    # file by itself must NOT be reported as a problem.
    p = build_fwsc(tmp_path / "res.fwsc", extra_files={"res.bin": b"\x00\x01\x02" * 4096})
    ins = packages.inspect(p)
    assert ins.severity == "ok", ins.headline
    # the file is still disclosed in the details
    assert any("res.bin" in d for d in ins.details)


def test_factory_sound_bank_is_warned(tmp_path):
    # What actually separated the bad installs: Melodee's res.bin holds readable bank
    # names ("BRASS 1", "STRINGS 2"), and that firmware reported missing factory sounds.
    bank = b"\x00" * 64 + b"\x00".join(
        [b"BRASS 1", b"BRASS 2", b"STRINGS 1", b"STRINGS 2"]) + b"\x00" * 64
    p = build_fwsc(tmp_path / "bank.fwsc", extra_files={"res.bin": bank})
    ins = packages.inspect(p)
    assert ins.severity == "warn"
    assert "factory sound bank" in ins.headline
    assert any("BRASS 1" in d for d in ins.details)


def test_identity_roundtrip(tmp_path):
    for ident in ("FM-1_900", "FM-1_9535", "FM-1_9010202"):
        p = build_fwsc(tmp_path / f"{ident}.fwsc", identity=ident)
        assert packages.parse(p).identity == ident


def test_chip_key_recovered(tmp_path):
    p = build_fwsc(tmp_path / "k.fwsc", chip_key=0x980F)
    assert packages.parse(p).chip_key == 0x980F


def test_corrupt_file_list_is_rejected(tmp_path):
    p = build_fwsc(tmp_path / "bad.fwsc")
    data = bytearray(p.read_bytes())
    # damage the container header (after the identity blocks)
    data[0x30 + 40] ^= 0xFF
    p.write_bytes(bytes(data))
    with pytest.raises(packages.PackageError):
        packages.parse(p)


def test_not_a_package_is_rejected(tmp_path):
    p = tmp_path / "random.fwsc"
    p.write_bytes(b"\x00" * 8192)
    ins = packages.inspect(p)
    assert ins.severity == "warn"
    assert "Unreadable" in ins.headline


REAL_DIR = Path(__file__).resolve().parent.parent / "realtest"

REAL_EXPECTED = {
    # Firmwares we have actually put on hardware, with what actually happened. The
    # inspector must keep matching reality — including the two that look like they
    # should fail and do not (SHRUB and V15 both carry extra payload files and both
    # installed completely), and the two that failed after looking fine.
    "sloop-2.8.5.fwsc": "ok",        # installed cleanly
    "felucca-1.8.2.fwsc": "ok",      # installed cleanly
    "obervave-3.5.2.fwsc": "ok",     # installed cleanly
    "melodee-1.6.1.fwsc": "warn",    # "Setup unfinished / Factory sounds missing"
    "choralroot-0.42.fwsc": "warn",  # "Presets Missing"
    "SHRUB.fwsc": "ok",              # installed completely (carries res.bin, no names)
    "V15-FM-1.fwsc": "ok",           # installed completely, on both flash chips
}


@pytest.mark.skipif(
    not all((REAL_DIR / n).exists() for n in REAL_EXPECTED),
    reason="real packages not in realtest/ (run tools/fetch-realtest.sh)",
)
def test_real_packages():
    """The parser, checked against packages whose behaviour we know on hardware."""
    for name, want in REAL_EXPECTED.items():
        ins = packages.inspect(REAL_DIR / name)
        assert ins.severity == want, f"{name}: {ins.severity} != {want} ({ins.headline})"


@pytest.mark.skipif(
    not all((REAL_DIR / n).exists() for n in REAL_EXPECTED),
    reason="real packages not in realtest/",
)
def test_real_packages_report_regions_and_key():
    pkg = packages.parse(REAL_DIR / "sloop-2.8.5.fwsc")
    assert pkg.identity == "FM-1_900"
    assert pkg.chip_key == 0x980F
    names = {r.name for r in pkg.regions}
    assert {"VM", "BTIF", "USR"} <= names
    # no extra payload: clean installs carry exactly flash.bin + ota.bin
    assert pkg.extra_payload == []


# --------------------------------------------------------------------- hub

def _entry(**kw) -> hub.Entry:
    base = dict(
        id="x", name="X", version="1.0", author="A", repo="a/x", ident="FM-1_900",
        what="w", sha256="a" * 64, size=100, chip="new",
    )
    base.update(kw)
    return hub.Entry(**base)


def test_chip_note_old_unit_never_warns():
    # The hub's "new" means "knows the newer chip". On an old chip every firmware can
    # write, so neither value may produce a warning.
    for chip in ("new", "old", "any"):
        sev, _ = _entry(chip=chip).chip_note("0x856014")
        assert sev == "ok", (chip, sev)


def test_chip_note_new_unit_warns_only_for_old():
    assert _entry(chip="new").chip_note("0xB36014")[0] == "ok"
    assert _entry(chip="any").chip_note("0xB36014")[0] == "ok"
    assert _entry(chip="old").chip_note("0xB36014")[0] == "warn"


def test_ident_is_a_regex():
    e = _entry(ident=r"FM-1_95[3-9]\d")
    assert e.ident_matches("FM-1_9535")
    assert not e.ident_matches("FM-1_901")
    assert not e.ident_matches(None)


def test_external_install_is_not_flashable():
    e = _entry(external_install=True)
    assert not e.installable_by_us


def test_parse_catalog_tolerates_rubbish():
    cat = hub.parse_catalog({"firmwares": [
        {"id": "a", "name": "A"},
        "not a dict",
        {"no": "id"},
    ]})
    assert [e.id for e in cat.entries] == ["a"]


# The catalogue the Pi really fetched on 2026-11-25 (B15). Unedited.
REAL_CATALOG = Path(__file__).parent / "captures" / "hub-catalog-2026-11-25.json"


def test_real_catalogue_capture_parses_every_entry():
    raw = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    cat = hub.parse_catalog(raw)
    assert len(cat.entries) == len(raw["firmwares"]) == 322
    by_id = {e.id: e for e in cat.entries}
    assert by_id["sloop"].sha256 and len(by_id["sloop"].sha256) == 64
    assert by_id["sloop"].pkg.endswith(".fwsc")


def test_load_without_network_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path))
    (tmp_path / "catalog.json").write_bytes(REAL_CATALOG.read_bytes())
    cat = hub.load(network=False)
    assert cat.from_cache and len(cat.entries) == 322
    assert "sloop" in {e.id for e in cat.entries}


def test_load_without_network_or_cache_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "empty"))
    with pytest.raises(hub.HubUnavailable):
        hub.load(network=False)


def test_fetch_refuses_unpinned(tmp_path, monkeypatch):
    entry = _entry(sha256="", pkg="fw/x.fwsc")
    with pytest.raises(hub.PackageDownloadError, match="no checksum"):
        hub.fetch_package(entry, tmp_path)


def test_fetch_refuses_external_install(tmp_path):
    entry = _entry(external_install=True, pkg="fw/x.fwsc")
    with pytest.raises(hub.PackageDownloadError):
        hub.fetch_package(entry, tmp_path)


def test_fetch_rejects_checksum_mismatch(tmp_path, monkeypatch):
    payload = b"firmware bytes"

    def fake_urlopen(req, timeout=0):
        class R(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(payload)

    monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)
    entry = _entry(sha256="0" * 64, size=len(payload), pkg="fw/x.fwsc")
    with pytest.raises(hub.ChecksumMismatch):
        hub.fetch_package(entry, tmp_path)
    # nothing may be left behind for the library scan to pick up
    assert list(tmp_path.iterdir()) == []


def test_fetch_accepts_matching_checksum(tmp_path, monkeypatch):
    payload = b"firmware bytes"
    good = hashlib.sha256(payload).hexdigest()

    def fake_urlopen(req, timeout=0):
        class R(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(payload)

    monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)
    entry = _entry(sha256=good, size=len(payload), pkg="fw/x.fwsc")
    out = hub.fetch_package(entry, tmp_path)
    assert out.read_bytes() == payload
    assert not (tmp_path / "x.fwsc.part").exists()


# --------------------------------------------------------------------- web api

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live server on a thread, with an isolated library and no network."""
    monkeypatch.setenv("FM1_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "backups"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nomap.txt"))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    (tmp_path / "hub").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "hub" / "catalog.json").write_text(json.dumps({
        "updated": "test",
        "firmwares": [
            {"id": "sloop", "name": "SLOOP", "version": "2.8.5", "author": "isod89",
             "repo": "isod89/sloop-fm1", "ident": "FM-1_900", "what": "groovebox",
             "sha256": "a" * 64, "size": 610076, "chip": "new", "kind": ["groove"],
             "pkg": "fw/sloop-2.8.5.fwsc", "stars": 10},
            {"id": "melodee", "name": "Melodee", "version": "1.6.1", "author": "k",
             "repo": "k/melodee", "ident": "FM-1_91061", "what": "sampler",
             "sha256": "b" * 64, "size": 668948, "chip": "new", "kind": ["drums"],
             "pkg": "fw/melodee-1.6.1.fwsc", "externalInstall": True},
        ],
    }))

    from fm1_switch import web

    web.APP.catalog = None
    web.APP.catalog_tried = False
    web.APP.units = {}
    web.APP.last_event = None
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    import urllib.request

    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path) as r:
            return json.loads(r.read())

    def post(path, payload):
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield get, post, tmp_path
    httpd.shutdown()
    httpd.server_close()


def test_index_serves(client):
    get, _post, _ = client
    state = get("/api/state")
    assert len(state["units"]) == 4
    assert state["attribution"].startswith("Firmware list: The Firmware Hub")


def test_catalog_search_and_flags(client):
    get, _post, _ = client
    data = get("/api/catalog?q=sloop")
    assert data["total"] == 2
    assert [e["id"] for e in data["entries"]] == ["sloop"]
    assert data["entries"][0]["installable_by_us"] is True
    m = get("/api/catalog?q=melodee")["entries"][0]
    assert m["installable_by_us"] is False


def test_flash_requires_matching_confirm(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": "", "confirm": "fm1-b"})
    assert code == 409
    assert "confirmation" in body["error"]


def test_flash_refuses_hash_drift(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": "f" * 64, "confirm": "aaaaaaaa00000001"})
    assert code == 409
    assert "changed" in body["error"]


def test_flash_refuses_absent_unit(client):
    _get, post, tmp_path = client
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": sha, "confirm": "aaaaaaaa00000001"})
    assert code == 409


def test_slot_addressed_flash_is_gone(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"slot": "fm1-a", "package": "p.fwsc",
                                     "sha256": "", "confirm": "fm1-a"})
    assert code == 409 and "unit_id" in body["error"]


def test_package_name_traversal_refused(client):
    _get, post, _ = client
    code, _ = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "../etc/passwd",
                                  "sha256": "", "confirm": "aaaaaaaa00000001"})
    assert code == 409
    assert code == 409


def test_flash_by_id_requires_matching_confirm(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": "", "confirm": "aaaaaaaa00000002"})
    assert code == 409 and "confirmation" in body["error"]


def test_flash_by_id_refuses_placeholder_ids(client):
    _get, post, tmp_path = client
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    for bad in ("port:1-1.2", "device:sg0"):
        code, body = post("/api/flash", {"unit_id": bad, "package": "p.fwsc",
                                         "sha256": sha, "confirm": bad})
        assert code == 409 and "flash id" in body["error"]


def test_flash_by_id_refuses_hash_drift(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": "f" * 64, "confirm": "aaaaaaaa00000001"})
    assert code == 409 and "changed" in body["error"]


def test_flash_by_id_calls_the_guided_flow_with_the_id(client, monkeypatch):
    _get, post, tmp_path = client
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    from fm1_switch import guided, web
    seen = {}

    def fake(unit_id, package, *, lock, on_phase, **kw):
        seen.update(unit_id=unit_id, package=package)
        on_phase("Writing…")
        assert web.APP.busy == "Writing…"
        return guided.Outcome(unit_id, "/dev/sg9", False, restarted=["x"], message="installed")
    monkeypatch.setattr(guided, "flash", fake)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": sha, "confirm": "aaaaaaaa00000001"})
    assert code == 200 and body["event"]["ok"] and body["event"]["restarted"] == ["x"]
    log = [json.loads(l) for l in (tmp_path / "flash.log").read_text().splitlines()]
    # The 100-byte fake is unreadable, so its verdict is not "ok": a write of it is treated as
    # two-step (unfinished until confirmed). That is the conservative reading, and it is logged.
    assert [e["event"] for e in log] == ["request", "done", "setup-pending"]
    assert log[0]["package"] == "p.fwsc" and log[0]["unit_id"] == "aaaaaaaa00000001"
    assert seen["unit_id"] == "aaaaaaaa00000001" and seen["package"].endswith("p.fwsc")
    assert web.APP.busy is None


def test_fleet_is_served_from_cache_while_a_write_runs(client, monkeypatch):
    get, _post, _ = client
    from fm1_switch import registry, web
    monkeypatch.setattr(registry, "known", lambda: [])
    first = get("/api/fleet")
    assert first["busy"] is None

    def boom():
        raise AssertionError("must not scan while a write is running")
    monkeypatch.setattr(registry, "known", boom)
    web.APP.lock.acquire()
    web.APP.busy = "Writing…"
    try:
        again = get("/api/fleet")
    finally:
        web.APP.busy = None
        web.APP.lock.release()
    assert again["busy"] == "Writing…" and again["units"] == first["units"]


def test_restart_endpoint_needs_matching_confirm_and_a_real_id(client):
    _get, post, _ = client
    code, body = post("/api/restart", {"unit_id": "aaaaaaaa00000001", "confirm": "x"})
    assert code == 409 and "confirmation" in body["error"]
    code, body = post("/api/restart", {"unit_id": "port:1-1", "confirm": "port:1-1"})
    assert code == 409 and "flash id" in body["error"]


def test_flashlog_lists_finished_records_newest_first(client, monkeypatch, tmp_path):
    get, _post, _ = client
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join([
        '{"at": "2026-11-22 10:00:00", "event": "request", "unit_id": "a"}',
        '{"at": "2026-11-22 10:01:00", "event": "done", "unit_id": "a", "package": "p.fwsc", "message": "installed"}',
        'not json',
        '{"at": "2026-11-22 10:02:00", "event": "refused", "unit_id": "b", "message": "no"}',
    ]) + "\n")
    monkeypatch.setenv("FM1_FLASHLOG", str(log))
    items = get("/api/flashlog")["items"]
    assert [i["event"] for i in items] == ["refused", "done"]


# ---- restore from backup -------------------------------------------------------------

def _backup(root, name, unit, sha=None, firmware="FM-1 901", saved="20261121-150141", data=b"img"):
    import hashlib
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.bin").write_bytes(data)
    (root / f"{name}.json").write_text(json.dumps({
        "unit id": unit, "firmware": firmware, "saved": saved, "bootloader": "stock",
        "sha256": sha or hashlib.sha256(data).hexdigest()}))


def test_backups_lists_only_this_units_images_newest_first(client, monkeypatch, tmp_path):
    get, _post, _ = client
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path))
    _backup(tmp_path / "x", "old", "aaaaaaaa00000003", saved="20261120-100000")
    _backup(tmp_path / "y", "new", "aaaaaaaa00000003", saved="20261121-150141")
    _backup(tmp_path, "other", "aaaaaaaa00000004")
    items = get("/api/backups?unit_id=aaaaaaaa00000003")["items"]
    assert [i["id"] for i in items] == ["y/new.bin", "x/old.bin"]
    assert get("/api/backups?unit_id=port:1-1")["items"] == []


def test_backups_report_space_used_and_free(client, monkeypatch, tmp_path):
    get, _post, _ = client
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path / "not-yet"))
    space = get("/api/backups?unit_id=aaaaaaaa00000003")["space"]
    assert space["used_kb"] == 0 and space["free_mb"] > 0
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path))
    _backup(tmp_path, "mine", "aaaaaaaa00000003")
    assert get("/api/backups?unit_id=aaaaaaaa00000003")["space"]["used_kb"] >= 0


def test_sha256_and_package_answers_are_remembered_until_the_file_changes(tmp_path):
    from fm1_switch import web
    f = tmp_path / "p.fwsc"
    f.write_bytes(b"one")
    first = web._sha256(f)
    f.write_bytes(b"two!")
    assert web._sha256(f) != first
    web._SHA_CACHE.clear()
    assert web._sha256(f) == web._sha256(f)
    assert len([k for k in web._SHA_CACHE if k[0] == str(f)]) == 1


def test_restore_refuses_wrong_unit_bad_checksum_escape_and_missing_confirm(client, monkeypatch, tmp_path):
    _get, post, _ = client
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path))
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "log"))
    _backup(tmp_path, "mine", "aaaaaaaa00000003")
    _backup(tmp_path, "theirs", "aaaaaaaa00000004")
    _backup(tmp_path, "bad", "aaaaaaaa00000003", sha="0" * 64)
    u = "aaaaaaaa00000003"
    called = []
    monkeypatch.setattr(web.guided, "flash", lambda *a, **k: called.append(a))
    for body, msg in [
        ({"unit_id": u, "backup": "mine.bin", "confirm": "x"}, "confirmation"),
        ({"unit_id": u, "backup": "theirs.bin", "confirm": u}, "different unit"),
        ({"unit_id": u, "backup": "bad.bin", "confirm": u}, "checksum"),
        ({"unit_id": u, "backup": "../etc/passwd", "confirm": u}, "does not exist"),
        ({"unit_id": u, "backup": "nope.bin", "confirm": u}, "does not exist"),
    ]:
        code, out = post("/api/restore", body)
        assert code == 409 and msg in out["error"], (body, out)
    assert called == []


def test_restore_runs_the_guided_flow_with_a_restore_writer(client, monkeypatch, tmp_path):
    _get, post, _ = client
    monkeypatch.setenv("FM1_BACKUPS", str(tmp_path))
    log = tmp_path / "log"
    monkeypatch.setenv("FM1_FLASHLOG", str(log))
    _backup(tmp_path, "mine", "aaaaaaaa00000003")
    seen = {}

    def fake_flash(unit_id, image, **kw):
        seen.update(unit_id=unit_id, image=image, label=kw["firmware_label"])
        calls = []
        monkeypatch.setattr(web.mflasher, "restore_image",
                            lambda node, img, **k: calls.append((node, img, k)))
        kw["writer"]("/dev/sg9", image)
        seen["writer_call"] = calls[0]
        return web.guided.Outcome(unit_id, "/dev/sg9", False, [], [], "installed", "ok")
    monkeypatch.setattr(web.guided, "flash", fake_flash)
    code, out = post("/api/restore", {"unit_id": "aaaaaaaa00000003", "backup": "mine.bin",
                                      "confirm": "aaaaaaaa00000003"})
    assert code == 200 and out["event"]["ok"]
    assert seen["unit_id"] == "aaaaaaaa00000003" and seen["label"] == "FM-1 901"
    assert seen["writer_call"][2] == {"keep_bootloader": True}
    assert '"event": "done"' in log.read_text() and "restore mine.bin" in log.read_text()


def test_prune_keeps_the_newest_per_unit_and_only_in_the_given_dir(tmp_path):
    auto = tmp_path / "auto"
    for i in range(5):
        _backup(auto, f"c{i}", "aaaaaaaa00000003", saved=f"2026112{i}-100000")
    _backup(auto, "d0", "aaaaaaaa00000004", saved="20261120-100000")
    _backup(tmp_path, "hand", "aaaaaaaa00000003", saved="20200101-000000")   # outside auto/
    gone = web.prune_auto_backups(auto, keep=2)
    assert sorted(gone) == ["c0", "c1", "c2"]
    left = sorted(p.stem for p in auto.glob("*.bin"))
    assert left == ["c3", "c4", "d0"]
    assert not (auto / "c0.json").exists() and (tmp_path / "hand.bin").exists()


# ---- backup now / delete / names / updates / downloads --------------------------------

U = "aaaaaaaa00000003"


def test_backup_now_saves_to_manual_then_restarts_even_when_the_copy_fails(client, monkeypatch, tmp_path):
    _get, post, _ = client
    log = []
    monkeypatch.setattr(web.mflasher, "backup_unit", lambda node, d: log.append(("backup", node, d)))
    monkeypatch.setattr(web.mflasher, "restart", lambda node: log.append(("restart", node)))

    def fake_flash(unit_id, pkg, **kw):
        assert kw.get("before_write") is None          # a backup is not preceded by a backup
        kw["writer"]("/dev/sg4", pkg)
        return web.guided.Outcome(unit_id, "/dev/sg4", False, [], [], "installed", "ok")
    monkeypatch.setattr(web.guided, "flash", fake_flash)
    code, out = post("/api/backup", {"unit_id": U, "confirm": U})
    assert code == 200 and out["event"]["ok"]
    assert log[0][0] == "backup" and log[0][2].endswith("backups/manual") and log[1] == ("restart", "/dev/sg4")

    log.clear()
    monkeypatch.setattr(web.mflasher, "backup_unit",
                        lambda node, d: (_ for _ in ()).throw(web.mflasher.Exited(1, "disk full")))
    code, out = post("/api/backup", {"unit_id": U, "confirm": U})
    assert code == 409 and "disk full" in out["error"] and log == [("restart", "/dev/sg4")]
    assert post("/api/backup", {"unit_id": U, "confirm": "x"})[0] == 409


def test_delete_backup_only_removes_this_units_image(client, monkeypatch, tmp_path):
    _get, post, _ = client
    root = tmp_path / "backups"
    _backup(root / "manual", "mine", U)
    _backup(root / "manual", "theirs", "aaaaaaaa00000004")
    assert post("/api/backup/delete", {"unit_id": U, "backup": "manual/theirs.bin", "confirm": U})[0] == 409
    assert post("/api/backup/delete", {"unit_id": U, "backup": "../x.bin", "confirm": U})[0] == 409
    assert post("/api/backup/delete", {"unit_id": U, "backup": "manual/mine.bin", "confirm": "x"})[0] == 409
    code, out = post("/api/backup/delete", {"unit_id": U, "backup": "manual/mine.bin", "confirm": U})
    assert code == 200 and out["removed"] == "mine.bin"
    assert not (root / "manual" / "mine.bin").exists() and not (root / "manual" / "mine.json").exists()
    assert (root / "manual" / "theirs.bin").exists()


def test_names_are_cleaned_stored_on_the_binding_and_survive_a_rebind(tmp_path, monkeypatch):
    from fm1_switch import registry
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    reg = registry.Registry()
    reg.bind(U, "fm1-c")
    registry.save(reg)
    assert registry.rename(U, "  bass  drum ") == "BASS DRUM"
    assert registry.load().binding_for_id(U).name == "BASS DRUM"
    reg = registry.load()
    reg.bind(U, "fm1-d")                       # moved to another sticker: keeps its name
    assert reg.binding_for_id(U).name == "BASS DRUM"
    assert registry.rename(U, "") == "" and registry.load().binding_for_id(U).name is None
    with pytest.raises(ValueError):
        registry.rename(U, "x" * 13)
    with pytest.raises(ValueError):
        registry.rename(U, "a/b")
    with pytest.raises(KeyError):
        registry.rename("0" * 16, "X")


def test_rename_endpoint(client, monkeypatch, tmp_path):
    _get, post, _ = client
    from fm1_switch import registry
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    reg = registry.Registry()
    reg.bind(U, "fm1-c")
    registry.save(reg)
    assert post("/api/rename", {"unit_id": U, "name": "bass", "confirm": U}) == (200, {"ok": True, "name": "BASS"})
    assert post("/api/rename", {"unit_id": U, "name": "bass", "confirm": "x"})[0] == 409
    code, out = post("/api/rename", {"unit_id": U, "name": "no/way", "confirm": U})
    assert code == 409 and "letters" in out["error"]


def test_update_for_compares_the_running_label_with_the_catalogue(client):
    cat = web.APP.catalog or web.APP.load_catalog(network=False)
    assert web.update_for("sloop-2.8.4", cat)["latest"] == "2.8.5"
    assert web.update_for("sloop-2.8.4", cat)["package"] == "sloop-2.8.5.fwsc"
    assert web.update_for("sloop-2.8.5", cat) is None          # already current
    assert web.update_for("sloop-2.10.0", cat) is None         # numeric, not text, comparison
    assert web.update_for("FM-1 901", cat) is None             # stock firmware: no claim
    assert web.update_for("melodee-1.0.0", cat) is None        # needs its maker's installer
    assert web.update_for("nosuch-1.0", cat) is None and web.update_for(None, cat) is None


def test_getlist_offers_installable_firmware_not_already_on_the_pi(client, tmp_path):
    get, _post, _ = client
    assert [i["id"] for i in get("/api/getlist")["items"]] == ["sloop"]   # melodee is external
    (tmp_path / "lib" / "sloop-2.8.5.fwsc").write_bytes(b"x")
    assert get("/api/getlist")["items"] == []


def test_fleet_carries_names_updates_and_the_last_event(client, monkeypatch):
    get, _post, _ = client
    monkeypatch.setattr(web.registry, "known", lambda: [web.registry.LiveUnit(
        unit_id=U, slot="fm1-c", running=True, firmware="sloop-2.8.4", name="BASS")])
    web.APP.last_event = {"unit_id": U, "ok": False, "message": "x", "at": 1.0}
    out = get("/api/fleet")
    assert out["units"][0]["name"] == "BASS" and out["units"][0]["update"]["latest"] == "2.8.5"
    assert out["last_event"]["unit_id"] == U and out["now"] > 1


# ---- B6: SAFE-02 (sha required), SAFE-04 (backup verified), GD-02 (failed is logged) ----

def test_flash_without_a_sha_is_refused(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "confirm": "aaaaaaaa00000001"})
    assert code == 409 and "sha256" in body["error"]


def test_a_failed_write_is_logged_as_failed_with_its_stage(client, monkeypatch):
    _get, post, tmp_path = client
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    from fm1_switch import guided, web

    def fake(unit_id, package, *, lock, on_phase, **kw):
        raise guided.WriteFailed("m-flasher timed out")
    monkeypatch.setattr(guided, "flash", fake)
    code, body = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                     "sha256": sha, "confirm": "aaaaaaaa00000001"})
    assert code == 409
    log = [json.loads(l) for l in (tmp_path / "flash.log").read_text().splitlines()]
    # Unreadable fake package, so not "ok": a partial write also leaves the unit unfinished.
    assert [e["event"] for e in log] == ["request", "failed", "setup-pending"]
    assert log[1]["stage"] == "write"
    assert web.APP.last_event["stage"] == "write"


def test_backup_before_write_trusts_files_not_the_exit_code(client, monkeypatch, tmp_path):
    from fm1_switch import web
    unit = "aaaaaaaa00000001"
    auto = tmp_path / "backups" / "auto"
    # exit 0 but nothing written
    monkeypatch.setattr(web.mflasher, "backup_unit", lambda node, d: None)
    with pytest.raises(web.mflasher.MflasherError, match="no matching"):
        web._backup_before_write("/dev/sg9", unit)
    # a record for the wrong unit
    monkeypatch.setattr(web.mflasher, "backup_unit",
                        lambda node, d: _backup(auto, "w", "aaaaaaaa00000002"))
    with pytest.raises(web.mflasher.MflasherError):
        web._backup_before_write("/dev/sg9", unit)
    # a record whose sha does not match the image
    monkeypatch.setattr(web.mflasher, "backup_unit",
                        lambda node, d: _backup(auto, "s", unit, sha="0" * 64))
    with pytest.raises(web.mflasher.MflasherError):
        web._backup_before_write("/dev/sg9", unit)
    # a good one
    monkeypatch.setattr(web.mflasher, "backup_unit",
                        lambda node, d: _backup(auto, "g", unit))
    web._backup_before_write("/dev/sg9", unit)


# ---- B8: every 409 leaves a line; a lost log line is reported (SAFE-05) ----------------

def _log(tmp_path):
    return [json.loads(l) for l in (tmp_path / "flash.log").read_text().splitlines()]


def test_pre_guided_refusals_are_logged_with_a_stage(client):
    _get, post, tmp_path = client
    (tmp_path / "lib" / "p.fwsc").write_bytes(b"x" * 100)
    post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc", "confirm": "no"})
    post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                        "sha256": "f" * 64, "confirm": "aaaaaaaa00000001"})
    post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "../x", "confirm": "z"})
    log = _log(tmp_path)
    assert [e["event"] for e in log] == ["refused"] * 3
    assert {e["stage"] for e in log} == {"request"}
    assert all(e["endpoint"] == "/api/flash" for e in log)
    assert "confirmation" in log[0]["message"] and "changed" in log[1]["message"]


def test_a_guided_refusal_is_logged_once_not_twice(client, monkeypatch):
    _get, post, tmp_path = client
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    code, _ = post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc",
                                  "sha256": sha, "confirm": "aaaaaaaa00000001"})
    assert code == 409
    refused = [e for e in _log(tmp_path) if e["event"] == "refused"]
    assert len(refused) == 1 and refused[0]["stage"] == "guided"


def test_restore_and_delete_refusals_are_logged(client):
    _get, post, tmp_path = client
    post("/api/restore", {"unit_id": "aaaaaaaa00000003", "backup": "nope.bin", "confirm": "x"})
    post("/api/backup/delete", {"unit_id": "aaaaaaaa00000003", "backup": "nope.bin", "confirm": "x"})
    log = _log(tmp_path)
    assert [e["endpoint"] for e in log] == ["/api/restore", "/api/backup/delete"]


def test_a_log_that_cannot_be_written_is_reported_by_the_fleet_endpoint(client, monkeypatch, tmp_path):
    get, post, _ = client
    from fm1_switch import web
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "no-such-dir" / "flash.log"))
    post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc", "confirm": "no"})
    assert "could not be written" in web.APP.log_error
    assert "could not be written" in get("/api/fleet")["log_error"]
    monkeypatch.setenv("FM1_FLASHLOG", str(tmp_path / "flash.log"))
    post("/api/flash", {"unit_id": "aaaaaaaa00000001", "package": "p.fwsc", "confirm": "no"})
    assert web.APP.log_error is None


# ---- CONC-01: nothing reads the devices while a write runs -----------------------------

def test_no_device_read_while_a_write_runs(client, monkeypatch, tmp_path):
    """Audit CONC-01 acceptance: a write blocks inside its writer, then the probe, the fleet
    (with an empty cache), a rename and a restart all arrive. ``mflasher.info`` must never
    run, and the registry must end with both the rename and the relearned fingerprint."""
    get, post, _ = client
    from fm1_switch import guided, mflasher, registry
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    reg = registry.Registry()
    reg.bind(U, "fm1-c")
    registry.save(reg)
    p = tmp_path / "lib" / "p.fwsc"
    p.write_bytes(b"x" * 100)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()

    infos = []
    monkeypatch.setattr(mflasher, "info", lambda dev: infos.append(dev) or (_ for _ in ()).throw(
        mflasher.MflasherError("no device")))
    monkeypatch.setattr(registry, "_download_devices", lambda: [("/dev/sg9", "1-1.1")])

    started, release = threading.Event(), threading.Event()

    def fake(unit_id, package, *, lock, on_phase, **kw):
        if not lock.acquire(blocking=False):
            raise guided.Busy("another flash is already running")
        try:
            on_phase("Writing…")
            started.set()
            release.wait(10)
            reg = registry.load()                    # the write's own re-learn
            with registry._io_lock:
                reg = registry.load()
                reg.binding_for_id(unit_id).fingerprint = "0322"
                registry.save(reg)
            return guided.Outcome(unit_id, "/dev/sg9", False, restarted=[unit_id],
                                  message="installed")
        finally:
            lock.release()
    monkeypatch.setattr(guided, "flash", fake)

    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=post("/api/flash", {"unit_id": U, "package": "p.fwsc", "sha256": sha, "confirm": U})))
    t.start()
    assert started.wait(5)
    try:
        web.APP.last_fleet = None
        fleet = get("/api/fleet")
        assert fleet["busy"] and fleet["units"] == []          # nothing cached, nothing read
        assert get("/api/units?probe=1") is not None
        assert post("/api/rename", {"unit_id": U, "name": "bass", "confirm": U})[0] == 200
        code, body = post("/api/restart", {"unit_id": U, "confirm": U})
        assert code == 409 and "running" in body["error"]
        # a second flash is refused and must not replace the running write's context
        code, _ = post("/api/flash", {"unit_id": U, "package": "p.fwsc", "sha256": sha,
                                      "confirm": U})
        assert code == 409
        assert (web.APP.last_event or {}).get("message") != "another flash is already running"
    finally:
        release.set()
        t.join(10)
    assert result["r"][0] == 200
    assert infos == []                                         # never read mid-write
    final = registry.load().binding_for_id(U)
    assert final.name == "BASS" and final.fingerprint == "0322"


def test_restart_shows_a_busy_phase_and_clears_it(client, monkeypatch):
    _get, post, _ = client
    from fm1_switch import guided, web
    seen = []

    def fake(unit_id, *, lock, on_phase=None):
        on_phase("Restarting…")
        seen.append(web.APP.busy)
        return guided.Outcome(unit_id, "/dev/sg9", True, [unit_id], [], "restarted")
    monkeypatch.setattr(guided, "restart_unit", fake)
    assert post("/api/restart", {"unit_id": U, "confirm": U})[0] == 200
    assert seen == ["Restarting…"] and web.APP.busy is None


def test_reconcile_needs_its_own_confirm_and_shows_a_busy_phase(client, monkeypatch):
    _get, post, _ = client
    from fm1_switch import guided, web
    code, body = post("/api/reconcile", {"confirm": "yes"})
    assert code == 409 and "confirmation" in body["error"]
    seen = []

    def fake(*, lock, everything=False, on_phase=None):
        assert everything is True
        on_phase("Identifying units…")
        seen.append(web.APP.busy)
        return guided.ReconcileOutcome(matched=[("fm1-a", U, "1-2.1")], message="matched A")
    monkeypatch.setattr(guided, "reconcile", fake)
    code, body = post("/api/reconcile", {"confirm": "reconcile", "all": True})
    assert code == 200 and body["message"] == "matched A"
    assert body["matched"] == [{"slot": "fm1-a", "unit_id": U, "port": "1-2.1"}]
    assert seen == ["Identifying units…"] and web.APP.busy is None


def test_inspect_cache_survives_concurrent_puts(tmp_path):
    cache = packages._Cache()
    cache.path = tmp_path / "inspect.json"
    files = []
    for i in range(8):
        f = tmp_path / f"f{i}.fwsc"
        f.write_bytes(bytes([i]) * 10)
        files.append(f)
    errors = []

    def put(f):
        try:
            for _ in range(20):
                cache.put(packages.Inspection(path=f, severity="ok", headline="h",
                                              details=[], package=None))
                cache.get(f)
        except Exception as exc:       # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=put, args=(f,)) for f in files]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert len(json.loads(cache.path.read_text())) == 8
    assert not list(tmp_path.glob("*.tmp"))


# ---- B10: hub terms and network discipline ----------------------------------------------

def _stale_cache(tmp_path, age_s):
    f = tmp_path / "catalog.json"
    f.write_text(json.dumps({"firmwares": [{"id": "a", "name": "A"}]}))
    t = __import__("time").time() - age_s
    os.utime(f, (t, t))


def test_forced_refresh_is_ignored_while_the_cache_is_under_an_hour_old(tmp_path, monkeypatch):
    """HUB-02: two refreshes in a row make at most one fetch."""
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path))
    _stale_cache(tmp_path, 120)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("offline")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    cat = hub.load(force=True)
    assert cat.entries[0].id == "a" and calls == []
    _stale_cache(tmp_path, 2 * 3600)          # old enough: the force is honoured
    hub.load(force=True)
    assert len(calls) == 1


def test_a_package_check_never_reaches_the_network(tmp_path, monkeypatch):
    """HUB-01: compat for a new-chip unit reads the cache only, even when it is stale."""
    from fm1_switch import compat
    monkeypatch.setenv("FM1_HUB_CACHE", str(tmp_path))
    _stale_cache(tmp_path, 3 * 86400)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    pkg = tmp_path / "p.fwsc"
    pkg.write_bytes(b"x" * 10)
    verdict = compat.compat_for(pkg, "0xB36014")[0]
    assert verdict is not None


def test_getlist_carries_author_repo_and_attribution(client, monkeypatch):
    get, _post, _ = client
    out = get("/api/getlist")
    assert "attribution" in out and "unavailable" in out
    for row in out["items"]:
        assert "author" in row and "repo" in row


def test_library_digest_is_remembered_until_the_file_changes(tmp_path, monkeypatch):
    from fm1_switch import library
    f = tmp_path / "p.fwsc"
    f.write_bytes(b"abc")
    calls = []
    real = library._hash_file
    monkeypatch.setattr(library, "_hash_file", lambda p: calls.append(1) or real(p))
    a = library._digest(f)
    assert library._digest(f) == a and len(calls) == 1
    f.write_bytes(b"abcd")
    assert library._digest(f) != a and len(calls) == 2
