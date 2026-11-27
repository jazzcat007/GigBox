# Using the FM-1 fleet manager

> **Retired (2026-11-22).** This describes the console/TUI on `tty1` (keys A–D, N, L, R),
> which was deleted in commit `814c556`. The panel is now the web kiosk at `/fleet`
> (`fm1-switch/fm1_switch/web/fleet.*`). The identity and registration concepts below are
> still correct; the keys and screens are not. Note: binding a flash id to a sticker was a
> console feature and has no replacement yet (see `docs/AUDIT-fleet-manager.md`, FEAT-01).

**What this is:** the screen that swaps firmware between your four FM-1s. It runs on the Pi's touch panel or, with no screen on the Pi, in a phone browser (headless mode).
One job, done carefully: get firmware onto the right unit.

The expert web UI (`http://<pi-hostname>.local:8080` from a phone or laptop) still exists for
browsing the catalogue, downloading and inspecting packages. The panel is for the
everyday act of switching a unit to a different firmware.

---

## The one thing to understand first

**Four identical units, in random USB ports, that must not be confused.**

While a unit is *running*, the Pi cannot tell which unit it is. Measured on your fleet:
three of the four present identically to USB as `1209:0001`, one presents as `4d4a:4155`,
and none of them reports a per-unit USB serial. That is a hardware fact, not something
the software can wish away.

What *does* identify a unit is the **factory id burnt into its flash chip**, readable only
when the unit is in download mode:

| Sticker | Flash unit id | Chip | Running `bcdDevice` |
|---|---|---|---|
| A | *(recorded on the bench)* | old `0x856014` | `0329` |
| B | *(recorded on the bench)* | old `0x856014` | `0320` |
| C | *(recorded on the bench)* | old `0x856014` | `0104` *(no MIDI)* |
| D | *(recorded on the bench)* | **new `0xB36014`** | `0328` |

So the device works in two registers, and the screen always tells you which one you are
looking at:

- **`A`, `B`, `C`, `D`** — the sticker, confirmed against the flash id. Trustworthy.
- **`A~`, `B~`, `C~`** — a *hint*, from the unit's `bcdDevice` while running. Probably
  right; not proof. The `~` is honest about that.

---

## First-time setup: teaching the Pi who is who

You do this once per unit. It takes about a minute each.

1. **Switch all four units off.**
2. On the panel, press **`N`**.
3. The status line asks which unit you are about to switch on. Press its letter,
   **`A`** to **`D`**.
4. **Switch on only that unit.**
5. Press **`Enter`** to start watching. The Pi waits up to 2 minutes.

If exactly one unit appears, it is recorded and the row shows `A~`.

If **two units appear at once**, or one appears while another disappears, **nothing is
recorded** and it says so. That is deliberate: an unattributable arrival is worse than no
record, because a wrong label here means a wrong unit flashed later.

Repeat for each unit with a different letter.

> **This is also the moment to check the label matches the box.** The Pi records
> *"this unit is the one you said was B"*. It cannot verify the sticker you read. If you
> mix them up here, everything downstream is confidently wrong. The confirmation step
> below catches it.

### Confirming a hint

Once a unit has been in download mode at least once, its row shows a plain letter with
no `~` — the flash id was read and matched, so the label is proven rather than guessed.

To confirm a `~` label: put that unit in download mode (see below) and press **`R`**.
The row should show its real firmware and chip, and the `~` disappears.

---

## Everyday use: putting a new firmware on a unit

1. The unit to change must be **in download mode**. Either:
   - press **`D`** on the panel (sends the soft key to the selected unit — this works
     while it is still running, and is the easiest route), or
   - power it off and on and let the bootloader fall back.

   Press **`R`** to refresh. The unit's row now reads `DOWNLOAD` and shows its real
   firmware and chip.

2. Press **`Tab`** to move to the firmware list, **`↑↓`** to pick the package.
   The right-hand column is its `FM-1_NNN` identity. A **`!`** means m-flasher cannot
   install it completely on its own (it carries a factory sound bank) — expect
   "Setup unfinished" or missing presets.

3. Press **`Tab`** back to the units, **`↑↓`** to select the unit.

4. Press **`Enter`**. The status bar turns inverted and says:
   *"Enter again (or A) to flash sloop-2.8.5.fwsc onto unit A"*.

5. Press **`Enter`** again, **or the unit's own letter**. The second press must name the
   unit, because a stylus resting on a resistive panel sends repeat taps — a
   "press again" that the same stationary press can satisfy is not a confirmation.

6. It writes, restarts the unit, and the row updates.

**Esc cancels.** Moving the selection or changing the firmware also disarms it. A
confirmation tied to unit A can never carry over to unit B.

---

## What the screen will not do

- **Flash a unit that is not in download mode.** It says so rather than trying.
- **Guess which unit is which.** An unlabelled unit shows `?` and the tail of its id.
- **Download or browse the catalogue.** That is the web UI's job.

---

## Reading the panel

```
 FM-1 FLEET                                    3 download · 1 likely
── UNITS ─────────────────────────────────────────────────────────
 A DOWNLOAD Felucca  chip old
 B DOWNLOAD FM-1 953  chip old
 D DOWNLOAD SLOOP 2.8.5  chip new
 C~ —        likely — confirm by putting it in download mode
── FIRMWARE ──────────────────────────────────────────────────────
 SHRUB.fwsc                                            FM-1_901
 felucca-1.8.2.fwsc                                    FM-1_918
 melodee-1.6.1.fwsc                                FM-1_91061 !
 sloop-2.8.5.fwsc                                      FM-1_900
```

