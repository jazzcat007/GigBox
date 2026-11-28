#!/usr/bin/env python3
"""The real bench test of the WiFi fallback (docs/BENCH-wifi-fallback.md). Run it ON THE PI, detached:

    nohup ~/fm1/fm1-switch/.venv/bin/python ~/fm1/fm1-switch/tools/bench_wifi.py > ~/bench-wifi.log 2>&1 &

It drives the sequence by itself, because the SSH session that starts it will drop when the Pi
leaves the house WiFi:

  1. checks it is safe to start (connected to a saved network, permissions, hotspot profile);
  2. runs the fallback supervisor with short test timings (the fm1-net service by default);
  3. takes wlan0 off the network (`nmcli device disconnect`, the simulated "WiFi lost") and
     measures how long the hotspot takes to appear;
  4. waits for you to join the hotspot with a phone, then holds for a while and checks that the Pi
     does NOT leave while the phone is on it, past several retry intervals;
  5. waits for you to leave, and measures how long the Pi takes to return to the saved network;
  6. ALWAYS, at the end, on any error and after --timeout: stops the supervisor, puts your settings
     back, and reconnects the saved network.

It writes one line per event with a time, then a PASS / FAIL / SKIP table. It never creates, changes or
deletes a saved network. A reboot also undoes everything it does to the radio.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fm1_switch import config, netctl  # noqa: E402

T0 = time.monotonic()
RESULTS: list[tuple[str, str, str]] = []


def say(msg: str) -> None:
    try:
        print(f"[{time.monotonic() - T0:6.1f}s {time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except OSError:
        pass


def result(name: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((name, verdict, detail))
    say(f"RESULT {verdict}: {name} {detail}".rstrip())


def poll(pred, timeout: float, every: float = 1.0) -> float | None:
    """Seconds until ``pred()`` was true, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if pred():
                return time.monotonic() - start
        except netctl.NetError:
            pass
        time.sleep(every)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", type=float, default=20, help="net_fallback_after for the test (s, >=10)")
    ap.add_argument("--retry-min", type=float, default=1.0, help="net_retry_home_every for the test (min)")
    ap.add_argument("--hold", type=float, default=150, help="how long a joined phone is watched (s)")
    ap.add_argument("--phone-wait", type=float, default=300, help="how long to wait for a phone to join (s)")
    ap.add_argument("--return-wait", type=float, default=300, help="how long to wait for the return (s)")
    ap.add_argument("--timeout", type=float, default=1200, help="hard stop for the whole test (s)")
    ap.add_argument("--no-service", action="store_true",
                    help="run `fm1-switch net watch` as a child instead of the fm1-net service")
    ap.add_argument("--interval", type=float, default=1.0, help="how often to look (s)")
    args = ap.parse_args()

    signal.signal(signal.SIGHUP, signal.SIG_IGN)        # the SSH session that started us will drop
    iface = str(config.value("net_iface"))
    cfg_path = config.config_path()
    saved_cfg = cfg_path.read_text() if cfg_path.exists() else None
    child: subprocess.Popen | None = None
    service = not args.no_service
    deadline = T0 + args.timeout
    state = lambda: netctl.wifi_state(netctl.nmcli, iface)  # noqa: E731
    phones = lambda: netctl.joined_phones(iface)  # noqa: E731

    def cleanup() -> None:
        say("CLEANUP: stopping the supervisor, restoring settings, reconnecting")
        try:
            if service:
                subprocess.run(["systemctl", "--user", "stop", "fm1-net"], timeout=20, check=False)
            elif child and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                child.wait(10)
        except Exception as e:  # noqa: BLE001
            say(f"  could not stop the supervisor: {e}")
        try:
            if saved_cfg is None:
                cfg_path.unlink(missing_ok=True)
            else:
                cfg_path.write_text(saved_cfg)
            say("  settings restored")
        except OSError as e:
            say(f"  could not restore {cfg_path}: {e}")
        try:
            if not state().client:
                if state().hotspot:
                    netctl.hotspot_down(netctl.nmcli)
                for name in netctl.saved_networks(netctl.nmcli):
                    if netctl.join(netctl.nmcli, name):
                        say(f"  reconnected {name}")
                        break
                else:
                    say("  NO SAVED NETWORK CONNECTED: reboot the Pi, or use the HDMI panel")
        except Exception as e:  # noqa: BLE001
            say(f"  could not reconnect: {e}")

    try:
        # ---- 1. safe to start?
        st = state()
        if not st.present:
            result("adapter present", "FAIL", iface)
            return 1
        if not st.client:
            result("starts connected to a saved network", "FAIL",
                   f"state={st.state} conn={st.connection!r}: nothing to test from")
            return 1
        say(f"start: {iface} is on {st.connection}")
        code, out = netctl.nmcli(["-t", "-f", "PERMISSION,VALUE", "general", "permissions"])
        perms = dict(r.split(":", 1) for r in out.splitlines() if ":" in r)
        need = ("org.freedesktop.NetworkManager.network-control",
                "org.freedesktop.NetworkManager.settings.modify.system",
                "org.freedesktop.NetworkManager.wifi.share.protected")
        bad = [p for p in need if perms.get(p, "").strip() != "yes"]
        if bad and not os.environ.get("FM1_NMCLI"):
            result("permissions (polkit rule)", "FAIL", f"not 'yes': {', '.join(bad)}")
            return 1
        result("permissions (polkit rule)", "PASS")
        if not netctl.hotspot_exists(netctl.nmcli):
            result("hotspot profile exists", "FAIL", "run `fm1-switch net setup`")
            return 1
        result("hotspot profile exists", "PASS")
        if netctl.is_default_passphrase():
            result("hotspot passphrase is not the published default", "FAIL",
                   "change it first: fm1-switch net passphrase --prompt")
            return 1
        result("hotspot passphrase is not the published default", "PASS")

        # ---- 2. test timings, start the supervisor
        new_cfg = json.loads(saved_cfg) if saved_cfg else {}
        new_cfg.update({"net_mode": "auto", "net_fallback_at_boot": True, "net_fallback_on_loss": True,
                        "net_fallback_after": args.after, "net_retry_home_every": args.retry_min,
                        "net_retry_only_when_empty": True})
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(new_cfg, indent=2))
        say(f"test settings: wait {args.after:g}s, retry every {args.retry_min:g} min, guard on")
        if service:
            r = subprocess.run(["systemctl", "--user", "start", "fm1-net"], capture_output=True,
                               text=True, timeout=30)
            if r.returncode != 0:
                result("supervisor service starts", "FAIL", r.stderr.strip()[:120])
                return 1
            result("supervisor service starts", "PASS")
        else:
            child = subprocess.Popen([sys.executable, "-m", "fm1_switch", "net", "watch"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(max(8, 5 * args.interval))
        if not state().client:
            result("connected Pi left alone while the supervisor runs", "FAIL", f"state={state().state}")
            return 1
        say("supervisor running; staying connected")
        time.sleep(args.after + 5)        # longer than the wait: it must not raise the hotspot
        if state().client:
            result("connected Pi is left alone past the wait", "PASS")
        else:
            result("connected Pi is left alone past the wait", "FAIL", f"state={state().state}")
            return 1

        # ---- 3. the WiFi goes away
        say("SIMULATING LOSS OF THE HOUSE WIFI: nmcli device disconnect")
        t_loss = time.monotonic()
        code, _ = netctl.nmcli(["device", "disconnect", iface])
        if code != 0:
            result("simulate the loss", "FAIL", f"nmcli exit {code}")
            return 1
        took = poll(lambda: state().hotspot, min(args.after * 3 + 60, deadline - time.monotonic()),
                    args.interval)
        if took is None:
            result("hotspot appears after the link is lost", "FAIL", "never came up")
            return 1
        lo, hi = args.after * 0.6, args.after * 2.5 + 15
        result("hotspot appears after the link is lost", "PASS" if lo <= took <= hi else "FAIL",
               f"{took:.0f}s after the loss (the setting is {args.after:g}s)")

        # ---- 4. a phone joins; the Pi must not leave
        say("NOW: join the Wi-Fi network 'FM-Pi' with a phone (passphrase: fm1-switch net passphrase)")
        say(f"     waiting up to {args.phone_wait:g}s for a phone; open http://fm-pi.local/ to check the UI")
        joined = poll(lambda: (phones() or 0) >= 1, min(args.phone_wait, deadline - time.monotonic()),
                      args.interval)
        if joined is None:
            result("a phone can join the hotspot", "SKIP", "no phone joined in time")
            result("a joined phone is not kicked off", "SKIP")
        else:
            result("a phone can join the hotspot", "PASS", f"after {joined:.0f}s")
            say(f"phone joined; watching for {args.hold:g}s (retry interval is {args.retry_min * 60:g}s)")
            start = time.monotonic()
            held = True
            while time.monotonic() - start < args.hold and time.monotonic() < deadline:
                if not state().hotspot:
                    held = False
                    break
                time.sleep(args.interval)
            n_retries = int(args.hold // (args.retry_min * 60))
            result("a joined phone is not kicked off", "PASS" if held else "FAIL",
                   f"watched {time.monotonic() - start:.0f}s, about {n_retries} retry interval(s) passed")
            if not held:
                say("the Pi left the hotspot while a phone was joined")

        # ---- 5. nobody on it: it returns
        if state().hotspot:
            say("NOW: disconnect the phone from FM-Pi (turn its Wi-Fi off, or forget the network)")
            gone = poll(lambda: phones() == 0, min(args.phone_wait, deadline - time.monotonic()),
                        args.interval)
            if gone is None:
                result("the Pi returns when nobody is on it", "SKIP", "the phone did not leave")
            else:
                say("nobody on the hotspot; waiting for the Pi to go back to the saved network")
                took = poll(lambda: state().client, min(args.return_wait, deadline - time.monotonic()),
                            args.interval)
                if took is None:
                    result("the Pi returns when nobody is on it", "FAIL", "still on the hotspot")
                else:
                    result("the Pi returns when nobody is on it", "PASS",
                           f"{took:.0f}s after the phone left (retry interval {args.retry_min * 60:g}s)")
        else:
            result("the Pi returns when nobody is on it", "PASS" if state().client else "FAIL",
                   "already back on a saved network")
        return 0
    except Exception as e:  # noqa: BLE001
        result("the bench test itself", "FAIL", repr(e))
        return 1
    finally:
        cleanup()
        say("SUMMARY")
        for name, verdict, detail in RESULTS:
            say(f"  {verdict:5} {name} {detail}".rstrip())
        fails = [r for r in RESULTS if r[1] == "FAIL"]
        say("ALL PASSED" if not fails and RESULTS else f"{len(fails)} FAILED")


if __name__ == "__main__":
    sys.exit(main())
