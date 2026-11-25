# Roadmap item: firmwares that need their maker's installer

**Status:** option 1 built on branch `phase-d/p1` (2026-11-25), not merged and not deployed to the
Pi. The investigation was read-only and nothing was written to any unit. Option 2 is not built.

## The problem

`m-flasher firmware` writes a package's flash image (the app region, 0x04000-0x92FFF) and nothing
else. Some firmwares also ship a resource region (factory sounds, a Bluetooth program, presets)
that only that firmware's own installer writes. Flash such a package with `m-flasher` alone and
the unit boots and reports the missing part on its own screen: "Setup unfinished / Factory sounds
missing" (Melodee), "Presets Missing" (ChoralRoot).

Detection exists: `fm1-switch inspect` flags packages that carry a sound bank (`packages.py`,
`_factory_sound_names`), and the panel marks those rows "NEEDS INSTALLER". Evidence for the flash
layout is in [FM1-firmware-install-model.md](FM1-firmware-install-model.md).

## Findings

Each point is marked **VERIFIED** (measured in bytes, in code, or on the bench, with the test or
source named) or **UNVERIFIED** (stated as a hypothesis or not checked).

### 1. Is FLASH blocked on a NEEDS INSTALLER row? **VERIFIED: not blocked. Only warned.**

- The panel only labels the row. `fleet.js:375` sets the "NEEDS INSTALLER" mark from `p.severity`.
  `flashButton` (`fleet.js:238`) gates on the unit's state, not the package.
  The confirm screen (`fleet.js:695`) shows only the chip-compatibility note, never the install verdict.
- The server does not read the verdict either. `web_writes.flash_unit_by_id` checks the confirmation
  (unit id), that the package is in the library, and its sha256. It then calls `_run_guided`, which
  calls `guided.flash`. Neither consults the severity.
- Test: `fm1-switch/tests/test_phase_d_findings.py::test_needs_installer_package_is_not_refused_by_the_flash_path`.
- Live, on the Pi: `~/fm1-flash.log` records **jiant-V10.7.81** (verdict warn) written to unit C at
  2026-11-23 11:09 and **melodee-1.6.1** (verdict warn) written to unit D at 11:12, both `installed`.

So the roadmap's earlier wording, "the panel warns and stops", is wrong. The panel warns and writes.

### 2. What state is a unit in after a partial install? **PARTLY VERIFIED.**

Verified in bytes, from controlled pairs of backups (`inspect`-style region diff, 1 MiB flash):

| Pair | Before | After | Changed | Unchanged |
|---|---|---|---|---|
| Unit C, Melodee | `archives/units/fm1-20261120-171537.bin` (stock V15) | `…173052.bin` | app 0x04000-0x92FFF, tail record 0xFC000-0xFD42F | boot 0x00000-0x03FFF, VM, BTIF, USR |
| Unit D, Melodee (2026-11-23) | Pi `auto/fm1-20261123-111209.bin` | Pi `auto/…111351.bin` | app 0x04000-0x92FFF, tail records 0xFC000-0xFEFE7 | boot, VM, BTIF, USR |

