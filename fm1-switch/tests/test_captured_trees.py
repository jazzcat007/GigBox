"""The registry's sysfs reading, replayed on trees captured from the bench Pi 2026-11-25."""

from pathlib import Path

from captured_trees import build
from fm1_switch import mflasher, registry, softkey, units


def _point(monkeypatch, paths):
    monkeypatch.setattr(registry, "_USB_DEV_ROOT", paths["usb"])
    monkeypatch.setattr(registry, "_SCSI_GENERIC", paths["sg"])
    monkeypatch.setattr(softkey, "USB_SYSFS", paths["usb"])


def test_all_running_lists_the_four_units_and_not_the_hub_or_lan_chip(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=False))
    got = {r.port: (r.product, r.fingerprint) for r in registry.running()}
    assert got == {"1-1.1.2": ("Felucca", "0320"), "1-1.1.3": ("Felucca", "0310"),
                   "1-1.2": ("FM-1", "0420"), "1-1.3": ("Felucca", "0328")}


def test_the_captured_sg0_chain_resolves_to_unit_as_port(tmp_path, monkeypatch):
    _point(monkeypatch, build(tmp_path, download=True))
    assert registry.port_of_sg("/dev/sg0") == "1-1.1.2"
    assert softkey.port_ids("1-1.1.2") in softkey.DOWNLOAD_IDS
    assert softkey.port_ids("1-1.1.3") == "1209:0001"      # the others did not move


def test_scan_finds_the_download_unit_and_running_ones_still_count(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=True))
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    monkeypatch.setattr(mflasher, "info", lambda dev: mflasher.Identity(
        flash_chip="0x856014", unit_id="aaaaaaaa00000001", firmware="FM-1 966"))
    rows = registry.known()
    dl = [u for u in rows if u.present]
    assert [(u.unit_id, u.port, u.device) for u in dl] == [("aaaaaaaa00000001", "1-1.1.2", "/dev/sg0")]
    # The three running units are still listed (as '?', no id can be read from them); the
    # download-mode unit is NOT also listed as a phantom running row (bug found by this replay).
    assert sorted(u.port for u in rows if u.running) == ["1-1.1.3", "1-1.2", "1-1.3"]
    assert len(rows) == 4


CAPTURES = Path(__file__).parent / "captures"


def test_new_chip_info_capture_is_read_as_new_chip():
    """The B15(b) capture: `m-flasher info` on fm1-d, flash chip 0xB36014, read on the Pi
    2026-11-25. The unit id is replaced by aaaaaaaa00000004; nothing else is edited."""
    text = (CAPTURES / "info-fm1-d.txt").read_text(encoding="utf-8")
    ident = mflasher.parse_info(text)
    assert ident.flash_chip == "0xB36014"
    assert ident.firmware == "SLOOP 2.8.5"
    assert ident.unit_id == "aaaaaaaa00000004"
    assert ident.model == "FM-1"
    assert "(new)" in text


def test_a_running_unit_is_matched_by_its_usb_serial_when_the_fingerprint_is_stale(tmp_path, monkeypatch):
    """Unit C under JIANT (bench 2026-11-25): serial == flash id, fingerprint 0100 while the
    registry holds 0420. It must show as C running, not 'C off' plus a phantom '?'."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    paths = build(tmp_path, download=False)
    _point(monkeypatch, paths)
    dev = Path(paths["usb"]) / "1-1.2"
    (dev / "bcdDevice").write_text("0100\n")
    (dev / "serial").write_text("1111aaaa00000003\n")
    reg = registry.load()
    reg.bind("1111aaaa00000003", "fm1-c", port="1-1.2", chip="0x856014", firmware="jiant",
             fingerprint="0420")
    registry.save(reg)
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    rows = {u.slot: u for u in registry.known() if u.slot}
    assert rows["fm1-c"].running and rows["fm1-c"].unit_id == "1111aaaa00000003"
    assert not any(u.unit_id == "port:1-1.2" for u in registry.known())


def test_running_slots_reads_sysfs_only_and_never_asks_a_unit_for_its_id(tmp_path, monkeypatch):
    """The router service calls this every few seconds, even mid-flash of another unit, so it
    must not run m-flasher on a download-mode unit (registry.known() does)."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    paths = build(tmp_path, download=True)
    _point(monkeypatch, paths)

    def boom(*a, **k):
        raise AssertionError("running_slots touched a unit")
    monkeypatch.setattr(mflasher, "info", boom)
    monkeypatch.setattr(mflasher, "run", boom)
    reg = registry.load()
    reg.bind("aaaaaaaa00000002", "fm1-b", port="1-1.1.3", fingerprint="0310")
    reg.bind("aaaaaaaa00000003", "fm1-c", port="1-1.2", fingerprint="0420")
    registry.save(reg)
    # the unit on 1-1.1.2 is in download mode and is not listed as running
    assert registry.running_slots() == {"fm1-b": "1-1.1.3", "fm1-c": "1-1.2"}


