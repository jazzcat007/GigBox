# FM-1 16-voice rig: flash plan (plan only)

> Written 2026-11-25. **Nothing in this document has been run.** No unit was flashed, backed up or restored, and m-flasher was not started. Each step is for the owner to run.

Target set (from [docs/FM1-16-voice-rig-candidates.md](FM1-16-voice-rig-candidates.md), section b):

| Unit | Channels | Target firmware | Catalogue chip flag |
|---|---|---|---|
| A | 1–4 | Felucca 1.8.3 | new |
| B | 5–8 | Melodee 1.6.1 | new |
| C | 9–12 | SLOOP 2.8.5 | new |
| D | 13–16 | sloopDX 4.0.1 | new |

## Starting point

From [docs/ROADMAP-device-settings.md §3](ROADMAP-device-settings.md) (verified from the Pi's flash and rename logs, 2026-11-23):

| Unit | Current firmware | Needs |
|---|---|---|
| A (BLACK) | grimm 2.0.2 (catalogue `chip=old`) | backup, stock V15, Felucca 1.8.3 |
| B (DARK) | ghoulbox 2.4 | backup, stock V15, Melodee 1.6.1 |
| C (LIGHT) | jiant V10.7.81 | backup, stock V15, SLOOP 2.8.5 |
| D (WHITE) | **mutable 2.4** (changed by the user, per the 2026-11-25 bench notes; was SLOOP 2.8.5 on 2026-11-23) | backup, stock V15, sloopDX 4.0.1 |

Correction: an earlier version of this plan said D was already on its target. That was wrong. D's target is sloopDX 4.0.1, and D is now on mutable 2.4.

### Chip readings (2026-11-25, Pi registry)

`fm1-switch units` on the Pi (registry `~/fm1-units.json`) reports the chip and unit id per slot. The unit ids match the 2026-11-20/21 `m-flasher info` readings in [archives/units/REGISTRY.md](../archives/units/REGISTRY.md).

| Slot | Unit id | Chip | Running now |
|---|---|---|---|
| A | `1111aaaa00000001` | `0x856014` (old) | grimm-2.0.2 |
| B | `1111aaaa00000002` | `0x856014` (old) | ghoulbox-2.4 |
| C | `1111aaaa00000003` | `0x856014` (old) | jiant-V10.7.81 |
| D | `1111aaaa00000004` | **`0xB36014` (new)** | mutable-2.4 |

**Live read, 2026-11-25.** Each unit was put into download mode with `fm1-switch download <slot>`, read with `m-flasher info` against its own scsi device (matched by USB port), then restarted. Nothing was written to flash. Every unit read back the same chip and unit id as the registry, and all four were running again afterwards with the router active.

| Slot | Live chip | Live unit id | Restarted |
|---|---|---|---|
| A | `0x856014` (old) | `1111aaaa00000001` | yes, running |
| B | `0x856014` (old) | `1111aaaa00000002` | yes, running |
| C | `0x856014` (old) | `1111aaaa00000003` | yes, running |
| D | `0xB36014` (new) | `1111aaaa00000004` | yes, running |

Note on B: the first `m-flasher info` on the default device refused because it found a running unit. Reading B via its own scsi device (`/dev/sg0`, port `1-1.1.3.3.3`) worked. The default device is not reliable when several units are in play, so always pass `--device` for the port being read.

## Backups taken

| Slot | Date | File (on the Pi) | Size | sha256 | Unit id checked | Restarted |
|---|---|---|---|---|---|---|
| A | 2026-11-25 | `~/fm1-backups/2026-11-25-fm1-a/fm1-20261125-130726.bin` | 1,048,576 bytes | `f45535017b3d2de40f71eaa75f07854fea5eb2db43b32cdaabdaa202b0fc2b1a` | `1111aaaa00000001` (matches) | yes, running |
| B | 2026-11-25 | `~/fm1-backups/2026-11-25-fm1-b/fm1-20261125-130636.bin` | 1,048,576 bytes | `46819df5a6e52f2aae4665df7b2be83e9316bd1af174dd6268bb2dd23439d490` | `1111aaaa00000002` (matches) | yes, running |
| C | 2026-11-25 | `~/fm1-backups/2026-11-25-fm1-c/fm1-20261125-130700.bin` | 1,048,576 bytes | `9a9ff7281b664bc060aaef2ba9014112d14f7924676fff3ec9a3504ceb47adb8` | `1111aaaa00000003` (matches) | yes, running |
| D | 2026-11-25 | `~/fm1-backups/2026-11-25-fm1-d/fm1-20261125-130540.bin` | 1,048,576 bytes | `1a2bf47c2f132b4c0b10a9bdcf47ffb8a2b05f322179c6bc5600736320828ba1` | `1111aaaa00000004` (matches) | yes, running |

All four backups were read from each unit's own scsi device, matched by USB port. Each read reported a matching chip CRC32. Copies of all four `.bin` and `.json` files are also in [archives/units/](../archives/units/) on the Mac, with the same names. The Mac copies' sha256 values match the Pi's.

## Flash log: D (2026-11-25)

| Step | Result |
|---|---|
| Fetch sloopDX 4.0.1 with `fm1-switch fetch sloopdx` | `~/fm1-firmwares/sloopdx-4.0.1.fwsc`, 610,154 bytes. sha256 `cebc88c0f996f338d4741502ec7f99d05ecb9c6de3715ab674401a14d6a255d7`, matches the catalogue. The tool reports it installs completely from m-flasher, identity `FM-1_940`. |
| Pre-write backup | Taken earlier, see the backup table (`fm1-20261125-130540.bin`). |
| Write stock V15 (`V15-FM-1.fwsc`) | Written to `0x04000-0x92FFF`, chip CRC32 verified. M-Flasher also saved `~/M-Flasher/backups/fm1-20261125-130947.bin` before the write. D booted stock as USB `4c4a:c755` "FM-1". |
| Write sloopDX 4.0.1 | Written to `0x04000-0x92FFF`, chip CRC32 verified, identity `FM-1_940`. M-Flasher saved `~/M-Flasher/backups/fm1-20261125-131110.bin` before the write, which is the V15 state. |
| Restart | D is running again on USB `1209:0001`, name "Felucca". This is the SLOOP family's name. |

**Incident, no harm done.** `fm1-switch download fm1-d` refused, because the registry places units by USB fingerprint and D's fingerprint had changed on V15. An earlier `m-flasher firmware` call put `--yes` into the device slot, which failed to parse, so nothing was written. D was then placed by port with `fm1_switch.softkey.soft_key("1-1.1.3.3.1")`, which addresses one port and refuses to guess. That worked.

**Not yet checked:**

- The identity of sloopDX after boot. The post-write read was done before the restart, and the USB name is only consistent with it, not proof.
**Save-persistence test on the new chip, D (2026-11-25): one song save, survived a reboot.**

- **Baseline** (after sloopDX was written and restarted): `~/fm1-backups/2026-11-25-fm1-d-sloopdx-baseline/fm1-20261125-131225.bin`, sha256 `1c7d1c4b88b6c8fd1e670ce74b64c2113bb3d9539de0ba0e01d336023e1337f3`.
- **Test:** a song was saved on D, then the unit was power-cycled. The song was still there after the reboot.
- **After** (taken after the reboot): `~/fm1-backups/2026-11-25-fm1-d-sloopdx-after-song-save/fm1-20261125-131654.bin`, sha256 `2244145b490d958388aa3f91ddfd26761b89d7c13861e923321e3ab2a99da5b4`.
- **Diff, 3,368 differing bytes in four 4 KiB blocks:** `0x09F000` (35 bytes), `0x0FC000` (2,439 bytes), `0x0FD000` (818 bytes), `0x0FE000` (76 bytes). The app region `0x04000–0x92FFF` and the boot head `0x0000–0x3FFF` are byte-identical to the baseline.

**Reading.** The song's data was written to flash, in the data area and not the app region, and it survived a power cycle. That is consistent with the new chip working for sloopDX saves. Limits:

- This is one save on one unit. The README says autosave also runs, and autosave is also a flash write, so this diff does not separate a manual save from autosave.
- The voice-edit test described earlier was not run. The song save was used instead.
- The `0x09F000` block and the `0x0FC000`–`0x0FE000` blocks were not mapped to sloopDX's storage areas. That needs the firmware's own layout.

**Voice-edit save test on D (2026-11-25): the value survived a reboot, per the operator.**

- **Before:** `~/fm1-backups/2026-11-25-fm1-d-sloopdx-after-song-save/fm1-20261125-131654.bin`, sha256 `2244145b…`.
- **After:** `~/fm1-backups/2026-11-25-fm1-d-sloopdx-after-voice-test/fm1-20261125-132357.bin`, sha256 `eddffae0467388ea1adaf7b539dee3faadecf716c069a71146bb28e24fc9422a`.
- **Diff, 8,367 differing bytes:** `0x09F000` (60 bytes), `0x0A0000`–`0x0A3000` (four blocks of about 2,072 bytes each), `0x0FE000` (19 bytes). The app region `0x04000–0x92FFF` and the boot head are unchanged.

**Reading.** A flash write happened in a region that did not change for the song save: `0x0A0000`–`0x0A3000`, about 8 KB across four blocks. That fits a voice-bank save, but the firmware's layout is needed to confirm it. The operator also reports the value survived a reboot. Both results point the same way: manual saves on the new chip persist on sloopDX. This is still one unit and one save of each kind.

**Connection note.** Port `1-1.1.3.3.1` (D) was briefly empty in one check. The kernel log shows many disconnect and reconnect events on both A's and D's ports. These match the soft-key and restart cycles from this session. At the end of the session all four ports were connected (`1209:0001`). The registry still lists A and D as `off/unplugged`, so it is stale for both. It is not a hardware fault.

### Mapping the changed blocks to sloopDX's storage layout

Source: [zvenson/dxsloop `firmware/src/storage.c` at v4.0.1](https://github.com/zvenson/dxsloop/blob/v4.0.1/firmware/src/storage.c). The flash map is in its comment at L22–26, and the sector mapping is `st_sector()` at L61–68. Each object has two 4 KiB copies, A and B.

| Flash block | Object in sloopDX's source | Song save | Voice save |
|---|---|---|---|
| `0x09F000` | autosave copy A (working project) | 35 bytes | 60 bytes |
| `0x0FE000` | autosave copy B (working project) | 76 bytes | 19 bytes |
| `0x0FC000` | settings copy A | 2,439 bytes | not changed |
| `0x0FD000` | settings copy B | 818 bytes | not changed |
| `0x0A0000`–`0x0A3000` | DX7 user bank 1, both halves, both copies | not changed | about 2 KB each, all four |

Projects (`0x97000`–`0x9EFFF`) did not change in either test.

**Correction to the song-save reading.** The song save did not write a project slot. It changed the autosave copies and the settings, which fits the README's autosave. So the song test shows that autosave persists across a reboot. It does not show a manual project save.

**The voice save is the stronger result.** It wrote DX7 user bank 1 in flash, which is a manual save. The sloopDX README says "SAVE stores your bank", so this is the path the voice-edit test was meant to exercise. It persisted across a reboot, and the chip's CRC32 matched in each read.

**Limits.** This is one unit, and the bank-to-menu label mapping (bank 1 as "U01") comes from the README and was not checked on the unit.
- The Pi's unit registry still shows D as `mutable-2.4`, `off/unplugged`. It needs a re-learn before `fm1-switch units` is trusted for D again. The 2026-11-20 factory backups in REGISTRY.md are different images, because all four units have been reflashed since.

D was read from its own scsi device (`/dev/sg0`, port `1-1.1.3.3.1`). The read reported a matching chip CRC32, and the file's sha256 is recorded in the sidecar JSON. D's backup is on the Pi only, not yet copied to the Mac's `archives/units/`. The 2026-11-20 factory backup of D is a different image, because D has been reflashed since.

The 2026-11-21 entries in REGISTRY.md (Felucca on A, OberVave on B, SHRUB on C, SLOOP on D) are older than the 2026-11-25 state and no longer describe the units.

## Steps

### 0. Before touching anything

1. **Charge each unit fully.** A power loss mid-write is the one step the community guides warn about ([landscape §3, step 1](FM1-firmware-landscape.md)).
2. **The router can stay running.** The 2026-11-25 router trial found the soft key still works with the router attached ([ROADMAP-dawless-hub.md](ROADMAP-dawless-hub.md)). Restart each unit after its read, so the router sees it come back.
3. **Note each unit's current per-unit settings**, such as the MIDI channel, on paper. A re-flash may keep the bytes but not the setting ([ROADMAP-device-settings §4](ROADMAP-device-settings.md)).

### 1. Read the flash chip ID on all four (read only)

- On the Pi, run `fm1-switch download <slot>` for one unit, then `m-flasher info` to read the chip ID and unit id. Then run `m-flasher restart` before moving to the next unit. M-Flasher is not installed on the Mac, so the read is done on the Pi.
- Record for each unit: `0x856014` (Puya, old) or `0xB36014` (UC25HQ80, new).
- Source: [landscape §2, "newer flash chip trap"](FM1-firmware-landscape.md); M-Flasher shows the ID ([M-Flasher](https://github.com/jvitkauskas/m-flasher)).
- **Stop if a unit reads an unexpected ID.** Do not continue with that unit until the ID is understood.

### 2. Per unit, in this order: D, B, C, A

D goes first because it is the only new-chip unit, so the first save test runs on the part that matters most. It is on mutable 2.4, which is not a target, so it needs a flash to sloopDX 4.0.1 like the others. B, C and A follow. A goes last because it runs grimm, the one current firmware flagged `chip=old`.

For each unit:

1. **Full flash backup with M-Flasher** before any write. It reads the whole flash first. Save the file named by unit letter and serial, and keep the four backups separate ([landscape §3, step 2](FM1-firmware-landscape.md)). Backups contain on-device songs and sounds, so treat them as user data.
2. **Back to stock V15** before moving to a custom firmware. Do not flash custom over custom ([landscape §3, step 3](FM1-firmware-landscape.md)). Source the V15 package from the official V15/V16 packages ([Quixotic7/FM1Backups](https://github.com/Quixotic7/FM1Backups)). Check its SHA-256 before use.
3. **Install the target** through its browser installer in Chrome or Edge over USB. Felucca's installer is [hugelton.github.io/Felucca](https://hugelton.github.io/Felucca/) ([Felucca README L11](https://github.com/hugelton/Felucca/blob/v1.8.3/README.md)). The installers for Melodee, SLOOP and sloopDX were not checked in this plan, so their install route is UNVERIFIED here. Confirm it from each release page before running step 3 on that unit.
4. **Verify**:
   - power-cycle the unit;
   - set a value, then power-cycle again and confirm it survives (the chip test, [landscape §3, step 5](FM1-firmware-landscape.md));
   - confirm the USB-MIDI device name appears and the updater still sees the unit.
5. **Record** the firmware version, chip ID and the outcome of the power-cycle test for that unit.

### 3. Hold rules

- **Do not run two units at once.** One unit per session, so a failed write can be reversed.
- **Do not flash custom over custom.** Always go back to stock V15 first.
- **Do not install m-bootloader** in this round. The landscape doc recommends it only after all four units are backed up and verified ([landscape §3.1](FM1-firmware-landscape.md)).
- **Stop on any failed power-cycle test** and keep that unit as it is until the cause is known.

## What this plan does not cover

- The adapter wiring for the thru box. That is a bench check, not a flash step ([candidates report, section c, item 1](FM1-16-voice-rig-candidates.md)).
- The block-isolation test across all 16 channels, which needs the rig wired.
- Whether the sha256 values in the catalogue match the packages. They were not checked against files ([candidates report, method](FM1-16-voice-rig-candidates.md)).

## Flash log: A (2026-11-25)

Pre-write backup: `~/fm1-backups/2026-11-25-fm1-a/fm1-20261125-130726.bin`, sha256 `f45535017b3d2de40f71eaa75f07854fea5eb2db43b32cdaabdaa202b0fc2b1a` (also in `archives/units/`). A was read from its own scsi device (`/dev/sg0`, port `1-1.1.3.3.4`) before each write.

| Step | Result |
|---|---|
| Felucca 1.8.3 package check | `~/fm1-firmwares/felucca-1.8.3.fwsc`, 610,286 bytes. sha256 `49af224d…34ec`, matches the catalogue. |
| Unit id before the first write | `1111aaaa00000001`, chip `0x856014` (old). Matches the registry. |
| Write stock V15 | Written to `0x04000-0x92FFF`, chip CRC32 verified. M-Flasher saved `~/M-Flasher/backups/fm1-20261125-134221.bin` first. A restarted on the V15 USB id `4c4a:c755`. |
| Write Felucca 1.8.3 | Written to `0x04000-0x92FFF`, chip CRC32 verified, identity `FM-1_918`. M-Flasher saved `~/M-Flasher/backups/fm1-20261125-134304.bin` first. |
| Restart | A is running on USB `1209:0001`. |

**Still to do on the unit.** The MIDI block has to be set on the unit itself: MENU > MIDI > MIDI IN, set to CH1-4 (from [Felucca's `seq.c` L63–68](https://github.com/hugelton/Felucca/blob/v1.8.3/firmware/src/seq.c)). That setting is not verified on this unit. The first setup on A should check it with a note on channel 1 and channel 5.

**Not verified here.** Whether the 1.8.3 settings persist across a power cycle. The chip save test isn't needed on A, which is the old chip.

## Target change: all four on Felucca 1.8.3

The target set above (Felucca, Melodee, SLOOP, sloopDX) was wrong for the goal. Felucca 1.8.3 has all four channel blocks in one firmware (CH1-4, CH5-8, CH9-12, CH13-16, [`seq.c` L63–68](https://github.com/hugelton/Felucca/blob/v1.8.3/firmware/src/seq.c)). The goal is one family with a similar UI, so all four units run Felucca 1.8.3, and each unit's MIDI IN is set to its own block on the unit.

| Slot | Block | Firmware now |
|---|---|---|
| A | CH1-4 | Felucca 1.8.3 |
| B | CH5-8 | Felucca 1.8.3 |
| C | CH9-12 | Felucca 1.8.3 |
| D | CH13-16 | Felucca 1.8.3 |

## Flash log: C and D (2026-11-25)

Both were written in the same order as A: stock V15 first, then Felucca 1.8.3. Each write was verified by the chip CRC32, and each unit was restarted and back on USB as "Felucca" (`1209:0001`).

| Slot | Pre-write backup | V15 write (M-Flasher save before write) | Felucca 1.8.3 write (M-Flasher save before write) | Identity |
|---|---|---|---|---|
| C | `~/fm1-backups/2026-11-25-fm1-c/fm1-20261125-130700.bin` | `fm1-20261125-140507.bin` | `fm1-20261125-140551.bin` | `FM-1_918` |
| D | `~/fm1-backups/2026-11-25-fm1-d/fm1-20261125-130540.bin` | `fm1-20261125-140633.bin` | `fm1-20261125-140711.bin` | `FM-1_918` |

D's sloopDX state at the time of the switch is in the sloopDX baseline and after-tests backups listed above.

**Still open.** Each unit's MIDI IN still has to be set on the unit, and no block on B, C or D has been tested with notes yet.

## Flash log: B (2026-11-25)

Pre-write backup: `~/fm1-backups/2026-11-25-fm1-b/fm1-20261125-130636.bin`, sha256 `46819df5…9d490`. B was read from its own scsi device (`/dev/sg0`, port `1-1.1.3.3.3`) before each write. Its unit id was `1111aaaa00000002` (chip `0x856014`, old) and matches the registry.

| Step | Result |
|---|---|
| Write stock V15 | Written to `0x04000-0x92FFF`, chip CRC32 verified. M-Flasher saved `~/M-Flasher/backups/fm1-20261125-140847.bin` first. B restarted on the V15 USB id `4c4a:c755`. |
| Write Felucca 1.8.3 | Written to `0x04000-0x92FFF`, chip CRC32 verified, identity `FM-1_918`. M-Flasher saved `~/M-Flasher/backups/fm1-20261125-140933.bin` first. |
| Restart | B is running on USB `1209:0001`, name "Felucca". |

## Block test, all four units (2026-11-25): passed

Each of the 16 channels got one note (note 60, 0.8 s) from the Pi over USB MIDI, in the order 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15, 4, 8, 12, 16. Every note went to all four units, like the thru box would. Each unit's MIDI IN had been set on the unit to its own block: A CH1-4, B CH5-8, C CH9-12, D CH13-16.

**Result, by the operator's ear and meters:** it works. Each note sounded on the unit that owns that channel's block.

**What this covers and what it doesn't:**

- It covers the USB path. The thru box and TRS-A input, and the adapter wiring, are not tested.
- The settings have not been saved. The next step is RESTORE LAST ON and a PROJECT SAVE on each unit, then a power-cycle check.
- Clock follow and note-off behaviour were not tested.
- It is one pass, by ear. The earlier single-unit readings on A were inconsistent and are not relied on here.

## Weakest assumption

That M-Flasher's backup and restore route works on all four units in the same way. The sources describe the restore path for V15 and V16 ([landscape §3.1](FM1-firmware-landscape.md)), but none of them has been tried on these four units. The first backup on unit D is the test for this.
