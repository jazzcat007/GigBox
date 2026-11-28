"""The WiFi fallback: the rules, the nmcli wrapper and the loop, against a fake nmcli.
No test touches a network or runs a real nmcli."""

from __future__ import annotations

import json
import threading

import pytest

from fm1_switch import netctl, netwatch
from fm1_switch.netwatch import HOTSPOT_UP, TRY_HOME, Fallback, Obs, Policy

NONE = Obs(present=True, client=False, hotspot=False, phones=None)
HOME = Obs(present=True, client=True, hotspot=False, phones=None)


def hot(phones=0):
    return Obs(present=True, client=False, hotspot=True, phones=phones)


# ------------------------------------------------------------------------------ the rules

def test_boot_with_no_network_raises_the_hotspot_only_after_the_wait():
    fb = Fallback(Policy(after=60))
    assert fb.step(1000, NONE) is None
    assert fb.step(1059, NONE) is None
    assert fb.step(1060, NONE) == HOTSPOT_UP


def test_a_network_that_connects_in_time_means_no_hotspot():
    fb = Fallback(Policy(after=60))
    fb.step(1000, NONE)
    assert fb.step(1030, HOME) is None
    assert fb.step(1100, HOME) is None


def test_boot_and_loss_rules_are_separate_switches():
    boot_off = Fallback(Policy(after=10, at_boot=False))
    boot_off.step(0, NONE)
    assert boot_off.step(500, NONE) is None  # never connected, boot fallback off
    loss_off = Fallback(Policy(after=10, on_loss=False))
    loss_off.step(0, HOME)
    loss_off.step(1, NONE)
    assert loss_off.step(500, NONE) is None  # was connected, loss fallback off
    on = Fallback(Policy(after=10))
    on.step(0, HOME)
    on.step(1, NONE)
    assert on.step(11, NONE) == HOTSPOT_UP


def test_a_blip_shorter_than_the_wait_does_not_raise_the_hotspot():
    fb = Fallback(Policy(after=60))
    fb.step(0, HOME)
    fb.step(10, NONE)
    fb.step(20, HOME)  # back
    assert fb.step(75, NONE) is None  # the clock started again at 75, not 10


def test_home_mode_never_raises_and_field_mode_always_does():
    home = Fallback(Policy(mode="home", after=1))
    home.step(0, NONE)
    assert home.step(1000, NONE) is None
    field = Fallback(Policy(mode="field"))
    assert field.step(1000, NONE) == HOTSPOT_UP
    assert field.step(1000, HOME) == HOTSPOT_UP  # even over a joined network
    assert field.step(1000, hot()) is None


def test_it_goes_back_to_a_saved_network_only_when_nobody_is_joined():
    fb = Fallback(Policy(after=10, retry_every=300))
    fb.step(0, NONE)
    assert fb.step(10, NONE) == HOTSPOT_UP
    fb.hotspot_raised(10, True)
    assert fb.step(100, hot(0)) is None        # too early
    assert fb.step(310, hot(2)) is None        # time is up but two phones are on it
    assert fb.step(311, hot(None)) is None     # cannot tell: stay
    assert fb.step(312, hot(0)) == TRY_HOME


def test_the_guard_can_be_turned_off_and_retry_can_be_never():
    fb = Fallback(Policy(retry_every=60, only_when_empty=False))
    fb.step(0, hot(3))
    assert fb.step(61, hot(3)) == TRY_HOME
    never = Fallback(Policy(retry_every=0))
    never.step(0, hot(0))
    assert never.step(10**6, hot(0)) is None


def test_failed_tries_back_off_and_are_capped_and_a_join_resets():
    fb = Fallback(Policy(retry_every=300))
    fb.step(0, hot(0))
    gaps = []
    t = 300.0
    for _ in range(6):
        assert fb.step(t, hot(0)) == TRY_HOME
        fb.home_tried(t, False)
        gaps.append(fb.next_try - t)
        t = fb.next_try
    assert gaps == [600, 1200, 2400, 3600, 3600, 3600]
    fb.home_tried(t, True)
    assert fb.fails == 0 and fb.fallback is False


