"""registry.relearn_after_flash: a flash changes bcdDevice; the registry must follow it."""

from fm1_switch import registry
from fm1_switch.registry import RunningUnit

A, B = "aaaaaaaa00000001", "aaaaaaaa00000002"


def setup(tmp_path, monkeypatch, running, extra_fp=None):
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    reg = registry.Registry()
    reg.bind(A, "fm1-a", port="1-1.1.2", firmware="Felucca", fingerprint="0329")
    reg.bind(B, "fm1-b", port="1-1.1.3", firmware="x", fingerprint=extra_fp or "0320")
    registry.save(reg)
    monkeypatch.setattr(registry, "running", lambda: running)


def run_at(port, fp):
    return RunningUnit(port=port, vendor_product="1209:0001", product="ChoralRoot FM-1",
                       fingerprint=fp)


def test_updates_fingerprint_port_and_firmware(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [run_at("1-1.1.2", "0322")])
    assert registry.relearn_after_flash(A, "1-1.1.2", "choralroot-0.42") == "ok"
    b = registry.load().binding_for_id(A)
    assert (b.fingerprint, b.firmware) == ("0322", "choralroot-0.42")


def test_unbound_id_is_left_alone(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [run_at("1-1.1.2", "0322")])
    assert registry.relearn_after_flash("ffffffffffffffff", "1-1.1.2") == "unbound"


def test_nothing_on_the_port_changes_nothing(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [])
    assert registry.relearn_after_flash(A, "1-1.1.2") == "not-running"
    assert registry.load().binding_for_id(A).fingerprint == "0329"


def test_shared_fingerprint_is_stored_and_reported_as_shared(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [run_at("1-1.1.2", "0320")])     # B already wears 0320
    assert registry.relearn_after_flash(A, "1-1.1.2") == "shared"
    assert registry.load().binding_for_id(A).fingerprint == "0320"


def bind_two_same_firmware(tmp_path, monkeypatch, runs):
    """A (port 1-1.1.2) and C (port 1-1.2) both run ChoralRoot: both fingerprint 0322."""
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    reg = registry.Registry()
    reg.bind(A, "fm1-a", port="1-1.1.2", firmware="choralroot", fingerprint="0322")
    reg.bind("aaaaaaaa00000003", "fm1-c", port="1-1.2", firmware="choralroot", fingerprint="0322")
    registry.save(reg)
    monkeypatch.setattr(registry, "running", lambda: runs)
    monkeypatch.setattr(registry, "scan", lambda: [])


def test_two_units_sharing_a_fingerprint_are_told_apart_by_port(tmp_path, monkeypatch):
    bind_two_same_firmware(tmp_path, monkeypatch,
                           [run_at("1-1.1.2", "0322"), run_at("1-1.2", "0322")])
    rows = [u for u in registry.known() if u.running]
    assert sorted((u.slot, u.port) for u in rows) == [("fm1-a", "1-1.1.2"), ("fm1-c", "1-1.2")]


def test_a_sticker_is_never_shown_on_two_units(tmp_path, monkeypatch):
    # both units were moved to jacks neither was last seen on: nothing to break the tie
    bind_two_same_firmware(tmp_path, monkeypatch,
                           [run_at("1-1.7", "0322"), run_at("1-1.8", "0322")])
    rows = [u for u in registry.known() if u.running]
    # unlabelled, not guessed -- but still counted: two "?" rows, never a sticker (REG-01)
    assert sorted((u.label, u.unit_id) for u in rows) == [("?", "port:1-1.7"), ("?", "port:1-1.8")]
    slots = [u.slot for u in registry.known()]
    assert slots.count("fm1-a") <= 1 and slots.count("fm1-c") <= 1


def test_one_physical_unit_with_a_unique_fingerprint_still_matches(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [run_at("1-1.9", "0329")])
    monkeypatch.setattr(registry, "scan", lambda: [])
    rows = [u for u in registry.known() if u.running]
    assert [(u.slot, u.port) for u in rows] == [("fm1-a", "1-1.9")]


def test_a_port_tie_break_is_marked_as_a_hint(tmp_path, monkeypatch):
    bind_two_same_firmware(tmp_path, monkeypatch,
                           [run_at("1-1.1.2", "0322"), run_at("1-1.2", "0322")])
    assert {u.hint for u in registry.known() if u.running} == {"port"}


def test_a_pid_codes_gadget_is_not_listed_as_an_fm1(tmp_path, monkeypatch):
    dev = tmp_path / "1-1.5"
    dev.mkdir()
    for name, val in (("idVendor", "1209"), ("idProduct", "5bf0"), ("bcdDevice", "0100"),
                      ("product", "Some Open Gadget")):
        (dev / name).write_text(val)
    good = tmp_path / "1-1.6"
    good.mkdir()
    for name, val in (("idVendor", "1209"), ("idProduct", "0001"), ("bcdDevice", "0322"),
                      ("product", "Felucca")):
        (good / name).write_text(val)
    monkeypatch.setattr(registry, "_USB_DEV_ROOT", str(tmp_path))
    monkeypatch.setattr(registry, "load", lambda: registry.Registry())
    assert [r.port for r in registry.running()] == ["1-1.6"]


def test_a_download_unit_on_an_unmapped_port_is_found_through_sysfs(tmp_path, monkeypatch):
    """ARCH-02: no /dev/fm1-* symlink exists for this unit, yet scan() reads its id."""
    from fm1_switch import mflasher, softkey, units
    sg = tmp_path / "scsi_generic"
    sg.mkdir()
    (sg / "sg3").write_text("")
    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setattr(registry, "_SCSI_GENERIC", str(sg))
    monkeypatch.setattr(registry, "port_of_sg", lambda dev: "1-1.4")
    monkeypatch.setattr(softkey, "port_ids", lambda port: "4c4a:8057")
    monkeypatch.setattr(units, "enumerate_units", lambda *a, **k: [])
    monkeypatch.setattr(mflasher, "info", lambda dev: mflasher.Identity(
        flash_chip="0xB36014", unit_id=A, firmware="FM-1 901"))
    monkeypatch.setattr(registry, "running", lambda: [])
    rows = registry.known()
    assert [(u.unit_id, u.port, u.device, u.present) for u in rows] == [(A, "1-1.4", "/dev/sg3", True)]
