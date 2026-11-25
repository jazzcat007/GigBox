# FM-1 fleet — the web UI

> **Partly out of date (written 2026-11-21).** This describes the *expert* UI (`/`), which is
> now secondary to the panel (`/fleet`). Corrections since: writes are addressed by **flash
> id**, not by slot or USB port (commit `46db937`); the soft key has been pointed, not a
> broadcast, in every recorded run since 2026-11-22; "hold OCT−" does not enter download mode
> on these units. The expert page's "Unit A" still means "the unit on udev port slot A",
> not the sticker — see `docs/AUDIT-fleet-manager.md` §7.7.

**Date:** 2026-11-21
**What this is:** the GUI for the fleet. A small web app served *by the Pi*, opened in a
browser on your phone or tablet over Wi-Fi. It lists the four units, the packages on the
Pi, and the whole community catalogue from The Firmware Hub — and it writes firmware to a
unit after a two-step confirmation.

**Why a web UI rather than the curses TUI on the panel:** the catalogue is 277 firmwares
with descriptions, authors and compatibility flags. A 99×30 terminal can show about
eight of them. The web UI costs nothing to run — it is Python's own standard library, so
no browser is installed on the Pi, no framework, no build step, and the 16 GB card and
1 GB of RAM stay comfortable. The Pi's screen stays the bench surface for flashing.

```
http://<pi-hostname>.local:8080      (or http://<pi-address>:8080)
```

---

## 1. What is there

Three sections, top to bottom:

**Units** — always shows all four slots (A–D), whether or not anything is plugged in.
The slot→USB-port binding is what gives a unit a stable name; `fm1-a` is port `1-1.1.3`
regardless of which unit sits there. A unit appears populated only while it is in
**download mode**, because that is the only state in which it can be flashed.

**On this Pi** — every `.fwsc` in `~/fm1-firmwares`, with what the inspector made of it
(§3). Packages the hub does not carry — SHRUB, your own V15 copy — appear here too, and
are marked *not in hub catalogue*.

**Browse the hub** — the community catalogue: 277 firmwares, searchable, filterable by
kind, each card showing author, version, identity pattern, beta status, chip
compatibility, and a one-tap **Download** that verifies the SHA-256 before the file
enters the library.

## 2. The safety model

A firmware write is the one irreversible thing this UI does. Four rules, each of which
exists because of a specific mistake:

1. **Two taps, and the second one names both sides.** The first tap picks a unit; the
   confirmation dialog shows the unit's slot, unit id and flash chip next to the
   package's name, identity and SHA-256. Only the second tap sends the write.
2. **The confirmation is bound to both objects.** The write request carries the slot and
   the SHA-256 the user saw. The server re-hashes the file and refuses if it moved, and
   refuses if the confirmation names a different slot. A stale confirmation cannot be
   replayed against another unit.
3. **No unit is ever guessed.** Writes are addressed by udev symlink. If the slot is
   absent the server says so and stops — it never falls back to "the one it found".
4. **One write at a time.** A lock is held for the duration, and a second request is
   refused rather than queued.

Verified on the live Pi: wrong slot → 409, drifted SHA-256 → 409, absent unit → 409,
path traversal → refused, static traversal → 404.

## 3. The package inspector, and what it learned from this fleet

`fm1-switch inspect FILE` (and every library card) answers one question: **will this
install completely from m-flasher, or does it need its maker's own installer?**

It parses the container rather than guessing. `packages.py` decodes M-VAVE's JieLi UFW
format — the identity woven one character per 48-byte block, the header and file list
under ENC 0xFFFF, `flash.bin` as the flash image, and the app's own file table listing the
data regions (`VM`, `PRCT`, `BTIF`, `USR`). The chip key is recovered exhaustively (it is
16-bit; exactly one candidate survives on every package we have) and comes out as
`0x980F`.

### The false start worth recording

My first rule was **"a package carrying files beyond `flash.bin` + `ota.bin` needs its
own installer."** It fit the data I had at the time: Melodee (`res.bin`) and ChoralRoot
(`res2.bin`) both carried extras and both failed, while Felucca/SLOOP/OberVave carried
none and both installed cleanly.

**SHRUB and V15 broke it.** SHRUB carries a 72 KiB `res.bin` — larger than Melodee's —
and installs completely. V15 carries a 71 KiB `USR` file and installs completely. The
rule would have wrongly warned on the two archives that matter most for recovery.

### What actually separates them

**Readable sound-bank names in the payload.**

| Package | Extra payload | Readable bank names | Installed on hardware |
|---|---|---|---|
| SLOOP 2.8.5 | none | 0 | ✅ complete |
| Felucca 1.8.2 | none | 0 | ✅ complete |
| OberVave 3.5.2 | none | 0 | ✅ complete |
| **SHRUB** | `res.bin` 72 KiB | **0** | ✅ **complete** |
| **V15** | `USR` 71 KiB | **1** (`N PAD`) | ✅ **complete** |
| **ChoralRoot 0.42** | `res2.bin` 16 KiB | **7** (`TINE EP`, `SOLID BASS`…) | ❌ "Presets Missing" |
| **Melodee 1.6.1** | `res.bin` 56 KiB | **35** (`BRASS 1`, `STRINGS 2`…) | ❌ "Factory sounds missing" |

