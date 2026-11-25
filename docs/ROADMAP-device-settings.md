# Roadmap item: per-unit settings from the fleet manager (MIDI channel and similar)

**Status:** investigated 2026-11-25 on branch `phase-d/p1`, read-only. **Not built.** No setting
was written to any unit. Build of settings writes waits for approval of the approach (see
Decisions).

## The idea

A fleet of identical units only works if each one has its own MIDI channel. The fleet manager knows
each unit by sticker letter, so it could set "A = channel 1, B = channel 2 …" and similar per-device
settings, instead of the operator doing it on each unit.

## Findings

Each point is marked **VERIFIED** (from source, bench logs or bytes, named) or **UNVERIFIED**.

### 1. Where Felucca keeps its MIDI channel. **VERIFIED from source (GPL-3.0, `hugelton/Felucca`, tag `v1.8.2`).**

- **There is no single "unit MIDI channel" in Felucca.** The MIDI IN routing is `G_ROUTE`, a project
  global with six modes:
  - 0 CH1-4 (channels 1..4 play tracks 1..4), 1 SEL, 2 CH5-8, 3 CH9-12, 4 CH13-16,
  - 5 TRACK: each track has its own channel, `song.mch[k]` (0..15 = channels 1..16, 16 OMNI, 17 OFF).
  - Source: `firmware/src/seq.c` 63-75 and 1269-1276; `project.c` 130, 162-166, 585-615;
    `web/EDITOR_PROTOCOL.md`, "Global ids".
- **Storage is the project, not the settings record.** `G_ROUTE` and `song.mch` are saved with the
  project (autosave). `settings_persist.c` holds palette, lowcut, zoom, panel and favourites, and has no
  MIDI field. So a Felucca channel travels with its project: restoring a backup brings back that
  backup's channels.
- **Version.** The tag `v1.8.2` (the 1.8.2 package in `realtest/`) contains the `T1 CHANNEL` string in
  `menu_items.c` and the `MENU_SET` command in its protocol doc (checked with `gh api` at `ref=v1.8.2`). The
  protocol doc was read from `main`, and the features are dated 1.5.5 and earlier, so they are in 1.8.2.

### 2. Does Felucca accept a channel change over MIDI while running? **DOCUMENTED, NOT VERIFIED on hardware.**

- The editor protocol is SysEx, `F0 7D 46 4C <cmd> <args> F7`. **MENU_SET (cmd 73)** takes `id, value
  (v14)` and replies `rc, id, value`. v14 is two bytes, least significant first, holding value + 8192.
- The relevant ids: **21** MIDI IN (`G_ROUTE`, the project's), **26..29** T1..T4 CHANNEL (the project's
  `song.mch`).
- `rc`: 0 applied and saved, 3 applied in RAM only, 4 applied and saved after STOP, 1 unknown id.
- **Writes with rc 0 or 4 go to flash** (`settings_save` or the project autosave); rc 3 is RAM only. Any
  use that returns rc 0 or 4 is a flash write and needs the same rules: backup first, confirmation, log,
  readback.
- **Hardware status.** The protocol doc lists commands 1-15, track selection, dumps, step reads and
  parameter writes (27, 29, 30, 31) as checked on hardware. MENU_SET (72, 73) is not in that list.
- No other route found. CC and Program Change are for playing. MIDI LEARN maps CCs to parameters, not
  channels.

### 3. How the four units differ. **VERIFIED from the Pi's flash and rename logs (2026-11-23).**

| Sticker | Name on the panel | Firmware last written | Notes |
|---|---|---|---|
| A | BLACK | grimm 2.0.2 | catalogue base: `dinghy` |
| B | DARK | ghoulbox 2.4 | own repo |
| C | LIGHT | jiant V10.7.81 | written 2026-11-23 11:09, no `bt.bin` (see installer roadmap) |
| D | WHITE | SLOOP 2.8.5 | written 2026-11-23 11:14 |

- **None of the four runs Felucca now.** The memory note that A-D run Felucca is out of date.
- Per firmware family, what is known about channel settings:
  - **Felucca 1.8.2:** MENU_SET documented (point 2).
  - **SLOOP 2.8.5:** its own model, per-track channels, GLO → MIDI CH1-4, with an OTHER rule
    (`archives/units/REGISTRY.md`). The repo has 25 code hits for "midi channel". The settings protocol
    was not read. **UNVERIFIED.**
  - **ghoulbox, jiant:** code search of the default branch found no MENU_SET and no channel matches.
    A search index is partial, so this is not proof of absence. **UNVERIFIED.**
  - **grimm:** the search could not run (the GitHub search rate limit was hit). **UNVERIFIED.**