- The Melodee resource bytes are **not** on the unit after the install. The string `BRASS 1`
  (Melodee's res.bin begins with 35 such names) occurs twice in the D pair before and twice after.
  Both occurrences are at 0xEC076 and 0xF0076, in the USR region, and are stock V15 content.
  No 512-byte window of any Melodee, ChoralRoot or SHRUB resource payload occurs in any local backup.
- The tail record is **not** install-specific. Across the Pi's backups it is non-blank in some states
  of Felucca, SHRUB, OberVave and SLOOP, and blank in others, including stock and some states of each
  of those. So it tracks use, not the install. Its writer is **UNVERIFIED**; the likely reading is that
  the running firmware writes it. (The sidecar `firmware` labels are not reliable: the Pi's own label
  for the D Melodee-state backup reads "Felucca".)
- Observed on the bench, not measured in this pass (from `archives/units/REGISTRY.md`, 2026-11-21):
  - Unit C with Melodee booted and showed "Setup unfinished / Factory sounds missing".
  - Unit A with ChoralRoot booted and showed "Presets Missing". A second ChoralRoot install
    answered "Nothing to write: the flash already holds this". Installing Felucca 1.8.2 over it
    cleared the message.

The state, in plain terms: the flash is intact (chip CRC32 matched), the unit boots, and the
firmware reports the missing resource on its own screen. Nothing in flash is a clean "setup
unfinished" flag that the fleet manager can read.

**UNVERIFIED:** what the firmware checks at boot (a flash flag, or only whether the region is present);
whether the message survives a power cycle with the region still absent (the 2026-11-21 observations
were taken at power-on, so it is likely); and where the SOUNDS region sits, which m-flasher's flash
map names but whose address is not established.

### 3. Which firmwares carry resource regions that `m-flasher firmware` does not write? **VERIFIED for the list. Writers in point 4.**

Parsed read-only with `fm1_switch.packages.parse`, on `realtest/` and `archives/V15-FM-1.fwsc`.
Resource magic is the first four bytes of the payload.

| Package | Extra files beyond flash.bin, ota.bin | Magic | Sound-bank names | Verdict | Bench behaviour |
|---|---|---|---|---|---|
| Felucca 1.8.2 | none | - | 0 | ok | installs clean |
| SLOOP 2.8.5 | none | - | 0 | ok | installs clean |
| OberVave 3.5.2 | none | - | 0 | ok | installs clean |
| SHRUB (FM-1_901) | `res.bin` 73,728 B | not checked | 0 | ok | runs with no message (2026-11-21) |
| V15 (stock) | `USR` 73,226 B, `isd_config.ini`, `blimit.bin`, `script.ver`, `tail.bin`, `ota.bin` | - | 1 (a stray) | ok | stock firmware |
| Melodee 1.6.1 | `res.bin` 57,344 B | `MRS1` | 35 | warn | "Factory sounds missing" |
| ChoralRoot 0.42 | `res2.bin` 16,384 B | `CRE2` | 7 | warn | "Presets Missing" |
| JIANT V10.7.81 (Pi only) | `bt.bin` 73,692 B | `JBT1` | 9 | warn | installed on unit C 2026-11-23; screen shows "BluetoothMissing (please reinstall)" (**VERIFIED**, owner, 2026-11-25) |

Two notes:
- SHRUB carries a 72 KiB `res.bin` that m-flasher does not write, and it runs with no message. So a
  file being present is not the same as the firmware needing it. The name-based warning in
  `packages.py` is a proxy, and SHRUB shows its limits.
- **VERIFIED on the bench (owner, 2026-11-25):** unit C, running JIANT without `bt.bin`, shows
  "BluetoothMissing (please reinstall)". So the missing resource region is visible on the unit,
  and the flag design matches what the unit itself reports. That `bt.bin` is the Bluetooth program
  follows from the message; the writer is still **UNVERIFIED**.

### 4. Does m-flasher expose any way to write those regions? **VERIFIED: no. One indirect path exists, and it is not acceptable.**

Subcommands, from `m-flasher --help` on the Pi (0.12.1): `find`, `download-mode`, `restart`, `info`,
`backup`, `restore BACKUP.bin` (whole flash image), `firmware PACKAGE` (app region),
`bootloader` (boot area), `run-ram` (runs a program in RAM, refuses the flash erase and write
opcodes in its own `--call` list).

- No subcommand writes a resource region alone.
- `restore` of a whole-flash image is the only path that can write it. The messages say a restore
  from another unit's backup keeps that unit's own factory data (calibration, Bluetooth address).
  It would also rewrite user data. No complete source image exists: no local or Pi backup
  contains a Melodee or ChoralRoot resource. So this path is not usable and is not recommended.

**Melodee's own writer (source, GPL-3.0-only, `keremimo/melodee`, not verified on hardware).**
`tools/fm1_resources.py` pushes the region over the same SysEx editor framing Felucca uses
(`F0 7D 46 4C <cmd> … F7`), command 82:

1. `82 [0]` status: reply `rc, installed, length, crc` (12 bytes). The tool refuses if length or crc
   do not match the running build, so the file must be this build's resources.
2. `82 [1]` begin.
3. `82 [2, offset (3 × 7-bit), 256 bytes packed 7-bit]` write, repeated.
4. `82 [3]` commit, then status again: `installed` and the crc must match.

The file's header is `MRS1`, a length and a CRC32, as the tool checks. The package's `res.bin` has
exactly that header (magic 0x3153524D, length 53,844, crc 0x2035D335). So the Pi could in principle
send the same sequence on the ALSA raw MIDI node it already uses for the soft key.
**UNVERIFIED:** that the command works on hardware, what each error code means on the bench, and the
resource's flash address (the firmware decides it).

**ChoralRoot (`CRE2`) and JIANT (`JBT1`): writers not identified.** Their installers were not
read in this pass.

### Summary table

| Point | Status |
|---|---|
| FLASH blocked on NEEDS INSTALLER? | VERIFIED: not blocked (code and live log) |
| Partial install leaves the flash intact? | VERIFIED for the app and data regions; the tail record is UNVERIFIED |
| Partial install leaves the unit reporting the gap? | OBSERVED on the bench 2026-11-21; not re-measured |
| Which packages carry an unwritten region? | VERIFIED (table, point 3) |
| Does m-flasher write any resource region? | VERIFIED: no (only the whole-flash restore, not usable) |
| Melodee's writer works on hardware? | UNVERIFIED (documented in source only) |
| ChoralRoot and JIANT writers? | UNVERIFIED (not identified) |

## Options

1. **Two-step handoff with a per-unit "setup unfinished" flag (recommended).**
   - Mark the firmware as two-step on the list and on the confirm screen, with the operator's
     second step written out (see the design below).
   - After the write, record `setup_pending` for that unit in the fleet state. Show "SETUP
     UNFINISHED" on the unit, not "READY", until the operator confirms the second step.
   - Works for every NEEDS INSTALLER package, because it does not depend on the writer.
   - Limitation: the second step needs a laptop with Chrome or Edge and USB, or the maker's own
     installer, so it is not a Pi-only job.
2. **Write the extra region from the Pi (Melodee only, for now).**
   - Feasible for Melodee by the source above. Needs owner approval, a backup first, a readback
     through the status command, and a trial on a spare unit.
   - ChoralRoot and JIANT are blocked until their writers are read. Each would need the same read-only
     protocol investigation first.
3. **Policy only.** Hide these firmwares from the fleet flow, or label them "use the maker's
   installer" with no flash button. This is the fallback.

## Recommendation

**Build option 1 now.** It covers all NEEDS INSTALLER packages, it does not depend on any
unverified protocol, and it fixes what the flash log shows is happening today (two two-step
packages written with no second step recorded).

Keep option 2 for Melodee as a separate later item. Do the read-only protocol work for ChoralRoot and
JIANT before deciding anything about them.

## Option 1: as built (phase-d/p1)

Approved 2026-11-25. Built as designed, with these choices:

- **Flag.** `setup_pending` on the unit's record in `fm1-units.json`: `{package, sha256, since}`. Set by
  `web_writes.flash_unit_by_id` when the package's verdict is not `ok`. That includes a write that failed
  part way, since the unit may be partly written. An unreadable package also counts as two-step (the
  conservative reading). The unit is never shown as READY while the flag is set.
- **Replacement (decision 2, default taken).** An ordinary (`ok`) write clears the flag and logs
  `setup-replaced`. A failed ordinary write leaves it alone.
- **Restore.** A restore from a backup does not touch the flag. This is a choice, not a verified outcome:
  a restore brings back a whole-flash image whose state may differ from the flagged firmware.
- **Confirm.** `POST /api/setup/finish` with `confirm` equal to the flash id. It clears the flag, logs
  `setup-finished`, and writes nothing to the unit. It is refused when nothing is pending.
- **Panel.** The state text is SETUP UNFINISHED, drawn as an attention card. The unit screen names the
  second step and offers FINISH SETUP. The flash confirm on a two-step package lists the two steps, and
  one replacing an unfinished setup says so. The summary counts UNFINISHED.
- **Not built: the link and QR.** The hub entries do not carry the maker's installer URL, so the panel
  names the second step in words only. A QR code needs a generator that is not in the repository.
- **Tests.** `tests/test_setup_pending.py` (13) covers the registry record, the write path (including
  failed writes), replacement and the confirm. Four JS tests in `tests/js/fleet.test.mjs` cover the state
  text, the finish confirm and the two-step copy. Two existing flash-log expectations gained a
  `setup-pending` line, because their fake package is unreadable.

## Proposed design for option 1 (as originally proposed; kept for the record)

- **State.** Add `setup_pending` to the unit's record in `fm1-units.json`: `{package, sha256, since}`.
  Set it on the server when a write goes through and the package's verdict is `warn`, so the panel
  cannot forget it. Clear it only by an explicit "Setup finished" confirm that names the unit's
  sticker letter, and log it to `fm1-flash.log`.
- **Replacement.** Writing a different package replaces the flag, since the flagged firmware is no
  longer on the unit. This is the rule to confirm (decision 2 below).
- **Panel.** On a `warn` package, the confirm screen says "Two steps: write here, then finish with
  the maker's installer (link and QR)". A unit with `setup_pending` shows "SETUP UNFINISHED" on its
  row and cannot show READY. Its action is "I finished setup".
- **Tests.**
  - Python: a write of a `warn` package sets `setup_pending`; a write of an `ok` package does not;
    the flag clears only with a matching sticker confirm; a different write replaces it.
  - JS: a unit with `setup_pending` never renders READY; the confirm copy names both steps.
  - Keep `test_needs_installer_package_is_not_refused_by_the_flash_path` as the premise: the verdict
    stays a warning, not a gate.

## Acceptance

- A two-step firmware can never be shown as "done" on the panel until the second step is confirmed.
- The operator is told, in the panel, exactly what the second step is.
- If option 2 is built: a resource write is verified by the writer's own status readback (the
  installed flag, length and crc), logged to `fm1-flash.log`, and covered by the same backup-first
  and confirmation rules as any other write.