The threshold is ≥3 readable names, which sits far below both failures and above both
successes with extras. The names are shown on the card, so the verdict carries its
evidence rather than asserting a bare judgement.

All seven are checked by `tests/test_web_hub.py::test_real_packages`, so the classifier
cannot silently drift away from what the hardware did. This is a heuristic with a
7-of-7 record, not a proof: a firmware could hide names or store them compressed.

## 4. The hub, and what its catalogue actually means

This app uses The Firmware Hub's published catalogue — `https://thefirmwarehub.com/catalog.json`
— which the hub offers explicitly for tools like this, under terms it states
(`/kb/#catalog`): name the hub with its address, credit each maker and link their
repository, **read the catalogue at most once a day**, and install only what matches its
SHA-256. All four are honoured; attribution is rendered in the UI footer and in the API.

### `chip` is not a compatibility gate — this matters

The catalogue marks 184 firmwares `chip: "new"` and 70 `"old"`. Reading that as
"requires the newer chip" is wrong, and expensive: **Felucca, SLOOP and OberVave are all
marked `new` and all three are running on this fleet's old-chip units right now.**

From the hub's own rescue page, the field means **"knows the newer chip"** — the list of
firmwares that save correctly on a UC25HQ80 unit. So:

* on an **old-chip** unit it is irrelevant — every firmware can write the flash;
* on a **new-chip** unit an `old` firmware keeps saves in RAM only, losing them at
  power-off, and may refuse to update.

The UI therefore warns only for new-chip units, and never blocks. Had it followed the
flag naively it would have forbidden V15 — the recovery image.

### One thing still unverified

m-flasher **merges** a package into flash rather than replacing it. So when an old-chip
firmware is installed over a new-chip-capable one, the chip-support fix in the save path
should survive — the fix is firmware-side, and the merge preserves what is not in the
new image. Our one new-chip unit is working, and it is not going to be risked to find
out. Recorded as `hub.STALE_CAVEAT`, surfaced in the API.

## 5. Performance: why it was slow, and what fixed it

First working version took **over 100 seconds** to list the library on the Pi.

Two causes, both mine:

* **The CRC was bit-at-a-time.** Recovering a 16-bit chip key runs CRC16 tens of thousands
  of times; on the Pi's ARM cores that was **31 s per package**. A table-driven CRC makes
  it 8× faster and is verified against the XMODEM check value `0x31C3`. Now ~8 s per
  package on the Pi, 0.3 s on the Mac.
* **The cache never loaded.** `Path("~/fm1-hub/inspect.json")` was missing
  `expanduser()` on its *default* branch, so it wrote to a literal `./~` directory and
  read nothing back. On top of that, the pruning step split the cache key on `|` and
  matched nothing, quietly emptying the cache after every write.

After the fixes, measured on the Pi: **cold 57 s** (parses all seven once, then never
again), **warm 0.11 s**. The library page is instant.

## 6. Running it

It is installed as a systemd **user** service, so it runs as the operator's own user, never root, and
starts at boot (linger is enabled):

```
systemctl --user status fm1-web
systemctl --user restart fm1-web
journalctl --user -u fm1-web -f
```

Deliberately not sandboxed with `ProtectSystem=strict`/`ProtectHome`: as a *user* unit
those make it fail with `status=226/NAMESPACE` against its own home. It needs no
privilege beyond the udev rule that already grants the operator group access to the
FM-1, so it is granted none.

CLI equivalents, for over SSH:

```
fm1-switch web [--port N]     serve the UI
fm1-switch hub "groove"       search the catalogue
fm1-switch fetch sloop        download one package, SHA-256 verified
fm1-switch inspect FILE       what a package carries and whether it will install
```

## 7. What is NOT done

Honest list, because the green cards could be read as more than they are:

1. **No unit has been proven to keep a save across a power cycle.** Unchanged from
   `FM1-firmware-install-model.md` §3, and still the most valuable test available. The
   UI cannot do it; it needs thumbs.
2. **The GUI has never performed a write on hardware.** Every refusal path is verified
   live, and installs have been done many times via the same `mflasher.install_firmware`,
   but the two-tap flow has not yet completed against a real unit in download mode.
3. **The hub browse is unverified against every entry.** 277 packages exist; 7 have been
   parsed. The ones that matter to this fleet are the 7.
4. **SHRUB is not on the hub** and publishes no checksum, so its copy in the library is
   pinned only to our own hash.
5. **Downloading fetches from the hub's mirror** (`/fw/…`), which is where its catalogue
   points. robots.txt disallows `/fw/` for crawlers; this app fetches only the single
   package a user asks for, once, and never enumerates the directory.
