"""Tests for the port-addressed soft key (``softkey.py``) and the port map in ``units.py``.

The point of these is the failure modes that matter on a real bench: sending to the wrong
unit, sending to nothing and calling it success, and confusing "no MIDI" (power-cycle it)
with "not there" (check the cable). None of them may pass silently.
"""

from __future__ import annotations

import os
import pathlib
from pathlib import Path

import pytest

from captured_trees import add_sound_cards, build, build_snd
from fm1_switch import softkey, units


# --------------------------------------------------------------------------- midi_node_for_port

@pytest.fixture
def fake_snd(tmp_path, monkeypatch):
    """A fake /dev/snd tree with by-path symlinks like the Pi's."""
    snd = tmp_path / "snd"
    (snd / "by-path").mkdir(parents=True)
    monkeypatch.setattr(softkey, "SND_DIR", snd)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    monkeypatch.setattr(softkey, "USB_SYSFS", str(sysfs))

    def add(port: str, card: int, *, midi: bool = True) -> None:
        bus, _, rest = port.partition("-")
        name = f"platform-3f980000.usb-usb-{bus}:{rest}:1.0"
        link = snd / "by-path" / name
        os.symlink(f"../controlC{card}", link)
        (snd / f"controlC{card}").touch()
        if midi:
            (snd / f"midiC{card}D0").touch()
        # sysfs is the authority for which card number belongs to this port
        iface = sysfs / port / f"{port}:1.0"
        (iface / "sound" / f"card{card}").mkdir(parents=True)

    return snd, add


def test_resolves_port_to_midi_node(fake_snd):
    snd, add = fake_snd
    add("1-1.1.2", 3)
    assert softkey.midi_node_for_port("1-1.1.2") == snd / "midiC3D0"


def test_different_ports_resolve_to_different_nodes(fake_snd):
    snd, add = fake_snd
    add("1-1.1.2", 3)
    add("1-1.1.3", 4)
    add("1-1.2", 5)
    add("1-1.3", 2)
    nodes = {p: softkey.midi_node_for_port(p) for p in
             ("1-1.1.2", "1-1.1.3", "1-1.2", "1-1.3")}
    assert len(set(nodes.values())) == 4, nodes
    # a prefix must not shadow a longer path: 1-1.1 must not match 1-1.1.2's link
    assert nodes["1-1.1.2"] != nodes["1-1.1.3"]


def test_usbv2_alias_is_ignored(fake_snd):
    snd, add = fake_snd
    add("1-1.1.2", 3)
    os.symlink("../controlC3", snd / "by-path" / "platform-usbv2-0:1.1.2:1.0")
    # still resolves to the single real node, and only once
    assert softkey.midi_node_for_port("1-1.1.2") == snd / "midiC3D0"


def test_no_midi_node_returns_none(fake_snd):
    """The stock-V15 case: by-path exists, but there is no raw MIDI device."""
    snd, add = fake_snd
    add("1-1.2", 5, midi=False)
    assert softkey.midi_node_for_port("1-1.2") is None


def test_unknown_port_returns_none(fake_snd):
    fake_snd
    assert softkey.midi_node_for_port("1-9.9") is None


def test_rejects_non_port_strings():
    for bad in ("", "fm1-a", "1"):
        with pytest.raises(ValueError):
            softkey.midi_node_for_port(bad)


# --------------------------------------------------------------------------- soft_key behaviour

def test_soft_key_raises_when_port_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(softkey, "port_ids", lambda port: None)
    with pytest.raises(softkey.NoSuchPort):
        softkey.soft_key("1-1.1.2")


def test_soft_key_raises_no_midi_for_audio_only_unit(fake_snd, monkeypatch):
    snd, add = fake_snd
    add("1-1.2", 5, midi=False)
    monkeypatch.setattr(softkey, "port_ids", lambda port: "4d4a:4155")
    with pytest.raises(softkey.NoMidiInterface) as exc:
        softkey.soft_key("1-1.2")
    # the message must tell the operator what to DO, not just what failed
    assert "power-cycle" in str(exc.value)
    assert "4d4a:4155" in str(exc.value)


def test_soft_key_writes_exact_sysex_to_the_named_port(fake_snd, monkeypatch):
    snd, add = fake_snd
    add("1-1.1.2", 3)
    add("1-1.1.3", 4)
    monkeypatch.setattr(softkey, "port_ids", lambda port: "1209:0001")

    res = softkey.soft_key("1-1.1.3")

    assert res.midi_node == str(snd / "midiC4D0")
    assert (snd / "midiC4D0").read_bytes() == bytes([0xF0, 0x22, 0x24, 0x35, 0x7D, 0xF7])
    # the OTHER unit must not have been touched -- this is the whole safety property
    assert (snd / "midiC3D0").read_bytes() == b""


