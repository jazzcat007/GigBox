# FM-1 Manager on a Pi 3B+ — hardware assessment

> **Historical assessment (2026-11-20).** Written before the web kiosk and flash-id
> addressing existed; the `fleetctl` plan it discusses is superseded by `fm1-switch`.

**Date:** 2026-11-20
**Hardware in hand:** Pi 3B+ · 5inch Waveshare HDMI resistive touchscreen · 16 GB SD card (found, already written)
**Question:** what does this change about our `fleetctl` firmware-swapping plan?

---

## 0. Bottom line

The hardware is a **workable** base with **two constraints that are not negotiable**, and the stack you quoted contains **one script that does not exist**.

| Thing | Verdict |
|---|---|
| Pi 3B+ for firmware swapping | **Yes.** Confirms our earlier call. |
| 5" resistive touch, terminal UI | **Yes, with care.** 800×480 gives ~**99×30** at 8px — workable, not roomy. Resistive needs a **stylus**, not fingers. |
| SD card in hand | **No.** It holds a **2018 image (Raspbian Stretch)** — EOL, and its own partitions are 46 MB `/boot` + 1.8 GB rootfs. Not the Pi manager. |
| `fm1buddy` MIDI host | **Wrong device.** It is **ESP32-S3 firmware**, GPL-3.0, no Pi binary. It does not run on the Pi. |
| `curl … /fm1buddy/main/tools/pi-setup.sh` | **Does not exist — HTTP 404**, and no such file in any branch of either repo. Do not pipe it to `sh`. |
| 4 FM-1s on the Pi's USB | **Blocked by power, not by software.** |

---

## 1. The SD card

`diskutil list` shows **disk6**, "Built In SDXC Reader", 15.7 GB, removable, not read-only:

| Partition | Size | Contents |
|---|---|---|
| `disk6s1` | 45.3 MB, FAT32 | `/boot` — `kernel7.img`, `bcm2710-rpi-3-b-plus.dtb` |
| `disk6s2` | 1.8 GB, Linux (ext4) | rootfs, unmounted |
| — | **13.8 GB** | unallocated |

`/boot/issue.txt`:

> `Raspberry Pi reference 2018-06-27` · `Generated using pi-gen, 0d34fb94…, stage2`

**Raspbian Stretch** — released 2018, **end-of-life in 2022, no security updates**. It will *boot* a 3B+, and that is the best thing to say about it.

`config.txt` is stock and untouched. `cmdline.txt` adds `fsck.repair=yes rootwait noswap ro fastboot` — a hand edit, with `ro` (read-only root), which looks like an attempt at a card-corruption-proof kiosk. Fine idea, wrong decade.

**This is not a keeper.** A 3B+ is fully supported by current Raspberry Pi OS. Use the card as **media** (flash a fresh image to it), not as the system.

### Why this matters more than it sounds

The unbrick tools need `sg` and raw SCSI; our `fleetctl` needs `usbutils`. Every package and every tutorial assumes a *current* base. Starting from Stretch means every `apt install` fights a dead archive, and a firmware-management appliance that cannot be patched is a poor place to keep four synths' only flash backups.

---

## 2. The touchscreen

