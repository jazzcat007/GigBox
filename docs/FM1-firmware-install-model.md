# FM-1 custom firmware — install model, evidence, and what's actually verified

> **Kept as evidence (2026-11-21).** Unit flash ids from the original bench are replaced
> with sticker letters. Still the best record of what `m-flasher firmware` does and does not
> write. Package-inspection logic built on it lives in `fm1-switch/fm1_switch/packages.py`.

**Date:** 2026-11-21
**Why this exists:** four units were flashed via `m-flasher firmware` and two of them
(ChoralRoot, Melodee) reported missing data afterwards. This records what the install
actually writes, what each firmware needs beyond that, and — separately from that — what
has been *verified* versus merely *assumed* on each unit. The point is that unit five
should not repeat the guessing.

---

## 1. What `m-flasher firmware` actually writes

From m-flasher's own docs (`docs/usage.md`, "Install firmware (.fwsc)"):

> the firmware from an M-VAVE package (or a custom one in the same format); the unit's
> boot area is kept. **Nothing outside the package is erased.**

> An install **writes the package's flash image and nothing else**: settings, Bluetooth
> pairings, presets and sequences stay as they are, **as with M-VAVE's updater and the
> custom firmwares' own installers.**

Observed behaviour on this fleet — every install reported the same shape:

```
Writes 143 sectors (572 KiB): 0x04000-0x92FFF     ← the app region
```

**The app region is `0x04000–0x92FFF` = 585,728 bytes.** The boot area
(`0x0000–0x3FFF`) is never written by a firmware install, and was confirmed unchanged on
every unit after every flash.

### A correction worth recording

It is tempting to compare `.fwsc` file size against bytes written and conclude data was
dropped. **That comparison is invalid.** A `.fwsc` is a container with framing — headers,
CRCs, metadata — so it is always larger than the image it carries. The delta of ~24 KB
seen on Felucca/SLOOP/OberVave is container overhead, not lost content.

What *is* informative is the **relative size of the delta** (see §2).

---

## 2. The resource-region problem, and which firmwares have it

Some firmwares ship content **outside** the app region — factory sounds, fonts, radio
firmware — which their own installer writes. `m-flasher` cannot: it writes the package's
flash image only.

| Firmware | bytes beyond app region | reported problem on screen? |
|---|---|---|
| Felucca 1.8.2 | +24,558 | no |
| SLOOP 2.8.5 | +24,348 | no |
| OberVave 3.5.2 | +26,495 | no |
| **Melodee 1.6.1** | **+83,220** | **yes — "Setup unfinished / Factory sounds missing"** |
| **ChoralRoot 0.42** | **+90,900** | **yes — "Presets Missing"** |

The two that complained carry roughly **3× the surplus** of the three that did not. That is
consistent with a resource payload that does not fit in the app region.

**Only Melodee documents this explicitly.** Its README:

> Factory sounds, the Rubik font and the Bluetooth radio program live in a resource region
> apart from the application. The update package carries it and **the web installer writes
> it**. If it is missing, the FM-1 says **Setup unfinished** at startup and your own sounds
> stay; press **Finish setup** in the installer.

So the fix for Melodee is **its own web installer**, not `m-flasher`, and not a repair of
`m-flasher`.

### Practical rule for the next firmware

Before flashing a new custom firmware, check its README for any of:

- a "resource region", separate from the application
- "factory sounds … the web installer writes it"
- "Setup unfinished" / "Finish setup" / "install the update again"
- a package notably larger than ~610 KB (the three clean installs were all within 2 KB of
  each other; the two problem ones were 669 KB and 677 KB)

If any is present, plan to run **that firmware's own installer** (Chrome/Edge, over USB),
not just `m-flasher`.

---

### 2.0 CORRECTION (2026-11-21): the size rule is wrong — SHRUB and V15 break it

The section above infers the resource region from package size. **Running the fleet
disproved the size rule.** SHRUB's package is **690 KB** — larger than both firmwares that
failed — and it installs completely. V15 is **700 KB** and installs completely, on both
flash chips.

Parsing the containers instead of comparing their sizes shows why. A `.fwsc` carries a
file list beyond `flash.bin`:

| Package | Files | Installed on hardware |
|---|---|---|
| SLOOP 2.8.5 (610 KB) | `flash.bin`, `ota.bin` | ✅ complete |
| Felucca 1.8.2 (610 KB) | `flash.bin`, `ota.bin` | ✅ complete |
| OberVave 3.5.2 (612 KB) | `flash.bin`, `ota.bin` | ✅ complete |
| **SHRUB (690 KB)** | `flash.bin`, `ota.bin`, `res.bin` 72 KB | ✅ **complete** |
| **V15 (700 KB)** | `flash.bin`, + `USR` 71 KB, `isd_config.ini`, `ota.bin`, … | ✅ **complete** |
| **ChoralRoot 0.42 (677 KB)** | `flash.bin`, `ota.bin`, `res2.bin` 16 KB | ❌ "Presets Missing" |
| **Melodee 1.6.1 (669 KB)** | `flash.bin`, `ota.bin`, `res.bin` 56 KB | ❌ "Factory sounds missing" |