def test_soft_key_sysex_constant_is_the_documented_one():
    assert softkey.SYSEX_SOFT_KEY == bytes.fromhex("F022243 57DF7".replace(" ", ""))


# --------------------------------------------------------------------------- download-mode wait

def test_wait_returns_true_on_download_id(monkeypatch):
    monkeypatch.setattr(softkey, "port_ids", lambda port: "4c4a:8057")
    assert softkey.wait_for_download_mode("1-1.1.2", timeout=1.0) is True


def test_wait_returns_false_when_unit_stays_running(monkeypatch):
    monkeypatch.setattr(softkey, "port_ids", lambda port: "1209:0001")
    assert softkey.wait_for_download_mode("1-1.1.2", timeout=0.2, interval=0.05) is False


def test_wait_notices_a_late_transition(monkeypatch):
    """The unit must be allowed to take a moment; a single immediate check would flake."""
    seen = iter(["1209:0001", "1209:0001", "4c4a:8057"])
    monkeypatch.setattr(softkey, "port_ids", lambda port: next(seen, "4c4a:8057"))
    assert softkey.wait_for_download_mode("1-1.1.2", timeout=2.0, interval=0.01) is True


# --------------------------------------------------------------------------- port map

def test_port_map_round_trip(tmp_path, monkeypatch):
    f = tmp_path / "map.txt"
    f.write_text("fm1-a\t1-1.1.2\nfm1-b\t1-1.1.3\n\nfm1-c\t1-1.2\n", encoding="utf-8")
    monkeypatch.setenv("FM1_PORTMAP", str(f))
    assert units.read_port_map() == {
        "fm1-a": "1-1.1.2", "fm1-b": "1-1.1.3", "fm1-c": "1-1.2"}
    assert units.port_for_slot("fm1-d") is None


def test_port_map_missing_file_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_PORTMAP", str(tmp_path / "nope.txt"))
    assert units.read_port_map() == {}


def test_port_map_ignores_junk_and_unknown_slots(tmp_path, monkeypatch):
    f = tmp_path / "map.txt"
    f.write_text("# comment\nfm1-a 1-1.1.2\nbogus 1-1.9\nfm1-b\nfm1-c 1-1.2 extra\n",
                 encoding="utf-8")
    monkeypatch.setenv("FM1_PORTMAP", str(f))
    assert units.read_port_map() == {"fm1-a": "1-1.1.2", "fm1-c": "1-1.2"}


# --------------------------------------------------------------- stale by-path (hardware bug)

def test_stale_by_path_link_is_not_trusted(fake_snd):
    """Reproduces the real bug found on hardware 2026-11-21.

    When the unit on 1-1.1.2 left USB for download mode, its ALSA card was freed but that
    port's by-path symlink survived, still naming controlC4 -- a number the kernel had
    meanwhile issued to the DIFFERENT unit on 1-1.1.3. A resolver that trusts by-path would
    have sent the soft key to the wrong unit.

    sysfs must win: port 1-1.1.3 has card 4, so it resolves to midiC4D0; the stale link on
    port 1-1.1.2 (which sysfs says has no card) must resolve to nothing.
    """
    snd, add = fake_snd
    add("1-1.1.3", 4)                       # the real owner of card 4
    os.symlink("../controlC4", snd / "by-path" / "platform-3f980000.usb-usb-0:1.1.2:1.0")
    (pathlib.Path(softkey.USB_SYSFS) / "1-1.1.2").mkdir(parents=True, exist_ok=True)

    assert softkey.midi_node_for_port("1-1.1.3") == snd / "midiC4D0"
    assert softkey.midi_node_for_port("1-1.1.2") is None


# ------------------------------------------------------------------- the CLI actually runs

def test_every_cli_command_is_reachable(monkeypatch, capsys):
    """A broken ``main()`` passed the whole suite twice before this test existed.

    Executing each command's entry path (with the hardware calls stubbed) catches naming
    and import errors that unit tests on the leaf functions cannot see.
    """
    from fm1_switch import __main__ as cli

    for verb in ("download", "list", "inspect"):
        monkeypatch.setattr(cli, "cmd_" + verb, lambda *a, v=verb: (print(v), 0)[1])
    for verb in ("download", "list", "inspect"):
        assert cli.main([verb]) == 0