def test_download_sends_the_soft_key_to_the_live_port_not_the_saved_map(tmp_path, monkeypatch, capsys):
    """Bench 2026-11-25: the saved map still named 1-1.1.3 for fm1-b after the move to the
    hub, where 1-1.1.3 is the hub itself. The soft key must go to the port the bus shows the
    unit on (1-1.1.3 in the capture, found by fingerprint), never to the stale map's port."""
    from types import SimpleNamespace
    from fm1_switch import __main__ as cli

    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "map.txt"))
    (tmp_path / "map.txt").write_text("fm1-b\t1-9.9\n", encoding="utf-8")
    _point(monkeypatch, build(tmp_path, download=False))
    reg = registry.load()
    reg.bind("aaaaaaaa00000002", "fm1-b", port="1-9.9", fingerprint="0310")
    registry.save(reg)
    monkeypatch.setattr(softkey, "port_ids", lambda port: "1209:0001")
    sent = []
    monkeypatch.setattr(softkey, "soft_key",
                        lambda port, **k: sent.append(port) or SimpleNamespace(midi_node="/dev/midiC0D0"))
    monkeypatch.setattr(softkey, "wait_for_download_mode", lambda port, **k: True)

    assert cli.main(["download", "fm1-b"]) == 0
    assert sent == ["1-1.1.3"]


def test_live_ports_places_a_unit_already_in_download_mode_by_its_flash_id(tmp_path, monkeypatch):
    """The unit on 1-1.1.2 is in download mode: it is not running, so only its flash id can
    place it. live_ports() must find it on the bus, with no saved map at all."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=True))
    reg = registry.load()
    reg.bind("aaaaaaaa00000001", "fm1-a", port="1-9.9", fingerprint="0320")
    registry.save(reg)
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    monkeypatch.setattr(mflasher, "info", lambda dev: mflasher.Identity(
        flash_chip="0x856014", unit_id="aaaaaaaa00000001", firmware="FM-1 966"))

    assert registry.live_ports().get("fm1-a") == "1-1.1.2"


# --------------------------------------------------- identity cache (the 2 s UI poll)

def _counting_info(monkeypatch, *, fail_first=False):
    """Stub ``m-flasher info``; return the list of devices it was asked about."""
    calls = []

    def info(dev):
        calls.append(dev)
        if fail_first and len(calls) == 1:
            raise mflasher.MflasherError("transient read error")
        return mflasher.Identity(flash_chip="0x856014", unit_id="aaaaaaaa00000001",
                                 firmware="FM-1 966")
    monkeypatch.setattr(mflasher, "info", info)
    return calls


def test_a_unit_in_download_mode_is_read_once_not_on_every_poll(tmp_path, monkeypatch):
    """Bench 2026-11-25: the UI's 2 s poll ran `m-flasher info` (a whole flash read) every
    time while fm1-a sat in download mode. Repeated scans must reuse the first read."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=True))
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    calls = _counting_info(monkeypatch)

    for _ in range(5):
        rows = registry.scan()
    assert len(calls) == 1
    assert [(u.unit_id, u.port) for u in rows] == [("aaaaaaaa00000001", "1-1.1.2")]


def test_a_new_enumeration_on_the_same_port_is_read_again(tmp_path, monkeypatch):
    """A restart or a different unit gets a new USB device number, so it must be re-read."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    paths = build(tmp_path, download=True)
    _point(monkeypatch, paths)
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    calls = _counting_info(monkeypatch)
    dev = Path(paths["usb"]) / "1-1.1.2"

    (dev / "devnum").write_text("41\n")
    registry.scan()
    registry.scan()
    assert len(calls) == 1
    (dev / "devnum").write_text("42\n")       # re-enumerated
    registry.scan()
    assert len(calls) == 2


def test_a_failed_read_is_not_cached_and_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=True))
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    calls = _counting_info(monkeypatch, fail_first=True)

    first = registry.scan()
    assert first[0].error and first[0].unit_id.startswith("device:")
    second = registry.scan()
    assert len(calls) == 2
    assert second[0].unit_id == "aaaaaaaa00000001" and second[0].error is None


def test_forget_identities_forces_a_fresh_read_after_a_flash(tmp_path, monkeypatch):
    """A flash changes what a unit reports while it stays on the same port, so the flash and
    restart paths clear the cache."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    _point(monkeypatch, build(tmp_path, download=True))
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    calls = _counting_info(monkeypatch)

    registry.scan()
    registry.forget_identities()
    registry.scan()
    assert len(calls) == 2
