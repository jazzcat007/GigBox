# M-VAVE FM-1 — Custom Firmware Landscape

> **Research snapshot as of 2026-11-19.** Firmware versions and projects change; check
> The Firmware Hub (https://thefirmwarehub.com) for current information.

Research compiled 2026-11-19 for a 4-unit FM-1 fleet.
Intent: survey the community, then pick a base for our own build. Priority: **live performance** and
**sound design / FM engine**.

> Status: every claim below was verified against a primary source fetched on 2026-11-19 (repos, raw
> READMEs, the Firmware Hub rescue guide, the Firmware Atlas). Five parallel research agents were run on
> hardware/RE, the bootloader, the Charles Vestal firmware family, the other forks and community testing
> reports; one failed and four were still running when this was compiled against the sources I had fetched
> directly, so they were stopped. Their dedicated passes are worth re-running later — in particular for
> Reddit/Elektronauts live-performance reports, which this environment could not fetch (Reddit blocks it).

---

## 1. What the hardware actually is

| Fact | Value | Source |
|---|---|---|
| SoC | JieLi AC791N / WL82 family, exact IC **AC7911B**, QFN-48 6×6 mm | [FM-1-RE](https://github.com/AL-255/FM-1-RE) |
| CPU | dual-core 32-bit **pi32v2**, FPU, up to 320 MHz | [FM-1-RE](https://github.com/AL-255/FM-1-RE) |
| Flash | SiP SPI NOR, **1 MB**, two chip variants: Puya P25Q80H (`0x856014`) and **UC25HQ80 (`0xB36014`)** on units sold since autumn 2026 | [hub](https://github.com/aroum/fm1-custom-fw), [rescue guide](https://thefirmwarehub.com/tools/rescue/) |
| Execution | XIP from SPI flash at `0x02000000` | [FM-1-RE](https://github.com/AL-255/FM-1-RE) |
| Display | 240×240 TFT | [Atlas](https://wero1414.github.io/atlas-fm-1/) |
| Keys | 27 capacitive keys, velocity/pressure readable | [Felucca BUILDING](https://github.com/hugelton/Felucca/blob/main/BUILDING.md), [fm1-base](https://github.com/charlesvestal/fm1-base) |
| Connectivity | USB-C (MIDI + USB audio), BLE, **3.5 mm TRS Type A MIDI IN (input only)**, WiFi on AC791N | [Atlas](https://wero1414.github.io/atlas-fm-1/), [fm1-base](https://github.com/charlesvestal/fm1-base) |
| Debug | **No exposed JTAG/SWD/UART pads, no recovery buttons** | [hub RE](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md) |

**Toolchain:** JieLi's Linux x86-64-only clang 4.0.1 targeting pi32v2, plus three files from the JieLi AC79 SDK
(`uboot.boot`, `cfg_tool.bin`, `eq_cfg_hw.bin`). On macOS the build runs in a `linux/amd64` Docker container —
this is exactly our workstation class. Sources: [Felucca BUILDING.md](https://github.com/hugelton/Felucca/blob/main/BUILDING.md),
[fm1-base](https://github.com/charlesvestal/fm1-base).

---

## 2. Boot chain, update path, and the actual risk model

Flash layout (from [hub RE](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md) and
[Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker)):

```
0x0000000  Mask ROM (in silicon — cannot be erased, cannot be bricked)
0x0000000 ─ 0x0003FFF   Boot head: stock bootloader + partition header (16 KB)
0x00004000 ─ 0x00092FFF App region written by the standard updater path (V15)
0x00093000 ─ ...        V16 extends to 0xA8FFF
... up to 0x00FFFFF     Flash DB / settings / samples
```

Key findings that change how we plan:

1. **The update channel is USB-MIDI SysEx**, not DFU. The vendor updater (Qt6 + RtMidi on macOS) carries its
   firmware as an embedded Qt resource (`@JMUA` / `JLUFW` container) and streams it over MIDI
   ([hub RE §4](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md)). Any browser
   installer is doing the same thing over WebMIDI.
2. **There is no cryptography to stop us.** Container headers, target tags and checksums are checked; RSA/ECDSA
   signing is not ([hub RE §2.3](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md)).
   That is why 270+ firmwares exist. It also means a malicious `.fwsc` will run — see §6.
3. **The mask-ROM rescue mode is the real safety net.** A unit whose firmware is broken/incomplete appears on USB
   as a mass-storage device `WL82 UBOOT1.00` (`VID 0x4C4A / PID 0x8057`). That ROM is in silicon, so a bad
   firmware install is *recoverable* without opening the case
   ([Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker)).
4. **The genuinely dangerous class is "boot head replaced/damaged".** `0x0000–0x3FFF` is what puts the unit into
   download mode and what the stock updater talks to. A firmware that cannot re-enter update mode, or a
   half-written boot head, is the one way to end up needing hardware.
   ([hub RE §5.2](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md))
5. **Hardware fallback exists but needs a $8 RP2040.** The [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter)
   pulses the JieLi USB boot key on D+/D− at power-up to force ROM boot, writing 1 MiB in ~3 s. It **cannot write
   the boot head** — so it does not cover case 4, which needs `full-restore` on a unit that still shows `WL82 UBOOT1.00`.

### The newer-flash-chip trap (this matters for 4 new units)

Units shipping since autumn 2026 carry the **UC25HQ80** flash (`0xB36014`). Firmwares written before it only
write flash when they see the old Puya ID. On a new unit that means: **saves vanish on power-off, and updates
often refuse to start.** Nothing is broken — but a firmware built for the old chip is effectively a different
device ([Firmware Hub rescue guide](https://thefirmwarehub.com/tools/rescue/)).

**Therefore, day-one action on all 4 units: read the flash chip ID before flashing anything.**
M-Flasher shows it as soon as the unit is in download mode ([M-Flasher](https://github.com/jvitkauskas/m-flasher),
[r/… rescue notes](https://thefirmwarehub.com/tools/rescue/)).

---

## 3. The safe procedure (what we should actually do)

Community standard, from [Firmware Hub](https://thefirmwarehub.com/tools/rescue/),
[Atlas](https://wero1414.github.io/atlas-fm-1/) and [hub](https://github.com/aroum/fm1-custom-fw/blob/main/OFFICIAL_FIRMWARE.md):

1. **Charge fully.** Never let a unit power off mid-write.
2. **Full flash backup first** — M-Flasher does this (reads whole flash before writing), the Unbricker reads
   twice and compares. Keep a per-unit backup file; keep the four backups separate and named by serial.
3. **Back to stock V15** before moving between custom firmwares (don't flash custom-over-custom).
4. Install the target via its browser installer (Chrome/Edge, USB, no hub).
5. Verify: power-cycle, confirm settings survive a power cycle (this is the flash-chip test), confirm USB-MIDI
   device name and that the updater can still see it.

### 3.1 Tool choice for a macOS workstation

**[M-Flasher](https://github.com/jvitkauskas/m-flasher)** is the tool that fits us: free, unsigned app for
Windows/macOS/Linux that talks to the chip's built-in USB download mode (so it works when the firmware is
broken), backs up the whole flash first, shows a flash map of exactly what will change, writes only what
differs and verifies every write. It has a CLI as well as a window. On a unit with stock firmware and the stock
bootloader, the keys can't enter download mode, but M-Flasher's **DOWNLOAD MODE** button can.

It also handles the rebranded twin (Fame MiniFM) and SMK-37 Pro/Elite.

**m-bootloader (optional, and a decision):** [m-bootloader](https://github.com/jvitkauskas/m-bootloader) (MIT)
replaces the vendor bootloader at `0x0000–0x3FFF` and adds REC-at-power-on download mode, a recovery menu, a
MIDI-mode installer that works with no firmware running, and a **firmware checksum before boot** (the stock
bootloader starts firmware unchecked). That is genuinely better protection for a fleet.

But note: **official M-VAVE updates and Baud Girl's installer put the stock bootloader back**, so it has to be
reinstalled after every vendor update — and it means deliberately rewriting the one region the hardware couldn't
otherwise recover. M-Flasher can restore the stock bootloader, so it is reversible on a unit that still reaches
download mode.

**Recommendation:** do the first round of installs on **stock bootloader + M-Flasher**. Only after all four units
are backed up and verified should we evaluate m-bootloader as an optional hardening step — the checksum-before-boot
is worth having on units used live.

Backup/restore facts worth internalising ([Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker),
[official V15/V16 packages](https://github.com/Quixotic7/FM1Backups)):

- Official **V15** region hash pinned; the Unbricker refuses a file that claims V15 with another hash.
- The standard restore path writes only `0x4000–0x92FFF`. **V16 runs to `0xA8FFF`** — so `restore` refuses V16 and
  you must use `full-restore` (which also writes the stock bootloader).
- Full-flash backups contain on-device songs and sounds.

---

## 4. The landscape

Nearly every musical FM-1 firmware descends from **Felucca** (Hügelton Instruments / Leo Kuroshita, GPL-3.0),
which itself uses **felucca-bsp** (MPL-2.0) as the hardware layer and **Felucca-Engines** (MIT).
Sources: [Felucca](https://github.com/hugelton/Felucca), [Felucca BUILDING.md](https://github.com/hugelton/Felucca/blob/main/BUILDING.md),
[Atlas lineage graph](https://wero1414.github.io/atlas-fm-1/), [hub](https://github.com/aroum/fm1-custom-fw).

### 4.1 The four lineages that matter

| Lineage | Base | Licence | Why it matters to us |
|---|---|---|---|
| **Felucca** | 9 engines (ANALOG, FM6/DX7, PHASE, LOFI, SAMPLE, VOICE, TRIO, WHEEL, GRAIN), 4 tracks, 64-step seq, SLICER, FX | GPL-3.0-only (engines MIT, BSP MPL-2.0) | The upstream. 8 voices + 6 drums. Web installer + web editor. 1.8.2 recognises the new flash chip. |
| **SLOOP** (+ sloopDX, Move, JIANT, ORBIT, Loom, KARMA…) | Felucca | GPL-3.0 | The big live-groovebox branch: dual-core rendering, USB stereo **audio in**, TRS MIDI, MIDI clock, sample chopping. |
| **fm1-base** (Charles Vestal) | felucca-bsp platform layer | MPL-2.0 — **"an app built on it may use any licence"** | The cleanest *new* project base: host simulator + browser emulator from the same source, optional BLE/WiFi/USB-audio/SD parts, packaging + installer tooling. Beta; `plat.h` may change before 1.0. |
| **fm1_open** (M-VAVE official) | — | GPL-3.0 app + linking exception, **closed-source sound engine** | The vendor's own SDK. Some firmwares build on it. |

### 4.2 Standouts for our two priorities

**Sound design / FM engine**
- **sloopDX** — Dexed `msfa` 6-op DX7 core at ~99% sample-identical, 256 DX7 `.syx` voices, on-device voice editing,
  filter behind every voice ([hub §1.1.2](https://github.com/aroum/fm1-custom-fw)).
- **Felucca FM6** — DX7-compatible 6-op within Felucca's 9-engine platform.
- **DX747** — 8-part multitimbral DX7 module, 16 6-op voices, TX816-inspired: this is the one to consider if we
  want the fleet to behave like a rack rather than four keyboards.
- **Saadet**, **P6** (Korg Polysix model), **Monohull**, **FMER** (6-op + VA, 4-track), **Shrub** (Serum-core
  port by Steve Duda) — breadth for a sound-design bench.
- Stock **V16** now ships 8-part multitimbral, 20-voice, 64-step arranger, stereo FX, DX7 `.syx` import
  ([hub §1.1](https://github.com/aroum/fm1-custom-fw)) — the vendor closed a lot of the gap in October 2026.

**Live performance**
- **SLOOP / FM1 Move / KARMA** — clip/scene launching, live recording, stems over USB.
- **CTL-1** — turns the FM-1 into a **MIDI controller** (velocity + aftertouch from the capacitive keys, BLE MIDI,
  Euclidean drum sequencer). This is the firmware that gives the four units a role *besides* being synths.
- **Radio** — WiFi internet radio (proof of concept, pre-release) ([fm1-radio](https://github.com/charlesvestal/fm1-radio)).

### 4.3 Scale check

The scene is enormous and moving daily: the hub catalogues **270+ community firmwares** across Felucca, SLOOP,
FoMni, independent-engine and games categories ([hub](https://github.com/aroum/fm1-custom-fw)), and the Firmware
Hub's credits list several hundred firmware projects by author ([Firmware Hub](https://thefirmwarehub.com/tools/rescue/)).
Most are solo projects of days-to-weeks age. **This is a reason to pick a base with a simulator and a licence that
lets us fork, not to chase releases.**

---

## 5. Our own build: which base to fork

The two realistic answers:

| | **Felucca / SLOOP (GPL-3.0)** | **fm1-base (MPL-2.0)** |
|---|---|---|
| What you get | Deepest audio engine + groovebox already built; 9 engines; community-proven on hardware | Platform only (screen, fonts, panel, LED, audio, MIDI USB+TRS, storage, screen reader, simulator, emulator, build/package/install) — *you write the instrument* |
| Licence effect | Derivative firmware must be GPL-3.0 | **Your app can be any licence** — the decisive difference for a product or a licensed performance tool |
| Host simulator | Browser "try it" demo + host tests; not a full simulator | **Same app runs on host simulator and browser emulator** — matches our host-first testing habit |
| Maturity | 1.8.2, active, new-chip support in 1.8.2 | **Beta**; `include/plat.h` and `fm1.toml` may change before 1.0 |
| Proven apps | SLOOP, Loom, JIANT, dozens | CTL-1, Radio, OG Synth, QUADRO, CHOPPAH, CHIMERA |

**Recommendation:** if the goal is *our own instrument with our own licence and host-first testing*, fork
**fm1-base** and take FM/DX7 DSP from **Felucca-Engines (MIT)** — you get the Apache-style reuse without the
GPL-3.0 obligation on the whole app. If the goal is *ship something playable in weeks*, fork **SLOOP** and accept
GPL-3.0. This mirrors the CLOCKWORK decision pattern: vendor bootloader stays untouched, host twin mirrors the
device.

---

## 6. Threats and hygiene (relevant with 4 units on a shared workstation)

- **No firmware signing** → a rogue `.fwsc` is arbitrary code on the FM-1; the AC791N can be reconfigured into a
  USB HID keyboard (BadUSB). Treat every firmware as untrusted software
  ([hub RE §10](https://github.com/aroum/fm1-custom-fw/blob/main/REVERSE_ENGINEERING.md)).
- Prefer firmwares with a public repo, release SHA256 and an active history; verify hashes
  ([Atlas](https://wero1414.github.io/atlas-fm-1/)).
- First flashes on a machine we can wipe, or through an isolated USB port. Once we know what we trust, move to the
  main rig.
- Backups contain on-device projects — treat as user data.

---

## 7. Unknowns worth resolving once the units land

- **Which flash chip each of the 4 units has** (and whether they're a uniform batch).
- **Whether the 4 units are the same board revision** — none of the sources documents revision-to-firmware mapping
  for the FM-1; the AC7911BA/BB SDRAM split (2 MB vs 8 MB) is documented for the chip, not for the FM-1 board.
- **TRS MIDI is input-only** — so a four-unit rig cannot share clock over TRS; USB hosting or a MIDI hub is needed.
- **Battery behaviour under flash** — no source documents a mid-write power-loss test on the FM-1.

---

## Sources

- [aroum/fm1-custom-fw](https://github.com/aroum/fm1-custom-fw) — hub, 270+ firmwares
- [AL-255/FM-1-RE](https://github.com/AL-255/FM-1-RE) — firmware RE
- [jvitkauskas/m-bootloader](https://github.com/jvitkauskas/m-bootloader) — open bootloader
- [Quixotic7/MvaveFM1Unbricker](https://github.com/Quixotic7/MvaveFM1Unbricker) — recovery
- [Firmware Hub rescue guide](https://thefirmwarehub.com/tools/rescue/) — flash chip, rescue tools
- [FM-1 Firmware Atlas](https://wero1414.github.io/atlas-fm-1/) — comparison + lineage
- [hugelton/Felucca](https://github.com/hugelton/Felucca) — upstream engine platform
- [charlesvestal/fm1-base](https://github.com/charlesvestal/fm1-base) — MPL platform