- **Header right:** how many units are addressable, how many labels are unproven.
- **`chip new` / `chip old`:** the newer chip (`0xB36014`) loses saves on older firmware.
  This is informational — you chose to be warned rather than blocked.
- **`—`:** nothing on that port. It may be off, unplugged, or running; all look the same.

### Keys

| Key | Does |
|---|---|
| `↑` `↓` | Move in the focused list |
| `Tab` | Swap between units and firmware |
| `Enter` | Flash (twice: arm, then confirm) |
| `A`–`D` | Confirm a flash for that unit, or pick a sticker while registering |
| `L` | Label the selected **download-mode** unit with a letter |
| `N` | Register a unit by switching it on alone |
| `D` | Send the soft key to the selected unit |
| `R` | Refresh the fleet |
| `Esc` | Cancel / disarm |
| `Q` | Quit the screen |

---

## Starting it, and keeping it started

The screen runs as a systemd service on **tty1**, so it is already showing after a reboot:

```bash
systemctl status fm1-screen.service
```

Two things about that, both learned by deploying it on 2026-11-22:

**getty on tty1 must be masked, not just disabled.** `disable` removes the wants
symlink, but a package upgrade can add it back and then getty and the screen fight over
`/dev/tty1` — which presents as "the screen randomly dies". `mask` makes that impossible.
If you want a shell on the panel instead:

```bash
sudo systemctl stop fm1-screen.service
sudo systemctl unmask getty@tty1.service && sudo systemctl start getty@tty1.service
```

**The service must not require `dev-tty1.device`.** The first version had
`Requires=dev-tty1.device` + `After=dev-tty1.device`, and it deadlocked: with getty
masked nothing ever *activates* tty1, so that device job sat in `start running` forever
and `systemctl start` hung with the screen never launching. `TTYPath=/dev/tty1` alone is
correct — systemd opens the tty itself, and if it is genuinely missing the exec fails and
`Restart=on-failure` reports it.

### The console font

The framebuffer on this Pi is **1024x768** (the Pi negotiates that over HDMI and the
panel scales it), not the panel's native 800x480. At the default 8x16 font that is
128x48 — legible at arm's length only to someone who is not trying to read firmware
names. `/etc/default/console-setup` sets **Terminus 24x12**, which gives **85 columns x
32 rows**:

```bash
stty -F /dev/tty1 size     # 32 85
```

Verify it that way after any font change; do not trust `config.txt` alone.

---

## When something is wrong

**"Unit A is not in download mode."** The write is refused, correctly. Press `D`, or
power-cycle the unit, then `R`.

**A label is wrong.** Put the *right* unit in download mode, select it, press `L`, then
its correct letter. Rebinding reassigns — the old unit gives the sticker up, and the
status line tells you what id it replaced.

**A row shows `?` with an id.** That unit has never been registered. Press `L` (in
download mode) or `N`.

**"no unit id."** That flash does not report its factory id, so it cannot be told apart
from its neighbour. It can still be flashed deliberately while it is the only unit in
download mode, but it cannot be given a sticker safely.

**Nothing appears during registration.** The unit is off, the cable is out, or it drew no
power. Switch it on and try again.

---

## Under the hood, briefly

- Identity: the flash chip's factory id, read by `m-flasher info`.
- Store: `~/fm1-units.json` — four records, rewritten atomically.
- Running hint: USB `bcdDevice`, which tracks the physical unit (verified by swapping
  every cable so each unit changed port). It is a hint because it has not been checked
  across a firmware flash.
- Writes go to `/dev/fm1-<letter>`, bound by USB port through the udev rule, and the
  port is re-read every time — the port is a cache, never an identity.
- Inspect the state without the screen: `fm1-switch units`.

### The soft key is pointed, not a broadcast

An earlier version of these notes claimed one soft-key write moved all MIDI-capable
units at once. **That was wrong.** Measured 2026-11-22 by keying three ports in turn:
each press moved exactly its own port. The soft key is per-port and safe to use on one
unit while others run.

### A unit with no MIDI interface can still take the soft key

Also corrected 2026-11-22: unit C presents as `4d4a:4155` and exposes **no MIDI node**,
yet a soft-key write to its port moved it into download mode anyway. So "no MIDI" is
not a reason to tell the operator to power-cycle — try the soft key first, and fall back
only if the port does not change within ~15 s.

Measured 2026-11-22, the four units:

| Sticker | Port at the time | USB | `bcdDevice` | Soft key works? |
|---|---|---|---|---|
| A | 1-1.1.2 | 1209:0001 | 0329 | yes |
| B | 1-1.1.3 | 1209:0001 | 0320 | yes |
| C | 1-1.2 | 4d4a:4155 | 0104 | **yes**, despite no MIDI |
| D | 1-1.3 | 1209:0001 | 0328 | yes |

### A parser bug worth remembering

`m-flasher info` prints fields as `  label      value` (whitespace). An earlier parser
expected `label: value` and therefore read **nothing at all** against the installed
m-flasher 1.3 — every unit id came back blank for weeks while the tests stayed green,
because the test fixtures were written to match the parser rather than the tool. The
fixture is now the verbatim captured output.