def test_download_with_no_unit_on_the_bus_sends_nothing(monkeypatch, tmp_path, capsys):
    """A saved map that names a port is not evidence a unit is there: with an empty bus the
    command must refuse, and must not send a soft key to the port the map names."""
    from fm1_switch import __main__ as cli, registry

    monkeypatch.setenv("FM1_UNITS", str(tmp_path / "units.json"))
    monkeypatch.setenv("FM1_PORTMAP", str(_map(tmp_path, {"fm1-a": "1-1.1.2"})))
    monkeypatch.setattr(registry, "_USB_DEV_ROOT", str(tmp_path / "usb"))
    monkeypatch.setattr(registry, "_SCSI_GENERIC", str(tmp_path / "sg"))
    (tmp_path / "usb").mkdir()
    (tmp_path / "sg").mkdir()
    sent = []
    monkeypatch.setattr(softkey, "soft_key", lambda port, **k: sent.append(port))

    assert cli.main(["download", "fm1-a"]) == 1
    assert sent == []
    assert "not on any USB port the registry can place" in capsys.readouterr().err


def test_download_rejects_unknown_slot(capsys):
    from fm1_switch import __main__ as cli
    assert cli.main(["download", "fm1-z"]) == 2


def _map(tmp_path, mapping):
    f = tmp_path / "map.txt"
    f.write_text("".join(f"{k}\t{v}\n" for k, v in mapping.items()), encoding="utf-8")
    return f


# --- real bench captures (B15): the soft key replayed against what the Pi actually reported
#     on 2026-11-25, fm1-d on port 1-1.3 (see tests/captures/README.md). The synthetic
#     `fake_snd` tests above remain only for edge cases no capture shows (a stale link, a port
#     with no node).

SYSEX = bytes([0xF0, 0x22, 0x24, 0x35, 0x7D, 0xF7])


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """The bench Pi's USB tree and /dev/snd, all four units running, as captured."""
    usb = build(tmp_path, download=False, usb_capture="sysfs-before-softkey-fm1-d.raw")["usb"]
    snd = Path(build_snd(tmp_path, "snd-before-softkey-fm1-d.raw"))
    add_sound_cards(Path(usb), snd)
    monkeypatch.setattr(softkey, "USB_SYSFS", usb)
    monkeypatch.setattr(softkey, "SND_DIR", snd)
    return tmp_path


def test_bench_ports_resolve_to_the_midi_node_the_capture_shows(bench):
    # by-path links in the capture: 1-1.3 -> controlC0, 1-1.1.3 -> controlC2, 1-1.2 -> controlC1,
    # 1-1.1.2 -> controlC3. Each card's raw MIDI node is midiC<card>D0.
    want = {"1-1.3": "midiC0D0", "1-1.2": "midiC1D0", "1-1.1.3": "midiC2D0", "1-1.1.2": "midiC3D0"}
    for port, node in want.items():
        assert softkey.midi_node_for_port(port) == softkey.SND_DIR / node, port


def test_soft_key_on_bench_writes_only_to_the_addressed_port(bench):
    softkey.soft_key("1-1.3")
    assert (softkey.SND_DIR / "midiC0D0").read_bytes() == SYSEX
    for other in ("midiC1D0", "midiC2D0", "midiC3D0"):
        assert (softkey.SND_DIR / other).read_bytes() == b"", other


def test_soft_key_on_an_empty_port_sends_nothing(bench):
    with pytest.raises(softkey.NoSuchPort):
        softkey.soft_key("1-1.9")
    assert all((softkey.SND_DIR / n).read_bytes() == b"" for n in ("midiC0D0", "midiC1D0"))


def test_real_reply_moved_only_fm1_d_into_rom_loader(bench, monkeypatch):
    # The capture after `fm1-switch download fm1-d`: 1-1.3 reports 4c4a:8057 (WL80UBOOT1.00),
    # the other three ports still report their running ids. The reply is a re-enumeration,
    # not a MIDI message, so this is what "in download mode" looks like to the code.
    (bench / "after").mkdir()
    usb = build(bench / "after", download=False, usb_capture="sysfs-after-softkey-fm1-d.raw")["usb"]
    monkeypatch.setattr(softkey, "USB_SYSFS", usb)
    assert softkey.port_ids("1-1.3") == "4c4a:8057"
    assert softkey.port_ids("1-1.2") == "1209:0001"
    assert softkey.port_ids("1-1.1.2") == "1209:0001"
    assert softkey.port_ids("1-1.1.3") == "1209:0001"
    assert softkey.wait_for_download_mode("1-1.3", timeout=0.2, interval=0.05)
    assert not softkey.wait_for_download_mode("1-1.2", timeout=0.2, interval=0.05)
