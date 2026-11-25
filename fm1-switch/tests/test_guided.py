"""guided.flash: the unit that is written is the one whose FLASH reports the confirmed id.

A fake bus stands in for the hardware. Ports are deliberately shuffled against the
"expected" ones so a test fails if anything routes by port instead of by id.
"""

import threading

import pytest

from fm1_switch import guided, mflasher, softkey
from fm1_switch.guided import Device, WriteRefused

A, B, C, D = "aaaaaaaa00000001", "aaaaaaaa00000002", "aaaaaaaa00000003", "aaaaaaaa00000004"


class Bus:
    """A fake USB bus. ``running`` = {port: id}; ``download`` = {Device: id}."""

    def __init__(self, running, download=None, broadcast=True):
        self.running = dict(running)
        self.download = dict(download or {})
        self.broadcast = broadcast
        self.log = []
        self.fail_restart = set()
        self.unreadable = set()
        self.n = 0
        self.vanish_after_restart = False

    def devices(self):
        return list(self.download)

    def soft_key(self, port):
        self.log.append(("soft_key", port))
        moving = list(self.running) if self.broadcast else [port]
        for p in moving:
            self.n += 1
            self.download[Device(f"/dev/sg{self.n}", p)] = self.running.pop(p)

    def restart(self, node):
        self.log.append(("restart", node))
        dev = next(d for d in self.download if d.node == node)
        if dev in self.fail_restart:
            raise mflasher.Exited(1, "boom")
        self.running[dev.port] = self.download.pop(dev)
        if self.vanish_after_restart:
            self.download.clear()

    def install(self, node, package, **kw):
        self.log.append(("install", node, package))
        return mflasher.Result(True, "ok")


@pytest.fixture()
def rig(monkeypatch):
    def make(running=None, download=None, **kw):
        bus = Bus(running or {}, download, **kw)
        monkeypatch.setattr(guided, "download_devices", bus.devices)
        monkeypatch.setattr(guided, "read_id",
                            lambda d: None if bus.download.get(d) in bus.unreadable else bus.download.get(d))
        monkeypatch.setattr(guided, "_soft_key_ports", lambda uid: list(bus.running))
        monkeypatch.setattr(softkey, "soft_key", bus.soft_key)
        monkeypatch.setattr(mflasher, "restart", bus.restart)
        monkeypatch.setattr(mflasher, "install_firmware", bus.install)
        return bus
    return make


FAST = dict(sleep=lambda s: None, clock=iter(range(0, 10_000, 10)).__next__)


def run(unit_id, **kw):
    clock = {"t": 0.0}
    return guided.flash(
        unit_id, "/lib/p.fwsc", lock=threading.Lock(),
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        clock=lambda: clock["t"], **kw)


def kinds(bus):
    return [e[0] for e in bus.log]


def test_placeholder_ids_never_reach_a_write(rig):
    bus = rig({"1-1.1": A})
    for bad in ("", "port:1-1.2", "device:sg0", "slot:fm1-a"):
        with pytest.raises(WriteRefused, match="flash id"):
            run(bad)
    assert bus.log == []


def test_busy_lock_refuses(rig):
    bus = rig({"1-1.1": A})
    lock = threading.Lock()
    lock.acquire()
    with pytest.raises(WriteRefused, match="already running"):
        guided.flash(A, "p", lock=lock)
    assert bus.log == []


def test_writes_the_unit_whose_flash_id_matches_not_the_port_it_was_last_on(rig):
    # A "used to be" on 1-1.1 but the cables were swapped: B is there now.
    bus = rig({"1-1.1": B, "1-1.2": A, "1-1.3": C})
    out = run(A)
    installs = [e for e in bus.log if e[0] == "install"]
    assert len(installs) == 1
    dev = next(d for d, i in {**bus.download}.items() if i == A)
    assert installs[0][1] == dev.node and dev.port == "1-1.2"
    assert out.unit_id == A and not out.already_in_download


def test_broadcast_others_are_restarted_before_the_write_and_never_written(rig):
    bus = rig({"1-1.1": A, "1-1.2": B, "1-1.3": C})
    out = run(A)
    k = kinds(bus)
    assert k[0] == "soft_key" and k.count("soft_key") == 1
    assert k.count("restart") == 2 and k.index("install") > max(
        i for i, x in enumerate(k) if x == "restart")
    assert sorted(out.restarted) == sorted([B, C])
    assert set(bus.running.values()) == {B, C}            # back to running
    assert list(bus.download.values()) == [A]              # only the target left