[Waveshare 5inch HDMI LCD](https://www.waveshare.com/wiki/5inch_HDMI_LCD): **800×480**, HDMI video, **resistive** touch over **SPI** (XPT2046, driven by the `ads7846` overlay). Backlight ~**235 mA** with the backlight on, 45 mA off.

### Constraint 1 — it eats the whole GPIO header

The panel occupies the **40-pin header** for touch (SPI0: GPIO 8/9/10/11 + `penirq`). You cannot stack a HAT, and you have no free GPIO for buttons, a buzzer or an LED. If we want a physical affordance, it has to be **USB** — which is also in short supply (§4).

### Constraint 2 — size, honestly

At the default 8×16 font, 800×480 is about:

```
99 columns × 30 rows
```

That is two double-height header lines plus ~20 lines of unit status. **Enough for a good TUI**, but it is not a page you can put four units *and* a firmware chooser on at once. Design for a persistent 4-unit header and a single focused panel below it, not a dashboard of panels.

### Constraint 3 — calibration is a config file, not a wizard

Waveshare's config for this panel:

```ini
# /boot/firmware/config.txt  (Bookworm; /boot/config.txt on older)
hdmi_group=2
hdmi_mode=87
hdmi_cvt 800 480 60 6 0 0 0
hdmi_drive=1
dtoverlay=waveshare-ads7846,penirq=25,xmin=200,xmax=3900,ymin=200,ymax=3900,speed=50000
```

plus `waveshare-ads7846.dtbo` copied into `/boot/firmware/overlays/`.

Two traps specific to our plan:

- **The default touch range (200–3900) is not calibrated to your panel.** Waveshare's own note reports better numbers as `x_min=164,x_max=4010,y_min=154,y_max=3758`, and the only reliable method is `evtest` — press each edge, read the raw numbers. Budget 20 minutes with a stylus on first boot.
- **If you rotate the screen, you must rotate the touch ranges too** — the four numbers have to be **swapped**, not just reordered, or the cursor moves 90° off from the finger. `display_rotate` (config.txt) and the X11 `Calibration`/`SwapAxes` options behave *oppositely*, per Waveshare's own table. Rotate **once**, early, and write down what worked.

Also note: the touch panel is an **X11/devicetree** affair. A Linux-console TUI gets it through `evdev`; if we end up on Wayland/X11 instead the calibration lives somewhere else again. Decide the display stack before tuning touch.

### Display recommendation

**Yes to the TUI, and it fits.** But:

- Resistive = **stylus only**. If the appliance lives on a bench, a tethered stylus is fine. If it moves, consider a capacitive panel later. This is not a blocker, just don't design 48px target rows.
- **Buy a separate power supply.** The panel's 235 mA comes off the same rail as the FM-1s (§4).

---

## 3. What the stack you quoted actually is

I checked the primary sources rather than take the summary at face value. Three things in it need correcting.

### 3.1 `fm1buddy` is an ESP32-S3 app, not a Pi app

From [konsumer/fm1buddy](https://github.com/konsumer/fm1buddy) directly:

- **"No soldering or assembly needed, just get a cheap Freenove ESP32-S3 2.8" (FNK0104B)"**
- **`src/s3/`** — "the CYD-S3 boards: screen, touch, and port.h"; **`lib/fm1s3/`** — "the ESP32-S3 side of hal.h: USB host (TinyUSB), the recovery key, Bluetooth MIDI (NimBLE), Wi-Fi"
- Built with PlatformIO (`pio run -e cyd-s3 -t upload`); license **GPL-3.0-only**
- It *also* builds a desktop **simulator** (`-e sim`, SDL2) for screenshots and headless tests

So there is **no Raspberry Pi build of fm1buddy**, and the Pi's USB controller / Bluetooth stack are not what it targets. Its `pi-setup.sh` **returns 404** and appears in **no branch** of `fm1buddy` or [`lilfm1buddy`](https://github.com/konsumer/lilfm1buddy).

> **Do not run `curl … | sudo sh` against that URL.** Even if a script appeared there, piping an unfetched, unread script into a root shell on the box holding your unit backups is the one habit worth skipping.

Two things in fm1buddy *are* worth keeping in mind, because they describe work we have already done or deliberately deferred:

- It advertises **"FM1 Recover: write a firmware or backup to an FM-1 that won't start … when people say 'use RP2040'"** — i.e. the Transporter route (§3.3).
- Its recovery loader is **m-bootloader's loader** (MIT), vendored as a prebuilt header. That is the same family as the `m-flasher` path we chose for round 1.

**You already decided not to make the Pi a MIDI host.** fm1buddy being unavailable on the Pi costs you nothing for that decision — but it does mean the touch/MIDI-mapping half of that feature list is **not reachable** on this hardware without writing it ourselves. That is a much bigger project than the TUI.

### 3.2 The unbricker is more capable than the summary implies

From [Quixotic7/MvaveFM1Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker) directly. It is **MIT**, Linux/Pi works as described (`sudo`, `sg`), and it does more than two restore modes:

| Command | Notes |
|---|---|
| `softkey` | Sends `F0 22 24 35 7D F7` over USB-MIDI to drop a **running** unit into ROM boot, so no button gymnastics. **Writes nothing**; a power cycle brings it back. |
| `info` | Uploads the loader, prints **chip key `0x980F` and flash ID** — `0x856014` **or** `0xB36014`. Both accepted since **1.2.1**. |
| `restore PKG` | Writes **only** `0x4000..0x92FFF`, double-read backup first, read-back per block, retry once. **Refuses official V16** (it runs to `0xA8FFF`) rather than write half a firmware. |
| `full-restore PKG` \| `--image FULL.bin` | Writes every differing 4 KiB sector **including the boot head**, head **last**, validating JieLi header CRCs and requiring the head to be stock (or `--other-head`). |
| `repair-settings --factory DUMP.bin` | Fixes the *known* post-custom-firmware corruption: Felucca-family data at `0x97000..0xDFFFF` / `0xFC000` sits **inside stock V15's** settings store `0x93000..0xE8FFF`, and stock then reads leftovers as its own settings. |

Three details that matter to our design:

1. **`full-restore` is the only path that writes the boot head**, and it validates the head it is about to write. This is exactly the capability gap I flagged for the Transporter — **and it is available over plain USB.** It strengthens the case for not needing an RP2040 at all.
2. **The `repair-settings` trap is a live risk for us.** We plan to install custom firmwares on all four units. Going *back* to stock V15 without this step produces corrupted presets that look like a firmware bug. Any "return to stock" workflow we build must run this, or we will chase a ghost.
3. **It refuses V16 on the narrow path** — correct behaviour, but it means "restore stock" needs the **V15** package, not whatever M-VAVE ships today (which is V16). **Archive V15 now.** If M-VAVE stops distributing it, `restore` stops working.

### 3.3 Transporter: keep it as the last resort

Confirmed against the source: the Transporter (XIAO RP2040) is for a unit that shows **nothing on USB at all**, and **its firmware refuses to write the boot head**. So it is *not* the tool for head repair — `full-restore` is, and `full-restore` needs only USB. The Transporter's niche is now genuinely narrow: dead USB, no ROM boot.

Also worth knowing, from the README's troubleshooting: Felucca-family firmware has a watchdog and a boot-loop guard, so a hung unit **enters ROM boot by itself** after two failed boots, or after three quick power cycles. Many "bricked" units need no tool at all — they need patience and a power cycle.

### 3.4 One oddity, flagged but not chased

[Matt Roboto's recovery guide](https://mr-roboto.github.io/nauclerus/docs/RECOVERY.md) (a separate macOS tool) reports the boot disk as **`WL80UBOOT1.00`**, while MvaveFM1Unbricker and our own notes say **`WL82 UBOOT1.00`** (same VID `0x4C4A` / PID `0x8057`). Either a typo, a different chip revision, or a different string in a different mode. It does not change anything we do, because we match on **VID/PID and SCSI inquiry**, not on the product string — but if a unit appears under the "WL80" spelling, do not assume it is the wrong device.

---

## 4. The real constraint: power and ports

This is the part that decides the appliance, and it is not about firmware at all.

### The Pi 3B+'s USB is a single shared 2.0 controller

| Rail | Budget |
|---|---|
| Total downstream USB (all 4 ports) | **1.2 A** (shared, and it must also cover Ethernet + Bluetooth which share the same chip) |
| Stock Pi 3B+ supply | **5 V / 2.5 A** |
| Waveshare 5" backlight | **~235 mA** |

So the picture on a stock 2.5 A supply is roughly: Pi (~600 mA) + panel (~235 mA) leaves you under 1 A of the 2.5 A **before** anything is plugged into USB — and that 1 A has to cover **four FM-1s**.

**Four FM-1s on the Pi's own ports is not going to work.** That is the honest answer, and it has nothing to do with `fleetctl`.

### Consequences for the design

Three options, in order of preference:

1. **Powered hub + bigger PSU (recommended).** 5 V 4 A supply, a good self-powered hub, Pi's own ports carrying at most one unit. Removes the 1.2 A limit from the equation entirely.
2. **Pi as controller, not as host.** Flash one unit at a time, from a **USB-C** port... which the 3B+ does not have. So: one unit at a time on the Pi, which serialises the fleet. Slow but bulletproof.
3. **Two-station rig.** Pi + panel = the "manager" (packages, backups, TUI); a separate powered hub held by the unbrick tool for bulk writes.

Also relevant, and already true for our tool: **the unbricker itself refuses more than one `WL82 UBOOT1.00` present**, and our udev rules bind units by **physical port path**. Both of those make "plug in all four and go" the wrong workflow anyway. **Serialise, and let the port path identify the unit.**

---

## 5. What this means for `fleetctl`

Good news: **almost nothing changes in the code.** The tool is `m-flasher`-based, stdlib-only, and non-interactive, which is exactly what a TUI needs to wrap.

But two things *should* change, and both are small:

### 5.1 Wrap the unbricker — it closes our biggest honest gap

`restore --id` currently fails closed on a **mis-parsed** identity (conservative, safe). The unbricker's `info` gives us a **second, independent** way to read the flash ID, and its `softkey` removes the "get it into download mode with your fingers" step. That is a direct answer to the one remaining open item.

### 5.2 `repair-settings` belongs in the "back to stock" path

If we ever restore a unit that previously ran a Felucca-family firmware, we should run `repair-settings` — or we will hand ourselves corrupted presets and blame the firmware. It needs a **factory dump** (`DUMP.bin`), which the unbricker README notes is available as a "full dump of a factory-fresh unit on the older flash chip" from [Quixotic7/FM1Backups](https://github.com/Quixotic7/FM1Backups). **Download that now, alongside V15** — neither is in our repo and both are load-bearing.

---

## 6. Recommended build order

1. **Archive the load-bearing files.** V15 `FM-1.fwsc` (file sha256 `db1642b2…`), and the factory dump. These are the two files whose disappearance breaks recovery. Keep them in two places.
2. **Flash a current Raspberry Pi OS Lite to the 16 GB card.** Ignore the 2018 image. Name the host, set the locale/timezone, enable SSH.
3. **Bring up the panel** — `config.txt` block above, dtbo, then `evtest` to get real touch numbers. **Rotate before calibrating**, and write down what worked.
4. **Install `fleetctl`'s dependencies + the unbricker**, then verify `sudo modprobe sg` and `python3 fm1_unbrick.py info` against **one** unit.
5. **Power the fleet properly** before believing anything about four units at once.
6. **Only then** build the TUI — over a `fleetctl` that is already known-good.

---

## 7. Open questions

- Which **Raspberry Pi OS** release — Bookworm (stable, well-documented for this panel) or Trixie (current)? Bookworm is the lower-risk choice for a bench appliance.
- **Stock supply or 5 V 4 A + powered hub?** This determines whether "plug in all four" is ever a supported workflow, or whether serialised-by-port is the design.
- Do we want the TUI on the **Linux console** (no display server, fewest moving parts, `fbcon`) or under **X11/Wayland** (easier rotation and calibration)? The console is the right answer for an appliance; it also makes the touch calibration a one-time `evdev` setup.
- Is the **4-unit-at-once** workflow a real requirement, or does serialised-with-port-identity cover it? (Everything in the tooling so far says serialised.)

## 8. Sources

- [Waveshare 5inch HDMI LCD wiki](https://www.waveshare.com/wiki/5inch_HDMI_LCD) — resolution, SPI touch pins, config block, calibration, rotation, backlight current
- [konsumer/fm1buddy](https://github.com/konsumer/fm1buddy) — ESP32-S3 target, license, recover/loader notes
- [konsumer/lilfm1buddy](https://github.com/konsumer/lilfm1buddy) — sibling project, confirms no `pi-setup.sh`
- [Quixotic7/MvaveFM1Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker) — commands, V15 hash, `full-restore`, `repair-settings`, safety gates
- [Quixotic7/FM1Backups](https://github.com/Quixotic7/FM1Backups) — V15/V16 packages and factory dump
- [Matt Roboto recovery guide](https://mr-roboto.github.io/nauclerus/docs/RECOVERY.md) — independent protocol account; the `WL80` spelling
- Local: `diskutil list`, `/Volumes/boot/issue.txt`, `/Volumes/boot/config.txt`, `cmdline.txt`, `overlays/`
