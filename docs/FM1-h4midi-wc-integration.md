# Adding the CME H4MIDI WC to the FM-1 fleet

> **Background only.** MIDI routing is outside this software's scope: the Pi swaps
> firmware and does not host MIDI. Kept for context on how the fleet is played.
>
> **2026-11-23:** the `fleetctl` tool this note discussed was retired and removed (audit
> §3.10, FC-01); `fm1-switch` — the panel, writing only through flash ids — does the
> firmware work it described. The note is kept as background history.

Design note, 2026-11-19. Answers: *we have an H4MIDI WC — how does it fit the Pi fleet
controller?*

## What the H4MIDI WC actually is

([CME product page](https://www.cme-pro.com/h4midi-wc-usb-host-midi-interface/), specs read 2026-11-19)

| | |
|---|---|
| Role | **USB Host** (expands MIDI reach) + **USB Client** to a computer |
| USB-A **Host** | up to **8-in-8-out**, class-compliant devices, **one optional external hub** |
| USB-C **Client** | up to **4-in-4-out** to a computer (macOS/Windows/Linux/Android/iOS) |
| DIN | **2× MIDI IN, 2× MIDI OUT** (5-pin), plus TRS-A via adapters |
| Processing | 32-bit; routing / filtering / mapping via **HxMIDI Tools** (Win/Mac/Android/iOS) |
| Presets | **4 presets, switchable by button with no computer attached** |
| Power | 5 V USB bus **or** 9 V DC (5.5×2.1 mm); **USB-A powers downstream devices, max 1 A** |
| Latency | "close to zero latency and zero jitter"; passes clock, SysEx, MTC, MPE |
| Storage | **none** — it is a router, not a file store |
| Optional | WIDI Core (BLE MIDI) module |

## The key architectural consequence

**The H4MIDI WC will not help you flash.** It carries MIDI only — no mass-storage/SCSI
pass-through — so it cannot enter the FM-1's ROM download mode or touch flash. Flashing
still needs the direct USB-SCSI path from the Pi (or from a unit's own SD-card slots).

It also **cannot replace the Pi as the host** for flashing, because the Pi must also see
each unit as a `/dev/sgN` SCSI device — which only happens on a direct USB connection.

So the H4MIDI WC is **not a sidegrade of the Pi; it is a complement**:

| job | who does it | path |
|---|---|---|
| Flash / backup / restore | **Pi** (`fleetctl` → `m-flasher`) | direct USB → SCSI |
| Route / split / merge / filter MIDI at play time | **H4MIDI WC** (standalone) | USB host + 2×2 DIN |
| Musical "footprint" (more notes, more channels, clock) | **H4MIDI WC** | standalone, 4 presets, no computer |

## Three wiring options

### A. Pi does everything (current design) — the H4 sits behind the Pi
Pi → USB → 4× FM-1 (direct, for flashing). H4MIDI WC hangs off the Pi's spare USB port as
a device, or takes MIDI from the Pi and fans it out over DIN.
**Use when:** flashing is the priority and MIDI routing is simple.
**But:** the Pi 3B+ shares one USB 2.0 controller; adding the H4 to it competes with the
four units on the same bus.

### B. H4MIDI WC takes over MIDI; the Pi keeps flashing — **recommended**
```
            ┌──────────── Raspberry Pi 3B+ ────────────┐
            │  fleetctl (flash/serialise only)         │
            └──┬──────────┬──────────┬──────────┬──────┘
               │ USB      │ USB      │ USB      │ USB → H4MIDI WC (client)
            FM-1 A     FM-1 B     FM-1 C     FM-1 D      │
                                                          │ USB-A host (hub, ≤8)
                                                     controllers / other gear
                                                          │ DIN 2 in / 2 out
                                                     TRS-A → FM-1 MIDI IN
```
**Why this wins:**
- **Flashing stays direct.** Four units on the Pi's own ports, exactly as designed and as
  `fleetctl` expects.
- **MIDI routing leaves the Pi.** The H4 handles split/merge/filter/mapping and its
  **4 button-selectable presets work with no computer** — so the rig still plays if the
  Pi is busy flashing, rebooted, or off.
- **TRS-A is the bridge.** The FM-1's MIDI is **3.5 mm TRS Type A, input only** — in
  pin terms, the jack's **ring** is the MIDI data line ([fm1-base `plat.h`](https://github.com/charlesvestal/fm1-base/blob/main/include/plat.h),
  which also states outright: *"in only: the FM-1 has no MIDI out jack"*). The H4's 2 DIN
  OUTs → TRS-A adapters → two of the four units' MIDI INs give you *external* control of
  those units, without consuming a USB port.
- **Relieves the 3B+ USB bus:** play-time MIDI traffic comes off USB entirely for any unit
  reached over DIN.

**Catch:** TRS-A reaches only **2 units** (2 DIN OUTs) — unless you add a thru box
(CME Thru5 WC or WIDI Thru6 BT). For all four, either add the thru box or route those two
over USB on the H4 host side.

### C. H4 as the primary USB host, Pi for flashing only
Pi flashes (direct), H4 hosts the units for MIDI via its USB-A host port + an external hub.
**Not recommended:** the units would need to be *re-cabled* between modes (they cannot be
plugged into both the Pi and the H4 at once for flashing), and USB-MIDI over a shared hub
through the H4 adds an indirection for no benefit the DIN route doesn't already give.

## What this changes in `fleetctl`

Very little — and that's the point of keeping the flash path direct:

1. **No code change required.** `fleetctl`/`m-flasher` still address units by `/dev/fm1-a`
   …`/dev/fm1-d` over direct USB.
2. **`MidiHost` becomes optional.** In option B the Pi does not own the FM-1 MIDI ports at
   play time, so `host.acquire()`/`release()` are only needed for units reached *over USB*.
   Keep the release-before-flash logic (it is free and still correct for those units), but
   the Pi's role narrows to flashing.
3. **New optional field:** a unit reached over DIN still needs a mapping
   (`slot -> H4 DIN output`) if the UI is to show "fm1-c plays on H4 OUT 2". That belongs
   in the udev/port-map table, not in the transition logic.
4. **Power:** the H4's USB-A supplies **1 A max** to downstream devices. That is fine for
   class-compliant controllers; **do not** try to charge the FM-1s from it (each has its own
   2000 mAh battery). Power the H4 from 9 V DC or a powered USB-C supply so its host port
   budget isn't shared with bus power.

## Hardware to note on arrival

- **TRS adapters must be Type A** to match the FM-1. (Type B adapters are a classic
  silent-failure trap.)
- The FM-1's TRS jack is **input only** — there is no TRS MIDI *out*, so DIN cannot be used
  to *send* from an FM-1. Anything the FM-1 must send (clock, notes, CC) goes over USB.
  **This is the one real limitation:** if you want an FM-1 to *drive* the rig, it has to be
  on USB.
- H4MIDI WC has no storage, so it cannot hold firmware or backups — keep the Pi for that.

## Revised architecture summary

- **Pi 3B+** = flasher + serialiser + backup store. Direct USB to all four.
- **H4MIDI WC** = play-time MIDI router with 2×2 DIN and 8-port USB host, 4 pedal-board
  presets, no computer needed.
- **Bridge** = TRS-A adapters (FM-1 MIDI IN only) and/or the H4 USB-A host.
- **Rule that does not change:** a unit is either being flashed or being played — the
  H4 does not blur that line, it just moves play-time routing off the Pi.