def test_already_in_download_sends_no_soft_key_and_leaves_others_alone(rig):
    dev_a, dev_b = Device("/dev/sg1", "1-1.1"), Device("/dev/sg2", "1-1.2")
    bus = rig({"1-1.3": C}, {dev_a: A, dev_b: B})
    out = run(A)
    assert kinds(bus) == ["install"]
    assert bus.log[0][1] == "/dev/sg1"
    assert out.already_in_download
    assert dev_b in bus.download                            # B was put there by the user


def test_target_not_among_the_units_that_moved_writes_nothing_and_restarts_them(rig):
    bus = rig({"1-1.1": B, "1-1.2": C})                    # A is not on the bus at all
    with pytest.raises(WriteRefused, match="nothing was written"):
        run(A)
    assert "install" not in kinds(bus)
    assert kinds(bus).count("restart") == 2
    assert not bus.download


def test_nothing_entered_download_mode_writes_nothing(rig, monkeypatch):
    bus = rig({"1-1.1": A})
    monkeypatch.setattr(softkey, "soft_key", lambda port: bus.log.append(("soft_key", port)))
    with pytest.raises(WriteRefused, match="no unit entered"):
        run(A)
    assert "install" not in kinds(bus)


def test_unreadable_unit_is_restarted_not_written(rig):
    bus = rig({"1-1.1": A, "1-1.2": B})
    bus.unreadable = {B}
    out = run(A)
    a_node = next(d.node for d, i in bus.download.items() if i == A)
    assert [e for e in bus.log if e[0] == "install"][0][1] == a_node
    assert kinds(bus).count("restart") == 1 and out.unit_id == A


def test_failed_restart_is_reported_as_stuck_but_the_write_still_happens(rig):
    bus = rig({"1-1.1": A, "1-1.2": B})
    bus.fail_restart = {Device("/dev/sg2", "1-1.2")}
    out = run(A)
    assert out.stuck == [B] and "power-cycle" in out.message
    assert "install" in kinds(bus)


def test_target_vanishing_during_restarts_is_refused(rig):
    bus = rig({"1-1.1": A, "1-1.2": B})
    bus.vanish_after_restart = True
    with pytest.raises(WriteRefused, match="moved"):
        run(A)
    assert "install" not in kinds(bus)


def test_no_midi_link_refuses_without_touching_anything(rig, monkeypatch):
    bus = rig({"1-1.1": A})
    monkeypatch.setattr(guided, "_soft_key_ports", lambda uid: [])
    with pytest.raises(WriteRefused, match="MIDI"):
        run(A)
    assert bus.log == []


def test_install_failure_is_a_refusal_with_the_reason(rig, monkeypatch):
    bus = rig({"1-1.1": A})

    def boom(node, package, **kw):
        raise mflasher.Exited(2, "write failed")
    monkeypatch.setattr(mflasher, "install_firmware", boom)
    with pytest.raises(WriteRefused, match="write failed"):
        run(A)


def test_relearns_fingerprint_after_the_write(rig, monkeypatch):
    bus = rig({"1-1.1": A})
    calls = []
    monkeypatch.setattr(softkey, "port_ids", lambda port: "1209:0001")
    monkeypatch.setattr(guided.registry, "relearn_after_flash",
                        lambda uid, port, label: calls.append((uid, port, label)) or "ok")
    out = run(A, firmware_label="choralroot-0.42")
    assert calls == [(A, "1-1.1", "choralroot-0.42")] and out.relearned == "ok"
    assert out.message == "installed"


def test_unit_that_does_not_come_back_is_reported_not_hidden(rig, monkeypatch):
    rig({"1-1.1": A})
    monkeypatch.setattr(softkey, "port_ids", lambda port: None)
    out = run(A)
    assert out.relearned == "not-running" and "could not re-identify" in out.message


def test_restart_unit_restarts_only_the_matching_device(rig):
    dev_a, dev_b = Device("/dev/sg1", "1-1.1"), Device("/dev/sg2", "1-1.2")
    bus = rig({}, {dev_a: A, dev_b: B})
    out = guided.restart_unit(B, lock=threading.Lock(), **FAST)
    assert bus.log == [("restart", "/dev/sg2")] and out.restarted == [B]
    assert dev_a in bus.download


