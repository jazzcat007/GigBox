# FM-1 Fleet Controller — a touchscreen Pi that flashes four FM-1s over USB

> **Historical design note (2026-11-19).** The `fleetctl` tool it led to is superseded by
> `fm1-switch` and is being retired. MIDI hosting on the Pi is out of scope.

Design note, 2026-11-19 (updated same day). Answers: *could we have a touchscreen RPi connected by USB to
the 4 FM-1s, able to flash them to various firmwares?* and *which Pi, and how to host MIDI?*

**Decided 2026-11-19:** MIDI transport is **USB only** (no BLE hosting on the Pi). Firmware target:
**Pi 3B+ (owned) is acceptable; Pi 4 (2 GB+) preferred if buying.** Power comes from each FM-1's own
battery, never from the Pi.

**Short answer: yes, and the pieces already exist.** M-Flasher ships an **ARM64 Linux** package for
Raspberry Pi OS, and its CLI already takes `--device PATH` "with more than one unit", plus per-unit
backup, restore and firmware install ([M-Flasher CLI docs](https://github.com/jvitkauskas/m-flasher/blob/main/docs/usage.md)).
What does *not* exist is the multi-unit **safety logic** and the touch UI. Those are ours to build.

---

## 0. Would a Pi 3B+ do it? (added 2026-11-19)

| Requirement | Pi 3B+ verdict | Why |
|---|---|---|
| Run M-Flasher | **Yes** | Releases include `m-flasher_0.12.1-1_arm64.deb`, `…-linux-aarch64.tar.gz` and `aarch64.rpm` — ARM64 glibc 2.35+, i.e. Raspberry Pi OS Bookworm 64-bit. The 3B+ is a 64-bit A53. |
| Touch UI | **Yes** | The UI is a local web app in kiosk Chromium. 1 GB RAM is enough. |
| USB MIDI host to 4 units | **Yes** | USB-MIDI class driver, no special hardware. |
| Power the 4 units over USB | **No — use a powered hub** | Pi 3B+ allows **1.2 A total to all downstream USB ports combined** ([RPi power table](https://github.com/raspberrypi/documentation/blob/485bfe88e81a85f66eeec681a2ac284a345d3202/faqs/README.md)). Four FM-1s charging is well beyond that, and the 3B+ also shares its single USB 2.0 link for this. |
| ~~BLE MIDI host to 4 units~~ | **Dropped** | Decided 2026-11-19: MIDI transport is **USB only**. BLE would have worked (3B+ holds ~8–9 links, [Classmethod](https://dev.classmethod.jp/articles/raspberry-pi-3b-plus-max-ble-connection-en/)), but it adds BlueZ/ALSA-sequencer hand-config and latency jitter. USB MIDI is the one path already proven for both flashing *and* timing. |

**One power fact that changes the design:** the FM-1 has a **rechargeable 2000 mAh battery, up to 12 h runtime, USB-C for power and data**, and USB, TRS and Bluetooth MIDI usable *simultaneously* ([FM-1 launch specs](https://www.synthtopia.com/content/2026/07/13/m-vave-introduces-fm-1-fm-pocket-synthesizer/)).
So the units are **self-powered for a long session** — the USB cable is carrying data first. Charging them
from the Pi is unnecessary and is what would sink a 3B+. Run them on battery, or charge from a separate
supply, and give the Pi a **powered hub** for the data links.

**Also note: the 3B+ shares one USB 2.0 controller and its Ethernet/Bluetooth are on it too.** With four
units flashing serially (which the safety rules require anyway), bandwidth is a non-issue, but keep the
touch display on DSI and avoid saturating the same bus.

**If you already own the 3B+, it will work.** If buying, a **Pi 4 (2 GB+)** is the safer choice:
separate, cleaner USB controllers and the same ARM64 package.

### 0.1 What "USB only" changes (decided 2026-11-19)

Choosing USB for MIDI removes the BlueZ work, but it also means **one cable per unit now carries three
different things**, and they collide:

| On that one USB-C cable | Presented as | Grabbed by |
|---|---|---|
| Normal operation MIDI | USB MIDI (composite audio+MIDI) | ALSA sequencer / `aplaymidi`, our host app |
| Firmware update | USB MIDI SysEx | `fm1_install.py` / M-Flasher's soft key |
| Download-mode flash | USB mass storage (`WL82 UBOOT1.00`) | M-Flasher via SCSI (`/dev/sgN`) |

Consequences to design for:

1. **Never run the MIDI host and a flash at the same time on the same unit.** M-Flasher's soft key goes
   out on "the unit's first MIDI port" over `/dev/snd/seq`, which per its own docs "shares the port with
   whatever else uses it". A MIDI host holding that port can starve the soft key. The job runner must
   **stop the host's subscription for that unit, flash, then restart it** — a per-unit state machine, not
   an afterthought.
2. **Download mode is a role change, not a parallel channel.** While a unit is in `WL82 UBOOT1.00` it is
   *not* a MIDI device: it vanishes from the ALSA port list and appears as a block/SCSI device. Any UI that
   assumes "all four are always MIDI ports" will look broken. The touch UI should show a per-unit state:
   `MIDI` / `UPDATING` / `DOWNLOAD MODE` / `MISSING`.
3. **MIDI port names will collide or renumber.** Four identical units mean four ports whose names differ
   only by a trailing number (Felucca's own installer strips it: `re.sub(r"\s+\d+$", "", name)`). Bind
   units by **USB port path → udev symlink** (§2), and treat the ALSA client/port ID as disposable.
4. **Hot-pluging changes ALSA client numbers on every power cycle.** Re-scan the sequencer on connect
   events (`aconnect -l` / libalsa port notifications) rather than caching client IDs at boot.
5. **Keep the Pi's own MIDI clean.** Nothing else on the Pi should hold the sequencer: no DAW, no
   PipeWire MIDI bridge left running, no browser holding WebMIDI. A minimal Raspberry Pi OS Lite + our
   host app only.

**Architecture this points to:** the Pi is not a passive "MIDI hub" — it is a small **MIDI router with an
exclusive-flash lock**. During performance the host app owns all four ports; when a flash is requested,
the runner relinquishes one port, does the job, and hands it back.

---

## 1. The critical constraint: two completely different paths

This drives the whole architecture, and it is easy to get wrong.

| | **Normal update** (browser installer / `fm1_install.py`) | **Download-mode flash** (M-Flasher / Unbricker) |
|---|---|---|
| Transport | **USB-MIDI SysEx** | **USB mass storage + vendor SCSI** (`WL82 UBOOT1.00`) |
| How unit is addressed | **By MIDI port name** — the FM-1 enumerates as a composite USB MIDI device | **By device path** (`/dev/sgN`) |
| Works when firmware is broken? | No (needs firmware to answer) | **Yes** — mask ROM |
| Can it read a full backup? | No | **Yes** |
| Can it write the boot head? | No | Yes (`full-restore`) |

Sources: [M-Flasher "How it works"](https://github.com/jvitkauskas/m-flasher/blob/main/docs/development.md),
[Felucca fm1_install.py](https://github.com/hugelton/Felucca/blob/main/tools/fm1_install.py).

**M-Flasher already handles both** — it uses the soft-key SysEx `F0 22 24 35 7D F7` on the unit's
first MIDI port to put a running unit into download mode, then does everything over SCSI. That is why
the Pi design works: **one tool, one uniform interface, SCSI device paths as the identity.**

---

## 2. The identity problem — solve it once, on day one

Four identical FM-1s on one USB hub present:
- four MIDI ports named the same thing (macOS numbers them; Felucca's installer already strips a
  trailing number, `base = re.sub(r"\s+\d+$", "", name)`, because of this), and
- four `/dev/sgN` paths that **renumber on every replug** — so "flash the second one" is meaningless
  unless we bind a stable identity to each physical unit.

The good news: the flash chip itself carries a unique ID that M-Flasher reads, and m-apps uses a
**per-unit 8-hex-digit unique ID** for its SD slot folders specifically "so slots can't be loaded on
the wrong unit by mistake" ([m-apps](https://github.com/jvitkauskas/m-apps)).

### Procedure on arrival, once per unit
1. Plug in **one unit at a time**, to a known hub port.
2. `m-flasher backup` → full 1 MiB image.
3. Record: `m-flasher info` (model, flash chip JEDEC ID + size), the **unique ID**, and the **physical
   USB bus/port path** from `/dev/sgN` (`udevadm info -q path -n /dev/sgN`).
4. Physically label the unit (sticker) to match.

### Then make the identity stick
Pin each unit to a **fixed USB port** and add a udev rule keyed on the port path, giving stable
symlinks that never renumber:

```
/etc/udev/rules.d/80-fm1.rules
# port path is stable per physical hub port; KERNELS matches the usb port chain
SUBSYSTEM=="scsi_generic", KERNELS=="1-1.1.2", ATTRS{idVendor}=="4c4a", SYMLINK+="fm1-a"
SUBSYSTEM=="scsi_generic", KERNELS=="1-1.1.3", ATTRS{idVendor}=="4c4a", SYMLINK+="fm1-b"
SUBSYSTEM=="scsi_generic", KERNELS=="1-1.1.3", ATTRS{idVendor}=="1209", SYMLINK+="fm1-b"
SUBSYSTEM=="scsi_generic", KERNELS=="1-1.2",   ATTRS{idVendor}=="4c4a", SYMLINK+="fm1-c"
SUBSYSTEM=="scsi_generic", KERNELS=="1-1.3",   ATTRS{idVendor}=="4c4a", SYMLINK+="fm1-d"
```

> **Measured 2026-11-21.** The sketch that used to be here (`1-1.2`, `1-1.3`, `1-1.4`, `1-2`) was
> wrong on two of four ports. The real layout is `1-1` (SMSC 4-port) → `1-1.1` (SMSC 3-port), and
> units occupy **`1-1.1.2`, `1-1.1.3`, `1-1.2`, `1-1.3`**. `1-1.4` does not exist — `1-1.1` takes
> one of `1-1`'s four ports — and `1-1.1.1` is the Pi's **own LAN7800 ethernet chip**, so the slot
> paths are deliberately not a contiguous run. See `fm1-switch/udev/80-fm1.rules` (the rule
> moved there when `fleetctl` was retired) for why each slot
> also carries a vendor filter.

(Exact `KERNELS` values must be read from the real hub topology with `udevadm info`. Verify each
symlink by a `m-flasher info --device /dev/fm1-a` read and confirming the unique ID matches the label.)

Now `m-flasher backup --device /dev/fm1-a` always means the same instrument, and the backup filename
can carry the unique ID for a cross-check.

---

## 3. Hardware

- **Pi 4 (2 GB+) or Pi 5** running **Raspberry Pi OS 64-bit** — M-Flasher's `.deb` targets ARM64
  (glibc 2.35+, so Bookworm or newer).
- **Touchscreen** — the official 7" DSI panel, or any HDMI touch display.
- **Powered USB 2.0 hub.** Four FM-1s flashing simultaneously is the worry: a flash writes ~143
  sectors, and a brownout mid-write is exactly the scenario we're trying to engineer out. Budget for a
  hub with a real 5 V supply, and consider a **separate powered hub per pair of units**.
- No need for the Pi's own USB power budget to matter if the hub is powered.

**Caveat that bites everyone:** use *data* cables. Charge-only cables are a common failure.

---

## 4. Software stack

```
┌─────────────────────────────────────────────┐
│  Touch UI  (local web app, kiosk browser)   │   ← our code
│  unit tiles: name, FW, flash chip, health   │
└───────────────┬─────────────────────────────┘
                │ HTTP / local API, serialised per unit
┌───────────────▼─────────────────────────────┐
│  Job runner: one flash at a time per unit,  │   ← our code
│  never two units writing the same bus hard  │
└───────────────┬─────────────────────────────┘
                │ subprocess
┌───────────────▼─────────────────────────────┐
│  m-flasher CLI (ARM64 .deb, udev rule)      │   ← existing
│  find / info / backup / firmware / restore  │
│  download-mode / run-ram / restart          │
└─────────────────────────────────────────────┘
```

**Why a local web app rather than a native touch UI:** the community installers are already web apps
over WebMIDI, our emulator work is browser-based, and it renders well on a touch panel in kiosk mode.
It also means the same UI can be opened from a laptop.

### Suggested per-unit action set
- **Identify** — `m-flasher info --device /dev/fm1-a`; show model, flash JEDEC ID, unique ID
- **Backup** — always before anything else; refuse to continue if it fails
- **Install firmware** — `m-flasher firmware PACKAGE.fwsc --device …`
- **Restore** — from that unit's own backup only (enforce the unique-ID match)
- **Download mode / restart**

### Non-negotiable rules to encode (these come from the risk model in the landscape doc)
1. **Never flash without a fresh backup** of that exact unit in this session.
2. **Refuse cross-unit restores** — unique ID in the image must match the unit.
3. **Flash one unit at a time**, even though four are attached: a per-unit mutex, and a queue.
4. **Check flash chip** (`0x856014` vs `0xB36014`) and warn when the chosen firmware predates the newer
   chip. This is the single biggest footgun for a fresh batch of four.
5. **Verify settings survive a power cycle** after install — that is the real flash-chip test.
6. **Prefer stock bootloader** for round one; m-bootloader is a separate, deliberate decision.

---

## 5. What does *not* work, and one alternative worth knowing

- **Do not plan on the browser installers for this.** They identify a unit by "the one that answers",
  and Felucca's own tooling refuses when anything is ambiguous — it "refuses if there are none or
  several". The web path is a one-unit-at-a-time tool.
- **BLE is not a flashing transport here**: M-Flasher's soft key and the ROM download mode are USB.
- **Alternative: SD-card slots on-device.** [m-apps `slots`](https://github.com/jvitkauskas/m-apps)
  swaps whole flash images on an FM-1 with no computer at all (~30 s, needs a card mod). If the goal is
  *"change what a unit is"*, a Pi is the better tool for fleet-wide and recovery work; slots are better
  for a unit used away from a computer. They are complementary, not competing.
- **If the goal is really "play a different synth tonight":** [fm1-dock](https://github.com/joakimeriksson/fm1-dock)'s
  module picker swaps instruments in seconds on the device, and beats any reflashing workflow for
  performance. Use the Pi for management, not for mid-set changes.

---

## 6. Honest unknowns

- I have **not tested M-Flasher on ARM64 hardware**; the ARM64 `.deb` exists, but four devices on one
  hub through SCSI generic is an untested configuration.
- **Simultaneous flashing on one hub is untested** and is the main thing to pilot — start with one
  unit, then two, before trusting four.
- The udev `KERNELS` values above are **illustrative**; they must be read from the actual hub topology.
- m-Flasher on Linux uses `/dev/snd/seq` for the soft key, "which shares the port with whatever else
  uses it" — run nothing else that grabs MIDI on the Pi.
- Whether the FM-1's unique ID is *per-unit* or *per-flash-image* matters enormously for rule 2; I have
  only seen it used as a per-unit tag by m-apps, not documented as a chip UID. **Verify this on day
  one** with two units — it is the linchpin of the whole identity scheme.

---

## 7. Suggested build order

1. Day one: Pi + M-Flasher ARM64 + one unit; verify backup/restore round-trip.
2. Verify the unique ID is per-unit (two units).
3. udev symlinks + physical labels.
4. Thin CLI wrapper with the serialised job runner and the six rules.
5. Touch UI as a kiosk web app.
6. Pilot with all four, one flash at a time, then consider concurrency.
