# Registering units, and what to do after you move the cables

Every unit on the bench has a **letter** (A, B, C, D, ...). The letter belongs to the unit, not to
a USB port. This guide explains how that works, what goes wrong when you move cables or swap a
hub, and how **RE-REGISTER FLEET** puts every letter back on the right unit.

Nothing described here writes to a unit's flash.

## Why a letter is tied to the flash id

Units plugged into one hub look the same to the Pi while they run. They often share a USB id, and
none reports a USB serial number, so "the unit on the left" has no reliable meaning in software.

Each unit has one durable identity: the **factory id in its flash chip**. It can only be read while
the unit is in download mode. So the Pi keeps a small file, the **registry** (`~/fm1-units.json`),
that records, per letter:

- the unit's **flash id** (the real identity);
- the **USB port** it was last seen on, and a **fingerprint** of how it looked while running
  (firmware, device number). These are only hints, used to recognise a running unit quickly
  without putting it into download mode.

A write always goes to the unit whose flash id was **just read** and matched. A hint never
authorises a write.

## First time: registering a unit

Do this once per unit, when the unit has no letter yet.

1. Put **one** unit that has no letter into download mode. (A unit that is running firmware with
   USB MIDI can be put there by the Pi itself with the soft key; see the install notes in the
   README.)
2. On the touch UI, tap **REGISTER A UNIT** (on an empty fleet) or **SETTINGS, then REGISTER**.
3. The screen reads the unit's flash id and proposes the next free letter. It never picks between
   two unclaimed units: if more than one is waiting, it tells you how many and stops.
4. Confirm. The confirmation names the id and the letter. Nothing is written to the unit.

From the shell, `fm1-switch units` shows what the Pi thinks each unit is and how sure it is.
`fm1-switch register <slot>` is the older route: it labels the one unit that powers on while you
watch, **provisionally** (no flash id is read). Confirm it afterwards with REGISTER in download
mode, which reads the flash id and makes the label real.

## After you move the cables

If you move a unit to another hub port, swap a hub, or replug everything, the saved ports and
fingerprints go stale. Units that look alike while running (same firmware, same device number)
cannot be told apart from USB. The panel then shows one or both of these:

- a registered letter showing **NOT CONNECTED**, while
- running units show as **?** (a unit with no letter).

A letter drawn with a `~` after it (`A~`) was matched only by its last-seen USB port, not by a
readable id. That is a guess, and it stops counting as proof once the unit re-enumerates (power
cycle, moved cable), so swapped cables can never silently swap letters.

When the Pi sees either situation, a bar appears **above the unit grid**:

> **FLEET NOT REGISTERED** &nbsp; [ RE-REGISTER FLEET ]

The bar is shown only when something looks wrong. A `?` unit with no missing letter is a *new*
unit: use REGISTER for that, not this.

### What RE-REGISTER FLEET does

1. Works out which running units the registry cannot place with certainty.
2. Puts those units into download mode (the soft key). The soft key can reach more units than the
   one it was aimed at, so every unit that moves is handled.
3. Reads each unit's flash id and matches it to its letter **by id**.
4. Restarts the units and **re-learns** each one's port, fingerprint and device number. From then
   on the panel can place identical-looking units again.

When it finishes, the panel shows a summary such as `matched A B C D`.

### What it will not do

- It **never writes flash.** Not a firmware, not a backup restore.
- It **never gives a new letter.** A flash id that no letter owns is reported
  (`1 unit(s) with no sticker; use REGISTER`) and left alone. Registering stays a deliberate act.
- It **leaves a unit that is already in download mode alone**: it may be there on purpose.
- It will not start while a flash is running ("a flash is running; wait for it to finish").

### What you will notice

- **Sound stops for about half a minute.** Every unit that is matched restarts. Save anything you
  want to keep on a unit first: a restart loses what is only in the unit's memory.
- **The MIDI router lets go and comes back.** If the router is running it frees the units for the
  soft key, then takes them back when the hold ends (the same as before any flash).
- The result is written to the audit log (`~/fm1-flash.log`, event `reconcile`).

### When it says something else

| Message | Meaning and what to do |
|---|---|
| `not found: C (off, unplugged, or not reachable)` | That letter's unit was not seen. Check power and cable, then run it again. |
| `N unit(s) with no sticker; use REGISTER` | A unit whose id no letter owns. If it is new, REGISTER it. If it replaced a unit that had a letter, see *If you replaced a unit* below. |
| `could not restart or re-identify: <id> (power-cycle them)` | The unit did not come back in time. Power-cycle it, then run it again. |
| `power-cycle the unit(s) on <port>` | That unit has no MIDI link to carry the download-mode key (some firmwares). Power-cycle it, or put it into download mode by hand and use REGISTER. |
| `no unit entered download mode, so nothing was matched` | The soft key reached nothing. Check that the units run firmware with USB MIDI, then power-cycle and retry. |
| `every running unit is already matched to its sticker` | Nothing to do. |

### If you replaced a unit

RE-REGISTER FLEET only re-matches letters that already hold an id. A replacement board has a new
flash id, so it counts as a new unit: it is reported as `1 unit(s) with no sticker`, and REGISTER
offers it the next **free** letter. There is no panel action yet to hand an existing letter to a
different physical unit. The panel refuses to bind an id that already has a letter, and says which.

## The same thing from the API

The panel button calls `POST /api/reconcile` with `{"confirm": "reconcile", "all": true}`. It goes
through the same Host, Origin and token checks as every other write. `"all": true` scans every
running unit, so the answer never rests on a guess. The response carries `matched`, `missing`,
`unregistered`, `stuck` and a `message`.

## Settings that change the timing

Both are on the Settings page (`Flashing and waiting`) and in `~/.config/fm1/config.json`:

| Setting | Default | What it waits for |
|---|---|---|
| `download_mode_wait` | 25 s | the units to appear in download mode after the soft key |
| `download_mode_settle` | 3 s | a pause once a unit appears (USB enumeration) |
| `restart_boot_wait` | 30 s | a unit to come back running after its restart |

## Status

RE-REGISTER FLEET was first run on the real bench on 2026-11-27 and put the units back on their
letters after a reconnect. It is also covered by tests against a fake USB bus. See
[`../CHANGELOG.md`](../CHANGELOG.md).