def test_restart_unit_relearns_the_fingerprint_it_comes_back_with(rig, monkeypatch):
    """Firmware changed outside our tooling leaves the stored fingerprint stale (the
    "4 lettered rows + 4 '?' rows" bench state, 2026-11-25); a restart must repair it."""
    dev_b = Device("/dev/sg2", "1-1.2")
    rig({}, {dev_b: B})
    calls = []
    monkeypatch.setattr(softkey, "port_ids", lambda port: "1209:0001")
    monkeypatch.setattr(guided.registry, "relearn_after_flash",
                        lambda uid, port, label: calls.append((uid, port, label)) or "ok")
    out = guided.restart_unit(B, lock=threading.Lock(), **FAST)
    assert calls == [(B, "1-1.2", None)] and out.relearned == "ok"
    assert out.message == "restarted"


def test_restart_unit_says_so_when_it_cannot_re_identify(rig, monkeypatch):
    rig({}, {Device("/dev/sg2", "1-1.2"): B})
    monkeypatch.setattr(softkey, "port_ids", lambda port: None)
    out = guided.restart_unit(B, lock=threading.Lock(), **FAST)
    assert out.relearned == "not-running" and "could not re-identify" in out.message


def test_restart_unit_refuses_a_unit_not_in_download_mode(rig):
    bus = rig({"1-1.1": A})
    with pytest.raises(WriteRefused, match="not in download mode"):
        guided.restart_unit(A, lock=threading.Lock())
    assert bus.log == []
    with pytest.raises(WriteRefused, match="flash id"):
        guided.restart_unit("port:1-1.1", lock=threading.Lock())


def test_custom_writer_is_used_after_the_same_id_checks(rig):
    bus = rig({"1-1.1": A, "1-1.2": B})
    calls = []
    out = run(A, writer=lambda node, pkg: calls.append((node, pkg)))
    a_node = next(d.node for d, i in bus.download.items() if i == A)
    assert calls == [(a_node, "/lib/p.fwsc")] and "install" not in kinds(bus)
    assert out.restarted == [B]


def test_backup_is_taken_from_the_matched_device_before_the_write(rig):
    bus = rig({"1-1.1": A, "1-1.2": B})
    order = []
    out = run(A, before_write=lambda node: order.append(("backup", node)),
              writer=lambda node, pkg: order.append(("write", node)))
    a_node = next(d.node for d, i in bus.download.items() if i == A)
    assert order == [("backup", a_node), ("write", a_node)] and out.unit_id == A


def test_failed_backup_writes_nothing_and_restarts_the_unit(rig):
    bus = rig({"1-1.1": A})

    def nope(node):
        raise mflasher.Exited(1, "disk full")
    with pytest.raises(WriteRefused, match="backup.*nothing was written"):
        run(A, before_write=nope)
    assert "install" not in kinds(bus) and not bus.download and bus.running


# ---- B6: GD-02 (a failed write is not a refusal) and GD-04 (OSError from before_write) ----

def test_a_write_that_fails_is_a_distinct_failure_that_says_the_unit_may_be_partial(rig):
    bus = rig({"1-1.1": A})

    def boom(node, package):
        raise mflasher.TimedOut(300)
    with pytest.raises(guided.WriteFailed, match="partly written"):
        run(A, writer=boom)


def test_an_oserror_from_before_write_refuses_and_restarts_instead_of_escaping(rig):
    bus = rig({"1-1.1": A})

    def nope(node):
        raise OSError("read-only file system")
    with pytest.raises(WriteRefused, match="nothing was written"):
        run(A, before_write=nope)
    assert "install" not in kinds(bus) and not bus.download and bus.running


# ---- B9: GD-03 (a slow boot is not "didn't restart") ----

def test_a_unit_that_boots_at_40_seconds_is_relearned_not_reported_stuck(rig, monkeypatch):
    rig({"1-1.1": A})
    t = {"n": 0}

    def ids(port):
        t["n"] += 1
        return softkey.RUNNING_IDS and next(iter(softkey.RUNNING_IDS)) if t["n"] > 80 else None
    monkeypatch.setattr(softkey, "port_ids", ids)          # ~0.5 s per poll: running at ~40 s
    monkeypatch.setattr(guided.registry, "relearn_after_flash", lambda *a: "ok")
    assert run(A).relearned == "ok"
