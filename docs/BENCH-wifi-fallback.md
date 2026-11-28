# Bench test: the WiFi fallback on the real Pi

The fallback (docs/ROADMAP-field-network.md) is tested end to end against a stand-in `nmcli` (see
`fm1-switch/tests/test_net_synthetic.py`). This is the same sequence against the real Pi, the real
NetworkManager and the real radio. It takes about 10 minutes and needs **you and a phone**.

## What it checks

1. A Pi on a saved WiFi network is left alone while the supervisor runs.
2. When the WiFi is lost (simulated with `nmcli device disconnect wlan0`), the hotspot appears after the
   configured wait, and the real `nmcli` and radio do what the stand-in did.
3. A phone can join it, and `http://fm-pi.local/` opens the UI.
4. A phone that is joined is **never** dropped, even after several retry intervals.
5. After the phone leaves, the Pi goes back to the saved network by itself.
6. All of that works from the **service** (`fm1-net`), which needs the polkit rule.

## Before (the Pi side is already staged)

- The polkit rule is installed (`nmcli general permissions` reads `yes` for network-control,
  settings.modify.system and wifi.share.protected).
- The hotspot profile exists (`fm1-switch net status` says `hotspot profile: present`).
- The service file `fm1-net.service` is installed for your user but **not enabled and not started**.
- The test script is `~/fm1/fm1-switch/tools/bench_wifi.py`.

**Change the hotspot passphrase first** (`fm1-switch net passphrase --prompt`). While it is still the published
default, the first page your phone opens is the set-a-passphrase form, which restarts the network in the middle of
the test; the script stops at its first check if it is still the default.

Have a phone with Wi-Fi ready, the Pi's HDMI panel or a power switch within reach, and know the hotspot
passphrase (`fm1-switch net passphrase`, run on the Pi; see [GUIDE-wifi-hotspot.md](GUIDE-wifi-hotspot.md)).

## Run it

On the Pi, over SSH:

```bash
nohup ~/fm1/fm1-switch/.venv/bin/python ~/fm1/fm1-switch/tools/bench_wifi.py > ~/bench-wifi.log 2>&1 &
```

Your SSH session will drop at about 30 seconds, when the Pi leaves the house WiFi. That is expected. The script
carries on by itself. Then:

1. **About 1 minute in:** the Wi-Fi network **FM-Pi** appears. Join it from your phone with the passphrase,
   and open `http://fm-pi.local/`. (You have 5 minutes.)
2. **Stay joined for about 2.5 minutes.** The Pi must not leave while you are on it.
3. **When it says so (or after 2.5 minutes), turn the phone's Wi-Fi off** (or forget FM-Pi). The Pi goes back
   to the house WiFi within about a minute.
4. Reconnect your Mac or phone to the house WiFi and read the result:

```bash
cat ~/bench-wifi.log
```

The end of the log is a table of `PASS` / `FAIL` / `SKIP` lines with the times measured. A phone that never
joined is a `SKIP`, not a `FAIL`.

## It cleans up after itself

At the end, on any error and after 20 minutes at most, the script stops the supervisor, puts your settings back
exactly as they were (the test uses a short wait and a 1-minute retry, then restores the real ones) and
reconnects the saved network. It never creates, changes or deletes a saved network.

If the Pi is not back on the house WiFi after about 25 minutes, **power-cycle it**: the radio change
(`device disconnect`) does not survive a reboot, and the hotspot profile has auto-connect off, so it boots onto
the house WiFi as before.

## If a check fails

| Failure | Likely cause |
|---|---|
| `hotspot passphrase is not the published default` | change it: `fm1-switch net passphrase --prompt` |
| `permissions (polkit rule)` | the rule is missing or not loaded: `sudo systemctl restart polkit` |
| `supervisor service starts` | `systemctl --user status fm1-net`; the unit file or lingering |
| `hotspot appears ...` never | the supervisor could not raise it: run `fm1-switch net up --yes --for 30` and read the error |
| `a joined phone is not kicked off` FAIL | the Pi switched back while a phone was on it: a real bug, keep the log |
| `the Pi returns ...` FAIL | `nmcli connection up <network>` failed; check the saved network is in range |

Keep `~/bench-wifi.log` and note anything odd (how long the join took, whether the phone warned about "no
internet"). Record the result in `docs/ROADMAP-field-network.md` under *Bench results*.
