# Hardware release checklist

For the owner, on the bench, before phase B is merged to `main` or any build is called a
release. The software suites cover the logic (Python 266, JS 57 at the time of writing); this
list covers what only the bench can show: the panel's touch, a phone on the LAN, the USB ports
and the flash chips.

Run it on one commit. Write the commit hash at the top of your record, and keep the record
with the release.

## 0. Before you start

- [ ] Commit under test: `git -C ~/fm1 rev-parse --short HEAD` → `________`
- [ ] Back up the Pi's package: `tar --exclude=__pycache__ -czf ~/fm1/code-backups/pre-release-$(date +%Y%m%d).tar.gz -C ~/fm1 fm1-switch/fm1_switch`
- [ ] All four units are on the bench, running, and you have their firmware labels written down.
- [ ] A phone on the same Wi-Fi as the Pi, a stylus, and a spare USB port on the Pi.
- [ ] You know where the photos for the README go (see the *Photos* section of the top-level README).

Expected: nothing in the list below is started until these are ticked.

## 1. Fresh Pi install

Use a card you have not used for FM-1 before, so nothing from the bench is left on it.

1. Flash Raspberry Pi OS Lite **64-bit** to the card. Boot, then update the OS.
2. Install m-flasher (its ARM64 `.deb`, or a binary at `~/bin/m-flasher`, or set `FM1_FLASHER`).
3. Clone the repository to `~/fm1` and run `sudo ./setup.sh` (or `sudo MFLASHER_DEB=… ./setup.sh`).
4. Check the udev rule: `udevadm info -a -p /sys/bus/usb/devices/<port>` for each port your
   units use. Edit `fm1-switch/udev/80-fm1.rules` if the port paths differ, then reinstall.
5. Run `~/fm1/fm1-switch/.venv/bin/fm1-switch diagnose` and `… units`.

Pass when:

- [ ] `diagnose` reports m-flasher, the sg driver and the udev links without an error line.
- [ ] `units` lists the four slots `fm1-a` … `fm1-d`, none of them written.
- [ ] `~/.config/fm1/config.json` (if you made one) parses; `units` is 1–8.

## 2. Headless install (phone or tablet, no panel)

1. Copy `fm1-switch/systemd/fm1-web-headless.service` to `/etc/systemd/system/`, replace
   `YOUR-USER`, then `sudo systemctl daemon-reload && sudo systemctl enable --now fm1-web-headless`.
2. On the phone, open `http://<pi-address>/`.

Pass when:

- [ ] The page shows `FM-1 FLEET` and the same screen as the 5-inch panel (not the expert page).
- [ ] `http://<pi-address>/expert` opens the hub and library page.
- [ ] A write from the phone asks for the Pi's write token once, and `~/.config/fm1/token`
      exists with mode `0600`.

## 3. Register four units (from the panel)

Register one unit at a time. Put only that unit into download mode on the Pi:

    ~/fm1/fm1-switch/.venv/bin/fm1-switch download fm1-a

Then on the panel: SETTINGS → REGISTER → tap the unregistered unit → REGISTER.

Repeat for `fm1-b`, `fm1-c`, `fm1-d`.

Pass when:

- [ ] Each unit gets a letter A–D, and the confirmation names its full flash id and its letter.
- [ ] `~/fm1-units.json` holds four different flash ids, one per letter.
- [ ] With two unregistered units in download mode, REGISTER refuses and says why.
- [ ] A unit that leaves download mode between the scan and the confirm is refused, not bound.
- [ ] NAME… on a unit's own screen renames it, and the name shows on its tile.

## 4. Flash one unit

Use unit A, and a firmware package that is in the library (or fetch one with GET FIRMWARE).

1. Open unit A, pick the package, tap FLASH.
2. Read the WRITE confirmation: it names unit A and the package, and says settings are not erased.
3. Tap WRITE. Watch the phase text in the footer.

Pass when:

- [ ] The unit restarts and its tile shows the new firmware label.
- [ ] A new `auto/` backup (`.bin` and `.json`) appeared in `~/fm1-backups/` for unit A's id.
- [ ] `~/fm1-flash.log` has a `request` line, then a `done` line, for unit A.
- [ ] **Stylus-hold check (audit UX-01):** on the Unit screen, hold the stylus on FLASH
      (or on the SEVERAL FLASH button) for three seconds. Nothing is written. Then lift.

## 5. Backup and restore

1. On unit A's screen, RESTORE… → BACK UP. Confirm. The copy goes to `~/fm1-backups/manual/`
   and shows with a ★ on the list.
2. On a backup row, tap DEL. A chooser opens with RESTORE… and DELETE…. Tap CANCEL.
   Check that nothing was deleted.
3. On the ★ hand backup from step 1, tap DEL, then DELETE…, then DELETE. Check that it is gone
   and its `.json` record with it.
4. Tap RESTORE on the `auto/` backup that step 4 saved before the flash. Confirm with WRITE.

Pass when:

- [ ] The hand backup appears with a ★ and the right date (with the year).
- [ ] DEL alone deletes nothing; the chooser and its confirm are both needed.
- [ ] After RESTORE, unit A runs the earlier firmware, and its presets and sequences are back.
- [ ] A backup of unit B is refused on unit A's screen (the record names the unit).
- [ ] Touch targets: DEL is easy to hit with a fingertip on the panel (about 106 × 62 px at 800×480).

## 6. Phone use

With the phone on the Wi-Fi only (not mobile data):

- [ ] The fleet screen loads from `http://<pi-address>/`, and the tiles match the panel.
- [ ] Turn the phone sideways: the layout uses the screen; upright, it scrolls in one column.
- [ ] A write from the phone asks for the token; a wrong token is refused, and repeated wrong
      tokens lock that address out for a few minutes.
- [ ] The Pi is **not** reachable from outside your network (no port forwarding on the router).

## 7. Power loss mid-write

Do this only on a unit whose firmware is backed up (step 5), and expect that unit may need a
restore afterwards.

1. Start a write on unit A (as in step 4).
2. While the footer shows a write phase, cut power to the Pi.
3. Power it back on. Open the panel, then `~/fm1-flash.log`.

Check, do not assume:

- [ ] The panel does not claim unit A was flashed unless the log has a `done` line for it.
- [ ] Unit A is either running its firmware again, or shown in download mode (with RESTART UNIT),
      or shown as not coming back, with a restore offered.
- [ ] The `request` line without a `done`/`failed` line is in the log (that is the record of
      an interrupted write).
- [ ] The backup taken before the write is in `~/fm1-backups/auto/`, and RESTORE can use it.

Record what the panel said at each step, and the log lines, in your results.

## Results

| Step | Date | Pass / fail | Notes (firmware labels, flash ids, what the panel said) |
|---|---|---|---|
| 0 Before you start | | | |
| 1 Fresh Pi install | | | |
| 2 Headless install | | | |
| 3 Register four units | | | |
| 4 Flash one unit | | | |
| 5 Backup and restore | | | |
| 6 Phone use | | | |
| 7 Power loss mid-write | | | |

## Not covered by this list

These need their own check before a release that depends on them:

- Touch calibration on another panel (`fm1-switch/tools/calibrate-touch.sh`) and its EDID
  (`fm1-switch/tools/make-edid.py`): both are per panel.
- A stock V15 unit, which has no MIDI interface: the power-cycle fallback is described in the
  code but has no capture yet (`tests/FIXTURES.md`).
- Soft-key replies on the other firmware the bench runs: B (ghoulbox-2.4) and C (jiant
  V10.7.81). Captured so far: A on stock `FM-1 966` (2026-11-25 08:36) and D on SLOOP 2.8.5.
- Firmware that needs its maker's own installer (the Phase D roadmap).