def test_a_hotspot_found_up_at_start_is_looked_after():
    fb = Fallback(Policy(retry_every=300))
    assert fb.step(0, hot(0)) is None
    assert fb.fallback is True
    assert fb.step(300, hot(0)) == TRY_HOME


def test_a_missing_adapter_does_nothing():
    fb = Fallback(Policy(after=1))
    gone = Obs(present=False, client=False, hotspot=False, phones=None)
    assert fb.step(0, gone) is None and fb.step(100, gone) is None


# --------------------------------------------------------------------------------- netctl

class FakeNm:
    """Just enough nmcli: devices, saved profiles, and the up/down/add/modify calls."""

    def __init__(self, *, saved=("Home",), in_range=("Home",), state="disconnected",
                 conn="", has_hotspot=False):
        self.saved, self.in_range = list(saved), set(in_range)
        self.state, self.conn, self.has_hotspot = state, conn, has_hotspot
        self.calls: list[list[str]] = []
        self.fail_hotspot = False

    def __call__(self, args):
        self.calls.append(args)
        a = [x for x in args if x != "-t"]
        if a[:3] == ["-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return 0, f"wlan0:wifi:{self.state}:{self.conn}\neth0:ethernet:unavailable:\n"
        if a[:3] == ["-f", "NAME,TYPE,AUTOCONNECT", "connection"]:
            rows = [f"{n}:802-11-wireless:yes" for n in self.saved]
            if self.has_hotspot:
                rows.append(f"{netctl.HOTSPOT}:802-11-wireless:no")
            return 0, "\n".join(rows) + "\n"
        if a[:2] == ["-f", "NAME"]:
            names = self.saved + ([netctl.HOTSPOT] if self.has_hotspot else [])
            return 0, "\n".join(names) + "\n"
        if a[:1] == ["-g"] and "802-11-wireless.mode" in a:
            return 0, "infrastructure\n"
        if a[:1] == ["-g"] and "connection.autoconnect-priority" in a:
            return 0, "0\n"
        if "up" in a:
            name = a[-1]
            if name == netctl.HOTSPOT:
                if self.fail_hotspot:
                    return 4, ""
                self.state, self.conn = "connected", name
                return 0, ""
            if name in self.in_range:
                self.state, self.conn = "connected", name
                return 0, ""
            if self.conn == netctl.HOTSPOT:  # joining drops the hotspot even on failure
                self.state, self.conn = "disconnected", ""
            return 4, ""
        if "down" in a:
            self.state, self.conn = "disconnected", ""
            return 0, ""
        if a[:2] == ["connection", "add"]:
            self.has_hotspot = True
            return 0, ""
        if a[:2] == ["connection", "modify"]:
            return 0, ""
        return 0, ""


def test_wifi_state_reads_the_adapter():
    st = netctl.wifi_state(FakeNm(state="connected", conn="Home"), "wlan0")
    assert st.client and not st.hotspot and st.connection == "Home"
    st = netctl.wifi_state(FakeNm(state="connected", conn=netctl.HOTSPOT), "wlan0")
    assert st.hotspot and not st.client
    assert netctl.wifi_state(FakeNm(), "wlan9").present is False


def test_saved_networks_leave_out_the_hotspot_and_keep_priority_order():
    nm = FakeNm(saved=("A", "B"), has_hotspot=True)
    assert netctl.saved_networks(nm) == ["A", "B"]


def test_terse_output_with_escaped_colons_is_split_right():
    assert netctl._split(r"My\:Net:802-11-wireless:yes") == ["My:Net", "802-11-wireless", "yes"]


def test_the_hotspot_profile_is_made_with_autoconnect_off_and_never_brought_up():
    nm = FakeNm()
    verb = netctl.ensure_hotspot(nm, iface="wlan0", ssid="FM-Pi", band="2.4", channel=6,
                                 psk="abcdefgh")
    assert verb == "created"
    add = next(c for c in nm.calls if c[:2] == ["connection", "add"])
    assert add[add.index("connection.autoconnect") + 1] == "no"
    assert add[add.index("802-11-wireless.mode") + 1] == "ap"
    assert not any("up" in c for c in nm.calls)
    assert netctl.ensure_hotspot(nm, iface="wlan0", ssid="FM-Pi", band="2.4", channel=6,
                                 psk="abcdefgh") == "updated"


@pytest.mark.parametrize("band,channel", [("2.4", 13), ("5", 6), ("6", 1)])
def test_a_channel_that_does_not_exist_on_the_band_is_refused(band, channel):
    with pytest.raises(netctl.NetError):
        netctl.ensure_hotspot(FakeNm(), iface="wlan0", ssid="x", band=band, channel=channel,
                              psk="abcdefgh")


def test_the_first_passphrase_is_the_published_default_kept_private_and_reused(tmp_path):
    f = tmp_path / "k"
    assert netctl.is_default_passphrase(f) is True       # no file yet counts as the default
    first = netctl.passphrase(f)
    assert first == netctl.DEFAULT_PASSPHRASE and netctl.passphrase(f) == first
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(netctl.NetError):
        netctl.write_passphrase("short", f)
    netctl.write_passphrase("my-own-phrase", f)
    assert netctl.passphrase(f) == "my-own-phrase" and netctl.is_default_passphrase(f) is False


def test_a_random_passphrase_is_twelve_unambiguous_characters():
    seen = {netctl.random_passphrase() for _ in range(20)}
    assert len(seen) == 20 and all(len(p) == 12 and not set(p) & set("0O1lI") for p in seen)


@pytest.fixture
def cli_world(tmp_path, monkeypatch):
    from fm1_switch import __main__ as cli
    monkeypatch.setenv("FM1_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setenv("FM1_HOTSPOT_KEY", str(tmp_path / "key"))
    nm = FakeNm(has_hotspot=True, state="connected", conn="Home")
    monkeypatch.setattr(netctl, "nmcli", nm)
    return cli, nm, tmp_path / "key"


def test_net_passphrase_sets_resets_and_randomises_and_updates_the_profile(cli_world, capsys):
    cli, nm, key = cli_world
    assert cli.main(["net", "passphrase"]) == 0
    assert capsys.readouterr().out.strip() == netctl.DEFAULT_PASSPHRASE
    assert cli.main(["net", "passphrase", "a-new-phrase"]) == 0
    assert key.read_text().strip() == "a-new-phrase"
    modify = [c for c in nm.calls if c[:2] == ["connection", "modify"]][-1]
    assert modify[modify.index("wifi-sec.psk") + 1] == "a-new-phrase"
    assert cli.main(["net", "passphrase", "--random"]) == 0
    out = capsys.readouterr().out
    assert key.read_text().strip() in out and key.read_text().strip() != "a-new-phrase"
    assert cli.main(["net", "passphrase", "--default"]) == 0
    assert key.read_text().strip() == netctl.DEFAULT_PASSPHRASE
    assert "public" in capsys.readouterr().out
    assert cli.main(["net", "passphrase", "--bogus"]) == 1


def test_net_passphrase_prompt_needs_the_same_entry_twice(cli_world, monkeypatch, capsys):
    cli, nm, key = cli_world
    import getpass
    answers = iter(["first-phrase-1", "first-phrase-1"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["net", "passphrase", "--prompt"]) == 0
    assert key.read_text().strip() == "first-phrase-1"
    answers = iter(["one-phrase-aaa", "another-bbb"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["net", "passphrase", "--prompt"]) == 1
    assert key.read_text().strip() == "first-phrase-1"     # unchanged
    assert "differ" in capsys.readouterr().err


def test_net_status_warns_while_the_default_is_in_use_and_not_after(cli_world, capsys):
    cli, nm, key = cli_world
    cli.main(["net", "status"])
    assert "still the published default" in capsys.readouterr().out.lower()
    netctl.write_passphrase("changed-phrase-1", key)
    cli.main(["net", "status"])
    out = capsys.readouterr().out
    assert "changed from the default" in out and "still the published" not in out.lower()


def test_joined_phones_counts_stations_and_says_none_when_it_cannot_tell():
    dump = "Station aa:bb (on wlan0)\n\tinactive time: 1 ms\nStation cc:dd (on wlan0)\n"
    assert netctl.joined_phones("wlan0", lambda a: dump) == 2
    assert netctl.joined_phones("wlan0", lambda a: "") == 0

    def boom(a):
        raise netctl.NetError("no iw")
    assert netctl.joined_phones("wlan0", boom) is None


def test_the_join_qr_text_escapes_what_a_phone_would_misread():
    assert netctl.qr_payload("FM-Pi", "ab;c:d") == r"WIFI:T:WPA;S:FM-Pi;P:ab\;c\:d;;"


# ---------------------------------------------------------------------------------- loop

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setenv("FM1_HOTSPOT_KEY", str(tmp_path / "k"))
    netctl.write_passphrase("a-changed-phrase", tmp_path / "k")   # the default would be refused
    return tmp_path


def run_ticks(nm, ticks, cfg, policy, phones=0, start=1000.0, step=5.0):
    """Drive the real loop for ``ticks`` iterations with a fake clock."""
    t = [start]
    stop = threading.Event()
    n = [0]
    logs: list[str] = []

    class Stepper:
        def wait(self, _):
            n[0] += 1
            t[0] += step
            if n[0] >= ticks:
                stop.set()

    class StopProxy:
        def is_set(self):
            return stop.is_set()

        def wait(self, x):
            Stepper().wait(x)

    netwatch.watch(nm, StopProxy(), iface="wlan0", policy_fn=lambda: policy,
                   phones_fn=lambda i: phones, log=logs.append, clock=lambda: t[0],
                   status_file=cfg / "status.json")
    return logs


def test_loop_raises_the_hotspot_when_no_saved_network_is_in_range(cfg):
    nm = FakeNm(in_range=())
    logs = run_ticks(nm, 20, cfg, Policy(after=30, retry_every=0))
    assert nm.conn == netctl.HOTSPOT and any("hotspot is up" in l for l in logs)
    assert (cfg / "status.json").exists()


def test_loop_leaves_a_connected_pi_alone(cfg):
    nm = FakeNm(state="connected", conn="Home")
    run_ticks(nm, 30, cfg, Policy(after=10))
    assert not any(c[:2] == ["connection", "add"] or "up" in c for c in nm.calls)


def test_loop_goes_home_when_the_network_returns_and_nobody_is_joined(cfg):
    nm = FakeNm(in_range=())
    logs = run_ticks(nm, 12, cfg, Policy(after=10, retry_every=30), phones=0)
    assert nm.conn == netctl.HOTSPOT
    nm.in_range = {"Home"}
    logs = run_ticks(nm, 20, cfg, Policy(after=10, retry_every=30), phones=0, start=5000)
    assert nm.conn == "Home" and any("joined Home" in l for l in logs)


def test_loop_keeps_the_hotspot_while_a_phone_is_joined(cfg):
    nm = FakeNm(in_range=("Home",), state="connected", conn=netctl.HOTSPOT, has_hotspot=True)
    run_ticks(nm, 40, cfg, Policy(after=10, retry_every=30), phones=1)
    assert nm.conn == netctl.HOTSPOT
    assert not any(c[-2:] == ["up", "Home"] for c in nm.calls)


def test_a_failed_try_puts_the_hotspot_straight_back(cfg):
    nm = FakeNm(in_range=(), state="connected", conn=netctl.HOTSPOT, has_hotspot=True)
    logs = run_ticks(nm, 12, cfg, Policy(after=10, retry_every=30), phones=0)
    assert nm.conn == netctl.HOTSPOT
    assert any("hotspot goes back up" in l for l in logs)


def test_a_failing_nmcli_is_logged_and_the_loop_survives(cfg):
    def broken(args):
        raise netctl.NetError("nmcli is not installed here")
    logs = run_ticks(broken, 3, cfg, Policy())
    assert len(logs) == 3 and all("check failed" in l for l in logs)


def test_policy_comes_from_the_settings(cfg):
    import json
    (cfg / "c.json").write_text(json.dumps({
        "net_mode": "home", "net_fallback_after": 90, "net_retry_home_every": 2,
        "net_fallback_on_loss": False}))
    p = Policy.from_config()
    assert (p.mode, p.after, p.retry_every, p.on_loss, p.at_boot) == ("home", 90.0, 120.0, False, True)


# ------------------------------------------------------------ the published default is refused

def test_the_hotspot_will_not_come_up_with_the_published_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setenv("FM1_HOTSPOT_KEY", str(tmp_path / "k"))
    (tmp_path / "c.json").write_text(json.dumps({"net_default_passphrase": "refuse"}))
    nm = FakeNm(in_range=())
    with pytest.raises(netctl.DefaultPassphraseRefused) as e:
        netwatch.raise_hotspot(nm, "wlan0")
    assert "passphrase --prompt" in str(e.value)
    assert not any("up" in c for c in nm.calls)            # nothing was brought up


def test_gate_and_allow_let_the_hotspot_come_up_with_the_default_and_changing_it_does_too(tmp_path, monkeypatch):
    monkeypatch.setenv("FM1_CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setenv("FM1_HOTSPOT_KEY", str(tmp_path / "k"))
    for policy in ("gate", "allow"):                                 # gate is the default
        (tmp_path / "c.json").write_text(json.dumps({"net_default_passphrase": policy}))
        netwatch.raise_hotspot(FakeNm(in_range=()), "wlan0")
    (tmp_path / "c.json").write_text("{}")
    netwatch.raise_hotspot(FakeNm(in_range=()), "wlan0")            # no setting at all: gate
    (tmp_path / "c.json").write_text(json.dumps({"net_default_passphrase": "refuse"}))
    netctl.write_passphrase("my-own-phrase", tmp_path / "k")
    netwatch.raise_hotspot(FakeNm(in_range=()), "wlan0")            # changed: allowed under refuse


def test_the_loop_says_it_once_and_does_not_hammer_when_the_default_is_refused(cfg):
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, cfg / "k")
    (cfg / "c.json").write_text(json.dumps({"net_default_passphrase": "refuse"}))
    nm = FakeNm(in_range=())
    logs = run_ticks(nm, 60, cfg, Policy(after=10, retry_every=0))
    assert sum("hotspot refused" in l for l in logs) == 1
    assert nm.conn == "" and not any("up" in c for c in nm.calls)
    assert json.loads((cfg / "status.json").read_text())["blocked"] == "default-passphrase"


def test_net_up_refuses_before_it_takes_the_wifi_down(cli_world, capsys):
    cli, nm, key = cli_world                    # the key file is the default
    (key.parent / "c.json").write_text(json.dumps({"net_default_passphrase": "refuse"}))
    assert cli.main(["net", "up", "--yes", "--for", "20"]) == 1
    assert "published default" in capsys.readouterr().err
    assert not any("up" in c for c in nm.calls)


def test_net_status_describes_each_policy_while_the_default_is_in_use(cli_world, capsys):
    cli, nm, key = cli_world
    cfg = key.parent / "c.json"
    for policy, words in (("gate", "first phone to open a page is made to choose one"),
                          ("refuse", "WILL NOT COME UP"),
                          ("allow", "allowed by net_default_passphrase")):
        cfg.write_text(json.dumps({"net_default_passphrase": policy}))
        cli.main(["net", "status"])
        assert words in capsys.readouterr().out, policy


def test_net_setup_offers_to_change_the_default_when_run_at_a_terminal(cli_world, monkeypatch, capsys):
    cli, nm, key = cli_world
    import getpass
    import sys as _sys
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    answers = iter(["chosen-phrase-1", "chosen-phrase-1"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["net", "setup"]) == 0
    assert key.read_text().strip() == "chosen-phrase-1" and "passphrase set" in capsys.readouterr().out
    # skipping (empty entry) leaves the default and says how to change it
    netctl.write_passphrase(netctl.DEFAULT_PASSPHRASE, key)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    assert cli.main(["net", "setup"]) == 0
    assert "net passphrase --prompt" in capsys.readouterr().out and netctl.is_default_passphrase(key)