## Decisions needed

1. Approve option 1 as the build, with the design above.
2. For the flag: does writing a different package clear `setup_pending` (proposed), or must the
   operator confirm each time?
3. Should Melodee's SysEx writer be trialled on a spare unit? That needs a Melodee install first, which
   is itself a flash write, and so needs your go-ahead.
4. Unit C (sticker LIGHT) runs JIANT without `bt.bin` and shows "BluetoothMissing (please
   reinstall)" (answered 2026-11-25). Unit D (WHITE) now runs mutable 2.4.

## Update 2026-11-25: how JIANT writes its Bluetooth part (read from source, nothing run)

**VERIFIED from source** (`juanjiant-bit/JIANT`, GPL-3.0, commit `0821a79`; files `web/fm1ota.js`,
`web/fm1diag.js`, `tools/fm1_install.py`). I read it and spot-checked the key lines. Nothing was
run on a unit.

- **JIANT's install is not a flash image plus a separate resource write.** The maker's installer
  updates a *running* unit over USB-MIDI SysEx, in two steps where "the device asks, we answer":
  step 1, the running firmware reads parts of the package and starts an **update loader**; step 2,
  the loader reads the **whole package** and writes it. The package's third entry is the Bluetooth
  part (`bt.bin`, magic `JBT1`). The host only serves the bytes the device asks for.