- The fm1-dock reference (`joakimeriksson/fm1-dock`) is Dinghy-based, as grimm's base is. Its notes do not
  cover settings. **UNVERIFIED.**

### 4. Does a per-unit setting survive a re-flash? **PARTLY VERIFIED.**

- **VERIFIED (bytes).** Installs left the data regions alone. Two controlled pairs confirm it: unit C,
  stock V15 → Melodee, and unit D, SLOOP → Melodee. VM (0x93000-0xE8FFF), BTIF (0xE9000) and USR
  (0xEA000-0xFBFFF) were identical before and after. The boot area was too.
- **VERIFIED (m-flasher).** `--wipe-settings` was removed: installing firmware no longer erases data
  outside the package, as the firmwares' own installers do.
- **UNVERIFIED: whether the new firmware reads the old setting.** Felucca's `settings_import` migrates its own older
  settings records (`settings_persist.c`). Other families use other layouts (ChoralRoot's settings are
  only readable by ChoralRoot, per the registry). The expectation is that a channel set by one family is
  not read by another. Bytes surviving a re-flash is not the same as the setting working.
- **Project-level.** A project load replaces `G_ROUTE` and `song.mch` with the project's own values
  (`project.c`). That is a fact of the code, not a bench test.

## What is settable, and how (summary)

| Setting | Felucca 1.8.2 | Route | Stored in | Status |
|---|---|---|---|---|
| MIDI IN routing (`G_ROUTE`, 0..5) | yes | MENU_SET id 21 | project | documented, not verified |
| Track channels T1..T4 (0..17) | yes (needs mode 5 to matter) | MENU_SET ids 26..29 | project `song.mch` | documented, not verified |
| Other MENU settings | yes | MENU_SET | settings record | documented, not verified |
| Anything on SLOOP, ghoulbox, jiant, grimm | unknown | - | - | UNVERIFIED |

## What fleet profiles would take

1. **Profile record.** `{letter A-D, firmware family, settings: [{id, value}]}`, stored with the fleet
   state and keyed by the unit's sticker letter. It builds on the existing unit-to-letter binding.
2. **Read before write.** Read the unit's current values (MENU_DESC, cmd 72, read-only), compare with the
   profile, and show the difference. Do not write what already matches.
3. **Write as a confirmed batch.** Each MENU_SET is a flash write: confirm, back up first, check `rc`,
   read back, log to `fm1-flash.log`. One failure stops the batch.
4. **Re-apply after a firmware change.** A profile is applied again after any write that changes the
   firmware, because the bytes may survive but the setting may not be read (point 4).
5. **Per-family definitions.** Each firmware family gets its own id table. Settings shown only where
   the family is known to support them.

## Steps (proposed, none built)

1. **Done.** Read-only investigation of the Felucca source, its protocol doc and the bench logs.
2. **Needs approval.** Install Felucca 1.8.2 (or 1.8.3) on one unit as the test platform. That is a flash
   write, so it needs your go-ahead, a backup first, and the Pi's download-mode path. No unit runs
   Felucca now.
3. **Read-only hardware check.** On that unit, send MENU_DESC (cmd 72) and read the reply. No write.
   This checks the framing and the id table on hardware before anything is written. Do not send MENU_DESC
   to any other family: its behaviour is unknown.
4. **One write, verified.** A single MENU_SET on one unit, for T1 CHANNEL, with readback and log.
5. **Profiles** once one setting is reliable on hardware.

## Acceptance

- A setting is shown only when the unit's firmware family is known to support it.
- A setting write is confirmed, logged to `fm1-flash.log`, and read back to check it took.
- No setting write can leave a unit in download mode or alter its flash image.
- A profile is applied as a list of confirmed writes, and any failure stops the rest.

## Decisions needed

1. **Channel scheme.** Felucca's TRACK mode (each track its own channel, T1..T4), or block routing (CH1-4,
   CH5-8, ...) with one block per unit? This decides what "A = channel 1" means on the device.
2. **Platform.** Standardise on Felucca-family firmware for channel settings? That means changing unit
   A-D's firmware, which is a write, so it is your call.
3. **Test install.** Approve installing Felucca 1.8.2 (or 1.8.3) on one unit so step 3 can run.
4. **Approach.** Approve MENU_SET over SysEx for Felucca-family units, under the flash-write rules above.