So neither size nor the mere presence of an extra file decides it. What separates them is
**readable sound-bank names inside the payload**: Melodee's `res.bin` contains 35 of them
(`BRASS 1`, `STRINGS 2`…), ChoralRoot's `res2.bin` contains 7 (`TINE EP`, `SOLID BASS`…),
while SHRUB's `res.bin` and V15's `USR` contain none that look like names (V15 yields one
stray `N PAD`).

That is implemented as `fm1-switch inspect FILE` (see `FM1-fleet-web-ui.md` §3) and checked
against all seven of these packages in the test suite. **Use `inspect` before flashing,
not the README heuristic above.**

---

### 2.1 CORRECTION: "browser-only" was often wrong — check for a direct `.fwsc` first

I concluded that SHRUB and Melodee could only be installed from a browser because their
sites present a web installer. **For SHRUB that conclusion was wrong.** The package is
served as a plain file; it was found by probing the installer page's own paths:

```
https://shrub.audio/editor/firmware/SHRUB.fwsc
  689,940 bytes
  sha256 5f4f5604b47a275f5eac1507f6056fff0d655d6493c909257e5109c7ef305b91
```

Installed with `m-flasher firmware` from the Pi, first try, verified by chip CRC32,
**running.** No browser involved.

**Rule: before declaring a firmware "web installer only", get the page and look for a
`.fwsc` path.** The installer page contained the literal string `firmware/SHRUB.fwsc`.
Probing `<site>/editor/firmware/<NAME>.fwsc` and similar took one command.

Note SHRUB publishes **no checksum** — the hash above is our own pin.

**A browser is genuinely required only when the firmware's resources are written by its
installer** (§2). SHRUB wrote `0x04000-0x8FFFF` plus `0x91000-0x91FFF` (deliberately
skipping the sector at `0x90000`) and was complete from `m-flasher` alone; it shows no
setup message. Melodee remains the one that needs its own installer.

## 3. What is VERIFIED on each unit versus ASSUMED

This is the honest ledger. "Installed" is not the same claim as "complete".

| Unit | Firmware | App written + CRC32 | Boots | Save survives power cycle | Resource region |
|---|---|---|---|---|---|
| unit A | Felucca 1.8.2 | ✅ verified | ✅ | ❌ **not tested** | not complained about; **not verified** |
| unit B | OberVave 3.5.2 | ✅ verified | ✅ | ❌ **not tested** | not complained about; **not verified** |
| unit C | **SHRUB (FM-1_901)** | ✅ verified | ✅ **works** | ❌ **not tested** | ✅ complete — no setup message |
| unit D | SLOOP 2.8.5 | ✅ verified | ✅ | ❌ **not tested** | not complained about; **not verified** |

Two gaps, both real:

1. **No unit has been proven to keep a save across a power cycle.** The installs are
   verified at the flash level; the firmwares' own save paths are not. ChoralRoot showed
   that "boots fine" and "data path works" are different questions.
2. **Silence is evidence, not proof.** Felucca, OberVave and SLOOP have not complained,
   and all three carry a normal-sized package — but nobody has confirmed their factory
   content is complete.

---

## 4. Repairs, if wanted

| Unit | To fix | Method |
|---|---|---|
| unit C (Melodee) | factory sounds | Run Melodee's **web installer** and press **Finish setup**. Needs Chrome/Edge + USB; this is a Mac job, not the Pi. |
| any unit | data-region corruption | Install a **different** firmware first, then the target. Reinstalling the same firmware does nothing: `m-flasher` reports "the flash already holds this" and skips. |
| any unit | back to factory | `m-flasher firmware ~/fm1-firmwares/V15-FM-1.fwsc`, or the unit's own archived `.bin` via `restore` (matched by unit id). |

---

## 5. Where this fits with the rest

- `archives/units/REGISTRY.md` — per-unit identity, backups, hashes, firmware history
- the original bench procedure (arrival runbook) is kept privately; the generic install steps are in the top-level `README.md`
- `./verify-units.sh` — re-verifies every archived backup
- `./bundle-archives.sh` — packs them for a second physical medium

The operating rule that cost the most time to learn, and belongs here too:

> **If `m-flasher` reports more units than are physically plugged in, reboot the Pi.**
> Stale USB enumeration kept a disconnected unit in `lsusb`, `/dev/snd` and `aconnect`
> until a reboot. `lsusb` is not proof of presence on this setup.