- **That is why our install left C without it.** `m-flasher firmware` writes through download mode
  and never runs JIANT's own loader. The maker's web installer says so in a comment: another
  loader "would write JIANT's app alone: the FM-1 then says BLUETOOTH PART MISSING". The same
  comment and `btCheck` say the fix is to install once more **from a running JIANT**: JIANT's own
  loader (`ota-FM-1_900`) writes the Bluetooth part, and sectors that already match are skipped.
- **A Python tool for this already exists:** `tools/fm1_install.py`. It installs a `.fwsc` over
  USB-MIDI (`fm1_install.py PACKAGE.fwsc [--port NAME]`; `--info` only asks for the unit's
  identity), needs `mido` and `python-rtmidi`, and runs on the Pi. It follows the same protocol as
  the web installer. A grep found **no Bluetooth check** in the CLI (the web page does the check),
  so whether the CLI alone clears BluetoothMissing is **UNVERIFIED**.
- **Status query (read-only):** the unit's INFO reply carries a tag `58 01 s` (`s` = 1 present, 2
  missing); older firmware uses VEL command 81, op 4. Framing: identity query
  `F0 00 32 45 00 00 00 40 7F F7`, upgrade trigger `F0 22 24 35 7F F7`, 512-byte chunks, 7-bit
  packing. No internet is needed; the web installer needs Chrome or Edge with Web MIDI.
- **Not found:** the firmware's boot check that shows the message, and where `bt.bin` is built
  (probably Jieli's AC79 SDK at build time).

**What this changes.** For JIANT and its forks (Rhythmer, cursed os, built on JIANT) the Pi can run
the maker's own update itself, so no laptop is needed. This is "option 2" in a safer form: the
Pi serves the package to the device's own loader and writes no resource region by hand. Melodee
and ChoralRoot are still **UNVERIFIED**; their writers differ (Melodee: SysEx command 82).

**Bench result, 2026-11-25 (read-only):** the maker's `fm1_install.py --info` runs on the Pi
(venv at `~/fm1/vendor/jiant/`, needs `libasound2-dev` and `python3-dev` to build `python-rtmidi`).
Run once per ALSA port (`--port " 20:0"`), it answers for all four units while running:
A `FM-1_966` (port 28:0), B `FM-1_900` (24:0), **C `FM-1_909`** (20:0, JIANT; matches the hub's
`product` for JIANT), D `FM-1_710` (16:0, mutable). **VERIFIED.** Without `--port`, `--info` shows
only the first unit that answers. It does not read the Bluetooth tag (`58 01 s`); that is a
separate read-only INFO request, **UNVERIFIED** and not yet sent.

**Bench result, 2026-11-25: the Pi fixed unit C. VERIFIED on hardware.**
- Read-only INFO to C (editor SysEx `F0 7D 46 4C 01 F7`) reported JIANT V10.7.81 with Bluetooth
  tag `58 01 02` (missing). The fleet manager's manual backup of C's flash was taken first
  (1 MiB, `~/fm1-backups/manual/fm1-20261125-102547.bin`).
- `fm1_install.py jiant-V10.7.81+de2922f2.fwsc --port " 20:0" --yes`, run on the Pi against the
  running JIANT, completed with exit 0 ("the FM-1 runs FM-1_909"). Same package as before, so
  only the missing part changed.
- Afterwards the same INFO read reports tag `58 01 01` (present). **No laptop was used.** The
  panel's flag for C was still set, as expected: nothing links the install to the flag yet.
- **VERIFIED (owner, 2026-11-25):** C's screen comes up with no BluetoothMissing error.
- Still **UNVERIFIED**: Melodee, ChoralRoot and the other forks. The Pi showed under-voltage flags (`0xd0000`)
  before and after; they did not change, and a powered hub is still advised for writes.

**Next, in order:**
1. (done, see above) Read-only on the Pi: install `mido` and `python-rtmidi` in a venv, copy `fm1_install.py`, run
   `--info` against unit C. This shows the unit's identity and Bluetooth tag and writes nothing.
2. With the owner's go-ahead (it is a flash write): back up unit C, then run the maker's install
   of `jiant-V10.7.81` (the package already on the Pi) from the running JIANT, and confirm the tag
   changes to 1 and the screen no longer says BluetoothMissing.
3. If that works, build a "FINISH SETUP FROM THE PI" step into the panel for JIANT-family
   packages, run as a separate process, and clear the flag only when the Bluetooth tag reads
   present. Do not copy the CLI into this repo without checking that its GPL-3.0 licence fits.

## Update 2026-11-25: how Melodee writes its resources (read from source, nothing run)

**VERIFIED from source** (`keremimo/melodee`, GPL-3.0; a Haiku agent read it and I spot-checked
`tools/fm1_resources.py`). Nothing was run on a unit.

- **Two steps, both on a running unit over USB-MIDI.** Like JIANT, the package goes through
  the Felucca update loader (`ota-FM-1_900`; the stock loader is refused for Melodee packages), and
  `res.bin` travels in the package as an entry the loader writes. Then a separate **command 82**
  step checks the resource region and writes it only if it is not current.
- **Command 82 framing:** `F0 7D 46 4C 82 <args> F7`, no checksum on requests. Args: `[0]` status,
  `[1]` begin (erases the region, up to 30 s), `[2, off0, off1, off2, packed ≤256 bytes]` write,
  `[3]` commit (10 s). Reply: 12 payload bytes (`rc`, `installed`, length and crc as 5 × 7-bit
  little-endian). `rc` 0 ok, 1 bad request, 2 no flash, 3 flash error, 4 busy (stop playback,
  Bluetooth off), 7 check failed. 7-bit packing: a mask byte plus seven bytes.
- **Read-only status query:** `F0 7D 46 4C 82 00 F7`. The reply says whether this build's
  resources are installed and gives this build's expected length and crc.
- **`res.bin` header (MRS1):** magic `0x3153524D`, payload length, zlib CRC-32, header CRC;
  payload starts at byte 32.
- **A Python tool exists:** `tools/fm1_resources.py` (`mido` + `python-rtmidi`) and
  `tools/fm1_install.py`, which runs the resource step when `melodee-res.bin` sits beside the
  package. The web installer fetches that file over the internet; the CLI needs it locally.
  **The `.fwsc` package alone does not carry a separate `melodee-res.bin`, so the file's origin on
  the Pi is UNVERIFIED.**
- Still **UNVERIFIED on hardware:** all of the above. No unit now runs Melodee, so a trial needs a
  Melodee install first (a flash write, with your go-ahead and a backup).

## Update 2026-11-25: how ChoralRoot writes its presets (read from source, nothing run)

**VERIFIED from source** (`Quixotic7/ChoralRootFM1`, GPL-3.0; a Haiku agent read it and I spot-checked
the "PRESETS MISSING" lines). Nothing was run on a unit.

- **One step, the same Felucca update protocol as JIANT.** The running firmware starts the update
  loader; the loader reads the **whole package** from the host and writes it. `res2.bin` is an
  ordinary package entry (type 200) that the **loader itself writes** to flash `0xDC000..0xE0000`
  (a 16 KB region) before the commit marker. There is **no separate SysEx resource command**
  (no equivalent of Melodee's command 82).
- **The maker's own notes agree:** `docs/releases/0.42.md` says M-Flasher "writes the firmware
  alone: the unit starts with PRESETS MISSING until ChoralRoot's own installer has run once".
  The 0.46 release notes do not repeat the caveat.
- **A Python tool exists:** `tools/fm1_install.py` (Python 3, `python-rtmidi`). Loader ids it
  accepts match `ota-(FM-1|…)_[789]xx`. ChoralRoot identifies as `FM-1_920`. No Raspberry Pi or ARM
  test was found, so Pi support is **UNVERIFIED** (JIANT's near-identical tool did run on the Pi).
- **`res2.bin` header (CRE2):** 64 bytes, little-endian: magic `0x32455243`, format 1, data length,
  CRC-32 of the data, the commit marker's body CRC, then a header CRC over the first 60 bytes.
- **No presets status query found** (**UNVERIFIED**). The firmware only shows PRESETS MISSING on
  the splash screen. The backup reply code (`cr_backup.c`) is a candidate and was not read.
- The repo's installer is `web/index_pkg.html` (Chrome or Edge, Web MIDI with sysex).

## Summary of the three writers (2026-11-25)

| Firmware | Where the extra region goes | Tool to run on the Pi | Status query | On hardware |
|---|---|---|---|---|
| JIANT | package entry, written by JIANT's update loader (`ota-FM-1_900`) | `tools/fm1_install.py` | INFO tag `58 01 s` | **VERIFIED** on unit C |
| ChoralRoot | package entry (`res2.bin`), written by the update loader | `tools/fm1_install.py` | none found | UNVERIFIED |
| Melodee | package entry, then command 82 if not current | `tools/fm1_install.py`, `fm1_resources.py` | command 82 status `82 00` | UNVERIFIED |

**What this means for the build.** All three makers' installers use the same family of protocol,
and the JIANT path is proven. A single "finish setup from the Pi" step can run the maker's own CLI
per firmware family, behind a flash-id match, a backup, and the existing guards. Each firmware
still needs its own trial before the button is offered for it, and the status checks differ.

## Built 2026-11-25: INSTALL FROM THE PI (JIANT only), on branch `phase-d/p2`

A unit with an unfinished JIANT setup now shows **INSTALL FROM THE PI…** next to FINISH SETUP.
Guards, in order: the confirm names the unit's flash id; the unit has an unfinished setup; the
package family was tried on real hardware (`maker_install.FAMILIES`, JIANT only); the maker's tool
is present on the Pi; the package in the library still matches the recorded sha256; every unit is
running (none off, none in download mode); the unit's ALSA port is found from its USB path; the
running unit's own INFO reply names the family. Then a whole-flash backup is saved, the maker's
installer runs as a separate process, and the flag is cleared **only if** the unit then reports
its Bluetooth part present. Otherwise the flag stays and the failure is logged. Melodee and
ChoralRoot are not offered until each has had one bench trial.
Not yet exercised by the panel on hardware (the same steps were run by hand on unit C): **UNVERIFIED**.
Tests: `tests/test_setup_from_pi.py` (18), two JS tests.

**Merged and deployed 2026-11-25.** `phase-d/p2` merged into `main` (not pushed) and deployed to the
Pi (code backup `~/fm1/code-backups/fm1_switch-20261125-104414`); `fm1-web` active afterwards.
**VERIFIED (read-only):** `/api/fleet` still shows unit C's `setup_pending` for JIANT V10.7.81, since
the hand-run install on 2026-11-25 never touched the flag. Clearing it from the panel is the first
real exercise of INSTALL FROM THE PI (a flash write, needs the owner's go-ahead) or FINISH SETUP
(writes nothing to the unit).

**First panel run on unit C, 2026-11-25: refused before any write (guard held).** `POST /api/setup/install`
for C answered "the unit did not answer (no answer)". Nothing was written (`fm1-flash.log` shows only the
`refused` line). Two findings:
- **Fixed:** the registry matched a running unit only by `bcdDevice`, so a firmware change left C
  "not running" plus a phantom `?` row, which would also trip the "every unit running" guard. Running
  units are now matched first by USB serial == bound flash id (JIANT reports it). Test added.
- **VERIFIED (Pi, read-only):** C now enumerates as `4c4a:c755 "FM-1"` and the maker's `--info` says
  `FM-1_015`, not JIANT's `FM-1_909`, so the editor INFO probe gets no reply. `dmesg` shows C went
  JIANT (`1209:0001`, bcd 4.20) -> `4d4a:4155` -> `4c4a:c755` between 10:40 and 10:41. What put it there
  is **UNKNOWN** (no write in `fm1-flash.log` after the 10:40 backup). Which firmware C holds now is
  **UNVERIFIED** until the owner looks at its screen.

**First successful panel run, unit C, 2026-11-25 10:53-10:55. VERIFIED on hardware.** C was on stock
firmware, so JIANT V10.7.81 was written with `m-flasher` through the panel (10:52, flag set, probe: Bluetooth
tag 2 = missing). `POST /api/setup/install` then took a whole-flash backup (10:53), ran the maker's installer
as a separate process, and cleared the flag only after the probe read tag 1 (present). `fm1-flash.log`:
`setup-finished`. All four units running, no flag set. The owner confirmed C's screen is clean (2026-11-25).
